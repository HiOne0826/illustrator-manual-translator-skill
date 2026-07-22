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
  delivery/     # only user-confirmed electronic + AB/A/B deliverables
```

The next AI task is described in `work/content-optimization-input.json`.

If `work/conflict-review-input.json` contains high-severity conflicts, obtain explicit conclusions and record all of them before Chinese confirmation:

```bash
python3 scripts/manual_workflow.py resolve-conflicts \
  --project "/path/to/customer-project" \
  --resolutions-json "/path/to/conflict-resolutions.json"
```

Each row must contain the original `conflictId`, `status: resolved`, and a non-empty factual `resolution`. Review placeholders such as `请确认`, `待确认`, `TODO`, or `TBD` are rejected.

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

If the Chinese JSON changes after the workbook has already moved beyond this stage, rebuild the Chinese Sheet and invalidate the old confirmation chain:

```bash
python3 scripts/manual_workflow.py refresh-chinese \
  --project "/path/to/customer-project" \
  --content-json "/path/to/chinese-rows.json"
```

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

The Sheet visibly embeds images extracted from the specification. The user deletes or pastes a real image in the yellow `最终使用图片` area and never types an image ID. `confirm-assets` reads the selected image bytes from the `.xlsx` package and converts them to internal content-hash assets. Even when the specification contains no extracted images, append the Sheet and require whole-file image confirmation; optional empty slots follow `emptyBehavior`. With `keep_template`, both the original template visual and its original bound label remain unchanged; a label is rewritten only when that slot has a newly selected image.

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

## 5. Render Target Languages And Confirm Electronic Editions

```bash
python3 scripts/manual_workflow.py render --project "/path/to/customer-project"
```

This runs Illustrator once per language, writes `.ai`, `.pdf`, preview PNG, page PNGs, and layout QA to `preview/<language>/`, and blocks on overflow, rule violations, missing/changed image assets, duplicate visual targets, page-count drift, page-render errors, or detected old-text residue.

Show the PDFs or page PNGs to the user. Stop until the user explicitly confirms the layout.

```bash
python3 scripts/manual_workflow.py confirm-layout --project "/path/to/customer-project"
```

This freezes the Chinese and every target-language electronic AI/PDF. It does not finish delivery.

At this point choose one print path for the project:

- Small-format manual: continue with `layout-small-format` and `confirm-small-format`; page count is automatic.
- Booklet: continue with `impose-ab`, `confirm-ab`, `split-a-b`, and `confirm-a-b`.
- Five-fold leaflet: only when separately requested, provide an explicit imposition plan and continue with `impose-five-fold` and `confirm-five-fold`.

## 5A. Generate And Confirm Variable Small Pages

Read `references/small-format.md` first.

```bash
python3 scripts/manual_workflow.py layout-small-format \
  --project "/path/to/customer-project"
```

The module writes native editable AI/PDF, one preview PNG per generated `76 × 156.22 mm` page, a small-format manifest, and QA for every language. It derives page count from content capacity, keeps fitting sections together, and may merge adjacent sparse footer-free source halves. Stop until the user explicitly confirms all small pages.

```bash
python3 scripts/manual_workflow.py confirm-small-format \
  --project "/path/to/customer-project"
```

## 5B. Generate And Confirm Five-Fold Leaflets

Read `references/folded-leaflet.md` first. The plan must map every electronic half-page exactly once into the ten outside/inside panels and must confirm the printer's duplex flip direction.

```bash
python3 scripts/manual_workflow.py impose-five-fold \
  --project "/path/to/customer-project" \
  --plan "/path/to/five-fold-plan.json"
```

The module writes native editable AI/PDF, a two-page preview, a folded-leaflet manifest, and QA for every language. It blocks on ambiguous source objects, missing panel assignments, font size below the configured minimum, overset text, geometry drift, non-zero bleed, lost editability, or an unconfirmed duplex flip direction.

Show both sides of every language. Stop until the user explicitly confirms the five-fold layout.

```bash
python3 scripts/manual_workflow.py confirm-five-fold \
  --project "/path/to/customer-project"
```

This verifies persisted hashes and creates the final electronic + five-fold delivery package. Do not continue into AB/A/B after choosing this path unless the customer explicitly requests a second print variant.

## 6. Generate And Confirm AB Editions

```bash
python3 scripts/manual_workflow.py impose-ab --project "/path/to/customer-project"
```

The independent imposition module moves native Illustrator objects inside the same document. It must preserve editable text, vectors, images, and layers; placing or rasterizing whole PDF pages is forbidden. It builds the canonical sequence from the electronic edition's physical half-pages: `front cover -> every non-cover half-page in artboard left/right order -> fully empty padding -> back cover`. Printed page-number text is used only to detect duplicate or missing labels; it never changes physical imposition order. Padding is inserted only after the last physical body page and before the back cover. The module writes AB AI/PDF plus an imposition manifest and page previews for every language.

The AB run blocks the whole language batch when any language contains a centerline-crossing object, an unrecognized page, duplicate or missing page numbers, inconsistent page size, bleed, page/artboard-count drift, or an invalid left/right plan. Each blocking issue contains a concrete `userAction`.

After the user explicitly confirms every AB edition:

```bash
python3 scripts/manual_workflow.py confirm-ab --project "/path/to/customer-project"
```

## 7. Split And Confirm A/B Editions

Only a hash-confirmed AB AI may be split:

```bash
python3 scripts/manual_workflow.py split-a-b --project "/path/to/customer-project"
```

A is the front-side set (`AB` artboards 0, 2, 4, ...); B is the back-side set (1, 3, 5, ...). Both remain editable AI and also produce PDF, QA JSON, and page previews. The A/B sets must be disjoint and their union must exactly equal AB.

After the user explicitly confirms every A and B edition:

```bash
python3 scripts/manual_workflow.py confirm-a-b --project "/path/to/customer-project"
```

If the user confirms A/B but explicitly does not want a delivery package, use:

```bash
python3 scripts/manual_workflow.py confirm-a-b --project "/path/to/customer-project" --no-delivery-package
```

This verifies every confirmed hash, writes `work/confirmed-a-b-manifest.json`, and finishes at `completed_without_delivery`. It must not create or modify `delivery/`.

When packaging is requested, the final `delivery/` contains electronic, AB, A, and B `.ai`/`.pdf` files, QA JSON, page previews, `imposition-manifest.json`, and `delivery-manifest.json` with hashes. The print contract is fixed: short-edge flip, 100% actual size, centered, no scaling/shrink-to-fit, and zero bleed.

## Recovery And Status

```bash
python3 scripts/manual_workflow.py status --project "/path/to/customer-project"
```

The state machine prevents commands from skipping a confirmation gate. If specifications or the template change, initialize a new project because previous confirmations are invalid.
