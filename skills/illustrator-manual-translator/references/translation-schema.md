# Translation JSON Schema

The `template` command creates a JSON file:

```json
{
  "sourceTextframes": "/path/to/textframes.json",
  "targetLanguage": "zh-CN",
  "notes": "Fill targetText for text that should be replaced. Leave targetText empty to keep original text.",
  "items": [
    {
      "index": 0,
      "sourceText": "User Manual",
      "targetText": "用户手册",
      "kind": "POINTTEXT",
      "fontName": "Arial-BoldMT",
      "fontSize": "17",
      "reviewStatus": "approved"
    }
  ]
}
```

## Required Fields

- `index`: Illustrator `doc.textFrames[index]`.
- `targetText`: replacement text. Empty means keep original.

## Recommended Review Status

- `pending`: not reviewed.
- `approved`: okay to write back.
- `keep-original`: intentionally leave empty.
- `needs-design`: translation likely needs layout adjustment.
- `outlined-text`: visible text was not found in TextFrames.

## Protected Terms

Do not translate unless the customer says otherwise:

- Brand names.
- Product model numbers.
- Certifications.
- Units and dimensions.
- Phone numbers.
- URLs and emails.
- Legal entity names when used as official contact info.

## Stable Matching

Indexes are stable for one file revision but may change if the `.ai` is edited. For production, also keep `sourceText`, `geometricBounds`, and page/region hints so a human or agent can detect index drift.

