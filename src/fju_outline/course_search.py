"""Scrapling client and comparison helpers for the FJU course-search APIs.

The student information site is a JavaScript application.  Its two public
course-search screens call JSON endpoints rather than returning course rows
in the initial HTML document.  This module reproduces those requests using
the project's existing Scrapling-capable HTTP client.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from .client import FjuOutlineClient, FetchResult


COURSE_SEARCH_BASE_URL = "https://travellerlink.fju.edu.tw/FjuCourse/api"
BASIC_ENDPOINT = "CourseStudent/CourseStudentQueryByBasics"
GENERAL_ENDPOINT = "CourseStudent/CourseStudentQueryByGDPT"
COURSE_SEARCH_COUNTS_FILENAME = "course-search-counts.json"
COURSE_SEARCH_COUNTS_SCHEMA_VERSION = "fju_course_search_counts_v1"
DIVISIONS = ("D", "C", "G", "T")
GENERAL_GROUPS = ("NT", "PT", "ST", "DT")


@dataclass(frozen=True)
class QueryPartition:
    """One independent API query, such as day-division D or group NT."""

    kind: str
    division: str
    group: str | None
    endpoint: str
    base_params: dict[str, Any]

    def params_for_page(self, page_number: int, page_size: int) -> dict[str, Any]:
        params = dict(self.base_params)
        if self.kind == "basic":
            # These names are the names emitted by the basicInfo screen.
            params.update(
                {
                    "rowsPerPage": page_size,
                    "pageNumber": page_number,
                    "pageSize": page_size,
                }
            )
        else:
            # These names are the names emitted by the generalEdu screen.
            params.update({"PageNumber": page_number, "PageSize": page_size})
        return params


def build_partitions(*, hy: int, ht: int, lcid: int, sco_typ: int) -> list[QueryPartition]:
    """Build the same division/group query grid exposed by the two screens."""

    basic = [
        QueryPartition(
            kind="basic",
            division=division,
            group=None,
            endpoint=BASIC_ENDPOINT,
            base_params={
                "Hy": hy,
                "ScoTyp": sco_typ,
                "Ht": ht,
                "DayNgt": division,
                "lcid": lcid,
                "sortBy": "",
                "descending": "false",
            },
        )
        for division in DIVISIONS
    ]
    general = [
        QueryPartition(
            kind="general",
            division=division,
            group=group,
            endpoint=GENERAL_ENDPOINT,
            base_params={
                "Hy": hy,
                "ScoTyp": sco_typ,
                "Ht": ht,
                "DayNgt": division,
                "GDptGroup": group,
                "ACTFlag": False,
                "Lcid": lcid,
            },
        )
        for division in DIVISIONS
        for group in GENERAL_GROUPS
    ]
    return [*basic, *general]


async def _fetch_partition(
    client: FjuOutlineClient,
    partition: QueryPartition,
    *,
    page_size: int,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    async def fetch_page(page_number: int) -> FetchResult:
        async with semaphore:
            return await client.get_json(
                partition.endpoint,
                **partition.params_for_page(page_number, page_size),
            )

    first = await fetch_page(1)
    first_result = _page_result(first)
    total_pages = int(first_result.get("totalPages") or 0)
    expected_total = int(first_result.get("totalItemCount") or 0)
    rows_by_page: dict[int, list[dict[str, Any]]] = {
        1: list(first_result.get("result") or [])
    }

    tasks = [
        asyncio.create_task(fetch_page(page_number))
        for page_number in range(2, total_pages + 1)
    ]
    for task in asyncio.as_completed(tasks):
        result = _page_result(await task)
        page_number = int(result.get("pageNumber") or 0)
        if page_number < 2:
            raise RuntimeError(
                f"{partition.endpoint} returned an invalid page number: {page_number}"
            )
        rows_by_page[page_number] = list(result.get("result") or [])

    missing_pages = [
        page_number for page_number in range(1, total_pages + 1)
        if page_number not in rows_by_page
    ]
    if missing_pages:
        raise RuntimeError(
            f"{partition.endpoint} missing pages {missing_pages} for {partition.base_params}"
        )

    rows = [row for page_number in range(1, total_pages + 1) for row in rows_by_page[page_number]]
    if len(rows) != expected_total:
        raise RuntimeError(
            f"{partition.endpoint} expected {expected_total} rows but fetched {len(rows)} "
            f"for {partition.base_params}"
        )

    return {
        "kind": partition.kind,
        "division": partition.division,
        "group": partition.group,
        "endpoint": partition.endpoint,
        "query": partition.base_params,
        "page_size": page_size,
        "total_item_count": expected_total,
        "total_pages": total_pages,
        "rows": rows,
    }


def _page_result(fetch_result: FetchResult) -> dict[str, Any]:
    result = fetch_result.data.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(
            f"{fetch_result.endpoint} did not return a paginated result object: "
            f"{fetch_result.data}"
        )
    return result


async def crawl_course_search(
    client: FjuOutlineClient,
    *,
    hy: int,
    ht: int,
    lcid: int,
    sco_typ: int,
    page_size: int = 100,
    concurrency: int = 3,
) -> dict[str, Any]:
    """Fetch every basic and general-education course-search page."""

    if page_size <= 0:
        raise ValueError("page_size must be positive")
    if concurrency <= 0:
        raise ValueError("concurrency must be positive")

    semaphore = asyncio.Semaphore(concurrency)
    partitions = build_partitions(hy=hy, ht=ht, lcid=lcid, sco_typ=sco_typ)
    results = await asyncio.gather(
        *(
            _fetch_partition(
                client,
                partition,
                page_size=page_size,
                semaphore=semaphore,
            )
            for partition in partitions
        )
    )

    by_kind: dict[str, dict[str, Any]] = {}
    for kind in ("basic", "general"):
        kind_partitions = [item for item in results if item["kind"] == kind]
        by_kind[kind] = {
            "partitions": kind_partitions,
            "rows": [
                row
                for partition in kind_partitions
                for row in partition["rows"]
            ],
        }
    return by_kind


def normalize_identifier(value: Any) -> str | None:
    """Normalize an API identifier without changing meaningful code symbols."""

    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    return re.sub(r"\s+", "", text).casefold() or None


def course_search_id(row: dict[str, Any]) -> str | None:
    return normalize_identifier(row.get("jonCouSn"))


def _normalize_course_search_count(value: Any) -> int | None:
    """Normalize a course-search quota while preserving zero as a value."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError("Course-search counts must be integers, not booleans")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Course-search count is not numeric: {value!r}") from exc
    if not number.is_integer() or number < 0:
        raise ValueError(f"Course-search count must be a non-negative integer: {value!r}")
    return int(number)


def build_course_search_counts_payload(
    basic_rows: Iterable[dict[str, Any]],
    target_course_ids: Iterable[Any],
    *,
    metadata: Mapping[str, Any] | None = None,
    basic_snapshot_sha256: str | None = None,
) -> dict[str, Any]:
    """Build the small, ID-keyed sidecar used by the recommendation catalog.

    ``target_course_ids`` is intentionally supplied by the embedding index.  A
    course-search row that has no corresponding outline/vector ID is ignored.
    Every target ID is emitted, including an all-null row when the course
    search snapshot does not contain it, so coverage is explicit and stable.
    """

    target_ids = [
        normalized
        for value in target_course_ids
        if (normalized := normalize_identifier(value)) is not None
    ]
    if len(target_ids) != len(set(target_ids)):
        raise ValueError("Embedding target course IDs are not unique")

    target_set = set(target_ids)
    matched: dict[str, dict[str, int | None]] = {}
    for row in basic_rows:
        course_id = course_search_id(row)
        if course_id is None or course_id not in target_set:
            continue
        if course_id in matched:
            raise ValueError(f"Course-search rows contain duplicate course ID: {course_id}")
        matched[course_id] = {
            "selectable_count": _normalize_course_search_count(row.get("totNum")),
            "external_count": _normalize_course_search_count(row.get("outNum")),
        }

    source_metadata = dict(metadata or {})
    payload: dict[str, Any] = {
        "schema_version": COURSE_SEARCH_COUNTS_SCHEMA_VERSION,
        "academic_year": source_metadata.get("hy"),
        "semester": source_metadata.get("ht"),
        "captured_at": source_metadata.get("captured_at"),
        "source": {
            "transport": source_metadata.get("transport"),
            "base_url": source_metadata.get("course_search_base_url", COURSE_SEARCH_BASE_URL),
            "endpoint": BASIC_ENDPOINT,
            "lcid": source_metadata.get("lcid"),
            "sco_typ": source_metadata.get("sco_typ"),
            "basic_snapshot_sha256": basic_snapshot_sha256,
        },
        "target_course_count": len(target_ids),
        "matched_course_count": len(matched),
        "courses": {
            course_id: matched.get(
                course_id,
                {"selectable_count": None, "external_count": None},
            )
            for course_id in target_ids
        },
    }
    return payload


def validate_course_search_counts_payload(
    payload: Any,
    expected_course_ids: Iterable[Any],
) -> dict[str, dict[str, int | None]]:
    """Validate and return sidecar rows keyed by the embedding catalog IDs."""

    if not isinstance(payload, dict) or payload.get("schema_version") != COURSE_SEARCH_COUNTS_SCHEMA_VERSION:
        raise ValueError("Course-search counts sidecar has an unsupported schema")
    courses = payload.get("courses")
    if not isinstance(courses, dict):
        raise ValueError("Course-search counts sidecar has no course map")

    expected = [
        normalized
        for value in expected_course_ids
        if (normalized := normalize_identifier(value)) is not None
    ]
    actual = {normalize_identifier(key) for key in courses}
    if (
        None in actual
        or len(courses) != len(expected)
        or len(actual) != len(courses)
        or actual != set(expected)
    ):
        raise ValueError("Course-search counts IDs do not exactly match the embedding catalog")
    if payload.get("target_course_count") is not None and int(payload["target_course_count"]) != len(expected):
        raise ValueError("Course-search counts target count does not match the embedding catalog")

    normalized_rows: dict[str, dict[str, int | None]] = {}
    for course_id in expected:
        raw = courses.get(course_id)
        if raw is None:
            # JSON object keys can differ only by normalization, e.g. casing or
            # Unicode width. Find the canonical key before rejecting the row.
            raw = next((value for key, value in courses.items() if normalize_identifier(key) == course_id), None)
        if not isinstance(raw, dict) or set(raw) != {"selectable_count", "external_count"}:
            raise ValueError(f"Course-search counts row is invalid: {course_id}")
        normalized_rows[course_id] = {
            "selectable_count": _normalize_course_search_count(raw["selectable_count"]),
            "external_count": _normalize_course_search_count(raw["external_count"]),
        }
    return normalized_rows


def attach_course_search_counts(
    catalog: list[dict[str, Any]],
    counts: Mapping[str, Mapping[str, int | None]],
) -> list[dict[str, Any]]:
    """Attach only the two approved course-search values to catalog rows."""

    for course in catalog:
        course_id = normalize_identifier(course.get("course_id"))
        if course_id in counts:
            course["course_search_counts"] = {
                "selectable_count": counts[course_id].get("selectable_count"),
                "external_count": counts[course_id].get("external_count"),
            }
    return catalog


def course_search_codes(row: dict[str, Any]) -> set[str]:
    return {
        code
        for value in (row.get("jAvaNO"), row.get("avaNO"))
        if (code := normalize_identifier(value)) is not None
    }


def outline_id(record: dict[str, Any]) -> str | None:
    course = record.get("course") or {}
    source = record.get("source") or {}
    return normalize_identifier(course.get("jon_cou_sn") or source.get("jon_cou_sn"))


def outline_codes(record: dict[str, Any]) -> set[str]:
    course = record.get("course") or {}
    list_row = record.get("raw", {}).get("list_row") or record.get("list_row") or {}
    return {
        code
        for value in (
            course.get("joint_ava_no"),
            course.get("ava_no"),
            list_row.get("jAvaNO"),
            list_row.get("avaNO"),
        )
        if (code := normalize_identifier(value)) is not None
    }


def _primary_course_search_code(row: dict[str, Any]) -> str | None:
    return normalize_identifier(row.get("jAvaNO"))


def _primary_outline_code(record: dict[str, Any]) -> str | None:
    course = record.get("course") or {}
    return normalize_identifier(course.get("joint_ava_no") or course.get("ava_no"))


def _duplicates(values: Iterable[str | None]) -> list[str]:
    counts = Counter(value for value in values if value is not None)
    return sorted(value for value, count in counts.items() if count > 1)


def _source_summary(
    rows: list[dict[str, Any]],
    *,
    id_getter: Callable[[dict[str, Any]], str | None],
    codes_getter: Callable[[dict[str, Any]], set[str]],
    primary_code_getter: Callable[[dict[str, Any]], str | None],
) -> dict[str, Any]:
    ids = [id_getter(row) for row in rows]
    code_sets = [codes_getter(row) for row in rows]
    primary_codes = {
        value
        for row in rows
        if (value := primary_code_getter(row)) is not None
    }
    all_codes = set().union(*code_sets) if code_sets else set()
    return {
        "records": len(rows),
        "records_with_id": sum(value is not None for value in ids),
        "unique_ids": len({value for value in ids if value is not None}),
        "duplicate_ids": _duplicates(ids),
        "records_with_code": sum(bool(values) for values in code_sets),
        # ``jAvaNO`` is the course code displayed by the search table.  The
        # variant count is kept separately because ``avaNO`` can be a related
        # code for cross-listed or jointly offered courses.
        "unique_codes": len(primary_codes),
        "unique_code_variants": len(all_codes),
        "duplicate_code_count": sum(
            count > 1
            for count in Counter(
                code for values in code_sets for code in values
            ).values()
        ),
    }


def _set_diff(left: set[str], right: set[str]) -> dict[str, Any]:
    only_left = sorted(left - right)
    only_right = sorted(right - left)
    return {
        "intersection": len(left & right),
        "only_left_count": len(only_left),
        "only_right_count": len(only_right),
        "only_left": only_left,
        "only_right": only_right,
    }


def _compare_pair(
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
    *,
    left_id_getter: Callable[[dict[str, Any]], str | None],
    right_id_getter: Callable[[dict[str, Any]], str | None],
    left_codes_getter: Callable[[dict[str, Any]], set[str]],
    right_codes_getter: Callable[[dict[str, Any]], set[str]],
    left_primary_code_getter: Callable[[dict[str, Any]], str | None],
    right_primary_code_getter: Callable[[dict[str, Any]], str | None],
) -> dict[str, Any]:
    left_ids = {value for row in left_rows if (value := left_id_getter(row)) is not None}
    right_ids = {value for row in right_rows if (value := right_id_getter(row)) is not None}
    left_codes = set().union(*(left_codes_getter(row) for row in left_rows)) if left_rows else set()
    right_codes = set().union(*(right_codes_getter(row) for row in right_rows)) if right_rows else set()
    left_primary = {
        value
        for row in left_rows
        if (value := left_primary_code_getter(row)) is not None
    }
    right_primary = {
        value
        for row in right_rows
        if (value := right_primary_code_getter(row)) is not None
    }
    return {
        "id": _set_diff(left_ids, right_ids),
        "code_any_variant": _set_diff(left_codes, right_codes),
        "code_primary": _set_diff(left_primary, right_primary),
        "left_records_matched_by_id": sum(
            left_id_getter(row) in right_ids for row in left_rows
        ),
        "right_records_matched_by_id": sum(
            right_id_getter(row) in left_ids for row in right_rows
        ),
        "left_records_matched_by_code": sum(
            bool(left_codes_getter(row) & right_codes) for row in left_rows
        ),
        "right_records_matched_by_code": sum(
            bool(right_codes_getter(row) & left_codes) for row in right_rows
        ),
    }


def compare_course_sources(
    *,
    outline_records: list[dict[str, Any]],
    basic_rows: list[dict[str, Any]],
    general_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare outline records with both course-search API result sets."""

    return {
        "outline": _source_summary(
            outline_records,
            id_getter=outline_id,
            codes_getter=outline_codes,
            primary_code_getter=_primary_outline_code,
        ),
        "basic": _source_summary(
            basic_rows,
            id_getter=course_search_id,
            codes_getter=course_search_codes,
            primary_code_getter=_primary_course_search_code,
        ),
        "general": _source_summary(
            general_rows,
            id_getter=course_search_id,
            codes_getter=course_search_codes,
            primary_code_getter=_primary_course_search_code,
        ),
        "comparisons": {
            "basic_vs_outline": _compare_pair(
                basic_rows,
                outline_records,
                left_id_getter=course_search_id,
                right_id_getter=outline_id,
                left_codes_getter=course_search_codes,
                right_codes_getter=outline_codes,
                left_primary_code_getter=_primary_course_search_code,
                right_primary_code_getter=_primary_outline_code,
            ),
            "general_vs_basic": _compare_pair(
                general_rows,
                basic_rows,
                left_id_getter=course_search_id,
                right_id_getter=course_search_id,
                left_codes_getter=course_search_codes,
                right_codes_getter=course_search_codes,
                left_primary_code_getter=_primary_course_search_code,
                right_primary_code_getter=_primary_course_search_code,
            ),
        },
    }
