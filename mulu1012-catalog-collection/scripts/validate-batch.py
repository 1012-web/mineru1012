#!/usr/bin/env python3
"""Validate a local catalog batch before building or uploading its review package."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path

FIELD_KEYS = (
    "title", "aliases", "wordpress_post_id", "site_type", "primary_category",
    "featured_sources", "database_type", "organizer", "series", "parent_database",
    "index_source", "introduction_url", "entrance_url", "access_type", "summary",
    "summary_source", "body_introduction", "body_source", "official_introduction",
    "official_source", "research_topic", "geo_scope", "special_history",
    "digital_format", "material_type", "material_language", "period",
    "coverage_years", "project", "site_language", "launch_date", "rating",
    "authorized_institutions", "authorized_libraries", "library_names", "ai_score",
    "ai_comment",
)
FINAL_VERDICTS = {"duplicate", "suspected_duplicate", "fillable", "blocked"}
CLASSIFICATION_CONTRACT = "2026-07-14.3"
MIN_STRICT_CLASSIFICATION_CONTRACT = "2026-07-14.2"
WORDPRESS_FIELD_CONTRACTS = {
    "2026-07-12.1": "0ca4bf5a608dcfdad7a7b25895cc70390963d63684701a3e1cf26fb6795bfe8c",
}
LEGACY_CLASSIFICATION_CONTRACTS = {
    "2026-07-14.1": "1b9045fc4aba03734cf56028e4ef63af4dbfa9312672b505e9fd41e7510afc09",
    "2026-07-14.2": "f29bc80465299a055416ebb6644de2966b9bb34d25fc3cbc82dc08b6fa3b29be",
}
SITE_TYPES = {"网站", "数据库", "母库", "子库", "独立库"}
DATABASE_TYPES = {"综合库", "专题库", "知识库"}
INVALID_PRIMARY_CATEGORIES = {"数据库", "资源导航"}
GENERIC_FEATURED_SOURCES = {
    "古籍", "档案", "期刊", "报刊", "照片", "图像", "人物传记", "历史资料", "数据库",
}
REQUIRED_FILES = (
    "batch.json",
    "search-records.jsonl",
    "candidates.jsonl",
    "dedup-results.jsonl",
    "field-fill.jsonl",
    "evidence.jsonl",
    "ai-review.jsonl",
)


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def current_classification_hash() -> str:
    path = skill_root() / "references" / "field-rules.md"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_site_catalog_vocabulary() -> dict[str, set[str] | dict[str, str]]:
    path = skill_root() / "references" / "site-catalog-vocabulary.md"
    text = path.read_text(encoding="utf-8-sig")

    def section(title: str) -> str:
        pattern = rf"^## {re.escape(title)}\s*$([\s\S]*?)(?=^## |\Z)"
        match = re.search(pattern, text, flags=re.MULTILINE)
        if not match:
            raise ValueError(f"{path.name}: missing section ## {title}")
        return match.group(1)

    def terms(title: str) -> set[str]:
        values: set[str] = set()
        for raw in section(title).splitlines():
            match = re.match(r"^\s*-\s+`?(.+?)`?\s*$", raw)
            if match:
                values.add(match.group(1).strip().strip("`"))
        return values

    def aliases(title: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for raw in section(title).splitlines():
            match = re.match(r"^\s*-\s+`?(.+?)`?\s*(?:->|→)\s*`?(.+?)`?\s*$", raw)
            if match:
                values[match.group(1).strip().strip("`")] = match.group(2).strip().strip("`")
        return values

    def catalog_terms() -> tuple[set[str], set[str]]:
        primary: set[str] = set()
        featured: set[str] = set()
        for raw in section("site_catalog 层级词表").splitlines():
            match = re.match(r"^\s*-\s+`([^`]+)`\s*（([^）]+)）", raw)
            if not match:
                continue
            term = match.group(1).strip()
            role_note = match.group(2)
            if "特色史料" in role_note:
                featured.add(term)
            elif "主分类" in role_note:
                primary.add(term)
        return primary, featured

    primary_terms, featured_terms = catalog_terms()
    return {
        "primary": primary_terms,
        "featured": featured_terms,
        "discouraged_primary": terms("不推荐主分类"),
        "database_types": terms("数据库类型"),
        "aliases": aliases("旧词映射"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a mulu1012 local catalog batch.")
    parser.add_argument("--batch-dir", required=True, help="Batch directory")
    parser.add_argument("--report", help="Optional JSON report path")
    return parser.parse_args()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain one JSON object")
    return value


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
                raise ValueError(f"{path.name}:{line_no}: invalid JSON: {error}") from error
            if not isinstance(value, dict):
                raise ValueError(f"{path.name}:{line_no}: each row must be an object")
            value["_line"] = line_no
            rows.append(value)
    return rows


def is_uuid(value: object) -> bool:
    try:
        return str(uuid.UUID(str(value))) == str(value).lower()
    except (ValueError, AttributeError, TypeError):
        return False


def is_blank(value: object) -> bool:
    return value is None or value == "" or value == []


def version_key(value: object) -> tuple[str, int]:
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.(\d+)", str(value).strip())
    if not match:
        return ("", -1)
    return (match.group(1), int(match.group(2)))


def taxonomy_values(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    return [
        item.strip()
        for item in re.split(r"[，,；;]", str(value))
        if item.strip()
    ]


def has_nonstandard_separator(value: object) -> bool:
    return isinstance(value, str) and ("," in value or ";" in value)


def proposed_terms_for(row: dict, field_key: str) -> set[str]:
    values: set[str] = set()
    for term in row.get("proposed_terms", []):
        if not isinstance(term, dict):
            continue
        if term.get("field_key") != field_key:
            continue
        term_name = str(term.get("term_name", "")).strip()
        if term_name:
            values.add(term_name)
    return values


def proposed_database_terms(row: dict) -> list[str]:
    result: list[str] = []
    for term in row.get("proposed_terms", []):
        if isinstance(term, dict) and term.get("field_key") == "database_type":
            term_name = str(term.get("term_name", "")).strip()
            if term_name:
                result.append(term_name)
    return result


def expected_classification_hash(version: str) -> str | None:
    if version == CLASSIFICATION_CONTRACT:
        return current_classification_hash()
    return LEGACY_CLASSIFICATION_CONTRACTS.get(version)


def index_unique(rows: list[dict], key: str, filename: str, errors: list[str]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for row in rows:
        value = str(row.get(key, ""))
        if not value:
            errors.append(f"{filename}:{row['_line']}: missing {key}")
            continue
        if value in result:
            errors.append(f"{filename}:{row['_line']}: duplicate {key} {value}")
            continue
        result[value] = row
    return result


def main() -> int:
    args = parse_args()
    batch_dir = Path(args.batch_dir).resolve()
    errors: list[str] = []
    warnings: list[str] = []
    vocabulary = load_site_catalog_vocabulary()
    primary_terms = vocabulary["primary"]
    featured_terms = vocabulary["featured"]
    discouraged_primary_terms = vocabulary["discouraged_primary"]
    database_type_terms = vocabulary["database_types"] or DATABASE_TYPES
    legacy_aliases = vocabulary["aliases"]

    if not batch_dir.is_dir():
        raise ValueError(f"batch directory does not exist: {batch_dir}")
    for filename in REQUIRED_FILES:
        if not (batch_dir / filename).is_file():
            errors.append(f"missing required file: {filename}")
    if errors:
        return finish(batch_dir, args.report, errors, warnings, {})

    batch = read_json(batch_dir / "batch.json")
    tables = {
        name: read_jsonl(batch_dir / name)
        for name in REQUIRED_FILES
        if name.endswith(".jsonl")
    }

    if not is_uuid(batch.get("batch_uuid")):
        errors.append("batch.json: batch_uuid must be a canonical UUID")
    batch_idempotency_key = str(batch.get("idempotency_key", "")).strip()
    if not batch_idempotency_key:
        errors.append("batch.json: idempotency_key is required")
    elif len(batch_idempotency_key) > 191:
        errors.append("batch.json: idempotency_key must be at most 191 characters")
    brief = batch.get("research_brief")
    if not isinstance(brief, dict):
        errors.append("batch.json: research_brief must be an object")
        brief = {}
    target_count = int(brief.get("target_count") or 0)
    if target_count < 1:
        errors.append("batch.json: research_brief.target_count must be at least 1")
    contract_version = str(batch.get("contract_version", "")).strip()
    contract_hash = str(batch.get("contract_hash", "")).strip().lower()
    if not contract_version:
        errors.append("batch.json: contract_version is required")
    if not re.fullmatch(r"[a-f0-9]{64}", contract_hash):
        errors.append("batch.json: contract_hash must be a lower-case SHA-256")
    elif contract_version in WORDPRESS_FIELD_CONTRACTS:
        expected = WORDPRESS_FIELD_CONTRACTS[contract_version]
        if contract_hash != expected:
            errors.append(
                f"batch.json: contract_hash does not match WordPress field contract "
                f"{contract_version}; expected {expected}"
            )
    elif expected_classification_hash(contract_version):
        expected = expected_classification_hash(contract_version)
        if contract_hash != expected:
            errors.append(
                f"batch.json: legacy classification contract_hash does not match "
                f"{contract_version}; expected {expected}"
            )
        warnings.append(
            "batch.json: contract_version uses a local classification contract; "
            "WordPress upload requires the WordPress field contract values"
        )
    elif contract_version:
        errors.append(f"batch.json: unknown contract_version {contract_version!r}")

    classification_version = str(
        batch.get("classification_contract_version")
        or (contract_version if contract_version.startswith("2026-07-14.") else "")
    ).strip()
    classification_hash = str(batch.get("classification_contract_hash") or "").strip().lower()
    if classification_version:
        expected = expected_classification_hash(classification_version)
        if not expected:
            errors.append(
                f"batch.json: unknown classification_contract_version {classification_version!r}"
            )
        elif classification_hash and classification_hash != expected:
            errors.append(
                f"batch.json: classification_contract_hash does not match "
                f"{classification_version}; expected {expected}"
            )
        elif not classification_hash and not contract_version.startswith("2026-07-14."):
            errors.append("batch.json: classification_contract_hash is required")
    else:
        warnings.append(
            "batch.json: classification_contract_version is missing; strict classification "
            "checks are disabled for legacy compatibility"
        )
    strict_classification = (
        version_key(classification_version) >= version_key(MIN_STRICT_CLASSIFICATION_CONTRACT)
    )

    candidates = index_unique(tables["candidates.jsonl"], "candidate_uuid", "candidates.jsonl", errors)
    dedup = index_unique(tables["dedup-results.jsonl"], "candidate_uuid", "dedup-results.jsonl", errors)
    fills = index_unique(tables["field-fill.jsonl"], "candidate_uuid", "field-fill.jsonl", errors)
    reviews = index_unique(tables["ai-review.jsonl"], "candidate_uuid", "ai-review.jsonl", errors)

    for candidate_uuid, row in candidates.items():
        if not is_uuid(candidate_uuid):
            errors.append(f"candidates.jsonl:{row['_line']}: invalid candidate_uuid")
        if not is_uuid(row.get("version_uuid")):
            errors.append(f"candidates.jsonl:{row['_line']}: invalid version_uuid")
        if not str(row.get("idempotency_key", "")).strip():
            errors.append(f"candidates.jsonl:{row['_line']}: idempotency_key is required")
        partial = row.get("fields", {})
        if not isinstance(partial, dict):
            errors.append(f"candidates.jsonl:{row['_line']}: fields must be an object")
        else:
            unknown = sorted(set(partial) - set(FIELD_KEYS))
            if unknown:
                errors.append(f"candidates.jsonl:{row['_line']}: unknown fields {unknown}")
        if candidate_uuid not in dedup:
            errors.append(f"candidate {candidate_uuid}: missing dedup result")

    for candidate_uuid, row in dedup.items():
        if candidate_uuid not in candidates:
            errors.append(f"dedup-results.jsonl:{row['_line']}: unknown candidate {candidate_uuid}")
        verdict = str(row.get("final_verdict", ""))
        if verdict not in FINAL_VERDICTS:
            errors.append(f"dedup-results.jsonl:{row['_line']}: invalid final_verdict {verdict!r}")
        score = row.get("score", 0)
        if not isinstance(score, (int, float)) or not 0 <= score <= 100:
            errors.append(f"dedup-results.jsonl:{row['_line']}: score must be 0-100")
        if not str(row.get("formal_revision", "")).strip():
            errors.append(f"dedup-results.jsonl:{row['_line']}: formal_revision is required")
        if not str(row.get("algorithm_version", "")).strip():
            errors.append(f"dedup-results.jsonl:{row['_line']}: algorithm_version is required")

    for candidate_uuid, row in fills.items():
        if candidate_uuid not in candidates:
            errors.append(f"field-fill.jsonl:{row['_line']}: unknown candidate {candidate_uuid}")
            continue
        if str(row.get("version_uuid", "")).lower() != str(candidates[candidate_uuid].get("version_uuid", "")).lower():
            errors.append(f"field-fill.jsonl:{row['_line']}: version_uuid does not match candidates.jsonl")
        fields = row.get("fields")
        if not isinstance(fields, dict):
            errors.append(f"field-fill.jsonl:{row['_line']}: fields must be an object")
            continue
        if tuple(fields.keys()) != FIELD_KEYS:
            errors.append(f"field-fill.jsonl:{row['_line']}: fields must contain the exact 37 keys in contract order")
        field_reviews = row.get("field_reviews")
        if not isinstance(field_reviews, list):
            errors.append(f"field-fill.jsonl:{row['_line']}: field_reviews must be an array")
            continue
        by_key = {str(item.get("field_key", "")): item for item in field_reviews if isinstance(item, dict)}
        if set(by_key) != set(FIELD_KEYS):
            missing = sorted(set(FIELD_KEYS) - set(by_key))
            extra = sorted(set(by_key) - set(FIELD_KEYS))
            errors.append(f"field-fill.jsonl:{row['_line']}: field_reviews mismatch missing={missing} extra={extra}")
        for key in FIELD_KEYS:
            item = by_key.get(key)
            if not item:
                continue
            value = fields.get(key)
            if is_blank(value):
                if not str(item.get("empty_reason", "")).strip():
                    errors.append(f"field-fill.jsonl:{row['_line']}: blank field {key} needs empty_reason")
            else:
                confidence = item.get("confidence")
                if not isinstance(confidence, int) or not 0 <= confidence <= 100:
                    errors.append(f"field-fill.jsonl:{row['_line']}: field {key} confidence must be 0-100")
                for required in ("evidence_excerpt", "source_name", "source_url", "method", "judgment"):
                    if not str(item.get(required, "")).strip():
                        errors.append(f"field-fill.jsonl:{row['_line']}: field {key} needs {required}")
                if item.get("method") not in {"direct", "inference"}:
                    errors.append(f"field-fill.jsonl:{row['_line']}: field {key} method must be direct or inference")
        if fields.get("rating") not in (None, "", []):
            errors.append(f"field-fill.jsonl:{row['_line']}: rating must remain blank for AI collection")
        if fields.get("wordpress_post_id") not in (None, "", 0):
            errors.append(f"field-fill.jsonl:{row['_line']}: wordpress_post_id must be blank before publication")
        parent = fields.get("parent_database")
        if not is_blank(parent) and not is_uuid(parent):
            errors.append(f"field-fill.jsonl:{row['_line']}: parent_database must be a formal UUID or blank")

        if strict_classification:
            raw_site_type = fields.get("site_type")
            site_type = raw_site_type.strip() if isinstance(raw_site_type, str) else ""
            if site_type not in SITE_TYPES:
                errors.append(
                    f"field-fill.jsonl:{row['_line']}: site_type must be one of "
                    f"{sorted(SITE_TYPES)} for contract {CLASSIFICATION_CONTRACT}+"
                )

            primary_values = taxonomy_values(fields.get("primary_category"))
            if has_nonstandard_separator(fields.get("primary_category")):
                warnings.append(
                    f"field-fill.jsonl:{row['_line']}: primary_category should use Chinese comma ， "
                    "between values"
                )
            if not primary_values:
                errors.append(
                    f"field-fill.jsonl:{row['_line']}: primary_category is required; "
                    "use 待分类 when no existing term fits"
                )
            invalid_primary = sorted(set(primary_values) & INVALID_PRIMARY_CATEGORIES)
            if invalid_primary:
                errors.append(
                    f"field-fill.jsonl:{row['_line']}: primary_category cannot mechanically use "
                    f"{invalid_primary}; classify the resource content or function"
                )
            unknown_primary = sorted(
                value
                for value in primary_values
                if (
                    value not in primary_terms
                    and value not in featured_terms
                    and value not in invalid_primary
                )
            )
            if unknown_primary:
                errors.append(
                    f"field-fill.jsonl:{row['_line']}: primary_category contains non-formal "
                    f"terms {unknown_primary}; use 待分类 and put suggestions in proposed_terms"
                )
            primary_feature_conflicts = sorted(set(primary_values) & featured_terms)
            if primary_feature_conflicts:
                errors.append(
                    f"field-fill.jsonl:{row['_line']}: primary_category uses featured-source "
                    f"terms {primary_feature_conflicts}; move them to featured_sources"
                )
            discouraged_primary = sorted(set(primary_values) & discouraged_primary_terms)
            if discouraged_primary:
                warnings.append(
                    f"field-fill.jsonl:{row['_line']}: primary_category uses discouraged broad "
                    f"terms {discouraged_primary}; verify this is not a shortcut"
                )
            alias_primary = sorted(value for value in primary_values if value in legacy_aliases)
            if alias_primary:
                replacements = {value: legacy_aliases[value] for value in alias_primary}
                errors.append(
                    f"field-fill.jsonl:{row['_line']}: primary_category uses legacy aliases "
                    f"{replacements}; use the formal term and role"
                )
            if len(primary_values) > 3:
                warnings.append(
                    f"field-fill.jsonl:{row['_line']}: primary_category has more than 3 values; "
                    "keep only core content or functions, or mark for human review"
                )

            featured_values = taxonomy_values(fields.get("featured_sources"))
            if has_nonstandard_separator(fields.get("featured_sources")):
                warnings.append(
                    f"field-fill.jsonl:{row['_line']}: featured_sources should use Chinese comma ， "
                    "between values"
                )
            proposed_featured = proposed_terms_for(row, "featured_sources")
            if not featured_values:
                warnings.append(
                    f"field-fill.jsonl:{row['_line']}: featured_sources is blank; verify the "
                    "empty_reason explains why no distinctive corpus can be identified and "
                    "marks human review"
                )
            else:
                unknown_featured = sorted(
                    value
                    for value in featured_values
                    if (
                        value not in featured_terms
                        and value not in primary_terms
                        and value not in proposed_featured
                    )
                )
                if unknown_featured:
                    errors.append(
                        f"field-fill.jsonl:{row['_line']}: featured_sources contains non-formal "
                        f"terms {unknown_featured}; add matching proposed_terms or use formal terms"
                    )
                featured_primary_conflicts = sorted(set(featured_values) & primary_terms)
                if featured_primary_conflicts:
                    errors.append(
                        f"field-fill.jsonl:{row['_line']}: featured_sources uses primary-category "
                        f"terms {featured_primary_conflicts}; move them to primary_category"
                    )
                alias_featured = sorted(value for value in featured_values if value in legacy_aliases)
                if alias_featured:
                    replacements = {value: legacy_aliases[value] for value in alias_featured}
                    errors.append(
                        f"field-fill.jsonl:{row['_line']}: featured_sources uses legacy aliases "
                        f"{replacements}; use the formal term"
                    )
            if featured_values and set(featured_values).issubset(GENERIC_FEATURED_SOURCES):
                warnings.append(
                    f"field-fill.jsonl:{row['_line']}: featured_sources contains only generic "
                    "material names; record the distinctive corpus, edition, collection, people, "
                    "period, or provenance"
                )
            if len(featured_values) > 3:
                warnings.append(
                    f"field-fill.jsonl:{row['_line']}: featured_sources has more than 3 values"
                )

            database_values = taxonomy_values(fields.get("database_type"))
            if has_nonstandard_separator(fields.get("database_type")):
                warnings.append(
                    f"field-fill.jsonl:{row['_line']}: database_type should use Chinese comma ， "
                    "between values"
                )
            proposed_database_values = proposed_database_terms(row)
            if proposed_database_values:
                errors.append(
                    f"field-fill.jsonl:{row['_line']}: database_type does not accept "
                    f"proposed_terms {proposed_database_values}; leave the field blank or use "
                    "综合库/专题库/知识库"
                )
            unknown_database_types = sorted(
                value
                for value in database_values
                if value not in database_type_terms
            )
            if unknown_database_types:
                errors.append(
                    f"field-fill.jsonl:{row['_line']}: unsupported database_type "
                    f"{unknown_database_types}; use 综合库/专题库/知识库 or leave blank"
                )
            if len(database_values) > 1:
                warnings.append(
                    f"field-fill.jsonl:{row['_line']}: database_type is generally single-valued"
                )
            if site_type == "网站" and database_values:
                warnings.append(
                    f"field-fill.jsonl:{row['_line']}: website has database_type; verify that it "
                    "is not actually a database"
                )
            if site_type == "子库" and is_blank(parent):
                warnings.append(
                    f"field-fill.jsonl:{row['_line']}: child database has no formal parent UUID; "
                    "record the parent name in the field review and mark it for completion"
                )
            if site_type in {"网站", "数据库", "母库", "独立库"} and not is_blank(parent):
                warnings.append(
                    f"field-fill.jsonl:{row['_line']}: non-child site_type has parent_database"
                )

        introduction_url = str(fields.get("introduction_url") or "").strip()
        entrance_url = str(fields.get("entrance_url") or "").strip()
        official = str(fields.get("official_introduction") or "").strip()
        official_source = str(fields.get("official_source") or "").strip()
        body = str(fields.get("body_introduction") or "").strip()
        body_source = str(fields.get("body_source") or "").strip()
        reviews_by_key = {
            str(item.get("field_key", "")): item
            for item in field_reviews
            if isinstance(item, dict)
        }
        official_excerpt = str(
            reviews_by_key.get("official_introduction", {}).get("evidence_excerpt") or ""
        ).strip()

        if official and not official_source:
            errors.append(
                f"field-fill.jsonl:{row['_line']}: official_introduction needs official_source"
            )
        if body and not body_source:
            errors.append(
                f"field-fill.jsonl:{row['_line']}: body_introduction needs body_source"
            )
        if official and len(official) < 500:
            warnings.append(
                f"field-fill.jsonl:{row['_line']}: official_introduction is under 500 characters; "
                "verify it is the complete official introduction rather than a snippet"
            )
        if official and official_excerpt and official == official_excerpt:
            warnings.append(
                f"field-fill.jsonl:{row['_line']}: official_introduction equals its evidence excerpt; "
                "verify the full introduction page was captured"
            )
        if (
            introduction_url
            and entrance_url
            and introduction_url.rstrip("/") == entrance_url.rstrip("/")
            and (not official or len(official) < 500)
        ):
            warnings.append(
                f"field-fill.jsonl:{row['_line']}: introduction_url equals entrance_url and no "
                "substantial official introduction was saved; search for a dedicated About/Project page"
            )
        if introduction_url and not official:
            warnings.append(
                f"field-fill.jsonl:{row['_line']}: introduction_url is present but "
                "official_introduction is blank; confirm the site truly has no capturable introduction"
            )

    independent_count = 0
    for candidate_uuid, result in dedup.items():
        verdict = result.get("final_verdict")
        if verdict in {"fillable", "blocked"}:
            independent_count += 1
            if candidate_uuid not in fills:
                errors.append(f"candidate {candidate_uuid}: independent candidate needs field-fill.jsonl")
            if candidate_uuid not in reviews:
                errors.append(f"candidate {candidate_uuid}: independent candidate needs ai-review.jsonl")
        elif candidate_uuid in fills:
            errors.append(f"candidate {candidate_uuid}: duplicate or suspected candidate must not receive full field fill")

    for candidate_uuid, row in reviews.items():
        if candidate_uuid not in candidates:
            errors.append(f"ai-review.jsonl:{row['_line']}: unknown candidate {candidate_uuid}")
        if not is_uuid(row.get("review_uuid")):
            errors.append(f"ai-review.jsonl:{row['_line']}: invalid review_uuid")
        score = row.get("score")
        if not isinstance(score, int) or not 0 <= score <= 100:
            errors.append(f"ai-review.jsonl:{row['_line']}: score must be 0-100")
        if row.get("independent") is not True:
            errors.append(f"ai-review.jsonl:{row['_line']}: review must be independent")
        if not str(row.get("rationale", "")).strip():
            errors.append(f"ai-review.jsonl:{row['_line']}: rationale is required")

    for row in tables["evidence.jsonl"]:
        if not is_uuid(row.get("evidence_uuid")):
            errors.append(f"evidence.jsonl:{row['_line']}: invalid evidence_uuid")
        if row.get("candidate_uuid") not in candidates:
            errors.append(f"evidence.jsonl:{row['_line']}: unknown candidate_uuid")
        field_keys = row.get("field_keys")
        if not isinstance(field_keys, list) or not field_keys or set(field_keys) - set(FIELD_KEYS):
            errors.append(f"evidence.jsonl:{row['_line']}: field_keys must contain contract keys")
        for required in ("idempotency_key", "url", "page_title", "excerpt", "accessed_at"):
            if not str(row.get(required, "")).strip():
                errors.append(f"evidence.jsonl:{row['_line']}: {required} is required")

    for row in tables["search-records.jsonl"]:
        if row.get("candidate_uuid") not in candidates:
            errors.append(f"search-records.jsonl:{row['_line']}: unknown candidate_uuid")
        if not str(row.get("index_source_name", "")).strip() or not str(row.get("index_source_url", "")).strip():
            errors.append(f"search-records.jsonl:{row['_line']}: index source name and URL are required")

    if independent_count < target_count:
        errors.append(f"independent candidate count {independent_count} is below target {target_count}")
    if not candidates:
        errors.append("candidates.jsonl contains no candidates")

    stats = {
        "candidate_count": len(candidates),
        "independent_count": independent_count,
        "target_count": target_count,
        "dedup_count": len(dedup),
        "filled_count": len(fills),
        "review_count": len(reviews),
        "evidence_count": len(tables["evidence.jsonl"]),
    }
    return finish(batch_dir, args.report, errors, warnings, stats)


def finish(batch_dir: Path, report_path: str | None, errors: list[str], warnings: list[str], stats: dict) -> int:
    report = {
        "ok": not errors,
        "batch_dir": str(batch_dir),
        "errors": errors,
        "warnings": warnings,
        "stats": stats,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if report_path:
        Path(report_path).resolve().write_text(text + "\n", encoding="utf-8")
    stream = sys.stdout if not errors else sys.stderr
    print(text, file=stream)
    return 0 if not errors else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
