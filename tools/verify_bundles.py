#!/usr/bin/env python3
"""校验构建产物。

三条规则：
1. dist/skills/ 里的 zip 数量必须等于 marketplace.json 登记的 plugin 数量
2. 每个 zip 里绝不允许出现本地运行产物目录（output/、data/、缓存、tests/）
3. 每个 zip 解压后根级必须有 SKILL.md（README 对用户的承诺）
"""
import json
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
FORBIDDEN = ("output/", "data/", ".pytest_cache/", "__pycache__/", "tests/")


def main() -> int:
    plugins = json.loads(
        (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )["plugins"]
    zips = sorted((ROOT / "dist" / "skills").glob("*.zip"))

    if len(zips) != len(plugins):
        print(
            f"error: built {len(zips)} zips but marketplace.json lists {len(plugins)} plugins",
            file=sys.stderr,
        )
        return 1

    for zip_path in zips:
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            bad = [n for n in names if n.startswith(FORBIDDEN)]
            if bad:
                print(f"error: {zip_path} contains forbidden paths: {bad[:5]}", file=sys.stderr)
                return 1
            if "SKILL.md" not in names:
                print(f"error: {zip_path} is missing root-level SKILL.md", file=sys.stderr)
                return 1

    print(f"ok: {len(zips)} bundles verified against {len(plugins)} plugins")
    return 0


if __name__ == "__main__":
    sys.exit(main())
