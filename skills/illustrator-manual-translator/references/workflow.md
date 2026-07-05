# End-to-End Workflow

## Customer Intake

Collect:

- Source `.ai` file.
- Existing PDF export if available.
- Target language list.
- Translation rules: brand names, model numbers, addresses, units, legal warnings.
- Required outputs: editable `.ai`, print PDF, review table, report.

## Execution

1. Run `doctor`.
2. Run `export`.
3. Inspect `textframes.md`.
4. Generate translation template.
5. Fill `targetText` values.
6. Run `apply`.
7. Run `verify`.
8. Render or open PDF for visual review.
9. Report unresolved visible source-language text.

## Recommended Output Folder

Use one folder per source file and language:

```text
outputs/
  product-model/
    textframes.json
    textframes.md
    translations.zh-CN.json
    product-model-zh-CN.ai
    product-model-zh-CN.pdf
    replace-report.md
    verify/
      verify-report.md
      render-page-1.png
```

## Handoff Summary

The final response should include:

- Output `.ai` path.
- Output `.pdf` path.
- TextFrame count and changed count.
- Known misses, especially outlined text.
- Verification status.

