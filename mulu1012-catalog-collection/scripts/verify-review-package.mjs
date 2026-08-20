#!/usr/bin/env node

import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { createRequire } from "node:module";

const EXPECTED_SHEETS = ["题录总表", "查重明细"];
const EXPECTED_HEADERS = [
	"标题", "别名", "文章ID", "站点类型", "主分类", "特色史料", "数据库类型", "主办者", "系列", "母库",
	"索引来源", "介绍页url", "入口url", "访问方式", "摘要", "简介来源", "正文介绍", "正文介绍来源", "官网介绍",
	"官网介绍来源", "研究主题", "国别/区域史", "专门史", "电子资源形态", "材料类型", "材料语言", "时期",
	"收录年限", "课题", "网站语言", "上线时间", "评级", "有授权的机构", "有授权的图书馆", "不同图书馆的名称",
	"AI填充评分", "AI填充评语",
];

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
	if (!result["batch-dir"]) throw new Error("--batch-dir is required");
	return result;
}

function loadArtifactTool(moduleRoot) {
	if (!moduleRoot) {
		throw new Error("Provide --node-modules using the path returned by load_workspace_dependencies.");
	}
	const requireFromRuntime = createRequire(path.join(path.resolve(moduleRoot), "artifact-loader.cjs"));
	return requireFromRuntime("@oai/artifact-tool");
}

function parseCsv(text) {
	const rows = [];
	let row = [];
	let cell = "";
	let quoted = false;
	for (let index = 0; index < text.length; index += 1) {
		const char = text[index];
		if (quoted) {
			if (char === '"' && text[index + 1] === '"') {
				cell += '"';
				index += 1;
			} else if (char === '"') {
				quoted = false;
			} else {
				cell += char;
			}
		} else if (char === '"') {
			quoted = true;
		} else if (char === ",") {
			row.push(cell);
			cell = "";
		} else if (char === "\n") {
			row.push(cell.replace(/\r$/, ""));
			rows.push(row);
			row = [];
			cell = "";
		} else {
			cell += char;
		}
	}
	if (cell || row.length) {
		row.push(cell);
		rows.push(row);
	}
	return rows;
}

function countJsonl(text) {
	return text.split(/\r?\n/).filter((line) => line.trim()).length;
}

function sha256(text) {
	return crypto.createHash("sha256").update(text).digest("hex");
}

async function renderCheck(workbook, sheetName, range, tempDir) {
	const blob = await workbook.render({
		sheetName,
		range,
		scale: 0.65,
		format: "png",
	});
	const bytes = new Uint8Array(await blob.arrayBuffer());
	if (bytes.byteLength < 1000) {
		throw new Error(`${sheetName} render is unexpectedly small`);
	}
	const filePath = path.join(tempDir, `${sheetName}.png`);
	await fs.writeFile(filePath, bytes);
	return { sheet: sheetName, bytes: bytes.byteLength };
}

async function main() {
	const args = parseArgs(process.argv);
	const batchDir = path.resolve(args["batch-dir"]);
	const { FileBlob, SpreadsheetFile } = loadArtifactTool(args["node-modules"] || process.env.CODEX_NODE_MODULES);
	const workbookPath = path.join(batchDir, "题录审核.xlsx");
	const csvPath = path.join(batchDir, "题录总表.csv");
	const uploadPath = path.join(batchDir, "wordpress-upload.jsonl");
	const manifestPath = path.join(batchDir, "upload-manifest.json");

	const [input, csvTextRaw, uploadText, manifestText] = await Promise.all([
		FileBlob.load(workbookPath),
		fs.readFile(csvPath, "utf8"),
		fs.readFile(uploadPath, "utf8"),
		fs.readFile(manifestPath, "utf8"),
	]);
	const workbook = await SpreadsheetFile.importXlsx(input);
	const sheetNames = workbook.worksheets.items.map((sheet) => sheet.name);
	if (JSON.stringify(sheetNames) !== JSON.stringify(EXPECTED_SHEETS)) {
		throw new Error(`XLSX sheets must be exactly ${EXPECTED_SHEETS.join(", ")}; got ${sheetNames.join(", ")}`);
	}

	const catalogSheet = workbook.worksheets.getItem("题录总表");
	const dedupSheet = workbook.worksheets.getItem("查重明细");
	const catalogValues = catalogSheet.getUsedRange().values;
	const dedupValues = dedupSheet.getUsedRange().values;
	const headers = (catalogValues[0] || []).map(String);
	if (JSON.stringify(headers) !== JSON.stringify(EXPECTED_HEADERS)) {
		throw new Error("题录总表 headers do not match the exact 37-field contract");
	}
	if ((dedupValues[0] || []).length !== 15) {
		throw new Error("查重明细 must contain 15 columns");
	}

	const csvText = csvTextRaw.replace(/^\uFEFF/, "");
	const csvRows = parseCsv(csvText);
	if (JSON.stringify(csvRows[0]) !== JSON.stringify(EXPECTED_HEADERS)) {
		throw new Error("CSV headers do not match the exact 37-field contract");
	}
	const xlsxCount = Math.max(0, catalogValues.length - 1);
	const dedupCount = Math.max(0, dedupValues.length - 1);
	const csvCount = Math.max(0, csvRows.length - 1);
	const uploadCount = countJsonl(uploadText);
	if (new Set([xlsxCount, dedupCount, csvCount, uploadCount]).size !== 1) {
		throw new Error(`candidate counts differ: xlsx=${xlsxCount}, dedup=${dedupCount}, csv=${csvCount}, jsonl=${uploadCount}`);
	}

	const manifest = JSON.parse(manifestText);
	if (Number(manifest.candidate_count) !== uploadCount) {
		throw new Error("manifest candidate_count does not match upload JSONL");
	}
	if (manifest.upload_sha256 !== sha256(uploadText)) {
		throw new Error("manifest upload_sha256 does not match wordpress-upload.jsonl");
	}

	const inspect = await workbook.inspect({
		kind: "sheet,table",
		include: "id,name,range",
		maxChars: 5000,
		tableMaxRows: 5,
		tableMaxCols: 8,
	});
	const formulaErrors = await workbook.inspect({
		kind: "match",
		searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
		options: { useRegex: true, maxResults: 100 },
		summary: "review package formula error scan",
	});
	if (/\"matchCount\"\s*:\s*[1-9]/.test(formulaErrors.ndjson)) {
		throw new Error("formula errors found in workbook");
	}

	const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "mulu1012-review-"));
	let renders;
	try {
		renders = await Promise.all([
			renderCheck(workbook, "题录总表", `A1:AK${Math.min(catalogValues.length, 6)}`, tempDir),
			renderCheck(workbook, "查重明细", `A1:O${Math.min(dedupValues.length, 10)}`, tempDir),
		]);
	} finally {
		await fs.rm(tempDir, { recursive: true, force: true });
	}

	console.log(JSON.stringify({
		ok: true,
		batch_dir: batchDir,
		candidate_count: uploadCount,
		sheets: sheetNames,
		renders,
		inspection: inspect.ndjson.slice(0, 1200),
	}, null, 2));
}

main().catch((error) => {
	console.error(JSON.stringify({ ok: false, error: error.message }, null, 2));
	process.exitCode = 1;
});
