"""WuKongIM (悟空IM) message event.

Status: DRAFT — see ``wukongim_adapter.py`` module docstring.

This is the AstrBot ``AstrMessageEvent`` subclass used for inbound WuKongIM
messages. It holds a back-reference to the adapter so ``send()`` can reach
the shared aiohttp ClientSession + bot credentials without rebuilding them
per-call (mirrors how ``LarkMessageEvent`` keeps a ``lark.Client`` handle).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.core.platform.message_type import MessageType

if TYPE_CHECKING:  # avoid circular import at runtime
    from .wukongim_adapter import WuKongIMPlatformAdapter


class WuKongIMMessageEvent(AstrMessageEvent):
    """An inbound WuKongIM message + helpers to reply to it."""

    def __init__(
        self,
        message_str: str,
        message_obj,
        platform_meta,
        session_id: str,
        adapter: "WuKongIMPlatformAdapter",
    ) -> None:
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.adapter = adapter

    async def send(self, message: MessageChain) -> None:
        """Send a reply MessageChain through the WuKongIM HTTP API."""
        channel_id, channel_type = self._resolve_target()

        if not channel_id:
            logger.error(
                "[WuKongIM] WuKongIMMessageEvent.send: empty channel_id; "
                "dropping reply",
            )
        else:
            try:
                await self.adapter.send_chain(
                    channel_id=channel_id,
                    channel_type=channel_type,
                    message_chain=message,
                )
            except Exception as e:
                logger.error(
                    f"[WuKongIM] WuKongIMMessageEvent.send failed: {e}",
                    exc_info=True,
                )

        # Run the framework-level side-effects (metrics, history, ...)
        await super().send(message)

    def _resolve_target(self) -> tuple[str, int]:
        """Pick (channel_id, channel_type) from the original inbound message.

        Group  -> use the group_id as channel_id, channel_type=2.
        Friend -> use the sender's user_id as channel_id, channel_type=1.
        Customer service inbound is tagged ``cs:<channel_id>`` in the
        session_id; strip the prefix and use channel_type=3.
        """
        # Import locally to keep the module dependency-free at top level.
        from .wukongim_adapter import (
            WK_CHANNEL_TYPE_CUSTOMER_SERVICE,
            WK_CHANNEL_TYPE_GROUP,
            WK_CHANNEL_TYPE_PERSON,
        )

        msg = self.message_obj

        if msg.type == MessageType.GROUP_MESSAGE:
            return msg.group_id or "", WK_CHANNEL_TYPE_GROUP

        sid = msg.session_id or ""
        if sid.startswith("cs:"):
            return sid[3:], WK_CHANNEL_TYPE_CUSTOMER_SERVICE

        # Default: 1-1 private chat — reply to whoever sent it
        if msg.sender and msg.sender.user_id:
            return msg.sender.user_id, WK_CHANNEL_TYPE_PERSON

        return sid, WK_CHANNEL_TYPE_PERSON
