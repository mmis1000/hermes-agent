from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner


@pytest.mark.asyncio
async def test_deliver_media_from_response_logs_every_failed_send_result(caplog, tmp_path):
    runner = GatewayRunner.__new__(GatewayRunner)

    image_path = tmp_path / "image.png"
    video_path = tmp_path / "clip.mp4"
    audio_path = tmp_path / "voice.ogg"
    document_path = tmp_path / "report.pdf"
    for path in (image_path, video_path, audio_path, document_path):
        path.write_bytes(b"data")

    adapter = SimpleNamespace(
        name="discord",
        extract_media=lambda response: (
            [
                (str(image_path), False),
                (str(video_path), False),
                (str(audio_path), True),
            ],
            response,
        ),
        extract_images=lambda response: ([], response),
        extract_local_files=lambda response: ([str(document_path)], response),
        send_voice=AsyncMock(return_value=SendResult(success=False, error="voice boom")),
        send_video=AsyncMock(return_value=SendResult(success=False, error="video boom")),
        send_document=AsyncMock(return_value=SendResult(success=False, error="file boom")),
        send_multiple_images=AsyncMock(
            return_value=SendResult(success=False, error="image boom")
        ),
    )
    event = SimpleNamespace(
        message_id="456",
        reply_to_message_id=None,
        source=SimpleNamespace(
            chat_id="123",
            thread_id="777",
            platform=Platform.DISCORD,
            chat_type="thread",
            message_id="456",
        ),
    )

    with caplog.at_level("WARNING"):
        await runner._deliver_media_from_response("MEDIA:...", event, adapter)

    adapter.send_multiple_images.assert_awaited_once()
    adapter.send_video.assert_awaited_once_with(
        chat_id="123",
        video_path=str(video_path),
        metadata={"thread_id": "777"},
    )
    adapter.send_voice.assert_awaited_once_with(
        chat_id="123",
        audio_path=str(audio_path),
        metadata={"thread_id": "777"},
    )
    adapter.send_document.assert_awaited_once_with(
        chat_id="123",
        file_path=str(document_path),
        metadata={"thread_id": "777"},
    )
    assert "Post-stream image batch delivery failed" in caplog.text
    assert "image boom" in caplog.text
    assert "Post-stream media delivery failed" in caplog.text
    assert "video boom" in caplog.text
    assert "voice boom" in caplog.text
    assert "Post-stream file delivery failed" in caplog.text
    assert "file boom" in caplog.text
    assert "chat=123" in caplog.text
    assert "thread=777" in caplog.text
