import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from tp4by import app as app_module
from tp4by.app import Application
from tp4by.config import CHANNELS, Settings
from tp4by.storage import Store


async def test_offline_replay_is_chronological_and_ignores_own_posts(tmp_path, monkeypatch):
    chats = {name: -(1000 + index) for index, name in enumerate(CHANNELS)}

    def message(chat, mid, second, key, group=None, forward=False):
        return SimpleNamespace(
            chat_id=chats[chat],
            id=mid,
            date=datetime(2026, 1, 1, 0, 0, second, tzinfo=UTC),
            grouped_id=group,
            message="123456",
            fwd_from=object() if forward else None,
            key=key,
        )

    raw = [
        message("from", 50, 0, "ignored-pre-baseline", 66),
        message("from", 101, 1, "A", 77),
        message("up", 201, 2, "B", forward=True),
        message("from", 102, 3, "Z", 78),
        message("up", 202, 4, "ignored-system-output"),
    ]
    history = {chat: [m for m in raw if m.chat_id == chat] for chat in chats.values()}
    store = Store(tmp_path / "tp4by.sqlite3")
    with store.db:
        store.db.executemany("INSERT INTO cursors VALUES (?,?,?)", [(chat, 100, 100) for chat in chats.values()])
    store.close()

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            pass

        async def disconnect(self):
            pass

        async def get_peer_id(self, chat):
            return chat

        def on(self, event):
            return lambda callback: callback

        async def get_messages(self, chat, limit):
            return history[chat][-limit:]

        async def iter_messages(self, chat, min_id, max_id, reverse):
            for m in history[chat]:
                if min_id < m.id < max_id:
                    yield m

    class Transport:
        def __init__(self, *args):
            pass

        async def prepare(self, payload):
            m = next(m for m in raw if m.chat_id == payload["chat_id"] and m.id == payload["message_ids"][0])
            return [{"key": "image:phash:" + m.key, "kind": "image", "path": m.key}]

        async def send(self, channel, spec, token):
            return [abs(hash(token)) % 100000 + 1]

        async def delete(self, channel, ids):
            pass

    monkeypatch.setattr(app_module, "TelegramClient", Client)
    monkeypatch.setattr(app_module, "TelethonGateway", Transport)
    app = Application(Settings(1, "fake-hash", tmp_path, chats))

    async def run_until_done():
        async with asyncio.timeout(5):
            while True:
                await asyncio.sleep(0.01)
                if hasattr(app, "store"):
                    count = app.store.db.execute("SELECT COUNT(*) FROM jobs WHERE status='done'").fetchone()[0]
                    if count == 3:
                        app.stop.set()
                        return

    await asyncio.gather(app.run(), run_until_done())
    store = Store(tmp_path / "tp4by.sqlite3")
    try:
        assert [(r["origin"], r["route"]) for r in store.records().values()] == [
            ("from", "up"),
            ("up", "up"),
            ("from", "oneshot"),
        ]
        # Reconnect overlap must not import anything below the original first-start floor.
        assert all(row[0] == 100 for row in store.db.execute("SELECT floor FROM cursors"))
    finally:
        store.close()
