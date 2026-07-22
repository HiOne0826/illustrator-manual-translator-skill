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

    def test_jsx_uses_native_objects_and_creates_print_marks(self):
        plan = folded.build_plan(inventory())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jsx = folded.build_layout_jsx(root / "source.ai", root / "out.ai", root / "out.pdf", root / "qa.json", plan)
        self.assertIn("item.resize", jsx)
        self.assertIn("item.translate", jsx)
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
