"""Custom filter for detecting Lark group member join events.

AstrBot currently only converts `im.message.receive_v1` into message events. For
other webhook event types, we inject a synthetic `AstrBotMessage` and tag it
with extras so plugins can react without changing the core event model.
"""

from __future__ import annotations

from astrbot.core.config import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.star.filter.custom_filter import CustomFilter


class LarkMemberJoinFilter(CustomFilter):
    """Match synthetic events produced for Lark member-join webhooks."""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        # get_platform_id() is the instance id (e.g. "lark-seminar"), not the adapter type.
        if event.get_platform_name() != "lark":
            return False
        return bool(event.get_extra("_lark_member_join", False))
