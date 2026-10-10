#!/usr/bin/env python3
"""Exercise version commands against disposable repositories only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parent


class VersionCommandsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "tools").mkdir()
        for script in ("check_versions.py", "set_version.py"):
            if (TOOLS / script).exists():
                shutil.copy2(TOOLS / script, self.root / "tools" / script)
        (self.root / ".claude-plugin").mkdir()
        (self.root / "skills" / "with-version").mkdir(parents=True)
        (self.root / "skills" / "without-version").mkdir()
        self.write_version("1.2.1")

    def write_version(self, version):
        (self.root / "VERSION").write_text(version + "\n", encoding="utf-8")
        self.write_manifest({
            "name": "fixture",
            "metadata": {"version": version, "description": "保留说明"},
            "plugins": [
                {"name": "first", "version": version, "source": "./"},
                {"name": "second", "version": version, "strict": False},
            ],
        })
        (self.root / "skills" / "with-version" / "VERSION").write_text(
            version + "\n", encoding="utf-8"
        )

    def write_manifest(self, manifest):
        (self.root / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def manifest(self):
        return json.loads((self.root / ".claude-plugin" / "marketplace.json").read_text())

    def run_command(self, script="check_versions.py", *args):
        return subprocess.run(
            [sys.executable, str(self.root / "tools" / script), *args],
            cwd=self.temp.name,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True, capture_output=True, check=False,
        )

    def snapshot(self):
        return {
            p.relative_to(self.root).as_posix(): p.read_bytes()
            for p in self.root.rglob("*") if p.is_file()
        }

    def test_stable_and_rc_versions_are_accepted(self):
        for version in ("0.0.0", "1.2.2", "1.2.2-rc.1", "20.10.0-rc.12"):
            with self.subTest(version=version):
                self.write_version(version)
                result = self.run_command()
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_skill_version_drift_is_rejected(self):
        (self.root / "skills" / "with-version" / "VERSION").write_text("0.1.0-rc.1\n")
        result = self.run_command()
        self.assertEqual(result.returncode, 1)
        self.assertIn("skills/with-version/VERSION", result.stderr)

    def test_manifest_metadata_drift_is_rejected(self):
        manifest = self.manifest()
        manifest["metadata"]["version"] = "1.2.0"
        self.write_manifest(manifest)
        self.assertEqual(self.run_command().returncode, 1)

    def test_any_plugin_version_drift_is_rejected(self):
        manifest = self.manifest()
        manifest["plugins"][1]["version"] = "1.2.0"
        self.write_manifest(manifest)
        self.assertEqual(self.run_command().returncode, 1)

    def test_root_drift_is_rejected(self):
        (self.root / "VERSION").write_text("1.3.0\n")
        self.assertEqual(self.run_command().returncode, 1)

    def test_invalid_versions_are_rejected(self):
        for version in ("v1.2.2", "01.2.2", "1.02.2", "1.2.02", "1.2", "1.2.2-rc.0",
                        "1.2.2-rc.01", "1.2.2-rc.-1", "1.2.2-beta.1", "1.2.2+build", "", "１.2.2"):
            with self.subTest(version=version):
                self.write_version(version)
                self.assertEqual(self.run_command().returncode, 1)

    def test_skills_without_version_files_are_supported_and_not_created(self):
        (self.root / "skills" / "with-version" / "VERSION").unlink()
        before = self.snapshot()
        self.assertEqual(self.run_command().returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_setter_synchronizes_existing_versions_for_rc_and_stable(self):
        for version in ("1.2.2-rc.1", "1.2.2"):
            with self.subTest(version=version):
                result = self.run_command("set_version.py", version)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.root / "VERSION").read_text(), version + "\n")
                self.assertEqual((self.root / "skills/with-version/VERSION").read_text(), version + "\n")
                manifest = self.manifest()
                self.assertEqual(manifest["metadata"]["version"], version)
                self.assertEqual([p["version"] for p in manifest["plugins"]], [version, version])
                self.assertEqual(self.run_command().returncode, 0)
                self.assertFalse((self.root / "skills/without-version/VERSION").exists())

    def test_setter_preserves_other_manifest_fields_and_unrelated_files(self):
        for name in ("README.md", "CHANGELOG.md", ".git/HEAD", "skills/without-version/SKILL.md"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("unchanged 1.2.1\n")
        before = self.snapshot()
        manifest_before = self.manifest()
        result = self.run_command("set_version.py", "1.2.2-rc.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.snapshot()
        allowed = {"VERSION", ".claude-plugin/marketplace.json", "skills/with-version/VERSION"}
        self.assertEqual(before.keys(), after.keys())
        self.assertEqual({k: v for k, v in before.items() if k not in allowed},
                         {k: v for k, v in after.items() if k not in allowed})
        manifest_before["metadata"]["version"] = "1.2.2-rc.1"
        for plugin in manifest_before["plugins"]:
            plugin["version"] = "1.2.2-rc.1"
        self.assertEqual(self.manifest(), manifest_before)

    def test_setter_rejects_invalid_input_without_writes(self):
        for version in ("v1.2.2", "1.2.2-rc.0", "1.2.2-rc.01", "1.02.2", "", "1.2.2\n"):
            with self.subTest(version=version):
                before = self.snapshot()
                self.assertNotEqual(self.run_command("set_version.py", version).returncode, 0)
                self.assertEqual(self.snapshot(), before)

    def test_setter_preflights_malformed_manifest_without_writes(self):
        for malformed in ("{", '[]', '{"metadata":{},"plugins":{}}',
                          '{"metadata":{"version":"1.2.1"},"plugins":[null]}'):
            with self.subTest(manifest=malformed):
                (self.root / ".claude-plugin/marketplace.json").write_text(malformed)
                before = self.snapshot()
                self.assertNotEqual(self.run_command("set_version.py", "1.2.2").returncode, 0)
                self.assertEqual(self.snapshot(), before)

    def test_setter_preflights_unreadable_skill_file_without_writes(self):
        (self.root / "skills/with-version/VERSION").write_bytes(b"\xff")
        before = self.snapshot()
        result = self.run_command("set_version.py", "1.2.2")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_setter_without_any_skill_version_keeps_them_absent(self):
        (self.root / "skills/with-version/VERSION").unlink()
        result = self.run_command("set_version.py", "1.2.2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list((self.root / "skills").glob("*/VERSION")), [])


if __name__ == "__main__":
    unittest.main()
