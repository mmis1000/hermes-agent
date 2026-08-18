"""Post-stream media delivery is explicit-only (#20834).

``GatewayRunner._deliver_media_from_response`` runs AFTER streaming has sent
the visible reply. At that point a bare local filesystem path in the response
text is either text the user already saw, or stale inspected/tool content —
it is NOT an attachment request. Only explicit ``MEDIA:`` directives may
trigger post-stream uploads.

The non-streaming path (``gateway/platforms/base.py``) keeps its bare-path
auto-detect (``extract_local_files``) — that path controls what text is sent
and can strip the path from the visible reply, so auto-attach is intentional
there. This file pins the asymmetry.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _event():
    source = SessionSource(
        platform=Platform.SLACK,
        chat_id="C123CHAN",
        chat_type="group",
        thread_id=None,
    )
    return MessageEvent(
        text="hi",
        message_type=MessageType.TEXT,
        source=source,
        message_id="171.000001",
    )


def _fake_runner(thread_meta):
    return SimpleNamespace(
        _thread_metadata_for_source=lambda source, anchor=None: thread_meta,
        _reply_anchor_for_event=lambda event: None,
    )


def _adapter():
    return SimpleNamespace(
        name="test",
        extract_media=BasePlatformAdapter.extract_media,
        extract_images=BasePlatformAdapter.extract_images,
        extract_local_files=BasePlatformAdapter.extract_local_files,
        send_voice=AsyncMock(return_value=SendResult(success=True, message_id="voice")),
        send_document=AsyncMock(return_value=SendResult(success=True, message_id="doc")),
        send_image_file=AsyncMock(return_value=SendResult(success=True, message_id="image")),
        send_video=AsyncMock(return_value=SendResult(success=True, message_id="video")),
        send_multiple_images=AsyncMock(return_value=SendResult(success=True, message_id="imgs")),
    )


def _allowed_media_path(tmp_path, monkeypatch, name):
    root = tmp_path / "media-cache"
    media_file = root / name
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"media")
    monkeypatch.setattr(
        "gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS",
        (root,),
    )
    return media_file.resolve()


@pytest.mark.asyncio
async def test_bare_local_path_in_streamed_reply_is_not_uploaded(tmp_path, monkeypatch):
    """The #20834 shape: visible reply contains a bare path (from inspected
    content), no MEDIA: directive — nothing may be uploaded post-stream."""
    media_file = _allowed_media_path(tmp_path, monkeypatch, "mockup.png")
    adapter = _adapter()

    await GatewayRunner._deliver_media_from_response(
        _fake_runner({}),
        f"The design lives at {media_file} if you want to look later.",
        _event(),
        adapter,
    )

    adapter.send_multiple_images.assert_not_awaited()
    adapter.send_image_file.assert_not_awaited()
    adapter.send_document.assert_not_awaited()
    adapter.send_video.assert_not_awaited()
    adapter.send_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_media_tag_still_delivers_post_stream(tmp_path, monkeypatch):
    """Explicit MEDIA: directives keep working after the #20834 fix."""
    media_file = _allowed_media_path(tmp_path, monkeypatch, "chart.png")
    adapter = _adapter()

    await GatewayRunner._deliver_media_from_response(
        _fake_runner({}),
        f"Here is the chart.\nMEDIA:{media_file}",
        _event(),
        adapter,
    )

    adapter.send_multiple_images.assert_awaited_once()
    images_kwargs = adapter.send_multiple_images.await_args.kwargs
    assert images_kwargs["chat_id"] == "C123CHAN"
    assert str(media_file) in images_kwargs["images"][0][0]


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
        extract_media=BasePlatformAdapter.extract_media,
    )
    source = SimpleNamespace(
        chat_id="-100123",
        thread_id="777",
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    metadata = {"message_thread_id": "777"}

    await runner._deliver_queued_first_response(
        response,
        source,
        adapter,
        metadata=metadata,
        event_message_id="456",
        text_already_delivered=already_streamed,
    )

    if already_streamed:
        adapter.send.assert_not_awaited()
    else:
        adapter.send.assert_awaited_once()

    runner._deliver_media_from_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_queued_followup_delivers_media_when_text_send_raises(tmp_path):
    """A text-send exception must not prevent queued attachment delivery."""
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._deliver_media_from_response = AsyncMock()

    markdown_path = tmp_path / "report.md"
    markdown_path.write_text("# Report\n", encoding="utf-8")
    response = f"Report ready.\n\nMEDIA:{markdown_path}"
    adapter = SimpleNamespace(
        send=AsyncMock(side_effect=RuntimeError("text boom")),
        extract_media=BasePlatformAdapter.extract_media,
    )
    source = SimpleNamespace(
        chat_id="-100123",
        thread_id="777",
        platform=Platform.TELEGRAM,
        chat_type="group",
    )

    await runner._deliver_queued_first_response(
        response,
        source,
        adapter,
        metadata={"message_thread_id": "777"},
        event_message_id="456",
        text_already_delivered=False,
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

