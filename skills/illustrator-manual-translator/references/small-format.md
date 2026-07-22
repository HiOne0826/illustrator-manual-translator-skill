# Variable Small-Format Page Contract

Small-format layout is content pagination, not fold imposition. Page count is never fixed to five folds or ten panels.

## Default geometry

- Page size: `76 × 156.22 mm`.
- Content top inset: `5.8 mm`.
- Content bottom inset: `3.7 mm`.
- Section gap: `4 mm`.
- Minimum body font: `5 pt`.
- PDF bleed: zero.

The geometry may be overridden with a plan, but every generated artboard in one output must use the same page size.

## Automatic pagination

The electronic AI is inspected as ordered physical half-pages. Each half-page becomes one content block:

- All fitting title/body/image sections from the same half-page remain together.
- Title bars, titles, bodies, and associated images keep their internal relative positions.
- Adjacent footer-free sparse blocks are greedily merged when their combined height plus the configured section gap fits one small page.
- A block containing a printed page-number footer remains atomic; the footer aligns to the bottom inset.
- Cover artwork without title bars remains one visual group.
- The algorithm never creates pages merely to reach a target count and never spreads sections to fill unused height.

For the FT-802LED English source this converts eight source half-pages into seven small pages: the sparse cover slogan merges with the front cover, while `Package Inspection`, `Brief Introduction`, and `Specification` remain together on one page.

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

- Generated AI artboard count differs from the automatic pagination plan.
- Generated PDF page count differs from the plan.
- Page size drift from the configured small-page geometry.
- Text overflow or an object outside its assigned page.
- Font size below the configured minimum after correction.
- Loss of editable text, vectors, images, groups, or layers.
- Non-zero PDF bleed.
