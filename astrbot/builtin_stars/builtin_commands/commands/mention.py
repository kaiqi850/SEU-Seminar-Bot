"""@指定群成员 command and shared mention logic for Feishu/Lark."""

from astrbot import logger
from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
from astrbot.core.platform.sources.lark.lark_members import resolve_chat_member_by_name


class MentionCommand:
    """Mention a specific group member by display name (Feishu/Lark)."""

    def __init__(self, context: star.Context) -> None:
        self.context = context

    async def build_mention_result(
        self,
        event: AstrMessageEvent,
        member_name: str,
        content: str = "",
    ) -> MessageEventResult | str:
        if event.message_obj.type != MessageType.GROUP_MESSAGE:
            return "此功能仅可在群聊中使用。"

        if not isinstance(event, LarkMessageEvent):
            name = member_name.strip()
            if name.startswith("ou_"):
                text = (content or "").strip() or "你好"
                return MessageEventResult().at(name, name).message(text)
            return "按姓名 @ 成员目前仅支持飞书（lark）平台。也可直接使用 open_id，例如：/at ou_xxx 消息内容"

        chat_id = event.get_group_id()
        if not chat_id:
            return "无法获取当前群聊 ID。"

        unique, candidates, list_error = await resolve_chat_member_by_name(
            event.bot,
            chat_id,
            member_name,
        )

        if list_error:
            return list_error

        if unique is None:
            if not candidates:
                return (
                    f"未在群内找到名为「{member_name.strip()}」的成员，"
                    "请确认姓名与群内显示名称一致。"
                )
            names = "、".join(m.name for m in candidates[:8])
            suffix = "…" if len(candidates) > 8 else ""
            return f"找到多位匹配成员：{names}{suffix}，请提供更完整的姓名。"

        text = (content or "").strip()
        result = MessageEventResult().at(unique.name, unique.open_id)
        # Feishu post requires non-empty text alongside @; use a space if no message.
        result.message(text if text else " ")
        logger.info(
            f"[Lark] @成员: name={unique.name}, open_id={unique.open_id}, "
            f"query={member_name.strip()!r}",
        )
        return result

    async def at_user(
        self,
        event: AstrMessageEvent,
        name: str,
        content: str = "",
    ) -> None:
        outcome = await self.build_mention_result(event, name, content)
        if isinstance(outcome, MessageEventResult):
            event.set_result(outcome)
        else:
            event.set_result(MessageEventResult().message(outcome))

    async def mention_via_llm_tool(
        self,
        event: AstrMessageEvent,
        member_name: str,
        message: str = "",
    ) -> MessageEventResult | str:
        """Build mention result; caller should yield MessageEventResult for tool_direct send."""
        return await self.build_mention_result(event, member_name, message)
