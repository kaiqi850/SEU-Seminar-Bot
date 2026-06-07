"""@全体成员 command (Feishu/Lark and other platforms supporting AtAll)."""

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.core.platform.message_type import MessageType


class AtAllCommand:
    """Send a group message that @all members."""

    def __init__(self, context: star.Context) -> None:
        self.context = context

    async def atall(self, event: AstrMessageEvent, content: str = "") -> None:
        if event.message_obj.type != MessageType.GROUP_MESSAGE:
            event.set_result(
                MessageEventResult().message("此命令仅可在群聊中使用。"),
            )
            return

        text = (content or "").strip() or "请注意以下通知："
        event.set_result(MessageEventResult().message(text).at_all())
