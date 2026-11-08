import copy
import json

import pytest
from telethon.errors import FloodWaitError

from tp4by import engine as engine_module
from tp4by.engine import Engine, business_number, presentation
from tp4by.storage import Store


@pytest.mark.parametrize(
    "text,expected",
    [
        ("123456 123456", "123456"),
        ("a1234567!", "1234567"),
        ("012345", "012345"),
        ("", None),
        ("123456 7654321", None),
        ("12345 12345678", None),
        ("１２３４５６", None),
        ("١123456", None),
        ("123456٧", None),
    ],
)
def test_business_number_uniqueness_and_digit_boundaries(text, expected):
    if expected is None:
        with pytest.raises(ValueError, match="必须且只能包含一个不同的 6/7 位业务编号"):
            business_number(text)
    else:
        assert business_number(text) == expected


def test_presentation_preserves_first_metadata_and_unique_copy_order_without_mutating_input():
    records = [
        {"business_no": "7654321", "media": [{"key": "B", "path": "b-old", "as_document": True}]},
        {
            "business_no": "123456",
            "media": [{"key": "A", "path": "a"}, {"key": "B", "path": "b-new", "as_document": False}],
        },
        {"business_no": "7654321", "media": [{"key": "B", "path": "b-old"}, {"key": "C", "path": "c"}]},
    ]
    original = copy.deepcopy(records)
    assert presentation(records) == (
        [
            {"key": "B", "path": "b-old", "as_document": True, "copies": ["b-old", "b-new"]},
            {"key": "A", "path": "a", "copies": ["a"]},
            {"key": "C", "path": "c", "copies": ["c"]},
        ],
        "7654321 123456",
    )
    assert records == original
    assert presentation([]) == ([], "")


class FakeGateway:
    def __init__(self):
        self.sent = []
        self.deleted = []
        self.accepted = {}
        self.fail = None
        self.uncertain = False

    async def prepare(self, payload):
        return [
            {
                "key": f"image:phash:{key}",
                "kind": "image",
                "path": f"{payload['event_key']}/{key}",
                "as_document": key in payload.get("documents", []),
            }
            for key in payload["keys"]
        ]

    async def send(self, channel, spec, token):
        if self.fail and self.fail(channel, spec):
            raise OSError("injected network failure")
        if token not in self.accepted:
            ids = list(range(10000 + len(self.sent) * 10, 10000 + len(self.sent) * 10 + len(spec.get("items", [1]))))
            self.accepted[token] = ids
            self.sent.append((channel, copy.deepcopy(spec), ids))
            if self.uncertain:
                self.uncertain = False
                raise OSError("accepted remotely but connection lost")
        return self.accepted[token]

    async def delete(self, channel, ids):
        self.deleted.append((channel, ids))

    def media(self, channel):
        return [spec for target, spec, _ in self.sent if target == channel and spec["type"] == "media"]


@pytest.fixture
def system(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    gateway = FakeGateway()
    yield store, gateway, Engine(store, gateway)
    store.close()


async def add(system, number, keys, origin="from", event=None, text=None, documents=()):
    store, _, engine = system
    count = store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    payload = {
        "event_key": event or f"event-{count}",
        "origin": origin,
        "text": text or number,
        "keys": keys,
        "chat_id": -123,
        "message_ids": [count + 1],
        "grouped": True,
        "documents": list(documents),
    }
    store.enqueue(payload)
    assert await engine.tick()
    return payload


def routes(store):
    return [r["route"] for r in store.records().values()]


def keys(spec):
    return [m["key"].split(":")[-1] for m in spec["items"]]


async def test_repeat_only_archives_new_input_and_event_replay_is_noop(system):
    store, gateway, engine = system
    first = await add(system, "123456", ["A", "B"])
    await add(system, "234567", ["B", "C"])
    await add(system, "345678", ["A", "D"])
    assert routes(store) == ["oneshot", "repeat", "repeat"]
    assert [keys(s) for s in gateway.media("oneshot")] == [["A", "B"]]
    assert [keys(s) for s in gateway.media("repeat")] == [["B", "C"], ["A", "D"]]
    assert not gateway.deleted
    assert not store.enqueue(first)
    assert not await engine.tick()
    assert len(gateway.sent) == 3


async def test_manual_number_once_then_hash_closure_and_persistent_up_links(system):
    store, gateway, _ = system
    await add(system, "123456", ["C"])
    await add(system, "234567", ["C", "D"])
    await add(system, "345678", ["D", "E"])
    await add(system, "234567", ["F"])  # Number discovered via hash MUST NOT expand this branch.
    await add(system, "123456", ["A", "B"], "up")
    assert routes(store) == ["up", "up", "up", "oneshot", "up"]
    assert keys(gateway.media("up")[-1]) == ["C", "D", "E", "A", "B"]
    assert gateway.media("up")[-1]["caption"] == "123456 234567 345678"
    assert len(gateway.deleted) == 2  # first ONESHOT + manual input, not old REPEATs
    await add(system, "123456", ["Z"])
    assert routes(store)[-1] == "oneshot"
    await add(system, "456789", ["B", "G"])
    assert keys(gateway.media("up")[-1]) == ["C", "D", "E", "A", "B", "G"]
    assert gateway.media("up")[-1]["items"][0]["path"] == "event-0/C"


async def test_down_closure_incremental_and_manual_original_preserved(system):
    store, gateway, _ = system
    await add(system, "222222", ["B", "C"])
    await add(system, "333333", ["C", "D"])
    manual = await add(system, "111111", ["A", "B"], "down")
    assert routes(store) == ["down", "down", "down"]
    assert [keys(s) for s in gateway.media("down")] == [["B", "C"], ["C", "D"]]
    assert ("down", manual["message_ids"]) not in gateway.deleted
    await add(system, "444444", ["D", "E"])
    assert sorted(r[0] for r in store.db.execute("SELECT hash FROM down_hashes")) == [
        f"image:phash:{k}" for k in "ABCDE"
    ]
    assert [keys(s) for s in gateway.media("down")] == [["B", "C"], ["C", "D"], ["D", "E"]]
    assert [target for target, _ in gateway.deleted] == ["oneshot"]


async def test_down_beats_up_for_entire_record_without_number_link_spill(system):
    store, gateway, _ = system
    await add(system, "123456", ["A"])
    await add(system, "123456", ["B"], "up")  # Explicit disconnected number relation.
    await add(system, "222222", ["A"], "down")
    assert routes(store) == ["down", "up", "down"]
    assert keys(gateway.media("up")[-1]) == ["B"]
    await add(system, "333333", ["A", "B", "C"])
    assert routes(store) == ["down", "down", "down", "down"]
    assert keys(gateway.media("down")[-1]) == ["A", "B", "C"]
    assert store.db.execute("SELECT COUNT(*) FROM up_groups").fetchone()[0] == 0


@pytest.mark.parametrize("origin", ["from", "up", "down"])
@pytest.mark.parametrize(
    "incoming,from_route",
    [
        (["X"], "oneshot"),
        (["H", "X"], "repeat"),
        (["U", "X"], "up"),
        (["D", "X"], "down"),
        (["H", "U", "X"], "up"),
        (["H", "D", "X"], "down"),
        (["U", "D", "X"], "down"),
        (["H", "U", "D", "X"], "down"),
    ],
)
async def test_routing_priority_for_from_and_manual_inputs(system, origin, incoming, from_route):
    store, gateway, _ = system
    await add(system, "111111", ["H"])
    await add(system, "222222", ["U"], "up")
    await add(system, "333333", ["D"], "down")
    expected = from_route if origin == "from" else "down" if "D" in incoming else origin
    sent_before = len(gateway.sent)
    payload = await add(system, "444444", incoming, origin)
    assert routes(store)[-1] == expected
    assert store.db.execute("SELECT status FROM jobs ORDER BY id DESC LIMIT 1").fetchone()[0] == "done"
    if origin == "down":
        archive = store.db.execute("SELECT messages FROM archives WHERE record_id=4 AND channel='down'").fetchone()
        assert json.loads(archive[0]) == payload["message_ids"]
        assert ("down", payload["message_ids"]) not in gateway.deleted
    else:
        assert any(target == expected for target, _, _ in gateway.sent[sent_before:])


async def test_up_merges_two_groups_and_skips_unchanged_display(system):
    store, gateway, _ = system
    await add(system, "123456", ["A", "B"], "up")
    await add(system, "234567", ["C", "D"], "up")
    old_ids = [ids for target, _, ids in gateway.sent if target == "up"]
    await add(system, "345678", ["B", "C", "E"])
    assert keys(gateway.media("up")[-1]) == ["A", "B", "C", "D", "E"]
    assert all(("up", ids) in gateway.deleted for ids in old_ids)
    assert store.db.execute("SELECT COUNT(*) FROM up_groups").fetchone()[0] == 1
    count = len(gateway.sent)
    await add(system, "123456", ["A", "B"])
    assert len(gateway.sent) == count
    members = json.loads(store.db.execute("SELECT members FROM up_groups").fetchone()[0])
    assert members == [1, 2, 3, 4]


async def test_partial_publish_keeps_old_version_and_resumes_after_restart(system, tmp_path):
    store, gateway, engine = system
    await add(system, "123456", ["A"], "up")
    await add(system, "234567", [str(i) for i in range(10)])
    old_group = tuple(store.db.execute("SELECT * FROM up_groups").fetchone())
    old_deleted = list(gateway.deleted)
    gateway.fail = lambda target, spec: target == "up" and len(spec.get("items", [])) == 2
    await add(system, "345678", ["A", "0", "Z"])
    assert tuple(store.db.execute("SELECT * FROM up_groups").fetchone()) == old_group
    assert gateway.deleted == old_deleted
    assert store.db.execute("SELECT COUNT(*) FROM archives WHERE channel='oneshot'").fetchone()[0] == 1
    first_batch = gateway.media("up")[-1]
    assert len(first_batch["items"]) == 10
    store.close()
    recovered = Store(tmp_path / "state.sqlite3")
    try:
        engine = Engine(recovered, gateway)
        gateway.fail = None
        with recovered.db:
            recovered.db.execute("UPDATE jobs SET next_at=0")
        await engine.tick()
        assert [len(s["items"]) for s in gateway.media("up")] == [1, 10, 2]
        assert gateway.media("up")[-2]["caption"] == gateway.media("up")[-1]["caption"]
        assert gateway.media("up")[-1]["caption"] == "123456 234567 345678"
        assert recovered.db.execute("SELECT COUNT(*) FROM archives WHERE channel='oneshot'").fetchone()[0] == 0
        assert len(gateway.deleted) > len(old_deleted)
    finally:
        recovered.close()
        # Fixture still owns original closed connection; sqlite close is idempotent.


async def test_uncertain_send_uses_same_token_and_does_not_duplicate(system):
    store, gateway, engine = system
    gateway.uncertain = True
    await add(system, "123456", ["A", "B"])
    assert len(gateway.sent) == 1
    assert not gateway.deleted
    with store.db:
        store.db.execute("UPDATE jobs SET next_at=0")
    await engine.tick()
    assert len(gateway.sent) == 1
    assert store.db.execute("SELECT status FROM jobs").fetchone()[0] == "done"
    assert len(store.records()) == 1


async def test_long_numbers_sent_as_boundary_split_text_not_captions(system):
    _, gateway, _ = system
    for i in range(600):
        await add(system, str(1000000 + i), ["A"])
    await add(system, "1000000", ["A"], "up")
    assert gateway.media("up")[-1]["caption"] == ""
    texts = [spec["text"] for target, spec, _ in gateway.sent if target == "up" and spec["type"] == "text"]
    assert len(texts) == 2
    assert all(len(text) <= 4096 for text in texts)
    assert " ".join(texts).split() == [str(1000000 + i) for i in range(600)]


@pytest.mark.parametrize("text", ["no number", "12345 12345678", "123456 7654321"])
async def test_business_errors_deliver_original_and_notice_to_man(system, text):
    store, gateway, engine = system
    payload = await add(system, "", ["A"], text=text)
    assert not store.records()
    assert await engine.report_failure()
    assert [spec["type"] for target, spec, _ in gateway.sent if target == "man"] == ["text", "forward"]
    assert gateway.sent[-1][1]["message_ids"] == payload["message_ids"]
    assert not await engine.report_failure()


async def test_retry_exhaustion_reports_and_preserves_oneshot(system):
    store, gateway, engine = system
    engine.retries = 2
    await add(system, "123456", ["A"])
    gateway.fail = lambda target, spec: target == "up"
    await add(system, "123456", ["B"], "up")
    with store.db:
        store.db.execute("UPDATE jobs SET next_at=0")
    await engine.tick()
    assert store.db.execute("SELECT status FROM jobs ORDER BY id DESC LIMIT 1").fetchone()[0] == "failed"
    assert not gateway.deleted
    assert store.db.execute("SELECT COUNT(*) FROM archives WHERE channel='oneshot'").fetchone()[0] == 1
    assert await engine.report_failure()
    assert gateway.sent[-1][0] == "man"


async def test_floodwait_and_man_retry_respect_wait_and_keep_progress(system, monkeypatch):
    store, gateway, engine = system
    clock = [1000.0]
    monkeypatch.setattr(engine_module.time, "time", lambda: clock[0])
    original_send = gateway.send
    raised = False

    async def send(channel, spec, token):
        nonlocal raised
        if not raised:
            raised = True
            raise FloodWaitError(request=None, capture=10)
        return await original_send(channel, spec, token)

    gateway.send = send
    await add(system, "123456", ["A"])
    row = store.db.execute("SELECT * FROM jobs").fetchone()
    assert row["next_at"] == 1011
    assert row["attempts"] == 0
    clock[0] = 1010
    assert not await engine.tick()
    clock[0] = 1011
    assert await engine.tick()
    assert len(gateway.sent) == 1
    await add(system, "", ["B"], text="missing number")
    gateway.fail = lambda target, spec: spec["type"] == "forward"
    assert await engine.report_failure()
    assert [s[1]["type"] for s in gateway.sent if s[0] == "man"] == ["text"]
    assert not await engine.report_failure()
    clock[0] += 60
    gateway.fail = None
    assert await engine.report_failure()
    assert [s[1]["type"] for s in gateway.sent if s[0] == "man"] == ["text", "forward"]


async def test_up_splits_document_albums_without_reordering_or_losing_captions(system):
    _, gateway, _ = system
    await add(system, "123456", ["P", "D1", "D2", "Q"], "up", documents=["D1", "D2"])
    assert [keys(spec) for spec in gateway.media("up")] == [["P"], ["D1", "D2"], ["Q"]]
    assert [spec["caption"] for spec in gateway.media("up")] == ["123456"] * 3
