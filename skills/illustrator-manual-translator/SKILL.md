---
name: illustrator-manual-translator
description: Turn product specifications and an Adobe Illustrator manual template into multilingual editable AI and PDF deliverables through a no-UI, file-based workflow. Use when the work requires AI extraction, one Excel workbook for Chinese and translation correction, explicit whole-file confirmation gates, Illustrator layout, and final QA.
---

# Illustrator Manual Translator

Run the customer workflow through files and chat. Do not start or require the repository web UI.

## Non-Negotiable Rules

- Never modify the source `.ai`; all rendering writes to project `preview/` and `delivery/`.
- The customer edits one workbook named `说明书内容确认.xlsx`.
- Do not add `action`, `review_status`, approval, pass, reject, or per-row decision columns.
- Treat the workbook as corrected content, not an approval system.
- Stop after creating the Chinese sheet. Continue only after the user explicitly confirms the Chinese content in chat.
- Stop after appending the translation sheet. Continue only after the user explicitly confirms the translations in chat.
- Never infer confirmation from the existence, save time, or hash of the workbook.
- Preserve template body font sizes when layout rules specify `bodyFontSize: preserve-template`.

## Project Command

Use `scripts/manual_workflow.py` for state, hashes, workbook validation, rendering, and delivery:

```bash
python3 scripts/manual_workflow.py <command> ...
```

Before the first project, run `python3 scripts/manual_workflow.py doctor`. It must find the bundled extraction/Illustrator modules, Adobe Illustrator automation, and the Codex workspace `@oai/artifact-tool` runtime used to create and read Excel.

Read `references/workflow.md` before running a customer project. Read `references/workbook-schema.md` before creating or interpreting the workbook.

## AI Content Tasks

The script deliberately does not call a fixed model provider. The agent running this Skill performs two bounded AI tasks.

### Generate Chinese rows

After `init`, read `work/content-generation-input.json`. Write a JSON file containing exactly one row for every `templateFields[].fieldId`:

```json
{
  "rows": [
    {
      "fieldId": "textframe_12",
      "fieldName": "额定功率",
      "sourceEvidence": "规格书表 2：额定功率 45 W",
      "aiChinese": "额定功率：45 W",
      "required": true,
      "protectedTokens": ["45 W"]
    }
  ]
}
```

Use only evidence in `canonicalProduct`. Do not invent missing facts. Keep model numbers, units, certifications, URLs, and legal names in `protectedTokens`. An inapplicable optional field may have empty `aiChinese`.

### Generate translation rows

Only after explicit Chinese confirmation, read `work/translation-generation-input.json`. Write every `fieldId × language` combination:

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

Translate only `finalChinese`. Empty Chinese remains empty. Preserve protected terms and quantities.

## Confirmation Semantics

The user may freely correct the yellow final-content column, including correcting a model number or quantity proposed by AI. After the user explicitly confirms, run the matching `confirm-*` command. The commands validate stable IDs, read-only columns, template-required content, source hashes, and the full expected row set. Translation confirmation also verifies numbers, units, and model-like tokens derived from the confirmed Chinese.

If validation fails, explain the affected Excel row and ask the user to correct the workbook. Do not bypass the check and do not add a status field.

## Layout And Delivery

Run `render` only after translation confirmation. Blocking layout issues keep the project blocked. Show the generated PDF/page previews to the user and wait for an explicit layout confirmation before `confirm-layout` copies `.ai`, `.pdf`, QA JSON, and page previews into `delivery/`.

Use `--no-execute` only to inspect generated JSX. A dry run must not advance the project to layout confirmation.

## References

- `references/workflow.md`: exact command sequence and stop points.
- `references/workbook-schema.md`: customer-visible workbook contract and validation rules.
- `references/translation-schema.md`: AI-only JSON inputs and outputs.
- `references/limitations.md`: Illustrator, font, outlined-text, and template risks.
