# Visual asset contract

Templates declare `visualSlots` in `layout-rules.json`. A slot owns a semantic `id`, display `name`, Illustrator `objectRefs`, target `bounds`, `replacementMode`, `emptyBehavior`, `defaultFit`, `required`, and optional `minDpi`. `emptyBehavior: keep_template` leaves the original Illustrator object untouched when the final image cell is empty; `emptyBehavior: hide` hides it.

When a template image has a nearby model label that must change with the selected product, bind it explicitly with `labelTextFrame`. The binding records the label text-frame `index`, original `sourceText`, source-of-truth `targetFieldId`, and optional `fontName`, `fontSize`, and `bounds`. Both translated and source-Chinese rendering then update the label from the confirmed product field. Do not leave product-gallery labels as untracked template residue.

At least one `visualSlots` entry is mandatory for every onboarded template. Chinese confirmation advances to image review; image confirmation and Chinese AI/PDF layout confirmation must both finish before translation review begins. Illustrator rendering requires a persisted whole-file image confirmation. An empty candidate gallery does not remove this gate.

Recommended slot IDs:

- `brand.logo.cover`
- `brand.logo.footer`
- `product.hero`
- `cover.gallery.1` through `cover.gallery.4`
- `diagram.product-structure`
- `diagram.assembly`

Source DOCX files may bind a single image paragraph to a slot by putting a marker in the next non-empty paragraph:

```text
[ASSET: diagram.assembly]
```

The marker is not ordinary manual content. Missing preceding images, multiple images in the preceding paragraph, invalid marker syntax, and duplicate slot markers are blocking extraction conflicts. Unmarked images remain valid candidates for manual selection in Excel.

`contain` preserves the complete image, `cover` fills and clips, `stretch` may distort and should be exceptional, and `hide` removes an optional template visual. Group/compound-path replacement hides the original Illustrator artwork and places the selected asset into the recorded bounds. Linked `PlacedItem` replacement uses `relink()`.

The worker rejects unknown modes, invalid fit values, missing assets, hash changes, duplicate slots, and duplicate Illustrator targets before launching Illustrator. Raster assets must also satisfy the slot's effective `minDpi`; an unreadable or undersized raster is a blocking quality issue.

The customer workbook never exposes these internal IDs. It embeds every unmarked specification candidate exactly once in a shared gallery and reserves one yellow final-image cell per semantic slot. A valid marker auto-fills its matching slot and keeps that bound image out of the unassigned gallery. Each slot displays only its current suggested final image. On confirmation, the workflow reads the drawing anchored in that cell directly from the `.xlsx` media package, hashes the bytes, and creates an internal asset binding. An empty optional final-image cell defaults to keeping the template visual unchanged. Brand Logo slots should declare `emptyBehavior: keep_template` explicitly and make that behavior visible in the customer-facing slot name.
