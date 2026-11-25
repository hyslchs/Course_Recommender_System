#!/usr/bin/env python3
"""Crawl FJU course-search APIs with Scrapling and compare them to outlines."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fju_outline.client import FjuOutlineClient
from fju_outline.config import DEFAULT_HT, DEFAULT_HY, DEFAULT_LCID, DEFAULT_SCO_TYP
from fju_outline.course_search import (
    COURSE_SEARCH_BASE_URL,
    compare_course_sources,
    crawl_course_search,
)
from fju_outline.io import iter_jsonl, write_json, write_jsonl


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl FJU basic/general course search APIs and audit outline coverage."
    )
    parser.add_argument("--hy", type=int, default=DEFAULT_HY)
    parser.add_argument("--ht", type=int, default=DEFAULT_HT)
    parser.add_argument("--sco-typ", type=int, default=DEFAULT_SCO_TYP)
    parser.add_argument("--lcid", type=int, default=DEFAULT_LCID)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument(
        "--canonical",
        type=Path,
        default=None,
        help="Canonical outline JSONL; defaults to data/canonical/course_outlines_<hy><ht>.jsonl.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for raw snapshots and audit reports; defaults to tmp/course_search_audit_<hy><ht>.",
    )
    return parser.parse_args()


def _markdown_report(
    *,
    metadata: dict[str, Any],
    comparison: dict[str, Any],
) -> str:
    basic = comparison["basic"]
    general = comparison["general"]
    outline = comparison["outline"]
    basic_match = comparison["comparisons"]["basic_vs_outline"]
    general_match = comparison["comparisons"]["general_vs_basic"]
    id_diff = basic_match["id"]
    code_diff = basic_match["code_any_variant"]

    lines = [
        f"# {metadata['hy']}-{metadata['ht']} 課程查詢與課程大綱比對",
        "",
        f"- 抓取時間：{metadata['captured_at']}",
        f"- 課程大綱快照時間：{metadata['outline_snapshot_fetched_at_min']} 至 {metadata['outline_snapshot_fetched_at_max']}",
        f"- 查詢 API：`{COURSE_SEARCH_BASE_URL}`",
        f"- 參數：學年度 {metadata['hy']}、學期 {metadata['ht']}、語系 {metadata['lcid']}、課程類別 {metadata['sco_typ']}",
        "- 注意：兩個來源的抓取時間不同；差異反映的是當時可查到的資料範圍，不能單憑這次比對判定為爬蟲漏抓。",
        "",
        "## 筆數摘要",
        "",
        "| 資料來源 | 原始筆數 | 不重複課程 ID | 主要課程碼 | 所有課程碼欄位 |",
        "|---|---:|---:|---:|---:|",
        f"| 基本資料查詢 | {basic['records']:,} | {basic['unique_ids']:,} | {basic['unique_codes']:,} | {basic['unique_code_variants']:,} |",
        f"| 通識查詢 | {general['records']:,} | {general['unique_ids']:,} | {general['unique_codes']:,} | {general['unique_code_variants']:,} |",
        f"| 課程大綱 | {outline['records']:,} | {outline['unique_ids']:,} | {outline['unique_codes']:,} | {outline['unique_code_variants']:,} |",
        "",
        "## 對應結果",
        "",
        f"- 基本資料 vs 課程大綱：以課程 ID 對上 {id_diff['intersection']:,} 筆；基本資料多 {id_diff['only_left_count']:,} 筆 ID；課程大綱多 {id_diff['only_right_count']:,} 筆 ID。",
        f"- 以主要課程碼比對：共同 {basic_match['code_primary']['intersection']:,} 個；基本資料獨有 {basic_match['code_primary']['only_left_count']:,} 個；課程大綱獨有 {basic_match['code_primary']['only_right_count']:,} 個。",
        f"- 若把 `avaNO` 等相關碼也納入：共同 {code_diff['intersection']:,} 個；基本資料獨有 {code_diff['only_left_count']:,} 個；課程大綱獨有 {code_diff['only_right_count']:,} 個。",
        f"- 通識查詢 vs 基本資料：共同課程 ID {general_match['id']['intersection']:,} 個；通識結果中有 {general_match['left_records_matched_by_id']:,} 筆可在基本資料找到。",
        "",
        "## 解讀",
        "",
        "基本資料查詢是完整開課清單；通識查詢是其中依通識領域篩出的子集合，因此不應把兩者直接相加。實際推薦系統若要顯示選課規定，應以基本資料的同一筆課程為主，再用通識查詢或其欄位補充通識分類。",
        "",
        "完整的差異 ID 與課程碼清單請見 `comparison.json`；原始 API 回應則保存在 `basic.jsonl` 與 `general.jsonl`。",
        "",
    ]
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    tag = f"{args.hy}{args.ht}"
    canonical = args.canonical or ROOT / "data" / "canonical" / f"course_outlines_{tag}.jsonl"
    output_dir = args.output_dir or ROOT / "tmp" / f"course_search_audit_{tag}"
    output_dir.mkdir(parents=True, exist_ok=True)

    outline_records = list(iter_jsonl(canonical) or [])
    client = FjuOutlineClient(
        base_url=COURSE_SEARCH_BASE_URL,
        transport="scrapling",
    )
    crawled = await crawl_course_search(
        client,
        hy=args.hy,
        ht=args.ht,
        lcid=args.lcid,
        sco_typ=args.sco_typ,
        page_size=args.page_size,
        concurrency=args.concurrency,
    )
    basic_rows = crawled["basic"]["rows"]
    general_rows = crawled["general"]["rows"]
    comparison = compare_course_sources(
        outline_records=outline_records,
        basic_rows=basic_rows,
        general_rows=general_rows,
    )
    outline_times = sorted(
        source.get("fetched_at")
        for record in outline_records
        if (source := record.get("source") or {}).get("fetched_at")
    )
    metadata = {
        "captured_at": _now_iso(),
        "hy": args.hy,
        "ht": args.ht,
        "sco_typ": args.sco_typ,
        "lcid": args.lcid,
        "page_size": args.page_size,
        "concurrency": args.concurrency,
        "transport": client.transport,
        "course_search_base_url": COURSE_SEARCH_BASE_URL,
        "canonical": str(canonical),
        "outline_snapshot_fetched_at_min": outline_times[0] if outline_times else None,
        "outline_snapshot_fetched_at_max": outline_times[-1] if outline_times else None,
        "basic_partition_counts": [
            {
                "division": item["division"],
                "total_item_count": item["total_item_count"],
                "total_pages": item["total_pages"],
            }
            for item in crawled["basic"]["partitions"]
        ],
        "general_partition_counts": [
            {
                "division": item["division"],
                "group": item["group"],
                "total_item_count": item["total_item_count"],
                "total_pages": item["total_pages"],
            }
            for item in crawled["general"]["partitions"]
        ],
    }

    write_jsonl(output_dir / "basic.jsonl", basic_rows)
    write_jsonl(output_dir / "general.jsonl", general_rows)
    write_json(output_dir / "metadata.json", metadata)
    write_json(output_dir / "comparison.json", comparison)
    (output_dir / "comparison.md").write_text(
        _markdown_report(metadata=metadata, comparison=comparison),
        encoding="utf-8",
    )

    return {
        "output_dir": str(output_dir),
        "basic_rows": len(basic_rows),
        "general_rows": len(general_rows),
        "outline_rows": len(outline_records),
        "comparison": comparison,
    }


def main() -> None:
    args = _parse_args()
    result = asyncio.run(_run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
