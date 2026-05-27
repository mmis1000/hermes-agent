from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner


@pytest.mark.asyncio
async def test_deliver_media_from_response_logs_failed_send_result(caplog, tmp_path):
    runner = GatewayRunner.__new__(GatewayRunner)

    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x18ftyp" + b"\x00" * 64)

    adapter = SimpleNamespace(
        name="discord",
        extract_media=lambda response: ([(str(video_path), False)], response),
        extract_images=lambda response: ([], response),
        extract_local_files=lambda response: ([], response),
        send_voice=AsyncMock(),
        send_video=AsyncMock(return_value=SendResult(success=False, error="boom")),
        send_document=AsyncMock(),
        send_multiple_images=AsyncMock(),
    )
    event = SimpleNamespace(
        source=SimpleNamespace(
            chat_id="123",
            thread_id="777",
            platform=Platform.DISCORD,
            chat_type="thread",
            message_id="456",
        )
    )

    with caplog.at_level("WARNING"):
        await runner._deliver_media_from_response(f"MEDIA:{video_path}", event, adapter)

    adapter.send_video.assert_awaited_once_with(
        chat_id="123",
        video_path=str(video_path),
        metadata={"thread_id": "777"},
    )
    assert "Post-stream media delivery failed: boom" in caplog.text
