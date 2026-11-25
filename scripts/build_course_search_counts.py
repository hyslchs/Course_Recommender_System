#!/usr/bin/env python3
"""Attach the two course-search counts to an existing artifact bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from fju_outline.artifacts import validate_artifacts
from fju_outline.course_search import (
    COURSE_SEARCH_COUNTS_FILENAME,
    build_course_search_counts_payload,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _index_ids(index: dict[str, Any]) -> list[str]:
    if isinstance(index.get("course_ids"), list):
        return [str(value) for value in index["course_ids"]]
    rows = index.get("rows")
    if isinstance(rows, list):
        return [str(row["course_id"]) for row in rows]
    raise ValueError("embedding-index.json has no course IDs")


def _set_manifest_file(
    entries: dict[str, dict[str, Any]] | list[dict[str, Any]],
    filename: str,
    metadata: dict[str, Any],
) -> dict[str, dict[str, Any]] | list[dict[str, Any]]:
    if isinstance(entries, dict):
        entries[filename] = metadata
        return entries
    for item in entries:
        if item.get("filename") == filename:
            item.update(metadata)
            return entries
    entries.append({"filename": filename, **metadata})
    return entries


def _read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid basic JSONL at line {line_number}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"basic JSONL row {line_number} is not an object")
            rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build course-search count metadata without recomputing vectors."
    )
    parser.add_argument("--basic", type=Path, required=True, help="Scrapling basic.jsonl")
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, help="Optional course-search metadata.json")
    parser.add_argument("--lock", type=Path, help="Optional bundle-lock.json to update")
    parser.add_argument("--year", type=int, default=115)
    parser.add_argument("--semester", type=int, default=1)
    parser.add_argument(
        "--skip-existing-validation",
        action="store_true",
        help="Allow metadata-only updates to a legacy artifact rejected by the current route validator.",
    )
    args = parser.parse_args(argv)

    if not args.basic.is_file():
        raise SystemExit(f"basic JSONL not found: {args.basic}")
    if not args.artifact_dir.is_dir():
        raise SystemExit(f"artifact directory not found: {args.artifact_dir}")

    # Validate the pre-existing bundle before changing its metadata.  This
    # protects the vector/index/catalog relationship and makes this command a
    # metadata-only operation by construction.
    if not args.skip_existing_validation:
        validate_artifacts(args.artifact_dir, verify_hashes=True)
    index = json.loads((args.artifact_dir / "embedding-index.json").read_text(encoding="utf-8"))
    metadata: dict[str, Any] = {}
    metadata_path = args.metadata or args.basic.with_name("metadata.json")
    if metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.setdefault("hy", args.year)
    metadata.setdefault("ht", args.semester)

    payload = build_course_search_counts_payload(
        _read_rows(args.basic),
        _index_ids(index),
        metadata=metadata,
        basic_snapshot_sha256=sha256(args.basic),
    )
    sidecar_path = args.artifact_dir / COURSE_SEARCH_COUNTS_FILENAME
    sidecar_tmp = sidecar_path.with_suffix(sidecar_path.suffix + ".tmp")
    sidecar_tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    sidecar_tmp.replace(sidecar_path)

    manifest_path = args.artifact_dir / "artifact-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = _set_manifest_file(
        manifest.get("files") or {},
        COURSE_SEARCH_COUNTS_FILENAME,
        {"sha256": sha256(sidecar_path), "bytes": sidecar_path.stat().st_size},
    )
    manifest_tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    manifest_tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest_tmp.replace(manifest_path)

    if args.lock:
        lock = json.loads(args.lock.read_text(encoding="utf-8"))
        artifact = lock.setdefault("artifact", {})
        lock_files = artifact.setdefault("files", {})
        if not isinstance(lock_files, dict):
            raise ValueError("bundle-lock.json artifact.files must be an object")
        lock_files[COURSE_SEARCH_COUNTS_FILENAME] = {
            "sha256": sha256(sidecar_path),
            "bytes": sidecar_path.stat().st_size,
        }
        artifact["manifest_sha256"] = sha256(manifest_path)
        lock_tmp = args.lock.with_suffix(args.lock.suffix + ".tmp")
        lock_tmp.write_text(
            json.dumps(lock, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        lock_tmp.replace(args.lock)

    if not args.skip_existing_validation:
        validate_artifacts(args.artifact_dir, verify_hashes=True)
    print(json.dumps({
        "artifact_dir": str(args.artifact_dir),
        "sidecar": str(sidecar_path),
        "target_course_count": payload["target_course_count"],
        "matched_course_count": payload["matched_course_count"],
        "missing_course_count": payload["target_course_count"] - payload["matched_course_count"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
