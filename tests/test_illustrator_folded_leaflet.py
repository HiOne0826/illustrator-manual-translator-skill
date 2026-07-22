import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "skills" / "illustrator-manual-translator" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
SPEC = importlib.util.spec_from_file_location("illustrator_folded_leaflet", SCRIPT_DIR / "illustrator_folded_leaflet.py")
folded = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(folded)


def inventory(artboard_count=4):
    boards = []
    halves = []
    items = []
    item_index = 0
    width = 841.89
    height = 595.276
    for artboard in range(artboard_count):
        top = -artboard * (height + 36)
        boards.append({"index": artboard, "name": f"page-{artboard + 1}", "rect": [0, top, width, top - height]})
        for side in ("left", "right"):
            halves.append({"artboardIndex": artboard, "side": side, "itemIndexes": [item_index], "pageNumbers": []})
            half_left = 0 if side == "left" else width / 2
            items.append({
                "index": item_index, "name": f"item-{item_index}", "typename": "GroupItem",
                "bounds": [half_left + 10, top - 20, half_left + 180, top - 80],
            })
            item_index += 1
    return {
        "artboardCount": artboard_count,
        "artboards": boards,
        "halves": halves,
        "items": items,
        "preservedItemIndexes": [],
    }


class FoldedLeafletPlanTests(unittest.TestCase):
    def test_reference_geometry_is_two_sides_of_five_76mm_panels(self):
        geometry = folded.normalize_geometry()
        self.assertEqual(geometry["panelCount"], 5)
        self.assertAlmostEqual(geometry["panelWidth"], 76.0)
        self.assertAlmostEqual(geometry["panelHeight"], 156.22)
        self.assertAlmostEqual(geometry["contentTopInset"], 5.8)
        self.assertAlmostEqual(geometry["contentBottomInset"], 3.7)
        self.assertAlmostEqual(geometry["artboardGap"], 31.0)

    def test_separate_visual_bands_are_distributed_to_reduce_bottom_whitespace(self):
        payload = inventory(1)
        half = payload["halves"][0]
        half["itemIndexes"] = [0, 2]
        payload["items"] = [
            {"index": 0, "typename": "TextFrame", "bounds": [10, -20, 200, -50]},
            {"index": 1, "typename": "GroupItem", "bounds": [500, -20, 700, -50]},
            {"index": 2, "typename": "TextFrame", "bounds": [10, -500, 200, -560]},
        ]
        payload["halves"][1]["itemIndexes"] = [1]
        plan = folded.build_plan(payload)
        first = next(item for item in plan["movements"] if item["itemIndex"] == 0)
        last = next(item for item in plan["movements"] if item["itemIndex"] == 2)
        self.assertTrue(first["verticalDistributionExpected"])
        self.assertEqual(first["layoutGroupCount"], 2)
        self.assertNotAlmostEqual(first["verticalShiftPt"], last["verticalShiftPt"])

    def test_title_bar_title_and_body_move_as_one_section(self):
        shifts, group_count, distributed = folded._vertical_section_distribution(
            [0, 1, 2, 3, 4, 5, 6],
            {
                0: {"typename": "PathItem", "bounds": [0, -20, 420, -60], "details": {"filled": True}},
                1: {"typename": "TextFrame", "bounds": [20, -30, 200, -55]},
                2: {"typename": "TextFrame", "bounds": [20, -65, 390, -150]},
                3: {"typename": "PathItem", "bounds": [0, -250, 420, -290], "details": {"filled": True}},
                4: {"typename": "TextFrame", "bounds": [20, -260, 200, -285]},
                5: {"typename": "TextFrame", "bounds": [20, -295, 390, -350]},
                6: {"typename": "GroupItem", "bounds": [204, -570, 216, -580]},
            },
            source_half_rect=[0, 0, 420, -595],
            fit_top=-40,
            target_top=-26,
            panel_height=folded.mm_to_pt(156.22),
            scale=0.5,
            content_top_inset=folded.mm_to_pt(5.8),
            content_bottom_inset=folded.mm_to_pt(3.7),
        )
        self.assertEqual(group_count, 3)
        self.assertTrue(distributed)
        self.assertAlmostEqual(shifts[0], shifts[1])
        self.assertAlmostEqual(shifts[0], shifts[2])
        self.assertAlmostEqual(shifts[3], shifts[4])
        self.assertAlmostEqual(shifts[3], shifts[5])
        self.assertNotAlmostEqual(shifts[2], shifts[3])
        self.assertNotAlmostEqual(shifts[5], shifts[6])

    def test_bundled_reference_plan_is_intentionally_blocked_until_print_direction_confirmation(self):
        plan_path = ROOT / "skills/illustrator-manual-translator/assets/folded-leaflet-plans/five-panel-reference.v1.json"
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
        payload_inventory = inventory()
        for half_index, new_index in ((3, 8), (7, 9)):
            half = payload_inventory["halves"][half_index]
            existing = payload_inventory["items"][half["itemIndexes"][0]]
            board = payload_inventory["artboards"][half["artboardIndex"]]
            existing["bounds"][1] = board["rect"][1] - 20
            existing["bounds"][3] = board["rect"][1] - 120
            payload_inventory["items"].append({
                "index": new_index, "name": f"item-{new_index}", "typename": "GroupItem",
                "bounds": [existing["bounds"][0], board["rect"][1] - 400, existing["bounds"][2], board["rect"][1] - 520],
            })
            half["itemIndexes"].append(new_index)
        plan = folded.build_plan(
            payload_inventory,
            geometry=payload["geometry"],
            assignments=payload["assignments"],
            print_profile=payload["printProfile"],
        )
        self.assertEqual(plan["printProfile"]["duplexFlip"], "unconfirmed")

    def test_default_plan_maps_every_a4_half_once_and_adds_two_blanks(self):
        plan = folded.build_plan(inventory())
        self.assertEqual(plan["schema"], folded.PLAN_SCHEMA)
        self.assertEqual(len(plan["panels"]), 10)
        self.assertEqual(sum(1 for item in plan["panels"] if item["blank"]), 2)
        self.assertEqual(len(plan["movements"]), 8)
        self.assertEqual({item["itemIndex"] for item in plan["movements"]}, set(range(8)))
        self.assertAlmostEqual(plan["mediaSizePt"][0], folded.mm_to_pt(390), places=5)
        self.assertAlmostEqual(plan["minimumScale"], folded.mm_to_pt(76) / (841.89 / 2), places=4)
        self.assertEqual(plan["artboardTops"], [-(folded.mm_to_pt(174.6) + folded.mm_to_pt(31)), 0.0])

    def test_source_a4_guides_are_replaced_by_reference_panel_guides(self):
        payload = inventory()
        payload["items"].append({
            "index": 8, "name": "old-center-guide", "typename": "PathItem",
            "details": {"guides": True},
        })
        payload["preservedItemIndexes"] = [8]
        plan = folded.build_plan(payload)
        self.assertEqual(plan["replaceGuideItemIndexes"], [8])
        self.assertEqual(plan["preserveItemIndexes"], [])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jsx = folded.build_layout_jsx(root / "source.ai", root / "out.ai", root / "out.pdf", root / "qa.json", plan)
        self.assertIn("FIVE_FOLD_GUIDES", jsx)
        self.assertIn("item.guides=true", jsx)
        self.assertIn("replaceGuides", jsx)

    def test_plan_rejects_duplicate_source_half(self):
        assignments = folded.default_assignments(4)
        assignments[8]["sourceArtboard"] = assignments[0]["sourceArtboard"]
        assignments[8]["sourceSide"] = assignments[0]["sourceSide"]
        with self.assertRaisesRegex(folded.FoldedLeafletError, "overlap or leave a gap"):
            folded.build_plan(inventory(), assignments=assignments)

    def test_plan_accepts_non_overlapping_semantic_bands_that_cover_a_source_half(self):
        assignments = folded.default_assignments(4)
        assignments[0]["sourceBand"] = [0.0, 0.5]
        assignments[8].update({"sourceArtboard": 0, "sourceSide": "left", "sourceBand": [0.5, 1.0]})
        normalized = folded.normalize_assignments(assignments, 4)
        bands = [item["sourceBand"] for item in normalized if item.get("sourceArtboard") == 0 and item.get("sourceSide") == "left"]
        self.assertEqual(bands, [[0.0, 0.5], [0.5, 1.0]])

    def test_plan_rejects_more_than_ten_source_halves(self):
        with self.assertRaisesRegex(folded.FoldedLeafletError, "only 10 panels"):
            folded.build_plan(inventory(6))

    def test_qa_blocks_overset_and_editability_loss(self):
        issues = folded.validate_qa({
            "artboardCount": 2,
            "panelCountPerSide": 5,
            "mediaWidthPt": folded.mm_to_pt(390),
            "mediaHeightPt": folded.mm_to_pt(174.6),
            "editableObjectsPreserved": False,
            "guideRectangleCount": 10,
            "artboardGapPt": folded.mm_to_pt(31),
            "oversetTextFrameIndexes": [3],
            "outOfPanelItemIndexes": [7],
        }, folded.normalize_geometry())
        self.assertEqual({item["type"] for item in issues}, {"editability_lost", "text_overflow", "panel_bounds_violation"})

    def test_qa_blocks_large_whitespace_before_a_top_aligned_section(self):
        issues = folded.validate_qa({
            "artboardCount": 2,
            "panelCountPerSide": 5,
            "mediaWidthPt": folded.mm_to_pt(390),
            "mediaHeightPt": folded.mm_to_pt(174.6),
            "editableObjectsPreserved": True,
            "guideRectangleCount": 10,
            "artboardGapPt": folded.mm_to_pt(31),
            "panelContentMetrics": [{
                "targetArtboard": 1, "targetPanel": 2, "layoutGroupCount": 1,
                "topBlankPt": folded.mm_to_pt(24), "bottomBlankPt": folded.mm_to_pt(4),
            }],
        }, folded.normalize_geometry())
        self.assertEqual([item["type"] for item in issues], ["excessive_top_whitespace"])

    def test_qa_allows_natural_bottom_whitespace_for_one_short_section(self):
        issues = folded.validate_qa({
            "artboardCount": 2,
            "panelCountPerSide": 5,
            "mediaWidthPt": folded.mm_to_pt(390),
            "mediaHeightPt": folded.mm_to_pt(174.6),
            "editableObjectsPreserved": True,
            "guideRectangleCount": 10,
            "artboardGapPt": folded.mm_to_pt(31),
            "panelContentMetrics": [{
                "targetArtboard": 1, "targetPanel": 2, "layoutGroupCount": 1,
                "verticalDistributionExpected": False,
                "topBlankPt": folded.mm_to_pt(5.8), "bottomBlankPt": folded.mm_to_pt(60),
            }],
        }, folded.normalize_geometry())
        self.assertEqual(issues, [])

    def test_qa_blocks_bottom_whitespace_when_multiple_groups_can_fill_panel(self):
        issues = folded.validate_qa({
            "artboardCount": 2,
            "panelCountPerSide": 5,
            "mediaWidthPt": folded.mm_to_pt(390),
            "mediaHeightPt": folded.mm_to_pt(174.6),
            "editableObjectsPreserved": True,
            "guideRectangleCount": 10,
            "artboardGapPt": folded.mm_to_pt(31),
            "panelContentMetrics": [{
                "targetArtboard": 1, "targetPanel": 2, "layoutGroupCount": 2,
                "verticalDistributionExpected": True,
                "topBlankPt": folded.mm_to_pt(5.8), "bottomBlankPt": folded.mm_to_pt(30),
            }],
        }, folded.normalize_geometry())
        self.assertEqual([item["type"] for item in issues], ["excessive_bottom_whitespace"])

    def test_jsx_uses_native_objects_and_creates_print_marks(self):
        plan = folded.build_plan(inventory())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jsx = folded.build_layout_jsx(root / "source.ai", root / "out.ai", root / "out.pdf", root / "qa.json", plan)
        self.assertIn("item.resize", jsx)
        self.assertIn("item.translate", jsx)
        self.assertIn("verticalShiftPt", jsx)
        self.assertIn("layoutGroupCount", jsx)
        self.assertIn("verticalDistributionExpected", jsx)
        self.assertIn("panelContentMetrics", jsx)
        self.assertIn("FIVE_FOLD_PRINT_MARKS", jsx)
        self.assertIn("FIVE_FOLD_GUIDES", jsx)
        self.assertIn("guideRectangleCount", jsx)
        self.assertIn("pdfOptions.preserveEditability=true", jsx)
        self.assertNotIn("placedItems.add", jsx)
        self.assertNotIn("rasterize", jsx.lower())

    def test_dry_run_writes_only_inspection_jsx(self):
        with tempfile.TemporaryDirectory() as temp:
            result = folded.layout_leaflet_job({
                "sourceAI": Path(temp) / "source.ai",
                "sourcePDF": Path(temp) / "source.pdf",
                "outputDir": Path(temp) / "output",
                "language": "en-US",
            }, execute=False)
            self.assertFalse(result["executed"])
            self.assertTrue(Path(result["inspectJsx"]).is_file())
            self.assertEqual(result["qualityIssues"], [])


if __name__ == "__main__":
    unittest.main()
