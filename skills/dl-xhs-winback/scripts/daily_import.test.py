import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from daily_followup_state import connect
from daily_import import import_csv, mapping_from_config


HEADERS = ["用户ID", "用户昵称", "归属账号", "最新进线时间", "最近开口时间", "最近留资时间", "手机号", "微信号", "留资方式", "用户类型"]


class DailyImportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "state.sqlite3"
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"accounts": [{"id": "acc-a", "name": "账号甲", "csv_names": ["甲别名"]}, {"id": "acc-b", "name": "账号乙"}]}), encoding="utf-8")
        self.mapping = mapping_from_config(self.config)
        self.cutoff = (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)).isoformat()

    def tearDown(self):
        self.temp.cleanup()

    def make_csv(self, rows):
        path = self.root / "用户列表.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=HEADERS)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def row(self, uid, account, **kwargs):
        values = {key: "" for key in HEADERS}
        values.update({"用户ID": uid, "用户昵称": "客户", "归属账号": account,
                       "最新进线时间": self.cutoff + " 10:00:00", "用户类型": "普通用户"})
        values.update(kwargs)
        return values

    def test_import_maps_alias_preserves_lead_protection_and_reports_unknown_account(self):
        path = self.make_csv([
            self.row("u1", "甲别名"),
            self.row("u1", "账号乙", **{"留资方式": "企业微信"}),
            self.row("u2", "未知账号"),
            self.row("u3", "账号甲", **{"用户昵称": "已注销用户"}),
        ])
        with connect(self.db) as conn:
            result = import_csv(conn, path, self.cutoff, self.mapping)
            self.assertEqual(result["candidate_users"], 2)
            self.assertEqual(result["unmapped_accounts"], {"未知账号": 1})
            aliases = conn.execute("SELECT account_id,lead_status_from_import FROM historical_user_accounts WHERE user_id='u1' ORDER BY account_name").fetchall()
            self.assertEqual({item["account_id"] for item in aliases}, {"acc-a", "acc-b"})
            self.assertIn("has_lead", {item["lead_status_from_import"] for item in aliases})
            self.assertTrue(import_csv(conn, path, self.cutoff, self.mapping)["already_imported"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM historical_imports").fetchone()[0], 1)

    def test_older_import_does_not_clear_prior_lead_signal(self):
        path = self.make_csv([self.row("u1", "账号甲", **{"手机号": "13800000000"})])
        with connect(self.db) as conn:
            import_csv(conn, path, self.cutoff, self.mapping)
            path = self.make_csv([self.row("u1", "账号甲")])
            import_csv(conn, path, self.cutoff, self.mapping)
            status = conn.execute("SELECT lead_status_from_import FROM historical_user_accounts WHERE user_id='u1'").fetchone()[0]
            self.assertEqual(status, "has_lead")

    def test_historical_monthly_files_are_accepted(self):
        path = self.make_csv([self.row("u1", "账号甲")])
        stale = (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=3)).isoformat()
        with connect(self.db) as conn:
            self.assertTrue(import_csv(conn, path, stale, self.mapping)["ok"])
            self.assertTrue(import_csv(conn, path, stale, self.mapping, allow_historical=True)["ok"])

    def test_same_csv_can_be_reimported_after_account_mapping_is_repaired(self):
        path = self.make_csv([self.row("u1", "新确认账号")])
        with connect(self.db) as conn:
            first = import_csv(conn, path, self.cutoff, self.mapping)
            self.assertEqual(first["unmapped_accounts"], {"新确认账号": 1})
            repaired_mapping = {**self.mapping, "新确认账号": "acc-c"}
            second = import_csv(conn, path, self.cutoff, repaired_mapping)
            self.assertFalse(second["already_imported"])
            self.assertNotEqual(first["import_id"], second["import_id"])
            row = conn.execute("SELECT account_id FROM historical_user_accounts WHERE user_id='u1'").fetchone()
            self.assertEqual(row["account_id"], "acc-c")

    def test_missing_inbound_date_still_provides_a_uid_candidate(self):
        path = self.make_csv([self.row("u1", "账号甲", **{"最新进线时间": ""})])
        with connect(self.db) as conn:
            result = import_csv(conn, path, self.cutoff, self.mapping)
            self.assertEqual(result["candidate_users"], 1)

    def test_example_account_id_cannot_be_used_as_config(self):
        self.config.write_text(json.dumps({"accounts": [{"id": "替换为真实账号ID", "name": "示例账号"}]}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "placeholder"):
            mapping_from_config(self.config)


if __name__ == "__main__":
    unittest.main()
