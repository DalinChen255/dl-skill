#!/usr/bin/env python3
"""Fast, conservative daily XHS follow-up ledger.

All commands emit JSON. Browser evidence is supplied by the caller; this module
never opens a browser or sends a message. The default database is private to the
Work skill and legacy imports are explicit, read-only operations on source DBs.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = SKILL_DIR.parent / "private" / "state.sqlite3"
DAY_MS = 86_400_000
OBSERVATION_TAIL_MS = 7 * DAY_MS
OBSERVATION_FRESH_MS = 120_000
ACCOUNT_MODES = {"all", "specified"}
SOURCES = {"ad", "natural", "unknown"}
LABELS = {"none", "lead", "no_intent", "unknown"}
REPLY_STATES = {"awaiting_customer", "awaiting_us", "unknown"}
NEGATIVE_STATES = {"safe", "blocked", "unknown"}
PARTY_STATES = {"customer", "peer", "unknown"}
TOUCH_POLICIES = {"uid_once", "account_once"}
_NEXT_EXCLUSIONS_UNSET = object()


def epoch_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _shanghai_day(at_ms: int) -> str:
    return datetime.fromtimestamp(at_ms / 1000, timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()


def canonical_copy(text: str) -> str:
    """Normalize whitespace only; punctuation and wording remain meaningful."""
    return re.sub(r"\s+", " ", text).strip()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _array(raw: str | None) -> list:
    try:
        value = json.loads(raw or "[]")
        return value if isinstance(value, list) else []
    except json.JSONDecodeError:
        return []


def connect_readonly(path: Path) -> sqlite3.Connection:
    """Inspect an existing ledger without creating files, migrations or DML."""
    conn = sqlite3.connect(Path(path).expanduser().resolve().as_uri() + "?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.resolve() == DEFAULT_DB.parent.resolve():
        os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS historical_imports (
            import_id TEXT PRIMARY KEY, source_file TEXT NOT NULL,
            cutoff_date TEXT NOT NULL, imported_at_ms INTEGER NOT NULL,
            row_count INTEGER NOT NULL, unique_user_ids INTEGER NOT NULL,
            lead_users INTEGER NOT NULL, targetable_no_lead_users INTEGER NOT NULL,
            deleted_users INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS historical_user_accounts (
            user_id TEXT NOT NULL, account_name TEXT NOT NULL,
            account_id TEXT NOT NULL DEFAULT '', display_name TEXT NOT NULL DEFAULT '',
            latest_in_at_ms INTEGER, latest_open_at_ms INTEGER,
            latest_lead_at_ms INTEGER, phone_present INTEGER NOT NULL DEFAULT 0,
            wechat_present INTEGER NOT NULL DEFAULT 0,
            lead_status_from_import TEXT NOT NULL DEFAULT 'no_lead',
            deleted INTEGER NOT NULL DEFAULT 0, user_type TEXT NOT NULL DEFAULT '',
            source_import_ids_json TEXT NOT NULL DEFAULT '[]',
            updated_at_ms INTEGER NOT NULL,
            PRIMARY KEY(user_id, account_name)
        );
        CREATE INDEX IF NOT EXISTS idx_daily_legacy_accounts
            ON historical_user_accounts(account_id, user_id);
        CREATE TABLE IF NOT EXISTS message_versions (
            version_id TEXT PRIMARY KEY, source TEXT NOT NULL,
            exact_text TEXT NOT NULL, exact_hash TEXT NOT NULL,
            normalized_hash TEXT NOT NULL, created_at_ms INTEGER NOT NULL,
            UNIQUE(source, exact_hash)
        );
        CREATE TABLE IF NOT EXISTS contact_protections (
            user_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
            observed_at_ms INTEGER NOT NULL, evidence TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS no_intent_updates (
            run_id TEXT NOT NULL, user_id TEXT NOT NULL, account_id TEXT NOT NULL,
            evidence TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
            observed_at_ms INTEGER NOT NULL, detail TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(run_id,user_id,account_id)
        );
        CREATE TABLE IF NOT EXISTS daily_runs (
            run_id TEXT PRIMARY KEY, started_at_ms INTEGER NOT NULL,
            scope_mode TEXT NOT NULL, account_ids_json TEXT NOT NULL,
            messages_json TEXT NOT NULL, version_ids_json TEXT NOT NULL,
            source_import_id TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS remaining_run_authorizations (
            run_id TEXT PRIMARY KEY REFERENCES daily_runs(run_id),
            authorized_at_ms INTEGER NOT NULL, evidence TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_run_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES daily_runs(run_id),
            event TEXT NOT NULL, recorded_at_ms INTEGER NOT NULL,
            evidence TEXT NOT NULL, before_json TEXT NOT NULL,
            after_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_run_items (
            run_id TEXT NOT NULL REFERENCES daily_runs(run_id),
            user_id TEXT NOT NULL, account_ids_json TEXT NOT NULL,
            preferred_account_id TEXT NOT NULL,
            latest_in_at_ms INTEGER, observation_only INTEGER NOT NULL,
            legacy_lead_protection INTEGER NOT NULL,
            last_action TEXT NOT NULL DEFAULT 'pending',
            last_reason TEXT NOT NULL DEFAULT '',
            last_source TEXT NOT NULL DEFAULT '', checked_at_ms INTEGER,
            browser_defer_stage TEXT NOT NULL DEFAULT '',
            slow_retry_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(run_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS daily_execution_sessions (
            session_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES daily_runs(run_id),
            started_at_ms INTEGER NOT NULL, ended_at_ms INTEGER,
            baseline_json TEXT NOT NULL, end_counts_json TEXT,
            end_detail TEXT NOT NULL DEFAULT ''
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_session_active
            ON daily_execution_sessions(run_id) WHERE ended_at_ms IS NULL;
        CREATE TABLE IF NOT EXISTS daily_item_corrections (
            correction_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, user_id TEXT NOT NULL,
            corrected_at_ms INTEGER NOT NULL, evidence TEXT NOT NULL,
            before_json TEXT NOT NULL, after_json TEXT NOT NULL,
            request_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_daily_run_items_action
            ON daily_run_items(run_id,last_action);
        CREATE TABLE IF NOT EXISTS daily_run_account_results (
            run_id TEXT NOT NULL, user_id TEXT NOT NULL, account_id TEXT NOT NULL,
            action TEXT NOT NULL, reason TEXT NOT NULL,
            checked_at_ms INTEGER NOT NULL,
            PRIMARY KEY(run_id,user_id,account_id)
        );
        CREATE TABLE IF NOT EXISTS daily_browser_deferrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL, user_id TEXT NOT NULL,
            reason TEXT NOT NULL, retry_stage TEXT NOT NULL,
            observed_at_ms INTEGER NOT NULL, recorded_at_ms INTEGER NOT NULL,
            evidence TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_daily_browser_deferrals_run
            ON daily_browser_deferrals(run_id,user_id,retry_stage);
        CREATE TABLE IF NOT EXISTS conversation_history_cache (
            user_id TEXT NOT NULL, account_id TEXT NOT NULL,
            boundary_message_id TEXT NOT NULL DEFAULT '',
            outbound_text_hashes_json TEXT NOT NULL DEFAULT '[]',
            verified_at_ms INTEGER NOT NULL,
            PRIMARY KEY(user_id,account_id)
        );
        CREATE TABLE IF NOT EXISTS lead_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            user_id TEXT NOT NULL, account_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('present','absent','unknown')),
            label TEXT NOT NULL, source TEXT NOT NULL,
            attribution_state TEXT NOT NULL DEFAULT 'unknown',
            observed_at_ms INTEGER NOT NULL,
            evidence TEXT NOT NULL DEFAULT '',
            manual_outbound_at_ms INTEGER,
            observation_key TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_lead_obs_uid_time
            ON lead_observations(user_id,observed_at_ms DESC);
        CREATE TABLE IF NOT EXISTS followup_tracking (
            user_id TEXT PRIMARY KEY,
            first_pre_send_absent_at_ms INTEGER,
            first_source TEXT,
            first_sent_at_ms INTEGER,
            last_sent_at_ms INTEGER,
            first_tag_observed_at_ms INTEGER,
            first_tag_run_id TEXT,
            first_tag_account_id TEXT,
            last_observed_at_ms INTEGER,
            last_observation_status TEXT,
            manual_intervention INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_followup_tracking_tail
            ON followup_tracking(last_sent_at_ms,first_tag_observed_at_ms);
        CREATE TABLE IF NOT EXISTS daily_touches (
            touch_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES daily_runs(run_id),
            user_id TEXT NOT NULL, account_id TEXT NOT NULL,
            source TEXT NOT NULL, version_id TEXT NOT NULL REFERENCES message_versions(version_id),
            message_snapshot TEXT NOT NULL, normalized_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('reserved','clicked','sent','failed','uncertain')),
            prepared_at_ms INTEGER NOT NULL, clicked_at_ms INTEGER,
            finished_at_ms INTEGER, detail TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            pre_send_absent_at_ms INTEGER NOT NULL,
            manual_intervention INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_daily_touches_uid_time
            ON daily_touches(user_id,prepared_at_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_daily_touches_uid_sent_time
            ON daily_touches(user_id,state,finished_at_ms DESC,account_id);
        CREATE INDEX IF NOT EXISTS idx_daily_touches_run_state
            ON daily_touches(run_id,state);
        CREATE INDEX IF NOT EXISTS idx_daily_touches_state
            ON daily_touches(state);
        CREATE TABLE IF NOT EXISTS legacy_send_events (
            fingerprint TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            account_id TEXT NOT NULL, status TEXT NOT NULL,
            attempted_at_ms INTEGER NOT NULL, message_snapshot TEXT NOT NULL,
            normalized_hash TEXT NOT NULL, exact_hash TEXT NOT NULL,
            campaign_id TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_legacy_send_uid_time
            ON legacy_send_events(user_id,attempted_at_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_legacy_send_uid_sent_time
            ON legacy_send_events(user_id,status,attempted_at_ms DESC,account_id);
        CREATE INDEX IF NOT EXISTS idx_legacy_send_status_time
            ON legacy_send_events(status,attempted_at_ms,user_id,account_id);
        CREATE TABLE IF NOT EXISTS legacy_account_snapshots (
            snapshot_id TEXT PRIMARY KEY, source_path TEXT NOT NULL,
            user_id TEXT NOT NULL, account_name TEXT NOT NULL,
            account_id TEXT NOT NULL, row_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_legacy_snapshot_uid
            ON legacy_account_snapshots(user_id,account_id);
        CREATE TABLE IF NOT EXISTS legacy_account_conflicts (
            user_id TEXT NOT NULL, account_name TEXT NOT NULL,
            existing_account_id TEXT NOT NULL,
            incoming_account_id TEXT NOT NULL,
            source_path TEXT NOT NULL,
            PRIMARY KEY(user_id,account_name,incoming_account_id,source_path)
        );
        CREATE INDEX IF NOT EXISTS idx_legacy_conflicts_uid
            ON legacy_account_conflicts(user_id);
        CREATE TABLE IF NOT EXISTS legacy_row_snapshots (
            snapshot_id TEXT PRIMARY KEY, source_path TEXT NOT NULL,
            table_name TEXT NOT NULL, row_json TEXT NOT NULL,
            snapshotted_at_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_legacy_row_source_table
            ON legacy_row_snapshots(source_path,table_name);
        """
    )
    _upgrade_observation_schema(conn)
    if "source_import_id" not in _columns(conn, "daily_runs"):
        conn.execute("ALTER TABLE daily_runs ADD COLUMN source_import_id TEXT NOT NULL DEFAULT ''")
    if "last_source" not in _columns(conn, "daily_run_items"):
        conn.execute("ALTER TABLE daily_run_items ADD COLUMN last_source TEXT NOT NULL DEFAULT ''")
    if "browser_defer_stage" not in _columns(conn, "daily_run_items"):
        conn.execute("ALTER TABLE daily_run_items ADD COLUMN browser_defer_stage TEXT NOT NULL DEFAULT ''")
    if "slow_retry_count" not in _columns(conn, "daily_run_items"):
        conn.execute("ALTER TABLE daily_run_items ADD COLUMN slow_retry_count INTEGER NOT NULL DEFAULT 0")
    if "business_reviewed" not in _columns(conn, "conversation_history_cache"):
        conn.execute("ALTER TABLE conversation_history_cache ADD COLUMN business_reviewed INTEGER NOT NULL DEFAULT 0")
    if "queue_policy_version" not in _columns(conn, "daily_runs"):
        conn.execute("ALTER TABLE daily_runs ADD COLUMN queue_policy_version INTEGER NOT NULL DEFAULT 0")
    if "source_import_ids_json" not in _columns(conn, "daily_runs"):
        conn.execute("ALTER TABLE daily_runs ADD COLUMN source_import_ids_json TEXT NOT NULL DEFAULT '[]'")
    if "touch_policy" not in _columns(conn, "daily_runs"):
        # Existing runs preserve their actual account-level authorization.
        # register_run explicitly supplies the new UID-level default.
        conn.execute("ALTER TABLE daily_runs ADD COLUMN touch_policy TEXT NOT NULL DEFAULT 'account_once'")
    if "ended_at_ms" not in _columns(conn, "daily_runs"):
        conn.execute("ALTER TABLE daily_runs ADD COLUMN ended_at_ms INTEGER")
    if "baseline_sent_touch_ids_json" not in _columns(conn, "daily_execution_sessions"):
        conn.execute("ALTER TABLE daily_execution_sessions ADD COLUMN baseline_sent_touch_ids_json TEXT")
    if "end_metrics_json" not in _columns(conn, "daily_execution_sessions"):
        conn.execute("ALTER TABLE daily_execution_sessions ADD COLUMN end_metrics_json TEXT")
    if "reason" not in _columns(conn, "daily_touches"):
        conn.execute("ALTER TABLE daily_touches ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
        # Older code stored an outcome reason only on the current run item.
        # Preserve it for the latest matching touch without inferring a cause
        # from free-text evidence or assigning it to older attempts.
        conn.execute("""UPDATE daily_touches SET reason=COALESCE((
            SELECT i.last_reason FROM daily_run_items i
            WHERE i.run_id=daily_touches.run_id AND i.user_id=daily_touches.user_id
              AND i.last_action=daily_touches.state AND i.last_reason<>''
              AND daily_touches.rowid=(SELECT MAX(t.rowid) FROM daily_touches t
                  WHERE t.run_id=i.run_id AND t.user_id=i.user_id)
            ),'') WHERE state IN ('sent','failed','uncertain')""")
    conn.commit()
    for suffix in ("-wal", "-shm"):
        auxiliary = Path(str(path) + suffix)
        if auxiliary.exists():
            os.chmod(auxiliary, 0o600)
    return conn


def _upgrade_observation_schema(conn: sqlite3.Connection) -> None:
    """One-time conversion of the first v2 prototype to append-only evidence."""
    columns = [row[1] for row in conn.execute("PRAGMA table_info(lead_observations)")]
    if "observation_key" in columns:
        if "manual_outbound_at_ms" not in columns:
            conn.execute("ALTER TABLE lead_observations ADD COLUMN manual_outbound_at_ms INTEGER")
        return
    rows = [dict(row) for row in conn.execute("SELECT * FROM lead_observations ORDER BY id")]
    conn.execute("DROP INDEX IF EXISTS idx_lead_obs_uid_time")
    conn.execute("ALTER TABLE lead_observations RENAME TO lead_observations_previous")
    conn.execute(
        """CREATE TABLE lead_observations (
           id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
           user_id TEXT NOT NULL, account_id TEXT NOT NULL,
           status TEXT NOT NULL CHECK(status IN ('present','absent','unknown')),
           label TEXT NOT NULL, source TEXT NOT NULL,
           attribution_state TEXT NOT NULL DEFAULT 'unknown',
           observed_at_ms INTEGER NOT NULL,evidence TEXT NOT NULL DEFAULT '',
           manual_outbound_at_ms INTEGER,
           observation_key TEXT NOT NULL UNIQUE)"""
    )
    for row in rows:
        key = digest(_json([row["run_id"], row["user_id"], row["account_id"],
                            row["observed_at_ms"], row["status"], row["label"],
                            row["source"], row.get("attribution_state", "unknown"), row["evidence"]]))
        conn.execute(
            """INSERT INTO lead_observations
               (id,run_id,user_id,account_id,status,label,source,attribution_state,observed_at_ms,evidence,manual_outbound_at_ms,observation_key)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["id"], row["run_id"], row["user_id"], row["account_id"],
             row["status"], row["label"], row["source"], row.get("attribution_state", "unknown"),
             row["observed_at_ms"], row["evidence"], row.get("manual_outbound_at_ms"), key),
        )
    conn.execute("DROP TABLE lead_observations_previous")
    conn.execute("CREATE INDEX idx_lead_obs_uid_time ON lead_observations(user_id,observed_at_ms DESC)")


def _managed_accounts(accounts: list[dict]) -> list[dict]:
    clean = []
    seen = set()
    for item in accounts:
        account_id = str(item.get("id", "")).strip()
        name = str(item.get("name", "")).strip()
        if account_id and name and account_id not in seen:
            clean.append({"id": account_id, "name": name})
            seen.add(account_id)
    return clean


def register_run(
    conn: sqlite3.Connection,
    scope_mode: str,
    account_ids: list[str],
    messages: dict[str, str],
    managed_accounts: list[dict],
    *,
    now_ms: int | None = None,
    require_fresh_import: bool = True,
    import_ids: list[str] | None = None,
    touch_policy: str = "uid_once",
) -> dict:
    now_ms = epoch_ms() if now_ms is None else now_ms
    accounts = _managed_accounts(managed_accounts)
    managed_ids = [item["id"] for item in accounts]
    if scope_mode not in ACCOUNT_MODES:
        raise ValueError("invalid_scope_mode")
    if touch_policy not in TOUCH_POLICIES:
        raise ValueError("invalid_touch_policy")
    requested = list(dict.fromkeys(str(x).strip() for x in account_ids if str(x).strip()))
    if scope_mode == "all":
        if requested:
            raise ValueError("all_scope_must_not_override_accounts")
        resolved = []  # Current UI search results define all-account scope.
    else:
        if not managed_ids:
            raise ValueError("managed_accounts_missing")
        if not requested:
            raise ValueError("account_scope_empty")
        if unknown := sorted(set(requested) - set(managed_ids)):
            raise ValueError("account_scope_unknown:" + ",".join(unknown))
        resolved = [account_id for account_id in managed_ids if account_id in requested]
    selected_imports = list(dict.fromkeys(import_ids or ([_latest_import_id(conn)] if _latest_import_id(conn) else [])))
    if require_fresh_import and not selected_imports:
        raise ValueError("user_list_import_required")
    if any(not conn.execute("SELECT 1 FROM historical_imports WHERE import_id=?", (value,)).fetchone()
           for value in selected_imports):
        raise ValueError("source_import_not_found")
    run_id = "day-" + uuid.uuid4().hex
    copies = {}
    versions = {}
    with conn:
        supplied = dict(messages)
        if supplied.get("ad") and supplied.get("ad") == supplied.get("natural"):
            supplied["unknown"] = supplied["ad"]
        for source in ("ad", "natural", "unknown"):
            raw = supplied.get(source, "")
            if not isinstance(raw, str):
                raise ValueError("message_must_be_text:" + source)
            copies[source] = raw
            if not canonical_copy(raw):
                continue
            exact_hash = digest(raw)
            normalized_hash = digest(canonical_copy(raw))
            version_id = source + "-" + exact_hash
            conn.execute(
                "INSERT OR IGNORE INTO message_versions VALUES (?,?,?,?,?,?)",
                (version_id, source, raw, exact_hash, normalized_hash, now_ms),
            )
            versions[source] = version_id
        conn.execute(
            "INSERT INTO daily_runs (run_id,started_at_ms,scope_mode,account_ids_json,messages_json,version_ids_json,source_import_id,source_import_ids_json,touch_policy) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, now_ms, scope_mode, _json(resolved), _json(copies), _json(versions), selected_imports[0] if selected_imports else "", _json(selected_imports), touch_policy),
        )
        _ensure_run_items(conn, _run(conn, run_id), now_ms)
        queue_count = conn.execute("SELECT COUNT(*) FROM daily_run_items WHERE run_id=?", (run_id,)).fetchone()[0]
    return {"ok": True, "run_id": run_id, "scope_mode": scope_mode, "account_ids": resolved,
            "version_ids": versions, "source_import_id": selected_imports[0] if selected_imports else "",
            "source_import_ids": selected_imports, "touch_policy": touch_policy, "run_state": "active",
            "initialized": True, "queue_count": queue_count}


def _run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM daily_runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError("run_not_found")
    return row


def _touch_policy(run) -> str:
    return run["touch_policy"] if "touch_policy" in run.keys() else "account_once"


def _run_event(conn, run_id: str, event: str, evidence: str, before: dict, after: dict, at_ms: int):
    conn.execute("INSERT INTO daily_run_events (run_id,event,recorded_at_ms,evidence,before_json,after_json) VALUES (?,?,?,?,?,?)",
                 (run_id, event, at_ms, evidence, _json(before), _json(after)))


def set_touch_policy(conn: sqlite3.Connection, run_id: str, policy: str, evidence: str,
                     *, now_ms: int | None = None) -> dict:
    now_ms = epoch_ms() if now_ms is None else now_ms
    if policy not in TOUCH_POLICIES:
        raise ValueError("invalid_touch_policy")
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError("evidence_required")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        run = _run(conn, run_id)
        if conn.execute("SELECT 1 FROM daily_touches WHERE run_id=? AND state IN ('reserved','clicked') LIMIT 1", (run_id,)).fetchone():
            raise ValueError("run_has_unfinished_touch")
        previous = _touch_policy(run)
        if previous != policy:
            conn.execute("UPDATE daily_runs SET touch_policy=? WHERE run_id=?", (policy, run_id))
            _run_event(conn, run_id, "touch_policy_changed", evidence.strip(),
                       {"touch_policy": previous}, {"touch_policy": policy}, now_ms)
    return {"ok": True, "run_id": run_id, "touch_policy": policy,
            "run_state": run["state"], "changed": previous != policy}


def _touch_counts(conn: sqlite3.Connection, run_id: str) -> dict:
    counts = {name: 0 for name in ("sent", "failed", "uncertain", "reserved", "clicked")}
    counts.update({row["state"]: row["n"] for row in conn.execute(
        "SELECT state,COUNT(*) n FROM daily_touches WHERE run_id=? GROUP BY state", (run_id,))})
    counts["sent_customers"] = conn.execute(
        "SELECT COUNT(DISTINCT user_id) FROM daily_touches WHERE run_id=? AND state='sent'", (run_id,)
    ).fetchone()[0]
    return counts


def session_status(conn: sqlite3.Connection, session_id: str | None = None, *,
                   run_id: str | None = None, now_ms: int | None = None) -> dict:
    """Read the durable baseline, never infer a user session from context segments."""
    now_ms = epoch_ms() if now_ms is None else now_ms
    if not _table_exists(conn, "daily_execution_sessions"):
        raise ValueError("session_schema_missing_run_init")
    if bool(session_id) == bool(run_id):
        raise ValueError("session_id_or_run_id_required")
    if run_id:
        row = conn.execute("""SELECT * FROM daily_execution_sessions WHERE run_id=?
                              ORDER BY ended_at_ms IS NULL DESC,started_at_ms DESC,rowid DESC LIMIT 1""", (run_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM daily_execution_sessions WHERE session_id=?", (session_id,)).fetchone()
    if row is None:
        raise ValueError("session_not_found")
    baseline = json.loads(row["baseline_json"])
    run_current = _touch_counts(conn, row["run_id"])
    end_counts = json.loads(row["end_counts_json"]) if row["end_counts_json"] else None
    current = end_counts if end_counts is not None else run_current
    elapsed = max(0, (row["ended_at_ms"] if row["ended_at_ms"] is not None else now_ms) - row["started_at_ms"])
    frozen_metrics = row["end_metrics_json"] if "end_metrics_json" in row.keys() else None
    baseline_ids_raw = row["baseline_sent_touch_ids_json"] if "baseline_sent_touch_ids_json" in row.keys() else None
    session_customers = None
    if baseline_ids_raw is not None:
        baseline_ids = set(_array(baseline_ids_raw))
        sent_since_baseline = [touch for touch in conn.execute(
            "SELECT touch_id,user_id FROM daily_touches WHERE run_id=? AND state='sent'", (row["run_id"],))
                              if touch["touch_id"] not in baseline_ids]
        session_customers = len({touch["user_id"] for touch in sent_since_baseline})
    metrics = json.loads(frozen_metrics) if frozen_metrics else {
        "sent_messages": current["sent"] - baseline["sent"],
        "sent_customers": session_customers if row["ended_at_ms"] is None else None,
        "new_cumulative_sent_customers": current["sent_customers"] - baseline["sent_customers"],
        "elapsed_wall_ms": elapsed,
        "messages_per_wall_hour": (current["sent"] - baseline["sent"]) * 3_600_000 / elapsed if elapsed else None,
        "customers_per_wall_hour": session_customers * 3_600_000 / elapsed if elapsed and session_customers is not None and row["ended_at_ms"] is None else None,
        "active_execution_ms": None,
        "customer_count_basis": "sent touch IDs absent from the durable session baseline" if baseline_ids_raw is not None else "legacy session has no touch-ID baseline; distinct touched customers cannot be reconstructed safely",
    }
    return {"ok": True, "session_id": row["session_id"], "run_id": row["run_id"],
            "state": "closed" if row["ended_at_ms"] is not None else "active",
            "started_at_ms": row["started_at_ms"], "ended_at_ms": row["ended_at_ms"],
            "baseline": baseline, "current": current, "end_counts": end_counts,
            "run_current": run_current,
            "delta": {key: current[key] - value for key, value in baseline.items()},
            "elapsed_wall_ms": elapsed, "session_metrics": metrics,
            "end_detail": row["end_detail"],
            "metric_note": "Deltas compare cumulative ledger snapshots; delta.sent_customers counts only customers newly added to the run. session_metrics.sent_customers counts all distinct customers sent during this session when a durable baseline exists. Wall time includes pauses; overlapping per-UID review waits are never summed as execution time."}


def begin_session(conn: sqlite3.Connection, run_id: str, session_id: str,
                  *, now_ms: int | None = None) -> dict:
    """Use the original user turn/request ID again after interruption or compaction."""
    now_ms = epoch_ms() if now_ms is None else now_ms
    if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 200:
        raise ValueError("invalid_session_id")
    run = _run(conn, run_id)
    _ensure_run_items(conn, run, now_ms)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT run_id FROM daily_execution_sessions WHERE session_id=?", (session_id,)).fetchone()
        if existing:
            if existing["run_id"] != run_id:
                raise ValueError("session_id_conflict")
            return session_status(conn, session_id, now_ms=now_ms)
        if conn.execute("SELECT 1 FROM daily_execution_sessions WHERE run_id=? AND ended_at_ms IS NULL", (run_id,)).fetchone():
            raise ValueError("active_session_exists")
        sent_ids = [row[0] for row in conn.execute("SELECT touch_id FROM daily_touches WHERE run_id=? AND state='sent'", (run_id,))]
        conn.execute("INSERT INTO daily_execution_sessions (session_id,run_id,started_at_ms,baseline_json,baseline_sent_touch_ids_json) VALUES (?,?,?,?,?)",
                     (session_id, run_id, now_ms, _json(_touch_counts(conn, run_id)), _json(sent_ids)))
    return session_status(conn, session_id, now_ms=now_ms)


def end_session(conn: sqlite3.Connection, session_id: str, detail: str = "",
                *, now_ms: int | None = None) -> dict:
    now_ms = epoch_ms() if now_ms is None else now_ms
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        current = session_status(conn, session_id, now_ms=now_ms)
        if conn.execute("SELECT 1 FROM daily_touches WHERE run_id=? AND state IN ('reserved','clicked') LIMIT 1", (current["run_id"],)).fetchone():
            raise ValueError("run_has_unfinished_touch")
        if now_ms < current["started_at_ms"]:
            raise ValueError("session_end_before_start")
        if current["state"] == "active":
            conn.execute("UPDATE daily_execution_sessions SET ended_at_ms=?,end_counts_json=?,end_metrics_json=?,end_detail=? WHERE session_id=?",
                         (now_ms, _json(current["current"]), _json(current["session_metrics"]), str(detail)[:1000], session_id))
    return session_status(conn, session_id, now_ms=now_ms)


def _latest_import_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT import_id FROM historical_imports ORDER BY cutoff_date DESC,imported_at_ms DESC,import_id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def _last_sender_account_id(conn: sqlite3.Connection, user_id: str, allowed_accounts: set[str]) -> str:
    """Use the latest confirmed send, without falling back from an out-of-scope account."""
    daily = conn.execute(
        """SELECT account_id,finished_at_ms FROM daily_touches
           WHERE user_id=? AND state='sent' AND finished_at_ms IS NOT NULL
           ORDER BY finished_at_ms DESC LIMIT 1""",
        (user_id,),
    ).fetchone()
    legacy = conn.execute(
        """SELECT account_id,attempted_at_ms FROM legacy_send_events
           WHERE user_id=? AND status='sent' AND attempted_at_ms>0
           ORDER BY attempted_at_ms DESC LIMIT 1""",
        (user_id,),
    ).fetchone()
    if daily and legacy:
        account_id = daily["account_id"] if daily["finished_at_ms"] >= legacy["attempted_at_ms"] else legacy["account_id"]
    elif daily:
        account_id = daily["account_id"]
    elif legacy:
        account_id = legacy["account_id"]
    else:
        account_id = ""
    return account_id if account_id in allowed_accounts else ""


def _ensure_run_items(conn: sqlite3.Connection, run: sqlite3.Row, now_ms: int) -> dict:
    if run["queue_policy_version"] >= 2:
        return {"built": False, "unmapped_accounts": []}
    scope = set(_array(run["account_ids_json"]))
    all_accounts = run["scope_mode"] == "all"
    imports = set(_array(run["source_import_ids_json"]) or [run["source_import_id"]])
    candidates = {}
    known = conn.execute("SELECT * FROM historical_user_accounts").fetchall()
    for row in known:
        if not imports.intersection(_array(row["source_import_ids_json"])):
            continue
        if not all_accounts and row["account_id"] not in scope:
            continue
        entry = candidates.setdefault(row["user_id"], {"accounts": {}, "fresh": True, "time": 0})
        entry["time"] = max(entry["time"], row["latest_in_at_ms"] or 0)
        if row["account_id"]:
            entry["accounts"][row["account_id"]] = row["latest_in_at_ms"] or 0
    for row in conn.execute(
        """SELECT user_id,account_id,MAX(finished_at_ms) AS at_ms FROM daily_touches
           WHERE state='sent' AND finished_at_ms>=? GROUP BY user_id,account_id""", (now_ms-OBSERVATION_TAIL_MS,)
    ):
        if not all_accounts and row["account_id"] not in scope:
            continue
        entry = candidates.setdefault(row["user_id"], {"accounts": {}, "fresh": False, "time": 0})
        entry["accounts"].setdefault(row["account_id"], 0)
    for row in conn.execute(
        """SELECT user_id,account_id FROM legacy_send_events WHERE status='sent' AND attempted_at_ms>=?""",
        (now_ms-OBSERVATION_TAIL_MS,)
    ):
        if all_accounts or row["account_id"] in scope:
            entry = candidates.setdefault(row["user_id"], {"accounts": {}, "fresh": False, "time": 0})
            entry["accounts"].setdefault(row["account_id"], 0)
    tagged = {row[0] for row in conn.execute("SELECT user_id FROM followup_tracking WHERE first_tag_observed_at_ms IS NOT NULL")}
    candidates = {uid: entry for uid, entry in candidates.items() if uid not in tagged or entry["fresh"]}
    protected = {row["user_id"] for row in known if row["lead_status_from_import"] == "has_lead"}
    # An enclosing registration transaction owns its commit and rollback.
    with (nullcontext() if conn.in_transaction else conn):
        for uid, entry in candidates.items():
            prior = _last_sender_account_id(conn, uid, set(entry["accounts"]))
            ordered = sorted(entry["accounts"], key=lambda acc: (-entry["accounts"][acc], acc != prior, acc))
            conn.execute(
                """INSERT OR IGNORE INTO daily_run_items
                   (run_id,user_id,account_ids_json,preferred_account_id,latest_in_at_ms,observation_only,legacy_lead_protection)
                   VALUES (?,?,?,?,?,?,?)""",
                (run["run_id"], uid, _json(ordered), ordered[0] if ordered else "", entry["time"],
                 int(not entry["fresh"]), int(uid in protected)),
            )
            if uid in protected:
                conn.execute("""UPDATE daily_run_items SET legacy_lead_protection=1,last_action='skip',
                             last_reason='legacy_lead_protection' WHERE run_id=? AND user_id=?""", (run["run_id"], uid))
        conn.execute("UPDATE daily_runs SET queue_policy_version=2 WHERE run_id=?", (run["run_id"],))
    return {"built": True, "unmapped_accounts": []}


def queue(conn: sqlite3.Connection, run_id: str, *, now_ms: int | None = None,
          read_only: bool = False, limit: int | None = None,
          exclude_user_ids: frozenset[str] = frozenset(),
          include_history: bool = True, include_sender: bool = True) -> dict:
    now_ms = epoch_ms() if now_ms is None else now_ms
    run = _run(conn, run_id)
    if read_only and run["queue_policy_version"] < 2:
        raise ValueError("queue_requires_initialization")
    build = {"unmapped_accounts": []} if read_only else _ensure_run_items(conn, run, now_ms)
    retired = {"within_24_hours", "same_message_previously_sent", "customer_inbound_unverified",
               "peer_or_seller", "party_unknown", "awaiting_human_reply", "reply_unknown",
               "outbound_copy_history_unknown", "negative_unknown", "negative_contact",
               "run_has_uncertain_send", "run_has_unfinished_send"}
    if run["scope_mode"] == "all":
        retired.update({"account_mapping_unknown", "legacy_account_conflict", "other_known_account_out_of_scope"})
    if not read_only:
        with conn:
            placeholders = ",".join("?" for _ in retired)
            conn.execute(f"""UPDATE daily_run_items SET last_action='pending',last_reason='',
                browser_defer_stage='',slow_retry_count=0 WHERE run_id=? AND last_reason IN ({placeholders})""",
                (run_id, *sorted(retired)))
    rows = conn.execute(
        """SELECT user_id,account_ids_json,preferred_account_id,latest_in_at_ms,
                  observation_only,legacy_lead_protection,last_action,last_reason,
                  browser_defer_stage,slow_retry_count
           FROM daily_run_items WHERE run_id=?
           ORDER BY observation_only DESC,latest_in_at_ms DESC,user_id""",
        (run_id,),
    ).fetchall()
    history_cache: dict[str, dict] = {}
    for cache in (conn.execute("SELECT user_id,account_id,boundary_message_id,outbound_text_hashes_json FROM conversation_history_cache WHERE business_reviewed=1") if include_history else ()):
        history_cache.setdefault(cache["user_id"], {})[cache["account_id"]] = {
            "boundary_message_id": cache["boundary_message_id"],
            "outbound_text_hashes": _array(cache["outbound_text_hashes_json"]),
        }
    items = []
    actionable_items = []
    compact_candidates = []
    blocked_by_unfinished_touch = bool(conn.execute(
        "SELECT 1 FROM daily_touches WHERE state IN ('reserved','clicked') LIMIT 1"
    ).fetchone())
    quarantined_uids = {row[0] for row in conn.execute(
        "SELECT user_id FROM daily_touches WHERE state='uncertain' UNION SELECT user_id FROM legacy_send_events WHERE status='uncertain'"
    )}
    failed_uids = {row[0] for row in conn.execute(
        "SELECT user_id FROM daily_touches WHERE run_id=? AND state='failed'", (run_id,)
    )}
    attempted_uids = {row[0] for row in conn.execute(
        "SELECT DISTINCT user_id FROM daily_touches WHERE run_id=?", (run_id,)
    )} if _touch_policy(run) == "uid_once" else set()
    run_date_expired = _run_date_expired(conn, run, now_ms)
    tagged_uids = {row[0] for row in conn.execute(
        "SELECT user_id FROM followup_tracking WHERE first_tag_observed_at_ms IS NOT NULL"
    )}
    excluded_uids = tagged_uids | {row[0] for row in conn.execute("SELECT user_id FROM contact_protections")} | {
        row[0] for row in conn.execute("SELECT user_id FROM no_intent_updates")} | {
        row[0] for row in conn.execute("SELECT user_id FROM historical_user_accounts WHERE lead_status_from_import='has_lead'")}
    excluded_uids.update(quarantined_uids | failed_uids | attempted_uids)
    account_results: dict[str, set[str]] = {}
    for result in conn.execute(
        "SELECT user_id,account_id,reason FROM daily_run_account_results WHERE run_id=?", (run_id,)
    ):
        if result["reason"] not in retired:
            account_results.setdefault(result["user_id"], set()).add(result["account_id"])
    def make_item(row, account_ids, remaining, retry_stage):
        return {
            "user_id": row["user_id"], "account_ids": account_ids,
            "remaining_account_ids": remaining,
            "preferred_account_id": row["preferred_account_id"],
            "last_sender_account_id": _last_sender_account_id(conn, row["user_id"], set(account_ids)) if include_sender else "",
            "latest_in_at_ms": row["latest_in_at_ms"],
            "observation_only": bool(row["observation_only"]),
            "legacy_lead_protection": bool(row["legacy_lead_protection"]),
            "last_action": row["last_action"], "last_reason": row["last_reason"],
            "retry_stage": retry_stage,
            "slow_retry_count": row["slow_retry_count"],
            "touch_policy": _touch_policy(run),
            "history_cache": {account_id: history_cache.get(row["user_id"], {}).get(account_id, {
                "boundary_message_id": "", "outbound_text_hashes": []
            }) for account_id in account_ids} if include_history else {},
        }

    for row in rows:
        if read_only and row["last_reason"] in retired:
            row = {**dict(row), "last_action": "pending", "last_reason": "",
                   "browser_defer_stage": "", "slow_retry_count": 0}
        account_ids = _array(row["account_ids_json"])
        remaining = [account_id for account_id in account_ids
                     if account_id not in account_results.get(row["user_id"], set())]
        retry_stage = None
        if row["last_action"] == "pending":
            retry_stage = "main"
        elif (row["last_action"] == "defer" and row["browser_defer_stage"] == "main"
              and row["slow_retry_count"] == 0):
            retry_stage = "slow"
        elif (remaining and account_results.get(row["user_id"])
              and (row["last_action"] == "sent" or
                   (row["last_reason"] in {"within_24_hours", "same_message_previously_sent",
                                            "source_unknown", "message_missing_for_source",
                                            "result_recheck_only"}
                    and row["last_action"] in {"skip", "defer", "observed_only"}))):
            retry_stage = "main"
        actionable = (retry_stage and not row["legacy_lead_protection"] and not blocked_by_unfinished_touch
                      and not run_date_expired and run["state"] == "active" and row["user_id"] not in excluded_uids)
        if limit is not None:
            if actionable:
                compact_candidates.append((row, account_ids, remaining, retry_stage))
        else:
            item = make_item(row, account_ids, remaining, retry_stage)
            items.append(item)
            if actionable:
                actionable_items.append(item)
    if limit is not None:
        compact_candidates.sort(key=lambda entry: entry[3] == "slow")
        # Local review/hold state is a selection filter, never a ledger decision.
        # Filter before limiting, without SQL placeholders or writes. Original
        # eligibility counts and queue ordering remain visible to the caller.
        visible_candidates = [entry for entry in compact_candidates
                              if entry[0]["user_id"] not in exclude_user_ids]
        return {"ok": True, "run_id": run_id,
                "touch_policy": _touch_policy(run), "run_state": run["state"],
                "items": [make_item(*entry) for entry in visible_candidates[:limit]],
                "total_items": len(rows), "actionable_count": len(compact_candidates),
                "retry_counts": {stage: sum(entry[3] == stage for entry in compact_candidates)
                                 for stage in ("main", "slow")},
                "blocked_by_unfinished_touch": blocked_by_unfinished_touch,
                "quarantined_count": len(quarantined_uids), "run_date_expired": run_date_expired}
    actionable_items.sort(key=lambda item: item["retry_stage"] == "slow")
    return {"ok": True, "run_id": run_id, "items": items,
            "touch_policy": _touch_policy(run), "run_state": run["state"],
            "actionable_items": actionable_items,
            "blocked_by_unfinished_touch": blocked_by_unfinished_touch,
            # Compatibility alias: finalized uncertain outcomes no longer stop
            # the queue; only an unfinished reservation/click blocks it.
            "blocked_by_uncertain": blocked_by_unfinished_touch,
            "quarantined_user_ids": sorted(quarantined_uids),
            "run_date_expired": run_date_expired,
            "unmapped_accounts": build["unmapped_accounts"]}


def next_items(conn: sqlite3.Connection, run_id: str, *, limit: int = 1,
               now_ms: int | None = None, exclude_user_ids: Any = _NEXT_EXCLUSIONS_UNSET) -> dict:
    """Return a small read-only queue view; no reservation or business decision."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid_next_limit")
    exclusions = [] if exclude_user_ids is _NEXT_EXCLUSIONS_UNSET else exclude_user_ids
    if not isinstance(exclusions, list) or any(not isinstance(uid, str) or not uid.strip() for uid in exclusions):
        raise ValueError("invalid_exclude_user_ids")
    return queue(conn, run_id, now_ms=now_ms, read_only=True, limit=limit,
                 exclude_user_ids=frozenset(exclusions))


def _exception_category(reason: str, action: str) -> str:
    if action in {"reserved", "clicked", "uncertain"}:
        return action
    if reason.startswith("search_"):
        return "search"
    if "identity" in reason or reason in {"panel_unknown", "panel_not_ready"}:
        return "identity"
    if reason.startswith("history_") or reason == "outbound_copy_history_unknown":
        return "history"
    if any(word in reason for word in ("image", "visual", "media")):
        return "media"
    return "pending" if action == "pending" else "other"


def _startup(conn: sqlite3.Connection, run, initialized: bool, counts: dict | None) -> dict:
    source_import_ids = _array(run["source_import_ids_json"]) or ([run["source_import_id"]] if run["source_import_id"] else [])
    imports = []
    for import_id in source_import_ids:
        row = conn.execute("SELECT import_id,source_file,unique_user_ids FROM historical_imports WHERE import_id=?", (import_id,)).fetchone()
        if row:
            imports.append(dict(row))
    return {"version": (SKILL_DIR / "VERSION").read_text(encoding="utf-8").strip(),
            "scope_mode": run["scope_mode"], "account_ids": _array(run["account_ids_json"]),
            "source_import_ids": source_import_ids, "imports": imports,
            "messages": json.loads(run["messages_json"]), "touch_policy": _touch_policy(run),
            "run_state": run["state"], "initialized": initialized, "counts": counts}


def run_status(conn: sqlite3.Connection, run_id: str, *, include_items: bool = False,
               now_ms: int | None = None) -> dict:
    """Read every uncompleted UID, including exhausted retries, without writes.

    Customer disposition counts are disjoint. Message outcomes and label work
    have their own units and must not be added to those customer counts.
    """
    now_ms = epoch_ms() if now_ms is None else now_ms
    run = _run(conn, run_id)
    if run["queue_policy_version"] < 2:
        # Registration supplies policy before a writable begin-session/queue
        # builds candidates. This read-only metadata path must not imply an
        # empty customer list, or prevent a new workflow from initializing.
        return {"ok": True, "run_id": run_id, "state": run["state"], "run_state": run["state"],
                "touch_policy": _touch_policy(run), "started_at_ms": run["started_at_ms"],
                "ended_at_ms": run["ended_at_ms"] if "ended_at_ms" in run.keys() else None,
                "initialized": False, "initialization_required": True,
                "startup": _startup(conn, run, False, None),
                "counts": None, "customer_dispositions": None, "uncompleted": None,
                "metric_note": "Candidate queue is not initialized; customer counts are unavailable until a writable begin-session or queue initializes it."}
    # Reports need dispositions, not cached message hashes or per-UID sender
    # lookups. Keep the normal small execution queue's richer payload intact.
    queue_view = queue(conn, run_id, now_ms=now_ms, read_only=True,
                       include_history=False, include_sender=False)
    touches = [dict(row) for row in conn.execute(
        "SELECT user_id,account_id,touch_id,state,reason,detail FROM daily_touches WHERE run_id=? ORDER BY prepared_at_ms,touch_id", (run_id,))]
    touches_by_uid: dict[str, list] = {}
    for touch in touches:
        touches_by_uid.setdefault(touch["user_id"], []).append(touch)
    protections = {row[0] for row in conn.execute("SELECT user_id FROM contact_protections UNION SELECT user_id FROM no_intent_updates UNION SELECT user_id FROM historical_user_accounts WHERE lead_status_from_import='has_lead' UNION SELECT user_id FROM followup_tracking WHERE first_tag_observed_at_ms IS NOT NULL")}
    # Unresolved attempts from a previous run still quarantine this UID.
    quarantined = set(queue_view["quarantined_user_ids"])
    pending_labels = [dict(row) for row in conn.execute(
        "SELECT user_id,account_id,state,evidence,detail FROM no_intent_updates WHERE run_id=? AND state IN ('pending','failed')", (run_id,))]
    counts = {"total_customers": len(queue_view["items"]), "sent_messages": 0,
              "sent_customers": 0, "failed_messages": 0, "failed_customers": 0,
              "open_touches": 0, "uncertain_touches": 0, "protected_customers": 0,
              "pending_labels": len(pending_labels), "uncompleted_customers": 0,
              "retry_exhausted_customers": 0, "actionable_customers": len(queue_view["actionable_items"]),
              "normal_pending_customers": 0, "completed_customers": 0}
    counts["local_no_intent_decisions"] = conn.execute("SELECT COUNT(*) FROM no_intent_updates WHERE run_id=? AND state='user_confirmed'", (run_id,)).fetchone()[0]
    counts["quarantined_customers"] = 0
    for outcome in ("sent", "failed"):
        matches = [touch for touch in touches if touch["state"] == outcome]
        counts[outcome + "_messages"] = len(matches)
        counts[outcome + "_customers"] = len({touch["user_id"] for touch in matches})
    counts["open_touches"] = sum(touch["state"] in {"reserved", "clicked"} for touch in touches)
    counts["uncertain_touches"] = sum(touch["state"] == "uncertain" for touch in touches)
    items = []
    quarantined_items = []
    reason_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    disposition = {name: 0 for name in ("uncompleted", "quarantined", "protected", "sent", "failed", "observed", "skipped")}
    retryable = 0
    for row in queue_view["items"]:
        uid = row["user_id"]
        uid_touches = touches_by_uid.get(uid, [])
        states = {touch["state"] for touch in uid_touches}
        unresolved = next((name for name in ("clicked", "reserved", "uncertain") if name in states), None)
        if unresolved is None and uid in quarantined:
            # Keep historical unresolved receipts visible without presenting
            # them as clicks performed in this run.
            disposition["quarantined"] += 1
            quarantined_items.append({"user_id": uid, "account_ids": row["account_ids"],
                                      "action": "quarantined", "reason": "historical_uncertain_requires_resolution"})
            continue
        protected = uid in protections or row["legacy_lead_protection"] or row["last_reason"] in {"no_intent", "lead_tag", "stop_contact", "protective_lead_signal", "legacy_lead_protection"}
        attempted_done = "failed" in states or (_touch_policy(run) == "uid_once" and "sent" in states)
        outstanding = bool(unresolved or (not protected and not attempted_done and
                           (row["last_action"] in {"pending", "defer"} or row["retry_stage"])))
        if outstanding:
            disposition["uncompleted"] += 1
            reason = unresolved or row["last_reason"] or row["last_action"]
            category = _exception_category(reason, unresolved or row["last_action"])
            exhausted = row["slow_retry_count"] > 0 and not unresolved
            counts["retry_exhausted_customers"] += int(exhausted)
            counts["normal_pending_customers"] += int(row["last_action"] == "pending" and not unresolved)
            retryable += int(bool(row["retry_stage"]) and not unresolved and not exhausted)
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            category_counts[category] = category_counts.get(category, 0) + 1
            items.append({"user_id": uid, "account_ids": row["account_ids"],
                          "preferred_account_id": row["preferred_account_id"],
                          "action": unresolved or row["last_action"], "reason": reason,
                          "category": category, "retry_stage": row["retry_stage"],
                          "retry_exhausted": exhausted, "slow_retry_count": row["slow_retry_count"]})
        elif protected:
            disposition["protected"] += 1
        elif "sent" in states:
            disposition["sent"] += 1
        elif "failed" in states:
            disposition["failed"] += 1
        elif row["last_action"] == "observed_only":
            disposition["observed"] += 1
        else:
            disposition["skipped"] += 1
    counts["uncompleted_customers"] = disposition["uncompleted"]
    counts["quarantined_customers"] = disposition["quarantined"]
    counts["unresolved_customers_total"] = disposition["uncompleted"] + disposition["quarantined"]
    counts["protected_customers"] = disposition["protected"]
    counts["completed_customers"] = counts["total_customers"] - counts["unresolved_customers_total"]
    out = {"ok": True, "run_id": run_id, "state": run["state"], "run_state": run["state"],
           "initialized": True, "initialization_required": False,
           "startup": _startup(conn, run, True, counts),
           "touch_policy": _touch_policy(run), "started_at_ms": run["started_at_ms"],
           "ended_at_ms": run["ended_at_ms"] if "ended_at_ms" in run.keys() else None,
           "counts": counts, "customer_dispositions": disposition,
           "uncompleted": {"reason_counts": reason_counts, "category_counts": category_counts,
                           "retryable_customers": retryable, "retry_exhausted_customers": counts["retry_exhausted_customers"]},
           "blocked_by_unfinished_touch": queue_view["blocked_by_unfinished_touch"],
           "run_date_expired": queue_view["run_date_expired"],
           "metric_note": "Customer dispositions are disjoint. Sent/failed outcome customers may overlap; label work counts account contexts. Empty actionable queue does not mean all customers were sent."}
    if include_items:
        out.update({"items": items, "pending_label_items": pending_labels,
                    "quarantined_items": quarantined_items,
                    "open_touch_items": [touch for touch in touches if touch["state"] in {"reserved", "clicked"}],
                    "uncertain_touch_items": [touch for touch in touches if touch["state"] == "uncertain"]})
    return out


def _transition_run(conn, run_id: str, event: str, detail: str, state: str, at_ms: int):
    previous = _run(conn, run_id)["state"]
    if previous != state:
        conn.execute("UPDATE daily_runs SET state=?,ended_at_ms=? WHERE run_id=?",
                     (state, at_ms if state.startswith("completed") else None, run_id))
        _run_event(conn, run_id, event, str(detail).strip()[:1000], {"state": previous}, {"state": state}, at_ms)


def pause_run(conn: sqlite3.Connection, run_id: str, detail: str, *,
              now_ms: int | None = None, state: str = "paused") -> dict:
    now_ms = epoch_ms() if now_ms is None else now_ms
    if state not in {"paused", "blocked"}:
        raise ValueError("invalid_pause_state")
    _ensure_run_items(conn, _run(conn, run_id), now_ms)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM daily_touches WHERE run_id=? AND state IN ('reserved','clicked') LIMIT 1", (run_id,)).fetchone():
            raise ValueError("run_has_unfinished_touch")
        if _run(conn, run_id)["state"].startswith("completed"):
            raise ValueError("completed_run_requires_resume")
        _transition_run(conn, run_id, "run_" + state, detail, state, now_ms)
    return run_status(conn, run_id, now_ms=now_ms)


def resume_run(conn: sqlite3.Connection, run_id: str, detail: str, *, now_ms: int | None = None) -> dict:
    now_ms = epoch_ms() if now_ms is None else now_ms
    _ensure_run_items(conn, _run(conn, run_id), now_ms)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _transition_run(conn, run_id, "run_resumed", detail, "active", now_ms)
    return run_status(conn, run_id, now_ms=now_ms)


def end_run(conn: sqlite3.Connection, run_id: str, detail: str, *, now_ms: int | None = None) -> dict:
    now_ms = epoch_ms() if now_ms is None else now_ms
    _ensure_run_items(conn, _run(conn, run_id), now_ms)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM daily_touches WHERE run_id=? AND state IN ('reserved','clicked') LIMIT 1", (run_id,)).fetchone():
            raise ValueError("run_has_unfinished_touch")
        before = run_status(conn, run_id, now_ms=now_ms)
        if before["run_state"] in {"paused", "blocked"}:
            raise ValueError("run_stopped_requires_resume")
        if before["counts"]["normal_pending_customers"]:
            raise ValueError("run_has_pending_items")
        exceptions = (before["counts"]["uncompleted_customers"] or before["counts"]["pending_labels"]
                      or before["counts"]["failed_messages"] or before["counts"]["quarantined_customers"])
        _transition_run(conn, run_id, "run_finished", detail,
                        "completed_with_exceptions" if exceptions else "completed", now_ms)
        active_session = conn.execute("SELECT session_id FROM daily_execution_sessions WHERE run_id=? AND ended_at_ms IS NULL", (run_id,)).fetchone()
        if active_session:
            current = session_status(conn, active_session["session_id"], now_ms=now_ms)
            conn.execute("UPDATE daily_execution_sessions SET ended_at_ms=?,end_counts_json=?,end_metrics_json=?,end_detail=? WHERE session_id=?",
                         (now_ms, _json(current["current"]), _json(current["session_metrics"]), str(detail)[:1000], active_session["session_id"]))
    return run_status(conn, run_id, now_ms=now_ms)


def add_run_account(conn: sqlite3.Connection, payload: dict, *, now_ms: int | None = None) -> dict:
    """Add a second account only after its UID and sender account were checked in the UI."""
    now_ms = epoch_ms() if now_ms is None else now_ms
    run_id = str(payload.get("run_id", ""))
    user_id = str(payload.get("user_id", "")).strip()
    account_id = str(payload.get("account_id", "")).strip()
    observed_at = payload.get("observed_at_ms")
    if not user_id or not account_id or payload.get("uid_verified") is not True or payload.get("account_verified") is not True:
        raise ValueError("ui_account_evidence_required")
    if not isinstance(observed_at, int) or observed_at <= 0 or observed_at > now_ms + 5_000 or now_ms - observed_at > OBSERVATION_FRESH_MS:
        raise ValueError("observed_account_stale")
    run = _run(conn, run_id)
    if run["scope_mode"] != "all" and account_id not in _array(run["account_ids_json"]):
        raise ValueError("account_out_of_run_scope")
    _ensure_run_items(conn, run, now_ms)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        item = conn.execute("SELECT account_ids_json FROM daily_run_items WHERE run_id=? AND user_id=?", (run_id, user_id)).fetchone()
        if item is None:
            raise ValueError("user_not_in_run_queue")
        accounts = _array(item["account_ids_json"])
        if account_id not in accounts:
            accounts.append(account_id)
            conn.execute("UPDATE daily_run_items SET account_ids_json=? WHERE run_id=? AND user_id=?",
                         (_json(accounts), run_id, user_id))
    return {"ok": True, "run_id": run_id, "user_id": user_id, "account_ids": accounts}


def record_browser_defer(conn: sqlite3.Connection, payload: dict, *, now_ms: int | None = None) -> dict:
    """Persist a browser-only failure without creating a send reservation."""
    now_ms = epoch_ms() if now_ms is None else now_ms
    run_id = str(payload.get("run_id", ""))
    user_id = str(payload.get("user_id", "")).strip()
    reason = str(payload.get("reason", "")).strip()[:200]
    stage = str(payload.get("retry_stage", ""))
    observed_at = payload.get("observed_at_ms", now_ms)
    if not user_id or not reason or stage not in {"main", "slow"}:
        raise ValueError("invalid_browser_defer")
    if not isinstance(observed_at, int) or observed_at <= 0 or observed_at > now_ms + 5_000:
        raise ValueError("invalid_observed_time")
    run = _run(conn, run_id)
    _ensure_run_items(conn, run, now_ms)
    reviews = payload.get("reviews", [])
    if not isinstance(reviews, list):
        raise ValueError("reviews_must_be_list")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        item = conn.execute(
            "SELECT * FROM daily_run_items WHERE run_id=? AND user_id=?", (run_id, user_id)
        ).fetchone()
        if item is None:
            raise ValueError("user_not_in_run_queue")
        if conn.execute(
            "SELECT 1 FROM daily_touches WHERE run_id=? AND user_id=? AND state IN ('reserved','clicked','uncertain') LIMIT 1",
            (run_id, user_id),
        ).fetchone():
            raise ValueError("touch_requires_reconciliation")
        if item["browser_defer_stage"] == stage and item["last_reason"] == reason:
            return {"ok": True, "run_id": run_id, "user_id": user_id,
                    "action": "defer", "reason": reason, "retry_stage": stage, "idempotent": True}
        if stage == "main" and item["last_action"] != "pending" and not (
            item["last_action"] in {"sent", "skip", "defer"}
            and conn.execute("SELECT 1 FROM daily_run_account_results WHERE run_id=? AND user_id=? LIMIT 1",
                             (run_id, user_id)).fetchone()
        ):
            raise ValueError("browser_main_retry_not_allowed")
        if stage == "slow" and (item["browser_defer_stage"] != "main" or item["slow_retry_count"] != 0):
            raise ValueError("browser_slow_retry_not_allowed")
        allowed_accounts = set(_array(item["account_ids_json"]))
        for review in reviews:
            if not isinstance(review, dict) or review.get("user_id") != user_id:
                raise ValueError("invalid_defer_review")
            if run["scope_mode"] != "all" and review.get("account_id") not in allowed_accounts:
                raise ValueError("defer_review_account_out_of_scope")
            if review.get("identity_verified") and review.get("panel_loaded") and review.get("label") in {"lead", "no_intent", "none"}:
                _save_observation(conn, run_id, user_id, review, now_ms)
        conn.execute(
            """INSERT INTO daily_browser_deferrals
               (run_id,user_id,reason,retry_stage,observed_at_ms,recorded_at_ms,evidence)
               VALUES (?,?,?,?,?,?,?)""",
            (run_id, user_id, reason, stage, observed_at, now_ms,
             str(payload.get("evidence", ""))),
        )
        conn.execute(
            """UPDATE daily_run_items SET last_action='defer',last_reason=?,
               browser_defer_stage=?,slow_retry_count=?,checked_at_ms=?
               WHERE run_id=? AND user_id=?""",
            (reason, stage, int(stage == "slow"), now_ms, run_id, user_id),
        )
    return {"ok": True, "run_id": run_id, "user_id": user_id,
            "action": "defer", "reason": reason, "retry_stage": stage,
            "slow_retry_done": stage == "slow"}


def reopen_browser_defer(conn: sqlite3.Connection, payload: dict, *, apply: bool = False,
                        now_ms: int | None = None) -> dict:
    """Preview by default; explicitly apply an audited correction for a read failure.

    This only puts a UID back through normal review. It cannot remove contact
    protection, override send outcomes, reserve a touch, or reuse stale UI proof.
    """
    now_ms = epoch_ms() if now_ms is None else now_ms
    request = {key: payload.get(key) for key in (
        "correction_id", "run_id", "user_id", "expected_reason", "expected_checked_at_ms", "evidence")}
    if (any(not isinstance(request[key], str) or not request[key].strip() for key in (
            "correction_id", "run_id", "user_id", "expected_reason", "evidence"))
            or type(request["expected_checked_at_ms"]) is not int
            or request["expected_checked_at_ms"] <= 0 or len(request["evidence"]) > 2000):
        raise ValueError("invalid_correction_request")
    run_id, user_id = request["run_id"], request["user_id"]

    def check_and_plan():
        _run(conn, run_id)
        if _table_exists(conn, "daily_item_corrections"):
            existing = conn.execute("SELECT * FROM daily_item_corrections WHERE correction_id=?", (request["correction_id"],)).fetchone()
            if existing:
                if json.loads(existing["request_json"]) != request:
                    raise ValueError("correction_id_conflict")
                return {"ok": True, "correction_id": request["correction_id"], "run_id": run_id,
                        "user_id": user_id, "action": "reopened_requires_review", "idempotent": True,
                        "before": json.loads(existing["before_json"]), "after": json.loads(existing["after_json"])}
        row = conn.execute("SELECT * FROM daily_run_items WHERE run_id=? AND user_id=?", (run_id, user_id)).fetchone()
        if (row is None or row["last_action"] != "defer" or row["browser_defer_stage"] not in {"main", "slow"}
                or row["last_reason"] != request["expected_reason"]
                or row["checked_at_ms"] != request["expected_checked_at_ms"]):
            raise ValueError("correction_state_changed")
        if not conn.execute("""SELECT 1 FROM daily_browser_deferrals WHERE run_id=? AND user_id=?
                              AND reason=? AND recorded_at_ms=?""",
                            (run_id, user_id, row["last_reason"], row["checked_at_ms"])).fetchone():
            raise ValueError("correction_not_browser_defer")
        if conn.execute("SELECT 1 FROM daily_touches WHERE run_id=? AND user_id=?", (run_id, user_id)).fetchone():
            raise ValueError("correction_uid_has_touch")
        if (conn.execute("SELECT 1 FROM daily_touches WHERE user_id=? AND state='uncertain'", (user_id,)).fetchone()
                or conn.execute("SELECT 1 FROM legacy_send_events WHERE user_id=? AND status='uncertain'", (user_id,)).fetchone()):
            raise ValueError("correction_uid_quarantined")
        if (row["legacy_lead_protection"]
                or conn.execute("SELECT 1 FROM contact_protections WHERE user_id=?", (user_id,)).fetchone()
                or conn.execute("SELECT 1 FROM no_intent_updates WHERE user_id=?", (user_id,)).fetchone()
                or conn.execute("SELECT 1 FROM followup_tracking WHERE user_id=? AND first_tag_observed_at_ms IS NOT NULL", (user_id,)).fetchone()
                or conn.execute("SELECT 1 FROM historical_user_accounts WHERE user_id=? AND lead_status_from_import='has_lead'", (user_id,)).fetchone()):
            raise ValueError("correction_uid_protected")
        before = dict(row)
        after = {**before, "last_action": "pending", "last_reason": "", "browser_defer_stage": "",
                 "slow_retry_count": 0, "checked_at_ms": now_ms}
        return {"ok": True, "correction_id": request["correction_id"], "run_id": run_id, "user_id": user_id,
                "action": "would_reopen", "idempotent": False, "before": before, "after": after,
                "requires_fresh_review": True, "evidence": request["evidence"]}

    if not apply:
        return check_and_plan()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        result = check_and_plan()
        if result["idempotent"]:
            return result
        conn.execute("""UPDATE daily_run_items SET last_action='pending',last_reason='',
                        browser_defer_stage='',slow_retry_count=0,checked_at_ms=? WHERE run_id=? AND user_id=?""",
                     (now_ms, run_id, user_id))
        conn.execute("""INSERT INTO daily_item_corrections
                        (correction_id,run_id,user_id,corrected_at_ms,evidence,before_json,after_json,request_json)
                        VALUES (?,?,?,?,?,?,?,?)""",
                     (request["correction_id"], run_id, user_id, now_ms, request["evidence"],
                      _json(result["before"]), _json(result["after"]), _json(request)))
        return {**result, "action": "reopened_requires_review"}


def _set_item_result(conn: sqlite3.Connection, run_id: str, user_id: str, action: str, reason: str, now_ms: int, source: str = "") -> dict:
    conn.execute(
        """UPDATE daily_run_items SET last_action=?,last_reason=?,
           last_source=CASE WHEN ?<>'' THEN ? ELSE last_source END,
           checked_at_ms=? WHERE run_id=? AND user_id=?""",
        (action, reason, source, source, now_ms, run_id, user_id),
    )
    return {"ok": True, "run_id": run_id, "user_id": user_id, "action": action, "reason": reason}


def _set_account_result(conn: sqlite3.Connection, run_id: str, user_id: str,
                        account_id: str, action: str, reason: str, now_ms: int) -> None:
    conn.execute(
        """INSERT INTO daily_run_account_results
           (run_id,user_id,account_id,action,reason,checked_at_ms)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(run_id,user_id,account_id) DO UPDATE SET
             action=excluded.action,reason=excluded.reason,checked_at_ms=excluded.checked_at_ms""",
        (run_id, user_id, account_id, action, reason, now_ms),
    )


def _observation_status(review: dict) -> str:
    if not review.get("identity_verified") or not review.get("panel_loaded"):
        return "unknown"
    if review.get("label") == "lead":
        return "present"
    if review.get("label") in {"none", "no_intent"}:
        return "absent"
    return "unknown"


def _save_observation(conn: sqlite3.Connection, run_id: str, user_id: str, review: dict, now_ms: int) -> None:
    status = _observation_status(review)
    account_id = review["account_id"]
    observed_at = int(review.get("observed_at_ms") or now_ms)
    source = review.get("source", "unknown")
    if source not in SOURCES:
        source = "unknown"
    attribution = review.get("attribution_note_state", "unknown")
    if attribution not in {"concrete", "empty"}:
        attribution = "unknown"
    evidence = str(review.get("source_evidence", ""))[:500]
    label = review.get("label", "unknown")
    manual_outbound = review.get("manual_outbound_at_ms")
    if not isinstance(manual_outbound, int) or manual_outbound <= 0:
        manual_outbound = None
    if manual_outbound is None:
        last_outbound = review.get("last_outbound_text_at_ms")
        prior = conn.execute(
            "SELECT last_sent_at_ms,first_tag_observed_at_ms FROM followup_tracking WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if (isinstance(last_outbound, int) and prior and prior["last_sent_at_ms"] is not None
                and last_outbound > prior["last_sent_at_ms"] + 10_000
                and (prior["first_tag_observed_at_ms"] is None or last_outbound <= prior["first_tag_observed_at_ms"])):
            manual_outbound = last_outbound
    key = digest(_json([run_id, user_id, account_id, observed_at, status, label,
                        source, attribution, evidence, manual_outbound]))
    conn.execute(
        """INSERT OR IGNORE INTO lead_observations
           (run_id,user_id,account_id,status,label,source,attribution_state,observed_at_ms,evidence,manual_outbound_at_ms,observation_key)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, user_id, account_id, status, label, source, attribution,
         observed_at, evidence, manual_outbound, key),
    )
    conn.execute("INSERT OR IGNORE INTO followup_tracking (user_id) VALUES (?)", (user_id,))
    conn.execute(
        """UPDATE followup_tracking SET
           last_observed_at_ms=CASE WHEN last_observed_at_ms IS NULL OR last_observed_at_ms<=?
                                   THEN ? ELSE last_observed_at_ms END,
           last_observation_status=CASE WHEN last_observed_at_ms IS NULL OR last_observed_at_ms<=?
                                        THEN ? ELSE last_observation_status END
           WHERE user_id=?""",
        (observed_at, observed_at, observed_at, status, user_id),
    )
    if status == "present":
        conn.execute(
            """UPDATE followup_tracking SET
               first_tag_observed_at_ms=COALESCE(first_tag_observed_at_ms,?),
               first_tag_run_id=COALESCE(first_tag_run_id,?),
               first_tag_account_id=COALESCE(first_tag_account_id,?)
               WHERE user_id=?""",
            (observed_at, run_id, account_id, user_id),
        )
    if manual_outbound is not None:
        conn.execute(
            """UPDATE followup_tracking SET manual_intervention=1
               WHERE user_id=? AND first_sent_at_ms IS NOT NULL
                 AND first_sent_at_ms<?
                 AND (first_tag_observed_at_ms IS NULL OR ?<=first_tag_observed_at_ms)""",
            (user_id, manual_outbound, manual_outbound),
        )


def _block(action: str, reason: str) -> tuple[str, str]:
    return action, reason


def _known_other_account_out_of_scope(conn: sqlite3.Connection, user_id: str, scope: set[str]) -> bool:
    """An unreviewed account context for this UID must not be bypassed."""
    known = set()
    for table in ("historical_user_accounts", "legacy_account_snapshots", "legacy_send_events"):
        known.update(
            row[0] for row in conn.execute(
                f"SELECT DISTINCT account_id FROM {table} WHERE user_id=? AND account_id<>''",
                (user_id,),
            )
        )
    return bool(known - scope)


def _history_cache_proven(conn: sqlite3.Connection, user_id: str, account_id: str, review: dict) -> bool:
    if not review.get("history_complete"):
        return False
    cache = conn.execute(
        "SELECT boundary_message_id,outbound_text_hashes_json,business_reviewed FROM conversation_history_cache WHERE user_id=? AND account_id=?",
        (user_id, account_id),
    ).fetchone()
    if review.get("history_from_start_complete"):
        return True
    if cache is None or not cache["business_reviewed"] or not cache["boundary_message_id"] or not review.get("history_reached_prior_boundary"):
        return False
    hashes = review.get("outbound_text_hashes")
    return isinstance(hashes, list) and set(_array(cache["outbound_text_hashes_json"])).issubset(set(hashes))


def _save_history_cache(conn: sqlite3.Connection, user_id: str, account_id: str, review: dict, now_ms: int) -> None:
    current = conn.execute(
        "SELECT boundary_message_id FROM conversation_history_cache WHERE user_id=? AND account_id=?",
        (user_id, account_id),
    ).fetchone()
    boundary = str(review.get("history_boundary_message_id") or (current[0] if current else ""))
    conn.execute(
        """INSERT INTO conversation_history_cache
           (user_id,account_id,boundary_message_id,outbound_text_hashes_json,verified_at_ms,business_reviewed)
           VALUES (?,?,?,?,?,1)
           ON CONFLICT(user_id,account_id) DO UPDATE SET
           boundary_message_id=excluded.boundary_message_id,
           outbound_text_hashes_json=excluded.outbound_text_hashes_json,
           verified_at_ms=excluded.verified_at_ms,business_reviewed=1""",
        (user_id, account_id, boundary, _json(review["outbound_text_hashes"]), now_ms),
    )


def record_reviewed_history(conn: sqlite3.Connection, payload: dict, *, now_ms: int | None = None) -> dict:
    """Persist reviewed historical evidence without reserving or sending anything.

    Historical meaning does not expire while a batch waits for review. Current
    identity, labels, scope and draft must still be re-read by prepare_touch.
    """
    now_ms = epoch_ms() if now_ms is None else now_ms
    run_id, uid = str(payload.get("run_id", "")), str(payload.get("user_id", ""))
    run = _run(conn, run_id)
    _ensure_run_items(conn, run, now_ms)
    if not conn.execute("SELECT 1 FROM daily_run_items WHERE run_id=? AND user_id=?", (run_id, uid)).fetchone():
        raise ValueError("user_not_in_run_queue")
    reviews = payload.get("reviews")
    if not isinstance(reviews, list) or not reviews:
        raise ValueError("reviews_required")
    seen = set()
    label_updates = []
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        for r in reviews:
            account = r.get("account_id")
            if r.get("user_id") != uid or not account or account in seen:
                raise ValueError("review_uid_or_account_mismatch")
            seen.add(account)
            if run["scope_mode"] != "all" and account not in _array(run["account_ids_json"]):
                raise ValueError("review_account_out_of_scope")
            observed = r.get("observed_at_ms")
            messages = r.get("messages")
            if (r.get("identity_verified") is not True or r.get("panel_loaded") is not True
                    or r.get("business_reviewed") is not True or r.get("context_complete") is not True
                    or r.get("negative_status") not in {"safe", "blocked"}
                    or not isinstance(observed, int) or not 0 < observed <= now_ms
                    or not isinstance(messages, list) or not messages
                    or not r.get("history_boundary_message_id")
                    or not any(m.get("id") == r["history_boundary_message_id"] for m in messages)
                    or not isinstance(r.get("outbound_text_hashes"), list)
                    or not _history_cache_proven(conn, uid, account, r)):
                raise ValueError("review_history_unproven")
            if r.get("protective_lead_signal"):
                evidence = str(r.get("contact_evidence", "")).strip()
                if not evidence:
                    raise ValueError("contact_evidence_required")
                conn.execute("INSERT OR IGNORE INTO contact_protections VALUES (?,?,?,?)", (uid, account, observed, evidence))
            if r["negative_status"] == "blocked":
                evidence = str(r.get("stop_contact_evidence", "")).strip()
                if not evidence or not any(m.get("side") == "left" and evidence in str(m.get("text", "")) for m in messages):
                    raise ValueError("stop_contact_customer_quote_required")
                tagged = r.get("label") == "no_intent" or r.get("no_intent_label") is True
                conn.execute("""INSERT OR IGNORE INTO no_intent_updates
                    (run_id,user_id,account_id,evidence,state,observed_at_ms) VALUES (?,?,?,?,?,?)""",
                    (run_id, uid, account, evidence, "verified" if tagged else "pending", observed))
                if not tagged:
                    label_updates.append({"user_id": uid, "account_id": account, "label": "无意向", "evidence": evidence})
            _save_history_cache(conn, uid, account, r, now_ms)
    return {"ok": True, "user_id": uid, "reviewed_accounts": sorted(seen), "sends": 0, "label_updates": label_updates}


def prepare_touch(conn: sqlite3.Connection, payload: dict, *, now_ms: int | None = None) -> dict:
    """Record current UI evidence and reserve one send, all in one transaction.

    The caller must inspect every known account context and provide fresh evidence.
    A reservation survives a crash and never expires automatically.
    """
    now_ms = epoch_ms() if now_ms is None else now_ms
    run_id = str(payload.get("run_id", ""))
    user_id = str(payload.get("user_id", "")).strip()
    selected = str(payload.get("selected_account_id", "")).strip()
    if not user_id or not selected:
        raise ValueError("identity_missing")
    run = _run(conn, run_id)
    _ensure_run_items(conn, run, now_ms)
    item = conn.execute("SELECT * FROM daily_run_items WHERE run_id=? AND user_id=?", (run_id, user_id)).fetchone()
    if item is None:
        raise ValueError("user_not_in_run_queue")
    reviews = payload.get("reviews")
    if not isinstance(reviews, list) or not reviews:
        raise ValueError("reviews_required")
    by_account = {}
    for review in reviews:
        if not isinstance(review, dict) or review.get("user_id") != user_id:
            raise ValueError("review_uid_mismatch")
        account_id = str(review.get("account_id", ""))
        if not account_id or account_id in by_account:
            raise ValueError("duplicate_or_missing_account_review")
        by_account[account_id] = review
    discovered = payload.get("discovered_account_ids", [])
    expected = list(dict.fromkeys(discovered))
    if selected not in by_account or (run["scope_mode"] != "all" and selected not in _array(run["account_ids_json"])):
        raise ValueError("selected_account_out_of_scope")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        run = _run(conn, run_id)
        if run["state"] != "active":
            return {"ok": True, "run_id": run_id, "user_id": user_id,
                    "action": "defer", "reason": "run_not_active", "label_updates": [],
                    "touch_policy": _touch_policy(run), "run_state": run["state"]}
        if item["browser_defer_stage"] == "main" and item["slow_retry_count"] == 0:
            # This prepare is the one permitted slow retry. A failed state
            # check must not silently put the UID back into the slow queue.
            conn.execute(
                """UPDATE daily_run_items SET browser_defer_stage='slow',slow_retry_count=1
                   WHERE run_id=? AND user_id=?""",
                (run_id, user_id),
            )
        if payload.get("search_complete") is True and set(expected) == set(by_account):
            conn.execute("UPDATE daily_run_items SET account_ids_json=? WHERE run_id=? AND user_id=?",
                         (_json(expected), run_id, user_id))
        for review in by_account.values():
            _save_observation(conn, run_id, user_id, review, now_ms)
            if (review.get("identity_verified") and review.get("panel_loaded")
                    and isinstance(review.get("observed_at_ms"), int)
                    and 0 <= now_ms-review["observed_at_ms"] <= OBSERVATION_FRESH_MS
                    and (review.get("platform_lead_signal") or
                         (review.get("business_reviewed") is True and review.get("protective_lead_signal")))):
                conn.execute("INSERT OR IGNORE INTO contact_protections VALUES (?,?,?,?)",
                    (user_id, review["account_id"], review["observed_at_ms"],
                     str(review.get("contact_evidence") or "verified contact in conversation/panel")))
        action = "send"
        reason = "eligible"
        if run["state"] != "active":
            action, reason = _block("defer", "run_not_active")
        elif _run_date_expired(conn, run, now_ms):
            action, reason = _block("defer", "run_date_expired")
        elif conn.execute(
            "SELECT 1 FROM daily_touches WHERE run_id=? AND user_id=? AND (?='uid_once' OR account_id=?) AND state='sent' LIMIT 1",
            (run_id, user_id, _touch_policy(run), selected),
        ).fetchone():
            action, reason = _block("skip", "already_sent_this_run")
        elif conn.execute(
            "SELECT 1 FROM daily_touches WHERE user_id=? AND state IN ('reserved','clicked') LIMIT 1",
            (user_id,),
        ).fetchone():
            action, reason = _block("defer", "pending_touch_requires_resolution")
        elif conn.execute(
            "SELECT 1 FROM daily_touches WHERE user_id=? AND state='uncertain' LIMIT 1", (user_id,),
        ).fetchone() or conn.execute(
            "SELECT 1 FROM legacy_send_events WHERE user_id=? AND status='uncertain' LIMIT 1",
            (user_id,),
        ).fetchone():
            action, reason = _block("skip", "uncertain_touch_requires_resolution")
        elif conn.execute(
            "SELECT 1 FROM daily_touches WHERE run_id=? AND user_id=? AND state='failed' LIMIT 1", (run_id, user_id),
        ).fetchone():
            action, reason = _block("skip", "failed_this_run")
        elif conn.execute(
            "SELECT 1 FROM daily_touches WHERE state IN ('reserved','clicked') LIMIT 1",
        ).fetchone():
            action, reason = _block("defer", "run_has_unfinished_send")
        elif conn.execute(
            "SELECT first_tag_observed_at_ms FROM followup_tracking WHERE user_id=?",
            (user_id,),
        ).fetchone()[0] is not None:
            action, reason = _block("skip", "lead_tag")
        elif run["scope_mode"] != "all" and _known_other_account_out_of_scope(conn, user_id, set(_array(run["account_ids_json"]))):
            action, reason = _block("defer", "other_known_account_out_of_scope")
        elif any(r.get("label") == "no_intent" and _observation_status(r) == "absent" for r in by_account.values()):
            action, reason = _block("skip", "no_intent")
        elif any(r.get("business_reviewed") is True and r.get("negative_status") == "blocked" for r in by_account.values()):
            action, reason = _block("skip", "stop_contact")
        elif conn.execute("SELECT 1 FROM no_intent_updates WHERE user_id=? LIMIT 1", (user_id,)).fetchone():
            action, reason = _block("skip", "stop_contact")
        elif conn.execute("SELECT 1 FROM contact_protections WHERE user_id=?", (user_id,)).fetchone():
            action, reason = _block("skip", "protective_lead_signal")
        elif any(r.get("protective_lead_signal") or r.get("platform_lead_signal") for r in by_account.values()):
            action, reason = _block("skip", "protective_lead_signal")
        elif item["legacy_lead_protection"] or conn.execute(
            "SELECT 1 FROM historical_user_accounts WHERE user_id=? AND lead_status_from_import='has_lead' LIMIT 1", (user_id,)
        ).fetchone():
            action, reason = _block("skip", "legacy_lead_protection")
        elif payload.get("search_complete") is not True or not expected or set(by_account) != set(expected):
            action, reason = _block("defer", "account_review_incomplete")
        elif any(not r.get("identity_verified") or not r.get("panel_loaded") or r.get("label") not in LABELS or r.get("label") == "unknown" for r in by_account.values()):
            action, reason = _block("defer", "panel_unknown")
        elif any(not r.get("history_complete") for r in by_account.values()):
            action, reason = _block("defer", "history_incomplete")
        elif any(not _history_cache_proven(conn, user_id, acc, r) for acc, r in by_account.items()):
            action, reason = _block("defer", "history_boundary_unverified")
        elif any(r.get("business_reviewed") is not True or r.get("negative_status") not in {"safe", "blocked"} for r in by_account.values()):
            action, reason = _block("defer", "contact_review_required")
        elif any(r.get("draft_present") for r in by_account.values()):
            action, reason = _block("defer", "draft_present")
        elif any(
            not isinstance(r.get("observed_at_ms"), int)
            or now_ms-r["observed_at_ms"] > OBSERVATION_FRESH_MS
            or r["observed_at_ms"] > now_ms + 5_000
            for r in by_account.values()
        ):
            action, reason = _block("defer", "observation_stale")
        elif item["observation_only"]:
            action, reason = _block("observed_only", "result_recheck_only")
        source = by_account.get(selected, {}).get("source", "unknown")
        attribution = by_account.get(selected, {}).get("attribution_note_state", "unknown")
        if (source, attribution) not in {("ad", "concrete"), ("natural", "empty")}:
            source = "unknown"
        messages = json.loads(run["messages_json"])
        versions = json.loads(run["version_ids_json"])
        message = messages.get(source, "")
        version_id = versions.get(source, "")
        # Older runs may predate the unknown-source shared-copy version.
        if source == "unknown" and messages.get("ad") and messages.get("ad") == messages.get("natural"):
            message = messages["ad"]
            version_id = "unknown-" + digest(message)
            conn.execute("INSERT OR IGNORE INTO message_versions VALUES (?,?,?,?,?,?)",
                         (version_id, "unknown", message, digest(message), digest(canonical_copy(message)), now_ms))
        if action == "send" and (not version_id or not canonical_copy(message)):
            action, reason = _block("defer", "source_unknown" if source == "unknown" else "message_missing_for_source")
        normalized_hash = digest(canonical_copy(message)) if message else ""
        label_updates = []
        for acc, review in by_account.items():
            if (review.get("identity_verified") and review.get("business_reviewed") is True
                    and review.get("negative_status") == "blocked"
                    and isinstance(review.get("observed_at_ms"), int)
                    and 0 <= now_ms-review["observed_at_ms"] <= OBSERVATION_FRESH_MS):
                evidence = str(review.get("stop_contact_evidence", "")).strip()
                if not evidence:
                    raise ValueError("stop_contact_evidence_required")
                already_tagged = review.get("label") == "no_intent" or review.get("no_intent_label") is True
                conn.execute("""INSERT OR IGNORE INTO no_intent_updates
                    (run_id,user_id,account_id,evidence,state,observed_at_ms) VALUES (?,?,?,?,?,?)""",
                    (run_id, user_id, acc, evidence, "verified" if already_tagged else "pending", now_ms))
                if not already_tagged:
                    label_updates.append({"user_id": user_id, "account_id": acc, "label": "无意向", "evidence": evidence})
        if action in {"send", "skip", "observed_only"}:
            for acc, review in by_account.items():
                if review.get("business_reviewed") is True and _history_cache_proven(conn, user_id, acc, review) and isinstance(review.get("outbound_text_hashes"), list):
                    _save_history_cache(conn, user_id, acc, review, now_ms)
        result = _set_item_result(conn, run_id, user_id, action, reason, now_ms,
                                  source if source in SOURCES else "")
        if reason in {"within_24_hours", "same_message_previously_sent", "source_unknown",
                      "message_missing_for_source", "result_recheck_only"}:
            _set_account_result(conn, run_id, user_id, selected, action, reason, now_ms)
        result["label_updates"] = label_updates
        result["touch_policy"] = _touch_policy(run)
        if action == "send":
            touch_id = "touch-" + uuid.uuid4().hex
            manual_intervention = int(any(r.get("manual_outbound_at_ms") for r in by_account.values()))
            conn.execute(
                """INSERT INTO daily_touches
                   (touch_id,run_id,user_id,account_id,source,version_id,message_snapshot,
                    normalized_hash,state,prepared_at_ms,pre_send_absent_at_ms,manual_intervention)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (touch_id, run_id, user_id, selected, source, version_id, message,
                 normalized_hash, "reserved", now_ms, now_ms, manual_intervention),
            )
            result.update({"source_sensitive": not (messages.get("ad") and messages.get("ad") == messages.get("natural")), "touch_id": touch_id, "message": message, "source": source, "version_id": version_id, "account_id": selected})
        return result


def record_no_intent(conn: sqlite3.Connection, payload: dict, *, now_ms: int | None = None) -> dict:
    now_ms = epoch_ms() if now_ms is None else now_ms
    key = (str(payload.get("run_id", "")), str(payload.get("user_id", "")), str(payload.get("account_id", "")))
    row = conn.execute("SELECT * FROM no_intent_updates WHERE run_id=? AND user_id=? AND account_id=?", key).fetchone()
    if row is None:
        raise ValueError("stop_contact_record_required")
    verified = (payload.get("uid_verified") is True and payload.get("account_verified") is True
                and payload.get("label") == "no_intent")
    observed = payload.get("observed_at_ms")
    if verified and (not isinstance(observed, int) or not 0 <= now_ms-observed <= OBSERVATION_FRESH_MS):
        raise ValueError("label_observation_stale")
    status = "verified" if verified else "failed"
    with conn:
        conn.execute("UPDATE no_intent_updates SET state=?,detail=? WHERE run_id=? AND user_id=? AND account_id=?",
                     (status, str(payload.get("detail", ""))[:500], *key))
    return {"ok": True, "user_id": key[1], "account_id": key[2], "label_state": status, "uid_suppressed": True}


def authorize_remaining(conn: sqlite3.Connection, run_id: str, evidence: str, *, now_ms: int | None = None) -> dict:
    run = _run(conn, run_id)
    if run['state'] != 'active' or not evidence.strip():
        raise ValueError('active_run_and_human_evidence_required')
    now_ms = epoch_ms() if now_ms is None else now_ms
    with conn:
        conn.execute('INSERT OR REPLACE INTO remaining_run_authorizations VALUES (?,?,?)',
                     (run_id, now_ms, evidence.strip()))
    return {'ok': True, 'run_id': run_id, 'authorized_at_ms': now_ms}


def _run_date_expired(conn: sqlite3.Connection, run, now_ms: int) -> bool:
    if _shanghai_day(run['started_at_ms']) == _shanghai_day(now_ms):
        return False
    table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='remaining_run_authorizations'").fetchone()
    return not (table and conn.execute('SELECT 1 FROM remaining_run_authorizations WHERE run_id=? AND authorized_at_ms<=?',
                                      (run['run_id'], now_ms)).fetchone())


def mark_clicked(conn: sqlite3.Connection, touch_id: str, *, now_ms: int | None = None) -> dict:
    now_ms = epoch_ms() if now_ms is None else now_ms
    with conn:
        row = conn.execute(
            """SELECT t.state,r.started_at_ms,r.run_id FROM daily_touches t
               JOIN daily_runs r ON r.run_id=t.run_id WHERE t.touch_id=?""",
            (touch_id,),
        ).fetchone()
        if row is None:
            raise ValueError("touch_not_found")
        if row["state"] != "reserved":
            raise ValueError("touch_not_reserved")
        if _run_date_expired(conn, row, now_ms):
            raise ValueError("run_date_expired")
        conn.execute("UPDATE daily_touches SET state='clicked',clicked_at_ms=? WHERE touch_id=?", (now_ms, touch_id))
    return {"ok": True, "touch_id": touch_id, "state": "clicked"}


def pending_touches(conn: sqlite3.Connection, run_id: str | None = None) -> dict:
    """Separate unfinished clicks from finalized but unverified UID outcomes."""
    where = "WHERE state IN ('reserved','clicked','uncertain')"
    params: tuple = ()
    if run_id is not None:
        _run(conn, run_id)
        where += " AND run_id=?"
        params = (run_id,)
    rows = conn.execute(
        f"""SELECT touch_id,run_id,user_id,account_id,state,prepared_at_ms,clicked_at_ms,finished_at_ms,
                   COALESCE(NULLIF(reason,''),state) AS reason,detail
            FROM daily_touches {where} ORDER BY prepared_at_ms,touch_id""",
        params,
    ).fetchall()
    return {"ok": True,
            "items": [dict(row) for row in rows if row["state"] in {"reserved", "clicked"}],
            "uncertain_items": [dict(row) for row in rows if row["state"] == "uncertain"],
            "legacy_uncertain_items": [dict(row) for row in conn.execute(
                """SELECT fingerprint,user_id,account_id,status,attempted_at_ms,detail
                   FROM legacy_send_events WHERE status='uncertain'
                   ORDER BY attempted_at_ms,fingerprint""")],
            "blocked_by_unfinished_touch": bool(conn.execute(
                "SELECT 1 FROM daily_touches WHERE state IN ('reserved','clicked') LIMIT 1"
            ).fetchone())}


def _credit_sent(conn: sqlite3.Connection, touch: sqlite3.Row, at_ms: int) -> None:
    conn.execute("INSERT OR IGNORE INTO followup_tracking (user_id) VALUES (?)", (touch["user_id"],))
    conn.execute(
        """UPDATE followup_tracking SET
           first_pre_send_absent_at_ms=COALESCE(first_pre_send_absent_at_ms,?),
           first_source=COALESCE(first_source,?),
           first_sent_at_ms=COALESCE(first_sent_at_ms,?),
           last_sent_at_ms=CASE WHEN last_sent_at_ms IS NULL OR last_sent_at_ms<?
                                THEN ? ELSE last_sent_at_ms END,
           manual_intervention=MAX(manual_intervention,?)
           WHERE user_id=?""",
        (touch["pre_send_absent_at_ms"], touch["source"], at_ms, at_ms, at_ms,
         touch["manual_intervention"], touch["user_id"]),
    )


def finish_touch(conn: sqlite3.Connection, payload: dict, *, now_ms: int | None = None) -> dict:
    now_ms = epoch_ms() if now_ms is None else now_ms
    touch_id = str(payload.get("touch_id", ""))
    status = str(payload.get("status", ""))
    if status not in {"sent", "failed", "uncertain"}:
        raise ValueError("invalid_finish_status")
    # The workflow bounds individual fields before serialization. Slicing here
    # could corrupt a JSON receipt and discard the platform's rejection text.
    detail = str(payload.get("detail", ""))
    reason = str(payload.get("reason") or "").strip()[:200] or status
    verified_at_ms = payload.get("verified_at_ms", now_ms)
    if not isinstance(verified_at_ms, int):
        raise ValueError("invalid_verified_time")
    with conn:
        row = conn.execute("SELECT * FROM daily_touches WHERE touch_id=?", (touch_id,)).fetchone()
        if row is None:
            raise ValueError("touch_not_found")
        if row["state"] in {"sent", "failed", "uncertain"}:
            if row["state"] == status:
                return {"ok": True, "touch_id": touch_id, "state": status, "idempotent": True}
            raise ValueError("touch_already_finished")
        if row["state"] != "clicked":
            raise ValueError("touch_not_marked_clicked")
        conn.execute(
            "UPDATE daily_touches SET state=?,finished_at_ms=?,detail=?,reason=? WHERE touch_id=?",
            (status, verified_at_ms, detail, reason, touch_id),
        )
        if status == "sent":
            _credit_sent(conn, row, verified_at_ms)
        _set_item_result(conn, row["run_id"], row["user_id"], status, reason, verified_at_ms)
        if status == "sent":
            _set_account_result(conn, row["run_id"], row["user_id"], row["account_id"],
                                "sent", "sent", verified_at_ms)
    return {"ok": True, "touch_id": touch_id, "state": status, "reason": reason}


def cancel_unclicked_touch(conn: sqlite3.Connection, payload: dict, *, now_ms: int | None = None) -> dict:
    """Only a stage that definitively did not click may cancel a reservation."""
    now_ms = epoch_ms() if now_ms is None else now_ms
    touch_id, reason = str(payload.get("touch_id", "")), str(payload.get("reason", ""))
    safe_reasons = {"page_or_customer_changed", "user_draft_present", "reply_input_not_unique",
                    "account_reviews_stale_reobserve_required", "attribution_changed_for_distinct_copies",
                    "message_exceeds_platform_limit", "preflight_changed_auto_draft_cleared"}
    if payload.get("stage_status") != "not_staged" or reason not in safe_reasons:
        raise ValueError("unclicked_stage_proof_required")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM daily_touches WHERE touch_id=?", (touch_id,)).fetchone()
        if row is None or row["state"] != "reserved" or row["clicked_at_ms"] is not None:
            raise ValueError("touch_not_unclicked_reservation")
        detail = "发送前检查返回 not_staged，尚未标记或点击发送；" + reason
        conn.execute("UPDATE daily_touches SET state='failed',finished_at_ms=?,reason='cancelled_before_click',detail=? WHERE touch_id=?",
                     (now_ms, detail, touch_id))
        _set_item_result(conn, row["run_id"], row["user_id"], "failed", "cancelled_before_click", now_ms)
    return {"ok": True, "touch_id": touch_id, "state": "failed", "reason": "cancelled_before_click"}


def resolve_touch(conn: sqlite3.Connection, touch_id: str, resolution: str, detail: str, *, now_ms: int | None = None) -> dict:
    """Manual reconciliation only, after the actual chat has been checked."""
    now_ms = epoch_ms() if now_ms is None else now_ms
    if resolution not in {"confirmed_sent", "confirmed_not_sent"}:
        raise ValueError("invalid_resolution")
    if not detail.strip():
        raise ValueError("resolution_evidence_required")
    target = "sent" if resolution == "confirmed_sent" else "failed"
    with conn:
        row = conn.execute("SELECT * FROM daily_touches WHERE touch_id=?", (touch_id,)).fetchone()
        if row is None:
            raise ValueError("touch_not_found")
        if row["state"] not in {"reserved", "clicked", "uncertain"}:
            raise ValueError("touch_not_pending")
        conn.execute("UPDATE daily_touches SET state=?,finished_at_ms=?,detail=?,reason=? WHERE touch_id=?", (target, now_ms, detail[:500], "manual_" + target, touch_id))
        if target == "sent":
            _credit_sent(conn, row, now_ms)
        _set_item_result(conn, row["run_id"], row["user_id"], target, "manual_" + target, now_ms)
        if target == "sent":
            _set_account_result(conn, row["run_id"], row["user_id"], row["account_id"],
                                "sent", "manual_sent", now_ms)
    return {"ok": True, "touch_id": touch_id, "state": target}


def report(conn: sqlite3.Connection, run_id: str, *, now_ms: int | None = None) -> dict:
    """Summarize confirmed observations and associations, never causal claims."""
    now_ms = epoch_ms() if now_ms is None else now_ms
    run = _run(conn, run_id)
    counts = {row["last_reason"] or row["last_action"]: row["n"] for row in conn.execute(
        "SELECT last_action,last_reason,COUNT(*) n FROM daily_run_items WHERE run_id=? GROUP BY last_action,last_reason",
        (run_id,),
    )}
    sent_this_run = conn.execute(
        "SELECT COUNT(*) FROM daily_touches WHERE run_id=? AND state='sent'", (run_id,)
    ).fetchone()[0]
    new_tags = conn.execute(
        "SELECT COUNT(*) FROM followup_tracking WHERE first_tag_run_id=?", (run_id,)
    ).fetchone()[0]
    tracked = conn.execute(
        """SELECT user_id,first_source,first_sent_at_ms,last_sent_at_ms,
                  first_tag_observed_at_ms,last_observed_at_ms,last_observation_status,
                  manual_intervention
           FROM followup_tracking WHERE first_pre_send_absent_at_ms IS NOT NULL
             AND first_sent_at_ms IS NOT NULL"""
    ).fetchall()
    cohort = {"eligible": 0, "converted": 0, "observing": 0, "matured_unknown": 0,
              "confirmed_no_tag": 0, "manual_intervention": 0}
    by_source = {source: dict(cohort) for source in SOURCES}
    for person in tracked:
        counters = [cohort]
        if person["first_source"] in by_source:
            counters.append(by_source[person["first_source"]])
        if person["first_tag_observed_at_ms"] is not None and person["first_tag_observed_at_ms"] >= person["first_sent_at_ms"]:
            kind = "converted"
        elif now_ms - person["last_sent_at_ms"] < OBSERVATION_TAIL_MS:
            kind = "observing"
        elif person["last_observation_status"] == "absent" and (person["last_observed_at_ms"] or 0) >= person["last_sent_at_ms"] + OBSERVATION_TAIL_MS:
            kind = "confirmed_no_tag"
        else:
            kind = "matured_unknown"
        for counter in counters:
            counter["eligible"] += 1
            counter[kind] += 1
            counter["manual_intervention"] += int(person["manual_intervention"])
    for counter in (cohort, *by_source.values()):
        counter["confirmed_minimum_rate"] = (
            counter["converted"] / counter["eligible"] if counter["eligible"] else None
        )
    versions = {}
    sent_uids_by_version: dict[str, set[str]] = {}
    tracked_by_uid = {row["user_id"]: row for row in tracked}
    touches_by_uid: dict[str, list[sqlite3.Row]] = {}
    manual_times_by_uid: dict[str, set[int]] = {}
    for observation in conn.execute(
        "SELECT user_id,manual_outbound_at_ms FROM lead_observations WHERE manual_outbound_at_ms IS NOT NULL"
    ):
        manual_times_by_uid.setdefault(observation["user_id"], set()).add(observation["manual_outbound_at_ms"])
    for touch in conn.execute(
        """SELECT user_id,version_id,source,finished_at_ms,manual_intervention
           FROM daily_touches WHERE state='sent'
           ORDER BY user_id,finished_at_ms,touch_id"""
    ):
        touches_by_uid.setdefault(touch["user_id"], []).append(touch)
        item = versions.setdefault(touch["version_id"], {
            "source": touch["source"], "sent_customers": 0,
            "sent_messages": 0, "unique_sent_customers": 0, "observation_windows": 0,
            "closed_windows": 0, "associated_tags": 0,
            "observing": 0, "matured_unknown": 0,
            "closed_no_tag": 0, "manual_intervention": 0,
            "manual_intervened_windows": 0,
        })
        item["sent_customers"] += 1  # Legacy field counted messages.
        item["sent_messages"] += 1
        item["observation_windows"] += 1
        sent_uids_by_version.setdefault(touch["version_id"], set()).add(touch["user_id"])
        item["unique_sent_customers"] = len(sent_uids_by_version[touch["version_id"]])
        item["manual_intervention"] += int(touch["manual_intervention"])
    for user_id, sends in touches_by_uid.items():
        person = tracked_by_uid.get(user_id)
        tag_at = person["first_tag_observed_at_ms"] if person else None
        for index, touch in enumerate(sends):
            item = versions[touch["version_id"]]
            at = touch["finished_at_ms"]
            next_at = sends[index + 1]["finished_at_ms"] if index + 1 < len(sends) else None
            cutoff = at + OBSERVATION_TAIL_MS
            associated = (
                tag_at is not None and at <= tag_at <= cutoff
                and (next_at is None or tag_at < next_at)
            )
            end = min(
                cutoff,
                next_at if next_at is not None else cutoff,
                tag_at if associated else cutoff,
                now_ms,
            )
            if any(at < manual_at <= end for manual_at in manual_times_by_uid.get(user_id, ())):
                item["manual_intervened_windows"] += 1
            if associated:
                item["closed_windows"] += 1
                item["associated_tags"] += 1
            elif next_at is not None and next_at <= now_ms and next_at <= cutoff:
                # The next successful send required a fresh no-tag check.
                item["closed_windows"] += 1
                item["closed_no_tag"] += 1
            elif now_ms < cutoff:
                item["observing"] += 1
            else:
                item["closed_windows"] += 1
                if person and person["last_observation_status"] == "absent" and (person["last_observed_at_ms"] or 0) >= cutoff:
                    item["closed_no_tag"] += 1
                else:
                    item["matured_unknown"] += 1
    for item in versions.values():
        item["association_rate_minimum"] = (
            item["associated_tags"] / item["closed_windows"] if item["closed_windows"] else None
        )
    pending = conn.execute(
        "SELECT state,COUNT(*) n FROM daily_touches WHERE run_id=? AND state IN ('reserved','clicked') GROUP BY state",
        (run_id,),
    ).fetchall()
    send_issues = [dict(row) for row in conn.execute(
        """SELECT touch_id,run_id,user_id,account_id,state,
                  COALESCE(NULLIF(reason,''),state) AS reason,detail,
                  prepared_at_ms,clicked_at_ms,finished_at_ms
           FROM daily_touches WHERE run_id=? AND state IN ('failed','uncertain')
           ORDER BY prepared_at_ms,touch_id""", (run_id,)
    )]
    issue_reasons: dict[str, dict[str, int]] = {"failed": {}, "uncertain": {}}
    for issue in send_issues:
        summary = issue_reasons[issue["state"]]
        summary[issue["reason"]] = summary.get(issue["reason"], 0) + 1
    change_copy = {"ad": 0, "natural": 0}
    for row in conn.execute(
        """SELECT last_source,COUNT(*) n FROM daily_run_items
           WHERE run_id=? AND last_reason='same_message_previously_sent'
           GROUP BY last_source""",
        (run_id,),
    ):
        if row["last_source"] in change_copy:
            change_copy[row["last_source"]] = row["n"]
    browser_deferred = {
        "main_attempts": conn.execute(
            "SELECT COUNT(*) FROM daily_browser_deferrals WHERE run_id=? AND retry_stage='main'",
            (run_id,),
        ).fetchone()[0],
        "slow_attempts": conn.execute(
            "SELECT COUNT(*) FROM daily_browser_deferrals WHERE run_id=? AND retry_stage='slow'",
            (run_id,),
        ).fetchone()[0],
        "slow_retry_done": conn.execute(
            "SELECT COUNT(*) FROM daily_run_items WHERE run_id=? AND slow_retry_count>0",
            (run_id,),
        ).fetchone()[0],
    }
    return {
        "ok": True, "run_id": run_id, "started_at_ms": run["started_at_ms"],
        "touch_policy": _touch_policy(run), "run_state": run["state"],
        "run_summary": run_status(conn, run_id, now_ms=now_ms),
        "scope_mode": run["scope_mode"], "account_ids": _array(run["account_ids_json"]),
        "queue_count": conn.execute("SELECT COUNT(*) FROM daily_run_items WHERE run_id=?", (run_id,)).fetchone()[0],
        "sent_this_run": sent_this_run, "new_tags_this_run": new_tags,
        "failed_this_run": sum(issue_reasons["failed"].values()),
        "uncertain_this_run": sum(issue_reasons["uncertain"].values()),
        "send_failure_reasons": issue_reasons["failed"],
        "send_uncertain_reasons": issue_reasons["uncertain"],
        "send_issues": send_issues,
        "reasons": counts, "pending_send_states": {row["state"]: row["n"] for row in pending},
        "cumulative": cohort, "by_source": by_source, "versions": versions,
        "versions_scope": "all_recorded_runs",
        "metric_notes": {"sent_customers": "Legacy alias of sent_messages; it counts successful messages, not unique customers.",
                         "observation_windows": "One seven-day association window per successful message; a later send closes the earlier window.",
                         "cross_version": "unique_sent_customers deduplicates UID across accounts and runs within each version. Counts across versions must not be added to infer global customers."},
        "change_copy_reminder": {"uids_count": sum(change_copy.values()), "by_source": change_copy},
        "browser_deferred": browser_deferred,
        "no_intent_updates": [dict(row) for row in conn.execute(
            "SELECT user_id,account_id,state,detail FROM no_intent_updates WHERE run_id=?", (run_id,))],
        "metric_note": "Rates are confirmed minimums; message association is observational, not causal.",
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute('PRAGMA table_info("' + table.replace('"', '""') + '")')]


def _copy_legacy_table(dest: sqlite3.Connection, source: sqlite3.Connection, table: str) -> tuple[int, int]:
    if not _table_exists(source, table):
        return 0, 0
    if not _table_exists(dest, table):
        ddl = source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
        dest.execute(ddl)
    columns = [column for column in _columns(source, table) if column in _columns(dest, table)]
    identifiers = ",".join('"' + column.replace('"', '""') + '"' for column in columns)
    placeholders = ",".join("?" for _ in columns)
    before = dest.total_changes
    source_count = source.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    for row in source.execute(f'SELECT {identifiers} FROM "{table}"'):
        dest.execute(
            f'INSERT OR IGNORE INTO "{table}" ({identifiers}) VALUES ({placeholders})',
            tuple(row),
        )
    imported = dest.total_changes - before
    return imported, source_count - imported


def _merge_historical_accounts(dest: sqlite3.Connection, source: sqlite3.Connection, source_path: Path) -> tuple[int, int]:
    if not _table_exists(source, "historical_user_accounts"):
        return 0, 0
    columns = [column for column in _columns(source, "historical_user_accounts") if column in _columns(dest, "historical_user_accounts")]
    identifiers = ",".join('"' + column + '"' for column in columns)
    placeholders = ",".join("?" for _ in columns)
    inserted = 0
    conflicts = 0
    for row in source.execute(f"SELECT {identifiers} FROM historical_user_accounts"):
        row_dict = dict(row)
        snapshot_id = digest(_json([str(source_path), row_dict]))
        dest.execute(
            """INSERT OR IGNORE INTO legacy_account_snapshots
               (snapshot_id,source_path,user_id,account_name,account_id,row_json)
               VALUES (?,?,?,?,?,?)""",
            (snapshot_id, str(source_path), row["user_id"], row["account_name"],
             str(row_dict.get("account_id") or ""), _json(row_dict)),
        )
        old = dest.execute(
            "SELECT * FROM historical_user_accounts WHERE user_id=? AND account_name=?",
            (row["user_id"], row["account_name"]),
        ).fetchone()
        if old is None:
            dest.execute(
                f"INSERT INTO historical_user_accounts ({identifiers}) VALUES ({placeholders})",
                tuple(row),
            )
            inserted += 1
            continue
        incoming_account = row["account_id"] if "account_id" in columns else ""
        current_account = old["account_id"]
        if incoming_account and current_account and incoming_account != current_account:
            conflicts += 1
            dest.execute(
                """INSERT OR IGNORE INTO legacy_account_conflicts
                   (user_id,account_name,existing_account_id,incoming_account_id,source_path)
                   VALUES (?,?,?,?,?)""",
                (row["user_id"], row["account_name"], current_account, incoming_account, str(source_path)),
            )
        incoming_ids = _array(row["source_import_ids_json"] if "source_import_ids_json" in columns else "[]")
        merged_ids = list(dict.fromkeys(_array(old["source_import_ids_json"]) + incoming_ids))
        latest_in = max(old["latest_in_at_ms"] or 0, row["latest_in_at_ms"] if "latest_in_at_ms" in columns and row["latest_in_at_ms"] else 0) or None
        latest_open = max(old["latest_open_at_ms"] or 0, row["latest_open_at_ms"] if "latest_open_at_ms" in columns and row["latest_open_at_ms"] else 0) or None
        latest_lead = max(old["latest_lead_at_ms"] or 0, row["latest_lead_at_ms"] if "latest_lead_at_ms" in columns and row["latest_lead_at_ms"] else 0) or None
        lead = "has_lead" if old["lead_status_from_import"] == "has_lead" or ("lead_status_from_import" in columns and row["lead_status_from_import"] == "has_lead") else "no_lead"
        dest.execute(
            """UPDATE historical_user_accounts SET account_id=?,latest_in_at_ms=?,latest_open_at_ms=?,
               latest_lead_at_ms=?,lead_status_from_import=?,phone_present=?,wechat_present=?,
               deleted=?,source_import_ids_json=?,updated_at_ms=?
               WHERE user_id=? AND account_name=?""",
            (current_account or incoming_account, latest_in, latest_open, latest_lead, lead,
             max(old["phone_present"], row["phone_present"] if "phone_present" in columns else 0),
             max(old["wechat_present"], row["wechat_present"] if "wechat_present" in columns else 0),
             max(old["deleted"], row["deleted"] if "deleted" in columns else 0),
             _json(merged_ids), max(old["updated_at_ms"], row["updated_at_ms"] if "updated_at_ms" in columns else 0),
             row["user_id"], row["account_name"]),
        )
    return inserted, conflicts


def migrate_legacy(conn: sqlite3.Connection, source_path: Path) -> dict:
    """Merge a legacy DB into the Work ledger without writing to the source.

    This command does not synthesize platform lead tags from CSV or old lead
    statuses. Root handles external backups before invoking it on real data.
    """
    source_path = Path(source_path).expanduser().resolve()
    target_path = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    if source_path == target_path:
        raise ValueError("source_equals_destination")
    if not source_path.is_file():
        raise ValueError("legacy_source_missing")
    source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    imported_attempts = 0
    copied = {}
    ignored_existing_or_conflicting = {}
    account_conflicts = 0
    snapshotted_rows_added = 0
    try:
        with conn:
            # Preserve each source variant before INSERT OR IGNORE resolves PK
            # collisions. This also retains raw rows omitted from the active
            # daily schema, with the read-only source path for later review.
            for (table_name,) in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ):
                safe_name = '"' + table_name.replace('"', '""') + '"'
                for source_row in source.execute(f"SELECT * FROM {safe_name}"):
                    raw = {
                        key: ({"blob_base64": base64.b64encode(value).decode("ascii")}
                              if isinstance(value, bytes) else value)
                        for key, value in dict(source_row).items()
                    }
                    row_json = _json(raw)
                    snapshot_id = digest(_json([str(source_path), table_name, row_json]))
                    before = conn.total_changes
                    conn.execute(
                        """INSERT OR IGNORE INTO legacy_row_snapshots
                           (snapshot_id,source_path,table_name,row_json,snapshotted_at_ms)
                           VALUES (?,?,?,?,?)""",
                        (snapshot_id, str(source_path), table_name, row_json, epoch_ms()),
                    )
                    snapshotted_rows_added += conn.total_changes - before
            for table in ("campaigns", "campaign_batches", "candidates", "historical_imports", "historical_users", "historical_uid_reviews", "campaign_events", "batch_items", "batch_item_events"):
                copied[table], ignored_existing_or_conflicting[table] = _copy_legacy_table(conn, source, table)
            copied["historical_user_accounts"], account_conflicts = _merge_historical_accounts(conn, source, source_path)
            if _table_exists(source, "send_attempts"):
                columns = _columns(source, "send_attempts")
                for row in source.execute("SELECT * FROM send_attempts"):
                    item = dict(row)
                    user_id = str(item.get("user_id") or "")
                    account_id = str(item.get("account_id") or "")
                    status = str(item.get("status") or "")
                    attempted = int(item.get("attempted_at_ms") or 0)
                    snapshot = str(item.get("message_snapshot") or "")
                    exact_hash = str(item.get("message_hash_snapshot") or "") or (digest(snapshot) if snapshot else "")
                    normalized_hash = digest(canonical_copy(snapshot)) if snapshot else ""
                    fingerprint = digest(_json([
                        item.get("campaign_id"), item.get("conversation_key"), user_id,
                        account_id, status, item.get("detail"), attempted, snapshot, exact_hash,
                    ]))
                    before = conn.total_changes
                    conn.execute(
                        """INSERT OR IGNORE INTO legacy_send_events
                           (fingerprint,user_id,account_id,status,attempted_at_ms,message_snapshot,
                            normalized_hash,exact_hash,campaign_id,detail)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (fingerprint, user_id, account_id, status, attempted, snapshot,
                         normalized_hash, exact_hash, str(item.get("campaign_id") or ""),
                         str(item.get("detail") or "")),
                    )
                    imported_attempts += conn.total_changes - before
        return {"ok": True, "source": str(source_path), "imported_attempts": imported_attempts,
                "copied_tables": copied,
                "ignored_existing_or_conflicting_rows": ignored_existing_or_conflicting,
                "account_conflicts": account_conflicts,
                "snapshotted_rows_added": snapshotted_rows_added,
                "source_send_attempts": source.execute("SELECT COUNT(*) FROM send_attempts").fetchone()[0] if _table_exists(source, "send_attempts") else 0}
    finally:
        source.close()


def _configured_accounts() -> list[dict]:
    config_path = Path(os.environ.get("XHS_FOLLOWUP_CONFIG", SKILL_DIR / "config.json")).expanduser()
    if not config_path.is_file():
        return []
    data = json.loads(config_path.read_text(encoding="utf-8"))
    return _managed_accounts(data.get("accounts", []))


def _input_payload(path: str) -> dict:
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=Path(os.environ.get("XHS_FOLLOWUP_STATE_DB", DEFAULT_DB)))
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    register = sub.add_parser("register-run")
    register.add_argument("--input-json", required=True)
    auth = sub.add_parser("authorize-remaining")
    auth.add_argument("--run-id", required=True)
    auth.add_argument("--evidence", required=True)
    touch_policy = sub.add_parser("set-touch-policy", help="Audited change for future attempts; existing touch records are preserved")
    touch_policy.add_argument("--run-id", required=True)
    touch_policy.add_argument("--policy", choices=sorted(TOUCH_POLICIES), required=True)
    touch_policy.add_argument("--evidence", required=True)
    run_status_parser = sub.add_parser("run-status", help="Read compact counts including exhausted retries and pending labels")
    run_status_parser.add_argument("--run-id", required=True)
    run_status_parser.add_argument("--include-items", action="store_true", help="Include all uncompleted UID and pending label details")
    for command in ("end-run", "pause-run", "resume-run"):
        lifecycle = sub.add_parser(command)
        lifecycle.add_argument("--run-id", required=True)
        lifecycle.add_argument("--detail", required=True)
        if command == "pause-run":
            lifecycle.add_argument("--state", choices=["paused", "blocked"], default="paused")
    q = sub.add_parser("queue")
    q.add_argument("--run-id", required=True)
    nxt = sub.add_parser("next", help="Read only the next small group; initialize via queue or begin-session first")
    nxt.add_argument("--run-id", required=True)
    nxt.add_argument("--limit", type=int, default=1)
    nxt.add_argument("--input-json", help="Optional local UID exclusions; '-' reads JSON from stdin")
    begin = sub.add_parser("begin-session", help="Persist a baseline using the original user request ID")
    begin.add_argument("--run-id", required=True)
    begin.add_argument("--session-id", required=True)
    status = sub.add_parser("session-status", help="Read a session baseline and count delta without writes")
    status_selection = status.add_mutually_exclusive_group(required=True)
    status_selection.add_argument("--session-id")
    status_selection.add_argument("--run-id", help="Recover the active or most recent user session")
    end = sub.add_parser("end-session")
    end.add_argument("--session-id", required=True)
    end.add_argument("--detail", default="")
    pending = sub.add_parser("pending-touches")
    pending.add_argument("--run-id")
    browser_defer = sub.add_parser("record-browser-defer")
    browser_defer.add_argument("--input-json", required=True)
    correction = sub.add_parser("reopen-browser-defer", help="Preview an audited correction; --apply restores normal review")
    correction.add_argument("--input-json", required=True)
    correction.add_argument("--apply", action="store_true")
    discovered = sub.add_parser("add-run-account")
    discovered.add_argument("--input-json", required=True)
    no_intent = sub.add_parser("record-no-intent")
    no_intent.add_argument("--input-json", required=True)
    prepare = sub.add_parser("prepare-touch")
    prepare.add_argument("--input-json", required=True)
    reviewed = sub.add_parser("record-reviewed-history")
    reviewed.add_argument("--input-json", required=True)
    cancelled = sub.add_parser("cancel-unclicked-touch")
    cancelled.add_argument("--input-json", required=True)
    clicked = sub.add_parser("mark-clicked")
    clicked.add_argument("--touch-id", required=True)
    finish = sub.add_parser("finish-touch")
    finish.add_argument("--input-json", required=True)
    resolve = sub.add_parser("resolve-touch")
    resolve.add_argument("--touch-id", required=True)
    resolve.add_argument("--resolution", choices=["confirmed_sent", "confirmed_not_sent"], required=True)
    resolve.add_argument("--detail", required=True)
    rep = sub.add_parser("report")
    rep.add_argument("--run-id", required=True)
    migrate = sub.add_parser("migrate-legacy")
    migrate.add_argument("--source", type=Path, required=True)
    return p


def main() -> None:
    args = parser().parse_args()
    try:
        readonly = args.command in {"next", "session-status", "report", "pending-touches", "run-status"} or (
            args.command == "reopen-browser-defer" and not args.apply)
        with (connect_readonly(args.db) if readonly else connect(args.db)) as conn:
            if args.command == "init":
                out = {"ok": True, "db": str(args.db.expanduser())}
            elif args.command == "register-run":
                value = _input_payload(args.input_json)
                out = register_run(conn, value.get("scope_mode", ""), value.get("account_ids", []),
                                   value.get("messages", {}), [] if value.get("scope_mode") == "all" else _configured_accounts(), import_ids=value.get("source_import_ids"), touch_policy=value.get("touch_policy", "uid_once"))
            elif args.command == "authorize-remaining":
                out = authorize_remaining(conn, args.run_id, args.evidence)
            elif args.command == "set-touch-policy":
                out = set_touch_policy(conn, args.run_id, args.policy, args.evidence)
            elif args.command == "run-status":
                out = run_status(conn, args.run_id, include_items=args.include_items)
            elif args.command == "end-run":
                out = end_run(conn, args.run_id, args.detail)
            elif args.command == "pause-run":
                out = pause_run(conn, args.run_id, args.detail, state=args.state)
            elif args.command == "resume-run":
                out = resume_run(conn, args.run_id, args.detail)
            elif args.command == "queue":
                out = queue(conn, args.run_id)
            elif args.command == "next":
                value = _input_payload(args.input_json) if args.input_json else {}
                if not isinstance(value, dict):
                    raise ValueError("invalid_exclude_user_ids")
                out = next_items(conn, args.run_id, limit=args.limit,
                                 exclude_user_ids=value.get("exclude_user_ids", []))
            elif args.command == "begin-session":
                out = begin_session(conn, args.run_id, args.session_id)
            elif args.command == "session-status":
                out = session_status(conn, args.session_id, run_id=args.run_id)
            elif args.command == "end-session":
                out = end_session(conn, args.session_id, args.detail)
            elif args.command == "pending-touches":
                out = pending_touches(conn, args.run_id)
            elif args.command == "record-browser-defer":
                out = record_browser_defer(conn, _input_payload(args.input_json))
            elif args.command == "reopen-browser-defer":
                out = reopen_browser_defer(conn, _input_payload(args.input_json), apply=args.apply)
            elif args.command == "add-run-account":
                out = add_run_account(conn, _input_payload(args.input_json))
            elif args.command == "prepare-touch":
                out = prepare_touch(conn, _input_payload(args.input_json))
            elif args.command == "record-reviewed-history":
                out = record_reviewed_history(conn, _input_payload(args.input_json))
            elif args.command == "cancel-unclicked-touch":
                out = cancel_unclicked_touch(conn, _input_payload(args.input_json))
            elif args.command == "record-no-intent":
                out = record_no_intent(conn, _input_payload(args.input_json))
            elif args.command == "mark-clicked":
                out = mark_clicked(conn, args.touch_id)
            elif args.command == "finish-touch":
                out = finish_touch(conn, _input_payload(args.input_json))
            elif args.command == "resolve-touch":
                out = resolve_touch(conn, args.touch_id, args.resolution, args.detail)
            elif args.command == "report":
                out = report(conn, args.run_id)
            elif args.command == "migrate-legacy":
                out = migrate_legacy(conn, args.source)
            else:
                raise AssertionError(args.command)
        print(_json(out))
    except (ValueError, sqlite3.Error, OSError, json.JSONDecodeError) as exc:
        print(_json({"ok": False, "error": str(exc)}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
