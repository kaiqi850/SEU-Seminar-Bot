from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from astrbot.builtin_stars.seminar_excel_updater.main import (
    Main as UpdaterMain,
)
from astrbot.builtin_stars.seminar_excel_updater.main import (
    PaperMetadata,
    TableSchema,
    UserState,
    _build_upsert_plan,
    _canonical_link,
    _extract_paper_metadata,
    _find_record_by_title,
    _is_expected_pdf_link_reply,
    _next_meeting_time,
    _parse_custom_meeting_date,
    _resolve_schema,
    _start_new_submission,
    _text_from_url,
    _upcoming_speaker_time,
)


@pytest.fixture
def schema() -> TableSchema:
    return TableSchema(
        title="论文标题",
        venue="会议/期刊",
        speaker="汇报人",
        paper_link="论文链接",
        time="组会汇报时间",
        location="地点",
        meeting_link="腾讯会议",
        ppt="ppt的pdf（命名示例）",
        field_types={"论文链接": 15, "腾讯会议": 15, "ppt的pdf（命名示例）": 17},
    )


def test_updates_by_case_insensitive_paper_title_before_speaker(
    schema: TableSchema,
) -> None:
    records = [
        {
            "record_id": "record_a",
            "fields": {
                "论文标题": "Paper A",
                "汇报人": "张三",
                "组会汇报时间": 1_000,
            },
        },
        {
            "record_id": "record_b",
            "fields": {
                "论文标题": "Paper B",
                "汇报人": "张三",
                "组会汇报时间": 2_000,
            },
        },
    ]

    plan = _build_upsert_plan(
        records,
        schema,
        PaperMetadata(
            title="pApEr b",
            venue="SIGCOMM",
            paper_link="https://example.com/paper-b",
        ),
        "张三",
    )

    assert plan.record_id == "record_b"
    assert plan.fields["论文标题"] == "pApEr b"
    assert plan.fields["论文链接"] == {
        "link": "https://example.com/paper-b",
        "text": "https://example.com/paper-b",
    }
    assert "组会汇报时间" not in plan.fields


def test_creates_new_record_and_fills_schedule_from_latest_record(
    schema: TableSchema,
) -> None:
    latest_time = 1_780_887_600_000
    records = [
        {
            "record_id": "older",
            "fields": {
                "论文标题": "Old",
                "组会汇报时间": latest_time - 604_800_000,
                "地点": "旧会议室",
                "腾讯会议": {"link": "https://old.example", "text": "old"},
            },
        },
        {
            "record_id": "latest_completed",
            "fields": {
                "论文标题": "Latest",
                "组会汇报时间": latest_time,
                "地点": "Room ARTS1021 @ SEU & Online",
                "腾讯会议": {
                    "link": "https://meeting.tencent.com/example",
                    "text": "腾讯会议",
                },
            },
        },
    ]

    plan = _build_upsert_plan(
        records,
        schema,
        PaperMetadata(
            title="New Paper",
            venue="OSDI",
            paper_link="https://example.com/new",
        ),
        "李四",
    )

    assert plan.record_id is None
    assert plan.fields["汇报人"] == "李四"
    assert plan.fields["组会汇报时间"] == latest_time + 604_800_000
    assert plan.fields["地点"] == "Room ARTS1021 @ SEU & Online"
    assert plan.fields["腾讯会议"] == {
        "link": "https://meeting.tencent.com/example",
        "text": "腾讯会议",
    }


def test_new_title_does_not_reuse_an_existing_speaker_record(
    schema: TableSchema,
) -> None:
    latest_time = 1_780_887_600_000
    records = [
        {
            "record_id": "existing",
            "fields": {
                "论文标题": "Existing",
                "汇报人": "李四",
                "组会汇报时间": latest_time,
                "地点": "Room ARTS1021 @ SEU & Online",
                "腾讯会议": {"link": "https://meeting.example", "text": "会议"},
            },
        }
    ]

    plan = _build_upsert_plan(
        records,
        schema,
        PaperMetadata(
            title="Unknown",
            venue="OSDI",
            paper_link="https://example.com/unknown",
        ),
        "李四",
    )

    assert plan.record_id is None
    assert plan.fields["组会汇报时间"] == latest_time + 604_800_000


def test_new_record_joins_nearest_future_meeting_instead_of_skipping_a_week(
    schema: TableSchema,
) -> None:
    records = [
        {
            "record_id": "next",
            "fields": {
                "论文标题": "Paper A",
                "汇报人": "张三",
                "组会汇报时间": 2_000,
                "地点": "Next room",
                "腾讯会议": "next meeting",
            },
        },
        {
            "record_id": "later",
            "fields": {
                "论文标题": "Paper B",
                "汇报人": "王五",
                "组会汇报时间": 3_000,
                "地点": "Later room",
                "腾讯会议": "later meeting",
            },
        },
    ]

    plan = _build_upsert_plan(
        records,
        schema,
        PaperMetadata(
            title="New Paper",
            venue="OSDI",
            paper_link="https://example.com/new",
        ),
        "李四",
        now_ms=1_000,
    )

    assert plan.record_id is None
    assert plan.fields["组会汇报时间"] == 2_000
    assert plan.fields["地点"] == "Next room"


def test_detects_same_speaker_at_upcoming_meeting(schema: TableSchema) -> None:
    records = [
        {
            "record_id": "next",
            "fields": {
                "论文标题": "Paper A",
                "汇报人": "李四",
                "组会汇报时间": 2_000,
            },
        },
        {
            "record_id": "other",
            "fields": {
                "论文标题": "Paper B",
                "汇报人": "张三",
                "组会汇报时间": 1_500,
            },
        },
    ]

    assert _upcoming_speaker_time(records, schema, "李四", now_ms=1_000) is None
    assert _upcoming_speaker_time(records, schema, "张三", now_ms=1_000) == 1_500
    assert _upcoming_speaker_time(records, schema, "王五", now_ms=1_000) is None


def test_october_first_row_does_not_override_september_next_meeting(
    schema: TableSchema,
) -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    now_ms = int(datetime(2026, 9, 16, 17, 47, tzinfo=timezone).timestamp() * 1000)
    september = int(datetime(2026, 9, 17, 10, 30, tzinfo=timezone).timestamp() * 1000)
    october = int(datetime(2026, 10, 1, 11, 30, tzinfo=timezone).timestamp() * 1000)
    records = [
        {
            "record_id": "october-first-row",
            "fields": {
                "论文标题": "Later Paper",
                "汇报人": "李四",
                "组会汇报时间": october,
                "地点": "October room",
            },
        },
        {
            "record_id": "september-next-meeting",
            "fields": {
                "论文标题": "Next Paper",
                "汇报人": "张三",
                "组会汇报时间": september,
                "地点": "September room",
            },
        },
    ]

    assert _next_meeting_time(records, schema, now_ms=now_ms) == september
    assert _upcoming_speaker_time(records, schema, "李四", now_ms=now_ms) is None
    plan = _build_upsert_plan(
        records,
        schema,
        PaperMetadata(
            title="ServerlessLLM", venue="OSDI", paper_link="https://example.com/new"
        ),
        "李四",
        now_ms=now_ms,
    )
    assert plan.fields["组会汇报时间"] == september
    assert plan.fields["地点"] == "September room"

    records[1]["fields"]["汇报人"] = "李四"
    assert _upcoming_speaker_time(records, schema, "李四", now_ms=now_ms) == september


def test_custom_date_keeps_hour_and_minute_from_next_meeting() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    reference = int(datetime(2026, 9, 24, 10, 30, tzinfo=timezone).timestamp() * 1000)

    scheduled = _parse_custom_meeting_date(
        "2026-10-08",
        reference,
        "Asia/Shanghai",
    )

    assert scheduled is not None
    result = datetime.fromtimestamp(scheduled / 1000, timezone)
    assert result == datetime(2026, 10, 8, 10, 30, tzinfo=timezone)


@pytest.mark.asyncio
async def test_second_upcoming_paper_stops_for_same_meeting_confirmation(
    tmp_path: Path,
    schema: TableSchema,
) -> None:
    upcoming = int(datetime.now().timestamp() * 1000) + 604_800_000
    table = SimpleNamespace(
        schema=schema,
        records=[
            {
                "record_id": "first-paper",
                "fields": {
                    "论文标题": "First Paper",
                    "汇报人": "李四",
                    "组会汇报时间": upcoming,
                },
            }
        ],
    )
    state = UserState(
        title="Second Paper",
        venue="OSDI",
        speaker="李四",
        paper_link="https://example.com/second",
    )
    plugin = UpdaterMain(SimpleNamespace(), {"cron_timezone": "Asia/Shanghai"})

    with patch(
        "astrbot.builtin_stars.seminar_excel_updater.main._load_bitable_table",
        new=AsyncMock(return_value=table),
    ):
        response = await plugin._finish_paper(tmp_path / "state.json", state)

    assert "也安排在同一次组会吗" in response
    assert state.pending_field == "same_meeting_confirmation"
    assert state.proposed_meeting_time == str(upcoming)


@pytest.mark.asyncio
async def test_same_meeting_confirmation_commits_with_existing_time(
    tmp_path: Path,
    schema: TableSchema,
) -> None:
    upcoming = int(datetime.now().timestamp() * 1000) + 604_800_000
    state = UserState(
        title="Second Paper",
        venue="OSDI",
        speaker="李四",
        paper_link="https://example.com/second",
        pending_field="same_meeting_confirmation",
        proposed_meeting_time=str(upcoming),
    )
    plugin = UpdaterMain(SimpleNamespace(), {})

    with (
        patch(
            "astrbot.builtin_stars.seminar_excel_updater.main._load_bitable_table",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    schema=schema,
                    records=[{"fields": {schema.time: upcoming}}],
                )
            ),
        ),
        patch.object(
            plugin,
            "_commit_paper",
            new=AsyncMock(return_value="written"),
        ) as commit,
    ):
        response = await plugin._handle_pending_reply(
            tmp_path / "state.json",
            state,
            "是",
        )

    assert response == "written"
    commit.assert_awaited_once_with(
        tmp_path / "state.json",
        state,
        scheduled_time_override=upcoming,
    )


@pytest.mark.asyncio
async def test_stale_confirmation_is_corrected_before_any_write(
    tmp_path: Path,
    schema: TableSchema,
) -> None:
    upcoming = int(datetime.now().timestamp() * 1000) + 86_400_000
    state = UserState(
        pending_field="same_meeting_confirmation",
        proposed_meeting_time=str(upcoming + 1_209_600_000),
    )
    plugin = UpdaterMain(SimpleNamespace(), {})
    with (
        patch(
            "astrbot.builtin_stars.seminar_excel_updater.main._load_bitable_table",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    schema=schema,
                    records=[{"fields": {schema.time: upcoming}}],
                )
            ),
        ),
        patch.object(plugin, "_commit_paper", new=AsyncMock()) as commit,
    ):
        response = await plugin._handle_pending_reply(
            tmp_path / "state.json", state, "是"
        )

    assert "按全表最近的未来场次校正" in response
    assert state.proposed_meeting_time == str(upcoming)
    commit.assert_not_awaited()


def test_new_submission_discards_stale_pending_paper_state(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    stale = UserState(
        title="Old Paper",
        venue="Old Venue",
        speaker="张三",
        paper_link="https://example.com/old",
        pending_field="venue",
        last_title="Completed Paper",
    )

    fresh = _start_new_submission(state_path, stale)

    assert fresh == UserState(last_title="Completed Paper")
    assert "Old Paper" not in state_path.read_text(encoding="utf-8")


def test_only_pdf_workflow_can_accept_a_link_as_pending_reply() -> None:
    assert not _is_expected_pdf_link_reply(UserState(pending_field="paper_link"))
    assert _is_expected_pdf_link_reply(
        UserState(pending_field="paper_link", source_kind="pdf")
    )


@pytest.mark.asyncio
async def test_arxiv_numeric_path_is_parsed_as_html_from_content_type() -> None:
    html = b'<meta name="citation_title" content="Large Language Models: A Survey">'
    with patch(
        "astrbot.builtin_stars.seminar_excel_updater.main._download_public_url",
        new=AsyncMock(
            return_value=(
                html,
                "text/html; charset=utf-8",
                "https://arxiv.org/abs/2402.06196",
            )
        ),
    ):
        text, final_url = await _text_from_url(
            "https://arxiv.org/abs/2402.06196",
            1024,
        )

    assert final_url == "https://arxiv.org/abs/2402.06196"
    assert "Title: Large Language Models: A Survey" in text


def test_duplicate_titles_are_rejected(schema: TableSchema) -> None:
    records = [
        {"record_id": "a", "fields": {"论文标题": "Same Paper"}},
        {"record_id": "b", "fields": {"论文标题": "SAME PAPER"}},
    ]

    with pytest.raises(ValueError, match="同名论文"):
        _find_record_by_title(records, schema.title, "same paper")


def test_ppt_lookup_falls_back_to_speaker(schema: TableSchema) -> None:
    records = [
        {
            "record_id": "speaker_record",
            "fields": {"论文标题": "Existing", "汇报人": "张三"},
        }
    ]

    record = _find_record_by_title(
        records,
        schema.title,
        "Unknown Paper",
        speaker_field=schema.speaker,
        speaker="张三",
    )

    assert record["record_id"] == "speaker_record"


def test_resolves_current_bitable_field_names_and_ppt_prefix() -> None:
    field_items = [
        {"field_name": "论文标题", "type": 1},
        {"field_name": "会议/期刊", "type": 1},
        {"field_name": "汇报人", "type": 1},
        {"field_name": "组会汇报时间", "type": 5},
        {"field_name": "地点", "type": 1},
        {"field_name": "论文链接", "type": 15},
        {"field_name": "腾讯会议", "type": 15},
        {"field_name": "ppt的pdf（命名示例）", "type": 17},
    ]

    resolved = _resolve_schema(field_items, {"ppt_field": "PPT"})

    assert resolved.title == "论文标题"
    assert resolved.ppt == "ppt的pdf（命名示例）"
    assert resolved.field_types["论文链接"] == 15


def test_extracts_canonical_arxiv_or_doi_link_from_pdf_text() -> None:
    assert (
        _canonical_link("Preprint: https://arxiv.org/pdf/2409.19488v2")
        == "https://arxiv.org/abs/2409.19488"
    )
    assert (
        _canonical_link("DOI: 10.1145/12345.67890")
        == "https://doi.org/10.1145/12345.67890"
    )


@pytest.mark.asyncio
async def test_pdf_doi_overrides_a_less_stable_llm_link() -> None:
    provider = SimpleNamespace(
        text_chat=AsyncMock(
            return_value=SimpleNamespace(
                completion_text=(
                    '{"title":"Paper","venue":"OSDI",'
                    '"paper_link":"https://example.com/download.pdf",'
                    '"document_kind":"paper"}'
                )
            )
        )
    )
    context = SimpleNamespace(get_using_provider=lambda _umo: provider)

    metadata = await _extract_paper_metadata(
        context,
        "Paper front page. DOI: 10.1145/12345.67890",
        prefer_doi=True,
    )

    assert metadata.paper_link == "https://doi.org/10.1145/12345.67890"


@pytest.mark.asyncio
async def test_html_metadata_title_overrides_wrong_llm_title() -> None:
    provider = SimpleNamespace(
        text_chat=AsyncMock(
            return_value=SimpleNamespace(
                completion_text=(
                    '{"title":"Old Paper","venue":"","paper_link":"",'
                    '"document_kind":"paper"}'
                )
            )
        )
    )
    context = SimpleNamespace(get_using_provider=lambda _umo: provider)

    metadata = await _extract_paper_metadata(
        context,
        "Title: Large Language Models: A Survey\nDescription: An LLM survey.",
        source_url="https://arxiv.org/abs/2402.06196",
    )

    assert metadata.title == "Large Language Models: A Survey"
    assert metadata.paper_link == "https://arxiv.org/abs/2402.06196"
