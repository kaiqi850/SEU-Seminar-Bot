from astrbot.api import llm_tool, star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter

from .commands import (
    AdminCommands,
    AtAllCommand,
    ConversationCommands,
    HelpCommand,
    MentionCommand,
    ProviderCommands,
    SetUnsetCommands,
    SIDCommand,
)


class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context

        self.admin_c = AdminCommands(self.context)
        self.conversation_c = ConversationCommands(self.context)
        self.help_c = HelpCommand(self.context)
        self.provider_c = ProviderCommands(self.context)
        self.setunset_c = SetUnsetCommands(self.context)
        self.sid_c = SIDCommand(self.context)
        self.atall_c = AtAllCommand(self.context)
        self.mention_c = MentionCommand(self.context)

    @filter.command("help")
    async def help(self, event: AstrMessageEvent) -> None:
        """Show help message"""
        await self.help_c.help(event)

    @filter.command("sid")
    async def sid(self, event: AstrMessageEvent) -> None:
        """Get session ID and other related information"""
        await self.sid_c.sid(event)

    @filter.command("reset")
    async def reset(self, message: AstrMessageEvent) -> None:
        """Reset conversation history"""
        await self.conversation_c.reset(message)

    @filter.command("stop")
    async def stop(self, message: AstrMessageEvent) -> None:
        """Stop agent execution"""
        await self.conversation_c.stop(message)

    @filter.command("new")
    async def new_conv(self, message: AstrMessageEvent) -> None:
        """Create new conversation"""
        await self.conversation_c.new_conv(message)

    @filter.command("stats")
    async def stats(self, message: AstrMessageEvent) -> None:
        """Show token usage statistics for the current conversation"""
        await self.conversation_c.stats(message)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("provider")
    async def provider(
        self,
        event: AstrMessageEvent,
        idx: str | int | None = None,
        idx2: int | None = None,
    ) -> None:
        """View or switch LLM Provider"""
        await self.provider_c.provider(event, idx, idx2)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("dashboard_update")
    async def update_dashboard(self, event: AstrMessageEvent) -> None:
        """Update AstrBot WebUI"""
        await self.admin_c.update_dashboard(event)

    @filter.command("set")
    async def set_variable(self, event: AstrMessageEvent, key: str, value: str) -> None:
        """Set session variable"""
        await self.setunset_c.set_variable(event, key, value)

    @filter.command("unset")
    async def unset_variable(self, event: AstrMessageEvent, key: str) -> None:
        """Unset session variable"""
        await self.setunset_c.unset_variable(event, key)

    @filter.command("atall", alias={"at_all", "全员", "艾特全体"})
    async def atall(self, event: AstrMessageEvent, content: str = "") -> None:
        """Send a group message that @all members (Feishu: requires group @all enabled)."""
        await self.atall_c.atall(event, content)

    @filter.command("at", alias={"艾特", "mention"})
    async def at_user(
        self,
        event: AstrMessageEvent,
        name: str,
        content: str = "",
    ) -> None:
        """Mention a group member by name (Feishu/Lark)."""
        await self.mention_c.at_user(event, name, content)

    @llm_tool("mention_group_member")
    async def mention_group_member(
        self,
        event: AstrMessageEvent,
        member_name: str,
        message: str = "",
    ):
        """在群聊中 @ 指定成员并发送消息。当用户要求艾特、提及某位群成员时调用。

        Args:
            member_name(string): 成员姓名（支持模糊匹配）
            message(string): 随 @ 一起发送的文本，可为空
        """
        outcome = await self.mention_c.mention_via_llm_tool(
            event,
            member_name,
            message,
        )
        if isinstance(outcome, MessageEventResult):
            event.set_extra("agent_stop_requested", True)
            yield outcome
            event.stop_event()
            return
        yield outcome
