"""Render ACP Agent run state as Feishu interactive cards (Schema 2.0).

Rendering functions:

- ``render_running_card(prompt)`` — initial "thinking" card before output.
- ``render_streaming_card(state)`` — progressive update as agent streams.
  Uses ``streaming_mode: true`` so Feishu keeps the card open for patches.
- ``render_result_card(state, status)`` — final card after run completes.
- ``render_error_card(text)`` — lightweight error fallback.

This module closely mirrors the TypeScript ``src/card/run-renderer.ts``:
- Tool panel border color: grey (normal), red (error), blue (collapsed summary)
- Tool panel icon: ✅ done, ❌ error, ⏳ running
- Reasoning panel: expanded while thinking, collapsed when done
- Tool collapse threshold: ≥3 tools → collapse older into summary panel
- Per-tool content formatting by kind (Bash/Read/Edit/Grep/etc.)
"""

from __future__ import annotations

from typing import Any

from lark_acp_bridge.acp.client import ImageInfo, SessionState

MAX_STREAMING_TEXT_CHARS = 4000
MAX_FINAL_TEXT_CHARS = 6000
MAX_THINKING_CHARS = 1500
TOOL_DISPLAY_LIMIT = 20       # max tools shown in result card
TOOL_COLLAPSE_THRESHOLD = 3   # tools beyond this are collapsed into a summary

# Per-field truncation limits matching the TS version
HEADER_SUMMARY_MAX = 80
BODY_FIELD_MAX = 600
OUTPUT_MAX = 1200
BODY_TOTAL_MAX = 2500


# =========================================================================== #
# Public API
# =========================================================================== #

def render_running_card(prompt: str) -> dict[str, Any]:
    """Render an initial 'thinking' card before any agent output arrives."""
    return _card(
        summary="思考中",
        streaming_mode=False,
        elements=[
            _panel(
                title="🧠 正在思考…",
                content=_truncate(prompt, 1000) or "_等待输入…_",
                expanded=True,
                border_color="grey",
            ),
            _note("正在思考中，完成后会自动更新这张卡片。"),
        ],
    )


def render_streaming_card(state: SessionState, show_tool_calls: bool = True) -> dict[str, Any]:
    """Render a progressive card reflecting the agent's current output.

    Called periodically (throttled) while the agent is producing output.
    Uses ``streaming_mode: true`` to keep the Feishu card open for patches.
    """
    elements: list[dict[str, Any]] = []

    # 1. Reasoning panel — expanded while actively thinking
    thinking_text = "".join(state.thinking_chunks).strip()
    if thinking_text:
        # If text output has started, thinking is "done" → collapse
        actively_thinking = not state.text_chunks
        elements.append(
            _panel(
                title="🧠 **思考中**" if actively_thinking else "🧠 **思考完成，点击查看**",
                content=_truncate(thinking_text, MAX_THINKING_CHARS),
                expanded=actively_thinking,
                border_color="grey",
            )
        )

    # 2. Streaming text content (interleaved with uploaded images)
    text = state.full_text.strip()
    if text or any(img.img_key for img in state.images):
        elements.extend(
            _build_content_elements(
                state,
                max_chars=MAX_STREAMING_TEXT_CHARS,
                sanitize_base64=True,
            )
        )

    # 3. Tool calls in progress
    if show_tool_calls and state.tool_calls:
        elements.extend(_render_tool_elements(state.tool_calls, streaming=True))

    # 4. Footer status line
    footer = _streaming_footer(state)
    if footer:
        elements.append(footer)

    summary = _streaming_summary(state)
    return _card(summary=summary, streaming_mode=True, elements=elements)


def render_result_card(
    state: SessionState, status: str = "done", show_tool_calls: bool = True
) -> dict[str, Any]:
    """Render the final result card after the run completes.

    ``status`` must be one of: ``"done"``, ``"cancelled"``, ``"error"``,
    ``"timeout"``.
    """
    elements: list[dict[str, Any]] = []

    # 1. Reasoning panel — collapsed after completion
    if state.thinking_chunks:
        elements.append(
            _panel(
                title="🧠 **思考完成，点击查看**",
                content=_truncate("".join(state.thinking_chunks), MAX_THINKING_CHARS),
                expanded=False,
                border_color="grey",
            )
        )

    # 2. Final text content (interleaved with uploaded images)
    text = state.full_text.strip()
    has_images = any(img.img_key for img in state.images)
    if text or has_images:
        elements.extend(
            _build_content_elements(state, max_chars=MAX_FINAL_TEXT_CHARS)
        )
    else:
        # Terminal annotation for empty content
        elements.append(_terminal_annotation(state, status))

    # 3. Tool calls — full collapsed list
    if show_tool_calls and state.tool_calls:
        elements.extend(_render_tool_elements(state.tool_calls, streaming=False))

    # 4. Terminal state annotation (if non-empty text but status is not "done")
    if text and status in ("cancelled", "timeout"):
        elements.append(_terminal_annotation(state, status))
    elif status == "error":
        error_msg = getattr(state, "_error_msg", None) or "未知错误"
        elements.append(_note(f"⚠️ agent 失败：{error_msg}"))

    # 5. Footer with token usage
    footer_parts = [_status_label(status)]
    if state.input_tokens or state.output_tokens:
        footer_parts.append(f"tokens: {state.input_tokens}→{state.output_tokens}")
    elements.append(_note(" · ".join(footer_parts)))

    summary_icon, summary_text = _status_summary(status)
    return _card(summary=f"{summary_icon} {summary_text}", streaming_mode=False, elements=elements)


def render_error_card(text: str) -> dict[str, Any]:
    """Render a simple error fallback card."""
    return _card(
        summary="❌ 出错",
        streaming_mode=False,
        elements=[
            _markdown(f"❌ **处理失败**\n\n{_truncate(text, 2000)}"),
        ],
    )


# =========================================================================== #
# Terminal state annotations
# =========================================================================== #

def _terminal_annotation(state: SessionState, status: str) -> dict[str, Any]:
    """Return the terminal-state annotation element."""
    if status == "cancelled":
        return _note("_⏹ 已被中断_")
    if status == "timeout":
        return _note("_⏱ 超时自动终止_")
    if status == "error":
        error_msg = getattr(state, "_error_msg", None) or "未知错误"
        return _note(f"⚠️ agent 失败：{error_msg}")
    # done + empty content
    return _note("_（未返回内容）_")


# =========================================================================== #
# Tool call rendering
# =========================================================================== #

def _render_tool_elements(tools: list[dict[str, Any]], streaming: bool) -> list[dict[str, Any]]:
    """Render tool calls as card elements.

    During streaming:
      < 3 tools → each shown individually (latest expanded)
      ≥ 3 tools → older tools collapsed into blue summary panel,
                  latest tool shown individually (expanded)
    After streaming:
      All tools (up to TOOL_DISPLAY_LIMIT) collapsed into one summary panel.
    """
    elements: list[dict[str, Any]] = []
    visible = tools[-TOOL_DISPLAY_LIMIT:]

    if streaming:
        if len(tools) < TOOL_COLLAPSE_THRESHOLD:
            # Show each individually; latest is expanded
            for i, tc in enumerate(tools):
                is_latest = (i == len(tools) - 1)
                elements.append(_tool_panel(tc, expanded=is_latest))
        else:
            # Collapse older tools into summary, show latest individually
            older = tools[:-1]
            latest = tools[-1]
            summary_lines = _tool_summary_lines(older)
            elements.append(
                _panel(
                    title=f"☕ **{len(older)} 个工具调用（已结束）**",
                    content="\n".join(summary_lines) or "_无详情_",
                    expanded=False,
                    border_color="blue",
                )
            )
            elements.append(_tool_panel(latest, expanded=True))
    else:
        # Final card: all tools collapsed into one summary panel
        lines = _tool_summary_lines(visible)
        hidden = len(tools) - len(visible)
        if hidden > 0:
            lines.append(f"（另有 {hidden} 次工具调用未展示）")
        content = "\n".join(lines) if lines else "_无工具调用_"
        elements.append(
            _panel(
                title=f"🔧 工具调用 ({len(tools)})",
                content=content,
                expanded=False,
                border_color="grey",
            )
        )

    return elements


def _tool_summary_lines(tools: list[dict[str, Any]]) -> list[str]:
    """Build one-line summary per tool for collapsed panels."""
    lines = []
    for tc in tools:
        title = _escape_md(str(tc.get("title") or tc.get("id") or "工具调用"))
        kind = _escape_md(str(tc.get("kind") or "tool"))
        status = str(tc.get("status") or "unknown")
        icon = {"done": "✅", "error": "❌", "running": "⏳"}.get(status, "•")
        lines.append(f"- {icon} **{title}** · `{kind}`")
    return lines


def _tool_panel(tc: dict[str, Any], expanded: bool = False) -> dict[str, Any]:
    """Render a single tool call as a collapsible panel."""
    title = str(tc.get("title") or tc.get("id") or "工具调用")
    status = str(tc.get("status") or "running")
    icon = {"done": "✅", "error": "❌", "running": "⏳"}.get(status, "•")
    border_color = "red" if status == "error" else "grey"

    # Build header summary (truncated to 80 chars)
    header = _truncate(f"{icon} **{_escape_md(title)}**", HEADER_SUMMARY_MAX)

    # Build body content based on tool kind
    body = _render_tool_body(tc)

    return _panel(
        title=header,
        content=body,
        expanded=expanded,
        border_color=border_color,
    )


def _render_tool_body(tc: dict[str, Any]) -> str:
    """Format tool call body content by tool kind.

    Supports tool-specific formatting when extra fields are available
    in the tool_call dict (command, path, pattern, url, query, output).
    Falls back to generic title-only display when fields are absent.
    """
    kind = str(tc.get("kind") or tc.get("title") or "").lower()
    status = str(tc.get("status") or "running")

    if status == "running" and not tc.get("output"):
        return "_运行中…_"

    parts: list[str] = []

    # Bash → show command + output
    if "bash" in kind or "terminal" in kind or "shell" in kind:
        cmd = tc.get("command", "")
        if cmd:
            parts.append(f"**Command**\n```bash\n{_truncate(cmd, BODY_FIELD_MAX)}\n```")
        output = tc.get("output", "")
        if output:
            parts.append(f"**Output**\n```\n{_truncate(output, OUTPUT_MAX)}\n```")

    # Read / Edit / Write → show file path
    elif any(k in kind for k in ("read", "edit", "write", "file")):
        path = tc.get("path") or tc.get("file_path", "")
        if path:
            parts.append(f"**File** `{_truncate(path, BODY_FIELD_MAX)}`")

    # Grep / Search → show pattern + path
    elif "grep" in kind or "search" in kind:
        pattern = tc.get("pattern", "")
        path = tc.get("path", "")
        if pattern:
            parts.append(f"**Pattern** `{_truncate(pattern, BODY_FIELD_MAX)}`")
        if path:
            parts.append(f"**Path** `{_truncate(path, BODY_FIELD_MAX)}`")

    # WebFetch → show URL
    elif "fetch" in kind or "web" in kind:
        url = tc.get("url", "")
        if url:
            parts.append(f"**URL** {_truncate(url, BODY_FIELD_MAX)}")

    # WebSearch → show query
    elif "search" in kind:
        query = tc.get("query", "")
        if query:
            parts.append(f"**Query** `{_truncate(query, BODY_FIELD_MAX)}`")

    # Error output
    error_output = tc.get("error", "")
    if error_output:
        parts.append(f"**Error**\n```\n{_truncate(error_output, OUTPUT_MAX)}\n```")

    # Generic output
    output = tc.get("output", "")
    if output and not any(k in kind for k in ("bash", "terminal", "shell")):
        parts.append(f"**Output**\n```\n{_truncate(output, OUTPUT_MAX)}\n```")

    # Fallback: show title only
    if not parts:
        title = str(tc.get("title") or tc.get("id") or "工具调用")
        parts.append(f"`{_escape_md(title)}` · {status}")

    body = "\n\n".join(parts)
    return _truncate(body, BODY_TOTAL_MAX)


# =========================================================================== #
# Footer / summary helpers
# =========================================================================== #

def _streaming_footer(state: SessionState) -> dict[str, Any] | None:
    """Return a footer note reflecting what the agent is currently doing."""
    # Priority: tool > thinking > text output
    if state.tool_calls and any(tc.get("status") not in ("done", "error") for tc in state.tool_calls):
        latest = state.tool_calls[-1]
        title = str(latest.get("title") or latest.get("id") or "工具")
        return _note(f"🧰 正在调用工具：{_escape_md(title)}")
    if state.thinking_chunks and not state.text_chunks:
        return _note("🧠 正在思考")
    if state.text_chunks:
        return _note("✍️ 正在输出")
    return None


def _streaming_summary(state: SessionState) -> str:
    """Short summary string for the card notification preview."""
    if state.tool_calls and any(tc.get("status") not in ("done", "error") for tc in state.tool_calls):
        return "正在调用工具"
    if state.thinking_chunks and not state.text_chunks:
        return "思考中"
    if state.text_chunks:
        return "正在输出"
    return "思考中"


def _status_label(status: str) -> str:
    return {
        "done": "✅ 完成",
        "cancelled": "🚫 已中断",
        "error": "❌ 出错",
        "timeout": "⏱️ 超时",
    }.get(status, status)


def _status_summary(status: str) -> tuple[str, str]:
    """Return (icon, text) tuple for the card summary."""
    return {
        "done": ("✅", "已完成"),
        "cancelled": ("🚫", "已中断"),
        "error": ("❌", "出错"),
        "timeout": ("⏱️", "超时"),
    }.get(status, ("•", status))


# =========================================================================== #
# Primitive card-building helpers (Schema 2.0)
# =========================================================================== #

def _card(summary: str, elements: list[dict[str, Any]], streaming_mode: bool = False) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
            "update_multi": True,
            "streaming_mode": streaming_mode,
            "summary": {"content": summary},
        },
        "body": {"elements": elements},
    }


def _markdown(content: str) -> dict[str, Any]:
    return {"tag": "markdown", "content": content}


def _img(img_key: str) -> dict[str, Any]:
    return {
        "tag": "img",
        "img_key": img_key,
        "alt": {"tag": "plain_text", "content": ""},
        "scale_type": "crop_center",
        "preview": True,
    }


_B64_PLACEHOLDER = "📷 _生成图片中…_"


def _strip_base64_for_display(text: str) -> str:
    """Replace inline base64 image data (complete or in-progress) with a
    placeholder so streaming cards don't ship hundreds of KB of raw base64
    to Feishu on every throttle tick.

    The actual image extraction & upload happens later in ``final_flush``
    via ``_extract_text_base64_images``.
    """
    import re

    # data:image/...;base64,<long string>
    text = re.sub(
        r'data:image/(?:png|jpeg|jpg|gif|webp);base64,[A-Za-z0-9+/=\s]{40,}',
        _B64_PLACEHOLDER,
        text,
        flags=re.IGNORECASE,
    )
    # Multi-line base64 block (≥2 wrapped lines of ~60-76 chars)
    text = re.sub(
        r'(?:[A-Za-z0-9+/]{60,76}\n){2,}[A-Za-z0-9+/=]{1,76}',
        _B64_PLACEHOLDER,
        text,
    )
    # Code-block-wrapped base64
    text = re.sub(
        r'```(?:base64|image)?\s*\n[A-Za-z0-9+/=\s]{80,}\n```',
        _B64_PLACEHOLDER,
        text,
    )
    # Long standalone single line of pure base64 (one continuous run)
    text = re.sub(
        r'^[A-Za-z0-9+/]{120,}={0,2}$',
        _B64_PLACEHOLDER,
        text,
        flags=re.MULTILINE,
    )
    return text


def _build_content_elements(
    state: SessionState,
    max_chars: int,
    *,
    sanitize_base64: bool = False,
) -> list[dict[str, Any]]:
    """Interleave text chunks and uploaded images into card elements.

    Images are placed at the character offset recorded in their
    ``insert_after_chars`` field (captured when the agent emitted the image).
    Only images with a non-empty ``img_key`` (i.e. successfully uploaded to
    Feishu) are included; others are silently skipped.

    The text is truncated to ``max_chars`` total.  Images whose insertion
    point falls after the truncation boundary are dropped.

    Falls back to a single ``_markdown`` element when there are no images,
    preserving the previous behavior.
    """
    # Only consider images that were successfully uploaded.
    inserted = sorted(
        [img for img in state.images if img.img_key],
        key=lambda i: i.insert_after_chars,
    )

    text = state.full_text
    if sanitize_base64:
        # During streaming we haven't extracted/uploaded inline base64 yet —
        # replace it with a placeholder so the card stays small and readable.
        text = _strip_base64_for_display(text)
    truncated = _truncate(text, max_chars)
    cut_len = len(truncated)
    # Drop the trailing "…" sentinel added by _truncate when measuring bounds
    # so image insertion points line up with the original text.
    was_truncated = len(truncated) < len(text)

    # Filter to images whose insertion point is within the visible range.
    visible_images = [
        img for img in inserted
        if img.insert_after_chars <= (cut_len - (1 if was_truncated else 0))
    ]

    if not visible_images:
        return [_markdown(truncated)] if truncated.strip() else []

    elements: list[dict[str, Any]] = []
    prev = 0
    for img in visible_images:
        cut = min(img.insert_after_chars, cut_len)
        chunk = truncated[prev:cut]
        if chunk.strip():
            elements.append(_markdown(chunk))
        elements.append(_img(img.img_key))  # type: ignore[arg-type]
        prev = cut
    tail = truncated[prev:]
    if tail.strip():
        elements.append(_markdown(tail))
    return elements


def _note(content: str) -> dict[str, Any]:
    return {"tag": "markdown", "content": content, "text_size": "notation"}


def _panel(
    title: str,
    content: str,
    expanded: bool = False,
    border_color: str = "grey",
) -> dict[str, Any]:
    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": {"tag": "markdown", "content": f"**{title}**"},
            "vertical_align": "center",
            "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined", "size": "16px 16px"},
            "icon_position": "follow_text",
            "icon_expanded_angle": -180,
        },
        "border": {"color": border_color, "corner_radius": "5px"},
        "vertical_spacing": "8px",
        "padding": "8px 8px 8px 8px",
        "elements": [{"tag": "markdown", "content": content, "text_size": "notation"}],
    }


def _truncate(value: str, max_chars: int) -> str:
    return value if len(value) <= max_chars else f"{value[:max_chars]}…"


def _escape_md(value: str) -> str:
    return value.replace("`", "'").replace("*", "\\*").replace("_", "\\_")


