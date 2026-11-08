from __future__ import annotations

import asyncio
import fcntl
import logging
import signal
from collections import defaultdict

from telethon import TelegramClient, events

from .config import Settings
from .engine import Engine
from .gateway import TelethonGateway
from .storage import Store

log = logging.getLogger(__name__)


def input_payload(origin: str, messages: list) -> dict:
    messages = sorted(messages, key=lambda m: m.id)
    first = messages[0]
    group = f"g{first.grouped_id}" if first.grouped_id else f"m{first.id}"
    return {
        "event_key": f"{first.chat_id}:{group}",
        "origin": origin,
        "chat_id": first.chat_id,
        "message_ids": [m.id for m in messages],
        "text": "\n".join(m.message for m in messages if m.message),
        "grouped": first.grouped_id is not None,
    }


class Application:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.stop = asyncio.Event()

    async def run(self):
        settings = self.settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        # flock is released by the OS even on abrupt termination.
        with (settings.data_dir / "writer.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("此数据目录已有 Writer 运行；禁止多实例共享") from error
            self.store = Store(settings.data_dir / "tp4by.sqlite3")
            client = TelegramClient(
                str(settings.data_dir / "writer"), settings.api_id, settings.api_hash, flood_sleep_threshold=0
            )
            try:
                await client.start()
                chats = {name: await client.get_peer_id(chat) for name, chat in settings.chats.items()}
                if len(set(chats.values())) != 6:
                    raise ValueError("解析后的六个频道必须互不相同")
                self.engine = Engine(
                    self.store, TelethonGateway(client, chats, settings.data_dir / "media"), settings.retries
                )
                origins = {chats[name]: name for name in ("from", "up", "down")}
                catching_up = True
                buffered = []

                def ingest(messages):
                    origin = origins[messages[0].chat_id]
                    if origin != "from" and not any(m.fwd_from for m in messages):
                        return  # Never ingest our own generated UP/DOWN posts.
                    self.store.enqueue(input_payload(origin, messages))

                def receive(messages):
                    if catching_up:
                        buffered.append(messages)
                    else:
                        ingest(messages)

                @client.on(events.Album(chats=list(origins)))
                async def album(event):
                    receive(event.messages)

                @client.on(events.NewMessage(chats=list(origins)))
                async def single(event):
                    if event.message.grouped_id is None:
                        receive([event.message])

                # Persisted high-water marks plus overlap recover events missed while offline.
                # First start establishes a baseline rather than ingesting all channel history.
                backlog, checkpoints = [], []
                for chat in origins:
                    row = self.store.db.execute("SELECT message,floor FROM cursors WHERE chat=?", (chat,)).fetchone()
                    latest = await client.get_messages(chat, limit=1)
                    high = latest[0].id if latest else 0
                    if row:
                        grouped = defaultdict(list)
                        async for message in client.iter_messages(
                            chat, min_id=max(row[1], row[0] - 20), max_id=high + 1, reverse=True
                        ):
                            key = ("g", message.grouped_id) if message.grouped_id else ("m", message.id)
                            grouped[key].append(message)
                        for messages in grouped.values():
                            backlog.append(messages)
                    checkpoints.append((chat, max(row[0], high) if row else high, row[1] if row else high))
                # Do not let a live manual operation overtake history discovered during startup.
                for messages in sorted(backlog + buffered, key=lambda group: (group[0].date, group[0].id)):
                    ingest(messages)
                with self.store.db:
                    self.store.db.executemany("INSERT OR REPLACE INTO cursors VALUES (?,?,?)", checkpoints)
                catching_up = False
                log.info("T4BY started: single Writer, six channels; database=%s", settings.data_dir)
                while not self.stop.is_set():
                    await self.engine.tick()
                    try:
                        await self.engine.report_failure()
                    except Exception:
                        # MAN unavailable: retain durable report and retry, never lose the business error.
                        log.exception("MAN delivery failed; pending report retained")
                        await asyncio.sleep(5)
                    try:
                        await asyncio.wait_for(self.stop.wait(), timeout=0.5)
                    except TimeoutError:
                        pass
            finally:
                await client.disconnect()
                self.store.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    async def run():
        app = Application(Settings.from_env())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, app.stop.set)
        await app.run()

    asyncio.run(run())
