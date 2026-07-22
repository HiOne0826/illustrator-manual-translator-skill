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
            items.append({"index": item_index, "name": f"item-{item_index}", "typename": "GroupItem"})
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

    def test_dense_half_page_spreads_positions_to_reference_vertical_insets(self):
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
        self.assertTrue(first["verticalFillExpected"])
        self.assertGreater(first["verticalShiftPt"], 0)
        self.assertLess(last["verticalShiftPt"], 0)

    def test_overlapping_title_and_bar_move_as_one_visual_cluster(self):
        shifts, eligible = folded._vertical_distribution(
            [0, 1, 2],
            {
                0: {"bounds": [0, -20, 200, -60]},
                1: {"bounds": [10, -30, 190, -55]},
                2: {"bounds": [10, -500, 190, -550]},
            },
            source_half_rect=[0, 0, 420, -595],
            fit_top=-40,
            target_top=-26,
            panel_height=folded.mm_to_pt(156.22),
            scale=0.5,
            content_top_inset=folded.mm_to_pt(5.8),
            content_bottom_inset=folded.mm_to_pt(3.7),
        )
        self.assertTrue(eligible)
        self.assertAlmostEqual(shifts[0], shifts[1])
        self.assertNotEqual(shifts[0], shifts[2])

    def test_bundled_reference_plan_is_intentionally_blocked_until_print_direction_confirmation(self):
        plan_path = ROOT / "skills/illustrator-manual-translator/assets/folded-leaflet-plans/five-panel-reference.v1.json"
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
        plan = folded.build_plan(
            inventory(),
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

    def test_plan_rejects_duplicate_source_half(self):
        assignments = folded.default_assignments(4)
        assignments[1]["sourceArtboard"] = assignments[0]["sourceArtboard"]
        assignments[1]["sourceSide"] = assignments[0]["sourceSide"]
        with self.assertRaisesRegex(folded.FoldedLeafletError, "assigned more than once"):
            folded.build_plan(inventory(), assignments=assignments)

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
            "oversetTextFrameIndexes": [3],
            "outOfPanelItemIndexes": [7],
        }, folded.normalize_geometry())
        self.assertEqual({item["type"] for item in issues}, {"editability_lost", "text_overflow", "panel_bounds_violation"})

    def test_qa_blocks_regression_to_large_vertical_whitespace(self):
        issues = folded.validate_qa({
            "artboardCount": 2,
            "panelCountPerSide": 5,
            "mediaWidthPt": folded.mm_to_pt(390),
            "mediaHeightPt": folded.mm_to_pt(174.6),
            "editableObjectsPreserved": True,
            "panelContentMetrics": [{
                "targetArtboard": 1, "targetPanel": 2, "verticalFillExpected": True,
                "topBlankPt": folded.mm_to_pt(24), "bottomBlankPt": folded.mm_to_pt(4),
            }],
        }, folded.normalize_geometry())
        self.assertEqual([item["type"] for item in issues], ["excessive_vertical_whitespace"])

    def test_jsx_uses_native_objects_and_creates_print_marks(self):
        plan = folded.build_plan(inventory())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jsx = folded.build_layout_jsx(root / "source.ai", root / "out.ai", root / "out.pdf", root / "qa.json", plan)
        self.assertIn("item.resize", jsx)
        self.assertIn("item.translate", jsx)
        self.assertIn("verticalShiftPt", jsx)
        self.assertIn("panelContentMetrics", jsx)
        self.assertIn("FIVE_FOLD_PRINT_MARKS", jsx)
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
