"""Feishu/Lark chat member lookup helpers."""

from dataclasses import dataclass

import lark_oapi as lark
from lark_oapi.api.im.v1 import GetChatMembersRequest

from astrbot import logger

_CHAT_MEMBER_NAME_CACHE: dict[tuple[str, str], str] = {}


@dataclass(frozen=True)
class LarkMemberMatch:
    open_id: str
    name: str


@dataclass(frozen=True)
class ListChatMembersResult:
    members: list[LarkMemberMatch]
    error: str | None = None


def _stringify_candidate(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _looks_like_lark_identifier(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    prefixes = ("ou_", "on_", "oc_", "cli_", "usr_", "user_")
    if text.startswith(prefixes):
        return True
    return text.startswith("用户") and text[2:].isdigit()


def remember_chat_member_name(chat_id: str, open_id: str, name: str) -> None:
    chat_key = chat_id.strip()
    open_key = open_id.strip()
    display_name = name.strip()
    if not chat_key or not open_key or not display_name:
        return
    if _looks_like_lark_identifier(display_name):
        return
    _CHAT_MEMBER_NAME_CACHE[(chat_key, open_key)] = display_name


def _cached_chat_member_name(chat_id: str, open_id: str) -> str:
    return _CHAT_MEMBER_NAME_CACHE.get((chat_id.strip(), open_id.strip()), "")


def _extract_member_display_name(item) -> str:
    # Prefer chat-facing nickname/display fields over generic profile names.
    direct_keys = (
        "display_name",
        "chat_nickname",
        "chat_display_name",
        "nickname",
        "tenant_key_name",
        "user_name",
        "employee_name",
        "name",
    )
    for key in direct_keys:
        value = _stringify_candidate(getattr(item, key, None))
        if value:
            return value

    nested_user = getattr(item, "user", None)
    if nested_user is not None:
        for key in direct_keys:
            value = _stringify_candidate(getattr(nested_user, key, None))
            if value:
                return value

    if hasattr(item, "__dict__"):
        for key, value in item.__dict__.items():
            text = _stringify_candidate(value)
            if key != "member_id" and text:
                return text
    return ""


def _is_permission_denied(code: int | None, msg: str | None) -> bool:
    text = (msg or "").lower()
    return (
        code == 99991672
        or "access denied" in text
        or "scope" in text
        or "权限" in (msg or "")
    )


async def list_chat_members(
    lark_client: lark.Client,
    chat_id: str,
    *,
    page_size: int = 100,
) -> ListChatMembersResult:
    """List all members in a chat (paginated). Requires im:chat.members:read or similar."""
    if lark_client.im is None:
        logger.error("[Lark] API Client im 模块未初始化")
        return ListChatMembersResult(members=[], error="飞书 API 未初始化。")

    members: list[LarkMemberMatch] = []
    page_token: str | None = None

    while True:
        builder = (
            GetChatMembersRequest.builder()
            .chat_id(chat_id)
            .member_id_type("open_id")
            .page_size(page_size)
        )
        if page_token:
            builder = builder.page_token(page_token)

        response = await lark_client.im.v1.chat_members.aget(builder.build())
        if not response.success():
            logger.error(
                f"[Lark] 获取群成员失败 chat_id={chat_id}, "
                f"code={response.code}, msg={response.msg}",
            )
            if _is_permission_denied(response.code, response.msg):
                return ListChatMembersResult(
                    members=[],
                    error=(
                        "飞书应用缺少「查看群成员」权限，无法按姓名 @ 成员。"
                        "请在飞书开发者后台开通 im:chat.members:read（或 im:chat:readonly），"
                        "发布新版本后重试。"
                    ),
                )
            return ListChatMembersResult(
                members=[],
                error=f"获取群成员失败（code={response.code}）：{response.msg}",
            )

        if response.data and response.data.items:
            for item in response.data.items:
                member_id = (item.member_id or "").strip()
                if not member_id:
                    continue
                display_name = _cached_chat_member_name(chat_id, member_id)
                if not display_name:
                    display_name = _extract_member_display_name(item) or member_id
                remember_chat_member_name(chat_id, member_id, display_name)
                if display_name.startswith("用户") and hasattr(item, "__dict__"):
                    logger.debug(
                        "[Lark] suspicious member display name=%s fields=%s",
                        display_name,
                        sorted(item.__dict__.keys()),
                    )
                members.append(LarkMemberMatch(open_id=member_id, name=display_name))

        if not response.data or not response.data.has_more:
            break
        page_token = response.data.page_token
        if not page_token:
            break

    return ListChatMembersResult(members=members)


def find_members_by_name(
    members: list[LarkMemberMatch],
    name_query: str,
) -> tuple[LarkMemberMatch | None, list[LarkMemberMatch]]:
    """Resolve a display name to a single member, or return ambiguous matches."""
    query = name_query.strip()
    if not query:
        return None, []

    if query.startswith("ou_"):
        return LarkMemberMatch(open_id=query, name=query), []

    exact: list[LarkMemberMatch] = []
    partial: list[LarkMemberMatch] = []

    for member in members:
        name = member.name
        if name == query:
            exact.append(member)
        elif query in name or name in query:
            partial.append(member)

    if len(exact) == 1:
        return exact[0], exact
    if len(exact) > 1:
        return None, exact

    if len(partial) == 1:
        return partial[0], partial
    if len(partial) > 1:
        return None, partial

    return None, []


async def resolve_chat_member_by_name(
    lark_client: lark.Client,
    chat_id: str,
    name_query: str,
) -> tuple[LarkMemberMatch | None, list[LarkMemberMatch], str | None]:
    listed = await list_chat_members(lark_client, chat_id)
    if listed.error:
        return None, [], listed.error
    unique, candidates = find_members_by_name(listed.members, name_query)
    return unique, candidates, None
