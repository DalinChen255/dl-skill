#!/usr/bin/env python3
"""Synchronize existing version mirrors; does not commit, tag, or push.

Usage: python3 tools/set_version.py 1.2.2
README and CHANGELOG remain manual editorial work.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from check_versions import ROOT, read_repository, validate_version


def set_version(root: Path, version: str) -> None:
    validate_version(version)
    _, manifest, skill_versions, originals = read_repository(root)
    manifest["metadata"]["version"] = version
    for plugin in manifest["plugins"]:
        plugin["version"] = version
    updates = {root / "VERSION": (version + "\n").encode("utf-8")}
    updates[root / ".claude-plugin" / "marketplace.json"] = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    updates.update({file: (version + "\n").encode("utf-8") for file in skill_versions})

    # Finish all reads, validation and staging before replacing a target file.
    staged = {}
    try:
        for file, content in updates.items():
            if content == originals[file]:
                continue
            with tempfile.NamedTemporaryFile(dir=file.parent, prefix=f".{file.name}.", delete=False) as temporary:
                staged[file] = Path(temporary.name)
                temporary.write(content)
            staged[file].chmod(file.stat().st_mode & 0o777)
        for file, temporary in staged.items():
            os.replace(temporary, file)
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="X.Y.Z 或 X.Y.Z-rc.N（N >= 1）")
    args = parser.parse_args()
    try:
        set_version(ROOT, args.version)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"ok: 版本已同步为 {args.version}；请另行更新发布说明并运行测试")
    return 0


if __name__ == "__main__":
    sys.exit(main())
