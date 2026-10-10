#!/usr/bin/env python3
"""Temporary-DB benchmark of 10k legacy sends and 30 prepared+finished touches."""

import json
import math
import statistics
import tempfile
import time
from pathlib import Path

import daily_followup_state as state


NOW = 1_800_000_000_000
DAY = state.DAY_MS
SAMPLES = 30


def percentile(values, pct):
    ordered = sorted(values)
    return ordered[math.ceil(pct * len(ordered)) - 1]


def main():
    with tempfile.TemporaryDirectory() as temporary:
        db = Path(temporary) / "benchmark.sqlite3"
        connection_start = time.perf_counter()
        conn = state.connect(db)
        connection_ms = (time.perf_counter() - connection_start) * 1000
        with conn:
            conn.executemany(
                """INSERT INTO legacy_send_events
                   (fingerprint,user_id,account_id,status,attempted_at_ms,message_snapshot,
                    normalized_hash,exact_hash,campaign_id,detail)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                ((f"f{i}", f"legacy-{i}", "a", "sent", NOW - 30 * DAY,
                  "旧话术", state.digest("旧话术"), state.digest("旧话术"), "c", "")
                 for i in range(10_000)),
            )
            conn.execute(
                """INSERT INTO historical_imports
                   VALUES ('i1','synthetic.csv','2026-09-29',?,30,30,0,30,0)""",
                (NOW,),
            )
            conn.executemany(
                """INSERT INTO historical_user_accounts
                   (user_id,account_name,account_id,latest_in_at_ms,
                    lead_status_from_import,source_import_ids_json,updated_at_ms)
                   VALUES (?,?,?,?,?,?,?)""",
                ((f"target-{i}", "账号甲", "a", NOW - DAY,
                  "no_lead", '["i1"]', NOW) for i in range(SAMPLES)),
            )
        run_id = state.register_run(
            conn, "specified", ["a"], {"ad": "本次广告话术", "natural": "本次自然话术"},
            [{"id": "a", "name": "账号甲"}], now_ms=NOW, require_fresh_import=False,
        )["run_id"]
        state.queue(conn, run_id, now_ms=NOW)
        timings = []
        for i in range(SAMPLES):
            review = {
                "user_id": f"target-{i}", "account_id": "a", "observed_at_ms": NOW,
                "business_reviewed": True, "identity_verified": True, "panel_loaded": True, "label": "none",
                "source": "ad", "attribution_note_state": "concrete",
                "history_complete": True, "history_from_start_complete": True,
                "history_reached_prior_boundary": False,
                "history_boundary_message_id": f"msg-{i}",
                "outbound_text_hashes": [], "reply_status": "awaiting_customer",
                "negative_status": "safe", "party_status": "customer",
                "draft_present": False, "protective_lead_signal": False,
                "last_outbound_text_at_ms": NOW - 2 * DAY,
                "last_customer_at_ms": NOW - 3 * DAY,
                "manual_outbound_at_ms": None,
            }
            start = time.perf_counter()
            prepared = state.prepare_touch(conn, {
                "run_id": run_id, "user_id": f"target-{i}",
                "selected_account_id": "a", "search_complete": True, "discovered_account_ids": ["a"], "reviews": [review],
            }, now_ms=NOW)
            if prepared["action"] != "send":
                raise AssertionError(prepared)
            prepared_ms = (time.perf_counter() - start) * 1000
            state.mark_clicked(conn, prepared["touch_id"], now_ms=NOW)
            start = time.perf_counter()
            state.finish_touch(conn, {"touch_id": prepared["touch_id"], "status": "sent"}, now_ms=NOW)
            finished_ms = (time.perf_counter() - start) * 1000
            timings.append(prepared_ms + finished_ms)
        result = {
            "ok": percentile(timings, .95) <= 1000,
            "records": 10_000,
            "touch_samples": SAMPLES,
            "connect_ms": round(connection_ms, 2),
            "prepare_finish_p50_ms": round(statistics.median(timings), 2),
            "prepare_finish_p95_ms": round(percentile(timings, .95), 2),
            "threshold_p95_ms": 1000,
            "synthetic_sent_messages": state._touch_counts(conn, run_id)["sent"],
            "synthetic_sent_customers": state._touch_counts(conn, run_id)["sent_customers"],
            "real_delivery_verified": False,
            "browser_messages_per_hour": None,
            "time_basis": "perf_counter durations of local prepare and finish only; browser waits, pauses and per-UID review waits are excluded",
            "scope": "local SQLite only; browser and send confirmation excluded",
        }
        print(json.dumps(result, ensure_ascii=False))
        conn.close()
        if not result["ok"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
