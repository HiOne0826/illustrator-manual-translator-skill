#!/usr/bin/env bash
set -euo pipefail

SKILL_NAME="illustrator-manual-translator"
ZIP_NAME="illustrator-manual-translator-skill.zip"
GITHUB_REPO="HiOne0826/illustrator-manual-translator-skill"
DEFAULT_ZIP_URL="https://github.com/HiOne0826/illustrator-manual-translator-skill/releases/latest/download/${ZIP_NAME}"
ZIP_URL="${ILLUSTRATOR_MANUAL_TRANSLATOR_ZIP_URL:-$DEFAULT_ZIP_URL}"
TARGET_ROOT="${1:-${CODEX_HOME:-$HOME/.codex}/skills}"
RUN_DOCTOR="${ILLUSTRATOR_MANUAL_TRANSLATOR_RUN_DOCTOR:-1}"
GITHUB_AUTH_TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_cmd curl
require_cmd unzip
require_cmd python3

download_with_github_api() {
  RELEASE_JSON="$TMP_DIR/latest-release.json"
  curl \
    -H "Authorization: Bearer ${GITHUB_AUTH_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -fsSL \
    "https://api.github.com/repos/${GITHUB_REPO}/releases/latest" \
    -o "$RELEASE_JSON"

  ASSET_API_URL="$(python3 - "$RELEASE_JSON" "$ZIP_NAME" <<'PY'
import json
import sys

release_path, asset_name = sys.argv[1], sys.argv[2]
with open(release_path, "r", encoding="utf-8") as f:
    release = json.load(f)

for asset in release.get("assets", []):
    if asset.get("name") == asset_name:
        print(asset["url"])
        break
else:
    raise SystemExit(f"Asset not found in latest release: {asset_name}")
PY
)"

  curl \
    -H "Authorization: Bearer ${GITHUB_AUTH_TOKEN}" \
    -H "Accept: application/octet-stream" \
    -L \
    -fsSL \
    "$ASSET_API_URL" \
    -o "$TMP_DIR/$ZIP_NAME"
}

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

mkdir -p "$TARGET_ROOT"

echo "Downloading ${SKILL_NAME} from:"
echo "$ZIP_URL"
if [ -n "$GITHUB_AUTH_TOKEN" ] && [ "$ZIP_URL" = "$DEFAULT_ZIP_URL" ]; then
  download_with_github_api
elif [ -n "$GITHUB_AUTH_TOKEN" ]; then
  curl -H "Authorization: Bearer ${GITHUB_AUTH_TOKEN}" -fsSL "$ZIP_URL" -o "$TMP_DIR/$ZIP_NAME"
else
  curl -fsSL "$ZIP_URL" -o "$TMP_DIR/$ZIP_NAME"
fi

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
  python3 "$TARGET_DIR/scripts/manual_workflow.py" doctor
else
  echo "Skipped doctor check because ILLUSTRATOR_MANUAL_TRANSLATOR_RUN_DOCTOR=$RUN_DOCTOR"
fi

echo "Done."
