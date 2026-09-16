import io
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pypdf import PdfWriter

from astrbot.builtin_stars.seminar_excel_reader.main import (
    _build_natural_reminder_entry,
    _build_tomorrow_reminder_text,
    _build_tomorrow_reminder_texts,
    _read_markdown_from_bytes,
    send_tomorrow_seminar_reminder,
)


def _tomorrow_rows() -> list[list[str]]:
    return [
        [
            "时间",
            "汇报人",
            "地点",
            "腾讯会议",
            "论文",
            "会议/期刊",
            "论文链接",
            "PPT",
        ],
        [
            "2026-09-17 10:30",
            "张三",
            "Room ARTS1021 @ SEU & Online",
            "https://meeting.tencent.com/example",
            "论文 A",
            "会议 A",
            "",
            "",
        ],
        [
            "2026-09-17 10:30",
            "李四",
            "Room ARTS1021 @ SEU & Online",
            "https://meeting.tencent.com/example",
            "论文 B",
            "会议 B",
            "",
            "",
        ],
        [
            "2026-09-18 10:30",
            "王五",
            "Room ARTS1021 @ SEU & Online",
            "https://meeting.tencent.com/example",
            "论文 C",
            "会议 C",
            "",
            "",
        ],
    ]


def test_pdf_source_uses_installed_pypdf_reader() -> None:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(output)

    assert _read_markdown_from_bytes(output.getvalue(), "paper.pdf") == ""


def test_html_source_extracts_paper_metadata_and_visible_text() -> None:
    source = b"""
    <html><head>
      <meta name="citation_title" content="Large Language Models: A Survey">
      <meta name="citation_conference_title" content="arXiv">
      <meta name="description" content="A broad survey of large language models.">
      <script>ignore this content</script>
    </head><body><h1>Large Language Models: A Survey</h1></body></html>
    """

    text = _read_markdown_from_bytes(source, "paper.html")

    assert "Title: Large Language Models: A Survey" in text
    assert "Conference: arXiv" in text
    assert "A broad survey of large language models." in text
    assert "ignore this content" not in text


@pytest.mark.asyncio
async def test_builds_one_tomorrow_reminder_for_each_matching_row() -> None:
    bodies = await _build_tomorrow_reminder_texts(
        SimpleNamespace(),
        _tomorrow_rows(),
        reference_day=date(2026, 9, 16),
    )

    assert len(bodies) == 2
    assert "论文标题：论文 A" in bodies[0]
    assert bodies[0].endswith("汇报人：张三")
    assert "论文标题：论文 B" in bodies[1]
    assert bodies[1].endswith("汇报人：李四")


@pytest.mark.asyncio
async def test_combines_all_tomorrow_rows_into_one_reminder() -> None:
    body = await _build_tomorrow_reminder_text(
        SimpleNamespace(),
        _tomorrow_rows(),
        reference_day=date(2026, 9, 16),
    )

    assert body is not None
    assert "我们下一次 Seminar 将在明天（2026/09/17 10:30）开始" in body
    assert "共有2人汇报，分别是：张三、李四" in body
    assert "地点在 Room ARTS1021 @ SEU & Online" in body
    assert "腾讯会议链接：https://meeting.tencent.com/example" in body
    assert "下面是本次分享的论文基础信息：" in body
    assert "会议/期刊：会议 A\n汇报人：张三\n\n论文标题：论文 B" in body
    assert "具体的 PPT 和录屏可在 Seminar 官网" in body


@pytest.mark.asyncio
async def test_sends_all_tomorrow_rows_in_one_message() -> None:
    context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    config = {"group_session": "lark:GroupMessage:oc_test"}

    with patch(
        "astrbot.builtin_stars.seminar_excel_reader.main."
        "_fetch_configured_sheet_values",
        new=AsyncMock(return_value=(_tomorrow_rows(), "seminar_excel")),
    ):
        sent = await send_tomorrow_seminar_reminder(
            context,
            config,
            reference_day=date(2026, 9, 16),
        )

    assert sent is True
    context.send_message.assert_awaited_once()
    message_chain = context.send_message.await_args.args[1]
    text = message_chain.chain[0].text
    assert "共有2人汇报，分别是：张三、李四" in text
    assert text.count("论文标题：") == 2


@pytest.mark.asyncio
async def test_summary_prefers_paper_link_over_ppt() -> None:
    headers = ["汇报人", "论文", "会议/期刊", "论文链接", "PPT"]
    row = [
        "张三",
        "Paper A",
        "OSDI",
        "https://example.com/paper.pdf",
        "https://example.com/slides.pdf",
    ]

    with (
        patch(
            "astrbot.builtin_stars.seminar_excel_reader.main._load_reference_text",
            new=AsyncMock(return_value="paper source"),
        ) as load_paper,
        patch(
            "astrbot.builtin_stars.seminar_excel_reader.main._load_ppt_text",
            new=AsyncMock(return_value="ppt source"),
        ) as load_ppt,
        patch(
            "astrbot.builtin_stars.seminar_excel_reader.main._summarize_paper_source",
            new=AsyncMock(return_value="论文来源生成的简介"),
        ),
    ):
        entry = await _build_natural_reminder_entry(
            row,
            headers,
            SimpleNamespace(),
        )

    load_paper.assert_awaited_once_with("https://example.com/paper.pdf")
    load_ppt.assert_not_awaited()
    assert "论文简介：论文来源生成的简介" in entry


@pytest.mark.asyncio
async def test_summary_falls_back_to_ppt_when_paper_source_is_unavailable() -> None:
    headers = ["汇报人", "论文", "会议/期刊", "论文链接", "PPT"]
    row = [
        "张三",
        "Paper A",
        "OSDI",
        "https://example.com/paper.pdf",
        "https://example.com/slides.pdf",
    ]
    summarize = AsyncMock(side_effect=["", "PPT 来源生成的简介"])
    context = SimpleNamespace()

    with (
        patch(
            "astrbot.builtin_stars.seminar_excel_reader.main._load_reference_text",
            new=AsyncMock(return_value=""),
        ),
        patch(
            "astrbot.builtin_stars.seminar_excel_reader.main._load_ppt_text",
            new=AsyncMock(return_value="ppt source"),
        ) as load_ppt,
        patch(
            "astrbot.builtin_stars.seminar_excel_reader.main._summarize_paper_source",
            new=summarize,
        ),
    ):
        entry = await _build_natural_reminder_entry(
            row,
            headers,
            context,
        )

    load_ppt.assert_awaited_once_with(
        "https://example.com/slides.pdf",
        context,
        platform_id=None,
    )
    assert summarize.await_count == 2
    assert "论文简介：PPT 来源生成的简介" in entry
