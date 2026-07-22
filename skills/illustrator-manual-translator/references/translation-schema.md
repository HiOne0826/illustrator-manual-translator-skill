# AI Exchange JSON

These JSON files are internal exchanges between the Skill agent and `manual_workflow.py`. Customers edit only Excel.

## Chinese Optimization

Input: `work/content-optimization-input.json`.

Output row fields:

- `fieldId`: exact ID from `templateFields`; required and unique.
- `fieldName`: customer-facing Chinese label describing the actual template field; do not replace ordinary labels with special legal-name review wording.
- `sourceEvidence`: concise, traceable evidence. For a template default, it must quote the exact `templateText`.
- `aiChinese`: Chinese content to place in the template.
- `optimizationNote`: required for every non-empty `aiChinese`; briefly states how technical evidence was made user-readable.
- `contentOrigin`: `product-evidence` when the specification supplies the content; `template-default` when the specification does not supply it and the template default is retained.
- `required`: whether empty content is forbidden.
- `protectedTokens`: exact text that must survive correction and translation, including registered legal entity names. Translate surrounding role labels such as `Manufacturer` or `Producer`, not the protected legal name itself.

Every template field must appear exactly once and must explicitly set `contentOrigin`. Prefer matching `canonicalProduct` evidence. If the specification does not provide the field, use and localize `templateText`, set `contentOrigin=template-default`, and quote the exact default in `sourceEvidence`. This fallback is normal confirmed content, not an omission, and applies to optional fields and product-specific template copy without exception. A field with non-empty `templateText` may not have empty `aiChinese`. There is no review or action field.

## Translation Generation

Input: `work/translation-generation-input.json`, created only from confirmed `finalChinese`.

Output row fields:

- `fieldId`: exact confirmed Chinese field ID.
- `language`: exact requested locale.
- `aiTranslation`: translation of `finalChinese` only.

Every field/language pair must appear exactly once. Empty confirmed Chinese must produce empty translation. There is no review or approval field.

## Stable Matching

`fieldId` is tied to one template revision. The workflow also hashes the copied source AI and specifications. If an input changes, do not reuse old confirmation JSON or Excel.
