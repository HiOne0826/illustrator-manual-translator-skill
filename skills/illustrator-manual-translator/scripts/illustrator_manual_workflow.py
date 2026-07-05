#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


APP_BUNDLE_ID = "com.adobe.illustrator"
DEFAULT_FONT_CANDIDATES = [
    "PingFangSC-Regular",
    "STHeitiSC-Light",
    "STHeitiSC-Medium",
    "SongtiSC-Regular",
    "ArialUnicodeMS",
]


def js_string(value: str | Path) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def repo_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def find_illustrator_app() -> Path | None:
    apps = sorted(Path("/Applications").glob("Adobe Illustrator*/Adobe Illustrator.app"))
    return apps[-1] if apps else None


def run_jsx(jsx_path: Path) -> None:
    if platform.system() != "Darwin":
        raise SystemExit("当前脚本自动运行 Illustrator 仅支持 macOS。Windows 可手动在 Illustrator 中运行生成的 JSX。")
    cmd = [
        "osascript",
        "-e",
        f'tell application id "{APP_BUNDLE_ID}" to do javascript POSIX file "{jsx_path}"',
    ]
    result = run(cmd, check=False)
    if result.returncode != 0:
        raise SystemExit(
            "Illustrator JSX 执行失败。\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}\n"
            "如果是首次运行，请确认系统已允许当前终端/agent 控制 Adobe Illustrator。"
        )


def write_temp_jsx(content: str, out_dir: Path, name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=name, suffix=".jsx", dir=str(out_dir))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return Path(temp_name)


def command_doctor(args: argparse.Namespace) -> None:
    app = find_illustrator_app()
    rows = [
        ("OS", platform.platform()),
        ("Illustrator app", str(app) if app else "未找到"),
        ("osascript", shutil.which("osascript") or "未找到"),
        ("python", sys.executable),
    ]
    print("# Illustrator Manual Translator 环境检查")
    for key, value in rows:
        print(f"- {key}: {value}")
    if not app:
        raise SystemExit("未找到 Adobe Illustrator。请确认已安装并至少手动启动过一次。")


def export_jsx(source: Path, out_json: Path, out_md: Path) -> str:
    return f"""#target illustrator
(function () {{
  var sourcePath = {js_string(source)};
  var outJsonPath = {js_string(out_json)};
  var outMdPath = {js_string(out_md)};

  function jsonEscape(value) {{
    return String(value)
      .replace(/\\\\/g, "\\\\\\\\")
      .replace(/"/g, '\\\\"')
      .replace(/\\r/g, "\\\\n")
      .replace(/\\n/g, "\\\\n")
      .replace(/\\t/g, "\\\\t");
  }}
  function q(value) {{ return '"' + jsonEscape(value) + '"'; }}
  function writeText(path, content) {{
    var file = new File(path);
    file.encoding = "UTF-8";
    file.open("w");
    file.write(content);
    file.close();
  }}
  function arr(value) {{
    if (!value || value.length < 2) return "null";
    var out = [];
    for (var i = 0; i < value.length; i += 1) out.push(value[i]);
    return "[" + out.join(",") + "]";
  }}
  function kindName(kind) {{
    if (kind === TextType.POINTTEXT) return "POINTTEXT";
    if (kind === TextType.AREATEXT) return "AREATEXT";
    if (kind === TextType.PATHTEXT) return "PATHTEXT";
    return String(kind);
  }}

  app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;
  var source = new File(sourcePath);
  if (!source.exists) throw new Error("Source file not found: " + sourcePath);
  var doc = app.open(source);
  var json = "[\\n";
  var md = "# Illustrator TextFrame Export\\n\\n";
  md += "- Source: `" + sourcePath + "`\\n";
  md += "- TextFrame count: " + doc.textFrames.length + "\\n\\n";
  md += "| index | kind | layer | font | size | text |\\n";
  md += "| --- | --- | --- | --- | --- | --- |\\n";

  for (var i = 0; i < doc.textFrames.length; i += 1) {{
    var tf = doc.textFrames[i];
    var contents = tf.contents || "";
    var attrs = tf.textRange.characterAttributes;
    var fontName = "";
    var fontSize = "";
    try {{ fontName = attrs.textFont ? attrs.textFont.name : ""; }} catch (e) {{}}
    try {{ fontSize = attrs.size; }} catch (e2) {{}}
    json += "  {{\\n";
    json += '    "index": ' + i + ",\\n";
    json += '    "name": ' + q(tf.name || "") + ",\\n";
    json += '    "layer": ' + q(tf.layer ? tf.layer.name : "") + ",\\n";
    json += '    "kind": ' + q(kindName(tf.kind)) + ",\\n";
    json += '    "contents": ' + q(contents) + ",\\n";
    json += '    "length": ' + contents.length + ",\\n";
    json += '    "position": ' + arr(tf.position) + ",\\n";
    json += '    "geometricBounds": ' + arr(tf.geometricBounds) + ",\\n";
    json += '    "visibleBounds": ' + arr(tf.visibleBounds) + ",\\n";
    json += '    "fontName": ' + q(fontName) + ",\\n";
    json += '    "fontSize": ' + q(fontSize) + ",\\n";
    json += '    "locked": ' + (tf.locked ? "true" : "false") + ",\\n";
    json += '    "hidden": ' + (tf.hidden ? "true" : "false") + "\\n";
    json += "  }}" + (i === doc.textFrames.length - 1 ? "\\n" : ",\\n");

    var preview = contents.replace(/\\r/g, " / ").replace(/\\n/g, " / ");
    if (preview.length > 120) preview = preview.substring(0, 120) + "...";
    md += "| " + i + " | " + kindName(tf.kind) + " | " + (tf.layer ? tf.layer.name : "") + " | " + fontName + " | " + fontSize + " | " + preview.replace(/\\|/g, "\\\\|") + " |\\n";
  }}
  json += "]\\n";
  writeText(outJsonPath, json);
  writeText(outMdPath, md);
  doc.close(SaveOptions.DONOTSAVECHANGES);
}})();
"""


def command_export(args: argparse.Namespace) -> None:
    source = repo_path(args.source)
    out_dir = repo_path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json = out_dir / "textframes.json"
    out_md = out_dir / "textframes.md"
    jsx = write_temp_jsx(export_jsx(source, out_json, out_md), out_dir, "export-textframes-")
    run_jsx(jsx)
    print(out_json)
    print(out_md)


def command_template(args: argparse.Namespace) -> None:
    textframes = json.loads(repo_path(args.textframes).read_text(encoding="utf-8"))
    out_path = repo_path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "sourceTextframes": str(repo_path(args.textframes)),
        "targetLanguage": args.language,
        "notes": "Fill targetText for text that should be replaced. Leave targetText empty to keep original text.",
        "items": [
            {
                "index": item["index"],
                "sourceText": item["contents"],
                "targetText": "",
                "kind": item.get("kind", ""),
                "fontName": item.get("fontName", ""),
                "fontSize": item.get("fontSize", ""),
                "reviewStatus": "pending",
            }
            for item in textframes
            if item.get("contents", "").strip()
        ],
    }
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(out_path)


def apply_jsx(source: Path, out_ai: Path, out_pdf: Path, report: Path, replacements: dict[int, str], font_candidates: list[str]) -> str:
    replacements_js = "{\n" + ",\n".join(f"    {idx}: {js_string(text)}" for idx, text in sorted(replacements.items())) + "\n  }"
    fonts_js = "[" + ",".join(js_string(font) for font in font_candidates) + "]"
    return f"""#target illustrator
(function () {{
  var sourcePath = {js_string(source)};
  var outAiPath = {js_string(out_ai)};
  var outPdfPath = {js_string(out_pdf)};
  var reportPath = {js_string(report)};
  var replacements = {replacements_js};
  var fontCandidates = {fonts_js};

  function writeText(path, content) {{
    var file = new File(path);
    file.encoding = "UTF-8";
    file.open("w");
    file.write(content);
    file.close();
  }}
  function normalize(value) {{ return String(value || "").replace(/\\r/g, "\\n"); }}
  function findFont() {{
    for (var i = 0; i < fontCandidates.length; i += 1) {{
      try {{ return app.textFonts.getByName(fontCandidates[i]); }} catch (e) {{}}
    }}
    return null;
  }}

  app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;
  var source = new File(sourcePath);
  if (!source.exists) throw new Error("Source file not found: " + sourcePath);
  var doc = app.open(source);
  var font = findFont();
  var changed = [];
  var missing = [];

  for (var key in replacements) {{
    if (!replacements.hasOwnProperty(key)) continue;
    var index = Number(key);
    if (index >= 0 && index < doc.textFrames.length) {{
      var tf = doc.textFrames[index];
      var before = normalize(tf.contents);
      tf.contents = replacements[key];
      if (font) {{
        try {{ tf.textRange.characterAttributes.textFont = font; }} catch (e2) {{}}
      }}
      changed.push([index, before, replacements[key]]);
    }} else {{
      missing.push(index);
    }}
  }}

  var aiOptions = new IllustratorSaveOptions();
  aiOptions.pdfCompatible = true;
  doc.saveAs(new File(outAiPath), aiOptions);

  var pdfOptions = new PDFSaveOptions();
  pdfOptions.preserveEditability = true;
  doc.saveAs(new File(outPdfPath), pdfOptions);

  var md = "# Illustrator TextFrame Replacement Report\\n\\n";
  md += "- Source: `" + sourcePath + "`\\n";
  md += "- Output AI: `" + outAiPath + "`\\n";
  md += "- Output PDF: `" + outPdfPath + "`\\n";
  md += "- TextFrame count: " + doc.textFrames.length + "\\n";
  md += "- Changed count: " + changed.length + "\\n";
  md += "- Font: " + (font ? font.name : "not changed") + "\\n";
  md += "- Missing indexes: " + (missing.length ? missing.join(", ") : "none") + "\\n\\n";
  md += "| index | before | after |\\n";
  md += "| --- | --- | --- |\\n";
  for (var i = 0; i < changed.length; i += 1) {{
    var before = changed[i][1].replace(/\\n/g, " / ");
    var after = changed[i][2].replace(/\\n/g, " / ");
    if (before.length > 100) before = before.substring(0, 100) + "...";
    if (after.length > 100) after = after.substring(0, 100) + "...";
    md += "| " + changed[i][0] + " | " + before.replace(/\\|/g, "\\\\|") + " | " + after.replace(/\\|/g, "\\\\|") + " |\\n";
  }}
  writeText(reportPath, md);
  doc.close(SaveOptions.DONOTSAVECHANGES);
}})();
"""


def load_replacements(path: Path) -> dict[int, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items", data if isinstance(data, list) else [])
    replacements: dict[int, str] = {}
    for item in items:
        text = item.get("targetText", "")
        if text and str(text).strip():
            replacements[int(item["index"])] = str(text)
    return replacements


def command_apply(args: argparse.Namespace) -> None:
    source = repo_path(args.source)
    translations = repo_path(args.translations)
    out_dir = repo_path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.name or f"{source.stem}-translated"
    out_ai = out_dir / f"{stem}.ai"
    out_pdf = out_dir / f"{stem}.pdf"
    report = out_dir / "replace-report.md"
    font_candidates = [args.font] if args.font else DEFAULT_FONT_CANDIDATES
    replacements = load_replacements(translations)
    if not replacements:
        raise SystemExit("没有可写回的 targetText。请先填写 translations.json。")
    jsx = write_temp_jsx(apply_jsx(source, out_ai, out_pdf, report, replacements, font_candidates), out_dir, "apply-textframes-")
    run_jsx(jsx)
    print(out_ai)
    print(out_pdf)
    print(report)


def command_verify(args: argparse.Namespace) -> None:
    pdf = repo_path(args.pdf)
    out_dir = repo_path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / "verify-report.md"
    lines = ["# PDF Verification Report", "", f"- PDF: `{pdf}`", f"- Exists: {pdf.exists()}"]
    pdfinfo = os.environ.get("PDFINFO_BIN") or shutil.which("pdfinfo")
    if pdf.exists() and pdfinfo and Path(pdfinfo).exists():
        result = run([pdfinfo, str(pdf)], check=False)
        lines.append("")
        lines.append("## pdfinfo")
        lines.append("")
        lines.append("```text")
        lines.append(result.stdout.strip() or result.stderr.strip())
        lines.append("```")
    pdftoppm = os.environ.get("PDFTOPPM_BIN") or shutil.which("pdftoppm")
    if pdf.exists() and pdftoppm and Path(pdftoppm).exists():
        prefix = out_dir / "render-page"
        result = run([pdftoppm, "-png", "-r", "120", str(pdf), str(prefix)], check=False)
        lines.append("")
        lines.append("## Render")
        lines.append("")
        lines.append(f"- Renderer: `{pdftoppm}`")
        lines.append(f"- Exit code: {result.returncode}")
        lines.append(f"- Output prefix: `{prefix}`")
    else:
        lines.append("")
        lines.append("## Render")
        lines.append("")
        lines.append("- Skipped: `pdftoppm` not found. Set `PDFTOPPM_BIN` or install Poppler to render pages.")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(report)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Adobe Illustrator manual translation workflow helper")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Check local Illustrator automation environment").set_defaults(func=command_doctor)

    p_export = sub.add_parser("export", help="Export Illustrator TextFrame inventory")
    p_export.add_argument("--source", required=True)
    p_export.add_argument("--out-dir", required=True)
    p_export.set_defaults(func=command_export)

    p_template = sub.add_parser("template", help="Create a translation JSON template from textframes.json")
    p_template.add_argument("--textframes", required=True)
    p_template.add_argument("--out", required=True)
    p_template.add_argument("--language", default="zh-CN")
    p_template.set_defaults(func=command_template)

    p_apply = sub.add_parser("apply", help="Apply targetText values to a copied AI file and export PDF")
    p_apply.add_argument("--source", required=True)
    p_apply.add_argument("--translations", required=True)
    p_apply.add_argument("--out-dir", required=True)
    p_apply.add_argument("--name")
    p_apply.add_argument("--font")
    p_apply.set_defaults(func=command_apply)

    p_verify = sub.add_parser("verify", help="Verify and optionally render exported PDF")
    p_verify.add_argument("--pdf", required=True)
    p_verify.add_argument("--out-dir", required=True)
    p_verify.set_defaults(func=command_verify)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
