"""Read seminar Excel: Feishu/Lark cloud spreadsheet (URL) or group file attachment."""

import asyncio
import io
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import httpx
import lark_oapi as lark
from lark_oapi.api.sheets.v3.model.get_spreadsheet_request import GetSpreadsheetRequest
from lark_oapi.api.sheets.v3.model.query_spreadsheet_sheet_request import (
    QuerySpreadsheetSheetRequest,
)
from lark_oapi.core.model.config import Config
from lark_oapi.core.token.manager import TokenManager
from markitdown_no_magika import MarkItDown, StreamInfo

import astrbot.api.message_components as Comp
from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core import logger
from astrbot.core.config import AstrBotConfig
from astrbot.core.star.filter.custom_filter import CustomFilter

# Match the user-visible file / document name (Feishu may add suffix like .xlsx).
_SEMINAR_NAME_MARKERS = ("seminar_excel", "seminar-excel")
_MAX_REPLY_CHARS = 3500
_MAX_PPT_SUMMARY_SOURCE_CHARS = 9000

# User asks about / read the cloud sheet (colloquial Chinese included).
_READ_INTENT_RE = re.compile(
    r"读取|读一?下|读|输出|查看|看看|看到|看见|内容|打开|获取|导出|文件|表格|云文档|"
    r"能不能|可不可以|能否|是否可以|访问|用一下|看到了吗|可以吗",
    re.I,
)

_PLUGIN_MODULE = "astrbot.builtin_stars.seminar_excel_reader.main"
_CRON_JOB_NAME = "seminar_excel_daily_reminder"
_DEFAULT_REMINDER_LABELS = ("汇报人", "时间", "地点", "论文", "会议/期刊", "论文链接")

# Output label -> spreadsheet header aliases (normalized lowercase).
_LABEL_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "汇报人": ("speaker", "汇报人", "姓名", "名字", "人员", "用户名", "name"),
    "时间": ("date", "时间", "日期", "datetime", "日程"),
    "地点": ("location", "地点", "会议地点", "地址", "place"),
    "论文": ("paper", "论文", "文章", "文章名", "标题", "title", "主题"),
    "会议/期刊": (
        "venue",
        "会议/期刊",
        "会议",
        "期刊",
        "会议期刊",
        "会议或期刊",
        "conference",
        "journal",
    ),
    "论文链接": (
        "paper link",
        "paper_link",
        "论文链接",
        "website",
        "网站",
        "链接",
        "网址",
        "url",
        "link",
    ),
    "腾讯会议链接": (
        "tencent meeting",
        "tencent meeting link",
        "meeting link",
        "meeting_link",
        "腾讯会议链接",
        "腾讯会议",
        "会议链接",
        "会议网址",
        "线上会议",
    ),
    "PPT": (
        "ppt",
        "slides",
        "slide",
        "presentation",
        "pdf",
        "PPT",
        "课件",
        "幻灯片",
        "报告PPT",
        "汇报PPT",
    ),
}

_CN_ROW_DIGITS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

_ROW_INDEX_RE = re.compile(
    r"第\s*([0-9一二三四五六七八九十两]+)\s*行",
    re.I,
)
_OUTPUT_LABEL_RE = re.compile(r"([^\s：:\n]{1,20})[：:]")
_NEAREST_FUTURE_DATE_RE = re.compile(
    r"晚于\s*今|大于\s*今|今天\s*之后|今日\s*之后|之后\s*的|未来|"
    r"距离今天最近|离今天最近|最近.*晚于|最近.*之后|即将到来|"
    r"最近的一?[条行]|最近的一?个",
    re.I,
)
_DATE_CELL_RE = re.compile(
    r"^(\d{4})[.\-/年](\d{1,2})[.\-/月](\d{1,2})(?:日)?(?:\s|$)",
)
_TIME_IN_CELL_RE = re.compile(r"(?:\s|^)(\d{1,2}):(\d{2})(?::\d{2})?(?:\s|$)")
_REPORT_INFO_RE = re.compile(r"汇报|报告|seminar|论文|信息|提醒", re.I)
_DAY_AFTER_TOMORROW_RE = re.compile(r"大后天")
_AFTER_TOMORROW_RE = re.compile(r"后天")
_TOMORROW_RE = re.compile(r"明天|明日")
_TODAY_RE = re.compile(r"今天|今日")

# Feishu / Lark spreadsheet and bitable links in IM (cloud docs are shared as URLs).
_SHEET_URL_RES = (
    re.compile(r"(?:https?://)?[\w.-]*feishu\.cn/sheets/([A-Za-z0-9]+)", re.I),
    re.compile(r"(?:https?://)?[\w.-]*larkoffice\.com/sheets/([A-Za-z0-9]+)", re.I),
    re.compile(r"(?:https?://)?[\w.-]*larksuite\.com/sheets/([A-Za-z0-9]+)", re.I),
)
_BITABLE_URL_RES = (
    re.compile(r"(?:https?://)?[\w.-]*feishu\.cn/base/([A-Za-z0-9]+)", re.I),
    re.compile(r"(?:https?://)?[\w.-]*larkoffice\.com/base/([A-Za-z0-9]+)", re.I),
    re.compile(r"(?:https?://)?[\w.-]*larksuite\.com/base/([A-Za-z0-9]+)", re.I),
)
_BITABLE_FULL_URL_RES = re.compile(
    r"((?:https?://)?[\w.-]*(?:feishu\.cn|larkoffice\.com|larksuite\.com)/base/[^\s]+)",
    re.I,
)
_HTTP_URL_RE = re.compile(r"https?://[^\s，。；;）)]+", re.I)
_HYPERLINK_FORMULA_RE = re.compile(
    r"""^=\s*HYPERLINK\s*\(\s*["']([^"']+)["']""",
    re.I,
)


def _markers_in(s: str) -> bool:
    t = s.lower().replace(" ", "")
    return any(m in t for m in _SEMINAR_NAME_MARKERS)


def _collect_all_text(event: AstrMessageEvent) -> str:
    parts: list[str] = [event.message_str or ""]
    for comp in event.get_messages():
        if isinstance(comp, Comp.Plain):
            parts.append(comp.text or "")
    return " ".join(parts)


def _extract_spreadsheet_tokens(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for rx in _SHEET_URL_RES:
        for m in rx.finditer(text):
            tok = m.group(1)
            if tok and tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out


def _extract_bitable_tokens(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for rx in _BITABLE_URL_RES:
        for m in rx.finditer(text):
            tok = m.group(1)
            if tok and tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out


def _extract_bitable_table_id(text: str) -> str | None:
    for m in _BITABLE_FULL_URL_RES.finditer(text):
        raw_url = m.group(1)
        if not raw_url.startswith(("http://", "https://")):
            raw_url = "https://" + raw_url
        parsed = urlparse(raw_url)
        table_ids = parse_qs(parsed.query).get("table")
        if table_ids and table_ids[0]:
            return table_ids[0]
    return None


def _extract_cloud_tokens(text: str) -> list[tuple[str, str, str | None]]:
    seen: set[tuple[str, str, str | None]] = set()
    out: list[tuple[str, str, str | None]] = []
    bitable_table_id = _extract_bitable_table_id(text)
    for kind, tokens, table_id in (
        ("sheet", _extract_spreadsheet_tokens(text), None),
        ("bitable", _extract_bitable_tokens(text), bitable_table_id),
    ):
        for token in tokens:
            item = (kind, token, table_id)
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def _has_sheet_url(text: str) -> bool:
    return bool(_extract_cloud_tokens(text))


def _is_seminar_file_msg(event: AstrMessageEvent) -> bool:
    for comp in event.get_messages():
        if not isinstance(comp, Comp.File):
            continue
        if not _markers_in(comp.name or ""):
            continue
        path_str = comp.file or ""
        if path_str and Path(path_str).is_file():
            return True
    return False


def _configured_spreadsheet_token(
    plugin_cfg: AstrBotConfig | dict | None,
) -> str | None:
    if not plugin_cfg:
        return None
    url = ""
    if isinstance(plugin_cfg, dict):
        url = str(plugin_cfg.get("spreadsheet_url") or "").strip()
    else:
        url = str(plugin_cfg.get("spreadsheet_url", "") or "").strip()
    if not url:
        return None
    tokens = _extract_spreadsheet_tokens(url)
    return tokens[0] if tokens else None


def _configured_cloud_token(
    plugin_cfg: AstrBotConfig | dict | None,
) -> tuple[str, str, str | None] | None:
    if not plugin_cfg:
        return None
    url = ""
    if isinstance(plugin_cfg, dict):
        url = str(plugin_cfg.get("spreadsheet_url") or "").strip()
    else:
        url = str(plugin_cfg.get("spreadsheet_url", "") or "").strip()
    if not url:
        return None
    tokens = _extract_cloud_tokens(url)
    return tokens[0] if tokens else None


def _load_configured_token_from_runtime() -> tuple[str, str, str | None] | None:
    """Read spreadsheet token from plugin instance, star metadata, or config file."""
    try:
        from astrbot.core.star.star import star_map

        md = star_map.get(_PLUGIN_MODULE)
        if md is not None:
            if md.config is not None:
                tok = _configured_cloud_token(md.config)
                if tok:
                    return tok
            star_inst = md.star_cls
            if star_inst is not None:
                tok = _configured_cloud_token(
                    getattr(star_inst, "plugin_config", None),
                )
                if tok:
                    return tok
    except Exception:
        logger.debug(
            "[seminar_excel_reader] star_map config lookup failed", exc_info=True
        )

    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_config_path

        cfg_path = Path(get_astrbot_config_path()) / "seminar_excel_reader_config.json"
        if cfg_path.is_file():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return _configured_cloud_token(data)
    except Exception:
        logger.debug("[seminar_excel_reader] config file lookup failed", exc_info=True)
    return None


def _wants_read_seminar(text: str, event: AstrMessageEvent) -> bool:
    if not _markers_in(text):
        return False
    if _has_sheet_url(text) or _is_seminar_file_msg(event):
        return True
    if _load_configured_token_from_runtime() is not None:
        return True
    return _READ_INTENT_RE.search(text) is not None


class SeminarExcelTriggerFilter(CustomFilter):
    """seminar_excel file/URL, or @bot asking to read the configured cloud sheet."""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        text = _collect_all_text(event)
        return _wants_read_seminar(text, event)


def _read_excel_markdown(path: Path, original_name: str) -> str:
    ext = path.suffix.lower() or Path(original_name).suffix.lower()
    if ext not in (".xlsx", ".xls", ".csv", ""):
        ext = ".xlsx"
    md = MarkItDown(enable_plugins=False)
    data = path.read_bytes()
    bio = io.BytesIO(data)
    stream_info = StreamInfo(
        extension=ext or ".xlsx", filename=original_name or path.name
    )
    result = md.convert(bio, stream_info=stream_info)
    return (result.markdown or "").strip()


def _read_markdown_from_bytes(data: bytes, filename: str) -> str:
    ext = Path(filename).suffix.lower()
    md = MarkItDown(enable_plugins=False)
    bio = io.BytesIO(data)
    stream_info = StreamInfo(extension=ext or ".pdf", filename=filename)
    result = md.convert(bio, stream_info=stream_info)
    return (result.markdown or "").strip()


def _lark_api_base(client: lark.Client) -> str:
    cfg = client._config
    if cfg is None:
        return "https://open.feishu.cn"
    return (cfg.domain or "https://open.feishu.cn").rstrip("/")


def _cell_str(cell: object) -> str:
    return "" if cell is None else str(cell).strip()


def _normalize_key(text: str) -> str:
    return text.strip().lower().replace(" ", "")


def _trim_table(values: list[list]) -> list[list]:
    rows: list[list] = []
    for row in values:
        cells = [_cell_str(c) for c in row]
        if any(cells):
            rows.append(cells)
    return rows


def _format_grid(values: list[list]) -> str:
    lines: list[str] = []
    for row in values:
        lines.append("\t".join(row))
    return "\n".join(lines).strip()


def _format_bitable_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str | int | float | bool):
        return str(value).strip()
    if isinstance(value, list):
        parts = [_format_bitable_cell(item) for item in value]
        return ", ".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in (
            "link",
            "url",
            "text",
            "name",
            "title",
            "display_name",
            "email",
            "value",
        ):
            item = value.get(key)
            if item not in (None, ""):
                return _format_bitable_cell(item)
        if "timestamp" in value:
            return _format_bitable_cell(value.get("timestamp"))
        parts = [_format_bitable_cell(item) for item in value.values()]
        return ", ".join(part for part in parts if part)
    return str(value).strip()


def _parse_chinese_number(token: str) -> int | None:
    token = token.strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    if len(token) == 1 and token in _CN_ROW_DIGITS:
        return _CN_ROW_DIGITS[token]
    if token == "两":
        return 2
    if token.startswith("十") and len(token) == 2 and token[1] in _CN_ROW_DIGITS:
        return 10 + _CN_ROW_DIGITS[token[1]]
    if token.endswith("十") and len(token) == 2 and token[0] in _CN_ROW_DIGITS:
        return _CN_ROW_DIGITS[token[0]] * 10
    return None


def _parse_target_row_index(user_text: str, row_count: int) -> int | None:
    """Parse 第N行 / 第二行. Returns 0-based index into trimmed table rows."""
    m = _ROW_INDEX_RE.search(user_text)
    if not m:
        return None
    n = _parse_chinese_number(m.group(1))
    if n is None or n < 1:
        return None
    idx = n - 1
    if idx >= row_count:
        return None
    return idx


def _extract_output_labels(user_text: str) -> list[str]:
    labels: list[str] = []
    skip = {"seminar_excel", "seminar-excel", "云文档", "文件", "表格", "格式"}
    for m in _OUTPUT_LABEL_RE.finditer(user_text):
        label = m.group(1).strip()
        if not label or label in skip:
            continue
        if label not in labels:
            labels.append(label)
    return labels


def _find_column_for_label(headers: list[str], label: str) -> int | None:
    norm_headers = [_normalize_key(h) for h in headers]
    label_key = _normalize_key(label)
    aliases = _LABEL_HEADER_ALIASES.get(label, (label_key,))
    alias_norms = {_normalize_key(a) for a in aliases}
    alias_norms.add(label_key)
    for idx, header in enumerate(norm_headers):
        if header in alias_norms:
            return idx
    for idx, header in enumerate(norm_headers):
        for alias in alias_norms:
            if alias and (alias in header or header in alias):
                return idx
    return None


def _wants_nearest_future_date_row(user_text: str) -> bool:
    return _NEAREST_FUTURE_DATE_RE.search(user_text) is not None


def _wants_report_info(user_text: str) -> bool:
    return _REPORT_INFO_RE.search(user_text) is not None


def _resolve_relative_target_date(
    user_text: str,
    *,
    reference_day: date | None = None,
) -> date | None:
    """Map 今天/明天/后天 in user text to a calendar date."""
    today = reference_day or date.today()
    if _DAY_AFTER_TOMORROW_RE.search(user_text):
        return today + timedelta(days=3)
    if _AFTER_TOMORROW_RE.search(user_text):
        return today + timedelta(days=2)
    if _TOMORROW_RE.search(user_text):
        return today + timedelta(days=1)
    if _TODAY_RE.search(user_text):
        return today
    return None


def _format_date_label(d: date) -> str:
    return f"{d.year}.{d.month}.{d.day}"


def _format_display_date(d: date) -> str:
    return f"{d.year:04d}/{d.month:02d}/{d.day:02d}"


def _format_display_datetime(value: str) -> str | None:
    parsed_date = _parse_date_cell(value)
    if parsed_date is None:
        return None

    text = value.strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        try:
            serial = float(text)
            dt: datetime | None = None
            if 1_000_000_000_000 <= serial < 10_000_000_000_000:
                dt = datetime.fromtimestamp(serial / 1000)
            elif 1_000_000_000 <= serial < 10_000_000_000:
                dt = datetime.fromtimestamp(serial)
            if dt is not None and (dt.hour or dt.minute):
                return f"{_format_display_date(parsed_date)} {dt.hour:02d}:{dt.minute:02d}"
        except ValueError:
            pass

    m = _TIME_IN_CELL_RE.search(text)
    if m:
        return f"{_format_display_date(parsed_date)} {int(m.group(1)):02d}:{m.group(2)}"
    return _format_display_date(parsed_date)


def _pick_row_by_exact_date(
    table: list[list],
    headers: list[str],
    target_day: date,
) -> tuple[list[str] | None, str | None]:
    date_col = _find_column_for_label(headers, "时间")
    if date_col is None:
        return None, "表格中未找到「时间/日期」列，无法按日期筛选。"

    matches: list[list[str]] = []
    for row in table[1:]:
        if date_col >= len(row):
            continue
        row_date = _parse_date_cell(row[date_col])
        if row_date == target_day:
            matches.append(row)

    if not matches:
        return None, f"没有找到日期为 {_format_date_label(target_day)} 的行。"
    if len(matches) > 1:
        logger.info(
            "[seminar_excel_reader] multiple rows on %s, using the first",
            target_day,
        )
    return matches[0], None


def _parse_date_cell(value: str) -> date | None:
    s = value.strip()
    if not s:
        return None
    m = _DATE_CELL_RE.match(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    if re.fullmatch(r"\d+(\.\d+)?", s):
        try:
            serial = float(s)
            if 1_000_000_000_000 <= serial < 10_000_000_000_000:
                return datetime.fromtimestamp(serial / 1000).date()
            if 1_000_000_000 <= serial < 10_000_000_000:
                return datetime.fromtimestamp(serial).date()
            if 30000 < serial < 80000:
                return date(1899, 12, 30) + timedelta(days=int(serial))
        except ValueError:
            return None
    return None


def _pick_nearest_future_row(
    table: list[list],
    headers: list[str],
    *,
    reference_day: date | None = None,
) -> tuple[list[str] | None, str | None]:
    """Row with date strictly after reference_day and closest to it."""
    date_col = _find_column_for_label(headers, "时间")
    if date_col is None:
        return None, "表格中未找到「时间/日期」列，无法按日期筛选。"

    today = reference_day or date.today()
    best_row: list[str] | None = None
    best_on: date | None = None

    for row in table[1:]:
        if date_col >= len(row):
            continue
        row_date = _parse_date_cell(row[date_col])
        if row_date is None or row_date <= today:
            continue
        if best_on is None or row_date < best_on:
            best_on = row_date
            best_row = row

    if best_row is None:
        return None, (
            f"没有找到晚于今天（{today.year}.{today.month}.{today.day}）的日程行。"
        )
    return best_row, None


def _format_row_with_labels(
    row: list[str], headers: list[str], labels: list[str]
) -> str:
    lines: list[str] = []
    for label in labels:
        col = _find_column_for_label(headers, label)
        value = row[col] if col is not None and col < len(row) else ""
        if col is not None and (
            label == "时间" or _find_column_for_label(headers, "时间") == col
        ):
            display_datetime = _format_display_datetime(value)
            if display_datetime is not None:
                value = display_datetime
        lines.append(f"{label}: {value}")
    return "\n".join(lines)


def _row_value_for_label(row: list[str], headers: list[str], label: str) -> str:
    col = _find_column_for_label(headers, label)
    value = row[col] if col is not None and col < len(row) else ""
    if col is not None and (
        label == "时间" or _find_column_for_label(headers, "时间") == col
    ):
        display_datetime = _format_display_datetime(value)
        if display_datetime is not None:
            value = display_datetime
    return value.strip()


async def _load_ppt_text(ppt_value: str) -> str:
    if not ppt_value:
        return ""
    urls = _HTTP_URL_RE.findall(ppt_value)
    if not urls:
        return ppt_value.strip()

    for url in urls:
        try:
            async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as http:
                r = await http.get(url)
            r.raise_for_status()
            content_type = r.headers.get("content-type", "").lower()
            filename = Path(urlparse(str(r.url)).path).name or "seminar_ppt.pdf"
            if not Path(filename).suffix and "pdf" in content_type:
                filename += ".pdf"
            text = await asyncio.to_thread(
                _read_markdown_from_bytes,
                r.content,
                filename,
            )
            if text:
                return text
        except Exception:
            logger.debug(
                "[seminar_excel_reader] failed to load PPT from %s",
                url,
                exc_info=True,
            )
    return ppt_value.strip()


async def _summarize_paper_from_ppt(
    context: star.Context,
    ppt_text: str,
    *,
    paper_title: str = "",
    venue: str = "",
) -> str:
    source = ppt_text.strip()
    if not source:
        return ""
    prov = context.get_using_provider()
    if prov is None:
        return ""

    source = source[:_MAX_PPT_SUMMARY_SOURCE_CHARS]
    prompt = (
        "请根据下面的 Seminar PPT 文本，用中文概括这篇论文的大致内容。"
        "要求：只输出两句话；适合作为群通知里的“论文简介”；"
        "不要使用项目符号，不要添加寒暄，不要编造 PPT 中没有的信息。\n\n"
    )
    if paper_title:
        prompt += f"论文标题：{paper_title}\n"
    if venue:
        prompt += f"会议/期刊：{venue}\n"
    prompt += f"PPT 文本：\n{source}"

    try:
        resp = await prov.text_chat(prompt=prompt)
    except Exception:
        logger.debug(
            "[seminar_excel_reader] PPT summary LLM request failed",
            exc_info=True,
        )
        return ""
    summary = (getattr(resp, "completion_text", None) or "").strip()
    return " ".join(summary.split())


async def _build_natural_reminder_text(
    row: list[str],
    headers: list[str],
    context: star.Context,
) -> str:
    time_value = _row_value_for_label(row, headers, "时间")
    speaker = _row_value_for_label(row, headers, "汇报人")
    location = _row_value_for_label(row, headers, "地点")
    meeting_link = _row_value_for_label(row, headers, "腾讯会议链接")
    paper_title = _row_value_for_label(row, headers, "论文")
    venue = _row_value_for_label(row, headers, "会议/期刊")
    paper_link = _row_value_for_label(row, headers, "论文链接")
    ppt_value = _row_value_for_label(row, headers, "PPT")

    intro = "老师、同学们大家好，我们下一次 Seminar 将在明天"
    if time_value:
        intro += f"（{time_value}）开始"
    else:
        intro += "开始"
    if speaker:
        intro += f"，汇报人为 {speaker}"
    if location:
        intro += f"，地点在 {location}"
    intro += "。"

    lines = [intro]
    if meeting_link:
        lines.append(f"腾讯会议链接：{meeting_link}")

    paper_lines: list[str] = []
    if paper_title:
        paper_lines.append(f"论文标题：{paper_title}")
    if venue:
        paper_lines.append(f"会议/期刊：{venue}")
    ppt_summary = ""
    if ppt_value:
        ppt_text = await _load_ppt_text(ppt_value)
        ppt_summary = await _summarize_paper_from_ppt(
            context,
            ppt_text,
            paper_title=paper_title,
            venue=venue,
        )
    if ppt_summary:
        paper_lines.append(f"论文简介：{ppt_summary}")
    if paper_link:
        paper_lines.append(f"论文链接：{paper_link}")
    if paper_lines:
        lines.append("下面是本次分享的论文基础信息：")
        lines.extend(paper_lines)

    lines.append(
        "具体的 PPT 和录屏可在 Seminar 官网 https://seu-sigmlsys.github.io/ "
        "以及云文档中找到，欢迎各位同学参加。"
    )
    return "\n".join(lines)


def _format_sheet_reply(values: list[list], user_text: str, doc_title: str) -> str:
    table = _trim_table(values)
    if not table:
        return "表格内容为空。"

    target_row = _parse_target_row_index(user_text, len(table))
    output_labels = _extract_output_labels(user_text)
    relative_day = _resolve_relative_target_date(user_text)
    wants_formatted = (
        target_row is not None
        or bool(output_labels)
        or relative_day is not None
        or _wants_report_info(user_text)
        or _wants_nearest_future_date_row(user_text)
    )

    if not wants_formatted:
        return f"已读取云文档「{doc_title}」：\n\n" + _format_grid(table)

    if len(table) < 2:
        return "表格中没有数据行（仅表头）。"

    headers = table[0]
    if not output_labels:
        output_labels = list(_LABEL_HEADER_ALIASES.keys())

    if relative_day is not None and target_row is None:
        row, err = _pick_row_by_exact_date(table, headers, relative_day)
        if err:
            return err
        if row is None:
            return "没有找到符合条件的行。"
        return _format_row_with_labels(row, headers, output_labels)

    if _wants_nearest_future_date_row(user_text) and target_row is None:
        row, err = _pick_nearest_future_row(table, headers)
        if err:
            return err
        if row is None:
            return "没有找到符合条件的行。"
        return _format_row_with_labels(row, headers, output_labels)

    if target_row is not None:
        if target_row >= len(table):
            return f"表格只有 {len(table)} 行，无法读取第 {target_row + 1} 行。"
        row = table[target_row]
    else:
        row, err = _pick_nearest_future_row(table, headers)
        if row is not None:
            pass
        elif err:
            return err
        else:
            row = table[1]

    return _format_row_with_labels(row, headers, output_labels)


async def _spreadsheet_meta(client: lark.Client, spreadsheet_token: str):
    sheets = client.sheets
    if sheets is None:
        raise RuntimeError("Lark client has no sheets service")
    req = GetSpreadsheetRequest.builder().spreadsheet_token(spreadsheet_token).build()
    return await sheets.v3.spreadsheet.aget(req)


async def _list_sheets(client: lark.Client, spreadsheet_token: str):
    sheets = client.sheets
    if sheets is None:
        raise RuntimeError("Lark client has no sheets service")
    req = (
        QuerySpreadsheetSheetRequest.builder()
        .spreadsheet_token(spreadsheet_token)
        .build()
    )
    return await sheets.v3.spreadsheet_sheet.aquery(req)


def _pick_sheet_id(sheets_resp) -> str | None:
    if not sheets_resp.success() or not sheets_resp.data or not sheets_resp.data.sheets:
        return None
    sheets = sheets_resp.data.sheets
    for sh in sheets:
        title = getattr(sh, "title", None) or ""
        if _markers_in(title):
            sid = getattr(sh, "sheet_id", None)
            if sid:
                return sid
    sh0 = sheets[0]
    return getattr(sh0, "sheet_id", None)


async def _v2_read_range(
    *,
    api_base: str,
    tenant_token: str,
    spreadsheet_token: str,
    sheet_id: str,
    a1_range: str = "A1:ZZ200",
    value_render_option: str | None = None,
) -> list[list] | None:
    range_spec = f"{sheet_id}!{a1_range}"
    encoded = quote(range_spec, safe="")
    url = f"{api_base}/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{encoded}"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    params = {}
    if value_render_option:
        params["valueRenderOption"] = value_render_option
    async with httpx.AsyncClient(timeout=60.0) as http:
        r = await http.get(url, headers=headers, params=params)
        r.raise_for_status()
        payload = r.json()
    if payload.get("code") != 0:
        logger.warning(
            "[seminar_excel_reader] sheets v2 values error: %s %s",
            payload.get("code"),
            payload.get("msg"),
        )
        return None
    data = payload.get("data") or {}
    vr = data.get("valueRange") or data.get("value_range") or {}
    values = vr.get("values")
    if isinstance(values, list):
        return values
    return None


def _hyperlink_url_from_formula(value: object) -> str | None:
    text = _cell_str(value)
    if not text:
        return None
    match = _HYPERLINK_FORMULA_RE.match(text)
    if match:
        return match.group(1).strip()
    urls = _HTTP_URL_RE.findall(text)
    return urls[0] if urls else None


def _merge_hyperlink_formula_values(
    values: list[list],
    formula_values: list[list] | None,
) -> list[list]:
    if not formula_values:
        return values
    merged: list[list] = []
    for row_idx, row in enumerate(values):
        formula_row = formula_values[row_idx] if row_idx < len(formula_values) else []
        merged_row = list(row)
        for col_idx, _cell in enumerate(merged_row):
            formula_cell = formula_row[col_idx] if col_idx < len(formula_row) else ""
            url = _hyperlink_url_from_formula(formula_cell)
            if url:
                merged_row[col_idx] = url
        merged.append(merged_row)
    return merged


async def _read_cloud_seminar_values(
    client: lark.Client,
    lark_config: Config,
    spreadsheet_token: str,
    *,
    skip_title_check: bool = False,
) -> tuple[list[list], str] | None:
    meta = await _spreadsheet_meta(client, spreadsheet_token)
    if not meta.success() or not meta.data or not meta.data.spreadsheet:
        logger.warning(
            "[seminar_excel_reader] get spreadsheet failed code=%s msg=%s",
            getattr(meta, "code", None),
            getattr(meta, "msg", None),
        )
        return None
    doc_title = (meta.data.spreadsheet.title or "").strip()
    if not skip_title_check and not _markers_in(doc_title):
        return None

    sheets_resp = await _list_sheets(client, spreadsheet_token)
    sheet_id = _pick_sheet_id(sheets_resp)
    if not sheet_id:
        return None

    token = await asyncio.to_thread(TokenManager.get_self_tenant_token, lark_config)
    values = await _v2_read_range(
        api_base=_lark_api_base(client),
        tenant_token=token,
        spreadsheet_token=spreadsheet_token,
        sheet_id=sheet_id,
    )
    if not values:
        return None
    try:
        formula_values = await _v2_read_range(
            api_base=_lark_api_base(client),
            tenant_token=token,
            spreadsheet_token=spreadsheet_token,
            sheet_id=sheet_id,
            value_render_option="Formula",
        )
    except Exception:
        logger.debug(
            "[seminar_excel_reader] formula value read failed",
            exc_info=True,
        )
        formula_values = None
    values = _merge_hyperlink_formula_values(values, formula_values)
    trimmed = _trim_table(values)
    if not trimmed:
        return None
    return trimmed, doc_title or spreadsheet_token


async def _bitable_get(
    *,
    api_base: str,
    tenant_token: str,
    path: str,
    params: dict[str, str | int | float | bool] | None = None,
) -> dict | None:
    url = f"{api_base}{path}"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    async with httpx.AsyncClient(timeout=60.0) as http:
        r = await http.get(url, headers=headers, params=params)
    try:
        r.raise_for_status()
    except httpx.HTTPStatusError:
        logger.warning(
            "[seminar_excel_reader] bitable http error path=%s params=%s "
            "status=%s body=%s",
            path,
            params,
            r.status_code,
            r.text[:500],
        )
        return None
    try:
        payload = r.json()
    except ValueError:
        logger.warning(
            "[seminar_excel_reader] bitable api returned non-json path=%s "
            "status=%s body=%s",
            path,
            r.status_code,
            r.text[:500],
        )
        return None
    if payload.get("code") != 0:
        logger.warning(
            "[seminar_excel_reader] bitable api error path=%s code=%s msg=%s",
            path,
            payload.get("code"),
            payload.get("msg"),
        )
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


async def _read_bitable_seminar_values(
    client: lark.Client,
    lark_config: Config,
    app_token: str,
    *,
    table_id: str | None = None,
) -> tuple[list[list], str] | None:
    tenant_token = await asyncio.to_thread(TokenManager.get_self_tenant_token, lark_config)
    api_base = _lark_api_base(client)

    tables_data = await _bitable_get(
        api_base=api_base,
        tenant_token=tenant_token,
        path=f"/open-apis/bitable/v1/apps/{app_token}/tables",
        params={"page_size": 100},
    )
    if not tables_data:
        return None
    tables = tables_data.get("items")
    if not isinstance(tables, list) or not tables:
        return None

    chosen_table = None
    if table_id:
        for table in tables:
            if table.get("table_id") == table_id:
                chosen_table = table
                break
    if chosen_table is None:
        for table in tables:
            name = str(table.get("name") or "")
            if _markers_in(name):
                chosen_table = table
                break
    if chosen_table is None:
        chosen_table = tables[0]

    chosen_table_id = str(chosen_table.get("table_id") or "").strip()
    if not chosen_table_id:
        return None
    doc_title = str(chosen_table.get("name") or app_token).strip() or app_token

    fields_data = await _bitable_get(
        api_base=api_base,
        tenant_token=tenant_token,
        path=f"/open-apis/bitable/v1/apps/{app_token}/tables/{chosen_table_id}/fields",
        params={"page_size": 100},
    )
    if not fields_data:
        return None
    field_items = fields_data.get("items")
    if not isinstance(field_items, list) or not field_items:
        return None

    headers: list[str] = []
    for field in field_items:
        name = str(field.get("field_name") or field.get("name") or "").strip()
        if name:
            headers.append(name)
    if not headers:
        return None

    rows: list[list[str]] = [headers]
    page_token = ""
    while True:
        params: dict[str, str | int | float | bool] = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        records_data = await _bitable_get(
            api_base=api_base,
            tenant_token=tenant_token,
            path=(
                f"/open-apis/bitable/v1/apps/{app_token}/tables/"
                f"{chosen_table_id}/records"
            ),
            params=params,
        )
        if not records_data:
            break
        records = records_data.get("items")
        if isinstance(records, list):
            for record in records:
                fields = record.get("fields") if isinstance(record, dict) else None
                if not isinstance(fields, dict):
                    continue
                rows.append(
                    [_format_bitable_cell(fields.get(header)) for header in headers]
                )
        if not records_data.get("has_more"):
            break
        page_token = str(records_data.get("page_token") or "")
        if not page_token:
            break

    trimmed = _trim_table(rows)
    if len(trimmed) < 1:
        return None
    return trimmed, doc_title


def _truncate(text: str) -> str:
    if len(text) <= _MAX_REPLY_CHARS:
        return text
    return text[:_MAX_REPLY_CHARS] + "\n…(内容过长已截断)"


def _cfg_get(plugin_cfg: AstrBotConfig | dict | None, key: str, default=None):
    if not plugin_cfg:
        return default
    if isinstance(plugin_cfg, dict):
        return plugin_cfg.get(key, default)
    return plugin_cfg.get(key, default)


def _reminder_labels(plugin_cfg: AstrBotConfig | dict | None) -> list[str]:
    raw = _cfg_get(plugin_cfg, "reminder_labels", None)
    if isinstance(raw, list) and raw:
        labels = [str(x).strip() for x in raw if str(x).strip()]
        return ["论文链接" if label == "网站" else label for label in labels]
    return list(_DEFAULT_REMINDER_LABELS)


def _get_lark_client_from_context(
    context: star.Context,
) -> tuple[lark.Client, Config] | None:
    for inst in context.platform_manager.platform_insts:
        if inst.meta().name != "lark":
            continue
        api = getattr(inst, "lark_api", None)
        if api is None:
            continue
        lark_cfg = api._config
        if lark_cfg is None:
            continue
        return api, lark_cfg
    return None


async def _fetch_configured_sheet_values(
    context: star.Context,
    plugin_cfg: AstrBotConfig | dict | None,
) -> tuple[list[list], str] | None:
    cloud_token = _configured_cloud_token(plugin_cfg)
    if not cloud_token:
        cloud_token = _load_configured_token_from_runtime()
    if not cloud_token:
        return None

    kind, token, table_id = cloud_token
    lark_pair = _get_lark_client_from_context(context)
    if lark_pair is None:
        return None
    client, lark_cfg = lark_pair
    if kind == "bitable":
        return await _read_bitable_seminar_values(
            client,
            lark_cfg,
            token,
            table_id=table_id,
        )

    if client.sheets is None:
        return None
    return await _read_cloud_seminar_values(
        client,
        lark_cfg,
        token,
        skip_title_check=True,
    )


async def _build_tomorrow_reminder_text(
    context: star.Context,
    values: list[list],
    *,
    reference_day: date | None = None,
) -> str | None:
    table = _trim_table(values)
    if len(table) < 2:
        return None
    headers = table[0]
    tomorrow = _resolve_relative_target_date("明天", reference_day=reference_day)
    if tomorrow is None:
        return None
    row, err = _pick_row_by_exact_date(table, headers, tomorrow)
    if err or row is None:
        return None
    return await _build_natural_reminder_text(row, headers, context)


async def send_tomorrow_seminar_reminder(
    context: star.Context,
    plugin_cfg: AstrBotConfig | dict | None,
    *,
    reference_day: date | None = None,
) -> bool:
    """Read tomorrow's row and send formatted reminder to configured group."""
    session = str(_cfg_get(plugin_cfg, "group_session", "") or "").strip()
    if not session:
        logger.warning("[seminar_excel_reader] group_session is not configured")
        return False

    sheet = await _fetch_configured_sheet_values(context, plugin_cfg)
    if not sheet:
        logger.warning("[seminar_excel_reader] failed to load seminar spreadsheet")
        return False

    values, _doc_title = sheet
    body = await _build_tomorrow_reminder_text(
        context,
        values,
        reference_day=reference_day,
    )
    if not body:
        tomorrow = (reference_day or date.today()) + timedelta(days=1)
        if _cfg_get(plugin_cfg, "cron_notify_when_empty", False):
            body = (
                f"📅 明日（{_format_date_label(tomorrow)}）没有在 seminar_excel "
                "表格中找到对应日程。"
            )
        else:
            logger.info(
                "[seminar_excel_reader] no seminar row for %s, skip send",
                _format_date_label(tomorrow),
            )
            return False

    reminder_text = f"{body}\n"
    sent = await context.send_message(
        session,
        MessageChain().message(reminder_text).at_all(),
    )
    if sent:
        logger.info("[seminar_excel_reader] sent tomorrow reminder to %s", session)
    else:
        logger.warning(
            "[seminar_excel_reader] failed to send tomorrow reminder to %s",
            session,
        )
    return sent


class Main(star.Star):
    def __init__(
        self,
        context: star.Context,
        config: AstrBotConfig | dict | None = None,
    ) -> None:
        self.context = context
        self.plugin_config = config if config is not None else {}
        self._cron_job_id: str | None = None

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        await self._sync_daily_cron_job()

    async def terminate(self) -> None:
        await self._remove_daily_cron_job()

    async def _sync_daily_cron_job(self) -> None:
        cron_mgr = self.context.cron_manager
        if cron_mgr is None:
            return

        await self._remove_daily_cron_job()

        if not _cfg_get(self.plugin_config, "cron_enabled", True):
            return

        session = str(_cfg_get(self.plugin_config, "group_session", "") or "").strip()
        if not session:
            logger.warning(
                "[seminar_excel_reader] cron enabled but group_session empty; "
                "set it in plugin config (use /sid in target group)."
            )
            return

        if not _configured_spreadsheet_token(self.plugin_config):
            cloud_token = _configured_cloud_token(self.plugin_config)
            if not cloud_token:
                logger.warning(
                    "[seminar_excel_reader] cron enabled but spreadsheet_url empty"
                )
                return

        cron_expr = str(
            _cfg_get(self.plugin_config, "cron_expression", "30 19 * * *")
            or "30 19 * * *"
        ).strip()
        tz = str(
            _cfg_get(self.plugin_config, "cron_timezone", "Asia/Shanghai")
            or "Asia/Shanghai"
        ).strip()

        plugin_ref = self

        async def _handler() -> None:
            logger.info(
                "[seminar_excel_reader] daily reminder cron triggered: %s (%s)",
                cron_expr,
                tz,
            )
            await send_tomorrow_seminar_reminder(
                plugin_ref.context, plugin_ref.plugin_config
            )

        try:
            job = await cron_mgr.add_basic_job(
                name=_CRON_JOB_NAME,
                cron_expression=cron_expr,
                handler=_handler,
                description="Send seminar_excel row for tomorrow to group (eve reminder)",
                timezone=tz,
                enabled=True,
                persistent=True,
            )
            self._cron_job_id = job.job_id
            logger.info(
                "[seminar_excel_reader] scheduled daily reminder: %s (%s %s)",
                cron_expr,
                tz,
                session,
            )
        except Exception:
            logger.exception("[seminar_excel_reader] failed to schedule cron job")

    async def _remove_daily_cron_job(self) -> None:
        cron_mgr = self.context.cron_manager
        if cron_mgr is None:
            return
        try:
            for job in await cron_mgr.list_jobs("basic"):
                if job.name == _CRON_JOB_NAME:
                    await cron_mgr.delete_job(job.job_id)
            if self._cron_job_id:
                await cron_mgr.delete_job(self._cron_job_id)
        except Exception:
            logger.debug("[seminar_excel_reader] remove cron job failed", exc_info=True)
        self._cron_job_id = None

    @filter.command("seminar_remind", alias={"seminar提醒", "会议提醒"})
    async def cmd_test_reminder(self, event: AstrMessageEvent):
        """Manually trigger tomorrow's seminar reminder (for testing)."""
        event.should_call_llm(True)
        ok = await send_tomorrow_seminar_reminder(self.context, self.plugin_config)
        if ok:
            yield event.plain_result(
                "已尝试发送明日 seminar 提醒到配置的群会话。"
            ).stop_event()
        else:
            yield event.plain_result(
                "未能发送提醒：请检查 group_session、spreadsheet_url 配置，"
                "或明日表格中是否有对应日期行。也可开启「明天无会议时也在群里发提示」。"
            ).stop_event()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=100)
    @filter.platform_adapter_type(filter.PlatformAdapterType.LARK)
    @filter.custom_filter(SeminarExcelTriggerFilter, False)
    async def on_lark_seminar_excel(self, event: AstrMessageEvent):
        """Parse seminar spreadsheet from cloud URL, plugin config, or local file."""
        event.should_call_llm(True)

        client = getattr(event, "bot", None)
        if client is None or not isinstance(client, lark.Client):
            yield event.plain_result("当前事件缺少飞书 API 客户端，无法读取云文档。")
            event.stop_event()
            return

        lark_cfg = client._config
        if lark_cfg is None:
            yield event.plain_result("飞书客户端未初始化，无法读取云文档。")
            event.stop_event()
            return

        full_text = _collect_all_text(event)
        tokens = _extract_cloud_tokens(full_text)
        configured_token = _configured_cloud_token(self.plugin_config)
        if not configured_token:
            configured_token = _load_configured_token_from_runtime()

        if not tokens and configured_token:
            tokens = [configured_token]

        if tokens:
            chosen: tuple[str, str, str | None] | None = None
            if len(tokens) == 1:
                chosen = tokens[0]
            else:
                for kind, tok, table_id in tokens:
                    if kind == "bitable":
                        chosen = (kind, tok, table_id)
                        break
                    if client.sheets is None:
                        continue
                    meta = await _spreadsheet_meta(client, tok)
                    if (
                        meta.success()
                        and meta.data
                        and meta.data.spreadsheet
                        and _markers_in(meta.data.spreadsheet.title or "")
                    ):
                        chosen = (kind, tok, table_id)
                        break
                if chosen is None:
                    chosen = tokens[0]

            skip_title_check = bool(configured_token and chosen == configured_token)
            try:
                kind, token, table_id = chosen
                if kind == "bitable":
                    sheet_data = await _read_bitable_seminar_values(
                        client,
                        lark_cfg,
                        token,
                        table_id=table_id,
                    )
                else:
                    if client.sheets is None:
                        yield event.plain_result(
                            "飞书客户端未初始化电子表格能力，无法读取普通云表格。"
                        )
                        event.stop_event()
                        return
                    sheet_data = await _read_cloud_seminar_values(
                        client,
                        lark_cfg,
                        token,
                        skip_title_check=skip_title_check,
                    )
            except Exception:
                logger.exception("[seminar_excel_reader] cloud sheet read failed")
                yield event.plain_result(
                    "读取飞书云表格/多维表格失败。请确认：\n"
                    "1. 开放平台已为应用开通电子表格或多维表格读取权限；\n"
                    "2. 云文档已授权给应用（或机器人可见）；\n"
                    "3. WebUI 插件配置中的链接为 /sheets/ 或 /base/ 地址。"
                )
                event.stop_event()
                return

            if not sheet_data:
                yield event.plain_result(
                    "未读取到表格数据。请检查文档权限，或在插件配置中填写正确的 "
                    "seminar_excel 云表格/多维表格链接（须包含 /sheets/ 或 /base/）。"
                )
                event.stop_event()
                return

            values, doc_title = sheet_data
            text = _format_sheet_reply(values, full_text, doc_title)
            yield event.plain_result(_truncate(text))
            event.stop_event()
            return

        file_comp: Comp.File | None = None
        for comp in event.get_messages():
            if isinstance(comp, Comp.File) and _markers_in(comp.name or ""):
                file_comp = comp
                break

        if file_comp is None or not file_comp.file:
            yield event.plain_result(
                "未找到 seminar_excel 云表格/多维表格。\n"
                "请在 AstrBot 管理面板 → 插件 → seminar_excel_reader 中填写 "
                "「seminar_excel 云表格链接」（飞书浏览器地址栏含 /sheets/ 或 /base/ 的完整 URL），"
                "保存后重启 AstrBot，再 @ 机器人读取；或在消息中直接粘贴该链接。"
            )
            event.stop_event()
            return

        path = Path(file_comp.file)
        if not path.is_file():
            yield event.plain_result("文件已失效或路径不存在，请重新上传。")
            event.stop_event()
            return

        try:
            text = _read_excel_markdown(path, file_comp.name or path.name)
        except Exception:
            logger.exception("[seminar_excel_reader] Failed to parse spreadsheet file")
            yield event.plain_result(
                "解析表格文件失败，请确认上传的是有效的 Excel/表格文件。"
            )
            event.stop_event()
            return

        if not text:
            yield event.plain_result("表格内容为空。")
            event.stop_event()
            return

        header = f"已读取本地文件「{file_comp.name or path.name}」内容：\n\n"
        yield event.plain_result(_truncate(header + text))
        event.stop_event()
