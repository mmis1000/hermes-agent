import inspect
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import (
    DiscordAdapter,
    _serialize_discord_attachments,
)


def _attachment(attachment_id, filename, *, content_type="application/octet-stream"):
    return SimpleNamespace(
        id=attachment_id,
        filename=filename,
        size=123,
        content_type=content_type,
        url=f"https://cdn.discordapp.test/{filename}",
        proxy_url=f"https://media.discordapp.test/{filename}",
        width=512,
        height=704,
    )


def _client_for(channel):
    return SimpleNamespace(
        get_channel=MagicMock(return_value=channel),
        fetch_channel=AsyncMock(),
        http=SimpleNamespace(request=AsyncMock()),
    )


def _aiohttp_session(response):
    response_ctx = MagicMock()
    response_ctx.__aenter__ = AsyncMock(return_value=response)
    response_ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=response_ctx)
    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    return session_ctx


def test_discord_media_methods_accept_metadata_kwarg():
    for method_name in (
        "send_voice",
        "send_image_file",
        "send_image",
        "send_multiple_images",
        "send_video",
        "send_document",
    ):
        signature = inspect.signature(getattr(DiscordAdapter, method_name))
        assert "metadata" in signature.parameters, method_name


def test_attachment_metadata_is_bounded_and_whitelisted():
    attachments = [
        {
            "id": index,
            "filename": "x" * 5000,
            "size": 123,
            "content_type": "image/png",
            "url": f"https://cdn.discordapp.test/{index}",
            "proxy_url": f"https://media.discordapp.test/{index}",
            "width": 10,
            "height": 20,
            "token": "must-not-leak",
            "nested": {"arbitrary": "adapter data"},
        }
        for index in range(25)
    ]

    serialized = _serialize_discord_attachments(attachments)

    assert len(serialized) == 10
    assert set(serialized[0]) == {
        "id",
        "filename",
        "size",
        "content_type",
        "url",
        "proxy_url",
        "width",
        "height",
    }
    assert serialized[0]["id"] == "0"
    assert len(serialized[0]["filename"]) <= 1024
    assert "token" not in serialized[0]
    assert "nested" not in serialized[0]


@pytest.mark.asyncio
async def test_send_video_routes_to_thread_and_surfaces_attachment_metadata(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x18ftyp" + b"\x00" * 64)

    attachment = _attachment(91, "clip.mp4", content_type="video/mp4")
    sent_msg = SimpleNamespace(id=333, attachments=[attachment])
    channel = MagicMock(id=7777)
    channel.send = AsyncMock(return_value=sent_msg)
    adapter._client = _client_for(channel)

    result = await adapter.send_video(
        chat_id="9001",
        video_path=str(video_path),
        metadata={"thread_id": "7777"},
    )

    adapter._client.get_channel.assert_called_once_with(7777)
    channel.send.assert_awaited_once()
    assert result.success is True
    assert result.message_id == "333"
    assert result.raw_response == {
        "channel_id": "7777",
        "thread_id": "7777",
        "attachments": [
            {
                "id": "91",
                "filename": "clip.mp4",
                "size": 123,
                "content_type": "video/mp4",
                "url": "https://cdn.discordapp.test/clip.mp4",
                "proxy_url": "https://media.discordapp.test/clip.mp4",
                "width": 512,
                "height": 704,
            }
        ],
    }
    assert adapter._last_self_message_id["7777"] == "333"


@pytest.mark.asyncio
async def test_send_voice_routes_native_upload_to_thread(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"not-a-real-ogg")

    channel = MagicMock(id=7777)
    adapter._client = _client_for(channel)
    adapter._client.http.request.return_value = {
        "id": "voice-msg",
        "channel_id": "7777",
        "attachments": [
            {
                "id": "voice-att",
                "filename": "voice-message.ogg",
                "size": 14,
                "content_type": "audio/ogg",
                "url": "https://cdn.discordapp.test/voice.ogg",
            }
        ],
    }

    result = await adapter.send_voice(
        chat_id="9001",
        audio_path=str(audio_path),
        metadata={"thread_id": "7777"},
    )

    adapter._client.get_channel.assert_called_once_with(7777)
    assert result.success is True
    assert result.raw_response["channel_id"] == "7777"
    assert result.raw_response["thread_id"] == "7777"
    assert result.raw_response["attachments"][0]["id"] == "voice-att"


@pytest.mark.asyncio
async def test_send_remote_image_routes_to_thread(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    channel = MagicMock(id=7777)
    channel.send = AsyncMock(
        return_value=SimpleNamespace(
            id="image-msg",
            attachments=[_attachment("image-att", "image.png", content_type="image/png")],
        )
    )
    adapter._client = _client_for(channel)

    response = SimpleNamespace(
        status=200,
        headers={"content-type": "image/png"},
        read=AsyncMock(return_value=b"png"),
    )
    fake_aiohttp = SimpleNamespace(
        ClientSession=MagicMock(return_value=_aiohttp_session(response)),
        ClientTimeout=lambda **kwargs: kwargs,
    )
    with patch("plugins.platforms.discord.adapter.is_safe_url", return_value=True), \
         patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
        result = await adapter.send_image(
            chat_id="9001",
            image_url="https://images.example.test/plan.png",
            metadata={"thread_id": "7777"},
        )

    adapter._client.get_channel.assert_called_once_with(7777)
    assert result.success is True
    assert result.raw_response["thread_id"] == "7777"
    assert result.raw_response["attachments"][0]["filename"] == "image.png"


@pytest.mark.asyncio
async def test_send_multiple_images_routes_batch_to_thread_and_returns_metadata(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    paths = []
    for index in range(2):
        path = tmp_path / f"image-{index}.png"
        path.write_bytes(b"png")
        paths.append(path)

    channel = MagicMock(id=7777)
    channel.send = AsyncMock(
        return_value=SimpleNamespace(
            id="batch-msg",
            attachments=[
                _attachment("att-1", "image-0.png", content_type="image/png"),
                _attachment("att-2", "image-1.png", content_type="image/png"),
            ],
        )
    )
    adapter._client = _client_for(channel)

    result = await adapter.send_multiple_images(
        chat_id="9001",
        images=[(path.as_uri(), "") for path in paths],
        metadata={"thread_id": "7777"},
    )

    adapter._client.get_channel.assert_called_once_with(7777)
    channel.send.assert_awaited_once()
    assert result.success is True
    assert result.message_id == "batch-msg"
    assert result.raw_response["channel_id"] == "7777"
    assert result.raw_response["thread_id"] == "7777"
    assert [item["id"] for item in result.raw_response["attachments"]] == ["att-1", "att-2"]


@pytest.mark.asyncio
async def test_send_document_preserves_forum_parent_creation_and_metadata(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    document = tmp_path / "report.pdf"
    document.write_bytes(b"pdf")

    starter = SimpleNamespace(
        id="starter-msg",
        attachments=[_attachment("forum-att", "report.pdf", content_type="application/pdf")],
    )
    thread_channel = SimpleNamespace(id=4242, send=AsyncMock())
    forum = MagicMock(id=9001)
    forum.create_thread = AsyncMock(
        return_value=SimpleNamespace(thread=thread_channel, message=starter)
    )
    adapter._client = _client_for(forum)
    adapter._is_forum_parent = MagicMock(return_value=True)

    result = await adapter.send_document(chat_id="9001", file_path=str(document))

    adapter._client.get_channel.assert_called_once_with(9001)
    forum.create_thread.assert_awaited_once()
    assert result.success is True
    assert result.message_id == "starter-msg"
    assert result.raw_response["channel_id"] == "4242"
    assert result.raw_response["thread_id"] == "4242"
    assert result.raw_response["attachments"][0]["id"] == "forum-att"
