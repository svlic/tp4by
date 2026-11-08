from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    """One SQLite connection, owned by the single async writer worker."""

    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, event_key TEXT UNIQUE NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
                plan TEXT, error TEXT, report TEXT
            );
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY, event_key TEXT UNIQUE NOT NULL,
                business_no TEXT NOT NULL, origin TEXT NOT NULL, route TEXT NOT NULL,
                media TEXT NOT NULL, source TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS records_number ON records(business_no);
            CREATE TABLE IF NOT EXISTS media_hashes (
                record_id INTEGER NOT NULL REFERENCES records(id), hash TEXT NOT NULL,
                PRIMARY KEY(record_id, hash)
            );
            CREATE INDEX IF NOT EXISTS hash_lookup ON media_hashes(hash);
            CREATE TABLE IF NOT EXISTS manual_operations (
                record_id INTEGER PRIMARY KEY REFERENCES records(id), seeds TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS down_hashes (hash TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS archives (
                record_id INTEGER NOT NULL REFERENCES records(id), channel TEXT NOT NULL,
                messages TEXT NOT NULL, PRIMARY KEY(record_id, channel)
            );
            CREATE TABLE IF NOT EXISTS up_groups (
                signature TEXT PRIMARY KEY, members TEXT NOT NULL, messages TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cursors (
                chat INTEGER PRIMARY KEY, message INTEGER NOT NULL, floor INTEGER NOT NULL
            );
        """)

    def enqueue(self, payload: dict) -> bool:
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO jobs(event_key,payload) VALUES (?,?)",
                (payload["event_key"], encode(payload)),
            )
        return cursor.rowcount == 1

    def next_job(self):
        # Do not overtake a sleeping retry: plans assume sequential business events.
        row = self.db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
        return row if row and row["next_at"] <= time.time() else None

    def records(self) -> dict[int, dict]:
        result = {}
        for row in self.db.execute("SELECT * FROM records ORDER BY id"):
            item = dict(row)
            item["media"] = json.loads(item["media"])
            item["source"] = json.loads(item["source"])
            item["hashes"] = {media["key"] for media in item["media"]}
            result[item["id"]] = item
        return result

    def close(self):
        self.db.close()
