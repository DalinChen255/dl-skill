#!/usr/bin/env python3
"""校验版本号一致性。

规则：VERSION 文件是唯一权威版本号，marketplace.json 的 metadata.version
和每个 plugin 的 version 都必须与它完全一致。CI 和发布流程都会跑这个脚本，
任何一处不一致就报错退出，防止版本号漂移。
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main() -> int:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    errors = []

    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        errors.append(f"VERSION 文件内容不是合法的语义化版本号: {version!r}")

    manifest = json.loads(
        (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )

    meta_version = manifest.get("metadata", {}).get("version")
    if meta_version != version:
        errors.append(
            f"marketplace.json metadata.version={meta_version!r} 与 VERSION={version!r} 不一致"
        )

    for plugin in manifest.get("plugins", []):
        name = plugin.get("name", "<unnamed>")
        if plugin.get("version") != version:
            errors.append(
                f"plugin {name} version={plugin.get('version')!r} 与 VERSION={version!r} 不一致"
            )

    if errors:
        for err in errors:
            print(f"error: {err}", file=sys.stderr)
        return 1

    plugin_count = len(manifest.get("plugins", []))
    print(f"ok: VERSION={version}，{plugin_count} 个 plugin 版本号全部一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
