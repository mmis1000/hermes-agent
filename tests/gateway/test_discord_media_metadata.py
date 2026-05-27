import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


def test_discord_media_methods_accept_metadata_kwarg():
    for method_name in ("send_voice", "send_image_file", "send_image", "send_video", "send_document"):
        signature = inspect.signature(getattr(DiscordAdapter, method_name))
        assert "metadata" in signature.parameters, method_name


@pytest.mark.asyncio
async def test_send_video_routes_to_thread_and_surfaces_attachment_metadata(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))

    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x18ftyp" + b"\x00" * 64)

    attachment = SimpleNamespace(
        id=91,
        filename="clip.mp4",
        size=video_path.stat().st_size,
        content_type="video/mp4",
        url="https://cdn.discordapp.com/attachments/clip.mp4",
        proxy_url="https://media.discordapp.net/attachments/clip.mp4",
        width=512,
        height=704,
    )
    sent_msg = SimpleNamespace(id=333, attachments=[attachment])
    channel = MagicMock(id=7777)
    channel.send = AsyncMock(return_value=sent_msg)

    adapter._client = SimpleNamespace(
        get_channel=MagicMock(return_value=channel),
        fetch_channel=AsyncMock(),
    )

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
                "size": video_path.stat().st_size,
                "content_type": "video/mp4",
                "url": "https://cdn.discordapp.com/attachments/clip.mp4",
                "proxy_url": "https://media.discordapp.net/attachments/clip.mp4",
                "width": 512,
                "height": 704,
            }
        ],
    }
    assert adapter._last_self_message_id["7777"] == "333"
