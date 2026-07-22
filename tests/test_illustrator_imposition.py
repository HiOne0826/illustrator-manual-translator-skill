from __future__ import annotations

import importlib.util
import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skills" / "illustrator-manual-translator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("release_illustrator_imposition", SCRIPTS / "illustrator_imposition.py")
assert SPEC and SPEC.loader
imposition = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(imposition)


def page_sources(count: int) -> list[dict[str, object]]:
    return [{"artboardIndex": (number + 1) // 2, "side": "left" if number % 2 else "right", "itemIndexes": [number], "pageNumbers": [number]} for number in range(1, count + 1)]


class ImpositionPlanTests(unittest.TestCase):
    def test_16_page_ab_order_matches_short_edge_sample(self) -> None:
        plan = imposition.build_ab_plan(page_sources(14), {
            "front_cover": {"artboardIndex": 0, "side": "right", "itemIndexes": [100]},
            "back_cover": {"artboardIndex": 0, "side": "left", "itemIndexes": [101]},
        })
        pairs = [(item["left"]["key"], item["right"]["key"]) for item in plan["pairs"]]
        self.assertEqual(pairs, [
            ("back_cover", "front_cover"), ("body_1", "body_14"),
            ("body_13", "body_2"), ("body_3", "body_12"),
            ("body_11", "body_4"), ("body_5", "body_10"),
            ("body_9", "body_6"), ("body_7", "body_8"),
        ])
        self.assertEqual(plan["aArtboardIndexes"], [0, 2, 4, 6])
        self.assertEqual(plan["bArtboardIndexes"], [1, 3, 5, 7])

    def test_blank_pages_are_inserted_after_body_before_back_cover(self) -> None:
        plan = imposition.build_ab_plan(page_sources(3), {
            "front_cover": {"artboardIndex": 0, "side": "right", "itemIndexes": []},
            "back_cover": {"artboardIndex": 0, "side": "left", "itemIndexes": []},
        })
        keys = [side["key"] for pair in plan["pairs"] for side in (pair["left"], pair["right"])]
        self.assertEqual(plan["blankPageCount"], 3)
        self.assertEqual(set(key for key in keys if key.startswith("blank_")), {"blank_1", "blank_2", "blank_3"})
        self.assertEqual(plan["blankPlacement"], "after-last-body-before-back-cover")

    def test_customer_six_physical_page_example(self) -> None:
        plan = imposition.build_ab_plan(page_sources(6), {
            "front_cover": {"artboardIndex": 0, "side": "right", "itemIndexes": [100]},
            "back_cover": {"artboardIndex": 0, "side": "left", "itemIndexes": [101]},
        })
        self.assertEqual([(pair["left"]["key"], pair["right"]["key"]) for pair in plan["pairs"]], [
            ("back_cover", "front_cover"),
            ("body_1", "body_6"),
            ("body_5", "body_2"),
            ("body_3", "body_4"),
        ])

    def test_print_profile_is_fixed(self) -> None:
        plan = imposition.build_ab_plan(page_sources(2), {
            "front_cover": {"artboardIndex": 0, "side": "right", "itemIndexes": []},
            "back_cover": {"artboardIndex": 0, "side": "left", "itemIndexes": []},
        })
        self.assertEqual(plan["printProfile"], {
            "duplexFlip": "short-edge", "scalePercent": 100, "centered": True,
            "shrinkToFit": False, "bleedPt": 0,
        })

    def test_a_and_b_are_an_exact_partition(self) -> None:
        plan = imposition.build_ab_plan(page_sources(6), {
            "front_cover": {"artboardIndex": 0, "side": "right", "itemIndexes": []},
            "back_cover": {"artboardIndex": 0, "side": "left", "itemIndexes": []},
        })
        selected = plan["aArtboardIndexes"] + plan["bArtboardIndexes"]
        self.assertEqual(sorted(selected), list(range(len(plan["pairs"]))))
        self.assertFalse(set(plan["aArtboardIndexes"]) & set(plan["bArtboardIndexes"]))

    def test_wrong_left_right_order_is_rejected(self) -> None:
        plan = imposition.build_ab_plan(page_sources(2), {
            "front_cover": {"artboardIndex": 0, "side": "right", "itemIndexes": []},
            "back_cover": {"artboardIndex": 0, "side": "left", "itemIndexes": []},
        })
        invalid = copy.deepcopy(plan)
        invalid["pairs"][1]["left"], invalid["pairs"][1]["right"] = invalid["pairs"][1]["right"], invalid["pairs"][1]["left"]
        with self.assertRaisesRegex(imposition.ImpositionError, "left/right"):
            imposition.validate_ab_plan(invalid)

    def test_generated_jsx_moves_native_objects_without_placing_pdf(self) -> None:
        plan = imposition.build_ab_plan(page_sources(2), {
            "front_cover": {"artboardIndex": 0, "side": "right", "itemIndexes": [10]},
            "back_cover": {"artboardIndex": 0, "side": "left", "itemIndexes": [11]},
        })
        jsx = imposition.build_reorder_jsx(Path("/tmp/source.ai"), Path("/tmp/AB.ai"), Path("/tmp/AB.pdf"), Path("/tmp/qa.json"), plan)
        self.assertIn("item.translate", jsx)
        self.assertIn("preserveEditability=true", jsx)
        self.assertNotIn("placedItems.add", jsx)
        self.assertNotIn("rasterize", jsx.lower())

    def test_generated_jsx_preserves_declared_nonprinting_items(self) -> None:
        plan = imposition.build_ab_plan(page_sources(2), {
            "front_cover": {"artboardIndex": 0, "side": "right", "itemIndexes": [10]},
            "back_cover": {"artboardIndex": 0, "side": "left", "itemIndexes": [11]},
        }, preserve_item_indexes=[4, 16])
        jsx = imposition.build_reorder_jsx(Path("/tmp/source.ai"), Path("/tmp/AB.ai"), Path("/tmp/AB.pdf"), Path("/tmp/qa.json"), plan)
        self.assertIn("preserve=[4, 16]", jsx)
        self.assertNotIn("preserveItem.remove()", jsx)

    def test_split_jsx_verifies_editable_top_level_objects(self) -> None:
        jsx = imposition.build_split_jsx(Path("/tmp/AB.ai"), Path("/tmp/A.ai"), Path("/tmp/A.pdf"), Path("/tmp/qa.json"), [0, 2], "A")
        self.assertIn("expectedKeepTop", jsx)
        self.assertIn("editableObjectsPreserved", jsx)
        self.assertIn("outputTopLevelItems", jsx)

    def test_pdf_boxes_must_all_match_for_zero_bleed(self) -> None:
        output = """Pages: 2
Page size: 841.89 x 595.276 pts (A4)
MediaBox: 0.00 0.00 841.89 595.28
CropBox: 0.00 0.00 841.89 595.28
BleedBox: 0.00 0.00 841.89 595.28
TrimBox: 0.00 0.00 841.89 595.28
"""
        metadata = imposition._parse_pdfinfo(output)
        self.assertEqual(metadata["pages"], 2)
        self.assertTrue(imposition._pdf_has_zero_bleed(metadata))
        metadata["boxes"]["BleedBox"][2] += 9
        self.assertFalse(imposition._pdf_has_zero_bleed(metadata))


class InventoryBlockingTests(unittest.TestCase):
    def fixture(self) -> dict[str, object]:
        return {
            "artboards": [
                {"index": 0, "rect": [0, 100, 200, 0]},
                {"index": 1, "rect": [210, 100, 410, 0]},
            ],
            "halves": [
                {"artboardIndex": 0, "side": "left", "itemIndexes": [0], "pageNumbers": []},
                {"artboardIndex": 0, "side": "right", "itemIndexes": [1], "pageNumbers": []},
                {"artboardIndex": 1, "side": "left", "itemIndexes": [2], "pageNumbers": [1]},
                {"artboardIndex": 1, "side": "right", "itemIndexes": [3], "pageNumbers": [2]},
            ],
            "items": [{"index": index, "name": f"item-{index}"} for index in range(4)],
            "issues": [],
        }

    def test_inventory_jsx_keeps_escape_sequences_literal(self) -> None:
        jsx = imposition.build_inspect_jsx(Path("/tmp/source.ai"), Path("/tmp/inventory.json"))
        self.assertIn(r'.replace(/\\/g, "\\\\")', jsx)
        self.assertIn(r'.replace(/\r/g, "\\r").replace(/\n/g, "\\n")', jsx)
        self.assertIn(r"text.match(/^\s*[-–—]?\s*(\d+)\s*[-–—]?\s*$/)", jsx)
        self.assertNotIn(r"/^\\s*", jsx)
        self.assertIn("geometricBounds", jsx)
        self.assertRegex(jsx, r'new File\("file:///(?:private/)?tmp/source\.ai"\)')

    def test_centerline_crossing_blocks_with_action(self) -> None:
        inventory = self.fixture()
        inventory["issues"] = [{"type": "centerline_crossing", "itemIndex": 2, "artboardIndex": 1, "side": "left", "message": "crosses"}]
        issues, _ = imposition.inspect_inventory_issues(inventory, language="zh-CN")
        issue = next(item for item in issues if item["type"] == "centerline_crossing")
        self.assertEqual(issue["level"], "blocking")
        self.assertIn("将对象完整移到中轴一侧", issue["userAction"])

    def test_duplicate_page_number_blocks(self) -> None:
        inventory = self.fixture()
        inventory["halves"][3]["pageNumbers"] = [1]
        issues, _ = imposition.inspect_inventory_issues(inventory, language="de-DE")
        self.assertIn("duplicate_page_number", {item["type"] for item in issues})

    def test_content_without_page_marker_remains_a_physical_page(self) -> None:
        inventory = self.fixture()
        inventory["halves"][3]["pageNumbers"] = []
        issues, normalized = imposition.inspect_inventory_issues(inventory, language="fr-FR")
        self.assertNotIn("page_unrecognized", {item["type"] for item in issues})
        self.assertEqual(normalized["bodySources"][1]["itemIndexes"], [3])

    def test_nonprinting_items_are_preserved(self) -> None:
        inventory = self.fixture()
        inventory["halves"][2]["pageNumbers"] = []
        inventory["preservedItemIndexes"] = [9]
        issues, normalized = imposition.inspect_inventory_issues(inventory, language="zh-CN")
        self.assertNotIn("page_unrecognized", {item["type"] for item in issues})
        self.assertEqual(normalized["preserveItemIndexes"], [9])
        self.assertEqual(normalized["bodySources"][0]["itemIndexes"], [2])


if __name__ == "__main__":
    unittest.main()
