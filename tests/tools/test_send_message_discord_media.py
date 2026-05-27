import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import Platform, PlatformConfig
from plugins.platforms.discord.adapter import _standalone_send
from tools.send_message_tool import _send_to_platform


def _make_aiohttp_resp(status, json_data=None, text_data=None):
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text_data or "")
    return resp


def _make_request_ctx(resp):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _make_aiohttp_session(post_responses):
    session = MagicMock()
    session.post = MagicMock(side_effect=[_make_request_ctx(resp) for resp in post_responses])

    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    return session_ctx, session


class _FakeClientTimeout:
    def __init__(self, total=None):
        self.total = total


class _FakeFormData:
    def __init__(self):
        self.fields = []

    def add_field(self, name, value, **kwargs):
        self.fields.append((name, value, kwargs))


def test_discord_standalone_send_returns_attachments_for_media(tmp_path):
    media_path = tmp_path / "clip.mp4"
    media_path.write_bytes(b"fake-mp4")

    text_resp = _make_aiohttp_resp(200, json_data={"id": "msg-text", "channel_id": "thread-1", "attachments": []})
    media_resp = _make_aiohttp_resp(
        200,
        json_data={
            "id": "msg-media",
            "channel_id": "thread-1",
            "attachments": [
                {
                    "id": "att-1",
                    "filename": "clip.mp4",
                    "content_type": "video/mp4",
                    "url": "https://cdn.discordapp.test/clip.mp4",
                }
            ],
        },
    )
    session_ctx, _session = _make_aiohttp_session([text_resp, media_resp])
    fake_aiohttp = SimpleNamespace(
        ClientSession=MagicMock(return_value=session_ctx),
        ClientTimeout=_FakeClientTimeout,
        FormData=_FakeFormData,
    )

    with patch.dict("sys.modules", {"aiohttp": fake_aiohttp}), \
         patch("gateway.platforms.base.resolve_proxy_url", return_value=None), \
         patch("gateway.platforms.base.proxy_kwargs_for_aiohttp", return_value=({}, {})):
        result = asyncio.run(
            _standalone_send(
                PlatformConfig(enabled=True, token="discord-token"),
                "thread-1",
                "hello",
                thread_id="thread-1",
                media_files=[(str(media_path), False)],
            )
        )

    assert result["success"] is True
    assert result["message_id"] == "msg-media"
    assert result["channel_id"] == "thread-1"
    assert result["thread_id"] == "thread-1"
    assert result["attachments"][0]["filename"] == "clip.mp4"
    assert result["attachments"][0]["content_type"] == "video/mp4"


def test_send_to_platform_discord_preserves_attachment_metadata():
    mock_result = {
        "success": True,
        "platform": "discord",
        "chat_id": "1498377115484164156",
        "channel_id": "1508919799395254446",
        "thread_id": "1508919799395254446",
        "message_id": "1234567890",
        "attachments": [{"id": "att-1", "filename": "clip.mp4"}],
    }
    entry = SimpleNamespace(standalone_sender_fn=AsyncMock(return_value=mock_result))

    with patch("gateway.platform_registry.platform_registry.get", return_value=entry):
        result = asyncio.run(
            _send_to_platform(
                Platform.DISCORD,
                PlatformConfig(enabled=True, token="discord-token"),
                "1498377115484164156",
                "hello",
                thread_id="1508919799395254446",
                media_files=[("/tmp/clip.mp4", False)],
            )
        )

    assert result == mock_result
