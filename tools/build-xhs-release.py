#!/usr/bin/env python3
"""Build only the reviewed public XHS skill files; never package Git/runtime data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

PUBLIC_FILES = [
    ".gitignore", "SKILL.md", "README.md", "VERSION", "CHANGELOG.md", "LICENSE",
    "config.example.json", "agents/openai.yaml",
    "examples/users.example.csv", "examples/run.example.json",
    "references/browser-contract.md", "references/eligibility-rules.md",
    "references/execution-runbook.md", "references/batch-design.md",
    "scripts/daily_import.py", "scripts/daily_import.test.py",
    "scripts/daily_followup_state.py", "scripts/daily_followup_state.test.py",
    "scripts/followup_analysis.py", "scripts/followup_analysis.test.py",
    "scripts/daily_followup_perf.py", "scripts/self_check.py",
    "scripts/xhs_business_review.mjs", "scripts/xhs_business_review.test.mjs",
    "scripts/xhs_followup_cua_adapter.mjs", "scripts/xhs_followup_cua_adapter.test.mjs",
    "scripts/xhs_followup_local_bridge.mjs", "scripts/xhs_followup_local_bridge.test.mjs",
    "scripts/xhs_followup_workflow.mjs", "scripts/xhs_followup_workflow.test.mjs",
]


def checked_files(root: Path) -> dict[str, bytes]:
    if root.is_symlink():
        raise ValueError("skill root must be a real directory, not a symlink")
    found = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("symlink rejected: " + str(path.relative_to(root)))
        if path.is_file():
            found.add(path.relative_to(root).as_posix())
    expected = set(PUBLIC_FILES)
    if found != expected:
        raise ValueError("release file list mismatch: " + json.dumps({
            "missing": sorted(expected - found), "unexpected": sorted(found - expected)}))
    values = {}
    for name in PUBLIC_FILES:
        data = (root / name).read_bytes()
        text = data.decode("utf-8")
        if re.search(r"/Users/[^\s/'\"]+|/home/[^\s/'\"]+|[A-Za-z]:\\Users\\", text):
            raise ValueError("personal absolute path in " + name)
        if re.search(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{20,}", text):
            raise ValueError("possible credential in " + name)
        values[name] = data
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-root", type=Path, default=Path(__file__).resolve().parents[1] / "skills/dl-xhs-winback")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    # Keep lexical root until symlink validation has run.
    root = args.skill_root.absolute()
    values = checked_files(root)
    version = values["VERSION"].decode().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", version):
        raise ValueError("invalid release version")
    output = args.output.resolve()
    if output == root.resolve() or root.resolve() in output.parents:
        raise ValueError("output must be outside the skill directory")
    output.mkdir(parents=True, exist_ok=True)
    archive_path = output / f"dl-xhs-winback-{version}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(values.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    (output / (archive_path.name + ".sha256")).write_text(f"{digest}  {archive_path.name}\n")
    manifest = {"version": version, "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(values.items())},
                "archive_sha256": digest, "note": "File/content checks do not replace human privacy review or live acceptance."}
    (output / "release-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"ok": True, "version": version, "files": len(values), "archive": str(archive_path), "sha256": digest}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
