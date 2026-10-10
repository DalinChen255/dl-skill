#!/usr/bin/env python3
"""校验版本号一致性。

规则：VERSION 文件是唯一权威版本号，marketplace.json 的 metadata.version
和每个 plugin 的 version、已存在的 skills/*/VERSION 都必须与它完全一致。
支持 X.Y.Z 和 X.Y.Z-rc.N（N >= 1），不接受 v 前缀或数字前导零。
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSION_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-rc\.[1-9][0-9]*)?")


def validate_version(version: str) -> None:
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"版本号须为 X.Y.Z 或 X.Y.Z-rc.N（N >= 1），无 v 前缀或前导零: {version!r}")


def read_repository(root: pathlib.Path):
    """Read and validate every target before any caller can write changes."""
    files = [root / "VERSION", root / ".claude-plugin" / "marketplace.json"]
    files.extend(sorted((root / "skills").glob("*/VERSION")))
    originals = {file: file.read_bytes() for file in files}
    texts = {file: content.decode("utf-8") for file, content in originals.items()}
    version = texts[files[0]].strip()
    validate_version(version)
    manifest = json.loads(texts[files[1]])
    if not isinstance(manifest, dict) or not isinstance(manifest.get("metadata"), dict):
        raise ValueError("marketplace.json 须包含 metadata 对象")
    validate_version(manifest["metadata"].get("version"))
    if not isinstance(manifest.get("plugins"), list):
        raise ValueError("marketplace.json plugins 须为数组")
    for plugin in manifest["plugins"]:
        if not isinstance(plugin, dict):
            raise ValueError("marketplace.json plugins 的每一项须为对象")
        validate_version(plugin.get("version"))
    skill_versions = {file: texts[file].strip() for file in files[2:]}
    for file, skill_version in skill_versions.items():
        try:
            validate_version(skill_version)
        except ValueError as error:
            raise ValueError(f"{file.relative_to(root)}: {error}") from error
    return version, manifest, skill_versions, originals


def main() -> int:
    try:
        version, manifest, skill_versions, _ = read_repository(ROOT)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    errors = []
    meta_version = manifest.get("metadata", {}).get("version")
    if meta_version != version:
        errors.append(
            f"marketplace.json metadata.version={meta_version!r} 与 VERSION={version!r} 不一致"
        )

    for plugin in manifest["plugins"]:
        name = plugin.get("name", "<unnamed>")
        if plugin.get("version") != version:
            errors.append(
                f"plugin {name} version={plugin.get('version')!r} 与 VERSION={version!r} 不一致"
            )

    for file, skill_version in skill_versions.items():
        if skill_version != version:
            errors.append(
                f"{file.relative_to(ROOT)}={skill_version!r} 与 VERSION={version!r} 不一致"
            )

    if errors:
        for err in errors:
            print(f"error: {err}", file=sys.stderr)
        return 1

    plugin_count = len(manifest.get("plugins", []))
    print(f"ok: VERSION={version}，{plugin_count} 个 plugin、{len(skill_versions)} 个 Skill VERSION 全部一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
