#!/usr/bin/env python3
"""Import a current XHS user-list CSV into the private daily follow-up ledger.

The CSV discovers UIDs and supplies conservative lead protection. It never
authorizes a send; each account conversation still needs browser review.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from daily_followup_state import DEFAULT_DB, connect, epoch_ms

SHANGHAI = ZoneInfo("Asia/Shanghai")
REQUIRED = {
    "用户ID", "用户昵称", "归属账号", "最新进线时间", "最近开口时间", "最近留资时间",
    "手机号", "微信号", "留资方式", "用户类型",
}


def clean(value: object) -> str:
    value = str(value or "").strip()
    return "" if value == "-" else value


def date_ms(value: object) -> int | None:
    value = clean(value)
    if not value:
        return None
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return int(datetime.strptime(value, pattern).replace(tzinfo=SHANGHAI).astimezone(timezone.utc).timestamp() * 1000)
        except ValueError:
            pass
    return None


def mapping_from_config(config_path: Path) -> dict[str, str]:
    if not config_path.is_file():
        return {}
    config = json.loads(config_path.read_text(encoding="utf-8"))
    mapping: dict[str, str] = {}
    for item in config.get("accounts", []):
        account_id = clean(item.get("id"))
        names = [account_id, clean(item.get("name"))]
        aliases = item.get("csv_names", [])
        if not isinstance(aliases, list):
            raise ValueError("csv_names_must_be_list")
        names.extend(clean(name) for name in aliases)
        if not account_id or not clean(item.get("name")):
            raise ValueError("managed_account_id_or_name_missing")
        if "替换为" in account_id or "替换为" in clean(item.get("name")):
            raise ValueError("managed_account_placeholder_not_allowed")
        for name in filter(None, names):
            if name in mapping and mapping[name] != account_id:
                raise ValueError("account_name_conflict:" + name)
            mapping[name] = account_id
    return mapping


def _union_json(existing: str | None, new: str) -> str:
    try:
        values = json.loads(existing or "[]")
    except json.JSONDecodeError:
        values = []
    return json.dumps(list(dict.fromkeys([*values, new])), ensure_ascii=False)


def import_csv(conn, source_file: Path, cutoff_date: str, account_mapping: dict[str, str], *, allow_historical: bool = True, now_ms: int | None = None) -> dict:
    source_file = source_file.expanduser().resolve()
    if not source_file.is_file():
        raise ValueError("user_list_file_missing")
    try:
        cutoff = date.fromisoformat(cutoff_date)
    except ValueError:
        raise ValueError("invalid_cutoff_date") from None
    today = datetime.now(SHANGHAI).date()
    if cutoff > today:
        raise ValueError("cutoff_date_in_future")
    if not allow_historical and cutoff < today - timedelta(days=1):
        raise ValueError("current_user_list_required_cutoff_at_least_yesterday")
    raw = source_file.read_bytes()
    mapping_snapshot = json.dumps(account_mapping, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")).encode("utf-8")
    import_id = "daily-" + hashlib.sha256(raw + cutoff_date.encode("ascii") + mapping_snapshot).hexdigest()[:24]
    if conn.execute("SELECT 1 FROM historical_imports WHERE import_id=?", (import_id,)).fetchone():
        return {"ok": True, "import_id": import_id, "already_imported": True}
    decoded = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(decoded, newline=""))
    missing = sorted(REQUIRED - set(reader.fieldnames or []))
    if missing:
        raise ValueError("missing_required_columns:" + ",".join(missing))
    records = list(reader)
    grouped: dict[tuple[str, str], dict] = {}
    unmapped = defaultdict(int)
    unique_users = set()
    deleted_users = set()
    for row in records:
        user_id = clean(row.get("用户ID"))
        if not user_id:
            continue
        unique_users.add(user_id)
        account_name = clean(row.get("归属账号"))
        account_id = account_mapping.get(account_name, "")
        if not account_id:
            unmapped[account_name or "<空账号>"] += 1
        key = (user_id, account_name)
        item = grouped.setdefault(key, {
            "user_id": user_id, "account_name": account_name, "account_id": account_id,
            "display_name": "", "latest_in_at_ms": None, "latest_open_at_ms": None,
            "latest_lead_at_ms": None, "phone_present": 0, "wechat_present": 0,
            "lead_status_from_import": "no_lead", "deleted": 0, "user_type": "",
        })
        item["display_name"] = item["display_name"] or clean(row.get("用户昵称"))
        item["user_type"] = item["user_type"] or clean(row.get("用户类型"))
        for field, column in (("最新进线时间", "latest_in_at_ms"), ("最近开口时间", "latest_open_at_ms"), ("最近留资时间", "latest_lead_at_ms")):
            parsed = date_ms(row.get(field))
            if parsed is not None:
                item[column] = max(item[column] or 0, parsed)
        item["phone_present"] |= int(bool(clean(row.get("手机号"))))
        item["wechat_present"] |= int(bool(clean(row.get("微信号"))))
        if item["latest_lead_at_ms"] or item["phone_present"] or item["wechat_present"] or clean(row.get("留资方式")):
            item["lead_status_from_import"] = "has_lead"
        if "已注销" in clean(row.get("用户昵称")) or "已注销" in clean(row.get("用户类型")):
            item["deleted"] = 1
            deleted_users.add(user_id)
    imported_at = epoch_ms() if now_ms is None else now_ms
    with conn:
        for item in grouped.values():
            old = conn.execute(
                "SELECT * FROM historical_user_accounts WHERE user_id=? AND account_name=?",
                (item["user_id"], item["account_name"]),
            ).fetchone()
            if old is None:
                conn.execute(
                    """INSERT INTO historical_user_accounts
                       (user_id,account_name,account_id,display_name,latest_in_at_ms,latest_open_at_ms,
                        latest_lead_at_ms,phone_present,wechat_present,lead_status_from_import,deleted,
                        user_type,source_import_ids_json,updated_at_ms)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item["user_id"], item["account_name"], item["account_id"], item["display_name"],
                     item["latest_in_at_ms"], item["latest_open_at_ms"], item["latest_lead_at_ms"],
                     item["phone_present"], item["wechat_present"], item["lead_status_from_import"],
                     item["deleted"], item["user_type"], json.dumps([import_id]), imported_at),
                )
                continue
            if old["account_id"] and item["account_id"] and old["account_id"] != item["account_id"]:
                raise ValueError("account_id_conflict_for_csv_name:" + item["account_name"])
            conn.execute(
                """UPDATE historical_user_accounts SET account_id=?,display_name=?,
                   latest_in_at_ms=?,latest_open_at_ms=?,latest_lead_at_ms=?,phone_present=?,
                   wechat_present=?,lead_status_from_import=?,deleted=?,user_type=?,
                   source_import_ids_json=?,updated_at_ms=? WHERE user_id=? AND account_name=?""",
                (old["account_id"] or item["account_id"], item["display_name"] or old["display_name"],
                 max(old["latest_in_at_ms"] or 0, item["latest_in_at_ms"] or 0) or None,
                 max(old["latest_open_at_ms"] or 0, item["latest_open_at_ms"] or 0) or None,
                 max(old["latest_lead_at_ms"] or 0, item["latest_lead_at_ms"] or 0) or None,
                 max(old["phone_present"], item["phone_present"]), max(old["wechat_present"], item["wechat_present"]),
                 "has_lead" if old["lead_status_from_import"] == "has_lead" or item["lead_status_from_import"] == "has_lead" else "no_lead",
                 max(old["deleted"], item["deleted"]), item["user_type"] or old["user_type"],
                 _union_json(old["source_import_ids_json"], import_id), imported_at,
                 item["user_id"], item["account_name"]),
            )
        lead_users = len({item["user_id"] for item in grouped.values() if item["lead_status_from_import"] == "has_lead"})
        protected = {item["user_id"] for item in grouped.values() if item["lead_status_from_import"] == "has_lead"}
        target_users = len({item["user_id"] for item in grouped.values()
                            if item["user_id"] not in protected})
        conn.execute(
            """INSERT INTO historical_imports
               (import_id,source_file,cutoff_date,imported_at_ms,row_count,unique_user_ids,lead_users,
                targetable_no_lead_users,deleted_users) VALUES (?,?,?,?,?,?,?,?,?)""",
            (import_id, str(source_file), cutoff_date, imported_at, len(records), len(unique_users),
             lead_users, target_users, len(deleted_users)),
        )
    return {"ok": True, "import_id": import_id, "already_imported": False,
            "cutoff_date": cutoff_date, "row_count": len(records), "unique_users": len(unique_users),
            "lead_users_protective": lead_users, "candidate_users": target_users,
            "unmapped_accounts": dict(sorted(unmapped.items()))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--file", type=Path, required=True, action="append")
    parser.add_argument("--cutoff-date", default=datetime.now(SHANGHAI).date().isoformat())
    parser.add_argument("--allow-historical", action="store_true")
    args = parser.parse_args()
    try:
        mapping = mapping_from_config(args.config) if args.config else {}
        with connect(args.db) as conn:
            results = [import_csv(conn, source, args.cutoff_date, mapping, allow_historical=True) for source in args.file]
            result = {"ok": True, "source_import_ids": [r["import_id"] for r in results], "imports": results}
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, OSError, UnicodeError, csv.Error, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
