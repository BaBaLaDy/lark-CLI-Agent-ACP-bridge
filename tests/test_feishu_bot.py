"""Tests for FeishuBot — dedup, active-task guard, and command routing."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, Mock

import pytest

from lark_acp_bridge.acp.client import SessionState
from lark_acp_bridge.bot.feishu_bot import FeishuBot
from lark_acp_bridge.config.settings import Settings


def _make_bot(**overrides: object) -> FeishuBot:
    settings_kwargs: dict = {
        "feishu_app_id": "app-id",
        "feishu_app_secret": "app-secret",
    }
    # Only forward keys that Settings actually accepts
    valid_keys = {
        "idle_timeout_seconds", "card_update_throttle_ms",
        "max_concurrent_runs_per_user", "show_tool_calls",
    }
    settings_kwargs.update({k: v for k, v in overrides.items() if k in valid_keys})
    settings = Settings(**settings_kwargs)
    bridge = Mock()
    bridge.is_running = True
    bridge.active_session_count = 0
    return FeishuBot(settings=settings, codex_bridge=bridge)


def test_feishu_bot_initializes_client() -> None:
    bot = _make_bot()
    assert bot.client is not None
    assert bot._settings.feishu_app_id == "app-id"


# ----------------------------------------------------------------------- #
# Dedup
# ----------------------------------------------------------------------- #

def test_is_duplicate_returns_false_on_first_seen() -> None:
    bot = _make_bot()
    assert bot._is_duplicate("msg-1") is False


def test_is_duplicate_returns_true_on_replay() -> None:
    bot = _make_bot()
    bot._is_duplicate("msg-1")
    assert bot._is_duplicate("msg-1") is True


def test_is_duplicate_clears_after_ttl() -> None:
    bot = _make_bot()
    bot._is_duplicate("msg-1")
    # Simulate TTL expiry by writing a stale timestamp directly
    bot._seen_message_ids["msg-1"] = time.monotonic() - 61.0
    assert bot._is_duplicate("msg-1") is False


# ----------------------------------------------------------------------- #
# Active-task guard
# ----------------------------------------------------------------------- #

def test_active_tasks_starts_empty() -> None:
    bot = _make_bot()
    assert bot._active_tasks == {}


# ----------------------------------------------------------------------- #
# Command routing (unit-level via _handle_command)
# ----------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_handle_command_help_sends_message() -> None:
    bot = _make_bot()
    bot._reply_card = AsyncMock()  # type: ignore[method-assign]

    await bot._handle_command("user-1", "msg-1", "/help")

    bot._reply_card.assert_called_once()
    args = bot._reply_card.call_args[0]
    assert args[0] == "msg-1"
    # Verify it's the help card (Schema 1.x with header)
    card = args[1]
    assert "header" in card
    assert "使用帮助" in card["header"]["title"]["content"]


@pytest.mark.asyncio
async def test_handle_command_stop_when_no_active_run() -> None:
    bot = _make_bot()
    bot._reply_card = AsyncMock()  # type: ignore[method-assign]
    bot.codex_bridge.cancel = AsyncMock()  # type: ignore[attr-defined]

    await bot._handle_command("user-1", "msg-1", "/stop")

    bot._reply_card.assert_called_once()
    args = bot._reply_card.call_args[0]
    assert args[0] == "msg-1"
    # Verify it's a card with the warning message
    card = args[1]
    assert "header" in card
    bot.codex_bridge.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_handle_command_status() -> None:
    bot = _make_bot()
    bot._reply_card = AsyncMock()  # type: ignore[method-assign]

    await bot._handle_command("user-1", "msg-1", "/status")

    bot._reply_card.assert_called_once()
    args = bot._reply_card.call_args[0]
    assert args[0] == "msg-1"
    # Verify it's the status card (Schema 1.x with header)
    card = args[1]
    assert "header" in card
    assert "当前状态" in card["header"]["title"]["content"]


@pytest.mark.asyncio
async def test_handle_command_unknown() -> None:
    bot = _make_bot()
    bot._reply_card = AsyncMock()  # type: ignore[method-assign]

    await bot._handle_command("user-1", "msg-1", "/xyz")

    bot._reply_card.assert_called_once()
    args = bot._reply_card.call_args[0]
    assert args[0] == "msg-1"
    card = args[1]
    assert "header" in card


@pytest.mark.asyncio
async def test_handle_command_resume() -> None:
    bot = _make_bot()
    bot._reply_card = AsyncMock()  # type: ignore[method-assign]

    await bot._handle_command("user-1", "msg-1", "/resume")

    bot._reply_card.assert_called_once()
    card = bot._reply_card.call_args[0][1]
    assert "历史会话" in card["header"]["title"]["content"]


@pytest.mark.asyncio
async def test_handle_command_ws() -> None:
    bot = _make_bot()
    bot._reply_card = AsyncMock()  # type: ignore[method-assign]

    await bot._handle_command("user-1", "msg-1", "/ws")

    bot._reply_card.assert_called_once()
    card = bot._reply_card.call_args[0][1]
    assert "工作目录" in card["header"]["title"]["content"]


# ----------------------------------------------------------------------- #
# Card template unit tests
# ----------------------------------------------------------------------- #

def test_help_card_has_buttons() -> None:
    from lark_acp_bridge.card.templates import help_card
    card = help_card()
    assert "header" in card
    assert "elements" in card
    # Find the action row
    action_rows = [e for e in card["elements"] if e.get("tag") == "action"]
    assert len(action_rows) == 1
    buttons = action_rows[0]["actions"]
    assert len(buttons) == 4  # status, agent, resume, new
    assert buttons[0]["value"]["cmd"] == "status"


def test_status_card_has_info_and_buttons() -> None:
    from lark_acp_bridge.card.templates import status_card
    card = status_card({
        "working_dir": "/test",
        "session_count": 2,
        "active_run": True,
        "bridge_running": True,
        "agent_type": "codex",
        "agent_command": "codex-acp",
    })
    assert "header" in card
    divs = [e for e in card["elements"] if e.get("tag") == "div"]
    assert len(divs) >= 1
    assert "/test" in divs[0]["text"]["content"]


def test_resume_card_empty() -> None:
    from lark_acp_bridge.card.templates import resume_card
    card = resume_card(entries=[])
    divs = [e for e in card["elements"] if e.get("tag") == "div"]
    assert any("没有历史会话" in d["text"]["content"] for d in divs)


def test_workspaces_card_empty() -> None:
    from lark_acp_bridge.card.templates import workspaces_card
    card = workspaces_card("/test", workspaces=[])
    divs = [e for e in card["elements"] if e.get("tag") == "div"]
    assert any("暂无" in d["text"]["content"] for d in divs)


# ----------------------------------------------------------------------- #
# Prefix routing tests
# ----------------------------------------------------------------------- #

def test_parse_prefix_routing_valid_prefix() -> None:
    from lark_acp_bridge.bot.feishu_bot import parse_prefix_routing
    agents = {"claude", "codex"}
    name, prompt = parse_prefix_routing("claude: review this code", agents)
    assert name == "claude"
    assert prompt == "review this code"


def test_parse_prefix_routing_codex_prefix() -> None:
    from lark_acp_bridge.bot.feishu_bot import parse_prefix_routing
    agents = {"claude", "codex"}
    name, prompt = parse_prefix_routing("codex: complete this function", agents)
    assert name == "codex"
    assert prompt == "complete this function"


def test_parse_prefix_routing_unknown_prefix() -> None:
    from lark_acp_bridge.bot.feishu_bot import parse_prefix_routing
    agents = {"claude", "codex"}
    name, prompt = parse_prefix_routing("unknown: do something", agents)
    assert name is None
    assert prompt == "unknown: do something"


def test_parse_prefix_routing_no_prefix() -> None:
    from lark_acp_bridge.bot.feishu_bot import parse_prefix_routing
    agents = {"claude", "codex"}
    name, prompt = parse_prefix_routing("just a normal message", agents)
    assert name is None
    assert prompt == "just a normal message"


def test_parse_prefix_routing_colon_no_space() -> None:
    from lark_acp_bridge.bot.feishu_bot import parse_prefix_routing
    agents = {"claude", "codex"}
    # Colon without space should NOT be treated as prefix routing
    name, prompt = parse_prefix_routing("claude:review this", agents)
    assert name is None
    assert prompt == "claude:review this"


def test_parse_prefix_routing_empty_agents() -> None:
    from lark_acp_bridge.bot.feishu_bot import parse_prefix_routing
    name, prompt = parse_prefix_routing("claude: do X", set())
    assert name is None
    assert prompt == "claude: do X"


def test_parse_prefix_routing_whitespace_in_prefix() -> None:
    from lark_acp_bridge.bot.feishu_bot import parse_prefix_routing
    agents = {"claude", "codex"}
    # " claude " is split on ": " → prefix=" claude ", stripped → "claude"
    # Since "claude" IS in agents, this matches (strip handles extra spaces)
    name, prompt = parse_prefix_routing(" claude : do X", agents)
    assert name == "claude"
    assert prompt == "do X"


# ----------------------------------------------------------------------- #
# _extract_text_base64_images tests
# ----------------------------------------------------------------------- #

def _make_state(text: str) -> SessionState:
    state = SessionState()
    state.text_chunks.append(text)
    return state


# Fake base64 strings long enough to pass the 100-char minimum
_B64_LONG = "A" * 100 + "=="          # 102 chars, valid padding
_B64_JPEG = "B" * 100 + "="           # 101 chars — padding check test too
_B64_LINE = "A" * 76                   # exactly one standard base64 line


def test_extract_no_base64_leaves_state_unchanged() -> None:
    state = _make_state("Hello, world! 你好")
    FeishuBot._extract_text_base64_images(state)
    assert state.full_text == "Hello, world! 你好"
    assert state.images == []


def test_extract_empty_text_is_noop() -> None:
    state = _make_state("")
    FeishuBot._extract_text_base64_images(state)
    assert state.full_text == ""
    assert state.images == []


def test_extract_pattern1_data_uri_png() -> None:
    text = f"看图：\ndata:image/png;base64,{_B64_LONG}\n结束"
    state = _make_state(text)
    FeishuBot._extract_text_base64_images(state)
    assert len(state.images) == 1
    assert state.images[0].mime_type == "image/png"
    assert state.images[0].data == _B64_LONG
    assert "data:image" not in state.full_text
    assert "看图" in state.full_text
    assert "结束" in state.full_text


def test_extract_pattern1_data_uri_jpeg() -> None:
    text = f"data:image/jpeg;base64,{_B64_LONG}"
    state = _make_state(text)
    FeishuBot._extract_text_base64_images(state)
    assert len(state.images) == 1
    assert state.images[0].mime_type == "image/jpeg"


def test_extract_pattern1_jpg_normalized_to_jpeg() -> None:
    text = f"data:image/jpg;base64,{_B64_LONG}"
    state = _make_state(text)
    FeishuBot._extract_text_base64_images(state)
    assert state.images[0].mime_type == "image/jpeg"


def test_extract_pattern2_code_block() -> None:
    b64_content = f"{_B64_LINE}\n{_B64_LINE}\nAA=="
    text = f"图片：\n```base64\n{b64_content}\n```\n后续文字"
    state = _make_state(text)
    FeishuBot._extract_text_base64_images(state)
    assert len(state.images) == 1
    assert state.images[0].mime_type == "image/png"
    assert "```" not in state.full_text
    assert "后续文字" in state.full_text


def test_extract_pattern3_standalone_line() -> None:
    text = f"看图\n{_B64_LONG}\n后续"
    state = _make_state(text)
    FeishuBot._extract_text_base64_images(state)
    assert len(state.images) == 1
    assert _B64_LONG not in state.full_text
    assert "看图" in state.full_text
    assert "后续" in state.full_text


def test_extract_pattern4_multiline_block() -> None:
    b64_block = f"{_B64_LINE}\n{_B64_LINE}\n{_B64_LINE}\nAA=="
    text = f"图片如下：\n{b64_block}\n以上"
    state = _make_state(text)
    FeishuBot._extract_text_base64_images(state)
    assert len(state.images) == 1
    assert state.images[0].mime_type == "image/png"
    assert _B64_LINE not in state.full_text
    assert "以上" in state.full_text


def test_extract_insert_after_chars_correct() -> None:
    prefix = "hello:\n"
    text = f"{prefix}data:image/png;base64,{_B64_LONG}\nend"
    state = _make_state(text)
    FeishuBot._extract_text_base64_images(state)
    assert len(state.images) == 1
    assert state.images[0].insert_after_chars == len(prefix)


def test_extract_multiple_images_in_order() -> None:
    text = (
        f"第一张：\ndata:image/png;base64,{_B64_LONG}\n"
        f"第二张：\ndata:image/jpeg;base64,{'B' * 100 + '=='}\n"
    )
    state = _make_state(text)
    FeishuBot._extract_text_base64_images(state)
    assert len(state.images) == 2
    assert state.images[0].mime_type == "image/png"
    assert state.images[1].mime_type == "image/jpeg"
    assert state.images[0].insert_after_chars < state.images[1].insert_after_chars


def test_extract_idempotent_second_call_adds_nothing() -> None:
    text = f"data:image/png;base64,{_B64_LONG}"
    state = _make_state(text)
    FeishuBot._extract_text_base64_images(state)
    count_after_first = len(state.images)
    FeishuBot._extract_text_base64_images(state)
    assert len(state.images) == count_after_first  # no duplicates
