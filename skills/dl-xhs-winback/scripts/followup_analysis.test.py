import json
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest

import followup_analysis as analysis
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "followup_analysis.py"
DAY = 86_400_000
NOW = 1_780_000_000_000


class AnalysisCLITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "state.sqlite3"
        con = sqlite3.connect(self.db)
        con.executescript(
            """
            CREATE TABLE historical_user_accounts (
              user_id TEXT, account_name TEXT, account_id TEXT,
              latest_in_at_ms INTEGER, deleted INTEGER,
              lead_status_from_import TEXT,
              PRIMARY KEY(user_id, account_name)
            );
            CREATE TABLE lead_observations (
              id INTEGER PRIMARY KEY, run_id TEXT, user_id TEXT,
              account_id TEXT, status TEXT, label TEXT, source TEXT,
              attribution_state TEXT, observed_at_ms INTEGER, evidence TEXT
            );
            """
        )
        con.close()

    def add_user(self, uid, account="a1", inbound=None, legacy="no_lead"):
        with sqlite3.connect(self.db) as con:
            con.execute(
                "INSERT INTO historical_user_accounts VALUES (?,?,?,?,?,?)",
                (uid, account, account, NOW - 5 * DAY if inbound is None else inbound, 0, legacy),
            )

    def add_observation(self, uid, source, status, account="a1", at=None):
        with sqlite3.connect(self.db) as con:
            con.execute(
                """INSERT INTO lead_observations
                (run_id,user_id,account_id,status,label,source,attribution_state,observed_at_ms,evidence)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                ("r1", uid, account, status, "lead" if status == "present" else "none", source,
                 "concrete" if source == "ad" else "empty" if source == "natural" else "unknown",
                 NOW - DAY if at is None else at, "confirmed panel"),
            )

    def call(self, *args):
        run = subprocess.run(
            [sys.executable, str(SCRIPT), *args], text=True, capture_output=True,
            check=False,
        )
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        return json.loads(run.stdout)

    def plan(self):
        return self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1", "--now-ms", str(NOW))

    def add_partial_mapping(self, uid="partial", known_account="a1"):
        with sqlite3.connect(self.db) as con:
            con.executemany("INSERT INTO historical_user_accounts VALUES (?,?,?,?,?,?)", [
                (uid, "known-A", known_account, NOW - 5 * DAY, 0, "no_lead"),
                (uid, "unresolved-B", "", NOW - 5 * DAY, 0, "no_lead")])

    def partial_sessions(self):
        return {("partial", "a1"): {
            "user_id": "partial", "account_id": "a1", "uid_verified": True,
            "account_verified": True, "captured_at_ms": NOW + 1000, "history_complete": True,
            "account_reviews": [{"user_id": "partial", "account_id": "a1", "uid_verified": True,
                "account_verified": True, "panel_loaded": True, "label": "none", "source": "natural",
                "attribution_note_state": "empty", "observed_at_ms": NOW}],
            "messages": [{"at_ms": NOW - DAY, "sender": "customer", "text": "课程怎么安排？"}]}}

    def test_partial_mapping_absence_does_not_prove_all_csv_contexts_untagged(self):
        self.add_partial_mapping()
        self.add_observation("partial", "natural", "absent")
        before = self.db.read_bytes()
        plan = self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1,a2", "--now-ms", str(NOW))
        self.assertEqual(plan["groups"]["natural_untagged"], [])
        self.assertEqual(plan["counts"]["unresolved"], 1)
        self.assertEqual(plan["counts"]["scoped_uids"], 1)
        self.assertEqual(plan["coverage_gaps"], {"mapping_missing_uids": 1, "scope_unknown_uids": 1})
        self.assertEqual(plan["lookup_queue"][0]["account_ids"], ["a1"])
        self.assertEqual(plan["lookup_queue"][0]["reason"], "account_mapping_incomplete")
        self.assertEqual(self.db.read_bytes(), before)

    def test_partial_mapping_enrich_does_not_promote_absent_known_account(self):
        self.add_partial_mapping()
        plan = self.plan()
        before = self.db.read_bytes()
        with analysis._read_only_db(self.db) as conn:
            enriched = analysis.enrich_plan(conn, plan, self.partial_sessions())
        self.assertEqual(enriched["groups"]["natural_untagged"], [])
        self.assertEqual(enriched["coverage_gaps"]["mapping_missing_uids"], 1)
        self.assertEqual(enriched["enrichment"]["deferred_reasons"], {"account_mapping_incomplete": 1})
        self.assertEqual(self.db.read_bytes(), before)

    def test_partial_mapping_enrich_removes_prior_untagged_sample(self):
        self.add_user("partial")
        self.add_observation("partial", "natural", "absent")
        plan = self.plan()
        self.assertEqual(len(plan["groups"]["natural_untagged"]), 1)
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO historical_user_accounts VALUES (?,?,?,?,?,?)",
                         ("partial", "unresolved-B", "", NOW - 5 * DAY, 0, "no_lead"))
        with analysis._read_only_db(self.db) as conn:
            enriched = analysis.enrich_plan(conn, plan, self.partial_sessions())
        self.assertEqual(enriched["groups"]["natural_untagged"], [])
        self.assertEqual(enriched["coverage_gaps"]["mapping_missing_uids"], 1)
        self.assertEqual(enriched["enrichment"]["existing_samples_deferred_without_current_check"], 1)

    def test_partial_mapping_outside_known_account_keeps_unknown_scope_gap(self):
        self.add_partial_mapping(known_account="outside")
        plan = self.plan()
        self.assertEqual(plan["counts"]["scoped_uids"], 0)
        self.assertEqual(plan["counts"]["unresolved"], 1)
        self.assertEqual(plan["coverage_gaps"], {"mapping_missing_uids": 1, "scope_unknown_uids": 1})
        self.assertEqual(plan["lookup_queue"], [])

    def test_partial_mapping_extra_verified_account_does_not_guess_original_name(self):
        self.add_partial_mapping()
        self.add_observation("partial", "natural", "absent")
        self.add_observation("partial", "natural", "absent", account="a2")
        plan = self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1,a2", "--now-ms", str(NOW))
        self.assertEqual(plan["groups"]["natural_untagged"], [])
        self.assertEqual(plan["coverage_gaps"]["mapping_missing_uids"], 1)
        # A persisted mapping of that actual original context can close the gap.
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE historical_user_accounts SET account_id='a2' WHERE account_name='unresolved-B'")
        resolved = self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1,a2", "--now-ms", str(NOW))
        self.assertEqual(resolved["coverage_gaps"]["mapping_missing_uids"], 0)
        self.assertEqual(len(resolved["groups"]["natural_untagged"]), 1)

    def test_partial_mapping_old_schema_without_account_name_keeps_gap_and_single_recovery(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("DROP TABLE historical_user_accounts")
            conn.execute("CREATE TABLE historical_user_accounts (user_id TEXT,account_id TEXT,latest_in_at_ms INTEGER,deleted INTEGER)")
            conn.executemany("INSERT INTO historical_user_accounts VALUES (?,?,?,0)", [
                ("partial", "a1", NOW - 5 * DAY), ("partial", "", NOW - 5 * DAY),
                ("single", "", NOW - 5 * DAY)])
        self.add_observation("partial", "natural", "absent")
        self.add_observation("single", "natural", "absent")
        plan = self.plan()
        self.assertEqual([x["user_id"] for x in plan["groups"]["natural_untagged"]], ["single"])
        self.assertEqual(plan["coverage_gaps"]["mapping_missing_uids"], 1)
        self.assertTrue(all(all(row["account_ids"]) for row in plan["lookup_queue"]))

    def test_unmapped_uids_do_not_consume_executable_lookup_budget(self):
        for index in range(125):
            self.add_user(f"missing{index:03d}", account="", inbound=NOW - DAY)
        for index in range(3):
            self.add_user(f"executable{index}", inbound=NOW - 5 * DAY)
        plan = self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1",
                         "--now-ms", str(NOW), "--max-lookup", "2")
        self.assertEqual(len(plan["lookup_queue"]), 2)
        self.assertTrue(all(row["account_ids"] == ["a1"] for row in plan["lookup_queue"]))
        self.assertTrue(all(row["user_id"].startswith("executable") for row in plan["lookup_queue"]))
        self.assertEqual(plan["counts"]["recent_import_uids"], 128)
        self.assertEqual(plan["counts"]["scoped_uids"], 3)
        self.assertEqual(plan["counts"]["mapping_missing_uids"], 125)
        self.assertEqual(plan["counts"]["scope_unknown_uids"], 125)
        self.assertEqual(plan["counts"]["lookup_omitted_due_limit"], 1)
        result = analysis.report(plan, {})
        self.assertEqual(result["counts"]["recent_import_uids"], 128)
        self.assertIn("账号映射缺失 125 人", analysis._markdown_report(result))

    def test_verified_observation_recovers_recent_uid_without_csv_account_mapping(self):
        self.add_user("verified", account="")
        self.add_observation("verified", "ad", "present")
        before = self.db.read_bytes()
        plan = self.plan()
        self.assertEqual([row["user_id"] for row in plan["groups"]["ad_tagged"]], ["verified"])
        self.assertEqual(plan["groups"]["ad_tagged"][0]["latest_in_at_ms"], NOW - 5 * DAY)
        self.assertEqual(plan["counts"]["recent_import_uids"], 1)
        self.assertEqual(plan["counts"]["scoped_uids"], 1)
        self.assertEqual(plan["counts"]["scope_unknown_uids"], 0)
        self.assertEqual(plan["counts"]["mapping_missing_uids"], 0)
        self.assertEqual(self.db.read_bytes(), before)

    def test_unknown_only_identity_is_a_reported_mapping_and_scope_gap(self):
        self.add_user("unknown", account="")
        self.add_observation("unknown", "unknown", "unknown")
        self.add_user("unmapped", account="")
        self.add_user("outside", account="a2")
        self.add_user("old", account="", inbound=NOW - 31 * DAY)
        plan = self.plan()
        self.assertEqual(plan.get("counts", {}).get("recent_import_uids"), 3)
        self.assertEqual(plan["counts"]["scoped_uids"], 0)
        self.assertEqual(plan["counts"]["scope_unknown_uids"], 2)
        self.assertEqual(plan["counts"]["mapping_missing_uids"], 2)
        self.assertEqual(plan["lookup_queue"], [])
        result = analysis.report(plan, {})
        self.assertEqual(result["coverage_gaps"], plan["coverage_gaps"])
        markdown = analysis._markdown_report(result)
        self.assertIn("账号映射缺失 2 人", markdown)
        self.assertIn("范围未知 2 人", markdown)
        self.assertIn("平台标签", markdown)
        with analysis._read_only_db(self.db) as conn:
            enriched = analysis.enrich_plan(conn, plan, {})
        self.assertEqual(enriched["enrichment"]["lookup_considered"], 0)
        self.assertEqual(enriched["coverage_gaps"]["mapping_missing_uids"], 2)

    def test_reviewed_cache_protection_refusal_and_sent_touch_supply_analysis_context(self):
        for uid in ("cache", "protected", "refused", "sent", "unreviewed", "no_boundary"):
            self.add_user(uid, account="")
        with sqlite3.connect(self.db) as con:
            con.executescript("""
                CREATE TABLE conversation_history_cache (user_id TEXT,account_id TEXT,boundary_message_id TEXT,business_reviewed INTEGER);
                CREATE TABLE contact_protections (user_id TEXT,account_id TEXT,evidence TEXT);
                CREATE TABLE no_intent_updates (user_id TEXT,account_id TEXT,evidence TEXT,state TEXT);
                CREATE TABLE daily_touches (user_id TEXT,account_id TEXT,state TEXT);
            """)
            con.executemany("INSERT INTO conversation_history_cache VALUES (?,?,?,?)", [
                ("cache", "a1", "boundary", 1), ("unreviewed", "a1", "boundary", 0), ("no_boundary", "a1", "", 1)])
            con.execute("INSERT INTO contact_protections VALUES ('protected','a1','已核验联系方式')")
            con.execute("INSERT INTO no_intent_updates VALUES ('refused','a1','明确拒绝','pending')")
            con.execute("INSERT INTO daily_touches VALUES ('sent','a1','sent')")
        plan = self.plan()
        self.assertEqual(plan.get("counts", {}).get("scoped_uids"), 4)
        self.assertEqual(plan["counts"]["mapping_missing_uids"], 2)
        known = {row["user_id"]: row for row in plan["lookup_queue"]}
        self.assertEqual(known["protected"]["account_ids"], ["a1"])
        self.assertEqual(known["refused"]["account_ids"], ["a1"])
        self.assertEqual(known["sent"]["account_ids"], ["a1"])
        self.assertEqual(known["cache"]["latest_in_at_ms"], NOW - 5 * DAY)
        self.assertNotIn("unreviewed", known)
        self.assertNotIn("no_boundary", known)

    def test_enrichment_uses_same_verified_context_sources_as_sampling(self):
        self.add_user("multi", account="a1")
        plan = self.plan()
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE contact_protections (user_id TEXT,account_id TEXT,evidence TEXT)")
            con.execute("INSERT INTO contact_protections VALUES ('multi','a2','已核验联系方式')")
        plan["account_ids"] = ["a1", "a2"]
        with analysis._read_only_db(self.db) as conn:
            result = analysis.enrich_plan(conn, plan, {})
        self.assertEqual(result["enrichment"]["deferred_reasons"].get("account_context_changed"), 1)

    def test_recovered_identity_requires_absence_after_real_csv_inbound(self):
        self.add_user("old_absence", account="", inbound=NOW - 5 * DAY)
        self.add_observation("old_absence", "natural", "absent", at=NOW - 6 * DAY)
        self.add_user("current_absence", account="", inbound=NOW - 5 * DAY)
        self.add_observation("current_absence", "natural", "absent", at=NOW - DAY)
        plan = self.plan()
        self.assertEqual([row["user_id"] for row in plan["groups"]["natural_untagged"]], ["current_absence"])
        self.assertIn("old_absence", [row["user_id"] for row in plan["lookup_queue"]])

    def test_enrichment_recovers_legacy_missing_lookup_from_verified_observation(self):
        self.add_user("recovered", account="")
        plan = self.plan()
        self.assertEqual(plan["lookup_queue"], [])
        # Compatibility with old saved plans; current sample-plan omits this
        # unexecutable item while retaining its counts and coverage gap.
        plan["lookup_queue"] = [{"user_id": "recovered", "account_ids": [],
                                "preferred_account_id": "", "latest_in_at_ms": NOW - 5 * DAY,
                                "reason": "account_mapping_missing"}]
        self.add_observation("recovered", "ad", "present", at=NOW)
        refreshed = self.plan()
        self.assertEqual([row["user_id"] for row in refreshed["groups"]["ad_tagged"]], ["recovered"])
        self.assertEqual(refreshed["coverage_gaps"]["mapping_missing_uids"], 0)
        sessions = {("recovered", "a1"): {
            "user_id": "recovered", "account_id": "a1", "uid_verified": True,
            "account_verified": True, "captured_at_ms": NOW + 1000,
            "history_complete": True,
            "account_reviews": [{"user_id": "recovered", "account_id": "a1", "uid_verified": True,
                                 "account_verified": True, "panel_loaded": True, "label": "lead",
                                 "source": "ad", "attribution_note_state": "concrete", "observed_at_ms": NOW}],
            "messages": [{"at_ms": NOW - DAY, "sender": "customer", "text": "多少钱？"}]}}
        with analysis._read_only_db(self.db) as conn:
            enriched = analysis.enrich_plan(conn, plan, sessions)
        self.assertEqual([row["user_id"] for row in enriched["groups"]["ad_tagged"]], ["recovered"])
        self.assertEqual(enriched["coverage_gaps"]["mapping_missing_uids"], 0)
        self.assertEqual(enriched["counts"]["scoped_uids"], 1)

    def test_four_verified_groups_ignore_legacy_lead_and_recent_untagged(self):
        # A CSV lead flag is not a confirmed platform label.
        for uid, source, status in (
            ("ad_yes", "ad", "present"),
            ("ad_no", "ad", "absent"),
            ("natural_yes", "natural", "present"),
            ("natural_no", "natural", "absent"),
        ):
            self.add_user(uid, legacy="has_lead" if uid == "ad_no" else "no_lead")
            self.add_observation(uid, source, status)
        self.add_user("fresh", inbound=NOW - DAY)
        self.add_observation("fresh", "ad", "absent")
        self.add_user("missing")
        self.add_user("unscoped", account="a2")
        self.add_observation("unscoped", "ad", "present", account="a2")

        plan = self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1",
                         "--now-ms", str(NOW), "--max-lookup", "5")
        self.assertEqual(
            {key: [item["user_id"] for item in group] for key, group in plan["groups"].items()},
            {"ad_tagged": ["ad_yes"], "ad_untagged": ["ad_no"],
             "natural_tagged": ["natural_yes"], "natural_untagged": ["natural_no"]},
        )
        self.assertEqual(plan["groups"]["ad_tagged"][0]["account_ids"], ["a1"])
        self.assertIn("missing", [item["user_id"] for item in plan["lookup_queue"]])
        self.assertIn("fresh", [item["user_id"] for item in plan["lookup_queue"]])
        self.assertNotIn("unscoped", [item["user_id"] for item in plan["lookup_queue"]])

    def test_unknown_newer_observation_blocks_unlabeled_claim_and_lookup_is_bounded(self):
        for i in range(7):
            uid = f"u{i}"
            self.add_user(uid)
        self.add_observation("u0", "ad", "absent", at=NOW - 2 * DAY)
        self.add_observation("u0", "unknown", "unknown", at=NOW - DAY)
        plan = self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1",
                         "--now-ms", str(NOW), "--max-lookup", "3")
        self.assertEqual(plan["groups"]["ad_untagged"], [])
        self.assertEqual(len(plan["lookup_queue"]), 3)
        self.assertEqual(plan["limits"]["max_seconds"], 1200)
        self.assertEqual(plan["limits"]["max_messages_per_user"], 40)
        self.assertEqual(plan["limits"]["max_history_days"], 30)

    def test_old_inbound_account_with_confirmed_tag_prevents_false_untagged(self):
        self.add_user("cross", account="a1")
        self.add_user("cross", account="a2", inbound=NOW - 45 * DAY)
        self.add_observation("cross", "natural", "absent", account="a1")
        self.add_observation("cross", "ad", "present", account="a2")
        plan = self.call("sample-plan", "--db", str(self.db),
                         "--account-ids", "a1,a2", "--now-ms", str(NOW))
        self.assertEqual([item["user_id"] for item in plan["groups"]["ad_tagged"]], ["cross"])
        self.assertEqual(plan["groups"]["ad_tagged"][0]["account_ids"], ["a1", "a2"])
        self.assertEqual(plan["groups"]["natural_untagged"], [])

    def test_out_of_scope_confirmed_tag_blocks_untagged_without_exposing_other_account(self):
        self.add_user("outscope", account="a1")
        self.add_observation("outscope", "natural", "absent", account="a1")
        self.add_observation("outscope", "ad", "present", account="a2")
        plan = self.call("sample-plan", "--db", str(self.db),
                         "--account-ids", "a1", "--now-ms", str(NOW))
        self.assertEqual(plan["groups"]["natural_untagged"], [])
        self.assertEqual(plan["counts"]["out_of_scope_confirmed_tag"], 1)
        self.assertNotIn("outscope", [item["user_id"] for item in plan["lookup_queue"]])
        self.assertNotIn("a2", json.dumps(plan, ensure_ascii=False))

    def test_scoped_account_known_only_from_platform_observation_is_still_checked(self):
        self.add_user("tagged_elsewhere", account="a1")
        self.add_observation("tagged_elsewhere", "natural", "absent", account="a1")
        self.add_observation("tagged_elsewhere", "ad", "present", account="a2")
        self.add_user("unknown_elsewhere", account="a1")
        self.add_observation("unknown_elsewhere", "natural", "absent", account="a1")
        self.add_observation("unknown_elsewhere", "unknown", "unknown", account="a2")
        plan = self.call("sample-plan", "--db", str(self.db),
                         "--account-ids", "a1,a2", "--now-ms", str(NOW))
        self.assertEqual([item["user_id"] for item in plan["groups"]["ad_tagged"]], ["tagged_elsewhere"])
        self.assertEqual(plan["groups"]["ad_tagged"][0]["account_ids"], ["a1", "a2"])
        self.assertNotIn("unknown_elsewhere", [item["user_id"] for item in plan["groups"]["natural_untagged"]])
        queued = next(item for item in plan["lookup_queue"] if item["user_id"] == "unknown_elsewhere")
        self.assertEqual(queued["account_ids"], ["a1", "a2"])

    def test_tagged_sample_uses_first_tag_discovery_and_excludes_later_chat(self):
        self.add_user("tagged")
        self.add_observation("tagged", "ad", "present", at=NOW - 2 * DAY)
        self.add_observation("tagged", "ad", "present", at=NOW - DAY)
        plan = self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1", "--now-ms", str(NOW))
        self.assertEqual(plan["groups"]["ad_tagged"][0]["observed_at_ms"], NOW - 2 * DAY)
        plan_path = Path(self.temp.name) / "plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        transcript_path = Path(self.temp.name) / "sessions.jsonl"
        transcript_path.write_text(json.dumps({
            "user_id": "tagged", "account_id": "a1", "uid_verified": True,
            "account_verified": True, "captured_at_ms": NOW,
            "history_complete": True,
            "messages": [
                {"at_ms": NOW - 3 * DAY, "sender": "customer", "text": "课程多少钱？"},
                {"at_ms": NOW - DAY, "sender": "customer", "text": "标签之后讨论了效果？"},
            ],
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        report = self.call("report", "--plan", str(plan_path), "--transcripts", str(transcript_path))
        group = report["groups"]["ad_tagged"]
        self.assertEqual(group["topic_mentions"].get("price"), 1)
        self.assertNotIn("effect", group["topic_mentions"])
        self.assertNotIn("标签之后", json.dumps(group, ensure_ascii=False))

    def test_tagged_sample_with_only_after_tag_chat_is_not_covered(self):
        self.add_user("tagged_late")
        self.add_observation("tagged_late", "ad", "present", at=NOW - 2 * DAY)
        plan = self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1", "--now-ms", str(NOW))
        plan_path = Path(self.temp.name) / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        transcript_path = Path(self.temp.name) / "sessions.jsonl"
        transcript_path.write_text(json.dumps({
            "user_id": "tagged_late", "account_id": "a1", "uid_verified": True,
            "account_verified": True, "captured_at_ms": NOW, "history_complete": True,
            "messages": [{"at_ms": NOW - DAY, "sender": "customer", "text": "标签之后才问价格？"}],
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        group = self.call("report", "--plan", str(plan_path),
                          "--transcripts", str(transcript_path))["groups"]["ad_tagged"]
        self.assertEqual(group["with_verified_transcripts"], 0)
        self.assertEqual(group["missing_or_unverified"], 1)
        self.assertNotIn("price", group["topic_mentions"])

    def test_report_uses_only_verified_bounded_transcripts_and_redacts_contact(self):
        self.add_user("ad_yes")
        self.add_observation("ad_yes", "ad", "present")
        plan = self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1", "--now-ms", str(NOW))
        plan_path = Path(self.temp.name) / "plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        transcript_path = Path(self.temp.name) / "transcripts.jsonl"
        transcript_path.write_text(
            json.dumps({
                "user_id": "ad_yes", "account_id": "a1", "uid_verified": True,
                "account_verified": True, "captured_at_ms": NOW,
                "history_complete": True,
                "messages": [
                    {"at_ms": NOW - DAY - 2000, "sender": "customer", "text": "课程多少钱？加我微信 abc123"},
                    {"at_ms": NOW - DAY - 1000, "sender": "staff", "text": "请留手机号 13800138000"},
                ],
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        report = self.call("report", "--plan", str(plan_path), "--transcripts", str(transcript_path))
        group = report["groups"]["ad_tagged"]
        self.assertEqual(group["with_verified_transcripts"], 1)
        self.assertEqual(group["topic_mentions"]["price"], 1)
        self.assertEqual(group["staff_expression_patterns"]["contact_request"], 1)
        self.assertEqual(report["comparison"]["ad"]["tagged"]["contact_request"], 1)
        self.assertTrue(group["candidate_changes"])
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("13800138000", serialized)
        self.assertNotIn("abc123", serialized)
        self.assertIn("[手机号]", serialized)
        self.assertIn("[微信号]", serialized)
        self.assertTrue(any("无法证明" in note for note in report["limitations"]))
        self.assertTrue(all("导致" not in item["proposal_for_review"] for item in group["candidate_changes"]))

    def test_report_marks_missing_and_unverified_chat_unknown(self):
        self.add_user("natural_no")
        self.add_observation("natural_no", "natural", "absent")
        plan = self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1", "--now-ms", str(NOW))
        plan_path = Path(self.temp.name) / "plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        transcript_path = Path(self.temp.name) / "transcripts.jsonl"
        transcript_path.write_text(json.dumps({
            "user_id": "natural_no", "account_id": "a1", "uid_verified": False,
            "account_verified": True, "captured_at_ms": NOW,
            "history_complete": False, "messages": [],
        }) + "\n", encoding="utf-8")
        report = self.call("report", "--plan", str(plan_path), "--transcripts", str(transcript_path))
        group = report["groups"]["natural_untagged"]
        self.assertEqual(group["planned"], 1)
        self.assertEqual(group["sampled"], 0)
        self.assertEqual(group["with_verified_transcripts"], 0)
        self.assertEqual(group["current_label_unverified_excluded"], 1)
        self.assertEqual(group["candidate_changes"], [])

    def test_report_rejects_tampered_limits_in_plan(self):
        self.add_user("ad_yes")
        self.add_observation("ad_yes", "ad", "present")
        plan = self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1", "--now-ms", str(NOW))
        plan["limits"]["max_messages_per_user"] = 4000
        plan_path = Path(self.temp.name) / "bad-plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        transcript_path = Path(self.temp.name) / "empty.jsonl"
        transcript_path.write_text("", encoding="utf-8")
        run = subprocess.run([sys.executable, str(SCRIPT), "report", "--plan", str(plan_path),
                              "--transcripts", str(transcript_path)],
                             text=True, capture_output=True, check=False)
        self.assertEqual(run.returncode, 2)
        self.assertIn("max_messages", run.stderr)

    def test_report_writes_readable_markdown_without_raw_contact(self):
        self.add_user("ad_yes")
        self.add_observation("ad_yes", "ad", "present")
        plan = self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1", "--now-ms", str(NOW))
        plan_path = Path(self.temp.name) / "plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        transcript_path = Path(self.temp.name) / "sessions.jsonl"
        transcript_path.write_text(json.dumps({
            "user_id": "ad_yes", "account_id": "a1", "uid_verified": True,
            "account_verified": True, "captured_at_ms": NOW, "history_complete": True,
            "messages": [{"at_ms": NOW - DAY, "sender": "customer",
                          "text": "价格是多少？我手机号 13800138000"}],
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        markdown_path = Path(self.temp.name) / "review.md"
        self.call("report", "--plan", str(plan_path), "--transcripts", str(transcript_path),
                  "--markdown-output", str(markdown_path))
        markdown = markdown_path.read_text(encoding="utf-8")
        self.assertIn("广告｜有留客资标签", markdown)
        self.assertIn("候选改法", markdown)
        self.assertIn("[手机号]", markdown)
        self.assertNotIn("13800138000", markdown)
        self.assertEqual(stat.S_IMODE(markdown_path.stat().st_mode), 0o600)

    def test_json_output_uses_private_file_and_new_directory_permissions(self):
        self.add_user("private")
        nested = Path(self.temp.name) / "new" / "analysis"
        output = nested / "plan.json"
        self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1",
                  "--now-ms", str(NOW), "--output", str(output))
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(nested.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(nested.parent.stat().st_mode), 0o700)
        output.chmod(0o644)
        self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1",
                  "--now-ms", str(NOW), "--output", str(output))
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_enrich_plan_moves_verified_lookup_into_tagged_report(self):
        self.add_user("lookup")
        plan = self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1", "--now-ms", str(NOW))
        self.assertEqual(plan["groups"]["ad_tagged"], [])
        plan_path = Path(self.temp.name) / "plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        transcript_path = Path(self.temp.name) / "lookup.jsonl"
        transcript_path.write_text(json.dumps({
            "user_id": "lookup", "account_id": "a1", "uid_verified": True,
            "account_verified": True, "captured_at_ms": NOW + 1000,
            "history_complete": True,
            "account_reviews": [{"user_id": "lookup", "account_id": "a1",
                                 "uid_verified": True, "account_verified": True,
                                 "panel_loaded": True, "label": "lead", "source": "ad",
                                 "attribution_note_state": "concrete", "observed_at_ms": NOW}],
            "messages": [{"at_ms": NOW - 2 * DAY, "sender": "customer", "text": "费用多少？"}],
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        enriched = self.call("enrich-plan", "--db", str(self.db), "--plan", str(plan_path),
                             "--transcripts", str(transcript_path))
        self.assertEqual([item["user_id"] for item in enriched["groups"]["ad_tagged"]], ["lookup"])
        self.assertEqual(enriched["enrichment"]["added_by_group"]["ad_tagged"], 1)
        enriched_path = Path(self.temp.name) / "enriched.json"
        enriched_path.write_text(json.dumps(enriched, ensure_ascii=False), encoding="utf-8")
        report = self.call("report", "--plan", str(enriched_path), "--transcripts", str(transcript_path))
        self.assertEqual(report["groups"]["ad_tagged"]["with_verified_transcripts"], 1)
        self.assertEqual(report["groups"]["ad_tagged"]["topic_mentions"]["price"], 1)

    def test_enrich_untagged_requires_all_scoped_accounts_and_48_hours(self):
        self.add_user("multi", account="a1", inbound=NOW - 5 * DAY)
        self.add_user("multi", account="a2", inbound=NOW - 4 * DAY)
        self.add_user("recent", account="a1", inbound=NOW - DAY)
        plan = self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1,a2", "--now-ms", str(NOW))
        plan_path = Path(self.temp.name) / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        transcript_path = Path(self.temp.name) / "lookup.jsonl"
        def snapshot(uid, account, reviews):
            return {"user_id": uid, "account_id": account, "uid_verified": True,
                    "account_verified": True, "captured_at_ms": NOW + 1000,
                    "history_complete": True, "account_reviews": reviews,
                    "messages": [{"at_ms": NOW - DAY, "sender": "customer", "text": "课程怎么安排？"}]}
        def review(uid, account, *, verified=True):
            return {"user_id": uid, "account_id": account, "uid_verified": verified,
                    "account_verified": True, "panel_loaded": True, "label": "none",
                    "source": "natural" if account == "a2" else "unknown",
                    "attribution_note_state": "empty" if account == "a2" else "unknown",
                    "observed_at_ms": NOW}
        records = [
            snapshot("multi", "a2", [review("multi", "a2")]),
            snapshot("recent", "a1", [review("recent", "a1")]),
        ]
        transcript_path.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
        partial = self.call("enrich-plan", "--db", str(self.db), "--plan", str(plan_path),
                            "--transcripts", str(transcript_path))
        self.assertEqual(partial["groups"]["natural_untagged"], [])
        records[0]["account_reviews"].append(review("multi", "a1", verified=False))
        transcript_path.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
        unverified = self.call("enrich-plan", "--db", str(self.db), "--plan", str(plan_path),
                               "--transcripts", str(transcript_path))
        self.assertEqual(unverified["groups"]["natural_untagged"], [])
        records[0]["account_reviews"][-1]["uid_verified"] = True
        transcript_path.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
        complete = self.call("enrich-plan", "--db", str(self.db), "--plan", str(plan_path),
                             "--transcripts", str(transcript_path))
        self.assertEqual([item["user_id"] for item in complete["groups"]["natural_untagged"]], ["multi"])
        self.assertNotIn("recent", [item["user_id"] for group in complete["groups"].values() for item in group])

    def test_enrich_rechecks_out_of_scope_confirmed_tag_after_plan_creation(self):
        self.add_user("outscope")
        plan = self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1", "--now-ms", str(NOW))
        plan_path = Path(self.temp.name) / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        self.add_observation("outscope", "ad", "present", account="a2", at=NOW + 1000)
        transcript_path = Path(self.temp.name) / "lookup.jsonl"
        transcript_path.write_text(json.dumps({
            "user_id": "outscope", "account_id": "a1", "uid_verified": True,
            "account_verified": True, "captured_at_ms": NOW + 2000, "history_complete": True,
            "account_reviews": [{"user_id": "outscope", "account_id": "a1",
                                 "uid_verified": True, "account_verified": True,
                                 "panel_loaded": True, "label": "none", "source": "natural",
                                 "attribution_note_state": "empty", "observed_at_ms": NOW + 1000}],
            "messages": [{"at_ms": NOW - DAY, "sender": "customer", "text": "您好"}],
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        enriched = self.call("enrich-plan", "--db", str(self.db), "--plan", str(plan_path),
                             "--transcripts", str(transcript_path))
        self.assertEqual(enriched["groups"]["natural_untagged"], [])
        self.assertEqual(enriched["enrichment"]["deferred_reasons"]["confirmed_tag_out_of_scope"], 1)
        self.assertNotIn("a2", json.dumps(enriched, ensure_ascii=False))

    def test_enrich_respects_group_cap_and_newer_inbound_after_plan(self):
        for i in range(12):
            self.add_user(f"lookup{i:02d}")
        plan = self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1", "--now-ms", str(NOW))
        plan_path = Path(self.temp.name) / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE historical_user_accounts SET latest_in_at_ms=? WHERE user_id='lookup11'",
                        (NOW - DAY,))
        transcript_path = Path(self.temp.name) / "lookup.jsonl"
        records = []
        for i in range(12):
            uid = f"lookup{i:02d}"
            records.append({
                "user_id": uid, "account_id": "a1", "uid_verified": True,
                "account_verified": True, "captured_at_ms": NOW + 1000,
                "history_complete": True,
                "account_reviews": [{"user_id": uid, "account_id": "a1",
                                     "uid_verified": True, "account_verified": True,
                                     "panel_loaded": True, "label": "none", "source": "natural",
                                     "attribution_note_state": "empty", "observed_at_ms": NOW}],
                "messages": [{"at_ms": NOW - 2 * DAY, "sender": "customer", "text": "你好"}],
            })
        transcript_path.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
        enriched = self.call("enrich-plan", "--db", str(self.db), "--plan", str(plan_path),
                             "--transcripts", str(transcript_path))
        selected = [item["user_id"] for item in enriched["groups"]["natural_untagged"]]
        self.assertEqual(len(selected), 10)
        self.assertNotIn("lookup11", selected)
        self.assertEqual(enriched["enrichment"]["deferred_reasons"]["label_or_account_context_unknown_or_recent"], 1)
        self.assertEqual(enriched["enrichment"]["deferred_reasons"]["group_full"], 1)

    def test_new_verified_tag_invalidates_original_untagged_sample(self):
        self.add_user("changed")
        self.add_observation("changed", "natural", "absent", at=NOW - DAY)
        plan = self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1", "--now-ms", str(NOW))
        self.assertEqual([item["user_id"] for item in plan["groups"]["natural_untagged"]], ["changed"])
        plan_path = Path(self.temp.name) / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        transcript_path = Path(self.temp.name) / "capture.jsonl"
        transcript_path.write_text(json.dumps({
            "user_id": "changed", "account_id": "a1", "uid_verified": True,
            "account_verified": True, "captured_at_ms": NOW + 1000,
            "history_complete": True,
            "account_reviews": [{"user_id": "changed", "account_id": "a1",
                                 "uid_verified": True, "account_verified": True,
                                 "panel_loaded": True, "label": "lead", "source": "natural",
                                 "attribution_note_state": "empty", "observed_at_ms": NOW}],
            "messages": [{"at_ms": NOW - 2 * DAY, "sender": "customer", "text": "课程效果如何？"}],
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        report = self.call("report", "--plan", str(plan_path), "--transcripts", str(transcript_path))
        self.assertEqual(report["groups"]["natural_untagged"]["with_verified_transcripts"], 0)
        self.assertEqual(report["groups"]["natural_untagged"]["stale_group_excluded"], 1)
        enriched = self.call("enrich-plan", "--db", str(self.db), "--plan", str(plan_path),
                             "--transcripts", str(transcript_path))
        self.assertEqual(enriched["groups"]["natural_untagged"], [])
        self.assertEqual(enriched["enrichment"]["existing_samples_invalidated_by_new_tag"], 1)

    def test_original_untagged_requires_current_all_account_verification(self):
        self.add_user("old", account="a1")
        self.add_user("old", account="a2")
        self.add_observation("old", "natural", "absent", account="a1")
        self.add_observation("old", "ad", "absent", account="a2")
        plan = self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1,a2", "--now-ms", str(NOW))
        self.assertEqual(plan["groups"]["natural_untagged"][0]["account_ids"], ["a1", "a2"])
        plan_path = Path(self.temp.name) / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        transcript_path = Path(self.temp.name) / "sessions.jsonl"
        old_chat = {
            "user_id": "old", "account_id": "a1", "uid_verified": True,
            "account_verified": True, "captured_at_ms": NOW - 1000, "history_complete": True,
            "messages": [{"at_ms": NOW - 2 * DAY, "sender": "customer", "text": "课程费用？"}],
        }
        transcript_path.write_text(json.dumps(old_chat, ensure_ascii=False) + "\n", encoding="utf-8")
        stale = self.call("report", "--plan", str(plan_path), "--transcripts", str(transcript_path))
        self.assertEqual(stale["groups"]["natural_untagged"]["with_verified_transcripts"], 0)
        self.assertEqual(stale["groups"]["natural_untagged"]["current_label_unverified_excluded"], 1)

        partial = {**old_chat, "captured_at_ms": NOW + 500,
                   "account_reviews": [{"user_id": "old", "account_id": "a1",
                                        "uid_verified": True, "account_verified": True,
                                        "panel_loaded": True, "label": "none", "source": "natural",
                                        "attribution_note_state": "empty", "observed_at_ms": NOW + 500}]}
        transcript_path.write_text("\n".join(json.dumps(x, ensure_ascii=False)
                                             for x in (old_chat, partial)) + "\n", encoding="utf-8")
        partial_report = self.call("report", "--plan", str(plan_path), "--transcripts", str(transcript_path))
        self.assertEqual(partial_report["groups"]["natural_untagged"]["current_label_unverified_excluded"], 1)

        failure = {"record_type": "capture_failure", "user_id": "old", "account_id": "a1",
                   "captured_at_ms": NOW + 1000, "status": "deferred",
                   "reason": "account_or_history_coverage_incomplete", "account_ids": ["a1", "a2"]}
        transcript_path.write_text("\n".join(json.dumps(x, ensure_ascii=False)
                                             for x in (old_chat, failure)) + "\n", encoding="utf-8")
        failed_report = self.call("report", "--plan", str(plan_path),
                                  "--transcripts", str(transcript_path))
        self.assertEqual(failed_report["groups"]["natural_untagged"]["current_label_unverified_excluded"], 1)
        self.assertEqual(failed_report["groups"]["natural_untagged"]["sampled"], 0)
        enriched = self.call("enrich-plan", "--db", str(self.db), "--plan", str(plan_path),
                             "--transcripts", str(transcript_path))
        self.assertEqual(enriched["groups"]["natural_untagged"], [])
        self.assertEqual(enriched["enrichment"]["existing_samples_deferred_without_current_check"], 1)
        enriched_path = Path(self.temp.name) / "enriched.json"
        enriched_path.write_text(json.dumps(enriched), encoding="utf-8")
        markdown_path = Path(self.temp.name) / "analysis.md"
        excluded_report = self.call("report", "--plan", str(enriched_path),
                                    "--transcripts", str(transcript_path),
                                    "--markdown-output", str(markdown_path))
        self.assertEqual(excluded_report["enrichment"]["existing_samples_deferred_without_current_check"], 1)
        self.assertIn("本次标签未完成核验 1 人", markdown_path.read_text(encoding="utf-8"))

        fresh = {**old_chat, "captured_at_ms": NOW + 2000,
                 "account_reviews": [
                     {"user_id": "old", "account_id": account, "uid_verified": True,
                      "account_verified": True, "panel_loaded": True, "label": "none",
                      "source": source, "attribution_note_state": note,
                      "observed_at_ms": NOW + 2000}
                     for account, source, note in (("a1", "natural", "empty"), ("a2", "ad", "concrete"))]}
        transcript_path.write_text("\n".join(json.dumps(x, ensure_ascii=False)
                                             for x in (old_chat, failure, fresh)) + "\n", encoding="utf-8")
        checked = self.call("enrich-plan", "--db", str(self.db), "--plan", str(plan_path),
                            "--transcripts", str(transcript_path))
        self.assertEqual([item["user_id"] for item in checked["groups"]["natural_untagged"]], ["old"])
        current = self.call("report", "--plan", str(plan_path), "--transcripts", str(transcript_path))
        self.assertEqual(current["groups"]["natural_untagged"]["sampled"], 1)
        self.assertEqual(current["groups"]["natural_untagged"]["with_verified_transcripts"], 1)

    def test_current_untagged_with_no_messages_in_window_is_a_gap(self):
        self.add_user("empty")
        self.add_observation("empty", "natural", "absent")
        plan = self.call("sample-plan", "--db", str(self.db), "--account-ids", "a1", "--now-ms", str(NOW))
        plan_path = Path(self.temp.name) / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        transcript_path = Path(self.temp.name) / "sessions.jsonl"
        transcript_path.write_text(json.dumps({
            "user_id": "empty", "account_id": "a1", "uid_verified": True,
            "account_verified": True, "captured_at_ms": NOW + 1000, "history_complete": True,
            "account_reviews": [{"user_id": "empty", "account_id": "a1",
                                 "uid_verified": True, "account_verified": True,
                                 "panel_loaded": True, "label": "none", "source": "natural",
                                 "attribution_note_state": "empty", "observed_at_ms": NOW + 1000}],
            "messages": [{"at_ms": NOW - 40 * DAY, "sender": "customer", "text": "很久以前"}],
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        group = self.call("report", "--plan", str(plan_path),
                          "--transcripts", str(transcript_path))["groups"]["natural_untagged"]
        self.assertEqual(group["with_verified_transcripts"], 0)
        self.assertEqual(group["missing_or_unverified"], 1)


if __name__ == "__main__":
    unittest.main()
