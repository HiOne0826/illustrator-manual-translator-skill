import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "skills" / "illustrator-manual-translator" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
SPEC = importlib.util.spec_from_file_location("illustrator_small_format", SCRIPT_DIR / "illustrator_small_format.py")
small = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(small)


def inventory():
    width = 840.0
    height = 595.0
    boards = [
        {"index": 0, "name": "source-1", "rect": [0, 0, width, -height]},
        {"index": 1, "name": "source-2", "rect": [0, -630, width, -1225]},
    ]
    halves = [
        {"artboardIndex": 0, "side": "left", "itemIndexes": [0], "pageNumbers": []},
        {"artboardIndex": 0, "side": "right", "itemIndexes": [1], "pageNumbers": []},
        {"artboardIndex": 1, "side": "left", "itemIndexes": [2], "pageNumbers": []},
        {"artboardIndex": 1, "side": "right", "itemIndexes": [3, 4, 5, 6, 7, 8], "pageNumbers": []},
    ]
    items = [
        {"index": 0, "typename": "GroupItem", "bounds": [0, -20, 420, -520], "details": {}},
        {"index": 1, "typename": "GroupItem", "bounds": [420, -20, 840, -520], "details": {}},
        {"index": 2, "typename": "GroupItem", "bounds": [0, -650, 420, -680], "details": {}},
        {"index": 3, "typename": "PathItem", "bounds": [420, -650, 840, -690], "details": {"filled": True}},
        {"index": 4, "typename": "TextFrame", "bounds": [450, -660, 650, -685], "details": {}},
        {"index": 5, "typename": "TextFrame", "bounds": [450, -700, 810, -760], "details": {}},
        {"index": 6, "typename": "PathItem", "bounds": [420, -800, 840, -840], "details": {"filled": True}},
        {"index": 7, "typename": "TextFrame", "bounds": [450, -810, 650, -835], "details": {}},
        {"index": 8, "typename": "TextFrame", "bounds": [450, -850, 810, -950], "details": {}},
    ]
    return {
        "artboardCount": 2,
        "artboards": boards,
        "halves": halves,
        "items": items,
        "preservedItemIndexes": [],
    }


class SmallFormatTests(unittest.TestCase):
    def test_page_count_is_content_driven_not_fixed_to_ten(self):
        plan = small.build_plan(inventory())
        self.assertEqual(plan["sourceHalfPageCount"], 4)
        self.assertEqual(plan["contentPageCount"], 3)
        self.assertEqual(plan["pageCount"], 4)
        self.assertEqual(plan["blankPageCount"], 1)
        self.assertEqual(plan["rowCount"], 2)
        self.assertEqual(plan["columnCount"], 2)
        self.assertNotEqual(plan["pageCount"], 10)

    def test_consecutive_sparse_half_can_share_a_small_page(self):
        plan = small.build_plan(inventory())
        merged_pages = [page for page in plan["pages"] if len(page["sources"]) == 2]
        self.assertEqual(len(merged_pages), 1)
        self.assertEqual(merged_pages[0]["sources"], [
            {"sourceArtboard": 0, "sourceSide": "right"},
            {"sourceArtboard": 1, "sourceSide": "left"},
        ])

    def test_sections_from_one_source_half_stay_on_one_page(self):
        plan = small.build_plan(inventory())
        target_pages = {
            move["targetArtboard"]
            for move in plan["movements"]
            if move["itemIndex"] in {3, 4, 5, 6, 7, 8}
        }
        self.assertEqual(len(target_pages), 1)

    def test_layout_uses_two_horizontal_rows_and_template_panel_guides(self):
        plan = small.build_plan(inventory())
        self.assertEqual(plan["artboardCount"], 2)
        self.assertEqual(len(plan["artboards"]), 2)
        self.assertEqual(len(plan["panelRects"]), 4)
        self.assertTrue(plan["pages"][-1]["blank"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jsx = small.build_layout_jsx(root / "source.ai", root / "out.ai", root / "out.pdf", root / "qa.json", plan)
        self.assertIn("SMALL-FORMAT-ROW-1", jsx)
        self.assertIn("SMALL_FORMAT_GUIDES", jsx)
        self.assertIn('pdfOptions.artboardRange="1-2"', jsx)
        self.assertIn('"variant":"SMALL_FORMAT"', jsx)
        self.assertNotIn("FIVE_FOLD_GUIDES", jsx)
        self.assertNotIn("FIVE_FOLD_PRINT_MARKS", jsx)
        self.assertIn("pdfOptions.preserveEditability=true", jsx)

    def test_sheet_geometry_matches_reference_template_margins(self):
        plan = small.build_plan(inventory())
        self.assertAlmostEqual(plan["sheetHeightPt"], small.mm_to_pt(174.6), places=2)
        self.assertAlmostEqual(plan["artboards"][1]["rect"][1] - plan["artboards"][0]["rect"][1], small.mm_to_pt(205.6), places=2)
        first = plan["panelRects"][0]
        self.assertAlmostEqual(first[0], small.mm_to_pt(5), places=2)
        self.assertAlmostEqual(first[1], -small.mm_to_pt(9.19), places=2)
        self.assertEqual(plan["pages"][0]["artboardIndex"], 1)
        self.assertEqual(plan["pages"][-1]["artboardIndex"], 0)

    def test_qa_blocks_overflow_bounds_and_editability_loss(self):
        plan = small.build_plan(inventory())
        issues = small.validate_qa({
            "artboardCount": 2,
            "artboard0TopPt": plan["artboards"][0]["rect"][1],
            "artboard1TopPt": plan["artboards"][1]["rect"][1],
            "artboardTopDeltaPt": plan["artboards"][1]["rect"][1] - plan["artboards"][0]["rect"][1],
            "rowCount": 2,
            "columnCount": plan["columnCount"],
            "pageCount": plan["pageCount"],
            "guideRectangleCount": plan["pageCount"],
            "sheetWidthPt": plan["sheetWidthPt"],
            "sheetHeightPt": plan["sheetHeightPt"],
            "pageWidthPt": small.mm_to_pt(76),
            "pageHeightPt": small.mm_to_pt(156.22),
            "editableObjectsPreserved": False,
            "oversetTextFrameIndexes": [2],
            "outOfPageItemIndexes": [4],
        }, plan)
        self.assertEqual({item["type"] for item in issues}, {"editability_lost", "text_overflow", "page_bounds_violation"})


if __name__ == "__main__":
    unittest.main()
