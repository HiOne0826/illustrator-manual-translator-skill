#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_NAME="illustrator-manual-translator"
DIST_DIR="$ROOT_DIR/dist"
ZIP_PATH="$DIST_DIR/illustrator-manual-translator-skill.zip"

if [ ! -f "$ROOT_DIR/skills/$SKILL_NAME/SKILL.md" ]; then
  echo "Missing skill: $ROOT_DIR/skills/$SKILL_NAME/SKILL.md" >&2
  exit 1
fi

mkdir -p "$DIST_DIR"
rm -f "$ZIP_PATH"

cd "$ROOT_DIR/skills"
zip -qr "$ZIP_PATH" "$SKILL_NAME"

echo "$ZIP_PATH"

