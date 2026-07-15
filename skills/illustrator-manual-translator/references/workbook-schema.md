# Workbook Contract

The customer receives one file: `说明书内容确认.xlsx`.

## Sheets

### 使用说明

Explains the two review rounds and the exact chat confirmations. It contains no workflow controls.

### 中文内容

| Column | Purpose | Customer edit |
|---|---|---|
| 模板字段 | Human-readable field name | No |
| 规格书原始依据 | Evidence used by AI | No |
| AI提取中文 | AI draft | No |
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

This sheet is appended only after explicit Chinese confirmation.

## Deliberately Absent Fields

Do not add `action`, `review_status`, `approved`, `rejected`, `pass`, or per-row checkboxes. The workbook carries corrected content. The user's explicit chat message advances the whole file.

## Validation

At each confirmation, reject the workbook if any expected row is missing or duplicated, an unknown ID appears, a read-only cell changed, a template-required final value is empty, or a source input hash changed. Translation confirmation additionally rejects missing numbers, units, and model-like tokens derived from the confirmed Chinese.

After Chinese confirmation, re-read the Chinese sheet before appending or confirming translations. If the user changed Chinese again, invalidate the old translation round and return to Chinese confirmation instead of silently translating stale content.

Rows may be reordered because stable IDs are authoritative. Do not rely on Excel row numbers for identity.
