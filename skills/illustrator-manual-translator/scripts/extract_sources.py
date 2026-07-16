#!/usr/bin/env python3
"""Extract DOCX, XLSX, and image sources into a canonical product JSON document."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import posixpath
import re
import struct
import sys
import unicodedata
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_W = {"w": W_NS}
NS_DOCX = {"w": W_NS, "r": R_NS, "a": A_NS, "wp": WP_NS}
NS_S = {"s": S_NS, "r": R_NS}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}

FIELD_ALIASES = {
    "产品名称": "product_name",
    "product name": "product_name",
    "型号": "model",
    "产品型号": "model",
    "model": "model",
    "产品类型": "type",
    "type": "type",
    "桌面尺寸": "tabletop_size",
    "tabletop size": "tabletop_size",
    "高度调节": "height_adjustment",
    "height adjustment": "height_adjustment",
    "承重": "load_capacity",
    "load capacity": "load_capacity",
    "总重量": "weight",
    "净重": "weight",
    "net weight": "weight",
    "gross weight": "weight",
    "产品外尺寸": "folded_dimensions",
    "折叠尺寸": "folded_dimensions",
    "folded size": "folded_dimensions",
    "桌架材质": "frame_material",
    "frame material": "frame_material",
    "桌面材质": "tabletop_material",
    "tabletop material": "tabletop_material",
    "结构形式": "structure",
    "structure": "structure",
    "配件": "accessories",
    "accessories": "accessories",
    "便携性": "portability",
    "portability": "portability",
}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(element: ET.Element, namespace: str) -> str:
    return "".join(node.text or "" for node in element.iter("{%s}t" % namespace)).strip()


def _language(text: str) -> str:
    return "zh" if re.search(r"[\u3400-\u9fff]", text) else "en"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _image_size(data: bytes) -> Tuple[Optional[int], Optional[int]]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if data.startswith(b"BM") and len(data) >= 26:
        width, height = struct.unpack("<ii", data[18:26])
        return abs(width), abs(height)
    if data.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 9 < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            offset += 2
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                continue
            if offset + 2 > len(data):
                break
            length = struct.unpack(">H", data[offset : offset + 2])[0]
            if length < 2 or offset + length > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                height, width = struct.unpack(">HH", data[offset + 3 : offset + 7])
                return width, height
            offset += length
    return None, None


def _media_type(name: str, data: bytes) -> str:
    guessed = mimetypes.guess_type(name)[0]
    if guessed:
        return guessed
    signatures = ((b"\x89PNG", "image/png"), (b"\xff\xd8", "image/jpeg"),
                  (b"GIF8", "image/gif"), (b"BM", "image/bmp"))
    return next((kind for signature, kind in signatures if data.startswith(signature)),
                "application/octet-stream")


def _cell_ref_column(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference.upper())
    value = 0
    for char in letters.group(0) if letters else "A":
        value = value * 26 + ord(char) - 64
    return value - 1


class CanonicalExtractor:
    def __init__(self, asset_dir: Optional[Path] = None) -> None:
        self.asset_dir = asset_dir.expanduser().resolve() if asset_dir else None
        if self.asset_dir:
            self.asset_dir.mkdir(parents=True, exist_ok=True)
        self.document = {
            "schema": "CanonicalProduct",
            "schema_version": "1.0",
            "product": {"name": None, "model": None},
            "sources": [],
            "paragraphs": [],
            "tables": [],
            "assets": [],
            "evidence": [],
            "conflicts": [],
        }

    def extract(self, paths: Sequence[Path]) -> dict:
        if not paths:
            raise ValueError("at least one source path is required")
        for path in paths:
            path = path.expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(str(path))
            suffix = path.suffix.lower()
            source_id = "source-%03d" % (len(self.document["sources"]) + 1)
            data = path.read_bytes()
            self.document["sources"].append({
                "id": source_id,
                "path": str(path),
                "name": path.name,
                "kind": suffix.lstrip("."),
                "sha256": _sha256(data),
                "byte_size": len(data),
            })
            if suffix == ".docx":
                self._extract_docx(path, source_id)
            elif suffix == ".xlsx":
                self._extract_xlsx(path, source_id)
            elif suffix in IMAGE_SUFFIXES:
                self._add_asset(source_id, path.name, data, "file")
            else:
                raise ValueError("unsupported source type: %s" % suffix)
        self._derive_evidence_and_conflicts()
        return self.document

    def _add_paragraph(self, source_id: str, text: str, location: dict) -> None:
        text = text.strip()
        if not text:
            return
        self.document["paragraphs"].append({
            "id": "paragraph-%04d" % (len(self.document["paragraphs"]) + 1),
            "source_id": source_id,
            "language": _language(text),
            "location": location,
            "text": text,
        })

    def _add_table(self, source_id: str, rows: List[List[str]], location: dict) -> None:
        if not rows:
            return
        width = max((len(row) for row in rows), default=0)
        padded = [row + [""] * (width - len(row)) for row in rows]
        self.document["tables"].append({
            "id": "table-%03d" % (len(self.document["tables"]) + 1),
            "source_id": source_id,
            "language": _language(" ".join(value for row in padded for value in row)),
            "location": location,
            "rows": padded,
        })

    def _add_asset(self, source_id: str, name: str, data: bytes, location: str) -> dict:
        width, height = _image_size(data)
        digest = _sha256(data)
        extracted_path = None
        if self.asset_dir:
            suffix = Path(name).suffix.lower() or mimetypes.guess_extension(_media_type(name, data)) or ".bin"
            target = self.asset_dir / (digest + suffix)
            if not target.exists():
                target.write_bytes(data)
            extracted_path = str(target)
        item = {
            "id": "asset-%03d" % (len(self.document["assets"]) + 1),
            "source_id": source_id,
            "name": Path(name).name,
            "location": location,
            "media_type": _media_type(name, data),
            "byte_size": len(data),
            "sha256": digest,
            "path": extracted_path,
            "width": width,
            "height": height,
            "format": Path(name).suffix.lower().lstrip(".") or None,
            "has_alpha": bool(data.startswith(b"\x89PNG") and len(data) > 25 and data[25] in (4, 6)) if data.startswith(b"\x89PNG") else False,
            "aspect_ratio": round(width / height, 6) if width and height else None,
            "occurrences": [],
            "slot_hints": [],
        }
        self.document["assets"].append(item)
        return item

    def _visual_marker_evidence(self, source_id: str, text: str, paragraph: int, slot_id: str | None) -> dict:
        item = {
            "id": "evidence-%04d" % (len(self.document["evidence"]) + 1),
            "kind": "visual_asset_marker", "field": "visual_asset", "value": text,
            "slot_id": slot_id, "source_id": source_id,
            "location": {"part": "word/document.xml", "paragraph": paragraph},
        }
        self.document["evidence"].append(item)
        return item

    def _extract_docx(self, path: Path, source_id: str) -> None:
        with zipfile.ZipFile(str(path)) as archive:
            names = set(archive.namelist())
            assets_by_part = {}
            for name in sorted(n for n in names if n.startswith("word/media/")):
                assets_by_part[posixpath.normpath(name)] = self._add_asset(source_id, name, archive.read(name), name)
            relationships = {}
            if "word/_rels/document.xml.rels" in names:
                rel_root = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
                relationships = {
                    str(rel.get("Id")): posixpath.normpath(posixpath.join("word", str(rel.get("Target") or "")))
                    for rel in rel_root.findall("{%s}Relationship" % P_NS)
                }
            root = ET.fromstring(archive.read("word/document.xml"))
            body = root.find("w:body", NS_W)
            paragraph_number = table_number = 0
            last_image_occurrences: List[dict] = []
            claimed_slots: Dict[str, dict] = {}
            if body is not None:
                for child in body:
                    name = _local_name(child.tag)
                    if name == "p":
                        paragraph_number += 1
                        text = _text(child, W_NS)
                        occurrences = []
                        for drawing in child.findall(".//w:drawing", NS_DOCX):
                            extent = drawing.find(".//wp:extent", NS_DOCX)
                            for blip in drawing.findall(".//a:blip", NS_DOCX):
                                rel_id = blip.get("{%s}embed" % R_NS) or ""
                                asset = assets_by_part.get(relationships.get(rel_id, ""))
                                if not asset:
                                    continue
                                cx = int(extent.get("cx", "0")) if extent is not None else 0
                                cy = int(extent.get("cy", "0")) if extent is not None else 0
                                occurrence = {
                                    "id": "asset-occurrence-%04d" % (sum(len(a["occurrences"]) for a in self.document["assets"]) + 1),
                                    "part": "word/document.xml", "paragraph": paragraph_number,
                                    "relationship_id": rel_id, "display_width_emu": cx or None, "display_height_emu": cy or None,
                                    "effective_dpi_x": round(asset["width"] * 914400 / cx, 1) if asset.get("width") and cx else None,
                                    "effective_dpi_y": round(asset["height"] * 914400 / cy, 1) if asset.get("height") and cy else None,
                                }
                                asset["occurrences"].append(occurrence)
                                occurrences.append({"asset": asset, "occurrence": occurrence})
                        marker = re.fullmatch(r"\s*\[ASSET:\s*([a-z0-9][a-z0-9._-]*)\s*\]\s*", text, flags=re.I)
                        if marker:
                            slot_id = marker.group(1).lower()
                            evidence = self._visual_marker_evidence(source_id, text, paragraph_number, slot_id)
                            if slot_id in claimed_slots:
                                self._add_conflict("duplicate_asset_slot_marker", "visual_asset", "high", slot_id,
                                                   "同一图片槽位被重复标记。", [claimed_slots[slot_id], evidence], [{"slot_id": slot_id}])
                            elif len(last_image_occurrences) == 1:
                                link = last_image_occurrences[0]
                                hint = {"slot_id": slot_id, "marker": text,
                                        "marker_location": evidence["location"], "occurrence_id": link["occurrence"]["id"]}
                                link["asset"]["slot_hints"].append(hint)
                                claimed_slots[slot_id] = evidence
                            else:
                                code = "ambiguous_asset_marker" if len(last_image_occurrences) > 1 else "asset_marker_without_image"
                                message = "图片标记前一个图片段落包含多张图片。" if last_image_occurrences else "图片标记前没有可绑定的单图段落。"
                                self._add_conflict(code, "visual_asset", "high", slot_id, message, [evidence], [{"slot_id": slot_id}])
                            last_image_occurrences = []
                            continue
                        if "[ASSET:" in text.upper():
                            evidence = self._visual_marker_evidence(source_id, text, paragraph_number, None)
                            self._add_conflict("invalid_asset_marker", "visual_asset", "high", "visual_asset",
                                               "图片标记格式无效。", [evidence], [{"marker": text}])
                            last_image_occurrences = []
                            continue
                        self._add_paragraph(source_id, text, {"part": "word/document.xml", "paragraph": paragraph_number})
                        if occurrences:
                            last_image_occurrences = occurrences
                        elif text.strip():
                            last_image_occurrences = []
                    elif name == "tbl":
                        last_image_occurrences = []
                        table_number += 1
                        rows = []
                        for row in child.findall("w:tr", NS_W):
                            rows.append([_text(cell, W_NS) for cell in row.findall("w:tc", NS_W)])
                        self._add_table(
                            source_id, rows,
                            {"part": "word/document.xml", "table": table_number},
                        )

    def _extract_xlsx(self, path: Path, source_id: str) -> None:
        with zipfile.ZipFile(str(path)) as archive:
            names = set(archive.namelist())
            shared = []
            if "xl/sharedStrings.xml" in names:
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = [_text(item, S_NS) for item in root.findall("s:si", NS_S)]

            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            rel_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            relationships = {
                rel.get("Id"): rel.get("Target")
                for rel in rel_root.findall("{%s}Relationship" % P_NS)
            }
            for sheet_index, sheet in enumerate(workbook.findall("s:sheets/s:sheet", NS_S), 1):
                rel_id = sheet.get("{%s}id" % R_NS)
                target = relationships.get(rel_id or "")
                if not target:
                    continue
                sheet_path = posixpath.normpath(posixpath.join("xl", target))
                sheet_root = ET.fromstring(archive.read(sheet_path))
                rows: List[List[str]] = []
                for row in sheet_root.findall(".//s:sheetData/s:row", NS_S):
                    values: List[str] = []
                    for cell in row.findall("s:c", NS_S):
                        column = _cell_ref_column(cell.get("r", "A1"))
                        while len(values) <= column:
                            values.append("")
                        cell_type = cell.get("t")
                        if cell_type == "inlineStr":
                            value = _text(cell, S_NS)
                        else:
                            value_node = cell.find("s:v", NS_S)
                            value = value_node.text if value_node is not None and value_node.text else ""
                            if cell_type == "s" and value:
                                try:
                                    value = shared[int(value)]
                                except (ValueError, IndexError):
                                    pass
                            elif cell_type == "b":
                                value = "TRUE" if value == "1" else "FALSE"
                        values[column] = value
                    while values and not values[-1]:
                        values.pop()
                    if values:
                        rows.append(values)
                self._add_table(source_id, rows, {
                    "part": sheet_path,
                    "sheet": sheet.get("name", "Sheet%d" % sheet_index),
                    "sheet_index": sheet_index,
                })
            for name in sorted(n for n in names if n.startswith("xl/media/")):
                self._add_asset(source_id, name, archive.read(name), name)

    def _derive_evidence_and_conflicts(self) -> None:
        evidence_by_field: Dict[str, List[dict]] = {}
        for table in self.document["tables"]:
            for row_number, row in enumerate(table["rows"], 1):
                if len(row) < 2:
                    continue
                label, value = row[0].strip(), row[1].strip()
                field = FIELD_ALIASES.get(label.casefold())
                if not field or not value:
                    continue
                item = {
                    "id": "evidence-%04d" % (len(self.document["evidence"]) + 1),
                    "kind": "specification",
                    "field": field,
                    "label": label,
                    "value": value,
                    "language": table["language"],
                    "source_id": table["source_id"],
                    "location": {"table_id": table["id"], "row": row_number},
                }
                self.document["evidence"].append(item)
                evidence_by_field.setdefault(field, []).append(item)

        names = evidence_by_field.get("product_name", [])
        models = evidence_by_field.get("model", [])
        if names:
            self.document["product"]["name"] = names[0]["value"]
        if models:
            self.document["product"]["model"] = models[0]["value"]
        if not self.document["product"]["model"]:
            for paragraph in self.document["paragraphs"]:
                match = re.search(r"\bFT[- ]?\d+[A-Z]*\b", paragraph["text"], re.I)
                if match:
                    self.document["product"]["model"] = match.group(0)
                    break

        languages_with_specs = {
            item["language"] for values in evidence_by_field.values() for item in values
        }
        zh_models = [item for item in models if item["language"] == "zh"]
        en_models = [item for item in models if item["language"] == "en"]
        if "zh" in languages_with_specs and en_models and not zh_models:
            self._add_conflict(
                "missing_model_zh", "missing_field", "high", "model",
                "中文技术参数缺少型号，而英文技术参数包含 Model。",
                en_models, [{"language": "zh", "value": None},
                            {"language": "en", "value": en_models[0]["value"]}],
            )

        self._add_label_mismatch(
            evidence_by_field.get("weight", []), "weight_label_mismatch", "weight",
            {"总重量", "gross weight"}, {"净重", "net weight"},
            "中文“总重量”和英文“Net Weight”语义不同，不能视为直接等价字段。",
        )
        self._add_label_mismatch(
            evidence_by_field.get("folded_dimensions", []),
            "dimensions_label_mismatch", "folded_dimensions",
            {"产品外尺寸"}, {"折叠尺寸", "folded size"},
            "中文“产品外尺寸”和英文“Folded Size”语义不同，需确认尺寸状态。",
        )
        self._find_anomalous_characters()

    def _add_label_mismatch(
        self, items: List[dict], code: str, field: str,
        left_labels: set, right_labels: set, message: str,
    ) -> None:
        left = [item for item in items if item["label"].casefold() in left_labels]
        right = [item for item in items if item["label"].casefold() in right_labels]
        if left and right:
            self._add_conflict(
                code, "semantic_mismatch", "high", field, message,
                left + right,
                [{"language": item["language"], "label": item["label"],
                  "value": item["value"]} for item in left + right],
            )

    def _find_anomalous_characters(self) -> None:
        entries: Iterable[Tuple[str, str, dict]] = (
            (p["source_id"], p["text"], {"paragraph_id": p["id"]})
            for p in self.document["paragraphs"]
        )
        table_entries = (
            (table["source_id"], value,
             {"table_id": table["id"], "row": row_index, "column": column_index})
            for table in self.document["tables"]
            for row_index, row in enumerate(table["rows"], 1)
            for column_index, value in enumerate(row, 1)
        )
        seen = set()
        for source_id, text, location in list(entries) + list(table_entries):
            bad = []
            for char in text:
                codepoint = ord(char)
                category = unicodedata.category(char)
                if (0x10A0 <= codepoint <= 0x10FF or 0x1C90 <= codepoint <= 0x1CBF
                        or category in {"Cc", "Cs", "Co"} or char == "\ufffd"):
                    bad.append(char)
            mojibake = any(marker in text for marker in ("Ã", "Â", "â€", "ï¿½"))
            if not bad and not mojibake:
                continue
            signature = (source_id, text, tuple(bad))
            if signature in seen:
                continue
            seen.add(signature)
            item = {
                "id": "evidence-%04d" % (len(self.document["evidence"]) + 1),
                "kind": "text_anomaly",
                "field": "text",
                "value": text,
                "source_id": source_id,
                "location": location,
                "characters": [
                    {"character": char, "codepoint": "U+%04X" % ord(char),
                     "name": unicodedata.name(char, "UNKNOWN")}
                    for char in dict.fromkeys(bad)
                ],
            }
            self.document["evidence"].append(item)
            shown = "".join(dict.fromkeys(bad)) or "mojibake sequence"
            self._add_conflict(
                "anomalous_characters", "text_anomaly", "high", "text",
                "检测到与上下文脚本不一致的异常字符：%s" % shown,
                [item], [{"text": text, "characters": item["characters"]}],
            )

    def _add_conflict(
        self, code: str, kind: str, severity: str, field: str, message: str,
        evidence: Sequence[dict], values: Sequence[dict],
    ) -> None:
        self.document["conflicts"].append({
            "id": "conflict-%03d" % (len(self.document["conflicts"]) + 1),
            "code": code,
            "kind": kind,
            "severity": severity,
            "field": field,
            "message": message,
            "evidence_ids": [item["id"] for item in evidence],
            "values": list(values),
        })


def extract_sources(paths: Sequence[Path], asset_dir: Optional[Path] = None) -> dict:
    """Return one CanonicalProduct document for all supplied source paths."""
    return CanonicalExtractor(asset_dir=asset_dir).extract([Path(path) for path in paths])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract DOCX, XLSX, and image files to CanonicalProduct JSON."
    )
    parser.add_argument("sources", nargs="+", type=Path, help="input source files")
    parser.add_argument("-o", "--output", type=Path, help="write JSON to this path")
    parser.add_argument("--asset-dir", type=Path, help="extract embedded images to this directory")
    parser.add_argument("--compact", action="store_true", help="disable pretty printing")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = extract_sources(args.sources, asset_dir=args.asset_dir)
        text = json.dumps(
            result, ensure_ascii=False, indent=None if args.compact else 2,
            sort_keys=False,
        ) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0
    except (FileNotFoundError, ValueError, zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        print("extract_sources: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
