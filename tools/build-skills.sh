#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-"$ROOT_DIR/dist/skills"}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 command is required" >&2
  exit 1
fi

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

build_one() {
  local skill_dir="$1"
  local name
  name="$(basename "$skill_dir")"

  python3 - "$skill_dir" "$OUT_DIR/${name}.zip" <<'PY'
import os
import sys
import zipfile

source_dir, archive_path = sys.argv[1], sys.argv[2]
skip_files = {"config.json", "overrides.json", ".gitignore", ".DS_Store"}
skip_dirs = {"__pycache__"}

with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for root, dirs, files in os.walk(source_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for filename in sorted(files):
            if filename in skip_files or filename.endswith(".pyc"):
                continue
            path = os.path.join(root, filename)
            archive.write(path, os.path.relpath(path, source_dir))
PY

  echo "built skills/${name}.zip"
}

for skill_md in "$ROOT_DIR"/skills/*/SKILL.md; do
  skill_dir="$(dirname "$skill_md")"
  skill_name="$(basename "$skill_dir")"

  if [[ "$skill_name" == *beta* ]]; then
    echo "skipped local-only beta skill: $skill_name"
    continue
  fi

  build_one "$skill_dir"
done

echo
echo "done: $OUT_DIR"
