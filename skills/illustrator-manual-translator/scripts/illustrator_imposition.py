#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import illustrator_worker


SCHEMA = "illustrator-imposition/1.0"
QA_CONTRACT_VERSION = 2
PAGE_NUMBER_RE = re.compile(r"^\s*[-–—]?\s*(\d+)\s*[-–—]?\s*$")


class ImpositionError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _jsx(value: str | Path) -> str:
    if isinstance(value, Path):
        value = value.resolve().as_uri()
    return json.dumps(str(value), ensure_ascii=False)


def blocking_issue(
    issue_type: str,
    message: str,
    user_action: str,
    *,
    language: str = "",
    source_artboard: int | None = None,
    side: str = "",
    logical_page: str | int = "",
    object_ref: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "level": "blocking",
        "type": issue_type,
        "language": language,
        "message": message,
        "userAction": user_action,
        "status": "open",
    }
    if source_artboard is not None:
        issue["sourceArtboard"] = source_artboard
    if side:
        issue["side"] = side
    if logical_page != "":
        issue["logicalPage"] = logical_page
    if object_ref:
        issue["objectRef"] = dict(object_ref)
    return issue


def _cover_policy(layout_rules: Mapping[str, Any] | None) -> dict[str, Any]:
    policy = dict((layout_rules or {}).get("impositionPolicy") or {})
    cover = dict(policy.get("cover") or {})
    return {
        "artboardIndex": int(cover.get("artboardIndex", 0)),
        "left": str(cover.get("left") or "back_cover"),
        "right": str(cover.get("right") or "front_cover"),
        "midlineTolerancePt": float(policy.get("midlineTolerancePt", 0.5)),
        "edgeTolerancePt": float(policy.get("edgeTolerancePt", 0.75)),
    }


def build_inspect_jsx(
    source_ai: Path,
    inventory_path: Path,
    *,
    midline_tolerance: float = 0.5,
    edge_tolerance: float = 0.75,
) -> str:
    return fr'''#target illustrator
(function () {{
  function q(value) {{
    var s = String(value === undefined || value === null ? "" : value);
    return '"' + s.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\r/g, "\\r").replace(/\n/g, "\\n") + '"';
  }}
  function arr(values) {{ var out=[]; for(var i=0;i<values.length;i++) out.push(Number(values[i])); return '['+out.join(',')+']'; }}
  function write(path, content) {{ var f=new File(path); f.encoding="UTF-8"; if(!f.open("w")) throw new Error("Cannot write inventory: "+path); f.write(content); f.close(); }}
  function topItems(doc) {{
    var out=[];
    for(var i=0;i<doc.pageItems.length;i++) {{
      var item=doc.pageItems[i];
      try {{ if(item.parent && item.parent.typename === "Layer") out.push(item); }} catch(e) {{}}
    }}
    return out;
  }}
  function layerPath(item) {{
    var names=[], current=item.parent;
    while(current && current.typename !== "Document") {{
      if(current.typename === "Layer") names.unshift(String(current.name || ""));
      current=current.parent;
    }}
    return names.join("/");
  }}
  function prop(item, name, fallback) {{ try {{ return item[name]; }} catch(e) {{ return fallback; }} }}
  function itemDetails(item) {{
    var geometric=prop(item,"geometricBounds",item.visibleBounds), visible=item.visibleBounds;
    return {{
      geometricBounds:[Number(geometric[0]),Number(geometric[1]),Number(geometric[2]),Number(geometric[3])],
      visibleBounds:[Number(visible[0]),Number(visible[1]),Number(visible[2]),Number(visible[3])],
      hidden:Boolean(prop(item,"hidden",false)), guides:Boolean(prop(item,"guides",false)),
      filled:Boolean(prop(item,"filled",false)), stroked:Boolean(prop(item,"stroked",false)),
      clipping:Boolean(prop(item,"clipping",false)), clipped:Boolean(prop(item,"clipped",false)),
      layerVisible:Boolean(prop(item.layer,"visible",true)), layerPrintable:Boolean(prop(item.layer,"printable",true))
    }};
  }}
  function assign(bounds, boards) {{
    var cx=(Number(bounds[0])+Number(bounds[2]))/2, cy=(Number(bounds[1])+Number(bounds[3]))/2;
    for(var i=0;i<boards.length;i++) {{
      var r=boards[i].rect;
      if(cx>=r[0]-{edge_tolerance} && cx<=r[2]+{edge_tolerance} && cy<=r[1]+{edge_tolerance} && cy>=r[3]-{edge_tolerance}) {{
        var mid=(r[0]+r[2])/2;
        return {{artboardIndex:i,side:cx<mid?"left":"right",midX:mid}};
      }}
    }}
    return null;
  }}
  var source=new File({_jsx(source_ai)});
  if(!source.exists) throw new Error("Source AI not found: "+source.fsName);
  app.userInteractionLevel=UserInteractionLevel.DONTDISPLAYALERTS;
  var doc=app.open(source), boards=[], halves=[], items=[], markers=[], issues=[], preserved=[];
  try {{
    for(var a=0;a<doc.artboards.length;a++) {{
      var rect=doc.artboards[a].artboardRect;
      boards.push({{index:a,name:String(doc.artboards[a].name||""),rect:[Number(rect[0]),Number(rect[1]),Number(rect[2]),Number(rect[3])]}});
      halves.push({{artboardIndex:a,side:"left",itemIndexes:[],pageNumbers:[]}});
      halves.push({{artboardIndex:a,side:"right",itemIndexes:[],pageNumbers:[]}});
    }}
    var top=topItems(doc);
    for(var i=0;i<top.length;i++) {{
      var item=top[i], details=itemDetails(item), bounds=details.geometricBounds, assigned=assign(bounds,boards), ref={{index:i,name:String(item.name||""),typename:String(item.typename||""),layerPath:layerPath(item),bounds:bounds,details:details}};
      items.push(ref);
      if(details.guides || details.hidden || !details.layerVisible || !details.layerPrintable) {{ preserved.push(i); continue; }}
      if(!assigned) {{ issues.push({{type:"page_unrecognized",itemIndex:i,message:"Object is outside every artboard"}}); continue; }}
      var rect=boards[assigned.artboardIndex].rect;
      if(Number(bounds[0]) < assigned.midX-{midline_tolerance} && Number(bounds[2]) > assigned.midX+{midline_tolerance}) issues.push({{type:"centerline_crossing",itemIndex:i,artboardIndex:assigned.artboardIndex,side:assigned.side,message:"Object crosses the page centerline"}});
      halves[assigned.artboardIndex*2+(assigned.side==="right"?1:0)].itemIndexes.push(i);
    }}
    for(var t=0;t<doc.textFrames.length;t++) {{
      var frame=doc.textFrames[t], tagged=String(frame.name||"")==="__layout_fixed_page_number__";
      if(!tagged) continue;
      var text=String(frame.contents||""), match=text.match(/^\s*[-–—]?\s*(\d+)\s*[-–—]?\s*$/);
      var bounds=frame.visibleBounds, assigned=assign(bounds,boards);
      if(!assigned || !match) {{ issues.push({{type:"page_unrecognized",textFrameIndex:t,message:"Fixed page-number frame could not be parsed"}}); continue; }}
      var number=Number(match[1]);
      halves[assigned.artboardIndex*2+(assigned.side==="right"?1:0)].pageNumbers.push(number);
      markers.push({{textFrameIndex:t,artboardIndex:assigned.artboardIndex,side:assigned.side,number:number,contents:text}});
    }}
    var boardParts=[],halfParts=[],itemParts=[],markerParts=[],issueParts=[];
    for(var b=0;b<boards.length;b++) boardParts.push('{{"index":'+boards[b].index+',"name":'+q(boards[b].name)+',"rect":'+arr(boards[b].rect)+'}}');
    for(var h=0;h<halves.length;h++) halfParts.push('{{"artboardIndex":'+halves[h].artboardIndex+',"side":'+q(halves[h].side)+',"itemIndexes":['+halves[h].itemIndexes.join(',')+'],"pageNumbers":['+halves[h].pageNumbers.join(',')+']}}');
    for(var p=0;p<items.length;p++) {{ var d=items[p].details; itemParts.push('{{"index":'+items[p].index+',"name":'+q(items[p].name)+',"typename":'+q(items[p].typename)+',"layerPath":'+q(items[p].layerPath)+',"bounds":'+arr(items[p].bounds)+',"details":{{"geometricBounds":'+arr(d.geometricBounds)+',"visibleBounds":'+arr(d.visibleBounds)+',"hidden":'+d.hidden+',"guides":'+d.guides+',"filled":'+d.filled+',"stroked":'+d.stroked+',"clipping":'+d.clipping+',"clipped":'+d.clipped+',"layerVisible":'+d.layerVisible+',"layerPrintable":'+d.layerPrintable+'}}}}'); }}
    for(var m=0;m<markers.length;m++) markerParts.push('{{"textFrameIndex":'+markers[m].textFrameIndex+',"artboardIndex":'+markers[m].artboardIndex+',"side":'+q(markers[m].side)+',"number":'+markers[m].number+',"contents":'+q(markers[m].contents)+'}}');
    for(var e=0;e<issues.length;e++) issueParts.push('{{"type":'+q(issues[e].type)+',"itemIndex":'+(issues[e].itemIndex===undefined?'null':issues[e].itemIndex)+',"textFrameIndex":'+(issues[e].textFrameIndex===undefined?'null':issues[e].textFrameIndex)+',"artboardIndex":'+(issues[e].artboardIndex===undefined?'null':issues[e].artboardIndex)+',"side":'+q(issues[e].side||"")+',"message":'+q(issues[e].message)+'}}');
    write({_jsx(inventory_path)}, '{{"schema":"{SCHEMA}","source":'+q(source.fsName)+',"artboardCount":'+boards.length+',"layerCount":'+doc.layers.length+',"textFrameCount":'+doc.textFrames.length+',"pathItemCount":'+doc.pathItems.length+',"placedItemCount":'+doc.placedItems.length+',"rasterItemCount":'+doc.rasterItems.length+',"groupItemCount":'+doc.groupItems.length+',"preservedItemIndexes":['+preserved.join(',')+'],"artboards":['+boardParts.join(',')+'],"halves":['+halfParts.join(',')+'],"items":['+itemParts.join(',')+'],"pageMarkers":['+markerParts.join(',')+'],"issues":['+issueParts.join(',')+']}}');
  }} finally {{ doc.close(SaveOptions.DONOTSAVECHANGES); }}
}}());
'''


def inspect_inventory_issues(
    inventory: Mapping[str, Any],
    *,
    language: str,
    layout_rules: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    policy = _cover_policy(layout_rules)
    issues: list[dict[str, Any]] = []
    boards = list(inventory.get("artboards") or [])
    halves = list(inventory.get("halves") or [])
    items = {int(item["index"]): item for item in inventory.get("items") or []}
    if not boards or len(halves) != len(boards) * 2:
        issues.append(blocking_issue(
            "artboard_count_mismatch", "电子版画板或半页清单不完整", "请重新生成电子版 AI 后再运行拼版", language=language,
        ))
        return issues, {}
    base_size = None
    for board in boards:
        rect = board.get("rect") or []
        if len(rect) != 4:
            continue
        size = (round(float(rect[2]) - float(rect[0]), 3), round(float(rect[1]) - float(rect[3]), 3))
        if base_size is None:
            base_size = size
        elif size != base_size:
            issues.append(blocking_issue(
                "invalid_page_size", f"第 {int(board['index']) + 1} 个画板尺寸与其他画板不一致",
                "请在电子版 AI 中统一所有画板成品尺寸和方向后重新生成电子版",
                language=language, source_artboard=int(board["index"]),
            ))
    for raw in inventory.get("issues") or []:
        item = items.get(int(raw["itemIndex"])) if raw.get("itemIndex") is not None else None
        artboard = raw.get("artboardIndex")
        action = "请在电子版 AI 中修复该对象后重新生成电子版"
        if raw.get("type") == "centerline_crossing":
            action = f"请打开 {language} 电子版 AI 的第 {int(artboard) + 1} 个画板，将对象完整移到中轴一侧，或拆成左右两个对象"
        elif raw.get("type") == "bleed_detected":
            action = f"请打开 {language} 电子版 AI 的第 {int(artboard) + 1} 个画板，把对象可见边界收回成品画板内并移除出血"
        issues.append(blocking_issue(
            str(raw.get("type") or "page_unrecognized"), str(raw.get("message") or "电子版对象无法识别"), action,
            language=language, source_artboard=int(artboard) if artboard is not None else None,
            side=str(raw.get("side") or ""), object_ref=item,
        ))
    half_by_key = {(int(item["artboardIndex"]), str(item["side"])): item for item in halves}
    cover_index = policy["artboardIndex"]
    logical_sources: dict[str, dict[str, Any]] = {
        policy["left"]: {"artboardIndex": cover_index, "side": "left", "itemIndexes": list(half_by_key.get((cover_index, "left"), {}).get("itemIndexes") or [])},
        policy["right"]: {"artboardIndex": cover_index, "side": "right", "itemIndexes": list(half_by_key.get((cover_index, "right"), {}).get("itemIndexes") or [])},
    }
    number_sources: dict[int, dict[str, Any]] = {}
    body_sources: list[dict[str, Any]] = []
    preserve_indexes = {int(value) for value in inventory.get("preservedItemIndexes") or inventory.get("ignoredItemIndexes") or []}
    for half in halves:
        artboard = int(half["artboardIndex"])
        side = str(half["side"])
        if artboard == cover_index:
            continue
        raw_numbers = [int(value) for value in half.get("pageNumbers") or []]
        numbers = sorted(set(raw_numbers))
        item_indexes = list(half.get("itemIndexes") or [])
        body_sources.append({"artboardIndex": artboard, "side": side, "itemIndexes": item_indexes, "pageNumbers": raw_numbers})
        if len(raw_numbers) > 1:
            issues.append(blocking_issue(
                "duplicate_page_number", f"第 {artboard + 1} 个画板{side}半页出现多个页码标记：{raw_numbers}",
                f"请打开 {language} 电子版 AI 的第 {artboard + 1} 个画板，只保留该半页唯一正确的页码标记",
                language=language, source_artboard=artboard, side=side,
            ))
            continue
        if not numbers:
            continue
        number = numbers[0]
        if number in number_sources:
            first = number_sources[number]
            issues.append(blocking_issue(
                "duplicate_page_number", f"页码 {number} 在多个半页重复出现",
                f"请检查 {language} 电子版 AI，只保留一个页码 {number}，然后重新生成电子版",
                language=language, source_artboard=artboard, side=side, logical_page=number,
            ))
            continue
        number_sources[number] = {"artboardIndex": artboard, "side": side, "itemIndexes": item_indexes}
    if number_sources:
        maximum = max(number_sources)
        missing = [number for number in range(1, maximum + 1) if number not in number_sources]
        if missing:
            issues.append(blocking_issue(
                "missing_page", f"电子版缺少页码：{missing}",
                f"请检查 {language} 电子版 AI，补齐缺失页码和对应正文后重新生成电子版",
                language=language,
            ))
    return issues, {"logicalSources": logical_sources, "numberSources": number_sources, "bodySources": body_sources, "pageSize": base_size, "preserveItemIndexes": sorted(preserve_indexes)}


def build_ab_plan(
    body_sources: Sequence[Mapping[str, Any]],
    logical_sources: Mapping[str, Mapping[str, Any]],
    *,
    preserve_item_indexes: Sequence[int] = (),
) -> dict[str, Any]:
    if not body_sources:
        raise ImpositionError("Cannot build an AB plan without physical body pages")
    total_without_blanks = len(body_sources) + 2
    total = int(math.ceil(total_without_blanks / 4.0) * 4)
    blank_count = total - total_without_blanks
    canonical: list[dict[str, Any]] = [{"key": "front_cover", "kind": "front_cover", "source": dict(logical_sources["front_cover"])}]
    canonical.extend({
        "key": f"body_{index + 1}",
        "kind": "body" if source.get("itemIndexes") else "source_blank",
        "physicalPage": index + 1,
        "printedPageNumbers": list(source.get("pageNumbers") or []),
        "source": dict(source),
    } for index, source in enumerate(body_sources))
    canonical.extend({"key": f"blank_{index + 1}", "kind": "blank", "source": None} for index in range(blank_count))
    canonical.append({"key": "back_cover", "kind": "back_cover", "source": dict(logical_sources["back_cover"])})
    pairs = []
    for index in range(total // 2):
        if index % 2 == 0:
            left, right = canonical[total - 1 - index], canonical[index]
        else:
            left, right = canonical[index], canonical[total - 1 - index]
        pairs.append({"artboardIndex": index, "left": left, "right": right})
    plan = {
        "schema": SCHEMA,
        "totalLogicalPages": total,
        "bodyPageCount": len(body_sources),
        "blankPageCount": blank_count,
        "blankPlacement": "after-last-body-before-back-cover",
        "pairs": pairs,
        "aArtboardIndexes": list(range(0, len(pairs), 2)),
        "bArtboardIndexes": list(range(1, len(pairs), 2)),
        "printProfile": {"duplexFlip": "short-edge", "scalePercent": 100, "centered": True, "shrinkToFit": False, "bleedPt": 0},
        "preserveItemIndexes": sorted(set(int(value) for value in preserve_item_indexes)),
    }
    validate_ab_plan(plan)
    return plan


def validate_ab_plan(plan: Mapping[str, Any]) -> None:
    total = int(plan.get("totalLogicalPages") or 0)
    body_count = int(plan.get("bodyPageCount") or 0)
    blank_count = int(plan.get("blankPageCount") or 0)
    pairs = list(plan.get("pairs") or [])
    if total <= 0 or total % 4:
        raise ImpositionError(f"Logical page total must be a positive multiple of 4: {total}")
    if len(pairs) != total // 2:
        raise ImpositionError("AB artboard count does not equal totalLogicalPages / 2")
    keys = [side["key"] for pair in pairs for side in (pair["left"], pair["right"])]
    if len(keys) != len(set(keys)):
        raise ImpositionError("AB plan contains duplicate logical pages")
    if keys.count("front_cover") != 1 or keys.count("back_cover") != 1:
        raise ImpositionError("AB plan must contain exactly one front and back cover")
    canonical = ["front_cover", *[f"body_{number}" for number in range(1, body_count + 1)], *[f"blank_{index + 1}" for index in range(blank_count)], "back_cover"]
    if len(canonical) != total or str(plan.get("blankPlacement") or "") != "after-last-body-before-back-cover":
        raise ImpositionError("AB canonical sequence or blank placement is invalid")
    expected_pairs = []
    for index in range(total // 2):
        expected_pairs.append((canonical[total - 1 - index], canonical[index]) if index % 2 == 0 else (canonical[index], canonical[total - 1 - index]))
    actual_pairs = [(str(pair["left"]["key"]), str(pair["right"]["key"])) for pair in pairs]
    if actual_pairs != expected_pairs:
        raise ImpositionError("AB left/right page order does not match the short-edge print plan")
    for pair in pairs:
        for side in (pair["left"], pair["right"]):
            if str(side.get("key") or "").startswith("blank_") and side.get("source") is not None:
                raise ImpositionError("Padding pages must be completely empty and cannot retain any source object")
    expected_a = list(range(0, len(pairs), 2))
    expected_b = list(range(1, len(pairs), 2))
    if list(plan.get("aArtboardIndexes") or []) != expected_a or list(plan.get("bArtboardIndexes") or []) != expected_b:
        raise ImpositionError("A/B artboard selection does not match alternating AB faces")
    if set(expected_a) & set(expected_b) or sorted(expected_a + expected_b) != list(range(len(pairs))):
        raise ImpositionError("A/B artboard sets do not form an exact partition of AB")


def _movement_plan(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    movements: list[dict[str, Any]] = []
    for pair in plan.get("pairs") or []:
        target_artboard = int(pair["artboardIndex"])
        for side in ("left", "right"):
            logical = pair[side]
            source = logical.get("source")
            if not source:
                continue
            for item_index in source.get("itemIndexes") or []:
                movements.append({
                    "itemIndex": int(item_index),
                    "sourceArtboard": int(source["artboardIndex"]),
                    "sourceSide": str(source["side"]),
                    "targetArtboard": target_artboard,
                    "targetSide": side,
                    "logicalKey": logical["key"],
                })
    return movements


def build_reorder_jsx(source_ai: Path, output_ai: Path, output_pdf: Path, qa_path: Path, plan: Mapping[str, Any]) -> str:
    movements = _movement_plan(plan)
    return fr'''#target illustrator
(function () {{
  function q(value) {{ var s=String(value===undefined||value===null?"":value); return '"'+s.replace(/\\/g,"\\\\").replace(/"/g,'\\"').replace(/\r/g,"\\r").replace(/\n/g,"\\n")+'"'; }}
  function write(path,content) {{ var f=new File(path); f.encoding="UTF-8"; if(!f.open("w")) throw new Error("Cannot write QA: "+path); f.write(content); f.close(); }}
  function topItems(doc) {{ var out=[]; for(var i=0;i<doc.pageItems.length;i++) {{ var item=doc.pageItems[i]; try {{ if(item.parent&&item.parent.typename==="Layer") out.push(item); }} catch(e) {{}} }} return out; }}
  function unlockParents(item) {{ var current=item; while(current&&current.typename!=="Document") {{ try {{ current.locked=false; }} catch(e) {{}} try {{ current.visible=true; }} catch(e2) {{}} current=current.parent; }} }}
  var source=new File({_jsx(source_ai)}), outputAI=new File({_jsx(output_ai)}), outputPDF=new File({_jsx(output_pdf)});
  if(!source.exists) throw new Error("Source AI not found: "+source.fsName);
  app.userInteractionLevel=UserInteractionLevel.DONTDISPLAYALERTS;
  var doc=app.open(source), sourceText=doc.textFrames.length, sourcePaths=doc.pathItems.length, sourcePlaced=doc.placedItems.length, sourceRaster=doc.rasterItems.length, sourceGroups=doc.groupItems.length, sourceLayers=doc.layers.length;
  try {{
    var boards=[]; for(var a=0;a<doc.artboards.length;a++) {{ var r=doc.artboards[a].artboardRect; boards.push([Number(r[0]),Number(r[1]),Number(r[2]),Number(r[3])]); }}
    if(boards.length!=={len(plan.get('pairs') or [])}) throw new Error("Artboard count does not match AB plan");
    var top=topItems(doc), moves={json.dumps(movements, ensure_ascii=False)}, preserve={json.dumps(plan.get("preserveItemIndexes") or [])};
    if(top.length===0) throw new Error("No editable top-level Illustrator objects found");
    var seen={{}};
    for(var i=0;i<moves.length;i++) {{
      var move=moves[i], item=top[Number(move.itemIndex)];
      if(!item) throw new Error("Top-level item index changed: "+move.itemIndex);
      if(seen[String(move.itemIndex)]) throw new Error("Top-level item assigned more than once: "+move.itemIndex);
      seen[String(move.itemIndex)]=true; unlockParents(item);
      var sr=boards[Number(move.sourceArtboard)], tr=boards[Number(move.targetArtboard)];
      var sourceLeft=move.sourceSide==="right"?(sr[0]+sr[2])/2:sr[0], targetLeft=move.targetSide==="right"?(tr[0]+tr[2])/2:tr[0];
      item.translate(targetLeft-sourceLeft,tr[1]-sr[1]);
    }}
    for(var p=0;p<preserve.length;p++) {{ var preserveIndex=Number(preserve[p]), preserveItem=top[preserveIndex]; if(!preserveItem) throw new Error("Preserved item index changed: "+preserveIndex); if(seen[String(preserveIndex)]) throw new Error("Mapped item cannot also be preserved: "+preserveIndex); seen[String(preserveIndex)]=true; }}
    for(var n=0;n<top.length;n++) if(!seen[String(n)]) throw new Error("Unmapped editable object remains: "+n);
    for(var b=0;b<doc.artboards.length;b++) doc.artboards[b].name="AB-"+(b+1);
    var aiOptions=new IllustratorSaveOptions(); aiOptions.pdfCompatible=true; doc.saveAs(outputAI,aiOptions);
    var pdfOptions=new PDFSaveOptions(); pdfOptions.preserveEditability=true; pdfOptions.viewAfterSaving=false;
    try {{ pdfOptions.bleedOffsetRect=[0,0,0,0]; pdfOptions.bleedLink=false; }} catch(bleedError) {{}}
    try {{ pdfOptions.trimMarks=false; pdfOptions.registrationMarks=false; pdfOptions.colorBars=false; pdfOptions.pageInformation=false; }} catch(markError) {{}}
    doc.saveAs(outputPDF,pdfOptions);
    var outputTop=topItems(doc).length, editable=(outputTop===top.length&&outputTop===moves.length+preserve.length&&doc.layers.length===sourceLayers&&doc.textFrames.length===sourceText&&doc.pathItems.length===sourcePaths&&doc.placedItems.length===sourcePlaced&&doc.rasterItems.length===sourceRaster&&doc.groupItems.length===sourceGroups);
    write({_jsx(qa_path)}, '{{"schema":"{SCHEMA}","qaContractVersion":{QA_CONTRACT_VERSION},"variant":"AB","artboardCount":'+doc.artboards.length+',"sourceCounts":{{"layers":'+sourceLayers+',"textFrames":'+sourceText+',"pathItems":'+sourcePaths+',"placedItems":'+sourcePlaced+',"rasterItems":'+sourceRaster+',"groupItems":'+sourceGroups+',"topLevelItems":'+top.length+'}},"outputCounts":{{"layers":'+doc.layers.length+',"textFrames":'+doc.textFrames.length+',"pathItems":'+doc.pathItems.length+',"placedItems":'+doc.placedItems.length+',"rasterItems":'+doc.rasterItems.length+',"groupItems":'+doc.groupItems.length+',"topLevelItems":'+outputTop+'}},"mappedTopLevelItems":'+moves.length+',"preservedTopLevelItems":'+preserve.length+',"editableObjectsPreserved":'+(editable?'true':'false')+',"bleedPt":0}}');
  }} finally {{ doc.close(SaveOptions.DONOTSAVECHANGES); }}
}}());
'''


def build_split_jsx(source_ai: Path, output_ai: Path, output_pdf: Path, qa_path: Path, keep_indexes: Sequence[int], variant: str) -> str:
    keep = [int(value) for value in keep_indexes]
    return fr'''#target illustrator
(function () {{
  function write(path,content) {{ var f=new File(path); f.encoding="UTF-8"; if(!f.open("w")) throw new Error("Cannot write QA: "+path); f.write(content); f.close(); }}
  function topItems(doc) {{ var out=[]; for(var i=0;i<doc.pageItems.length;i++) {{ var item=doc.pageItems[i]; try {{ if(item.parent&&item.parent.typename==="Layer") out.push(item); }} catch(e) {{}} }} return out; }}
  function unlockParents(item) {{ var current=item; while(current&&current.typename!=="Document") {{ try {{ current.locked=false; }} catch(e) {{}} try {{ current.visible=true; }} catch(e2) {{}} current=current.parent; }} }}
  var source=new File({_jsx(source_ai)}), outputAI=new File({_jsx(output_ai)}), outputPDF=new File({_jsx(output_pdf)}), keep={json.dumps(keep)};
  if(!source.exists) throw new Error("Confirmed AB AI not found: "+source.fsName);
  app.userInteractionLevel=UserInteractionLevel.DONTDISPLAYALERTS;
  var doc=app.open(source), originalCount=doc.artboards.length, sourceLayers=doc.layers.length;
  try {{
    var keepMap={{}}; for(var k=0;k<keep.length;k++) keepMap[String(keep[k])]=true;
    var top=topItems(doc), expectedKeepTop=0, preservedTop=0;
    for(var i=top.length-1;i>=0;i--) {{
      var item=top[i], nonprinting=false;
      try {{ nonprinting=Boolean(item.guides||item.hidden||!item.layer.visible||!item.layer.printable); }} catch(nonprintError) {{}}
      if(nonprinting) {{ expectedKeepTop++; preservedTop++; continue; }}
      var bounds=item.visibleBounds, cx=(Number(bounds[0])+Number(bounds[2]))/2, cy=(Number(bounds[1])+Number(bounds[3]))/2, assigned=-1;
      for(var a=0;a<doc.artboards.length;a++) {{ var r=doc.artboards[a].artboardRect; if(cx>=r[0]&&cx<=r[2]&&cy<=r[1]&&cy>=r[3]) {{ assigned=a; break; }} }}
      if(assigned<0) throw new Error("Editable object is outside every confirmed AB artboard");
      if(!keepMap[String(assigned)]) {{ unlockParents(item); item.remove(); }} else expectedKeepTop++;
    }}
    for(var removeIndex=doc.artboards.length-1;removeIndex>=0;removeIndex--) if(!keepMap[String(removeIndex)]) doc.artboards.remove(removeIndex);
    if(doc.artboards.length!==keep.length) throw new Error("Split artboard count mismatch");
    for(var b=0;b<doc.artboards.length;b++) doc.artboards[b].name={_jsx(variant)}+"-"+(b+1);
    var aiOptions=new IllustratorSaveOptions(); aiOptions.pdfCompatible=true; doc.saveAs(outputAI,aiOptions);
    var pdfOptions=new PDFSaveOptions(); pdfOptions.preserveEditability=true; pdfOptions.viewAfterSaving=false;
    try {{ pdfOptions.bleedOffsetRect=[0,0,0,0]; pdfOptions.bleedLink=false; }} catch(bleedError) {{}}
    try {{ pdfOptions.trimMarks=false; pdfOptions.registrationMarks=false; pdfOptions.colorBars=false; pdfOptions.pageInformation=false; }} catch(markError) {{}}
    doc.saveAs(outputPDF,pdfOptions);
    var outputTop=topItems(doc).length, editable=(outputTop===expectedKeepTop&&doc.layers.length===sourceLayers);
    write({_jsx(qa_path)}, '{{"schema":"{SCHEMA}","qaContractVersion":{QA_CONTRACT_VERSION},"variant":"{variant}","sourceArtboardCount":'+originalCount+',"artboardCount":'+doc.artboards.length+',"selectedSourceArtboards":{json.dumps(keep)} ,"expectedTopLevelItems":'+expectedKeepTop+',"outputTopLevelItems":'+outputTop+',"preservedNonprintingTopLevelItems":'+preservedTop+',"layerCount":'+doc.layers.length+',"editableObjectsPreserved":'+(editable?'true':'false')+',"bleedPt":0}}');
  }} finally {{ doc.close(SaveOptions.DONOTSAVECHANGES); }}
}}());
'''


def _parse_pdfinfo(output: str) -> dict[str, Any]:
    pages = re.search(r"^Pages:\s+(\d+)", output, re.M)
    size = re.search(r"^Page size:\s+(.+)$", output, re.M)
    boxes: dict[str, list[float]] = {}
    for name in ("MediaBox", "CropBox", "BleedBox", "TrimBox"):
        match = re.search(rf"^{name}:\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)", output, re.M)
        if match:
            boxes[name] = [float(value) for value in match.groups()]
    return {"pages": int(pages.group(1)) if pages else None, "pageSize": size.group(1).strip() if size else None, "boxes": boxes}


def _pdf_has_zero_bleed(metadata: Mapping[str, Any]) -> bool:
    boxes = metadata.get("boxes") or {}
    required = [boxes.get(name) for name in ("MediaBox", "CropBox", "BleedBox", "TrimBox")]
    return all(value is not None for value in required) and all(value == required[0] for value in required[1:])


def _pdf_metadata(path: Path) -> dict[str, Any]:
    pdfinfo = shutil.which("pdfinfo")
    if not pdfinfo:
        raise ImpositionError("pdfinfo is required for imposition QA")
    result = subprocess.run([pdfinfo, "-box", str(path)], text=True, capture_output=True, check=False, timeout=60)
    if result.returncode != 0:
        raise ImpositionError(result.stderr.strip() or f"pdfinfo failed for {path.name}")
    return _parse_pdfinfo(result.stdout)


def _page_previews(pdf: Path, directory: Path) -> list[dict[str, Any]]:
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        raise ImpositionError("pdftoppm is required for imposition page previews")
    directory.mkdir(parents=True, exist_ok=True)
    prefix = directory / "page"
    result = subprocess.run([pdftoppm, "-png", "-r", "120", str(pdf), str(prefix)], text=True, capture_output=True, check=False, timeout=180)
    if result.returncode != 0:
        raise ImpositionError(result.stderr.strip() or f"pdftoppm failed for {pdf.name}")
    return [{"type": "page_png", "path": str(path), "sha256": sha256(path), "metadata": {"bytes": path.stat().st_size}} for path in sorted(directory.glob("page-*.png"))]


def _artifacts(outputs: Mapping[str, Path], qa_path: Path, preview_dir: Path) -> list[dict[str, Any]]:
    result = []
    for kind, path in outputs.items():
        result.append({"type": kind, "path": str(path), "sha256": sha256(path), "metadata": {"bytes": path.stat().st_size}})
    result.append({"type": "imposition_qa", "path": str(qa_path), "sha256": sha256(qa_path), "metadata": {"bytes": qa_path.stat().st_size}})
    result.extend(_page_previews(outputs["pdf"], preview_dir))
    return result


def impose_ab_job(job: Mapping[str, Any], *, execute: bool = True) -> dict[str, Any]:
    source_ai = Path(str(job["sourceAI"])).expanduser().resolve()
    source_pdf = Path(str(job["sourcePDF"])).expanduser().resolve()
    output_dir = Path(str(job["outputDir"])).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    language = str(job.get("language") or "")
    layout_rules = job.get("layoutRules") or {}
    policy = _cover_policy(layout_rules)
    inventory_path = output_dir / "electronic-inventory.json"
    inspect_jsx = illustrator_worker.write_jsx(build_inspect_jsx(
        source_ai, inventory_path, midline_tolerance=policy["midlineTolerancePt"], edge_tolerance=policy["edgeTolerancePt"],
    ), output_dir, "inspect-imposition-")
    result: dict[str, Any] = {
        "schema": SCHEMA, "qaContractVersion": QA_CONTRACT_VERSION,
        "runtimeSha256": sha256(Path(__file__).resolve()),
        "language": language, "executed": False, "inspectJsx": str(inspect_jsx), "qualityIssues": [],
    }
    if not execute:
        return result
    if not source_ai.is_file() or not source_pdf.is_file():
        raise ImpositionError("Electronic AI/PDF inputs are required")
    illustrator_worker.run_jsx(inspect_jsx, timeout=600)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8-sig"))
    issues, normalized = inspect_inventory_issues(inventory, language=language, layout_rules=layout_rules)
    source_pdf_meta = _pdf_metadata(source_pdf)
    if source_pdf_meta.get("pages") != int(inventory.get("artboardCount") or 0):
        issues.append(blocking_issue(
            "artboard_count_mismatch", f"电子版 AI 有 {inventory.get('artboardCount')} 个画板，但 PDF 有 {source_pdf_meta.get('pages')} 页",
            f"请重新从 {language} 电子版 AI 导出 PDF，确保使用全部画板且未合并页面", language=language,
        ))
    if issues:
        result.update({"executed": True, "status": "blocked", "inventory": str(inventory_path), "qualityIssues": issues})
        return result
    plan = build_ab_plan(normalized["bodySources"], normalized["logicalSources"], preserve_item_indexes=normalized["preserveItemIndexes"])
    if int(inventory["artboardCount"]) != len(plan["pairs"]):
        issues.append(blocking_issue(
            "artboard_count_mismatch", f"电子版画板数 {inventory['artboardCount']} 与 AB 计划画板数 {len(plan['pairs'])} 不一致",
            f"请检查 {language} 电子版的空白页数量；系统只允许在最后正文后、封底前补完全空白页", language=language,
        ))
        result.update({"executed": True, "status": "blocked", "inventory": str(inventory_path), "qualityIssues": issues})
        return result
    output_name = str(job.get("outputName") or f"manual-{language}-AB")
    outputs = {"ai": output_dir / f"{output_name}.ai", "pdf": output_dir / f"{output_name}.pdf"}
    qa_path = output_dir / "imposition-qa.json"
    manifest_path = output_dir / "imposition-manifest.json"
    reorder_jsx = illustrator_worker.write_jsx(build_reorder_jsx(source_ai, outputs["ai"], outputs["pdf"], qa_path, plan), output_dir, "impose-ab-")
    illustrator_worker.run_jsx(reorder_jsx, timeout=900)
    missing = [str(path) for path in [*outputs.values(), qa_path] if not path.is_file()]
    if missing:
        raise ImpositionError(f"Illustrator did not create AB outputs: {', '.join(missing)}")
    qa = json.loads(qa_path.read_text(encoding="utf-8-sig"))
    if not qa.get("editableObjectsPreserved"):
        issues.append(blocking_issue(
            "editability_lost", "AB 版 AI 的可编辑对象计数与电子版不一致",
            f"请保留 {language} 电子版中的文字、矢量、图片和图层，不要整页栅格化后重新运行拼版", language=language,
        ))
    pdf_meta = _pdf_metadata(outputs["pdf"])
    if pdf_meta.get("pages") != len(plan["pairs"]):
        issues.append(blocking_issue(
            "artboard_count_mismatch", f"AB 版 PDF 页数 {pdf_meta.get('pages')} 与计划 {len(plan['pairs'])} 不一致",
            "请重新运行 impose-ab；若仍失败，请检查 Illustrator PDF 导出是否启用了全部画板", language=language,
        ))
    if source_pdf_meta.get("pageSize") and pdf_meta.get("pageSize") != source_pdf_meta.get("pageSize"):
        issues.append(blocking_issue(
            "invalid_page_size", f"AB 版 PDF 页面尺寸 {pdf_meta.get('pageSize')} 与电子版 {source_pdf_meta.get('pageSize')} 不一致",
            "请重新运行 impose-ab，确保 100% 实际大小且未缩放画板", language=language,
        ))
    if not _pdf_has_zero_bleed(pdf_meta):
        issues.append(blocking_issue(
            "bleed_detected", "AB 版 PDF 的 MediaBox/CropBox/BleedBox/TrimBox 不一致",
            "请重新运行 impose-ab，确保出血值为 0 且不导出印刷标记", language=language,
        ))
    manifest = {
        **plan,
        "language": language,
        "createdAt": utc_now(),
        "source": {"ai": str(source_ai), "aiSha256": sha256(source_ai), "pdf": str(source_pdf), "pdfSha256": sha256(source_pdf)},
        "outputs": {kind: {"path": str(path), "sha256": sha256(path)} for kind, path in outputs.items()},
        "qualityIssues": issues,
    }
    write_json(manifest_path, manifest)
    artifacts = _artifacts(outputs, qa_path, output_dir / "verify-pages")
    artifacts.append({"type": "imposition_manifest", "path": str(manifest_path), "sha256": sha256(manifest_path), "metadata": {"bytes": manifest_path.stat().st_size}})
    result.update({
        "executed": True, "status": "blocked" if issues else "succeeded", "inventory": str(inventory_path), "plan": plan,
        "outputs": {key: str(value) for key, value in outputs.items()}, "qaReport": str(qa_path), "manifest": str(manifest_path),
        "artifacts": artifacts, "qualityIssues": issues, "reorderJsx": str(reorder_jsx), "pdfMetadata": pdf_meta,
    })
    return result


def split_ab_job(job: Mapping[str, Any], *, execute: bool = True) -> dict[str, Any]:
    source_ai = Path(str(job["sourceAI"])).expanduser().resolve()
    confirmed_sha = str(job.get("sourceAISha256") or "")
    manifest = job.get("abManifest") or {}
    validate_ab_plan(manifest)
    source_pdf_value = ((manifest.get("outputs") or {}).get("pdf") or {}).get("path")
    source_pdf_meta = _pdf_metadata(Path(str(source_pdf_value)).resolve()) if execute and source_pdf_value else {}
    if not source_ai.is_file() or (confirmed_sha and sha256(source_ai) != confirmed_sha):
        raise ImpositionError("Confirmed AB AI is missing or changed after confirmation")
    output_root = Path(str(job["outputDir"])).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    language = str(job.get("language") or "")
    result: dict[str, Any] = {
        "schema": SCHEMA, "qaContractVersion": QA_CONTRACT_VERSION,
        "runtimeSha256": sha256(Path(__file__).resolve()),
        "language": language, "executed": False, "variants": {}, "qualityIssues": [],
    }
    for variant, indexes in (("A", manifest["aArtboardIndexes"]), ("B", manifest["bArtboardIndexes"])):
        variant_dir = output_root / variant
        variant_dir.mkdir(parents=True, exist_ok=True)
        output_name = f"{str(job.get('outputPrefix') or f'manual-{language}')}-{variant}版"
        outputs = {"ai": variant_dir / f"{output_name}.ai", "pdf": variant_dir / f"{output_name}.pdf"}
        qa_path = variant_dir / "imposition-qa.json"
        jsx_path = illustrator_worker.write_jsx(build_split_jsx(source_ai, outputs["ai"], outputs["pdf"], qa_path, indexes, variant), variant_dir, f"split-{variant.lower()}-")
        result["variants"][variant] = {
            "qaContractVersion": QA_CONTRACT_VERSION,
            "runtimeSha256": sha256(Path(__file__).resolve()),
            "jsx": str(jsx_path), "selectedSourceArtboards": list(indexes),
        }
        if not execute:
            continue
        illustrator_worker.run_jsx(jsx_path, timeout=900)
        missing = [str(path) for path in [*outputs.values(), qa_path] if not path.is_file()]
        if missing:
            raise ImpositionError(f"Illustrator did not create {variant} outputs: {', '.join(missing)}")
        pdf_meta = _pdf_metadata(outputs["pdf"])
        qa = json.loads(qa_path.read_text(encoding="utf-8-sig"))
        if pdf_meta.get("pages") != len(indexes):
            result["qualityIssues"].append(blocking_issue(
                "artboard_count_mismatch", f"{variant} 版 PDF 页数 {pdf_meta.get('pages')} 与计划 {len(indexes)} 不一致",
                f"请重新运行 split-a-b；若仍失败，请检查 {language} 已确认 AB AI 的画板结构", language=language,
            ))
        if source_pdf_meta.get("pageSize") and pdf_meta.get("pageSize") != source_pdf_meta.get("pageSize"):
            result["qualityIssues"].append(blocking_issue(
                "invalid_page_size", f"{variant} 版 PDF 页面尺寸 {pdf_meta.get('pageSize')} 与已确认 AB 版 {source_pdf_meta.get('pageSize')} 不一致",
                f"请重新运行 split-a-b，不要缩放 {language} 的画板", language=language,
            ))
        if not qa.get("editableObjectsPreserved"):
            result["qualityIssues"].append(blocking_issue(
                "editability_lost", f"{variant} 版拆分后的可编辑顶层对象数与计划不一致",
                f"请重新运行 split-a-b；不要将 {language} 的 AB 页面栅格化或作为整页 PDF 置入", language=language,
            ))
        if not _pdf_has_zero_bleed(pdf_meta):
            result["qualityIssues"].append(blocking_issue(
                "bleed_detected", f"{variant} 版 PDF 的 MediaBox/CropBox/BleedBox/TrimBox 不一致",
                f"请重新运行 split-a-b，确保 {language} {variant} 版出血值为 0", language=language,
            ))
        artifacts = _artifacts(outputs, qa_path, variant_dir / "verify-pages")
        result["variants"][variant].update({
            "outputs": {key: str(value) for key, value in outputs.items()}, "qaReport": str(qa_path), "artifacts": artifacts, "pdfMetadata": pdf_meta,
        })
    result["executed"] = execute
    result["status"] = "dry_run" if not execute else ("blocked" if result["qualityIssues"] else "succeeded")
    return result
