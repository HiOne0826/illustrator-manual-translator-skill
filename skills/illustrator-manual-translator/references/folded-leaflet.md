# Five-Fold Leaflet Contract

The five-fold output is an alternative to AB/A/B booklet imposition. Do not run both paths for one confirmed electronic edition unless the customer explicitly requests both deliverables.

## Fixed geometry

- Two Illustrator artboards: `FIVE-FOLD-OUTSIDE` and `FIVE-FOLD-INSIDE`.
- Media size: `390 × 174.6 mm`.
- Five finished panels per side.
- Horizontal trim margins: `5 mm` on each side.
- Vertical trim margins: `9.19 mm` on each side.
- Finished panel size: `76 × 156.22 mm`.
- Crop and fold marks are native Illustrator paths inside the media box.
- PDF bleed is zero; `MediaBox == CropBox == BleedBox == TrimBox`.

## Plan file

The plan must define all ten target panels and every source half-page exactly once. A missing `sourceArtboard` marks an intentionally blank target panel.

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

The first implementation scales and moves each source half-page's native top-level Illustrator objects into its assigned panel. It does not place a PDF page or rasterize the source. Text below the configured `minimumBodyFontPt` is raised to the minimum and any resulting overset TextFrame is blocking.

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
- More than ten source half-pages without an editorial reflow plan.
- Output other than two artboards or five panels per side.
- Media size drift from `390 × 174.6 mm`.
- Font size below the configured minimum or overset text after correction.
- Loss of editable text, vectors, placed images, raster images, groups, or layers.
- Missing page previews, non-zero bleed, or unconfirmed duplex flip direction.
