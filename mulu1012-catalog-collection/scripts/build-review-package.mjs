#!/usr/bin/env node

import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { createRequire } from "node:module";

const FIELD_DEFINITIONS = [
	["title", "标题"],
	["aliases", "别名"],
	["wordpress_post_id", "文章ID"],
	["site_type", "站点类型"],
	["primary_category", "主分类"],
	["featured_sources", "特色史料"],
	["database_type", "数据库类型"],
	["organizer", "主办者"],
	["series", "系列"],
	["parent_database", "母库"],
	["index_source", "索引来源"],
	["introduction_url", "介绍页url"],
	["entrance_url", "入口url"],
	["access_type", "访问方式"],
	["summary", "摘要"],
	["summary_source", "简介来源"],
	["body_introduction", "正文介绍"],
	["body_source", "正文介绍来源"],
	["official_introduction", "官网介绍"],
	["official_source", "官网介绍来源"],
	["research_topic", "研究主题"],
	["geo_scope", "国别/区域史"],
	["special_history", "专门史"],
	["digital_format", "电子资源形态"],
	["material_type", "材料类型"],
	["material_language", "材料语言"],
	["period", "时期"],
	["coverage_years", "收录年限"],
	["project", "课题"],
	["site_language", "网站语言"],
	["launch_date", "上线时间"],
	["rating", "评级"],
	["authorized_institutions", "有授权的机构"],
	["authorized_libraries", "有授权的图书馆"],
	["library_names", "不同图书馆的名称"],
	["ai_score", "AI填充评分"],
	["ai_comment", "AI填充评语"],
];

const DEDUP_HEADERS = [
	"序号",
	"候选UUID",
	"标题",
	"最终结论",
	"最高分",
	"本地互重结论",
	"正式库结论",
	"正式库修订号",
	"匹配题录",
	"逐项依据",
	"独立反证",
	"AI深审评分",
	"阻断项",
	"索引来源",
	"检查时间",
];

const STRUCTURED_TEXT_FIELDS = new Set([
	"authorized_institutions",
	"authorized_libraries",
	"library_names",
]);
const FULL_TEXT_FIELDS = new Set(["body_introduction", "official_introduction"]);
const EXCEL_CELL_LIMIT = 32767;

function parseArgs(argv) {
	const result = {};
	for (let index = 2; index < argv.length; index += 1) {
		const arg = argv[index];
		if (!arg.startsWith("--")) continue;
		const key = arg.slice(2);
		const next = argv[index + 1];
		if (next && !next.startsWith("--")) {
			result[key] = next;
			index += 1;
		} else {
			result[key] = true;
		}
	}
	if (!result["batch-dir"]) {
		throw new Error("--batch-dir is required");
	}
	return result;
}

function loadArtifactTool(moduleRoot) {
	if (!moduleRoot) {
		throw new Error("Provide --node-modules using the path returned by load_workspace_dependencies.");
	}
	const requireFromRuntime = createRequire(path.join(path.resolve(moduleRoot), "artifact-loader.cjs"));
	return requireFromRuntime("@oai/artifact-tool");
}

async function readJson(filePath) {
	return JSON.parse(await fs.readFile(filePath, "utf8"));
}

async function readJsonl(filePath) {
	const text = await fs.readFile(filePath, "utf8");
	return text
		.replace(/^\uFEFF/, "")
		.split(/\r?\n/)
		.filter((line) => line.trim())
		.map((line) => JSON.parse(line));
}

function indexBy(rows, key) {
	return new Map(rows.map((row) => [String(row[key]), row]));
}

function withoutInternal(value) {
	if (Array.isArray(value)) return value.map(withoutInternal);
	if (!value || typeof value !== "object") return value;
	return Object.fromEntries(
		Object.entries(value)
			.filter(([key]) => !key.startsWith("_"))
			.map(([key, item]) => [key, withoutInternal(item)])
	);
}

function formatStructuredItem(item) {
	if (item === null || item === undefined) return "";
	if (typeof item !== "object") return String(item);
	const preferred = [
		["name", "名称"],
		["institution", "机构"],
		["library", "图书馆"],
		["status", "状态"],
		["access_scope", "访问范围"],
		["content", "授权内容"],
		["expires_at", "截止日期"],
		["verified_at", "核验日期"],
		["source_name", "来源"],
		["source_url", "来源URL"],
	];
	const parts = [];
	for (const [key, label] of preferred) {
		if (item[key] !== null && item[key] !== undefined && item[key] !== "") {
			parts.push(`${label}：${Array.isArray(item[key]) ? item[key].join("、") : item[key]}`);
		}
	}
	return parts.length ? parts.join("；") : JSON.stringify(item);
}

function structuredText(value) {
	const items = Array.isArray(value) ? value : [value];
	return items.map(formatStructuredItem).filter(Boolean).join("\n");
}

function cellValue(value) {
	if (value === null || value === undefined) return "";
	if (Array.isArray(value)) return value.map(formatStructuredItem).filter(Boolean).join("；");
	if (typeof value === "object") return formatStructuredItem(value);
	return value;
}

async function workbookCellValue(value, key, candidateUuid, batchDir) {
	const fullValue = String(cellValue(value));
	if (fullValue.length <= EXCEL_CELL_LIMIT) return fullValue;
	if (!FULL_TEXT_FIELDS.has(key)) {
		return `${fullValue.slice(0, EXCEL_CELL_LIMIT - 32)}\n[超过 Excel 单元格长度上限，已截断]`;
	}

	const evidenceDir = path.join(batchDir, "evidence");
	await fs.mkdir(evidenceDir, { recursive: true });
	const fileName = `${candidateUuid}-${key}.md`;
	await fs.writeFile(path.join(evidenceDir, fileName), fullValue, "utf8");
	return `完整 Markdown 已保存：evidence/${fileName}`;
}

function blankFields() {
	return Object.fromEntries(
		FIELD_DEFINITIONS.map(([key]) => [
			key,
			["aliases", "site_type", "primary_category", "featured_sources", "database_type", "organizer", "series", "access_type", "research_topic", "geo_scope", "special_history", "digital_format", "material_type", "material_language", "period", "rating"].includes(key)
				? []
				: key === "wordpress_post_id" || key === "ai_score"
					? null
					: "",
		])
	);
}

function compactDedupComment(dedup) {
	const labels = {
		duplicate: "重复",
		suspected_duplicate: "待人工判重",
		fillable: "独立，可填充",
		blocked: "独立但有阻断",
	};
	const top = Array.isArray(dedup?.matches) ? dedup.matches[0] : null;
	const lines = [
		"### 查重结论",
		"",
		`- **结论：** ${labels[dedup?.final_verdict] || dedup?.final_verdict || "未判断"}`,
		`- **最高分：** ${Number(dedup?.score || top?.score || 0)}/100`,
		`- **正式库修订号：** ${dedup?.formal_revision || ""}`,
	];
	if (top) {
		lines.push(`- **最高匹配：** ${top.title || top.record_uuid || top.candidate_uuid || "未命名"}`);
	}
	return lines.join("\n");
}

function mergeFields(candidate, fill, dedup, review) {
	const fields = blankFields();
	Object.assign(fields, candidate?.fields || {});
	Object.assign(fields, fill?.fields || {});
	for (const key of STRUCTURED_TEXT_FIELDS) {
		if (fields[key] && typeof fields[key] === "object") {
			fields[key] = structuredText(fields[key]);
		}
	}
	if ((fields.ai_score === null || fields.ai_score === "") && Number.isInteger(review?.score)) {
		fields.ai_score = review.score;
	}
	if (!String(fields.ai_comment || "").trim() && dedup?.final_verdict !== "fillable") {
		fields.ai_comment = compactDedupComment(dedup);
	}
	return fields;
}

function csvCell(value) {
	const text = String(cellValue(value)).replace(/\r\n/g, "\n");
	return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function toCsv(rows) {
	return rows.map((row) => row.map(csvCell).join(",")).join("\r\n") + "\r\n";
}

function columnName(index) {
	let value = index + 1;
	let result = "";
	while (value > 0) {
		const remainder = (value - 1) % 26;
		result = String.fromCharCode(65 + remainder) + result;
		value = Math.floor((value - 1) / 26);
	}
	return result;
}

function formatMatch(match) {
	const identity = match?.title || match?.record_uuid || match?.wp_post_id || match?.candidate_uuid || "未命名";
	return `${identity}${match?.score !== undefined ? `（${match.score}分）` : ""}`;
}

function formatReasons(matches) {
	const top = Array.isArray(matches) ? matches[0] : null;
	return (top?.reasons || [])
		.map((reason) => `${reason.label || reason.item}（${reason.weight}）`)
		.join("\n");
}

function formatCounterevidence(dedup) {
	const review = dedup?.ai_deep_review;
	const value = review?.counterevidence || review?.independent_evidence || review?.distinct_evidence || "";
	return Array.isArray(value) ? value.join("\n") : String(value || "");
}

function formatBlockers(review, dedup) {
	const blockers = [
		...(Array.isArray(review?.blockers) ? review.blockers : []),
		...(Array.isArray(dedup?.blockers) ? dedup.blockers : []),
	];
	return blockers
		.map((item) => (typeof item === "string" ? item : item.message || item.code || JSON.stringify(item)))
		.join("\n");
}

function countBy(rows, key) {
	const counts = {};
	for (const row of rows) {
		const value = String(row[key] || "unknown");
		counts[value] = (counts[value] || 0) + 1;
	}
	return counts;
}

function sha256(text) {
	return crypto.createHash("sha256").update(text).digest("hex");
}

async function main() {
	const args = parseArgs(process.argv);
	const batchDir = path.resolve(args["batch-dir"]);
	const { SpreadsheetFile, Workbook } = loadArtifactTool(args["node-modules"] || process.env.CODEX_NODE_MODULES);

	const batch = await readJson(path.join(batchDir, "batch.json"));
	const candidates = await readJsonl(path.join(batchDir, "candidates.jsonl"));
	const dedupRows = await readJsonl(path.join(batchDir, "dedup-results.jsonl"));
	const fillRows = await readJsonl(path.join(batchDir, "field-fill.jsonl"));
	const evidenceRows = await readJsonl(path.join(batchDir, "evidence.jsonl"));
	const reviewRows = await readJsonl(path.join(batchDir, "ai-review.jsonl"));

	const dedupByCandidate = indexBy(dedupRows, "candidate_uuid");
	const fillByCandidate = indexBy(fillRows, "candidate_uuid");
	const reviewByCandidate = indexBy(reviewRows, "candidate_uuid");
	const evidenceByCandidate = new Map();
	for (const evidence of evidenceRows) {
		const key = String(evidence.candidate_uuid);
		if (!evidenceByCandidate.has(key)) evidenceByCandidate.set(key, []);
		evidenceByCandidate.get(key).push(withoutInternal(evidence));
	}

	const mainRows = [];
	const csvMainRows = [];
	const dedupDetails = [];
	const uploadRows = [];

	for (const [index, candidate] of candidates.entries()) {
		const candidateUuid = String(candidate.candidate_uuid);
		const dedup = dedupByCandidate.get(candidateUuid) || {};
		const fill = fillByCandidate.get(candidateUuid) || null;
		const review = reviewByCandidate.get(candidateUuid) || null;
		const fields = mergeFields(candidate, fill, dedup, review);
		const topMatches = Array.isArray(dedup.matches) ? dedup.matches : [];

		const fullRow = FIELD_DEFINITIONS.map(([key]) => cellValue(fields[key]));
		csvMainRows.push(fullRow);
		const workbookRow = [];
		for (const [key] of FIELD_DEFINITIONS) {
			workbookRow.push(await workbookCellValue(fields[key], key, candidateUuid, batchDir));
		}
		mainRows.push(workbookRow);
		dedupDetails.push([
			index + 1,
			candidateUuid,
			fields.title || "",
			dedup.final_verdict || "",
			Number(dedup.score || topMatches[0]?.score || 0),
			dedup.local_verdict || "",
			dedup.formal_verdict || "",
			dedup.formal_revision || "",
			topMatches.slice(0, 5).map(formatMatch).join("\n"),
			formatReasons(topMatches),
			formatCounterevidence(dedup),
			dedup.ai_deep_review?.score ?? "",
			formatBlockers(review, dedup),
			fields.index_source || candidate.source_locator || "",
			dedup.checked_at || "",
		]);

		uploadRows.push({
			candidate_uuid: candidateUuid,
			version_uuid: fill?.version_uuid || candidate.version_uuid,
			idempotency_key: candidate.idempotency_key,
			source_kind: candidate.source_kind || "local_collection",
			source_locator: candidate.source_locator || fields.index_source || "",
			parent_database_confirmed: Boolean(fill?.parent_database_confirmed),
			fields,
			dedup: {
				run_uuid: dedup.dedup_uuid,
				formal_revision: dedup.formal_revision,
				algorithm_version: dedup.algorithm_version,
				verdict: dedup.final_verdict === "blocked" ? "fillable" : dedup.final_verdict,
				matches: topMatches,
			},
			evidence: evidenceByCandidate.get(candidateUuid) || [],
			ai_review: review ? withoutInternal(review) : {},
			proposed_terms: Array.isArray(fill?.proposed_terms) ? withoutInternal(fill.proposed_terms) : [],
			blockers: [
				...(Array.isArray(review?.blockers) ? withoutInternal(review.blockers) : []),
				...(Array.isArray(dedup?.blockers) ? withoutInternal(dedup.blockers) : []),
			],
		});
	}

	const uploadText = uploadRows.map((row) => JSON.stringify(row)).join("\n") + "\n";
	const csvRows = [FIELD_DEFINITIONS.map(([, label]) => label), ...csvMainRows];
	const csvText = "\uFEFF" + toCsv(csvRows);

	const workbook = Workbook.create();
	const catalogSheet = workbook.worksheets.add("题录总表");
	const dedupSheet = workbook.worksheets.add("查重明细");
	catalogSheet.showGridLines = false;
	dedupSheet.showGridLines = false;

	const catalogMatrix = [FIELD_DEFINITIONS.map(([, label]) => label), ...mainRows];
	const catalogEnd = `${columnName(FIELD_DEFINITIONS.length - 1)}${catalogMatrix.length}`;
	catalogSheet.getRange(`A1:${catalogEnd}`).values = catalogMatrix;
	catalogSheet.freezePanes.freezeRows(1);
	catalogSheet.freezePanes.freezeColumns(2);
	catalogSheet.getRange(`A1:${columnName(FIELD_DEFINITIONS.length - 1)}1`).format = {
		fill: "#9F2D20",
		font: { bold: true, color: "#FFFFFF", name: "Microsoft YaHei", size: 10 },
		verticalAlignment: "center",
		wrapText: true,
		borders: { preset: "outside", style: "medium", color: "#6E1D14" },
	};
	catalogSheet.getRange(`A2:${catalogEnd}`).format = {
		font: { color: "#1E1E1E", name: "Microsoft YaHei", size: 9 },
		verticalAlignment: "top",
		wrapText: true,
		borders: { preset: "inside", style: "thin", color: "#DEDAD2" },
	};
	catalogSheet.getRange(`A1:${catalogEnd}`).format.rowHeight = 54;
	catalogSheet.getRange(`A1:${columnName(FIELD_DEFINITIONS.length - 1)}1`).format.rowHeight = 34;
	for (let index = 0; index < FIELD_DEFINITIONS.length; index += 1) {
		const key = FIELD_DEFINITIONS[index][0];
		const column = columnName(index);
		const wide = ["summary", "body_introduction", "official_introduction", "ai_comment"].includes(key);
		const medium = ["index_source", "introduction_url", "entrance_url", "summary_source", "body_source", "official_source", "authorized_institutions", "authorized_libraries"].includes(key);
		catalogSheet.getRange(`${column}:${column}`).format.columnWidth = wide ? 46 : medium ? 30 : 18;
	}
	const catalogTable = catalogSheet.tables.add(`A1:${catalogEnd}`, true, "CatalogEntries");
	catalogTable.showFilterButton = true;
	catalogTable.showBandedRows = true;

	const dedupMatrix = [DEDUP_HEADERS, ...dedupDetails];
	const dedupEnd = `${columnName(DEDUP_HEADERS.length - 1)}${dedupMatrix.length}`;
	dedupSheet.getRange(`A1:${dedupEnd}`).values = dedupMatrix;
	dedupSheet.freezePanes.freezeRows(1);
	dedupSheet.freezePanes.freezeColumns(3);
	dedupSheet.getRange(`A1:${columnName(DEDUP_HEADERS.length - 1)}1`).format = {
		fill: "#2E3532",
		font: { bold: true, color: "#FFFFFF", name: "Microsoft YaHei", size: 10 },
		verticalAlignment: "center",
		wrapText: true,
		borders: { preset: "outside", style: "medium", color: "#161A18" },
	};
	dedupSheet.getRange(`A2:${dedupEnd}`).format = {
		font: { color: "#1E1E1E", name: "Microsoft YaHei", size: 9 },
		verticalAlignment: "top",
		wrapText: true,
		borders: { preset: "inside", style: "thin", color: "#D8D8D8" },
	};
	dedupSheet.getRange(`A1:${dedupEnd}`).format.rowHeight = 48;
	dedupSheet.getRange("A:A").format.columnWidth = 8;
	dedupSheet.getRange("B:B").format.columnWidth = 38;
	dedupSheet.getRange("C:C").format.columnWidth = 28;
	dedupSheet.getRange("D:H").format.columnWidth = 18;
	dedupSheet.getRange("I:O").format.columnWidth = 34;
	const dedupTable = dedupSheet.tables.add(`A1:${dedupEnd}`, true, "DedupDetails");
	dedupTable.showFilterButton = true;
	dedupTable.showBandedRows = true;
	if (dedupDetails.length) {
		dedupSheet.getRange(`D2:D${dedupMatrix.length}`).conditionalFormats.add("containsText", {
			text: "duplicate",
			format: { fill: "#FCE8E6", font: { color: "#9F2D20", bold: true } },
		});
		dedupSheet.getRange(`D2:D${dedupMatrix.length}`).conditionalFormats.add("containsText", {
			text: "fillable",
			format: { fill: "#E8F3EA", font: { color: "#256B3B", bold: true } },
		});
	}

	const xlsx = await SpreadsheetFile.exportXlsx(workbook);
	const xlsxPath = path.join(batchDir, "题录审核.xlsx");
	await xlsx.save(xlsxPath);
	await fs.rm(`${xlsxPath}.inspect.ndjson`, { force: true });
	await fs.writeFile(path.join(batchDir, "题录总表.csv"), csvText, "utf8");
	await fs.writeFile(path.join(batchDir, "wordpress-upload.jsonl"), uploadText, "utf8");

	const verdictCounts = countBy(dedupRows, "final_verdict");
	const formalRevisions = [...new Set(dedupRows.map((row) => row.formal_revision).filter(Boolean))];
	const manifest = {
		schema_version: "1.0",
		batch_uuid: batch.batch_uuid,
		idempotency_key: batch.idempotency_key,
		name: batch.name,
		contract_version: batch.contract_version,
		contract_hash: batch.contract_hash,
		classification_contract_version: batch.classification_contract_version || "",
		classification_contract_hash: batch.classification_contract_hash || "",
		formal_revision: formalRevisions.length === 1 ? formalRevisions[0] : formalRevisions,
		research_brief: batch.research_brief,
		candidate_count: candidates.length,
		verdict_counts: verdictCounts,
		filled_count: fillRows.length,
		evidence_count: evidenceRows.length,
		review_count: reviewRows.length,
		upload_sha256: sha256(uploadText),
		generated_at: new Date().toISOString(),
		files: {
			review_workbook: "题录审核.xlsx",
			catalog_csv: "题录总表.csv",
			upload_jsonl: "wordpress-upload.jsonl",
			summary: "任务总结.md",
		},
	};
	await fs.writeFile(
		path.join(batchDir, "upload-manifest.json"),
		JSON.stringify(manifest, null, 2) + "\n",
		"utf8"
	);

	const proposedTerms = fillRows.flatMap((row) => row.proposed_terms || []);
	const summary = [
		`# ${batch.name || "目录1012题录搜集任务"}`,
		"",
		"## 任务说明",
		"",
		`- **批次 UUID：** \`${batch.batch_uuid}\``,
		`- **主题：** ${batch.research_brief?.topic || ""}`,
		`- **目标独立候选：** ${batch.research_brief?.target_count || 0}`,
		`- **任务目标：** ${batch.research_brief?.goal || ""}`,
		"",
		"## 搜索与查重",
		"",
		`- **全部候选：** ${candidates.length}`,
		`- **确认独立：** ${(verdictCounts.fillable || 0) + (verdictCounts.blocked || 0)}`,
		`- **重复：** ${verdictCounts.duplicate || 0}`,
		`- **待人工判重：** ${verdictCounts.suspected_duplicate || 0}`,
		`- **独立但阻断：** ${verdictCounts.blocked || 0}`,
		`- **正式库修订号：** ${formalRevisions.join("、")}`,
		"",
		"## 填充与审核",
		"",
		`- **完整填充：** ${fillRows.length}`,
		`- **字段证据：** ${evidenceRows.length}`,
		`- **独立 AI 审核：** ${reviewRows.length}`,
		`- **拟新增词项：** ${proposedTerms.length}`,
		"",
		"## 阻断项",
		"",
		formatBlockers({ blockers: reviewRows.flatMap((row) => row.blockers || []) }, { blockers: dedupRows.flatMap((row) => row.blockers || []) }) || "无",
		"",
		"## 输出文件",
		"",
		"- `题录审核.xlsx`：题录总表与查重明细。",
		"- `题录总表.csv`：UTF-8 BOM 审核副本。",
		"- `wordpress-upload.jsonl`：待上传候选。",
		"- `upload-manifest.json`：数量、版本和校验和。",
		"",
		"## 上传状态",
		"",
		"尚未上传。审核包生成后已暂停，等待用户明确确认。",
		"",
	];
	await fs.writeFile(path.join(batchDir, "任务总结.md"), summary.join("\n"), "utf8");

	console.log(JSON.stringify({
		ok: true,
		batch_dir: batchDir,
		candidate_count: candidates.length,
		files: manifest.files,
	}, null, 2));
}

main().catch((error) => {
	console.error(JSON.stringify({ ok: false, error: error.message }, null, 2));
	process.exitCode = 1;
});
