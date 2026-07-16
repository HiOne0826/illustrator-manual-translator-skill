# End-to-End Workflow

## 0. Preflight

```bash
python3 scripts/manual_workflow.py doctor
```

Do not start customer work until the command finds the Excel runtime, bundled extraction/Illustrator modules, Adobe Illustrator, and `osascript`.
Execution is macOS-only. `doctor` must pass a real harmless JSX probe through Apple Events; an installed application without automation permission is not ready.

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

`--layout-rules` may be omitted when the source `.ai` SHA-256 matches a bundled template profile. The FT-802LED English template profile is bundled with the Skill and contains the validated multilingual layout corrections. For any other template, provide an onboarded layout-rules file explicitly; an unknown template without visual slots remains blocked.

The project contains:

```text
customer-project/
  inputs/       # immutable copies of specifications and source AI
  review/       # the single customer workbook
  work/         # state, AI task inputs, confirmed JSON, render jobs
  preview/      # AI/PDF/PNG before final layout confirmation
  delivery/     # final confirmed deliverables
```

The next AI task is described in `work/content-optimization-input.json`.

## 2. AI-optimize Chinese and create the review workbook

Generate user-facing Chinese rows as specified in `SKILL.md`, then freeze the optimized result:

```bash
python3 scripts/manual_workflow.py optimize-chinese \
  --project "/path/to/customer-project" \
  --content-json "/path/to/optimized-chinese-rows.json"
```

Only after that gate succeeds, create the workbook:

```bash
python3 scripts/manual_workflow.py export-chinese \
  --project "/path/to/customer-project"
```

Return `review/说明书内容确认.xlsx` to the user. Ask them to inspect the evidence and edit only the yellow `最终中文` column.

Stop. Do not run the next command until the user explicitly says the Chinese content is confirmed.

```bash
python3 scripts/manual_workflow.py confirm-chinese --project "/path/to/customer-project"
```

This advances to image review. It does not create translation input yet.

## 3. Confirm Images And Chinese Proof

Prepare suggestions and append the image Sheet:

```bash
python3 scripts/manual_workflow.py append-assets --project "/path/to/customer-project" --assets-json "/path/to/asset-suggestions.json"
python3 scripts/manual_workflow.py confirm-assets --project "/path/to/customer-project"
```

The Sheet visibly embeds images extracted from the specification. The user deletes or pastes a real image in the yellow `最终使用图片` area and never types an image ID. `confirm-assets` reads the selected image bytes from the `.xlsx` package and converts them to internal content-hash assets. Even when the specification contains no extracted images, append the Sheet and require whole-file image confirmation; optional empty slots follow `emptyBehavior`.

After the user explicitly replies `图片内容已确认`, generate the Chinese proof:

```bash
python3 scripts/manual_workflow.py render-source-chinese --project "/path/to/customer-project"
```

Show the Chinese PDF/page PNGs. After explicit layout confirmation, run:

```bash
python3 scripts/manual_workflow.py confirm-source-chinese-layout --project "/path/to/customer-project"
```

Only this command creates `work/translation-generation-input.json` and opens the translation phase.

## 4. Append Translation Review

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

## 5. Render Target Languages And QA

```bash
python3 scripts/manual_workflow.py render --project "/path/to/customer-project"
```

This runs Illustrator once per language, writes `.ai`, `.pdf`, preview PNG, page PNGs, and layout QA to `preview/<language>/`, and blocks on overflow, rule violations, missing/changed image assets, duplicate visual targets, page-count drift, page-render errors, or detected old-text residue.

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
