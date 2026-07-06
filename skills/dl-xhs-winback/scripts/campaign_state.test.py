#!/usr/bin/env python3
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


SCRIPT = Path(__file__).with_name("campaign_state.py")
ACCOUNT_IDS = "aaaa1111000000000d000001,bbbb2222000000000d000002"

# 测试用假账号配置：Carol 有两个历史账号 ID（legacy + current），用来测试
# 一个人名下挂多个账号 ID 时的归属/去重逻辑；Carol 的别名同样是假数据。
TEST_SKILL_CONFIG = {
    "accounts": [
        {"id": "aaaa1111000000000d000001", "name": "Alice"},
        {"id": "bbbb2222000000000d000002", "name": "Bob"},
        {"id": "cccc3333000000000d000003", "name": "Carol-legacy"},
        {"id": "dddd4444000000000d000004", "name": "Carol"},
    ],
    "aliases": {
        "Carol（外贸订单合作 只对接工厂老板）": "Carol",
    },
}


def shanghai_date(days=0):
    return (datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=days)).isoformat()


class CampaignStateIncrementalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.sqlite3"
        self.config_path = Path(self.tmp.name) / "config.json"
        self.config_path.write_text(json.dumps(TEST_SKILL_CONFIG), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_state(self, *args):
        result = self.run_raw(*args)
        if result.returncode != 0:
            raise AssertionError(
                f"command failed: {' '.join(args)}\nstdout={result.stdout}\nstderr={result.stderr}"
            )
        return json.loads(result.stdout)

    def run_raw(self, *args):
        env = {**os.environ, "XHS_FOLLOWUP_CONFIG": str(self.config_path)}
        result = subprocess.run(
            ["python3", str(SCRIPT), "--db", str(self.db), *args],
            check=False,
            text=True,
            capture_output=True,
            env=env,
        )
        return result

    def add_candidate(self, campaign_id, user_id, reply_status="awaiting_customer"):
        account_id = "aaaa1111000000000d000001"
        key = f"Total-{user_id}-{account_id}"
        last_message_at_ms = int(time.time() * 1000) - 26 * 60 * 60 * 1000
        self.run_state(
            "candidate",
            "--campaign-id",
            campaign_id,
            "--conversation-key",
            key,
            "--user-id",
            user_id,
            "--account-id",
            account_id,
            "--display-name",
            f"user-{user_id}",
            "--lead-status",
            "no_lead",
            "--party-status",
            "customer",
            "--reply-status",
            reply_status,
            "--negative-status",
            "safe",
            "--last-message-at-ms",
            str(last_message_at_ms),
            "--evidence",
            "test",
        )
        return key

    def write_user_list_csv(self, rows):
        path = Path(self.tmp.name) / "用户列表.csv"
        headers = [
            "用户昵称",
            "用户ID",
            "归属账号",
            "最新进线时间",
            "最近开口时间",
            "最近留资时间",
            "手机号",
            "微信号",
            "留资方式",
            "用户类型",
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            import csv

            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            for row in rows:
                base = {key: "-" for key in headers}
                base.update(row)
                writer.writerow(base)
        return path

    def create_source_with_sent_user(self, user_id="usource"):
        source = self.run_state(
            "start",
            "--message",
            "yesterday",
            "--activate",
            "--coverage-scope",
            "month",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]
        key = self.add_candidate(source, user_id)
        self.run_state("mark-scan", "--campaign-id", source, "--state", "completed")
        self.run_state(
            "record-send",
            "--campaign-id",
            source,
            "--conversation-key",
            key,
            "--status",
            "sent",
            "--message-snapshot",
            "yesterday",
        )
        sent_at = int(time.time() * 1000) - 25 * 60 * 60 * 1000
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE send_attempts SET attempted_at_ms=?", (sent_at,))
        return source, key, sent_at

    def create_source_with_many_sent_users(self, count=101):
        source = self.run_state(
            "start",
            "--message",
            "baseline-many",
            "--activate",
            "--coverage-scope",
            "month",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]
        sent_at = int(time.time() * 1000) - 25 * 60 * 60 * 1000
        keys = []
        for i in range(count):
            user_id = f"ubase{i:03d}"
            key = self.add_candidate(source, user_id)
            keys.append(key)
        self.run_state("mark-scan", "--campaign-id", source, "--state", "completed")
        for key in keys:
            self.run_state("record-send", "--campaign-id", source, "--conversation-key", key, "--status", "sent", "--message-snapshot", "baseline-many")
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE send_attempts SET attempted_at_ms=? WHERE campaign_id=?", (sent_at, source))
        return source

    def campaign_rows(self):
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(
                "SELECT campaign_id, message, state, scan_completed_at_ms FROM campaigns ORDER BY created_at_ms"
            )]

    def test_operator_plan_recommends_latest_user_list_followup_without_mutating_state(self):
        source, _, sent_at = self.create_source_with_sent_user()
        before = self.campaign_rows()

        plan = self.run_state(
            "operator-plan",
            "--message",
            "today",
            "--account-ids",
            ACCOUNT_IDS,
        )
        after = self.campaign_rows()

        self.assertTrue(plan["ok"])
        self.assertTrue(plan["requires_manual_mode_choice"])
        self.assertEqual(plan["recommended_mode"], "latest_user_list_followup")
        self.assertEqual(plan["baseline"]["campaign_id"], source)
        self.assertEqual(plan["baseline"]["first_sent_at_ms"], sent_at)
        self.assertEqual(plan["scan_window"]["start_ms"], sent_at - 10 * 60 * 1000)
        self.assertEqual(plan["modes"][0]["mode"], "table_followup")
        self.assertEqual(plan["modes"][0]["label"], "表格跟进")
        self.assertTrue(plan["modes"][0]["requires_data_source_choice"])
        self.assertEqual(plan["coverage_modes"][0]["scope"], "user_list")
        self.assertEqual(plan["coverage_modes"][0]["label"], "表格跟进")
        self.assertFalse(plan["coverage_modes"][0]["requires_browser_scan"])
        self.assertTrue(plan["coverage_modes"][0]["requires_uid_review"])
        self.assertEqual(
            [option["id"] for option in plan["coverage_modes"][0]["data_sources"]],
            ["1A", "1B"],
        )
        self.assertEqual(plan["coverage_modes"][0]["data_sources"][0]["mode"], "latest_single_csv")
        self.assertEqual(plan["coverage_modes"][0]["data_sources"][0]["maps_to_scope"], "user_list")
        self.assertEqual(plan["coverage_modes"][0]["data_sources"][1]["mode"], "historical_multi_csv")
        self.assertEqual(plan["coverage_modes"][0]["data_sources"][1]["maps_to_scope"], "history")
        self.assertEqual(plan["would_create_campaign"], True)
        self.assertIsNone(plan["would_reuse_campaign_id"])
        self.assertEqual(before, after)
        self.assertFalse(any(row["message"] == "today" for row in after))

    def test_operator_plan_reports_reuse_without_resetting_existing_campaign(self):
        target = self.run_state(
            "start",
            "--message",
            "same-message",
            "--activate",
            "--coverage-scope",
            "month",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]
        self.run_state("mark-scan", "--campaign-id", target, "--state", "completed")
        before = self.campaign_rows()

        plan = self.run_state(
            "operator-plan",
            "--message",
            "same-message",
            "--account-ids",
            ACCOUNT_IDS,
        )
        after = self.campaign_rows()

        self.assertTrue(plan["ok"])
        self.assertIsNone(plan["would_reuse_campaign_id"])
        self.assertEqual(plan["would_reuse_campaigns_by_scope"]["month"], target)
        self.assertEqual(plan["would_create_campaign"], True)
        self.assertEqual(before, after)
        reused = next(row for row in after if row["campaign_id"] == target)
        self.assertIsNotNone(reused["scan_completed_at_ms"])

    def test_start_defaults_to_latest_user_list_scope(self):
        campaign = self.run_state(
            "start",
            "--message",
            "default-scope",
            "--activate",
            "--account-ids",
            ACCOUNT_IDS,
        )

        self.assertEqual(campaign["coverage_scope"], "user_list")
        summary = self.run_state("summary", "--campaign-id", campaign["campaign_id"])
        self.assertEqual(summary["coverage"]["scope"], "user_list")
        self.assertEqual(summary["coverage"]["missing_reason"], "current_user_list_import_required")

    def test_operator_plan_reuses_latest_user_list_campaign_by_default(self):
        target = self.run_state(
            "start",
            "--message",
            "same-user-list-message",
            "--activate",
            "--coverage-scope",
            "user_list",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]

        plan = self.run_state(
            "operator-plan",
            "--message",
            "same-user-list-message",
            "--account-ids",
            ACCOUNT_IDS,
        )

        self.assertEqual(plan["would_reuse_campaign_id"], target)
        self.assertFalse(plan["would_create_campaign"])
        self.assertEqual(plan["would_reuse_campaigns_by_scope"]["user_list"], target)

    def test_user_list_summary_points_to_latest_csv_import_not_scan(self):
        campaign_id = self.run_state(
            "start",
            "--message",
            "needs-latest-csv",
            "--activate",
            "--coverage-scope",
            "user_list",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]

        summary = self.run_state("summary", "--campaign-id", campaign_id)
        preflight = self.run_state("preflight", "--campaign-id", campaign_id)

        self.assertEqual(summary["coverage"]["missing_reason"], "current_user_list_import_required")
        self.assertEqual(summary["doctor"]["status"], "current_user_list_import_required")
        self.assertEqual(summary["doctor"]["next_action"], "import_latest_user_list_csv")
        self.assertIn("账号范围过滤归属账号", summary["doctor"]["plain_zh"])
        self.assertIn("UID 复核", summary["doctor"]["plain_zh"])
        self.assertEqual(preflight["status"], "current_user_list_import_required")
        self.assertEqual(preflight["next_action"], "import_latest_user_list_csv")

    def test_operator_plan_reports_reuse_by_coverage_scope(self):
        history_campaign = self.run_state(
            "start",
            "--message",
            "same-message",
            "--activate",
            "--coverage-scope",
            "history",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]

        plan = self.run_state(
            "operator-plan",
            "--message",
            "same-message",
            "--account-ids",
            ACCOUNT_IDS,
        )

        self.assertIsNone(plan["would_reuse_campaign_id"])
        self.assertIsNone(plan["would_reuse_campaigns_by_scope"]["user_list"])
        self.assertIsNone(plan["would_reuse_campaigns_by_scope"]["month"])
        self.assertEqual(plan["would_reuse_campaigns_by_scope"]["history"], history_campaign)
        self.assertIsNone(plan["would_reuse_campaigns_by_scope"]["combined"])

    def test_resume_mode_available_for_unfinished_campaign_without_history_import(self):
        unfinished = self.run_state(
            "start",
            "--message",
            "unfinished-message",
            "--activate",
            "--coverage-scope",
            "month",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]

        plan = self.run_state(
            "operator-plan",
            "--message",
            "new-message",
            "--account-ids",
            ACCOUNT_IDS,
        )
        resume_mode = next(mode for mode in plan["coverage_modes"] if mode["id"] == "2")

        self.assertTrue(resume_mode["available"])
        self.assertEqual(resume_mode["scope"], "resume_unfinished")
        self.assertEqual(resume_mode["unfinished_tasks"]["latest_campaign_id"], unfinished)
        self.assertNotEqual(resume_mode.get("unavailable_reason"), "historical_user_list_csv_required")

    def test_incremental_plan_recommends_daily_incremental_from_recent_baseline(self):
        source, _, sent_at = self.create_source_with_sent_user()
        target = self.run_state(
            "start",
            "--message",
            "today",
            "--activate",
            "--coverage-scope",
            "month",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]

        plan = self.run_state("incremental-plan", "--campaign-id", target)

        self.assertTrue(plan["ok"])
        self.assertEqual(plan["recommended_mode"], "daily_incremental_scan")
        self.assertEqual(plan["baseline"]["campaign_id"], source)
        self.assertEqual(plan["baseline"]["first_sent_at_ms"], sent_at)
        self.assertEqual(plan["scan_window"]["start_ms"], sent_at - 10 * 60 * 1000)
        self.assertTrue(plan["requires_manual_mode_choice"])

    def test_seed_incremental_candidates_copies_sent_users_without_overwriting_updates(self):
        source, source_key, _ = self.create_source_with_sent_user("u1")
        target = self.run_state(
            "start",
            "--message",
            "today",
            "--activate",
            "--coverage-scope",
            "month",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]
        self.add_candidate(target, "u1", reply_status="awaiting_us")

        seeded = self.run_state(
            "seed-incremental-candidates",
            "--campaign-id",
            target,
            "--source-campaign-id",
            source,
        )
        summary = self.run_state("summary", "--campaign-id", target)

        self.assertTrue(seeded["ok"])
        self.assertEqual(seeded["copied_candidates"], 0)
        self.assertEqual(seeded["skipped_existing_candidates"], 1)
        self.assertEqual(summary["counts"]["awaiting_us"], 1)
        self.assertEqual(summary["counts"]["remaining_queueable_users"], 0)

        target2 = self.run_state(
            "start",
            "--message",
            "today-2",
            "--activate",
            "--coverage-scope",
            "month",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]
        seeded2 = self.run_state(
            "seed-incremental-candidates",
            "--campaign-id",
            target2,
            "--source-campaign-id",
            source,
        )
        self.assertEqual(seeded2["copied_candidates"], 1)
        queue = self.run_state("queue", "--campaign-id", target2)
        self.assertEqual(queue["queue"][0]["conversation_key"], source_key)

    def test_allow_incomplete_scan_requires_seed_and_marked_incremental_window(self):
        source, _, sent_at = self.create_source_with_sent_user("uincremental")
        target = self.run_state(
            "start",
            "--message",
            "today",
            "--activate",
            "--coverage-scope",
            "month",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]

        unseeded = self.run_raw(
            "prepare-batch",
            "--campaign-id",
            target,
            "--allow-incomplete-scan",
        )
        self.assertNotEqual(unseeded.returncode, 0)
        self.assertEqual(json.loads(unseeded.stdout)["error"], "incremental_seed_not_completed")

        self.run_state(
            "seed-incremental-candidates",
            "--campaign-id",
            target,
            "--source-campaign-id",
            source,
        )
        unmarked = self.run_raw(
            "prepare-batch",
            "--campaign-id",
            target,
            "--allow-incomplete-scan",
        )
        self.assertNotEqual(unmarked.returncode, 0)
        self.assertEqual(json.loads(unmarked.stdout)["error"], "incremental_window_not_completed")

        marked = self.run_state(
            "mark-incremental-window",
            "--campaign-id",
            target,
            "--source-campaign-id",
            source,
            "--window-start-ms",
            str(sent_at - 10 * 60 * 1000),
        )
        self.assertTrue(marked["ok"])
        prepared = self.run_state(
            "prepare-batch",
            "--campaign-id",
            target,
            "--allow-incomplete-scan",
        )
        self.assertTrue(prepared["ok"])
        self.assertEqual(prepared["counts"]["pending"], 1)

    def test_uncertain_item_blocks_same_batch_pending_queue_and_can_send(self):
        campaign_id = self.run_state(
            "start",
            "--message",
            "today",
            "--activate",
            "--coverage-scope",
            "month",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]
        key1 = self.add_candidate(campaign_id, "uuncertain1")
        key2 = self.add_candidate(campaign_id, "uuncertain2")
        self.run_state("mark-scan", "--campaign-id", campaign_id, "--state", "completed")
        batch_id = self.run_state("prepare-batch", "--campaign-id", campaign_id, "--limit", "2")["batch_id"]
        self.run_state("approve-batch", "--batch-id", batch_id)
        self.run_state(
            "record-send",
            "--campaign-id",
            campaign_id,
            "--conversation-key",
            key1,
            "--status",
            "uncertain",
            "--detail",
            "new_right_message_not_verified",
            "--batch-id",
            batch_id,
            "--message-snapshot",
            "today",
        )

        doctor = self.run_state("doctor", "--campaign-id", campaign_id)
        self.assertEqual(doctor["doctor"]["status"], "needs_uncertain_review")

        queue = self.run_raw("batch-queue", "--batch-id", batch_id)
        self.assertNotEqual(queue.returncode, 0)
        self.assertEqual(json.loads(queue.stdout)["error"], "batch_has_uncertain_requires_manual_review")

        allowed = self.run_state(
            "can-send",
            "--campaign-id",
            campaign_id,
            "--conversation-key",
            key2,
            "--batch-id",
            batch_id,
        )
        self.assertFalse(allowed["allowed"])
        self.assertEqual(allowed["reason"], "batch_has_uncertain_requires_manual_review")

    def test_completed_scan_requires_overlap_with_large_recent_baseline(self):
        self.create_source_with_many_sent_users()
        target = self.run_state(
            "start",
            "--message",
            "today",
            "--activate",
            "--coverage-scope",
            "month",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]
        self.add_candidate(target, "unew")

        marked = self.run_raw("mark-scan", "--campaign-id", target, "--state", "completed")

        self.assertNotEqual(marked.returncode, 0)
        payload = json.loads(marked.stdout)
        self.assertEqual(payload["error"], "scan_completion_suspicious_recent_baseline_not_covered")
        self.assertEqual(payload["suspicious_scan"]["overlap_users"], 0)

    def test_suspicious_forced_completed_scan_blocks_queueing(self):
        self.create_source_with_many_sent_users()
        target = self.run_state(
            "start",
            "--message",
            "today",
            "--activate",
            "--coverage-scope",
            "month",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]
        self.add_candidate(target, "unew")
        self.run_state("mark-scan", "--campaign-id", target, "--state", "completed", "--force")

        doctor = self.run_state("doctor", "--campaign-id", target)
        self.assertEqual(doctor["doctor"]["status"], "scan_suspicious_needs_rescan")
        prepared = self.run_raw("prepare-batch", "--campaign-id", target)
        self.assertNotEqual(prepared.returncode, 0)
        self.assertEqual(json.loads(prepared.stdout)["error"], "formal_scan_suspicious_recent_baseline_not_covered")

    def test_invalidate_scan_stops_open_batch_and_resets_completion(self):
        campaign_id = self.run_state(
            "start",
            "--message",
            "today",
            "--activate",
            "--coverage-scope",
            "month",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]
        self.add_candidate(campaign_id, "uinvalidate1")
        self.add_candidate(campaign_id, "uinvalidate2")
        self.run_state("mark-scan", "--campaign-id", campaign_id, "--state", "completed")
        batch_id = self.run_state("prepare-batch", "--campaign-id", campaign_id, "--limit", "2")["batch_id"]
        self.run_state("approve-batch", "--batch-id", batch_id)

        invalidated = self.run_state(
            "invalidate-scan",
            "--campaign-id",
            campaign_id,
            "--reason",
            "test_suspicious_scan",
        )

        self.assertFalse(invalidated["scan"]["complete"])
        self.assertEqual(invalidated["scan"]["stopped_reason"], "test_suspicious_scan")
        batch = self.run_state("batch-summary", "--batch-id", batch_id)
        self.assertEqual(batch["state"], "stopped")
        self.assertEqual(batch["counts"]["skipped"], 2)

    def test_import_user_list_dedupes_lead_protection_and_targets_only_no_lead_active_users(self):
        csv_path = self.write_user_list_csv([
            {
                "用户昵称": "no-lead-old",
                "用户ID": "uhistory1",
                "归属账号": "Alice",
                "最新进线时间": "2026-05-30 12:00:00",
                "最近开口时间": "2026-05-30 12:00:00",
                "用户类型": "正常用户",
            },
            {
                "用户昵称": "later-lead",
                "用户ID": "uhistory1",
                "归属账号": "Bob",
                "最新进线时间": "2026-05-31 12:00:00",
                "最近开口时间": "2026-05-31 12:00:00",
                "微信号": "wx-present",
                "用户类型": "正常用户",
            },
            {
                "用户昵称": "target",
                "用户ID": "uhistory2",
                "归属账号": "Alice",
                "最新进线时间": "2026-05-29 12:00:00",
                "最近开口时间": "2026-05-29 12:00:00",
                "用户类型": "正常用户",
            },
            {
                "用户昵称": "用户已注销",
                "用户ID": "uhistory3",
                "归属账号": "Alice",
                "最新进线时间": "2026-05-28 12:00:00",
                "最近开口时间": "2026-05-28 12:00:00",
                "用户类型": "正常用户",
            },
        ])

        imported = self.run_state(
            "import-user-list",
            "--file",
            str(csv_path),
            "--cutoff-date",
            "2026-05-31",
        )

        self.assertTrue(imported["ok"])
        self.assertEqual(imported["row_count"], 4)
        self.assertEqual(imported["unique_user_ids"], 3)
        self.assertEqual(imported["lead_users"], 1)
        self.assertEqual(imported["targetable_no_lead_users"], 1)

        imports = self.run_state("historical-imports")
        self.assertEqual(imports["count"], 1)
        self.assertEqual(imports["imports"][0]["unique_user_ids"], 3)

        campaign_id = self.run_state(
            "start",
            "--message",
            "history",
            "--activate",
            "--coverage-scope",
            "history",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]
        targets = self.run_state("historical-targets", "--campaign-id", campaign_id)

        self.assertEqual(targets["count"], 1)
        self.assertEqual(targets["targets"][0]["user_id"], "uhistory2")

    def test_historical_targets_filter_csv_owner_by_campaign_account_scope(self):
        csv_path = self.write_user_list_csv([
            {
                "用户昵称": "allowed-alice",
                "用户ID": "uallowed1",
                "归属账号": "Alice",
                "最新进线时间": "2026-05-31 12:00:00",
                "最近开口时间": "2026-05-31 12:00:00",
                "用户类型": "正常用户",
            },
            {
                "用户昵称": "allowed-bob",
                "用户ID": "uallowed2",
                "归属账号": "Bob",
                "最新进线时间": "2026-05-31 12:00:00",
                "最近开口时间": "2026-05-31 12:00:00",
                "用户类型": "正常用户",
            },
            {
                "用户昵称": "outside-niko",
                "用户ID": "uoutside1",
                "归属账号": "OutsideAccountA出海",
                "最新进线时间": "2026-05-31 12:00:00",
                "最近开口时间": "2026-05-31 12:00:00",
                "用户类型": "正常用户",
            },
            {
                "用户昵称": "outside-coco",
                "用户ID": "uoutside2",
                "归属账号": "OutsideAccountB外贸",
                "最新进线时间": "2026-05-31 12:00:00",
                "最近开口时间": "2026-05-31 12:00:00",
                "用户类型": "正常用户",
            },
        ])
        self.run_state("import-user-list", "--file", str(csv_path), "--cutoff-date", "2026-05-31")
        campaign_id = self.run_state(
            "start",
            "--message",
            "history",
            "--activate",
            "--coverage-scope",
            "history",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]

        targets = self.run_state("historical-targets", "--campaign-id", campaign_id)

        self.assertEqual(targets["total_targetable"], 2)
        self.assertEqual({target["user_id"] for target in targets["targets"]}, {"uallowed1", "uallowed2"})
        for target in targets["targets"]:
            self.assertTrue(set(target["account_names"]).issubset({"Alice", "Bob"}))

    def test_historical_targets_include_carol_full_csv_owner_name(self):
        carol_id = "dddd4444000000000d000004"
        carol_csv_name = "Carol（外贸订单合作 只对接工厂老板）"
        csv_path = self.write_user_list_csv([
            {
                "用户昵称": "allowed-carol",
                "用户ID": "ucarol1",
                "归属账号": carol_csv_name,
                "最新进线时间": "2026-05-31 12:00:00",
                "最近开口时间": "2026-05-31 12:00:00",
                "用户类型": "正常用户",
            },
        ])
        self.run_state("import-user-list", "--file", str(csv_path), "--cutoff-date", "2026-05-31")
        campaign_id = self.run_state(
            "start",
            "--message",
            "history-carol",
            "--activate",
            "--coverage-scope",
            "history",
            "--account-ids",
            carol_id,
        )["campaign_id"]

        targets = self.run_state("historical-targets", "--campaign-id", campaign_id)

        self.assertEqual(targets["total_targetable"], 1)
        self.assertEqual(targets["targets"][0]["user_id"], "ucarol1")
        self.assertEqual(targets["targets"][0]["account_names"], [carol_csv_name])
        with sqlite3.connect(self.db) as conn:
            account_id = conn.execute(
                "SELECT account_id FROM historical_user_accounts WHERE user_id=? AND account_name=?",
                ("ucarol1", carol_csv_name),
            ).fetchone()[0]
        self.assertEqual(account_id, carol_id)

    def test_default_account_scope_authorizes_both_observed_carol_ids(self):
        plan = self.run_state("operator-plan", "--message", "dual-carol")

        self.assertIn("cccc3333000000000d000003", plan["account_ids"])
        self.assertIn("dddd4444000000000d000004", plan["account_ids"])

    def test_connect_backfills_existing_carol_full_csv_owner_account_id(self):
        carol_id = "dddd4444000000000d000004"
        carol_csv_name = "Carol（外贸订单合作 只对接工厂老板）"
        csv_path = self.write_user_list_csv([
            {
                "用户昵称": "existing-carol",
                "用户ID": "ucarol-existing",
                "归属账号": carol_csv_name,
                "最新进线时间": "2026-05-31 12:00:00",
                "最近开口时间": "2026-05-31 12:00:00",
                "用户类型": "正常用户",
            },
        ])
        self.run_state("import-user-list", "--file", str(csv_path), "--cutoff-date", "2026-05-31")
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "UPDATE historical_user_accounts SET account_id='' WHERE user_id=? AND account_name=?",
                ("ucarol-existing", carol_csv_name),
            )

        self.run_state("historical-imports")

        with sqlite3.connect(self.db) as conn:
            account_id = conn.execute(
                "SELECT account_id FROM historical_user_accounts WHERE user_id=? AND account_name=?",
                ("ucarol-existing", carol_csv_name),
            ).fetchone()[0]
        self.assertEqual(account_id, carol_id)

    def test_authorized_no_lead_target_is_not_blocked_by_non_authorized_account_lead(self):
        csv_path = self.write_user_list_csv([
            {
                "用户昵称": "allowed-no-lead",
                "用户ID": "ucrossaccount",
                "归属账号": "Alice",
                "最新进线时间": "2026-05-31 12:00:00",
                "最近开口时间": "2026-05-31 12:00:00",
                "用户类型": "正常用户",
            },
            {
                "用户昵称": "outside-has-lead",
                "用户ID": "ucrossaccount",
                "归属账号": "OutsideAccountA出海",
                "最新进线时间": "2026-05-31 12:30:00",
                "最近开口时间": "2026-05-31 12:30:00",
                "微信号": "wx-outside",
                "用户类型": "正常用户",
            },
        ])
        self.run_state("import-user-list", "--file", str(csv_path), "--cutoff-date", "2026-05-31")
        campaign_id = self.run_state(
            "start",
            "--message",
            "history",
            "--activate",
            "--coverage-scope",
            "history",
            "--account-ids",
            "aaaa1111000000000d000001",
        )["campaign_id"]

        targets = self.run_state("historical-targets", "--campaign-id", campaign_id)

        self.assertEqual(targets["total_targetable"], 1)
        self.assertEqual(targets["targets"][0]["user_id"], "ucrossaccount")
        self.assertEqual(targets["targets"][0]["account_names"], ["Alice"])

    def test_mark_historical_review_rejects_user_outside_campaign_account_scope(self):
        csv_path = self.write_user_list_csv([
            {
                "用户昵称": "outside-nana",
                "用户ID": "uoutside-nana",
                "归属账号": "Nana",
                "最新进线时间": "2026-05-31 12:00:00",
                "最近开口时间": "2026-05-31 12:00:00",
                "用户类型": "正常用户",
            },
        ])
        self.run_state("import-user-list", "--file", str(csv_path), "--cutoff-date", "2026-05-31")
        campaign_id = self.run_state(
            "start",
            "--message",
            "history",
            "--activate",
            "--coverage-scope",
            "history",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]

        reviewed = self.run_raw(
            "mark-historical-review",
            "--campaign-id",
            campaign_id,
            "--user-id",
            "uoutside-nana",
            "--state",
            "eligible",
            "--conversation-key",
            "Total-uoutside-nana-aaaa1111000000000d000001",
            "--account-id",
            "aaaa1111000000000d000001",
            "--display-name",
            "outside-nana",
            "--last-message-at-ms",
            str(int(time.time() * 1000) - 26 * 60 * 60 * 1000),
        )

        self.assertNotEqual(reviewed.returncode, 0)
        self.assertEqual(json.loads(reviewed.stdout)["error"], "historical_user_not_in_campaign_target_scope")

    def test_historical_targets_can_select_only_retryable_reviews(self):
        csv_path = self.write_user_list_csv([
            {
                "用户昵称": "retryable-target",
                "用户ID": "uretryable",
                "归属账号": "Alice",
                "最新进线时间": "2026-05-31 12:00:00",
                "最近开口时间": "2026-05-31 12:00:00",
                "用户类型": "正常用户",
            },
            {
                "用户昵称": "eligible-target",
                "用户ID": "ueligible",
                "归属账号": "Alice",
                "最新进线时间": "2026-05-31 12:00:00",
                "最近开口时间": "2026-05-31 12:00:00",
                "用户类型": "正常用户",
            },
        ])
        self.run_state("import-user-list", "--file", str(csv_path), "--cutoff-date", "2026-05-31")
        campaign_id = self.run_state(
            "start",
            "--message",
            "history-retryable",
            "--activate",
            "--coverage-scope",
            "history",
            "--account-ids",
            "aaaa1111000000000d000001",
        )["campaign_id"]
        self.run_state(
            "mark-historical-review",
            "--campaign-id",
            campaign_id,
            "--user-id",
            "uretryable",
            "--state",
            "retryable",
            "--reason",
            "uid_search_no_matching_result",
        )
        self.run_state(
            "mark-historical-review",
            "--campaign-id",
            campaign_id,
            "--user-id",
            "ueligible",
            "--state",
            "eligible",
            "--conversation-key",
            "Total-ueligible-aaaa1111000000000d000001",
            "--account-id",
            "aaaa1111000000000d000001",
        )

        retryable_targets = self.run_state(
            "historical-targets",
            "--campaign-id",
            campaign_id,
            "--review-state",
            "retryable",
        )

        self.assertEqual(retryable_targets["count"], 1)
        self.assertEqual(retryable_targets["targets"][0]["user_id"], "uretryable")
        self.assertEqual(retryable_targets["targets"][0]["review_state"], "retryable")

    def test_retryable_historical_targets_prioritize_lowest_retry_count(self):
        csv_path = self.write_user_list_csv([
            {
                "用户昵称": "retried-twice",
                "用户ID": "uretried-twice",
                "归属账号": "Alice",
                "最新进线时间": "2026-05-31 13:00:00",
                "最近开口时间": "2026-05-31 13:00:00",
                "用户类型": "正常用户",
            },
            {
                "用户昵称": "retried-once",
                "用户ID": "uretried-once",
                "归属账号": "Alice",
                "最新进线时间": "2026-05-31 12:00:00",
                "最近开口时间": "2026-05-31 12:00:00",
                "用户类型": "正常用户",
            },
        ])
        self.run_state("import-user-list", "--file", str(csv_path), "--cutoff-date", "2026-05-31")
        campaign_id = self.run_state(
            "start",
            "--message",
            "history-retry-order",
            "--activate",
            "--coverage-scope",
            "history",
            "--account-ids",
            "aaaa1111000000000d000001",
        )["campaign_id"]
        for _ in range(2):
            self.run_state(
                "mark-historical-review",
                "--campaign-id",
                campaign_id,
                "--user-id",
                "uretried-twice",
                "--state",
                "retryable",
                "--reason",
                "uid_search_no_matching_result",
            )
        self.run_state(
            "mark-historical-review",
            "--campaign-id",
            campaign_id,
            "--user-id",
            "uretried-once",
            "--state",
            "retryable",
            "--reason",
            "uid_search_no_matching_result",
        )

        retryable_targets = self.run_state(
            "historical-targets",
            "--campaign-id",
            campaign_id,
            "--review-state",
            "retryable",
            "--limit",
            "1",
        )

        self.assertEqual(retryable_targets["targets"][0]["user_id"], "uretried-once")

    def test_history_scope_blocks_batch_until_import_and_review_window_are_ready(self):
        campaign_id = self.run_state(
            "start",
            "--message",
            "history",
            "--activate",
            "--coverage-scope",
            "history",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]

        no_import = self.run_raw("prepare-batch", "--campaign-id", campaign_id)
        self.assertNotEqual(no_import.returncode, 0)
        self.assertEqual(json.loads(no_import.stdout)["error"], "historical_import_required")

        csv_path = self.write_user_list_csv([
            {
                "用户昵称": "target",
                "用户ID": "uhistory2",
                "归属账号": "Alice",
                "最新进线时间": "2026-05-29 12:00:00",
                "最近开口时间": "2026-05-29 12:00:00",
                "用户类型": "正常用户",
            },
        ])
        self.run_state("import-user-list", "--file", str(csv_path), "--cutoff-date", "2026-05-31")

        no_review = self.run_raw("prepare-batch", "--campaign-id", campaign_id)
        self.assertNotEqual(no_review.returncode, 0)
        self.assertEqual(json.loads(no_review.stdout)["error"], "historical_review_window_not_completed")

        last_message_at_ms = int(time.time() * 1000) - 26 * 60 * 60 * 1000
        reviewed = self.run_state(
            "mark-historical-review",
            "--campaign-id",
            campaign_id,
            "--user-id",
            "uhistory2",
            "--state",
            "eligible",
            "--conversation-key",
            "Total-uhistory2-aaaa1111000000000d000001",
            "--account-id",
            "aaaa1111000000000d000001",
            "--display-name",
            "target",
            "--last-message-at-ms",
            str(last_message_at_ms),
            "--reason",
            "browser_uid_review_no_lead",
        )
        self.assertTrue(reviewed["ok"])

        still_no_window = self.run_raw("prepare-batch", "--campaign-id", campaign_id)
        self.assertNotEqual(still_no_window.returncode, 0)
        self.assertEqual(json.loads(still_no_window.stdout)["error"], "historical_review_window_not_completed")

        self.run_state("mark-historical-window", "--campaign-id", campaign_id)
        prepared = self.run_state("prepare-batch", "--campaign-id", campaign_id)
        self.assertTrue(prepared["ok"])
        self.assertEqual(prepared["counts"]["pending"], 1)

    def test_history_multi_csv_scope_does_not_require_fresh_user_list_cutoff(self):
        csv_path = self.write_user_list_csv([
            {
                "用户昵称": "history-target",
                "用户ID": "uhistoryoldcutoff",
                "归属账号": "Alice",
                "最新进线时间": "2026-05-29 12:00:00",
                "最近开口时间": "2026-05-29 12:00:00",
                "用户类型": "正常用户",
            },
        ])
        self.run_state("import-user-list", "--file", str(csv_path), "--cutoff-date", "2026-05-31")
        campaign_id = self.run_state(
            "start",
            "--message",
            "history-old-cutoff",
            "--activate",
            "--coverage-scope",
            "history",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]

        blocked = self.run_raw("prepare-batch", "--campaign-id", campaign_id)

        self.assertNotEqual(blocked.returncode, 0)
        self.assertEqual(json.loads(blocked.stdout)["error"], "historical_review_window_not_completed")

    def test_user_list_scope_requires_fresh_import_and_uid_review_but_not_scan(self):
        campaign_id = self.run_state(
            "start",
            "--message",
            "user-list",
            "--activate",
            "--coverage-scope",
            "user_list",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]

        old_csv_path = self.write_user_list_csv([
            {
                "用户昵称": "old-target",
                "用户ID": "uoldlist",
                "归属账号": "Alice",
                "最新进线时间": "2026-05-29 12:00:00",
                "最近开口时间": "2026-05-29 12:00:00",
                "用户类型": "正常用户",
            },
        ])
        self.run_state("import-user-list", "--file", str(old_csv_path), "--cutoff-date", "2026-05-31")

        stale = self.run_raw("prepare-batch", "--campaign-id", campaign_id)
        self.assertNotEqual(stale.returncode, 0)
        self.assertEqual(json.loads(stale.stdout)["error"], "current_user_list_import_required")

        fresh_csv_path = self.write_user_list_csv([
            {
                "用户昵称": "fresh-target",
                "用户ID": "ufreshlist",
                "归属账号": "Alice",
                "最新进线时间": shanghai_date(-1) + " 12:00:00",
                "最近开口时间": shanghai_date(-1) + " 12:00:00",
                "用户类型": "正常用户",
            },
        ])
        self.run_state("import-user-list", "--file", str(fresh_csv_path), "--cutoff-date", shanghai_date(-1))

        no_review = self.run_raw("prepare-batch", "--campaign-id", campaign_id)
        self.assertNotEqual(no_review.returncode, 0)
        self.assertEqual(json.loads(no_review.stdout)["error"], "historical_review_window_not_completed")

        last_message_at_ms = int(time.time() * 1000) - 26 * 60 * 60 * 1000
        self.run_state(
            "mark-historical-review",
            "--campaign-id",
            campaign_id,
            "--user-id",
            "ufreshlist",
            "--state",
            "eligible",
            "--conversation-key",
            "Total-ufreshlist-aaaa1111000000000d000001",
            "--account-id",
            "aaaa1111000000000d000001",
            "--display-name",
            "fresh-target",
            "--last-message-at-ms",
            str(last_message_at_ms),
            "--reason",
            "browser_uid_review_eligible",
        )
        self.run_state("mark-historical-window", "--campaign-id", campaign_id)

        summary = self.run_state("summary", "--campaign-id", campaign_id)
        self.assertFalse(summary["scan"]["complete"])
        self.assertTrue(summary["coverage"]["ready"])
        self.assertTrue(summary["coverage"]["current_user_list_import"]["ready"])
        self.assertEqual(summary["doctor"]["status"], "ready_to_prepare_batch")

        prepared = self.run_state("prepare-batch", "--campaign-id", campaign_id)
        self.assertTrue(prepared["ok"])
        self.assertEqual(prepared["counts"]["pending"], 1)

    def test_combined_scope_requires_current_month_scan_and_history_review(self):
        csv_path = self.write_user_list_csv([
            {
                "用户昵称": "target",
                "用户ID": "uhistory2",
                "归属账号": "Alice",
                "最新进线时间": "2026-05-29 12:00:00",
                "最近开口时间": "2026-05-29 12:00:00",
                "用户类型": "正常用户",
            },
        ])
        self.run_state("import-user-list", "--file", str(csv_path), "--cutoff-date", "2026-05-31")
        campaign_id = self.run_state(
            "start",
            "--message",
            "combined",
            "--activate",
            "--coverage-scope",
            "combined",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]

        no_month_scan = self.run_raw("prepare-batch", "--campaign-id", campaign_id)
        self.assertNotEqual(no_month_scan.returncode, 0)
        self.assertEqual(json.loads(no_month_scan.stdout)["error"], "current_month_scan_not_completed")

        self.run_state("mark-scan", "--campaign-id", campaign_id, "--state", "completed")
        no_history_window = self.run_raw("prepare-batch", "--campaign-id", campaign_id)
        self.assertNotEqual(no_history_window.returncode, 0)
        self.assertEqual(json.loads(no_history_window.stdout)["error"], "historical_review_window_not_completed")

    def test_month_scope_allows_queue_after_current_month_window_without_full_bottom_scan(self):
        campaign_id = self.run_state(
            "start",
            "--message",
            "month-window",
            "--activate",
            "--coverage-scope",
            "month",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]
        self.add_candidate(campaign_id, "umonthwindow")

        blocked = self.run_raw("prepare-batch", "--campaign-id", campaign_id)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertEqual(json.loads(blocked.stdout)["error"], "current_month_scan_not_completed")

        marked = self.run_state(
            "mark-current-month-window",
            "--campaign-id",
            campaign_id,
            "--window-start-date",
            "2026-06-01",
            "--visible-oldest-date",
            "2026-05-31",
        )
        self.assertTrue(marked["ok"])
        self.assertTrue(marked["current_month_window"]["completed"])
        summary = self.run_state("summary", "--campaign-id", campaign_id)
        self.assertFalse(summary["scan"]["complete"])
        self.assertTrue(summary["coverage"]["ready"])
        self.assertEqual(summary["doctor"]["status"], "ready_to_prepare_batch")

        prepared = self.run_state("prepare-batch", "--campaign-id", campaign_id)
        self.assertTrue(prepared["ok"])
        self.assertEqual(prepared["counts"]["pending"], 1)

    def test_combined_scope_current_month_window_then_requires_history_review(self):
        csv_path = self.write_user_list_csv([
            {
                "用户昵称": "target",
                "用户ID": "uhistory2",
                "归属账号": "Alice",
                "最新进线时间": "2026-05-29 12:00:00",
                "最近开口时间": "2026-05-29 12:00:00",
                "用户类型": "正常用户",
            },
        ])
        self.run_state("import-user-list", "--file", str(csv_path), "--cutoff-date", "2026-05-31")
        campaign_id = self.run_state(
            "start",
            "--message",
            "combined-window",
            "--activate",
            "--coverage-scope",
            "combined",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]
        self.add_candidate(campaign_id, "umonthwindow")
        self.run_state(
            "mark-current-month-window",
            "--campaign-id",
            campaign_id,
            "--window-start-date",
            "2026-06-01",
            "--visible-oldest-date",
            "2026-05-31",
        )

        blocked = self.run_raw("prepare-batch", "--campaign-id", campaign_id)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertEqual(json.loads(blocked.stdout)["error"], "historical_review_window_not_completed")

    def test_imported_lead_user_blocks_queue_even_if_candidate_is_added_later(self):
        csv_path = self.write_user_list_csv([
            {
                "用户昵称": "has-lead",
                "用户ID": "ulead1",
                "归属账号": "Alice",
                "最新进线时间": "2026-05-29 12:00:00",
                "最近开口时间": "2026-05-29 12:00:00",
                "最近留资时间": "2026-05-29 13:00:00",
                "微信号": "wx-present",
                "用户类型": "正常用户",
            },
        ])
        self.run_state("import-user-list", "--file", str(csv_path), "--cutoff-date", "2026-05-31")
        campaign_id = self.run_state(
            "start",
            "--message",
            "month",
            "--activate",
            "--coverage-scope",
            "month",
            "--account-ids",
            ACCOUNT_IDS,
        )["campaign_id"]
        key = self.add_candidate(campaign_id, "ulead1")
        self.run_state("mark-scan", "--campaign-id", campaign_id, "--state", "completed")

        queue = self.run_state("queue", "--campaign-id", campaign_id)
        self.assertEqual(queue["remaining_unassigned_users"], 0)

        allowed = self.run_state(
            "can-send",
            "--campaign-id",
            campaign_id,
            "--conversation-key",
            key,
        )
        self.assertFalse(allowed["allowed"])
        self.assertEqual(allowed["reason"], "historical_import_has_lead")


if __name__ == "__main__":
    unittest.main()
