#!/usr/bin/env python3
"""Upload and read back one reviewed local task through WordPress Abilities."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wp_ability import (
    AbilityCallError,
    add_transport_arguments,
    invoke_ability,
    transport_from_args,
    write_json_atomic,
)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise AbilityCallError(f"Unable to read JSON: {path}", details=str(error)) from error
    if not isinstance(value, dict):
        raise AbilityCallError(f"{path.name} must contain one object.")
    return value


def read_jsonl_text(path: Path) -> tuple[str, list[dict]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise AbilityCallError(f"Unable to read JSONL: {path}", details=str(error)) from error
    rows: list[dict] = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise AbilityCallError(f"{path.name}:{line_no}: invalid JSON") from error
        if not isinstance(value, dict):
            raise AbilityCallError(f"{path.name}:{line_no}: row must be an object")
        rows.append(value)
    normalized = "\n".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows
    ) + "\n"
    return normalized, rows


def chunk_hash(rows: list[dict]) -> str:
    value = json.dumps(
        rows,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload one reviewed local catalog batch.")
    parser.add_argument("--batch-dir", required=True)
    parser.add_argument("--chunk-size", type=int, default=100)
    parser.add_argument(
        "--readback",
        choices=("full", "batch", "none"),
        default="full",
        help="Read back the batch and optionally every candidate.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--confirm-upload",
        action="store_true",
        help="Required for writes. Pass only after explicit user confirmation.",
    )
    add_transport_arguments(parser)
    return parser.parse_args()


def validate_package(batch_dir: Path, chunk_size: int) -> tuple[dict, dict, list[dict], str]:
    if not 1 <= chunk_size <= 500:
        raise AbilityCallError("--chunk-size must be between 1 and 500.")
    batch = read_json(batch_dir / "batch.json")
    manifest = read_json(batch_dir / "upload-manifest.json")
    upload_text, candidates = read_jsonl_text(batch_dir / "wordpress-upload.jsonl")
    if not candidates:
        raise AbilityCallError("wordpress-upload.jsonl contains no candidates.")

    actual_hash = hashlib.sha256(upload_text.encode("utf-8")).hexdigest()
    if actual_hash != str(manifest.get("upload_sha256", "")):
        raise AbilityCallError(
            "wordpress-upload.jsonl does not match upload-manifest.json.",
            details={"expected": manifest.get("upload_sha256"), "actual": actual_hash},
        )
    if int(manifest.get("candidate_count") or 0) != len(candidates):
        raise AbilityCallError("Manifest candidate_count does not match upload JSONL.")
    if manifest.get("batch_uuid") != batch.get("batch_uuid"):
        raise AbilityCallError("Batch UUID differs between batch.json and manifest.")
    if manifest.get("idempotency_key") != batch.get("idempotency_key"):
        raise AbilityCallError("Batch idempotency key differs between batch.json and manifest.")
    if isinstance(manifest.get("formal_revision"), list):
        raise AbilityCallError("Upload package contains mixed formal revisions.")

    uuids = [str(row.get("candidate_uuid", "")) for row in candidates]
    if any(not value for value in uuids) or len(uuids) != len(set(uuids)):
        raise AbilityCallError("Candidate UUIDs must be non-empty and unique.")
    return batch, manifest, candidates, actual_hash


def create_or_resume_receipt(
    path: Path,
    batch: dict,
    manifest: dict,
    upload_hash: str,
    chunk_size: int,
    chunk_count: int,
    transport: str,
) -> dict:
    if path.is_file():
        receipt = read_json(path)
        expected = {
            "batch_uuid": batch["batch_uuid"],
            "upload_sha256": upload_hash,
            "chunk_size": chunk_size,
            "chunk_count": chunk_count,
        }
        for key, value in expected.items():
            if receipt.get(key) != value:
                raise AbilityCallError(
                    "Existing upload receipt belongs to a different upload plan.",
                    details={"field": key, "expected": value, "actual": receipt.get(key)},
                )
        return receipt
    return {
        "schema_version": "1.0",
        "batch_uuid": batch["batch_uuid"],
        "idempotency_key": batch["idempotency_key"],
        "upload_sha256": upload_hash,
        "formal_revision": manifest["formal_revision"],
        "transport": transport,
        "chunk_size": chunk_size,
        "chunk_count": chunk_count,
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "complete": False,
        "chunks": {},
        "readback": {},
    }


def upload_batch(args: argparse.Namespace) -> dict:
    batch_dir = Path(args.batch_dir).resolve()
    batch, manifest, candidates, upload_hash = validate_package(batch_dir, args.chunk_size)
    chunks = [
        candidates[index : index + args.chunk_size]
        for index in range(0, len(candidates), args.chunk_size)
    ]

    plan = {
        "ok": True,
        "dry_run": bool(args.dry_run),
        "batch_dir": str(batch_dir),
        "batch_uuid": batch["batch_uuid"],
        "candidate_count": len(candidates),
        "chunk_size": args.chunk_size,
        "chunk_count": len(chunks),
        "formal_revision": manifest["formal_revision"],
        "upload_sha256": upload_hash,
    }
    if args.dry_run:
        return plan
    if not args.confirm_upload:
        raise AbilityCallError(
            "Upload is blocked. Pass --confirm-upload only after the user explicitly confirms."
        )

    config = transport_from_args(args)
    receipt_path = batch_dir / "upload-receipt.json"
    receipt = create_or_resume_receipt(
        receipt_path,
        batch,
        manifest,
        upload_hash,
        args.chunk_size,
        len(chunks),
        config.transport,
    )
    batch_input = {
        "batch_uuid": batch["batch_uuid"],
        "name": batch["name"],
        "idempotency_key": batch["idempotency_key"],
        "formal_revision": manifest["formal_revision"],
        "contract_version": batch["contract_version"],
        "contract_hash": batch["contract_hash"],
        "source_ref": "upload-manifest.json",
        "research_brief": batch["research_brief"],
    }

    for index, chunk in enumerate(chunks, 1):
        key = str(index)
        digest = chunk_hash(chunk)
        previous = receipt["chunks"].get(key)
        if previous and previous.get("status") == "complete":
            if previous.get("hash") != digest:
                raise AbilityCallError(f"Completed chunk {index} changed locally.")
            print(f"上传分片 {index}/{len(chunks)}：已完成，跳过。", file=sys.stderr)
            continue

        print(f"上传分片 {index}/{len(chunks)}：{len(chunk)} 条。", file=sys.stderr)
        result = invoke_ability(
            "mulu1012-catalog/import-local-batch",
            {
                "batch": batch_input,
                "chunk_index": index,
                "chunk_count": len(chunks),
                "final_chunk": index == len(chunks),
                "candidates": chunk,
            },
            config,
        )
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise AbilityCallError("Import Ability returned an invalid result.", details=result)
        failed = int(result.get("failed_count") or 0)
        receipt["chunks"][key] = {
            "hash": digest,
            "candidate_count": len(chunk),
            "status": "complete" if failed == 0 else "failed",
            "created_count": int(result.get("created_count") or 0),
            "reused_count": int(result.get("reused_count") or 0),
            "failed_count": failed,
            "rechecked_count": int(result.get("rechecked_count") or 0),
            "items": result["items"],
            "updated_at": utc_now(),
        }
        receipt["updated_at"] = utc_now()
        write_json_atomic(receipt_path, receipt)

    totals = {
        "created_count": 0,
        "reused_count": 0,
        "failed_count": 0,
        "rechecked_count": 0,
    }
    for chunk in receipt["chunks"].values():
        for key in totals:
            totals[key] += int(chunk.get(key) or 0)

    batch_readback: dict[str, Any] = {}
    candidate_mismatches: list[dict] = []
    if args.readback != "none":
        batch_readback = invoke_ability(
            "mulu1012-catalog/get-batch",
            {"batch_uuid": batch["batch_uuid"]},
            config,
        )
        state_counts = batch_readback.get("state_counts", []) if isinstance(batch_readback, dict) else []
        if isinstance(state_counts, list):
            readback_count = sum(
                int(row.get("count") or 0)
                for row in state_counts
                if isinstance(row, dict)
            )
        elif isinstance(state_counts, dict):
            readback_count = sum(int(value or 0) for value in state_counts.values())
        else:
            readback_count = 0
        if readback_count != len(candidates):
            candidate_mismatches.append(
                {
                    "type": "batch_count",
                    "expected": len(candidates),
                    "actual": readback_count,
                }
            )

    if args.readback == "full" and totals["failed_count"] == 0:
        for index, expected in enumerate(candidates, 1):
            if index == 1 or index % 20 == 0 or index == len(candidates):
                print(f"回读候选 {index}/{len(candidates)}。", file=sys.stderr)
            detail = invoke_ability(
                "mulu1012-catalog/get-candidate",
                {"candidate_uuid": expected["candidate_uuid"]},
                config,
            )
            candidate = detail.get("candidate", {}) if isinstance(detail, dict) else {}
            version = detail.get("version", {}) if isinstance(detail, dict) else {}
            if candidate.get("uuid") != expected["candidate_uuid"]:
                candidate_mismatches.append(
                    {
                        "candidate_uuid": expected["candidate_uuid"],
                        "type": "candidate_uuid",
                        "actual": candidate.get("uuid"),
                    }
                )
                continue
            if version.get("uuid") != expected.get("version_uuid"):
                candidate_mismatches.append(
                    {
                        "candidate_uuid": expected["candidate_uuid"],
                        "type": "version_uuid",
                        "expected": expected.get("version_uuid"),
                        "actual": version.get("uuid"),
                    }
                )
            if version.get("fields") != expected.get("fields"):
                candidate_mismatches.append(
                    {
                        "candidate_uuid": expected["candidate_uuid"],
                        "type": "fields",
                    }
                )
            expected_evidence = len(expected.get("evidence") or [])
            actual_evidence = len(detail.get("evidence") or [])
            if expected_evidence != actual_evidence:
                candidate_mismatches.append(
                    {
                        "candidate_uuid": expected["candidate_uuid"],
                        "type": "evidence_count",
                        "expected": expected_evidence,
                        "actual": actual_evidence,
                    }
                )

    receipt["readback"] = {
        "mode": args.readback,
        "batch": batch_readback,
        "mismatches": candidate_mismatches,
        "checked_at": utc_now(),
    }
    receipt["totals"] = totals
    receipt["complete"] = (
        len(receipt["chunks"]) == len(chunks)
        and totals["failed_count"] == 0
        and not candidate_mismatches
    )
    receipt["updated_at"] = utc_now()
    write_json_atomic(receipt_path, receipt)
    update_summary(batch_dir / "任务总结.md", receipt)

    plan.update(
        {
            "dry_run": False,
            "receipt": str(receipt_path),
            "totals": totals,
            "readback_mismatches": len(candidate_mismatches),
            "complete": receipt["complete"],
        }
    )
    if totals["failed_count"] or candidate_mismatches:
        raise AbilityCallError("Upload finished with failures or readback mismatches.", details=plan)
    return plan


def update_summary(path: Path, receipt: dict) -> None:
    start_marker = f"<!-- mulu1012-upload:{receipt['batch_uuid']}:start -->"
    end_marker = f"<!-- mulu1012-upload:{receipt['batch_uuid']}:end -->"
    legacy_marker = f"<!-- mulu1012-upload:{receipt['batch_uuid']} -->"
    existing = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    totals = receipt.get("totals", {})
    batch = receipt.get("readback", {}).get("batch", {})
    section = [
        start_marker,
        "## WordPress 上传与回读",
        "",
        f"- **批次 UUID：** `{receipt['batch_uuid']}`",
        f"- **上传状态：** {'完成' if receipt.get('complete') else '未完成'}",
        f"- **上传完成时间：** {receipt.get('updated_at', '')}",
        f"- **创建候选：** {totals.get('created_count', 0)}",
        f"- **复用候选：** {totals.get('reused_count', 0)}",
        f"- **失败候选：** {totals.get('failed_count', 0)}",
        f"- **重新查重：** {totals.get('rechecked_count', 0)}",
        f"- **工作台状态：** {batch.get('status', '') if isinstance(batch, dict) else ''}",
        f"- **回读差异：** {len(receipt.get('readback', {}).get('mismatches', []))}",
        end_marker,
    ]
    replacement = "\n".join(section)

    start = existing.find(start_marker)
    end = existing.find(end_marker)
    if start >= 0 and end >= start:
        end += len(end_marker)
        updated = existing[:start].rstrip() + "\n\n" + replacement + existing[end:]
    elif legacy_marker in existing:
        legacy_start = existing.find(legacy_marker)
        updated = existing[:legacy_start].rstrip() + "\n\n" + replacement + "\n"
    else:
        updated = existing.rstrip() + "\n\n" + replacement + "\n"
    path.write_text(updated, encoding="utf-8")


def main() -> int:
    args = parse_args()
    if args.dry_run and args.confirm_upload:
        raise AbilityCallError("Use either --dry-run or --confirm-upload, not both.")
    result = upload_batch(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


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
