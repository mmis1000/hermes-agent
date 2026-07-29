from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
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


@pytest.mark.asyncio
@pytest.mark.parametrize("already_streamed", [False, True])
async def test_queued_followup_preserves_markdown_attachment(
    tmp_path, already_streamed
):
    """Queued follow-ups must not bypass MEDIA delivery for Markdown files."""
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._deliver_media_from_response = AsyncMock()

    markdown_path = tmp_path / "report.md"
    markdown_path.write_text("# Report\n", encoding="utf-8")
    response = f"Report ready.\n\nMEDIA:{markdown_path}"

    adapter = SimpleNamespace(
        send=AsyncMock(return_value=SendResult(success=True, message_id="text")),
    )
    source = SimpleNamespace(
        chat_id="-100123",
        thread_id="777",
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    metadata = {"message_thread_id": "777"}

    await runner._deliver_queued_first_response(
        response=response,
        source=source,
        adapter=adapter,
        thread_metadata=metadata,
        already_streamed=already_streamed,
        event_message_id="456",
    )

    if already_streamed:
        adapter.send.assert_not_awaited()
    else:
        adapter.send.assert_awaited_once_with(
            source.chat_id,
            "Report ready.",
            metadata=metadata,
        )

    runner._deliver_media_from_response.assert_awaited_once()
    delivered_response, synthetic_event, delivered_adapter = (
        runner._deliver_media_from_response.await_args.args
    )
    assert delivered_response == response
    assert synthetic_event.source is source
    assert synthetic_event.message_id == "456"
    assert delivered_adapter is adapter


@pytest.mark.asyncio
async def test_queued_followup_delivers_media_when_text_send_raises(tmp_path):
    """A text-send exception must not prevent queued attachment delivery."""
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._deliver_media_from_response = AsyncMock()

    markdown_path = tmp_path / "report.md"
    markdown_path.write_text("# Report\n", encoding="utf-8")
    response = f"Report ready.\n\nMEDIA:{markdown_path}"
    adapter = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("text boom")))
    source = SimpleNamespace(
        chat_id="-100123",
        thread_id="777",
        platform=Platform.TELEGRAM,
        chat_type="group",
    )

    await runner._deliver_queued_first_response(
        response=response,
        source=source,
        adapter=adapter,
        thread_metadata={"message_thread_id": "777"},
        already_streamed=False,
        event_message_id="456",
    )

    runner._deliver_media_from_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_stream_markdown_routes_to_document_sender(tmp_path):
    """The shared MEDIA delivery routine treats .md as a document."""
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._thread_metadata_for_source = lambda source, anchor=None: {
        "message_thread_id": source.thread_id,
    }
    runner._reply_anchor_for_event = lambda event: None

    markdown_path = tmp_path / "report.md"
    markdown_path.write_text("# Report\n", encoding="utf-8")
    adapter = SimpleNamespace(
        name="telegram",
        extract_media=BasePlatformAdapter.extract_media,
        extract_images=BasePlatformAdapter.extract_images,
        extract_local_files=BasePlatformAdapter.extract_local_files,
        send_voice=AsyncMock(return_value=SendResult(success=True, message_id="voice")),
        send_video=AsyncMock(return_value=SendResult(success=True, message_id="video")),
        send_document=AsyncMock(return_value=SendResult(success=True, message_id="doc")),
        send_multiple_images=AsyncMock(return_value=SendResult(success=True, message_id="images")),
    )
    event = SimpleNamespace(
        message_id="456",
        reply_to_message_id=None,
        source=SimpleNamespace(
            chat_id="-100123",
            thread_id="777",
            platform=Platform.TELEGRAM,
            chat_type="group",
        ),
    )

    await runner._deliver_media_from_response(
        f"Report ready.\n\nMEDIA:{markdown_path}",
        event,
        adapter,
    )

    adapter.send_document.assert_awaited_once_with(
        chat_id="-100123",
        file_path=str(markdown_path),
        metadata={"message_thread_id": "777"},
    )
    adapter.send_voice.assert_not_awaited()
    adapter.send_video.assert_not_awaited()
    adapter.send_multiple_images.assert_not_awaited()
