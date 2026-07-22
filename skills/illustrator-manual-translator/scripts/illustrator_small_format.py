#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import illustrator_folded_leaflet as folded
import illustrator_imposition as imposition
import illustrator_worker


SCHEMA = "illustrator-small-format/2.0"
PLAN_SCHEMA = "small-format-plan/2.0"
QA_CONTRACT_VERSION = 2
DEFAULT_GEOMETRY_MM = {
    "pageWidth": 76.0,
    "pageHeight": 156.22,
    "minimumBodyFontPt": 5.0,
    "contentTopInset": 5.8,
    "contentBottomInset": 3.7,
    "sectionGap": 4.0,
    "artboardGap": 31.0,
    "sheetMarginHorizontal": 5.0,
    "sheetMarginVertical": 9.19,
}


class SmallFormatError(RuntimeError):
    pass


def mm_to_pt(value: float) -> float:
    return float(value) * 72.0 / 25.4


def normalize_geometry(raw: Mapping[str, Any] | None = None) -> dict[str, float]:
    geometry = {**DEFAULT_GEOMETRY_MM, **dict(raw or {})}
    normalized: dict[str, float] = {}
    for key, value in geometry.items():
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise SmallFormatError(f"{key} must be a finite positive number")
        normalized[key] = number
    if normalized["contentTopInset"] + normalized["contentBottomInset"] >= normalized["pageHeight"]:
        raise SmallFormatError("Content insets leave no usable page height")
    return normalized


def _slot_records(
    inventory: Mapping[str, Any], geometry: Mapping[str, float]
) -> list[dict[str, Any]]:
    boards = {int(item["index"]): item for item in inventory.get("artboards") or []}
    items_by_index = {int(item["index"]): item for item in inventory.get("items") or []}
    page_width_pt = mm_to_pt(geometry["pageWidth"])
    section_gap_pt = mm_to_pt(geometry["sectionGap"])
    slots: list[dict[str, Any]] = []
    for half in inventory.get("halves") or []:
        artboard_index = int(half["artboardIndex"])
        side = str(half["side"])
        board_rect = [float(value) for value in boards[artboard_index]["rect"]]
        mid = (board_rect[0] + board_rect[2]) / 2
        source_rect = [
            mid if side == "right" else board_rect[0],
            board_rect[1],
            board_rect[2] if side == "right" else mid,
            board_rect[3],
        ]
        item_indexes = [int(value) for value in half.get("itemIndexes") or []]
        groups, footer_indexes, header_count = folded._vertical_layout_groups(
            item_indexes, items_by_index, source_half_rect=source_rect
        )
        if not groups and not footer_indexes:
            continue
        scale = page_width_pt / (source_rect[2] - source_rect[0])
        group_heights: list[float] = []
        for group in groups:
            bounds = [items_by_index[index]["bounds"] for index in group]
            group_heights.append((max(bound[1] for bound in bounds) - min(bound[3] for bound in bounds)) * scale)
        body_height = sum(group_heights) + section_gap_pt * max(0, len(group_heights) - 1)
        footer_height = 0.0
        if footer_indexes:
            bounds = [items_by_index[index]["bounds"] for index in footer_indexes]
            footer_height = (max(bound[1] for bound in bounds) - min(bound[3] for bound in bounds)) * scale
        slots.append({
            "sourceArtboard": artboard_index,
            "sourceSide": side,
            "sourceRect": source_rect,
            "itemIndexes": item_indexes,
            "groups": groups,
            "footerIndexes": footer_indexes,
            "headerCount": header_count,
            "scale": scale,
            "bodyHeightPt": body_height,
            "footerHeightPt": footer_height,
        })
    return slots


def _pack_slots(slots: Sequence[Mapping[str, Any]], geometry: Mapping[str, float]) -> list[list[dict[str, Any]]]:
    usable_height = mm_to_pt(geometry["pageHeight"] - geometry["contentTopInset"] - geometry["contentBottomInset"])
    gap = mm_to_pt(geometry["sectionGap"])
    pages: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_height = 0.0
    for raw_slot in slots:
        slot = dict(raw_slot)
        body_height = float(slot["bodyHeightPt"])
        footer_height = float(slot["footerHeightPt"])
        required = body_height + (gap + footer_height if footer_height else 0.0)
        if required > usable_height + 0.1:
            raise SmallFormatError(
                f"Source half-page does not fit small page: {slot['sourceArtboard']}:{slot['sourceSide']}"
            )
        mergeable = not slot["footerIndexes"]
        can_append = bool(current) and mergeable and all(not item["footerIndexes"] for item in current)
        appended_height = current_height + (gap if current else 0.0) + body_height
        if can_append and appended_height <= usable_height + 0.1:
            current.append(slot)
            current_height = appended_height
            continue
        if current:
            pages.append(current)
        current = [slot]
        current_height = required
        if slot["footerIndexes"]:
            pages.append(current)
            current = []
            current_height = 0.0
    if current:
        pages.append(current)
    return pages


def build_plan(
    inventory: Mapping[str, Any], *, geometry: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    artboard_count = int(inventory.get("artboardCount") or 0)
    if artboard_count <= 0:
        raise SmallFormatError("Source AI must contain at least one artboard")
    normalized = normalize_geometry(geometry)
    items_by_index = {int(item["index"]): item for item in inventory.get("items") or []}
    slots = _slot_records(inventory, normalized)
    pages = _pack_slots(slots, normalized)
    page_width = mm_to_pt(normalized["pageWidth"])
    page_height = mm_to_pt(normalized["pageHeight"])
    top_inset = mm_to_pt(normalized["contentTopInset"])
    bottom_inset = mm_to_pt(normalized["contentBottomInset"])
    section_gap = mm_to_pt(normalized["sectionGap"])
    artboard_gap = mm_to_pt(normalized["artboardGap"])
    content_page_count = len(pages)
    page_count = content_page_count + (content_page_count % 2)
    columns = page_count // 2
    sheet_margin_x = mm_to_pt(normalized["sheetMarginHorizontal"])
    sheet_margin_y = mm_to_pt(normalized["sheetMarginVertical"])
    sheet_width = page_width * columns + sheet_margin_x * 2
    sheet_height = page_height + sheet_margin_y * 2
    artboards = [
        {
            "index": 0,
            "name": "SMALL-FORMAT-ROW-2",
            "rect": [0.0, -(sheet_height + artboard_gap), sheet_width, -(sheet_height + artboard_gap) - sheet_height],
        },
        {"index": 1, "name": "SMALL-FORMAT-ROW-1", "rect": [0.0, 0.0, sheet_width, -sheet_height]},
    ]
    panel_rects: list[list[float]] = []
    movements: list[dict[str, Any]] = []
    page_manifest: list[dict[str, Any]] = []
    for page_index in range(page_count):
        row_index = page_index // columns
        column_index = page_index % columns
        target_artboard = 1 - row_index
        board = artboards[target_artboard]
        page_left = sheet_margin_x + column_index * page_width
        page_top = float(board["rect"][1]) - sheet_margin_y
        page_rect = [page_left, page_top, page_left + page_width, page_top - page_height]
        panel_rects.append(page_rect)
        if page_index >= content_page_count:
            page_manifest.append({"pageIndex": page_index, "row": row_index, "column": column_index, "artboardIndex": target_artboard, "blank": True, "sources": []})
            continue
        page_slots = pages[page_index]
        cursor_top = page_top - top_inset
        source_refs: list[dict[str, Any]] = []
        for slot_index, slot in enumerate(page_slots):
            source_rect = slot["sourceRect"]
            scale = float(slot["scale"])
            source_refs.append({"sourceArtboard": slot["sourceArtboard"], "sourceSide": slot["sourceSide"]})
            for group in slot["groups"]:
                bounds = [items_by_index[index]["bounds"] for index in group]
                group_top = max(bound[1] for bound in bounds)
                group_bottom = min(bound[3] for bound in bounds)
                current_top = page_top - (source_rect[1] - group_top) * scale
                shift = cursor_top - current_top
                for item_index in group:
                    movements.append({
                        "itemIndex": item_index,
                        "targetArtboard": target_artboard,
                        "sourceRect": source_rect,
                        "fitRect": [page_left, page_top, page_left + page_width, page_top - (source_rect[1] - source_rect[3]) * scale],
                        "targetPageRect": page_rect,
                        "scale": scale,
                        "verticalShiftPt": shift,
                    })
                cursor_top -= (group_top - group_bottom) * scale + section_gap
            footer_indexes = list(slot["footerIndexes"])
            if footer_indexes:
                bounds = [items_by_index[index]["bounds"] for index in footer_indexes]
                footer_bottom = min(bound[3] for bound in bounds)
                current_bottom = page_top - (source_rect[1] - footer_bottom) * scale
                footer_shift = page_rect[3] + bottom_inset - current_bottom
                for item_index in footer_indexes:
                    movements.append({
                        "itemIndex": item_index,
                        "targetArtboard": target_artboard,
                        "sourceRect": source_rect,
                        "fitRect": [page_left, page_top, page_left + page_width, page_top - (source_rect[1] - source_rect[3]) * scale],
                        "targetPageRect": page_rect,
                        "scale": scale,
                        "verticalShiftPt": footer_shift,
                    })
            if slot_index + 1 < len(page_slots):
                cursor_top -= section_gap
        page_manifest.append({"pageIndex": page_index, "row": row_index, "column": column_index, "artboardIndex": target_artboard, "blank": False, "sources": source_refs})
    mapped = {int(item["itemIndex"]) for item in movements}
    preserved = {int(value) for value in inventory.get("preservedItemIndexes") or []}
    source_guides = {
        int(item["index"])
        for item in inventory.get("items") or []
        if bool((item.get("details") or {}).get("guides"))
    }
    printable = {
        int(item["index"])
        for item in inventory.get("items") or []
        if int(item["index"]) not in preserved
    }
    if mapped != printable:
        raise SmallFormatError(f"Printable object mapping is incomplete; unmapped={sorted(printable - mapped)}")
    return {
        "schema": PLAN_SCHEMA,
        "geometryMm": normalized,
        "contentPageCount": content_page_count,
        "pageCount": page_count,
        "blankPageCount": page_count - content_page_count,
        "rowCount": 2,
        "columnCount": columns,
        "artboardCount": 2,
        "sheetWidthPt": sheet_width,
        "sheetHeightPt": sheet_height,
        "sourceHalfPageCount": len(slots),
        "artboards": artboards,
        "panelRects": panel_rects,
        "pages": page_manifest,
        "movements": movements,
        "preserveItemIndexes": sorted(preserved - source_guides),
        "replaceGuideItemIndexes": sorted(source_guides),
    }


def build_layout_jsx(
    source_ai: Path, output_ai: Path, output_pdf: Path, qa_path: Path, plan: Mapping[str, Any]
) -> str:
    geometry = plan["geometryMm"]
    movements = list(plan["movements"])
    preserved = list(plan["preserveItemIndexes"])
    guides = list(plan["replaceGuideItemIndexes"])
    artboards = list(plan["artboards"])
    panel_rects = list(plan["panelRects"])
    minimum_font = float(geometry["minimumBodyFontPt"])
    return fr'''#target illustrator
(function () {{
  function write(path,content) {{ var f=new File(path); f.encoding="UTF-8"; if(!f.open("w")) throw new Error("Cannot write small-format QA: "+path); f.write(content); f.close(); }}
  function topItems(doc) {{ var out=[]; for(var i=0;i<doc.pageItems.length;i++) {{ var item=doc.pageItems[i]; try {{ if(item.parent&&item.parent.typename==="Layer") out.push(item); }} catch(e) {{}} }} return out; }}
  function unlockParents(item) {{ var current=item; while(current&&current.typename!=="Document") {{ try {{ current.locked=false; }} catch(e) {{}} try {{ current.visible=true; }} catch(e2) {{}} current=current.parent; }} }}
  var source=new File({imposition._jsx(source_ai)}), outputAI=new File({imposition._jsx(output_ai)}), outputPDF=new File({imposition._jsx(output_pdf)});
  if(!source.exists) throw new Error("Source AI not found: "+source.fsName);
  app.userInteractionLevel=UserInteractionLevel.DONTDISPLAYALERTS;
  var doc=app.open(source), sourceText=doc.textFrames.length, sourcePlaced=doc.placedItems.length, sourceRaster=doc.rasterItems.length, sourceGroups=doc.groupItems.length, sourceLayers=doc.layers.length;
  try {{
    var top=topItems(doc), moves={json.dumps(movements, ensure_ascii=False)}, preserve={json.dumps(preserved)}, replaceGuides={json.dumps(guides)}, boards={json.dumps(artboards, ensure_ascii=False)}, panelRects={json.dumps(panel_rects)}, seen={{}};
    for(var i=0;i<moves.length;i++) {{
      var move=moves[i], item=top[Number(move.itemIndex)];
      if(!item) throw new Error("Top-level item index changed: "+move.itemIndex);
      if(seen[String(move.itemIndex)]) throw new Error("Top-level item assigned more than once: "+move.itemIndex);
      seen[String(move.itemIndex)]=true; unlockParents(item);
      var old=item.geometricBounds, oldLeft=Number(old[0]), oldTop=Number(old[1]), factor=Number(move.scale);
      item.resize(factor*100,factor*100,true,true,true,true,factor*100,Transformation.TOPLEFT);
      var scaled=item.geometricBounds;
      var targetLeft=Number(move.fitRect[0])+(oldLeft-Number(move.sourceRect[0]))*factor;
      var targetTop=Number(move.fitRect[1])-(Number(move.sourceRect[1])-oldTop)*factor+Number(move.verticalShiftPt||0);
      item.translate(targetLeft-Number(scaled[0]),targetTop-Number(scaled[1]));
    }}
    for(var p=0;p<preserve.length;p++) {{ var idx=Number(preserve[p]); if(!top[idx]) throw new Error("Preserved item index changed: "+idx); seen[String(idx)]=true; }}
    for(var g=0;g<replaceGuides.length;g++) {{ var guideIndex=Number(replaceGuides[g]); if(!top[guideIndex]) throw new Error("Source guide index changed: "+guideIndex); seen[String(guideIndex)]=true; }}
    for(var n=0;n<top.length;n++) if(!seen[String(n)]) throw new Error("Unmapped editable object remains: "+n);
    for(var removeGuide=0;removeGuide<replaceGuides.length;removeGuide++) {{ unlockParents(top[Number(replaceGuides[removeGuide])]); top[Number(replaceGuides[removeGuide])].remove(); }}

    while(doc.artboards.length<boards.length) doc.artboards.add(boards[doc.artboards.length].rect);
    for(var a=0;a<boards.length;a++) {{ doc.artboards[a].artboardRect=boards[a].rect; doc.artboards[a].name=boards[a].name; }}
    for(var removeIndex=doc.artboards.length-1;removeIndex>=boards.length;removeIndex--) doc.artboards.remove(removeIndex);

    var guideLayer=doc.layers.add(); guideLayer.name="SMALL_FORMAT_GUIDES"; guideLayer.printable=false; var guideCount=0;
    for(var panelIndex=0;panelIndex<panelRects.length;panelIndex++) {{
      var panel=panelRects[panelIndex], guide=guideLayer.pathItems.rectangle(Number(panel[1]),Number(panel[0]),Number(panel[2])-Number(panel[0]),Number(panel[1])-Number(panel[3]));
      guide.stroked=false; guide.filled=false; guide.guides=true; guide.name="SMALL-PAGE-"+(panelIndex+1); guideCount++;
    }}

    var minFont={minimum_font}, raisedFonts=0, overset=[];
    for(var t=0;t<doc.textFrames.length;t++) {{
      var frame=doc.textFrames[t], attrs=frame.textRange.characterAttributes, size=0;
      try {{ size=Number(attrs.size); }} catch(sizeError) {{ size=0; }}
      if(size>0&&size<minFont) {{ try {{ attrs.size=minFont; raisedFonts++; }} catch(fontError) {{}} }}
      try {{ if(frame.kind===TextType.AREATEXT&&frame.overflows) overset.push(t); }} catch(overflowError) {{}}
    }}
    var outOfPage=[];
    for(var c=0;c<moves.length;c++) {{
      var checkedMove=moves[c], checkedItem=top[Number(checkedMove.itemIndex)], visible=checkedItem.visibleBounds, pageRect=checkedMove.targetPageRect, tolerance=0.75;
      if(Number(visible[0])<Number(pageRect[0])-tolerance||Number(visible[1])>Number(pageRect[1])+tolerance||Number(visible[2])>Number(pageRect[2])+tolerance||Number(visible[3])<Number(pageRect[3])-tolerance) outOfPage.push(Number(checkedMove.itemIndex));
    }}
    var aiOptions=new IllustratorSaveOptions(); aiOptions.pdfCompatible=true; doc.saveAs(outputAI,aiOptions);
    var pdfOptions=new PDFSaveOptions(); pdfOptions.preserveEditability=true; pdfOptions.viewAfterSaving=false;
    try {{ pdfOptions.saveMultipleArtboards=true; pdfOptions.artboardRange="1-2"; pdfOptions.bleedOffsetRect=[0,0,0,0]; pdfOptions.bleedLink=false; }} catch(pdfError) {{}}
    try {{ pdfOptions.trimMarks=false; pdfOptions.registrationMarks=false; pdfOptions.colorBars=false; pdfOptions.pageInformation=false; }} catch(markError) {{}}
    doc.saveAs(outputPDF,pdfOptions);
    var outputTop=topItems(doc).length, board0=doc.artboards[0].artboardRect, board1=doc.artboards[1].artboardRect, editable=(doc.artboards.length===2&&outputTop===top.length-replaceGuides.length+guideCount&&doc.textFrames.length===sourceText&&doc.placedItems.length===sourcePlaced&&doc.rasterItems.length===sourceRaster&&doc.groupItems.length===sourceGroups&&doc.layers.length===sourceLayers+1);
    write({imposition._jsx(qa_path)}, '{{"schema":"{SCHEMA}","qaContractVersion":{QA_CONTRACT_VERSION},"variant":"SMALL_FORMAT","artboardCount":'+doc.artboards.length+',"artboard0TopPt":'+Number(board0[1])+',"artboard1TopPt":'+Number(board1[1])+',"artboardTopDeltaPt":'+(Number(board1[1])-Number(board0[1]))+',"rowCount":2,"columnCount":{int(plan['columnCount'])},"contentPageCount":{int(plan['contentPageCount'])},"pageCount":{int(plan['pageCount'])},"blankPageCount":{int(plan['blankPageCount'])},"guideRectangleCount":'+guideCount+',"sheetWidthPt":{float(plan['sheetWidthPt'])},"sheetHeightPt":{float(plan['sheetHeightPt'])},"pageWidthPt":{mm_to_pt(geometry['pageWidth'])},"pageHeightPt":{mm_to_pt(geometry['pageHeight'])},"minimumBodyFontPt":'+minFont+',"raisedFontFrameCount":'+raisedFonts+',"oversetTextFrameIndexes":['+overset.join(',')+'],"outOfPageItemIndexes":['+outOfPage.join(',')+'],"sourceTopLevelItems":'+top.length+',"mappedTopLevelItems":'+moves.length+',"preservedTopLevelItems":'+preserve.length+',"replacedSourceGuideCount":'+replaceGuides.length+',"outputTopLevelItems":'+outputTop+',"editableObjectsPreserved":'+(editable?'true':'false')+',"bleedPt":0}}');
  }} finally {{ doc.close(SaveOptions.DONOTSAVECHANGES); }}
}}());
'''


def validate_qa(qa: Mapping[str, Any], plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if int(qa.get("artboardCount") or 0) != 2 or int(qa.get("rowCount") or 0) != 2:
        issues.append(imposition.blocking_issue("row_layout_mismatch", "小版面必须输出为上下两个横向画板", "请按模板恢复上下两行横向排列"))
    expected_top_delta = float(plan["sheetHeightPt"]) + mm_to_pt(plan["geometryMm"]["artboardGap"])
    if float(qa.get("artboard0TopPt") or 0) >= float(qa.get("artboard1TopPt") or 0) or abs(float(qa.get("artboardTopDeltaPt") or 0) - expected_top_delta) > 0.1:
        issues.append(imposition.blocking_issue("artboard_order_mismatch", "画板顺序或上下行间距与模板不一致", "请保持下行为画板 1、上行为画板 2，并使用 31 mm 行间距"))
    if int(qa.get("pageCount") or 0) != int(plan["pageCount"]) or int(plan["pageCount"]) % 2:
        issues.append(imposition.blocking_issue("odd_page_count", "小版面页数必须为偶数", "请在末尾补一个无内容空白页后重新拼版"))
    if int(qa.get("columnCount") or 0) != int(plan["columnCount"]):
        issues.append(imposition.blocking_issue("column_count_mismatch", "上下两行的小版面列数不一致", "请确保两行使用相同横向列数"))
    if int(qa.get("guideRectangleCount") or 0) != int(plan["pageCount"]):
        issues.append(imposition.blocking_issue("guide_count_mismatch", "小版面参考线数量与页面数量不一致", "请为每个小页面生成一个模板尺寸参考框"))
    if not qa.get("editableObjectsPreserved"):
        issues.append(imposition.blocking_issue("editability_lost", "小版面输出的可编辑对象计数不一致", "请从电子版 AI 重新生成，不要置入整页 PDF"))
    if qa.get("oversetTextFrameIndexes"):
        issues.append(imposition.blocking_issue("text_overflow", f"小版面存在溢出文本框：{qa['oversetTextFrameIndexes']}", "请调整内容分页，不得继续缩小低于最小字号"))
    if qa.get("outOfPageItemIndexes"):
        issues.append(imposition.blocking_issue("page_bounds_violation", f"小版面对象越界：{qa['outOfPageItemIndexes']}", "请调整自动分页或章节组合"))
    geometry = plan["geometryMm"]
    if abs(float(qa.get("pageWidthPt") or 0) - mm_to_pt(geometry["pageWidth"])) > 0.1 or abs(float(qa.get("pageHeightPt") or 0) - mm_to_pt(geometry["pageHeight"])) > 0.1:
        issues.append(imposition.blocking_issue("invalid_page_size", "小版面尺寸与计划不一致", "请恢复计划中的 pageWidth/pageHeight"))
    if abs(float(qa.get("sheetWidthPt") or 0) - float(plan["sheetWidthPt"])) > 0.1 or abs(float(qa.get("sheetHeightPt") or 0) - float(plan["sheetHeightPt"])) > 0.1:
        issues.append(imposition.blocking_issue("invalid_sheet_size", "横向画板尺寸与模板几何不一致", "请恢复模板留边和横向列宽"))
    return issues


def layout_small_format_job(job: Mapping[str, Any], *, execute: bool = True) -> dict[str, Any]:
    source_ai = Path(str(job["sourceAI"])).expanduser().resolve()
    source_pdf = Path(str(job["sourcePDF"])).expanduser().resolve()
    output_dir = Path(str(job["outputDir"])).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    language = str(job.get("language") or "")
    inventory_path = output_dir / "electronic-inventory.json"
    inspect_jsx = illustrator_worker.write_jsx(imposition.build_inspect_jsx(source_ai, inventory_path), output_dir, "inspect-small-format-")
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "qaContractVersion": QA_CONTRACT_VERSION,
        "runtimeSha256": imposition.sha256(Path(__file__).resolve()),
        "language": language,
        "executed": False,
        "inspectJsx": str(inspect_jsx),
        "qualityIssues": [],
    }
    if not execute:
        return result
    if not source_ai.is_file() or not source_pdf.is_file():
        raise SmallFormatError("Electronic AI/PDF inputs are required")
    illustrator_worker.run_jsx(inspect_jsx, timeout=600)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8-sig"))
    if inventory.get("issues"):
        result.update({"executed": True, "status": "blocked", "inventory": str(inventory_path), "qualityIssues": inventory["issues"]})
        return result
    plan = build_plan(inventory, geometry=(job.get("plan") or {}).get("geometry"))
    output_name = str(job.get("outputName") or f"manual-{language}-小版面")
    outputs = {"ai": output_dir / f"{output_name}.ai", "pdf": output_dir / f"{output_name}.pdf"}
    qa_path = output_dir / "small-format-qa.json"
    manifest_path = output_dir / "small-format-manifest.json"
    layout_jsx = illustrator_worker.write_jsx(build_layout_jsx(source_ai, outputs["ai"], outputs["pdf"], qa_path, plan), output_dir, "layout-small-format-")
    illustrator_worker.run_jsx(layout_jsx, timeout=900)
    missing = [str(path) for path in [*outputs.values(), qa_path] if not path.is_file()]
    if missing:
        raise SmallFormatError(f"Illustrator did not create small-format outputs: {', '.join(missing)}")
    qa = json.loads(qa_path.read_text(encoding="utf-8-sig"))
    issues = validate_qa(qa, plan)
    pdf_meta = imposition._pdf_metadata(outputs["pdf"])
    if pdf_meta.get("pages") != 2:
        issues.append(imposition.blocking_issue("artboard_count_mismatch", f"小版面 PDF 应为上下两行共 2 个横向画板，实际为 {pdf_meta.get('pages')}", "请确保导出上下两个横向画板", language=language))
    if not imposition._pdf_has_zero_bleed(pdf_meta):
        issues.append(imposition.blocking_issue("bleed_detected", "小版面 PDF 检测到非零出血", "请保持零出血并重新生成", language=language))
    manifest = {
        **plan,
        "language": language,
        "createdAt": imposition.utc_now(),
        "source": {"ai": str(source_ai), "aiSha256": imposition.sha256(source_ai), "pdf": str(source_pdf), "pdfSha256": imposition.sha256(source_pdf)},
        "outputs": {kind: {"path": str(path), "sha256": imposition.sha256(path)} for kind, path in outputs.items()},
        "qualityIssues": issues,
    }
    imposition.write_json(manifest_path, manifest)
    artifacts = imposition._artifacts(outputs, qa_path, output_dir / "verify-pages")
    artifacts.append({"type": "small_format_manifest", "path": str(manifest_path), "sha256": imposition.sha256(manifest_path), "metadata": {"bytes": manifest_path.stat().st_size}})
    result.update({
        "executed": True,
        "status": "blocked" if issues else "succeeded",
        "inventory": str(inventory_path),
        "plan": plan,
        "outputs": {key: str(value) for key, value in outputs.items()},
        "qaReport": str(qa_path),
        "manifest": str(manifest_path),
        "artifacts": artifacts,
        "qualityIssues": issues,
        "layoutJsx": str(layout_jsx),
        "pdfMetadata": pdf_meta,
    })
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reflow editable Illustrator content into variable-count small pages")
    parser.add_argument("--source-ai", required=True)
    parser.add_argument("--source-pdf", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--plan")
    parser.add_argument("--language", default="")
    parser.add_argument("--no-execute", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8-sig")) if args.plan else {}
    try:
        result = layout_small_format_job({
            "sourceAI": args.source_ai,
            "sourcePDF": args.source_pdf,
            "outputDir": args.output_dir,
            "language": args.language,
            "plan": plan,
        }, execute=not args.no_execute)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") != "blocked" else 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "type": type(exc).__name__}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
