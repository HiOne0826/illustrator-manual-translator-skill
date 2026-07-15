# AI Exchange JSON

These JSON files are internal exchanges between the Skill agent and `manual_workflow.py`. Customers edit only Excel.

## Chinese Generation

Input: `work/content-generation-input.json`.

Output row fields:

- `fieldId`: exact ID from `templateFields`; required and unique.
- `fieldName`: customer-facing Chinese label.
- `sourceEvidence`: concise, traceable specification evidence.
- `aiChinese`: Chinese content to place in the template.
- `required`: whether empty content is forbidden.
- `protectedTokens`: exact text that must survive correction and translation.

Every template field must appear exactly once. There is no review or action field.

## Translation Generation

Input: `work/translation-generation-input.json`, created only from confirmed `finalChinese`.

Output row fields:

- `fieldId`: exact confirmed Chinese field ID.
- `language`: exact requested locale.
- `aiTranslation`: translation of `finalChinese` only.

Every field/language pair must appear exactly once. Empty confirmed Chinese must produce empty translation. There is no review or approval field.

## Stable Matching

`fieldId` is tied to one template revision. The workflow also hashes the copied source AI and specifications. If an input changes, do not reuse old confirmation JSON or Excel.
