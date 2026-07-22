#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import posixpath
import re
import shutil
import subprocess
import sys
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterable
from xml.etree import ElementTree as ET


SCHEMA = "illustrator-manual-project/1.0"
WORKBOOK_NAME = "说明书内容确认.xlsx"
IMPOSITION_QA_CONTRACT_VERSION = 2
FOLDED_LEAFLET_QA_CONTRACT_VERSION = 5
SMALL_FORMAT_QA_CONTRACT_VERSION = 1
REVIEW_META_PATTERN = re.compile(
    r"(?:请(?:客户)?确认|待确认|需要确认|需确认|please\s+confirm|needs?\s+confirmation|\bTODO\b|\bTBD\b)",
    re.IGNORECASE,
)
SKILL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
REVIEW_SCRIPT = Path(__file__).with_name("review_workbook.mjs")


class WorkflowError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def project_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def state_path(project: Path) -> Path:
    return project / "work" / "project.json"


@contextmanager
def project_lock(project: Path) -> Iterable[None]:
    """Serialize workflow commands for one project without global state."""
    work = project / "work"
    work.mkdir(parents=True, exist_ok=True)
    lock_path = work / ".workflow.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkflowError(f"项目正在被另一个命令处理：{project}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_state(project: Path) -> dict[str, Any]:
    filename = state_path(project)
    if not filename.is_file():
        raise WorkflowError(f"项目尚未初始化：{filename}")
    state = read_json(filename)
    if state.get("schema") != SCHEMA:
        raise WorkflowError("不支持的项目状态版本")
    return state


def save_state(project: Path, state: dict[str, Any]) -> None:
    state["revision"] = int(state.get("revision") or 0) + 1
    state["updatedAt"] = now()
    write_json(state_path(project), state)
    event_path = project / "work" / "events.jsonl"
    event = {
        "at": state["updatedAt"], "revision": state["revision"],
        "stage": state.get("stage"), "stateSha256": sha256(state_path(project)),
    }
    with event_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def require_stage(state: dict[str, Any], *allowed: str) -> None:
    if state.get("stage") not in allowed:
        raise WorkflowError(f"当前阶段 {state.get('stage')} 不允许执行此操作；允许阶段：{', '.join(allowed)}")


def ensure_input_hashes(project: Path, state: dict[str, Any]) -> None:
    for item in state.get("sources", []):
        filename = project / item["path"]
        if not filename.is_file() or sha256(filename) != item["sha256"]:
            raise WorkflowError(f"规格书已变化，旧确认失效：{filename.name}")
    template = project / state["template"]["path"]
    if not template.is_file() or sha256(template) != state["template"]["sha256"]:
        raise WorkflowError("Illustrator 模板已变化，旧确认失效")
    for path_key, hash_key, label in (
        ("metadataPath", "metadataSha256", "模板元数据"),
        ("layoutRulesPath", "layoutRulesSha256", "排版规则"),
    ):
        expected = state["template"].get(hash_key)
        if not expected:
            continue
        filename = project / state["template"][path_key]
        if not filename.is_file() or sha256(filename) != expected:
            raise WorkflowError(f"{label}已变化，旧确认失效")
    for asset in state.get("visualAssets") or []:
        filename = Path(str(asset.get("path") or ""))
        if not filename.is_absolute():
            filename = project / filename
        if not filename.is_file() or sha256(filename) != asset.get("sha256"):
            raise WorkflowError(f"图片资产已变化，旧确认失效：{asset.get('id')}")


def repo_imports() -> tuple[Any, Any]:
    local_extract = Path(__file__).with_name("extract_sources.py")
    local_worker = Path(__file__).with_name("illustrator_worker.py")
    if local_extract.is_file() and local_worker.is_file():
        extract_spec = importlib.util.spec_from_file_location("manual_skill_extract_sources", local_extract)
        worker_spec = importlib.util.spec_from_file_location("manual_skill_illustrator_worker", local_worker)
        if not extract_spec or not extract_spec.loader or not worker_spec or not worker_spec.loader:
            raise WorkflowError("无法加载 Skill 内置运行模块")
        extract_module = importlib.util.module_from_spec(extract_spec)
        worker_module = importlib.util.module_from_spec(worker_spec)
        extract_spec.loader.exec_module(extract_module)
        worker_spec.loader.exec_module(worker_module)
        return extract_module.extract_sources, worker_module
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        from tools.extract_sources import extract_sources
        from workers import illustrator_worker
    except ImportError as exc:
        raise WorkflowError(f"缺少规格书抽取或 Illustrator 运行模块：{exc}") from exc
    return extract_sources, illustrator_worker


def imposition_import() -> Any:
    local_module = Path(__file__).with_name("illustrator_imposition.py")
    if not local_module.is_file():
        raise WorkflowError("缺少独立 Illustrator 拼版模块 illustrator_imposition.py")
    script_root = str(local_module.parent)
    if script_root not in sys.path:
        sys.path.insert(0, script_root)
    spec = importlib.util.spec_from_file_location("manual_skill_illustrator_imposition", local_module)
    if not spec or not spec.loader:
        raise WorkflowError("无法加载 Illustrator 拼版模块")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def folded_leaflet_import() -> Any:
    local_module = Path(__file__).with_name("illustrator_folded_leaflet.py")
    if not local_module.is_file():
        raise WorkflowError("缺少独立五折页模块 illustrator_folded_leaflet.py")
    script_root = str(local_module.parent)
    if script_root not in sys.path:
        sys.path.insert(0, script_root)
    spec = importlib.util.spec_from_file_location("manual_skill_illustrator_folded_leaflet", local_module)
    if not spec or not spec.loader:
        raise WorkflowError("无法加载 Illustrator 五折页模块")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def small_format_import() -> Any:
    local_module = Path(__file__).with_name("illustrator_small_format.py")
    if not local_module.is_file():
        raise WorkflowError("缺少小版面自动分页模块 illustrator_small_format.py")
    script_root = str(local_module.parent)
    if script_root not in sys.path:
        sys.path.insert(0, script_root)
    spec = importlib.util.spec_from_file_location("manual_skill_illustrator_small_format", local_module)
    if not spec or not spec.loader:
        raise WorkflowError("无法加载 Illustrator 小版面自动分页模块")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def artifact_runtime(project: Path) -> tuple[str, Path, Path]:
    default_root = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies"
    node = Path(os.environ.get("ARTIFACT_TOOL_NODE", default_root / "node/bin/node")).expanduser()
    node_modules = Path(os.environ.get("ARTIFACT_TOOL_NODE_MODULES", default_root / "node/node_modules")).expanduser()
    if not node.is_file() or not node_modules.is_dir():
        raise WorkflowError("缺少 @oai/artifact-tool 运行时；请设置 ARTIFACT_TOOL_NODE 和 ARTIFACT_TOOL_NODE_MODULES")
    module = node_modules / "@oai" / "artifact-tool" / "dist" / "artifact_tool.mjs"
    if not module.is_file():
        raise WorkflowError(f"缺少 @oai/artifact-tool 模块入口：{module}")
    runtime = project / "work" / ".artifact-runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    link = runtime / "node_modules"
    if link.is_symlink() and link.resolve() != node_modules.resolve():
        link.unlink()
    if not link.exists():
        link.symlink_to(node_modules, target_is_directory=True)
    return str(node), runtime, module


def run_workbook(project: Path, *args: str) -> dict[str, Any]:
    node, runtime, module = artifact_runtime(project)
    command = [node, str(REVIEW_SCRIPT), *args]
    env = {**os.environ, "ARTIFACT_TOOL_MODULE": str(module)}
    result = subprocess.run(command, cwd=runtime, text=True, capture_output=True, env=env)
    if result.returncode != 0:
        raise WorkflowError(f"Excel 处理失败：{result.stderr or result.stdout}")
    try:
        payload = result.stdout[result.stdout.find("{"):] if "{" in result.stdout else result.stdout
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"Excel 工具未返回 JSON：{result.stdout[:500]}") from exc


def relative(project: Path, filename: Path) -> str:
    return str(filename.resolve().relative_to(project.resolve()))


def unique_copy(source: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / source.name
    if target.exists() and sha256(target) != sha256(source):
        target = directory / f"{source.stem}-{sha256(source)[:8]}{source.suffix}"
    shutil.copy2(source, target)
    return target


def bundled_layout_rules(template_hash: str) -> tuple[Path | None, dict[str, Any] | None]:
    profiles_root = SKILL_ROOT / "assets" / "template-profiles"
    if not profiles_root.is_dir():
        return None, None
    for candidate in sorted(profiles_root.glob("*/layout-rules.v1.json")):
        rules = read_json(candidate)
        if str(rules.get("templateSha256") or "") == template_hash:
            return candidate, rules
    return None, None


def load_layout_rules(path: Path | None, *, template_hash: str = "") -> tuple[Path | None, dict[str, Any]]:
    if path:
        return path, read_json(path)
    bundled_path, bundled_rules = bundled_layout_rules(template_hash)
    if bundled_path and bundled_rules:
        return bundled_path, bundled_rules
    return None, {"schemaVersion": "layout-rules/0.1", "pages": []}


def visual_assets(canonical: dict[str, Any], project: Path) -> list[dict[str, Any]]:
    assets = []
    for item in canonical.get("assets") or []:
        path = Path(str(item.get("path") or ""))
        digest = str(item.get("sha256") or (sha256(path) if path.is_file() else ""))
        assets.append({
            "id": f"asset_sha256_{digest[:24]}",
            "sourceAssetId": str(item["id"]),
            "name": str(item.get("name") or path.name),
            "path": relative(project, path) if path.is_file() and path.is_relative_to(project) else str(path),
            "sha256": digest,
            "mediaType": str(item.get("media_type") or "application/octet-stream"),
            "width": item.get("width"),
            "height": item.get("height"),
            "suggestedSlotIds": sorted({
                str(hint.get("slot_id") or "").strip().lower()
                for hint in item.get("slot_hints") or []
                if str(hint.get("slot_id") or "").strip()
            }),
        })
    return assets


def visual_slots(layout_rules: dict[str, Any]) -> list[dict[str, Any]]:
    slots = []
    seen: set[str] = set()
    for raw in layout_rules.get("visualSlots") or []:
        slot_id = str(raw.get("id") or "").strip()
        if not slot_id or slot_id in seen:
            raise WorkflowError(f"视觉槽位编号为空或重复：{slot_id}")
        seen.add(slot_id)
        mode = str(raw.get("replacementMode") or "replace_group_with_raster")
        if mode not in {"replace_placed", "replace_group_with_raster", "replace_group_with_vector", "hide_when_empty"}:
            raise WorkflowError(f"视觉槽位 {slot_id} 的 replacementMode 不受支持")
        empty_behavior = str(raw.get("emptyBehavior") or ("hide" if mode == "hide_when_empty" else "keep_template"))
        if empty_behavior not in {"keep_template", "hide"}:
            raise WorkflowError(f"视觉槽位 {slot_id} 的 emptyBehavior 不受支持")
        label = raw.get("labelTextFrame")
        if label is not None:
            if not isinstance(label, dict):
                raise WorkflowError(f"视觉槽位 {slot_id} 的 labelTextFrame 必须是对象")
            if not isinstance(label.get("index"), int) or label["index"] < 0:
                raise WorkflowError(f"视觉槽位 {slot_id} 的 labelTextFrame.index 无效")
            if not str(label.get("sourceText") or "").strip():
                raise WorkflowError(f"视觉槽位 {slot_id} 的 labelTextFrame.sourceText 为空")
            if not str(label.get("targetFieldId") or "").strip():
                raise WorkflowError(f"视觉槽位 {slot_id} 的 labelTextFrame.targetFieldId 为空")
        slots.append({
            **raw,
            "id": slot_id,
            "name": str(raw.get("name") or slot_id),
            "required": bool(raw.get("required", False)),
            "defaultFit": str(raw.get("defaultFit") or "contain"),
            "replacementMode": mode,
            "emptyBehavior": empty_behavior,
        })
    return slots


def require_visual_slots(layout_rules: dict[str, Any]) -> list[dict[str, Any]]:
    slots = visual_slots(layout_rules)
    if not slots:
        raise WorkflowError("图片确认是强制门禁；模板排版规则必须配置 visualSlots")
    return slots


def require_asset_confirmation(project: Path, state: dict[str, Any]) -> None:
    slots = {item["id"] for item in state.get("visualSlots") or []}
    selections = {item.get("slotId") for item in state.get("assetSelections") or []}
    confirmation_path = str(state.get("confirmedAssetsPath") or "")
    confirmation = project / confirmation_path if confirmation_path else None
    if not slots:
        raise WorkflowError("图片确认是强制门禁；当前项目没有 visualSlots")
    if not state.get("assetConfirmedAt") or confirmation is None or not confirmation.is_file():
        raise WorkflowError("尚未完成整份图片确认；请先追加图片 Sheet，并在用户明确回复“图片内容已确认”后运行 confirm-assets")
    if selections != slots:
        missing = sorted(slots - selections)
        extra = sorted(item for item in selections - slots if item)
        raise WorkflowError(f"图片确认槽位不完整；缺少：{missing}；多出：{extra}")


def asset_marker_bindings(assets: dict[str, dict[str, Any]], slots: dict[str, dict[str, Any]]) -> tuple[dict[str, str], set[str]]:
    bindings: dict[str, str] = {}
    bound_assets: set[str] = set()
    for asset_id, asset in assets.items():
        for slot_id in asset.get("suggestedSlotIds") or []:
            if slot_id not in slots:
                raise WorkflowError(f"规格书图片标记引用了未知槽位：{slot_id}")
            if slot_id in bindings:
                raise WorkflowError(f"规格书图片槽位被多张图片重复标记：{slot_id}")
            bindings[slot_id] = asset_id
            bound_assets.add(asset_id)
    return bindings, bound_assets


def candidate_fields(metadata: dict[str, Any], layout_rules: dict[str, Any]) -> list[dict[str, Any]]:
    frames = {int(item["index"]): item for item in metadata.get("textFrames", []) if "index" in item}
    bindings: dict[int, dict[str, Any]] = {}
    for page in layout_rules.get("pages", []):
        for region in page.get("regions", []):
            behavior = region.get("behavior") or {}
            if behavior.get("translateText") is False or region.get("role") == "fixed-page-number":
                continue
            if behavior.get("translateText") is not True:
                continue
            text_position = 0
            body_positions = {int(value) for value in behavior.get("bodyTextFramePositions") or []}
            role = str(region.get("role") or "")
            for ref in region.get("objectRefs", []):
                if ref.get("type") == "textFrame":
                    content_source = str(behavior.get("contentSource") or "")
                    if not content_source:
                        if role in {"fixed-brand", "fixed-entry-label", "fixed-panel-title", "adaptive-entry-label", "fixed-contact-info"} or (body_positions and text_position not in body_positions):
                            content_source = "template-copy"
                        else:
                            content_source = "product-evidence"
                    bindings.setdefault(int(ref["value"]), {
                        "regionId": region.get("id", ""), "role": role,
                        "allowTemplateDefault": bool(behavior.get("allowTemplateDefault", False)),
                        "contentSource": content_source,
                    })
                    text_position += 1
    if not bindings:
        bindings = {index: {"regionId": "", "role": "text-frame"} for index in frames}
    rows: list[dict[str, Any]] = []
    for index, binding in bindings.items():
        frame = frames.get(index)
        if not frame or not str(frame.get("contents") or "").strip():
            continue
        row = {
            "fieldId": f"textframe_{index}",
            "objectType": "text",
            "objectIndex": index,
            "regionId": binding["regionId"],
            "role": binding["role"],
            "templateText": str(frame.get("contents") or ""),
            "fontName": frame.get("fontName"),
            "fontSize": frame.get("fontSize"),
            "bounds": frame.get("geometricBounds"),
            "objectName": str(frame.get("name") or ""),
            "layerName": str(frame.get("layer") or ""),
            "required": any(token in str(binding["role"]).lower() for token in ("safety", "operation", "specification", "product-title", "fixed-brand")),
            "allowTemplateDefault": bool(binding.get("allowTemplateDefault", False)),
            "contentSource": str(binding.get("contentSource") or "product-evidence"),
            "fallbackPolicy": "template-default-when-spec-missing",
        }
        rows.append(row)
    for slot in layout_rules.get("virtualTextSlots", []):
        rows.append({
            "fieldId": str(slot.get("id") or f"layout_{slot.get('objectIndex')}"),
            "objectType": "layoutText",
            "objectIndex": int(slot["objectIndex"]),
            "regionId": "virtual-text",
            "role": "layout-title",
            "templateText": str(slot.get("sourceText") or ""),
            "fontName": slot.get("fontName"),
            "fontSize": slot.get("fontSize"),
            "bounds": slot.get("bounds"),
            "required": False,
            "allowTemplateDefault": bool(slot.get("allowTemplateDefault", False)),
            "contentSource": "template-copy",
            "fallbackPolicy": "template-default-when-spec-missing",
        })
    return sorted(rows, key=lambda item: (item["objectType"] == "layoutText", int(item["objectIndex"])))


def validate_template_mapping(metadata: dict[str, Any], layout_rules: dict[str, Any], template_hash: str) -> None:
    metadata_hash = str((metadata.get("source") or {}).get("sha256") or "")
    if metadata_hash and metadata_hash != template_hash:
        raise WorkflowError(f"模板元数据与 AI 文件哈希不一致：{metadata_hash} != {template_hash}")
    if layout_rules.get("schemaVersion") != "layout-rules/0.1":
        raise WorkflowError(f"不支持的排版规则版本：{layout_rules.get('schemaVersion')}")
    collections = {
        "textFrame": "textFrames", "groupItem": "groupItems", "placedItem": "placedItems",
        "rasterItem": "rasterItems", "pathItem": "layoutPathItems", "compoundPathItem": "compoundPathItems",
    }
    indexes = {
        kind: {int(item["index"]) for item in metadata.get(collection, []) if "index" in item}
        for kind, collection in collections.items()
    }
    artboards = {int(item["index"]) for item in metadata.get("artboards", []) if "index" in item}
    missing: list[str] = []
    region_ids: set[str] = set()
    for page in layout_rules.get("pages", []):
        artboard_index = int(page.get("artboardIndex", -1))
        if artboard_index not in artboards:
            raise WorkflowError(f"排版规则引用了不存在的画板：{artboard_index}")
        safe_bounds = page.get("safeBounds")
        if safe_bounds is not None and (not isinstance(safe_bounds, list) or len(safe_bounds) != 4 or not all(math.isfinite(float(value)) for value in safe_bounds)):
            raise WorkflowError(f"画板 {artboard_index} 的 safeBounds 无效")
        for region in page.get("regions", []):
            region_id = str(region.get("id") or "").strip()
            if not region_id or region_id in region_ids:
                raise WorkflowError(f"排版区域编号为空或重复：{region_id}")
            region_ids.add(region_id)
            for ref in region.get("objectRefs", []):
                ref_type = str(ref.get("type") or "")
                if ref_type not in indexes or not isinstance(ref.get("value"), int):
                    raise WorkflowError(f"排版区域 {region_id} 包含无效对象引用：{ref}")
                if int(ref["value"]) not in indexes[ref_type]:
                    missing.append(f"{ref_type}:{ref['value']}")
    seen_visual_targets: set[tuple[str, int]] = set()
    for slot in visual_slots(layout_rules):
        bounds = slot.get("bounds")
        if not isinstance(bounds, list) or len(bounds) != 4 or not all(math.isfinite(float(value)) for value in bounds):
            raise WorkflowError(f"视觉槽位 {slot['id']} 的 bounds 无效")
        for ref in slot.get("objectRefs") or []:
            ref_type = str(ref.get("type") or "")
            if ref_type not in indexes or not isinstance(ref.get("value"), int):
                raise WorkflowError(f"视觉槽位 {slot['id']} 包含无效对象引用：{ref}")
            target = (ref_type, int(ref["value"]))
            if target in seen_visual_targets:
                raise WorkflowError(f"视觉对象被多个槽位重复绑定：{ref_type}:{ref['value']}")
            seen_visual_targets.add(target)
            if target[1] not in indexes[ref_type]:
                missing.append(f"{ref_type}:{target[1]}")
        label = slot.get("labelTextFrame")
        if label and int(label["index"]) not in indexes["textFrame"]:
            missing.append(f"textFrame:{label['index']}")
    virtual_ids: set[str] = set()
    for slot in layout_rules.get("virtualTextSlots") or []:
        slot_id = str(slot.get("id") or "").strip()
        if not slot_id or slot_id in virtual_ids:
            raise WorkflowError(f"虚拟文本槽位编号为空或重复：{slot_id}")
        virtual_ids.add(slot_id)
        if not isinstance(slot.get("objectIndex"), int) or not str(slot.get("sourceText") or "").strip():
            raise WorkflowError(f"虚拟文本槽位 {slot_id} 缺少有效 objectIndex/sourceText")
    if missing:
        raise WorkflowError(f"排版规则引用了模板中不存在的对象：{sorted(set(missing))[:10]}")


def command_init(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    if state_path(project).exists() and not args.overwrite:
        raise WorkflowError("项目已经初始化；如需重建请使用新目录")
    for name in ("inputs", "review", "work", "preview", "delivery"):
        (project / name).mkdir(parents=True, exist_ok=True)
    source_files = [project_path(value) for value in args.spec]
    template_source = project_path(args.template)
    for filename in [*source_files, template_source]:
        if not filename.is_file():
            raise WorkflowError(f"输入文件不存在：{filename}")
    copied_sources = [unique_copy(item, project / "inputs") for item in source_files]
    copied_template = unique_copy(template_source, project / "inputs")
    extract_sources, illustrator_worker = repo_imports()
    canonical = extract_sources(copied_sources, asset_dir=project / "work" / "assets")
    canonical_path = project / "work" / "canonical-product.json"
    write_json(canonical_path, canonical)
    if args.template_metadata:
        metadata_source = project_path(args.template_metadata)
        metadata = read_json(metadata_source)
        metadata_path = project / "work" / "template.v2.json"
        write_json(metadata_path, metadata)
    else:
        extracted = illustrator_worker.extract_template(copied_template, project / "work" / "template", overwrite=True)
        metadata = extracted["metadata"]
        metadata_path = Path(extracted["template"])
    template_hash = sha256(copied_template)
    requested_layout_source = project_path(args.layout_rules) if args.layout_rules else None
    layout_source, layout_rules = load_layout_rules(requested_layout_source, template_hash=template_hash)
    validate_template_mapping(metadata, layout_rules, sha256(copied_template))
    layout_path = project / "work" / "layout-rules.json"
    write_json(layout_path, layout_rules)
    fields = candidate_fields(metadata, layout_rules)
    content_input = {
        "task": "optimize-technical-specification-for-user-manual",
        "instructions": "先忠实提取技术事实，再将其改写为普通用户可理解、可直接用于产品说明书的中文。按使用场景、操作步骤、安全提示和维护建议组织表达；解释必要术语，避免只罗列参数。不得发明功能、效果、认证或安全结论。每个模板字段都先查 canonicalProduct：规格书提供对应内容时使用规格书；规格书未提供时必须以 templateText 作为默认内容，翻译或本地化后写入确认书，不得留空，也不得把这种情况标成内容遗漏。使用模板默认内容时将 contentOrigin 设为 template-default，并在 sourceEvidence 中逐字引用 templateText。型号、数字、单位、品牌、网址和企业法定名称必须原样保留；企业角色标签可以本地化，企业法定名称本体默认不翻译。每个非空 aiChinese 都必须同时给出可追溯 sourceEvidence 和 optimizationNote。",
        "optimizationPrinciples": [
            "来源优先级：先使用 canonicalProduct；缺失时使用当前字段的 templateText 默认内容",
            "默认不等于遗漏：规格书未提供的字段仍保留并本地化模板默认内容",
            "用户可读：把技术术语改写成短句、步骤或明确用途",
            "面向任务：优先回答是什么、怎么用、注意什么、如何维护",
            "禁止夸大：不新增性能承诺、比较结论、认证或医疗效果",
            "完整保真：型号、数字、单位、品牌、网址和企业法定名称进入 protectedTokens；翻译时只本地化 Manufacturer、Producer 等角色标签"
        ],
        "canonicalProduct": canonical,
        "templateFields": fields,
    }
    content_input_path = project / "work" / "content-optimization-input.json"
    write_json(content_input_path, content_input)
    high_conflicts = [item for item in canonical.get("conflicts") or [] if str(item.get("severity") or "").casefold() == "high"]
    conflict_review_path = project / "work" / "conflict-review-input.json"
    write_json(conflict_review_path, {
        "instructions": "逐项核对高严重度冲突。只有得到明确事实结论后，才能把 status 设为 resolved；resolution 必须写明最终采用的事实或修正。",
        "conflicts": high_conflicts,
        "resolutionSchema": {"conflictId": "conflict-001", "status": "resolved", "resolution": "最终采用的事实或修正"},
    })
    languages = [item.strip() for item in args.languages.split(",") if item.strip()]
    if not languages:
        raise WorkflowError("至少需要一个目标语种")
    mandatory_visual_slots = require_visual_slots(layout_rules)
    state = {
        "schema": SCHEMA,
        "stage": "needs_chinese_optimization",
        "createdAt": now(),
        "sources": [{"path": relative(project, item), "sha256": sha256(item), "name": item.name} for item in copied_sources],
        "template": {
            "path": relative(project, copied_template),
            "sha256": sha256(copied_template),
            "metadataPath": relative(project, metadata_path),
            "metadataSha256": sha256(metadata_path),
            "layoutRulesPath": relative(project, layout_path),
            "layoutRulesSha256": sha256(layout_path),
            "layoutRulesSource": str(layout_source) if layout_source else "",
        },
        "languages": languages,
        "fields": fields,
        "visualAssets": visual_assets(canonical, project),
        "visualSlots": mandatory_visual_slots,
        "canonicalProductPath": relative(project, canonical_path),
        "canonicalProductSha256": sha256(canonical_path),
        "conflictReviewInputPath": relative(project, conflict_review_path),
        "highConflictIds": [str(item["id"]) for item in high_conflicts],
        "contentOptimizationInputPath": relative(project, content_input_path),
        "workbookPath": f"review/{WORKBOOK_NAME}",
    }
    save_state(project, state)
    print(json.dumps({
        "project": str(project), "stage": state["stage"],
        "next": "先处理 conflict-review-input.json 中的高严重度冲突，再完成中文优化" if high_conflicts else "让 Skill 根据 content-optimization-input.json 完成用户化改写，然后运行 optimize-chinese",
        "contentOptimizationInput": str(content_input_path),
        "highConflictCount": len(high_conflicts), "conflictReviewInput": str(conflict_review_path),
    }, ensure_ascii=False, indent=2))


def validate_content_rows(state: dict[str, Any], rows: list[dict[str, Any]], *, require_optimization: bool = False) -> list[dict[str, Any]]:
    expected = {item["fieldId"]: item for item in state["fields"]}
    seen: set[str] = set()
    result = []
    for row in rows:
        field_id = str(row.get("fieldId") or "")
        if field_id not in expected or field_id in seen:
            raise WorkflowError(f"中文内容包含未知或重复字段：{field_id}")
        seen.add(field_id)
        item = expected[field_id]
        ai_chinese = str(row.get("aiChinese") or "")
        source_evidence = str(row.get("sourceEvidence") or "").strip()
        optimization_note = str(row.get("optimizationNote") or "").strip()
        content_origin = str(row.get("contentOrigin") or "").strip()
        template_text = str(item.get("templateText") or "").strip()
        missing_spec_evidence = any(marker in source_evidence.casefold() for marker in (
            "规格书未提供", "规格书中未提供", "specification does not provide", "not provided by specification",
        ))
        if require_optimization and missing_spec_evidence and content_origin != "template-default":
            raise WorkflowError(f"规格书未提供该字段时，内容来源必须明确为 template-default：{field_id}")
        if require_optimization and content_origin and content_origin not in {"product-evidence", "template-default"}:
            raise WorkflowError(f"AI 内容来源无效：{field_id}={content_origin}")
        if require_optimization and template_text and not ai_chinese.strip():
            raise WorkflowError(f"模板字段存在默认内容；规格书未提供时必须沿用该默认内容，AI 中文不得为空：{field_id}")
        if require_optimization and bool(item.get("required")) and not ai_chinese.strip():
            raise WorkflowError(f"AI 内容优化后的必填字段为空：{field_id}")
        if require_optimization and ai_chinese.strip() and not optimization_note:
            raise WorkflowError(f"AI 内容优化字段缺少 optimizationNote：{field_id}")
        if require_optimization and ai_chinese.strip() and not source_evidence:
            raise WorkflowError(f"AI 内容优化字段缺少 sourceEvidence：{field_id}")
        if require_optimization and item.get("contentSource") == "template-copy" and not content_origin:
            content_origin = "template-default"
        if require_optimization and content_origin == "template-default" and template_text not in source_evidence:
            raise WorkflowError(f"模板默认内容必须在 sourceEvidence 中逐字引用 templateText：{field_id}")
        result.append({
            **item,
            "fieldName": str(row.get("fieldName") or item.get("regionId") or field_id),
            "sourceEvidence": source_evidence,
            "aiChinese": ai_chinese,
            "finalChinese": ai_chinese,
            "optimizationNote": optimization_note,
            "contentOrigin": content_origin or "product-evidence",
            "required": bool(item.get("required", False) or row.get("required", False)),
            "protectedTokens": list(dict.fromkeys([
                *[str(token) for token in row.get("protectedTokens") or [] if str(token).strip()],
                *automatic_protected_tokens(str(row.get("aiChinese") or "")),
            ])),
        })
    missing = sorted(set(expected) - seen)
    if missing:
        raise WorkflowError(f"中文内容缺少模板字段：{', '.join(missing[:10])}")
    return result


def invalidate_downstream_state(state: dict[str, Any]) -> None:
    for key in (
        "chineseConfirmedAt", "confirmedChinesePath",
        "translations", "translationConfirmedAt", "confirmedTranslationsPath", "translationInputPath",
        "assetSelections", "assetConfirmedAt", "confirmedAssetsPath",
        "sourceChineseRenderResultsPath", "sourceChineseLayoutConfirmedAt", "sourceChineseManifestPath",
        "renderResultsPath", "blockingIssues", "blockingPhase",
        "electronicConfirmedAt", "electronicManifestPath",
        "abImpositionResultsPath", "abConfirmedAt", "confirmedAbManifestPath",
        "splitResultsPath", "aBConfirmedAt", "confirmedABSplitManifestPath", "impositionManifestPath",
        "foldedLeafletResultsPath", "foldedLeafletConfirmedAt", "foldedLeafletManifestPath",
        "smallFormatResultsPath", "smallFormatConfirmedAt", "smallFormatManifestPath", "printVariant",
        "deliveryPackageGenerated", "deliveredAt", "deliveryManifestPath",
    ):
        state.pop(key, None)


def command_refresh_chinese(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(
        state,
        "waiting_chinese_confirmation", "needs_asset_review", "waiting_asset_confirmation",
        "ready_to_render_chinese", "waiting_chinese_layout_confirmation", "blocked_chinese",
        "needs_translation_generation", "waiting_translation_confirmation",
        "ready_to_render", "waiting_layout_confirmation", "blocked",
        "ready_to_impose_ab", "blocked_ab_imposition", "waiting_ab_confirmation",
        "blocked_folded_leaflet", "waiting_folded_leaflet_confirmation",
        "blocked_small_format", "waiting_small_format_confirmation",
        "ready_to_split_ab", "blocked_a_b_split", "waiting_a_b_confirmation", "completed_without_delivery", "delivered",
    )
    ensure_input_hashes(project, state)
    payload = read_json(project_path(args.content_json))
    rows = validate_content_rows(state, payload.get("rows", payload), require_optimization=True)
    optimized = project / "work" / "optimized-chinese.json"
    write_json(optimized, {"optimizedAt": now(), "rows": rows})
    review_input = project / "work" / "chinese-review-input.json"
    write_json(review_input, {"rows": rows})
    workbook = project / state["workbookPath"]
    backup = workbook.with_name(f"{workbook.stem}.刷新前备份-{datetime.now().strftime('%Y%m%d-%H%M%S')}{workbook.suffix}")
    if workbook.is_file():
        shutil.copy2(workbook, backup)
    run_workbook(project, "create-chinese", "--input", str(review_input), "--output", str(workbook))
    state["fields"] = rows
    state["optimizedChinesePath"] = relative(project, optimized)
    state["optimizedChineseSha256"] = sha256(optimized)
    invalidate_downstream_state(state)
    state["stage"] = "waiting_chinese_confirmation"
    state["workbookGeneratedHash"] = sha256(workbook)
    save_state(project, state)
    print(json.dumps({
        "stage": state["stage"], "workbook": str(workbook),
        "backup": str(backup) if backup.is_file() else "",
        "message": "中文内容已刷新，旧中文、图片、版式及翻译确认已失效；请重新核对黄色‘最终中文’列并明确确认",
    }, ensure_ascii=False, indent=2))


def command_optimize_chinese(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "needs_chinese_optimization")
    ensure_input_hashes(project, state)
    payload = read_json(project_path(args.content_json))
    rows = validate_content_rows(state, payload.get("rows", payload), require_optimization=True)
    optimized = project / "work" / "optimized-chinese.json"
    write_json(optimized, {"optimizedAt": now(), "rows": rows})
    state["fields"] = rows
    state["optimizedChinesePath"] = relative(project, optimized)
    state["optimizedChineseSha256"] = sha256(optimized)
    state["stage"] = "ready_to_export_chinese"
    save_state(project, state)
    print(json.dumps({"stage": state["stage"], "optimizedChinese": str(optimized), "next": "运行 export-chinese 生成中文版说明书 Excel"}, ensure_ascii=False, indent=2))


def command_export_chinese(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "ready_to_export_chinese")
    ensure_input_hashes(project, state)
    optimized_path = project / str(state.get("optimizedChinesePath") or "")
    if not optimized_path.is_file():
        raise WorkflowError("缺少已完成的 AI 内容优化结果")
    expected_optimized_hash = str(state.get("optimizedChineseSha256") or "")
    if not expected_optimized_hash or sha256(optimized_path) != expected_optimized_hash:
        raise WorkflowError("AI 内容优化结果已变化；请重新运行 optimize-chinese 固化")
    rows = validate_content_rows(state, read_json(optimized_path).get("rows") or [], require_optimization=True)
    review_input = project / "work" / "chinese-review-input.json"
    write_json(review_input, {"rows": rows})
    workbook = project / state["workbookPath"]
    run_workbook(project, "create-chinese", "--input", str(review_input), "--output", str(workbook))
    state["fields"] = rows
    state["stage"] = "waiting_chinese_confirmation"
    state["workbookGeneratedHash"] = sha256(workbook)
    save_state(project, state)
    print(json.dumps({"stage": state["stage"], "workbook": str(workbook), "message": "请只修改“最终中文”列，保存并关闭后明确回复“中文内容已确认”"}, ensure_ascii=False, indent=2))


def workbook_rows(project: Path, workbook: Path, command: str) -> list[dict[str, Any]]:
    result = run_workbook(project, command, "--workbook", str(workbook))
    return result.get("rows") or []


def assert_exact_ids(actual: Iterable[str], expected: Iterable[str], label: str) -> None:
    actual_list = list(actual)
    expected_list = list(expected)
    if len(actual_list) != len(set(actual_list)):
        raise WorkflowError(f"{label}包含重复编号")
    if set(actual_list) != set(expected_list):
        missing = sorted(set(expected_list) - set(actual_list))
        unknown = sorted(set(actual_list) - set(expected_list))
        raise WorkflowError(f"{label}字段集合变化；缺少={missing[:5]}，未知={unknown[:5]}")


def normalize_workbook_text(value: Any) -> str:
    """Treat Excel's equivalent CR/LF cell line endings as identical."""
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


def assert_readonly(actual: dict[str, Any], expected: dict[str, Any], keys: Iterable[str], excel_row: int) -> None:
    for key in keys:
        if normalize_workbook_text(actual.get(key)) != normalize_workbook_text(expected.get(key)):
            raise WorkflowError(f"Excel 第 {excel_row} 行误改了只读列：{key}")


def assert_protected_tokens(text: str, tokens: Iterable[str], excel_row: int) -> None:
    missing = [token for token in tokens if token and token not in text]
    if missing:
        raise WorkflowError(f"Excel 第 {excel_row} 行缺少受保护内容：{', '.join(missing[:8])}")


def assert_publishable_text(text: str, excel_row: int) -> None:
    if REVIEW_META_PATTERN.search(text):
        raise WorkflowError(f"Excel 第 {excel_row} 行仍包含待确认/TODO 等审核提示，不能进入正式说明书")


def command_confirm_chinese(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "waiting_chinese_confirmation")
    ensure_input_hashes(project, state)
    require_resolved_conflicts(project, state)
    workbook = project / state["workbookPath"]
    rows = workbook_rows(project, workbook, "read-chinese")
    expected = {item["fieldId"]: item for item in state["fields"]}
    assert_exact_ids((row["fieldId"] for row in rows), expected, "中文确认表")
    confirmed = []
    for row in rows:
        item = expected[row["fieldId"]]
        assert_readonly(row, item, ("fieldName", "sourceEvidence", "aiChinese"), int(row["excelRow"]))
        final_text = str(row.get("finalChinese") or "").strip()
        if item.get("required") and not final_text:
            raise WorkflowError(f"Excel 第 {row['excelRow']} 行必填中文为空")
        assert_publishable_text(final_text, int(row["excelRow"]))
        confirmed.append({
            **item,
            "finalChinese": final_text,
            "protectedTokens": list(dict.fromkeys([
                *[str(token) for token in item.get("protectedTokens") or [] if str(token).strip()],
                *automatic_protected_tokens(final_text),
            ])),
            "excelRow": int(row["excelRow"]),
        })
    output = project / "work" / "confirmed-chinese.json"
    write_json(output, {"confirmedAt": now(), "rows": confirmed})
    state["fields"] = confirmed
    for key in (
        "translations", "translationConfirmedAt", "confirmedTranslationsPath",
        "assetSelections", "assetConfirmedAt", "confirmedAssetsPath",
        "sourceChineseRenderResultsPath", "sourceChineseLayoutConfirmedAt",
        "renderResultsPath", "blockingIssues",
    ):
        state.pop(key, None)
    state["stage"] = "needs_asset_review"
    state["chineseConfirmedAt"] = now()
    state["confirmedChinesePath"] = relative(project, output)
    save_state(project, state)
    print(json.dumps({"stage": state["stage"], "next": "先完成图片确认，再生成并确认中文版 AI/PDF"}, ensure_ascii=False, indent=2))


def command_resolve_conflicts(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "needs_chinese_optimization", "ready_to_export_chinese", "waiting_chinese_confirmation")
    ensure_input_hashes(project, state)
    payload = read_json(project_path(args.resolutions_json))
    expected, canonical_hash = expected_high_conflicts(project, state)
    rows = payload.get("resolutions", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise WorkflowError("冲突处理文件必须是 resolutions 数组或数组本身")
    actual = [str(item.get("conflictId") or "") for item in rows if isinstance(item, dict)]
    assert_exact_ids(actual, expected, "高严重度冲突处理")
    normalized = []
    for item in rows:
        conflict_id = str(item.get("conflictId") or "")
        if str(item.get("status") or "").casefold() != "resolved":
            raise WorkflowError(f"高严重度冲突 {conflict_id} 尚未标记为 resolved")
        resolution = str(item.get("resolution") or "").strip()
        if not resolution or REVIEW_META_PATTERN.search(resolution):
            raise WorkflowError(f"高严重度冲突 {conflict_id} 缺少明确、可执行的最终结论")
        normalized.append({"conflictId": conflict_id, "status": "resolved", "resolution": resolution})
    output = project / "work" / "resolved-conflicts.json"
    write_json(output, {
        "resolvedAt": now(),
        "canonicalProductSha256": canonical_hash,
        "resolutions": normalized,
    })
    state["resolvedConflictsPath"] = relative(project, output)
    state["resolvedConflictsSha256"] = sha256(output)
    state["conflictsResolvedAt"] = now()
    save_state(project, state)
    print(json.dumps({"stage": state["stage"], "resolved": expected, "next": "继续当前中文内容流程"}, ensure_ascii=False, indent=2))


def require_resolved_conflicts(project: Path, state: dict[str, Any]) -> None:
    expected, canonical_hash = expected_high_conflicts(project, state)
    if not expected:
        return
    path_value = str(state.get("resolvedConflictsPath") or "")
    path = project / path_value if path_value else None
    if path is None or not path.is_file() or sha256(path) != str(state.get("resolvedConflictsSha256") or ""):
        raise WorkflowError(f"仍有未解决的高严重度规格冲突：{', '.join(expected)}；请先运行 resolve-conflicts")
    payload = read_json(path)
    if payload.get("canonicalProductSha256") != canonical_hash:
        raise WorkflowError("规格抽取结果已变化，旧冲突处理结论失效")
    rows = payload.get("resolutions") or []
    actual = [str(item.get("conflictId") or "") for item in rows]
    assert_exact_ids(actual, expected, "已解决冲突")
    if any(str(item.get("status") or "").casefold() != "resolved" or not str(item.get("resolution") or "").strip() for item in rows):
        raise WorkflowError("高严重度规格冲突处理结论不完整")


def expected_high_conflicts(project: Path, state: dict[str, Any]) -> tuple[list[str], str]:
    path_value = str(state.get("canonicalProductPath") or "")
    path = project / path_value if path_value else None
    if path is None or not path.is_file():
        expected = list(dict.fromkeys(str(value) for value in state.get("highConflictIds") or []))
        return expected, str(state.get("canonicalProductSha256") or "")
    canonical_hash = sha256(path)
    expected = [
        str(item.get("id") or "")
        for item in read_json(path).get("conflicts") or []
        if str(item.get("severity") or "").casefold() == "high" and str(item.get("id") or "")
    ]
    return list(dict.fromkeys(expected)), canonical_hash


def create_translation_input(project: Path, state: dict[str, Any]) -> Path:
    translation_input = project / "work" / "translation-generation-input.json"
    write_json(translation_input, {
        "instructions": "仅根据已确认中文版 finalChinese 翻译。为每个 fieldId 和目标语种输出 aiTranslation，不得修改型号、品牌、数字、单位及 protectedTokens 中的企业法定名称；Manufacturer、Producer 等角色标签可以本地化，但企业法定名称本体必须原样保留。空中文保持空。",
        "languages": state["languages"],
        "rows": [{
            "fieldId": item["fieldId"], "fieldName": item["fieldName"],
            "finalChinese": item["finalChinese"],
            "protectedTokens": list(dict.fromkeys([
                *[str(token) for token in item.get("protectedTokens") or [] if str(token) and str(token) in item["finalChinese"]],
                *automatic_protected_tokens(item["finalChinese"]),
            ])),
        } for item in state["fields"]],
    })
    state["translationInputPath"] = relative(project, translation_input)
    return translation_input


def require_source_chinese_confirmation(project: Path, state: dict[str, Any]) -> None:
    if not state.get("sourceChineseLayoutConfirmedAt") or not state.get("sourceChineseManifestPath"):
        raise WorkflowError("尚未生成并确认中文版 AI/PDF；不能进入其他语种环节")
    manifest = project / state["sourceChineseManifestPath"]
    if not manifest.is_file():
        raise WorkflowError("中文版确认清单缺失；请重新生成并确认中文版 AI/PDF")
    for item in (read_json(manifest).get("files") or []):
        filename = project / item["path"]
        if not filename.is_file() or sha256(filename) != item.get("sha256"):
            raise WorkflowError(f"中文版确认产物已变化：{filename.name}")


def assert_chinese_still_confirmed(project: Path, state: dict[str, Any], *, invalidate: bool = False) -> None:
    workbook = project / state["workbookPath"]
    rows = workbook_rows(project, workbook, "read-chinese")
    expected = {item["fieldId"]: item for item in state["fields"]}
    try:
        assert_exact_ids((row["fieldId"] for row in rows), expected, "中文确认表")
        for row in rows:
            item = expected[row["fieldId"]]
            assert_readonly(row, item, ("fieldName", "sourceEvidence", "aiChinese"), int(row["excelRow"]))
            if str(row.get("finalChinese") or "").strip() != str(item.get("finalChinese") or "").strip():
                raise WorkflowError(f"Excel 第 {row['excelRow']} 行中文在确认后又发生变化")
    except WorkflowError:
        if invalidate:
            state["stage"] = "waiting_chinese_confirmation"
            for key in (
                "translations", "translationConfirmedAt", "confirmedTranslationsPath", "translationInputPath",
                "assetSelections", "assetConfirmedAt", "confirmedAssetsPath",
                "sourceChineseRenderResultsPath", "sourceChineseLayoutConfirmedAt", "sourceChineseManifestPath",
                "renderResultsPath", "blockingIssues",
            ):
                state.pop(key, None)
            save_state(project, state)
        raise


def command_append_translations(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "needs_translation_generation")
    ensure_input_hashes(project, state)
    require_source_chinese_confirmation(project, state)
    assert_chinese_still_confirmed(project, state, invalidate=True)
    payload = read_json(project_path(args.translations_json))
    raw_rows = payload.get("rows", payload)
    fields = {item["fieldId"]: item for item in state["fields"]}
    expected_keys = {f"{field_id}::{language}" for field_id in fields for language in state["languages"]}
    seen: set[str] = set()
    rows = []
    for raw in raw_rows:
        field_id = str(raw.get("fieldId") or "")
        language = str(raw.get("language") or "")
        key = f"{field_id}::{language}"
        if key not in expected_keys or key in seen:
            raise WorkflowError(f"翻译包含未知或重复组合：{key}")
        seen.add(key)
        field = fields[field_id]
        translation = str(raw.get("aiTranslation") or "") if field.get("finalChinese") else ""
        rows.append({
            "translationId": key,
            "fieldId": field_id,
            "fieldName": field["fieldName"],
            "language": language,
            "confirmedChinese": field.get("finalChinese") or "",
            "aiTranslation": translation,
            "finalTranslation": translation,
        })
    missing = sorted(expected_keys - seen)
    if missing:
        raise WorkflowError(f"翻译缺少字段/语种组合：{', '.join(missing[:10])}")
    review_input = project / "work" / "translation-review-input.json"
    write_json(review_input, {"rows": rows})
    workbook = project / state["workbookPath"]
    backup = workbook.with_suffix(".中文确认备份.xlsx")
    shutil.copy2(workbook, backup)
    temp = workbook.with_suffix(".更新中.xlsx")
    run_workbook(project, "append-translations", "--workbook", str(workbook), "--input", str(review_input), "--output", str(temp))
    os.replace(temp, workbook)
    state["translations"] = rows
    state["stage"] = "waiting_translation_confirmation"
    state["translationWorkbookHash"] = sha256(workbook)
    save_state(project, state)
    print(json.dumps({"stage": state["stage"], "workbook": str(workbook), "backup": str(backup), "message": "请只修改“最终译文”列，保存并关闭后明确回复“翻译内容已确认”"}, ensure_ascii=False, indent=2))


def automatic_protected_tokens(text: str) -> list[str]:
    patterns = [
        r"https?://[^\s<>()]+",
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        r"\b[A-Za-z]{1,12}-?\d[A-Za-z0-9._/-]*\b",
        r"\b(?:CE|FCC|UL|ETL|RoHS|REACH|ISO)\s*[-:]?\s*[A-Za-z0-9./-]*\b",
        r"\bIP(?:X?\d{1,2}|\d{2})\b",
        r"\b\d+(?:[.,]\d+)?(?:\s?(?:%|mm|cm|km|m|mg|kg|g|ml|mL|L|lb|lbs|oz|V|mV|kV|Hz|kHz|MHz|W|kW|A|mA|Ah|Wh|Pa|kPa|MPa|bar|psi|N|Nm|N\s*[\u00b7•]\s*m|rpm|dB|°C|°F))?\b",
        r"\b\d+(?:[.,]\d+)?\s?[xX×]\s?\d+(?:[.,]\d+)?(?:\s?[xX×]\s?\d+(?:[.,]\d+)?)?\s?(?:mm|cm|m)?\b",
    ]
    result: list[str] = []
    for pattern in patterns:
        for token in re.findall(pattern, text, flags=re.IGNORECASE):
            if token not in result:
                result.append(token)
    legal_name_pattern = re.compile(
        r"\b(?:[A-Z][A-Za-z0-9&.'-]*\s+){1,8}(?:Co\.,\s*Ltd\.|Co\.\s*Ltd\.|Ltd\.|LLC|Inc\.|GmbH)"
    )
    for match in legal_name_pattern.finditer(text):
        token = match.group(0).strip()
        if token not in result:
            result.append(token)
    chinese_legal_name = re.compile(r"[\u4e00-\u9fffA-Za-z0-9（）()&·-]{2,40}(?:股份有限公司|有限责任公司|有限公司)")
    for match in chinese_legal_name.finditer(text):
        token = match.group(0).strip()
        if token not in result:
            result.append(token)
    return result


def command_confirm_translations(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "waiting_translation_confirmation")
    ensure_input_hashes(project, state)
    assert_chinese_still_confirmed(project, state, invalidate=True)
    workbook = project / state["workbookPath"]
    rows = workbook_rows(project, workbook, "read-translations")
    expected = {item["translationId"]: item for item in state["translations"]}
    assert_exact_ids((row["translationId"] for row in rows), expected, "翻译确认表")
    confirmed = []
    for row in rows:
        item = expected[row["translationId"]]
        assert_readonly(row, item, ("fieldName", "language", "confirmedChinese", "aiTranslation"), int(row["excelRow"]))
        final_text = str(row.get("finalTranslation") or "").strip()
        if item["confirmedChinese"] and not final_text:
            raise WorkflowError(f"Excel 第 {row['excelRow']} 行最终译文为空")
        assert_publishable_text(final_text, int(row["excelRow"]))
        if final_text:
            field = next(candidate for candidate in state["fields"] if candidate["fieldId"] == item["fieldId"])
            tokens = list(dict.fromkeys([
                *[str(token) for token in field.get("protectedTokens") or [] if str(token) and str(token) in item["confirmedChinese"]],
                *automatic_protected_tokens(item["confirmedChinese"]),
            ]))
            assert_protected_tokens(final_text, tokens, int(row["excelRow"]))
        confirmed.append({**item, "finalTranslation": final_text, "excelRow": int(row["excelRow"])})
    output = project / "work" / "confirmed-translations.json"
    write_json(output, {"confirmedAt": now(), "rows": confirmed})
    state["translations"] = confirmed
    require_source_chinese_confirmation(project, state)
    require_asset_confirmation(project, state)
    state["stage"] = "ready_to_render"
    state["translationConfirmedAt"] = now()
    state["confirmedTranslationsPath"] = relative(project, output)
    save_state(project, state)
    print(json.dumps({"stage": state["stage"], "next": "运行 render 生成最终多语种 AI/PDF"}, ensure_ascii=False, indent=2))


def asset_path(project: Path, asset: dict[str, Any]) -> Path:
    filename = Path(str(asset.get("path") or ""))
    return filename if filename.is_absolute() else project / filename


def workbook_images_by_row(workbook: Path, sheet_name: str, column: int) -> dict[int, list[dict[str, Any]]]:
    spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    office_rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    drawing_ns = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
    drawing_main_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    with zipfile.ZipFile(workbook) as archive:
        book = ET.fromstring(archive.read("xl/workbook.xml"))
        book_rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        book_targets = {item.get("Id"): item.get("Target") for item in book_rels.findall(f"{{{package_rel_ns}}}Relationship")}
        sheet_path = None
        for sheet in book.findall(f".//{{{spreadsheet_ns}}}sheet"):
            if sheet.get("name") == sheet_name:
                target = book_targets.get(sheet.get(f"{{{office_rel_ns}}}id"))
                if target:
                    sheet_path = posixpath.normpath(posixpath.join("xl", target)).lstrip("/")
                break
        if not sheet_path:
            raise WorkflowError(f"Excel 缺少 Sheet：{sheet_name}")
        sheet_root = ET.fromstring(archive.read(sheet_path))
        drawing_node = sheet_root.find(f".//{{{spreadsheet_ns}}}drawing")
        if drawing_node is None:
            return {}
        sheet_rels_path = posixpath.join(posixpath.dirname(sheet_path), "_rels", posixpath.basename(sheet_path) + ".rels")
        sheet_rels = ET.fromstring(archive.read(sheet_rels_path))
        sheet_targets = {item.get("Id"): item.get("Target") for item in sheet_rels.findall(f"{{{package_rel_ns}}}Relationship")}
        drawing_target = sheet_targets.get(drawing_node.get(f"{{{office_rel_ns}}}id"))
        if not drawing_target:
            return {}
        drawing_path = posixpath.normpath(posixpath.join(posixpath.dirname(sheet_path), drawing_target)).lstrip("/")
        drawing_root = ET.fromstring(archive.read(drawing_path))
        drawing_rels_path = posixpath.join(posixpath.dirname(drawing_path), "_rels", posixpath.basename(drawing_path) + ".rels")
        drawing_rels = ET.fromstring(archive.read(drawing_rels_path))
        drawing_targets = {item.get("Id"): item.get("Target") for item in drawing_rels.findall(f"{{{package_rel_ns}}}Relationship")}
        found: dict[int, list[dict[str, Any]]] = {}
        for anchor in list(drawing_root.findall(f"{{{drawing_ns}}}oneCellAnchor")) + list(drawing_root.findall(f"{{{drawing_ns}}}twoCellAnchor")):
            origin = anchor.find(f"{{{drawing_ns}}}from")
            blip = anchor.find(f".//{{{drawing_main_ns}}}blip")
            if origin is None or blip is None:
                continue
            row_node = origin.find(f"{{{drawing_ns}}}row")
            col_node = origin.find(f"{{{drawing_ns}}}col")
            if row_node is None or col_node is None or int(col_node.text or "-1") != column:
                continue
            rel_id = blip.get(f"{{{office_rel_ns}}}embed")
            media_target = drawing_targets.get(rel_id)
            if not media_target:
                continue
            media_path = posixpath.normpath(posixpath.join(posixpath.dirname(drawing_path), media_target)).lstrip("/")
            excel_row = int(row_node.text or "0") + 1
            found.setdefault(excel_row, []).append({"path": media_path, "bytes": archive.read(media_path)})
        return found


def command_prepare_assets(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "needs_asset_review", "waiting_asset_confirmation", "ready_to_render_chinese", "blocked_chinese")
    ensure_input_hashes(project, state)
    layout_rules = read_json(project / state["template"]["layoutRulesPath"])
    canonical = read_json(project / "work" / "canonical-product.json")
    state["visualAssets"] = visual_assets(canonical, project)
    state["visualSlots"] = require_visual_slots(layout_rules)
    state["stage"] = "needs_asset_review"
    save_state(project, state)
    print(json.dumps({"stage": state["stage"], "visualSlots": len(state["visualSlots"]), "visualAssets": len(state["visualAssets"])}, ensure_ascii=False, indent=2))


def command_append_assets(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "needs_asset_review", "waiting_asset_confirmation")
    ensure_input_hashes(project, state)
    payload = read_json(project_path(args.assets_json))
    raw_rows = payload.get("rows", payload)
    canonical = read_json(project / "work" / "canonical-product.json")
    state["visualAssets"] = visual_assets(canonical, project)
    layout_rules = read_json(project / state["template"]["layoutRulesPath"])
    state["visualSlots"] = require_visual_slots(layout_rules)
    slots = {item["id"]: item for item in state.get("visualSlots") or []}
    assets = {item["id"]: item for item in state.get("visualAssets") or []}
    marker_bindings, marker_bound_assets = asset_marker_bindings(assets, slots)
    candidates = [
        {"path": str(asset_path(project, item).resolve())}
        for asset_id, item in assets.items()
        if asset_id not in marker_bound_assets
    ]
    rows = []
    seen: set[str] = set()
    for raw in raw_rows:
        slot_id = str(raw.get("slotId") or "")
        if slot_id not in slots or slot_id in seen:
            raise WorkflowError(f"图片确认包含未知或重复槽位：{slot_id}")
        seen.add(slot_id)
        ai_asset_id = marker_bindings.get(slot_id) or str(raw.get("aiAssetId") or "").strip()
        if ai_asset_id and ai_asset_id not in assets:
            raise WorkflowError(f"图片槽位 {slot_id} 引用了未知资产：{ai_asset_id}")
        slot = slots[slot_id]
        rows.append({
            "slotId": slot_id, "slotName": slot["name"],
            "suggestedAsset": {"path": str(asset_path(project, assets[ai_asset_id]).resolve())} if ai_asset_id else None,
            "aiAssetId": ai_asset_id, "finalAssetId": ai_asset_id,
            "fitMode": str(raw.get("fitMode") or slot.get("defaultFit") or "contain"),
        })
    missing = sorted(set(slots) - seen)
    if missing:
        raise WorkflowError(f"图片确认缺少槽位：{', '.join(missing)}")
    review_input = project / "work" / "asset-review-input.json"
    write_json(review_input, {"unassignedCandidates": candidates, "rows": rows})
    workbook = project / state["workbookPath"]
    temp = workbook.with_suffix(".图片更新中.xlsx")
    run_workbook(project, "append-assets", "--workbook", str(workbook), "--input", str(review_input), "--output", str(temp))
    os.replace(temp, workbook)
    state["assetSelections"] = rows
    state["stage"] = "waiting_asset_confirmation"
    save_state(project, state)
    print(json.dumps({"stage": state["stage"], "workbook": str(workbook), "message": "请在图片 Sheet 黄色的“最终使用图片”区域直接删除或粘贴图片，保存并关闭后明确回复“图片内容已确认”"}, ensure_ascii=False, indent=2))


def command_confirm_assets(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "waiting_asset_confirmation")
    ensure_input_hashes(project, state)
    workbook = project / state["workbookPath"]
    rows = workbook_rows(project, workbook, "read-assets")
    expected = {item["slotName"]: item for item in state["assetSelections"]}
    assets = {item["id"]: item for item in state.get("visualAssets") or []}
    assets_by_hash = {item["sha256"]: item for item in assets.values()}
    slots = {item["id"]: item for item in state.get("visualSlots") or []}
    assert_exact_ids((row["slotName"] for row in rows), expected, "图片确认表")
    final_images = workbook_images_by_row(workbook, "图片与视觉资产", 1)
    confirmed = []
    for row in rows:
        item = expected[row["slotName"]]
        images = final_images.get(int(row["excelRow"]), [])
        if len(images) > 1:
            raise WorkflowError(f"Excel 第 {row['excelRow']} 行“最终使用图片”区域只能放一张图")
        asset_id = ""
        if images:
            image = images[0]
            digest = hashlib.sha256(image["bytes"]).hexdigest()
            asset = assets_by_hash.get(digest)
            if not asset:
                suffix = Path(str(image["path"])).suffix.lower() or ".bin"
                destination = project / "work" / "confirmed-assets" / f"{digest}{suffix}"
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(image["bytes"])
                asset = {
                    "id": f"asset_sha256_{digest[:24]}", "sourceAssetId": "workbook-upload",
                    "name": destination.name, "path": relative(project, destination), "sha256": digest,
                    "mediaType": "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/png",
                    "width": None, "height": None,
                }
                state.setdefault("visualAssets", []).append(asset)
                assets[asset["id"]] = asset
                assets_by_hash[digest] = asset
            asset_id = asset["id"]
        slot_id = item["slotId"]
        fit = str(item.get("fitMode") or slots[slot_id].get("defaultFit") or "contain").lower()
        if slots[slot_id].get("required") and not asset_id:
            raise WorkflowError(f"Excel 第 {row['excelRow']} 行必填图片为空")
        if fit not in {"contain", "cover", "stretch", "hide"}:
            raise WorkflowError(f"Excel 第 {row['excelRow']} 行适配方式不受支持：{fit}")
        confirmed.append({**item, "finalAssetId": asset_id, "fitMode": fit, "excelRow": int(row["excelRow"])})
    output = project / "work" / "confirmed-assets.json"
    write_json(output, {"confirmedAt": now(), "rows": confirmed})
    state["assetSelections"] = confirmed
    state["assetConfirmedAt"] = now()
    state["confirmedAssetsPath"] = relative(project, output)
    state["stage"] = "ready_to_render_chinese"
    save_state(project, state)
    print(json.dumps({"stage": state["stage"], "next": "运行 render-source-chinese 生成中文版 AI/PDF"}, ensure_ascii=False, indent=2))


def render_asset_bindings(project: Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    assets = {item["id"]: item for item in state.get("visualAssets") or []}
    slots = {item["id"]: item for item in state.get("visualSlots") or []}
    bindings = []
    for selection in state.get("assetSelections") or []:
        slot = slots[selection["slotId"]]
        asset_id = selection.get("finalAssetId") or ""
        asset = assets.get(asset_id)
        mode = "hide_when_empty" if not asset and slot.get("emptyBehavior") == "hide" else slot["replacementMode"]
        bindings.append({
            "slotId": slot["id"], "mode": mode,
            "objectRefs": slot.get("objectRefs") or [], "bounds": slot.get("bounds"),
            "fit": selection.get("fitMode") or slot.get("defaultFit") or "contain",
            "path": str((project / asset["path"]).resolve()) if asset and not Path(asset["path"]).is_absolute() else (asset or {}).get("path", ""),
            "assetId": asset_id, "minDpi": slot.get("minDpi", 150),
            "required": bool(slot.get("required", False)), "sha256": (asset or {}).get("sha256", ""),
            "emptyBehavior": slot.get("emptyBehavior", "keep_template"),
            "width": (asset or {}).get("width"), "height": (asset or {}).get("height"),
        })
    return bindings


def render_items(state: dict[str, Any], language: str) -> list[dict[str, Any]]:
    translations = {item["fieldId"]: item for item in state["translations"] if item["language"] == language}
    items = []
    for field in state["fields"]:
        translation = translations[field["fieldId"]]
        target = translation.get("finalTranslation") or ""
        if not target and field.get("allowTemplateDefault"):
            target = field.get("templateText") or ""
        items.append({
            "index": field["objectIndex"],
            "objectType": field["objectType"],
            "targetText": target,
            "remove": not bool(target),
            "expectedSourceText": field["templateText"],
            "bounds": field.get("bounds"),
            "locator": {"name": field.get("objectName", ""), "layer": field.get("layerName", ""), "bounds": field.get("bounds")},
            "slotId": field["fieldId"],
            "fontName": field.get("fontName"),
            "fontSize": field.get("fontSize"),
        })
    items.extend(render_visual_label_items(
        state,
        {field_id: item.get("finalTranslation") or "" for field_id, item in translations.items()},
        {item["index"] for item in items},
    ))
    return items


def render_visual_label_items(
    state: dict[str, Any],
    target_by_field_id: dict[str, str],
    occupied_indexes: set[int],
) -> list[dict[str, Any]]:
    items = []
    selected_asset_by_slot = {
        str(item.get("slotId") or ""): str(item.get("finalAssetId") or "")
        for item in state.get("assetSelections") or []
    }
    for slot in state.get("visualSlots") or []:
        label = slot.get("labelTextFrame")
        if not label:
            continue
        index = int(label["index"])
        if index in occupied_indexes:
            raise WorkflowError(f"视觉槽位 {slot['id']} 的型号标签与普通文本字段重复指向 textFrame {index}")
        target_field_id = str(label["targetFieldId"])
        if target_field_id not in target_by_field_id:
            raise WorkflowError(f"视觉槽位 {slot['id']} 的型号标签引用了未知字段：{target_field_id}")
        selected_asset = selected_asset_by_slot.get(str(slot["id"]), "")
        if not selected_asset and slot.get("emptyBehavior") == "keep_template":
            continue
        target = target_by_field_id[target_field_id] if selected_asset else ""
        items.append({
            "index": index,
            "objectType": "text",
            "targetText": target,
            "remove": not bool(target),
            "expectedSourceText": str(label["sourceText"]),
            "bounds": label.get("bounds"),
            "slotId": f"{slot['id']}.label",
            "fontName": label.get("fontName"),
            "fontSize": label.get("fontSize"),
        })
        occupied_indexes.add(index)
    return items


def render_source_chinese_items(state: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for field in state["fields"]:
        target = field.get("finalChinese") or ""
        if not target and field.get("allowTemplateDefault"):
            target = field.get("templateText") or ""
        font_size = field.get("fontSize")
        is_heading = "标题" in str(field.get("fieldName") or "") or (font_size is not None and float(font_size) >= 12)
        items.append({
            "index": field["objectIndex"],
            "objectType": field["objectType"],
            "targetText": target,
            "remove": not bool(target),
            "expectedSourceText": field["templateText"],
            "bounds": field.get("bounds"),
            "locator": {"name": field.get("objectName", ""), "layer": field.get("layerName", ""), "bounds": field.get("bounds")},
            "slotId": field["fieldId"],
            "fontName": "PingFangSC-Semibold" if is_heading else "PingFangSC-Regular",
            "fontSize": font_size,
        })
    items.extend(render_visual_label_items(
        state,
        {field["fieldId"]: field.get("finalChinese") or "" for field in state["fields"]},
        {item["index"] for item in items},
    ))
    return items


def execute_render(args: argparse.Namespace, targets: list[dict[str, Any]], results_name: str, *, phase: str) -> None:
    project = project_path(args.project)
    state = load_state(project)
    if phase == "source_chinese":
        require_stage(state, "ready_to_render_chinese", "blocked_chinese")
        ready_stage = "ready_to_render_chinese"
        blocked_stage = "blocked_chinese"
        waiting_stage = "waiting_chinese_layout_confirmation"
        result_key = "sourceChineseRenderResultsPath"
    else:
        require_stage(state, "ready_to_render", "blocked")
        require_source_chinese_confirmation(project, state)
        if not state.get("translationConfirmedAt"):
            raise WorkflowError("其他语种 Excel 尚未完成整表确认")
        ready_stage = "ready_to_render"
        blocked_stage = "blocked"
        waiting_stage = "waiting_layout_confirmation"
        result_key = "renderResultsPath"
    ensure_input_hashes(project, state)
    require_asset_confirmation(project, state)
    _, illustrator_worker = repo_imports()
    source = project / state["template"]["path"]
    layout_rules = read_json(project / state["template"]["layoutRulesPath"])
    results = []
    blocking = []
    for target in targets:
        language = str(target["language"])
        output_name = str(target["outputName"])
        output_dir = project / "preview" / language
        job = {
            "id": f"manual-{language}",
            "source": str(source),
            "sourceHash": state["template"]["sha256"],
            "output_dir": str(output_dir),
            "output_name": output_name,
            "formats": ["ai", "pdf", "preview"],
            "overwrite": True,
            "items": target["items"],
            "assetBindings": render_asset_bindings(project, state),
            "layoutRules": layout_rules,
        }
        job_path = project / "work" / f"render-job-{language}.json"
        write_json(job_path, job)
        result = illustrator_worker.render_job(job, execute=not args.no_execute, allowed_output_root=output_dir)
        results.append({"language": language, **result})
        for issue in result.get("qualityIssues") or []:
            if issue.get("level") == "blocking":
                blocking.append({"language": language, **issue})
        if not args.no_execute and not any(item.get("type") == "page_png" for item in result.get("artifacts") or []):
            blocking.append({
                "language": language,
                "level": "blocking",
                "type": "page_preview_missing",
                "message": "未生成逐页 PDF 校对图；请安装 pdftoppm 后重新 render",
                "status": "open",
            })
    output = project / "work" / results_name
    write_json(output, {"results": results, "blocking": blocking})
    state[result_key] = relative(project, output)
    if args.no_execute:
        state["stage"] = ready_stage
    else:
        state["stage"] = blocked_stage if blocking else waiting_stage
    state["blockingIssues"] = blocking
    save_state(project, state)
    if args.no_execute:
        message = "只生成了 Illustrator JSX；尚未生成可确认的 AI/PDF，请去掉 --no-execute 后重新运行"
    else:
        message = "请查看各语种 PDF；无问题后明确回复“版式确认”" if not blocking else "存在阻塞问题，修复后重新 render"
    print(json.dumps({"stage": state["stage"], "preview": str(project / "preview"), "blockingIssues": blocking, "message": message}, ensure_ascii=False, indent=2))


def command_render(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "ready_to_render", "blocked")
    ensure_input_hashes(project, state)
    require_asset_confirmation(project, state)
    targets = [
        {"language": language, "outputName": f"manual-{language}", "items": render_items(state, language)}
        for language in state["languages"]
    ]
    execute_render(args, targets, "render-results.json", phase="translations")


def command_render_source_chinese(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    execute_render(args, [{
        "language": "zh-CN",
        "outputName": "说明书",
        "items": render_source_chinese_items(state),
    }], "render-results-zh-CN.json", phase="source_chinese")


def copy_confirmed_results(project: Path, render_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    delivered = []
    for result in render_results:
        language = result["language"]
        preview_root = (project / "preview" / language).resolve()
        directory = project / "delivery" / language
        directory.mkdir(parents=True, exist_ok=True)
        outputs = result.get("outputs") or {}
        artifact_hashes = {
            str(item.get("type")): str(item.get("sha256"))
            for item in result.get("artifacts") or []
            if item.get("type") and item.get("sha256")
        }
        missing = [kind for kind in ("ai", "pdf") if not Path(outputs.get(kind) or "").is_file()]
        if missing:
            raise WorkflowError(f"{language} 缺少正式输出：{', '.join(missing)}")
        for kind, value in outputs.items():
            source = Path(value).resolve()
            if not source.is_file():
                continue
            if not source.is_relative_to(preview_root):
                raise WorkflowError(f"{language} 的 {kind} 输出不在受控 preview 目录")
            artifact_type = "preview_png" if kind == "preview" else kind
            expected_hash = artifact_hashes.get(artifact_type)
            if not expected_hash:
                raise WorkflowError(f"{language} 的 {kind} 输出缺少生成时哈希")
            if sha256(source) != expected_hash:
                raise WorkflowError(f"{language} 的 {kind} 输出在校对后发生变化")
            target = directory / source.name
            shutil.copy2(source, target)
            delivered.append({"language": language, "type": kind, "path": relative(project, target), "sha256": sha256(target)})
        qa = Path(result.get("qaReport") or "").resolve()
        if not qa.is_file() or not qa.is_relative_to(preview_root):
            raise WorkflowError(f"{language} 缺少受控排版校对报告")
        if not artifact_hashes.get("layout_qa") or sha256(qa) != artifact_hashes["layout_qa"]:
            raise WorkflowError(f"{language} 的排版校对报告在生成后发生变化")
        target = directory / qa.name
        shutil.copy2(qa, target)
        delivered.append({"language": language, "type": "qa", "path": relative(project, target), "sha256": sha256(target)})
        page_artifacts = [item for item in result.get("artifacts") or [] if item.get("type") == "page_png"]
        if not page_artifacts:
            raise WorkflowError(f"{language} 缺少逐页 PDF 校对图")
        for artifact in page_artifacts:
            source = Path(artifact.get("path") or "").resolve()
            if not source.is_file() or not source.is_relative_to(preview_root):
                raise WorkflowError(f"{language} 缺少受控逐页校对图：{source.name}")
            if not artifact.get("sha256") or sha256(source) != artifact["sha256"]:
                raise WorkflowError(f"{language} 的逐页校对图在生成后发生变化")
            target = directory / source.name
            shutil.copy2(source, target)
            delivered.append({"language": language, "type": "page_png", "path": relative(project, target), "sha256": sha256(target)})
    return delivered


def command_confirm_source_chinese_layout(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "waiting_chinese_layout_confirmation")
    ensure_input_hashes(project, state)
    results_path = project / state["sourceChineseRenderResultsPath"]
    delivered = copy_confirmed_results(project, read_json(results_path)["results"])
    manifest = project / "delivery" / "source-chinese-manifest.json"
    write_json(manifest, {"projectSchema": SCHEMA, "confirmedAt": now(), "files": delivered})
    state["sourceChineseManifestPath"] = relative(project, manifest)
    state["sourceChineseLayoutConfirmedAt"] = now()
    state["stage"] = "needs_translation_generation"
    translation_input = create_translation_input(project, state)
    save_state(project, state)
    print(json.dumps({
        "stage": state["stage"], "chineseDelivery": str(project / "delivery" / "zh-CN"),
        "translationInput": str(translation_input),
        "next": "生成其他语种内容并运行 append-translations",
    }, ensure_ascii=False, indent=2))


def command_confirm_layout(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "waiting_layout_confirmation")
    ensure_input_hashes(project, state)
    render_results = read_json(project / state["renderResultsPath"])["results"]
    require_source_chinese_confirmation(project, state)
    electronic = list(read_json(project / state["sourceChineseManifestPath"]).get("files") or [])
    electronic.extend(copy_confirmed_results(project, render_results))
    manifest = project / "work" / "electronic-manifest.json"
    write_json(manifest, {"projectSchema": SCHEMA, "confirmedAt": now(), "files": electronic})
    state["stage"] = "ready_to_impose_ab"
    state["electronicConfirmedAt"] = now()
    state["electronicManifestPath"] = relative(project, manifest)
    save_state(project, state)
    print(json.dumps({
        "stage": state["stage"], "electronicManifest": str(manifest),
        "next": "小册子运行 impose-ab；五折页提供明确 plan 后运行 impose-five-fold",
    }, ensure_ascii=False, indent=2))


def expected_languages(state: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(["zh-CN", *[str(value) for value in state.get("languages") or []]]))


def electronic_sources(project: Path, state: dict[str, Any]) -> dict[str, dict[str, Path]]:
    manifest = read_json(project / state["electronicManifestPath"])
    grouped: dict[str, dict[str, Path]] = {}
    for item in manifest.get("files") or []:
        language = str(item.get("language") or "")
        kind = str(item.get("type") or "")
        if kind not in ("ai", "pdf"):
            continue
        path = (project / str(item.get("path") or "")).resolve()
        if not path.is_file() or sha256(path) != item.get("sha256"):
            raise WorkflowError(f"{language} 电子版 {kind.upper()} 在确认后发生变化")
        grouped.setdefault(language, {})[kind] = path
    expected = expected_languages(state)
    missing = [language for language in expected if set(grouped.get(language) or {}) != {"ai", "pdf"}]
    extras = sorted(set(grouped) - set(expected))
    if missing or extras:
        raise WorkflowError(f"电子版语种集不完整；缺失={missing}，多余={extras}")
    return grouped


def current_imposition_runtime_sha256() -> str:
    return sha256(SKILL_ROOT / "scripts" / "illustrator_imposition.py")


def validate_imposition_result_contract(result: dict[str, Any], variant: str) -> None:
    if int(result.get("qaContractVersion") or 0) != IMPOSITION_QA_CONTRACT_VERSION:
        raise WorkflowError(f"{variant} 拼版结果使用旧 QA 契约；请用当前 Skill 重新生成")
    if str(result.get("runtimeSha256") or "") != current_imposition_runtime_sha256():
        raise WorkflowError(f"{variant} 拼版结果由不同版本脚本生成；请用当前 Skill 重新生成")


def validate_imposition_qa(path: Path, variant: str) -> None:
    qa = read_json(path)
    if qa.get("schema") != "illustrator-imposition/1.0" or int(qa.get("qaContractVersion") or 0) != IMPOSITION_QA_CONTRACT_VERSION:
        raise WorkflowError(f"{variant} 拼版 QA 文件版本过旧或格式不受支持")
    if str(qa.get("variant") or "") != variant:
        raise WorkflowError(f"拼版 QA 版本不匹配：期望 {variant}，实际 {qa.get('variant')}")
    if not qa.get("editableObjectsPreserved") or float(qa.get("bleedPt", -1)) != 0:
        raise WorkflowError(f"{variant} 拼版未通过可编辑对象或零出血校验")
    if int(qa.get("artboardCount") or 0) <= 0:
        raise WorkflowError(f"{variant} 拼版 QA 缺少有效画板数量")
    if variant == "AB":
        source = qa.get("sourceCounts") or {}
        output = qa.get("outputCounts") or {}
        if int(source.get("topLevelItems", -1)) != int(output.get("topLevelItems", -2)):
            raise WorkflowError("AB 拼版前后顶层可编辑对象数量不一致")
        accounted = int(qa.get("mappedTopLevelItems") or 0) + int(qa.get("preservedTopLevelItems") or 0)
        if accounted != int(output.get("topLevelItems", -1)):
            raise WorkflowError("AB 拼版存在未追踪的顶层对象")
    elif int(qa.get("expectedTopLevelItems", -1)) != int(qa.get("outputTopLevelItems", -2)):
        raise WorkflowError(f"{variant} 版拆分前后顶层可编辑对象数量不一致")


def _verified_artifacts(result: dict[str, Any], allowed_root: Path, *, required: set[str], variant: str) -> list[dict[str, Any]]:
    validate_imposition_result_contract(result, variant)
    artifacts = list(result.get("artifacts") or [])
    available = {str(item.get("type") or "") for item in artifacts}
    missing = sorted(required - available)
    if missing:
        raise WorkflowError(f"{result.get('language')} 缺少拼版产物：{', '.join(missing)}")
    if sum(1 for item in artifacts if item.get("type") == "imposition_qa") != 1:
        raise WorkflowError(f"{variant} 拼版必须且只能包含一个 QA 文件")
    verified = []
    for item in artifacts:
        source = Path(str(item.get("path") or "")).resolve()
        if not source.is_file() or not source.is_relative_to(allowed_root.resolve()):
            raise WorkflowError(f"拼版产物不在受控 preview 目录：{source}")
        if not item.get("sha256") or sha256(source) != item["sha256"]:
            raise WorkflowError(f"拼版产物在确认后发生变化：{source.name}")
        if item.get("type") == "imposition_qa":
            validate_imposition_qa(source, variant)
        verified.append({**item, "path": str(source)})
    return verified


def command_impose_ab(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "ready_to_impose_ab", "blocked_ab_imposition", "waiting_ab_confirmation")
    ensure_input_hashes(project, state)
    sources = electronic_sources(project, state)
    imposition = imposition_import()
    layout_rules = read_json(project / state["template"]["layoutRulesPath"])
    current_rules_source = Path(str(state["template"].get("layoutRulesSource") or "")).expanduser()
    if current_rules_source.is_file():
        current_rules = read_json(current_rules_source)
        if str(current_rules.get("templateSha256") or "") == str(state["template"].get("sha256") or ""):
            layout_rules["impositionPolicy"] = dict(current_rules.get("impositionPolicy") or layout_rules.get("impositionPolicy") or {})
    results, blocking = [], []
    for language in expected_languages(state):
        output_dir = project / "preview" / language / "imposition" / "AB"
        result = imposition.impose_ab_job({
            "language": language,
            "sourceAI": str(sources[language]["ai"]),
            "sourcePDF": str(sources[language]["pdf"]),
            "outputDir": str(output_dir),
            "outputName": f"manual-{language}-AB打印版",
            "layoutRules": layout_rules,
        }, execute=not args.no_execute)
        results.append({"language": language, **result})
        blocking.extend({"language": language, **issue} for issue in result.get("qualityIssues") or [] if issue.get("level") == "blocking")
    output = project / "work" / "imposition-ab-results.json"
    write_json(output, {"results": results, "blocking": blocking})
    state["abImpositionResultsPath"] = relative(project, output)
    state["blockingIssues"] = blocking
    state["blockingPhase"] = "ab_imposition" if blocking else None
    state["stage"] = "ready_to_impose_ab" if args.no_execute else ("blocked_ab_imposition" if blocking else "waiting_ab_confirmation")
    save_state(project, state)
    print(json.dumps({
        "stage": state["stage"], "results": str(output), "blockingIssues": blocking,
        "message": "请逐语种确认 AB AI/PDF，确认后运行 confirm-ab" if not blocking and not args.no_execute else "请根据 userAction 处理阻塞问题后重试",
    }, ensure_ascii=False, indent=2))


def current_folded_leaflet_runtime_sha256() -> str:
    return sha256(SKILL_ROOT / "scripts" / "illustrator_folded_leaflet.py")


def current_small_format_runtime_sha256() -> str:
    return sha256(SKILL_ROOT / "scripts" / "illustrator_small_format.py")


def _verified_small_format_artifacts(result: dict[str, Any], allowed_root: Path) -> list[dict[str, Any]]:
    if int(result.get("qaContractVersion") or 0) != SMALL_FORMAT_QA_CONTRACT_VERSION:
        raise WorkflowError("小版面结果使用旧 QA 契约；请用当前 Skill 重新生成")
    if str(result.get("runtimeSha256") or "") != current_small_format_runtime_sha256():
        raise WorkflowError("小版面结果由不同版本脚本生成；请用当前 Skill 重新生成")
    artifacts = list(result.get("artifacts") or [])
    available = {str(item.get("type") or "") for item in artifacts}
    required = {"ai", "pdf", "imposition_qa", "small_format_manifest", "page_png"}
    missing = sorted(required - available)
    if missing:
        raise WorkflowError(f"{result.get('language')} 缺少小版面产物：{', '.join(missing)}")
    verified = []
    for item in artifacts:
        source = Path(str(item.get("path") or "")).resolve()
        if not source.is_file() or not source.is_relative_to(allowed_root.resolve()):
            raise WorkflowError(f"小版面产物不在受控 preview 目录：{source}")
        if not item.get("sha256") or sha256(source) != item["sha256"]:
            raise WorkflowError(f"小版面产物在确认后发生变化：{source.name}")
        if item.get("type") == "imposition_qa":
            qa = read_json(source)
            if qa.get("schema") != "illustrator-small-format/1.0" or int(qa.get("qaContractVersion") or 0) != SMALL_FORMAT_QA_CONTRACT_VERSION:
                raise WorkflowError("小版面 QA 文件不符合当前契约")
            if not qa.get("editableObjectsPreserved"):
                raise WorkflowError("小版面 QA 未证明可编辑对象得到保留")
        verified.append({**item, "path": str(source)})
    return verified


def command_layout_small_format(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "ready_to_impose_ab", "blocked_small_format", "waiting_small_format_confirmation")
    ensure_input_hashes(project, state)
    sources = electronic_sources(project, state)
    small_format = small_format_import()
    plan = read_json(Path(args.plan).expanduser().resolve()) if args.plan else {}
    results, blocking = [], []
    for language in expected_languages(state):
        output_dir = project / "preview" / language / "small-format"
        result = small_format.layout_small_format_job({
            "language": language,
            "sourceAI": str(sources[language]["ai"]),
            "sourcePDF": str(sources[language]["pdf"]),
            "outputDir": str(output_dir),
            "outputName": f"manual-{language}-小版面",
            "plan": plan,
        }, execute=not args.no_execute)
        results.append({"language": language, **result})
        blocking.extend({"language": language, **issue} for issue in result.get("qualityIssues") or [] if issue.get("level") == "blocking")
    output = project / "work" / "small-format-results.json"
    write_json(output, {"results": results, "blocking": blocking, "plan": plan})
    state["smallFormatResultsPath"] = relative(project, output)
    state["blockingIssues"] = blocking
    state["blockingPhase"] = "small_format" if blocking else None
    state["printVariant"] = "small-format"
    state["stage"] = "ready_to_impose_ab" if args.no_execute else ("blocked_small_format" if blocking else "waiting_small_format_confirmation")
    save_state(project, state)
    print(json.dumps({
        "stage": state["stage"], "results": str(output), "blockingIssues": blocking,
        "message": "请逐语种确认自动分页的小版面 AI/PDF，确认后运行 confirm-small-format" if not blocking and not args.no_execute else "请按 userAction 修复小版面阻塞问题后重试",
    }, ensure_ascii=False, indent=2))


def command_confirm_small_format(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "waiting_small_format_confirmation")
    ensure_input_hashes(project, state)
    results = read_json(project / state["smallFormatResultsPath"]).get("results") or []
    if [item.get("language") for item in results] != expected_languages(state):
        raise WorkflowError("小版面结果语种集不完整或顺序不一致")
    copied: list[dict[str, Any]] = []
    page_counts: dict[str, int] = {}
    for result in results:
        language = str(result["language"])
        if result.get("status") != "succeeded" or result.get("qualityIssues"):
            raise WorkflowError(f"{language} 小版面尚未通过自动校验")
        root = project / "preview" / language / "small-format"
        artifacts = _verified_small_format_artifacts(result, root)
        manifest = read_json(Path(result["manifest"]).resolve())
        page_counts[language] = int(manifest.get("pageCount") or 0)
        for artifact in artifacts:
            source = Path(artifact["path"])
            target = project / "delivery" / language / "SMALL_FORMAT" / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append({
                "language": language, "variant": "SMALL_FORMAT", "type": artifact["type"],
                "path": relative(project, target), "sha256": sha256(target),
            })
    files = list(read_json(project / state["electronicManifestPath"]).get("files") or [])
    files.extend(copied)
    small_manifest = project / "delivery" / "small-format-manifest.json"
    write_json(small_manifest, {"projectSchema": SCHEMA, "confirmedAt": now(), "files": copied, "pageCounts": page_counts})
    delivery_manifest = project / "delivery" / "delivery-manifest.json"
    write_json(delivery_manifest, {"projectSchema": SCHEMA, "deliveredAt": now(), "files": files})
    state["stage"] = "delivered"
    state["smallFormatConfirmedAt"] = now()
    state["smallFormatManifestPath"] = relative(project, small_manifest)
    state["deliveryManifestPath"] = relative(project, delivery_manifest)
    state["deliveryPackageGenerated"] = True
    state["blockingIssues"] = []
    state.pop("blockingPhase", None)
    save_state(project, state)
    print(json.dumps({"stage": state["stage"], "delivery": str(project / "delivery"), "manifest": str(delivery_manifest)}, ensure_ascii=False, indent=2))


def validate_folded_leaflet_result_contract(result: dict[str, Any]) -> None:
    if int(result.get("qaContractVersion") or 0) != FOLDED_LEAFLET_QA_CONTRACT_VERSION:
        raise WorkflowError("五折页结果使用旧 QA 契约；请用当前 Skill 重新生成")
    if str(result.get("runtimeSha256") or "") != current_folded_leaflet_runtime_sha256():
        raise WorkflowError("五折页结果由不同版本脚本生成；请用当前 Skill 重新生成")


def _verified_folded_leaflet_artifacts(result: dict[str, Any], allowed_root: Path) -> list[dict[str, Any]]:
    validate_folded_leaflet_result_contract(result)
    artifacts = list(result.get("artifacts") or [])
    available = {str(item.get("type") or "") for item in artifacts}
    required = {"ai", "pdf", "imposition_qa", "folded_leaflet_manifest", "page_png"}
    missing = sorted(required - available)
    if missing:
        raise WorkflowError(f"{result.get('language')} 缺少五折页产物：{', '.join(missing)}")
    verified = []
    for item in artifacts:
        source = Path(str(item.get("path") or "")).resolve()
        if not source.is_file() or not source.is_relative_to(allowed_root.resolve()):
            raise WorkflowError(f"五折页产物不在受控 preview 目录：{source}")
        if not item.get("sha256") or sha256(source) != item["sha256"]:
            raise WorkflowError(f"五折页产物在确认后发生变化：{source.name}")
        if item.get("type") == "imposition_qa":
            qa = read_json(source)
            if qa.get("schema") != "illustrator-folded-leaflet/1.0" or int(qa.get("qaContractVersion") or 0) != FOLDED_LEAFLET_QA_CONTRACT_VERSION:
                raise WorkflowError("五折页 QA 文件不符合当前契约")
            if not qa.get("editableObjectsPreserved"):
                raise WorkflowError("五折页 QA 未证明可编辑对象得到保留")
        verified.append({**item, "path": str(source)})
    return verified


def command_impose_five_fold(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "ready_to_impose_ab", "blocked_folded_leaflet", "waiting_folded_leaflet_confirmation")
    ensure_input_hashes(project, state)
    sources = electronic_sources(project, state)
    leaflet = folded_leaflet_import()
    plan = read_json(Path(args.plan).expanduser().resolve()) if args.plan else {}
    results, blocking = [], []
    for language in expected_languages(state):
        output_dir = project / "preview" / language / "imposition" / "FIVE_FOLD"
        result = leaflet.layout_leaflet_job({
            "language": language,
            "sourceAI": str(sources[language]["ai"]),
            "sourcePDF": str(sources[language]["pdf"]),
            "outputDir": str(output_dir),
            "outputName": f"manual-{language}-五折页打印版",
            "plan": plan,
        }, execute=not args.no_execute)
        results.append({"language": language, **result})
        blocking.extend({"language": language, **issue} for issue in result.get("qualityIssues") or [] if issue.get("level") == "blocking")
    output = project / "work" / "folded-leaflet-results.json"
    write_json(output, {"results": results, "blocking": blocking, "plan": plan})
    state["foldedLeafletResultsPath"] = relative(project, output)
    state["blockingIssues"] = blocking
    state["blockingPhase"] = "folded_leaflet" if blocking else None
    state["printVariant"] = "five-fold"
    state["stage"] = "ready_to_impose_ab" if args.no_execute else ("blocked_folded_leaflet" if blocking else "waiting_folded_leaflet_confirmation")
    save_state(project, state)
    print(json.dumps({
        "stage": state["stage"],
        "results": str(output),
        "blockingIssues": blocking,
        "message": "请逐语种确认五折页 AI/PDF，确认后运行 confirm-five-fold" if not blocking and not args.no_execute else "请按 userAction 修复五折页阻塞问题后重试",
    }, ensure_ascii=False, indent=2))


def command_confirm_five_fold(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "waiting_folded_leaflet_confirmation")
    ensure_input_hashes(project, state)
    results = read_json(project / state["foldedLeafletResultsPath"]).get("results") or []
    if [item.get("language") for item in results] != expected_languages(state):
        raise WorkflowError("五折页结果语种集不完整或顺序不一致")
    copied: list[dict[str, Any]] = []
    print_profiles: dict[str, Any] = {}
    for result in results:
        language = str(result["language"])
        if result.get("status") != "succeeded" or result.get("qualityIssues"):
            raise WorkflowError(f"{language} 五折页尚未通过自动校验")
        root = project / "preview" / language / "imposition" / "FIVE_FOLD"
        artifacts = _verified_folded_leaflet_artifacts(result, root)
        manifest = read_json(Path(result["manifest"]).resolve())
        profile = dict(manifest.get("printProfile") or {})
        if profile.get("duplexFlip") not in ("long-edge", "short-edge"):
            raise WorkflowError(f"{language} 五折页尚未确认正反面翻转方向")
        print_profiles[language] = profile
        for artifact in artifacts:
            source = Path(artifact["path"])
            target = project / "delivery" / language / "FIVE_FOLD" / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append({
                "language": language,
                "variant": "FIVE_FOLD",
                "type": artifact["type"],
                "path": relative(project, target),
                "sha256": sha256(target),
            })
    files = list(read_json(project / state["electronicManifestPath"]).get("files") or [])
    files.extend(copied)
    folded_manifest = project / "delivery" / "folded-leaflet-manifest.json"
    write_json(folded_manifest, {
        "projectSchema": SCHEMA,
        "confirmedAt": now(),
        "files": copied,
        "printProfiles": print_profiles,
    })
    delivery_manifest = project / "delivery" / "delivery-manifest.json"
    write_json(delivery_manifest, {"projectSchema": SCHEMA, "deliveredAt": now(), "files": files})
    state["stage"] = "delivered"
    state["foldedLeafletConfirmedAt"] = now()
    state["foldedLeafletManifestPath"] = relative(project, folded_manifest)
    state["deliveryManifestPath"] = relative(project, delivery_manifest)
    state["deliveryPackageGenerated"] = True
    state["blockingIssues"] = []
    state.pop("blockingPhase", None)
    save_state(project, state)
    print(json.dumps({"stage": state["stage"], "delivery": str(project / "delivery"), "manifest": str(delivery_manifest)}, ensure_ascii=False, indent=2))


def command_confirm_ab(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "waiting_ab_confirmation")
    ensure_input_hashes(project, state)
    payload = read_json(project / state["abImpositionResultsPath"])
    results = payload.get("results") or []
    if [item.get("language") for item in results] != expected_languages(state):
        raise WorkflowError("AB 结果语种集不完整或顺序不一致")
    confirmed = []
    for result in results:
        if result.get("status") != "succeeded" or result.get("qualityIssues"):
            raise WorkflowError(f"{result.get('language')} AB 版尚未通过自动校验")
        root = project / "preview" / result["language"] / "imposition" / "AB"
        artifacts = _verified_artifacts(result, root, required={"ai", "pdf", "imposition_qa", "imposition_manifest", "page_png"}, variant="AB")
        manifest_path = Path(result["manifest"]).resolve()
        confirmed.append({
            "language": result["language"], "confirmedAt": now(), "artifacts": artifacts,
            "sourceAI": result["outputs"]["ai"], "sourceAISha256": sha256(Path(result["outputs"]["ai"])),
            "abManifest": read_json(manifest_path),
        })
    manifest = project / "work" / "confirmed-ab-manifest.json"
    write_json(manifest, {"projectSchema": SCHEMA, "confirmedAt": now(), "results": confirmed})
    state["abConfirmedAt"] = now()
    state["confirmedAbManifestPath"] = relative(project, manifest)
    state["blockingIssues"] = []
    state.pop("blockingPhase", None)
    state["stage"] = "ready_to_split_ab"
    save_state(project, state)
    print(json.dumps({"stage": state["stage"], "manifest": str(manifest), "next": "运行 split-a-b 生成 A 版和 B 版"}, ensure_ascii=False, indent=2))


def command_split_a_b(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "ready_to_split_ab", "blocked_a_b_split")
    ensure_input_hashes(project, state)
    imposition = imposition_import()
    confirmed = read_json(project / state["confirmedAbManifestPath"]).get("results") or []
    if [item.get("language") for item in confirmed] != expected_languages(state):
        raise WorkflowError("已确认 AB 语种集不完整或顺序不一致")
    results, blocking = [], []
    for item in confirmed:
        language = item["language"]
        result = imposition.split_ab_job({
            "language": language, "sourceAI": item["sourceAI"], "sourceAISha256": item["sourceAISha256"],
            "abManifest": item["abManifest"], "outputDir": str(project / "preview" / language / "imposition"),
            "outputPrefix": f"manual-{language}",
        }, execute=not args.no_execute)
        results.append({"language": language, **result})
        blocking.extend({"language": language, **issue} for issue in result.get("qualityIssues") or [] if issue.get("level") == "blocking")
    output = project / "work" / "split-a-b-results.json"
    write_json(output, {"results": results, "blocking": blocking})
    state["splitResultsPath"] = relative(project, output)
    state["blockingIssues"] = blocking
    state["blockingPhase"] = "a_b_split" if blocking else None
    state["stage"] = "ready_to_split_ab" if args.no_execute else ("blocked_a_b_split" if blocking else "waiting_a_b_confirmation")
    save_state(project, state)
    print(json.dumps({
        "stage": state["stage"], "results": str(output), "blockingIssues": blocking,
        "message": "请确认每个语种的 A/B AI/PDF，确认后运行 confirm-a-b" if not blocking and not args.no_execute else "请处理阻塞问题后重试",
    }, ensure_ascii=False, indent=2))


def command_confirm_a_b(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "waiting_a_b_confirmation")
    ensure_input_hashes(project, state)
    split_results = read_json(project / state["splitResultsPath"]).get("results") or []
    confirmed_ab = read_json(project / state["confirmedAbManifestPath"]).get("results") or []
    if [item.get("language") for item in split_results] != expected_languages(state):
        raise WorkflowError("A/B 结果语种集不完整或顺序不一致")
    verified_sources: list[dict[str, Any]] = []
    for item in confirmed_ab:
        language = item["language"]
        for artifact in item["artifacts"]:
            source = Path(artifact["path"]).resolve()
            if not source.is_file() or sha256(source) != artifact["sha256"]:
                raise WorkflowError(f"{language} AB 产物在确认后发生变化：{source.name}")
            verified_sources.append({"language": language, "variant": "AB", "type": artifact["type"], "source": source, "sha256": artifact["sha256"]})
    for result in split_results:
        language = result["language"]
        if result.get("status") != "succeeded" or result.get("qualityIssues"):
            raise WorkflowError(f"{language} A/B 版尚未通过自动校验")
        for variant in ("A", "B"):
            variant_result = result.get("variants", {}).get(variant) or {}
            root = project / "preview" / language / "imposition" / variant
            artifacts = _verified_artifacts(variant_result, root, required={"ai", "pdf", "imposition_qa", "page_png"}, variant=variant)
            for artifact in artifacts:
                source = Path(artifact["path"])
                verified_sources.append({"language": language, "variant": variant, "type": artifact["type"], "source": source, "sha256": artifact["sha256"]})

    confirmed_at = now()
    state["aBConfirmedAt"] = confirmed_at
    state["blockingIssues"] = []
    state.pop("blockingPhase", None)
    if args.no_delivery_package:
        manifest = project / "work" / "confirmed-a-b-manifest.json"
        write_json(manifest, {
            "projectSchema": SCHEMA,
            "confirmedAt": confirmed_at,
            "deliveryPackageGenerated": False,
            "files": [{
                "language": item["language"], "variant": item["variant"], "type": item["type"],
                "path": relative(project, item["source"]), "sha256": item["sha256"],
            } for item in verified_sources],
            "printProfile": {"duplexFlip": "short-edge", "scalePercent": 100, "centered": True, "shrinkToFit": False, "bleedPt": 0},
        })
        state["stage"] = "completed_without_delivery"
        state["confirmedABSplitManifestPath"] = relative(project, manifest)
        state["deliveryPackageGenerated"] = False
        save_state(project, state)
        print(json.dumps({
            "stage": state["stage"], "manifest": str(manifest), "deliveryPackageGenerated": False,
        }, ensure_ascii=False, indent=2))
        return

    files = list(read_json(project / state["electronicManifestPath"]).get("files") or [])
    imposition_files: list[dict[str, Any]] = []
    for item in verified_sources:
        source = item["source"]
        target = project / "delivery" / item["language"] / item["variant"] / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        imposition_files.append({
            "language": item["language"], "variant": item["variant"], "type": item["type"],
            "path": relative(project, target), "sha256": sha256(target),
        })
    imposition_manifest = project / "delivery" / "imposition-manifest.json"
    write_json(imposition_manifest, {
        "projectSchema": SCHEMA, "confirmedAt": now(), "files": imposition_files,
        "printProfile": {"duplexFlip": "short-edge", "scalePercent": 100, "centered": True, "shrinkToFit": False, "bleedPt": 0},
    })
    files.extend(imposition_files)
    manifest = project / "delivery" / "delivery-manifest.json"
    write_json(manifest, {"projectSchema": SCHEMA, "deliveredAt": now(), "files": files})
    state["stage"] = "delivered"
    state["impositionManifestPath"] = relative(project, imposition_manifest)
    state["deliveredAt"] = now()
    state["deliveryManifestPath"] = relative(project, manifest)
    state["deliveryPackageGenerated"] = True
    save_state(project, state)
    print(json.dumps({"stage": state["stage"], "delivery": str(project / "delivery"), "manifest": str(manifest)}, ensure_ascii=False, indent=2))


def command_status(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    messages = {
        "needs_chinese_optimization": "根据 content-optimization-input.json 完成 AI 用户化改写后运行 optimize-chinese",
        "ready_to_export_chinese": "AI 内容优化已完成，运行 export-chinese 生成中文版 Excel",
        "waiting_chinese_confirmation": "等待用户修改 Excel 并明确确认中文内容",
        "needs_translation_generation": "生成多语种翻译 JSON 后运行 append-translations",
        "waiting_translation_confirmation": "等待用户修改 Excel 并明确确认翻译内容",
        "needs_asset_review": "生成图片槽位建议 JSON 后运行 append-assets",
        "waiting_asset_confirmation": "等待用户修改 Excel 并明确确认图片内容",
        "ready_to_render_chinese": "运行 render-source-chinese 生成中文版 AI/PDF",
        "waiting_chinese_layout_confirmation": "等待用户查看中文版 PDF 并明确确认版式",
        "blocked_chinese": "处理中文版阻塞问题后重新运行 render-source-chinese",
        "ready_to_render": "运行 render",
        "waiting_layout_confirmation": "等待用户查看预览 PDF 并明确确认版式",
        "blocked": "处理阻塞问题后重新 render",
        "ready_to_impose_ab": "小版面运行 layout-small-format；小册子运行 impose-ab；需要时再选择五折拼版",
        "blocked_small_format": "按 blockingIssues.userAction 修复后重新运行 layout-small-format",
        "waiting_small_format_confirmation": "等待用户确认全部语种小版面，然后运行 confirm-small-format",
        "blocked_folded_leaflet": "按 blockingIssues.userAction 修复后重新运行 impose-five-fold",
        "waiting_folded_leaflet_confirmation": "等待用户确认全部语种五折页，然后运行 confirm-five-fold",
        "blocked_ab_imposition": "按 blockingIssues.userAction 修复电子版后重新运行 impose-ab",
        "waiting_ab_confirmation": "等待用户确认全部语种 AB 版，然后运行 confirm-ab",
        "ready_to_split_ab": "运行 split-a-b 从已确认 AB AI 生成 A/B 版",
        "blocked_a_b_split": "按 blockingIssues.userAction 处理后重新运行 split-a-b",
        "waiting_a_b_confirmation": "等待用户确认全部语种 A/B 版，然后运行 confirm-a-b",
        "completed_without_delivery": "A/B 已确认，用户选择不生成 delivery 交付包",
        "delivered": "交付完成",
    }
    print(json.dumps({"project": str(project), "stage": state["stage"], "next": messages.get(state["stage"], "未知"), "workbook": str(project / state.get("workbookPath", "")), "languages": state.get("languages", []), "blockingIssues": state.get("blockingIssues", [])}, ensure_ascii=False, indent=2))


def command_doctor(args: argparse.Namespace) -> None:
    extract_sources, illustrator_worker = repo_imports()
    del extract_sources
    imposition = imposition_import()
    folded_leaflet = folded_leaflet_import()
    small_format = small_format_import()
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        raise WorkflowError("缺少 pdftoppm；无法生成正式逐页 PDF 校对图")
    with TemporaryDirectory() as temp:
        node, _, module = artifact_runtime(Path(temp))
    report = illustrator_worker.doctor_report(probe=True)
    payload = {
        "ok": bool(report.get("ok")),
        "workflowSchema": SCHEMA,
        "node": node,
        "artifactToolModule": str(module),
        "pdftoppm": pdftoppm,
        "illustratorWorker": report,
        "impositionSchema": imposition.SCHEMA,
        "foldedLeafletSchema": folded_leaflet.SCHEMA,
        "smallFormatSchema": small_format.SCHEMA,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["ok"]:
        raise WorkflowError("Illustrator 真实自动化探针失败；请检查应用安装、Automation 权限和 Illustrator 启动状态")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="无 UI 的 Illustrator 多语种说明书工作流")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="检查 Skill 内置模块、Excel 运行时和 Illustrator Worker")
    init = sub.add_parser("init", help="初始化项目并抽取规格书/模板")
    init.add_argument("--project", required=True)
    init.add_argument("--spec", action="append", required=True)
    init.add_argument("--template", required=True)
    init.add_argument("--template-metadata")
    init.add_argument("--layout-rules")
    init.add_argument("--languages", required=True, help="逗号分隔，如 de-DE,es-ES")
    init.add_argument("--overwrite", action="store_true")
    optimize = sub.add_parser("optimize-chinese", help="验证并固化 AI 用户化中文改写结果")
    optimize.add_argument("--project", required=True)
    optimize.add_argument("--content-json", required=True)
    export = sub.add_parser("export-chinese", help="根据已固化的 AI 优化结果创建中文确认 Excel")
    export.add_argument("--project", required=True)
    refresh = sub.add_parser("refresh-chinese", help="用新中文 JSON 重建确认书并使下游确认失效")
    refresh.add_argument("--project", required=True)
    refresh.add_argument("--content-json", required=True)
    resolve = sub.add_parser("resolve-conflicts", help="固化全部高严重度规格冲突的明确处理结论")
    resolve.add_argument("--project", required=True)
    resolve.add_argument("--resolutions-json", required=True)
    confirm = sub.add_parser("confirm-chinese", help="在用户整表确认后回读最终中文")
    confirm.add_argument("--project", required=True)
    append = sub.add_parser("append-translations", help="向同一 Excel 追加多语种翻译 Sheet")
    append.add_argument("--project", required=True)
    append.add_argument("--translations-json", required=True)
    confirm_tr = sub.add_parser("confirm-translations", help="在用户整表确认后回读最终译文")
    confirm_tr.add_argument("--project", required=True)
    prepare_assets = sub.add_parser("prepare-assets", help="为旧项目重新读取视觉槽位")
    prepare_assets.add_argument("--project", required=True)
    append_assets = sub.add_parser("append-assets", help="向同一 Excel 追加图片确认 Sheet")
    append_assets.add_argument("--project", required=True)
    append_assets.add_argument("--assets-json", required=True)
    confirm_assets = sub.add_parser("confirm-assets", help="在用户整表确认后回读图片选择")
    confirm_assets.add_argument("--project", required=True)
    render = sub.add_parser("render", help="调用 Illustrator 生成 AI/PDF/预览并执行 QA")
    render.add_argument("--project", required=True)
    render.add_argument("--no-execute", action="store_true")
    render_source = sub.add_parser("render-source-chinese", help="用已确认中文和图片生成中文 AI/PDF")
    render_source.add_argument("--project", required=True)
    render_source.add_argument("--no-execute", action="store_true")
    confirm_source = sub.add_parser("confirm-source-chinese-layout", help="确认中文版 AI/PDF 后开放其他语种 Excel")
    confirm_source.add_argument("--project", required=True)
    layout = sub.add_parser("confirm-layout", help="确认全部电子版 AI/PDF 并进入 AB 拼版")
    layout.add_argument("--project", required=True)
    impose = sub.add_parser("impose-ab", help="为中文和全部目标语种生成可编辑 AB AI/PDF")
    impose.add_argument("--project", required=True)
    impose.add_argument("--no-execute", action="store_true")
    layout_small = sub.add_parser("layout-small-format", help="按内容容量生成可变页数的小版面 AI/PDF")
    layout_small.add_argument("--project", required=True)
    layout_small.add_argument("--plan", help="可选的小版面尺寸和间距 JSON")
    layout_small.add_argument("--no-execute", action="store_true")
    confirm_small = sub.add_parser("confirm-small-format", help="确认全部语种小版面并生成交付包")
    confirm_small.add_argument("--project", required=True)
    impose_five_fold = sub.add_parser("impose-five-fold", help="将电子版原生对象排入双面五折页 AI/PDF")
    impose_five_fold.add_argument("--project", required=True)
    impose_five_fold.add_argument("--plan", help="五折页面板映射、几何和正反面翻转合同 JSON")
    impose_five_fold.add_argument("--no-execute", action="store_true")
    confirm_five_fold = sub.add_parser("confirm-five-fold", help="确认全部语种五折页并生成交付包")
    confirm_five_fold.add_argument("--project", required=True)
    confirm_ab = sub.add_parser("confirm-ab", help="确认全部语种 AB 版后开放 A/B 拆分")
    confirm_ab.add_argument("--project", required=True)
    split = sub.add_parser("split-a-b", help="从已确认 AB AI 生成可编辑 A/B AI/PDF")
    split.add_argument("--project", required=True)
    split.add_argument("--no-execute", action="store_true")
    confirm_split = sub.add_parser("confirm-a-b", help="确认 A/B 版并形成最终交付")
    confirm_split.add_argument("--project", required=True)
    confirm_split.add_argument("--no-delivery-package", action="store_true", help="仅记录 A/B 确认和哈希清单，不复制文件或生成 delivery 交付包")
    status = sub.add_parser("status", help="显示当前阶段和下一步")
    status.add_argument("--project", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    commands = {
        "doctor": command_doctor,
        "init": command_init,
        "optimize-chinese": command_optimize_chinese,
        "export-chinese": command_export_chinese,
        "refresh-chinese": command_refresh_chinese,
        "resolve-conflicts": command_resolve_conflicts,
        "confirm-chinese": command_confirm_chinese,
        "append-translations": command_append_translations,
        "confirm-translations": command_confirm_translations,
        "prepare-assets": command_prepare_assets,
        "append-assets": command_append_assets,
        "confirm-assets": command_confirm_assets,
        "render": command_render,
        "render-source-chinese": command_render_source_chinese,
        "confirm-source-chinese-layout": command_confirm_source_chinese_layout,
        "confirm-layout": command_confirm_layout,
        "impose-ab": command_impose_ab,
        "layout-small-format": command_layout_small_format,
        "confirm-small-format": command_confirm_small_format,
        "impose-five-fold": command_impose_five_fold,
        "confirm-five-fold": command_confirm_five_fold,
        "confirm-ab": command_confirm_ab,
        "split-a-b": command_split_a_b,
        "confirm-a-b": command_confirm_a_b,
        "status": command_status,
    }
    try:
        project_value = getattr(args, "project", None)
        if project_value:
            with project_lock(project_path(project_value)):
                commands[args.command](args)
        else:
            commands[args.command](args)
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "type": type(exc).__name__}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
