# Variable Small-Format Page Contract

Small-format layout combines content-aware pagination with the reference template's physical two-row arrangement. Page count is never fixed to five folds or ten panels, but it must be even.

## Default geometry

- Page size: `76 × 156.22 mm`.
- Content top inset: `5.8 mm`.
- Content bottom inset: `3.7 mm`.
- Section gap: `4 mm`.
- Minimum body font: `5 pt`.
- PDF bleed: zero.
- Physical arrangement: two vertically stacked horizontal artboards, with the same number of small pages in each row.
- Sheet margins: about `5 mm` left/right and `9.19 mm` top/bottom, matching the reference template.
- Gap between the two row artboards: `31 mm`.

The geometry may be overridden with a plan, but every generated artboard in one output must use the same page size.

## Automatic pagination

The electronic AI is inspected as ordered physical half-pages. Each half-page becomes one content block:

- All fitting title/body/image sections from the same half-page remain together.
- Title bars, titles, bodies, and associated images keep their internal relative positions.
- Adjacent footer-free sparse blocks are greedily merged when their combined height plus the configured section gap fits one small page.
- A block containing a printed page-number footer remains atomic; the footer aligns to the bottom inset.
- Cover artwork without title bars remains one visual group.
- The algorithm never creates pages merely to reach a target count and never spreads sections to fill unused height.

For the FT-802LED English source this produces seven content pages: the sparse cover slogan merges with the front cover, while `Package Inspection`, `Brief Introduction`, and `Specification` remain together on one page. One blank page is then appended to make eight physical pages, arranged as two rows of four horizontal panels.

## Two-row physical arrangement

- Compute content pages first; never split a fitting section merely to reach an even count.
- If content page count is odd, append exactly one empty page after the last content page.
- Divide the resulting even page count equally across two horizontal rows.
- Create one Illustrator artboard per row, not one artboard per small page.
- Match the reference file's artboard order: lower row is artboard 1, upper row is artboard 2.
- Recreate one `76 × 156.22 mm` guide rectangle for every small page using the reference template margins.
- Export the two row artboards as a two-page PDF while retaining the logical small-page count in QA and the manifest.

## Commands

```bash
python3 scripts/manual_workflow.py layout-small-format \
  --project "/path/to/customer-project"
```

After reviewing every AI/PDF page:

```bash
python3 scripts/manual_workflow.py confirm-small-format \
  --project "/path/to/customer-project"
```

Five-fold, saddle-stitch, or another printer sheet arrangement is a separate optional imposition after this content layout is accepted.

## Blocking validation

- Generated AI does not contain exactly two vertically stacked horizontal row artboards.
- Logical small-page count is odd, or the two rows have unequal column counts.
- Generated guide count differs from the logical small-page count.
- Generated PDF does not contain exactly the two horizontal row artboards.
- Page size drift from the configured small-page geometry.
- Text overflow or an object outside its assigned page.
- Font size below the configured minimum after correction.
- Loss of editable text, vectors, images, groups, or layers.
- Non-zero PDF bleed.
