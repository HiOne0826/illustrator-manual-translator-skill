#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import illustrator_imposition as imposition
import illustrator_worker


SCHEMA = "illustrator-folded-leaflet/1.0"
PLAN_SCHEMA = "folded-leaflet-plan/1.0"
QA_CONTRACT_VERSION = 2
SIDE_NAMES = ("outside", "inside")
DEFAULT_GEOMETRY_MM = {
    "mediaWidth": 390.0,
    "mediaHeight": 174.6,
    "trimLeft": 5.0,
    "trimRight": 5.0,
    "trimTop": 9.19,
    "trimBottom": 9.19,
    "panelCount": 5,
    "minimumBodyFontPt": 5.0,
    "contentTopInset": 5.8,
    "contentBottomInset": 3.7,
    "artboardGap": 31.0,
}


class FoldedLeafletError(RuntimeError):
    pass


def mm_to_pt(value: float) -> float:
    return float(value) * 72.0 / 25.4


def _finite_positive(value: Any, label: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise FoldedLeafletError(f"{label} must be a finite positive number")
    return number


def normalize_geometry(raw: Mapping[str, Any] | None = None) -> dict[str, Any]:
    geometry = {**DEFAULT_GEOMETRY_MM, **dict(raw or {})}
    for key in (
        "mediaWidth", "mediaHeight", "trimLeft", "trimRight", "trimTop", "trimBottom",
        "minimumBodyFontPt", "contentTopInset", "contentBottomInset", "artboardGap",
    ):
        geometry[key] = _finite_positive(geometry[key], key)
    geometry["panelCount"] = int(geometry.get("panelCount") or 0)
    if geometry["panelCount"] != 5:
        raise FoldedLeafletError("Five-fold layout requires exactly 5 panels per side")
    printable_width = geometry["mediaWidth"] - geometry["trimLeft"] - geometry["trimRight"]
    printable_height = geometry["mediaHeight"] - geometry["trimTop"] - geometry["trimBottom"]
    if printable_width <= 0 or printable_height <= 0:
        raise FoldedLeafletError("Trim margins leave no printable panel area")
    geometry["panelWidth"] = printable_width / geometry["panelCount"]
    geometry["panelHeight"] = printable_height
    if geometry["contentTopInset"] + geometry["contentBottomInset"] >= printable_height:
        raise FoldedLeafletError("Content insets leave no usable panel height")
    if abs(geometry["panelWidth"] - 76.0) > 0.05:
        raise FoldedLeafletError("Reference five-fold contract requires 76 mm finished panel width")
    return geometry


def _vertical_distribution(
    item_indexes: Sequence[int],
    items_by_index: Mapping[int, Mapping[str, Any]],
    *,
    source_half_rect: Sequence[float],
    fit_top: float,
    target_top: float,
    panel_height: float,
    scale: float,
    content_top_inset: float,
    content_bottom_inset: float,
) -> tuple[dict[int, float], bool]:
    records: list[tuple[int, list[float]]] = []
    for item_index in item_indexes:
        raw_bounds = items_by_index.get(int(item_index), {}).get("bounds")
        if not isinstance(raw_bounds, list) or len(raw_bounds) != 4:
            continue
        records.append((int(item_index), [float(value) for value in raw_bounds]))
    if not records:
        return ({int(item_index): 0.0 for item_index in item_indexes}, False)

    source_top = float(source_half_rect[1])
    source_bottom = float(source_half_rect[3])
    content_top = min(source_top, max(bounds[1] for _, bounds in records))
    content_bottom = max(source_bottom, min(bounds[3] for _, bounds in records))
    content_height = content_top - content_bottom
    source_height = source_top - source_bottom
    eligible = len(records) >= 2 and content_height >= source_height * 0.4
    if not eligible or content_height <= 0:
        return ({int(item_index): 0.0 for item_index in item_indexes}, False)

    current_top = fit_top - (source_top - content_top) * scale
    current_bottom = fit_top - (source_top - content_bottom) * scale
    desired_top = target_top - content_top_inset
    desired_bottom = target_top - panel_height + content_bottom_inset
    top_shift = desired_top - current_top
    bottom_shift = desired_bottom - current_bottom
    clusters: list[dict[str, Any]] = []
    for item_index, bounds in sorted(records, key=lambda item: item[1][1], reverse=True):
        matching = [
            cluster for cluster in clusters
            if bounds[1] >= float(cluster["bottom"]) - 0.75
            and bounds[3] <= float(cluster["top"]) + 0.75
        ]
        if matching:
            cluster = matching[0]
            cluster["top"] = max(float(cluster["top"]), bounds[1])
            cluster["bottom"] = min(float(cluster["bottom"]), bounds[3])
            cluster["indexes"].append(item_index)
            for other in matching[1:]:
                cluster["top"] = max(float(cluster["top"]), float(other["top"]))
                cluster["bottom"] = min(float(cluster["bottom"]), float(other["bottom"]))
                cluster["indexes"].extend(other["indexes"])
                clusters.remove(other)
        else:
            clusters.append({"top": bounds[1], "bottom": bounds[3], "indexes": [item_index]})

    shifts: dict[int, float] = {}
    for cluster in clusters:
        cluster_height = max(0.0, float(cluster["top"]) - float(cluster["bottom"]))
        travel = content_height - cluster_height
        position = 0.5 if travel <= 0.01 else max(
            0.0, min(1.0, (content_top - float(cluster["top"])) / travel)
        )
        shift = top_shift + (bottom_shift - top_shift) * position
        for item_index in cluster["indexes"]:
            shifts[int(item_index)] = shift
    for item_index in item_indexes:
        shifts.setdefault(int(item_index), 0.0)
    return shifts, True


def _source_slots(artboard_count: int) -> list[dict[str, Any]]:
    return [
        {"sourceArtboard": artboard, "sourceSide": side}
        for artboard in range(artboard_count)
        for side in ("left", "right")
    ]


def default_assignments(artboard_count: int) -> list[dict[str, Any]]:
    if artboard_count <= 0:
        raise FoldedLeafletError("Source AI must contain at least one artboard")
    slots = _source_slots(artboard_count)
    if len(slots) > 10:
        raise FoldedLeafletError("Five-fold output has only 10 panels; provide an editorial reflow plan for larger sources")
    assignments: list[dict[str, Any]] = []
    for index in range(10):
        assignment = {
            "targetSide": SIDE_NAMES[index // 5],
            "targetPanel": index % 5,
        }
        if index < len(slots):
            assignment.update(slots[index])
        assignments.append(assignment)
    return assignments


def normalize_assignments(raw: Sequence[Mapping[str, Any]] | None, artboard_count: int) -> list[dict[str, Any]]:
    assignments = [dict(item) for item in (raw or default_assignments(artboard_count))]
    if len(assignments) != 10:
        raise FoldedLeafletError("Five-fold plan must define exactly 10 target panels")
    targets: set[tuple[str, int]] = set()
    source_bands: dict[tuple[int, str], list[tuple[float, float]]] = {}
    normalized: list[dict[str, Any]] = []
    for item in assignments:
        side = str(item.get("targetSide") or "")
        panel = int(item.get("targetPanel", -1))
        if side not in SIDE_NAMES or panel not in range(5):
            raise FoldedLeafletError(f"Invalid target panel: {side}:{panel}")
        target_key = (side, panel)
        if target_key in targets:
            raise FoldedLeafletError(f"Duplicate target panel: {side}:{panel}")
        targets.add(target_key)
        result: dict[str, Any] = {"targetSide": side, "targetPanel": panel, "blank": True}
        if item.get("sourceArtboard") is not None:
            source_artboard = int(item["sourceArtboard"])
            source_side = str(item.get("sourceSide") or "")
            if source_artboard not in range(artboard_count) or source_side not in ("left", "right"):
                raise FoldedLeafletError(f"Invalid source half-page: {source_artboard}:{source_side}")
            raw_band = item.get("sourceBand") or [0.0, 1.0]
            if not isinstance(raw_band, Sequence) or isinstance(raw_band, (str, bytes)) or len(raw_band) != 2:
                raise FoldedLeafletError("sourceBand must contain normalized start/end values")
            band = (float(raw_band[0]), float(raw_band[1]))
            if not all(math.isfinite(value) for value in band) or band[0] < 0 or band[1] > 1 or band[0] >= band[1]:
                raise FoldedLeafletError("sourceBand must satisfy 0 <= start < end <= 1")
            source_key = (source_artboard, source_side)
            source_bands.setdefault(source_key, []).append(band)
            result.update({
                "sourceArtboard": source_artboard,
                "sourceSide": source_side,
                "sourceBand": [band[0], band[1]],
                "blank": False,
            })
        normalized.append(result)
    expected_targets = {(side, panel) for side in SIDE_NAMES for panel in range(5)}
    if targets != expected_targets:
        raise FoldedLeafletError("Target panels must cover outside/inside panels 0 through 4 exactly once")
    expected_sources = {(item["sourceArtboard"], item["sourceSide"]) for item in _source_slots(artboard_count)}
    if set(source_bands) != expected_sources:
        missing = sorted(expected_sources - set(source_bands))
        raise FoldedLeafletError(f"Every source half-page must be assigned exactly once; missing: {missing}")
    for source_key, bands in source_bands.items():
        ordered = sorted(bands)
        cursor = 0.0
        for start, end in ordered:
            if abs(start - cursor) > 0.000001:
                raise FoldedLeafletError(f"Source half-page bands overlap or leave a gap: {source_key}")
            cursor = end
        if abs(cursor - 1.0) > 0.000001:
            raise FoldedLeafletError(f"Source half-page bands must cover 0 through 1: {source_key}")
    return sorted(normalized, key=lambda item: (SIDE_NAMES.index(item["targetSide"]), item["targetPanel"]))


def build_plan(
    inventory: Mapping[str, Any],
    *,
    geometry: Mapping[str, Any] | None = None,
    assignments: Sequence[Mapping[str, Any]] | None = None,
    print_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    artboard_count = int(inventory.get("artboardCount") or 0)
    boards = list(inventory.get("artboards") or [])
    halves = list(inventory.get("halves") or [])
    if artboard_count <= 0 or len(boards) != artboard_count or len(halves) != artboard_count * 2:
        raise FoldedLeafletError("Illustrator inventory has incomplete artboard or half-page data")
    normalized_geometry = normalize_geometry(geometry)
    normalized_assignments = normalize_assignments(assignments, artboard_count)
    half_lookup = {(int(item["artboardIndex"]), str(item["side"])): item for item in halves}
    board_lookup = {int(item["index"]): item for item in boards}
    media_width_pt = mm_to_pt(normalized_geometry["mediaWidth"])
    media_height_pt = mm_to_pt(normalized_geometry["mediaHeight"])
    trim_left_pt = mm_to_pt(normalized_geometry["trimLeft"])
    trim_top_pt = mm_to_pt(normalized_geometry["trimTop"])
    panel_width_pt = mm_to_pt(normalized_geometry["panelWidth"])
    panel_height_pt = mm_to_pt(normalized_geometry["panelHeight"])
    content_top_inset_pt = mm_to_pt(normalized_geometry["contentTopInset"])
    content_bottom_inset_pt = mm_to_pt(normalized_geometry["contentBottomInset"])
    side_gap_pt = mm_to_pt(normalized_geometry["artboardGap"])
    artboard_tops = [-(media_height_pt + side_gap_pt), 0.0]
    items_by_index = {int(item["index"]): item for item in inventory.get("items") or []}
    movements: list[dict[str, Any]] = []
    panels: list[dict[str, Any]] = []
    for item in normalized_assignments:
        side_index = SIDE_NAMES.index(item["targetSide"])
        board_top = artboard_tops[side_index]
        target_left = trim_left_pt + item["targetPanel"] * panel_width_pt
        target_top = board_top - trim_top_pt
        panel = {
            **item,
            "targetArtboard": side_index,
            "targetRect": [target_left, target_top, target_left + panel_width_pt, target_top - panel_height_pt],
        }
        panels.append(panel)
        if item["blank"]:
            continue
        half = half_lookup[(item["sourceArtboard"], item["sourceSide"])]
        source_board = board_lookup[item["sourceArtboard"]]
        source_rect = [float(value) for value in source_board["rect"]]
        source_mid = (source_rect[0] + source_rect[2]) / 2.0
        source_full_half_rect = [
            source_mid if item["sourceSide"] == "right" else source_rect[0],
            source_rect[1],
            source_rect[2] if item["sourceSide"] == "right" else source_mid,
            source_rect[3],
        ]
        band_start, band_end = [float(value) for value in item.get("sourceBand") or [0.0, 1.0]]
        full_source_height = source_full_half_rect[1] - source_full_half_rect[3]
        source_half_rect = [
            source_full_half_rect[0],
            source_full_half_rect[1] - full_source_height * band_start,
            source_full_half_rect[2],
            source_full_half_rect[1] - full_source_height * band_end,
        ]
        source_width = source_half_rect[2] - source_half_rect[0]
        source_height = source_half_rect[1] - source_half_rect[3]
        scale = min(panel_width_pt / source_width, panel_height_pt / source_height)
        fitted_width = source_width * scale
        fitted_height = source_height * scale
        fit_left = target_left + (panel_width_pt - fitted_width) / 2.0
        fit_top = target_top - (panel_height_pt - fitted_height) / 2.0
        item_indexes: list[int] = []
        for raw_index in half.get("itemIndexes") or []:
            item_index = int(raw_index)
            if band_start <= 0 and band_end >= 1:
                item_indexes.append(item_index)
                continue
            raw_bounds = items_by_index.get(item_index, {}).get("bounds")
            if not isinstance(raw_bounds, list) or len(raw_bounds) != 4:
                raise FoldedLeafletError(f"Split source band requires object bounds: {item_index}")
            bounds = [float(value) for value in raw_bounds]
            for boundary in (band_start, band_end):
                if boundary <= 0 or boundary >= 1:
                    continue
                boundary_y = source_full_half_rect[1] - full_source_height * boundary
                if bounds[1] > boundary_y + 0.75 and bounds[3] < boundary_y - 0.75:
                    raise FoldedLeafletError(
                        f"Source object crosses semantic band boundary: item {item_index} at {boundary}"
                    )
            center_ratio = (source_full_half_rect[1] - (bounds[1] + bounds[3]) / 2.0) / full_source_height
            if center_ratio >= band_start - 0.000001 and (
                center_ratio < band_end - 0.000001 or abs(band_end - 1.0) <= 0.000001
            ):
                item_indexes.append(item_index)
        if not item_indexes:
            raise FoldedLeafletError(
                f"Source band contains no native objects: {item['sourceArtboard']}:{item['sourceSide']}:{band_start}-{band_end}"
            )
        vertical_shifts, vertical_fill_expected = _vertical_distribution(
            item_indexes,
            items_by_index,
            source_half_rect=source_half_rect,
            fit_top=fit_top,
            target_top=target_top,
            panel_height=panel_height_pt,
            scale=scale,
            content_top_inset=content_top_inset_pt,
            content_bottom_inset=content_bottom_inset_pt,
        )
        panel["verticalFillExpected"] = vertical_fill_expected
        for item_index in item_indexes:
            movements.append({
                "itemIndex": int(item_index),
                "sourceArtboard": item["sourceArtboard"],
                "sourceSide": item["sourceSide"],
                "targetArtboard": side_index,
                "targetPanel": item["targetPanel"],
                "sourceRect": source_half_rect,
                "fitRect": [fit_left, fit_top, fit_left + fitted_width, fit_top - fitted_height],
                "targetPanelRect": panel["targetRect"],
                "scale": scale,
                "verticalShiftPt": vertical_shifts[item_index],
                "verticalFillExpected": vertical_fill_expected,
            })
    mapped = {item["itemIndex"] for item in movements}
    printable = {
        int(item["index"])
        for item in inventory.get("items") or []
        if int(item["index"]) not in {int(value) for value in inventory.get("preservedItemIndexes") or []}
    }
    if mapped != printable:
        raise FoldedLeafletError(f"Printable top-level object mapping is incomplete; unmapped={sorted(printable - mapped)}")
    raw_print_profile = dict(print_profile or {})
    duplex_flip = str(raw_print_profile.get("duplexFlip") or "unconfirmed")
    if duplex_flip not in ("unconfirmed", "long-edge", "short-edge"):
        raise FoldedLeafletError("duplexFlip must be long-edge, short-edge, or unconfirmed")
    source_guide_indexes = [
        int(item["index"])
        for item in inventory.get("items") or []
        if bool((item.get("details") or {}).get("guides"))
    ]
    preserved_indexes = [int(value) for value in inventory.get("preservedItemIndexes") or []]
    return {
        "schema": PLAN_SCHEMA,
        "geometryMm": normalized_geometry,
        "mediaSizePt": [media_width_pt, media_height_pt],
        "sideGapPt": side_gap_pt,
        "artboardTops": artboard_tops,
        "panels": panels,
        "movements": movements,
        "preserveItemIndexes": [value for value in preserved_indexes if value not in source_guide_indexes],
        "replaceGuideItemIndexes": source_guide_indexes,
        "sourceArtboardCount": artboard_count,
        "minimumScale": min((item["scale"] for item in movements), default=1.0),
        "printProfile": {
            "duplexFlip": duplex_flip,
            "scalePercent": 100,
            "centered": True,
            "shrinkToFit": False,
            "bleedPt": 0,
        },
    }


def build_layout_jsx(source_ai: Path, output_ai: Path, output_pdf: Path, qa_path: Path, plan: Mapping[str, Any]) -> str:
    geometry = plan["geometryMm"]
    media_width_pt, media_height_pt = plan["mediaSizePt"]
    panel_width_pt = mm_to_pt(geometry["panelWidth"])
    panel_height_pt = mm_to_pt(geometry["panelHeight"])
    trim_left_pt = mm_to_pt(geometry["trimLeft"])
    trim_top_pt = mm_to_pt(geometry["trimTop"])
    minimum_font = float(geometry["minimumBodyFontPt"])
    mark_length_pt = mm_to_pt(3.0)
    mark_gap_pt = mm_to_pt(2.0)
    movements = list(plan.get("movements") or [])
    preserved = list(plan.get("preserveItemIndexes") or [])
    return fr'''#target illustrator
(function () {{
  function q(value) {{ var s=String(value===undefined||value===null?"":value); return '"'+s.replace(/\\/g,"\\\\").replace(/"/g,'\\"').replace(/\r/g,"\\r").replace(/\n/g,"\\n")+'"'; }}
  function write(path,content) {{ var f=new File(path); f.encoding="UTF-8"; if(!f.open("w")) throw new Error("Cannot write folded leaflet QA: "+path); f.write(content); f.close(); }}
  function topItems(doc) {{ var out=[]; for(var i=0;i<doc.pageItems.length;i++) {{ var item=doc.pageItems[i]; try {{ if(item.parent&&item.parent.typename==="Layer") out.push(item); }} catch(e) {{}} }} return out; }}
  function unlockParents(item) {{ var current=item; while(current&&current.typename!=="Document") {{ try {{ current.locked=false; }} catch(e) {{}} try {{ current.visible=true; }} catch(e2) {{}} current=current.parent; }} }}
  function line(layer,x1,y1,x2,y2) {{ var item=layer.pathItems.add(); item.setEntirePath([[x1,y1],[x2,y2]]); item.stroked=true; item.filled=false; item.strokeWidth=0.25; item.strokeColor=markColor; return item; }}
  function guideRect(layer,left,top,right,bottom) {{ var item=layer.pathItems.add(); item.setEntirePath([[right,bottom],[left,bottom],[left,top],[right,top]]); item.closed=true; item.stroked=false; item.filled=false; item.guides=true; return item; }}
  var source=new File({imposition._jsx(source_ai)}), outputAI=new File({imposition._jsx(output_ai)}), outputPDF=new File({imposition._jsx(output_pdf)});
  if(!source.exists) throw new Error("Source AI not found: "+source.fsName);
  app.userInteractionLevel=UserInteractionLevel.DONTDISPLAYALERTS;
  var doc=app.open(source), sourceText=doc.textFrames.length, sourcePlaced=doc.placedItems.length, sourceRaster=doc.rasterItems.length, sourceGroups=doc.groupItems.length, sourceLayers=doc.layers.length;
  try {{
    var top=topItems(doc), moves={json.dumps(movements, ensure_ascii=False)}, preserve={json.dumps(preserved)}, replaceGuides={json.dumps(list(plan.get('replaceGuideItemIndexes') or []))}, seen={{}};
    if(top.length===0) throw new Error("No editable top-level Illustrator objects found");
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
    for(var p=0;p<preserve.length;p++) {{ var idx=Number(preserve[p]); if(!top[idx]) throw new Error("Preserved item index changed: "+idx); if(seen[String(idx)]) throw new Error("Mapped item cannot also be preserved: "+idx); seen[String(idx)]=true; }}
    for(var oldGuide=0;oldGuide<replaceGuides.length;oldGuide++) {{ var guideIndex=Number(replaceGuides[oldGuide]); if(!top[guideIndex]) throw new Error("Source guide index changed: "+guideIndex); if(seen[String(guideIndex)]) throw new Error("Source guide cannot also be mapped: "+guideIndex); seen[String(guideIndex)]=true; }}
    for(var n=0;n<top.length;n++) if(!seen[String(n)]) throw new Error("Unmapped editable object remains: "+n);
    for(var removeGuide=0;removeGuide<replaceGuides.length;removeGuide++) {{ unlockParents(top[Number(replaceGuides[removeGuide])]); top[Number(replaceGuides[removeGuide])].remove(); }}

    var mediaW={float(media_width_pt)}, mediaH={float(media_height_pt)}, gap={float(plan['sideGapPt'])}, boardTops={json.dumps(list(plan['artboardTops']))};
    while(doc.artboards.length<2) doc.artboards.add([0,-(doc.artboards.length)*(mediaH+gap),mediaW,-(doc.artboards.length)*(mediaH+gap)-mediaH]);
    doc.artboards[0].artboardRect=[0,Number(boardTops[0]),mediaW,Number(boardTops[0])-mediaH];
    doc.artboards[1].artboardRect=[0,Number(boardTops[1]),mediaW,Number(boardTops[1])-mediaH];
    for(var removeIndex=doc.artboards.length-1;removeIndex>=2;removeIndex--) doc.artboards.remove(removeIndex);
    doc.artboards[0].name="FIVE-FOLD-OUTSIDE"; doc.artboards[1].name="FIVE-FOLD-INSIDE";

    var minFont={minimum_font}, raisedFonts=0, overset=[];
    for(var t=0;t<doc.textFrames.length;t++) {{
      var frame=doc.textFrames[t], attrs=frame.textRange.characterAttributes, size=0;
      try {{ size=Number(attrs.size); }} catch(sizeError) {{ size=0; }}
      if(size>0&&size<minFont) {{ try {{ attrs.size=minFont; raisedFonts++; }} catch(fontError) {{}} }}
      try {{ if(frame.kind===TextType.AREATEXT&&frame.overflows) overset.push(t); }} catch(overflowError) {{}}
    }}
    var outOfPanel=[], panelContent={{}};
    for(var checkIndex=0;checkIndex<moves.length;checkIndex++) {{
      var checkedMove=moves[checkIndex], checkedItem=top[Number(checkedMove.itemIndex)], visible=checkedItem.visibleBounds, panelRect=checkedMove.targetPanelRect, tolerance=0.75;
      if(Number(visible[0])<Number(panelRect[0])-tolerance||Number(visible[1])>Number(panelRect[1])+tolerance||Number(visible[2])>Number(panelRect[2])+tolerance||Number(visible[3])<Number(panelRect[3])-tolerance) outOfPanel.push(Number(checkedMove.itemIndex));
      var panelKey=String(checkedMove.targetArtboard)+":"+String(checkedMove.targetPanel), metric=panelContent[panelKey];
      if(!metric) metric=panelContent[panelKey]={{targetArtboard:Number(checkedMove.targetArtboard),targetPanel:Number(checkedMove.targetPanel),panelTop:Number(panelRect[1]),panelBottom:Number(panelRect[3]),contentTop:Number(visible[1]),contentBottom:Number(visible[3]),verticalFillExpected:Boolean(checkedMove.verticalFillExpected)}};
      else {{ metric.contentTop=Math.max(metric.contentTop,Number(visible[1])); metric.contentBottom=Math.min(metric.contentBottom,Number(visible[3])); metric.verticalFillExpected=metric.verticalFillExpected||Boolean(checkedMove.verticalFillExpected); }}
    }}
    var metricParts=[];
    for(var metricKey in panelContent) if(panelContent.hasOwnProperty(metricKey)) {{
      var panelMetric=panelContent[metricKey], topBlank=Math.max(0,panelMetric.panelTop-panelMetric.contentTop), bottomBlank=Math.max(0,panelMetric.contentBottom-panelMetric.panelBottom);
      metricParts.push('{{"targetArtboard":'+panelMetric.targetArtboard+',"targetPanel":'+panelMetric.targetPanel+',"topBlankPt":'+topBlank+',"bottomBlankPt":'+bottomBlank+',"verticalFillExpected":'+(panelMetric.verticalFillExpected?'true':'false')+'}}');
    }}

    var guides=doc.layers.add(); guides.name="FIVE_FOLD_GUIDES"; guides.printable=true; guides.visible=true;
    var guideCount=0;
    for(var guideSide=0;guideSide<2;guideSide++) {{
      var guideTop=Number(boardTops[guideSide])-{trim_top_pt}, guideBottom=guideTop-{panel_height_pt};
      for(var guidePanel=0;guidePanel<5;guidePanel++) {{ var guideLeft={trim_left_pt}+guidePanel*{panel_width_pt}; guideRect(guides,guideLeft,guideTop,guideLeft+{panel_width_pt},guideBottom); guideCount++; }}
    }}

    var marks=doc.layers.add(); marks.name="FIVE_FOLD_PRINT_MARKS"; marks.printable=true;
    var markColor=new CMYKColor(); markColor.cyan=0; markColor.magenta=0; markColor.yellow=0; markColor.black=100;
    var trimLeft={trim_left_pt}, trimTop={trim_top_pt}, panelW={panel_width_pt}, panelH={panel_height_pt}, markLength={mark_length_pt}, markGap={mark_gap_pt}, markCount=0;
    for(var side=0;side<2;side++) {{
      var boardTop=Number(boardTops[side]), topY=boardTop-trimTop, bottomY=topY-panelH;
      for(var boundary=0;boundary<=5;boundary++) {{
        var x=trimLeft+boundary*panelW;
        line(marks,x,topY+markGap+markLength,x,topY+markGap); line(marks,x,bottomY-markGap,x,bottomY-markGap-markLength); markCount+=2;
      }}
      line(marks,0,topY,trimLeft-markGap,topY); line(marks,0,bottomY,trimLeft-markGap,bottomY);
      line(marks,trimLeft+5*panelW+markGap,topY,mediaW,topY); line(marks,trimLeft+5*panelW+markGap,bottomY,mediaW,bottomY); markCount+=4;
    }}

    var aiOptions=new IllustratorSaveOptions(); aiOptions.pdfCompatible=true; doc.saveAs(outputAI,aiOptions);
    var pdfOptions=new PDFSaveOptions(); pdfOptions.preserveEditability=true; pdfOptions.viewAfterSaving=false;
    try {{ pdfOptions.saveMultipleArtboards=true; pdfOptions.artboardRange="1-2"; pdfOptions.bleedOffsetRect=[0,0,0,0]; pdfOptions.bleedLink=false; }} catch(pdfError) {{}}
    try {{ pdfOptions.trimMarks=false; pdfOptions.registrationMarks=false; pdfOptions.colorBars=false; pdfOptions.pageInformation=false; }} catch(markError) {{}}
    doc.saveAs(outputPDF,pdfOptions);
    var outputTop=topItems(doc).length, editable=(doc.artboards.length===2&&outputTop===top.length-replaceGuides.length+markCount+guideCount&&doc.textFrames.length===sourceText&&doc.placedItems.length===sourcePlaced&&doc.rasterItems.length===sourceRaster&&doc.groupItems.length===sourceGroups&&doc.layers.length===sourceLayers+2);
    write({imposition._jsx(qa_path)}, '{{"schema":"{SCHEMA}","qaContractVersion":{QA_CONTRACT_VERSION},"variant":"FIVE_FOLD","artboardCount":'+doc.artboards.length+',"panelCountPerSide":5,"mediaWidthPt":'+mediaW+',"mediaHeightPt":'+mediaH+',"artboardGapPt":'+gap+',"minimumBodyFontPt":'+minFont+',"raisedFontFrameCount":'+raisedFonts+',"oversetTextFrameIndexes":['+overset.join(',')+'],"outOfPanelItemIndexes":['+outOfPanel.join(',')+'],"panelContentMetrics":['+metricParts.join(',')+'],"sourceTopLevelItems":'+top.length+',"mappedTopLevelItems":'+moves.length+',"preservedTopLevelItems":'+preserve.length+',"replacedSourceGuideCount":'+replaceGuides.length+',"guideRectangleCount":'+guideCount+',"outputTopLevelItems":'+outputTop+',"printMarkCount":'+markCount+',"editableObjectsPreserved":'+(editable?'true':'false')+',"bleedPt":0}}');
  }} finally {{ doc.close(SaveOptions.DONOTSAVECHANGES); }}
}}());
'''


def validate_qa(qa: Mapping[str, Any], geometry: Mapping[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if int(qa.get("artboardCount") or 0) != 2 or int(qa.get("panelCountPerSide") or 0) != 5:
        issues.append(imposition.blocking_issue("fold_geometry_invalid", "五折页必须包含 2 个画板且每面 5 个面板", "请重新运行五折页排版，不要手动增删画板"))
    if not qa.get("editableObjectsPreserved"):
        issues.append(imposition.blocking_issue("editability_lost", "五折页输出的文字、矢量、图片或图层计数不一致", "请从已确认电子版 AI 重新生成，不要置入或栅格化整页 PDF"))
    if qa.get("oversetTextFrameIndexes"):
        issues.append(imposition.blocking_issue("text_overflow", f"五折页存在溢出文本框：{qa['oversetTextFrameIndexes']}", "请编辑内容或调整面板语义重排规则；不得继续缩小低于最小正文字号"))
    if qa.get("outOfPanelItemIndexes"):
        issues.append(imposition.blocking_issue("panel_bounds_violation", f"五折页对象超出所属成品面板：{qa['outOfPanelItemIndexes']}", "请调整面板映射或执行语义重排，避免内容跨越裁切线或折线"))
    if int(qa.get("guideRectangleCount") or 0) != 10:
        issues.append(imposition.blocking_issue(
            "reference_guides_invalid",
            "五折页必须包含与参考模板一致的 10 个闭合面板参考线矩形",
            "请移除 A4 源参考线，并按每面 5 个面板重新生成 Illustrator guides",
        ))
    expected_gap = mm_to_pt(geometry["artboardGap"])
    if abs(float(qa.get("artboardGapPt") or 0) - expected_gap) > 0.1:
        issues.append(imposition.blocking_issue(
            "reference_guides_invalid",
            "五折页两画板间距与参考模板不一致",
            "请恢复 31 mm 画板间距后重新生成",
        ))
    excessive_blank = [
        f"{int(item.get('targetArtboard', -1))}:{int(item.get('targetPanel', -1))}"
        for item in qa.get("panelContentMetrics") or []
        if item.get("verticalFillExpected") and (
            float(item.get("topBlankPt") or 0) > mm_to_pt(12)
            or float(item.get("bottomBlankPt") or 0) > mm_to_pt(12)
        )
    ]
    if excessive_blank:
        issues.append(imposition.blocking_issue(
            "excessive_vertical_whitespace",
            f"五折页有效内容面板上下留白超过参考阈值：{excessive_blank}",
            "请检查内容边界识别和纵向分布；不要退回按整块 A4 半页缩放",
        ))
    expected_width = mm_to_pt(geometry["mediaWidth"])
    expected_height = mm_to_pt(geometry["mediaHeight"])
    if abs(float(qa.get("mediaWidthPt") or 0) - expected_width) > 0.1 or abs(float(qa.get("mediaHeightPt") or 0) - expected_height) > 0.1:
        issues.append(imposition.blocking_issue("fold_geometry_invalid", "五折页画板尺寸与 390×174.6 mm 合同不一致", "请恢复标准五折页 geometry 后重新生成"))
    return issues


def layout_leaflet_job(job: Mapping[str, Any], *, execute: bool = True) -> dict[str, Any]:
    source_ai = Path(str(job["sourceAI"])).expanduser().resolve()
    source_pdf = Path(str(job["sourcePDF"])).expanduser().resolve()
    output_dir = Path(str(job["outputDir"])).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    language = str(job.get("language") or "")
    geometry = normalize_geometry(job.get("geometry"))
    inventory_path = output_dir / "electronic-inventory.json"
    inspect_jsx = illustrator_worker.write_jsx(
        imposition.build_inspect_jsx(source_ai, inventory_path), output_dir, "inspect-five-fold-"
    )
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
        raise FoldedLeafletError("Electronic AI/PDF inputs are required")
    illustrator_worker.run_jsx(inspect_jsx, timeout=600)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8-sig"))
    raw_issues = list(inventory.get("issues") or [])
    if raw_issues:
        result.update({
            "executed": True,
            "status": "blocked",
            "inventory": str(inventory_path),
            "qualityIssues": [imposition.blocking_issue(
                str(item.get("type") or "page_unrecognized"),
                str(item.get("message") or "电子版对象无法分配到明确半页"),
                "请修复跨中线或画板外对象后重新生成电子版",
                language=language,
                source_artboard=item.get("artboardIndex"),
                side=str(item.get("side") or ""),
            ) for item in raw_issues],
        })
        return result
    plan_payload = job.get("plan") or {}
    plan = build_plan(
        inventory,
        geometry=plan_payload.get("geometry") or geometry,
        assignments=plan_payload.get("assignments"),
        print_profile=plan_payload.get("printProfile"),
    )
    output_name = str(job.get("outputName") or f"manual-{language}-五折页")
    outputs = {"ai": output_dir / f"{output_name}.ai", "pdf": output_dir / f"{output_name}.pdf"}
    qa_path = output_dir / "folded-leaflet-qa.json"
    manifest_path = output_dir / "folded-leaflet-manifest.json"
    layout_jsx = illustrator_worker.write_jsx(build_layout_jsx(source_ai, outputs["ai"], outputs["pdf"], qa_path, plan), output_dir, "layout-five-fold-")
    illustrator_worker.run_jsx(layout_jsx, timeout=900)
    missing = [str(path) for path in [*outputs.values(), qa_path] if not path.is_file()]
    if missing:
        raise FoldedLeafletError(f"Illustrator did not create folded leaflet outputs: {', '.join(missing)}")
    qa = json.loads(qa_path.read_text(encoding="utf-8-sig"))
    issues = validate_qa(qa, plan["geometryMm"])
    if plan["printProfile"]["duplexFlip"] == "unconfirmed":
        issues.append(imposition.blocking_issue(
            "print_contract_unconfirmed",
            "五折页正反面翻转方向尚未确认",
            "请与印厂确认 long-edge 或 short-edge，并在 plan.printProfile.duplexFlip 中明确填写后重新生成",
            language=language,
        ))
    pdf_meta = imposition._pdf_metadata(outputs["pdf"])
    if pdf_meta.get("pages") != 2:
        issues.append(imposition.blocking_issue("artboard_count_mismatch", f"五折页 PDF 应为 2 页，实际为 {pdf_meta.get('pages')}", "请确保 PDF 导出全部两个五折页画板", language=language))
    if not imposition._pdf_has_zero_bleed(pdf_meta):
        issues.append(imposition.blocking_issue("bleed_detected", "五折页 PDF 的 MediaBox/CropBox/BleedBox/TrimBox 不一致", "请保持 390×174.6 mm 媒体尺寸和零出血；裁切线应作为画板内原生矢量对象", language=language))
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
    artifacts.append({"type": "folded_leaflet_manifest", "path": str(manifest_path), "sha256": imposition.sha256(manifest_path), "metadata": {"bytes": manifest_path.stat().st_size}})
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
    parser = argparse.ArgumentParser(description="Create an editable 5-panel, 2-sided Illustrator leaflet")
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
        result = layout_leaflet_job({
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
