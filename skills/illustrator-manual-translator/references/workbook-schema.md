# Workbook Contract

The customer receives one file: `说明书内容确认.xlsx`.

## Sheets

### 使用说明

Explains the Chinese, image, Chinese-layout, and translation sequence and the exact chat confirmations. It contains no workflow controls.

### 中文内容

| Column | Purpose | Customer edit |
|---|---|---|
| 模板字段 | Human-readable field name | No |
| 规格书/模板原始依据 | Evidence used by AI | No |
| AI优化中文 | User-facing Chinese rewritten by AI | No |
| 最终中文 | Corrected source content | Yes, yellow |
| 字段编号（请勿修改） | Stable machine key | No |

The sheet is created first. An empty optional `最终中文` means the matching template text is removed from the generated manual.

### 多语种翻译

| Column | Purpose | Customer edit |
|---|---|---|
| 模板字段 | Human-readable field name | No |
| 语种 | Target locale | No |
| 已确认中文 | Chinese source of truth | No |
| AI翻译 | AI draft | No |
| 最终译文 | Corrected translation | Yes, yellow |
| 翻译编号（请勿修改） | Stable `fieldId::language` key | No |

This sheet is appended only after explicit Chinese confirmation, image confirmation, and Chinese AI/PDF layout confirmation.

## Deliberately Absent Fields

Do not add `action`, `review_status`, `approved`, `rejected`, `pass`, or per-row checkboxes. The workbook carries corrected content. The user's explicit chat message advances the whole file.

## Validation

At each confirmation, reject the workbook if any expected row is missing or duplicated, an unknown ID appears, a read-only cell changed, a template-required final value is empty, or a source input hash changed. Translation confirmation additionally rejects missing numbers, units, and model-like tokens derived from the confirmed Chinese.

After Chinese confirmation, re-read the Chinese sheet before appending or confirming translations. If the user changed Chinese again, invalidate the old translation round and return to Chinese confirmation instead of silently translating stale content.

Rows may be reordered because stable IDs are authoritative. Do not rely on Excel row numbers for identity.

## Sheet: `图片与视觉资产`

The slot table has only the visible columns `模板位置` and `最终使用图片`. Above it, one unassigned candidate gallery embeds every unmarked specification image exactly once. A valid `[ASSET: slot]` marker removes that image from the unassigned gallery and automatically fills the matching slot. Each slot embeds only its current suggested final image. An empty optional slot retains the template visual by default; Logo rows with explicit `emptyBehavior: keep_template` must say `留空保留模板默认` in the visible slot name. The customer changes only the yellow `最终使用图片` area by deleting or pasting an image. Do not display or request image names, paths, IDs, slot IDs, or fit modes. Stable semantic slot IDs and content hashes remain internal project state.

Do not add per-row status, approval, accept, or reject columns. The only approval is the whole-file chat confirmation `图片内容已确认`.
