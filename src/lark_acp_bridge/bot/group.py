"""Group chat creation via Feishu im:chat API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import lark_oapi as lark
import structlog
from lark_oapi.api.im.v1 import (
    CreateChatRequest,
    CreateChatRequestBody,
)

logger = structlog.get_logger()


@dataclass
class ChatInfo:
    """Minimal info about a newly created group chat."""
    chat_id: str
    name: str


async def create_bound_chat(
    client: lark.Client,
    name: str,
    invite_open_id: str,
) -> ChatInfo:
    """Create a Feishu group chat and invite the specified user.

    Requires the ``im:chat`` permission on the Feishu app.

    Args:
        client: The lark-oapi client (FeishuBot.client).
        name: Display name for the new group.
        invite_open_id: open_id of the user to invite.

    Returns:
        ChatInfo with the new group's chat_id and name.

    Raises:
        RuntimeError: If the API call fails (e.g. missing permission).
    """
    body = (
        CreateChatRequestBody.builder()
        .name(name)
        .user_id_list([invite_open_id])
        .build()
    )
    request = (
        CreateChatRequest.builder()
        .user_id_type("open_id")
        .request_body(body)
        .build()
    )
    response = client.im.v1.chat.create(request)

    if not response.success():
        code = getattr(response, "code", "?")
        msg = getattr(response, "msg", str(response))
        logger.error("create-chat-failed", code=code, msg=msg)
        if "permission" in str(msg).lower() or code in (230001, 230002):
            raise RuntimeError(
                f"创建群聊失败：飞书应用缺少 im:chat 权限。请在飞书开放平台后台添加该权限。"
                f"\n错误码: {code}, 详情: {msg}"
            )
        raise RuntimeError(f"创建群聊失败 (code={code}): {msg}")

    data = response.data
    chat_id = getattr(data, "chat_id", "") or ""
    logger.info("chat-created", chat_id=chat_id, name=name)
    return ChatInfo(chat_id=chat_id, name=name)
