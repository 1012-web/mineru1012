#!/usr/bin/env python3
"""Run deterministic mutual deduplication inside one local catalog task."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ALGORITHM_VERSION = "local-1.0.0"
CONTENT_KEYS = (
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
)
SECOND_LEVEL_SUFFIXES = {
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
    "com.hk", "net.hk", "org.hk", "gov.hk", "edu.hk",
    "co.uk", "ac.uk", "org.uk", "gov.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp",
    "co.nz", "ac.nz", "org.nz", "govt.nz",
    "co.kr", "or.kr", "ac.kr", "go.kr",
    "co.in", "org.in", "ac.in", "gov.in",
    "com.sg", "org.sg", "edu.sg", "gov.sg",
    "com.tw", "org.tw", "edu.tw", "gov.tw",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare every candidate with the other candidates in one task."
    )
    parser.add_argument("--input", required=True, help="Path to candidates.jsonl")
    parser.add_argument("--output", required=True, help="Output JSONL path")
    parser.add_argument("--top", type=int, default=20, help="Maximum matches per candidate")
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_no, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {error}") from error
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_no}: each JSONL row must be an object")
            rows.append(value)
    return rows


def text(value: object) -> str:
    source = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(
        char
        for char in source
        if not (char.isspace() or unicodedata.category(char)[0] in {"P", "S"})
    )


def values(value: object) -> list[str]:
    items = value if isinstance(value, list) else [value]
    return sorted({normalized for item in items if (normalized := text(item))})


def normalized_url(value: object) -> str:
    source = str(value or "").strip()
    if not source:
        return ""
    try:
        parts = urlsplit(source if "://" in source else f"https://{source}")
    except ValueError:
        return ""
    if not parts.hostname:
        return ""
    scheme = parts.scheme.lower() or "https"
    host = parts.hostname.lower().rstrip(".")
    port = f":{parts.port}" if parts.port else ""
    path = re.sub(r"/+", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query_pairs = []
    for key, item in parse_qsl(parts.query, keep_blank_values=True):
        if re.match(r"^(utm_|fbclid$|gclid$|mc_|ref$|source$)", key, re.I):
            continue
        query_pairs.append((key, item))
    query_pairs.sort()
    return urlunsplit((scheme, host + port, path, urlencode(query_pairs), ""))


def host(value: object) -> str:
    try:
        return (urlsplit(str(value or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def main_domain(hostname: str) -> str:
    if not hostname:
        return ""
    labels = [item for item in hostname.split(".") if item]
    if len(labels) <= 2:
        return hostname
    last_two = ".".join(labels[-2:])
    if last_two in SECOND_LEVEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return last_two


def payload(row: dict) -> dict:
    raw = row.get("fields")
    return raw if isinstance(raw, dict) else row


def normalize(row: dict) -> dict:
    data = payload(row)
    entrance = normalized_url(data.get("entrance_url"))
    intro = normalized_url(data.get("introduction_url"))
    result = {
        "title": text(data.get("title")),
        "aliases": values(data.get("aliases", [])),
        "entrance_url": entrance,
        "introduction_url": intro,
        "host": host(entrance),
        "organizer": values(data.get("organizer", [])),
        "parent_database": text(data.get("parent_database")),
        "series": values(data.get("series", [])),
        "period": values(data.get("period", [])),
        "content": [],
    }
    result["main_domain"] = main_domain(result["host"])
    content: set[str] = set()
    for key in CONTENT_KEYS:
        content.update(values(data.get(key, [])))
    result["content"] = sorted(content)
    return result


def overlap(left: object, right: object) -> bool:
    return bool(set(left if isinstance(left, list) else [left]) & set(right if isinstance(right, list) else [right]))


def score(left_row: dict, right_row: dict, left: dict, right: dict) -> dict:
    total = 0
    reasons: list[dict] = []
    categories: set[str] = set()
    strong = 0

    def add(condition: bool, weight: int, category: str, label: str, is_strong: bool = False) -> None:
        nonlocal total, strong
        if not condition:
            return
        total += weight
        categories.add(category)
        if is_strong:
            strong += 1
        reasons.append({"item": category, "weight": weight, "label": label})

    add(bool(left["entrance_url"]) and left["entrance_url"] == right["entrance_url"], 70, "entrance_url", "规范化入口 URL 完全相同", True)
    add(bool(left["introduction_url"]) and left["introduction_url"] == right["introduction_url"], 55, "introduction_url", "规范化介绍页 URL 完全相同", True)
    add(bool(left["title"]) and left["title"] == right["title"], 45, "name", "规范化名称完全相同", True)
    left_names = sorted(set([left["title"], *left["aliases"]]) - {""})
    right_names = sorted(set([right["title"], *right["aliases"]]) - {""})
    add(overlap(left_names, right_names) and left["title"] != right["title"], 30, "aliases", "名称或别名交叉匹配")
    add(bool(left["host"]) and left["host"] == right["host"], 25, "host", "入口 host 完全相同")
    add(bool(left["main_domain"]) and left["main_domain"] == right["main_domain"], 15, "domain", "主域相同")
    add(overlap(left["organizer"], right["organizer"]), 18, "organizer", "主办者重合", True)
    add(overlap(left["content"], right["content"]), 12, "content", "内容范围重合")
    add(overlap(left["period"], right["period"]), 8, "period", "时期重合")
    add(bool(left["parent_database"]) and left["parent_database"] == right["parent_database"], 15, "parent", "母库关系相同", True)
    add(overlap(left["series"], right["series"]), 10, "series", "系列重合")

    total = min(100, total)
    verdict = "distinct"
    if total >= 90 and strong >= 2:
        verdict = "duplicate"
    elif total >= 55 and len(categories) >= 2:
        verdict = "suspected_duplicate"

    right_data = payload(right_row)
    return {
        "candidate_uuid": str(right_row.get("candidate_uuid", "")),
        "title": str(right_data.get("title", "")),
        "url": str(right_data.get("entrance_url", "")),
        "score": total,
        "verdict": verdict,
        "strong_signal_count": strong,
        "reasons": reasons,
    }


def main() -> int:
    args = parse_args()
    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    rows = read_jsonl(input_path)
    normalized = [normalize(row) for row in rows]
    uuids = [str(row.get("candidate_uuid", "")) for row in rows]
    if any(not item for item in uuids) or len(set(uuids)) != len(uuids):
        raise ValueError("candidate_uuid must be non-empty and unique")

    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    results: list[dict] = []
    for index, row in enumerate(rows):
        matches = []
        for other_index, other in enumerate(rows):
            if index == other_index:
                continue
            match = score(row, other, normalized[index], normalized[other_index])
            if match["score"] > 0:
                matches.append(match)
        matches.sort(key=lambda item: (-item["score"], item["title"], item["candidate_uuid"]))
        matches = matches[: max(1, args.top)]
        local_verdict = "distinct"
        if matches and matches[0]["verdict"] == "duplicate":
            local_verdict = "duplicate"
        elif matches and matches[0]["verdict"] == "suspected_duplicate":
            local_verdict = "suspected_duplicate"
        results.append(
            {
                "candidate_uuid": row["candidate_uuid"],
                "algorithm_version": ALGORITHM_VERSION,
                "local_verdict": local_verdict,
                "score": matches[0]["score"] if matches else 0,
                "matches": matches,
                "checked_at": checked_at,
            }
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(json.dumps({"ok": True, "candidates": len(results), "output": str(output_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
