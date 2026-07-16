# AI Exchange JSON

These JSON files are internal exchanges between the Skill agent and `manual_workflow.py`. Customers edit only Excel.

## Chinese Optimization

Input: `work/content-optimization-input.json`.

Output row fields:

- `fieldId`: exact ID from `templateFields`; required and unique.
- `fieldName`: customer-facing Chinese label. If the input field has `fieldNameHint`, use it exactly; legal-name rows should clearly say that the registered name is preserved by default.
- `sourceEvidence`: concise, traceable specification evidence.
- `aiChinese`: Chinese content to place in the template.
- `optimizationNote`: required for every non-empty `aiChinese`; briefly states how technical evidence was made user-readable.
- `required`: whether empty content is forbidden.
- `protectedTokens`: exact text that must survive correction and translation, including registered legal entity names. Translate surrounding role labels such as `Manufacturer` or `Producer`, not the protected legal name itself.

Every template field must appear exactly once. `contentSource=product-evidence` uses only `canonicalProduct`; `contentSource=template-copy` must quote the exact `templateText` in `sourceEvidence` before localizing it. Required fields may not be empty. There is no review or action field.

## Translation Generation

Input: `work/translation-generation-input.json`, created only from confirmed `finalChinese`.

Output row fields:

- `fieldId`: exact confirmed Chinese field ID.
- `language`: exact requested locale.
- `aiTranslation`: translation of `finalChinese` only.

Every field/language pair must appear exactly once. Empty confirmed Chinese must produce empty translation. There is no review or approval field.

## Stable Matching

`fieldId` is tied to one template revision. The workflow also hashes the copied source AI and specifications. If an input changes, do not reuse old confirmation JSON or Excel.
