# Limitations And Failure Modes

## Outlined Text

If visible text does not appear in `textframes.json`, it is probably:

- Outlined into paths.
- Embedded in a placed image.
- Part of a symbol or complex object not exposed as `TextFrame`.
- Hidden or locked in a way the basic exporter does not traverse.

Do not claim this text was replaced. Log it and choose an exception path:

- Ask designer to restore live text.
- Overlay a new TextFrame.
- Rebuild that region in a template.

## Layout Drift

Changing language changes text length. Illustrator may keep the same text box, causing:

- Overflow.
- Unexpected line breaks.
- Overlap with graphics.
- Titles too large for Chinese/Japanese/German.

Always render and inspect the PDF after replacement.

## Template Mapping

An arbitrary `.ai` file does not describe which text belongs to product identity, safety, operation, service, or fixed page furniture. Reliable automatic layout requires extracted template metadata plus reviewed layout rules. Without layout rules, the workflow can replace live TextFrames but cannot promise semantic page restructuring.

Treat a new Illustrator design as a template-onboarding task. Validate its TextFrame bindings, fixed text, artboards, title/body roles, and overflow policy before customer production.

## Fonts

The script sets a preferred font if available. If not available, Illustrator keeps or substitutes fonts.

Recommended checks:

- Confirm target-language glyphs render.
- Confirm PDF embeds or preserves needed fonts.
- Use separate font choices per language when needed.

## Automation Permissions

On macOS, `osascript` may require permission to control Adobe Illustrator. If automation fails on first run, grant the terminal or agent app permission in System Settings.

## Excel Runtime

The review workbook uses the Codex workspace `@oai/artifact-tool` runtime. Run `manual_workflow.py doctor` after installing the Skill. If the runtime is absent, stop before customer work; the Skill does not silently fall back to a different spreadsheet engine because that could change workbook rendering and validation behavior.

## Source Safety

The apply step opens the source file and saves to a new `.ai`. It should not modify the source, but still keep source files read-only or backed up for customer work.
