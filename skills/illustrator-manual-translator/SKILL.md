---
name: illustrator-manual-translator
description: Turn product specifications and an Adobe Illustrator manual template into multilingual editable AI and PDF deliverables through a no-UI, file-based workflow. Use when the work requires AI extraction, one Excel workbook for Chinese, translation, and image replacement correction, explicit whole-file confirmation gates, Illustrator layout, and final QA.
---

# Illustrator Manual Translator

Run the customer workflow through files and chat. Do not start or require the repository web UI.

## Non-Negotiable Rules

- Never modify the source `.ai`; all rendering writes to project `preview/` and `delivery/`.
- Treat Logo, product photos, product-structure diagrams, and assembly diagrams as semantic visual slots. Keep stable slot IDs in project state; do not expose image IDs or slot IDs in the customer-facing image Sheet.
- The customer edits one workbook named `说明书内容确认.xlsx`.
- Do not add `action`, `review_status`, approval, pass, reject, or per-row decision columns.
- Treat the workbook as corrected content, not an approval system.
- Stop after creating the Chinese sheet. Continue only after the user explicitly confirms the Chinese content in chat.
- After Chinese confirmation, append and confirm the image Sheet, then generate and explicitly confirm the Chinese `.ai`/`.pdf` proof before creating any translation Sheet.
- Stop after appending the translation sheet. Continue only after the user explicitly confirms the translations in chat.
- Always append the `图片与视觉资产` Sheet after Chinese confirmation and stop until the user explicitly replies `图片内容已确认`.
- Image confirmation is mandatory even when the specification contains no extracted images; optional empty slots retain or hide template visuals according to `emptyBehavior`.
- Reject template onboarding when `visualSlots` is empty. Never skip the image gate or advance directly from translation confirmation to rendering.
- Never infer confirmation from the existence, save time, or hash of the workbook.
- Treat every `severity=high` extraction conflict as blocking. Record an explicit `resolved` conclusion with `resolve-conflicts` before Chinese confirmation; never print `请确认`, `待确认`, `TODO`, `TBD`, or equivalent review notes as manual copy.
- Apply the template typography policy consistently across languages. For `fixed-template-standard`, never shrink the configured body size or leading to fit; expand, move, paginate, or block instead.
- When the Illustrator template hash matches a bundled template profile, use that profile automatically. An explicit `--layout-rules` path always overrides bundled profile discovery.
- Treat the enhanced file workflow as the only production baseline. Electronic layout confirmation is not final delivery.
- Generate AB AI/PDF for Chinese and every target language, wait for explicit AB confirmation, and only then generate A and B.
- AB, A, and B AI files must preserve editable text, vectors, images, and layers. Never place, flatten, or rasterize a whole PDF page as an imposition shortcut.
- Treat every non-cover half-page in the electronic edition as a physical body page, including unnumbered decorative or intentionally blank halves. Printed page numbers are validation labels only and never determine imposition order. Insert any additional padding only after the last physical body page and before the back cover; generated padding must contain no page number, Logo, or other object.
- Block imposition when an object crosses the centerline, a page is unrecognized, or a page number is duplicated. Return an actionable `userAction`; do not guess.
- Validate missing/duplicate pages, left/right order, the exact A+B partition of AB, page size, zero bleed, PDF page count, and Illustrator artboard count.
- Use the fixed print contract: short-edge flip, 100% actual size, centered, no scaling or shrink-to-fit, and zero bleed.
- Treat five-fold output as an explicit alternative print path after electronic confirmation. Require a ten-panel mapping and confirmed duplex flip direction; never infer the fold order from page numbers or visual proximity. If additional target panels are needed, split a source half only at explicit contiguous semantic bands that do not cross native objects.
- Five-fold AI/PDF must preserve native editable objects, use two `390 × 174.6 mm` artboards with five `76 × 156.22 mm` finished panels per side, distribute dense content to the reference `5.8 mm` top / `3.7 mm` bottom insets without stretching objects, keep body text at or above the configured minimum, and block on overset text or excessive vertical whitespace.
- `confirm-a-b` generates the complete delivery package by default on every run. Only when the user explicitly declines the package for that specific run may you add `--no-delivery-package`; the exception is not a saved preference for later projects or reruns.

## Project Command

Use `scripts/manual_workflow.py` for state, hashes, workbook validation, rendering, and delivery:

```bash
python3 scripts/manual_workflow.py <command> ...
```

Before the first project, run `python3 scripts/manual_workflow.py doctor`. It must find the bundled extraction/Illustrator modules, Adobe Illustrator automation, and the Codex workspace `@oai/artifact-tool` runtime used to create and read Excel.

Illustrator execution is supported on macOS only. The real doctor probe must execute a harmless JSX through Apple Events; merely finding the application is not sufficient.

Read `references/workflow.md` before running a customer project. Read `references/workbook-schema.md` before creating or interpreting the workbook.

## AI Content Tasks

The script deliberately does not call a fixed model provider. The agent running this Skill performs two bounded AI tasks.

### Optimize technical content into user-facing Chinese

After `init`, read `work/content-optimization-input.json`. First preserve the extracted facts, then rewrite technical material into concise, user-readable manual copy. Explain necessary terms, organize operation and safety content as actionable steps, and never invent a capability, benefit, certification, or safety claim. For every field, prefer matching specification evidence; when the specification does not provide that content, use and localize the field's `templateText` as the default instead of treating the field as omitted.

Write exactly one row for every `templateFields[].fieldId`:

```json
{
  "rows": [
    {
      "fieldId": "textframe_12",
      "fieldName": "额定功率",
      "sourceEvidence": "规格书表 2：额定功率 45 W",
      "aiChinese": "额定功率：45 W",
      "optimizationNote": "保留参数，并改写为用户可直接识别的参数短句",
      "contentOrigin": "product-evidence",
      "required": true,
      "protectedTokens": ["45 W"]
    }
  ]
}
```

Use matching evidence in `canonicalProduct` first. If the specification does not provide content for a field, set `contentOrigin=template-default`, quote that field's exact `templateText` in `sourceEvidence`, and place its localized default content in `aiChinese`; this is a retained template default, not missing confirmation content. This rule applies uniformly to every field with non-empty `templateText`, including optional fields, contact details, addresses, websites, service notes, packaging text, and component lists. Do not suppress a template default merely because it appears product-specific or is not corroborated by the specification; the specification overrides it only when it supplies that field. Do not invent facts beyond those two sources. Keep model numbers, units, certifications, URLs, and legal entity names in `protectedTokens`. Translate or localize role labels such as `Manufacturer` and `Producer`, but preserve the registered legal entity name itself unless the source provides an official localized legal name. `contentOrigin` is mandatory on every row. Every non-empty `aiChinese` requires both traceable `sourceEvidence` and a concise `optimizationNote`. A field with non-empty `templateText` may not have empty `aiChinese`.

If Chinese content changes after the workbook has already entered a later confirmation stage, run `refresh-chinese --project ... --content-json ...`. It rebuilds the Chinese Sheet from the latest `aiChinese`, initializes the yellow `finalChinese` cells from that latest content, backs up the old workbook, and invalidates all downstream image, layout, and translation confirmations.

Run `optimize-chinese` to validate and freeze the AI rewrite. Only after it advances to `ready_to_export_chinese` may `export-chinese` create the Chinese Excel workbook.

### Generate translation rows

Only after the Chinese `.ai`/`.pdf` proof has been generated and confirmed with `confirm-source-chinese-layout`, read `work/translation-generation-input.json`. Write every `fieldId × language` combination:

```json
{
  "rows": [
    {
      "fieldId": "textframe_12",
      "language": "en-US",
      "aiTranslation": "Rated power: 45 W"
    }
  ]
}
```

Translate only `finalChinese`. Empty Chinese remains empty. Preserve protected terms, quantities, and registered legal entity names; localize only their surrounding role labels unless an official localized legal name is supplied by the source.

## Confirmation Semantics

The user may freely correct the yellow final-content column, including correcting a model number or quantity proposed by AI. High-severity source conflicts listed in `work/conflict-review-input.json` require explicit final conclusions through `resolve-conflicts`; editing copy alone does not silently resolve source conflicts. After the user explicitly confirms, run the matching `confirm-*` command. The commands validate stable IDs, read-only columns, template-required content, source hashes, the full expected row set, and absence of review-only placeholders. Translation confirmation also verifies numbers, units, and model-like tokens derived from the confirmed Chinese.

If validation fails, explain the affected Excel row and ask the user to correct the workbook. Do not bypass the check and do not add a status field.

## Layout And Delivery

After Chinese confirmation, append the `图片与视觉资产` Sheet and wait for the exact whole-file confirmation `图片内容已确认`. Every onboarded template must define at least one semantic `visualSlots` entry; a missing or empty list is a blocking template error. Embed each unmarked specification image exactly once in a shared candidate gallery. A valid `[ASSET: slot]` marker auto-fills the matching slot and removes that image from the unassigned gallery. Each slot displays only its current suggested final image. An empty optional slot may retain the original template visual according to `emptyBehavior=keep_template`; when retained, its original nearby product/model label must also remain unchanged. Only a newly selected image may trigger that slot's label rewrite. The customer edits only the yellow `最终使用图片` area by deleting or pasting an image.

After image confirmation, `render-source-chinese` is mandatory. Show the Chinese proof and wait for explicit layout confirmation before running `confirm-source-chinese-layout`; only that command creates the translation-generation input. After translation confirmation, run `render` for target languages and show their proofs. `confirm-layout` freezes all electronic editions and advances to `impose-ab`; it does not deliver. After every AB edition is explicitly confirmed, run `confirm-ab`, then `split-a-b`. Only after every A/B edition is explicitly confirmed may `confirm-a-b` create final delivery. All render and imposition phases verify persisted confirmations and hashes.

For a customer-confirmed five-fold leaflet, use `impose-five-fold --plan ...` instead of `impose-ab`. The plan must map every source half-page exactly once into ten outside/inside panels and explicitly confirm `long-edge` or `short-edge` duplex printing. Show the two-page proofs and wait for explicit confirmation before `confirm-five-fold` creates delivery. Native-fit conversion is only the baseline; any text overflow or readability failure requires panel-level semantic reflow rather than further shrinking.

For generated live titles that replace outlined template text, always prefer the current render item's language-specific font over the template's original font. Keep the title above its title-bar artwork, align the visible glyph bounds to the bottom of the title bar with the configured bottom inset, and repeat the same visible title, font, and bottom alignment on every continuation artboard. A generated TextFrame without visible glyphs or without the required bottom alignment is a layout failure.

Use `--no-execute` only to inspect generated JSX. A dry run must not advance the project to layout confirmation.

`render-source-chinese` renders confirmed `finalChinese` values directly and never creates a fake `zh-CN` translation round. It writes `说明书.ai` and `说明书.pdf` under `preview/zh-CN/`.

The bundled `assets/template-profiles/aeolus-ft802led/layout-rules.v1.json` profile carries the verified FT-802LED fixes: live text replacements for outlined labels, fixed multilingual body typography, centered/localized cover brand copy, a widened distributor-title box that leaves contact lines unchanged, sequential section flow with body-level continuation, and safe-bound/overset checks. Select it automatically only for the exact matching source-template SHA-256.

## References

- `references/workflow.md`: exact command sequence and stop points.
- `references/workbook-schema.md`: customer-visible workbook contract and validation rules.
- `references/translation-schema.md`: AI-only JSON inputs and outputs.
- `references/visual-assets.md`: semantic image slots, DPI rules, and asset replacement behavior.
- `references/limitations.md`: Illustrator, font, outlined-text, and template risks.
- `references/imposition.md`: exact physical-page AB/A/B ordering, rejection recovery, validation, and optional no-package completion. Read it before any imposition command.
- `references/folded-leaflet.md`: five-panel geometry, mapping plan, native-object layout, print-direction gate, and QA contract. Read it before `impose-five-fold` or `confirm-five-fold`.
