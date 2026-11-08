from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Protocol

from telethon.errors import FloodWaitError, SlowModeWaitError

from .storage import Store, encode

log = logging.getLogger(__name__)
NUMBER = re.compile(r"(?<!\d)[0-9]{6,7}(?!\d)")


class Gateway(Protocol):
    async def prepare(self, payload: dict) -> list[dict]: ...
    async def send(self, channel: str, spec: dict, token: str) -> list[int]: ...
    async def delete(self, channel: str, ids: list[int]) -> None: ...


def business_number(text: str) -> str:
    numbers = set(NUMBER.findall(text))
    if len(numbers) != 1:
        raise ValueError("必须且只能包含一个不同的 6/7 位业务编号")
    return numbers.pop()


def expand(records: dict[int, dict], seeds: set[int], links=()) -> set[int]:
    """Number links are explicit historical edges, never a live number rule."""
    selected = set(seeds)
    hashes = set().union(*(records[r]["hashes"] for r in selected)) if selected else set()
    while True:
        added = {rid for rid, r in records.items() if r["hashes"] & hashes} - selected
        for link in links:
            if selected.intersection(link):
                added.update(set(link) & records.keys() - selected)
        if not added:
            return selected
        selected.update(added)
        hashes.update(*(records[r]["hashes"] for r in added))


def presentation(records: list[dict]) -> tuple[list[dict], str]:
    copies = {}
    for record in records:
        for item in record["media"]:
            copies.setdefault(item["key"], []).append(item["path"])
    media, numbers = [], []
    for record in records:
        if record["business_no"] not in numbers:
            numbers.append(record["business_no"])
        for item in record["media"]:
            if item["key"] in copies:
                # Persist all copies; gateway can try the next if the earliest cache is unavailable.
                media.append({**item, "copies": list(dict.fromkeys(copies.pop(item["key"])))})
    return media, " ".join(numbers)


def batches(media: list[dict], caption: str) -> list[dict]:
    long_caption = len(caption.encode("utf-16-le")) // 2 > 1024
    result = []
    for item in media:
        # Documents cannot share a Telegram album with photos/videos. Preserve order.
        if (
            not result
            or len(result[-1]["items"]) == 10
            or bool(result[-1]["items"][0].get("as_document")) != bool(item.get("as_document"))
        ):
            result.append({"type": "media", "items": [], "caption": "" if long_caption else caption})
        result[-1]["items"].append(item)
    if long_caption:
        text = ""
        for number in caption.split():
            if len(text) + len(number) + 1 > 4096:
                result.append({"type": "text", "text": text})
                text = ""
            text = f"{text} {number}".strip()
        if text:
            result.append({"type": "text", "text": text})
    return result


class Engine:
    def __init__(self, store: Store, gateway: Gateway, retries: int = 5):
        self.store, self.gateway, self.retries = store, gateway, retries

    async def tick(self) -> bool:
        job = self.store.next_job()
        if job is None:
            return False
        payload = json.loads(job["payload"])
        try:
            if job["plan"] is None:
                media = await self.gateway.prepare(payload)
                number = business_number(payload.get("text", ""))
                if payload["origin"] == "from" and not payload.get("grouped"):
                    raise ValueError("FROM 输入必须是完整组消息")
                if not media:
                    raise ValueError("输入必须包含可处理的图片或视频")
                with self.store.db:
                    plan = self._plan(job["id"], payload, number, media)
                    self.store.db.execute(
                        "UPDATE jobs SET plan=?,payload=? WHERE id=?", (encode(plan), encode(payload), job["id"])
                    )
            else:
                plan = json.loads(job["plan"])
            await self._execute(job["id"], plan)
        except (FloodWaitError, SlowModeWaitError) as error:
            with self.store.db:
                self.store.db.execute(
                    "UPDATE jobs SET next_at=?,error=?,payload=? WHERE id=?",
                    (time.time() + error.seconds + 1, str(error), encode(payload), job["id"]),
                )
        except Exception as error:
            log.exception("Job %s failed", job["id"])
            attempts = job["attempts"] + 1
            terminal = isinstance(error, ValueError) or attempts >= self.retries
            with self.store.db:
                self.store.db.execute(
                    "UPDATE jobs SET attempts=?,next_at=?,error=?,status=?,payload=? WHERE id=?",
                    (
                        attempts,
                        0 if terminal else time.time() + min(300, 2**attempts),
                        str(error),
                        "failed" if terminal else "pending",
                        encode(payload),
                        job["id"],
                    ),
                )
        return True

    def _plan(self, job_id: int, payload: dict, number: str, media: list[dict]) -> dict:
        db = self.store.db
        old = self.store.records()
        hashes = {m["key"] for m in media}
        down = {row[0] for row in db.execute("SELECT hash FROM down_hashes")}
        up = set().union(*(r["hashes"] for r in old.values() if r["route"] == "up"))
        history = set().union(*(r["hashes"] for r in old.values()))
        origin = payload["origin"]
        route = origin
        if hashes & down:
            route = "down"  # Even manual UP cannot override an existing DOWN hash.
        elif origin == "from":
            route = "up" if hashes & up else "repeat" if hashes & history else "oneshot"
        rid = db.execute(
            "INSERT INTO records(event_key,business_no,origin,route,media,source) VALUES (?,?,?,?,?,?)",
            (payload["event_key"], number, origin, route, encode(media), encode(payload)),
        ).lastrowid
        db.executemany("INSERT INTO media_hashes VALUES (?,?)", [(rid, h) for h in hashes])
        records = self.store.records()
        seeds = {rid}
        if origin != "from":
            seeds.update(r for r, item in old.items() if item["business_no"] == number)
            db.execute("INSERT INTO manual_operations VALUES (?,?)", (rid, encode(sorted(seeds))))
        links = [
            set(json.loads(row[0]))
            for row in db.execute(
                "SELECT seeds FROM manual_operations m JOIN records r ON r.id=m.record_id WHERE r.origin='up'"
            )
        ]
        if route == "down":
            selected, pending = set(), list(seeds)
            while pending:
                r = pending.pop()
                if r in selected:
                    continue
                selected.add(r)
                db.execute("UPDATE records SET route='down' WHERE id=?", (r,))
                for h in records[r]["hashes"]:
                    added = db.execute("INSERT OR IGNORE INTO down_hashes VALUES (?)", (h,)).rowcount
                    if added:
                        pending.extend(
                            row[0] for row in db.execute("SELECT record_id FROM media_hashes WHERE hash=?", (h,))
                        )
        elif route == "up":
            eligible = {r: item for r, item in records.items() if item["route"] != "down"}
            selected = expand(eligible, seeds & eligible.keys(), links)
            for r in selected:
                db.execute("UPDATE records SET route='up' WHERE id=?", (r,))
        records = self.store.records()
        plan = {"sends": [], "groups": [], "archives": [], "cleanup": []}

        def send(channel, spec):
            index = len(plan["sends"])
            plan["sends"].append(
                {"channel": channel, "spec": spec, "ids": None, "token": f"{payload['event_key']}:{job_id}:{index}"}
            )
            return index

        for r, item in records.items():
            channel = item["route"]
            if (
                channel == "up"
                or db.execute("SELECT 1 FROM archives WHERE record_id=? AND channel=?", (r, channel)).fetchone()
            ):
                continue
            if channel == "down" and item["origin"] == "down":
                plan["archives"].append(
                    {"rid": r, "channel": channel, "ids": item["source"]["message_ids"], "steps": []}
                )
            else:
                steps = [send(channel, spec) for spec in batches(item["media"], item["source"].get("text", ""))]
                plan["archives"].append({"rid": r, "channel": channel, "ids": [], "steps": steps})

        old_groups = {row["signature"]: dict(row) for row in db.execute("SELECT * FROM up_groups")}
        remaining = {r: item for r, item in records.items() if item["route"] == "up"}
        while remaining:
            component = expand(remaining, {min(remaining)}, links)
            items, caption = presentation([remaining[r] for r in sorted(component)])
            signature = hashlib.sha256(encode([caption, [m["key"] for m in items]]).encode()).hexdigest()
            previous = old_groups.get(signature)
            plan["groups"].append(
                {
                    "signature": signature,
                    "members": sorted(component),
                    "ids": json.loads(previous["messages"]) if previous else [],
                    "steps": [] if previous else [send("up", spec) for spec in batches(items, caption)],
                }
            )
            for r in component:
                del remaining[r]
        retained = {g["signature"] for g in plan["groups"]}
        for signature, group in old_groups.items():
            if signature not in retained:
                plan["cleanup"].append({"channel": "up", "ids": json.loads(group["messages"])})
        for row in db.execute("SELECT * FROM archives WHERE channel='oneshot'"):
            if records[row["record_id"]]["route"] in ("up", "down"):
                plan["cleanup"].append(
                    {"channel": "oneshot", "ids": json.loads(row["messages"]), "rid": row["record_id"]}
                )
        # Only replace manual UP inputs after the entire new presentation has succeeded.
        for r, item in records.items():
            if item["origin"] == "up" and item["route"] == "up" and not item["source"].get("replaced"):
                plan["cleanup"].append({"channel": "up", "ids": item["source"]["message_ids"], "manual_rid": r})
        return plan

    async def _execute(self, job_id: int, plan: dict):
        db = self.store.db
        for step in plan["sends"]:
            if step["ids"] is not None:
                continue
            step["ids"] = await self.gateway.send(step["channel"], step["spec"], step["token"])
            with db:
                db.execute("UPDATE jobs SET plan=? WHERE id=?", (encode(plan), job_id))
        if not plan.get("published"):
            with db:
                for archive in plan["archives"]:
                    ids = archive["ids"] + [mid for i in archive["steps"] for mid in plan["sends"][i]["ids"]]
                    db.execute(
                        "INSERT OR IGNORE INTO archives VALUES (?,?,?)",
                        (archive["rid"], archive["channel"], encode(ids)),
                    )
                db.execute("DELETE FROM up_groups")
                for group in plan["groups"]:
                    ids = group["ids"] + [mid for i in group["steps"] for mid in plan["sends"][i]["ids"]]
                    db.execute(
                        "INSERT INTO up_groups VALUES (?,?,?)",
                        (group["signature"], encode(group["members"]), encode(ids)),
                    )
                plan["published"] = True
                db.execute("UPDATE jobs SET plan=? WHERE id=?", (encode(plan), job_id))
        for cleanup in plan["cleanup"]:
            if cleanup.get("done"):
                continue
            await self.gateway.delete(cleanup["channel"], cleanup["ids"])
            cleanup["done"] = True
            with db:
                if "rid" in cleanup:
                    db.execute("DELETE FROM archives WHERE record_id=? AND channel='oneshot'", (cleanup["rid"],))
                if "manual_rid" in cleanup:
                    rid = cleanup["manual_rid"]
                    source = json.loads(db.execute("SELECT source FROM records WHERE id=?", (rid,)).fetchone()[0])
                    source["replaced"] = True
                    db.execute("UPDATE records SET source=? WHERE id=?", (encode(source), rid))
                db.execute("UPDATE jobs SET plan=? WHERE id=?", (encode(plan), job_id))
        with db:
            db.execute("UPDATE jobs SET status='done' WHERE id=?", (job_id,))

    async def report_failure(self) -> bool:
        row = self.store.db.execute(
            "SELECT * FROM jobs WHERE status='failed' AND report IS NULL AND next_at<=? ORDER BY id LIMIT 1",
            (time.time(),),
        ).fetchone()
        if row is None:
            return False
        payload = json.loads(row["payload"])
        message = f"T4BY 异常\n事件：{row['event_key']}\n错误：{row['error']}\n原文：{payload.get('text', '')}"
        # Notification and media each use stable tokens and independent persisted progress.
        plan = json.loads(row["plan"]) if row["plan"] else {}
        specs = [
            {"type": "text", "text": message[:2000]},
            {"type": "forward", "chat_id": payload["chat_id"], "message_ids": payload["message_ids"]},
        ]
        for i, spec in enumerate(specs):
            key = f"man:{i}"
            if key not in plan:
                try:
                    plan[key] = await self.gateway.send("man", spec, f"{row['event_key']}:man:{i}")
                except Exception as error:
                    delay = error.seconds + 1 if isinstance(error, (FloodWaitError, SlowModeWaitError)) else 60
                    log.exception("MAN unavailable for event %s; retry in %s seconds", row["event_key"], delay)
                    with self.store.db:
                        self.store.db.execute("UPDATE jobs SET next_at=? WHERE id=?", (time.time() + delay, row["id"]))
                    return True
                with self.store.db:
                    self.store.db.execute("UPDATE jobs SET plan=? WHERE id=?", (encode(plan), row["id"]))
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET report='sent' WHERE id=?", (row["id"],))
        return True
