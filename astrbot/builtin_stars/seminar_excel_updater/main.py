"""Update seminar bitable records from private Lark messages."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import shutil
import socket
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
import lark_oapi as lark
from lark_oapi.api.drive.v1 import UploadAllMediaRequest, UploadAllMediaRequestBody
from lark_oapi.core.token.manager import TokenManager

import astrbot.api.message_components as Comp
from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.builtin_stars.seminar_excel_guard.main import (
    _get_lark_client_for_platform,
    _group_chat_id_from_session,
)
from astrbot.builtin_stars.seminar_excel_reader.main import (
    _bitable_get,
    _cfg_get,
    _configured_cloud_token,
    _format_bitable_cell,
    _lark_api_base,
    _read_markdown_from_bytes,
)
from astrbot.core import logger
from astrbot.core.config import AstrBotConfig
from astrbot.core.platform.sources.lark.lark_members import list_chat_members
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

_URL_RE = re.compile(r"https?://[^\s，。；;）)]+", re.I)
_ARXIV_RE = re.compile(
    r"https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?",
    re.I,
)
_DOI_RE = re.compile(
    r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)(10\.\d{4,9}/[^\s<>\"]+)", re.I
)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.S)
_SOURCE_TEXT_LIMIT = 30_000
_WEEK_MS = 7 * 24 * 60 * 60 * 1000


@dataclass
class PaperMetadata:
    title: str = ""
    venue: str = ""
    paper_link: str = ""
    document_kind: str = "unknown"


@dataclass
class UserState:
    title: str = ""
    venue: str = ""
    speaker: str = ""
    paper_link: str = ""
    pending_field: str = ""
    pending_pdf_path: str = ""
    pending_pdf_name: str = ""
    source_kind: str = ""
    proposed_meeting_time: str = ""
    last_title: str = ""


@dataclass(frozen=True)
class TableSchema:
    title: str
    venue: str
    speaker: str
    paper_link: str
    time: str
    location: str
    meeting_link: str
    ppt: str
    field_types: dict[str, int]


@dataclass(frozen=True)
class BitableTable:
    client: lark.Client
    api_base: str
    tenant_token: str
    app_token: str
    table_id: str
    schema: TableSchema
    records: list[dict[str, Any]]


@dataclass(frozen=True)
class UpsertPlan:
    record_id: str | None
    fields: dict[str, Any]


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _normalize_title(value: object) -> str:
    text = unicodedata.normalize("NFKC", _clean_text(value)).casefold()
    return " ".join(text.split())


def _looks_like_identifier(value: str) -> bool:
    text = value.strip()
    return not text or text.startswith(("ou_", "on_", "oc_")) or len(text) == 8


def _state_root() -> Path:
    root = Path(get_astrbot_plugin_data_path()) / "seminar_excel_updater"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _state_path(platform_id: str, sender_id: str) -> Path:
    key = hashlib.sha256(f"{platform_id}:{sender_id}".encode()).hexdigest()[:24]
    return _state_root() / f"{key}.json"


def _load_state(path: Path) -> UserState:
    if not path.exists():
        return UserState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        logger.warning("[seminar_excel_updater] failed to read state %s", path)
        return UserState()
    if not isinstance(raw, dict):
        return UserState()
    allowed = UserState.__dataclass_fields__
    return UserState(**{key: _clean_text(raw.get(key)) for key in allowed})


def _save_state(path: Path, state: UserState) -> None:
    path.write_text(
        json.dumps(asdict(state), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _remove_pending_pdf(state: UserState) -> None:
    if state.pending_pdf_path:
        try:
            Path(state.pending_pdf_path).unlink(missing_ok=True)
        except OSError:
            logger.debug(
                "[seminar_excel_updater] failed to remove pending PDF",
                exc_info=True,
            )
    state.pending_pdf_path = ""
    state.pending_pdf_name = ""


def _copy_pending_pdf(source: Path, filename: str, state: UserState) -> None:
    _remove_pending_pdf(state)
    suffix = source.suffix.lower() or ".pdf"
    target = (
        _state_root()
        / f"pending_{hashlib.sha256(source.read_bytes()).hexdigest()[:20]}{suffix}"
    )
    shutil.copy2(source, target)
    state.pending_pdf_path = str(target)
    state.pending_pdf_name = filename


def _extract_json_object(text: str) -> dict[str, Any]:
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        return {}
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _doi_link(source_text: str) -> str:
    match = _DOI_RE.search(source_text[:15_000])
    if not match:
        return ""
    doi = match.group(1).rstrip(".,;:)]}")
    return f"https://doi.org/{doi}"


def _canonical_link(
    source_text: str,
    source_url: str = "",
    *,
    prefer_doi: bool = False,
) -> str:
    doi_link = _doi_link(source_text)
    if prefer_doi and doi_link:
        return doi_link
    if source_url:
        return source_url
    match = _ARXIV_RE.search(source_text)
    if match:
        return f"https://arxiv.org/abs/{match.group(1)}"
    return doi_link


def _source_metadata_value(source_text: str, *labels: str) -> str:
    prefixes = tuple(f"{label}:".casefold() for label in labels)
    for line in source_text.splitlines()[:100]:
        stripped = line.strip()
        folded = stripped.casefold()
        for prefix in prefixes:
            if folded.startswith(prefix):
                return stripped[len(prefix) :].strip()
    return ""


async def _extract_paper_metadata(
    context: star.Context,
    source_text: str,
    *,
    source_url: str = "",
    prefer_doi: bool = False,
    umo: str | None = None,
) -> PaperMetadata:
    fallback_link = _canonical_link(
        source_text,
        source_url,
        prefer_doi=prefer_doi,
    )
    source_title = _source_metadata_value(source_text, "Title")
    source_venue = _source_metadata_value(source_text, "Conference", "Journal")
    provider = context.get_using_provider(umo)
    if provider is None:
        return PaperMetadata(
            title=source_title,
            venue=source_venue,
            paper_link=fallback_link,
        )

    prompt = (
        "请从下面的网页或 PDF 文本中抽取论文信息。只返回一个 JSON 对象，不要 Markdown。"
        "字段固定为 title、venue、paper_link、document_kind。"
        "title 是论文完整标题；venue 是会议或期刊名称，无法确定就留空；"
        "paper_link 优先使用论文正式网页、arXiv abs 或 DOI 链接，无法确定就留空；"
        "document_kind 只能是 paper、slides、unknown，其中演示幻灯片或汇报 PPT 填 slides。"
        "不得猜测文本中没有的信息。\n"
    )
    if source_url:
        prompt += f"来源网址：{source_url}\n"
    prompt += f"文本：\n{source_text[:_SOURCE_TEXT_LIMIT]}"
    try:
        response = await provider.text_chat(prompt=prompt)
    except Exception:
        logger.exception("[seminar_excel_updater] metadata LLM request failed")
        return PaperMetadata(paper_link=fallback_link)

    payload = _extract_json_object(
        _clean_text(getattr(response, "completion_text", ""))
    )
    kind = _clean_text(payload.get("document_kind")).lower()
    if kind not in {"paper", "slides", "unknown"}:
        kind = "unknown"
    extracted_link = _clean_text(payload.get("paper_link"))
    if prefer_doi and fallback_link.startswith("https://doi.org/"):
        extracted_link = fallback_link
    return PaperMetadata(
        title=source_title or _clean_text(payload.get("title")),
        venue=source_venue or _clean_text(payload.get("venue")),
        paper_link=extracted_link or fallback_link,
        document_kind=kind,
    )


async def _is_public_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    hostname = parsed.hostname
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except OSError:
            return False
        addresses = list({ipaddress.ip_address(item[4][0]) for item in infos})
    return bool(addresses) and all(address.is_global for address in addresses)


async def _download_public_url(url: str, max_bytes: int) -> tuple[bytes, str, str]:
    current = url
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as http:
        for _ in range(6):
            if not await _is_public_http_url(current):
                raise ValueError("链接地址不是允许访问的公网 HTTP/HTTPS 地址。")
            async with http.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("论文链接重定向缺少目标地址。")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > max_bytes:
                    raise ValueError("链接内容超过配置的大小上限。")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("链接内容超过配置的大小上限。")
                    chunks.append(chunk)
                return (
                    b"".join(chunks),
                    response.headers.get("content-type", ""),
                    current,
                )
    raise ValueError("论文链接重定向次数过多。")


async def _text_from_url(url: str, max_bytes: int) -> tuple[str, str]:
    data, content_type, final_url = await _download_public_url(url, max_bytes)
    filename = Path(urlparse(final_url).path).name or "paper.html"
    normalized_content_type = content_type.casefold()
    suffix = Path(filename).suffix.casefold()
    if "pdf" in normalized_content_type and suffix != ".pdf":
        filename += ".pdf"
    elif "html" in normalized_content_type and suffix not in {".html", ".htm"}:
        filename += ".html"
    text = await asyncio.to_thread(_read_markdown_from_bytes, data, filename)
    return text, final_url


async def _text_from_pdf(path: Path, max_bytes: int) -> str:
    if path.stat().st_size > max_bytes:
        raise ValueError("PDF 超过配置的大小上限。")
    return await asyncio.to_thread(
        _read_markdown_from_bytes, path.read_bytes(), path.name
    )


def _configured_field(plugin_cfg: AstrBotConfig | dict | None, key: str) -> str:
    return _clean_text(_cfg_get(plugin_cfg, key, ""))


def _resolve_field(
    field_items: list[dict[str, Any]],
    configured: str,
    aliases: tuple[str, ...],
    *,
    allow_prefix: bool = False,
) -> str:
    names = [_clean_text(item.get("field_name")) for item in field_items]
    for candidate in (configured, *aliases):
        if not candidate:
            continue
        for name in names:
            if name == candidate:
                return name
    if allow_prefix:
        candidates = tuple(x.casefold() for x in (configured, *aliases) if x)
        for name in names:
            lowered = name.casefold()
            if any(lowered.startswith(candidate) for candidate in candidates):
                return name
    return ""


def _resolve_schema(
    field_items: list[dict[str, Any]],
    plugin_cfg: AstrBotConfig | dict | None,
) -> TableSchema:
    field_types = {
        _clean_text(item.get("field_name")): int(item.get("type") or 0)
        for item in field_items
        if _clean_text(item.get("field_name"))
    }
    schema = TableSchema(
        title=_resolve_field(
            field_items,
            _configured_field(plugin_cfg, "title_field"),
            ("论文标题", "论文"),
        ),
        venue=_resolve_field(
            field_items,
            _configured_field(plugin_cfg, "venue_field"),
            ("会议/期刊", "会议", "期刊"),
        ),
        speaker=_resolve_field(
            field_items,
            _configured_field(plugin_cfg, "speaker_field"),
            ("汇报人", "报告人"),
        ),
        paper_link=_resolve_field(
            field_items,
            _configured_field(plugin_cfg, "paper_link_field"),
            ("论文链接", "网站"),
        ),
        time=_resolve_field(
            field_items,
            _configured_field(plugin_cfg, "time_field"),
            ("组会汇报时间", "时间", "日期"),
        ),
        location=_resolve_field(
            field_items,
            _configured_field(plugin_cfg, "location_field"),
            ("地点", "会议地点"),
        ),
        meeting_link=_resolve_field(
            field_items,
            _configured_field(plugin_cfg, "meeting_link_field"),
            ("腾讯会议", "腾讯会议链接"),
        ),
        ppt=_resolve_field(
            field_items,
            _configured_field(plugin_cfg, "ppt_field"),
            ("PPT", "ppt"),
            allow_prefix=True,
        ),
        field_types=field_types,
    )
    missing = [
        label
        for label, value in (
            ("论文标题", schema.title),
            ("会议/期刊", schema.venue),
            ("汇报人", schema.speaker),
            ("论文链接", schema.paper_link),
            ("组会汇报时间", schema.time),
            ("地点", schema.location),
            ("腾讯会议", schema.meeting_link),
            ("PPT", schema.ppt),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"多维表格缺少字段：{', '.join(missing)}。")
    return schema


async def _load_bitable_table(
    context: star.Context,
    plugin_cfg: AstrBotConfig | dict | None,
) -> BitableTable:
    configured = _configured_cloud_token(plugin_cfg)
    if not configured:
        raise ValueError("请先在插件配置中填写飞书多维表格链接。")
    kind, app_token, table_id = configured
    if kind != "bitable":
        raise ValueError("自动更新目前只支持包含 /base/ 的飞书多维表格。")

    group_session = _clean_text(_cfg_get(plugin_cfg, "group_session", ""))
    platform_id, _chat_id = _group_chat_id_from_session(group_session)
    lark_client = (
        _get_lark_client_for_platform(context, platform_id) if platform_id else None
    )
    if lark_client is None:
        for inst in context.platform_manager.platform_insts:
            if inst.meta().name == "lark":
                lark_client = getattr(inst, "lark_api", None)
                if lark_client is not None:
                    break
    if lark_client is None or lark_client._config is None:
        raise ValueError("没有找到可用的飞书客户端。")

    tenant_token = await asyncio.to_thread(
        TokenManager.get_self_tenant_token,
        lark_client._config,
    )
    api_base = _lark_api_base(lark_client)
    if not table_id:
        tables_data = await _bitable_get(
            api_base=api_base,
            tenant_token=tenant_token,
            path=f"/open-apis/bitable/v1/apps/{app_token}/tables",
            params={"page_size": 100},
        )
        items = (tables_data or {}).get("items")
        if not isinstance(items, list) or not items:
            raise ValueError("多维表格中没有可用的数据表。")
        table_id = _clean_text(items[0].get("table_id"))
    if not table_id:
        raise ValueError("无法确定多维表格 table_id。")

    fields_data = await _bitable_get(
        api_base=api_base,
        tenant_token=tenant_token,
        path=f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
        params={"page_size": 100},
    )
    field_items = (fields_data or {}).get("items")
    if not isinstance(field_items, list):
        raise ValueError("无法读取多维表格字段。")
    schema = _resolve_schema(field_items, plugin_cfg)

    records: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params: dict[str, str | int] = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        records_data = await _bitable_get(
            api_base=api_base,
            tenant_token=tenant_token,
            path=f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            params=params,
        )
        if not records_data:
            break
        items = records_data.get("items")
        if isinstance(items, list):
            records.extend(item for item in items if isinstance(item, dict))
        if not records_data.get("has_more"):
            break
        page_token = _clean_text(records_data.get("page_token"))
        if not page_token:
            break

    return BitableTable(
        client=lark_client,
        api_base=api_base,
        tenant_token=tenant_token,
        app_token=app_token,
        table_id=table_id,
        schema=schema,
        records=records,
    )


def _link_cell(schema: TableSchema, field_name: str, url: str) -> Any:
    if schema.field_types.get(field_name) == 15:
        return {"link": url, "text": url}
    return url


def _build_upsert_plan(
    records: list[dict[str, Any]],
    schema: TableSchema,
    metadata: PaperMetadata,
    speaker: str,
    *,
    scheduled_time_override: int | None = None,
    now_ms: int | None = None,
) -> UpsertPlan:
    title_key = _normalize_title(metadata.title)
    title_matches = []
    for record in records:
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue
        if (
            _normalize_title(_format_bitable_cell(fields.get(schema.title)))
            == title_key
        ):
            title_matches.append(record)
    if len(title_matches) > 1:
        raise ValueError("表格中存在多条同名论文记录，已停止更新，请先合并重复记录。")

    target_record: dict[str, Any] | None = title_matches[0] if title_matches else None

    fields: dict[str, Any] = {
        schema.title: metadata.title,
        schema.venue: metadata.venue,
        schema.speaker: speaker,
        schema.paper_link: _link_cell(schema, schema.paper_link, metadata.paper_link),
    }
    target_fields = target_record.get("fields") if target_record else {}
    if not isinstance(target_fields, dict):
        target_fields = {}
    if not isinstance(target_fields.get(schema.time), (int, float)):
        timed_records: list[tuple[int, dict[str, Any]]] = []
        for record in records:
            candidate = record.get("fields")
            if not isinstance(candidate, dict):
                continue
            raw_time = candidate.get(schema.time)
            if isinstance(raw_time, (int, float)):
                timed_records.append((int(raw_time), candidate))
        if not timed_records:
            raise ValueError("表格中没有可沿用的上一条组会时间，无法自动安排记录。")

        if scheduled_time_override is not None:
            scheduled_time = scheduled_time_override
        else:
            scheduled_time = _next_meeting_time(records, schema, now_ms=now_ms)
            if scheduled_time is None:
                scheduled_time = (
                    max(value for value, _candidate in timed_records) + _WEEK_MS
                )
        _reference_time, reference_fields = min(
            timed_records,
            key=lambda item: (abs(item[0] - scheduled_time), -item[0]),
        )
        fields[schema.time] = scheduled_time
        fields[schema.location] = reference_fields.get(schema.location, "")
        fields[schema.meeting_link] = reference_fields.get(schema.meeting_link, "")
    record_id = _clean_text(target_record.get("record_id")) if target_record else ""
    if target_record is not None and not record_id:
        raise ValueError("匹配到的表格记录缺少 record_id，已停止更新。")
    return UpsertPlan(record_id=record_id or None, fields=fields)


async def _write_record(
    table: BitableTable,
    plan: UpsertPlan,
) -> str:
    base_path = (
        f"/open-apis/bitable/v1/apps/{table.app_token}/tables/{table.table_id}/records"
    )
    method = "POST"
    path = base_path
    action = "新增"
    if plan.record_id:
        method = "PUT"
        path = f"{base_path}/{plan.record_id}"
        action = "更新"
    headers = {"Authorization": f"Bearer {table.tenant_token}"}
    async with httpx.AsyncClient(timeout=60.0) as http:
        response = await http.request(
            method,
            f"{table.api_base}{path}",
            headers=headers,
            json={"fields": plan.fields},
        )
    try:
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPStatusError, ValueError) as exc:
        raise ValueError(
            f"飞书多维表格写入失败（HTTP {response.status_code}）。"
        ) from exc
    if payload.get("code") != 0:
        raise ValueError(
            f"飞书多维表格写入失败：{payload.get('msg') or payload.get('code')}"
        )
    return action


def _format_scheduled_time(value: Any, timezone_name: str) -> str:
    if not isinstance(value, (int, float)):
        return "沿用原记录"
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception:
        timezone = ZoneInfo("Asia/Shanghai")
    return datetime.fromtimestamp(value / 1000, timezone).strftime("%Y-%m-%d %H:%M")


def _title_exists(
    records: list[dict[str, Any]],
    schema: TableSchema,
    title: str,
) -> bool:
    title_key = _normalize_title(title)
    return any(
        isinstance(fields := record.get("fields"), dict)
        and _normalize_title(_format_bitable_cell(fields.get(schema.title)))
        == title_key
        for record in records
    )


def _next_meeting_time(
    records: list[dict[str, Any]],
    schema: TableSchema,
    *,
    now_ms: int | None = None,
) -> int | None:
    current_ms = (
        now_ms if now_ms is not None else int(datetime.now().timestamp() * 1000)
    )
    future_times: list[int] = []
    for record in records:
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue
        raw_time = fields.get(schema.time)
        if isinstance(raw_time, (int, float)) and int(raw_time) > current_ms:
            future_times.append(int(raw_time))
    return min(future_times) if future_times else None


def _upcoming_speaker_time(
    records: list[dict[str, Any]],
    schema: TableSchema,
    speaker: str,
    *,
    now_ms: int | None = None,
) -> int | None:
    speaker_key = _normalize_title(speaker)
    if not speaker_key:
        return None
    next_meeting = _next_meeting_time(records, schema, now_ms=now_ms)
    if next_meeting is None:
        return None
    for record in records:
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue
        record_speaker = _normalize_title(
            _format_bitable_cell(fields.get(schema.speaker))
        )
        raw_time = fields.get(schema.time)
        if (
            record_speaker == speaker_key
            and isinstance(raw_time, (int, float))
            and int(raw_time) == next_meeting
        ):
            return next_meeting
    return None


def _parse_custom_meeting_date(
    value: str,
    reference_time_ms: int,
    timezone_name: str,
) -> int | None:
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception:
        timezone = ZoneInfo("Asia/Shanghai")
    reference = datetime.fromtimestamp(reference_time_ms / 1000, timezone)
    text = value.strip()
    full = re.fullmatch(r"(\d{4})[./\-年](\d{1,2})[./\-月](\d{1,2})日?", text)
    short = re.fullmatch(r"(\d{1,2})[./\-月](\d{1,2})日?", text)
    if full:
        year, month, day = (int(part) for part in full.groups())
    elif short:
        year = reference.year
        month, day = (int(part) for part in short.groups())
    else:
        return None
    try:
        scheduled = datetime(
            year,
            month,
            day,
            reference.hour,
            reference.minute,
            tzinfo=timezone,
        )
    except ValueError:
        return None
    return int(scheduled.timestamp() * 1000)


async def _upsert_paper(
    context: star.Context,
    plugin_cfg: AstrBotConfig | dict | None,
    metadata: PaperMetadata,
    speaker: str,
    *,
    scheduled_time_override: int | None = None,
) -> tuple[str, int | None]:
    table = await _load_bitable_table(context, plugin_cfg)
    plan = _build_upsert_plan(
        table.records,
        table.schema,
        metadata,
        speaker,
        scheduled_time_override=scheduled_time_override,
    )
    action = await _write_record(table, plan)
    timezone_name = _clean_text(_cfg_get(plugin_cfg, "cron_timezone", "Asia/Shanghai"))
    scheduled = _format_scheduled_time(
        plan.fields.get(table.schema.time), timezone_name
    )
    scheduled_value = plan.fields.get(table.schema.time)
    scheduled_ms = (
        int(scheduled_value) if isinstance(scheduled_value, (int, float)) else None
    )
    return f"已{action}论文记录：{metadata.title}\n组会时间：{scheduled}", scheduled_ms


def _find_record_by_title(
    records: list[dict[str, Any]],
    title_field: str,
    title: str,
    *,
    speaker_field: str = "",
    speaker: str = "",
) -> dict[str, Any]:
    title_key = _normalize_title(title)
    matches = []
    for record in records:
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue
        if title_key and (
            _normalize_title(_format_bitable_cell(fields.get(title_field))) == title_key
        ):
            matches.append(record)
    if len(matches) > 1:
        raise ValueError("表格中存在多条同名论文记录，无法确定 PPT 应更新到哪一条。")
    if matches:
        return matches[0]

    speaker_key = _normalize_title(speaker)
    if speaker_field and speaker_key:
        speaker_matches = []
        for record in records:
            fields = record.get("fields")
            if not isinstance(fields, dict):
                continue
            if (
                _normalize_title(_format_bitable_cell(fields.get(speaker_field)))
                == speaker_key
            ):
                speaker_matches.append(record)
        if len(speaker_matches) == 1:
            return speaker_matches[0]
        if len(speaker_matches) > 1:
            raise ValueError(
                "没有找到该论文标题，且该汇报人对应多条记录，"
                "无法确定 PPT 应更新到哪一条。"
            )
    raise ValueError("没有找到该论文标题或汇报人对应的表格记录，已停止更新。")


async def _upload_bitable_attachment(
    table: BitableTable,
    path: Path,
    filename: str,
) -> str:
    if table.client.drive is None:
        raise ValueError("飞书云盘 API 未初始化，无法上传 PPT PDF。")
    with path.open("rb") as file_obj:
        body = (
            UploadAllMediaRequestBody.builder()
            .file_name(filename)
            .parent_type("bitable_file")
            .parent_node(table.app_token)
            .size(path.stat().st_size)
            .file(file_obj)
            .build()
        )
        request = UploadAllMediaRequest.builder().request_body(body).build()
        response = await table.client.drive.v1.media.aupload_all(request)
    if not response.success() or response.data is None or not response.data.file_token:
        raise ValueError(
            f"PPT PDF 上传失败：{response.msg or response.code or '未知错误'}"
        )
    return response.data.file_token


async def _attach_ppt(
    context: star.Context,
    plugin_cfg: AstrBotConfig | dict | None,
    title: str,
    path: Path,
    filename: str,
    speaker: str = "",
) -> str:
    table = await _load_bitable_table(context, plugin_cfg)
    record = _find_record_by_title(
        table.records,
        table.schema.title,
        title,
        speaker_field=table.schema.speaker,
        speaker=speaker,
    )
    token = await _upload_bitable_attachment(table, path, filename)
    plan = UpsertPlan(
        record_id=_clean_text(record.get("record_id")),
        fields={table.schema.ppt: [{"file_token": token}]},
    )
    await _write_record(table, plan)
    return f"已把 PPT PDF「{filename}」更新到论文：{title}"


def _missing_field(state: UserState) -> str:
    for field_name in ("title", "venue", "speaker", "paper_link"):
        if not _clean_text(getattr(state, field_name)):
            return field_name
    return ""


def _missing_prompt(field_name: str) -> str:
    prompts = {
        "title": "没有识别到论文标题，请直接回复完整的论文标题。",
        "venue": "没有识别到会议/期刊信息，请回复会议或期刊名称。",
        "speaker": "没有识别到你的汇报人姓名，请回复表格中应填写的姓名。",
        "paper_link": "没有识别到论文网页链接，请回复论文网页、arXiv 或 DOI 链接。",
    }
    return prompts[field_name]


def _explicit_pdf_kind(text: str, filename: str) -> str:
    combined = f"{text} {filename}".casefold()
    if re.search(r"(?:^|[\s_\-])ppt(?:[\s_\-.]|$)|slides?|幻灯|汇报ppt", combined):
        return "slides"
    if re.search(r"论文pdf|paper[\s_\-]*pdf|原文", combined):
        return "paper"
    return ""


async def _resolve_speaker(
    context: star.Context,
    plugin_cfg: AstrBotConfig | dict | None,
    event: AstrMessageEvent,
) -> str:
    group_session = _clean_text(_cfg_get(plugin_cfg, "group_session", ""))
    platform_id, chat_id = _group_chat_id_from_session(group_session)
    if platform_id and chat_id:
        client = _get_lark_client_for_platform(context, platform_id)
        if client is not None:
            listed = await list_chat_members(client, chat_id)
            if not listed.error:
                sender_id = event.get_sender_id()
                for member in listed.members:
                    if member.open_id == sender_id and not _looks_like_identifier(
                        member.name
                    ):
                        return member.name
    sender_name = event.get_sender_name()
    return "" if _looks_like_identifier(sender_name) else sender_name


def _apply_metadata(state: UserState, metadata: PaperMetadata, speaker: str) -> None:
    state.title = metadata.title
    state.venue = metadata.venue
    state.paper_link = metadata.paper_link
    state.speaker = speaker
    state.pending_field = ""


def _start_new_submission(state_path: Path, state: UserState) -> UserState:
    last_title = state.last_title
    _remove_pending_pdf(state)
    fresh_state = UserState(last_title=last_title)
    _save_state(state_path, fresh_state)
    return fresh_state


def _is_expected_pdf_link_reply(state: UserState) -> bool:
    return state.pending_field == "paper_link" and state.source_kind == "pdf"


class Main(star.Star):
    def __init__(
        self,
        context: star.Context,
        config: AstrBotConfig | dict | None = None,
    ) -> None:
        self.context = context
        self.plugin_config = config if config is not None else {}
        self._write_lock = asyncio.Lock()

    async def _commit_paper(
        self,
        state_path: Path,
        state: UserState,
        *,
        scheduled_time_override: int | None = None,
    ) -> str:
        metadata = PaperMetadata(
            title=state.title,
            venue=state.venue,
            paper_link=state.paper_link,
            document_kind="paper",
        )
        async with self._write_lock:
            result, _scheduled_ms = await _upsert_paper(
                self.context,
                self.plugin_config,
                metadata,
                state.speaker,
                scheduled_time_override=scheduled_time_override,
            )
        state.last_title = state.title
        state.title = ""
        state.venue = ""
        state.speaker = ""
        state.paper_link = ""
        state.pending_field = ""
        state.source_kind = ""
        state.proposed_meeting_time = ""
        _save_state(state_path, state)
        return result

    async def _finish_paper(self, state_path: Path, state: UserState) -> str:
        missing = _missing_field(state)
        if missing:
            state.pending_field = missing
            _save_state(state_path, state)
            return _missing_prompt(missing)

        table = await _load_bitable_table(self.context, self.plugin_config)
        if not _title_exists(table.records, table.schema, state.title):
            upcoming_time = _upcoming_speaker_time(
                table.records,
                table.schema,
                state.speaker,
            )
            if upcoming_time is not None:
                timezone_name = _clean_text(
                    _cfg_get(self.plugin_config, "cron_timezone", "Asia/Shanghai")
                )
                state.pending_field = "same_meeting_confirmation"
                state.proposed_meeting_time = str(upcoming_time)
                _save_state(state_path, state)
                scheduled = _format_scheduled_time(upcoming_time, timezone_name)
                return (
                    f"检测到你已有一篇论文安排在下次组会（{scheduled}）。"
                    "这篇论文也安排在同一次组会吗？请回复“是”或“否”。"
                )

        return await self._commit_paper(state_path, state)

    async def _handle_pending_reply(
        self,
        state_path: Path,
        state: UserState,
        text: str,
    ) -> str:
        if state.pending_field == "same_meeting_confirmation":
            table = await _load_bitable_table(self.context, self.plugin_config)
            next_meeting = _next_meeting_time(table.records, table.schema)
            if next_meeting is None:
                return "表格中已没有尚未开始的组会场次，请重新发送论文链接以重新排期。"
            if state.proposed_meeting_time != str(next_meeting):
                state.proposed_meeting_time = str(next_meeting)
                _save_state(state_path, state)
                timezone_name = _clean_text(
                    _cfg_get(self.plugin_config, "cron_timezone", "Asia/Shanghai")
                )
                scheduled = _format_scheduled_time(next_meeting, timezone_name)
                return (
                    f"已按全表最近的未来场次校正：下一次组会为 {scheduled}。"
                    "这篇论文安排在这一场吗？请重新回复“是”或“否”。"
                )
            answer = re.sub(r"[\s，。,.！!？?]", "", text).casefold()
            if answer in {"是", "是的", "对", "可以", "yes", "y"} or any(
                marker in answer for marker in ("都是", "同一次", "一起", "下次汇报")
            ):
                try:
                    scheduled_time = int(state.proposed_meeting_time)
                except ValueError:
                    return "待确认的组会时间已失效，请重新发送论文链接。"
                state.pending_field = ""
                return await self._commit_paper(
                    state_path,
                    state,
                    scheduled_time_override=scheduled_time,
                )
            if answer in {"否", "不是", "不", "no", "n"} or answer.startswith("不"):
                try:
                    scheduled_time = int(state.proposed_meeting_time)
                except ValueError:
                    return "待确认的组会时间已失效，请重新发送论文链接。"
                timezone_name = _clean_text(
                    _cfg_get(self.plugin_config, "cron_timezone", "Asia/Shanghai")
                )
                inherited = _format_scheduled_time(scheduled_time, timezone_name)
                inherited_time = inherited.rsplit(" ", 1)[-1]
                state.pending_field = "custom_meeting_date"
                _save_state(state_path, state)
                return (
                    "请指定这篇论文的汇报日期，例如“2026-09-30”或“9月30日”。"
                    f"小时和分钟将沿用 {inherited_time}。"
                )
            return "请回复“是”或“否”：这篇论文是否也安排在下次组会？"

        if state.pending_field == "custom_meeting_date":
            try:
                reference_time = int(state.proposed_meeting_time)
            except ValueError:
                return "待指定的组会时间已失效，请重新发送论文链接。"
            timezone_name = _clean_text(
                _cfg_get(self.plugin_config, "cron_timezone", "Asia/Shanghai")
            )
            scheduled_time = _parse_custom_meeting_date(
                text,
                reference_time,
                timezone_name,
            )
            if scheduled_time is None:
                return "日期格式无法识别，请回复例如“2026-09-30”或“9月30日”。"
            state.pending_field = ""
            return await self._commit_paper(
                state_path,
                state,
                scheduled_time_override=scheduled_time,
            )

        if state.pending_field == "pdf_kind":
            lowered = text.casefold()
            if "ppt" in lowered or "幻灯" in lowered:
                state.pending_field = "ppt_title"
                if state.last_title:
                    state.title = state.last_title
                    return await self._finish_pending_ppt(state_path, state)
                _save_state(state_path, state)
                return "请回复这份 PPT 对应的完整论文标题。"
            if "论文" in lowered or "paper" in lowered:
                path = Path(state.pending_pdf_path)
                max_bytes = (
                    int(_cfg_get(self.plugin_config, "max_pdf_mb", 20)) * 1024 * 1024
                )
                source = await _text_from_pdf(path, max_bytes)
                metadata = await _extract_paper_metadata(
                    self.context,
                    source,
                    prefer_doi=True,
                )
                speaker = state.speaker
                _remove_pending_pdf(state)
                _apply_metadata(state, metadata, speaker)
                return await self._finish_paper(state_path, state)
            return "请回复“论文”或“PPT”，以便我正确处理这份 PDF。"

        if state.pending_field == "ppt_title":
            state.title = text.strip()
            return await self._finish_pending_ppt(state_path, state)

        if state.pending_field in {"title", "venue", "speaker", "paper_link"}:
            value = text.strip()
            if state.pending_field == "paper_link":
                urls = _URL_RE.findall(value)
                if not urls:
                    return "请发送以 http:// 或 https:// 开头的论文链接。"
                value = urls[0]
            setattr(state, state.pending_field, value)
            state.pending_field = ""
            return await self._finish_paper(state_path, state)

        return ""

    async def _finish_pending_ppt(
        self,
        state_path: Path,
        state: UserState,
    ) -> str:
        if not state.pending_pdf_path or not Path(state.pending_pdf_path).is_file():
            _remove_pending_pdf(state)
            state.pending_field = ""
            _save_state(state_path, state)
            return "待处理的 PDF 已失效，请重新发送 PPT PDF。"
        if not state.title:
            state.pending_field = "ppt_title"
            _save_state(state_path, state)
            return "请回复这份 PPT 对应的完整论文标题。"
        path = Path(state.pending_pdf_path)
        filename = state.pending_pdf_name or path.name
        async with self._write_lock:
            result = await _attach_ppt(
                self.context,
                self.plugin_config,
                state.title,
                path,
                filename,
                speaker=state.speaker,
            )
        state.last_title = state.title
        state.title = ""
        state.pending_field = ""
        _remove_pending_pdf(state)
        _save_state(state_path, state)
        return result

    async def _handle_pdf(
        self,
        event: AstrMessageEvent,
        state_path: Path,
        state: UserState,
        file_comp: Comp.File,
        text: str,
    ) -> str:
        state.source_kind = "pdf"
        filename = _clean_text(file_comp.name) or "document.pdf"
        local_file = await file_comp.get_file()
        path = Path(local_file)
        if not local_file or not path.is_file():
            return "无法读取这份 PDF，请重新发送文件。"
        max_bytes = int(_cfg_get(self.plugin_config, "max_pdf_mb", 20)) * 1024 * 1024
        if path.stat().st_size > max_bytes:
            return f"PDF 超过 {max_bytes // 1024 // 1024} MB，无法处理。"

        explicit_kind = _explicit_pdf_kind(text, filename)
        source = await _text_from_pdf(path, max_bytes)
        metadata = await _extract_paper_metadata(
            self.context,
            source,
            prefer_doi=True,
            umo=str(event.unified_msg_origin),
        )
        speaker = await _resolve_speaker(self.context, self.plugin_config, event)
        state.speaker = speaker
        kind = explicit_kind or metadata.document_kind
        if kind == "slides":
            title = state.title or state.last_title or metadata.title
            if title:
                async with self._write_lock:
                    result = await _attach_ppt(
                        self.context,
                        self.plugin_config,
                        title,
                        path,
                        filename,
                        speaker=speaker,
                    )
                state.last_title = title
                _save_state(state_path, state)
                return result
            _copy_pending_pdf(path, filename, state)
            state.pending_field = "ppt_title"
            _save_state(state_path, state)
            return "已识别为 PPT PDF。请回复它对应的完整论文标题。"

        if kind == "paper":
            _apply_metadata(state, metadata, speaker)
            return await self._finish_paper(state_path, state)

        _copy_pending_pdf(path, filename, state)
        state.pending_field = "pdf_kind"
        _save_state(state_path, state)
        return "暂时无法确定这份 PDF 的类型。请回复“论文”或“PPT”。"

    async def _handle_url(
        self,
        event: AstrMessageEvent,
        state_path: Path,
        state: UserState,
        url: str,
    ) -> str:
        state.source_kind = "url"
        max_bytes = int(_cfg_get(self.plugin_config, "max_pdf_mb", 20)) * 1024 * 1024
        try:
            source, final_url = await _text_from_url(url, max_bytes)
        except httpx.HTTPError:
            logger.warning(
                "[seminar_excel_updater] paper page fetch failed, "
                "continue with the submitted URL",
                exc_info=True,
            )
            source, final_url = url, url
        metadata = await _extract_paper_metadata(
            self.context,
            source,
            source_url=final_url,
            umo=str(event.unified_msg_origin),
        )
        if metadata.document_kind == "slides":
            return "这个链接看起来是汇报材料。请直接发送导出的 PPT PDF 文件。"
        speaker = await _resolve_speaker(self.context, self.plugin_config, event)
        _apply_metadata(state, metadata, speaker)
        return await self._finish_paper(state_path, state)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=90)
    @filter.platform_adapter_type(filter.PlatformAdapterType.LARK)
    async def on_private_seminar_update(self, event: AstrMessageEvent):
        text = _clean_text(event.get_message_str())
        state_path = _state_path(event.get_platform_id(), event.get_sender_id())
        state = _load_state(state_path)

        if text in {"/seminar_update_cancel", "取消更新", "取消填表"}:
            last_title = state.last_title
            _remove_pending_pdf(state)
            state = UserState(last_title=last_title)
            _save_state(state_path, state)
            event.should_call_llm(False)
            yield event.plain_result("已取消当前自动填表流程。 ").stop_event()
            return

        if text in {"/seminar_update_status", "填表状态"}:
            event.should_call_llm(False)
            missing = _missing_field(state)
            status = (
                f"当前论文：{state.title or '未开始'}\n"
                f"等待信息：{missing or state.pending_field or '无'}\n"
                f"最近论文：{state.last_title or '无'}"
            )
            yield event.plain_result(status).stop_event()
            return

        handled = False
        response = ""
        try:
            pdf_files = [
                comp
                for comp in event.get_messages()
                if isinstance(comp, Comp.File)
                and _clean_text(comp.name).lower().endswith(".pdf")
            ]
            urls = _URL_RE.findall(text)
            if pdf_files:
                state = _start_new_submission(state_path, state)
                handled = True
                response = await self._handle_pdf(
                    event,
                    state_path,
                    state,
                    pdf_files[0],
                    text,
                )
            elif urls and not _is_expected_pdf_link_reply(state):
                state = _start_new_submission(state_path, state)
                handled = True
                response = await self._handle_url(
                    event,
                    state_path,
                    state,
                    urls[0],
                )
            elif state.pending_field and text:
                response = await self._handle_pending_reply(state_path, state, text)
                handled = bool(response)
            elif urls:
                handled = True
                response = await self._handle_url(
                    event,
                    state_path,
                    state,
                    urls[0],
                )
        except (ValueError, httpx.HTTPError, OSError) as exc:
            handled = True
            response = f"自动更新未完成：{exc}"
        except Exception:
            handled = True
            logger.exception("[seminar_excel_updater] private update failed")
            response = "自动更新遇到未预期错误，请稍后重试或联系管理员查看日志。"

        if not handled:
            return
        event.should_call_llm(False)
        yield event.plain_result(response).stop_event()
