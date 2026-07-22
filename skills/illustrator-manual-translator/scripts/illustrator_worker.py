#!/usr/bin/env python3
"""Illustrator template v2 extractor, renderer, and remote worker client."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import json
import os
import platform
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


APP_BUNDLE_ID = "com.adobe.illustrator"
TEMPLATE_SCHEMA = "illustrator-template/v2"
DRIVER_PROTOCOL = "illustrator-driver/v1"
WORKER_RESULT_SCHEMA = "illustrator-worker-result/v1"
WORKER_VERSION = "2.1"
DEFAULT_TIMEOUT = 30.0
CODEX_RUNTIME_BIN = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override"


class WorkerError(RuntimeError):
    """A user-facing worker, automation, or protocol failure."""


class IndeterminateWorkerError(WorkerError):
    """The wrapper stopped, but Illustrator may still be executing."""


def find_binary(name: str) -> str | None:
    discovered = shutil.which(name)
    if discovered:
        return discovered
    bundled = CODEX_RUNTIME_BIN / name
    return str(bundled) if bundled.is_file() else None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_path(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve()


def jsx_string(value: str | os.PathLike[str]) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def source_metadata(source: Path) -> dict[str, Any]:
    stat = source.stat()
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": str(source),
        "name": source.name,
        "size": stat.st_size,
        "modifiedAt": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
        "sha256": digest.hexdigest(),
    }


def validate_source(source: Path) -> None:
    if not source.is_file():
        raise WorkerError(f"Source file not found: {source}")
    if source.suffix.lower() not in {".ai", ".ait", ".eps", ".pdf"}:
        raise WorkerError(f"Unsupported Illustrator source type: {source.suffix or '<none>'}")


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def validate_output(source: Path, output: Path, *, overwrite: bool = False, allowed_root: Path | None = None) -> None:
    if source.resolve() == output.resolve():
        raise WorkerError(f"Refusing to overwrite source file: {source}")
    if allowed_root is not None and not path_is_within(output, allowed_root):
        raise WorkerError(f"Output escapes allowed root {allowed_root}: {output}")
    if output.parent.is_symlink():
        raise WorkerError(f"Output parent must not be a symlink: {output.parent}")
    if output.exists() and not overwrite:
        raise WorkerError(f"Output already exists (set overwrite=true to replace it): {output}")


def raster_dimensions(path: Path) -> tuple[int, int] | None:
    """Read common raster dimensions without adding an imaging dependency."""
    suffix = path.suffix.lower()
    try:
        with path.open("rb") as handle:
            header = handle.read(32)
            if suffix == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n"):
                return struct.unpack(">II", header[16:24])
            if suffix == ".gif" and header[:6] in {b"GIF87a", b"GIF89a"}:
                return struct.unpack("<HH", header[6:10])
            if suffix == ".bmp" and header.startswith(b"BM"):
                width, height = struct.unpack("<ii", header[18:26])
                return abs(width), abs(height)
            if suffix in {".jpg", ".jpeg"} and header.startswith(b"\xff\xd8"):
                handle.seek(2)
                while True:
                    marker_start = handle.read(1)
                    if not marker_start:
                        break
                    if marker_start != b"\xff":
                        continue
                    marker = handle.read(1)
                    while marker == b"\xff":
                        marker = handle.read(1)
                    if marker in {b"\xd8", b"\xd9"}:
                        continue
                    length_bytes = handle.read(2)
                    if len(length_bytes) != 2:
                        break
                    segment_length = struct.unpack(">H", length_bytes)[0]
                    if marker and marker[0] in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                        payload = handle.read(5)
                        if len(payload) == 5:
                            height, width = struct.unpack(">HH", payload[1:5])
                            return width, height
                        break
                    handle.seek(max(0, segment_length - 2), os.SEEK_CUR)
    except (OSError, struct.error, ValueError):
        return None
    return None


def asset_dpi_issue(binding: Mapping[str, Any], path: Path) -> dict[str, Any] | None:
    minimum = float(binding.get("minDpi") or 0)
    if minimum <= 0 or str(binding.get("fit") or "contain") == "hide":
        return None
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".bmp"}:
        return None
    width = int(binding.get("width") or 0)
    height = int(binding.get("height") or 0)
    if width <= 0 or height <= 0:
        dimensions = raster_dimensions(path)
        if dimensions:
            width, height = dimensions
    slot = binding.get("bounds") or []
    if width <= 0 or height <= 0 or not isinstance(slot, Sequence) or len(slot) != 4:
        return {
            "level": "blocking",
            "type": "asset_dpi_unavailable",
            "message": f"Cannot verify effective DPI for asset slot {binding.get('slotId')}",
            "status": "open",
        }
    slot_width = abs(float(slot[2]) - float(slot[0]))
    slot_height = abs(float(slot[1]) - float(slot[3]))
    if slot_width <= 0 or slot_height <= 0:
        raise WorkerError(f"Asset slot {binding.get('slotId')} has invalid bounds")
    fit = str(binding.get("fit") or "contain")
    scale_x = slot_width / width
    scale_y = slot_height / height
    if fit == "contain":
        points_per_pixel = min(scale_x, scale_y)
    elif fit == "cover":
        points_per_pixel = max(scale_x, scale_y)
    else:
        points_per_pixel = max(scale_x, scale_y)
    effective = 72.0 / points_per_pixel
    if effective + 0.01 < minimum:
        return {
            "level": "blocking",
            "type": "asset_low_dpi",
            "message": f"Asset slot {binding.get('slotId')} effective DPI {effective:.1f} is below {minimum:.0f}",
            "status": "open",
            "metadata": {"effectiveDpi": round(effective, 1), "minimumDpi": minimum, "pixels": [width, height]},
        }
    return None


def qa_quality_issues(qa_data: Mapping[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    mappings = (
        ("oversetTextFrames", "blocking", "text_overflow", "Overset TextFrames"),
        ("outOfSafeBoundsDetails", "blocking", "text_out_of_safe_bounds", "Text outside safe bounds"),
        ("layoutViolations", "blocking", "layout_rule_violation", "Layout rule violations"),
        ("unresolvedNamedObjects", "warning", "layout_binding_missing", "Unresolved named objects"),
        ("fontFallbacks", "blocking", "font_fallback", "Configured fonts were unavailable"),
        ("continuationRequests", "blocking", "layout_continuation_required", "New artboard required"),
    )
    for key, level, issue_type, label in mappings:
        if qa_data.get(key):
            issues.append({"level": level, "type": issue_type, "message": f"{label}: {qa_data[key]}", "status": "open"})
    return issues


def find_illustrator_app() -> Path | None:
    candidates = sorted(Path("/Applications").glob("Adobe Illustrator*/Adobe Illustrator.app"))
    return candidates[-1] if candidates else None


def _run(
    command: Sequence[str],
    *,
    timeout: float | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            list(command),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        seconds = timeout if timeout is not None else "unknown"
        raise IndeterminateWorkerError(f"Command timed out after {seconds}s; Illustrator state is unknown") from exc
    except OSError as exc:
        raise WorkerError(f"Command failed to start: {' '.join(command)}: {exc}") from exc


class IllustratorDriver:
    """Thin execution boundary; rendering and QA stay in the worker."""

    driver_id = "unknown"

    def execute_jsx(self, jsx_path: Path, *, timeout: float = 600.0) -> dict[str, Any]:
        raise NotImplementedError

    def doctor(self, *, probe: bool = False) -> dict[str, Any]:
        raise NotImplementedError


class AppleScriptDriver(IllustratorDriver):
    """macOS-only Illustrator automation through Apple Events."""

    driver_id = "macos-osascript"

    def __init__(
        self,
        *,
        system: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.system = system or platform.system()
        self.runner = runner

    def _command(self, jsx_path: Path) -> list[str]:
        if self.system != "Darwin":
            raise WorkerError(f"Illustrator automation requires macOS; current platform is {self.system}")
        osascript = shutil.which("osascript")
        if not osascript:
            raise WorkerError("osascript not found; macOS Illustrator automation is unavailable")
        return [
            osascript,
            "-e",
            f'tell application id "{APP_BUNDLE_ID}" to do javascript POSIX file {jsx_string(jsx_path)}',
        ]

    def execute_jsx(self, jsx_path: Path, *, timeout: float = 600.0) -> dict[str, Any]:
        resolved = resolve_path(jsx_path)
        if not resolved.is_file() or resolved.suffix.lower() != ".jsx":
            raise WorkerError(f"JSX file not found or invalid: {resolved}")
        command = self._command(resolved)
        started = time.monotonic()
        result = _run(command, timeout=timeout, runner=self.runner)
        duration_ms = round((time.monotonic() - started) * 1000)
        if result.returncode != 0:
            details = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
            raise WorkerError(f"Illustrator JSX execution failed (exit {result.returncode}): {details}")
        return {
            "command": command,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "durationMs": duration_ms,
            "driver": {"id": self.driver_id, "protocolVersion": DRIVER_PROTOCOL},
        }

    def doctor(self, *, probe: bool = False) -> dict[str, Any]:
        report: dict[str, Any] = {
            "ok": False,
            "driver": {"id": self.driver_id, "protocolVersion": DRIVER_PROTOCOL},
            "installed": False,
            "automationAvailable": False,
            "checks": {},
        }
        checks = report["checks"]
        if self.system != "Darwin":
            checks["error"] = f"macOS is required; current platform is {self.system}"
            return report
        app = find_illustrator_app()
        osascript = shutil.which("osascript")
        checks.update({"illustratorApp": str(app) if app else None, "osascript": osascript})
        report["installed"] = bool(app)
        report["automationAvailable"] = bool(osascript)
        report["ok"] = bool(app and osascript)
        if probe and report["ok"]:
            with tempfile.TemporaryDirectory(prefix="illustrator-doctor-") as temp:
                probe_jsx = Path(temp) / "probe.jsx"
                probe_jsx.write_text('#target illustrator\n(function () { return "illustrator:" + app.version; }());\n', encoding="utf-8")
                try:
                    result = self.execute_jsx(probe_jsx, timeout=30)
                    checks["automationProbe"] = {
                        "ok": True,
                        "result": result.get("stdout") or None,
                        "durationMs": result.get("durationMs"),
                    }
                except WorkerError as exc:
                    checks["automationProbe"] = {"ok": False, "error": str(exc)}
                    report["ok"] = False
        return report


def default_driver(
    *,
    system: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> IllustratorDriver:
    return AppleScriptDriver(system=system, runner=runner)


@contextmanager
def illustrator_execution_lock() -> Any:
    """Serialize all local Illustrator mutations across projects and workers."""
    lock_path = Path(tempfile.gettempdir()) / "illustrator-manual-translator.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_jsx(
    jsx_path: Path,
    *,
    system: str | None = None,
    timeout: float = 600.0,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    driver: IllustratorDriver | None = None,
) -> dict[str, Any]:
    """Execute a generated JSX file through the selected macOS driver."""
    selected = driver or default_driver(system=system, runner=runner)
    with illustrator_execution_lock():
        return selected.execute_jsx(jsx_path, timeout=timeout)


def write_jsx(content: str, directory: Path, prefix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=".jsx", dir=str(directory))
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    return Path(name)


def build_extract_template_jsx(source: Path, output: Path, preview: Path, source_info: Mapping[str, Any]) -> str:
    source_json = json.dumps(dict(source_info), ensure_ascii=False, separators=(",", ":"))
    return fr'''#target illustrator
(function () {{
  var sourcePath = {jsx_string(source)};
  var outputPath = {jsx_string(output)};
  var previewPath = {jsx_string(preview)};
  var sourceMetadata = {source_json};

  function esc(value) {{
    return String(value === undefined || value === null ? "" : value)
      .replace(/\\/g, "\\\\").replace(/"/g, '\\"')
      .replace(/\r/g, "\\r").replace(/\n/g, "\\n").replace(/\t/g, "\\t");
  }}
  function q(value) {{ return '"' + esc(value) + '"'; }}
  function arr(value) {{
    if (!value) return "null";
    var parts = [];
    for (var i = 0; i < value.length; i += 1) parts.push(Number(value[i]));
    return "[" + parts.join(",") + "]";
  }}
  function bool(value) {{ return value ? "true" : "false"; }}
  function isoNow() {{
    var value = new Date();
    function pad(number) {{ return number < 10 ? "0" + number : String(number); }}
    return value.getUTCFullYear() + "-" + pad(value.getUTCMonth() + 1) + "-" + pad(value.getUTCDate()) +
      "T" + pad(value.getUTCHours()) + ":" + pad(value.getUTCMinutes()) + ":" + pad(value.getUTCSeconds()) + "Z";
  }}
  function write(path, content) {{
    var file = new File(path); file.encoding = "UTF-8";
    if (!file.open("w")) throw new Error("Cannot write metadata: " + path);
    file.write(content); file.close();
  }}
  function textKind(value) {{
    if (value === TextType.POINTTEXT) return "POINTTEXT";
    if (value === TextType.AREATEXT) return "AREATEXT";
    if (value === TextType.PATHTEXT) return "PATHTEXT";
    return String(value);
  }}
  function itemBase(item, index) {{
    return '"index":' + index + ',"name":' + q(item.name || "") +
      ',"layer":' + q(item.layer ? item.layer.name : "") +
      ',"geometricBounds":' + arr(item.geometricBounds) +
      ',"visibleBounds":' + arr(item.visibleBounds) +
      ',"locked":' + bool(item.locked) + ',"hidden":' + bool(item.hidden);
  }}

  app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;
  var source = new File(sourcePath);
  if (!source.exists) throw new Error("Source file not found: " + sourcePath);
  var doc = app.open(source);
  try {{
    var artboards = [];
    for (var a = 0; a < doc.artboards.length; a += 1) {{
      var board = doc.artboards[a];
      artboards.push('{{"index":' + a + ',"name":' + q(board.name || "") +
        ',"artboardRect":' + arr(board.artboardRect) + '}}');
    }}
    var textFrames = [];
    for (var t = 0; t < doc.textFrames.length; t += 1) {{
      var tf = doc.textFrames[t], attrs = tf.textRange.characterAttributes;
      var fontName = "", fontSize = null;
      try {{ fontName = attrs.textFont ? attrs.textFont.name : ""; }} catch (fontError) {{}}
      try {{ fontSize = Number(attrs.size); }} catch (sizeError) {{}}
      textFrames.push('{{' + itemBase(tf, t) + ',"kind":' + q(textKind(tf.kind)) +
        ',"contents":' + q(tf.contents || "") + ',"position":' + arr(tf.position) +
        ',"fontName":' + q(fontName) + ',"fontSize":' + (fontSize === null ? "null" : fontSize) + '}}');
    }}
    var placedItems = [];
    for (var p = 0; p < doc.placedItems.length; p += 1) {{
      var pi = doc.placedItems[p], filePath = "";
      try {{ filePath = pi.file ? pi.file.fsName : ""; }} catch (fileError) {{}}
      placedItems.push('{{' + itemBase(pi, p) + ',"file":' + q(filePath) +
        ',"embedded":' + bool(pi.embedded) + '}}');
    }}
    var rasterItems = [];
    for (var r = 0; r < doc.rasterItems.length; r += 1) {{
      var ri = doc.rasterItems[r];
      rasterItems.push('{{' + itemBase(ri, r) + ',"width":' + Number(ri.width) +
        ',"height":' + Number(ri.height) + ',"embedded":' + bool(ri.embedded) + '}}');
    }}
    var groupItems = [];
    for (var g = 0; g < doc.groupItems.length; g += 1) {{
      var gi = doc.groupItems[g], parentType = "", parentName = "";
      try {{ parentType = gi.parent ? gi.parent.typename : ""; }} catch (parentTypeError) {{}}
      try {{ parentName = gi.parent && gi.parent.name ? gi.parent.name : ""; }} catch (parentNameError) {{}}
      groupItems.push('{{' + itemBase(gi, g) + ',"clipped":' + bool(gi.clipped) +
        ',"childCount":' + Number(gi.pageItems.length) + ',"parentType":' + q(parentType) +
        ',"parentName":' + q(parentName) + '}}');
    }}
    var layoutPathItems = [];
    for (var lp = 0; lp < doc.pathItems.length; lp += 1) {{
      var lpi = doc.pathItems[lp], lpParentType = "", lpParentName = "";
      try {{ lpParentType = lpi.parent ? lpi.parent.typename : ""; }} catch (lpParentTypeError) {{}}
      if (lpParentType !== "Layer") continue;
      var lpb = lpi.geometricBounds;
      var lpWidth = Number(lpb[2]) - Number(lpb[0]), lpHeight = Number(lpb[1]) - Number(lpb[3]);
      if (lpWidth < 80 || lpHeight < 20 || lpHeight > 180) continue;
      try {{ lpParentName = lpi.parent && lpi.parent.name ? lpi.parent.name : ""; }} catch (lpParentNameError) {{}}
      layoutPathItems.push('{{' + itemBase(lpi, lp) + ',"parentType":' + q(lpParentType) +
        ',"parentName":' + q(lpParentName) + '}}');
    }}
    var compoundPathItems = [];
    for (var cp = 0; cp < doc.compoundPathItems.length; cp += 1) {{
      var cpi = doc.compoundPathItems[cp], cpParentType = "", cpParentName = "";
      try {{ cpParentType = cpi.parent ? cpi.parent.typename : ""; }} catch (cpParentTypeError) {{}}
      if (cpParentType !== "Layer") continue;
      try {{ cpParentName = cpi.parent && cpi.parent.name ? cpi.parent.name : ""; }} catch (cpParentNameError) {{}}
      compoundPathItems.push('{{' + itemBase(cpi, cp) + ',"parentType":' + q(cpParentType) +
        ',"parentName":' + q(cpParentName) + '}}');
    }}

    var previewError = "", previewItems = [];
    try {{
      for (var previewIndex = 0; previewIndex < doc.artboards.length; previewIndex += 1) {{
        doc.artboards.setActiveArtboardIndex(previewIndex);
        var pagePath = previewPath.replace(/\.png$/i, "-" + (previewIndex + 1) + ".png");
        var options = new ExportOptionsPNG24();
        options.antiAliasing = true; options.artBoardClipping = true;
        options.horizontalScale = 50; options.verticalScale = 50; options.transparency = true;
        doc.exportFile(new File(pagePath), ExportType.PNG24, options);
        previewItems.push('{{"artboard":' + previewIndex + ',"path":' + q(pagePath) +
          ',"exists":' + bool(new File(pagePath).exists) + '}}');
      }}
    }} catch (previewException) {{ previewError = String(previewException); }}

    var json = '{{"schema":{jsx_string(TEMPLATE_SCHEMA)},"createdAt":' + q(isoNow()) +
      ',"source":' + {jsx_string(source_json)} +
      ',"document":{{"name":' + q(doc.name) + ',"colorSpace":' + q(doc.documentColorSpace) +
      ',"width":' + Number(doc.width) + ',"height":' + Number(doc.height) +
      ',"artboardCount":' + doc.artboards.length + ',"textFrameCount":' + doc.textFrames.length +
      ',"placedItemCount":' + doc.placedItems.length + ',"rasterItemCount":' + doc.rasterItems.length +
      ',"groupItemCount":' + doc.groupItems.length + '}}' +
      ',"artboards":[' + artboards.join(",") + ']' +
      ',"textFrames":[' + textFrames.join(",") + ']' +
      ',"placedItems":[' + placedItems.join(",") + ']' +
      ',"rasterItems":[' + rasterItems.join(",") + ']' +
      ',"groupItems":[' + groupItems.join(",") + ']' +
      ',"compoundPathItems":[' + compoundPathItems.join(",") + ']' +
      ',"layoutPathItems":[' + layoutPathItems.join(",") + ']' +
      ',"preview":{{"path":' + (previewItems.length ? q(previewPath.replace(/\.png$/i, "-1.png")) : q(previewPath)) +
      ',"format":"png","scalePercent":50,"exists":' + bool(previewItems.length > 0) +
      ',"pages":[' + previewItems.join(",") + '],"error":' + q(previewError) + '}}}}';
    write(outputPath, json + "\n");
  }} finally {{
    doc.close(SaveOptions.DONOTSAVECHANGES);
  }}
}})();
'''


def extract_template(
    source: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    *,
    execute: bool = True,
    overwrite: bool = False,
    system: str | None = None,
    driver: IllustratorDriver | None = None,
) -> dict[str, Any]:
    source_path = resolve_path(source)
    validate_source(source_path)
    directory = resolve_path(output_dir)
    output = directory / "template.v2.json"
    preview = directory / "template-preview.png"
    validate_output(source_path, output, overwrite=overwrite)
    validate_output(source_path, preview, overwrite=overwrite)
    directory.mkdir(parents=True, exist_ok=True)
    jsx_path = write_jsx(build_extract_template_jsx(source_path, output, preview, source_metadata(source_path)), directory, "extract-template-")
    result: dict[str, Any] = {"jsx": str(jsx_path), "template": str(output), "preview": str(preview), "executed": False}
    if execute:
        result["automation"] = run_jsx(jsx_path, system=system, driver=driver)
        result["executed"] = True
        if not output.is_file():
            raise WorkerError(f"Illustrator completed without creating template metadata: {output}")
        result["metadata"] = json.loads(output.read_text(encoding="utf-8-sig"))
        artifacts = [{"type": "template_metadata", "path": str(output), "sha256": source_metadata(output)["sha256"], "metadata": {"bytes": output.stat().st_size}}]
        for item in (result["metadata"].get("preview") or {}).get("pages") or []:
            page = Path(str(item.get("path") or ""))
            if page.is_file():
                artifacts.append({"type": "template_preview", "path": str(page), "sha256": source_metadata(page)["sha256"], "metadata": {"bytes": page.stat().st_size, "artboard": item.get("artboard")}})
        result["artifacts"] = artifacts
    return result


def _job_replacements(job: Mapping[str, Any]) -> dict[int, str]:
    raw = job.get("replacements")
    replacements: dict[int, str] = {}
    if isinstance(raw, Mapping):
        replacements.update({int(key): str(value) for key, value in raw.items() if value is not None})
    items = job.get("items") or job.get("translations") or []
    if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
        for item in items:
            if not isinstance(item, Mapping) or "index" not in item:
                continue
            if item.get("objectType") == "layoutText":
                continue
            value = item.get("targetText", item.get("text"))
            if value is not None and (str(value).strip() or item.get("remove")):
                replacements[int(item["index"])] = str(value)
    return replacements


def _job_layout_texts(job: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for item in job.get("items") or job.get("translations") or []:
        if not isinstance(item, Mapping) or item.get("objectType") != "layoutText" or "index" not in item:
            continue
        result[int(item["index"])] = {
            "text": str(item.get("targetText", item.get("text", ""))),
            "fontName": item.get("fontName"),
            "fontSize": item.get("fontSize"),
            "slotId": item.get("slotId"),
        }
    return result


def _job_guards(job: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    guards: dict[int, dict[str, Any]] = {}
    for item in job.get("items") or job.get("translations") or []:
        if isinstance(item, Mapping) and "index" in item:
            guards[int(item["index"])] = {
                "expectedSourceText": item.get("expectedSourceText"),
                "bounds": item.get("bounds"),
                "slotId": item.get("slotId"),
                "locator": item.get("locator") or {},
            }
    return guards


def _job_styles(job: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    styles: dict[int, dict[str, Any]] = {}
    for item in job.get("items") or job.get("translations") or []:
        if not isinstance(item, Mapping) or "index" not in item:
            continue
        style = {"fontName": item.get("fontName"), "fontSize": item.get("fontSize")}
        if style["fontName"] or style["fontSize"] is not None:
            styles[int(item["index"])] = style
    return styles


def build_render_jsx(source: Path, outputs: Mapping[str, Path], replacements: Mapping[int, str], guards: Mapping[int, Mapping[str, Any]] | None = None, styles: Mapping[int, Mapping[str, Any]] | None = None, asset_bindings: Sequence[Mapping[str, Any]] | None = None, layout_rules: Mapping[str, Any] | None = None, qa_path: Path | None = None, preview_artboard: int = 0, layout_texts: Mapping[int, Mapping[str, Any]] | None = None) -> str:
    replacements_json = json.dumps({str(key): value for key, value in replacements.items()}, ensure_ascii=False)
    guards_json = json.dumps({str(key): value for key, value in (guards or {}).items()}, ensure_ascii=False)
    styles_json = json.dumps({str(key): value for key, value in (styles or {}).items()}, ensure_ascii=False)
    assets_json = json.dumps(list(asset_bindings or []), ensure_ascii=False)
    layout_json = json.dumps(dict(layout_rules or {}), ensure_ascii=False)
    layout_text_json = json.dumps({str(key): value for key, value in (layout_texts or {}).items()}, ensure_ascii=False)
    output_json = json.dumps({key: str(value) for key, value in outputs.items()}, ensure_ascii=False)
    qa_json = json.dumps(str(qa_path) if qa_path else "", ensure_ascii=False)
    return f'''#target illustrator
(function () {{
  var sourcePath = {jsx_string(source)};
  var outputs = {output_json};
  var replacements = {replacements_json};
  var guards = {guards_json};
  var styles = {styles_json};
  var assetBindings = {assets_json};
  var layoutRules = {layout_json};
  var layoutTextReplacements = {layout_text_json};
  var sourceTextFrames = [], sourceGroupItems = [], sourcePathItems = [], sourceCompoundPathItems = [];
  var preservedBodyFontIndexes = {{}}, templateBodyFontSizes = {{}};
  var qaPath = {qa_json};
  var previewArtboard = {int(preview_artboard)};
  // Illustrator may preserve leading indentation inside a text frame after a
  // cosmetic edit. Guard the meaningful text, while still rejecting content
  // changes that could point at the wrong object.
  function normalize(value) {{ return String(value || "").replace(/\\r/g, "\\n").replace(/\u00a0/g, " ").replace(/^\\s+|\\s+$/g, ""); }}
  function jsonString(value) {{ return '"' + String(value || "").replace(/\\\\/g, "\\\\\\\\").replace(/"/g, '\\"').replace(/\\r/g, "\\\\r").replace(/\\n/g, "\\\\n") + '"'; }}
  function refsByType(region, type) {{
    var refs = [], source = region && region.objectRefs ? region.objectRefs : [];
    for (var i = 0; i < source.length; i += 1) if (source[i].type === type) refs.push(source[i].value);
    return refs;
  }}
  function collectPreservedBodyFontIndexes() {{
    var result = {{}}, policy = layoutRules && layoutRules.typographyPolicy ? layoutRules.typographyPolicy : {{}};
    if (policy.bodyFontSize !== "preserve-template" && policy.bodyFontSize !== "fixed-template-standard") return result;
    var pages = layoutRules && layoutRules.pages ? layoutRules.pages : [];
    for (var p = 0; p < pages.length; p += 1) {{
      var regions = pages[p].regions || [];
      for (var r = 0; r < regions.length; r += 1) {{
        var textRefs = refsByType(regions[r], "textFrame"), behavior = regions[r].behavior || {{}}, positions = behavior.bodyTextFramePositions || [];
        for (var i = 0; i < positions.length; i += 1) {{
          var position = Number(positions[i]);
          if (position >= 0 && position < textRefs.length) result[String(Number(textRefs[position]))] = true;
        }}
      }}
    }}
    return result;
  }}
  function bodyFontIsLocked(index) {{ return preservedBodyFontIndexes[String(Number(index))] === true; }}
  function applyBodyTypographyToFrame(frame, behavior, label, adjustments) {{
    var policy = layoutRules && layoutRules.typographyPolicy ? layoutRules.typographyPolicy : {{}}, options = behavior || {{}};
    var fixed = policy.bodyFontSize === "fixed-template-standard";
    var size = Number(options.bodyFontSizePt || (fixed ? policy.bodyFontSizePt : 0));
    var leading = Number(options.bodyLeadingPt || (fixed ? policy.bodyLeadingPt : 0));
    try {{
      if (size > 0) frame.textRange.characterAttributes.size = size;
      if (leading > 0) frame.textRange.characterAttributes.leading = leading;
      if (size > 0 || leading > 0) adjustments.push(label + ":typography-" + (size || "template") + "pt-" + (leading || "auto") + "leading");
    }} catch (typographyError) {{}}
  }}
  function applyStandardBodyTypography(adjustments) {{
    var pages = layoutRules && layoutRules.pages ? layoutRules.pages : [];
    for (var p = 0; p < pages.length; p += 1) {{
      var regions = pages[p].regions || [];
      for (var r = 0; r < regions.length; r += 1) {{
        var behavior = regions[r].behavior || {{}}, refs = refsByType(regions[r], "textFrame"), positions = behavior.bodyTextFramePositions || [];
        for (var i = 0; i < positions.length; i += 1) {{
          var position = Number(positions[i]);
          if (position >= 0 && position < refs.length) applyBodyTypographyToFrame(textAt(Number(refs[position])), behavior, regions[r].id + "-body", adjustments);
        }}
      }}
    }}
  }}
  function restoreTemplateBodyFontSizes(adjustments) {{
    var policy = layoutRules && layoutRules.typographyPolicy ? layoutRules.typographyPolicy : {{}};
    if (policy.bodyFontSize !== "preserve-template") return;
    for (var key in templateBodyFontSizes) {{
      if (!templateBodyFontSizes.hasOwnProperty(key)) continue;
      var index = Number(key), expected = Number(templateBodyFontSizes[key]);
      if (index < 0 || index >= sourceTextFrames.length || !isFinite(expected)) continue;
      var current = Number(textAt(index).textRange.characterAttributes.size || expected);
      if (Math.abs(current - expected) > 0.01) {{
        textAt(index).textRange.characterAttributes.size = expected;
        adjustments.push("text-frame-" + index + ":template-font-size-restored-" + expected);
      }}
    }}
  }}
  function textAt(index) {{ return sourceTextFrames[Number(index)]; }}
  function groupAt(index) {{ return sourceGroupItems[Number(index)]; }}
  function pathAt(index) {{ return sourcePathItems[Number(index)]; }}
  function compoundAt(index) {{ return sourceCompoundPathItems[Number(index)]; }}
  function findNamedItem(name) {{
    for (var i = 0; i < doc.pageItems.length; i += 1) if (String(doc.pageItems[i].name || "") === String(name)) return doc.pageItems[i];
    return null;
  }}
  function lineCount(value) {{ return String(value || "").replace(/\\n/g, "\\r").split("\\r").length; }}
  function compactLength(value) {{ return String(value || "").replace(/\\s+/g, "").length; }}
  function hasTextOverflow(frame) {{
    try {{
      var previous = frame.previousFrame, next = frame.nextFrame;
      if ((previous && previous !== frame) || (next && next !== frame)) return frame.overflows === true;
    }} catch (threadError) {{}}
    try {{ if (typeof frame.overflows === "boolean") return frame.overflows; }} catch (overflowError) {{}}
    var visible = "";
    try {{ for (var i = 0; i < frame.lines.length; i += 1) visible += String(frame.lines[i].contents || ""); }} catch (lineError) {{ return true; }}
    return compactLength(visible) < compactLength(frame.contents);
  }}
  function isFixedPageNumberFrame(frame) {{
    try {{ if (String(frame.name || "") === "__layout_fixed_page_number__") return true; }} catch (nameError) {{}}
    try {{ if (/^\\s*-\\s*\\d+\\s*-\\s*$/.test(String(frame.contents || ""))) return true; }} catch (pageNumberError) {{}}
    var pages = layoutRules && layoutRules.pages ? layoutRules.pages : [];
    for (var p = 0; p < pages.length; p += 1) {{
      var regions = pages[p].regions || [];
      for (var r = 0; r < regions.length; r += 1) {{
        if (regions[r].role !== "fixed-page-number") continue;
        var refs = refsByType(regions[r], "textFrame");
        for (var i = 0; i < refs.length; i += 1) if (sourceTextFrames[Number(refs[i])] === frame) return true;
      }}
    }}
    return false;
  }}
  function markFixedPageNumberFrames() {{
    var pages = layoutRules && layoutRules.pages ? layoutRules.pages : [];
    for (var p = 0; p < pages.length; p += 1) {{
      var regions = pages[p].regions || [];
      for (var r = 0; r < regions.length; r += 1) {{
        if (regions[r].role !== "fixed-page-number") continue;
        var refs = refsByType(regions[r], "textFrame");
        for (var i = 0; i < refs.length; i += 1) {{
          try {{ sourceTextFrames[Number(refs[i])].name = "__layout_fixed_page_number__"; }} catch (markError) {{}}
        }}
      }}
    }}
  }}
  function wrapAtHalf(value) {{
    var text = normalize(value).replace(/\\n/g, " ");
    if (!text || text.indexOf(" ") < 0) {{
      var middle = Math.ceil(text.length / 2);
      return text.substring(0, middle) + "\\r" + text.substring(middle);
    }}
    var words = text.split(/\\s+/), best = 1, bestDelta = 999999;
    for (var i = 1; i < words.length; i += 1) {{
      var left = words.slice(0, i).join(" "), right = words.slice(i).join(" ");
      var delta = Math.abs(left.length - right.length);
      if (delta < bestDelta) {{ best = i; bestDelta = delta; }}
    }}
    return words.slice(0, best).join(" ") + "\\r" + words.slice(best).join(" ");
  }}
  function fitPointText(index, maxLines, minSize, maxWidth, label, adjustments, violations, behavior) {{
    if (index < 0 || index >= sourceTextFrames.length) {{ violations.push(label + ":missing-text-frame-" + index); return; }}
    var frame = textAt(index), attrs = frame.textRange.characterAttributes, options = behavior || {{}};
    var originalText = String(frame.contents || ""), size = Number(attrs.size || 12);
    if (maxLines === 1) frame.contents = normalize(originalText).replace(/\\n/g, " ");
    else if (maxLines === 2 && lineCount(originalText) === 1) frame.contents = wrapAtHalf(originalText);
    try {{ app.redraw(); }} catch (redrawError) {{}}
    attrs = frame.textRange.characterAttributes;
    size = Number(attrs.size || size || 12);
    if (size < minSize) {{ attrs.size = minSize; size = minSize; adjustments.push(label + ":font-raised-to-" + minSize); }}
    function measuredWidth() {{
      var geometric = Number(frame.geometricBounds[2]) - Number(frame.geometricBounds[0]);
      var scale = 100;
      try {{ scale = Number(attrs.horizontalScale || 100); }} catch (measureScaleError) {{}}
      var parts = String(frame.contents || "").replace(/\\n/g, "\\r").split("\\r"), longest = 0;
      for (var partIndex = 0; partIndex < parts.length; partIndex += 1) longest = Math.max(longest, compactLength(parts[partIndex]));
      return Math.max(geometric, longest * size * 0.66 * scale / 100);
    }}
    var width = measuredWidth();
    while (width > maxWidth + 0.5 && size > minSize) {{
      size = Math.max(minSize, size - 0.5); attrs.size = size;
      try {{ app.redraw(); }} catch (redrawSizeError) {{}}
      width = measuredWidth();
    }}
    var minHorizontalScale = Number(options.titleMinHorizontalScale || 70), horizontalScale = 100;
    try {{ horizontalScale = Number(attrs.horizontalScale || 100); }} catch (scaleReadError) {{}}
    while (width > maxWidth + 0.5 && horizontalScale > minHorizontalScale) {{
      horizontalScale = Math.max(minHorizontalScale, horizontalScale - 1);
      try {{ attrs.horizontalScale = horizontalScale; }} catch (scaleWriteError) {{ break; }}
      try {{ app.redraw(); }} catch (redrawScaleError) {{}}
      width = measuredWidth();
    }}
    if (horizontalScale < 99.5) adjustments.push(label + ":horizontal-scale-" + horizontalScale);
    if (width > maxWidth + 0.5 && options.titleAllowWrap && maxLines > 1 && lineCount(frame.contents) === 1) {{ frame.contents = wrapAtHalf(frame.contents); adjustments.push(label + ":wrapped-after-horizontal-scale"); }}
    width = measuredWidth();
    if (String(frame.contents) !== originalText) adjustments.push(label + ":wrapped");
    if (width > maxWidth + 0.5) violations.push(label + ":width-overflow");
    if (lineCount(frame.contents) > maxLines) violations.push(label + ":line-limit");
  }}
  function fitCoverBrandHeader(region, adjustments, violations) {{
    var refs = refsByType(region, "textFrame"), behavior = region.behavior || {{}};
    if (!refs.length) {{ violations.push(region.id + ":missing-brand-copy"); return; }}
    var index = Number(refs[0]), frame = textAt(index), rightLimit = Number(behavior.maxRightPt || 400);
    var maxWidth = rightLimit - Number(frame.geometricBounds[0]), wrapped = false;
    try {{ app.redraw(); }} catch (redrawError) {{}}
    if (Number(frame.geometricBounds[2]) > rightLimit + 0.5 && Number(behavior.maxLines || 1) > 1 && lineCount(frame.contents) === 1) {{
      frame.contents = wrapAtHalf(frame.contents); wrapped = true;
      adjustments.push(region.id + ":wrapped-to-fit-back-cover");
    }}
    if (wrapped) {{
      try {{ frame.textRange.characterAttributes.size = Math.min(Number(frame.textRange.characterAttributes.size || 18), Number(behavior.wrappedFontSizePt || 14.5)); }} catch (wrappedSizeError) {{}}
    }}
    fitPointText(index, wrapped ? Number(behavior.maxLines || 2) : 1, Number(behavior.minFontSizePt || 14), maxWidth, region.id, adjustments, violations, {{titleMinHorizontalScale:Number(behavior.minHorizontalScale || 85)}});
    try {{
      frame.textRange.characterAttributes.leading = Number(frame.textRange.characterAttributes.size || 14) * 1.08; app.redraw();
      var bottomLimit = Number(behavior.maxBottomPt || -102), currentBottom = Number(frame.geometricBounds[3]);
      if (currentBottom < bottomLimit) {{ frame.translate(0, bottomLimit - currentBottom); adjustments.push(region.id + ":moved-up-inside-header"); app.redraw(); }}
      try {{ frame.textRange.paragraphAttributes.justification = Justification.CENTER; }} catch (alignmentError) {{}}
      app.redraw();
      var headerLeft = Number(behavior.headerLeftPt || 0), headerRight = Number(behavior.headerRightPt || 420.9449), centeredBounds = frame.geometricBounds;
      var frameCenter = (Number(centeredBounds[0]) + Number(centeredBounds[2])) / 2, headerCenter = (headerLeft + headerRight) / 2;
      frame.translate(headerCenter - frameCenter, 0); app.redraw();
      adjustments.push(region.id + ":centered-in-back-cover-header");
    }} catch (leadingError) {{}}
    if (Number(frame.geometricBounds[2]) > rightLimit + 0.5) violations.push(region.id + ":crosses-cover-spine");
  }}
  function estimatedWrappedLines(value, width, fontSize) {{
    var paragraphs = String(value || "").replace(/\\n/g, "\\r").split("\\r");
    var capacity = Math.max(8, Math.floor(width / Math.max(1, fontSize * 0.55))), total = 0;
    for (var i = 0; i < paragraphs.length; i += 1) total += Math.max(1, Math.ceil(compactLength(paragraphs[i]) / capacity));
    return Math.max(1, total);
  }}
  function frameHeight(frame) {{ try {{ if (frame.kind === TextType.AREATEXT) return Number(frame.textPath.height); }} catch (kindError) {{}} return Number(frame.height); }}
  function resizeTextFrame(frame, width, height) {{
    var topLeft = [Number(frame.geometricBounds[0]), Number(frame.geometricBounds[1])];
    try {{
      if (frame.kind === TextType.AREATEXT) {{ if (width !== null && width !== undefined) frame.textPath.width = Number(width); if (height !== null && height !== undefined) frame.textPath.height = Number(height); }}
      else {{ if (width !== null && width !== undefined) frame.width = Number(width); if (height !== null && height !== undefined) frame.height = Number(height); }}
      moveTopLeft(frame, topLeft[0], topLeft[1]);
      return true;
    }} catch (resizeError) {{ return false; }}
  }}
  function fitAreaTextHeight(index, minSize, minHeight, maxHeight, sourceText, label, adjustments, violations) {{
    if (index < 0 || index >= sourceTextFrames.length) {{ violations.push(label + ":missing-text-frame-" + index); return null; }}
    var frame = textAt(index), attrs = frame.textRange.characterAttributes;
    var originalHeight = frameHeight(frame);
    var size = Number(attrs.size || 8);
    if (!bodyFontIsLocked(index) && size < minSize) {{ attrs.size = minSize; adjustments.push(label + ":font-raised-to-" + minSize); }}
    if (sourceText === undefined || sourceText === null || normalize(sourceText) === normalize(frame.contents)) return frame;
    var width = Number(frame.geometricBounds[2]) - Number(frame.geometricBounds[0]);
    var sourceLines = estimatedWrappedLines(sourceText, width, size), targetLines = estimatedWrappedLines(frame.contents, width, size);
    var requiredHeight = Math.max(minHeight, Math.min(maxHeight, originalHeight * targetLines / sourceLines));
    if (!resizeTextFrame(frame, null, requiredHeight)) {{ violations.push(label + ":height-not-editable"); return frame; }}
    if (requiredHeight >= maxHeight && targetLines > sourceLines) violations.push(label + ":body-overflow");
    if (Math.abs(frameHeight(frame) - originalHeight) > 0.5) adjustments.push(label + ":height-" + Math.round(frameHeight(frame) * 10) / 10);
    return frame;
  }}
  function growAreaUntilFits(frame, minSize, maxHeight, label, adjustments, violations, preserveFontSize) {{
    var startHeight = frameHeight(frame), size = Number(frame.textRange.characterAttributes.size || 8);
    while (hasTextOverflow(frame) && frameHeight(frame) < maxHeight - 0.5) {{
      if (!resizeTextFrame(frame, null, Math.min(maxHeight, frameHeight(frame) + 12))) break;
    }}
    while (!preserveFontSize && hasTextOverflow(frame) && size > minSize) {{
      size = Math.max(minSize, size - 0.5); frame.textRange.characterAttributes.size = size;
    }}
    if (frameHeight(frame) > startHeight + 0.5) adjustments.push(label + ":actual-height-" + Math.round(frameHeight(frame) * 10) / 10);
    if (!preserveFontSize && size < 8) adjustments.push(label + ":font-size-" + size);
    if (preserveFontSize && hasTextOverflow(frame)) adjustments.push(label + ":template-font-size-preserved-" + size);
    if (hasTextOverflow(frame)) violations.push(label + ":body-overflow");
    return frame;
  }}
  function shrinkAreaToContent(frame, minHeight, label, adjustments) {{
    var startHeight = frameHeight(frame), currentHeight = startHeight, floor = Math.max(18, Number(minHeight || 24)), step = 3;
    try {{ if (frame.kind !== TextType.AREATEXT) return frame; }} catch (kindError) {{ return frame; }}
    while (currentHeight - step >= floor) {{
      var candidate = currentHeight - step;
      if (!resizeTextFrame(frame, null, candidate)) break;
      try {{ app.redraw(); }} catch (redrawError) {{}}
      if (hasTextOverflow(frame)) {{ resizeTextFrame(frame, null, currentHeight); break; }}
      currentHeight = candidate;
    }}
    if (startHeight - currentHeight > 0.5) adjustments.push(label + ":content-height-" + Math.round(currentHeight * 10) / 10);
    return frame;
  }}
  function translateY(item, delta) {{ if (item && Math.abs(delta) > 0.01) item.translate(0, delta); }}
  function ensureTitleVisibleAboveBar(title, bar, label, adjustments, violations) {{
    if (!title || !bar) {{ violations.push(label + ":missing-title-or-bar"); return; }}
    try {{
      title.hidden = false;
      title.opacity = 100;
      title.zOrder(ZOrderMethod.BRINGTOFRONT);
      app.redraw();
      if (!normalize(title.contents)) violations.push(label + ":empty-title");
      else adjustments.push(label + ":title-brought-to-front");
    }} catch (visibilityError) {{ violations.push(label + ":title-visibility-unavailable"); }}
  }}
  function applyTitleBarFill(bar, behavior, label, adjustments, violations) {{
    var cmyk = (behavior && behavior.titleBarFillCMYK) || [0, 0, 0, 57];
    if (!bar || cmyk.length !== 4) return;
    try {{
      var color = new CMYKColor();
      color.cyan = Number(cmyk[0]); color.magenta = Number(cmyk[1]);
      color.yellow = Number(cmyk[2]); color.black = Number(cmyk[3]);
      bar.filled = true; bar.fillColor = color;
      adjustments.push(label + ":title-bar-fill-cmyk-" + cmyk.join("-"));
    }} catch (colorError) {{ violations.push(label + ":title-bar-fill-unavailable"); }}
  }}
  function applyPageTitleBarFill(page, adjustments, violations) {{
    var regions = page.regions || [];
    for (var i = 0; i < regions.length; i += 1) {{
      var region = regions[i], paths = refsByType(region, "pathItem"), behavior = region.behavior || {{}}, role = String(region.role || "");
      if (role.indexOf("section") < 0 && role.indexOf("title") < 0) continue;
      if (paths.length) applyTitleBarFill(pathAt(Number(paths[0])), behavior, region.id, adjustments, violations);
    }}
  }}
  function applyVerticalFlowPage(page, adjustments, violations, continuations) {{
    var regions = page.regions || [], sections = [];
    for (var i = 0; i < regions.length; i += 1) if (regions[i].role === "flow-section") sections.push(regions[i]);
    sections.sort(function (left, right) {{ return Number((left.behavior || {{}}).order || 0) - Number((right.behavior || {{}}).order || 0); }});
    var previousBottom = null, lastBody = null, lastSection = null, lastTitle = null, lastBar = null;
    for (var s = 0; s < sections.length; s += 1) {{
      var section = sections[s], behavior = section.behavior || {{}};
      var text = refsByType(section, "textFrame"), paths = refsByType(section, "pathItem");
      if (text.length < 2 || paths.length < 1) {{ violations.push(section.id + ":incomplete-flow-binding"); continue; }}
      var titleIndex = Number(text[0]), bodyIndex = Number(text[1]), pathIndex = Number(paths[0]);
      if (pathIndex < 0 || pathIndex >= sourcePathItems.length) {{ violations.push(section.id + ":missing-path-item-" + pathIndex); continue; }}
      var bar = pathAt(pathIndex), title = textAt(titleIndex);
      applyTitleBarFill(bar, behavior, section.id, adjustments, violations);
      var barWidth = Number(bar.geometricBounds[2]) - Number(bar.geometricBounds[0]);
      var titleGuard = guards[String(titleIndex)] || {{}};
      if (titleGuard.expectedSourceText !== undefined && titleGuard.expectedSourceText !== null) fitPointText(titleIndex, Number(behavior.titleMaxLines || 1), Number(behavior.titleMinFontSizePt || 30), Number(bar.geometricBounds[2]) - Number(title.geometricBounds[0]) - 14, section.id + "-title", adjustments, violations, behavior);
      var bodyGuard = guards[String(bodyIndex)] || {{}};
      applyBodyTypographyToFrame(textAt(bodyIndex), behavior, section.id + "-body", adjustments);
      var body = fitAreaTextHeight(bodyIndex, Number(behavior.bodyMinFontSizePt || 7), Number(behavior.bodyMinHeightPt || 24), Number(behavior.bodyMaxHeightPt || 260), bodyGuard.expectedSourceText, section.id + "-body", adjustments, violations);
      if (!body) continue;
      growAreaUntilFits(body, Number(behavior.bodyMinFontSizePt || 7), Number(behavior.bodyMaxHeightPt || 260), section.id + "-body", adjustments, violations, bodyFontIsLocked(bodyIndex));
      shrinkAreaToContent(body, Number(behavior.bodyMinHeightPt || 24), section.id + "-body", adjustments);
      var currentTop = Number(bar.geometricBounds[1]);
      var desiredTop = previousBottom === null ? currentTop : previousBottom - Number(behavior.sectionGapPt || 12);
      var delta = desiredTop - currentTop;
      translateY(bar, delta); translateY(title, delta); translateY(body, delta);
      if (Math.abs(delta) > 0.01) adjustments.push(section.id + ":moved-y-" + Math.round(delta * 10) / 10);
      previousBottom = Number(body.geometricBounds[3]);
      lastBody = body; lastSection = section; lastTitle = title; lastBar = bar;
      adjustments.push(section.id + ":bar-top-" + Math.round(Number(bar.geometricBounds[1]) * 10) / 10 + ":body-bottom-" + Math.round(previousBottom * 10) / 10);
    }}
    var contentBottom = page.safeBounds && page.safeBounds.length === 4 ? Number(page.safeBounds[3]) : -550;
    // Keep every section directly after the preceding section. If the final
    // body still crosses the safe bottom, fill the remaining space first and
    // thread only the overflow onto an inserted continuation artboard.
    if (previousBottom !== null && previousBottom < contentBottom && lastSection && lastBody && lastTitle && lastBar) {{
      var pageNumber = null;
      for (var pr = 0; pr < regions.length; pr += 1) if (regions[pr].role === "fixed-page-number") {{
        var pageRefs = refsByType(regions[pr], "textFrame"); if (pageRefs.length) pageNumber = textAt(Number(pageRefs[0]));
      }}
      var splitBodyTop = Number(lastBar.geometricBounds[3]) - Number((lastSection.behavior || {{}}).bodyTopGapPt || 18);
      var availableHeight = splitBodyTop - contentBottom;
      if (availableHeight >= Number((lastSection.behavior || {{}}).minFirstPanelHeightPt || 24)) {{
        moveTopLeft(lastBody, Number(lastBody.geometricBounds[0]), splitBodyTop);
        resizeTextFrame(lastBody, null, availableHeight);
        adjustments.push(lastSection.id + ":filled-remaining-page-space");
        var current = lastBody, sequence = 0, limit = Number((lastSection.behavior || {{}}).maxContinuationArtboards || 10);
        while (hasTextOverflow(current) && sequence < limit) {{
          sequence += 1;
          var continued = createSafetyContinuation(page, lastSection, lastTitle, lastBody, lastBar, pageNumber, sequence, current, adjustments, violations, pageNumber ? parsePageNumber(pageNumber.contents) : Number(page.artboardIndex) + 1);
          if (!continued) break;
          current = continued.body;
        }}
        if (hasTextOverflow(current)) continuations.push("artboard-" + page.artboardIndex + ":vertical-continuation-limit");
      }} else {{
        adjustments.push(lastSection.id + ":next-page-because-no-title-row-space");
        var inserted = moveVerticalSectionToInsertedArtboard(page, lastSection, lastTitle, lastBody, lastBar, pageNumber, adjustments, violations);
        if (!inserted || hasTextOverflow(inserted.body)) continuations.push("artboard-" + page.artboardIndex + ":new-artboard-required");
      }}
    }}
  }}
  function splitParagraphs(value) {{
    var raw = String(value || "").replace(/\\n/g, "\\r").split("\\r"), result = [];
    for (var i = 0; i < raw.length; i += 1) if (normalize(raw[i])) result.push(normalize(raw[i]));
    return result;
  }}
  function joinParagraphs(items, start, end) {{ return items.slice(start, end).join("\\r"); }}
  function applyBalancedPartsList(region, adjustments, violations) {{
    var refs = refsByType(region, "textFrame"), behavior = region.behavior || {{}};
    if (refs.length < 2) {{ violations.push(region.id + ":incomplete-list-binding"); return; }}
    var leftIndex = Number(refs[0]), rightIndex = Number(refs[1]);
    if (leftIndex < 0 || rightIndex < 0 || leftIndex >= sourceTextFrames.length || rightIndex >= sourceTextFrames.length) {{ violations.push(region.id + ":missing-list-frame"); return; }}
    var left = textAt(leftIndex), right = textAt(rightIndex);
    var items = splitParagraphs(left.contents).concat(splitParagraphs(right.contents));
    var midpoint = Math.ceil(items.length / 2);
    left.contents = joinParagraphs(items, 0, midpoint); right.contents = joinParagraphs(items, midpoint, items.length);
    adjustments.push(region.id + ":balanced-" + items.length + "-items-" + midpoint + "+" + (items.length - midpoint));
    if (behavior.rightColumnLeftPt !== undefined && behavior.rightColumnLeftPt !== null) {{
      moveTopLeft(right, Number(behavior.rightColumnLeftPt), Number(right.geometricBounds[1]));
      adjustments.push(region.id + ":right-column-left-" + Number(behavior.rightColumnLeftPt));
    }}
    var minSize = Number(behavior.minFontSizePt || 7), size = Math.min(Number(left.textRange.characterAttributes.size || 8), Number(right.textRange.characterAttributes.size || 8));
    var maxWidth = Number(behavior.columnWidthPt || 125), bottomBound = Number(behavior.bottomBound || -1150);
    var preserveFontSize = bodyFontIsLocked(leftIndex) || bodyFontIsLocked(rightIndex);
    while (!preserveFontSize && size > minSize) {{
      var leftBounds = left.geometricBounds, rightBounds = right.geometricBounds;
      var widthOverflow = (Number(leftBounds[2]) - Number(leftBounds[0]) > maxWidth + 0.5) || (Number(rightBounds[2]) - Number(rightBounds[0]) > maxWidth + 0.5);
      var bottomOverflow = Math.min(Number(leftBounds[3]), Number(rightBounds[3])) < bottomBound;
      if (!widthOverflow && !bottomOverflow) break;
      size = Math.max(minSize, size - 0.5); left.textRange.characterAttributes.size = size; right.textRange.characterAttributes.size = size;
    }}
    if (!preserveFontSize && size < 8) adjustments.push(region.id + ":font-size-" + size);
    if (preserveFontSize) adjustments.push(region.id + ":template-font-size-preserved-" + size);
    var finalLeft = left.geometricBounds, finalRight = right.geometricBounds;
    var minColumnGap = Number(behavior.minColumnGapPt || 8);
    if (Number(finalLeft[2]) + minColumnGap > Number(finalRight[0])) {{
      var rightShift = Number(finalLeft[2]) + minColumnGap - Number(finalRight[0]);
      right.translate(rightShift, 0);
      adjustments.push(region.id + ":right-column-auto-shift-" + Math.round(rightShift * 10) / 10);
      finalRight = right.geometricBounds;
    }}
    var finalBottom = Math.min(Number(finalLeft[3]), Number(finalRight[3]));
    if (finalBottom < bottomBound) {{
      var upward = Math.min(Number(behavior.maxUpwardShiftPt || 18), bottomBound - finalBottom);
      translateY(left, upward); translateY(right, upward);
      adjustments.push(region.id + ":moved-up-" + Math.round(upward * 10) / 10);
      finalLeft = left.geometricBounds; finalRight = right.geometricBounds;
    }}
    if (Number(finalLeft[2]) - Number(finalLeft[0]) > maxWidth + 0.5 || Number(finalRight[2]) - Number(finalRight[0]) > maxWidth + 0.5) violations.push(region.id + ":column-width-overflow");
    if (Number(finalLeft[2]) + minColumnGap > Number(finalRight[0])) violations.push(region.id + ":column-overlap");
    if (Math.min(Number(finalLeft[3]), Number(finalRight[3])) < bottomBound) violations.push(region.id + ":list-height-overflow");
  }}
  function moveTopLeft(item, left, top) {{
    var bounds = item.geometricBounds;
    item.translate(Number(left) - Number(bounds[0]), Number(top) - Number(bounds[1]));
  }}
  function alignTitleVerticallyInBar(title, bar, behavior, label, adjustments, violations) {{
    var policy = String((behavior || {{}}).titleVerticalAlign || "preserve");
    if (policy === "preserve") return;
    try {{
      app.redraw();
      var barBounds = bar.geometricBounds, titleBounds = title.visibleBounds, allowedBottomBleed = 0;
      if (policy === "bottom") {{
        var configuredBottomInset = (behavior || {{}}).titleBottomInsetPt;
        var bottomInset = configuredBottomInset === undefined || configuredBottomInset === null ? 0 : Number(configuredBottomInset);
        translateY(title, Number(barBounds[3]) + bottomInset - Number(titleBounds[3]));
      }} else if (policy === "match-reference-bottom") {{
        var referenceOffset = Number((behavior || {{}}).resolvedTitleBottomOffsetPt);
        if (!isFinite(referenceOffset)) {{ violations.push(label + ":missing-title-reference-offset"); return; }}
        allowedBottomBleed = Math.max(0, -referenceOffset);
        translateY(title, Number(barBounds[3]) + referenceOffset - Number(titleBounds[3]));
      }} else if (policy === "center") {{
        var barCenter = (Number(barBounds[1]) + Number(barBounds[3])) / 2;
        var titleCenter = (Number(titleBounds[1]) + Number(titleBounds[3])) / 2;
        translateY(title, barCenter - titleCenter);
      }} else {{
        violations.push(label + ":unsupported-title-vertical-align-" + policy);
        return;
      }}
      app.redraw();
      titleBounds = title.visibleBounds;
      if (Number(titleBounds[1]) > Number(barBounds[1]) + 0.5 || Number(titleBounds[3]) < Number(barBounds[3]) - allowedBottomBleed - 0.5) violations.push(label + ":title-vertical-overflow");
      else adjustments.push(label + ":title-vertical-align-" + policy);
    }} catch (alignError) {{ violations.push(label + ":title-vertical-align-unavailable"); }}
  }}
  function parsePageNumber(value) {{
    var match = String(value || "").match(/-\\s*(\\d+)\\s*-/);
    return match ? Number(match[1]) : 0;
  }}
  function placeContinuationPageNumber(pageNumber, left, width, top, behavior, label, adjustments, violations) {{
    var panelCount = Math.max(1, Math.round(Number((behavior || {{}}).continuationPanelCount || 1)));
    var panelIndex = Math.round(Number((behavior || {{}}).continuationPanelIndex || 0));
    if (panelIndex < 0 || panelIndex >= panelCount) {{
      violations.push(label + ":invalid-continuation-panel-index");
      panelIndex = 0;
    }}
    var panelWidth = Number(width) / panelCount;
    var centerX = Number(left) + panelWidth * (panelIndex + 0.5);
    moveTopLeft(pageNumber, centerX - Number(pageNumber.width) / 2, Number(top));
    adjustments.push(label + ":page-number-panel-" + panelIndex + "-of-" + panelCount);
  }}
  function createSafetyContinuation(page, section, sourceTitle, sourceBody, sourceBar, sourcePageNumber, sequence, currentBody, adjustments, violations, basePageNumber) {{
    var baseRect = doc.artboards[page.artboardIndex].artboardRect;
    var width = Number(baseRect[2]) - Number(baseRect[0]), height = Number(baseRect[1]) - Number(baseRect[3]);
    var lowestBottom = Number(baseRect[3]);
    for (var i = 0; i < doc.artboards.length; i += 1) lowestBottom = Math.min(lowestBottom, Number(doc.artboards[i].artboardRect[3]));
    var newLeft = Number(baseRect[0]), newTop = lowestBottom - 36, newRight = newLeft + width, newBottom = newTop - height;
    var insertIndex = Math.min(doc.artboards.length, Number(page.artboardIndex) + sequence);
    try {{ doc.artboards.insert([newLeft, newTop, newRight, newBottom], insertIndex); }} catch (insertError) {{ violations.push(section.id + ":cannot-insert-artboard"); return null; }}
    var bar = sourceBar.duplicate(), title = sourceTitle.duplicate(), body = sourceBody.duplicate(), pageNumber = sourcePageNumber ? sourcePageNumber.duplicate() : null;
    applyTitleBarFill(bar, section.behavior || {{}}, section.id + ":continuation-" + sequence, adjustments, violations);
    try {{ bar.width = width; }} catch (barWidthError) {{ violations.push(section.id + ":continuation-bar-width"); }}
    moveTopLeft(bar, newLeft, newTop - 31.1811);
    moveTopLeft(title, newLeft + 35.7573, newTop - 39.3774);
    if (!resizeTextFrame(body, width - 72, height - 153.1785)) violations.push(section.id + ":continuation-body-size");
    moveTopLeft(body, newLeft + 36, newTop - 99.4478);
    body.contents = "";
    try {{ currentBody.nextFrame = body; currentBody.name = "__layout_threaded__"; body.name = "__layout_threaded__"; }} catch (threadError) {{ violations.push(section.id + ":cannot-thread-continuation"); return null; }}
    if (pageNumber) {{
      pageNumber.contents = "- " + ((basePageNumber === undefined ? parsePageNumber(sourcePageNumber.contents) : Number(basePageNumber)) + sequence) + " -";
      placeContinuationPageNumber(pageNumber, newLeft, width, newBottom + 22.9666, section.behavior || {{}}, section.id + ":continuation-" + sequence, adjustments, violations);
    }}
    var titleWidth = width - 72;
    var titleSize = Number(title.textRange.characterAttributes.size || 45);
    while (Number(title.width) > titleWidth && titleSize > Number((section.behavior || {{}}).titleMinFontSizePt || 30)) {{ titleSize = Math.max(Number((section.behavior || {{}}).titleMinFontSizePt || 30), titleSize - 0.5); title.textRange.characterAttributes.size = titleSize; }}
    alignTitleVerticallyInBar(title, bar, section.behavior || {{}}, section.id + ":continuation-" + sequence, adjustments, violations);
    ensureTitleVisibleAboveBar(title, bar, section.id + ":continuation-" + sequence, adjustments, violations);
    adjustments.push(section.id + ":created-continuation-artboard-" + insertIndex);
    return {{body: body, pageNumber: pageNumber}};
  }}
  function applyTwoPanelFlowPage(page, adjustments, violations, continuations) {{
    var regions = page.regions || [], parts = null, safety = null, safetyPageNumber = null, titleRegions = [];
    for (var i = 0; i < regions.length; i += 1) {{
      if (regions[i].role === "balanced-two-column-list") parts = regions[i];
      if (regions[i].role === "continuable-section") safety = regions[i];
      if (regions[i].id === "page3-safety-page-number") safetyPageNumber = regions[i];
      if (regions[i].role === "fixed-panel-title") titleRegions.push(regions[i]);
    }}
    for (var t = 0; t < titleRegions.length; t += 1) {{
      var titleRefs = refsByType(titleRegions[t], "textFrame"), titlePaths = refsByType(titleRegions[t], "pathItem"), titleBehavior = titleRegions[t].behavior || {{}};
      if (titleRefs.length && titlePaths.length) {{
        var titleBar = pathAt(titlePaths[0]), titleGuard = guards[String(titleRefs[0])] || {{}};
        if (titleGuard.expectedSourceText !== undefined && titleGuard.expectedSourceText !== null) fitPointText(Number(titleRefs[0]), Number(titleBehavior.titleMaxLines || 1), Number(titleBehavior.titleMinFontSizePt || 30), Number(titleBar.geometricBounds[2]) - Number(textAt(Number(titleRefs[0])).geometricBounds[0]) - 14, titleRegions[t].id, adjustments, violations, titleBehavior);
      }}
    }}
    if (parts) applyBalancedPartsList(parts, adjustments, violations);
    if (!safety) {{ violations.push("page3-safety-warning:missing-region"); return; }}
    var text = refsByType(safety, "textFrame"), paths = refsByType(safety, "pathItem");
    if (text.length < 2 || paths.length < 1) {{ violations.push(safety.id + ":incomplete-continuation-binding"); return; }}
    var title = textAt(text[0]), body = textAt(text[1]), bar = pathAt(paths[0]);
    var titleGuard = guards[String(text[0])] || {{}}, behavior = safety.behavior || {{}};
    if (titleGuard.expectedSourceText !== undefined && titleGuard.expectedSourceText !== null) fitPointText(Number(text[0]), Number(behavior.titleMaxLines || 1), Number(behavior.titleMinFontSizePt || 30), Number(bar.geometricBounds[2]) - Number(title.geometricBounds[0]) - 14, safety.id + "-title", adjustments, violations, behavior);
    if (!bodyFontIsLocked(Number(text[1])) && Number(body.textRange.characterAttributes.size || 8.5) < Number(behavior.bodyMinFontSizePt || 7)) body.textRange.characterAttributes.size = Number(behavior.bodyMinFontSizePt || 7);
    var numberRefs = safetyPageNumber ? refsByType(safetyPageNumber, "textFrame") : [], pageNumber = numberRefs.length ? textAt(numberRefs[0]) : null;
    var originalLaterNumbers = [], baseNumber = pageNumber ? parsePageNumber(pageNumber.contents) : 0;
    for (var n = 0; n < sourceTextFrames.length; n += 1) if (parsePageNumber(sourceTextFrames[n].contents) > baseNumber) originalLaterNumbers.push(sourceTextFrames[n]);
    var currentBody = body, created = 0, maxPages = Number(behavior.maxContinuationArtboards || 10);
    while (hasTextOverflow(currentBody) && created < maxPages) {{
      created += 1;
      var result = createSafetyContinuation(page, safety, title, body, bar, pageNumber, created, currentBody, adjustments, violations);
      if (!result) break;
      currentBody = result.body;
    }}
    if (hasTextOverflow(currentBody)) continuations.push("artboard-" + page.artboardIndex + ":safety-warning-continuation-limit");
    if (created > 0) {{
      for (var pn = 0; pn < originalLaterNumbers.length; pn += 1) {{
        var value = parsePageNumber(originalLaterNumbers[pn].contents);
        if (value > baseNumber) originalLaterNumbers[pn].contents = "- " + (value + created) + " -";
      }}
      adjustments.push(safety.id + ":continuation-count-" + created);
    }}
  }}
  function copyTextStyle(source, target, fallbackSize) {{
    try {{ target.textRange.characterAttributes.size = Number(source.textRange.characterAttributes.size || fallbackSize || 8); }} catch (sizeError) {{}}
    try {{ target.textRange.characterAttributes.textFont = source.textRange.characterAttributes.textFont; }} catch (fontError) {{}}
    try {{ target.textRange.characterAttributes.fillColor = source.textRange.characterAttributes.fillColor; }} catch (colorError) {{}}
  }}
  function createAreaFrame(left, top, width, height, contents, styleSource) {{
    var path = doc.pathItems.rectangle(Number(top), Number(left), Math.max(12, Number(width)), Math.max(12, Number(height)));
    var frame = doc.textFrames.areaText(path); frame.contents = String(contents || "");
    frame.name = "__layout_generated_text__";
    if (styleSource) copyTextStyle(styleSource, frame, 8);
    return frame;
  }}
  function createLiveOutlinedTitle(region, adjustments, violations) {{
    var compounds = refsByType(region, "compoundPathItem"), paths = refsByType(region, "pathItem"), behavior = region.behavior || {{}};
    if (!compounds.length || !paths.length) {{ violations.push(region.id + ":missing-outlined-title-binding"); return null; }}
    var compoundIndex = Number(compounds[0]);
    if (compoundIndex < 0 || compoundIndex >= sourceCompoundPathItems.length) {{ violations.push(region.id + ":missing-compound-path-" + compoundIndex); return null; }}
    var outline = compoundAt(compoundIndex), bounds = outline.geometricBounds;
    var replacement = layoutTextReplacements[String(behavior.virtualTitleSlotIndex)] || {{}};
    var title = doc.textFrames.pointText([Number(bounds[0]), Number(bounds[1])]);
    title.name = "__layout_generated_text__";
    title.contents = String(replacement.text || behavior.sourceTitleText || "Service Support");
    var attrs = title.textRange.characterAttributes, size = Number(replacement.fontSize || 30), minSize = Number(behavior.titleMinFontSizePt || 30);
    attrs.size = Math.max(minSize, size);
    var fontName = String(replacement.fontName || behavior.titleFontName || "ArialMT");
    try {{ attrs.textFont = app.textFonts.getByName(fontName); }} catch (fontError) {{ violations.push(region.id + ":title-font-fallback"); }}
    try {{ var white = new RGBColor(); white.red = 255; white.green = 255; white.blue = 255; attrs.fillColor = white; }} catch (colorError) {{}}
    outline.hidden = true;
    var bar = pathAt(paths[0]), maxWidth = Number(bar.width) - 72;
    while (Number(title.width) > maxWidth && Number(attrs.size) > minSize) attrs.size = Math.max(minSize, Number(attrs.size) - 0.5);
    var horizontalScale = 100, minHorizontalScale = Number(behavior.titleMinHorizontalScale || 70);
    try {{ horizontalScale = Number(attrs.horizontalScale || 100); }} catch (scaleReadError) {{}}
    while (Number(title.width) > maxWidth + 0.5 && horizontalScale > minHorizontalScale) {{
      horizontalScale = Math.max(minHorizontalScale, horizontalScale - 1);
      try {{ attrs.horizontalScale = horizontalScale; }} catch (scaleWriteError) {{ break; }}
    }}
    alignTitleVerticallyInBar(title, bar, behavior, region.id, adjustments, violations);
    ensureTitleVisibleAboveBar(title, bar, region.id, adjustments, violations);
    if (horizontalScale < 99.5) adjustments.push(region.id + ":title-horizontal-scale-" + horizontalScale);
    if (Number(title.width) > maxWidth + 0.5) violations.push(region.id + ":title-width-overflow");
    adjustments.push(region.id + ":outlined-title-replaced");
    return title;
  }}
  function createLiveCoverLabel(region, adjustments, violations) {{
    var groups = refsByType(region, "groupItem"), behavior = region.behavior || {{}};
    if (!groups.length) {{ violations.push(region.id + ":missing-outlined-cover-binding"); return null; }}
    var groupIndex = Number(groups[0]);
    if (groupIndex < 0 || groupIndex >= sourceGroupItems.length) {{ violations.push(region.id + ":missing-group-" + groupIndex); return null; }}
    var outline = groupAt(groupIndex), bounds = outline.geometricBounds;
    var replacement = layoutTextReplacements[String(behavior.virtualTitleSlotIndex)] || {{}};
    var value = normalize(replacement.text || behavior.sourceTitleText || "New Generation");
    if (value.indexOf(" ") >= 0 && lineCount(value) === 1 && compactLength(value) > 18) value = wrapAtHalf(value);
    var title = doc.textFrames.pointText([Number(bounds[0]), Number(bounds[1])]);
    title.name = "__layout_generated_text__";
    title.contents = value;
    var attrs = title.textRange.characterAttributes;
    attrs.size = Number(behavior.titleFontSizePt || replacement.fontSize || 22);
    try {{ attrs.textFont = app.textFonts.getByName(String(replacement.fontName || behavior.titleFontName || "TimesNewRomanPSMT")); }}
    catch (fontError) {{ violations.push(region.id + ":title-font-fallback"); }}
    try {{ var white = new RGBColor(); white.red = 255; white.green = 255; white.blue = 255; attrs.fillColor = white; }} catch (colorError) {{}}
    try {{ title.rotate(Number(behavior.rotationDegrees || -45)); }} catch (rotateError) {{ violations.push(region.id + ":title-rotation-unavailable"); }}
    try {{
      app.redraw();
      var targetWidth = Math.max(12, Number(bounds[2]) - Number(bounds[0]) - 4);
      var targetHeight = Math.max(12, Number(bounds[1]) - Number(bounds[3]) - 4);
      var configuredMinSize = Number(behavior.titleMinFontSizePt || 16);
      var absoluteMinSize = Number(behavior.titleAbsoluteMinFontSizePt || 10);
      var currentSize = Number(attrs.size || 22);
      var rendered = title.visibleBounds;
      function coverLabelFits() {{
        return Number(rendered[2]) - Number(rendered[0]) <= targetWidth + 0.5 &&
          Number(rendered[1]) - Number(rendered[3]) <= targetHeight + 0.5;
      }}
      while (!coverLabelFits() && currentSize > absoluteMinSize) {{
        currentSize = Math.max(absoluteMinSize, currentSize - 0.5);
        attrs.size = currentSize;
        app.redraw();
        rendered = title.visibleBounds;
      }}
      if (currentSize < configuredMinSize) adjustments.push(region.id + ":title-fallback-font-" + currentSize);
      if (!coverLabelFits()) violations.push(region.id + ":title-bounds-overflow");
      var centerX = (Number(bounds[0]) + Number(bounds[2])) / 2, centerY = (Number(bounds[1]) + Number(bounds[3])) / 2;
      title.translate(centerX - (Number(rendered[0]) + Number(rendered[2])) / 2, centerY - (Number(rendered[1]) + Number(rendered[3])) / 2);
    }} catch (centerError) {{ violations.push(region.id + ":title-centering-unavailable"); }}
    outline.hidden = true;
    adjustments.push(region.id + ":outlined-label-replaced");
    return title;
  }}
  function applyCoverDistributorEntry(region, page, adjustments, violations) {{
    var refs = refsByType(region, "textFrame"), paths = refsByType(region, "pathItem"), behavior = region.behavior || {{}};
    if (!refs.length || !paths.length) {{ violations.push(region.id + ":incomplete-entry-binding"); return; }}
    var pathIndex = Number(paths[0]);
    if (pathIndex < 0 || pathIndex >= sourcePathItems.length) {{ violations.push(region.id + ":missing-entry-path-" + pathIndex); return; }}
    var card = pathAt(pathIndex), titleIndex = Number(refs[0]), title = textAt(titleIndex);
    var left = Number(behavior.cardLeftPt || card.geometricBounds[0]), right = Number(behavior.cardRightPt || card.geometricBounds[2]);
    var top = Number(behavior.cardTopPt || card.geometricBounds[1]), bottom = Number(behavior.cardBottomPt || card.geometricBounds[3]);
    var width = right - left, height = top - bottom, inset = 12;
    try {{ card.width = width; card.height = height; moveTopLeft(card, left, top); }}
    catch (cardResizeError) {{ violations.push(region.id + ":card-resize-unavailable"); return; }}
    fitPointText(titleIndex, 1, Number(behavior.titleMinFontSizePt || 10), width - inset * 2, region.id + "-title", adjustments, violations, {{titleMinHorizontalScale:Number(behavior.titleMinHorizontalScale || 85)}});
    try {{
      app.redraw();
      var titleHeight = Number(title.geometricBounds[1]) - Number(title.geometricBounds[3]);
      moveTopLeft(title, left + (width - Number(title.width)) / 2, top - (height - titleHeight) / 2);
    }}
    catch (titlePositionError) {{ violations.push(region.id + ":title-position-unavailable"); }}
    var titleBounds = title.geometricBounds;
    if (Number(titleBounds[0]) < left + inset - 0.5 || Number(titleBounds[2]) > right - inset + 0.5 || Number(titleBounds[1]) > top + 0.5 || Number(titleBounds[3]) < bottom - 0.5) violations.push(region.id + ":title-outside-entry-card");
    adjustments.push(region.id + ":entry-card-widened-title-centered");
  }}
  function serviceSupportText(refs) {{
    var byIndex = {{}}, values = [];
    for (var i = 0; i < refs.length; i += 1) byIndex[Number(refs[i])] = textAt(refs[i]);
    function block(indexes) {{
      var lines = [];
      for (var j = 0; j < indexes.length; j += 1) {{ var frame = byIndex[indexes[j]]; if (frame && normalize(frame.contents)) lines.push(normalize(frame.contents)); }}
      return lines.join("\\r");
    }}
    var groups = [[15], [14,13,12,11], [10,9,8], [7,6,5,4], [3,2,1]];
    for (var g = 0; g < groups.length; g += 1) {{ var value = block(groups[g]); if (value) values.push(value); }}
    return values.join("\\r\\r");
  }}
  function placeSectionTitleAndBody(bar, title, body, barTop, bodyTop, bodyHeight) {{
    var delta = Number(barTop) - Number(bar.geometricBounds[1]);
    translateY(bar, delta); translateY(title, delta);
    resizeTextFrame(body, null, Math.max(12, Number(bodyHeight)));
    moveTopLeft(body, Number(body.geometricBounds[0]), Number(bodyTop));
  }}
  function continueSection(page, section, title, body, bar, pageNumber, state, adjustments, violations, continuations) {{
    var behavior = section.behavior || {{}}, current = body, limit = Number(behavior.maxContinuationArtboards || 10), local = 0;
    while (hasTextOverflow(current) && local < limit) {{
      local += 1; state.sequence += 1;
      var result = createSafetyContinuation(page, section, title, body, bar, pageNumber, state.sequence, current, adjustments, violations, state.basePageNumber);
      if (!result) break;
      current = result.body;
    }}
    if (hasTextOverflow(current)) continuations.push("artboard-" + page.artboardIndex + ":" + section.id + "-continuation-limit");
    if (local) adjustments.push(section.id + ":continuation-count-" + local);
  }}
  function splitFirstParagraph(contents) {{
    var text = String(contents || ""), match = /\\r\\n|\\r|\\n/.exec(text);
    if (!match) return null;
    var first = text.substring(0, match.index), rest = text.substring(match.index + match[0].length);
    while (rest.length && (rest.charAt(0) === "\\r" || rest.charAt(0) === "\\n")) rest = rest.substring(1);
    return first && rest ? {{first:first, rest:rest}} : null;
  }}
  function continueIntoSiblingPanel(section, sourceTitle, sourceBody, sourceBar, targetBar, siblingBottom, remainingText, adjustments, violations) {{
    var left = Number(targetBar.geometricBounds[0]), right = Number(targetBar.geometricBounds[2]), top = Number(targetBar.geometricBounds[1]), bottom = Number(targetBar.geometricBounds[3]), width = right - left;
    var bar = sourceBar.duplicate(), title = sourceTitle.duplicate(), body = sourceBody.duplicate();
    applyTitleBarFill(bar, section.behavior || {{}}, section.id + ":sibling", adjustments, violations);
    try {{ bar.width = width; }} catch (barWidthError) {{ violations.push(section.id + ":sibling-bar-width"); }}
    moveTopLeft(bar, left, top); moveTopLeft(title, left + 36, top - 8);
    alignTitleVerticallyInBar(title, bar, section.behavior || {{}}, section.id + ":sibling", adjustments, violations);
    ensureTitleVisibleAboveBar(title, bar, section.id + ":sibling", adjustments, violations);
    var fontSize = Number(body.textRange.characterAttributes.size || 7), requiredHeight = Math.max(96, estimatedWrappedLines(remainingText, width - 72, fontSize) * fontSize * 1.6 + 30);
    var availableHeight = Math.max(24, Number(targetBar.geometricBounds[3]) - Number(siblingBottom) - 72);
    if (!resizeTextFrame(body, width - 72, Math.min(availableHeight, requiredHeight))) {{ violations.push(section.id + ":sibling-body-size"); return null; }}
    moveTopLeft(body, left + 36, Number(bar.geometricBounds[3]) - 18); body.contents = remainingText;
    if (hasTextOverflow(body)) violations.push(section.id + ":sibling-panel-overflow");
    adjustments.push(section.id + ":continued-into-sibling-panel");
    return {{bar:bar,title:title,body:body}};
  }}
  function moveSectionToNewArtboard(page, section, sourceTitle, sourceBody, sourceBar, sourcePageNumber, sequence, adjustments, violations) {{
    var baseRect = doc.artboards[page.artboardIndex].artboardRect, width = Number(baseRect[2]) - Number(baseRect[0]), height = Number(baseRect[1]) - Number(baseRect[3]), lowest = Number(baseRect[3]);
    for (var i = 0; i < doc.artboards.length; i += 1) lowest = Math.min(lowest, Number(doc.artboards[i].artboardRect[3]));
    var left = Number(baseRect[0]), top = lowest - 36, bottom = top - height, index = doc.artboards.length;
    try {{ doc.artboards.add([left, top, left + width, bottom]); }} catch (artboardError) {{ violations.push(section.id + ":cannot-move-to-new-artboard"); return null; }}
    var bar = sourceBar.duplicate(), title = sourceTitle.duplicate(), body = sourceBody.duplicate(), pageNumber = sourcePageNumber ? sourcePageNumber.duplicate() : null;
    applyTitleBarFill(bar, section.behavior || {{}}, section.id + ":moved", adjustments, violations);
    try {{ bar.width = width; }} catch (barWidthError) {{ violations.push(section.id + ":moved-bar-width"); }}
    moveTopLeft(bar, left, top - 31.1811); moveTopLeft(title, left + 35.7573, top - 39.3774);
    alignTitleVerticallyInBar(title, bar, section.behavior || {{}}, section.id + ":moved", adjustments, violations);
    ensureTitleVisibleAboveBar(title, bar, section.id + ":moved", adjustments, violations);
    if (!resizeTextFrame(body, width - 72, height - 153.1785)) {{ violations.push(section.id + ":moved-body-size"); return null; }}
    moveTopLeft(body, left + 36, top - 99.4478); body.contents = sourceBody.contents;
    if (pageNumber) {{ pageNumber.contents = "- " + (parsePageNumber(sourcePageNumber.contents) + sequence) + " -"; placeContinuationPageNumber(pageNumber, left, width, bottom + 22.9666, section.behavior || {{}}, section.id + ":moved", adjustments, violations); }}
    sourceBar.hidden = true; sourceTitle.hidden = true; sourceBody.hidden = true;
    adjustments.push(section.id + ":moved-to-artboard-" + index);
    return {{body:body,pageNumber:pageNumber}};
  }}
  function moveVerticalSectionToInsertedArtboard(page, section, sourceTitle, sourceBody, sourceBar, sourcePageNumber, adjustments, violations) {{
    var baseRect = doc.artboards[page.artboardIndex].artboardRect;
    var width = Number(baseRect[2]) - Number(baseRect[0]), height = Number(baseRect[1]) - Number(baseRect[3]);
    var lowest = Number(baseRect[3]);
    for (var i = 0; i < doc.artboards.length; i += 1) lowest = Math.min(lowest, Number(doc.artboards[i].artboardRect[3]));
    var left = Number(baseRect[0]), top = lowest - 36, bottom = top - height, insertedIndex = Number(page.artboardIndex) + 1;
    try {{ doc.artboards.insert([left, top, left + width, bottom], insertedIndex); }}
    catch (insertError) {{ violations.push(section.id + ":cannot-insert-continuation-artboard"); return null; }}
    var bar = sourceBar.duplicate(), title = sourceTitle.duplicate(), body = sourceBody.duplicate();
    var pageNumber = sourcePageNumber ? sourcePageNumber.duplicate() : null, behavior = section.behavior || {{}};
    applyTitleBarFill(bar, behavior, section.id + ":inserted", adjustments, violations);
    try {{ bar.width = width; }} catch (barWidthError) {{ violations.push(section.id + ":inserted-bar-width"); }}
    moveTopLeft(bar, left, top - 31.1811); moveTopLeft(title, left + 35.7573, top - 39.3774);
    ensureTitleVisibleAboveBar(title, bar, section.id + ":inserted", adjustments, violations);
    if (!resizeTextFrame(body, width - 72, height - 153.1785)) {{ violations.push(section.id + ":inserted-body-size"); return null; }}
    moveTopLeft(body, left + 36, top - 99.4478); applyBodyTypographyToFrame(body, behavior, section.id + "-body", adjustments);
    var baseNumber = sourcePageNumber ? parsePageNumber(sourcePageNumber.contents) : Number(page.artboardIndex) + 1;
    for (var n = 0; n < sourceTextFrames.length; n += 1) {{
      var value = parsePageNumber(sourceTextFrames[n].contents);
      if (value > baseNumber) sourceTextFrames[n].contents = "- " + (value + 1) + " -";
    }}
    if (pageNumber) {{ pageNumber.contents = "- " + (baseNumber + 1) + " -"; placeContinuationPageNumber(pageNumber, left, width, bottom + 22.9666, behavior, section.id + ":inserted", adjustments, violations); }}
    sourceBar.hidden = true; sourceTitle.hidden = true; sourceBody.hidden = true;
    adjustments.push(section.id + ":inserted-artboard-" + insertedIndex);
    return {{body:body,pageNumber:pageNumber}};
  }}
  function applyPairedSectionsFlowPage(page, adjustments, violations, continuations) {{
    var regions = page.regions || [], assembly = null, diagramRegion = null, operation = null, maintenance = null, service = null, leftNumberRegion = null, rightNumberRegion = null;
    for (var i = 0; i < regions.length; i += 1) {{
      if (regions[i].role === "assembly-flow-section") assembly = regions[i];
      if (regions[i].role === "adaptive-contain-diagram") diagramRegion = regions[i];
      if (regions[i].role === "continuable-operation-section") operation = regions[i];
      if (regions[i].role === "maintenance-flow-section") maintenance = regions[i];
      if (regions[i].role === "continuable-service-section") service = regions[i];
      if (regions[i].id === "page4-left-page-number") leftNumberRegion = regions[i];
      if (regions[i].id === "page4-right-page-number") rightNumberRegion = regions[i];
    }}
    if (!assembly || !diagramRegion || !operation || !maintenance || !service) {{ violations.push("page4:incomplete-region-set"); return; }}
    var assemblyText = refsByType(assembly, "textFrame"), assemblyPaths = refsByType(assembly, "pathItem"), operationText = refsByType(operation, "textFrame"), operationPaths = refsByType(operation, "pathItem");
    var maintenanceText = refsByType(maintenance, "textFrame"), maintenancePaths = refsByType(maintenance, "pathItem"), diagramRefs = refsByType(diagramRegion, "groupItem");
    if (assemblyText.length < 2 || !assemblyPaths.length || operationText.length < 2 || !operationPaths.length || maintenanceText.length < 2 || !maintenancePaths.length || !diagramRefs.length) {{ violations.push("page4:incomplete-object-binding"); return; }}
    var assemblyTitle = textAt(assemblyText[0]), assemblyBody = textAt(assemblyText[1]), assemblyBar = pathAt(assemblyPaths[0]);
    var operationTitle = textAt(operationText[0]), operationBody = textAt(operationText[1]), operationBar = pathAt(operationPaths[0]);
    var maintenanceTitle = textAt(maintenanceText[0]), maintenanceBody = textAt(maintenanceText[1]), maintenanceBar = pathAt(maintenancePaths[0]);
    fitPointText(Number(assemblyText[0]), Number((assembly.behavior || {{}}).titleMaxLines || 1), Number((assembly.behavior || {{}}).titleMinFontSizePt || 30), Number(assemblyBar.geometricBounds[2]) - Number(assemblyTitle.geometricBounds[0]) - 14, assembly.id + "-title", adjustments, violations, assembly.behavior);
    fitPointText(Number(operationText[0]), Number((operation.behavior || {{}}).titleMaxLines || 1), Number((operation.behavior || {{}}).titleMinFontSizePt || 30), Number(operationBar.geometricBounds[2]) - Number(operationTitle.geometricBounds[0]) - 14, operation.id + "-title", adjustments, violations, operation.behavior);
    fitPointText(Number(maintenanceText[0]), Number((maintenance.behavior || {{}}).titleMaxLines || 1), Number((maintenance.behavior || {{}}).titleMinFontSizePt || 30), Number(maintenanceBar.geometricBounds[2]) - Number(maintenanceTitle.geometricBounds[0]) - 14, maintenance.id + "-title", adjustments, violations, maintenance.behavior);
    var assemblyGuard = guards[String(assemblyText[1])] || {{}}, operationGuard = guards[String(operationText[1])] || {{}}, maintenanceGuard = guards[String(maintenanceText[1])] || {{}};
    fitAreaTextHeight(Number(assemblyText[1]), Number((assembly.behavior || {{}}).bodyMinFontSizePt || 7), 24, Number((assembly.behavior || {{}}).bodyMaxHeightPt || 180), assemblyGuard.expectedSourceText, assembly.id + "-body", adjustments, violations);
    fitAreaTextHeight(Number(operationText[1]), Number((operation.behavior || {{}}).bodyMinFontSizePt || 7), 24, 260, operationGuard.expectedSourceText, operation.id + "-body", adjustments, violations);
    fitAreaTextHeight(Number(maintenanceText[1]), Number((maintenance.behavior || {{}}).bodyMinFontSizePt || 7), 24, Number((maintenance.behavior || {{}}).bodyMaxHeightPt || 180), maintenanceGuard.expectedSourceText, maintenance.id + "-body", adjustments, violations);
    growAreaUntilFits(assemblyBody, Number((assembly.behavior || {{}}).bodyMinFontSizePt || 7), Number((assembly.behavior || {{}}).bodyMaxHeightPt || 180), assembly.id + "-body", adjustments, violations, bodyFontIsLocked(Number(assemblyText[1])));
    growAreaUntilFits(maintenanceBody, Number((maintenance.behavior || {{}}).bodyMinFontSizePt || 7), Number((maintenance.behavior || {{}}).bodyMaxHeightPt || 180), maintenance.id + "-body", adjustments, violations, bodyFontIsLocked(Number(maintenanceText[1])));
    var diagram = groupAt(diagramRefs[0]), db = diagram.geometricBounds, originalWidth = Number(db[2]) - Number(db[0]), originalHeight = Number(db[1]) - Number(db[3]), centerX = (Number(db[0]) + Number(db[2])) / 2;
    var diagramBehavior = diagramRegion.behavior || {{}}, diagramTop = Number(assemblyBody.geometricBounds[3]) - Number(diagramBehavior.minTopGapPt || 4), minScale = Number(diagramBehavior.minScale || 0.7);
    var operationBodyHeight = frameHeight(operationBody), operationBodyOffset = Number(operationBody.geometricBounds[1]) - Number(operationBar.geometricBounds[1]), operationBehavior = operation.behavior || {{}}, firstPanelMinHeight = Number(operationBehavior.firstPanelBodyMinHeightPt || 24);
    var requiredHeight = originalHeight + Number(diagramBehavior.minBottomGapPt || 8) + Math.abs(operationBodyOffset) + operationBodyHeight;
    var availableHeight = diagramTop - Number(page.safeBounds[3]);
    var scale = Math.max(minScale, Math.min(1, (availableHeight - Number(diagramBehavior.minBottomGapPt || 8) - Math.abs(operationBodyOffset) - firstPanelMinHeight) / originalHeight));
    if (scale < 0.999) {{ diagram.resize(scale * 100, scale * 100); adjustments.push(diagramRegion.id + ":scaled-" + Math.round(scale * 1000) / 1000); }}
    db = diagram.geometricBounds; moveTopLeft(diagram, centerX - (Number(db[2]) - Number(db[0])) / 2, diagramTop);
    db = diagram.geometricBounds;
    var operationBarTop = Number(db[3]) - Number(diagramBehavior.minBottomGapPt || 8), operationBodyTop = operationBarTop + operationBodyOffset;
    var operationAvailable = operationBodyTop - Number(page.safeBounds[3]);
    placeSectionTitleAndBody(operationBar, operationTitle, operationBody, operationBarTop, operationBodyTop, operationAvailable);
    if (operationAvailable < firstPanelMinHeight) {{
      if (operation.behavior && operation.behavior.preferSiblingPanel) adjustments.push(operation.id + ":small-first-panel-continued");
      else violations.push(operation.id + ":insufficient-first-page-space");
    }}
    var serviceBehavior = service.behavior || {{}};
    serviceBehavior.titleVerticalAlign = "bottom";
    serviceBehavior.titleBottomInsetPt = serviceBehavior.titleBottomInsetPt === undefined || serviceBehavior.titleBottomInsetPt === null ? 0 : Number(serviceBehavior.titleBottomInsetPt);
    adjustments.push(service.id + ":title-bottom-align-inset-" + serviceBehavior.titleBottomInsetPt);
    var serviceTitle = createLiveOutlinedTitle(service, adjustments, violations), servicePaths = refsByType(service, "pathItem"), serviceText = refsByType(service, "textFrame"), serviceGroups = refsByType(service, "groupItem");
    if (!serviceTitle || !servicePaths.length || !serviceText.length) return;
    var serviceBar = pathAt(servicePaths[0]), maintenanceGap = Number((maintenance.behavior || {{}}).sectionGapPt || 18), serviceBarTop = Number(maintenanceBody.geometricBounds[3]) - maintenanceGap;
    var serviceDelta = serviceBarTop - Number(serviceBar.geometricBounds[1]); translateY(serviceBar, serviceDelta); translateY(serviceTitle, serviceDelta);
    var contacts = serviceSupportText(serviceText), contactStyle = textAt(serviceText[0]);
    if (serviceGroups.length) groupAt(serviceGroups[0]).hidden = true;
    var contactsTop = Number(serviceBar.geometricBounds[3]) - 18, contactsHeight = contactsTop - Number(page.safeBounds[3]);
    var contactsBody = createAreaFrame(Number(contactStyle.geometricBounds[0]), contactsTop, 325.8, contactsHeight, contacts, contactStyle);
    applyBodyTypographyToFrame(contactsBody, service.behavior || {{}}, service.id + "-body", adjustments);
    if (!bodyFontIsLocked(Number(serviceText[0])) && Number(contactsBody.textRange.characterAttributes.size || 8) < Number((service.behavior || {{}}).bodyMinFontSizePt || 7)) contactsBody.textRange.characterAttributes.size = Number((service.behavior || {{}}).bodyMinFontSizePt || 7);
    var leftRefs = leftNumberRegion ? refsByType(leftNumberRegion, "textFrame") : [], rightRefs = rightNumberRegion ? refsByType(rightNumberRegion, "textFrame") : [];
    var leftNumber = leftRefs.length ? textAt(leftRefs[0]) : null, rightNumber = rightRefs.length ? textAt(rightRefs[0]) : null;
    var state = {{sequence: 0, basePageNumber: Math.max(parsePageNumber(leftNumber && leftNumber.contents), parsePageNumber(rightNumber && rightNumber.contents))}};
    if (hasTextOverflow(operationBody) && operation.behavior && operation.behavior.preferSiblingPanel) {{
      var operationParts = splitFirstParagraph(operationBody.contents);
      if (!operationParts) {{
        violations.push(operation.id + ":cannot-split-sibling-content");
      }} else {{
        operationBody.contents = operationParts.first;
        var maintenanceBodyOffset = Number(maintenanceBody.geometricBounds[1]) - Number(maintenanceBar.geometricBounds[1]), maintenanceBodyHeight = frameHeight(maintenanceBody);
        var sibling = continueIntoSiblingPanel(operation, operationTitle, operationBody, operationBar, maintenanceBar, Number(page.safeBounds[3]), operationParts.rest, adjustments, violations);
        if (sibling) {{
          var maintenanceTop = Number(sibling.body.geometricBounds[3]) - maintenanceGap;
          placeSectionTitleAndBody(maintenanceBar, maintenanceTitle, maintenanceBody, maintenanceTop, maintenanceTop + maintenanceBodyOffset, maintenanceBodyHeight);
          growAreaUntilFits(maintenanceBody, Number((maintenance.behavior || {{}}).bodyMinFontSizePt || 7), Number((maintenance.behavior || {{}}).bodyMaxHeightPt || 180), maintenance.id + "-body", adjustments, violations, bodyFontIsLocked(Number(maintenanceText[1])));
          adjustments.push(maintenance.id + ":moved-below-operation-continuation");
          var splitService = serviceBehavior.allowCardSplitAcrossPages === true;
          var followedServiceBarTop = Number(maintenanceBody.geometricBounds[3]) - maintenanceGap;
          var followedContactsTop = followedServiceBarTop - (Number(serviceBar.geometricBounds[1]) - Number(serviceBar.geometricBounds[3])) - 18;
          var followedContactsHeight = followedContactsTop - Number(page.safeBounds[3]);
          var minimumServiceBodyHeight = Number(serviceBehavior.firstPanelBodyMinHeightPt || 42);
          if (splitService && followedContactsHeight >= minimumServiceBodyHeight) {{
            placeSectionTitleAndBody(serviceBar, serviceTitle, contactsBody, followedServiceBarTop, followedContactsTop, followedContactsHeight);
            adjustments.push(service.id + ":started-below-maintenance");
            continueSection(page, service, serviceTitle, contactsBody, serviceBar, rightNumber, state, adjustments, violations, continuations);
          }} else {{
            moveSectionToNewArtboard(page, service, serviceTitle, contactsBody, serviceBar, rightNumber, 1, adjustments, violations);
          }}
        }}
      }}
    }} else {{
      continueSection(page, operation, operationTitle, operationBody, operationBar, leftNumber, state, adjustments, violations, continuations);
      continueSection(page, service, serviceTitle, contactsBody, serviceBar, rightNumber, state, adjustments, violations, continuations);
    }}
    adjustments.push("page4:continuation-artboards-" + state.sequence);
  }}
  function ensureEvenArtboards(adjustments, violations) {{
    var policy = layoutRules && layoutRules.printPolicy ? layoutRules.printPolicy : {{}};
    if (!policy.requireEvenArtboardCount || doc.artboards.length % 2 === 0) return;
    var reference = doc.artboards[doc.artboards.length - 1].artboardRect, width = Number(reference[2]) - Number(reference[0]), height = Number(reference[1]) - Number(reference[3]), lowest = Number(reference[3]);
    for (var i = 0; i < doc.artboards.length; i += 1) lowest = Math.min(lowest, Number(doc.artboards[i].artboardRect[3]));
    try {{ doc.artboards.add([Number(reference[0]), lowest - 36, Number(reference[0]) + width, lowest - 36 - height]); adjustments.push("print-policy:blank-artboard-appended"); }}
    catch (error) {{ violations.push("print-policy:cannot-append-blank-artboard"); }}
  }}
  function applyLayoutRules(adjustments, violations, unresolved, continuations) {{
    var pages = layoutRules && layoutRules.pages ? layoutRules.pages : [];
    for (var p = pages.length - 1; p >= 0; p -= 1) {{
      var regions = pages[p].regions || [];
      for (var r = 0; r < regions.length; r += 1) {{
        var region = regions[r], named = refsByType(region, "namedObject"), groups = refsByType(region, "groupItem"), paths = refsByType(region, "pathItem"), compounds = refsByType(region, "compoundPathItem");
        for (var n = 0; n < named.length; n += 1) if (!findNamedItem(named[n])) unresolved.push(String(named[n]));
        for (var g = 0; g < groups.length; g += 1) {{
          var groupIndex = Number(groups[g]);
          if (groupIndex < 0 || groupIndex >= sourceGroupItems.length) violations.push(region.id + ":missing-group-item-" + groupIndex);
        }}
        for (var pi = 0; pi < paths.length; pi += 1) {{
          var pathIndex = Number(paths[pi]);
          if (pathIndex < 0 || pathIndex >= sourcePathItems.length) violations.push(region.id + ":missing-path-item-" + pathIndex);
        }}
        for (var ci = 0; ci < compounds.length; ci += 1) {{
          var compoundIndex = Number(compounds[ci]);
          if (compoundIndex < 0 || compoundIndex >= sourceCompoundPathItems.length) violations.push(region.id + ":missing-compound-path-item-" + compoundIndex);
        }}
        if (region.role === "outlined-cover-label") createLiveCoverLabel(region, adjustments, violations);
        if (region.role === "adaptive-entry-label") applyCoverDistributorEntry(region, pages[p], adjustments, violations);
        if (region.id === "cover-brand-header") fitCoverBrandHeader(region, adjustments, violations);
        if (region.id === "cover-main-visual") {{
          var manualRefs = refsByType(region, "textFrame");
          if (manualRefs.length > 0) {{
            var manualIndex = Number(manualRefs[0]), manualFrame = textAt(manualIndex);
            var visualRight = groups.length > 1 ? Number(groupAt(Number(groups[1])).geometricBounds[2]) : Number(pages[p].safeBounds[2]);
            var manualMaxWidth = visualRight - Number(manualFrame.geometricBounds[0]) - 14;
            try {{
              var manualAttrs = manualFrame.textRange.characterAttributes;
              if (Number(manualAttrs.size || 12) > 11) manualAttrs.size = 11;
              manualAttrs.horizontalScale = 100;
            }} catch (manualTitleResetError) {{ violations.push("cover-manual-title:style-reset-unavailable"); }}
            fitPointText(manualIndex, 1, 8.5, manualMaxWidth, "cover-manual-title", adjustments, violations, {{titleMinHorizontalScale: 75}});
            try {{
              var manualAttrs = manualFrame.textRange.characterAttributes;
              app.redraw();
              var manualRightLimit = visualRight - 14;
              var manualSize = Number(manualAttrs.size || 11), manualScale = Number(manualAttrs.horizontalScale || 100);
              while (Number(manualFrame.geometricBounds[2]) > manualRightLimit + 0.5 && manualSize > 8.5) {{
                manualSize = Math.max(8.5, manualSize - 0.5); manualAttrs.size = manualSize; app.redraw();
              }}
              while (Number(manualFrame.geometricBounds[2]) > manualRightLimit + 0.5 && manualScale > 70) {{
                manualScale = Math.max(70, manualScale - 1); manualAttrs.horizontalScale = manualScale; app.redraw();
              }}
              if (manualSize < 10.9) adjustments.push("cover-manual-title:font-" + manualSize);
              if (manualScale < 99.5) adjustments.push("cover-manual-title:horizontal-scale-" + manualScale);
              if (Number(manualFrame.geometricBounds[2]) > manualRightLimit + 0.5) violations.push("cover-manual-title:right-overflow");
            }} catch (manualTitleMarginError) {{ violations.push("cover-manual-title:safe-margin-unavailable"); }}
          }}
        }}
        if (region.id !== "cover-product-identity") continue;
        var refs = refsByType(region, "textFrame"), behavior = region.behavior || {{}};
        if (refs.length > 0) {{
          var titleIndex = Number(refs[0]), titleGuard = guards[String(titleIndex)] || {{}};
          var titleBounds = titleGuard.bounds || textAt(titleIndex).geometricBounds;
          fitPointText(titleIndex, Number(behavior.titleMaxLines || 2), Number(behavior.titleMinFontSizePt || 16), Number(titleBounds[2]) - Number(titleBounds[0]), "cover-title", adjustments, violations);
        }}
        if (refs.length > 1) {{
          var modelIndex = Number(refs[1]), modelGuard = guards[String(modelIndex)] || {{}};
          var modelBounds = modelGuard.bounds || textAt(modelIndex).geometricBounds;
          fitPointText(modelIndex, Number(behavior.modelMaxLines || 1), Number(behavior.modelMinFontSizePt || 9.5), Number(modelBounds[2]) - Number(modelBounds[0]), "cover-model", adjustments, violations);
        }}
      }}
      if (pages[p].mode === "vertical-flow") applyVerticalFlowPage(pages[p], adjustments, violations, continuations);
      if (pages[p].mode === "two-panel-flow") applyTwoPanelFlowPage(pages[p], adjustments, violations, continuations);
      if (pages[p].mode === "paired-sections-flow") applyPairedSectionsFlowPage(pages[p], adjustments, violations, continuations);
      applyPageTitleBarFill(pages[p], adjustments, violations);
    }}
    ensureEvenArtboards(adjustments, violations);
  }}
  function writeText(path, content) {{ var file = new File(path); file.encoding = "UTF-8"; if (!file.open("w")) throw new Error("Cannot write QA report: " + path); file.write(content); file.close(); }}
  function assetBounds(value) {{ return [Number(value[0]), Number(value[1]), Number(value[2]), Number(value[3])]; }}
  function fitPlacedItem(item, frameBounds, fit) {{
    var frame = assetBounds(frameBounds), frameW = frame[2] - frame[0], frameH = frame[1] - frame[3];
    var current = item.geometricBounds, itemW = Number(current[2]) - Number(current[0]), itemH = Number(current[1]) - Number(current[3]);
    if (frameW <= 0 || frameH <= 0 || itemW <= 0 || itemH <= 0) throw new Error("Invalid image or frame bounds");
    if (fit === "stretch") {{ item.width = frameW; item.height = frameH; }}
    else {{
      var factor = fit === "cover" ? Math.max(frameW / itemW, frameH / itemH) : Math.min(frameW / itemW, frameH / itemH);
      item.resize(factor * 100, factor * 100, true, true, true, true, factor * 100, Transformation.CENTER);
    }}
    current = item.geometricBounds;
    var itemCX = (Number(current[0]) + Number(current[2])) / 2, itemCY = (Number(current[1]) + Number(current[3])) / 2;
    item.translate((frame[0] + frame[2]) / 2 - itemCX, (frame[1] + frame[3]) / 2 - itemCY);
  }}
  function hideAssetRefs(refs) {{
    for (var hr = 0; hr < refs.length; hr += 1) {{
      var ref = refs[hr] || {{}}, index = Number(ref.value);
      if (ref.type === "groupItem") {{
        if (index < 0 || index >= sourceGroupItems.length) throw new Error("GroupItem index out of range: " + index);
        sourceGroupItems[index].hidden = true;
      }} else if (ref.type === "compoundPathItem") {{
        if (index < 0 || index >= sourceCompoundPathItems.length) throw new Error("CompoundPathItem index out of range: " + index);
        sourceCompoundPathItems[index].hidden = true;
      }}
    }}
  }}
  function placeAsset(binding, assetFile, target, frameBounds) {{
    var placed = doc.placedItems.add(); placed.file = assetFile; placed.name = "__asset__" + String(binding.slotId || "unnamed");
    try {{ placed.move(target, ElementPlacement.PLACEBEFORE); }} catch (moveError) {{}}
    fitPlacedItem(placed, frameBounds, String(binding.fit || "contain"));
    if (String(binding.fit || "contain") === "cover") {{
      var frame = assetBounds(frameBounds), group = doc.groupItems.add(); group.name = "__asset_clip__" + String(binding.slotId || "unnamed");
      try {{ group.move(target, ElementPlacement.PLACEBEFORE); }} catch (groupMoveError) {{}}
      placed.move(group, ElementPlacement.PLACEATEND);
      var mask = group.pathItems.rectangle(frame[1], frame[0], frame[2] - frame[0], frame[1] - frame[3]);
      mask.stroked = false; mask.filled = false; mask.clipping = true; mask.move(group, ElementPlacement.PLACEATBEGINNING); group.clipped = true;
    }}
    return placed;
  }}
  app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;
  var source = new File(sourcePath);
  if (!source.exists) throw new Error("Source file not found: " + sourcePath);
  var doc = app.open(source);
  for (var st = 0; st < doc.textFrames.length; st += 1) sourceTextFrames.push(doc.textFrames[st]);
  for (var sg = 0; sg < doc.groupItems.length; sg += 1) sourceGroupItems.push(doc.groupItems[sg]);
  for (var sp = 0; sp < doc.pathItems.length; sp += 1) sourcePathItems.push(doc.pathItems[sp]);
  for (var sc = 0; sc < doc.compoundPathItems.length; sc += 1) sourceCompoundPathItems.push(doc.compoundPathItems[sc]);
  preservedBodyFontIndexes = collectPreservedBodyFontIndexes();
  for (var lockedIndex in preservedBodyFontIndexes) {{
    if (!preservedBodyFontIndexes.hasOwnProperty(lockedIndex)) continue;
    var bodyIndex = Number(lockedIndex);
    if (bodyIndex >= 0 && bodyIndex < sourceTextFrames.length) templateBodyFontSizes[lockedIndex] = Number(textAt(bodyIndex).textRange.characterAttributes.size);
  }}
  markFixedPageNumberFrames();
  try {{
    var styleFallbacks = [];
    for (var key in replacements) {{
      if (!replacements.hasOwnProperty(key)) continue;
      var index = Number(key);
      if (index < 0 || index >= sourceTextFrames.length) throw new Error("TextFrame index out of range: " + index);
      var guard = guards[key] || {{}};
      var locator = guard.locator || {{}}, located = textAt(index);
      if (locator.name && String(located.name || "") !== String(locator.name)) throw new Error("TextFrame name mismatch at index " + index);
      if (locator.layer && located.layer && String(located.layer.name || "") !== String(locator.layer)) throw new Error("TextFrame layer mismatch at index " + index);
      if (locator.bounds && locator.bounds.length === 4) {{
        var actualBounds = located.geometricBounds;
        for (var boundIndex = 0; boundIndex < 4; boundIndex += 1) if (Math.abs(Number(actualBounds[boundIndex]) - Number(locator.bounds[boundIndex])) > 2) throw new Error("TextFrame bounds fingerprint mismatch at index " + index);
      }}
      if (guard.expectedSourceText !== undefined && guard.expectedSourceText !== null && normalize(textAt(index).contents) !== normalize(guard.expectedSourceText)) {{
        throw new Error("TextFrame source mismatch at index " + index + " (" + (guard.slotId || "unknown slot") + ")");
      }}
      textAt(index).contents = replacements[key];
      var style = styles[key] || {{}};
      if (style.fontName) {{
        try {{ textAt(index).textRange.characterAttributes.textFont = app.textFonts.getByName(String(style.fontName)); }}
        // Illustrator ExtendScript does not provide JSON.stringify. The QA
        // report only needs the affected object indexes for a fallback.
        catch (fontError) {{ styleFallbacks.push(String(index)); }}
      }}
      if (!bodyFontIsLocked(index) && style.fontSize !== undefined && style.fontSize !== null && Number(style.fontSize) >= 4 && Number(style.fontSize) <= 96) {{
        try {{ textAt(index).textRange.characterAttributes.size = Number(style.fontSize); }} catch (sizeError) {{}}
      }}
    }}
    try {{ app.redraw(); }} catch (replacementRedrawError) {{}}
    for (var a = 0; a < assetBindings.length; a += 1) {{
      var binding = assetBindings[a], refs = binding.objectRefs || [];
      var assetFile = new File(binding.path || ""), hasAsset = Boolean(binding.path) && assetFile.exists;
      if (!hasAsset) {{
        if (binding.mode === "hide_when_empty" || String(binding.fit || "") === "hide") {{ hideAssetRefs(refs); continue; }}
        if (binding.required) throw new Error("Replacement image not found: " + binding.path);
        continue;
      }}
      if (binding.mode === "replace_placed") {{
        var placedRef = refs.length ? refs[0] : {{type:"placed",value:binding.index}};
        var assetIndex = Number(placedRef.value);
        if (assetIndex < 0 || assetIndex >= doc.placedItems.length) throw new Error("PlacedItem index out of range: " + assetIndex);
        var placedTarget = doc.placedItems[assetIndex], placedFrame = binding.bounds || placedTarget.geometricBounds;
        placedTarget.relink(assetFile); fitPlacedItem(placedTarget, placedFrame, String(binding.fit || "contain"));
      }} else if (binding.mode === "replace_group_with_raster" || binding.mode === "replace_group_with_vector") {{
        var targetRef = null, groupTarget = null;
        for (var gr = 0; gr < refs.length; gr += 1) if (refs[gr].type === "groupItem" || refs[gr].type === "compoundPathItem") {{ targetRef = refs[gr]; break; }}
        if (!targetRef) throw new Error("Asset slot has no supported target: " + binding.slotId);
        var groupIndex = Number(targetRef.value);
        if (targetRef.type === "groupItem") {{
          if (groupIndex < 0 || groupIndex >= sourceGroupItems.length) throw new Error("GroupItem index out of range: " + groupIndex);
          groupTarget = sourceGroupItems[groupIndex];
        }} else {{
          if (groupIndex < 0 || groupIndex >= sourceCompoundPathItems.length) throw new Error("CompoundPathItem index out of range: " + groupIndex);
          groupTarget = sourceCompoundPathItems[groupIndex];
        }}
        var groupFrame = binding.bounds || groupTarget.geometricBounds;
        placeAsset(binding, assetFile, groupTarget, groupFrame); hideAssetRefs(refs);
      }} else if (binding.mode === "hide_when_empty") {{
        hideAssetRefs(refs);
      }} else {{ throw new Error("Unsupported asset binding mode: " + binding.mode); }}
    }}
    var layoutAdjustments = [], layoutViolations = [], unresolvedNamedObjects = [], continuationRequests = [];
    applyStandardBodyTypography(layoutAdjustments);
    applyLayoutRules(layoutAdjustments, layoutViolations, unresolvedNamedObjects, continuationRequests);
    restoreTemplateBodyFontSizes(layoutAdjustments);
    if (outputs.ai) {{
      var aiOptions = new IllustratorSaveOptions(); aiOptions.pdfCompatible = true;
      doc.saveAs(new File(outputs.ai), aiOptions);
    }}
    if (outputs.pdf) {{
      var pdfOptions = new PDFSaveOptions(); pdfOptions.preserveEditability = true;
      doc.saveAs(new File(outputs.pdf), pdfOptions);
    }}
    if (outputs.preview) {{
      if (previewArtboard >= 0 && previewArtboard < doc.artboards.length) doc.artboards.setActiveArtboardIndex(previewArtboard);
      var pngOptions = new ExportOptionsPNG24();
      pngOptions.antiAliasing = true; pngOptions.artBoardClipping = true;
      pngOptions.horizontalScale = 50; pngOptions.verticalScale = 50; pngOptions.transparency = true;
      doc.exportFile(new File(outputs.preview), ExportType.PNG24, pngOptions);
    }}
    if (qaPath) {{
      var overset = [], oversetDetails = [], outOfSafeBoundsDetails = [];
      for (var q = 0; q < doc.textFrames.length; q += 1) {{
        if (String(doc.textFrames[q].name || "") === "__layout_threaded__") continue;
        try {{ if (doc.textFrames[q].hidden) continue; }} catch (hiddenError) {{}}
        if (isFixedPageNumberFrame(doc.textFrames[q])) continue;
        try {{
          if (hasTextOverflow(doc.textFrames[q])) {{
            var frame = doc.textFrames[q], bounds = frame.geometricBounds;
            overset.push(q);
            oversetDetails.push('{{"index":' + q + ',"name":' + jsonString(frame.name) + ',"contents":' + jsonString(frame.contents) + ',"bounds":[' + Number(bounds[0]) + ',' + Number(bounds[1]) + ',' + Number(bounds[2]) + ',' + Number(bounds[3]) + ']}}');
          }}
        }} catch (overflowError) {{}}
        try {{
          var changed = replacements.hasOwnProperty(String(q)) || String(doc.textFrames[q].name || "") === "__layout_generated_text__";
          if (!changed) continue;
          var checked = doc.textFrames[q].visibleBounds, centerX = (Number(checked[0]) + Number(checked[2])) / 2;
          var centerY = (Number(checked[1]) + Number(checked[3])) / 2, artboardIndex = -1, safe = null;
          for (var ab = 0; ab < doc.artboards.length; ab += 1) {{
            var rect = doc.artboards[ab].artboardRect;
            if (centerX >= Number(rect[0]) && centerX <= Number(rect[2]) && centerY <= Number(rect[1]) && centerY >= Number(rect[3])) {{
              artboardIndex = ab;
              var configured = layoutRules.pages && ab < layoutRules.pages.length ? layoutRules.pages[ab].safeBounds : null;
              var configuredMatches = configured && configured.length === 4 && Number(configured[0]) >= Number(rect[0]) && Number(configured[2]) <= Number(rect[2]) && Number(configured[1]) <= Number(rect[1]) && Number(configured[3]) >= Number(rect[3]);
              safe = configuredMatches ? configured : [Number(rect[0]) + 14, Number(rect[1]) - 14, Number(rect[2]) - 14, Number(rect[3]) + 14];
              break;
            }}
          }}
          if (safe && (Number(checked[0]) < Number(safe[0]) - 0.5 || Number(checked[1]) > Number(safe[1]) + 0.5 || Number(checked[2]) > Number(safe[2]) + 0.5 || Number(checked[3]) < Number(safe[3]) - 0.5)) {{
            outOfSafeBoundsDetails.push('{{"index":' + q + ',"artboardIndex":' + artboardIndex + ',"contents":' + jsonString(doc.textFrames[q].contents) + ',"bounds":[' + Number(checked[0]) + ',' + Number(checked[1]) + ',' + Number(checked[2]) + ',' + Number(checked[3]) + '],"safeBounds":[' + Number(safe[0]) + ',' + Number(safe[1]) + ',' + Number(safe[2]) + ',' + Number(safe[3]) + ']}}');
          }}
        }} catch (safeBoundsError) {{}}
      }}
      var adjustmentJson = [], violationJson = [], unresolvedJson = [], continuationJson = [];
      for (var la = 0; la < layoutAdjustments.length; la += 1) adjustmentJson.push(jsonString(layoutAdjustments[la]));
      for (var lv = 0; lv < layoutViolations.length; lv += 1) violationJson.push(jsonString(layoutViolations[lv]));
      for (var lu = 0; lu < unresolvedNamedObjects.length; lu += 1) unresolvedJson.push(jsonString(unresolvedNamedObjects[lu]));
      for (var lc = 0; lc < continuationRequests.length; lc += 1) continuationJson.push(jsonString(continuationRequests[lc]));
      writeText(qaPath, '{{"oversetTextFrames":[' + overset.join(",") + '],"oversetDetails":[' + oversetDetails.join(",") + '],"outOfSafeBoundsDetails":[' + outOfSafeBoundsDetails.join(",") + '],"fontFallbacks":[' + styleFallbacks.join(",") + '],"layoutAdjustments":[' + adjustmentJson.join(",") + '],"layoutViolations":[' + violationJson.join(",") + '],"unresolvedNamedObjects":[' + unresolvedJson.join(",") + '],"continuationRequests":[' + continuationJson.join(",") + ']}}');
    }}
  }} finally {{
    doc.close(SaveOptions.DONOTSAVECHANGES);
  }}
}})();
'''


def load_job(value: str | os.PathLike[str] | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    path = resolve_path(value)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerError(f"Cannot load render job {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkerError("Render job must be a JSON object")
    return data


def render_job(
    value: str | os.PathLike[str] | Mapping[str, Any],
    *,
    execute: bool = True,
    system: str | None = None,
    driver: IllustratorDriver | None = None,
    allowed_output_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    started_at = utc_now()
    started_monotonic = time.monotonic()
    job = load_job(value)
    source_value = job.get("source") or job.get("source_path") or job.get("input")
    if isinstance(source_value, Mapping):
        source_value = source_value.get("path")
    if not source_value:
        raise WorkerError("Render job is missing source/source_path")
    source = resolve_path(str(source_value))
    validate_source(source)
    expected_hash = job.get("sourceHash") or job.get("source_hash")
    actual_hash = source_metadata(source)["sha256"]
    if expected_hash and str(expected_hash) != actual_hash:
        raise WorkerError(f"Source hash mismatch: expected {expected_hash}, got {actual_hash}")

    job_id = str(job.get("id") or job.get("job_id") or uuid.uuid4().hex[:12])
    execution_id = uuid.uuid4().hex
    output_dir = resolve_path(str(job.get("output_dir") or (source.parent / "renders" / job_id)))
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(str(job.get("output_name") or f"{source.stem}-rendered-{job_id}")).name
    stem = re.sub(r"[^\w.-]+", "_", stem, flags=re.UNICODE).lstrip(".") or f"rendered-{job_id}"
    formats = job.get("formats") or ["ai", "pdf", "preview"]
    if isinstance(formats, str):
        formats = [part.strip() for part in formats.split(",") if part.strip()]
    suffixes = {"ai": ".ai", "pdf": ".pdf", "preview": ".png", "png": ".png"}
    outputs: dict[str, Path] = {}
    configured = job.get("outputs") if isinstance(job.get("outputs"), Mapping) else {}
    safe_job_id = re.sub(r"[^A-Za-z0-9._-]+", "-", job_id).strip("-") or "render"
    for requested in formats:
        key = "preview" if requested == "png" else str(requested)
        if key not in suffixes:
            raise WorkerError(f"Unsupported render format: {requested}")
        configured_path = configured.get(key) if isinstance(configured, Mapping) else None
        if configured_path:
            outputs[key] = resolve_path(str(configured_path))
        elif key == "preview":
            outputs[key] = output_dir / f"{safe_job_id}-preview.png"
        else:
            outputs[key] = output_dir / f"{stem}{suffixes[key]}"

    overwrite_value = job.get("overwrite", False)
    if not isinstance(overwrite_value, bool):
        raise WorkerError("overwrite must be a boolean")
    overwrite = overwrite_value
    output_root = resolve_path(allowed_output_root) if allowed_output_root is not None else None
    output_keys = [str(path).casefold() for path in outputs.values()]
    if len(output_keys) != len(set(output_keys)):
        raise WorkerError("Render outputs must resolve to unique paths")
    for key, output in outputs.items():
        if output.suffix.lower() != suffixes[key]:
            raise WorkerError(f"Output for {key} must use the {suffixes[key]} extension")
        output.parent.mkdir(parents=True, exist_ok=True)
        validate_output(source, output, overwrite=overwrite, allowed_root=output_root)
    replacements = _job_replacements(job)
    layout_texts = _job_layout_texts(job)
    guards = _job_guards(job)
    styles = _job_styles(job)
    asset_bindings = job.get("assetBindings") or job.get("asset_bindings") or []
    if not isinstance(asset_bindings, Sequence) or isinstance(asset_bindings, (str, bytes)):
        raise WorkerError("assetBindings must be an array")
    seen_asset_slots: set[str] = set()
    seen_asset_targets: set[tuple[str, int]] = set()
    supported_asset_modes = {"replace_placed", "replace_group_with_raster", "replace_group_with_vector", "hide_when_empty"}
    supported_asset_fits = {"contain", "cover", "stretch", "hide"}
    quality_issues: list[dict[str, Any]] = []
    for raw_binding in asset_bindings:
        if not isinstance(raw_binding, Mapping):
            raise WorkerError("Each asset binding must be an object")
        slot_id = str(raw_binding.get("slotId") or "").strip()
        if not slot_id or slot_id in seen_asset_slots:
            raise WorkerError(f"Asset slot id is empty or duplicated: {slot_id}")
        seen_asset_slots.add(slot_id)
        mode = str(raw_binding.get("mode") or "")
        fit = str(raw_binding.get("fit") or "contain")
        if mode not in supported_asset_modes:
            raise WorkerError(f"Unsupported asset binding mode: {mode}")
        if fit not in supported_asset_fits:
            raise WorkerError(f"Unsupported asset fit: {fit}")
        refs = raw_binding.get("objectRefs") or []
        if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)):
            raise WorkerError(f"Asset slot {slot_id} objectRefs must be an array")
        for ref in refs:
            if not isinstance(ref, Mapping) or "type" not in ref or "value" not in ref:
                raise WorkerError(f"Asset slot {slot_id} has an invalid object reference")
            target = (str(ref["type"]), int(ref["value"]))
            if target in seen_asset_targets:
                raise WorkerError(f"Asset target is bound more than once: {target[0]} {target[1]}")
            seen_asset_targets.add(target)
        asset_path = str(raw_binding.get("path") or "")
        if asset_path:
            resolved_asset = resolve_path(asset_path)
            if not resolved_asset.is_file():
                raise WorkerError(f"Replacement image not found: {resolved_asset}")
            expected_asset_hash = str(raw_binding.get("sha256") or "")
            if expected_asset_hash and source_metadata(resolved_asset)["sha256"] != expected_asset_hash:
                raise WorkerError(f"Replacement image hash mismatch for slot {slot_id}")
            dpi_issue = asset_dpi_issue(raw_binding, resolved_asset)
            if dpi_issue:
                quality_issues.append(dpi_issue)
        elif raw_binding.get("required") and mode != "hide_when_empty":
            raise WorkerError(f"Required asset slot is empty: {slot_id}")
    layout_rules = job.get("layoutRules") or job.get("layout_rules") or {}
    if not isinstance(layout_rules, Mapping):
        raise WorkerError("layoutRules must be an object")
    preview_artboard = int(job.get("previewArtboard", job.get("preview_artboard", 0)))
    if preview_artboard < 0:
        raise WorkerError("previewArtboard must be zero or greater")
    qa_path = output_dir / f"{safe_job_id}-layout-qa.json"
    execution_outputs = outputs
    execution_qa_path = qa_path
    staging_dir: Path | None = None
    if execute:
        staging_dir = output_dir / f".staging-{safe_job_id}-{uuid.uuid4().hex[:8]}"
        staging_dir.mkdir(parents=True, exist_ok=False)
        execution_outputs = {key: staging_dir / path.name for key, path in outputs.items()}
        execution_qa_path = staging_dir / qa_path.name
    jsx_path = write_jsx(build_render_jsx(source, execution_outputs, replacements, guards, styles, asset_bindings, layout_rules, execution_qa_path, preview_artboard, layout_texts), output_dir, "render-job-")
    result: dict[str, Any] = {
        "schema": WORKER_RESULT_SCHEMA,
        "jobId": job_id,
        "executionId": execution_id,
        "status": "prepared",
        "startedAt": started_at,
        "source": str(source),
        "outputs": {key: str(path) for key, path in outputs.items()},
        "replacementCount": len(replacements),
        "layoutTextReplacementCount": len(layout_texts),
        "assetReplacementCount": len([item for item in asset_bindings if isinstance(item, Mapping) and item.get("path")]),
        "layoutRulePages": len(layout_rules.get("pages") or []),
        "jsx": str(jsx_path),
        "qaReport": str(qa_path),
        "executed": False,
        "qualityIssues": quality_issues,
    }
    if execute:
        try:
            result["automation"] = run_jsx(jsx_path, system=system, driver=driver)
        except Exception:
            if staging_dir:
                shutil.rmtree(staging_dir, ignore_errors=True)
            raise
        final_source_hash = source_metadata(source)["sha256"]
        if final_source_hash != actual_hash:
            if staging_dir:
                shutil.rmtree(staging_dir, ignore_errors=True)
            raise WorkerError(
                f"Source file changed during Illustrator execution: expected {actual_hash}, got {final_source_hash}"
            )
        result["executed"] = True
        missing = [str(path) for path in execution_outputs.values() if not path.exists()]
        if missing:
            if staging_dir:
                shutil.rmtree(staging_dir, ignore_errors=True)
            raise WorkerError(f"Illustrator completed without creating outputs: {', '.join(missing)}")
        if staging_dir:
            for key, staged in execution_outputs.items():
                os.replace(staged, outputs[key])
            if execution_qa_path.exists():
                os.replace(execution_qa_path, qa_path)
            shutil.rmtree(staging_dir, ignore_errors=True)
        qa_data = json.loads(qa_path.read_text(encoding="utf-8-sig")) if qa_path.exists() else {}
        created_artboards = len([
            item for item in qa_data.get("layoutAdjustments", [])
            if ":created-continuation-artboard-" in str(item)
            or ":moved-to-artboard-" in str(item)
            or ":inserted-artboard-" in str(item)
            or str(item) == "print-policy:blank-artboard-appended"
        ])
        artifacts = []
        source_pdf_meta: dict[str, Any] = {}
        pdfinfo = find_binary("pdfinfo")
        if pdfinfo:
            source_info = _run([pdfinfo, str(source)], timeout=30)
            if source_info.returncode == 0:
                source_pages = re.search(r"^Pages:\s+(\d+)", source_info.stdout, re.M)
                source_size = re.search(r"^Page size:\s+(.+)$", source_info.stdout, re.M)
                source_pdf_meta = {"pages": int(source_pages.group(1)) if source_pages else None, "pageSize": source_size.group(1).strip() if source_size else None}
        for key, output in outputs.items():
            metadata: dict[str, Any] = {"bytes": output.stat().st_size}
            if key == "pdf" and pdfinfo:
                info = _run([pdfinfo, str(output)], timeout=30)
                if info.returncode == 0:
                    pages = re.search(r"^Pages:\s+(\d+)", info.stdout, re.M)
                    page_size = re.search(r"^Page size:\s+(.+)$", info.stdout, re.M)
                    metadata.update({"pages": int(pages.group(1)) if pages else None, "pageSize": page_size.group(1).strip() if page_size else None})
                    expected_pages = source_pdf_meta.get("pages", 0) + created_artboards
                    if source_pdf_meta.get("pages") and metadata.get("pages") != expected_pages:
                        quality_issues.append({"level":"blocking","type":"page_count","message":f"Output pages {metadata.get('pages')} != expected pages {expected_pages}","status":"open"})
            artifacts.append({"type": "preview_png" if key == "preview" else key, "path": str(output), "sha256": source_metadata(output)["sha256"], "metadata": metadata})
        if qa_path.exists():
            artifacts.append({"type":"layout_qa","path":str(qa_path),"sha256":source_metadata(qa_path)["sha256"],"metadata":qa_data})
            quality_issues.extend(qa_quality_issues(qa_data))
        pdf_output = outputs.get("pdf")
        pdftoppm = find_binary("pdftoppm")
        if pdf_output and pdftoppm:
            page_dir = output_dir / "verify-pages"
            page_dir.mkdir(parents=True, exist_ok=True)
            rendered = _run([pdftoppm, "-png", "-r", "120", str(pdf_output), str(page_dir / "page")], timeout=120)
            if rendered.returncode != 0:
                quality_issues.append({"level":"blocking","type":"page_render","message":rendered.stderr.strip() or "PDF page rendering failed","status":"open"})
            else:
                for page in sorted(page_dir.glob("page-*.png")):
                    artifacts.append({"type":"page_png","path":str(page),"sha256":source_metadata(page)["sha256"],"metadata":{"bytes":page.stat().st_size}})
        pdftotext = find_binary("pdftotext")
        if pdf_output and pdftotext:
            extracted = _run([pdftotext, str(pdf_output), "-"], timeout=60)
            if extracted.returncode == 0:
                text = re.sub(r"\s+", " ", extracted.stdout)
                for item in job.get("items") or []:
                    if not isinstance(item, Mapping): continue
                    old = re.sub(r"\s+", " ", str(item.get("expectedSourceText") or "")).strip()
                    new = re.sub(r"\s+", " ", str(item.get("targetText") or "")).strip()
                    if 5 <= len(old) <= 100 and old != new and old in text:
                        quality_issues.append({"level":"blocking","type":"old_text_residue","message":f"Old text remains for slot {item.get('slotId')}: {old[:60]}","status":"open"})
                    if 5 <= len(new) <= 100 and new not in text:
                        quality_issues.append({"level":"warning","type":"target_text_not_extractable","message":f"Target text not found in PDF layer for slot {item.get('slotId')}","status":"open"})
        result["artifacts"] = artifacts
        result["qualityIssues"] = quality_issues
        result["status"] = "succeeded"
    result["finishedAt"] = utc_now()
    result["durationMs"] = round((time.monotonic() - started_monotonic) * 1000)
    return result


def doctor_report(
    *,
    probe: bool = False,
    system: str | None = None,
    driver: IllustratorDriver | None = None,
) -> dict[str, Any]:
    current_system = system or platform.system()
    selected = driver or default_driver(system=current_system)
    driver_report = selected.doctor(probe=probe)
    return {
        **driver_report,
        "workerVersion": WORKER_VERSION,
        "platform": current_system,
        "platformDetail": platform.platform(),
        "python": sys.executable,
        "hostname": socket.gethostname(),
    }


class HttpJsonClient:
    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json", "User-Agent": f"illustrator-worker/{WORKER_VERSION}"}
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                if response.status == 204 or not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if exc.code in {204, 404} and method == "POST" and path.rstrip("/").endswith("claim"):
                return None
            raise WorkerError(f"HTTP {exc.code} {method} {url}: {raw or exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise WorkerError(f"HTTP {method} {url} failed: {exc}") from exc

    def download(self, url_or_path: str, destination: Path, *, worker_id: str | None = None, lease_token: str | None = None) -> Path:
        url = url_or_path if url_or_path.startswith(("http://", "https://")) else f"{self.base_url}/{url_or_path.lstrip('/')}"
        base = urllib.parse.urlsplit(self.base_url)
        target = urllib.parse.urlsplit(url)
        base_port = base.port or (443 if base.scheme == "https" else 80)
        target_port = target.port or (443 if target.scheme == "https" else 80)
        if target.scheme != base.scheme or target.hostname != base.hostname or target_port != base_port:
            raise WorkerError(f"Cross-origin worker download is not allowed: {target.scheme}://{target.netloc}")
        headers = {"User-Agent": f"illustrator-worker/{WORKER_VERSION}"}
        if self.token: headers["Authorization"] = f"Bearer {self.token}"
        if worker_id: headers["X-Worker-Id"] = worker_id
        if lease_token: headers["X-Lease-Token"] = lease_token
        try:
            class RejectRedirects(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
                    raise WorkerError(f"Worker download redirects are not allowed: {newurl}")
            opener = urllib.request.build_opener(RejectRedirects())
            with opener.open(urllib.request.Request(url, headers=headers), timeout=max(self.timeout, 120)) as response:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as handle: shutil.copyfileobj(response, handle)
            return destination
        except (urllib.error.URLError, OSError, WorkerError) as exc:
            raise WorkerError(f"Download {url} failed: {exc}") from exc

    def upload(self, path: str, source: Path, artifact_type: str, *, worker_id: str | None = None, lease_token: str | None = None) -> Any:
        url=f"{self.base_url}/{path.lstrip('/')}";headers={"Content-Type":"application/octet-stream","X-Filename":source.name,"X-Artifact-Type":artifact_type,"User-Agent":f"illustrator-worker/{WORKER_VERSION}"}
        if self.token: headers["Authorization"] = f"Bearer {self.token}"
        if worker_id: headers["X-Worker-Id"] = worker_id
        if lease_token: headers["X-Lease-Token"] = lease_token
        try:
            parsed=urllib.parse.urlsplit(url);connection_class=http.client.HTTPSConnection if parsed.scheme=="https" else http.client.HTTPConnection
            connection=connection_class(parsed.hostname,parsed.port,timeout=max(self.timeout,300));request_path=parsed.path+(f"?{parsed.query}" if parsed.query else "")
            connection.putrequest("POST",request_path);headers["Content-Length"]=str(source.stat().st_size)
            for key,value in headers.items():connection.putheader(key,value)
            connection.endheaders()
            with source.open("rb") as handle:
                while chunk:=handle.read(1024*1024):connection.send(chunk)
            response=connection.getresponse();raw=response.read().decode("utf-8","replace")
            if response.status>=400:raise WorkerError(f"HTTP {response.status} POST {url}: {raw or response.reason}")
            return json.loads(raw)
        except (OSError,http.client.HTTPException,json.JSONDecodeError) as exc: raise WorkerError(f"Upload {source} failed: {exc}") from exc
        finally:
            if 'connection' in locals():connection.close()


class RemoteWorkerClient:
    def __init__(
        self,
        base_url: str,
        worker_id: str,
        *,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        heartbeat_interval: float = 15.0,
    ) -> None:
        self.http = HttpJsonClient(base_url, token=token, timeout=timeout)
        self.worker_id = worker_id
        self.heartbeat_interval = heartbeat_interval

    def capabilities(self) -> dict[str, Any]:
        report = doctor_report(probe=False)
        return {
            "workerVersion": WORKER_VERSION,
            "platform": report["platform"],
            "hostname": report["hostname"],
            "commands": ["extract-template", "render"],
            "templateSchema": TEMPLATE_SCHEMA,
            "automationReady": report["ok"],
        }

    def claim(self) -> dict[str, Any] | None:
        response = self.http.request(
            "POST",
            "/claim",
            {"workerId": self.worker_id, "capabilities": self.capabilities(), "claimedAt": utc_now()},
        )
        if not response:
            return None
        if not isinstance(response, dict):
            raise WorkerError("Claim response must be a JSON object")
        if "job" in response and response.get("job") is None:
            return None
        job = response.get("job", response)
        if not isinstance(job, dict) or not (job.get("id") or job.get("job_id")):
            raise WorkerError("Claim response does not contain a job id")
        if "leaseToken" in response and "leaseToken" not in job:
            job = dict(job)
            job["leaseToken"] = response["leaseToken"]
        return job

    def heartbeat(self, job_id: str, lease_token: str | None = None) -> Any:
        payload: dict[str, Any] = {"workerId": self.worker_id, "jobId": job_id, "at": utc_now(), "status": "running"}
        if lease_token:
            payload["leaseToken"] = lease_token
        return self.http.request("POST", f"/jobs/{urllib.parse.quote(job_id, safe='')}/heartbeat", payload)

    def complete(
        self,
        job_id: str,
        *,
        status: str,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        lease_token: str | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"workerId": self.worker_id, "jobId": job_id, "status": status, "completedAt": utc_now()}
        if result is not None:
            payload["result"] = dict(result)
        if error is not None:
            payload["error"] = dict(error)
        if lease_token:
            payload["leaseToken"] = lease_token
        return self.http.request("POST", f"/jobs/{urllib.parse.quote(job_id, safe='')}/complete", payload)

    def run_once(self) -> dict[str, Any] | None:
        job = self.claim()
        if job is None:
            return None
        job_id = str(job.get("id") or job.get("job_id"))
        lease_token = str(job.get("leaseToken")) if job.get("leaseToken") else None
        stop = threading.Event()
        lease_lost = threading.Event()
        work_dir: Path | None = None

        def send_heartbeats() -> None:
            while not stop.wait(self.heartbeat_interval):
                try:
                    self.heartbeat(job_id, lease_token)
                except WorkerError:
                    lease_lost.set()
                    return

        self.heartbeat(job_id, lease_token)
        thread = threading.Thread(target=send_heartbeats, name=f"heartbeat-{job_id}", daemon=True)
        thread.start()
        try:
            job_type = str(job.get("type") or job.get("command") or "render")
            payload = job.get("payload") if isinstance(job.get("payload"), Mapping) else job
            if not isinstance(payload, Mapping) or not payload.get("sourceUrl"):
                raise WorkerError("Remote Illustrator jobs must provide a same-origin sourceUrl")
            if any(key in payload for key in ("jsx", "script", "executeJsx", "execute_jsx")):
                raise WorkerError("Remote jobs may not provide raw JSX or script source")
            if isinstance(payload, Mapping) and payload.get("sourceUrl"):
                work_dir=Path(tempfile.mkdtemp(prefix=f"illustrator-job-{job_id}-"));payload=dict(payload)
                source_name=Path(urllib.parse.urlparse(str(payload["sourceUrl"])).path).name or "source.ai"
                if Path(source_name).suffix.lower() not in {".ai",".ait",".eps",".pdf"}: source_name="source.ai"
                payload["source"]=str(self.http.download(str(payload["sourceUrl"]),work_dir/source_name,worker_id=self.worker_id,lease_token=lease_token));payload["output_dir"]=str(work_dir/"outputs")
                payload.pop("outputs", None)
                payload["overwrite"] = False
                bindings=[]
                for index,binding in enumerate(payload.get("assetBindings") or []):
                    item=dict(binding)
                    if item.get("downloadUrl"):
                        suffix=Path(str(item.get("name") or "")).suffix or Path(urllib.parse.urlparse(str(item["downloadUrl"])).path).suffix or ".bin"
                        item["path"]=str(self.http.download(str(item["downloadUrl"]),work_dir/"assets"/f"asset-{index}{suffix}",worker_id=self.worker_id,lease_token=lease_token))
                    elif item.get("path"):
                        raise WorkerError(f"Remote asset slot {item.get('slotId')} must use downloadUrl")
                    bindings.append(item)
                payload["assetBindings"]=bindings
            if job_type in {"extract-template", "extract_template"}:
                result = extract_template(
                    str(payload["source"]),
                    str(payload["output_dir"]),
                    overwrite=bool(payload.get("overwrite", False)),
                )
            elif job_type in {"render", "render-job", "render_job"}:
                render_payload = dict(payload)
                render_payload.setdefault("id", job_id)
                result = render_job(render_payload, allowed_output_root=work_dir / "outputs")
            else:
                raise WorkerError(f"Unsupported remote job type: {job_type}")
            if lease_lost.is_set():
                raise IndeterminateWorkerError("Worker lease heartbeat failed while Illustrator was running; result was not published")
            if work_dir and result.get("artifacts"):
                uploaded=[]
                for artifact in result["artifacts"]:
                    server_artifact=self.http.upload(f"/jobs/{urllib.parse.quote(job_id,safe='')}/artifacts",Path(artifact["path"]),str(artifact.get("type") or "other"),worker_id=self.worker_id,lease_token=lease_token);uploaded.append(server_artifact)
                result["artifacts"]=uploaded
            issues = list(result.get("qualityIssues") or [])
            public_result = {
                "schema": WORKER_RESULT_SCHEMA,
                "jobId": job_id,
                "executionId": result.get("executionId"),
                "status": "succeeded",
                "artifacts": list(result.get("artifacts") or []),
                "qualityGate": {"status": "blocked" if any(item.get("level") == "blocking" for item in issues) else "passed", "issues": issues},
                "completedAt": utc_now(),
            }
            self.complete(job_id, status="succeeded", result=public_result, lease_token=lease_token)
            return public_result
        except Exception as exc:
            final_status = "indeterminate" if isinstance(exc, IndeterminateWorkerError) else "failed"
            error = {"type": type(exc).__name__, "message": str(exc), "retryable": False}
            try:
                self.complete(job_id, status=final_status, error=error, lease_token=lease_token)
            except Exception as completion_exc:
                error["completionError"] = str(completion_exc)
            return {"jobId": job_id, "status": final_status, "error": error}
        finally:
            stop.set()
            thread.join(timeout=min(self.heartbeat_interval + 1, 5))
            if work_dir: shutil.rmtree(work_dir,ignore_errors=True)

    def poll(self, *, interval: float = 5.0, once: bool = False) -> None:
        while True:
            result = self.run_once()
            if once:
                return
            if result is None:
                time.sleep(interval)


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Illustrator template v2 and remote worker client")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check Illustrator automation readiness")
    doctor.add_argument("--probe", action="store_true", help="Attempt a real Illustrator automation call")

    extract = subparsers.add_parser("extract-template", help="Extract template v2 metadata and preview")
    extract.add_argument("--source", required=True)
    extract.add_argument("--output-dir", "--out-dir", dest="output_dir", required=True)
    extract.add_argument("--no-execute", action="store_true", help="Generate JSX without launching Illustrator")
    extract.add_argument("--overwrite", action="store_true")

    for name in ("render", "render-job"):
        render = subparsers.add_parser(name, help="Execute a JSON render job")
        render.add_argument("--job", required=True)
        render.add_argument("--no-execute", action="store_true", help="Generate JSX without launching Illustrator")

    poll = subparsers.add_parser("poll", help="Poll a remote claim/heartbeat/complete worker API")
    poll.add_argument("--server", required=True, help="Worker API base URL; /claim is appended")
    poll.add_argument("--worker-id", default=f"{socket.gethostname()}-illustrator")
    poll.add_argument("--token", default=os.environ.get("ILLUSTRATOR_WORKER_TOKEN"))
    poll.add_argument("--interval", type=float, default=5.0)
    poll.add_argument("--heartbeat-interval", type=float, default=15.0)
    poll.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    poll.add_argument("--once", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            report = doctor_report(probe=args.probe)
            _print_json(report)
            return 0 if report["ok"] else 2
        if args.command == "extract-template":
            _print_json(extract_template(args.source, args.output_dir, execute=not args.no_execute, overwrite=args.overwrite))
            return 0
        if args.command in {"render", "render-job"}:
            _print_json(render_job(args.job, execute=not args.no_execute))
            return 0
        if args.command == "poll":
            RemoteWorkerClient(
                args.server,
                args.worker_id,
                token=args.token,
                timeout=args.timeout,
                heartbeat_interval=args.heartbeat_interval,
            ).poll(interval=args.interval, once=args.once)
            return 0
    except (WorkerError, KeyError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc), "type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
