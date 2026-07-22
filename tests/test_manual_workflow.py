from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "skills" / "illustrator-manual-translator" / "scripts" / "manual_workflow.py"
SPEC = importlib.util.spec_from_file_location("release_manual_workflow", MODULE_PATH)
assert SPEC and SPEC.loader
workflow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workflow)


class TemplateDefaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = {"fields": [{
            "fieldId": "textframe_37",
            "regionId": "cover-contact",
            "role": "fixed-contact-info",
            "templateText": "Manufacturer: Shanghai Xunyuan Industrial Co., Ltd.",
            "contentSource": "template-copy",
            "required": False,
        }]}

    def test_template_default_is_normal_confirmed_content(self) -> None:
        rows = workflow.validate_content_rows(self.state, [{
            "fieldId": "textframe_37",
            "fieldName": "制造商",
            "sourceEvidence": "模板默认：Manufacturer: Shanghai Xunyuan Industrial Co., Ltd.",
            "aiChinese": "制造商：Shanghai Xunyuan Industrial Co., Ltd.",
            "optimizationNote": "规格书未提供，沿用并本地化模板默认内容",
            "contentOrigin": "template-default",
        }], require_optimization=True)
        self.assertEqual(rows[0]["fieldName"], "制造商")
        self.assertEqual(rows[0]["contentOrigin"], "template-default")
        self.assertEqual(rows[0]["finalChinese"], rows[0]["aiChinese"])

    def test_missing_spec_content_cannot_be_exported_as_blank(self) -> None:
        with self.assertRaisesRegex(workflow.WorkflowError, "必须沿用该默认内容"):
            workflow.validate_content_rows(self.state, [{
                "fieldId": "textframe_37",
                "fieldName": "制造商",
                "sourceEvidence": "",
                "aiChinese": "",
                "optimizationNote": "",
                "contentOrigin": "template-default",
            }], require_optimization=True)

    def test_template_default_must_quote_the_exact_default(self) -> None:
        with self.assertRaisesRegex(workflow.WorkflowError, "逐字引用 templateText"):
            workflow.validate_content_rows(self.state, [{
                "fieldId": "textframe_37",
                "fieldName": "制造商",
                "sourceEvidence": "规格书未提供",
                "aiChinese": "制造商：Shanghai Xunyuan Industrial Co., Ltd.",
                "optimizationNote": "规格书未提供，沿用并本地化模板默认内容",
                "contentOrigin": "template-default",
            }], require_optimization=True)

    def test_product_evidence_field_can_fall_back_to_template_default(self) -> None:
        state = {"fields": [{
            "fieldId": "textframe_50",
            "regionId": "operation",
            "role": "body",
            "templateText": "Keep the power cord away from heat.",
            "contentSource": "product-evidence",
            "required": False,
        }]}
        rows = workflow.validate_content_rows(state, [{
            "fieldId": "textframe_50",
            "fieldName": "电源线安全",
            "sourceEvidence": "模板默认：Keep the power cord away from heat.",
            "aiChinese": "请让电源线远离热源。",
            "optimizationNote": "规格书未提供，沿用并本地化模板默认内容",
            "contentOrigin": "template-default",
        }], require_optimization=True)
        self.assertEqual(rows[0]["aiChinese"], "请让电源线远离热源。")

    def test_candidate_field_uses_general_fallback_without_legal_name_hint(self) -> None:
        metadata = {"textFrames": [{
            "index": 37,
            "contents": "Manufacturer: Shanghai Xunyuan Industrial Co., Ltd.",
            "fontName": "ArialMT",
            "fontSize": 8,
            "geometricBounds": [0, 0, 100, 20],
        }]}
        rules = {"pages": [{"regions": [{
            "id": "cover-contact",
            "role": "fixed-contact-info",
            "behavior": {"translateText": True},
            "objectRefs": [{"type": "textFrame", "value": 37}],
        }]}]}
        field = workflow.candidate_fields(metadata, rules)[0]
        self.assertEqual(field["fallbackPolicy"], "template-default-when-spec-missing")
        self.assertNotIn("fieldNameHint", field)


class ImpositionWorkflowGateTests(unittest.TestCase):
    def test_delivery_package_remains_the_default(self) -> None:
        args = workflow.build_parser().parse_args(["confirm-a-b", "--project", "/tmp/example"])
        self.assertFalse(args.no_delivery_package)

    def test_five_fold_commands_are_explicit_alternative_print_path(self) -> None:
        args = workflow.build_parser().parse_args([
            "impose-five-fold", "--project", "/tmp/example", "--plan", "/tmp/plan.json", "--no-execute",
        ])
        self.assertEqual(args.command, "impose-five-fold")
        self.assertEqual(args.plan, "/tmp/plan.json")
        self.assertTrue(args.no_execute)
        confirm = workflow.build_parser().parse_args(["confirm-five-fold", "--project", "/tmp/example"])
        self.assertEqual(confirm.command, "confirm-five-fold")

    def test_five_fold_runtime_is_bundled_and_versioned(self) -> None:
        module = workflow.folded_leaflet_import()
        self.assertEqual(module.SCHEMA, "illustrator-folded-leaflet/1.0")
        self.assertEqual(workflow.FOLDED_LEAFLET_QA_CONTRACT_VERSION, module.QA_CONTRACT_VERSION)
        self.assertEqual(workflow.current_folded_leaflet_runtime_sha256(), workflow.sha256(Path(module.__file__)))

    def test_small_format_commands_use_variable_page_layout_path(self) -> None:
        args = workflow.build_parser().parse_args([
            "layout-small-format", "--project", "/tmp/example", "--no-execute",
        ])
        self.assertEqual(args.command, "layout-small-format")
        self.assertTrue(args.no_execute)
        confirm = workflow.build_parser().parse_args(["confirm-small-format", "--project", "/tmp/example"])
        self.assertEqual(confirm.command, "confirm-small-format")

    def test_small_format_runtime_is_bundled_and_versioned(self) -> None:
        module = workflow.small_format_import()
        self.assertEqual(module.SCHEMA, "illustrator-small-format/2.0")
        self.assertEqual(workflow.SMALL_FORMAT_QA_CONTRACT_VERSION, module.QA_CONTRACT_VERSION)
        self.assertEqual(workflow.current_small_format_runtime_sha256(), workflow.sha256(Path(module.__file__)))

    def test_bundled_product_slots_keep_template_when_empty(self) -> None:
        profile = json.loads((ROOT / "skills/illustrator-manual-translator/assets/template-profiles/aeolus-ft802led/layout-rules.v1.json").read_text(encoding="utf-8"))
        product_slots = [item for item in profile["visualSlots"] if item["role"] not in {"brand-logo"}]
        self.assertTrue(product_slots)
        self.assertTrue(all(item.get("emptyBehavior") == "keep_template" for item in product_slots))

    def test_empty_keep_template_visual_does_not_relabel_old_template_image(self) -> None:
        state = {
            "assetSelections": [{"slotId": "cover.gallery.4", "finalAssetId": ""}],
            "visualSlots": [{
                "id": "cover.gallery.4", "emptyBehavior": "keep_template",
                "labelTextFrame": {"index": 62, "sourceText": "FT-833E", "targetFieldId": "model"},
            }],
        }
        self.assertEqual(workflow.render_visual_label_items(state, {"model": "FT-822M"}, set()), [])

    def test_selected_visual_relabels_to_confirmed_model(self) -> None:
        state = {
            "assetSelections": [{"slotId": "cover.gallery.4", "finalAssetId": "asset-1"}],
            "visualSlots": [{
                "id": "cover.gallery.4", "emptyBehavior": "keep_template",
                "labelTextFrame": {"index": 62, "sourceText": "FT-833E", "targetFieldId": "model"},
            }],
        }
        [item] = workflow.render_visual_label_items(state, {"model": "FT-822M"}, set())
        self.assertEqual(item["targetText"], "FT-822M")

    def test_unresolved_high_conflict_blocks_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "work").mkdir()
            canonical = project / "work/canonical.json"
            canonical.write_text(json.dumps({"conflicts": [{"id": "conflict-001", "severity": "high"}]}), encoding="utf-8")
            state = {"canonicalProductPath": "work/canonical.json"}
            with self.assertRaisesRegex(workflow.WorkflowError, "resolve-conflicts"):
                workflow.require_resolved_conflicts(project, state)

    def test_review_meta_text_is_rejected(self) -> None:
        with self.assertRaisesRegex(workflow.WorkflowError, "审核提示"):
            workflow.assert_publishable_text("净重：10 kg，请确认", 12)

    def test_translation_input_excludes_evidence_only_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            state = {"languages": ["en-US"], "fields": [{
                "fieldId": "textframe_1", "fieldName": "电话", "finalChinese": "电话：+49 172 8844780",
                "protectedTokens": ["+49 172 8844780", "paragraph-0198", "0198"],
            }]}
            path = workflow.create_translation_input(project, state)
            tokens = json.loads(path.read_text(encoding="utf-8"))["rows"][0]["protectedTokens"]
            self.assertIn("+49 172 8844780", tokens)
            self.assertNotIn("paragraph-0198", tokens)
            self.assertNotIn("0198", tokens)

    def test_confirm_layout_advances_to_ab_instead_of_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "work").mkdir()
            (project / "delivery").mkdir()
            (project / "work" / "render-results.json").write_text('{"results": []}', encoding="utf-8")
            (project / "delivery" / "source-chinese-manifest.json").write_text('{"files": []}', encoding="utf-8")
            state = {
                "stage": "waiting_layout_confirmation",
                "renderResultsPath": "work/render-results.json",
                "sourceChineseManifestPath": "delivery/source-chinese-manifest.json",
            }
            captured = {}

            def capture_state(_project, value):
                captured.update(value)

            with patch.object(workflow, "load_state", return_value=state), \
                    patch.object(workflow, "ensure_input_hashes"), \
                    patch.object(workflow, "require_source_chinese_confirmation"), \
                    patch.object(workflow, "copy_confirmed_results", return_value=[]), \
                    patch.object(workflow, "save_state", side_effect=capture_state):
                workflow.command_confirm_layout(SimpleNamespace(project=str(project)))
            self.assertEqual(captured["stage"], "ready_to_impose_ab")
            self.assertNotIn("deliveredAt", captured)
            manifest = json.loads((project / captured["electronicManifestPath"]).read_text(encoding="utf-8"))
            self.assertIn("confirmedAt", manifest)

    def test_all_languages_include_chinese_first(self) -> None:
        self.assertEqual(workflow.expected_languages({"languages": ["de-DE", "fr-FR", "de-DE"]}), ["zh-CN", "de-DE", "fr-FR"])

    def test_refresh_invalidates_all_imposition_confirmations(self) -> None:
        state = {key: True for key in (
            "electronicConfirmedAt", "electronicManifestPath", "abImpositionResultsPath",
            "abConfirmedAt", "confirmedAbManifestPath", "splitResultsPath",
            "aBConfirmedAt", "confirmedABSplitManifestPath", "impositionManifestPath",
            "foldedLeafletResultsPath", "foldedLeafletConfirmedAt", "foldedLeafletManifestPath", "printVariant",
            "deliveryPackageGenerated", "deliveryManifestPath", "deliveredAt",
        )}
        workflow.invalidate_downstream_state(state)
        self.assertEqual(state, {})

    def test_confirm_a_b_can_finish_without_delivery_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "work").mkdir()
            artifact_types = ("ai", "pdf", "imposition_qa", "page_png")

            def artifact(path: Path, kind: str, variant: str = "") -> dict[str, object]:
                path.parent.mkdir(parents=True, exist_ok=True)
                if kind == "imposition_qa":
                    qa = {
                        "schema": "illustrator-imposition/1.0", "qaContractVersion": 2,
                        "variant": variant, "artboardCount": 1, "editableObjectsPreserved": True,
                        "bleedPt": 0, "expectedTopLevelItems": 1, "outputTopLevelItems": 1,
                    }
                    path.write_text(json.dumps(qa), encoding="utf-8")
                else:
                    path.write_text(f"{kind}:{path.name}", encoding="utf-8")
                return {"type": kind, "path": str(path), "sha256": workflow.sha256(path)}

            ab_artifact = artifact(project / "preview/zh-CN/imposition/AB/manual.ai", "ai")
            split_result = {"language": "zh-CN", "status": "succeeded", "qualityIssues": [], "variants": {}}
            for variant in ("A", "B"):
                root = project / "preview" / "zh-CN" / "imposition" / variant
                split_result["variants"][variant] = {
                    "qaContractVersion": 2,
                    "runtimeSha256": workflow.current_imposition_runtime_sha256(),
                    "artifacts": [artifact(root / f"artifact-{kind}", kind, variant) for kind in artifact_types],
                }
            (project / "work/split.json").write_text(json.dumps({"results": [split_result]}), encoding="utf-8")
            (project / "work/confirmed-ab.json").write_text(json.dumps({
                "results": [{"language": "zh-CN", "artifacts": [ab_artifact]}]
            }), encoding="utf-8")
            state = {
                "stage": "waiting_a_b_confirmation", "languages": [],
                "splitResultsPath": "work/split.json", "confirmedAbManifestPath": "work/confirmed-ab.json",
            }
            captured = {}

            def capture_state(_project, value):
                captured.update(value)

            with patch.object(workflow, "load_state", return_value=state), \
                    patch.object(workflow, "ensure_input_hashes"), \
                    patch.object(workflow, "save_state", side_effect=capture_state):
                workflow.command_confirm_a_b(SimpleNamespace(project=str(project), no_delivery_package=True))
            self.assertEqual(captured["stage"], "completed_without_delivery")
            self.assertFalse(captured["deliveryPackageGenerated"])
            self.assertFalse((project / "delivery").exists())
            manifest = json.loads((project / captured["confirmedABSplitManifestPath"]).read_text(encoding="utf-8"))
            self.assertFalse(manifest["deliveryPackageGenerated"])
            self.assertEqual(len(manifest["files"]), 9)


class ProductionBaselineRegressionTests(unittest.TestCase):
    def test_skill_bundles_current_extraction_and_illustrator_runtime(self) -> None:
        scripts = ROOT / "skills" / "illustrator-manual-translator" / "scripts"
        self.assertTrue((scripts / "extract_sources.py").is_file())
        self.assertTrue((scripts / "illustrator_worker.py").is_file())

    def test_candidate_fields_respects_explicit_translation_bindings(self) -> None:
        metadata = {"textFrames": [
            {"index": 1, "contents": "Translate me", "fontSize": 9},
            {"index": 2, "contents": "Page 1", "fontSize": 8},
        ]}
        rules = {"pages": [{"regions": [
            {"id": "body", "role": "body", "objectRefs": [{"type": "textFrame", "value": 1}], "behavior": {"translateText": True}},
            {"id": "page", "role": "fixed-page-number", "objectRefs": [{"type": "textFrame", "value": 2}], "behavior": {"translateText": False}},
        ]}]}
        fields = workflow.candidate_fields(metadata, rules)
        self.assertEqual([item["fieldId"] for item in fields], ["textframe_1"])

    def test_template_metadata_hash_must_match_ai(self) -> None:
        metadata = {"source": {"sha256": "old-hash"}, "textFrames": []}
        with self.assertRaisesRegex(workflow.WorkflowError, "模板元数据与 AI 文件哈希不一致"):
            workflow.validate_template_mapping(metadata, {"pages": []}, "new-hash")

    def test_mapping_change_invalidates_previous_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "inputs").mkdir()
            (project / "work").mkdir()
            spec = project / "inputs" / "spec.docx"
            template = project / "inputs" / "manual.ai"
            metadata = project / "work" / "template.json"
            rules = project / "work" / "layout-rules.json"
            spec.write_bytes(b"spec")
            template.write_bytes(b"ai")
            metadata.write_text("{}", encoding="utf-8")
            rules.write_text("{}", encoding="utf-8")
            state = {
                "sources": [{"path": "inputs/spec.docx", "sha256": workflow.sha256(spec)}],
                "template": {
                    "path": "inputs/manual.ai", "sha256": workflow.sha256(template),
                    "metadataPath": "work/template.json", "metadataSha256": workflow.sha256(metadata),
                    "layoutRulesPath": "work/layout-rules.json", "layoutRulesSha256": workflow.sha256(rules),
                },
            }
            rules.write_text('{"changed":true}', encoding="utf-8")
            with self.assertRaisesRegex(workflow.WorkflowError, "排版规则已变化"):
                workflow.ensure_input_hashes(project, state)

    def test_electronic_confirmation_rejects_changed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            preview = project / "preview" / "en-US"
            preview.mkdir(parents=True)
            ai, pdf = preview / "manual.ai", preview / "manual.pdf"
            qa, page = preview / "layout-qa.json", preview / "page-1.png"
            ai.write_bytes(b"ai")
            pdf.write_bytes(b"pdf")
            qa.write_text("{}", encoding="utf-8")
            page.write_bytes(b"png")
            result = {
                "language": "en-US", "outputs": {"ai": str(ai), "pdf": str(pdf)}, "qaReport": str(qa),
                "artifacts": [
                    {"type": "ai", "path": str(ai), "sha256": workflow.sha256(ai)},
                    {"type": "pdf", "path": str(pdf), "sha256": workflow.sha256(pdf)},
                    {"type": "layout_qa", "path": str(qa), "sha256": workflow.sha256(qa)},
                    {"type": "page_png", "path": str(page), "sha256": workflow.sha256(page)},
                ],
            }
            pdf.write_bytes(b"tampered")
            with self.assertRaisesRegex(workflow.WorkflowError, "输出在校对后发生变化"):
                workflow.copy_confirmed_results(project, [result])


if __name__ == "__main__":
    unittest.main()
