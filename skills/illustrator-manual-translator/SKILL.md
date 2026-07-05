---
name: illustrator-manual-translator
description: Extract, translate, replace, and verify live text in Adobe Illustrator `.ai` manuals using local Adobe Illustrator automation. Use when a user needs multilingual instruction manuals, product manuals, packaging docs, or other Illustrator files converted by reading TextFrame content, preparing translation tables, writing translations back into a copied `.ai`, exporting PDF, and reporting outlined text or layout risks.
---

# Illustrator Manual Translator

Use this skill when the deliverable should preserve an Illustrator-based manual workflow. Assume the customer machine has Adobe Illustrator installed and the agent can control local desktop apps.

## Core Rule

Never modify the source `.ai` directly. Always export a TextFrame inventory first, prepare translations, then write into a copied `.ai` and exported PDF.

## Standard Workflow

1. Check environment:

```bash
python3 scripts/illustrator_manual_workflow.py doctor
```

2. Export live Illustrator text:

```bash
python3 scripts/illustrator_manual_workflow.py export \
  --source "/path/to/manual.ai" \
  --out-dir "/path/to/output"
```

This creates:

- `textframes.json`: machine-readable TextFrame inventory.
- `textframes.md`: human-readable extraction table.

3. Create translation template:

```bash
python3 scripts/illustrator_manual_workflow.py template \
  --textframes "/path/to/output/textframes.json" \
  --out "/path/to/output/translations.zh-CN.json" \
  --language zh-CN
```

Fill only `targetText` values that should be replaced. Leave `targetText` empty to keep the original text.

4. Apply translations to a copy:

```bash
python3 scripts/illustrator_manual_workflow.py apply \
  --source "/path/to/manual.ai" \
  --translations "/path/to/output/translations.zh-CN.json" \
  --out-dir "/path/to/output" \
  --name "manual-zh-CN" \
  --font "PingFangSC-Regular"
```

This creates:

- `manual-zh-CN.ai`: copied Illustrator file with replaced TextFrames.
- `manual-zh-CN.pdf`: exported PDF.
- `replace-report.md`: replacement summary.

5. Verify PDF:

```bash
python3 scripts/illustrator_manual_workflow.py verify \
  --pdf "/path/to/output/manual-zh-CN.pdf" \
  --out-dir "/path/to/output/verify"
```

Review rendered PNGs and `verify-report.md` before calling the result acceptable.

## Decision Points

- If important visible text is missing from `textframes.json`, treat it as outlined/path text or a complex object. Do not claim it was replaced.
- If the user requires editable `.ai` output, use the Illustrator TextFrame route first.
- If the user only needs a visual PDF and Illustrator automation fails, fall back to PDF-compatible AI visual replacement.
- If text is outlined, choose one: ask a designer to restore live text, overlay a new TextFrame, or rebuild that region in a template system.

## Quality Gates

Before delivery, confirm:

- Source `.ai` remained unchanged.
- Output `.ai` and `.pdf` exist.
- PDF page count and size match the source.
- Translated text is extractable from the PDF text layer.
- Rendered pages show no obvious missing glyphs, overflow, or old visible text.
- `replace-report.md` lists changed TextFrame indexes.
- Untranslated visible text is either intentionally preserved or logged as outlined/path text.

## References

- Read `references/workflow.md` when implementing an end-to-end customer workflow.
- Read `references/translation-schema.md` when preparing or validating translation JSON.
- Read `references/limitations.md` when diagnosing missing text, outlined text, font problems, or layout drift.
