#!/usr/bin/env python3
"""Refresh semantic query-route artifacts without rebuilding course vectors.

The course vectors are intentionally left byte-for-byte unchanged.  This is
used to bring a legacy EmbeddingGemma bundle back in sync with the six route
definitions in ``src/fju_outline/query_routes.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

import numpy as np

from fju_outline.artifacts import (
    EMBEDDING_GEMMA_DOCUMENT_PROMPT,
    EMBEDDING_GEMMA_DOCUMENT_PROMPT_VERSION,
    EMBEDDING_GEMMA_MODEL,
    EMBEDDING_GEMMA_QUERY_PROMPT,
    EMBEDDING_GEMMA_QUERY_PROMPT_VERSION,
    EMBEDDING_GEMMA_REVISION,
    EmbeddingGemmaEncoder,
)
from fju_outline.query_routes import build_route_embeddings, route_data


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file_entries(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    raw = manifest.get("files")
    if isinstance(raw, dict):
        return {str(name): dict(value) for name, value in raw.items()}
    if isinstance(raw, list):
        return {
            str(item["filename"]): dict(item)
            for item in raw
            if isinstance(item, dict) and item.get("filename")
        }
    raise ValueError("artifact manifest has no valid files section")


def _set_file_entries(manifest: dict[str, object], entries: dict[str, dict[str, object]]) -> None:
    raw = manifest.get("files")
    if isinstance(raw, list):
        manifest["files"] = [
            {"filename": name, "sha256": value["sha256"], "bytes": value["bytes"]}
            for name, value in entries.items()
        ]
    else:
        manifest["files"] = entries


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
    temporary.replace(path)


def _atomic_write_json(path: Path, value: object) -> None:
    _atomic_write_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def refresh(artifact_dir: Path, *, device: str | None = None) -> dict[str, object]:
    manifest_path = artifact_dir / "artifact-manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("model_id") != EMBEDDING_GEMMA_MODEL:
        raise ValueError("This script only refreshes EmbeddingGemma artifacts")
    if str(manifest.get("model_revision")) != EMBEDDING_GEMMA_REVISION:
        raise ValueError("EmbeddingGemma model revision does not match the supported artifact")
    dimension = int(manifest.get("dimension", 0))
    if dimension not in {512, 768}:
        raise ValueError("Expected a 512- or 768-dimensional EmbeddingGemma artifact")

    encoder = EmbeddingGemmaEncoder(
        EMBEDDING_GEMMA_MODEL,
        model_revision=EMBEDDING_GEMMA_REVISION,
        tokenizer_revision=manifest.get("tokenizer_revision"),
        dimension=768,
        document_prompt_template=EMBEDDING_GEMMA_DOCUMENT_PROMPT,
        query_prompt_template=EMBEDDING_GEMMA_QUERY_PROMPT,
        document_prompt_version=EMBEDDING_GEMMA_DOCUMENT_PROMPT_VERSION,
        query_prompt_version=EMBEDDING_GEMMA_QUERY_PROMPT_VERSION,
        device=device,
    )
    full_route_vectors = build_route_embeddings(encoder)
    if dimension == 512:
        route_vectors = full_route_vectors[:, :512]
        route_vectors /= np.maximum(np.linalg.norm(route_vectors, axis=1, keepdims=True), 1e-12)
        route_vectors = route_vectors.astype("<f4")
    else:
        route_vectors = full_route_vectors.astype("<f4")

    route_index = route_data(
        route_vectors,
        model_name=manifest.get("model_name") or EMBEDDING_GEMMA_MODEL,
        model_revision=manifest.get("model_revision"),
    )
    route_index_path = artifact_dir / "query-route-index.json"
    route_vectors_path = artifact_dir / "query-route-embeddings.f32"
    _atomic_write_json(route_index_path, route_index)
    _atomic_write_bytes(route_vectors_path, route_vectors.tobytes(order="C"))

    entries = _file_entries(manifest)
    for path in (route_index_path, route_vectors_path):
        entries[path.name] = {"sha256": sha256(path), "bytes": path.stat().st_size}
    manifest["query_route_count"] = len(route_index["routes"])
    manifest["query_routes"] = [
        {"query_text": route["label"], "route_id": route["id"]}
        for route in route_index["routes"]
    ]
    manifest["route_count"] = len(route_index["routes"])
    manifest["analysis_version"] = route_index["analysis_version"]
    _set_file_entries(manifest, entries)
    _atomic_write_json(manifest_path, manifest)

    return {
        "artifact_dir": str(artifact_dir),
        "dimension": dimension,
        "route_count": len(route_index["routes"]),
        "route_ids": [route["id"] for route in route_index["routes"]],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, action="append", required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), default=None)
    args = parser.parse_args()
    for artifact_dir in args.artifact_dir:
        print(json.dumps(refresh(artifact_dir, device=args.device), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
