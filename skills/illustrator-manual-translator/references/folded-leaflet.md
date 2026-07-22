# Five-Fold Leaflet Contract

The five-fold output is an alternative to AB/A/B booklet imposition. Do not run both paths for one confirmed electronic edition unless the customer explicitly requests both deliverables.

## Fixed geometry

- Two Illustrator artboards: `FIVE-FOLD-OUTSIDE` and `FIVE-FOLD-INSIDE`.
- Media size: `390 × 174.6 mm`.
- Five finished panels per side.
- Horizontal trim margins: `5 mm` on each side.
- Vertical trim margins: `9.19 mm` on each side.
- Finished panel size: `76 × 156.22 mm`.
- Dense reference panels commonly keep about `5.8 mm` above and `3.7 mm` below live content.
- The two Illustrator artboards keep the reference `31 mm` canvas gap; outside is artboard 1 below, inside is artboard 2 above.
- Replace source A4 guides with ten closed Illustrator guide rectangles (`guides=true`), one exact `76 × 156.22 mm` rectangle per panel.
- Crop and fold marks are native Illustrator paths inside the media box.
- PDF bleed is zero; `MediaBox == CropBox == BleedBox == TrimBox`.

## Plan file

The plan must define all ten target panels and every source half-page exactly once. A missing `sourceArtboard` marks an intentionally blank target panel.

When the source has fewer logical panels than the target, one source half-page may be partitioned with contiguous normalized `sourceBand: [start, end]` ranges. Bands for that half-page must cover `0` through `1` without gaps or overlap. A split boundary must fall between native objects; the run blocks if any object crosses it. This moves complete editable sections into additional panels without duplicating or inventing content.

```json
{
  "schema": "folded-leaflet-plan/1.0",
  "printProfile": {
    "duplexFlip": "long-edge"
  },
  "assignments": [
    {"targetSide": "outside", "targetPanel": 0, "sourceArtboard": 0, "sourceSide": "left"},
    {"targetSide": "outside", "targetPanel": 1, "sourceArtboard": 0, "sourceSide": "right"},
    {"targetSide": "outside", "targetPanel": 2, "sourceArtboard": 1, "sourceSide": "left"},
    {"targetSide": "outside", "targetPanel": 3, "sourceArtboard": 1, "sourceSide": "right"},
    {"targetSide": "outside", "targetPanel": 4},
    {"targetSide": "inside", "targetPanel": 0, "sourceArtboard": 2, "sourceSide": "left"},
    {"targetSide": "inside", "targetPanel": 1, "sourceArtboard": 2, "sourceSide": "right"},
    {"targetSide": "inside", "targetPanel": 2, "sourceArtboard": 3, "sourceSide": "left"},
    {"targetSide": "inside", "targetPanel": 3, "sourceArtboard": 3, "sourceSide": "right"},
    {"targetSide": "inside", "targetPanel": 4}
  ]
}
```

`duplexFlip` must be confirmed with the printer as `long-edge` or `short-edge`. The module blocks confirmation when it remains `unconfirmed`.

## Native-object layout

The implementation scales source objects uniformly to the panel width, then identifies complete visual sections. A full-width filled title bar starts a section; the bar, title, corresponding body, and associated image move with one shared offset. Page-number footers form a separate block. When a panel contains multiple blocks, only those complete blocks are distributed between the reference top and bottom insets. Object dimensions and every internal section offset remain proportional: titles and bodies are never spread independently, and images or text are not stretched vertically. A panel containing only one short section may retain unavoidable bottom whitespace because the module does not duplicate or invent content. It does not place a PDF page or rasterize the source. Text below the configured `minimumBodyFontPt` is raised to the minimum and any resulting overset TextFrame is blocking.

QA records `panelContentMetrics` and blocks when whitespace before the first block exceeds `12 mm`. For a panel with multiple distributable blocks it also blocks when whitespace below the last block exceeds `12 mm`. Single-section bottom whitespace is reported but cannot be rejected without a panel-level editorial remap or additional content.

This native-fit mode is a technical conversion baseline, not semantic editorial reflow. If the customer requires the dense MC-230 reference style, onboard panel-level semantic rules and edit content until all overset and readability gates pass.

## Commands

After `confirm-layout`:

```bash
python3 scripts/manual_workflow.py impose-five-fold \
  --project "/path/to/customer-project" \
  --plan "/path/to/five-fold-plan.json"
```

Inspect every language's two-page AI/PDF proof. Only after explicit confirmation:

```bash
python3 scripts/manual_workflow.py confirm-five-fold \
  --project "/path/to/customer-project"
```

## Blocking validation

- Source object outside an artboard or crossing the A4 centerline.
- Duplicate or missing source half-page assignment.
- Overlapping/gapped `sourceBand` ranges or a native object crossing a semantic band boundary.
- More than ten source half-pages without an editorial reflow plan.
- Output other than two artboards or five panels per side.
- Missing reference guide rectangles, retained A4 source guides, or artboard-gap drift from `31 mm`.
- Media size drift from `390 × 174.6 mm`.
- Font size below the configured minimum or overset text after correction.
- Loss of editable text, vectors, placed images, raster images, groups, or layers.
- Missing page previews, non-zero bleed, or unconfirmed duplex flip direction.
