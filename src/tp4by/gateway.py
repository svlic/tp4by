"""Telegram I/O with persistent originals and deterministic send identifiers.

The caller owns retries (including FloodWait). Telegram only remembers random_ids
for a bounded server-side window: deterministic IDs reduce duplicate sends, but
cannot promise exactly-once delivery after arbitrarily delayed retries. An
ambiguous RPC result is an error, never a claimed successful delivery.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import tempfile
from pathlib import Path

import imagehash
from PIL import Image, UnidentifiedImageError
from telethon import functions, types, utils


def _random_id(token: str, offset: int) -> int:
    value = hashlib.sha256(b"tp4by-send\0" + token.encode("utf-8") + b"\0" + str(offset).encode("ascii")).digest()[:8]
    return int.from_bytes(value, "big", signed=True) or 1


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def _cached(path: Path) -> dict | None:
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if Path(value["path"]).is_file() else None


def _store_original(source: Path, root: Path, video: bool, filename: str, video_info: dict) -> dict:
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    sha = digest.hexdigest()
    try:
        with Image.open(source) as image:
            # Do not disable Pillow's decompression-bomb limit. Decode the full
            # image, including documents with incorrect or generic MIME types.
            image.load()
            key = f"image:phash:{imagehash.phash(image, hash_size=8)}"
            mime = Image.MIME.get(image.format, "application/octet-stream")
        kind = "image"
    except UnidentifiedImageError as error:
        if not video:
            raise ValueError("attachment is not a supported image or video") from error
        kind = "video"
        key = f"video:sha256:{sha}"
        mime = mimetypes.guess_type(filename)[0] or "video/mp4"
    # A digest directory retains a video's original filename without treating
    # untrusted Telegram filenames as paths. The original bytes are never edited.
    filename = Path(filename.replace("\\", "/")).name
    if filename in {"", ".", ".."}:
        filename = "video.mp4" if kind == "video" else "original"
    destination = root / "objects" / sha / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(source, destination)
    directory = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    if kind == "video" and video_info:
        _atomic_json(root / "video" / f"{sha}.json", video_info)
    return {"key": key, "kind": kind, "path": str(destination), "mime": mime}


def _video_attributes(path: Path, root: Path) -> list:
    attributes = [types.DocumentAttributeFilename(path.name)]
    metadata = root / "video" / f"{path.parent.name}.json"
    if metadata.exists():
        info = json.loads(metadata.read_text(encoding="utf-8"))
        attributes.append(types.DocumentAttributeVideo(**info))
    return attributes


def _readable_copy(item: dict) -> Path:
    # copies are supplied in business order, NOT quality/resolution order.
    for name in [*item.get("copies", []), item["path"]]:
        path = Path(name)
        try:
            with path.open("rb") as stream:
                stream.read(1)
            return path
        except OSError:
            continue
    raise FileNotFoundError("no readable cached copy of media")


def _sent_ids(result: object, random_ids: list[int], peer: object) -> list[int]:
    if isinstance(result, types.UpdateShortSentMessage):
        if len(random_ids) == 1 and result.id > 0:
            return [result.id]
        raise RuntimeError("ambiguous short send response")
    updates = getattr(result, "updates", None)
    if updates is None and isinstance(result, types.UpdateShort):
        updates = [result.update]
    mapping: dict[int, int] = {}
    messages: set[int] = set()
    for update in updates or []:
        if isinstance(update, types.UpdateMessageID) and update.random_id in random_ids:
            old = mapping.get(update.random_id)
            if old is not None and old != update.id:
                raise RuntimeError("conflicting send random_id mappings")
            mapping[update.random_id] = update.id
        elif isinstance(update, (types.UpdateNewMessage, types.UpdateNewChannelMessage)):
            message = update.message
            if getattr(message, "out", False) and utils.get_peer_id(message.peer_id) == utils.get_peer_id(peer):
                messages.add(message.id)
    if len(mapping) == len(random_ids):
        ids = [mapping[value] for value in random_ids]
        if all(value > 0 for value in ids) and len(set(ids)) == len(ids):
            return ids
    # A single outbound message for the requested peer in the RPC response is
    # unambiguous. Album order must never be inferred from update ordering.
    if not mapping and len(random_ids) == 1 and len(messages) == 1:
        value = next(iter(messages))
        if value > 0:
            return [value]
    raise RuntimeError("incomplete or ambiguous Telegram send response")


class TelethonGateway:
    def __init__(self, client, chats: dict[str, int], media_dir: Path):
        self.client = client
        self.chats = dict(chats)
        self.media_dir = Path(media_dir).resolve()

    async def prepare(self, payload: dict) -> list[dict]:
        chat_id = payload["chat_id"]
        ids = payload["message_ids"]
        if payload["origin"] not in {"from", "up", "down"}:
            raise ValueError("invalid origin")
        if not isinstance(chat_id, int) or not ids or any(not isinstance(i, int) or i <= 0 for i in ids):
            raise ValueError("chat_id and nonempty positive message_ids required")
        album = {}
        if payload.get("grouped", False):
            requested = await self.client.get_messages(chat_id, ids=list(ids))
            requested = [raw for raw in requested if raw is not None and not isinstance(raw, types.MessageEmpty)]
            album = {raw.id: raw for raw in requested}
            if any(message_id not in album for message_id in ids):
                raise LookupError("requested album members are not available")
            grouped_id = getattr(album[ids[0]], "grouped_id", None)
            if grouped_id is None or any(getattr(raw, "grouped_id", None) != grouped_id for raw in requested):
                raise LookupError("requested messages no longer form the expected album")
            neighbors = await self.client.get_messages(
                chat_id,
                ids=list(range(max(1, ids[0] - 10), ids[0] + 11)),
            )
            album.update(
                {raw.id: raw for raw in neighbors if raw is not None and getattr(raw, "grouped_id", None) == grouped_id}
            )
            ids = sorted(album)
            payload["message_ids"] = ids
            payload["text"] = "\n".join(raw.message for message_id in ids if (raw := album[message_id]).message)
        results = []
        for message_id in ids:
            manifest = self.media_dir / "sources" / str(chat_id) / f"{message_id}.json"
            cached = await asyncio.to_thread(_cached, manifest)
            if cached is not None:
                results.append(cached)
                continue
            raw = album[message_id] if album else await self.client.get_messages(chat_id, ids=message_id)
            if raw is None or isinstance(raw, types.MessageEmpty):
                raise LookupError(f"message {chat_id}/{message_id} is not available")
            if not getattr(raw, "media", None):
                raise ValueError(f"message {chat_id}/{message_id} has no media")
            document = getattr(raw, "document", None)
            if not (getattr(raw, "photo", None) or document or getattr(raw, "video", None)):
                raise ValueError("unsupported attachment")
            mime = getattr(document, "mime_type", "") or ""
            video = bool(getattr(raw, "video", None)) or mime.lower().startswith("video/")
            filename = "video.mp4" if video else "original"
            video_info = {}
            for attribute in getattr(document, "attributes", []):
                if isinstance(attribute, types.DocumentAttributeFilename):
                    filename = attribute.file_name
                elif isinstance(attribute, types.DocumentAttributeVideo):
                    video_info = {
                        "duration": attribute.duration,
                        "w": attribute.w,
                        "h": attribute.h,
                        "supports_streaming": bool(attribute.supports_streaming),
                    }
            await asyncio.to_thread(self.media_dir.mkdir, parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".download-", dir=self.media_dir) as directory:
                target = Path(directory) / "original"
                downloaded = await self.client.download_media(raw, file=str(target))
                if not downloaded:
                    raise OSError(f"download failed for {chat_id}/{message_id}")
                result = await asyncio.to_thread(
                    _store_original, Path(downloaded), self.media_dir, video, filename, video_info
                )
                result["as_document"] = result["kind"] == "image" and not bool(getattr(raw, "photo", None))
                await asyncio.to_thread(_atomic_json, manifest, result)
                results.append(result)
        return results

    async def send(self, channel: str, spec: dict, token: str) -> list[int]:
        peer = await self.client.get_input_entity(self.chats[channel])
        if spec["type"] == "forward":
            ids = spec["message_ids"]
            if not ids or any(not isinstance(i, int) or i <= 0 for i in ids):
                raise ValueError("forward requires positive message_ids")
            source = await self.client.get_input_entity(spec["chat_id"])
            random_ids = [_random_id(token, index) for index in range(len(ids))]
            result = await self.client(
                functions.messages.ForwardMessagesRequest(
                    from_peer=source,
                    id=ids,
                    random_id=random_ids,
                    to_peer=peer,
                    drop_author=False,
                    drop_media_captions=False,
                )
            )
            return _sent_ids(result, random_ids, peer)
        if spec["type"] == "text":
            random_ids = [_random_id(token, 0)]
            result = await self.client(
                functions.messages.SendMessageRequest(
                    peer=peer,
                    message=spec["text"],
                    random_id=random_ids[0],
                    no_webpage=True,
                )
            )
            return _sent_ids(result, random_ids, peer)
        if spec["type"] != "media":
            raise ValueError("unknown send type")
        items = spec["items"]
        if not 1 <= len(items) <= 10:
            raise ValueError("media group must contain 1 to 10 items")
        if any(item["kind"] not in {"image", "video"} for item in items):
            raise ValueError("unknown media kind")
        if len({bool(item.get("as_document")) for item in items}) > 1:
            raise ValueError("document and photo/video albums must be sent separately")
        random_ids = [_random_id(token, index) for index in range(len(items))]
        media = []
        for item in items:
            path = await asyncio.to_thread(_readable_copy, item)
            photo = item["kind"] == "image" and not item.get("as_document")
            # Photo caches are named "original"; Telegram requires an image
            # extension on the uploaded file, including for pre-existing caches.
            filename = path.name
            if photo:
                filename = "photo" + (mimetypes.guess_extension(item.get("mime") or "image/jpeg") or ".jpg")
            uploaded = await self.client.upload_file(str(path), file_name=filename)
            if photo:
                value = types.InputMediaUploadedPhoto(file=uploaded)
            else:
                mime = item.get("mime") or mimetypes.guess_type(path.name)[0] or "video/mp4"
                attributes = (
                    await asyncio.to_thread(_video_attributes, path, self.media_dir)
                    if item["kind"] == "video"
                    else [types.DocumentAttributeFilename(path.name)]
                )
                value = types.InputMediaUploadedDocument(
                    file=uploaded,
                    mime_type=mime,
                    attributes=attributes,
                    force_file=bool(item.get("as_document")),
                )
            if len(items) > 1:
                # Uploaded media must first become persistent server media
                # before it can be referenced inside SendMultiMediaRequest.
                resolved = await self.client(functions.messages.UploadMediaRequest(peer=peer, media=value))
                if photo:
                    value = types.InputMediaPhoto(id=utils.get_input_photo(resolved.photo))
                else:
                    value = types.InputMediaDocument(id=utils.get_input_document(resolved.document))
            media.append(value)
        if len(items) == 1:
            result = await self.client(
                functions.messages.SendMediaRequest(
                    peer=peer,
                    media=media[0],
                    message=spec.get("caption", ""),
                    random_id=random_ids[0],
                )
            )
        else:
            result = await self.client(
                functions.messages.SendMultiMediaRequest(
                    peer=peer,
                    multi_media=[
                        types.InputSingleMedia(
                            media=value,
                            random_id=random_ids[index],
                            message=spec.get("caption", "") if index == 0 else "",
                        )
                        for index, value in enumerate(media)
                    ],
                )
            )
        return _sent_ids(result, random_ids, peer)

    async def delete(self, channel: str, ids: list[int]) -> None:
        if ids:
            await self.client.delete_messages(self.chats[channel], ids, revoke=True)


# Keep the short name usable by callers that depend only on the gateway contract.
Gateway = TelethonGateway
