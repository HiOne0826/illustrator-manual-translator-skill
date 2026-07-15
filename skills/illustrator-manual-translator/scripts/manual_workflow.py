#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterable


SCHEMA = "illustrator-manual-project/1.0"
WORKBOOK_NAME = "说明书内容确认.xlsx"
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
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def project_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def state_path(project: Path) -> Path:
    return project / "work" / "project.json"


def load_state(project: Path) -> dict[str, Any]:
    filename = state_path(project)
    if not filename.is_file():
        raise WorkflowError(f"项目尚未初始化：{filename}")
    state = read_json(filename)
    if state.get("schema") != SCHEMA:
        raise WorkflowError("不支持的项目状态版本")
    return state


def save_state(project: Path, state: dict[str, Any]) -> None:
    state["updatedAt"] = now()
    write_json(state_path(project), state)


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


def load_layout_rules(path: Path | None) -> dict[str, Any]:
    return read_json(path) if path else {"schemaVersion": "layout-rules/0.1", "pages": []}


def candidate_fields(metadata: dict[str, Any], layout_rules: dict[str, Any]) -> list[dict[str, Any]]:
    frames = {int(item["index"]): item for item in metadata.get("textFrames", []) if "index" in item}
    bindings: dict[int, dict[str, str]] = {}
    for page in layout_rules.get("pages", []):
        for region in page.get("regions", []):
            behavior = region.get("behavior") or {}
            if behavior.get("translateText") is False or region.get("role") == "fixed-page-number":
                continue
            if behavior.get("translateText") is not True:
                continue
            for ref in region.get("objectRefs", []):
                if ref.get("type") == "textFrame":
                    bindings.setdefault(int(ref["value"]), {"regionId": region.get("id", ""), "role": region.get("role", "")})
    if not bindings:
        bindings = {index: {"regionId": "", "role": "text-frame"} for index in frames}
    rows: list[dict[str, Any]] = []
    for index, binding in bindings.items():
        frame = frames.get(index)
        if not frame or not str(frame.get("contents") or "").strip():
            continue
        rows.append({
            "fieldId": f"textframe_{index}",
            "objectType": "text",
            "objectIndex": index,
            "regionId": binding["regionId"],
            "role": binding["role"],
            "templateText": str(frame.get("contents") or ""),
            "fontName": frame.get("fontName"),
            "fontSize": frame.get("fontSize"),
            "bounds": frame.get("geometricBounds"),
            "required": any(token in str(binding["role"]).lower() for token in ("safety", "operation", "specification", "product-title")),
        })
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
        })
    return sorted(rows, key=lambda item: (item["objectType"] == "layoutText", int(item["objectIndex"])))


def validate_template_mapping(metadata: dict[str, Any], layout_rules: dict[str, Any], template_hash: str) -> None:
    metadata_hash = str((metadata.get("source") or {}).get("sha256") or "")
    if metadata_hash and metadata_hash != template_hash:
        raise WorkflowError(f"模板元数据与 AI 文件哈希不一致：{metadata_hash} != {template_hash}")
    frame_indexes = {int(item["index"]) for item in metadata.get("textFrames", []) if "index" in item}
    missing: list[int] = []
    for page in layout_rules.get("pages", []):
        for region in page.get("regions", []):
            if (region.get("behavior") or {}).get("translateText") is not True:
                continue
            for ref in region.get("objectRefs", []):
                if ref.get("type") == "textFrame" and int(ref["value"]) not in frame_indexes:
                    missing.append(int(ref["value"]))
    if missing:
        raise WorkflowError(f"排版规则引用了模板中不存在的 TextFrame：{sorted(set(missing))[:10]}")


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
    layout_source = project_path(args.layout_rules) if args.layout_rules else None
    layout_rules = load_layout_rules(layout_source)
    validate_template_mapping(metadata, layout_rules, sha256(copied_template))
    layout_path = project / "work" / "layout-rules.json"
    write_json(layout_path, layout_rules)
    fields = candidate_fields(metadata, layout_rules)
    content_input = {
        "instructions": "根据规格书证据和 Illustrator 模板文字，为每个 fieldId 输出 fieldName、sourceEvidence、aiChinese、required、protectedTokens。不得补充规格书中没有的事实；不适用字段可将 aiChinese 留空。",
        "canonicalProduct": canonical,
        "templateFields": fields,
    }
    content_input_path = project / "work" / "content-generation-input.json"
    write_json(content_input_path, content_input)
    languages = [item.strip() for item in args.languages.split(",") if item.strip()]
    if not languages:
        raise WorkflowError("至少需要一个目标语种")
    state = {
        "schema": SCHEMA,
        "stage": "needs_chinese_generation",
        "createdAt": now(),
        "sources": [{"path": relative(project, item), "sha256": sha256(item), "name": item.name} for item in copied_sources],
        "template": {
            "path": relative(project, copied_template),
            "sha256": sha256(copied_template),
            "metadataPath": relative(project, metadata_path),
            "metadataSha256": sha256(metadata_path),
            "layoutRulesPath": relative(project, layout_path),
            "layoutRulesSha256": sha256(layout_path),
        },
        "languages": languages,
        "fields": fields,
        "contentInputPath": relative(project, content_input_path),
        "workbookPath": f"review/{WORKBOOK_NAME}",
    }
    save_state(project, state)
    print(json.dumps({"project": str(project), "stage": state["stage"], "next": "让 Skill 根据 content-generation-input.json 生成中文 rows JSON，然后运行 export-chinese", "contentInput": str(content_input_path)}, ensure_ascii=False, indent=2))


def validate_content_rows(state: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expected = {item["fieldId"]: item for item in state["fields"]}
    seen: set[str] = set()
    result = []
    for row in rows:
        field_id = str(row.get("fieldId") or "")
        if field_id not in expected or field_id in seen:
            raise WorkflowError(f"中文内容包含未知或重复字段：{field_id}")
        seen.add(field_id)
        item = expected[field_id]
        result.append({
            **item,
            "fieldName": str(row.get("fieldName") or item.get("regionId") or field_id),
            "sourceEvidence": str(row.get("sourceEvidence") or ""),
            "aiChinese": str(row.get("aiChinese") or ""),
            "required": bool(item.get("required", False) or row.get("required", False)),
            "protectedTokens": list(dict.fromkeys([
                *[str(token) for token in row.get("protectedTokens") or [] if str(token).strip()],
                *automatic_protected_tokens(str(row.get("aiChinese") or "")),
                *automatic_protected_tokens(str(row.get("sourceEvidence") or "")),
            ])),
        })
    missing = sorted(set(expected) - seen)
    if missing:
        raise WorkflowError(f"中文内容缺少模板字段：{', '.join(missing[:10])}")
    return result


def command_export_chinese(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "needs_chinese_generation")
    ensure_input_hashes(project, state)
    payload = read_json(project_path(args.content_json))
    rows = validate_content_rows(state, payload.get("rows", payload))
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


def assert_readonly(actual: dict[str, Any], expected: dict[str, Any], keys: Iterable[str], excel_row: int) -> None:
    for key in keys:
        if str(actual.get(key) or "") != str(expected.get(key) or ""):
            raise WorkflowError(f"Excel 第 {excel_row} 行误改了只读列：{key}")


def assert_protected_tokens(text: str, tokens: Iterable[str], excel_row: int) -> None:
    missing = [token for token in tokens if token and token not in text]
    if missing:
        raise WorkflowError(f"Excel 第 {excel_row} 行缺少受保护内容：{', '.join(missing[:8])}")


def command_confirm_chinese(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "waiting_chinese_confirmation")
    ensure_input_hashes(project, state)
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
        confirmed.append({
            **item,
            "finalChinese": final_text,
            "protectedTokens": automatic_protected_tokens(final_text),
            "excelRow": int(row["excelRow"]),
        })
    output = project / "work" / "confirmed-chinese.json"
    write_json(output, {"confirmedAt": now(), "rows": confirmed})
    state["fields"] = confirmed
    state["stage"] = "needs_translation_generation"
    state["chineseConfirmedAt"] = now()
    state["confirmedChinesePath"] = relative(project, output)
    save_state(project, state)
    translation_input = project / "work" / "translation-generation-input.json"
    write_json(translation_input, {
        "instructions": "仅根据 finalChinese 翻译。为每个 fieldId 和目标语种输出 aiTranslation，不得修改型号、品牌、数字和单位；空中文保持空。",
        "languages": state["languages"],
        "rows": [{"fieldId": item["fieldId"], "fieldName": item["fieldName"], "finalChinese": item["finalChinese"], "protectedTokens": item.get("protectedTokens") or []} for item in confirmed],
    })
    print(json.dumps({"stage": state["stage"], "next": "让 Skill 根据 translation-generation-input.json 生成翻译 rows JSON，然后运行 append-translations", "translationInput": str(translation_input)}, ensure_ascii=False, indent=2))


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
            state.pop("translations", None)
            state.pop("translationConfirmedAt", None)
            state.pop("confirmedTranslationsPath", None)
            save_state(project, state)
        raise


def command_append_translations(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "needs_translation_generation")
    ensure_input_hashes(project, state)
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
    patterns = [r"\b[A-Za-z]{1,12}-?\d[A-Za-z0-9-]*\b", r"\b\d+(?:\.\d+)?(?:\s?(?:mm|cm|m|kg|g|lb|lbs|V|Hz|W|A))?\b"]
    result: list[str] = []
    for pattern in patterns:
        for token in re.findall(pattern, text, flags=re.IGNORECASE):
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
        if final_text:
            field = next(candidate for candidate in state["fields"] if candidate["fieldId"] == item["fieldId"])
            tokens = list(dict.fromkeys([*(field.get("protectedTokens") or []), *automatic_protected_tokens(item["confirmedChinese"])]))
            assert_protected_tokens(final_text, tokens, int(row["excelRow"]))
        confirmed.append({**item, "finalTranslation": final_text, "excelRow": int(row["excelRow"])})
    output = project / "work" / "confirmed-translations.json"
    write_json(output, {"confirmedAt": now(), "rows": confirmed})
    state["translations"] = confirmed
    state["stage"] = "ready_to_render"
    state["translationConfirmedAt"] = now()
    state["confirmedTranslationsPath"] = relative(project, output)
    save_state(project, state)
    print(json.dumps({"stage": state["stage"], "next": "运行 render 生成各语种 AI/PDF 和版式预览"}, ensure_ascii=False, indent=2))


def render_items(state: dict[str, Any], language: str) -> list[dict[str, Any]]:
    translations = {item["fieldId"]: item for item in state["translations"] if item["language"] == language}
    items = []
    for field in state["fields"]:
        translation = translations[field["fieldId"]]
        target = translation.get("finalTranslation") or ""
        items.append({
            "index": field["objectIndex"],
            "objectType": field["objectType"],
            "targetText": target,
            "remove": not bool(target),
            "expectedSourceText": field["templateText"],
            "bounds": field.get("bounds"),
            "slotId": field["fieldId"],
            "fontName": field.get("fontName"),
            "fontSize": field.get("fontSize"),
        })
    return items


def command_render(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "ready_to_render", "blocked")
    ensure_input_hashes(project, state)
    _, illustrator_worker = repo_imports()
    source = project / state["template"]["path"]
    layout_rules = read_json(project / state["template"]["layoutRulesPath"])
    results = []
    blocking = []
    for language in state["languages"]:
        output_dir = project / "preview" / language
        job = {
            "id": f"manual-{language}",
            "source": str(source),
            "sourceHash": state["template"]["sha256"],
            "output_dir": str(output_dir),
            "output_name": f"manual-{language}",
            "formats": ["ai", "pdf", "preview"],
            "overwrite": True,
            "items": render_items(state, language),
            "layoutRules": layout_rules,
        }
        job_path = project / "work" / f"render-job-{language}.json"
        write_json(job_path, job)
        result = illustrator_worker.render_job(job, execute=not args.no_execute)
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
    output = project / "work" / "render-results.json"
    write_json(output, {"results": results, "blocking": blocking})
    state["renderResultsPath"] = relative(project, output)
    if args.no_execute:
        state["stage"] = "ready_to_render"
    else:
        state["stage"] = "blocked" if blocking else "waiting_layout_confirmation"
    state["blockingIssues"] = blocking
    save_state(project, state)
    if args.no_execute:
        message = "只生成了 Illustrator JSX；尚未生成可确认的 AI/PDF，请去掉 --no-execute 后重新运行"
    else:
        message = "请查看各语种 PDF；无问题后明确回复“版式确认”" if not blocking else "存在阻塞问题，修复后重新 render"
    print(json.dumps({"stage": state["stage"], "preview": str(project / "preview"), "blockingIssues": blocking, "message": message}, ensure_ascii=False, indent=2))


def command_confirm_layout(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    require_stage(state, "waiting_layout_confirmation")
    ensure_input_hashes(project, state)
    render_results = read_json(project / state["renderResultsPath"])["results"]
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
            if source.is_file():
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
        if not qa.is_file():
            raise WorkflowError(f"{language} 缺少排版校对报告")
        if not qa.is_relative_to(preview_root):
            raise WorkflowError(f"{language} 的排版校对报告不在受控 preview 目录")
        if not artifact_hashes.get("layout_qa"):
            raise WorkflowError(f"{language} 的排版校对报告缺少生成时哈希")
        if sha256(qa) != artifact_hashes["layout_qa"]:
            raise WorkflowError(f"{language} 的排版校对报告在生成后发生变化")
        target = directory / qa.name
        shutil.copy2(qa, target)
        delivered.append({"language": language, "type": "qa", "path": relative(project, target), "sha256": sha256(target)})
        page_artifacts = [item for item in result.get("artifacts") or [] if item.get("type") == "page_png"]
        if not page_artifacts:
            raise WorkflowError(f"{language} 缺少逐页 PDF 校对图")
        for artifact in page_artifacts:
            source = Path(artifact.get("path") or "").resolve()
            if not source.is_file():
                raise WorkflowError(f"{language} 缺少逐页校对图文件：{source.name}")
            if not source.is_relative_to(preview_root):
                raise WorkflowError(f"{language} 的逐页校对图不在受控 preview 目录")
            if not artifact.get("sha256") or sha256(source) != artifact["sha256"]:
                raise WorkflowError(f"{language} 的逐页校对图在生成后发生变化")
            target = directory / source.name
            shutil.copy2(source, target)
            delivered.append({"language": language, "type": "page_png", "path": relative(project, target), "sha256": sha256(target)})
    manifest = project / "delivery" / "delivery-manifest.json"
    write_json(manifest, {"projectSchema": SCHEMA, "deliveredAt": now(), "files": delivered})
    state["stage"] = "delivered"
    state["deliveredAt"] = now()
    state["deliveryManifestPath"] = relative(project, manifest)
    save_state(project, state)
    print(json.dumps({"stage": state["stage"], "delivery": str(project / "delivery"), "manifest": str(manifest)}, ensure_ascii=False, indent=2))


def command_status(args: argparse.Namespace) -> None:
    project = project_path(args.project)
    state = load_state(project)
    messages = {
        "needs_chinese_generation": "生成中文内容 JSON 后运行 export-chinese",
        "waiting_chinese_confirmation": "等待用户修改 Excel 并明确确认中文内容",
        "needs_translation_generation": "生成多语种翻译 JSON 后运行 append-translations",
        "waiting_translation_confirmation": "等待用户修改 Excel 并明确确认翻译内容",
        "ready_to_render": "运行 render",
        "waiting_layout_confirmation": "等待用户查看预览 PDF 并明确确认版式",
        "blocked": "处理阻塞问题后重新 render",
        "delivered": "交付完成",
    }
    print(json.dumps({"project": str(project), "stage": state["stage"], "next": messages.get(state["stage"], "未知"), "workbook": str(project / state.get("workbookPath", "")), "languages": state.get("languages", []), "blockingIssues": state.get("blockingIssues", [])}, ensure_ascii=False, indent=2))


def command_doctor(args: argparse.Namespace) -> None:
    extract_sources, illustrator_worker = repo_imports()
    del extract_sources
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        raise WorkflowError("缺少 pdftoppm；无法生成正式逐页 PDF 校对图")
    with TemporaryDirectory() as temp:
        node, _, module = artifact_runtime(Path(temp))
    report = illustrator_worker.doctor_report(probe=False)
    print(json.dumps({
        "ok": True,
        "workflowSchema": SCHEMA,
        "node": node,
        "artifactToolModule": str(module),
        "pdftoppm": pdftoppm,
        "illustratorWorker": report,
    }, ensure_ascii=False, indent=2))


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
    export = sub.add_parser("export-chinese", help="根据 Skill 生成的中文 JSON 创建确认 Excel")
    export.add_argument("--project", required=True)
    export.add_argument("--content-json", required=True)
    confirm = sub.add_parser("confirm-chinese", help="在用户整表确认后回读最终中文")
    confirm.add_argument("--project", required=True)
    append = sub.add_parser("append-translations", help="向同一 Excel 追加多语种翻译 Sheet")
    append.add_argument("--project", required=True)
    append.add_argument("--translations-json", required=True)
    confirm_tr = sub.add_parser("confirm-translations", help="在用户整表确认后回读最终译文")
    confirm_tr.add_argument("--project", required=True)
    render = sub.add_parser("render", help="调用 Illustrator 生成 AI/PDF/预览并执行 QA")
    render.add_argument("--project", required=True)
    render.add_argument("--no-execute", action="store_true")
    layout = sub.add_parser("confirm-layout", help="在用户确认预览 PDF 后形成正式交付")
    layout.add_argument("--project", required=True)
    status = sub.add_parser("status", help="显示当前阶段和下一步")
    status.add_argument("--project", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    commands = {
        "doctor": command_doctor,
        "init": command_init,
        "export-chinese": command_export_chinese,
        "confirm-chinese": command_confirm_chinese,
        "append-translations": command_append_translations,
        "confirm-translations": command_confirm_translations,
        "render": command_render,
        "confirm-layout": command_confirm_layout,
        "status": command_status,
    }
    try:
        commands[args.command](args)
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "type": type(exc).__name__}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
