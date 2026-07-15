#!/usr/bin/env node
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { pathToFileURL } from "node:url";

const defaultArtifactModule = path.join(
  os.homedir(),
  ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/@oai/artifact-tool/dist/artifact_tool.mjs",
);
const artifactModule = process.env.ARTIFACT_TOOL_MODULE || defaultArtifactModule;
const { FileBlob, SpreadsheetFile, Workbook } = await import(pathToFileURL(artifactModule).href);

const CHINESE_SHEET = "中文内容";
const TRANSLATION_SHEET = "多语种翻译";
const INSTRUCTIONS_SHEET = "使用说明";
const CHINESE_HEADERS = ["模板字段", "规格书原始依据", "AI提取中文", "最终中文", "字段编号（请勿修改）"];
const TRANSLATION_HEADERS = ["模板字段", "语种", "已确认中文", "AI翻译", "最终译文", "翻译编号（请勿修改）"];

function parseArgs(argv) {
  const [command, ...rest] = argv;
  const args = { command };
  for (let i = 0; i < rest.length; i += 1) {
    if (!rest[i].startsWith("--")) continue;
    const key = rest[i].slice(2).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
    const next = rest[i + 1];
    args[key] = next && !next.startsWith("--") ? (i += 1, next) : true;
  }
  return args;
}

function required(args, key) {
  if (!args[key]) throw new Error(`缺少参数 --${key.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`)}`);
  return path.resolve(String(args[key]));
}

function asText(value) {
  const text = value === undefined || value === null ? "" : String(value);
  return text.startsWith("=") ? `'${text}` : text;
}

function normalizeReadText(value) {
  const text = value === undefined || value === null ? "" : String(value);
  return text.startsWith("'=") ? text.slice(1) : text;
}

function styleReviewSheet(sheet, lastRow, lastColumn) {
  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(4);
  sheet.getRange(`A1:${lastColumn}1`).format = {
    fill: "#17324D",
    font: { bold: true, color: "#FFFFFF", size: 16 },
    rowHeight: 30,
    verticalAlignment: "center",
  };
  sheet.getRange(`A2:${lastColumn}2`).format = {
    fill: "#EAF2F8",
    font: { color: "#314A5E", size: 10 },
    wrapText: true,
    rowHeight: 34,
    verticalAlignment: "center",
  };
  sheet.getRange(`A4:${lastColumn}4`).format = {
    fill: "#2F6B8A",
    font: { bold: true, color: "#FFFFFF" },
    wrapText: true,
    verticalAlignment: "center",
    borders: { preset: "outside", style: "thin", color: "#B8C7D1" },
  };
  if (lastRow >= 5) {
    sheet.getRange(`A5:${lastColumn}${lastRow}`).format = {
      verticalAlignment: "top",
      wrapText: true,
      borders: {
        insideHorizontal: { style: "thin", color: "#DDE5EA" },
        bottom: { style: "thin", color: "#B8C7D1" },
      },
    };
  }
}

function createInstructions(workbook) {
  const sheet = workbook.worksheets.add(INSTRUCTIONS_SHEET);
  sheet.showGridLines = false;
  sheet.getRange("A1:D1").merge();
  sheet.getRange("A1").values = [["说明书内容确认"]];
  sheet.getRange("A1:D1").format = { fill: "#17324D", font: { bold: true, color: "#FFFFFF", size: 18 }, rowHeight: 34 };
  sheet.getRange("A3:D7").values = [
    ["步骤", "你需要做什么", "可以修改", "不要修改"],
    ["1", "先检查“中文内容”Sheet", "最终中文", "其他列"],
    ["2", "保存并关闭 Excel，然后回复“中文内容已确认”", "", ""],
    ["3", "系统追加“多语种翻译”Sheet 后再次检查", "最终译文", "其他列"],
    ["4", "保存并关闭 Excel，然后回复“翻译内容已确认”", "", ""],
  ];
  sheet.getRange("A3:D3").format = { fill: "#2F6B8A", font: { bold: true, color: "#FFFFFF" } };
  sheet.getRange("A4:D7").format = { wrapText: true, verticalAlignment: "top", borders: { preset: "inside", style: "thin", color: "#DDE5EA" } };
  sheet.getRange("A3:D7").format.autofitRows();
  sheet.getRange("A:A").format.columnWidth = 10;
  sheet.getRange("B:B").format.columnWidth = 52;
  sheet.getRange("C:D").format.columnWidth = 24;
  return sheet;
}

function writeChineseSheet(workbook, rows) {
  const sheet = workbook.worksheets.add(CHINESE_SHEET);
  const lastRow = rows.length + 4;
  sheet.getRange("A1:E1").merge();
  sheet.getRange("A1").values = [["中文内容确认"]];
  sheet.getRange("A2:E2").merge();
  sheet.getRange("A2").values = [["请逐行核对规格书依据，只修改黄色的“最终中文”列。保存并关闭文件后，在对话中回复“中文内容已确认”。"]];
  sheet.getRange("A4:E4").values = [CHINESE_HEADERS];
  if (rows.length) {
    sheet.getRange(`A5:E${lastRow}`).values = rows.map((row) => [
      asText(row.fieldName),
      asText(row.sourceEvidence),
      asText(row.aiChinese),
      asText(row.finalChinese ?? row.aiChinese),
      asText(row.fieldId),
    ]);
    sheet.getRange(`D5:D${lastRow}`).format.fill = "#FFF2CC";
    sheet.getRange(`E5:E${lastRow}`).format = { fill: "#F2F2F2", font: { color: "#7F8C8D", size: 8 }, wrapText: false };
  }
  styleReviewSheet(sheet, lastRow, "E");
  sheet.getRange("A:A").format.columnWidth = 24;
  sheet.getRange("B:B").format.columnWidth = 54;
  sheet.getRange("C:D").format.columnWidth = 42;
  sheet.getRange("E:E").format.columnWidth = 18;
  return sheet;
}

function writeTranslationSheet(workbook, rows) {
  const sheet = workbook.worksheets.getOrAdd(TRANSLATION_SHEET);
  const used = sheet.getUsedRange();
  if (used) used.clear({ applyTo: "all" });
  const lastRow = rows.length + 4;
  sheet.getRange("A1:F1").merge();
  sheet.getRange("A1").values = [["多语种翻译确认"]];
  sheet.getRange("A2:F2").merge();
  sheet.getRange("A2").values = [["请对照已确认中文，只修改黄色的“最终译文”列。保存并关闭文件后，在对话中回复“翻译内容已确认”。"]];
  sheet.getRange("A4:F4").values = [TRANSLATION_HEADERS];
  if (rows.length) {
    sheet.getRange(`A5:F${lastRow}`).values = rows.map((row) => [
      asText(row.fieldName),
      asText(row.language),
      asText(row.confirmedChinese),
      asText(row.aiTranslation),
      asText(row.finalTranslation ?? row.aiTranslation),
      asText(row.translationId),
    ]);
    sheet.getRange(`E5:E${lastRow}`).format.fill = "#FFF2CC";
    sheet.getRange(`F5:F${lastRow}`).format = { fill: "#F2F2F2", font: { color: "#7F8C8D", size: 8 }, wrapText: false };
  }
  styleReviewSheet(sheet, lastRow, "F");
  sheet.getRange("A:A").format.columnWidth = 24;
  sheet.getRange("B:B").format.columnWidth = 12;
  sheet.getRange("C:E").format.columnWidth = 42;
  sheet.getRange("F:F").format.columnWidth = 24;
  return sheet;
}

async function importWorkbook(filename) {
  return SpreadsheetFile.importXlsx(await FileBlob.load(filename));
}

function readRows(sheet, expectedHeaders, columns) {
  if (!sheet) throw new Error("缺少确认 Sheet");
  const used = sheet.getUsedRange();
  const values = used?.values || [];
  const headers = values[3] || [];
  for (let i = 0; i < expectedHeaders.length; i += 1) {
    if (String(headers[i] || "").trim() !== expectedHeaders[i]) throw new Error(`表头不匹配：期望 ${expectedHeaders[i]}`);
  }
  const rows = [];
  for (let i = 4; i < values.length; i += 1) {
    const raw = values[i] || [];
    if (!raw.some((value) => String(value ?? "").trim())) continue;
    const item = {};
    for (const [key, column] of Object.entries(columns)) item[key] = normalizeReadText(raw[column]);
    item.excelRow = i + 1;
    rows.push(item);
  }
  return rows;
}

async function createChinese(args) {
  const input = JSON.parse(await fs.readFile(required(args, "input"), "utf8"));
  const output = required(args, "output");
  const workbook = Workbook.create();
  createInstructions(workbook);
  writeChineseSheet(workbook, input.rows || input);
  await fs.mkdir(path.dirname(output), { recursive: true });
  await (await SpreadsheetFile.exportXlsx(workbook)).save(output);
  console.log(JSON.stringify({ output, rows: (input.rows || input).length, sheets: [INSTRUCTIONS_SHEET, CHINESE_SHEET] }, null, 2));
}

async function appendTranslations(args) {
  const workbookPath = required(args, "workbook");
  const input = JSON.parse(await fs.readFile(required(args, "input"), "utf8"));
  const output = args.output ? path.resolve(String(args.output)) : workbookPath;
  const workbook = await importWorkbook(workbookPath);
  writeTranslationSheet(workbook, input.rows || input);
  await fs.mkdir(path.dirname(output), { recursive: true });
  await (await SpreadsheetFile.exportXlsx(workbook)).save(output);
  console.log(JSON.stringify({ output, rows: (input.rows || input).length, sheet: TRANSLATION_SHEET }, null, 2));
}

async function readChinese(args) {
  const workbook = await importWorkbook(required(args, "workbook"));
  const rows = readRows(workbook.worksheets.getItem(CHINESE_SHEET), CHINESE_HEADERS, {
    fieldName: 0, sourceEvidence: 1, aiChinese: 2, finalChinese: 3, fieldId: 4,
  });
  console.log(JSON.stringify({ rows }, null, 2));
}

async function readTranslations(args) {
  const workbook = await importWorkbook(required(args, "workbook"));
  const rows = readRows(workbook.worksheets.getItem(TRANSLATION_SHEET), TRANSLATION_HEADERS, {
    fieldName: 0, language: 1, confirmedChinese: 2, aiTranslation: 3, finalTranslation: 4, translationId: 5,
  });
  console.log(JSON.stringify({ rows }, null, 2));
}

async function verify(args) {
  const workbook = await importWorkbook(required(args, "workbook"));
  const outputDir = required(args, "outputDir");
  await fs.mkdir(outputDir, { recursive: true });
  const names = [INSTRUCTIONS_SHEET, CHINESE_SHEET, TRANSLATION_SHEET].filter((name) => {
    try { return Boolean(workbook.worksheets.getItem(name)); } catch { return false; }
  });
  const rendered = [];
  for (const name of names) {
    const sheet = workbook.worksheets.getItem(name);
    const used = sheet.getUsedRange();
    const inspection = await workbook.inspect({ kind: "table", sheetId: name, range: used?.address, include: "values,formulas", tableMaxRows: 12, tableMaxCols: 8, maxChars: 6000 });
    const preview = await workbook.render({ sheetName: name, autoCrop: "all", scale: 1.4, format: "png" });
    const filename = path.join(outputDir, `${name}.png`);
    await fs.writeFile(filename, new Uint8Array(await preview.arrayBuffer()));
    rendered.push({ sheet: name, preview: filename, inspection: inspection.ndjson });
  }
  const errors = await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A", options: { useRegex: true, maxResults: 100 }, summary: "formula error scan" });
  console.log(JSON.stringify({ rendered, formulaErrors: errors.ndjson }, null, 2));
}

const args = parseArgs(process.argv.slice(2));
const commands = {
  "create-chinese": createChinese,
  "append-translations": appendTranslations,
  "read-chinese": readChinese,
  "read-translations": readTranslations,
  verify,
};

try {
  if (!commands[args.command]) throw new Error("命令必须是 create-chinese、append-translations、read-chinese、read-translations 或 verify");
  await commands[args.command](args);
} catch (error) {
  console.error(JSON.stringify({ ok: false, error: error?.message || String(error) }, null, 2));
  process.exitCode = 1;
}
