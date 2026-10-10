#!/usr/bin/env python3
"""Behavior tests for the daily follow-up ledger; all data stays in a temp DB."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import daily_followup_state as state


NOW = 1_800_000_000_000
DAY = 86_400_000
ACCOUNTS = [{"id": "a", "name": "账号甲"}, {"id": "b", "name": "账号乙"}]


def review(uid="u1", account="a", **changes):
    value = {
        "user_id": uid,
        "account_id": account,
        "observed_at_ms": NOW,
        "identity_verified": True,
        "panel_loaded": True,
        "label": "none",
        "source": "ad",
        "attribution_note_state": "concrete",
        "history_complete": True,
        "reply_status": "awaiting_customer",
        "negative_status": "safe",
        "party_status": "customer",
        "draft_present": False,
        "last_outbound_text_at_ms": NOW - 2 * DAY,
        "last_customer_at_ms": NOW - 3 * DAY,
        "manual_outbound_at_ms": None,
        "outbound_text_hashes": [],
        "history_from_start_complete": True,
        "history_reached_prior_boundary": False,
        "history_boundary_message_id": "msg-latest",
        "protective_lead_signal": False,
        "business_reviewed": True,
    }
    value.update(changes)
    return value


class DailyStateTest(unittest.TestCase):
    def test_explicit_remaining_authorization_continues_original_run_across_days(self):
        self.candidate()
        rid = self.run_id()
        state.queue(self.conn, rid, now_ms=NOW)
        self.assertTrue(state.queue(self.conn, rid, now_ms=NOW + DAY)['run_date_expired'])
        state.authorize_remaining(self.conn, rid, 'direct human continuation', now_ms=NOW + DAY)
        self.assertFalse(state.queue(self.conn, rid, now_ms=NOW + DAY)['run_date_expired'])
        touch = state.prepare_touch(self.conn, {
            'run_id': rid, 'user_id': 'u1', 'selected_account_id': 'a',
            'search_complete': True, 'discovered_account_ids': ['a'],
            'reviews': [review(observed_at_ms=NOW + DAY)],
        }, now_ms=NOW + DAY)
        self.assertEqual(touch['action'], 'send')
        self.assertEqual(state.mark_clicked(self.conn, touch['touch_id'], now_ms=NOW + DAY)['state'], 'clicked')
        self.assertEqual(state._run(self.conn, rid)['started_at_ms'], NOW)
        other = self.run_id()
        self.assertTrue(state.queue(self.conn, other, now_ms=NOW + DAY)['run_date_expired'])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.conn = state.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def run_id(self, *, mode="all", account_ids=None, ad="广告话术", natural="自然话术", touch_policy="account_once"):
        result = state.register_run(
            self.conn,
            mode,
            account_ids or [],
            {"ad": ad, "natural": natural},
            ACCOUNTS,
            now_ms=NOW,
            require_fresh_import=False,
            touch_policy=touch_policy,
        )
        return result["run_id"]

    def candidate(self, uid="u1", account="a", latest_in_at_ms=None, lead="no_lead", import_id="i1"):
        self.conn.execute(
            "INSERT OR IGNORE INTO historical_imports (import_id,source_file,cutoff_date,imported_at_ms,row_count,unique_user_ids,lead_users,targetable_no_lead_users,deleted_users) VALUES (?,?,?,?,?,?,?,?,?)",
            (import_id, "test.csv", "2026-09-29", NOW, 1, 1, 0, 1, 0),
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO historical_user_accounts (user_id,account_name,account_id,latest_in_at_ms,lead_status_from_import,deleted,source_import_ids_json,updated_at_ms) VALUES (?,?,?,?,?,?,?,?)",
            (uid, "账号甲" if account == "a" else "账号乙", account, latest_in_at_ms or NOW - DAY, lead, 0, '["i1"]', NOW),
        )
        self.conn.commit()

    def prepare(self, run_id, uid="u1", account="a", reviews=None):
        return state.prepare_touch(self.conn, {
            "run_id": run_id,
            "user_id": uid,
            "selected_account_id": account,
            "search_complete": True,
            "discovered_account_ids": next(x for x in state.queue(self.conn, run_id, now_ms=NOW)["items"] if x["user_id"] == uid)["account_ids"],
            "reviews": reviews if reviews is not None else [review(uid, account)],
        }, now_ms=NOW)

    def test_all_scope_resolves_to_explicit_accounts_and_empty_specified_fails(self):
        run_id = self.run_id()
        row = self.conn.execute("SELECT account_ids_json FROM daily_runs WHERE run_id=?", (run_id,)).fetchone()
        self.assertEqual(json.loads(row[0]), [])
        with self.assertRaisesRegex(ValueError, "account_scope_empty"):
            self.run_id(mode="specified")
        self.assertTrue(state.register_run(self.conn, "all", [], {"ad": "A"}, [], now_ms=NOW, require_fresh_import=False)["ok"])
        with self.assertRaisesRegex(ValueError, "managed_accounts_missing"):
            state.register_run(self.conn, "specified", ["c"], {"ad": "A"}, [], now_ms=NOW, require_fresh_import=False)
        with self.assertRaisesRegex(ValueError, "account_scope_unknown"):
            state.register_run(self.conn, "specified", ["c"], {"ad": "A"}, ACCOUNTS, now_ms=NOW, require_fresh_import=False)
        with self.assertRaisesRegex(ValueError, "user_list_import_required"):
            state.register_run(self.conn, "all", [], {"ad": "A"}, ACCOUNTS, now_ms=NOW, require_fresh_import=True)

    def test_reviewed_history_can_wait_without_creating_a_send_reservation(self):
        self.candidate()
        run_id = self.run_id()
        r = review(context_complete=True, observed_at_ms=NOW-600_000,
                   messages=[{"id": "msg-latest", "side": "left", "text": "我们做建材"}])
        out = state.record_reviewed_history(self.conn, {"run_id": run_id, "user_id": "u1", "reviews": [r]}, now_ms=NOW)
        self.assertEqual(out["sends"], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM daily_touches").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT business_reviewed FROM conversation_history_cache").fetchone()[0], 1)
        # Historical approval does not remove the fresh page observation gate.
        self.assertEqual(self.prepare(run_id, reviews=[r])["action"], "defer")

    def test_reviewed_history_protects_split_contact_globally_without_sending(self):
        self.candidate()
        self.candidate(account="b")
        run_id = self.run_id()
        r = review(context_complete=True, protective_lead_signal=True, contact_evidence="客户分段提供手机号",
                   messages=[{"id": "msg-latest", "side": "left", "text": "8000"}])
        state.record_reviewed_history(self.conn, {"run_id": run_id, "user_id": "u1", "reviews": [r]}, now_ms=NOW)
        self.assertEqual(self.prepare(run_id, account="b", reviews=[review(), review(account="b")])["reason"], "protective_lead_signal")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM daily_touches").fetchone()[0], 0)

    def test_reviewed_history_rolls_back_all_accounts_on_invalid_second_review(self):
        self.candidate()
        run_id = self.run_id()
        first = review(context_complete=True, messages=[{"id": "msg-latest", "side": "left", "text": "你好"}])
        second = review(account="b", context_complete=False, messages=first["messages"])
        with self.assertRaisesRegex(ValueError, "review_history_unproven"):
            state.record_reviewed_history(self.conn, {"run_id": run_id, "user_id": "u1", "reviews": [first, second]}, now_ms=NOW)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM conversation_history_cache").fetchone()[0], 0)

    def test_reviewed_optout_requires_customer_quote_and_produces_label_work(self):
        self.candidate()
        run_id = self.run_id()
        r = review(context_complete=True, negative_status="blocked", stop_contact_evidence="不要再发",
                   messages=[{"id": "msg-latest", "side": "left", "text": "不要再发"}])
        out = state.record_reviewed_history(self.conn, {"run_id": run_id, "user_id": "u1", "reviews": [r]}, now_ms=NOW)
        self.assertEqual(len(out["label_updates"]), 1)
        self.assertEqual(self.prepare(run_id)["reason"], "stop_contact")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM daily_touches").fetchone()[0], 0)

    def test_only_a_proved_unclicked_reservation_can_be_cancelled_automatically(self):
        self.candidate()
        self.candidate(uid="u2")
        run_id = self.run_id()
        touch = self.prepare(run_id)
        with self.assertRaisesRegex(ValueError, "unclicked_stage_proof_required"):
            state.cancel_unclicked_touch(self.conn, {"touch_id": touch["touch_id"], "stage_status": "uncertain",
                "reason": "page_or_customer_changed"}, now_ms=NOW)
        out = state.cancel_unclicked_touch(self.conn, {"touch_id": touch["touch_id"], "stage_status": "not_staged",
            "reason": "page_or_customer_changed"}, now_ms=NOW)
        self.assertEqual(out["state"], "failed")
        self.assertEqual(state.pending_touches(self.conn)["items"], [])
        self.assertEqual(self.prepare(run_id)["reason"], "failed_this_run")
        clicked = self.prepare(run_id, uid="u2")
        state.mark_clicked(self.conn, clicked["touch_id"], now_ms=NOW)
        with self.assertRaisesRegex(ValueError, "touch_not_unclicked_reservation"):
            state.cancel_unclicked_touch(self.conn, {"touch_id": clicked["touch_id"], "stage_status": "not_staged",
                "reason": "page_or_customer_changed"}, now_ms=NOW)

    def test_cli_cannot_override_missing_managed_account_config(self):
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        now = state.epoch_ms()
        self.conn.execute("INSERT INTO historical_imports VALUES ('fresh','source.csv',?,?,1,1,0,1,0)", (today, now))
        self.conn.commit()
        env = os.environ.copy()
        env["XHS_FOLLOWUP_CONFIG"] = str(Path(self.tmp.name) / "no-config.json")
        result = subprocess.run(
            [sys.executable, str(Path(state.__file__)), "--db", str(self.path), "register-run", "--input-json", "-"],
            input=json.dumps({"scope_mode": "specified", "account_ids": ["unverified"],
                              "managed_accounts": [{"id": "unverified", "name": "虚构账号"}],
                              "messages": {"ad": "A"}}), text=True, capture_output=True, env=env,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"], "managed_accounts_missing")

    def test_database_and_wal_files_are_private(self):
        self.conn.close()
        os.chmod(self.path, 0o644)
        self.conn = state.connect(self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        for suffix in ("-wal", "-shm"):
            sibling = Path(str(self.path) + suffix)
            if sibling.exists():
                self.assertEqual(sibling.stat().st_mode & 0o777, 0o600)

    def test_queue_combines_latest_import_with_recent_sent_for_observation(self):
        self.candidate("u1")
        run_id = self.run_id()
        first = self.prepare(run_id)
        self.assertEqual(first["action"], "send")
        state.mark_clicked(self.conn, first["touch_id"], now_ms=NOW)
        state.finish_touch(self.conn, {"touch_id": first["touch_id"], "status": "sent"}, now_ms=NOW)
        self.conn.execute("DELETE FROM historical_user_accounts WHERE user_id='u1'")
        self.conn.commit()
        next_run = state.register_run(self.conn, "all", [], {"ad": "新版", "natural": "自然话术"}, ACCOUNTS, now_ms=NOW + DAY, require_fresh_import=False)["run_id"]
        items = state.queue(self.conn, next_run, now_ms=NOW + DAY)["items"]
        self.assertEqual([(item["user_id"], item["observation_only"]) for item in items], [("u1", True)])

    def test_run_freezes_import_used_for_candidate_selection(self):
        self.candidate("u1", import_id="i1")
        run_id = self.run_id()
        self.conn.execute("INSERT INTO historical_imports VALUES ('i2','new.csv','2026-09-30',?,1,1,0,1,0)", (NOW + 1,))
        self.conn.execute("INSERT INTO historical_user_accounts (user_id,account_name,account_id,latest_in_at_ms,lead_status_from_import,source_import_ids_json,updated_at_ms) VALUES ('u2','账号甲','a',?,'no_lead','[\"i2\"]',?)", (NOW, NOW))
        self.conn.commit()
        self.assertEqual([item["user_id"] for item in state.queue(self.conn, run_id, now_ms=NOW)["items"]], ["u1"])

    def test_candidate_without_real_csv_inbound_is_not_fresh_but_recent_send_is_rechecked(self):
        self.candidate("valid")
        self.conn.execute(
            """INSERT INTO historical_user_accounts
               (user_id,account_name,account_id,latest_in_at_ms,lead_status_from_import,source_import_ids_json,updated_at_ms)
               VALUES ('no-inbound','账号甲','a',NULL,'no_lead','[\"i1\"]',?)""",
            (NOW,),
        )
        run_id = self.run_id()
        self.assertEqual([item["user_id"] for item in state.queue(self.conn, run_id, now_ms=NOW)["items"]], ["valid", "no-inbound"])
        self.conn.execute(
            "INSERT INTO legacy_send_events VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("old-no-inbound", "no-inbound", "a", "sent", NOW-DAY, "旧话术", state.digest("旧话术"), state.digest("旧话术"), "c", ""),
        )
        new_run = self.run_id()
        new_items = state.queue(self.conn, new_run, now_ms=NOW)["items"]
        observed = {item["user_id"]: item["observation_only"] for item in new_items}
        self.assertEqual(observed, {"valid": False, "no-inbound": False})

    def test_prepare_requires_every_known_account_and_blocks_no_intent_or_unknown(self):
        self.candidate("u1", "a")
        self.candidate("u1", "b")
        run_id = self.run_id()
        self.assertEqual(self.prepare(run_id)["reason"], "account_review_incomplete")
        self.assertEqual(self.prepare(run_id, reviews=[review(), review(account="b", label="no_intent")])["reason"], "no_intent")
        self.assertEqual(self.prepare(run_id, reviews=[review(), review(account="b", panel_loaded=False, label="unknown")])["reason"], "panel_unknown")

    def test_legacy_contact_signal_blocks_send_without_counting_platform_tag(self):
        self.candidate("u1")
        run_id = self.run_id()
        self.assertEqual(self.prepare(run_id, reviews=[review(protective_lead_signal=True)])["reason"], "protective_lead_signal")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM lead_observations WHERE status='present'").fetchone()[0], 0)

    def test_customer_inbound_is_not_required_before_sending(self):
        self.candidate("u1")
        run_id = self.run_id()
        self.assertEqual(self.prepare(run_id, reviews=[review(last_customer_at_ms=None, party_status="unknown", reply_status="unknown")])["action"], "send")

    def test_recent_outbound_and_old_copy_do_not_block_new_run(self):
        self.candidate("u1", "a")
        self.candidate("u1", "b")
        run_id = self.run_id()
        two = [review(), review(account="b", last_outbound_text_at_ms=NOW - 12 * 3_600_000)]
        first = self.prepare(run_id, account="b", reviews=two)
        self.assertEqual(first["action"], "send")
        state.mark_clicked(self.conn, first["touch_id"], now_ms=NOW)
        state.finish_touch(self.conn, {"touch_id": first["touch_id"], "status": "sent"}, now_ms=NOW)
        next_run = state.register_run(self.conn, "all", [], {"ad": "广告话术", "natural": "自然话术"}, ACCOUNTS, now_ms=NOW + 2 * DAY, require_fresh_import=False)["run_id"]
        later = [review(observed_at_ms=NOW + 2 * DAY), review(account="b", observed_at_ms=NOW + 2 * DAY)]
        self.assertEqual(state.prepare_touch(self.conn, {"run_id": next_run, "user_id": "u1", "selected_account_id": "b", "search_complete": True, "discovered_account_ids": ["a", "b"], "reviews": later}, now_ms=NOW + 2 * DAY)["action"], "send")

    def test_uncertain_isolates_uid_across_accounts_and_runs_without_blocking_others(self):
        self.candidate("u1")
        self.candidate("u1", "b")
        self.candidate("u2")
        run_id = self.run_id()
        first = self.prepare(run_id, reviews=[review(), review(account="b")])
        self.assertEqual(self.prepare(run_id)["reason"], "pending_touch_requires_resolution")
        state.mark_clicked(self.conn, first["touch_id"], now_ms=NOW)
        state.finish_touch(self.conn, {"touch_id": first["touch_id"], "status": "uncertain",
                                      "reason": "receipt_unconfirmed", "detail": "没有稳定消息回执"}, now_ms=NOW)
        isolated = self.prepare(run_id, account="b", reviews=[review(), review(account="b")])
        self.assertEqual((isolated["action"], isolated["reason"]), ("skip", "uncertain_touch_requires_resolution"))
        current = state.queue(self.conn, run_id, now_ms=NOW)
        self.assertFalse(current["blocked_by_unfinished_touch"])
        self.assertFalse(current["blocked_by_uncertain"])
        self.assertEqual(current["quarantined_user_ids"], ["u1"])
        self.assertEqual([item["user_id"] for item in current["actionable_items"]], ["u2"])
        pending = state.pending_touches(self.conn)
        self.assertEqual(pending["items"], [])
        self.assertEqual([item["touch_id"] for item in pending["uncertain_items"]], [first["touch_id"]])
        self.assertEqual(pending["uncertain_items"][0]["detail"], "没有稳定消息回执")
        other = self.prepare(run_id, "u2")
        self.assertEqual(other["action"], "send")
        state.mark_clicked(self.conn, other["touch_id"], now_ms=NOW)
        state.finish_touch(self.conn, {"touch_id": other["touch_id"], "status": "sent"}, now_ms=NOW)
        later_run = state.register_run(self.conn, "all", [], {"ad": "新版"}, ACCOUNTS, now_ms=NOW + DAY, require_fresh_import=False)["run_id"]
        same_uid = state.prepare_touch(self.conn, {"run_id": later_run, "user_id": "u1", "selected_account_id": "b", "search_complete": True, "discovered_account_ids": ["a", "b"], "reviews": [review(observed_at_ms=NOW + DAY), review(account="b", observed_at_ms=NOW + DAY)]}, now_ms=NOW + DAY)
        self.assertEqual(same_uid["reason"], "uncertain_touch_requires_resolution")
        self.assertEqual(state.prepare_touch(self.conn, {"run_id": later_run, "user_id": "u2", "selected_account_id": "a", "search_complete": True, "discovered_account_ids": ["a"], "reviews": [review("u2", observed_at_ms=NOW + DAY)]}, now_ms=NOW + DAY)["action"], "send")
        self.assertEqual(self.conn.execute("SELECT state FROM daily_touches WHERE touch_id=?", (first["touch_id"],)).fetchone()[0], "uncertain")

    def test_reserved_touch_blocks_other_uid_and_can_be_listed_after_restart(self):
        self.candidate("u1")
        self.candidate("u2")
        run_id = self.run_id()
        first = self.prepare(run_id)
        self.assertEqual(self.prepare(run_id, "u2", reviews=[review("u2")])["reason"], "run_has_unfinished_send")
        pending = state.pending_touches(self.conn)["items"]
        self.assertEqual([(item["touch_id"], item["state"]) for item in pending], [(first["touch_id"], "reserved")])
        self.assertNotIn("message_snapshot", pending[0])
        self.assertTrue(state.queue(self.conn, run_id, now_ms=NOW)["blocked_by_unfinished_touch"])
        state.mark_clicked(self.conn, first["touch_id"], now_ms=NOW)
        self.conn.close()
        self.conn = state.connect(self.path)
        self.assertEqual(state.pending_touches(self.conn)["items"][0]["state"], "clicked")
        self.assertEqual(self.prepare(run_id, "u2")["reason"], "run_has_unfinished_send")
        self.assertEqual(state.queue(self.conn, run_id, now_ms=NOW)["actionable_items"], [])
        other_run = self.run_id()
        self.assertTrue(state.queue(self.conn, other_run, now_ms=NOW)["blocked_by_unfinished_touch"])
        self.assertEqual(self.prepare(other_run, "u2")["reason"], "run_has_unfinished_send")
        state.finish_touch(self.conn, {"touch_id": first["touch_id"], "status": "uncertain"}, now_ms=NOW)
        self.assertEqual([item["user_id"] for item in state.queue(self.conn, run_id, now_ms=NOW)["actionable_items"]], ["u2"])

    def test_failed_send_skips_uid_this_run_and_next_customer_can_send(self):
        self.candidate("u1")
        self.candidate("u1", "b")
        self.candidate("u2")
        run_id = self.run_id()
        first = self.prepare(run_id, reviews=[review(), review(account="b")])
        state.mark_clicked(self.conn, first["touch_id"], now_ms=NOW)
        state.finish_touch(self.conn, {"touch_id": first["touch_id"], "status": "failed", "reason": "recipient_privacy", "detail": "由于对方隐私设置，你无法发送消息"}, now_ms=NOW)
        replay = self.prepare(run_id, account="b", reviews=[review(), review(account="b")])
        self.assertEqual((replay["action"], replay["reason"]), ("skip", "failed_this_run"))
        self.assertEqual([item["user_id"] for item in state.queue(self.conn, run_id, now_ms=NOW)["actionable_items"]], ["u2"])
        second = self.prepare(run_id, "u2")
        self.assertEqual(second["action"], "send")
        state.mark_clicked(self.conn, second["touch_id"], now_ms=NOW)
        state.finish_touch(self.conn, {"touch_id": second["touch_id"], "status": "sent"}, now_ms=NOW)
        later_run = state.register_run(self.conn, "all", [], {"ad": "新版"}, ACCOUNTS, now_ms=NOW + DAY, require_fresh_import=False)["run_id"]
        later = state.prepare_touch(self.conn, {"run_id": later_run, "user_id": "u1", "selected_account_id": "b", "search_complete": True, "discovered_account_ids": ["a", "b"], "reviews": [review(observed_at_ms=NOW + DAY), review(account="b", observed_at_ms=NOW + DAY)]}, now_ms=NOW + DAY)
        self.assertEqual(later["action"], "send")

    def test_legacy_uncertain_isolated_and_old_global_defer_is_recovered(self):
        self.candidate("u1")
        self.candidate("u2")
        self.conn.execute("INSERT INTO legacy_send_events VALUES (?,?,?,?,?,?,?,?,?,?)", ("legacy-unknown", "u1", "b", "uncertain", NOW-DAY, "旧话术", state.digest("旧话术"), state.digest("旧话术"), "old", "旧账本无回执"))
        run_id = self.run_id()
        state.queue(self.conn, run_id, now_ms=NOW)
        self.conn.execute("UPDATE daily_run_items SET last_action='defer',last_reason='run_has_uncertain_send' WHERE run_id=? AND user_id='u2'", (run_id,))
        self.conn.commit()
        current = state.queue(self.conn, run_id, now_ms=NOW)
        self.assertEqual(current["quarantined_user_ids"], ["u1"])
        self.assertEqual([item["user_id"] for item in current["actionable_items"]], ["u2"])
        self.assertEqual(self.prepare(run_id)["reason"], "uncertain_touch_requires_resolution")
        self.assertEqual(self.prepare(run_id, "u2")["action"], "send")
        self.assertEqual(state.pending_touches(self.conn)["legacy_uncertain_items"][0]["user_id"], "u1")

    def test_report_keeps_failed_and_uncertain_counts_reasons_and_evidence(self):
        self.candidate("u1")
        self.candidate("u2")
        run_id = self.run_id()
        touches = []
        for uid, status, reason, detail in [("u1", "failed", "recipient_privacy", "隐私提示原文"), ("u2", "uncertain", "receipt_unconfirmed", "未查到稳定回执")]:
            touch = self.prepare(run_id, uid)
            touches.append(touch["touch_id"])
            state.mark_clicked(self.conn, touch["touch_id"], now_ms=NOW)
            state.finish_touch(self.conn, {"touch_id": touch["touch_id"], "status": status, "reason": reason, "detail": detail}, now_ms=NOW)
        result = state.report(self.conn, run_id, now_ms=NOW)
        self.assertEqual((result["failed_this_run"], result["uncertain_this_run"], result["sent_this_run"]), (1, 1, 0))
        self.assertEqual(result["send_failure_reasons"], {"recipient_privacy": 1})
        self.assertEqual(result["send_uncertain_reasons"], {"receipt_unconfirmed": 1})
        self.assertEqual(result["pending_send_states"], {})
        issues = {issue["user_id"]: issue for issue in result["send_issues"]}
        self.assertEqual((issues["u1"]["touch_id"], issues["u1"]["account_id"], issues["u1"]["detail"]), (touches[0], "a", "隐私提示原文"))
        self.assertEqual((issues["u2"]["state"], issues["u2"]["reason"]), ("uncertain", "receipt_unconfirmed"))

    def test_old_database_without_touch_reason_migrates_without_inventing_cause(self):
        self.candidate("u1")
        run_id = self.run_id()
        touch = self.prepare(run_id)
        state.mark_clicked(self.conn, touch["touch_id"], now_ms=NOW)
        state.finish_touch(self.conn, {"touch_id": touch["touch_id"], "status": "failed", "detail": "重新发送"}, now_ms=NOW)
        if "reason" in state._columns(self.conn, "daily_touches"):
            self.conn.execute("ALTER TABLE daily_touches DROP COLUMN reason")
        self.conn.commit()
        self.conn.close()
        self.conn = state.connect(self.path)
        result = state.report(self.conn, run_id, now_ms=NOW)
        self.assertEqual(result["failed_this_run"], 1)
        self.assertEqual(result["send_failure_reasons"], {"failed": 1})
        self.assertEqual(result["send_issues"][0]["detail"], "重新发送")
        self.assertIn("reason", state._columns(self.conn, "daily_touches"))

    def test_previous_calendar_day_run_cannot_prepare_or_click_new_send(self):
        self.candidate("u1")
        self.candidate("u2")
        old_run = self.run_id()
        old_day = state.prepare_touch(self.conn, {
            "run_id": old_run, "user_id": "u1", "selected_account_id": "a",
            "search_complete": True, "discovered_account_ids": ["a"], "reviews": [review(observed_at_ms=NOW + DAY, label="lead")],
        }, now_ms=NOW + DAY)
        self.assertEqual(old_day["reason"], "run_date_expired")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM lead_observations WHERE status='present'").fetchone()[0], 1)
        new_run = state.register_run(self.conn, "all", [], {"ad": "A"}, ACCOUNTS, now_ms=NOW, require_fresh_import=False)["run_id"]
        first = state.prepare_touch(self.conn, {"run_id": new_run,"user_id":"u2","selected_account_id":"a","search_complete":True,"discovered_account_ids":["a"],"reviews":[review("u2")]}, now_ms=NOW)
        self.assertEqual(first["action"], "send")
        with self.assertRaisesRegex(ValueError, "run_date_expired"):
            state.mark_clicked(self.conn, first["touch_id"], now_ms=NOW + DAY)

    def test_run_expires_at_shanghai_midnight_even_within_one_hour(self):
        self.candidate("u1")
        before = int(datetime(2026, 9, 29, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
        after = before + 2 * 60_000
        run_id = state.register_run(
            self.conn, "all", [], {"ad": "当日话术"}, ACCOUNTS,
            now_ms=before, require_fresh_import=False,
        )["run_id"]
        self.assertTrue(state.queue(self.conn, run_id, now_ms=after)["run_date_expired"])
        self.assertEqual(state.queue(self.conn, run_id, now_ms=after)["actionable_items"], [])
        outcome = state.prepare_touch(self.conn, {
            "run_id": run_id, "user_id": "u1", "selected_account_id": "a",
            "search_complete": True, "discovered_account_ids": ["a"], "reviews": [review(observed_at_ms=after, last_customer_at_ms=before-DAY,
                               last_outbound_text_at_ms=before-DAY)],
        }, now_ms=after)
        self.assertEqual(outcome["reason"], "run_date_expired")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM daily_touches").fetchone()[0], 0)

    def test_completed_send_is_not_actionable_or_repeatable_with_new_source_same_run(self):
        self.candidate("u1")
        run_id = self.run_id()
        self.assertEqual([item["user_id"] for item in state.queue(self.conn, run_id, now_ms=NOW)["actionable_items"]], ["u1"])
        first = self.prepare(run_id)
        state.mark_clicked(self.conn, first["touch_id"], now_ms=NOW)
        state.finish_touch(self.conn, {"touch_id": first["touch_id"], "status": "sent"}, now_ms=NOW)
        self.assertEqual(state.queue(self.conn, run_id, now_ms=NOW)["actionable_items"], [])
        natural = review(observed_at_ms=NOW + 1_000, source="natural", attribution_note_state="empty")
        result = state.prepare_touch(self.conn, {"run_id": run_id, "user_id": "u1", "selected_account_id": "a", "search_complete": True, "discovered_account_ids": ["a"], "reviews": [natural]}, now_ms=NOW + 1_000)
        self.assertEqual(result["reason"], "already_sent_this_run")

    def test_same_uid_can_send_each_verified_account_once_in_one_run(self):
        self.candidate("u1", "a")
        self.candidate("u1", "b")
        run_id = self.run_id(ad="同一句广告话术")
        both = [review(), review(account="b")]
        first = self.prepare(run_id, account="a", reviews=both)
        self.assertEqual(first["action"], "send")
        state.mark_clicked(self.conn, first["touch_id"], now_ms=NOW)
        state.finish_touch(self.conn, {"touch_id": first["touch_id"], "status": "sent"}, now_ms=NOW)
        queued = state.queue(self.conn, run_id, now_ms=NOW + 1_000)["actionable_items"]
        self.assertEqual(queued[0]["remaining_account_ids"], ["b"])
        later = [review(observed_at_ms=NOW + 1_000, last_outbound_text_at_ms=NOW),
                 review(account="b", observed_at_ms=NOW + 1_000)]
        second = state.prepare_touch(self.conn, {"run_id": run_id, "user_id": "u1",
            "selected_account_id": "b", "search_complete": True, "discovered_account_ids": ["a", "b"], "reviews": later}, now_ms=NOW + 1_000)
        self.assertEqual(second["action"], "send")
        state.mark_clicked(self.conn, second["touch_id"], now_ms=NOW + 1_000)
        state.finish_touch(self.conn, {"touch_id": second["touch_id"], "status": "sent"}, now_ms=NOW + 1_000)
        self.assertEqual(state.queue(self.conn, run_id, now_ms=NOW + 2_000)["actionable_items"], [])
        self.assertEqual(self.prepare(run_id, account="b", reviews=later)["reason"], "already_sent_this_run")

    def test_ui_discovered_account_is_added_to_current_run_scope(self):
        self.candidate("u1", "a")
        run_id = self.run_id()
        result = state.add_run_account(self.conn, {"run_id": run_id, "user_id": "u1",
            "account_id": "b", "uid_verified": True, "account_verified": True,
            "observed_at_ms": NOW}, now_ms=NOW)
        self.assertEqual(result["account_ids"], ["a", "b"])
        self.assertEqual(self.prepare(run_id, reviews=[review()])["reason"], "account_review_incomplete")

    def test_lead_seen_in_second_account_stops_remaining_account_queue(self):
        self.candidate("u1", "a")
        self.candidate("u1", "b")
        run_id = self.run_id()
        first = self.prepare(run_id, reviews=[review(), review(account="b")])
        state.mark_clicked(self.conn, first["touch_id"], now_ms=NOW)
        state.finish_touch(self.conn, {"touch_id": first["touch_id"], "status": "sent"}, now_ms=NOW)
        later = [review(observed_at_ms=NOW + 1_000), review(account="b", observed_at_ms=NOW + 1_000, label="lead")]
        blocked = state.prepare_touch(self.conn, {"run_id": run_id, "user_id": "u1",
            "selected_account_id": "b", "search_complete": True, "discovered_account_ids": ["a", "b"], "reviews": later}, now_ms=NOW + 1_000)
        self.assertEqual(blocked["reason"], "lead_tag")
        self.assertEqual(state.queue(self.conn, run_id, now_ms=NOW + 2_000)["actionable_items"], [])

    def test_second_account_browser_defer_after_first_send_gets_one_slow_retry(self):
        self.candidate("u1", "a")
        self.candidate("u1", "b")
        run_id = self.run_id()
        first = self.prepare(run_id, reviews=[review(), review(account="b")])
        state.mark_clicked(self.conn, first["touch_id"], now_ms=NOW)
        state.finish_touch(self.conn, {"touch_id": first["touch_id"], "status": "sent"}, now_ms=NOW)
        self.assertEqual(state.queue(self.conn, run_id, now_ms=NOW + 1)["actionable_items"][0]["retry_stage"], "main")
        state.record_browser_defer(self.conn, {"run_id": run_id, "user_id": "u1",
            "reason": "search_no_result", "retry_stage": "main", "observed_at_ms": NOW + 1}, now_ms=NOW + 1)
        self.assertEqual(state.queue(self.conn, run_id, now_ms=NOW + 2)["actionable_items"][0]["retry_stage"], "slow")
        state.record_browser_defer(self.conn, {"run_id": run_id, "user_id": "u1",
            "reason": "search_no_result", "retry_stage": "slow", "observed_at_ms": NOW + 2}, now_ms=NOW + 2)
        self.assertEqual(state.queue(self.conn, run_id, now_ms=NOW + 3)["actionable_items"], [])

    def test_browser_defer_gets_one_slow_retry_and_audit_record(self):
        self.candidate("u1")
        run_id = self.run_id()
        first = state.record_browser_defer(self.conn, {"run_id": run_id, "user_id": "u1", "reason": "uid_search_timeout", "retry_stage": "main", "observed_at_ms": NOW}, now_ms=NOW)
        self.assertEqual(first["action"], "defer")
        retry = state.queue(self.conn, run_id, now_ms=NOW)["actionable_items"]
        self.assertEqual([(item["user_id"], item["retry_stage"]) for item in retry], [("u1", "slow")])
        state.record_browser_defer(self.conn, {"run_id": run_id, "user_id": "u1", "reason": "uid_search_timeout", "retry_stage": "slow", "observed_at_ms": NOW + 1}, now_ms=NOW + 1)
        self.assertEqual(state.queue(self.conn, run_id, now_ms=NOW + 1)["actionable_items"], [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM daily_browser_deferrals").fetchone()[0], 2)
        self.assertEqual(state.report(self.conn, run_id, now_ms=NOW + 1)["browser_deferred"]["slow_retry_done"], 1)

    def test_state_defer_during_slow_retry_does_not_requeue_third_time(self):
        self.candidate("u1")
        run_id = self.run_id()
        state.record_browser_defer(self.conn, {"run_id": run_id, "user_id": "u1", "reason": "uid_search_timeout", "retry_stage": "main", "observed_at_ms": NOW}, now_ms=NOW)
        self.assertEqual(self.prepare(run_id, reviews=[review(panel_loaded=False, label="unknown")])["reason"], "panel_unknown")
        self.assertEqual(state.queue(self.conn, run_id, now_ms=NOW)["actionable_items"], [])

    def test_old_copy_and_missing_hashes_are_not_send_gates(self):
        self.candidate("u1")
        run_id = self.run_id()
        manual = review(outbound_text_hashes=[state.digest(state.canonical_copy("广告话术"))],
                        party_status="peer", reply_status="awaiting_us", last_outbound_text_at_ms=NOW)
        self.assertEqual(self.prepare(run_id, reviews=[manual])["action"], "send")

    def test_legacy_sent_reenters_observation_queue_and_old_lead_is_not_tag(self):
        self.conn.execute(
            "INSERT INTO legacy_send_events VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("f1", "old", "a", "sent", NOW - DAY, "旧话术", state.digest("旧话术"), state.digest("旧话术"), "old-c", ""),
        )
        self.conn.commit()
        run_id = self.run_id()
        queued = state.queue(self.conn, run_id, now_ms=NOW)["items"]
        self.assertEqual([(item["user_id"], item["observation_only"]) for item in queued], [("old", True)])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM lead_observations").fetchone()[0], 0)

    def test_latest_successful_sender_breaks_equal_account_inbound_tie(self):
        self.candidate("u1", "a")
        self.candidate("u1", "b")
        self.conn.execute(
            "INSERT INTO legacy_send_events VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("legacy-b", "u1", "b", "sent", NOW-2*DAY, "旧话术", state.digest("旧话术"), state.digest("旧话术"), "c", ""),
        )
        run_id = self.run_id()
        legacy_item = state.queue(self.conn, run_id, now_ms=NOW)["items"][0]
        self.assertEqual(legacy_item["last_sender_account_id"], "b")
        self.assertEqual(legacy_item["preferred_account_id"], "b")
        scoped_run = self.run_id(mode="specified", account_ids=["a"])
        scoped_item = state.queue(self.conn, scoped_run, now_ms=NOW)["items"][0]
        self.assertEqual(scoped_item["last_sender_account_id"], "")
        self.assertEqual(scoped_item["preferred_account_id"], "a")
        version_id = json.loads(self.conn.execute(
            "SELECT version_ids_json FROM daily_runs WHERE run_id=?", (run_id,),
        ).fetchone()[0])["ad"]
        self.conn.execute(
            """INSERT INTO daily_touches
               (touch_id,run_id,user_id,account_id,source,version_id,message_snapshot,
                normalized_hash,state,prepared_at_ms,finished_at_ms,pre_send_absent_at_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("daily-a", run_id, "u1", "a", "ad", version_id, "新话术", state.digest("新话术"),
             "sent", NOW-DAY, NOW-DAY, NOW-2*DAY),
        )
        next_run = self.run_id()
        latest_item = state.queue(self.conn, next_run, now_ms=NOW)["items"][0]
        self.assertEqual(latest_item["last_sender_account_id"], "a")
        self.assertEqual(latest_item["preferred_account_id"], "a")

    def test_legacy_result_recheck_excludes_confirmed_platform_tag(self):
        self.conn.execute(
            "INSERT INTO legacy_send_events VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("legacy-tagged", "tagged", "a", "sent", NOW-DAY, "旧话术", state.digest("旧话术"), state.digest("旧话术"), "c", ""),
        )
        self.conn.execute(
            "INSERT INTO followup_tracking (user_id,first_tag_observed_at_ms) VALUES (?,?)",
            ("tagged", NOW-1_000),
        )
        run_id = self.run_id()
        self.assertEqual(state.queue(self.conn, run_id, now_ms=NOW)["items"], [])

    def test_history_cache_requires_reaching_saved_boundary_and_merges_hashes(self):
        self.candidate("u1")
        first_run = self.run_id()
        first = self.prepare(first_run)
        self.assertEqual(first["action"], "send")
        state.mark_clicked(self.conn, first["touch_id"], now_ms=NOW)
        state.finish_touch(self.conn, {"touch_id": first["touch_id"], "status": "sent"}, now_ms=NOW)
        cached = self.conn.execute("SELECT boundary_message_id FROM conversation_history_cache WHERE user_id='u1' AND account_id='a'").fetchone()
        self.assertEqual(cached[0], "msg-latest")
        next_run = state.register_run(self.conn, "all", [], {"ad": "B"}, ACCOUNTS, now_ms=NOW + DAY, require_fresh_import=False)["run_id"]
        item = state.queue(self.conn, next_run, now_ms=NOW + DAY)["items"][0]
        self.assertEqual(item["history_cache"]["a"]["boundary_message_id"], "msg-latest")
        next_review = review(observed_at_ms=NOW + DAY, history_from_start_complete=False, history_reached_prior_boundary=False, history_boundary_message_id="msg-new")
        self.assertEqual(state.prepare_touch(self.conn, {"run_id": next_run,"user_id":"u1","selected_account_id":"a","search_complete":True,"discovered_account_ids":["a"],"reviews":[next_review]}, now_ms=NOW + DAY)["reason"], "history_boundary_unverified")
        next_review["history_reached_prior_boundary"] = True
        self.assertEqual(state.prepare_touch(self.conn, {"run_id": next_run,"user_id":"u1","selected_account_id":"a","search_complete":True,"discovered_account_ids":["a"],"reviews":[next_review]}, now_ms=NOW + DAY)["action"], "send")

    def test_a_then_b_then_tag_counts_one_customer_and_associates_b(self):
        self.candidate("u1")
        r1 = self.run_id(ad="A")
        t1 = self.prepare(r1)
        state.mark_clicked(self.conn, t1["touch_id"], now_ms=NOW)
        state.finish_touch(self.conn, {"touch_id": t1["touch_id"], "status": "sent"}, now_ms=NOW)
        r2 = state.register_run(self.conn, "all", [], {"ad": "B", "natural": "N"}, ACCOUNTS, now_ms=NOW + DAY, require_fresh_import=False)["run_id"]
        t2 = state.prepare_touch(self.conn, {"run_id": r2, "user_id": "u1", "selected_account_id": "a", "search_complete": True, "discovered_account_ids": ["a"], "reviews": [review(observed_at_ms=NOW + DAY)]}, now_ms=NOW + DAY)
        self.assertEqual(t2["action"], "send")
        state.mark_clicked(self.conn, t2["touch_id"], now_ms=NOW + DAY)
        state.finish_touch(self.conn, {"touch_id": t2["touch_id"], "status": "sent"}, now_ms=NOW + DAY)
        r3 = state.register_run(self.conn, "all", [], {"ad": "C", "natural": "N"}, ACCOUNTS, now_ms=NOW + 2 * DAY, require_fresh_import=False)["run_id"]
        observed = state.prepare_touch(self.conn, {"run_id": r3, "user_id": "u1", "selected_account_id": "a", "search_complete": True, "discovered_account_ids": ["a"], "reviews": [review(observed_at_ms=NOW + 2 * DAY, label="lead")]}, now_ms=NOW + 2 * DAY)
        self.assertEqual(observed["reason"], "lead_tag")
        report = state.report(self.conn, r3, now_ms=NOW + 2 * DAY)
        self.assertEqual(report["cumulative"]["converted"], 1)
        self.assertEqual(report["versions"][t1["version_id"]]["associated_tags"], 0)
        self.assertEqual(report["versions"][t2["version_id"]]["associated_tags"], 1)
        self.assertEqual(report["versions"][t1["version_id"]]["closed_windows"], 1)
        self.assertEqual(report["versions"][t2["version_id"]]["closed_windows"], 1)
        self.assertEqual(report["versions"][t2["version_id"]]["observing"], 0)

    def test_same_day_absent_then_present_observations_are_both_retained(self):
        self.candidate("u1")
        run_id = self.run_id()
        self.assertEqual(self.prepare(run_id, reviews=[review()])["action"], "send")
        self.assertEqual(self.prepare(run_id, reviews=[review(label="lead")])["reason"], "pending_touch_requires_resolution")
        statuses = [r[0] for r in self.conn.execute("SELECT status FROM lead_observations ORDER BY id")]
        self.assertEqual(statuses, ["absent", "present"])
        self.prepare(run_id, reviews=[review(label="lead")])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM lead_observations").fetchone()[0], 2)

    def test_manual_message_after_send_is_flagged_in_conversion_report(self):
        self.candidate("u1")
        run_id = self.run_id()
        touch = self.prepare(run_id)
        state.mark_clicked(self.conn, touch["touch_id"], now_ms=NOW)
        state.finish_touch(self.conn, {"touch_id": touch["touch_id"], "status": "sent"}, now_ms=NOW)
        next_run = state.register_run(self.conn, "all", [], {"ad": "B"}, ACCOUNTS, now_ms=NOW + DAY, require_fresh_import=False)["run_id"]
        state.prepare_touch(self.conn, {
            "run_id": next_run, "user_id": "u1", "selected_account_id": "a",
            "search_complete": True, "discovered_account_ids": ["a"], "reviews": [review(observed_at_ms=NOW + DAY, label="lead", last_outbound_text_at_ms=NOW + DAY // 2)],
        }, now_ms=NOW + DAY)
        report = state.report(self.conn, next_run, now_ms=NOW + DAY)
        self.assertEqual(report["cumulative"]["manual_intervention"], 1)
        self.assertEqual(report["versions"][touch["version_id"]]["manual_intervened_windows"], 1)

    def test_migration_dedupes_overlap_without_turning_old_lead_into_tag(self):
        source = Path(self.tmp.name) / "old.sqlite3"
        raw = sqlite3.connect(source)
        raw.executescript("CREATE TABLE send_attempts (id INTEGER PRIMARY KEY,campaign_id TEXT,conversation_key TEXT,user_id TEXT,account_id TEXT,status TEXT,detail TEXT,attempted_at_ms INTEGER,message_snapshot TEXT,message_hash_snapshot TEXT,batch_id TEXT); CREATE TABLE historical_user_accounts (user_id TEXT,account_name TEXT,account_id TEXT,display_name TEXT,latest_in_at_ms INTEGER,latest_open_at_ms INTEGER,latest_lead_at_ms INTEGER,phone_present INTEGER,wechat_present INTEGER,lead_status_from_import TEXT,deleted INTEGER,user_type TEXT,source_import_ids_json TEXT,updated_at_ms INTEGER); CREATE TABLE historical_imports (import_id TEXT,source_file TEXT,cutoff_date TEXT,imported_at_ms INTEGER,row_count INTEGER,unique_user_ids INTEGER,lead_users INTEGER,targetable_no_lead_users INTEGER,deleted_users INTEGER);")
        raw.execute("INSERT INTO send_attempts VALUES (1,'c','k','u1','a','sent','',?,'旧话术','','')", (NOW - DAY,))
        raw.execute("INSERT INTO historical_user_accounts VALUES ('u1','账号甲','a','',?,NULL,NULL,1,0,'has_lead',0,'','[\"i1\"]',?)", (NOW-DAY, NOW))
        raw.commit()
        raw.close()
        self.assertEqual(state.migrate_legacy(self.conn, source)["imported_attempts"], 1)
        self.assertEqual(state.migrate_legacy(self.conn, source)["imported_attempts"], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM lead_observations").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM legacy_send_events").fetchone()[0], 1)

    def test_migration_preserves_ignored_conflicting_raw_rows_with_source(self):
        self.conn.executescript("CREATE TABLE historical_users (user_id TEXT PRIMARY KEY, display_name TEXT); CREATE TABLE campaigns (campaign_id TEXT PRIMARY KEY, message TEXT);")
        self.conn.execute("INSERT INTO historical_users VALUES ('u1','原库甲')")
        self.conn.execute("INSERT INTO campaigns VALUES ('c1','文案甲')")
        self.conn.commit()
        source = Path(self.tmp.name) / "secondary.sqlite3"
        old = sqlite3.connect(source)
        old.executescript("CREATE TABLE historical_users (user_id TEXT PRIMARY KEY, display_name TEXT); CREATE TABLE campaigns (campaign_id TEXT PRIMARY KEY, message TEXT);")
        old.execute("INSERT INTO historical_users VALUES ('u1','原库乙')")
        old.execute("INSERT INTO campaigns VALUES ('c1','文案乙')")
        old.commit()
        old.close()
        summary = state.migrate_legacy(self.conn, source)
        self.assertEqual(summary["ignored_existing_or_conflicting_rows"]["historical_users"], 1)
        preserved = self.conn.execute("SELECT source_path,table_name,row_json FROM legacy_row_snapshots WHERE table_name IN ('historical_users','campaigns') ORDER BY table_name").fetchall()
        self.assertEqual(len(preserved), 2)
        self.assertTrue(all(Path(row["source_path"]).resolve() == source.resolve() for row in preserved))
        self.assertEqual({json.loads(row["row_json"])["display_name"] for row in preserved if row["table_name"] == "historical_users"}, {"原库乙"})
        self.assertEqual(state.migrate_legacy(self.conn, source)["snapshotted_rows_added"], 0)

    def test_conflicting_legacy_account_rows_keep_both_sources_and_protect_uid(self):
        self.candidate("u1", "a")
        source = Path(self.tmp.name) / "conflict.sqlite3"
        raw = sqlite3.connect(source)
        raw.execute("CREATE TABLE historical_user_accounts (user_id TEXT,account_name TEXT,account_id TEXT,latest_in_at_ms INTEGER,latest_open_at_ms INTEGER,latest_lead_at_ms INTEGER,phone_present INTEGER,wechat_present INTEGER,lead_status_from_import TEXT,deleted INTEGER,source_import_ids_json TEXT,updated_at_ms INTEGER)")
        raw.execute("INSERT INTO historical_user_accounts VALUES ('u1','账号甲','b',NULL,NULL,NULL,0,1,'has_lead',0,'[\"i2\"]',?)", (NOW,))
        raw.commit()
        raw.close()
        self.assertEqual(state.migrate_legacy(self.conn, source)["account_conflicts"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM legacy_account_snapshots WHERE user_id='u1'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT lead_status_from_import FROM historical_user_accounts WHERE user_id='u1'").fetchone()[0], "has_lead")
        run_id = self.run_id()
        self.assertEqual(self.prepare(run_id)["reason"], "legacy_lead_protection")

    def test_unmapped_old_account_for_same_uid_defers_send(self):
        self.candidate("u1", "a")
        self.conn.execute(
            """INSERT INTO historical_user_accounts
               (user_id,account_name,account_id,lead_status_from_import,source_import_ids_json,updated_at_ms)
               VALUES ('u1','未知昵称','','has_lead','[\"old\"]',?)""",
            (NOW,),
        )
        self.conn.commit()
        run_id = self.run_id()
        self.assertEqual(self.prepare(run_id)["reason"], "legacy_lead_protection")

    def test_known_other_account_outside_scope_defers_without_opening_it(self):
        self.candidate("u1", "a")
        self.candidate("u2", "a")
        self.conn.execute(
            """INSERT INTO historical_user_accounts
               (user_id,account_name,account_id,latest_in_at_ms,lead_status_from_import,source_import_ids_json,updated_at_ms)
               VALUES ('u1','账号乙','b',?,'no_lead','[\"old\"]',?)""",
            (NOW - 10 * DAY, NOW),
        )
        self.conn.execute(
            "INSERT INTO legacy_account_snapshots VALUES (?,?,?,?,?,?)",
            ("snapshot-u2", "old.sqlite3", "u2", "账号乙", "b", "{}"),
        )
        self.conn.commit()
        run_id = self.run_id(mode="specified", account_ids=["a"])
        self.assertEqual(self.prepare(run_id, "u1", reviews=[review("u1")])["reason"], "other_known_account_out_of_scope")
        self.assertEqual(self.prepare(run_id, "u2", reviews=[review("u2")])["reason"], "other_known_account_out_of_scope")

    def test_recent_legacy_send_query_uses_status_time_range_index(self):
        query = """SELECT user_id,account_id,MAX(attempted_at_ms) AS last_send
                   FROM legacy_send_events WHERE status='sent' AND attempted_at_ms>=?
                   GROUP BY user_id,account_id"""
        detail = " ".join(row[3] for row in self.conn.execute("EXPLAIN QUERY PLAN " + query, (NOW - 7 * DAY,)))
        self.assertIn("idx_legacy_send_status_time", detail)
        self.assertIn("attempted_at_ms>?", detail)


class SimplifiedWorkflowTest(unittest.TestCase):
    setUp = DailyStateTest.setUp
    tearDown = DailyStateTest.tearDown
    candidate = DailyStateTest.candidate
    prepare = DailyStateTest.prepare
    run_id = DailyStateTest.run_id
    def test_all_scope_accepts_an_unmapped_uid_and_live_account(self):
        self.candidate(account="")
        run_id = self.run_id()
        q = state.queue(self.conn, run_id, now_ms=NOW)
        self.assertEqual(q["actionable_items"][0]["account_ids"], [])
        result = state.prepare_touch(self.conn, {"run_id": run_id, "user_id": "u1",
            "selected_account_id": "new-live-account", "search_complete": True,
            "discovered_account_ids": ["new-live-account"],
            "reviews": [review(account="new-live-account")]}, now_ms=NOW)
        self.assertEqual(result["action"], "send")

    def test_multiple_imports_union_uids_and_exclude_lead_across_unknown_accounts(self):
        self.candidate("u1")
        self.conn.execute("INSERT INTO historical_imports VALUES ('i2','month.csv','2026-06-30',?,1,1,0,1,0)", (NOW,))
        self.conn.execute("INSERT INTO historical_user_accounts (user_id,account_name,account_id,lead_status_from_import,source_import_ids_json,updated_at_ms) VALUES ('u2','旧昵称','','no_lead',?,?)", (json.dumps(["i2"]), NOW))
        self.conn.execute("INSERT INTO historical_user_accounts (user_id,account_name,account_id,lead_status_from_import,source_import_ids_json,updated_at_ms) VALUES ('u1','另一旧昵称','','has_lead',?,?)", (json.dumps(["i2"]), NOW))
        run_id = state.register_run(self.conn, "all", [], {"ad": "A"}, [], now_ms=NOW, import_ids=["i1","i2"])["run_id"]
        self.assertEqual([x["user_id"] for x in state.queue(self.conn, run_id, now_ms=NOW)["actionable_items"]], ["u2"])

    def test_unknown_source_uses_shared_copy_without_claiming_natural(self):
        self.candidate()
        run_id = self.run_id(ad="同一原文", natural="同一原文")
        result = self.prepare(run_id, reviews=[review(source="unknown", attribution_note_state="unknown")])
        self.assertEqual((result["action"], result["source"], result["message"]), ("send", "unknown", "同一原文"))

    def test_unknown_source_defers_if_copies_differ(self):
        self.candidate()
        result = self.prepare(self.run_id(), reviews=[review(source="unknown", attribution_note_state="unknown")])
        self.assertEqual(result["reason"], "source_unknown")

    def test_new_reply_peer_and_recent_send_do_not_block(self):
        self.candidate()
        result = self.prepare(self.run_id(), reviews=[review(party_status="peer", reply_status="awaiting_us",
            last_customer_at_ms=None, last_outbound_text_at_ms=NOW)])
        self.assertEqual(result["action"], "send")

    def test_semantic_review_cannot_be_invented_by_keyword_parser(self):
        self.candidate()
        result = self.prepare(self.run_id(), reviews=[review(business_reviewed=False)])
        self.assertEqual(result["reason"], "contact_review_required")

    def test_refusal_persists_suppression_before_label_and_failed_label_stays_suppressed(self):
        self.candidate()
        run_id = self.run_id()
        result = self.prepare(run_id, reviews=[review(negative_status="blocked", stop_contact_evidence="别再联系我")])
        self.assertEqual((result["action"], result["reason"]), ("skip", "stop_contact"))
        self.assertEqual(result["label_updates"][0]["label"], "无意向")
        self.assertEqual(state.pending_touches(self.conn)["items"], [])
        failed = state.record_no_intent(self.conn, {"run_id": run_id, "user_id": "u1", "account_id": "a", "detail": "标签控件未加载"}, now_ms=NOW)
        self.assertEqual(failed["label_state"], "failed")
        self.assertEqual(self.prepare(self.run_id())["reason"], "stop_contact")
        self.assertEqual(state.report(self.conn, run_id, now_ms=NOW)["no_intent_updates"][0]["state"], "failed")

    def test_no_intent_label_requires_verified_readback(self):
        self.candidate()
        run_id = self.run_id()
        self.prepare(run_id, reviews=[review(negative_status="blocked", stop_contact_evidence="不要再发")])
        result = state.record_no_intent(self.conn, {"run_id": run_id,"user_id":"u1","account_id":"a",
            "uid_verified":True,"account_verified":True,"label":"no_intent","observed_at_ms":NOW}, now_ms=NOW)
        self.assertEqual(result["label_state"], "verified")

    def test_search_completeness_is_required_even_with_one_good_review(self):
        self.candidate()
        run_id = self.run_id()
        result = state.prepare_touch(self.conn, {"run_id":run_id,"user_id":"u1","selected_account_id":"a",
            "search_complete":False,"discovered_account_ids":["a"],"reviews":[review()]}, now_ms=NOW)
        self.assertEqual(result["reason"], "account_review_incomplete")


    def test_contact_evidence_remains_protective_when_history_is_cached_later(self):
        self.candidate()
        run_id = self.run_id()
        first = self.prepare(run_id, reviews=[review(protective_lead_signal=True, contact_evidence="客户已提供微信")])
        self.assertEqual(first["reason"], "protective_lead_signal")
        next_run = self.run_id()
        self.assertEqual(state.queue(self.conn, next_run, now_ms=NOW)["actionable_items"], [])
        self.assertEqual(self.prepare(next_run)["reason"], "protective_lead_signal")

    def test_old_peer_skip_reenters_queue_after_policy_change(self):
        self.candidate()
        run_id = self.run_id()
        state.queue(self.conn, run_id, now_ms=NOW)
        self.conn.execute("UPDATE daily_run_items SET last_action='skip',last_reason='peer_or_seller' WHERE run_id=?", (run_id,))
        self.conn.commit()
        self.assertEqual(state.queue(self.conn, run_id, now_ms=NOW)["actionable_items"][0]["retry_stage"], "main")

    def test_execution_session_survives_restart_without_resetting_baseline(self):
        for uid in ("u1", "u2", "u3", "u4"):
            self.candidate(uid)
        run_id = self.run_id()
        def sent(uid):
            touch = self.prepare(run_id, uid)
            state.mark_clicked(self.conn, touch["touch_id"], now_ms=NOW)
            state.finish_touch(self.conn, {"touch_id": touch["touch_id"], "status": "sent"}, now_ms=NOW)
        sent("u1")
        first = state.begin_session(self.conn, run_id, "user-turn-1", now_ms=NOW)
        self.assertEqual(first["baseline"]["sent"], 1)
        sent("u2")
        self.conn.close()
        self.conn = state.connect(self.path)
        resumed = state.begin_session(self.conn, run_id, "user-turn-1", now_ms=NOW + 1000)
        self.assertEqual(resumed["baseline"]["sent"], 1)
        self.assertEqual(resumed["delta"]["sent"], 1)
        with state.connect_readonly(self.path) as ro:
            self.assertEqual(state.session_status(ro, run_id=run_id, now_ms=NOW + 1000)["session_id"], "user-turn-1")
        with self.assertRaisesRegex(ValueError, "active_session_exists"):
            state.begin_session(self.conn, run_id, "mistaken-new-segment", now_ms=NOW + 1000)
        sent("u3")
        closed = state.end_session(self.conn, "user-turn-1", "用户暂停", now_ms=NOW + 2000)
        self.assertEqual(closed["delta"]["sent"], 2)
        sent("u4")
        with state.connect_readonly(self.path) as ro:
            frozen = state.session_status(ro, "user-turn-1", now_ms=NOW + 3000)
            self.assertEqual(frozen["delta"]["sent"], 2)
            self.assertEqual(frozen["end_counts"]["sent"], 3)
            self.assertEqual(frozen["run_current"]["sent"], 4)
            self.assertEqual(frozen["elapsed_wall_ms"], 2000)
        self.assertEqual(state.begin_session(self.conn, run_id, "user-turn-1", now_ms=NOW + 4000)["state"], "closed")
        self.assertEqual(state.begin_session(self.conn, run_id, "user-turn-2", now_ms=NOW + 4000)["baseline"]["sent"], 4)

    def test_compact_next_is_read_only_and_matches_queue_order_and_guards(self):
        for uid in ("u1", "u2", "u3"):
            self.candidate(uid)
        run_id = self.run_id()
        state.queue(self.conn, run_id, now_ms=NOW)
        state.record_browser_defer(self.conn, {"run_id": run_id, "user_id": "u1",
            "reason": "search_result_not_confirmed", "retry_stage": "main"}, now_ms=NOW)
        self.conn.execute("UPDATE daily_run_items SET last_action='skip',last_reason='peer_or_seller' WHERE run_id=? AND user_id='u2'", (run_id,))
        self.conn.commit()
        with state.connect_readonly(self.path) as ro:
            trace = []
            ro.set_trace_callback(trace.append)
            compact = state.next_items(ro, run_id, limit=2, now_ms=NOW)
            self.assertEqual([x["user_id"] for x in compact["items"]], ["u2", "u3"])
            self.assertEqual(compact["actionable_count"], 3)
            self.assertEqual(compact["retry_counts"], {"main": 2, "slow": 1})
            self.assertNotIn("actionable_items", compact)
            self.assertFalse(any(s.lstrip().split()[0].upper() in {"UPDATE", "INSERT", "CREATE", "DELETE", "ALTER"} for s in trace))
        self.assertEqual(self.conn.execute("SELECT last_action FROM daily_run_items WHERE run_id=? AND user_id='u2'", (run_id,)).fetchone()[0], "skip")
        full = state.queue(self.conn, run_id, now_ms=NOW)
        self.assertEqual(compact["items"], full["actionable_items"][:2])
        self.prepare(run_id, "u2")
        with state.connect_readonly(self.path) as ro:
            blocked = state.next_items(ro, run_id, now_ms=NOW)
            self.assertTrue(blocked["blocked_by_unfinished_touch"])
            self.assertEqual(blocked["items"], [])
        for limit in (0, 101, True):
            with self.assertRaisesRegex(ValueError, "invalid_next_limit"):
                state.next_items(self.conn, run_id, limit=limit, now_ms=NOW)

    def test_next_excludes_waiting_uids_before_limit_without_database_writes(self):
        for index in range(205):
            self.candidate(f"u{index:03d}", latest_in_at_ms=NOW - DAY - index)
        run_id = self.run_id()
        state.queue(self.conn, run_id, now_ms=NOW)
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = self.path.read_bytes()
        excluded = [f"u{index:03d}" for index in range(101)]
        # Large local queues must not depend on SQLite's SQL parameter limit.
        excluded += [f"absent-{index}" for index in range(35_000)] + ["u000"]
        with state.connect_readonly(self.path) as ro:
            result = state.next_items(ro, run_id, limit=100, now_ms=NOW,
                                      exclude_user_ids=excluded)
        self.assertEqual(len(result["items"]), 100)
        self.assertEqual(result["items"][0]["user_id"], "u101")
        self.assertEqual(result["items"][-1]["user_id"], "u200")
        self.assertEqual(result["actionable_count"], 205)
        self.assertEqual(self.path.read_bytes(), before)

    def test_next_exclusions_preserve_main_then_slow_order_and_global_pending_guard(self):
        for uid in ("u1", "u2", "u3", "u4"):
            self.candidate(uid)
        run_id = self.run_id()
        state.record_browser_defer(self.conn, {"run_id": run_id, "user_id": "u1",
            "reason": "search_result_not_confirmed", "retry_stage": "main"}, now_ms=NOW)
        with state.connect_readonly(self.path) as ro:
            result = state.next_items(ro, run_id, limit=3, now_ms=NOW,
                                      exclude_user_ids=["u2", "u2"])
        self.assertEqual([i["user_id"] for i in result["items"]], ["u3", "u4", "u1"])
        self.assertEqual([i["retry_stage"] for i in result["items"]], ["main", "main", "slow"])
        self.assertEqual(result["retry_counts"], {"main": 3, "slow": 1})
        self.prepare(run_id, "u2")
        another_run = self.run_id(ad="另一轮")
        state.queue(self.conn, another_run, now_ms=NOW)
        with state.connect_readonly(self.path) as ro:
            blocked = state.next_items(ro, another_run, limit=100, now_ms=NOW,
                                       exclude_user_ids=["u2"])
        self.assertTrue(blocked["blocked_by_unfinished_touch"])
        self.assertEqual(blocked["items"], [])

    def test_next_rejects_malformed_exclusion_lists(self):
        self.candidate()
        run_id = self.run_id()
        for value in (None, "u1", {}, [""], ["  "], [1], [True], ["u1", None]):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "invalid_exclude_user_ids"):
                state.next_items(self.conn, run_id, now_ms=NOW, exclude_user_ids=value)

    def test_next_cli_reads_exclusions_from_stdin_without_mutating_database(self):
        now = state.epoch_ms()
        for index in range(102):
            self.candidate(f"u{index:03d}", latest_in_at_ms=now - DAY - index)
        run_id = state.register_run(self.conn, "all", [], {"ad": "A", "natural": "A"}, ACCOUNTS,
                                    now_ms=now, require_fresh_import=False)["run_id"]
        state.queue(self.conn, run_id, now_ms=now)
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = self.path.read_bytes()
        command = [sys.executable, str(Path(state.__file__)), "--db", str(self.path),
                   "next", "--run-id", run_id, "--limit", "1", "--input-json", "-"]
        result = subprocess.run(command, input=json.dumps({"exclude_user_ids": [f"u{i:03d}" for i in range(101)]}),
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([i["user_id"] for i in json.loads(result.stdout)["items"]], ["u101"])
        self.assertEqual(self.path.read_bytes(), before)
        for payload in ([], {"exclude_user_ids": None}, {"exclude_user_ids": [""]}):
            with self.subTest(payload=payload):
                invalid = subprocess.run(command, input=json.dumps(payload), text=True, capture_output=True)
                self.assertEqual(invalid.returncode, 2)
                self.assertEqual(json.loads(invalid.stdout)["error"], "invalid_exclude_user_ids")
        self.assertEqual(self.path.read_bytes(), before)

    def test_reopening_incorrect_browser_defer_preserves_audit_and_requires_recheck(self):
        self.candidate()
        run_id = self.run_id()
        state.queue(self.conn, run_id, now_ms=NOW)
        state.record_browser_defer(self.conn, {"run_id": run_id, "user_id": "u1",
            "reason": "history_boundary_unverified", "retry_stage": "main"}, now_ms=NOW)
        payload = {"correction_id": "correction-1", "run_id": run_id, "user_id": "u1",
            "expected_reason": "history_boundary_unverified", "expected_checked_at_ms": NOW,
            "evidence": "Prior-boundary history was complete; operator misread from-start=false."}
        with state.connect_readonly(self.path) as ro:
            preview = state.reopen_browser_defer(ro, payload, now_ms=NOW + 1)
        self.assertEqual(preview["action"], "would_reopen")
        self.assertEqual(self.conn.execute("SELECT last_action FROM daily_run_items WHERE run_id=?", (run_id,)).fetchone()[0], "defer")
        with self.assertRaisesRegex(ValueError, "correction_state_changed"):
            state.reopen_browser_defer(self.conn, {**payload, "expected_checked_at_ms": NOW - 1}, apply=True, now_ms=NOW + 1)
        result = state.reopen_browser_defer(self.conn, payload, apply=True, now_ms=NOW + 1)
        self.assertEqual(result["action"], "reopened_requires_review")
        self.assertEqual(state.next_items(self.conn, run_id, now_ms=NOW + 1)["items"][0]["retry_stage"], "main")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM daily_browser_deferrals").fetchone()[0], 1)
        audit = self.conn.execute("SELECT * FROM daily_item_corrections").fetchone()
        self.assertEqual(json.loads(audit["before_json"])["last_reason"], "history_boundary_unverified")
        self.assertEqual(json.loads(audit["after_json"])["last_action"], "pending")
        self.assertEqual(state.pending_touches(self.conn)["items"], [])
        self.assertTrue(state.reopen_browser_defer(self.conn, payload, apply=True, now_ms=NOW + 2)["idempotent"])
        with self.assertRaisesRegex(ValueError, "correction_id_conflict"):
            state.reopen_browser_defer(self.conn, {**payload, "evidence": "different"}, apply=True, now_ms=NOW + 2)

    def test_browser_defer_correction_cannot_remove_contact_protection(self):
        self.candidate()
        run_id = self.run_id()
        state.queue(self.conn, run_id, now_ms=NOW)
        state.record_browser_defer(self.conn, {"run_id": run_id, "user_id": "u1",
            "reason": "history_boundary_unverified", "retry_stage": "main"}, now_ms=NOW)
        self.conn.execute("INSERT INTO contact_protections VALUES ('u1','a',?,'已加')", (NOW,))
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "correction_uid_protected"):
            state.reopen_browser_defer(self.conn, {"correction_id": "bad", "run_id": run_id,
                "user_id": "u1", "expected_reason": "history_boundary_unverified",
                "expected_checked_at_ms": NOW, "evidence": "cannot override protection"}, apply=True, now_ms=NOW + 1)

    def test_read_only_cli_never_initializes_missing_database(self):
        missing = Path(self.tmp.name) / "missing.sqlite3"
        result = subprocess.run([sys.executable, str(Path(state.__file__)), "--db", str(missing),
                                 "next", "--run-id", "none"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(missing.exists())

    def test_read_only_cli_does_not_run_schema_migrations(self):
        self.candidate()
        run_id = self.run_id()
        state.begin_session(self.conn, run_id, "read-only-check", now_ms=NOW)
        self.conn.execute("DROP TABLE daily_item_corrections")
        self.conn.commit()
        schema = list(self.conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name"))
        commands = (["next", "--run-id", run_id, "--limit", "1"],
                    ["report", "--run-id", run_id], ["pending-touches", "--run-id", run_id],
                    ["session-status", "--session-id", "read-only-check"],
                    ["session-status", "--run-id", run_id])
        for command in commands:
            with self.subTest(command=command[0]):
                result = subprocess.run([sys.executable, str(Path(state.__file__)), "--db", str(self.path), *command],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(json.loads(result.stdout)["ok"])
                self.assertEqual(list(self.conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name")), schema)

    def test_browser_defer_correction_cannot_reopen_uid_with_send_outcome(self):
        self.candidate(account="a")
        self.candidate(account="b")
        run_id = self.run_id()
        touch = self.prepare(run_id, reviews=[review(), review(account="b")])
        state.mark_clicked(self.conn, touch["touch_id"], now_ms=NOW)
        state.finish_touch(self.conn, {"touch_id": touch["touch_id"], "status": "sent"}, now_ms=NOW)
        state.record_browser_defer(self.conn, {"run_id": run_id, "user_id": "u1",
            "reason": "history_boundary_unverified", "retry_stage": "main"}, now_ms=NOW + 1)
        with self.assertRaisesRegex(ValueError, "correction_uid_has_touch"):
            state.reopen_browser_defer(self.conn, {"correction_id": "has-send", "run_id": run_id,
                "user_id": "u1", "expected_reason": "history_boundary_unverified",
                "expected_checked_at_ms": NOW + 1, "evidence": "must preserve existing send result"}, apply=True, now_ms=NOW + 2)


class RunPolicyAndClosureTest(unittest.TestCase):
    setUp = DailyStateTest.setUp
    tearDown = DailyStateTest.tearDown
    candidate = DailyStateTest.candidate
    prepare = DailyStateTest.prepare
    run_id = DailyStateTest.run_id

    def new_run(self):
        return state.register_run(self.conn, "all", [], {"ad": "A", "natural": "A"},
                                  ACCOUNTS, now_ms=NOW, require_fresh_import=False)

    def finish(self, touch, outcome="sent", at=NOW):
        state.mark_clicked(self.conn, touch["touch_id"], now_ms=at)
        state.finish_touch(self.conn, {"touch_id": touch["touch_id"], "status": outcome}, now_ms=at)

    def test_new_run_uid_once_and_legacy_explicit_account_once(self):
        self.candidate(account="a")
        self.candidate(account="b")
        registered = self.new_run()
        rid = registered["run_id"]
        self.assertEqual(registered["touch_policy"], "uid_once")
        first = self.prepare(rid, reviews=[review(), review(account="b")])
        self.finish(first)
        self.assertEqual(state.queue(self.conn, rid, now_ms=NOW)["actionable_items"], [])
        other = self.prepare(rid, account="b", reviews=[review(), review(account="b")])
        self.assertEqual(other["reason"], "already_sent_this_run")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM daily_touches WHERE run_id=?", (rid,)).fetchone()[0], 1)

    def test_uid_once_blocks_other_account_for_every_attempt_state(self):
        self.candidate(account="a")
        self.candidate(account="b")
        for outcome in ("reserved", "clicked", "sent", "failed", "uncertain"):
            with self.subTest(outcome=outcome):
                rid = self.new_run()["run_id"]
                first = self.prepare(rid, reviews=[review(), review(account="b")])
                if outcome != "reserved":
                    state.mark_clicked(self.conn, first["touch_id"], now_ms=NOW)
                if outcome in {"sent", "failed", "uncertain"}:
                    state.finish_touch(self.conn, {"touch_id": first["touch_id"], "status": outcome}, now_ms=NOW)
                blocked = self.prepare(rid, account="b", reviews=[review(), review(account="b")])
                self.assertNotEqual(blocked["action"], "send")
                self.assertEqual(state.next_items(self.conn, rid, now_ms=NOW)["items"], [])
                if outcome in {"reserved", "clicked", "uncertain"}:
                    state.resolve_touch(self.conn, first["touch_id"], "confirmed_not_sent", "test cleanup", now_ms=NOW)

    def test_old_schema_migration_keeps_account_once_and_touch_history(self):
        old_path = Path(self.tmp.name) / "old.sqlite3"
        old = sqlite3.connect(old_path)
        old.executescript("CREATE TABLE daily_runs (run_id TEXT PRIMARY KEY,started_at_ms INTEGER NOT NULL,scope_mode TEXT NOT NULL,account_ids_json TEXT NOT NULL,messages_json TEXT NOT NULL,version_ids_json TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'active'); INSERT INTO daily_runs VALUES ('old',1800000000000,'all','[]','{}','{}','active');")
        old.close()
        migrated = state.connect(old_path)
        self.assertEqual(state._run(migrated, "old")["touch_policy"], "account_once")
        migrated.close()

    def test_existing_run_policy_change_is_audited_and_open_attempt_blocks_it(self):
        self.candidate(account="a")
        self.candidate(account="b")
        rid = self.run_id()
        touch = self.prepare(rid, reviews=[review(), review(account="b")])
        with self.assertRaisesRegex(ValueError, "unfinished_touch"):
            state.set_touch_policy(self.conn, rid, "uid_once", "用户采用最佳实践", now_ms=NOW)
        self.finish(touch)
        changed = state.set_touch_policy(self.conn, rid, "uid_once", "用户采用最佳实践", now_ms=NOW + 1)
        self.assertEqual(changed["touch_policy"], "uid_once")
        self.assertEqual(state.queue(self.conn, rid, now_ms=NOW)["actionable_items"], [])
        audit = self.conn.execute("SELECT * FROM daily_run_events WHERE run_id=? AND event='touch_policy_changed'", (rid,)).fetchone()
        self.assertEqual(json.loads(audit["before_json"])["touch_policy"], "account_once")
        self.assertIn("用户", audit["evidence"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM daily_touches WHERE run_id=?", (rid,)).fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "evidence_required"):
            state.set_touch_policy(self.conn, rid, "account_once", "", now_ms=NOW)

    def test_full_status_keeps_exhausted_deferred_protection_and_pending_label(self):
        for uid in ("u1", "u2", "u3", "u4"):
            self.candidate(uid)
        rid = self.new_run()["run_id"]
        state.queue(self.conn, rid, now_ms=NOW)
        for stage in ("main", "slow"):
            state.record_browser_defer(self.conn, {"run_id": rid, "user_id": "u1", "reason": "search_result_not_confirmed", "retry_stage": stage}, now_ms=NOW)
        state.record_browser_defer(self.conn, {"run_id": rid, "user_id": "u2", "reason": "conversation_identity_not_ready", "retry_stage": "main"}, now_ms=NOW)
        self.prepare(rid, "u3", reviews=[review("u3", protective_lead_signal=True)])
        self.prepare(rid, "u4", reviews=[review("u4", negative_status="blocked", stop_contact_evidence="不要发了")])
        compact = state.run_status(self.conn, rid, now_ms=NOW)
        self.assertNotIn("items", compact)
        self.assertEqual(compact["counts"]["uncompleted_customers"], 2)
        self.assertEqual(compact["counts"]["retry_exhausted_customers"], 1)
        self.assertEqual(compact["counts"]["protected_customers"], 2)
        self.assertEqual(compact["counts"]["pending_labels"], 1)
        full = state.run_status(self.conn, rid, include_items=True, now_ms=NOW)
        self.assertEqual({row["user_id"] for row in full["items"]}, {"u1", "u2"})
        self.assertEqual(full["uncompleted"]["category_counts"], {"search": 1, "identity": 1})

    def test_safe_closure_pause_resume_and_exceptions_survive_restart(self):
        self.candidate()
        rid = self.new_run()["run_id"]
        state.queue(self.conn, rid, now_ms=NOW)
        with self.assertRaisesRegex(ValueError, "pending_items"):
            state.end_run(self.conn, rid, "不可漏掉未执行名单", now_ms=NOW)
        self.assertEqual(state.pause_run(self.conn, rid, "用户暂停", now_ms=NOW)["run_state"], "paused")
        self.assertEqual(state.next_items(self.conn, rid, now_ms=NOW)["items"], [])
        self.assertEqual(self.prepare(rid)["reason"], "run_not_active")
        state.resume_run(self.conn, rid, "用户恢复", now_ms=NOW)
        state.record_browser_defer(self.conn, {"run_id": rid, "user_id": "u1", "reason": "history_load_stalled", "retry_stage": "main"}, now_ms=NOW)
        closed = state.end_run(self.conn, rid, "异常交人工", now_ms=NOW)
        self.assertEqual(closed["run_state"], "completed_with_exceptions")
        self.assertEqual(closed["counts"]["uncompleted_customers"], 1)
        self.conn.close()
        self.conn = state.connect(self.path)
        self.assertEqual(state.next_items(self.conn, rid, now_ms=NOW)["items"], [])
        state.resume_run(self.conn, rid, "继续核查异常", now_ms=NOW)
        self.assertEqual(state.next_items(self.conn, rid, now_ms=NOW)["items"][0]["retry_stage"], "slow")

    def test_open_touch_forbids_pause_and_close_but_uncertain_can_finish_with_exceptions(self):
        self.candidate()
        rid = self.new_run()["run_id"]
        touch = self.prepare(rid)
        for operation in (state.end_run, state.pause_run):
            with self.assertRaisesRegex(ValueError, "unfinished_touch"):
                operation(self.conn, rid, "先核销", now_ms=NOW)
        self.finish(touch, "uncertain")
        ended = state.end_run(self.conn, rid, "待人工核查点击", now_ms=NOW)
        self.assertEqual(ended["run_state"], "completed_with_exceptions")
        self.assertEqual(ended["counts"]["uncertain_touches"], 1)

    def test_session_metrics_distinguish_repeat_customer_from_cumulative_new_customer(self):
        self.candidate(account="a")
        self.candidate(account="b")
        rid = self.run_id()
        self.finish(self.prepare(rid, reviews=[review(), review(account="b")]))
        state.begin_session(self.conn, rid, "session-two", now_ms=NOW + 1)
        self.finish(self.prepare(rid, account="b", reviews=[review(), review(account="b")]), at=NOW + 2)
        current = state.session_status(self.conn, "session-two", now_ms=NOW + 3)
        self.assertEqual(current["delta"]["sent_customers"], 0)
        self.assertEqual(current["session_metrics"]["sent_customers"], 1)
        self.assertEqual(current["session_metrics"]["sent_messages"], 1)
        self.assertEqual(current["run_current"]["sent_customers"], 1)
        state.end_session(self.conn, "session-two", now_ms=NOW + 4)
        frozen = state.session_status(self.conn, "session-two", now_ms=NOW + 5)
        self.assertEqual(frozen["session_metrics"]["sent_customers"], 1)
        self.assertEqual(frozen["session_metrics"]["elapsed_wall_ms"], 3)

    def test_structured_browser_and_send_evidence_is_not_truncated_after_serialization(self):
        self.candidate("u1")
        self.candidate("u2")
        rid = self.new_run()["run_id"]
        structured = json.dumps({"diagnostics": {"right_uid": "u1", "visible_accounts": ["a"], "text": "证据" * 400}}, ensure_ascii=False)
        state.queue(self.conn, rid, now_ms=NOW)
        state.record_browser_defer(self.conn, {"run_id": rid, "user_id": "u1", "reason": "conversation_identity_not_ready", "retry_stage": "main", "evidence": structured}, now_ms=NOW)
        self.assertEqual(json.loads(self.conn.execute("SELECT evidence FROM daily_browser_deferrals WHERE run_id=?", (rid,)).fetchone()[0]), json.loads(structured))
        touch = self.prepare(rid, "u2", reviews=[review("u2")])
        state.mark_clicked(self.conn, touch["touch_id"], now_ms=NOW)
        detail = json.dumps({"reason": "send_rejected", "platform_notices": [{"text": "原弹窗" * 300, "severity": "error"}]}, ensure_ascii=False)
        state.finish_touch(self.conn, {"touch_id": touch["touch_id"], "status": "failed", "detail": detail}, now_ms=NOW)
        self.assertEqual(json.loads(self.conn.execute("SELECT detail FROM daily_touches WHERE touch_id=?", (touch["touch_id"],)).fetchone()[0]), json.loads(detail))

    def test_terminal_send_failures_are_processed_but_reported_as_exceptions(self):
        self.candidate()
        rid = self.new_run()["run_id"]
        self.finish(self.prepare(rid), "failed")
        ended = state.end_run(self.conn, rid, "平台拒绝后跳过", now_ms=NOW)
        self.assertEqual(ended["run_state"], "completed_with_exceptions")
        self.assertEqual(ended["counts"]["uncompleted_customers"], 0)
        self.assertEqual(ended["counts"]["failed_customers"], 1)
        self.assertEqual(sum(ended["customer_dispositions"].values()), 1)

    def test_end_run_freezes_active_session_and_paused_or_blocked_cannot_be_finished(self):
        self.candidate()
        rid = self.new_run()["run_id"]
        state.begin_session(self.conn, rid, "session", now_ms=NOW)
        touch = self.prepare(rid)
        with self.assertRaisesRegex(ValueError, "unfinished_touch"):
            state.end_session(self.conn, "session", now_ms=NOW)
        self.finish(touch)
        for stopped in ("paused", "blocked"):
            state.pause_run(self.conn, rid, "用户暂停或页面失效", state=stopped, now_ms=NOW)
            with self.assertRaisesRegex(ValueError, "requires_resume"):
                state.end_run(self.conn, rid, "不能冒充完成", now_ms=NOW)
            state.resume_run(self.conn, rid, "恢复", now_ms=NOW)
        self.assertEqual(state.end_run(self.conn, rid, "正常处理结束", now_ms=NOW + 2)["run_state"], "completed")
        session = state.session_status(self.conn, "session", now_ms=NOW + 10)
        self.assertEqual(session["state"], "closed")
        self.assertEqual(session["elapsed_wall_ms"], 2)
        self.assertEqual(session["session_metrics"]["sent_customers"], 1)

    def test_run_status_cli_is_readonly_and_full_exception_list_is_opt_in(self):
        self.candidate()
        rid = self.new_run()["run_id"]
        state.queue(self.conn, rid, now_ms=NOW)
        before = self.path.stat().st_mtime_ns
        command = [sys.executable, str(Path(state.__file__)), "--db", str(self.path), "run-status", "--run-id", rid]
        compact = json.loads(subprocess.check_output(command, text=True))
        full = json.loads(subprocess.check_output([*command, "--include-items"], text=True))
        self.assertNotIn("items", compact)
        self.assertEqual(full["items"][0]["user_id"], "u1")
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_historical_quarantine_and_user_confirmed_no_intent_have_separate_counts(self):
        self.candidate("old-uncertain")
        self.candidate("ignored")
        self.candidate("new-uncertain")
        rid = self.new_run()["run_id"]
        state.queue(self.conn, rid, now_ms=NOW)
        with self.conn:
            self.conn.execute("INSERT INTO legacy_send_events VALUES ('legacy','old-uncertain','a','uncertain',?,'旧话术','','','old','historical only')", (NOW - DAY,))
            self.conn.execute("INSERT INTO no_intent_updates VALUES (?, 'ignored','a','用户明确忽略','user_confirmed',?,'不需要补平台标签')", (rid, NOW))
        self.finish(self.prepare(rid, "new-uncertain", reviews=[review("new-uncertain")]), "uncertain")
        full = state.run_status(self.conn, rid, include_items=True, now_ms=NOW)
        self.assertEqual(full["counts"]["uncompleted_customers"], 1)
        self.assertEqual(full["counts"]["quarantined_customers"], 1)
        self.assertEqual(full["counts"]["unresolved_customers_total"], 2)
        self.assertEqual(full["counts"]["completed_customers"], 1)
        self.assertEqual(full["counts"]["pending_labels"], 0)
        self.assertEqual(full["counts"]["local_no_intent_decisions"], 1)
        self.assertEqual(full["quarantined_items"][0]["user_id"], "old-uncertain")
        self.assertEqual(sum(full["customer_dispositions"].values()), 3)

    def test_registration_initializes_next_and_startup_without_query_writes(self):
        self.candidate()
        raw = "  原文\n第二行  "
        registered = state.register_run(self.conn, "specified", ["a"], {"ad": raw},
                                        ACCOUNTS, now_ms=NOW, import_ids=["i1"])
        self.assertTrue(registered.get("initialized", False))
        self.assertEqual(registered["queue_count"], 1)
        rid = registered["run_id"]
        before = self.conn.total_changes
        next_uid = state.next_items(self.conn, rid, now_ms=NOW)["items"][0]["user_id"]
        self.assertEqual(next_uid, "u1")
        startup = state.run_status(self.conn, rid, now_ms=NOW)["startup"]
        self.assertEqual(startup["version"], (state.SKILL_DIR / "VERSION").read_text().strip())
        self.assertEqual(startup["scope_mode"], "specified")
        self.assertEqual(startup["account_ids"], ["a"])
        self.assertEqual(startup["source_import_ids"], ["i1"])
        self.assertEqual(startup["imports"], [{"import_id": "i1", "source_file": "test.csv", "unique_user_ids": 1}])
        self.assertEqual(startup["messages"]["ad"], raw)
        self.assertEqual(startup["touch_policy"], "uid_once")
        self.assertEqual(startup["run_state"], "active")
        self.assertTrue(startup["initialized"])
        self.assertEqual(startup["counts"]["total_customers"], 1)
        state.report(self.conn, rid, now_ms=NOW)
        self.assertEqual(self.conn.total_changes, before)
        state.queue(self.conn, rid, now_ms=NOW)
        state.queue(self.conn, rid, now_ms=NOW)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM daily_run_items WHERE run_id=?", (rid,)).fetchone()[0], 1)

    def test_registration_queue_failure_rolls_back_versions_run_and_items(self):
        self.candidate()
        self.conn.execute("""CREATE TRIGGER reject_queue_version BEFORE UPDATE OF queue_policy_version
            ON daily_runs BEGIN SELECT RAISE(ABORT, 'queue initialization failed'); END""")
        self.conn.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "queue initialization failed"):
            self.new_run()
        for table in ("daily_runs", "message_versions", "daily_run_items"):
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)

    def test_version_metrics_deduplicate_uid_across_accounts_and_runs(self):
        self.candidate(account="a")
        self.candidate(account="b")
        first = self.run_id(ad="A")
        self.finish(self.prepare(first, account="a", reviews=[review(account="a"), review(account="b")]), at=NOW)
        self.finish(self.prepare(first, account="b", reviews=[review(account="a"), review(account="b")]), at=NOW)
        second = self.run_id(ad="A")
        self.finish(self.prepare(second, account="a", reviews=[review(account="a"), review(account="b")]), at=NOW)
        third = self.run_id(ad="B")
        self.finish(self.prepare(third, account="a", reviews=[review(account="a"), review(account="b")]), at=NOW)
        result = state.report(self.conn, third, now_ms=NOW + 8 * DAY)
        self.assertEqual(result.get("versions_scope"), "all_recorded_runs")
        a, b = sorted(result["versions"].values(), key=lambda row: -row["sent_messages"])
        self.assertEqual((a["sent_messages"], a["unique_sent_customers"], a["observation_windows"]), (3, 1, 3))
        self.assertEqual((b["sent_messages"], b["unique_sent_customers"], b["observation_windows"]), (1, 1, 1))
        self.assertEqual(a["sent_customers"], 3)
        self.assertIn("sent_customers", result["metric_notes"])
        self.assertIn("cross_version", result["metric_notes"])

    def test_legacy_uninitialized_run_exposes_policy_metadata_without_writes(self):
        self.candidate()
        rid = self.new_run()["run_id"]
        with self.conn:
            self.conn.execute("DELETE FROM daily_run_items WHERE run_id=?", (rid,))
            self.conn.execute("UPDATE daily_runs SET queue_policy_version=0 WHERE run_id=?", (rid,))
        before = self.path.stat().st_mtime_ns
        with state.connect_readonly(self.path) as ro:
            metadata = state.run_status(ro, rid, now_ms=NOW)
            self.assertFalse(metadata["initialized"])
            self.assertTrue(metadata["initialization_required"])
            self.assertEqual(metadata["touch_policy"], "uid_once")
            self.assertEqual(metadata["run_state"], "active")
            self.assertIsNone(metadata["counts"])
            self.assertIsNone(metadata["uncompleted"])
            self.assertEqual(ro.execute("SELECT COUNT(*) FROM daily_run_items WHERE run_id=?", (rid,)).fetchone()[0], 0)
        self.assertEqual(self.path.stat().st_mtime_ns, before)
        state.begin_session(self.conn, rid, "new-request", now_ms=NOW)
        initialized = state.run_status(self.conn, rid, now_ms=NOW)
        self.assertTrue(initialized["initialized"])
        self.assertEqual(initialized["counts"]["uncompleted_customers"], 1)
        self.assertEqual(state.next_items(self.conn, rid, now_ms=NOW)["items"][0]["user_id"], "u1")


if __name__ == "__main__":
    unittest.main()
