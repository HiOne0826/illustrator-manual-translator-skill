# End-to-End Workflow

## 0. Preflight

```bash
python3 scripts/manual_workflow.py doctor
```

Do not start customer work until the command finds the Excel runtime, bundled extraction/Illustrator modules, Adobe Illustrator, and `osascript`.

## 1. Initialize

```bash
python3 scripts/manual_workflow.py init \
  --project "/path/to/customer-project" \
  --spec "/path/to/specification.docx" \
  --template "/path/to/manual.ai" \
  --template-metadata "/path/to/template.v2.json" \
  --layout-rules "/path/to/layout-rules.json" \
  --languages "en-US,de-DE"
```

The project contains:

```text
customer-project/
  inputs/       # immutable copies of specifications and source AI
  review/       # the single customer workbook
  work/         # state, AI task inputs, confirmed JSON, render jobs
  preview/      # AI/PDF/PNG before final layout confirmation
  delivery/     # final confirmed deliverables
```

The next AI task is described in `work/content-generation-input.json`.

## 2. Create Chinese Review Workbook

Generate Chinese rows as specified in `SKILL.md`, then run:

```bash
python3 scripts/manual_workflow.py export-chinese \
  --project "/path/to/customer-project" \
  --content-json "/path/to/chinese-rows.json"
```

Return `review/说明书内容确认.xlsx` to the user. Ask them to inspect the evidence and edit only the yellow `最终中文` column.

Stop. Do not run the next command until the user explicitly says the Chinese content is confirmed.

```bash
python3 scripts/manual_workflow.py confirm-chinese --project "/path/to/customer-project"
```

This creates `work/translation-generation-input.json` from the corrected Chinese text.

## 3. Append Translation Review

Generate all translation rows as specified in `SKILL.md`, then run:

```bash
python3 scripts/manual_workflow.py append-translations \
  --project "/path/to/customer-project" \
  --translations-json "/path/to/translation-rows.json"
```

The command appends `多语种翻译` to the same workbook and retains a Chinese-confirmation backup. Ask the user to edit only the yellow `最终译文` column.

Stop. Do not run the next command until the user explicitly says the translations are confirmed.

```bash
python3 scripts/manual_workflow.py confirm-translations --project "/path/to/customer-project"
```

## 4. Render And QA

```bash
python3 scripts/manual_workflow.py render --project "/path/to/customer-project"
```

This runs Illustrator once per language, writes `.ai`, `.pdf`, preview PNG, page PNGs, and layout QA to `preview/<language>/`, and blocks on overflow, rule violations, page-count drift, page-render errors, or detected old-text residue.

Show the PDFs or page PNGs to the user. Stop until the user explicitly confirms the layout.

```bash
python3 scripts/manual_workflow.py confirm-layout --project "/path/to/customer-project"
```

The final `delivery/` must contain `.ai`, `.pdf`, QA JSON, page previews, and `delivery-manifest.json` with hashes.

## Recovery And Status

```bash
python3 scripts/manual_workflow.py status --project "/path/to/customer-project"
```

The state machine prevents commands from skipping a confirmation gate. If specifications or the template change, initialize a new project because previous confirmations are invalid.
