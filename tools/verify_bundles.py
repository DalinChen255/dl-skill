#!/usr/bin/env python3
"""校验构建产物。

三条规则：
1. dist/skills/ 里的 zip 必须与插件清单中的 Skill 逐一对应
2. 每个 zip 里绝不允许出现本地运行产物目录（output/、data/、缓存、tests/）
3. 首页必须能打开每个工具的教程，ZIP 根目录须同时包含教程和 SKILL.md
"""
import json
import pathlib
import re
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
FORBIDDEN = ("output/", "data/", ".pytest_cache/", "__pycache__/", "tests/")


def main() -> int:
    plugins = json.loads(
        (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )["plugins"]
    zips = sorted((ROOT / "dist" / "skills").glob("*.zip"))

    expected = {
        pathlib.Path(skill).name for plugin in plugins for skill in plugin["skills"]
    }
    actual = {path.stem for path in zips}
    if actual != expected:
        print(
            f"error: missing bundles: {sorted(expected - actual)}; "
            f"unexpected bundles: {sorted(actual - expected)}",
            file=sys.stderr,
        )
        return 1

    homepage = (ROOT / "README.md").read_text(encoding="utf-8")
    tutorial_links = set(re.findall(r"\]\((?:\./)?(skills/[^/()\s]+/README\.md)(?:#[^)]*)?\)", homepage))
    for zip_path in zips:
        guide = pathlib.Path("skills") / zip_path.stem / "README.md"
        if guide.as_posix() not in tutorial_links or not (ROOT / guide).is_file():
            print(f"error: homepage must link to the tutorial {guide}", file=sys.stderr)
            return 1
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            bad = [n for n in names if n.startswith(FORBIDDEN)]
            if bad:
                print(f"error: {zip_path} contains forbidden paths: {bad[:5]}", file=sys.stderr)
                return 1
            if "SKILL.md" not in names:
                print(f"error: {zip_path} is missing root-level SKILL.md", file=sys.stderr)
                return 1
            if "README.md" not in names or archive.read("README.md") != (ROOT / guide).read_bytes():
                print(f"error: {zip_path} must include the current {guide}", file=sys.stderr)
                return 1

    print(f"ok: {len(zips)} expected skill bundles verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
