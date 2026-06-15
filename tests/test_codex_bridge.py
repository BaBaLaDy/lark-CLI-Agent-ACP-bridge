"""Tests for CodexACPBridge."""

from unittest.mock import AsyncMock, patch

import pytest

from lark_acp_bridge.acp.client import SessionState
from lark_acp_bridge.acp.codex_bridge import CodexACPBridge


class TestCodexACPBridge:
    """Test suite for CodexACPBridge."""

    def test_init(self):
        bridge = CodexACPBridge(
            working_dir="/tmp/test",
            api_key=None,
            base_url="https://example.test/v1",
            model="gpt-4",
        )

        assert bridge.working_dir == "/tmp/test"
        assert bridge.api_key is None
        assert bridge.base_url == "https://example.test/v1"
        assert bridge.model == "gpt-4"
        assert not bridge.is_running

    @pytest.mark.asyncio
    async def test_start_does_not_require_api_key(self):
        bridge = CodexACPBridge(working_dir="/tmp/test")

        with (
            patch("lark_acp_bridge.acp.codex_bridge.ACPClient") as mock_client_class,
            patch("lark_acp_bridge.acp.codex_bridge.which", return_value="C:/node/npx.cmd"),
        ):
            mock_client = AsyncMock()
            mock_client.process = None  # prevent watchdog from triggering
            mock_client_class.return_value = mock_client

            await bridge.start()

            _, kwargs = mock_client_class.call_args
            assert "OPENAI_API_KEY" not in kwargs["env"]
            assert kwargs["env"]["CODEX_BIN"] == "codex"

    def test_resolve_npx_prefers_path_lookup(self):
        with patch("lark_acp_bridge.acp.codex_bridge.which", return_value="C:/node/npx.cmd"):
            assert CodexACPBridge._resolve_npx() == "C:/node/npx.cmd"

    def test_resolve_npx_gives_clear_error(self):
        with patch("lark_acp_bridge.acp.codex_bridge.which", return_value=None):
            with pytest.raises(RuntimeError, match="找不到 npx"):
                CodexACPBridge._resolve_npx()

    @pytest.mark.asyncio
    async def test_start_creates_acp_client(self):
        bridge = CodexACPBridge(working_dir="/tmp/test", api_key="sk-test-key")

        with patch("lark_acp_bridge.acp.codex_bridge.ACPClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.process = None  # prevent watchdog from triggering
            mock_client_class.return_value = mock_client

            await bridge.start()

            assert bridge.is_running
            mock_client.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_without_start_is_noop(self):
        bridge = CodexACPBridge(working_dir="/tmp/test", api_key="sk-test-key")

        await bridge.stop()

        assert not bridge.is_running

    @pytest.mark.asyncio
    async def test_create_session_records_user_session(self):
        bridge = CodexACPBridge(working_dir="/tmp/test", api_key="sk-test-key")

        with patch("lark_acp_bridge.acp.codex_bridge.ACPClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.new_session = AsyncMock(return_value="session-123")
            mock_client.process = None  # prevent watchdog from triggering
            mock_client_class.return_value = mock_client

            await bridge.start()
            session_id = await bridge.create_session(user_id="user-456")

            assert session_id == "session-123"
            assert bridge._user_sessions["user-456"] == "session-123"
            mock_client.new_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_or_create_session_reuses_existing_session(self):
        bridge = CodexACPBridge(working_dir="/tmp/test", api_key="sk-test-key")

        with patch("lark_acp_bridge.acp.codex_bridge.ACPClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.new_session = AsyncMock(return_value="session-789")
            mock_client.process = None  # prevent watchdog from triggering
            mock_client_class.return_value = mock_client

            await bridge.start()
            first, is_new_first = await bridge.get_or_create_session("user-111")
            second, is_new_second = await bridge.get_or_create_session("user-111")

            assert first == "session-789"
            assert is_new_first is True
            assert second == "session-789"
            assert is_new_second is False
            assert mock_client.new_session.call_count == 1

    @pytest.mark.asyncio
    async def test_chat_creates_session_automatically(self):
        bridge = CodexACPBridge(working_dir="/tmp/test", api_key="sk-test-key")

        with patch("lark_acp_bridge.acp.codex_bridge.ACPClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.new_session = AsyncMock(return_value="session-auto")
            mock_client.process = None  # prevent watchdog from triggering
            mock_state = SessionState()
            mock_state.text_chunks = ["Hello!"]
            mock_client.prompt = AsyncMock(return_value=mock_state)
            mock_client_class.return_value = mock_client

            await bridge.start()
            state = await bridge.chat(message="Hi there", user_id="user-auto")

            assert state.full_text == "Hello!"
            assert bridge._user_sessions["user-auto"] == "session-auto"
            mock_client.new_session.assert_called_once()
            mock_client.prompt.assert_called_once_with(
                message="Hi there",
                session_id="session-auto",
                on_text=None,
                on_state_change=None,
                extra_blocks=None,
            )

    @pytest.mark.asyncio
    async def test_cancel_operation(self):
        bridge = CodexACPBridge(working_dir="/tmp/test", api_key="sk-test-key")

        with patch("lark_acp_bridge.acp.codex_bridge.ACPClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.new_session = AsyncMock(return_value="session-cancel")
            mock_client.process = None  # prevent watchdog from triggering
            mock_client_class.return_value = mock_client

            await bridge.start()
            await bridge.create_session(user_id="user-cancel")
            await bridge.cancel(user_id="user-cancel")

            mock_client.cancel.assert_called_once_with("session-cancel")

    @pytest.mark.asyncio
    async def test_context_manager(self):
        bridge = CodexACPBridge(working_dir="/tmp/test", api_key="sk-test-key")

        with patch("lark_acp_bridge.acp.codex_bridge.ACPClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.process = None  # prevent watchdog from triggering
            mock_client_class.return_value = mock_client

            async with bridge:
                assert bridge.is_running

            assert not bridge.is_running
            mock_client.start.assert_called_once()
            mock_client.stop.assert_called_once()
