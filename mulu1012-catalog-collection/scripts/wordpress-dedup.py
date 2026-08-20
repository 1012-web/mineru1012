#!/usr/bin/env python3
"""Run the formal WordPress dedup Ability for one local task."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from wp_ability import (
    AbilityCallError,
    add_transport_arguments,
    invoke_ability,
    rest_endpoint,
    transport_from_args,
)

DEDUP_FIELD_KEYS = {
    "title",
    "aliases",
    "entrance_url",
    "introduction_url",
    "organizer",
    "primary_category",
    "featured_sources",
    "database_type",
    "research_topic",
    "geo_scope",
    "special_history",
    "digital_format",
    "material_type",
    "material_language",
    "coverage_years",
    "period",
    "parent_database",
    "series",
    "wordpress_post_id",
}


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_no, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as error:
                raise AbilityCallError(f"{path.name}:{line_no}: invalid JSON") from error
            if not isinstance(value, dict):
                raise AbilityCallError(f"{path.name}:{line_no}: row must be an object")
            rows.append(value)
    return rows


def write_jsonl_atomic(path: Path, rows: list[dict]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deduplicate one local task against WordPress.")
    parser.add_argument("--batch-dir", required=True, help="Local batch directory.")
    parser.add_argument("--input", help="Candidates JSONL. Defaults to candidates.jsonl.")
    parser.add_argument("--output", help="Output JSONL. Defaults to formal-dedup-results.jsonl.")
    parser.add_argument("--chunk-size", type=int, default=500)
    parser.add_argument(
        "--max-rest-url-length",
        type=int,
        default=7000,
        help="Maximum encoded GET URL for read-only REST calls.",
    )
    parser.add_argument("--revision-retries", type=int, default=2)
    add_transport_arguments(parser)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.chunk_size <= 500:
        raise AbilityCallError("--chunk-size must be between 1 and 500.")
    if not 0 <= args.revision_retries <= 5:
        raise AbilityCallError("--revision-retries must be between 0 and 5.")
    if not 2048 <= args.max_rest_url_length <= 32768:
        raise AbilityCallError("--max-rest-url-length must be between 2048 and 32768.")

    batch_dir = Path(args.batch_dir).resolve()
    input_path = Path(args.input).resolve() if args.input else batch_dir / "candidates.jsonl"
    output_path = Path(args.output).resolve() if args.output else batch_dir / "formal-dedup-results.jsonl"
    candidates = read_jsonl(input_path)
    if not candidates:
        raise AbilityCallError("candidates.jsonl contains no candidates.")

    seen: set[str] = set()
    ability_rows: list[dict] = []
    for index, candidate in enumerate(candidates, 1):
        candidate_uuid = str(candidate.get("candidate_uuid", ""))
        fields = candidate.get("fields")
        if not candidate_uuid or candidate_uuid in seen:
            raise AbilityCallError(f"Candidate {index} has a missing or duplicate UUID.")
        if not isinstance(fields, dict):
            raise AbilityCallError(f"Candidate {candidate_uuid} fields must be an object.")
        dedup_fields = {
            key: value
            for key, value in fields.items()
            if key in DEDUP_FIELD_KEYS and value not in ("", None, [], {})
        }
        if not dedup_fields:
            raise AbilityCallError(
                f"Candidate {candidate_uuid} has no usable deduplication fields."
            )
        seen.add(candidate_uuid)
        ability_rows.append({"candidate_uuid": candidate_uuid, "fields": dedup_fields})

    config = transport_from_args(args)
    attempts = args.revision_retries + 1
    final_rows: list[dict] = []
    final_revision = ""
    algorithm_version = ""

    for attempt in range(1, attempts + 1):
        revisions: set[str] = set()
        algorithms: set[str] = set()
        collected: dict[str, dict] = {}
        chunks = build_chunks(
            ability_rows,
            args.chunk_size,
            args.max_rest_url_length,
            config.transport,
            config.site_url,
        )
        for chunk_index, chunk in enumerate(chunks, 1):
            print(
                f"正式库查重：分片 {chunk_index}/{len(chunks)}，候选 {len(chunk)} 条。",
                file=sys.stderr,
            )
            result = invoke_ability(
                "mulu1012-catalog/deduplicate-candidates",
                {"candidates": chunk},
                config,
            )
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                raise AbilityCallError("Dedup Ability returned an invalid result.", details=result)
            revision = str(result.get("formal_revision", ""))
            algorithm = str(result.get("algorithm_version", ""))
            if not revision or not algorithm:
                raise AbilityCallError("Dedup Ability omitted revision or algorithm.", details=result)
            revisions.add(revision)
            algorithms.add(algorithm)
            for item in result["items"]:
                candidate_uuid = str(item.get("candidate_uuid", ""))
                if candidate_uuid in collected or candidate_uuid not in seen:
                    raise AbilityCallError(
                        "Dedup Ability returned an unknown or repeated candidate.",
                        details=item,
                    )
                collected[candidate_uuid] = item

        if len(collected) != len(ability_rows):
            raise AbilityCallError(
                "Dedup Ability did not return every candidate.",
                details={"expected": len(ability_rows), "actual": len(collected)},
            )
        if len(revisions) == 1 and len(algorithms) == 1:
            final_revision = next(iter(revisions))
            algorithm_version = next(iter(algorithms))
            checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            final_rows = [
                {
                    "candidate_uuid": row["candidate_uuid"],
                    "formal_revision": final_revision,
                    "algorithm_version": algorithm_version,
                    "formal_verdict": collected[row["candidate_uuid"]].get("verdict", ""),
                    "score": collected[row["candidate_uuid"]].get("score", 0),
                    "matches": collected[row["candidate_uuid"]].get("matches", []),
                    "checked_at": checked_at,
                }
                for row in ability_rows
            ]
            break
        if attempt < attempts:
            print(
                "正式库修订号在分片间发生变化，正在重新执行全批查重。",
                file=sys.stderr,
            )

    if not final_rows:
        raise AbilityCallError(
            "Formal revision kept changing; no mixed-revision output was written."
        )

    write_jsonl_atomic(output_path, final_rows)
    counts = Counter(str(row["formal_verdict"]) for row in final_rows)
    print(
        json.dumps(
            {
                "ok": True,
                "candidate_count": len(final_rows),
                "formal_revision": final_revision,
                "algorithm_version": algorithm_version,
                "verdict_counts": dict(sorted(counts.items())),
                "output": str(output_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_chunks(
    rows: list[dict],
    chunk_size: int,
    max_rest_url_length: int,
    transport: str,
    site_url: str,
) -> list[list[dict]]:
    if transport != "rest":
        return [
            rows[index : index + chunk_size]
            for index in range(0, len(rows), chunk_size)
        ]

    chunks: list[list[dict]] = []
    current: list[dict] = []
    for row in rows:
        trial = current + [row]
        trial_url = rest_endpoint(
            "mulu1012-catalog/deduplicate-candidates",
            {"candidates": trial},
            site_url,
        )
        if current and (
            len(trial) > chunk_size
            or len(trial_url.encode("ascii")) > max_rest_url_length
        ):
            chunks.append(current)
            current = [row]
            trial_url = rest_endpoint(
                "mulu1012-catalog/deduplicate-candidates",
                {"candidates": current},
                site_url,
            )
        else:
            current = trial
        if len(trial_url.encode("ascii")) > max_rest_url_length:
            raise AbilityCallError(
                "One candidate exceeds the configured REST URL limit. "
                "Use Studio transport or raise --max-rest-url-length."
            )
    if current:
        chunks.append(current)
    return chunks


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AbilityCallError as error:
        print(
            json.dumps(
                {"ok": False, "error": str(error), "details": error.details},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)
