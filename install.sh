#!/usr/bin/env bash
set -euo pipefail

SKILL_NAME="illustrator-manual-translator"
ZIP_NAME="illustrator-manual-translator-skill.zip"
DEFAULT_ZIP_URL="https://github.com/HiOne0826/illustrator-manual-translator-skill/releases/latest/download/${ZIP_NAME}"
ZIP_URL="${ILLUSTRATOR_MANUAL_TRANSLATOR_ZIP_URL:-$DEFAULT_ZIP_URL}"
TARGET_ROOT="${1:-${CODEX_HOME:-$HOME/.codex}/skills}"
RUN_DOCTOR="${ILLUSTRATOR_MANUAL_TRANSLATOR_RUN_DOCTOR:-1}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_cmd curl
require_cmd unzip
require_cmd python3

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

mkdir -p "$TARGET_ROOT"

echo "Downloading ${SKILL_NAME} from:"
echo "$ZIP_URL"
curl -fsSL "$ZIP_URL" -o "$TMP_DIR/$ZIP_NAME"

mkdir -p "$TMP_DIR/unpacked"
unzip -q "$TMP_DIR/$ZIP_NAME" -d "$TMP_DIR/unpacked"

SOURCE_DIR=""
if [ -f "$TMP_DIR/unpacked/$SKILL_NAME/SKILL.md" ]; then
  SOURCE_DIR="$TMP_DIR/unpacked/$SKILL_NAME"
elif [ -f "$TMP_DIR/unpacked/skills/$SKILL_NAME/SKILL.md" ]; then
  SOURCE_DIR="$TMP_DIR/unpacked/skills/$SKILL_NAME"
else
  FOUND_SKILL_MD="$(find "$TMP_DIR/unpacked" -type f -name SKILL.md -path "*/$SKILL_NAME/SKILL.md" -print -quit)"
  if [ -n "$FOUND_SKILL_MD" ]; then
    SOURCE_DIR="$(dirname "$FOUND_SKILL_MD")"
  fi
fi

if [ -z "$SOURCE_DIR" ] || [ ! -f "$SOURCE_DIR/SKILL.md" ]; then
  echo "Could not find $SKILL_NAME/SKILL.md in downloaded archive." >&2
  exit 1
fi

TARGET_DIR="$TARGET_ROOT/$SKILL_NAME"
if [ -d "$TARGET_DIR" ]; then
  BACKUP_DIR="$TARGET_DIR.backup.$(date +%Y%m%d%H%M%S)"
  echo "Existing skill found. Moving it to $BACKUP_DIR"
  mv "$TARGET_DIR" "$BACKUP_DIR"
fi

cp -R "$SOURCE_DIR" "$TARGET_DIR"

echo "Installed to $TARGET_DIR"

if [ "$RUN_DOCTOR" = "1" ]; then
  python3 "$TARGET_DIR/scripts/illustrator_manual_workflow.py" doctor
else
  echo "Skipped doctor check because ILLUSTRATOR_MANUAL_TRANSLATOR_RUN_DOCTOR=$RUN_DOCTOR"
fi

echo "Done."
