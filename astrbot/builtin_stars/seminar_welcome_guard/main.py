"""Welcome new group members and remind seminar_excel requirements."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import star
from astrbot.api.event import MessageChain, filter
from astrbot.builtin_stars.seminar_excel_reader.main import _cfg_get
from astrbot.core import logger
from astrbot.core.config import AstrBotConfig
from astrbot.core.message.components import At
from astrbot.core.platform.message_session import MessageSesion
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.lark.lark_member_join_filter import (
    LarkMemberJoinFilter,
)
from astrbot.core.platform.sources.lark.lark_members import list_chat_members
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

_JOB_NAME = "seminar_welcome_guard_poll_members"


def _cfg_str(plugin_cfg: AstrBotConfig | dict | None, key: str, default: str) -> str:
    return str(_cfg_get(plugin_cfg, key, default) or default).strip()


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


def _get_lark_client_for_platform(
    context: star.Context, platform_id: str
) -> Any | None:
    for inst in context.platform_manager.platform_insts:
        if inst.meta().id != platform_id:
            continue
        lark_client = getattr(inst, "lark_api", None)
        if lark_client is not None:
            return lark_client
    return None


def _state_path(*, platform_id: str, chat_id: str) -> Path:
    base = Path(get_astrbot_plugin_data_path()) / "seminar_welcome_guard"
    base.mkdir(parents=True, exist_ok=True)
    safe_name = f"{platform_id}_{chat_id}".replace(":", "_")
    return base / f"{safe_name}.json"


@dataclass(frozen=True)
class _State:
    seen_open_ids: set[str]


def _load_state(path: Path) -> _State:
    if not path.exists():
        return _State(seen_open_ids=set())
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _State(seen_open_ids=set())
    items = raw.get("seen_open_ids", [])
    if not isinstance(items, list):
        items = []
    return _State(seen_open_ids={str(x).strip() for x in items if str(x).strip()})


def _save_state(path: Path, state: _State) -> None:
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "seen_open_ids": sorted(state.seen_open_ids),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _welcome_text(plugin_cfg: AstrBotConfig | dict | None) -> str:
    prefix = _cfg_str(plugin_cfg, "welcome_text_prefix", "欢迎加入本群！")
    excel_name = _cfg_str(plugin_cfg, "seminar_excel_name", "seminar_excel")
    deadline_time = _cfg_str(plugin_cfg, "fill_deadline_time", "20:00")
    return (
        f"{prefix}\n"
        f"请在汇报前一天 {deadline_time} 之前在云文档 {excel_name} 中填写汇报信息。\n"
        "另外：请私聊机器人发送任意一句话（建立会话），以便后续接收私信提醒。"
    )


async def _poll_and_welcome(
    context: star.Context,
    plugin_cfg: AstrBotConfig | dict | None,
) -> tuple[int, int, bool, str]:
    group_session = _cfg_str(plugin_cfg, "group_session", "")
    platform_id, chat_id = _group_chat_id_from_session(group_session)
    if not platform_id or not chat_id:
        logger.warning("[seminar_welcome_guard] invalid group_session")
        return 0, 0, False, ""

    lark_client = _get_lark_client_for_platform(context, platform_id)
    if lark_client is None:
        logger.warning(
            "[seminar_welcome_guard] lark client not found for %s", platform_id
        )
        return 0, 0, False, ""

    listed = await list_chat_members(lark_client, chat_id)
    if listed.error:
        logger.warning("[seminar_welcome_guard] list members failed: %s", listed.error)
        return 0, 0, False, ""

    state_path = _state_path(platform_id=platform_id, chat_id=chat_id)
    had_state = state_path.exists()
    state = _load_state(state_path)

    current_ids = {m.open_id for m in listed.members if m.open_id}
    if not had_state and not _cfg_get(plugin_cfg, "welcome_on_first_sync", False):
        logger.info(
            "[seminar_welcome_guard] first sync: record %s members, no welcome sent",
            len(current_ids),
        )
        _save_state(state_path, _State(seen_open_ids=current_ids))
        return 0, len(current_ids), False, str(state_path)

    new_ids = current_ids - state.seen_open_ids
    if not new_ids:
        return 0, len(current_ids), had_state, str(state_path)

    text = _welcome_text(plugin_cfg)
    id_to_name = {m.open_id: (m.name or "新成员") for m in listed.members if m.open_id}
    for open_id in sorted(new_ids):
        name = id_to_name.get(open_id, "新成员")
        try:
            await context.send_message(
                group_session,
                MessageChain().at(name, open_id).message(f" {text}"),
            )
        except Exception:
            logger.warning(
                "[seminar_welcome_guard] failed to send welcome message",
                exc_info=True,
            )

    _save_state(state_path, _State(seen_open_ids=state.seen_open_ids | new_ids))
    return len(new_ids), len(current_ids), had_state, str(state_path)


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
        # Legacy polling-based sync remains for non-webhook runtimes.
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
        if _cfg_get(self.plugin_config, "use_join_event", True):
            return
        if not _cfg_str(self.plugin_config, "group_session", ""):
            logger.warning(
                "[seminar_welcome_guard] cron enabled but group_session empty"
            )
            return

        cron_expr = _cfg_str(self.plugin_config, "cron_expression", "*/5 * * * *")
        tz = _cfg_str(self.plugin_config, "cron_timezone", "Asia/Shanghai")

        async def _handler() -> None:
            await _poll_and_welcome(self.context, self.plugin_config)

        try:
            job = await cron_mgr.add_basic_job(
                name=_JOB_NAME,
                cron_expression=cron_expr,
                handler=_handler,
                description="Poll group members and welcome new ones",
                timezone=tz,
                enabled=True,
                persistent=True,
            )
            self._job_id = job.job_id
            logger.info(
                "[seminar_welcome_guard] scheduled cron job: %s (%s)",
                cron_expr,
                tz,
            )
        except Exception:
            logger.exception("[seminar_welcome_guard] failed to schedule cron job")

    async def _remove_cron_job(self) -> None:
        cron_mgr = self.context.cron_manager
        if cron_mgr is None:
            return
        try:
            for job in await cron_mgr.list_jobs("basic"):
                if job.name == _JOB_NAME:
                    await cron_mgr.delete_job(job.job_id)
            if self._job_id:
                await cron_mgr.delete_job(self._job_id)
        except Exception:
            logger.debug(
                "[seminar_welcome_guard] remove cron job failed", exc_info=True
            )
        self._job_id = None

    @filter.custom_filter(LarkMemberJoinFilter, False)
    async def on_lark_member_join(self, event):
        """Welcome new members from Lark join webhook events."""
        group_session = _cfg_str(self.plugin_config, "group_session", "")
        if not group_session:
            return
        if str(event.unified_msg_origin) != str(group_session):
            return

        users = event.get_extra("_lark_member_join_users", [])
        if not isinstance(users, list) or not users:
            return

        text = _welcome_text(self.plugin_config)
        for item in users:
            if not isinstance(item, dict):
                continue
            open_id = str(item.get("user_id") or item.get("open_id") or "").strip()
            if not open_id:
                continue
            name = str(item.get("name") or item.get("user_name") or "新成员").strip()
            try:
                await self.context.send_message(
                    group_session,
                    MessageChain().at(name, open_id).message(f" {text}"),
                )
            except Exception:
                logger.warning(
                    "[seminar_welcome_guard] failed to send welcome message",
                    exc_info=True,
                )

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=80)
    @filter.platform_adapter_type(filter.PlatformAdapterType.LARK)
    async def on_lark_invite_system_message(self, event):
        """Fallback: detect Lark system invite message in group chat."""
        group_session = _cfg_str(self.plugin_config, "group_session", "")
        if not group_session:
            return
        if str(event.unified_msg_origin) != str(group_session):
            return

        text = str(getattr(event, "message_str", "") or "").strip()
        if "邀请" not in text or "加入此群" not in text:
            return

        at_components = [c for c in event.get_messages() if isinstance(c, At)]
        if not at_components:
            return

        invitee = at_components[-1]
        invitee_open_id = str(invitee.qq or "").strip()
        if not invitee_open_id or invitee_open_id == str(event.get_self_id() or ""):
            return

        welcome = _welcome_text(self.plugin_config)
        try:
            await self.context.send_message(
                group_session,
                MessageChain()
                .at(str(invitee.name or "新成员"), invitee_open_id)
                .message(f" {welcome}"),
            )
        except Exception:
            logger.warning(
                "[seminar_welcome_guard] failed to send invite-fallback welcome message",
                exc_info=True,
            )

    @filter.command("welcome_guard_sync", alias={"welcome同步", "welcome检查"})
    async def cmd_welcome_sync(self, event: Any):
        """Manually poll members and welcome new members."""
        event.should_call_llm(True)
        welcomed, total, had_state, state_path = await _poll_and_welcome(
            self.context,
            self.plugin_config,
        )
        note = "已有历史记录" if had_state else "首次同步(默认不欢迎现有成员)"
        yield event.plain_result(
            f"欢迎检查完成：本次欢迎 {welcomed} 人；当前群成员 {total} 人；{note}；state={state_path}"
        ).stop_event()

    @filter.command("welcome_guard_reset", alias={"welcome重置", "welcome清空"})
    async def cmd_welcome_reset(self, event: Any):
        """Clear local welcome state."""
        event.should_call_llm(True)
        group_session = _cfg_str(self.plugin_config, "group_session", "")
        platform_id, chat_id = _group_chat_id_from_session(group_session)
        if not platform_id or not chat_id:
            yield event.plain_result("group_session 无效，无法重置。").stop_event()
            return
        path = _state_path(platform_id=platform_id, chat_id=chat_id)
        try:
            path.unlink(missing_ok=True)
        except Exception:
            logger.warning(
                "[seminar_welcome_guard] failed to remove state file",
                exc_info=True,
            )
            yield event.plain_result(f"重置失败：{path}").stop_event()
            return
        yield event.plain_result(f"已重置欢迎记录：{path}").stop_event()
