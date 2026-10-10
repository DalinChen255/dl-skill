#!/usr/bin/env python3
"""Offline installation check. Uses synthetic data and a temporary ledger only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    checks = []
    try:
        if sys.version_info < (3, 9):
            raise RuntimeError("Python 3.9 or newer is required")
        node = shutil.which("node")
        if not node:
            raise RuntimeError("Node.js is missing; install Node.js 24 and retry")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        # Do not inherit a user's production config/database for a local check.
        env.pop("XHS_FOLLOWUP_STATE_DB", None)
        env.pop("XHS_FOLLOWUP_CONFIG", None)
        node_version = subprocess.check_output([node, "--version"], text=True).strip()
        if int(node_version.lstrip("v").split(".")[0]) < 24:
            raise RuntimeError("This package is validated with Node.js 24; use Node.js 24 or newer")
        checks.append("python_and_node_available")
        with tempfile.TemporaryDirectory(prefix="xhs-offline-check-") as temporary:
            private = Path(temporary)
            db = private / "state.sqlite3"
            env["XHS_FOLLOWUP_CONFIG"] = str(private / "no-config.json")

            def command(script, *args):
                result = subprocess.run([sys.executable, str(root / "scripts" / script), *map(str, args)],
                                        env=env, capture_output=True, text=True, timeout=30)
                if result.returncode:
                    raise RuntimeError(f"{script} failed: {result.stdout.strip() or result.stderr.strip()}")
                data = json.loads(result.stdout)
                if data.get("ok") is False:
                    raise RuntimeError(f"{script} returned an error")
                return data

            imported = command("daily_import.py", "--db", db, "--file", root / "examples/users.example.csv")
            if imported["imports"][0]["unique_users"] != 2:
                raise RuntimeError("synthetic import count mismatch")
            payload = json.loads((root / "examples/run.example.json").read_text())
            payload["source_import_ids"] = imported["source_import_ids"]
            input_path = private / "run-input.json"
            input_path.write_text(json.dumps(payload), encoding="utf-8")
            run = command("daily_followup_state.py", "--db", db, "register-run", "--input-json", input_path)
            if run.get("initialized") is not True:
                raise RuntimeError("temporary queue was not initialized")
            command("daily_followup_state.py", "--db", db, "run-status", "--run-id", run["run_id"])
            command("daily_followup_state.py", "--db", db, "report", "--run-id", run["run_id"])
            checks.append("synthetic_import_registration_and_readback")
            modules = ["xhs_business_review.mjs", "xhs_followup_cua_adapter.mjs",
                       "xhs_followup_local_bridge.mjs", "xhs_followup_workflow.mjs"]
            module_code = "\n".join(f"await import({json.dumps((root / 'scripts' / name).as_uri())});" for name in modules)
            subprocess.run([node, "--input-type=module", "-e", module_code], env=env,
                           capture_output=True, text=True, check=True, timeout=30)
            checks.append("javascript_modules_importable")
        print(json.dumps({"ok": True, "version": (root / "VERSION").read_text().strip(),
                          "python": sys.version.split()[0], "node": node_version, "checks": checks,
                          "browser_verified": False, "messages_sent": 0, "labels_changed": 0,
                          "note": "Offline checks passed. CUA access, live UI, sending and label writes require separate verification."},
                         ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"ok": False, "error": str(error), "checks": checks,
                          "browser_verified": False, "messages_sent": 0, "labels_changed": 0}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
