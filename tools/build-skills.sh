#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-"$ROOT_DIR/dist/skills"}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 command is required" >&2
  exit 1
fi

if ! git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "error: must run inside the git repository (packaging is based on git-tracked files)" >&2
  exit 1
fi

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

build_one() {
  local skill_dir="$1"
  local name
  name="$(basename "$skill_dir")"

  # 只打包被 git 跟踪的文件：本地运行产物（output/、data/、缓存等）从源头上进不了发布包
  python3 - "$ROOT_DIR" "skills/$name" "$OUT_DIR/${name}.zip" <<'PY'
import os
import subprocess
import sys
import zipfile

root, prefix, archive_path = sys.argv[1], sys.argv[2], sys.argv[3]
out = subprocess.run(
    ["git", "-C", root, "ls-files", "-z", "--", prefix],
    check=True, capture_output=True,
).stdout
tracked = [p.decode("utf-8") for p in out.split(b"\0") if p]
if not tracked:
    sys.exit(f"error: no git-tracked files under {prefix}")
skip_top_dirs = {"tests"}
skip_files = {".gitignore"}

with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for repo_path in sorted(tracked):
        rel = os.path.relpath(repo_path, prefix)
        if rel.split(os.sep)[0] in skip_top_dirs or os.path.basename(rel) in skip_files:
            continue
        archive.write(os.path.join(root, repo_path), rel)
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
