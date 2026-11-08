import asyncio
import hashlib
import io
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import imagehash
import pytest
from PIL import Image
from telethon import functions, types

from tp4by.gateway import Gateway, TelethonGateway, _sent_ids


def run(coroutine):
    return asyncio.run(coroutine)


def picture():
    image = Image.new("RGBA", (64, 48), (30, 100, 200, 128))
    for x in range(32):
        for y in range(24):
            image.putpixel((x, y), (x * 7, y * 9, 20, 255))
    stream = io.BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


def message(data, *, photo=False, mime=None, filename="clip.mp4", video=False):
    document = (
        None
        if mime is None
        else SimpleNamespace(mime_type=mime, attributes=[types.DocumentAttributeFilename(filename)])
    )
    return SimpleNamespace(
        media=object(),
        photo=photo,
        document=document,
        video=video,
        data=data,
    )


def payload(ids):
    return {"chat_id": -100123, "message_ids": ids, "origin": "from", "event_key": "e1"}


class FakeClient:
    def __init__(self, messages=None):
        self.messages = messages or {}
        self.fetched = []
        self.requests = []
        self.uploads = []
        self.deleted = []
        self.response = None
        self.error = None

    async def get_messages(self, chat, ids):
        self.fetched.append((chat, ids))
        if isinstance(ids, list):
            return [self.messages.get(message_id) for message_id in ids]
        return self.messages.get(ids)

    async def download_media(self, raw, file):
        Path(file).write_bytes(raw.data)
        return file

    async def get_input_entity(self, chat):
        assert chat == -100123
        return types.InputPeerChannel(123, 456)

    async def upload_file(self, file):
        path = Path(file)
        self.uploads.append((path, path.read_bytes()))
        return types.InputFile(len(self.uploads), 1, path.name, "digest")

    async def delete_messages(self, chat, ids, revoke):
        self.deleted.append((chat, ids, revoke))

    async def __call__(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        if isinstance(request, functions.messages.UploadMediaRequest):
            if isinstance(request.media, types.InputMediaUploadedPhoto):
                photo = types.Photo(
                    id=10,
                    access_hash=20,
                    file_reference=b"ref",
                    date=datetime.now(UTC),
                    sizes=[],
                    dc_id=1,
                )
                return types.MessageMediaPhoto(photo=photo)
            document = types.Document(
                id=11,
                access_hash=21,
                file_reference=b"ref",
                date=datetime.now(UTC),
                mime_type="video/mp4",
                size=10,
                dc_id=1,
                attributes=request.media.attributes,
            )
            return types.MessageMediaDocument(document=document)
        if self.response is not None:
            return self.response
        if isinstance(request, functions.messages.ForwardMessagesRequest):
            ids = request.random_id
        elif isinstance(request, functions.messages.SendMultiMediaRequest):
            ids = [item.random_id for item in request.multi_media]
        else:
            ids = [request.random_id]
        # Deliberately shuffled: ordering must follow request random IDs.
        return SimpleNamespace(
            updates=list(
                reversed(
                    [types.UpdateMessageID(id=100 + index, random_id=random_id) for index, random_id in enumerate(ids)]
                )
            )
        )


def gateway(tmp_path, client):
    return Gateway(client, {"up": -100123}, tmp_path / "media")


def test_image_documents_use_decoded_pixels_and_original_cache(tmp_path):
    data = picture()
    client = FakeClient(
        {
            1: message(data, photo=True),
            2: message(data, mime="application/octet-stream", filename="mislabelled.bin"),
        }
    )
    gw = gateway(tmp_path, client)
    items = run(gw.prepare(payload([2, 1])))
    with Image.open(io.BytesIO(data)) as image:
        expected = f"image:phash:{imagehash.phash(image, hash_size=8)}"
    assert [item["key"] for item in items] == [expected, expected]
    assert all(item["kind"] == "image" for item in items)
    for item in items:
        assert Path(item["path"]).read_bytes() == data
        assert Path(item["path"]).parent.name == hashlib.sha256(data).hexdigest()
    assert [entry[1] for entry in client.fetched] == [2, 1]
    client.messages.clear()
    # A new Gateway instance proves the index is durable, not in-memory.
    assert run(gateway(tmp_path, client).prepare(payload([2, 1]))) == items
    assert len(client.fetched) == 2


@pytest.mark.parametrize("mime,raw_video", [("video/mp4", False), ("application/octet-stream", True)])
def test_video_sha256_and_filename(tmp_path, mime, raw_video):
    data = b"synthetic video bytes, not an image"
    raw = message(data, mime=mime, video=raw_video, filename="../../movie.mp4")
    raw.document.attributes.append(
        types.DocumentAttributeVideo(
            duration=3,
            w=100,
            h=80,
            supports_streaming=True,
        )
    )
    client = FakeClient({7: raw})
    gw = gateway(tmp_path, client)
    (item,) = run(gw.prepare(payload([7])))
    assert item["key"] == "video:sha256:" + hashlib.sha256(data).hexdigest()
    assert item["kind"] == "video"
    assert Path(item["path"]).name == "movie.mp4"
    assert Path(item["path"]).read_bytes() == data
    assert run(gw.send("up", {"type": "media", "items": [item], "caption": "c"}, "v")) == [100]
    attributes = client.requests[-1].media.attributes
    assert attributes[0].file_name == "movie.mp4"
    assert attributes[1].supports_streaming
    assert client.uploads[-1][1] == data
    assert Path(item["path"]).exists()


@pytest.mark.parametrize(
    "raw",
    [
        SimpleNamespace(media=None),
        SimpleNamespace(media=object(), photo=None, document=None, video=None),
        message(b"a PDF, sticker, audio or arbitrary document", mime="application/pdf"),
    ],
)
def test_unsupported_media_rejected(tmp_path, raw):
    with pytest.raises(ValueError):
        run(gateway(tmp_path, FakeClient({1: raw})).prepare(payload([1])))


@pytest.mark.parametrize("raw", [None, types.MessageEmpty(id=1, peer_id=types.PeerChannel(123))])
def test_missing_source_is_technical_lookup_error(tmp_path, raw):
    with pytest.raises(LookupError):
        run(gateway(tmp_path, FakeClient({1: raw})).prepare(payload([1])))


def test_pixel_bomb_limit_is_not_disabled(tmp_path, monkeypatch):
    data = picture()
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(Image.DecompressionBombError):
        run(gateway(tmp_path, FakeClient({1: message(data, photo=True)})).prepare(payload([1])))
    assert Image.MAX_IMAGE_PIXELS == 100


def test_mixed_album_raw_requests_stable_ids_caption_and_originals(tmp_path):
    client = FakeClient(
        {
            1: message(picture(), photo=True),
            2: message(b"video bytes", mime="video/mp4", filename="movie.mp4"),
        }
    )
    gw = gateway(tmp_path, client)
    items = run(gw.prepare(payload([1, 2])))
    spec = {"type": "media", "items": items, "caption": "**literal** <b>caption</b>"}
    assert run(gw.send("up", spec, "album-token")) == [100, 101]
    assert run(gateway(tmp_path, client).send("up", spec, "album-token")) == [100, 101]
    requests = [r for r in client.requests if isinstance(r, functions.messages.SendMultiMediaRequest)]
    first, second = requests
    first_ids = [item.random_id for item in first.multi_media]
    assert first_ids == [item.random_id for item in second.multi_media]
    assert len(set(first_ids)) == 2
    assert all(-(2**63) <= i < 2**63 and i != 0 for i in first_ids)
    assert [item.message for item in first.multi_media] == [spec["caption"], ""]
    assert all(not item.entities for item in first.multi_media)
    assert isinstance(first.multi_media[0].media, types.InputMediaPhoto)
    assert isinstance(first.multi_media[1].media, types.InputMediaDocument)
    assert client.uploads[0][1] == picture()
    assert all(Path(item["path"]).exists() for item in items)
    assert Path(items[0]["path"]).read_bytes() == picture()


def test_single_photo_and_text_use_raw_stable_random_ids(tmp_path):
    client = FakeClient({1: message(picture(), photo=True)})
    gw = gateway(tmp_path, client)
    items = run(gw.prepare(payload([1])))
    assert run(gw.send("up", {"type": "media", "items": items, "caption": "raw"}, "photo")) == [100]
    request = client.requests[-1]
    assert isinstance(request, functions.messages.SendMediaRequest)
    assert isinstance(request.media, types.InputMediaUploadedPhoto)
    assert request.message == "raw" and not request.entities
    spec = {"type": "text", "text": "<b>literal</b>"}
    assert run(gw.send("up", spec, "text")) == [100]
    first = client.requests[-1]
    assert run(gw.send("up", spec, "text")) == [100]
    assert first.random_id == client.requests[-1].random_id
    assert first.message == spec["text"] and not first.entities
    run(gw.send("up", spec, "another-token"))
    assert first.random_id != client.requests[-1].random_id


@pytest.mark.parametrize("size", [0, 11])
def test_album_size_rejected(tmp_path, size):
    client = FakeClient()
    with pytest.raises(ValueError):
        run(gateway(tmp_path, client).send("up", {"type": "media", "items": [{}] * size}, "token"))
    assert not client.requests and not client.uploads


def test_result_correlation_and_ambiguity():
    peer = types.InputPeerChannel(123, 456)
    short = types.UpdateShortSentMessage(id=42, pts=1, pts_count=1, date=datetime.now(UTC))
    assert _sent_ids(short, [1], peer) == [42]
    message = types.Message(id=43, peer_id=types.PeerChannel(123), out=True, message="x")
    update = types.UpdateNewChannelMessage(message=message, pts=1, pts_count=1)
    assert _sent_ids(SimpleNamespace(updates=[update]), [1], peer) == [43]
    user_message = types.Message(id=44, peer_id=types.PeerUser(5), out=True, message="x")
    assert _sent_ids(
        SimpleNamespace(
            updates=[
                types.UpdateNewMessage(
                    message=user_message,
                    pts=1,
                    pts_count=1,
                )
            ]
        ),
        [1],
        types.InputPeerUser(5, 9),
    ) == [44]
    for updates in [
        [],
        [update],
        [types.UpdateMessageID(id=20, random_id=1)],
        [types.UpdateMessageID(id=20, random_id=1), types.UpdateMessageID(id=21, random_id=1)],
        [types.UpdateMessageID(id=20, random_id=1), types.UpdateMessageID(id=20, random_id=2)],
    ]:
        with pytest.raises(RuntimeError):
            _sent_ids(SimpleNamespace(updates=updates), [1, 2], peer)


def test_send_failure_is_not_retried_and_delete_is_repeatable(tmp_path):
    client = FakeClient()
    client.error = OSError("connection lost")
    gw = gateway(tmp_path, client)
    with pytest.raises(OSError, match="connection lost"):
        run(gw.send("up", {"type": "text", "text": "x"}, "token"))
    assert len(client.requests) == 1
    run(gw.delete("up", []))
    assert not client.deleted
    run(gw.delete("up", [10, 11]))
    run(gw.delete("up", [10, 11]))
    assert client.deleted == [(-100123, [10, 11], True)] * 2


def test_forward_has_stable_random_ids_and_duplicate_ack_is_uncertain(tmp_path):
    assert Gateway is TelethonGateway
    client = FakeClient()
    gw = gateway(tmp_path, client)
    spec = {"type": "forward", "chat_id": -100123, "message_ids": [8, 9]}
    assert run(gw.send("up", spec, "manual")) == [100, 101]
    assert run(gw.send("up", spec, "manual")) == [100, 101]
    first, second = client.requests
    assert isinstance(first, functions.messages.ForwardMessagesRequest)
    assert first.id == [8, 9]
    assert first.random_id == second.random_id
    assert len(set(first.random_id)) == 2
    client.response = SimpleNamespace(updates=[])
    with pytest.raises(RuntimeError, match="ambiguous"):
        run(gw.send("up", spec, "manual"))
    assert not client.deleted


def test_earliest_readable_copy_is_used_not_highest_quality(tmp_path):
    early = tmp_path / "small.mp4"
    later = tmp_path / "large.mp4"
    early.write_bytes(b"small original")
    later.write_bytes(b"a much larger later copy" * 100)
    client = FakeClient()
    gw = gateway(tmp_path, client)
    item = {
        "kind": "video",
        "path": str(later),
        "copies": [str(tmp_path / "missing.mp4"), str(early), str(later)],
    }
    spec = {"type": "media", "items": [item], "caption": ""}
    run(gw.send("up", spec, "earliest"))
    assert client.uploads[-1][1] == early.read_bytes()
    early.unlink()
    run(gw.send("up", spec, "fallback"))
    assert client.uploads[-1][1] == later.read_bytes()
    later.unlink()
    with pytest.raises(FileNotFoundError):
        run(gw.send("up", spec, "missing"))


def test_grouped_prepare_collects_neighbors_and_complete_captions(tmp_path):
    data = picture()
    messages = {}
    for message_id, group, text in [(19, 77, "12345"), (20, 77, ""), (21, 77, "more"), (22, 88, "other")]:
        raw = message(data, photo=True)
        raw.id, raw.grouped_id, raw.message = message_id, group, text
        messages[message_id] = raw
    client = FakeClient(messages)
    event = {**payload([20]), "grouped": True, "text": ""}
    items = run(gateway(tmp_path, client).prepare(event))
    assert event["message_ids"] == [19, 20, 21]
    assert event["text"] == "12345\nmore"
    assert len(items) == 3
    assert client.fetched == [(-100123, [20]), (-100123, list(range(10, 31)))]
    event = {**payload([19, 25]), "grouped": True}
    with pytest.raises(LookupError):
        run(gateway(tmp_path, client).prepare(event))
    event = {**payload([19, 22]), "grouped": True}
    with pytest.raises(LookupError, match="no longer form the expected album"):
        run(gateway(tmp_path, client).prepare(event))


def test_grouped_unsupported_media_still_enriches_manual_forward_payload(tmp_path):
    raw = message(b"arbitrary file", mime="application/pdf")
    raw.id, raw.grouped_id, raw.message = 1, 77, "bad number"
    event = {**payload([1]), "grouped": True}
    with pytest.raises(ValueError):
        run(gateway(tmp_path, FakeClient({1: raw})).prepare(event))
    assert event["message_ids"] == [1]
    assert event["text"] == "bad number"


def test_image_document_upload_preserves_original_and_transport_type(tmp_path):
    data = picture()
    client = FakeClient({1: message(data, mime="image/png", filename="image.png")})
    gw = gateway(tmp_path, client)
    items = run(gw.prepare(payload([1])))
    assert items[0]["as_document"]
    assert run(gw.send("up", {"type": "media", "items": items, "caption": "123456"}, "doc")) == [100]
    request = client.requests[-1]
    assert isinstance(request.media, types.InputMediaUploadedDocument)
    assert request.media.mime_type == "image/png"
    assert request.media.force_file
    assert client.uploads[-1][1] == data
