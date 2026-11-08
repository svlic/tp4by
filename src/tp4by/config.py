from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CHANNELS = ("from", "oneshot", "repeat", "up", "down", "man")


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    data_dir: Path
    chats: dict[str, int | str]
    retries: int = 5

    @classmethod
    def from_env(cls):
        def required(name):
            value = os.environ.get(f"T4BY_{name}", "").strip()
            if not value:
                raise ValueError(f"缺少环境变量 T4BY_{name}")
            return value

        chats = {}
        for name in CHANNELS:
            raw = required(f"{name.upper()}_CHAT")
            chats[name] = int(raw) if raw.lstrip("-").isdigit() else raw
        if len(set(chats.values())) != 6:
            raise ValueError("六个频道必须互不相同")
        retries = int(os.environ.get("T4BY_RETRIES", "5"))
        api_id = int(required("WRITER_API_ID"))
        if retries < 1 or api_id < 1:
            raise ValueError("API ID 和重试上限必须是正整数")
        return cls(api_id, required("WRITER_API_HASH"), Path(os.getenv("T4BY_DATA_DIR", "data")), chats, retries)
