"""Remind seminar speakers when required seminar_excel information is missing."""

import re
from datetime import date, timedelta
from typing import Any

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core import logger
from astrbot.core.config import AstrBotConfig
from astrbot.core.platform.message_session import MessageSesion
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.lark.lark_members import (
    LarkMemberMatch,
    list_chat_members,
)

from astrbot.builtin_stars.seminar_excel_reader.main import (
    _cfg_get,
    _fetch_configured_sheet_values,
    _find_column_for_label,
    _parse_date_cell,
    _row_value_for_label,
    _trim_table,
)

_INFO_JOB_NAME = "seminar_excel_guard_info_check"
_DEFAULT_REQUIRED_INFO_LABELS = ("时间", "地点", "论文", "会议/期刊", "论文链接", "PPT")
_BRACKET_TEXT_RE = re.compile(r"[（(【\[].*?[）)】\]]")
_NAME_SEPARATORS = ("-", "_", "｜", "|", "/", "\\", "@", " ")


def _clean_label(label: object) -> str:
    return str(label).strip()


def _cfg_list(plugin_cfg: AstrBotConfig | dict | None, key: str, default) -> list[str]:
    raw = _cfg_get(plugin_cfg, key, None)
    if isinstance(raw, list) and raw:
        return [_clean_label(item) for item in raw if _clean_label(item)]
    return list(default)


def _cfg_speaker_aliases(
    plugin_cfg: AstrBotConfig | dict | None,
) -> dict[str, str]:
    raw = _cfg_get(plugin_cfg, "speaker_aliases", None)
    if not isinstance(raw, list):
        return {}
    aliases: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        sheet_name = str(item.get("sheet_name") or "").strip()
        member_name = str(item.get("member_name") or "").strip()
        if sheet_name and member_name:
            aliases[sheet_name] = member_name
    return aliases


def _row_date(row: list[str], headers: list[str]) -> date | None:
    col = _find_column_for_label(headers, "时间")
    if col is None or col >= len(row):
        return None
    return _parse_date_cell(row[col])


def _display_time(row: list[str], headers: list[str]) -> str:
    value = _row_value_for_label(row, headers, "时间")
    return value or "未填写时间"


def _missing_labels(row: list[str], headers: list[str], labels: list[str]) -> list[str]:
    missing: list[str] = []
    for label in labels:
        value = _row_value_for_label(row, headers, label)
        if not value:
            missing.append(label)
    return missing


def _rows_before_deadline(
    values: list[list],
    *,
    reference_day: date,
    notice_days: int,
) -> tuple[list[str], list[list[str]]]:
    table = _trim_table(values)
    if len(table) < 2:
        return [], []
    headers = table[0]
    target_start = reference_day
    target_end = reference_day + timedelta(days=max(notice_days, 0))
    rows: list[list[str]] = []
    for row in table[1:]:
        row_day = _row_date(row, headers)
        if row_day is not None and target_start <= row_day <= target_end:
            rows.append(row)
    return headers, rows


def _group_chat_id_from_session(
    group_session: str,
) -> tuple[str, str] | tuple[None, None]:
    try:
        session = MessageSesion.from_str(group_session)
    except Exception:
        return None, None
    if session.message_type != MessageType.GROUP_MESSAGE:
        return None, None
    chat_id = session.session_id
    if "%" in chat_id:
        chat_id = chat_id.split("%", 1)[1]
    return session.platform_name, chat_id


def _get_lark_client_for_platform(context: star.Context, platform_id: str) -> Any | None:
    for inst in context.platform_manager.platform_insts:
        if inst.meta().id != platform_id:
            continue
        lark_client = getattr(inst, "lark_api", None)
        if lark_client is not None:
            return lark_client
    return None


def _normalize_member_name(name: str) -> str:
    text = _BRACKET_TEXT_RE.sub("", name)
    for sep in _NAME_SEPARATORS:
        text = text.replace(sep, "")
    return "".join(
        ch
        for ch in text.strip().lower()
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff"
    )


def _resolve_member_from_list(
    members: list[LarkMemberMatch],
    speaker: str,
) -> tuple[LarkMemberMatch | None, list[LarkMemberMatch]]:
    query = speaker.strip()
    if query.startswith("ou_"):
        return LarkMemberMatch(open_id=query, name=query), []

    normalized_query = _normalize_member_name(query)
    exact: list[LarkMemberMatch] = []
    partial: list[LarkMemberMatch] = []

    for member in members:
        name = member.name.strip()
        normalized_name = _normalize_member_name(name)
        if name == query or normalized_name == normalized_query:
            exact.append(member)
        elif (
            normalized_query
            and normalized_name
            and (
                normalized_query in normalized_name
                or normalized_name in normalized_query
            )
        ):
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


async def _private_session_for_speaker(
    context: star.Context,
    group_session: str,
    speaker: str,
    *,
    speaker_aliases: dict[str, str] | None = None,
) -> tuple[str | None, str | None]:
    platform_id, chat_id = _group_chat_id_from_session(group_session)
    if not platform_id or not chat_id:
        return None, "group_session 不是有效的群会话 UMO。"

    lark_client = _get_lark_client_for_platform(context, platform_id)
    if lark_client is None:
        return None, f"未找到平台 {platform_id} 对应的飞书客户端。"

    listed = await list_chat_members(lark_client, chat_id)
    if listed.error:
        return None, listed.error
    alias_name = ""
    if speaker_aliases:
        alias_name = speaker_aliases.get(speaker, "").strip()
    unique, candidates = _resolve_member_from_list(listed.members, speaker)
    if unique is None and alias_name:
        if alias_name:
            unique, candidates = _resolve_member_from_list(listed.members, alias_name)
    if unique is None:
        if not candidates:
            sample = "、".join(member.name for member in listed.members[:12])
            suffix = "…" if len(listed.members) > 12 else ""
            alias_note = f"（已尝试别名：{alias_name}）" if alias_name else ""
            return None, (
                f"未在群成员列表中找到「{speaker}」。"
                f"{alias_note}当前读取到的部分群成员：{sample}{suffix}"
            )
        names = "、".join(member.name for member in candidates[:8])
        suffix = "…" if len(candidates) > 8 else ""
        return None, f"找到多位匹配「{speaker}」的群成员：{names}{suffix}。"
    return f"{platform_id}:FriendMessage:{unique.open_id}", None


async def _safe_send(
    context: star.Context,
    session: str,
    text: str,
) -> bool:
    try:
        return await context.send_message(session, MessageChain().message(text))
    except Exception:
        logger.warning(
            "[seminar_excel_guard] failed to send private message",
            exc_info=True,
        )
        return False


async def check_missing_required_info(
    context: star.Context,
    plugin_cfg: AstrBotConfig | dict | None,
    *,
    reference_day: date | None = None,
) -> tuple[int, int]:
    sheet = await _fetch_configured_sheet_values(context, plugin_cfg)
    if not sheet:
        logger.warning("[seminar_excel_guard] failed to load seminar spreadsheet")
        return 0, 0

    group_session = str(_cfg_get(plugin_cfg, "group_session", "") or "").strip()
    if not group_session:
        logger.warning("[seminar_excel_guard] group_session is not configured")
        return 0, 0

    values, _doc_title = sheet
    today = reference_day or date.today()
    notice_days = int(_cfg_get(plugin_cfg, "deadline_notice_days", 1) or 1)
    required_labels = _cfg_list(
        plugin_cfg,
        "required_info_labels",
        _DEFAULT_REQUIRED_INFO_LABELS,
    )
    speaker_label = str(_cfg_get(plugin_cfg, "speaker_label", "汇报人") or "汇报人")
    speaker_aliases = _cfg_speaker_aliases(plugin_cfg)

    headers, rows = _rows_before_deadline(
        values,
        reference_day=today,
        notice_days=notice_days,
    )
    sent = 0
    unresolved = 0
    for row in rows:
        missing = _missing_labels(row, headers, required_labels)
        if not missing:
            continue
        speaker = _row_value_for_label(row, headers, speaker_label)
        if not speaker:
            unresolved += 1
            logger.warning("[seminar_excel_guard] row missing speaker name")
            continue

        private_session, error = await _private_session_for_speaker(
            context,
            group_session,
            speaker,
            speaker_aliases=speaker_aliases,
        )
        if not private_session:
            unresolved += 1
            logger.warning("[seminar_excel_guard] speaker resolve failed: %s", error)
            continue

        seminar_time = _display_time(row, headers)
        text = (
            f"{speaker}同学你好，seminar_excel 中你在 {seminar_time} 的组会信息还未填写完整。\n"
            f"目前缺少：{', '.join(missing)}。\n"
            "请在截止日期前补充到表格中，谢谢。"
        )
        if await _safe_send(context, private_session, text):
            sent += 1
        else:
            unresolved += 1

    logger.info(
        "[seminar_excel_guard] missing info check done, sent=%s unresolved=%s",
        sent,
        unresolved,
    )
    return sent, unresolved


class Main(star.Star):
    def __init__(
        self,
        context: star.Context,
        config: AstrBotConfig | dict | None = None,
    ) -> None:
        self.context = context
        self.plugin_config = config if config is not None else {}
        self._job_id: str | None = None

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        await self._sync_cron_job()

    async def terminate(self) -> None:
        await self._remove_cron_job()

    async def _sync_cron_job(self) -> None:
        cron_mgr = self.context.cron_manager
        if cron_mgr is None:
            return
        await self._remove_cron_job()

        if not _cfg_get(self.plugin_config, "cron_enabled", True):
            return
        if not str(_cfg_get(self.plugin_config, "spreadsheet_url", "") or "").strip():
            logger.warning("[seminar_excel_guard] cron enabled but spreadsheet_url empty")
            return
        if not str(_cfg_get(self.plugin_config, "group_session", "") or "").strip():
            logger.warning("[seminar_excel_guard] cron enabled but group_session empty")
            return

        cron_expr = str(
            _cfg_get(self.plugin_config, "info_check_cron_expression", "0 10 * * *")
            or "0 10 * * *"
        ).strip()
        tz = str(
            _cfg_get(self.plugin_config, "cron_timezone", "Asia/Shanghai")
            or "Asia/Shanghai"
        ).strip()

        async def _handler() -> None:
            await check_missing_required_info(self.context, self.plugin_config)

        try:
            job = await cron_mgr.add_basic_job(
                name=_INFO_JOB_NAME,
                cron_expression=cron_expr,
                handler=_handler,
                description="Remind speakers to fill missing seminar_excel fields",
                timezone=tz,
                enabled=True,
                persistent=True,
            )
            self._job_id = job.job_id
            logger.info("[seminar_excel_guard] scheduled info check cron job")
        except Exception:
            logger.exception("[seminar_excel_guard] failed to schedule cron job")

    async def _remove_cron_job(self) -> None:
        cron_mgr = self.context.cron_manager
        if cron_mgr is None:
            return
        try:
            for job in await cron_mgr.list_jobs("basic"):
                if job.name == _INFO_JOB_NAME:
                    await cron_mgr.delete_job(job.job_id)
            if self._job_id:
                await cron_mgr.delete_job(self._job_id)
        except Exception:
            logger.debug("[seminar_excel_guard] remove cron job failed", exc_info=True)
        self._job_id = None

    @filter.command("seminar_guard_check_info", alias={"seminar检查信息"})
    async def cmd_check_info(self, event: AstrMessageEvent):
        """Manually check missing required seminar information."""
        event.should_call_llm(True)
        sent, unresolved = await check_missing_required_info(
            self.context,
            self.plugin_config,
        )
        yield event.plain_result(
            f"缺信息检查完成：已私聊 {sent} 人，{unresolved} 人未能自动匹配或发送。"
        ).stop_event()

    @filter.command("seminar_guard_list_members", alias={"seminar成员列表"})
    async def cmd_list_members(self, event: AstrMessageEvent):
        """List member names returned by Feishu for debugging name matching."""
        event.should_call_llm(True)
        group_session = str(_cfg_get(self.plugin_config, "group_session", "") or "")
        platform_id, chat_id = _group_chat_id_from_session(group_session.strip())
        if not platform_id or not chat_id:
            yield event.plain_result("group_session 不是有效的群会话 UMO。").stop_event()
            return
        lark_client = _get_lark_client_for_platform(self.context, platform_id)
        if lark_client is None:
            yield event.plain_result(
                f"未找到平台 {platform_id} 对应的飞书客户端。"
            ).stop_event()
            return
        listed = await list_chat_members(lark_client, chat_id)
        if listed.error:
            yield event.plain_result(listed.error).stop_event()
            return
        names = "、".join(member.name for member in listed.members[:50])
        suffix = "…" if len(listed.members) > 50 else ""
        yield event.plain_result(
            f"已读取到 {len(listed.members)} 位群成员：{names}{suffix}"
        ).stop_event()
