import asyncio
import json
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import Platform, PlatformConfig
from plugins.platforms.discord.adapter import _remember_channel_is_forum, _standalone_send
from tools.send_message_tool import _send_to_platform, _send_via_adapter


def _response(status, json_data=None, text_data=None):
    response = AsyncMock()
    response.status = status
    body = json.dumps(json_data or {}).encode() if json_data is not None else (text_data or "").encode()
    response.content = MagicMock()
    response.content.read = AsyncMock(side_effect=[body, b"", b""])
    response.get_encoding = MagicMock(return_value="utf-8")
    response.json = AsyncMock(return_value=json_data or {})
    response.text = AsyncMock(return_value=text_data or "")
    return response


def _session_with(post_responses):
    session = MagicMock()
    response_contexts = []
    for response in post_responses:
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=False)
        response_contexts.append(context)
    session.post = MagicMock(side_effect=response_contexts)

    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=False)
    return session_context, session


def _pconfig():
    return PlatformConfig(enabled=True, token="discord-token")


def test_discord_standalone_send_returns_thread_attachment_metadata(tmp_path):
    media_path = tmp_path / "clip.mp4"
    media_path.write_bytes(b"fake-mp4")
    thread_id = "1508919799395254446"

    media_response = _response(
        200,
        {
            "id": "msg-media",
            "channel_id": thread_id,
            "attachments": [
                {
                    "id": "att-1",
                    "filename": "clip.mp4",
                    "content_type": "video/mp4",
                    "url": "https://cdn.discordapp.test/clip.mp4",
                    "unexpected": {"must": "not escape"},
                }
            ],
        },
    )
    session_context, _session = _session_with([media_response])

    with patch("aiohttp.ClientSession", return_value=session_context):
        result = asyncio.run(
            _standalone_send(
                _pconfig(),
                "1498377115484164156",
                "",
                thread_id=thread_id,
                media_files=[(str(media_path), False)],
            )
        )

    assert result["success"] is True
    assert result["message_id"] == "msg-media"
    assert result["channel_id"] == thread_id
    assert result["thread_id"] == thread_id
    assert result["attachments"][0]["filename"] == "clip.mp4"
    assert result["attachments"][0]["content_type"] == "video/mp4"
    assert "unexpected" not in result["attachments"][0]


def test_discord_standalone_send_accumulates_multiple_upload_attachments(tmp_path):
    chat_id = "1498377115484164156"
    _remember_channel_is_forum(chat_id, False)
    media_files = []
    responses = []
    for index in range(2):
        media_path = tmp_path / f"image-{index}.png"
        media_path.write_bytes(b"png")
        media_files.append((str(media_path), False))
        responses.append(
            _response(
                200,
                {
                    "id": f"msg-{index}",
                    "channel_id": chat_id,
                    "attachments": [
                        {
                            "id": f"att-{index}",
                            "filename": media_path.name,
                            "content_type": "image/png",
                            "url": f"https://cdn.discordapp.test/{media_path.name}",
                        }
                    ],
                },
            )
        )
    session_context, session = _session_with(responses)

    with patch("aiohttp.ClientSession", return_value=session_context):
        result = asyncio.run(
            _standalone_send(
                _pconfig(),
                chat_id,
                "",
                media_files=media_files,
            )
        )

    assert session.post.call_count == 2
    assert result["message_id"] == "msg-1"
    assert result["channel_id"] == chat_id
    assert [item["id"] for item in result["attachments"]] == ["att-0", "att-1"]


def test_discord_standalone_forum_result_includes_starter_metadata(tmp_path):
    chat_id = "1498377115484164156"
    _remember_channel_is_forum(chat_id, True)
    media_path = tmp_path / "report.pdf"
    media_path.write_bytes(b"pdf")
    response = _response(
        201,
        {
            "id": "thread-created",
            "message": {
                "id": "starter-msg",
                "channel_id": "thread-created",
                "attachments": [
                    {
                        "id": "forum-att",
                        "filename": "report.pdf",
                        "content_type": "application/pdf",
                        "url": "https://cdn.discordapp.test/report.pdf",
                    }
                ],
            },
        },
    )
    session_context, _session = _session_with([response])

    with patch("aiohttp.ClientSession", return_value=session_context):
        result = asyncio.run(
            _standalone_send(
                _pconfig(),
                chat_id,
                "report",
                media_files=[(str(media_path), False)],
            )
        )

    assert result["message_id"] == "starter-msg"
    assert result["thread_id"] == "thread-created"
    assert result["channel_id"] == "thread-created"
    assert result["attachments"][0]["id"] == "forum-att"


def test_send_to_platform_discord_preserves_standalone_first_routing():
    mock_result = {
        "success": True,
        "platform": "discord",
        "chat_id": "1498377115484164156",
        "channel_id": "1508919799395254446",
        "thread_id": "1508919799395254446",
        "message_id": "1234567890",
        "attachments": [{"id": "att-1", "filename": "clip.mp4"}],
    }
    entry = SimpleNamespace(
        max_message_length=2000,
        standalone_sender_fn=AsyncMock(return_value=mock_result),
    )
    live_adapter = SimpleNamespace(
        send=AsyncMock(side_effect=AssertionError("Discord must stay standalone-first"))
    )
    runner = SimpleNamespace(adapters={Platform.DISCORD: live_adapter})
    fake_gateway_run = ModuleType("gateway.run")
    fake_gateway_run._gateway_runner_ref = lambda: runner

    with patch.dict("sys.modules", {"gateway.run": fake_gateway_run}), \
         patch("gateway.platform_registry.platform_registry.get", return_value=entry):
        result = asyncio.run(
            _send_to_platform(
                Platform.DISCORD,
                _pconfig(),
                "1498377115484164156",
                "hello",
                thread_id="1508919799395254446",
                media_files=[("/tmp/clip.mp4", False)],
                force_document=True,
            )
        )

    assert result == mock_result
    entry.standalone_sender_fn.assert_awaited_once_with(
        _pconfig(),
        "1498377115484164156",
        "",
        thread_id="1508919799395254446",
        media_files=[("/tmp/clip.mp4", False)],
        caption="hello",
    )
    live_adapter.send.assert_not_awaited()


def test_live_adapter_result_payload_whitelists_raw_response_keys():
    platform = Platform("ntfy")
    adapter = SimpleNamespace(
        send=AsyncMock(
            return_value=SimpleNamespace(
                success=True,
                message_id="canonical-message",
                error=None,
                raw_response={
                    "success": False,
                    "platform": "attacker",
                    "chat_id": "attacker-chat",
                    "message_id": "attacker-message",
                    "channel_id": "delivery-channel",
                    "thread_id": "delivery-thread",
                    "attachments": [{"id": "att-1"}],
                    "warnings": ["partial delivery"],
                    "arbitrary": "must-not-escape",
                },
            )
        )
    )
    runner = SimpleNamespace(adapters={platform: adapter})
    fake_gateway_run = ModuleType("gateway.run")
    fake_gateway_run._gateway_runner_ref = lambda: runner

    with patch.dict("sys.modules", {"gateway.run": fake_gateway_run}):
        result = asyncio.run(
            _send_via_adapter(
                platform,
                SimpleNamespace(extra={}),
                "canonical-chat",
                "hello",
            )
        )

    assert result == {
        "success": True,
        "platform": "ntfy",
        "chat_id": "canonical-chat",
        "message_id": "canonical-message",
        "channel_id": "delivery-channel",
        "thread_id": "delivery-thread",
        "attachments": [{"id": "att-1"}],
        "warnings": ["partial delivery"],
    }
