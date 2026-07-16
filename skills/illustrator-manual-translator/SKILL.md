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
- Apply the template typography policy consistently across languages. For `fixed-template-standard`, never shrink the configured body size or leading to fit; expand, move, paginate, or block instead.
- When the Illustrator template hash matches a bundled template profile, use that profile automatically. An explicit `--layout-rules` path always overrides bundled profile discovery.

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

After `init`, read `work/content-optimization-input.json`. First preserve the extracted facts, then rewrite technical material into concise, user-readable manual copy. Explain necessary terms, organize operation and safety content as actionable steps, and never invent a capability, benefit, certification, or safety claim. For `contentSource=template-copy`, quote the exact `templateText` in `sourceEvidence` and localize fixed headings or brand copy instead of leaving them empty.

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
      "required": true,
      "protectedTokens": ["45 W"]
    }
  ]
}
```

Use only evidence in `canonicalProduct`, except fields explicitly marked `contentSource=template-copy`. Do not invent missing facts. Keep model numbers, units, certifications, URLs, and legal entity names in `protectedTokens`. Translate or localize role labels such as `Manufacturer` and `Producer`, but preserve the registered legal entity name itself unless the source provides an official localized legal name. When a template field provides `fieldNameHint`, copy it exactly into `fieldName` so the customer can recognize legal-name rows in Excel. Every non-empty `aiChinese` requires both traceable `sourceEvidence` and a concise `optimizationNote`. Required fields may not be empty. An inapplicable optional field may have empty `aiChinese`.

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

The user may freely correct the yellow final-content column, including correcting a model number or quantity proposed by AI. After the user explicitly confirms, run the matching `confirm-*` command. The commands validate stable IDs, read-only columns, template-required content, source hashes, and the full expected row set. Translation confirmation also verifies numbers, units, and model-like tokens derived from the confirmed Chinese.

If validation fails, explain the affected Excel row and ask the user to correct the workbook. Do not bypass the check and do not add a status field.

## Layout And Delivery

After Chinese confirmation, append the `图片与视觉资产` Sheet and wait for the exact whole-file confirmation `图片内容已确认`. Every onboarded template must define at least one semantic `visualSlots` entry; a missing or empty list is a blocking template error. Embed each unmarked specification image exactly once in a shared candidate gallery. A valid `[ASSET: slot]` marker auto-fills the matching slot and removes that image from the unassigned gallery. Each slot displays only its current suggested final image. An empty optional slot defaults to retaining the original template visual. The customer edits only the yellow `最终使用图片` area by deleting or pasting an image.

After image confirmation, `render-source-chinese` is mandatory. Show the Chinese proof and wait for explicit layout confirmation before running `confirm-source-chinese-layout`; only that command creates the translation-generation input. After translation confirmation, run `render` for target languages, show their proofs, and use `confirm-layout` for final delivery. Both render phases verify persisted confirmations, hashes, overflow, safe bounds, typography, and image rules.

Use `--no-execute` only to inspect generated JSX. A dry run must not advance the project to layout confirmation.

`render-source-chinese` renders confirmed `finalChinese` values directly and never creates a fake `zh-CN` translation round. It writes `说明书.ai` and `说明书.pdf` under `preview/zh-CN/`.

The bundled `assets/template-profiles/aeolus-ft802led/layout-rules.v1.json` profile carries the verified FT-802LED fixes: live text replacements for outlined labels, fixed multilingual body typography, centered/localized cover brand copy, a widened distributor-title box that leaves contact lines unchanged, sequential section flow with body-level continuation, and safe-bound/overset checks. Select it automatically only for the exact matching source-template SHA-256.

## References

- `references/workflow.md`: exact command sequence and stop points.
- `references/workbook-schema.md`: customer-visible workbook contract and validation rules.
- `references/translation-schema.md`: AI-only JSON inputs and outputs.
- `references/visual-assets.md`: semantic image slots, DPI rules, and asset replacement behavior.
- `references/limitations.md`: Illustrator, font, outlined-text, and template risks.
