#!/usr/bin/env python3
"""Persistent campaign, batching, and deduplication state for XHS follow-up."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


DEFAULT_DB = Path(
    os.environ.get(
        "XHS_FOLLOWUP_STATE_DB",
        "~/.codex/state/xhs-private-message-followup/state.sqlite3",
    )
).expanduser()


def _load_skill_config() -> dict:
    config_path = Path(
        os.environ.get(
            "XHS_FOLLOWUP_CONFIG",
            str(Path(__file__).resolve().parent.parent / "config.json"),
        )
    ).expanduser()
    if not config_path.is_file():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


_SKILL_CONFIG = _load_skill_config()
_CONFIG_ACCOUNTS = _SKILL_CONFIG.get("accounts", [])
VALID_LEAD = {"has_lead", "no_lead", "unknown"}
VALID_PARTY = {"customer", "peer", "unknown"}
VALID_REPLY = {"awaiting_customer", "awaiting_us", "unknown"}
VALID_NEGATIVE = {"safe", "blocked", "unknown"}
VALID_SEND = {"sent", "failed", "skipped", "uncertain"}
VALID_BATCH = {"draft", "approved", "in_progress", "completed", "stopped"}
VALID_BATCH_ITEM = {"pending", "sent", "failed", "skipped", "uncertain"}
VALID_COVERAGE_SCOPE = {"user_list", "month", "history", "combined"}
VALID_HISTORICAL_REVIEW = {"eligible", "ineligible", "retryable", "unknown", "skipped"}
FINAL_OR_ASSIGNED_BATCH_ITEM_STATES = {"pending", "sent", "skipped", "failed", "uncertain"}
UNCERTAIN_RESOLUTIONS = {"confirmed_sent", "confirmed_not_sent", "skip"}
FAILED_RESOLUTIONS = {"retry", "skip"}
UNCERTAIN_SEND_DETAILS = {
    "new_right_message_not_verified",
}
RESTORABLE_TECHNICAL_REASONS = {
    "missing_user_id",
    "not_visible_and_search_disabled",
    "search_input_count_0",
    "search_no_matching_uid",
    "search_result_scroller_missing",
    "search_identity_not_confirmed",
    "uid_search_no_matching_result",
    "uid_search_identity_not_confirmed",
    "uid_search_popup_identity_not_confirmed",
    "uid_search_result_identity_not_confirmed",
    "missing_saved_queue_user_id_for_uid_search",
    "missing_saved_queue_account_id_for_uid_search",
    "pre_send_identity_failed",
}
RESTORABLE_TECHNICAL_REASON_PREFIXES = (
    "uid_search_no_matching_result; list_fallback:",
    "uid_search_popup_count_",
    "uid_search_popup_click_failed:",
    "conversation_click_failed:",
    "can_send:candidate_not_found",
    "can_send:candidate_not_in_batch",
    "can_send:batch_campaign_mismatch",
    "can_send:batch_item_identity_mismatch",
    "list_fallback_",
    "list_fallback:",
    "pre_send_identity_failed:",
    "send_identity_not_confirmed_before_type",
    "send_input_",
    "send_button_",
)
MAX_BATCH_SIZE = 100
# 账号 ID/昵称从 config.json 的 "accounts" 字段读取（示例见 config.example.json）；
# 没有 config.json 或字段为空时，这里都是空集合，调用方必须显式传入账号 ID。
DEFAULT_ACCOUNT_IDS = [account["id"] for account in _CONFIG_ACCOUNTS if account.get("id")]
ACCOUNT_NAMES = {
    account["id"]: account["name"] for account in _CONFIG_ACCOUNTS if account.get("id") and account.get("name")
}
ACCOUNT_NAME_TO_ID = {name: account_id for account_id, name in ACCOUNT_NAMES.items()}
# "aliases" 是可选的历史/备用昵称到规范昵称的映射，同样从 config.json 读取，默认为空。
ACCOUNT_NAME_ALIASES = dict(_SKILL_CONFIG.get("aliases", {}))
for alias, canonical_name in ACCOUNT_NAME_ALIASES.items():
    if canonical_name in ACCOUNT_NAME_TO_ID:
        ACCOUNT_NAME_TO_ID[alias] = ACCOUNT_NAME_TO_ID[canonical_name]
CHINA_TZ = ZoneInfo("Asia/Shanghai")
STATUS_LABELS = {
    "pending": "待发送",
    "sent": "已发送",
    "skipped": "已跳过",
    "failed": "发送失败",
    "uncertain": "待人工核对",
}
MESSAGE_VERSION_CURRENT = "当前文案"
MESSAGE_VERSION_PREVIOUS = "历史文案"
MESSAGE_VERSION_UNKNOWN = "未知"
CONVERSATION_KEY_PREFIXES = {"Total", "Active", "Favorite"}
REASON_LABELS = {
    "": "",
    "uid_search_popup_identity_not_confirmed": "多条搜索结果里暂时没确认到授权账号",
    "uid_search_result_identity_not_confirmed": "搜索结果已打开，但暂时没确认到授权账号",
    "uid_search_visible_list_identity_not_confirmed": "左侧可见结果已打开，但暂时没确认到授权账号",
    "uid_search_identity_not_confirmed": "UID 搜索已打开会话，但身份暂时没确认",
    "uid_search_no_matching_result": "UID 搜索后没有匹配结果",
    "search_result_scroller_missing": "搜索结果列表没有稳定出现",
    "list_fallback_timeout_or_scroll_limit": "左侧列表兜底已到时间或滚动限制",
    "pre_send_identity_failed": "发送前身份确认失败",
    "missing_saved_queue_user_id_for_uid_search": "队列缺少 UID，不能搜索",
    "missing_saved_queue_account_id_for_uid_search": "队列缺少账号 ID，不能确认授权范围",
    "lead_status=has_lead": "已留资，不再发",
    "historical_import_has_lead": "历史线索表显示已留资，不再发",
    "lead_status=unknown": "留资状态不确定，保守跳过",
    "party_status=peer": "同行/服务商/推销方，不自动发",
    "party_status=unknown": "疑似同行/商业服务号，人工复核",
    "reply_status=awaiting_us": "客户已回复，轮到我方处理",
    "reply_status=unknown": "回复方向不确定，保守跳过",
    "negative_status=blocked": "有拒绝或负面信号，不自动发",
    "no_real_messages": "没有可判断的真实对话",
    "account_not_allowed": "队列账号不在本轮授权范围内",
    "located_account_not_allowed": "打开的会话账号不在本轮授权范围内",
    "new_right_message_not_verified": "发送后没有确认到新的我方消息，需要人工核对",
    "manual_review_confirmed_not_sent": "人工确认未发出，已放回待处理",
    "manual_review_confirmed_sent": "人工确认已发出",
    "manual_review_skip_uncertain": "人工决定跳过该不确定项",
    "manual_review_retry_failed": "人工决定重试失败项",
    "manual_review_skip_failed": "人工决定跳过失败项",
}
REASON_PREFIX_LABELS = (
    ("silence_less_than_", "距离上次消息不足 {hours} 小时"),
    ("uid_search_popup_count_", "浮层搜索结果数量异常"),
    ("uid_search_popup_click_failed:", "浮层搜索结果点击失败"),
    ("uid_search_popup_index_out_of_range_", "浮层搜索结果序号超出范围"),
    ("visible_conversation_click_failed:", "左侧会话点击失败"),
    ("conversation_click_failed:", "会话点击失败"),
    ("can_send:user_already_touched_or_uncertain_in_campaign", "本活动已触达或待核对，不重复发"),
    ("can_send:user_already_touched_or_uncertain_for_same_message", "同文案已触达或待核对，不重复发"),
    ("can_send:user_touched_or_uncertain_within_silence_window", "24 小时内已触达或待核对，不重复发"),
    ("can_send:batch_item_", "队列项已不是待发送状态"),
    ("can_send:", "发送许可检查未通过"),
    ("pre_send_identity_failed:right_panel_uid_mismatch", "右侧 UID 与队列 UID 不一致"),
    ("pre_send_identity_failed:right_panel_uid_missing", "右侧 UID 没读到"),
    ("pre_send_identity_failed:opened_other_account", "打开的是其他账号会话"),
    ("pre_send_identity_failed:expected_account_not_present", "页面消息里没确认到目标账号"),
    ("list_fallback:", "左侧列表兜底："),
)


def parse_conversation_key(key: str) -> tuple[str, str, str]:
    parts = str(key or "").split("-")
    if len(parts) < 2 or parts[0] not in CONVERSATION_KEY_PREFIXES or not parts[1]:
        return "", "", ""
    prefix = parts[0]
    user_id = parts[1]
    account_id = "-".join(parts[2:]) if len(parts) >= 3 else ""
    return prefix, user_id, account_id


def parse_account_ids(value: str | None) -> list[str]:
    if value is None or not str(value).strip():
        return list(DEFAULT_ACCOUNT_IDS)
    raw = str(value).replace("\n", ",").replace(";", ",")
    ids = []
    seen = set()
    for part in raw.split(","):
        account_id = part.strip()
        if not account_id or account_id in seen:
            continue
        seen.add(account_id)
        ids.append(account_id)
    return ids


def normalize_coverage_scope(value: str | None) -> str:
    scope = str(value or "user_list").strip().lower()
    aliases = {
        "latest-user-list": "user_list",
        "latest_user_list": "user_list",
        "user-list": "user_list",
        "userlist": "user_list",
        "csv": "user_list",
        "uid": "user_list",
        "uid-review": "user_list",
        "uid_review": "user_list",
        "current-month": "month",
        "current_month": "month",
        "monthly": "month",
        "history-only": "history",
        "historical": "history",
        "all": "combined",
        "both": "combined",
        "month+history": "combined",
    }
    scope = aliases.get(scope, scope)
    if scope not in VALID_COVERAGE_SCOPE:
        emit({
            "ok": False,
            "error": "invalid_coverage_scope",
            "allowed": sorted(VALID_COVERAGE_SCOPE),
        }, 2)
    return scope


def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def emit(payload: dict, exit_code: int = 0) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    raise SystemExit(exit_code)


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA busy_timeout=30000;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS campaigns (
            campaign_id TEXT PRIMARY KEY,
            message TEXT NOT NULL,
            message_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('draft','approved','completed','stopped')),
            min_silence_hours INTEGER NOT NULL DEFAULT 24,
            created_at_ms INTEGER NOT NULL,
            approved_at_ms INTEGER
        );
        CREATE TABLE IF NOT EXISTS candidates (
            campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
            conversation_key TEXT NOT NULL,
            user_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            lead_status TEXT NOT NULL,
            party_status TEXT NOT NULL,
            reply_status TEXT NOT NULL,
            negative_status TEXT NOT NULL,
            last_message_at_ms INTEGER NOT NULL,
            evidence TEXT NOT NULL DEFAULT '',
            eligible INTEGER NOT NULL,
            exclusion_reason TEXT NOT NULL DEFAULT '',
            updated_at_ms INTEGER NOT NULL,
            PRIMARY KEY (campaign_id, conversation_key)
        );
        CREATE TABLE IF NOT EXISTS send_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL DEFAULT '',
            campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
            conversation_key TEXT NOT NULL,
            user_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('sent','failed','skipped','uncertain')),
            detail TEXT NOT NULL DEFAULT '',
            attempted_at_ms INTEGER NOT NULL,
            message_hash_snapshot TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_attempt_user_time
            ON send_attempts(user_id, attempted_at_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_candidate_campaign_eligible
            ON candidates(campaign_id, eligible);
        """
    )
    ensure_column(conn, "campaigns", "batch_size", "INTEGER NOT NULL DEFAULT 100")
    ensure_column(conn, "campaigns", "expires_at_ms", "INTEGER")
    ensure_column(conn, "campaigns", "account_ids_json", "TEXT NOT NULL DEFAULT '[]'")
    ensure_column(conn, "campaigns", "scan_started_at_ms", "INTEGER")
    ensure_column(conn, "campaigns", "scan_completed_at_ms", "INTEGER")
    ensure_column(conn, "campaigns", "scan_stopped_reason", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "campaigns", "coverage_scope", "TEXT NOT NULL DEFAULT 'month'")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS campaign_batches (
            batch_id TEXT PRIMARY KEY,
            campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
            sequence_no INTEGER NOT NULL,
            max_size INTEGER NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('draft','approved','in_progress','completed','stopped')),
            created_at_ms INTEGER NOT NULL,
            approved_at_ms INTEGER,
            completed_at_ms INTEGER,
            UNIQUE(campaign_id, sequence_no)
        );
        CREATE TABLE IF NOT EXISTS batch_items (
            batch_id TEXT NOT NULL REFERENCES campaign_batches(batch_id),
            conversation_key TEXT NOT NULL,
            user_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending','sent','failed','skipped','uncertain')),
            reason TEXT NOT NULL DEFAULT '',
            updated_at_ms INTEGER NOT NULL,
            PRIMARY KEY(batch_id, conversation_key),
            UNIQUE(batch_id, user_id, account_id)
        );
        CREATE INDEX IF NOT EXISTS idx_batch_campaign_state
            ON campaign_batches(campaign_id, state, sequence_no);
        CREATE INDEX IF NOT EXISTS idx_batch_item_state
            ON batch_items(batch_id, state, position);
        CREATE TABLE IF NOT EXISTS batch_item_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL REFERENCES campaign_batches(batch_id),
            conversation_key TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ('retryable_locate_failure','technical_skip_restored')),
            reason TEXT NOT NULL DEFAULT '',
            created_at_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_batch_item_event
            ON batch_item_events(batch_id, event_type, created_at_ms);
        CREATE TABLE IF NOT EXISTS campaign_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
            event_type TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_campaign_events
            ON campaign_events(campaign_id, created_at_ms);
        CREATE TABLE IF NOT EXISTS historical_imports (
            import_id TEXT PRIMARY KEY,
            source_file TEXT NOT NULL,
            cutoff_date TEXT NOT NULL,
            imported_at_ms INTEGER NOT NULL,
            row_count INTEGER NOT NULL,
            unique_user_ids INTEGER NOT NULL,
            lead_users INTEGER NOT NULL,
            targetable_no_lead_users INTEGER NOT NULL,
            deleted_users INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS historical_users (
            user_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL DEFAULT '',
            latest_in_at_ms INTEGER,
            latest_open_at_ms INTEGER,
            latest_lead_at_ms INTEGER,
            phone_present INTEGER NOT NULL DEFAULT 0,
            wechat_present INTEGER NOT NULL DEFAULT 0,
            lead_status_from_import TEXT NOT NULL DEFAULT 'no_lead',
            deleted INTEGER NOT NULL DEFAULT 0,
            user_type TEXT NOT NULL DEFAULT '',
            account_names_json TEXT NOT NULL DEFAULT '[]',
            source_import_ids_json TEXT NOT NULL DEFAULT '[]',
            updated_at_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_historical_users_target
            ON historical_users(lead_status_from_import, deleted, latest_in_at_ms DESC);
        CREATE TABLE IF NOT EXISTS historical_user_accounts (
            user_id TEXT NOT NULL,
            account_name TEXT NOT NULL,
            account_id TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT '',
            latest_in_at_ms INTEGER,
            latest_open_at_ms INTEGER,
            latest_lead_at_ms INTEGER,
            phone_present INTEGER NOT NULL DEFAULT 0,
            wechat_present INTEGER NOT NULL DEFAULT 0,
            lead_status_from_import TEXT NOT NULL DEFAULT 'no_lead',
            deleted INTEGER NOT NULL DEFAULT 0,
            user_type TEXT NOT NULL DEFAULT '',
            source_import_ids_json TEXT NOT NULL DEFAULT '[]',
            updated_at_ms INTEGER NOT NULL,
            PRIMARY KEY(user_id, account_name)
        );
        CREATE INDEX IF NOT EXISTS idx_historical_user_accounts_target
            ON historical_user_accounts(account_id, lead_status_from_import, deleted, latest_in_at_ms DESC);
        CREATE TABLE IF NOT EXISTS historical_uid_reviews (
            campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
            user_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('eligible','ineligible','retryable','unknown','skipped')),
            conversation_key TEXT NOT NULL DEFAULT '',
            account_id TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            reviewed_at_ms INTEGER NOT NULL,
            retryable_count INTEGER NOT NULL DEFAULT 0,
            target_import_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(campaign_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_historical_reviews_campaign_state
            ON historical_uid_reviews(campaign_id, state, reviewed_at_ms);
        """
    )
    migrate_send_attempts_uncertain(conn)
    ensure_column(conn, "send_attempts", "batch_id", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "send_attempts", "message_snapshot", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "send_attempts", "message_hash_snapshot", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "historical_uid_reviews", "target_import_id", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "historical_user_accounts", "account_id", "TEXT NOT NULL DEFAULT ''")
    backfill_historical_user_accounts(conn)
    backfill_historical_user_account_ids(conn)
    backfill_send_attempt_batch_ids(conn)
    backfill_send_attempt_message_hashes(conn)
    migrate_batch_items_schema(conn)
    migrate_uncertain_records(conn)
    conn.commit()
    return conn


def migrate_send_attempts_uncertain(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='send_attempts'"
    ).fetchone()
    ddl = row[0] if row else ""
    if "'uncertain'" in ddl:
        return
    conn.executescript(
        """
        PRAGMA foreign_keys=OFF;
        ALTER TABLE send_attempts RENAME TO send_attempts_old_uncertain;
        CREATE TABLE send_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
            conversation_key TEXT NOT NULL,
            user_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('sent','failed','skipped','uncertain')),
            detail TEXT NOT NULL DEFAULT '',
            attempted_at_ms INTEGER NOT NULL
        );
        INSERT INTO send_attempts
          (id, campaign_id, conversation_key, user_id, account_id, status, detail, attempted_at_ms)
        SELECT id, campaign_id, conversation_key, user_id, account_id, status, detail, attempted_at_ms
        FROM send_attempts_old_uncertain;
        DROP TABLE send_attempts_old_uncertain;
        CREATE INDEX IF NOT EXISTS idx_attempt_user_time
            ON send_attempts(user_id, attempted_at_ms DESC);
        PRAGMA foreign_keys=ON;
        """
    )


def backfill_send_attempt_batch_ids(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, campaign_id, conversation_key, status, attempted_at_ms
        FROM send_attempts
        WHERE COALESCE(batch_id, '')=''
        """
    ).fetchall()
    for row in rows:
        match = conn.execute(
            """
            SELECT bi.batch_id
            FROM batch_items bi
            JOIN campaign_batches b ON b.batch_id=bi.batch_id
            WHERE b.campaign_id=? AND bi.conversation_key=?
            ORDER BY
              CASE WHEN bi.state=? THEN 0 ELSE 1 END,
              ABS(COALESCE(bi.updated_at_ms, 0) - ?),
              b.sequence_no DESC
            LIMIT 1
            """,
            (row["campaign_id"], row["conversation_key"], row["status"], row["attempted_at_ms"]),
        ).fetchone()
        if match:
            conn.execute(
                "UPDATE send_attempts SET batch_id=? WHERE id=?",
                (match["batch_id"], row["id"]),
            )


def backfill_send_attempt_message_hashes(conn: sqlite3.Connection) -> None:
    event_rows = conn.execute(
        """
        SELECT campaign_id, created_at_ms, detail
        FROM campaign_events
        WHERE event_type='message_updated'
        ORDER BY campaign_id, created_at_ms
        """
    ).fetchall()
    events_by_campaign: dict[str, list[dict]] = {}
    for event in event_rows:
        try:
            detail = json.loads(event["detail"] or "{}")
        except json.JSONDecodeError:
            detail = {}
        events_by_campaign.setdefault(event["campaign_id"], []).append({
            "created_at_ms": event["created_at_ms"],
            "old_message_hash": detail.get("old_message_hash", ""),
            "new_message_hash": detail.get("new_message_hash", ""),
        })

    rows = conn.execute(
        """
        SELECT s.id, s.campaign_id, s.attempted_at_ms,
               s.message_snapshot, s.message_hash_snapshot, c.message_hash
        FROM send_attempts s
        JOIN campaigns c ON c.campaign_id=s.campaign_id
        """
    ).fetchall()
    for row in rows:
        snapshot = row["message_snapshot"] or ""
        if snapshot:
            digest = message_digest(snapshot)
        else:
            digest = row["message_hash"]
            for event in events_by_campaign.get(row["campaign_id"], []):
                if row["attempted_at_ms"] < event["created_at_ms"]:
                    digest = event["old_message_hash"] or digest
                    break
                digest = event["new_message_hash"] or digest
        if digest and digest != (row["message_hash_snapshot"] or ""):
            conn.execute(
                "UPDATE send_attempts SET message_hash_snapshot=? WHERE id=?",
                (digest, row["id"]),
            )


def migrate_batch_items_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='batch_items'"
    ).fetchone()
    ddl = row[0] if row else ""
    needs_identity_unique = "UNIQUE(batch_id, user_id)" in ddl
    needs_uncertain = "'uncertain'" not in ddl
    if not needs_identity_unique and not needs_uncertain:
        return
    conn.executescript(
        """
        PRAGMA foreign_keys=OFF;
        ALTER TABLE batch_items RENAME TO batch_items_old_schema;
        CREATE TABLE batch_items (
            batch_id TEXT NOT NULL REFERENCES campaign_batches(batch_id),
            conversation_key TEXT NOT NULL,
            user_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending','sent','failed','skipped','uncertain')),
            reason TEXT NOT NULL DEFAULT '',
            updated_at_ms INTEGER NOT NULL,
            PRIMARY KEY(batch_id, conversation_key),
            UNIQUE(batch_id, user_id, account_id)
        );
        INSERT OR IGNORE INTO batch_items
          (batch_id, conversation_key, user_id, account_id, position, state, reason, updated_at_ms)
        SELECT batch_id, conversation_key, user_id, account_id, position, state, reason, updated_at_ms
        FROM batch_items_old_schema;
        DROP TABLE batch_items_old_schema;
        CREATE INDEX IF NOT EXISTS idx_batch_item_state
            ON batch_items(batch_id, state, position);
        PRAGMA foreign_keys=ON;
        """
    )


def sql_placeholders(values: set[str] | list[str] | tuple[str, ...]) -> str:
    return ",".join("?" for _ in values)


def migrate_uncertain_records(conn: sqlite3.Connection) -> None:
    if not UNCERTAIN_SEND_DETAILS:
        return
    details = sorted(UNCERTAIN_SEND_DETAILS)
    placeholders = sql_placeholders(details)
    conn.execute(
        f"""
        UPDATE send_attempts
        SET status='uncertain'
        WHERE status='failed' AND detail IN ({placeholders})
        """,
        details,
    )
    conn.execute(
        f"""
        UPDATE batch_items
        SET state='uncertain'
        WHERE state='failed' AND reason IN ({placeholders})
        """,
        details,
    )


def message_digest(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def make_campaign_id(message: str) -> tuple[str, str]:
    digest = message_digest(message)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:19]
    return f"{stamp}-{digest[:8]}", digest


def campaign(conn: sqlite3.Connection, campaign_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,)
    ).fetchone()
    if not row:
        emit({"ok": False, "error": "campaign_not_found", "campaign_id": campaign_id}, 2)
    return row


def batch(conn: sqlite3.Connection, batch_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM campaign_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    if not row:
        emit({"ok": False, "error": "batch_not_found", "batch_id": batch_id}, 2)
    return row


def is_expired(row: sqlite3.Row) -> bool:
    return row["expires_at_ms"] is not None and now_ms() >= row["expires_at_ms"]


def campaign_account_ids(row: sqlite3.Row) -> list[str]:
    try:
        payload = json.loads(row["account_ids_json"] or "[]")
    except (KeyError, TypeError, json.JSONDecodeError):
        return list(DEFAULT_ACCOUNT_IDS)
    if not isinstance(payload, list):
        return list(DEFAULT_ACCOUNT_IDS)
    ids = [str(x) for x in payload if str(x).strip()]
    return ids or list(DEFAULT_ACCOUNT_IDS)


def campaign_coverage_scope(row: sqlite3.Row) -> str:
    try:
        scope = str(row["coverage_scope"] or "month")
    except (KeyError, IndexError):
        scope = "month"
    return scope if scope in VALID_COVERAGE_SCOPE else "month"


def same_account_scope(left: list[str], right: list[str]) -> bool:
    return set(left or []) == set(right or [])


def account_allowed(row: sqlite3.Row, account_id: str) -> bool:
    allowed = campaign_account_ids(row)
    return not allowed or account_id in set(allowed)


def csv_account_names_for_ids(account_ids: list[str]) -> list[str]:
    names = []
    seen = set()
    for account_id in account_ids:
        mapped_names = [
            name for name, mapped_id in ACCOUNT_NAME_TO_ID.items()
            if mapped_id == account_id
        ] or [account_id]
        for name in mapped_names:
            if name and name not in seen:
                names.append(name)
                seen.add(name)
    return names


def historical_account_scope_sql(alias: str, account_ids: list[str], params: list[object]) -> str:
    checks = []
    names = csv_account_names_for_ids(account_ids)
    if account_ids:
        checks.append(f"{alias}.account_id IN (" + ",".join("?" for _ in account_ids) + ")")
        params.extend(account_ids)
    if names:
        checks.append(f"{alias}.account_name IN (" + ",".join("?" for _ in names) + ")")
        params.extend(names)
    return " AND (" + " OR ".join(checks) + ")" if checks else ""


def parse_import_time(value: str | None) -> int | None:
    text = str(value or "").strip()
    if not text or text == "-":
        return None
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ]
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            return int(parsed.replace(tzinfo=CHINA_TZ).astimezone(timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
    return None


def normalize_csv_value(value: str | None) -> str:
    text = str(value or "").strip()
    return "" if text == "-" else text


def csv_has_value(value: str | None) -> bool:
    return bool(normalize_csv_value(value))


def is_deleted_historical_row(row: dict) -> bool:
    nickname = str(row.get("用户昵称") or "").strip()
    user_type = str(row.get("用户类型") or "").strip()
    return "已注销" in nickname or "已注销" in user_type


def historical_import_id(source_file: Path, cutoff_date: str, imported_at_ms: int) -> str:
    raw = f"{source_file}|{cutoff_date}|{imported_at_ms}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"hist-{cutoff_date.replace('-', '')}-{digest}"


def merge_json_list(existing: str | None, additions: list[str]) -> str:
    try:
        values = json.loads(existing or "[]")
    except json.JSONDecodeError:
        values = []
    merged = []
    seen = set()
    for value in [*values, *additions]:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        merged.append(text)
    return json.dumps(merged, ensure_ascii=False)


def merge_optional_ms(*values: int | None) -> int | None:
    return max([value for value in values if value is not None], default=None)


def backfill_historical_user_accounts(conn: sqlite3.Connection) -> None:
    existing_count = conn.execute("SELECT COUNT(*) FROM historical_user_accounts").fetchone()[0] or 0
    if existing_count:
        return
    rows = conn.execute("SELECT * FROM historical_users").fetchall()
    for row in rows:
        try:
            account_names = json.loads(row["account_names_json"] or "[]")
        except json.JSONDecodeError:
            account_names = []
        if not account_names:
            account_names = [""]
        for account_name in account_names:
            account_name = str(account_name or "").strip()
            conn.execute(
                """
                INSERT OR IGNORE INTO historical_user_accounts
                  (user_id, account_name, account_id, display_name, latest_in_at_ms,
                   latest_open_at_ms, latest_lead_at_ms, phone_present, wechat_present,
                   lead_status_from_import, deleted, user_type, source_import_ids_json,
                   updated_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["user_id"],
                    account_name,
                    ACCOUNT_NAME_TO_ID.get(account_name, ""),
                    row["display_name"],
                    row["latest_in_at_ms"],
                    row["latest_open_at_ms"],
                    row["latest_lead_at_ms"],
                    row["phone_present"],
                    row["wechat_present"],
                    row["lead_status_from_import"],
                    row["deleted"],
                    row["user_type"],
                    row["source_import_ids_json"],
                    row["updated_at_ms"],
                ),
            )


def backfill_historical_user_account_ids(conn: sqlite3.Connection) -> None:
    for account_name, account_id in ACCOUNT_NAME_TO_ID.items():
        conn.execute(
            """
            UPDATE historical_user_accounts
            SET account_id=?
            WHERE account_name=? AND account_id<>?
            """,
            (account_id, account_name, account_id),
        )


def latest_historical_import(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM historical_imports
        ORDER BY cutoff_date DESC, imported_at_ms DESC LIMIT 1
        """
    ).fetchone()


def historical_import_summary(conn: sqlite3.Connection) -> dict:
    latest = latest_historical_import(conn)
    total_targetable = conn.execute(
        """
        SELECT COUNT(*) FROM historical_users
        WHERE lead_status_from_import='no_lead' AND deleted=0
        """
    ).fetchone()[0] or 0
    total_lead = conn.execute(
        """
        SELECT COUNT(*) FROM historical_users
        WHERE lead_status_from_import='has_lead'
        """
    ).fetchone()[0] or 0
    return {
        "ready": bool(latest),
        "latest_import": dict(latest) if latest else None,
        "targetable_no_lead_users": total_targetable,
        "lead_protected_users": total_lead,
    }


def current_user_list_import_status(conn: sqlite3.Connection) -> dict:
    latest = latest_historical_import(conn)
    required_cutoff = (datetime.now(CHINA_TZ).date() - timedelta(days=1)).isoformat()
    if not latest:
        return {
            "ready": False,
            "required_cutoff_date": required_cutoff,
            "latest_import": None,
            "missing_reason": "no_user_list_import",
        }
    cutoff = str(latest["cutoff_date"] or "")
    ready = cutoff >= required_cutoff
    return {
        "ready": ready,
        "required_cutoff_date": required_cutoff,
        "latest_import": dict(latest),
        "missing_reason": "" if ready else "latest_user_list_cutoff_too_old",
    }


def parse_iso_date_arg(value: str, name: str) -> str:
    text = str(value or "").strip()
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        emit({"ok": False, "error": f"invalid_{name}", "expected": "YYYY-MM-DD"}, 2)
    return text


def imported_historical_targets(
    conn: sqlite3.Connection,
    import_id: str = "",
    account_ids: list[str] | None = None,
) -> list[dict]:
    params: list[object] = []
    scope_filter = historical_account_scope_sql("ha", account_ids or [], params)
    rows = conn.execute(
        f"""
        SELECT * FROM historical_user_accounts ha
        WHERE 1=1 {scope_filter}
        """,
        params,
    ).fetchall()
    per_user: dict[str, dict] = {}
    for row in rows:
        try:
            source_import_ids = json.loads(row["source_import_ids_json"] or "[]")
        except json.JSONDecodeError:
            source_import_ids = []
        if import_id and import_id not in source_import_ids:
            continue
        user_id = row["user_id"]
        item = per_user.setdefault(user_id, {
            "user_id": user_id,
            "display_name": "",
            "latest_in_at_ms": None,
            "latest_open_at_ms": None,
            "latest_lead_at_ms": None,
            "phone_present": 0,
            "wechat_present": 0,
            "lead_status_from_import": "no_lead",
            "deleted": 0,
            "user_type": "",
            "account_names": [],
            "source_import_ids": [],
            "updated_at_ms": 0,
        })
        if row["display_name"] and not item["display_name"]:
            item["display_name"] = row["display_name"]
        for key in ("latest_in_at_ms", "latest_open_at_ms", "latest_lead_at_ms"):
            item[key] = merge_optional_ms(item[key], row[key])
        item["phone_present"] = max(item["phone_present"], row["phone_present"])
        item["wechat_present"] = max(item["wechat_present"], row["wechat_present"])
        if row["lead_status_from_import"] == "has_lead":
            item["lead_status_from_import"] = "has_lead"
        item["deleted"] = max(item["deleted"], row["deleted"])
        if row["user_type"] and not item["user_type"]:
            item["user_type"] = row["user_type"]
        if row["account_name"] and row["account_name"] not in item["account_names"]:
            item["account_names"].append(row["account_name"])
        for source_import_id in source_import_ids:
            if source_import_id not in item["source_import_ids"]:
                item["source_import_ids"].append(source_import_id)
        item["updated_at_ms"] = max(item["updated_at_ms"], row["updated_at_ms"] or 0)
    targets = [
        item for item in per_user.values()
        if item["lead_status_from_import"] == "no_lead" and not item["deleted"]
    ]
    targets.sort(key=lambda item: (item["latest_open_at_ms"] or item["latest_in_at_ms"] or 0, item["user_id"]), reverse=True)
    return targets


def imported_historical_target(
    conn: sqlite3.Connection,
    campaign_row: sqlite3.Row,
    user_id: str,
) -> dict | None:
    target_import_id = review_target_import_id(conn, campaign_row["campaign_id"])
    for target in imported_historical_targets(
        conn,
        import_id=target_import_id,
        account_ids=campaign_account_ids(campaign_row),
    ):
        if target["user_id"] == user_id:
            return target
    return None


def historical_user_has_lead_in_scope(
    conn: sqlite3.Connection,
    user_id: str,
    account_ids: list[str],
) -> bool:
    params: list[object] = [user_id]
    scope_filter = historical_account_scope_sql("ha", account_ids, params)
    return bool(conn.execute(
        f"""
        SELECT 1 FROM historical_user_accounts ha
        WHERE ha.user_id=?
          AND ha.lead_status_from_import='has_lead'
          {scope_filter}
        LIMIT 1
        """,
        params,
    ).fetchone())


def review_target_import_id(conn: sqlite3.Connection, campaign_id: str) -> str:
    row = campaign(conn, campaign_id)
    if campaign_coverage_scope(row) != "user_list":
        return ""
    latest = latest_historical_import(conn)
    return str(latest["import_id"] or "") if latest else ""


def historical_review_status(conn: sqlite3.Connection, campaign_id: str) -> dict:
    camp = campaign(conn, campaign_id)
    target_import_id = review_target_import_id(conn, campaign_id)
    target_users = {
        row["user_id"] for row in imported_historical_targets(
            conn,
            import_id=target_import_id,
            account_ids=campaign_account_ids(camp),
        )
    }
    total_targets = len(target_users)
    counts: dict[str, int] = {}
    review_rows = conn.execute(
        """
        SELECT user_id, state, target_import_id
        FROM historical_uid_reviews
        WHERE campaign_id=?
        """,
        (campaign_id,),
    ).fetchall()
    for row in review_rows:
        if target_import_id and row["target_import_id"] != target_import_id:
            continue
        if target_users and row["user_id"] not in target_users:
            continue
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    reviewed_terminal = sum(counts.get(state, 0) for state in ("eligible", "ineligible", "unknown", "skipped"))
    retryable = counts.get("retryable", 0)
    window = latest_campaign_event(conn, campaign_id, "historical_review_window_completed")
    if target_import_id:
        event_import_id = str((window or {}).get("target_import_id") or "")
        if event_import_id != target_import_id:
            window = None
    return {
        "target_users": total_targets,
        "reviewed_terminal_users": reviewed_terminal,
        "retryable_users": retryable,
        "eligible_users": counts.get("eligible", 0),
        "ineligible_users": counts.get("ineligible", 0),
        "unknown_users": counts.get("unknown", 0),
        "skipped_users": counts.get("skipped", 0),
        "window_completed": window,
        "target_import_id": target_import_id,
        "counts": counts,
    }


def eligibility(args: argparse.Namespace, min_hours: int) -> tuple[bool, str]:
    checks = [
        (args.lead_status == "no_lead", f"lead_status={args.lead_status}"),
        (args.party_status == "customer", f"party_status={args.party_status}"),
        (args.reply_status == "awaiting_customer", f"reply_status={args.reply_status}"),
        (args.negative_status == "safe", f"negative_status={args.negative_status}"),
    ]
    for passed, reason in checks:
        if not passed:
            return False, reason
    required_ms = min_hours * 60 * 60 * 1000
    if now_ms() - args.last_message_at_ms < required_ms:
        return False, f"silence_less_than_{min_hours}h"
    return True, ""


def stop_open_batches_for_campaign(
    conn: sqlite3.Connection,
    campaign_id: str,
    *,
    reason: str,
    stopped_at: int | None = None,
) -> list[str]:
    stopped_at = stopped_at or now_ms()
    open_batch_ids = [
        row["batch_id"] for row in conn.execute(
            """
            SELECT batch_id FROM campaign_batches
            WHERE campaign_id=? AND state IN ('draft','approved','in_progress')
            """,
            (campaign_id,),
        )
    ]
    for batch_id in open_batch_ids:
        conn.execute(
            """
            UPDATE batch_items
            SET state='skipped', reason=?, updated_at_ms=?
            WHERE batch_id=? AND state='pending'
            """,
            (reason, stopped_at, batch_id),
        )
    conn.execute(
        """
        UPDATE campaign_batches SET state='stopped', completed_at_ms=?
        WHERE campaign_id=? AND state IN ('draft','approved','in_progress')
        """,
        (stopped_at, campaign_id),
    )
    return open_batch_ids


def stop_other_active_campaigns(
    conn: sqlite3.Connection,
    *,
    keep_campaign_id: str,
    message_hash: str,
    account_ids: list[str],
    reason: str,
    enabled: bool = True,
) -> list[dict]:
    if not enabled:
        return []
    stopped_at = now_ms()
    stopped: list[dict] = []
    rows = conn.execute(
        """
        SELECT * FROM campaigns
        WHERE campaign_id<>? AND state IN ('draft','approved')
        ORDER BY created_at_ms DESC
        """,
        (keep_campaign_id,),
    ).fetchall()
    for row in rows:
        same_message = row["message_hash"] == message_hash
        same_scope = same_account_scope(campaign_account_ids(row), account_ids)
        if same_message and not same_scope:
            continue
        open_batches = stop_open_batches_for_campaign(
            conn,
            row["campaign_id"],
            reason=reason,
            stopped_at=stopped_at,
        )
        conn.execute(
            "UPDATE campaigns SET state='stopped' WHERE campaign_id=?",
            (row["campaign_id"],),
        )
        stopped.append({
            "campaign_id": row["campaign_id"],
            "same_message": same_message,
            "same_account_scope": same_scope,
            "open_batches": len(open_batches),
        })
    return stopped


def count_open_batches_for_campaign(conn: sqlite3.Connection, campaign_id: str) -> int:
    return conn.execute(
        """
        SELECT COUNT(*) FROM campaign_batches
        WHERE campaign_id=? AND state IN ('draft','approved','in_progress')
        """,
        (campaign_id,),
    ).fetchone()[0]


def unfinished_task_status(conn: sqlite3.Connection) -> dict:
    latest = conn.execute(
        """
        SELECT campaign_id, coverage_scope, created_at_ms
        FROM campaigns
        WHERE state IN ('draft','approved')
        ORDER BY created_at_ms DESC
        LIMIT 1
        """
    ).fetchone()
    counts = conn.execute(
        """
        SELECT
          COUNT(DISTINCT c.campaign_id) AS active_campaigns,
          COUNT(DISTINCT CASE
            WHEN b.state IN ('draft','approved','in_progress') THEN b.batch_id
          END) AS open_batches,
          SUM(CASE WHEN bi.state='pending' THEN 1 ELSE 0 END) AS pending_batch_users,
          SUM(CASE WHEN bi.state='uncertain' THEN 1 ELSE 0 END) AS uncertain_users,
          SUM(CASE WHEN bi.state='failed' THEN 1 ELSE 0 END) AS failed_users
        FROM campaigns c
        LEFT JOIN campaign_batches b ON b.campaign_id=c.campaign_id
        LEFT JOIN batch_items bi ON bi.batch_id=b.batch_id
        WHERE c.state IN ('draft','approved')
        """
    ).fetchone()
    review_retryable = conn.execute(
        """
        SELECT COUNT(*)
        FROM historical_uid_reviews r
        JOIN campaigns c ON c.campaign_id=r.campaign_id
        WHERE c.state IN ('draft','approved') AND r.state='retryable'
        """
    ).fetchone()[0] or 0
    active_campaigns = counts["active_campaigns"] or 0
    return {
        "available": bool(active_campaigns),
        "latest_campaign_id": latest["campaign_id"] if latest else None,
        "latest_coverage_scope": latest["coverage_scope"] if latest else None,
        "active_campaigns": active_campaigns,
        "open_batches": counts["open_batches"] or 0,
        "pending_batch_users": counts["pending_batch_users"] or 0,
        "uncertain_users": counts["uncertain_users"] or 0,
        "failed_users": counts["failed_users"] or 0,
        "retryable_uid_reviews": review_retryable,
    }


def preview_other_active_campaigns(
    conn: sqlite3.Connection,
    *,
    keep_campaign_id: str,
    message_hash: str,
    account_ids: list[str],
    enabled: bool = True,
) -> list[dict]:
    if not enabled:
        return []
    rows = conn.execute(
        """
        SELECT * FROM campaigns
        WHERE campaign_id<>? AND state IN ('draft','approved')
        ORDER BY created_at_ms DESC
        """,
        (keep_campaign_id,),
    ).fetchall()
    preview: list[dict] = []
    for row in rows:
        same_message = row["message_hash"] == message_hash
        same_scope = same_account_scope(campaign_account_ids(row), account_ids)
        if same_message and not same_scope:
            continue
        preview.append({
            "campaign_id": row["campaign_id"],
            "same_message": same_message,
            "same_account_scope": same_scope,
            "open_batches": count_open_batches_for_campaign(conn, row["campaign_id"]),
        })
    return preview


def start_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    if not args.message or not args.message.strip():
        emit({"ok": False, "error": "empty_message"}, 2)
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        emit({"ok": False, "error": "invalid_batch_size", "max": MAX_BATCH_SIZE}, 2)
    if args.min_silence_hours < 24:
        emit({"ok": False, "error": "min_silence_must_be_at_least_24h"}, 2)
    if args.expires_at_ms is not None and args.expires_at_ms <= now_ms():
        emit({"ok": False, "error": "expiry_must_be_in_future"}, 2)
    account_ids = parse_account_ids(args.account_ids)
    coverage_scope = normalize_coverage_scope(getattr(args, "coverage_scope", "user_list"))
    digest = message_digest(args.message)
    created_at = now_ms()
    state = "approved" if args.activate else "draft"
    approved_at = created_at if args.activate else None

    reusable = conn.execute(
        """
        SELECT * FROM campaigns
        WHERE message_hash=? AND state IN ('draft','approved')
        ORDER BY created_at_ms DESC
        """
        ,
        (digest,),
    ).fetchall()
    for row in reusable:
        if is_expired(row):
            continue
        if (
            same_account_scope(campaign_account_ids(row), account_ids)
            and campaign_coverage_scope(row) == coverage_scope
        ):
            stopped_reset_batches = stop_open_batches_for_campaign(
                conn,
                row["campaign_id"],
                reason="scan_reset_invalidates_open_batch",
                stopped_at=created_at,
            )
            conn.execute(
                """
                UPDATE campaigns
                SET state=CASE WHEN ?='approved' THEN 'approved' ELSE state END,
                    approved_at_ms=CASE
                      WHEN ?='approved' THEN COALESCE(approved_at_ms, ?)
                      ELSE approved_at_ms
                    END,
                    min_silence_hours=?,
                    batch_size=?,
                    expires_at_ms=COALESCE(?, expires_at_ms),
                    coverage_scope=?,
                    scan_started_at_ms=NULL,
                    scan_completed_at_ms=NULL,
                    scan_stopped_reason=''
                WHERE campaign_id=?
                """,
                (
                    state, state, approved_at, args.min_silence_hours,
                    args.batch_size, args.expires_at_ms, coverage_scope, row["campaign_id"],
                ),
            )
            stopped_other = stop_other_active_campaigns(
                conn, keep_campaign_id=row["campaign_id"],
                message_hash=digest,
                account_ids=account_ids,
                reason="superseded_by_reused_daily_run",
                enabled=not args.keep_other_active,
            )
            conn.commit()
            payload = summary_payload(conn, row["campaign_id"])
            payload["reused"] = True
            payload["scan_reset"] = True
            payload["scan_reset_stopped_batches"] = stopped_reset_batches
            payload["stopped_other_active_campaigns"] = stopped_other
            emit(payload)

    campaign_id, digest = make_campaign_id(args.message)
    conn.execute(
        """
        INSERT INTO campaigns
          (campaign_id, message, message_hash, state, min_silence_hours,
           created_at_ms, approved_at_ms, batch_size, expires_at_ms,
           account_ids_json, coverage_scope)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            campaign_id, args.message, digest, state, args.min_silence_hours,
            created_at, approved_at, args.batch_size, args.expires_at_ms,
            json.dumps(account_ids, ensure_ascii=False), coverage_scope,
        ),
    )
    stopped_other = stop_other_active_campaigns(
        conn, keep_campaign_id=campaign_id,
        message_hash=digest,
        account_ids=account_ids,
        reason="superseded_by_new_daily_run",
        enabled=not args.keep_other_active,
    )
    conn.commit()
    emit({
        "ok": True, "campaign_id": campaign_id, "state": state,
        "message": args.message, "batch_size": args.batch_size,
        "expires_at_ms": args.expires_at_ms, "account_ids": account_ids,
        "coverage_scope": coverage_scope,
        "reused": False, "scan_reset": False,
        "stopped_other_active_campaigns": stopped_other,
    })


def supersede_campaign_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    old = campaign(conn, args.campaign_id)
    if old["state"] not in {"draft", "approved"}:
        emit({"ok": False, "error": "campaign_not_active", "state": old["state"]}, 2)
    if not args.message or not args.message.strip():
        emit({"ok": False, "error": "empty_message"}, 2)
    if args.message == old["message"]:
        emit({"ok": False, "error": "message_unchanged"}, 2)
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        emit({"ok": False, "error": "invalid_batch_size", "max": MAX_BATCH_SIZE}, 2)
    if args.min_silence_hours < 24:
        emit({"ok": False, "error": "min_silence_must_be_at_least_24h"}, 2)
    if args.expires_at_ms is not None and args.expires_at_ms <= now_ms():
        emit({"ok": False, "error": "expiry_must_be_in_future"}, 2)
    account_ids = parse_account_ids(args.account_ids)
    attempts = conn.execute(
        "SELECT COUNT(*) FROM send_attempts WHERE campaign_id=?",
        (args.campaign_id,),
    ).fetchone()[0]
    protected_batches = conn.execute(
        """
        SELECT COUNT(*) FROM campaign_batches
        WHERE campaign_id=? AND state IN ('approved','in_progress')
        """,
        (args.campaign_id,),
    ).fetchone()[0]
    if attempts or protected_batches:
        emit({
            "ok": False, "error": "campaign_requires_manual_stop",
            "attempts": attempts, "protected_batches": protected_batches,
        }, 2)

    created_at = now_ms()
    new_campaign_id, digest = make_campaign_id(args.message)
    conn.execute(
        """
        INSERT INTO campaigns
          (campaign_id, message, message_hash, state, min_silence_hours,
           created_at_ms, approved_at_ms, batch_size, expires_at_ms,
           account_ids_json, coverage_scope)
        VALUES (?, ?, ?, 'draft', ?, ?, NULL, ?, ?, ?, ?)
        """,
        (
            new_campaign_id, args.message, digest, args.min_silence_hours,
            created_at, args.batch_size, args.expires_at_ms,
            json.dumps(account_ids, ensure_ascii=False),
            campaign_coverage_scope(old),
        ),
    )
    conn.execute(
        """
        INSERT INTO candidates
          (campaign_id, conversation_key, user_id, account_id, display_name,
           lead_status, party_status, reply_status, negative_status,
           last_message_at_ms, evidence, eligible, exclusion_reason, updated_at_ms)
        SELECT ?, conversation_key, user_id, account_id, display_name,
               lead_status, party_status, reply_status, negative_status,
               last_message_at_ms, evidence, eligible, exclusion_reason, ?
        FROM candidates WHERE campaign_id=?
        """,
        (new_campaign_id, created_at, args.campaign_id),
    )
    draft_batch_ids = [
        row["batch_id"] for row in conn.execute(
            """
            SELECT batch_id FROM campaign_batches
            WHERE campaign_id=? AND state='draft'
            """,
            (args.campaign_id,),
        )
    ]
    for batch_id in draft_batch_ids:
        conn.execute(
            """
            UPDATE batch_items
            SET state='skipped', reason='superseded_before_send', updated_at_ms=?
            WHERE batch_id=? AND state='pending'
            """,
            (created_at, batch_id),
        )
    conn.execute(
        """
        UPDATE campaign_batches SET state='stopped', completed_at_ms=?
        WHERE campaign_id=? AND state='draft'
        """,
        (created_at, args.campaign_id),
    )
    conn.execute(
        "UPDATE campaigns SET state='stopped' WHERE campaign_id=?",
        (args.campaign_id,),
    )
    conn.commit()
    payload = summary_payload(conn, new_campaign_id)
    payload["superseded_campaign_id"] = args.campaign_id
    payload["copied_candidates"] = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE campaign_id=?",
        (new_campaign_id,),
    ).fetchone()[0]
    emit(payload)


def update_message_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    camp = campaign(conn, args.campaign_id)
    if camp["state"] not in {"draft", "approved"}:
        emit({"ok": False, "error": "campaign_not_active", "state": camp["state"]}, 2)
    if not args.message or not args.message.strip():
        emit({"ok": False, "error": "empty_message"}, 2)
    old_message = camp["message"]
    if args.message == old_message:
        emit({
            "ok": True, "campaign_id": args.campaign_id,
            "changed": False, "message": old_message,
            "message_hash": camp["message_hash"],
        })
    digest = message_digest(args.message)
    changed_at = now_ms()
    detail = json.dumps(
        {
            "old_message_hash": camp["message_hash"],
            "new_message_hash": digest,
            "reason": args.reason,
        },
        ensure_ascii=False,
    )
    conn.execute(
        """
        UPDATE campaigns
        SET message=?, message_hash=?
        WHERE campaign_id=?
        """,
        (args.message, digest, args.campaign_id),
    )
    conn.execute(
        """
        INSERT INTO campaign_events (campaign_id, event_type, detail, created_at_ms)
        VALUES (?, 'message_updated', ?, ?)
        """,
        (args.campaign_id, detail, changed_at),
    )
    conn.commit()
    payload = summary_payload(conn, args.campaign_id)
    payload["changed"] = True
    payload["updated_message_hash"] = digest
    payload["previous_message_hash"] = camp["message_hash"]
    emit(payload)


def candidate_values(
    campaign_id: str, item: dict, eligible: bool, reason: str
) -> tuple:
    return (
        campaign_id, item["conversation_key"], item["user_id"], item["account_id"],
        item["display_name"], item["lead_status"], item["party_status"],
        item["reply_status"], item["negative_status"], int(item["last_message_at_ms"]),
        item.get("evidence", ""), int(eligible), reason, now_ms(),
    )


CANDIDATE_UPSERT = """
    INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(campaign_id, conversation_key) DO UPDATE SET
      user_id=excluded.user_id, account_id=excluded.account_id,
      display_name=excluded.display_name, lead_status=excluded.lead_status,
      party_status=excluded.party_status, reply_status=excluded.reply_status,
      negative_status=excluded.negative_status,
      last_message_at_ms=excluded.last_message_at_ms, evidence=excluded.evidence,
      eligible=excluded.eligible, exclusion_reason=excluded.exclusion_reason,
      updated_at_ms=excluded.updated_at_ms
"""


def validate_candidate(item: dict) -> None:
    required = {
        "conversation_key", "user_id", "account_id", "display_name",
        "lead_status", "party_status", "reply_status", "negative_status",
        "last_message_at_ms",
    }
    if not isinstance(item, dict) or not required.issubset(item):
        emit({"ok": False, "error": "invalid_candidate_record"}, 2)
    if item["lead_status"] not in VALID_LEAD or item["party_status"] not in VALID_PARTY:
        emit({"ok": False, "error": "invalid_candidate_classification"}, 2)
    if item["reply_status"] not in VALID_REPLY or item["negative_status"] not in VALID_NEGATIVE:
        emit({"ok": False, "error": "invalid_candidate_classification"}, 2)
    prefix, key_user_id, key_account_id = parse_conversation_key(item["conversation_key"])
    if not prefix or key_user_id != item["user_id"]:
        emit({"ok": False, "error": "conversation_key_user_mismatch"}, 2)
    if key_account_id and key_account_id != item["account_id"]:
        emit({"ok": False, "error": "conversation_key_account_mismatch"}, 2)


def candidate_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    row = campaign(conn, args.campaign_id)
    item = vars(args)
    validate_candidate(item)
    if not account_allowed(row, args.account_id):
        emit({
            "ok": True, "conversation_key": args.conversation_key,
            "eligible": False, "ignored": True,
            "reason": "account_not_allowed",
        })
    eligible, reason = eligibility(args, row["min_silence_hours"])
    conn.execute(CANDIDATE_UPSERT, candidate_values(args.campaign_id, item, eligible, reason))
    conn.commit()
    emit({
        "ok": True, "conversation_key": args.conversation_key,
        "eligible": eligible, "reason": reason,
    })


def bulk_candidates_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    row = campaign(conn, args.campaign_id)
    payload = json.loads(args.file.expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        emit({"ok": False, "error": "candidate_file_must_be_array"}, 2)
    rows = []
    eligible_count = 0
    ignored_count = 0
    for item in payload:
        validate_candidate(item)
        if not account_allowed(row, item["account_id"]):
            ignored_count += 1
            continue
        eligible, reason = eligibility(argparse.Namespace(**item), row["min_silence_hours"])
        eligible_count += int(eligible)
        rows.append(candidate_values(args.campaign_id, item, eligible, reason))
    conn.executemany(CANDIDATE_UPSERT, rows)
    conn.commit()
    emit({"ok": True, "imported": len(rows), "ignored": ignored_count, "eligible_conversations": eligible_count})


def eligible_queue(conn: sqlite3.Connection, campaign_id: str) -> list[sqlite3.Row]:
    camp = campaign(conn, campaign_id)
    cutoff = now_ms() - camp["min_silence_hours"] * 60 * 60 * 1000
    allowed_accounts = campaign_account_ids(camp)
    target_import_id = review_target_import_id(conn, campaign_id)
    account_filter = ""
    current_import_filter = ""
    lead_protection_filter = ""
    params: list[object] = [cutoff, campaign_id]
    if allowed_accounts:
        account_filter = " AND account_id IN (" + ",".join("?" for _ in allowed_accounts) + ")"
        params.extend(allowed_accounts)
    params.extend([campaign_id])
    if allowed_accounts:
        params.extend(allowed_accounts)
    if target_import_id:
        current_import_params: list[object] = []
        current_import_scope_filter = historical_account_scope_sql("current_h", allowed_accounts, current_import_params)
        current_import_filter = """
            AND EXISTS (
              SELECT 1 FROM historical_user_accounts current_h
              WHERE current_h.user_id=c.user_id
                AND current_h.lead_status_from_import='no_lead'
                AND current_h.deleted=0
                {current_import_scope_filter}
                AND current_h.source_import_ids_json LIKE ?
            )
        """.format(current_import_scope_filter=current_import_scope_filter)
        params.extend(current_import_params)
        params.append(f'%"{target_import_id}"%')
    lead_protection_params: list[object] = []
    lead_protection_filter = historical_account_scope_sql("ha", allowed_accounts, lead_protection_params)
    params.extend([cutoff, camp["message_hash"], cutoff])
    params.extend(lead_protection_params)
    return conn.execute(
        f"""
        WITH user_status AS (
          SELECT user_id,
            MAX(CASE WHEN lead_status='no_lead'
                       AND party_status='customer'
                       AND reply_status='awaiting_customer'
                       AND negative_status='safe'
                       AND last_message_at_ms<=?
                     THEN 1 ELSE 0 END) AS has_eligible,
            MAX(CASE WHEN lead_status='has_lead'
                       OR party_status IN ('peer','unknown')
                       OR negative_status='blocked'
                     THEN 1 ELSE 0 END) AS has_conflict
          FROM candidates
          WHERE campaign_id=? {account_filter}
          GROUP BY user_id
        ), ranked AS (
          SELECT c.*,
            ROW_NUMBER() OVER (
              PARTITION BY c.user_id
              ORDER BY c.last_message_at_ms DESC, c.updated_at_ms DESC, c.conversation_key
            ) AS rank_for_user
          FROM candidates c
          JOIN user_status u ON u.user_id=c.user_id
          WHERE c.campaign_id=?
            {account_filter}
            {current_import_filter}
            AND c.lead_status='no_lead'
            AND c.party_status='customer'
            AND c.reply_status='awaiting_customer'
            AND c.negative_status='safe'
            AND c.last_message_at_ms<=?
            AND u.has_eligible=1 AND u.has_conflict=0
            AND NOT EXISTS (
              SELECT 1 FROM send_attempts s
              WHERE s.campaign_id=c.campaign_id
                AND s.user_id=c.user_id AND s.status IN ('sent','uncertain')
            )
            AND NOT EXISTS (
              SELECT 1 FROM send_attempts s
              WHERE s.message_hash_snapshot=?
                AND s.user_id=c.user_id
                AND s.status IN ('sent','uncertain')
            )
            AND NOT EXISTS (
              SELECT 1 FROM send_attempts s
              WHERE s.user_id=c.user_id
                AND (
                  (s.status='sent' AND s.attempted_at_ms>?)
                  OR s.status='uncertain'
                )
            )
            AND NOT EXISTS (
              SELECT 1 FROM historical_user_accounts ha
              WHERE ha.user_id=c.user_id
                AND ha.lead_status_from_import='has_lead'
                {lead_protection_filter}
            )
        )
        SELECT * FROM ranked
        WHERE rank_for_user=1
        ORDER BY last_message_at_ms DESC, user_id, account_id
        """,
        params,
    ).fetchall()


def assigned_user_ids(conn: sqlite3.Connection, campaign_id: str) -> set[str]:
    states = sorted(FINAL_OR_ASSIGNED_BATCH_ITEM_STATES)
    placeholders = sql_placeholders(states)
    return {
        row["user_id"] for row in conn.execute(
            f"""
            SELECT bi.user_id
            FROM batch_items bi
            JOIN campaign_batches b ON b.batch_id=bi.batch_id
            WHERE b.campaign_id=?
              AND bi.state IN ({placeholders})
            """,
            [campaign_id, *states],
        )
    }


def remaining_queue(conn: sqlite3.Connection, campaign_id: str) -> list[sqlite3.Row]:
    assigned = assigned_user_ids(conn, campaign_id)
    return [row for row in eligible_queue(conn, campaign_id) if row["user_id"] not in assigned]


def sent_range(conn: sqlite3.Connection, campaign_id: str) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*) AS sent_attempts,
               COUNT(DISTINCT user_id) AS sent_users,
               MIN(attempted_at_ms) AS first_sent_at_ms,
               MAX(attempted_at_ms) AS last_sent_at_ms
        FROM send_attempts
        WHERE campaign_id=? AND status='sent'
        """,
        (campaign_id,),
    ).fetchone()
    return {
        "sent_attempts": row["sent_attempts"] or 0,
        "sent_users": row["sent_users"] or 0,
        "first_sent_at_ms": row["first_sent_at_ms"],
        "last_sent_at_ms": row["last_sent_at_ms"],
    }


def sent_range_payload(conn: sqlite3.Connection, campaign_id: str) -> dict:
    payload = sent_range(conn, campaign_id)
    payload["first_sent_at"] = format_ms(payload["first_sent_at_ms"])
    payload["last_sent_at"] = format_ms(payload["last_sent_at_ms"])
    return payload


def find_incremental_baseline_for_scope(
    conn: sqlite3.Connection,
    *,
    account_ids: list[str],
    before_ms: int,
    baseline_hours: int,
    exclude_campaign_id: str = "",
) -> sqlite3.Row | None:
    cutoff = now_ms() - baseline_hours * 60 * 60 * 1000
    rows = conn.execute(
        """
        SELECT c.*
        FROM campaigns c
        WHERE c.campaign_id<>?
          AND c.created_at_ms<=?
          AND c.scan_completed_at_ms IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM send_attempts s
            WHERE s.campaign_id=c.campaign_id
              AND s.status='sent'
              AND s.attempted_at_ms>=?
          )
        ORDER BY c.created_at_ms DESC
        """,
        (exclude_campaign_id, before_ms, cutoff),
    ).fetchall()
    for row in rows:
        if same_account_scope(campaign_account_ids(row), account_ids):
            return row
    return None


def find_incremental_baseline(
    conn: sqlite3.Connection,
    target: sqlite3.Row,
    baseline_hours: int,
) -> sqlite3.Row | None:
    return find_incremental_baseline_for_scope(
        conn,
        account_ids=campaign_account_ids(target),
        before_ms=target["created_at_ms"],
        baseline_hours=baseline_hours,
        exclude_campaign_id=target["campaign_id"],
    )


def build_operator_modes(baseline: sqlite3.Row | None) -> list[dict]:
    return [
        {
            "id": "1",
            "mode": "table_followup",
            "label": "表格跟进",
            "recommended": True,
            "requires_browser_scan": False,
            "requires_uid_review": True,
            "requires_data_source_choice": True,
            "data_sources": ["1A 最新单表", "1B 历史多表"],
        },
        {
            "id": "2",
            "mode": "resume_unfinished_task",
            "label": "继续未完成任务",
            "recommended": False,
            "requires_browser_scan": False,
            "requires_uid_review": True,
        },
        {
            "id": "3",
            "mode": "handle_exceptions_or_resend",
            "label": "处理异常/补发",
            "recommended": False,
            "requires_browser_scan": False,
        },
        {
            "id": "4",
            "mode": "status_only",
            "label": "只查状态",
            "recommended": False,
            "requires_browser_scan": False,
        },
    ]


def build_coverage_modes(conn: sqlite3.Connection) -> list[dict]:
    imports = historical_import_summary(conn)
    current_import = current_user_list_import_status(conn)
    history_targets = imports["targetable_no_lead_users"]
    unfinished = unfinished_task_status(conn)
    return [
        {
            "id": "1",
            "scope": "user_list",
            "label": "表格跟进",
            "available": True,
            "recommended": True,
            "does": "choose_latest_single_csv_or_historical_multi_csv_then_review_no_lead_uids",
            "requires_browser_scan": False,
            "requires_uid_review": True,
            "requires_data_source_choice": True,
            "data_sources": [
                {
                    "id": "1A",
                    "mode": "latest_single_csv",
                    "label": "最新单表",
                    "maps_to_scope": "user_list",
                    "does": "use_only_the_csv_exported_immediately_before_this_run",
                    "requires_fresh_cutoff": True,
                },
                {
                    "id": "1B",
                    "mode": "historical_multi_csv",
                    "label": "历史多表",
                    "maps_to_scope": "history",
                    "does": "merge_specified_csvs_or_folder_for_authorized_accounts_only",
                    "requires_fresh_cutoff": False,
                },
            ],
            "current_user_list_import": current_import,
        },
        {
            "id": "2",
            "scope": "resume_unfinished",
            "label": "继续未完成任务",
            "available": unfinished["available"],
            "recommended": False,
            "does": "continue_existing_uid_review_or_pending_batch",
            "unavailable_reason": "" if unfinished["available"] else "no_unfinished_task",
            "maps_to_scope": "user_list",
            "unfinished_tasks": unfinished,
        },
        {
            "id": "3",
            "scope": "exceptions",
            "label": "处理异常/补发",
            "available": True,
            "recommended": False,
            "does": "resolve_uncertain_failed_retryable_or_resume_explicit_old_queue",
        },
        {
            "id": "4",
            "scope": "status_only",
            "label": "只查状态",
            "available": True,
            "recommended": False,
            "does": "inspect_state_without_browser_side_effects",
        },
        {
            "id": "internal-month-scan",
            "scope": "month",
            "label": "本月扫描兜底",
            "available": True,
            "recommended": False,
            "does": "fallback_scan_current_month_private_messages_when_csv_or_uid_search_is_not_viable",
            "internal": True,
            "requires_browser_scan": True,
        },
        {
            "id": "internal-history",
            "scope": "history",
            "label": "旧历史 UID 复核",
            "available": imports["ready"],
            "recommended": False,
            "does": "legacy_review_imported_historical_uid_pool",
            "internal": True,
            "targetable_no_lead_users": history_targets,
        },
    ]


def plan_with_baseline(
    *,
    conn: sqlite3.Connection,
    campaign_id: str | None,
    baseline: sqlite3.Row | None,
    baseline_hours: int,
    buffer_minutes: int,
    recommended_mode: str | None = None,
) -> dict:
    payload = {
        "ok": True,
        "campaign_id": campaign_id,
        "baseline_hours": baseline_hours,
        "buffer_minutes": buffer_minutes,
        "recommended_mode": recommended_mode or ("daily_incremental_scan" if baseline else "full_scan"),
        "requires_manual_mode_choice": True,
        "default_reply_is_not_authorization": True,
        "modes": build_operator_modes(baseline),
        "coverage_modes": build_coverage_modes(conn),
        "historical_import": historical_import_summary(conn),
        "current_user_list_import": current_user_list_import_status(conn),
        "baseline": None,
        "scan_window": None,
    }
    if not baseline:
        payload["reason"] = "no_recent_completed_full_scan_with_sent_attempts_for_same_account_scope"
        return payload
    base_sent = sent_range_payload(conn, baseline["campaign_id"])
    window_start = (base_sent["first_sent_at_ms"] or 0) - buffer_minutes * 60 * 1000
    payload["baseline"] = {
        "campaign_id": baseline["campaign_id"],
        "scan_completed_at_ms": baseline["scan_completed_at_ms"],
        "scan_completed_at": format_ms(baseline["scan_completed_at_ms"]),
        "account_ids": campaign_account_ids(baseline),
        **base_sent,
    }
    payload["scan_window"] = {
        "start_ms": window_start,
        "start_at": format_ms(window_start),
        "rule": "scan_from_top_until_before_first_successful_send_minus_buffer",
    }
    return payload


def incremental_plan_payload(
    conn: sqlite3.Connection,
    campaign_id: str,
    baseline_hours: int = 48,
    buffer_minutes: int = 10,
) -> dict:
    target = campaign(conn, campaign_id)
    baseline = find_incremental_baseline(conn, target, baseline_hours)
    return plan_with_baseline(
        conn=conn,
        campaign_id=campaign_id,
        baseline=baseline,
        baseline_hours=baseline_hours,
        buffer_minutes=buffer_minutes,
    )


def incremental_plan_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    if args.baseline_hours < 1:
        emit({"ok": False, "error": "baseline_hours_must_be_positive"}, 2)
    if args.buffer_minutes < 0:
        emit({"ok": False, "error": "buffer_minutes_must_be_non_negative"}, 2)
    emit(incremental_plan_payload(conn, args.campaign_id, args.baseline_hours, args.buffer_minutes))


def latest_campaign_event(conn: sqlite3.Connection, campaign_id: str, event_type: str) -> dict | None:
    row = conn.execute(
        """
        SELECT detail, created_at_ms
        FROM campaign_events
        WHERE campaign_id=? AND event_type=?
        ORDER BY created_at_ms DESC, id DESC
        LIMIT 1
        """,
        (campaign_id, event_type),
    ).fetchone()
    if not row:
        return None
    try:
        detail = json.loads(row["detail"] or "{}")
    except json.JSONDecodeError:
        detail = {}
    if not isinstance(detail, dict):
        detail = {}
    detail["created_at_ms"] = row["created_at_ms"]
    return detail


def incremental_bypass_status(conn: sqlite3.Connection, campaign_id: str) -> dict:
    return {
        "seeded": latest_campaign_event(conn, campaign_id, "incremental_seeded"),
        "window_completed": latest_campaign_event(conn, campaign_id, "incremental_window_completed"),
    }


def current_month_window_status(conn: sqlite3.Connection, campaign_id: str) -> dict:
    event = latest_campaign_event(conn, campaign_id, "current_month_window_completed")
    return {
        "completed": bool(event),
        "event": event,
    }


def incremental_bypass_missing_reason(conn: sqlite3.Connection, campaign_id: str) -> str:
    status = incremental_bypass_status(conn, campaign_id)
    if not status["seeded"]:
        return "incremental_seed_not_completed"
    if not status["window_completed"]:
        return "incremental_window_not_completed"
    return ""


def recent_baseline_coverage_warning(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    baseline_hours: int = 48,
    min_baseline_sent_users: int = 100,
    min_overlap_ratio: float = 0.02,
) -> dict:
    baseline = find_incremental_baseline(conn, row, baseline_hours)
    if not baseline:
        return {}
    base_sent = sent_range(conn, baseline["campaign_id"])
    sent_users = base_sent["sent_users"] or 0
    if sent_users < min_baseline_sent_users:
        return {}
    overlap = conn.execute(
        """
        SELECT COUNT(DISTINCT c.user_id)
        FROM candidates c
        JOIN send_attempts s ON s.user_id=c.user_id
        WHERE c.campaign_id=?
          AND s.campaign_id=?
          AND s.status='sent'
        """,
        (row["campaign_id"], baseline["campaign_id"]),
    ).fetchone()[0] or 0
    ratio = overlap / sent_users if sent_users else 0
    if ratio >= min_overlap_ratio:
        return {}
    return {
        "warning": "recent_baseline_sent_users_not_covered_by_current_scan",
        "baseline_campaign_id": baseline["campaign_id"],
        "baseline_sent_users": sent_users,
        "overlap_users": overlap,
        "overlap_ratio": ratio,
        "min_overlap_ratio": min_overlap_ratio,
        "message": "current full scan is suspiciously shallow compared with the recent same-account baseline",
    }


def require_incremental_bypass_ready(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    status = incremental_bypass_status(conn, row["campaign_id"])
    if not status["seeded"]:
        emit({
            "ok": False,
            "error": "incremental_seed_not_completed",
            "campaign_id": row["campaign_id"],
            "scan": scan_payload(row),
            "message": "allow_incomplete_scan_requires_seed_incremental_candidates_first",
        }, 2)
    if not status["window_completed"]:
        emit({
            "ok": False,
            "error": "incremental_window_not_completed",
            "campaign_id": row["campaign_id"],
            "scan": scan_payload(row),
            "incremental": status,
            "message": "allow_incomplete_scan_requires_mark_incremental_window_after_strict_window_scan",
        }, 2)


def coverage_missing_reason(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    scope = campaign_coverage_scope(row)
    if scope == "user_list":
        if not current_user_list_import_status(conn)["ready"]:
            return "current_user_list_import_required"
        status = historical_review_status(conn, row["campaign_id"])
        if not status["window_completed"]:
            return "historical_review_window_not_completed"
    if scope in {"month", "combined"} and not (
        scan_is_complete(row) or current_month_window_status(conn, row["campaign_id"])["completed"]
    ):
        return "current_month_scan_not_completed"
    if scope in {"history", "combined"}:
        if not latest_historical_import(conn):
            return "historical_import_required"
        status = historical_review_status(conn, row["campaign_id"])
        if not status["window_completed"]:
            return "historical_review_window_not_completed"
    return ""


def require_coverage_ready(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    missing = coverage_missing_reason(conn, row)
    if not missing:
        if campaign_coverage_scope(row) in {"month", "combined"}:
            warning = recent_baseline_coverage_warning(conn, row)
            if warning:
                emit({
                    "ok": False,
                    "error": "formal_scan_suspicious_recent_baseline_not_covered",
                    "campaign_id": row["campaign_id"],
                    "coverage_scope": campaign_coverage_scope(row),
                    "scan": scan_payload(row),
                    "suspicious_scan": warning,
                    "message": "current_month_scan_completed_but_recent_baseline_overlap_is_too_low",
                }, 2)
        return
    payload = {
        "ok": False,
        "error": missing,
        "campaign_id": row["campaign_id"],
        "coverage_scope": campaign_coverage_scope(row),
        "scan": scan_payload(row),
        "current_user_list_import": current_user_list_import_status(conn),
        "current_month_window": current_month_window_status(conn, row["campaign_id"]),
        "historical_import": historical_import_summary(conn),
        "historical_review": historical_review_status(conn, row["campaign_id"]),
    }
    emit(payload, 2)


def find_reusable_campaign_for_message(
    conn: sqlite3.Connection,
    *,
    message_hash: str,
    account_ids: list[str],
    coverage_scope: str = "",
) -> sqlite3.Row | None:
    rows = conn.execute(
        """
        SELECT * FROM campaigns
        WHERE message_hash=? AND state IN ('draft','approved')
        ORDER BY created_at_ms DESC
        """,
        (message_hash,),
    ).fetchall()
    for row in rows:
        if is_expired(row):
            continue
        if same_account_scope(campaign_account_ids(row), account_ids):
            if coverage_scope and campaign_coverage_scope(row) != coverage_scope:
                continue
            return row
    return None


def operator_plan_payload(
    conn: sqlite3.Connection,
    *,
    message: str,
    account_ids: list[str],
    baseline_hours: int = 48,
    buffer_minutes: int = 10,
    keep_other_active: bool = False,
) -> dict:
    digest = message_digest(message)
    reusable_by_scope = {
        scope: find_reusable_campaign_for_message(
            conn,
            message_hash=digest,
            account_ids=account_ids,
            coverage_scope=scope,
        )
        for scope in sorted(VALID_COVERAGE_SCOPE)
    }
    reusable = reusable_by_scope.get("user_list")
    reference_ms = reusable["created_at_ms"] if reusable else now_ms()
    baseline = find_incremental_baseline_for_scope(
        conn,
        account_ids=account_ids,
        before_ms=reference_ms,
        baseline_hours=baseline_hours,
        exclude_campaign_id=reusable["campaign_id"] if reusable else "",
    )
    payload = plan_with_baseline(
        conn=conn,
        campaign_id=reusable["campaign_id"] if reusable else None,
        baseline=baseline,
        baseline_hours=baseline_hours,
        buffer_minutes=buffer_minutes,
        recommended_mode="latest_user_list_followup",
    )
    payload.update({
        "side_effect_free": True,
        "message_hash": digest,
        "account_ids": account_ids,
        "would_create_campaign": reusable is None,
        "would_reuse_campaign_id": reusable["campaign_id"] if reusable else None,
        "would_reuse_campaigns_by_scope": {
            scope: (row["campaign_id"] if row else None)
            for scope, row in reusable_by_scope.items()
        },
        "would_reset_reused_campaign_state": reusable is not None,
        "would_reset_reused_campaign_scan": reusable is not None,
        "would_stop_active_campaigns": preview_other_active_campaigns(
            conn,
            keep_campaign_id=reusable["campaign_id"] if reusable else "",
            message_hash=digest,
            account_ids=account_ids,
            enabled=not keep_other_active,
        ),
    })
    return payload


def operator_plan_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    if not args.message or not args.message.strip():
        emit({"ok": False, "error": "empty_message"}, 2)
    if args.baseline_hours < 1:
        emit({"ok": False, "error": "baseline_hours_must_be_positive"}, 2)
    if args.buffer_minutes < 0:
        emit({"ok": False, "error": "buffer_minutes_must_be_non_negative"}, 2)
    emit(operator_plan_payload(
        conn,
        message=args.message,
        account_ids=parse_account_ids(args.account_ids),
        baseline_hours=args.baseline_hours,
        buffer_minutes=args.buffer_minutes,
        keep_other_active=args.keep_other_active,
    ))


def seed_incremental_candidates_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    target = campaign(conn, args.campaign_id)
    if target["state"] in {"completed", "stopped"}:
        emit({"ok": False, "error": "campaign_not_active", "state": target["state"]}, 2)
    if args.source_campaign_id:
        source = campaign(conn, args.source_campaign_id)
    else:
        source = find_incremental_baseline(conn, target, args.baseline_hours)
        if not source:
            emit({
                "ok": False,
                "error": "no_incremental_baseline",
                "message": "run incremental-plan or choose full_scan",
            }, 2)
    if not same_account_scope(campaign_account_ids(source), campaign_account_ids(target)):
        emit({
            "ok": False,
            "error": "account_scope_mismatch",
            "source_account_ids": campaign_account_ids(source),
            "target_account_ids": campaign_account_ids(target),
        }, 2)
    source_sent = sent_range_payload(conn, source["campaign_id"])
    if not source_sent["sent_users"]:
        emit({"ok": False, "error": "source_has_no_sent_users"}, 2)
    existing = conn.execute(
        """
        SELECT COUNT(*)
        FROM candidates t
        WHERE t.campaign_id=?
          AND EXISTS (
            SELECT 1 FROM send_attempts s
            WHERE s.campaign_id=?
              AND s.status='sent'
              AND s.conversation_key=t.conversation_key
          )
        """,
        (target["campaign_id"], source["campaign_id"]),
    ).fetchone()[0]
    ts = now_ms()
    conn.execute(
        """
        INSERT INTO candidates
          (campaign_id, conversation_key, user_id, account_id, display_name,
           lead_status, party_status, reply_status, negative_status,
           last_message_at_ms, evidence, eligible, exclusion_reason, updated_at_ms)
        SELECT ?, c.conversation_key, c.user_id, c.account_id, c.display_name,
               c.lead_status, c.party_status, c.reply_status, c.negative_status,
               c.last_message_at_ms,
               'seeded_from_previous_sent campaign=' || ? || '; ' || c.evidence,
               c.eligible, c.exclusion_reason, ?
        FROM candidates c
        WHERE c.campaign_id=?
          AND EXISTS (
            SELECT 1 FROM send_attempts s
            WHERE s.campaign_id=c.campaign_id
              AND s.status='sent'
              AND s.conversation_key=c.conversation_key
          )
          AND NOT EXISTS (
            SELECT 1 FROM candidates t
            WHERE t.campaign_id=? AND t.conversation_key=c.conversation_key
          )
        """,
        (
            target["campaign_id"], source["campaign_id"], ts,
            source["campaign_id"], target["campaign_id"],
        ),
    )
    copied = conn.execute("SELECT changes()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO campaign_events (campaign_id, event_type, detail, created_at_ms)
        VALUES (?, 'incremental_seeded', ?, ?)
        """,
        (
            target["campaign_id"],
            json.dumps({
                "source_campaign_id": source["campaign_id"],
                "source_first_sent_at_ms": source_sent["first_sent_at_ms"],
                "source_last_sent_at_ms": source_sent["last_sent_at_ms"],
                "source_sent_users": source_sent["sent_users"],
                "copied_candidates": copied,
                "skipped_existing_candidates": existing,
            }, ensure_ascii=False),
            ts,
        ),
    )
    conn.commit()
    emit({
        "ok": True,
        "campaign_id": target["campaign_id"],
        "source_campaign_id": source["campaign_id"],
        "source_sent": source_sent,
        "copied_candidates": copied,
        "skipped_existing_candidates": existing,
        "target_counts": summary_payload(conn, target["campaign_id"])["counts"],
    })


def mark_incremental_window_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    target = campaign(conn, args.campaign_id)
    if target["state"] in {"completed", "stopped"}:
        emit({"ok": False, "error": "campaign_not_active", "state": target["state"]}, 2)
    if args.window_start_ms <= 0:
        emit({"ok": False, "error": "invalid_window_start_ms"}, 2)
    seeded = latest_campaign_event(conn, target["campaign_id"], "incremental_seeded")
    if not seeded:
        emit({"ok": False, "error": "incremental_seed_not_completed"}, 2)
    source_campaign_id = args.source_campaign_id or str(seeded.get("source_campaign_id") or "")
    if not source_campaign_id:
        emit({"ok": False, "error": "incremental_source_missing"}, 2)
    if source_campaign_id != str(seeded.get("source_campaign_id") or ""):
        emit({
            "ok": False,
            "error": "incremental_source_mismatch",
            "seeded_source_campaign_id": seeded.get("source_campaign_id"),
            "source_campaign_id": source_campaign_id,
        }, 2)
    source = campaign(conn, source_campaign_id)
    if not same_account_scope(campaign_account_ids(source), campaign_account_ids(target)):
        emit({
            "ok": False,
            "error": "account_scope_mismatch",
            "source_account_ids": campaign_account_ids(source),
            "target_account_ids": campaign_account_ids(target),
        }, 2)
    source_sent = sent_range_payload(conn, source["campaign_id"])
    if not source_sent["sent_users"]:
        emit({"ok": False, "error": "source_has_no_sent_users"}, 2)
    if source_sent["first_sent_at_ms"] and args.window_start_ms > source_sent["first_sent_at_ms"]:
        emit({
            "ok": False,
            "error": "window_start_after_first_sent",
            "window_start_ms": args.window_start_ms,
            "first_sent_at_ms": source_sent["first_sent_at_ms"],
        }, 2)
    ts = now_ms()
    detail = {
        "source_campaign_id": source["campaign_id"],
        "window_start_ms": args.window_start_ms,
        "window_start_at": format_ms(args.window_start_ms),
        "source_first_sent_at_ms": source_sent["first_sent_at_ms"],
        "source_first_sent_at": source_sent["first_sent_at"],
        "reason": args.reason,
    }
    conn.execute(
        """
        INSERT INTO campaign_events (campaign_id, event_type, detail, created_at_ms)
        VALUES (?, 'incremental_window_completed', ?, ?)
        """,
        (target["campaign_id"], json.dumps(detail, ensure_ascii=False), ts),
    )
    conn.commit()
    emit({
        "ok": True,
        "campaign_id": target["campaign_id"],
        "incremental": incremental_bypass_status(conn, target["campaign_id"]),
    })


def import_user_list_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    source_file = args.file.expanduser()
    if not source_file.exists():
        emit({"ok": False, "error": "file_not_found", "file": str(source_file)}, 2)
    try:
        datetime.strptime(args.cutoff_date, "%Y-%m-%d")
    except ValueError:
        emit({"ok": False, "error": "invalid_cutoff_date", "expected": "YYYY-MM-DD"}, 2)
    imported_at = now_ms()
    import_id = args.import_id or historical_import_id(source_file, args.cutoff_date, imported_at)
    rows: list[dict] = []
    with source_file.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"用户ID", "用户昵称", "归属账号", "最新进线时间", "最近开口时间", "最近留资时间", "手机号", "微信号", "留资方式", "用户类型"}
        missing = sorted(required - set(reader.fieldnames or []))
        if missing:
            emit({"ok": False, "error": "missing_required_columns", "missing": missing}, 2)
        for row in reader:
            rows.append(row)
    per_user: dict[str, dict] = {}
    per_user_account: dict[tuple[str, str], dict] = {}
    deleted_users = set()
    for row in rows:
        user_id = normalize_csv_value(row.get("用户ID"))
        if not user_id:
            continue
        current = per_user.setdefault(user_id, {
            "user_id": user_id,
            "display_name": "",
            "latest_in_at_ms": None,
            "latest_open_at_ms": None,
            "latest_lead_at_ms": None,
            "phone_present": 0,
            "wechat_present": 0,
            "lead_status_from_import": "no_lead",
            "deleted": 0,
            "user_type": "",
            "account_names": [],
        })
        display_name = normalize_csv_value(row.get("用户昵称"))
        if display_name and not current["display_name"]:
            current["display_name"] = display_name
        account_name = normalize_csv_value(row.get("归属账号"))
        if account_name and account_name not in current["account_names"]:
            current["account_names"].append(account_name)
        account_key = (user_id, account_name)
        scoped = per_user_account.setdefault(account_key, {
            "user_id": user_id,
            "account_name": account_name,
            "account_id": ACCOUNT_NAME_TO_ID.get(account_name, ""),
            "display_name": "",
            "latest_in_at_ms": None,
            "latest_open_at_ms": None,
            "latest_lead_at_ms": None,
            "phone_present": 0,
            "wechat_present": 0,
            "lead_status_from_import": "no_lead",
            "deleted": 0,
            "user_type": "",
        })
        user_type = normalize_csv_value(row.get("用户类型"))
        if user_type:
            current["user_type"] = user_type
            scoped["user_type"] = user_type
        latest_in = parse_import_time(row.get("最新进线时间"))
        latest_open = parse_import_time(row.get("最近开口时间"))
        latest_lead = parse_import_time(row.get("最近留资时间"))
        for key, value in (
            ("latest_in_at_ms", latest_in),
            ("latest_open_at_ms", latest_open),
            ("latest_lead_at_ms", latest_lead),
        ):
            if value and (current[key] is None or value > current[key]):
                current[key] = value
            if value and (scoped[key] is None or value > scoped[key]):
                scoped[key] = value
        if csv_has_value(row.get("手机号")):
            current["phone_present"] = 1
            scoped["phone_present"] = 1
        if csv_has_value(row.get("微信号")):
            current["wechat_present"] = 1
            scoped["wechat_present"] = 1
        if display_name and not scoped["display_name"]:
            scoped["display_name"] = display_name
        if latest_lead or scoped["phone_present"] or scoped["wechat_present"] or csv_has_value(row.get("留资方式")):
            current["lead_status_from_import"] = "has_lead"
            scoped["lead_status_from_import"] = "has_lead"
        if is_deleted_historical_row(row):
            current["deleted"] = 1
            scoped["deleted"] = 1
            deleted_users.add(user_id)
    for item in per_user.values():
        existing = conn.execute(
            "SELECT * FROM historical_users WHERE user_id=?",
            (item["user_id"],),
        ).fetchone()
        if existing:
            latest_in = max([v for v in (existing["latest_in_at_ms"], item["latest_in_at_ms"]) if v is not None], default=None)
            latest_open = max([v for v in (existing["latest_open_at_ms"], item["latest_open_at_ms"]) if v is not None], default=None)
            latest_lead = max([v for v in (existing["latest_lead_at_ms"], item["latest_lead_at_ms"]) if v is not None], default=None)
            lead_status = "has_lead" if (
                existing["lead_status_from_import"] == "has_lead"
                or item["lead_status_from_import"] == "has_lead"
            ) else "no_lead"
            conn.execute(
                """
                UPDATE historical_users
                SET display_name=COALESCE(NULLIF(?, ''), display_name),
                    latest_in_at_ms=?,
                    latest_open_at_ms=?,
                    latest_lead_at_ms=?,
                    phone_present=MAX(phone_present, ?),
                    wechat_present=MAX(wechat_present, ?),
                    lead_status_from_import=?,
                    deleted=MAX(deleted, ?),
                    user_type=COALESCE(NULLIF(?, ''), user_type),
                    account_names_json=?,
                    source_import_ids_json=?,
                    updated_at_ms=?
                WHERE user_id=?
                """,
                (
                    item["display_name"],
                    latest_in,
                    latest_open,
                    latest_lead,
                    item["phone_present"],
                    item["wechat_present"],
                    lead_status,
                    item["deleted"],
                    item["user_type"],
                    merge_json_list(existing["account_names_json"], item["account_names"]),
                    merge_json_list(existing["source_import_ids_json"], [import_id]),
                    imported_at,
                    item["user_id"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO historical_users
                  (user_id, display_name, latest_in_at_ms, latest_open_at_ms,
                   latest_lead_at_ms, phone_present, wechat_present,
                   lead_status_from_import, deleted, user_type, account_names_json,
                   source_import_ids_json, updated_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["user_id"],
                    item["display_name"],
                    item["latest_in_at_ms"],
                    item["latest_open_at_ms"],
                    item["latest_lead_at_ms"],
                    item["phone_present"],
                    item["wechat_present"],
                    item["lead_status_from_import"],
                    item["deleted"],
                    item["user_type"],
                    json.dumps(item["account_names"], ensure_ascii=False),
                    json.dumps([import_id], ensure_ascii=False),
                    imported_at,
                ),
            )
    for item in per_user_account.values():
        existing = conn.execute(
            "SELECT * FROM historical_user_accounts WHERE user_id=? AND account_name=?",
            (item["user_id"], item["account_name"]),
        ).fetchone()
        if existing:
            latest_in = merge_optional_ms(existing["latest_in_at_ms"], item["latest_in_at_ms"])
            latest_open = merge_optional_ms(existing["latest_open_at_ms"], item["latest_open_at_ms"])
            latest_lead = merge_optional_ms(existing["latest_lead_at_ms"], item["latest_lead_at_ms"])
            lead_status = "has_lead" if (
                existing["lead_status_from_import"] == "has_lead"
                or item["lead_status_from_import"] == "has_lead"
            ) else "no_lead"
            conn.execute(
                """
                UPDATE historical_user_accounts
                SET account_id=COALESCE(NULLIF(?, ''), account_id),
                    display_name=COALESCE(NULLIF(?, ''), display_name),
                    latest_in_at_ms=?,
                    latest_open_at_ms=?,
                    latest_lead_at_ms=?,
                    phone_present=MAX(phone_present, ?),
                    wechat_present=MAX(wechat_present, ?),
                    lead_status_from_import=?,
                    deleted=MAX(deleted, ?),
                    user_type=COALESCE(NULLIF(?, ''), user_type),
                    source_import_ids_json=?,
                    updated_at_ms=?
                WHERE user_id=? AND account_name=?
                """,
                (
                    item["account_id"],
                    item["display_name"],
                    latest_in,
                    latest_open,
                    latest_lead,
                    item["phone_present"],
                    item["wechat_present"],
                    lead_status,
                    item["deleted"],
                    item["user_type"],
                    merge_json_list(existing["source_import_ids_json"], [import_id]),
                    imported_at,
                    item["user_id"],
                    item["account_name"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO historical_user_accounts
                  (user_id, account_name, account_id, display_name, latest_in_at_ms,
                   latest_open_at_ms, latest_lead_at_ms, phone_present, wechat_present,
                   lead_status_from_import, deleted, user_type, source_import_ids_json,
                   updated_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["user_id"],
                    item["account_name"],
                    item["account_id"],
                    item["display_name"],
                    item["latest_in_at_ms"],
                    item["latest_open_at_ms"],
                    item["latest_lead_at_ms"],
                    item["phone_present"],
                    item["wechat_present"],
                    item["lead_status_from_import"],
                    item["deleted"],
                    item["user_type"],
                    json.dumps([import_id], ensure_ascii=False),
                    imported_at,
                ),
            )
    unique_user_ids = len(per_user)
    lead_users = sum(1 for item in per_user.values() if item["lead_status_from_import"] == "has_lead")
    targetable = sum(
        1 for item in per_user.values()
        if item["lead_status_from_import"] == "no_lead" and not item["deleted"]
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO historical_imports
          (import_id, source_file, cutoff_date, imported_at_ms, row_count,
           unique_user_ids, lead_users, targetable_no_lead_users, deleted_users)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            import_id,
            str(source_file),
            args.cutoff_date,
            imported_at,
            len(rows),
            unique_user_ids,
            lead_users,
            targetable,
            len(deleted_users),
        ),
    )
    conn.commit()
    emit({
        "ok": True,
        "import_id": import_id,
        "source_file": str(source_file),
        "cutoff_date": args.cutoff_date,
        "row_count": len(rows),
        "unique_user_ids": unique_user_ids,
        "lead_users": lead_users,
        "targetable_no_lead_users": targetable,
        "deleted_users": len(deleted_users),
    })


def historical_imports_cmd(conn: sqlite3.Connection, _args: argparse.Namespace) -> None:
    imports = [
        dict(row) for row in conn.execute(
            """
            SELECT * FROM historical_imports
            ORDER BY cutoff_date DESC, imported_at_ms DESC
            """
        )
    ]
    emit({
        "ok": True,
        "count": len(imports),
        "imports": imports,
        "summary": historical_import_summary(conn),
    })


def historical_targets_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    camp = campaign(conn, args.campaign_id)
    limit = args.limit if args.limit and args.limit > 0 else 500
    target_import_id = review_target_import_id(conn, args.campaign_id)
    rows = imported_historical_targets(
        conn,
        import_id=target_import_id,
        account_ids=campaign_account_ids(camp),
    )
    reviewed = {
        row["user_id"]: dict(row) for row in conn.execute(
            """
            SELECT user_id, state, retryable_count, reviewed_at_ms
            FROM historical_uid_reviews
            WHERE campaign_id=?
              AND (?='' OR target_import_id=?)
            """,
            (args.campaign_id, target_import_id, target_import_id),
        )
    }
    targets = []
    for row in rows:
        review = reviewed.get(row["user_id"])
        review_state = review["state"] if review else ""
        if args.review_state and review_state != args.review_state:
            continue
        if not args.review_state and review_state and not args.include_reviewed:
            continue
        item = dict(row)
        item["review_state"] = review_state
        item["review_retryable_count"] = int(review["retryable_count"] or 0) if review else 0
        item["reviewed_at_ms"] = int(review["reviewed_at_ms"] or 0) if review else 0
        item["latest_in_at"] = format_ms(item.get("latest_in_at_ms"))
        item["latest_open_at"] = format_ms(item.get("latest_open_at_ms"))
        targets.append(item)
    if args.review_state == "retryable":
        targets.sort(key=lambda item: (
            item["review_retryable_count"],
            item["reviewed_at_ms"],
        ))
    targets = targets[:limit]
    emit({
        "ok": True,
        "campaign_id": args.campaign_id,
        "count": len(targets),
        "total_targetable": len(rows),
        "target_import_id": target_import_id,
        "account_ids": campaign_account_ids(camp),
        "account_names": csv_account_names_for_ids(campaign_account_ids(camp)),
        "review": historical_review_status(conn, args.campaign_id),
        "targets": targets,
    })


def mark_historical_review_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    camp = campaign(conn, args.campaign_id)
    target_import_id = review_target_import_id(conn, args.campaign_id)
    if args.state not in VALID_HISTORICAL_REVIEW:
        emit({"ok": False, "error": "invalid_review_state", "allowed": sorted(VALID_HISTORICAL_REVIEW)}, 2)
    scoped_target = imported_historical_target(conn, camp, args.user_id)
    if not scoped_target:
        emit({
            "ok": False,
            "error": "historical_user_not_in_campaign_target_scope",
            "user_id": args.user_id,
            "target_import_id": target_import_id,
            "account_ids": campaign_account_ids(camp),
        }, 2)
    imported = conn.execute(
        "SELECT * FROM historical_users WHERE user_id=?",
        (args.user_id,),
    ).fetchone()
    if not imported:
        emit({"ok": False, "error": "historical_user_not_imported", "user_id": args.user_id}, 2)
    if target_import_id:
        try:
            source_import_ids = json.loads(imported["source_import_ids_json"] or "[]")
        except json.JSONDecodeError:
            source_import_ids = []
        if target_import_id not in source_import_ids:
            emit({
                "ok": False,
                "error": "user_not_in_current_user_list_import",
                "user_id": args.user_id,
                "target_import_id": target_import_id,
            }, 2)
    if scoped_target["lead_status_from_import"] == "has_lead" and args.state == "eligible":
        emit({"ok": False, "error": "historical_import_has_lead_protection", "user_id": args.user_id}, 2)
    if args.state == "eligible":
        if not args.conversation_key or not args.account_id:
            emit({"ok": False, "error": "eligible_review_requires_conversation_key_and_account_id"}, 2)
        if not account_allowed(camp, args.account_id):
            emit({"ok": False, "error": "account_not_allowed"}, 2)
        item = {
            "conversation_key": args.conversation_key,
            "user_id": args.user_id,
            "account_id": args.account_id,
            "display_name": args.display_name or imported["display_name"] or args.user_id,
            "lead_status": "no_lead",
            "party_status": "customer",
            "reply_status": "awaiting_customer",
            "negative_status": "safe",
            "last_message_at_ms": args.last_message_at_ms,
            "evidence": f"historical_uid_review:{args.reason or 'browser_revalidated'}",
        }
        validate_candidate(item)
        eligible, reason = eligibility(argparse.Namespace(**item), camp["min_silence_hours"])
        conn.execute(CANDIDATE_UPSERT, candidate_values(args.campaign_id, item, eligible, reason))
    retryable_count = 1 if args.state == "retryable" else 0
    existing = conn.execute(
        """
        SELECT retryable_count FROM historical_uid_reviews
        WHERE campaign_id=? AND user_id=?
        """,
        (args.campaign_id, args.user_id),
    ).fetchone()
    if existing and args.state == "retryable":
        retryable_count = existing["retryable_count"] + 1
    conn.execute(
        """
        INSERT INTO historical_uid_reviews
          (campaign_id, user_id, state, conversation_key, account_id, reason,
           reviewed_at_ms, retryable_count, target_import_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(campaign_id, user_id) DO UPDATE SET
          state=excluded.state,
          conversation_key=excluded.conversation_key,
          account_id=excluded.account_id,
          reason=excluded.reason,
          reviewed_at_ms=excluded.reviewed_at_ms,
          retryable_count=excluded.retryable_count,
          target_import_id=excluded.target_import_id
        """,
        (
            args.campaign_id,
            args.user_id,
            args.state,
            args.conversation_key or "",
            args.account_id or "",
            args.reason or "",
            now_ms(),
            retryable_count,
            target_import_id,
        ),
    )
    conn.commit()
    emit({
        "ok": True,
        "campaign_id": args.campaign_id,
        "user_id": args.user_id,
        "state": args.state,
        "review": historical_review_status(conn, args.campaign_id),
    })


def mark_historical_window_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    campaign(conn, args.campaign_id)
    import_summary = historical_import_summary(conn)
    if not import_summary["ready"]:
        emit({"ok": False, "error": "historical_import_required"}, 2)
    status = historical_review_status(conn, args.campaign_id)
    terminal = status["reviewed_terminal_users"]
    total = status["target_users"]
    if terminal < total and not args.force:
        emit({
            "ok": False,
            "error": "historical_review_incomplete",
            "campaign_id": args.campaign_id,
            "review": status,
            "message": "all_imported_no_lead_historical_uids_must_be_reviewed_or_force_marked",
        }, 2)
    detail = {
        "target_users": total,
        "reviewed_terminal_users": terminal,
        "retryable_users": status["retryable_users"],
        "target_import_id": status.get("target_import_id", ""),
        "forced": bool(args.force),
        "reason": args.reason,
    }
    conn.execute(
        """
        INSERT INTO campaign_events (campaign_id, event_type, detail, created_at_ms)
        VALUES (?, 'historical_review_window_completed', ?, ?)
        """,
        (args.campaign_id, json.dumps(detail, ensure_ascii=False), now_ms()),
    )
    conn.commit()
    emit({
        "ok": True,
        "campaign_id": args.campaign_id,
        "review": historical_review_status(conn, args.campaign_id),
    })


def mark_current_month_window_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    row = campaign(conn, args.campaign_id)
    window_start_date = parse_iso_date_arg(args.window_start_date, "window_start_date")
    visible_oldest_date = parse_iso_date_arg(args.visible_oldest_date, "visible_oldest_date")
    if visible_oldest_date > window_start_date:
        emit({
            "ok": False,
            "error": "current_month_window_not_reached",
            "campaign_id": args.campaign_id,
            "window_start_date": window_start_date,
            "visible_oldest_date": visible_oldest_date,
            "message": "visible_oldest_date_must_be_on_or_before_window_start_date",
        }, 2)
    detail = {
        "window_start_date": window_start_date,
        "visible_oldest_date": visible_oldest_date,
        "reason": args.reason,
    }
    conn.execute(
        """
        INSERT INTO campaign_events (campaign_id, event_type, detail, created_at_ms)
        VALUES (?, 'current_month_window_completed', ?, ?)
        """,
        (args.campaign_id, json.dumps(detail, ensure_ascii=False), now_ms()),
    )
    conn.commit()
    emit({
        "ok": True,
        "campaign_id": args.campaign_id,
        "coverage_scope": campaign_coverage_scope(row),
        "current_month_window": current_month_window_status(conn, args.campaign_id),
    })


def campaign_has_open_batches(conn: sqlite3.Connection, campaign_id: str) -> bool:
    return bool(
        conn.execute(
            """
            SELECT 1 FROM campaign_batches
            WHERE campaign_id=? AND state IN ('draft','approved','in_progress')
            LIMIT 1
            """,
            (campaign_id,),
        ).fetchone()
    )


def campaign_has_review_items(conn: sqlite3.Connection, campaign_id: str) -> bool:
    return bool(
        conn.execute(
            """
            SELECT 1
            FROM batch_items bi
            JOIN campaign_batches b ON b.batch_id=bi.batch_id
            WHERE b.campaign_id=?
              AND bi.state IN ('uncertain','failed')
            LIMIT 1
            """,
            (campaign_id,),
        ).fetchone()
    )


def campaign_has_uncertain_items(conn: sqlite3.Connection, campaign_id: str) -> bool:
    return bool(
        conn.execute(
            """
            SELECT 1
            FROM batch_items bi
            JOIN campaign_batches b ON b.batch_id=bi.batch_id
            WHERE b.campaign_id=? AND bi.state='uncertain'
            LIMIT 1
            """,
            (campaign_id,),
        ).fetchone()
    )


def batch_has_uncertain_items(conn: sqlite3.Connection, batch_id: str) -> bool:
    return bool(
        conn.execute(
            """
            SELECT 1 FROM batch_items
            WHERE batch_id=? AND state='uncertain'
            LIMIT 1
            """,
            (batch_id,),
        ).fetchone()
    )


def maybe_complete_campaign(conn: sqlite3.Connection, campaign_id: str) -> bool:
    row = campaign(conn, campaign_id)
    if row["state"] not in {"draft", "approved"}:
        return False
    if coverage_missing_reason(conn, row):
        return False
    has_batches = bool(
        conn.execute(
            "SELECT 1 FROM campaign_batches WHERE campaign_id=? LIMIT 1",
            (campaign_id,),
        ).fetchone()
    )
    if not has_batches:
        return False
    if campaign_has_open_batches(conn, campaign_id):
        return False
    if campaign_has_review_items(conn, campaign_id):
        return False
    if remaining_queue(conn, campaign_id):
        return False
    conn.execute(
        "UPDATE campaigns SET state='completed' WHERE campaign_id=?",
        (campaign_id,),
    )
    return True


def scan_is_complete(row: sqlite3.Row) -> bool:
    return row["scan_completed_at_ms"] is not None and row["scan_stopped_reason"] == "completed"


def scan_payload(row: sqlite3.Row) -> dict:
    return {
        "started_at_ms": row["scan_started_at_ms"],
        "completed_at_ms": row["scan_completed_at_ms"],
        "stopped_reason": row["scan_stopped_reason"],
        "complete": scan_is_complete(row),
    }


def format_ms(value: int | None) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def human_reason_label(reason: object) -> str:
    text = str(reason or "").strip()
    if not text:
        return ""
    if "," in text or ";" in text:
        parts = [part.strip() for part in re.split(r"[;,]", text) if part.strip()]
        labels = [human_reason_label(part) for part in parts]
        return "；".join(label for label in labels if label)
    if text in REASON_LABELS:
        return REASON_LABELS[text]
    for prefix, label in REASON_PREFIX_LABELS:
        if text.startswith(prefix):
            if "{hours}" in label:
                hours = text[len(prefix):].rstrip("h") or "?"
                return label.format(hours=hours)
            suffix = text[len(prefix):]
            if label.endswith("：") and suffix:
                return label + human_reason_label(suffix)
            return label
    return text


def doctor_decision(counts: dict, scan: dict) -> dict:
    if not scan["complete"] and counts.get("pending_batch_users", 0) > 0:
        next_action = "stop_invalid_open_batch_then_rescan"
        status = "invalid_open_batch_before_scan_complete"
    elif not scan["complete"]:
        next_action = "run_probe_then_continue_full_scan"
        status = "needs_full_scan"
    elif counts.get("uncertain_users", 0) > 0:
        next_action = "manual_review_uncertain_users"
        status = "needs_uncertain_review"
    elif counts.get("pending_batch_users", 0) > 0:
        next_action = "send_pending_batch_or_resume_old_queue_if_user_explicitly_asked"
        status = "has_pending_batch"
    elif counts.get("failed_users", 0) > 0:
        next_action = "manual_review_failed_users_before_closing_campaign"
        status = "needs_failed_review"
    elif counts.get("remaining_queueable_users", 0) > 0:
        next_action = "prepare_and_approve_next_batch"
        status = "ready_to_prepare_batch"
    else:
        next_action = "completed_or_no_eligible_users"
        status = "no_pending_work"
    return {
        "status": status,
        "next_action": next_action,
        "plain_zh": {
            "needs_full_scan": "还没正式扫完，不能建队列或发送。",
            "invalid_open_batch_before_scan_complete": "状态异常：扫描未完成但存在待发送队列。必须先停止该队列并重新全量扫描。",
            "has_pending_batch": "已有待发送队列；明确继续旧队列时可发送 pending。",
            "ready_to_prepare_batch": "覆盖条件已满足，还有可入队客户，可以生成下一批队列。",
            "needs_uncertain_review": "有发送结果不确定的人，需要先人工核对，再继续队列。",
            "needs_failed_review": "没有待发送客户，但有发送失败记录，需要人工检查后再关闭活动。",
            "no_pending_work": "当前活动没有待处理客户。",
        }[status],
    }


def item_operator_status(state: str, retryable_count: int) -> str:
    if state == "pending" and retryable_count:
        return "待发送（曾定位失败）"
    return STATUS_LABELS.get(state, state)


def message_version_label(snapshot_hash: str | None, current_hash: str | None, attempted: bool) -> str:
    if not attempted:
        return ""
    if not snapshot_hash:
        return MESSAGE_VERSION_UNKNOWN
    if snapshot_hash == current_hash:
        return MESSAGE_VERSION_CURRENT
    return MESSAGE_VERSION_PREVIOUS


def is_restorable_technical_reason(reason: str | None) -> bool:
    text = str(reason or "")
    return text in RESTORABLE_TECHNICAL_REASONS or any(
        text.startswith(prefix) for prefix in RESTORABLE_TECHNICAL_REASON_PREFIXES
    )


def activity_kind_label(kind: str | None) -> str:
    return {
        "send": "发送记录",
        "retryable_locate_failure": "定位可重试",
        "technical_skip_restored": "技术恢复",
        "message_updated": "文案更新",
        "uncertain_resolved": "不确定已处理",
        "failed_resolved": "失败已处理",
    }.get(str(kind or ""), str(kind or ""))


def activity_status_label(status: str | None) -> str:
    text = str(status or "")
    return STATUS_LABELS.get(text, {
        "retryable_locate_failure": "待发送（定位失败）",
        "technical_skip_restored": "已恢复为待发送",
        "message_updated": "已更新",
        "uncertain_resolved": "已处理",
        "failed_resolved": "已处理",
    }.get(text, text))


def activity_detail_label(kind: str | None, detail: str | None) -> str:
    text = str(detail or "")
    if kind == "message_updated":
        try:
            payload = json.loads(text or "{}")
        except json.JSONDecodeError:
            payload = {}
        old_hash = str(payload.get("old_message_hash") or "")
        new_hash = str(payload.get("new_message_hash") or "")
        reason = str(payload.get("reason") or "")
        parts = []
        if old_hash or new_hash:
            parts.append(f"文案哈希 {old_hash[:8] or '?'} -> {new_hash[:8] or '?'}")
        if reason:
            parts.append(f"原因：{reason}")
        return "；".join(parts) or "文案已更新"
    if kind in {"uncertain_resolved", "failed_resolved"}:
        try:
            payload = json.loads(text or "{}")
        except json.JSONDecodeError:
            payload = {}
        user_id = str(payload.get("user_id") or "")
        resolution = str(payload.get("resolution") or "")
        batches = payload.get("updated_batches") or []
        state = str(payload.get("new_batch_item_state") or payload.get("new_status") or "")
        parts = []
        if user_id:
            parts.append(f"UID：{user_id}")
        if resolution:
            parts.append(f"处理：{resolution}")
        if state:
            parts.append(f"新状态：{state}")
        if batches:
            parts.append(f"批次：{','.join(str(x) for x in batches)}")
        return "；".join(parts) or text
    return text


def require_completed_scan(conn: sqlite3.Connection, row: sqlite3.Row, *, allow_incomplete: bool = False) -> None:
    if scan_is_complete(row):
        warning = recent_baseline_coverage_warning(conn, row)
        if warning:
            emit({
                "ok": False,
                "error": "formal_scan_suspicious_recent_baseline_not_covered",
                "campaign_id": row["campaign_id"],
                "scan": scan_payload(row),
                "suspicious_scan": warning,
                "message": "full_scan_completed_but_recent_baseline_overlap_is_too_low",
            }, 2)
        return
    if allow_incomplete:
        require_incremental_bypass_ready(conn, row)
        return
    emit({
        "ok": False,
        "error": "formal_scan_not_completed",
        "campaign_id": row["campaign_id"],
        "scan": scan_payload(row),
        "message": "default_daily_mode_requires_completed_scan_before_queue_or_send",
    }, 2)


def require_queue_coverage_ready(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    allow_incomplete: bool = False,
) -> None:
    if allow_incomplete and campaign_coverage_scope(row) == "month":
        require_completed_scan(conn, row, allow_incomplete=True)
        return
    require_coverage_ready(conn, row)


def mark_scan_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    row = campaign(conn, args.campaign_id)
    ts = now_ms()
    if args.state == "started":
        conn.execute(
            """
            UPDATE campaigns
            SET scan_started_at_ms=COALESCE(scan_started_at_ms, ?),
                scan_completed_at_ms=NULL,
                scan_stopped_reason=''
            WHERE campaign_id=?
              AND scan_completed_at_ms IS NULL
            """,
            (ts, args.campaign_id),
        )
    elif args.state == "completed":
        warning = recent_baseline_coverage_warning(conn, row)
        if warning and not args.force:
            emit({
                "ok": False,
                "error": "scan_completion_suspicious_recent_baseline_not_covered",
                "campaign_id": args.campaign_id,
                "scan": scan_payload(row),
                "suspicious_scan": warning,
                "message": "refusing_to_mark_completed_without_operator_review",
            }, 2)
        conn.execute(
            """
            UPDATE campaigns
            SET scan_started_at_ms=COALESCE(scan_started_at_ms, ?),
                scan_completed_at_ms=?,
                scan_stopped_reason='completed'
            WHERE campaign_id=?
            """,
            (ts, ts, args.campaign_id),
        )
    else:
        reason = args.reason or "stopped"
        conn.execute(
            """
            UPDATE campaigns
            SET scan_started_at_ms=COALESCE(scan_started_at_ms, ?),
                scan_completed_at_ms=NULL,
                scan_stopped_reason=?
            WHERE campaign_id=?
              AND (scan_completed_at_ms IS NULL OR ?)
            """,
            (ts, reason, args.campaign_id, int(args.force)),
        )
    conn.commit()
    emit({
        "ok": True,
        "campaign_id": args.campaign_id,
        "scan": scan_payload(campaign(conn, args.campaign_id)),
        "previous_scan": scan_payload(row),
    })


def invalidate_scan_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    row = campaign(conn, args.campaign_id)
    reason = args.reason or "manual_invalidated_suspicious_scan"
    ts = now_ms()
    stopped_batches = stop_open_batches_for_campaign(
        conn,
        args.campaign_id,
        reason=reason,
        stopped_at=ts,
    )
    conn.execute(
        """
        UPDATE campaigns
        SET scan_started_at_ms=COALESCE(scan_started_at_ms, ?),
            scan_completed_at_ms=NULL,
            scan_stopped_reason=?
        WHERE campaign_id=?
        """,
        (ts, reason, args.campaign_id),
    )
    conn.commit()
    emit({
        "ok": True,
        "campaign_id": args.campaign_id,
        "reason": reason,
        "stopped_open_batches": stopped_batches,
        "scan": scan_payload(campaign(conn, args.campaign_id)),
        "previous_scan": scan_payload(row),
    })


def batch_payload(conn: sqlite3.Connection, batch_id: str) -> dict:
    b = batch(conn, batch_id)
    camp = campaign(conn, b["campaign_id"])
    counts = {
        row["state"]: row["n"] for row in conn.execute(
            "SELECT state, COUNT(*) n FROM batch_items WHERE batch_id=? GROUP BY state",
            (batch_id,),
        )
    }
    items = [
        dict(row) for row in conn.execute(
            """
            SELECT bi.conversation_key, bi.user_id, bi.account_id, bi.position,
                   bi.state, bi.reason, COALESCE(ev.retryable_count, 0) AS retryable_count
            FROM batch_items bi
            LEFT JOIN (
              SELECT conversation_key, COUNT(*) AS retryable_count
              FROM batch_item_events
              WHERE batch_id=? AND event_type='retryable_locate_failure'
              GROUP BY conversation_key
            ) ev ON ev.conversation_key=bi.conversation_key
            WHERE bi.batch_id=?
            ORDER BY COALESCE(ev.retryable_count, 0), bi.position
            """,
            (batch_id, batch_id),
        )
    ]
    names = {
        row["conversation_key"]: row["display_name"] for row in conn.execute(
            "SELECT conversation_key, display_name FROM candidates WHERE campaign_id=?",
            (b["campaign_id"],),
        )
    }
    account_counts = [
        dict(row) for row in conn.execute(
            """
            SELECT account_id, COUNT(*) AS users
            FROM batch_items WHERE batch_id=?
            GROUP BY account_id ORDER BY account_id
            """,
            (batch_id,),
        )
    ]
    technical_events = {
        row["event_type"]: row["n"] for row in conn.execute(
            """
            SELECT event_type, COUNT(*) AS n
            FROM batch_item_events WHERE batch_id=?
            GROUP BY event_type
            """,
            (batch_id,),
        )
    }
    return {
        "ok": True, "batch_id": batch_id, "campaign_id": b["campaign_id"],
        "sequence_no": b["sequence_no"], "max_size": b["max_size"],
        "state": b["state"], "message": camp["message"],
        "expires_at_ms": camp["expires_at_ms"], "counts": counts,
        "accounts": account_counts,
        "technical_events": technical_events,
        "items": [dict(item, display_name=names.get(item["conversation_key"], "")) for item in items],
    }


def batch_report_rows(conn: sqlite3.Connection, batch_id: str) -> list[dict]:
    b = batch(conn, batch_id)
    rows = []
    for row in conn.execute(
        """
        SELECT bi.position, bi.conversation_key, bi.user_id, bi.account_id,
               bi.state, bi.reason, bi.updated_at_ms,
               c.display_name, c.lead_status, c.party_status,
               c.reply_status, c.negative_status, c.eligible,
               c.exclusion_reason, c.last_message_at_ms,
               s.status AS attempt_status, s.detail AS attempt_detail,
               s.account_id AS attempt_account_id,
               s.attempted_at_ms, COALESCE(s.message_snapshot, '') AS message_snapshot,
               COALESCE(s.message_hash_snapshot, '') AS message_hash_snapshot,
               camp.message_hash AS current_message_hash,
               COALESCE(ev.retryable_count, 0) AS retryable_count,
               COALESCE(ev.retryable_reasons, '') AS retryable_reasons
        FROM batch_items bi
        JOIN campaign_batches b ON b.batch_id=bi.batch_id
        JOIN campaigns camp ON camp.campaign_id=b.campaign_id
        LEFT JOIN candidates c
          ON c.campaign_id=? AND c.conversation_key=bi.conversation_key
        LEFT JOIN send_attempts s
          ON s.id=(
            SELECT sx.id FROM send_attempts sx
            WHERE sx.campaign_id=? AND sx.conversation_key=bi.conversation_key
              AND (sx.batch_id=? OR COALESCE(sx.batch_id, '')='')
            ORDER BY CASE WHEN sx.batch_id=? THEN 0 ELSE 1 END, sx.id DESC
            LIMIT 1
          )
        LEFT JOIN (
          SELECT conversation_key, COUNT(*) AS retryable_count,
                 GROUP_CONCAT(DISTINCT reason) AS retryable_reasons
          FROM batch_item_events
          WHERE batch_id=? AND event_type='retryable_locate_failure'
          GROUP BY conversation_key
        ) ev ON ev.conversation_key=bi.conversation_key
        WHERE bi.batch_id=?
        ORDER BY bi.position
        """,
        (b["campaign_id"], b["campaign_id"], batch_id, batch_id, batch_id, batch_id),
    ):
        item = dict(row)
        retryable_count = int(item.get("retryable_count") or 0)
        item["account_name"] = ACCOUNT_NAMES.get(item["account_id"], item["account_id"])
        item["attempt_account_name"] = (
            ACCOUNT_NAMES.get(item["attempt_account_id"], item["attempt_account_id"])
            if item.get("attempt_account_id") else ""
        )
        item["report_account_name"] = item["attempt_account_name"] or item["account_name"]
        item["operator_status"] = item_operator_status(item["state"], retryable_count)
        item["updated_at"] = format_ms(item["updated_at_ms"])
        item["attempted_at"] = format_ms(item["attempted_at_ms"])
        item["last_message_at"] = format_ms(item["last_message_at_ms"])
        item["message_snapshot_recorded"] = "yes" if item.get("message_snapshot") else "no"
        item["message_version"] = message_version_label(
            item.get("message_hash_snapshot"),
            item.get("current_message_hash"),
            bool(item.get("attempt_status")),
        )
        item["reason_label"] = human_reason_label(item.get("reason"))
        item["attempt_detail_label"] = human_reason_label(item.get("attempt_detail"))
        item["retryable_reasons_label"] = human_reason_label(item.get("retryable_reasons"))
        item["next_action"] = {
            "sent": "无需操作",
            "skipped": "无需发送",
            "failed": "人工检查",
            "uncertain": "人工核对是否已发",
            "pending": "继续发送",
        }.get(item["state"], "")
        if item["state"] == "pending" and retryable_count:
            item["next_action"] = "修复定位后继续发送"
        rows.append(item)
    return rows


def batch_report_payload(conn: sqlite3.Connection, batch_id: str) -> dict:
    payload = batch_payload(conn, batch_id)
    rows = batch_report_rows(conn, batch_id)
    by_status = {}
    by_account = {}
    message_versions = {}
    sent_message_versions = {}
    for row in rows:
        by_status[row["operator_status"]] = by_status.get(row["operator_status"], 0) + 1
        account = row.get("report_account_name") or row["account_name"]
        by_account.setdefault(account, {})
        by_account[account][row["operator_status"]] = by_account[account].get(row["operator_status"], 0) + 1
        if row.get("attempt_status"):
            version = row.get("message_version") or MESSAGE_VERSION_UNKNOWN
            message_versions[version] = message_versions.get(version, 0) + 1
            if row.get("state") == "sent":
                sent_message_versions[version] = sent_message_versions.get(version, 0) + 1
    missing_message_snapshots = sum(
        1 for row in rows
        if row["state"] in {"sent", "failed", "uncertain"} and not row.get("message_snapshot")
    )
    return {
        "ok": True,
        "batch": payload,
        "generated_at": format_ms(now_ms()),
        "rows": rows,
        "by_status": by_status,
        "by_account": by_account,
        "message_versions": message_versions,
        "sent_message_versions": sent_message_versions,
        "missing_message_snapshots": missing_message_snapshots,
    }


def write_batch_report_files(report: dict, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    batch_id = report["batch"]["batch_id"]
    csv_path = output_dir / f"{batch_id}-status.csv"
    md_path = output_dir / f"{batch_id}-summary.md"
    headers = [
        ("position", "队列序号"),
        ("operator_status", "当前状态"),
        ("next_action", "下一步"),
        ("account_name", "账号"),
        ("attempt_account_name", "实际发送账号"),
        ("user_id", "小红书UID"),
        ("display_name", "昵称"),
        ("conversation_key", "会话键"),
        ("attempted_at", "发送/尝试时间"),
        ("updated_at", "状态更新时间"),
        ("reason", "队列原因"),
        ("reason_label", "队列原因说明"),
        ("attempt_detail", "发送验证/跳过原因"),
        ("attempt_detail_label", "发送验证/跳过说明"),
        ("retryable_count", "技术定位次数"),
        ("retryable_reasons", "技术定位原因"),
        ("retryable_reasons_label", "技术定位说明"),
        ("lead_status", "留资状态"),
        ("party_status", "同行状态"),
        ("reply_status", "回复状态"),
        ("negative_status", "负面状态"),
        ("exclusion_reason", "当前排除原因"),
        ("message_snapshot_recorded", "是否记录文案快照"),
        ("message_version", "文案版本"),
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[label for _, label in headers])
        writer.writeheader()
        for row in report["rows"]:
            writer.writerow({label: row.get(key, "") for key, label in headers})

    batch_info = report["batch"]
    lines = [
        f"# 小红书私信队列报告：{batch_id}",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 活动 ID：{batch_info['campaign_id']}",
        f"- 队列序号：{batch_info['sequence_no']}",
        f"- 队列状态：{batch_info['state']}",
        f"- 队列总人数：{sum(batch_info['counts'].values())}",
    ]
    for label, count in report["by_status"].items():
        lines.append(f"- {label}：{count}")
    sent_versions = report.get("sent_message_versions") or {}
    current_sent = sent_versions.get(MESSAGE_VERSION_CURRENT, 0)
    previous_sent = sent_versions.get(MESSAGE_VERSION_PREVIOUS, 0)
    unknown_sent = sent_versions.get(MESSAGE_VERSION_UNKNOWN, 0)
    lines.append(
        f"- 已发送文案版本：当前文案 {current_sent}，历史文案 {previous_sent}，未知 {unknown_sent}"
    )
    if previous_sent:
        lines.append("- 注意：历史文案已发送不等于当前文案已发送。")
    technical_events = batch_info.get("technical_events") or {}
    if technical_events:
        lines.append(f"- 技术定位事件：{sum(technical_events.values())}")
    if report["missing_message_snapshots"]:
        lines.append(
            f"- 文案快照缺失：{report['missing_message_snapshots']} 条历史发送记录未保存实际文案快照"
        )
    lines.extend(["", "## 按账号汇总", ""])
    for account, counts in sorted(report["by_account"].items()):
        parts = "，".join(f"{status} {count}" for status, count in counts.items())
        lines.append(f"- {account}：{parts}")
    lines.extend([
        "",
        "## 未完成项",
        "",
        "| 序号 | 状态 | 账号 | 小红书UID | 昵称 | 原因 | 下一步 | 技术定位次数 |",
        "|---:|---|---|---|---|---|---|---:|",
    ])
    for row in report["rows"]:
        if row["state"] == "sent":
            continue
        nickname = str(row.get("display_name") or "").replace("|", " ").strip()
        reason = str(row.get("attempt_detail_label") or row.get("reason_label") or row.get("retryable_reasons_label") or "").replace("|", " ").strip()
        lines.append(
            f"| {row['position']} | {row['operator_status']} | {row['account_name']} | "
            f"{row['user_id']} | {nickname} | {reason} | {row['next_action']} | {row['retryable_count']} |"
        )
    lines.extend([
        "",
        "## 已发送项",
        "",
        "| 序号 | 队列账号 | 实际发送账号 | 小红书UID | 昵称 | 发送/尝试时间 | 文案快照 | 文案版本 |",
        "|---:|---|---|---|---|---|---|---|",
    ])
    for row in report["rows"]:
        if row["state"] != "sent":
            continue
        nickname = str(row.get("display_name") or "").replace("|", " ").strip()
        lines.append(
            f"| {row['position']} | {row['account_name']} | {row.get('attempt_account_name') or row['account_name']} | {row['user_id']} | "
            f"{nickname} | {row['attempted_at']} | {row['message_snapshot_recorded']} | "
            f"{row['message_version']} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"csv": str(csv_path), "markdown": str(md_path)}


def export_batch_report_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    report = batch_report_payload(conn, args.batch_id)
    output_dir = args.output_dir or Path.cwd() / "reports" / "xhs-followup"
    files = write_batch_report_files(report, output_dir.expanduser())
    emit({
        "ok": True,
        "batch_id": args.batch_id,
        "campaign_id": report["batch"]["campaign_id"],
        "counts": report["batch"]["counts"],
        "by_status": report["by_status"],
        "by_account": report["by_account"],
        "message_versions": report["message_versions"],
        "sent_message_versions": report["sent_message_versions"],
        "missing_message_snapshots": report["missing_message_snapshots"],
        "files": files,
    })


def current_or_latest_batch_id(conn: sqlite3.Connection, campaign_id: str) -> str | None:
    row = conn.execute(
        """
        SELECT batch_id FROM campaign_batches
        WHERE campaign_id=? AND state IN ('draft','approved','in_progress')
        ORDER BY sequence_no LIMIT 1
        """,
        (campaign_id,),
    ).fetchone()
    if row:
        return row["batch_id"]
    row = conn.execute(
        """
        SELECT batch_id FROM campaign_batches
        WHERE campaign_id=?
        ORDER BY sequence_no DESC LIMIT 1
        """,
        (campaign_id,),
    ).fetchone()
    return row["batch_id"] if row else None


def campaign_candidate_breakdown(conn: sqlite3.Connection, campaign_id: str) -> dict:
    rows = conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN eligible=1 THEN 1 ELSE 0 END) AS eligible_snapshot,
          SUM(CASE WHEN lead_status='has_lead' THEN 1 ELSE 0 END) AS has_lead,
          SUM(CASE WHEN lead_status='unknown' THEN 1 ELSE 0 END) AS lead_unknown,
          SUM(CASE WHEN party_status='peer' THEN 1 ELSE 0 END) AS peer,
          SUM(CASE WHEN party_status='unknown' THEN 1 ELSE 0 END) AS party_unknown,
          SUM(CASE WHEN reply_status='awaiting_us' THEN 1 ELSE 0 END) AS awaiting_us,
          SUM(CASE WHEN reply_status='unknown' THEN 1 ELSE 0 END) AS reply_unknown,
          SUM(CASE WHEN negative_status='blocked' THEN 1 ELSE 0 END) AS blocked,
          SUM(CASE WHEN negative_status='unknown' THEN 1 ELSE 0 END) AS negative_unknown
        FROM candidates WHERE campaign_id=?
        """,
        (campaign_id,),
    ).fetchone()
    return {key: rows[key] or 0 for key in rows.keys()}


def campaign_batches_dashboard(conn: sqlite3.Connection, campaign_id: str) -> list[dict]:
    batches = []
    for row in conn.execute(
        """
        SELECT b.batch_id, b.sequence_no, b.max_size, b.state,
               b.created_at_ms, b.approved_at_ms, b.completed_at_ms,
               SUM(CASE WHEN bi.state='pending' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN bi.state='sent' THEN 1 ELSE 0 END) AS sent,
               SUM(CASE WHEN bi.state='skipped' THEN 1 ELSE 0 END) AS skipped,
               SUM(CASE WHEN bi.state='failed' THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN bi.state='uncertain' THEN 1 ELSE 0 END) AS uncertain,
               COUNT(bi.conversation_key) AS total
        FROM campaign_batches b
        LEFT JOIN batch_items bi ON bi.batch_id=b.batch_id
        WHERE b.campaign_id=?
        GROUP BY b.batch_id
        ORDER BY b.sequence_no
        """,
        (campaign_id,),
    ):
        item = dict(row)
        item["created_at"] = format_ms(item["created_at_ms"])
        item["approved_at"] = format_ms(item["approved_at_ms"])
        item["completed_at"] = format_ms(item["completed_at_ms"])
        for key in ("pending", "sent", "skipped", "failed", "uncertain", "total"):
            item[key] = item[key] or 0
        item["progress_pct"] = round(((item["sent"] + item["skipped"] + item["failed"] + item["uncertain"]) / item["total"] * 100), 1) if item["total"] else 0
        batches.append(item)
    return batches


def recent_activity_rows(conn: sqlite3.Connection, campaign_id: str, limit: int = 30) -> list[dict]:
    rows = []
    for row in conn.execute(
        """
        SELECT s.id, s.attempted_at_ms AS ts, 'send' AS kind, s.conversation_key,
               s.user_id, s.account_id, s.status, s.detail, s.batch_id,
               c.display_name
        FROM send_attempts s
        LEFT JOIN candidates c
          ON c.campaign_id=s.campaign_id AND c.conversation_key=s.conversation_key
        WHERE s.campaign_id=?
        ORDER BY s.attempted_at_ms DESC, s.id DESC LIMIT ?
        """,
        (campaign_id, limit),
    ):
        item = dict(row)
        if not item.get("batch_id"):
            match = conn.execute(
                """
                SELECT bi.batch_id, bi.position
                FROM batch_items bi
                JOIN campaign_batches b ON b.batch_id=bi.batch_id
                WHERE b.campaign_id=? AND bi.conversation_key=?
                ORDER BY
                  CASE WHEN bi.state=? THEN 0 ELSE 1 END,
                  ABS(COALESCE(bi.updated_at_ms, 0) - ?),
                  b.sequence_no DESC
                LIMIT 1
                """,
                (campaign_id, item["conversation_key"], item["status"], item["ts"]),
            ).fetchone()
            if match:
                item["batch_id"] = match["batch_id"]
                item["position"] = match["position"]
            else:
                item["position"] = ""
        else:
            match = conn.execute(
                "SELECT position FROM batch_items WHERE batch_id=? AND conversation_key=?",
                (item["batch_id"], item["conversation_key"]),
            ).fetchone()
            item["position"] = match["position"] if match else ""
        item["time"] = format_ms(item["ts"])
        item["account_name"] = ACCOUNT_NAMES.get(item["account_id"], item["account_id"] or "")
        item["kind_label"] = activity_kind_label(item.get("kind"))
        item["status_label"] = activity_status_label(item.get("status"))
        item["detail_label"] = activity_detail_label(item.get("kind"), item.get("detail"))
        rows.append(item)
    for row in conn.execute(
        """
        SELECT e.created_at_ms AS ts, e.event_type AS kind, e.conversation_key,
               bi.user_id, bi.account_id, bi.state AS status, e.reason AS detail,
               c.display_name, e.batch_id, bi.position
        FROM batch_item_events e
        JOIN campaign_batches b ON b.batch_id=e.batch_id
        LEFT JOIN batch_items bi
          ON bi.batch_id=e.batch_id AND bi.conversation_key=e.conversation_key
        LEFT JOIN candidates c
          ON c.campaign_id=b.campaign_id AND c.conversation_key=e.conversation_key
        WHERE b.campaign_id=?
        ORDER BY e.created_at_ms DESC, e.id DESC LIMIT ?
        """,
        (campaign_id, limit),
    ):
        item = dict(row)
        item["time"] = format_ms(item["ts"])
        item["account_name"] = ACCOUNT_NAMES.get(item["account_id"], item["account_id"] or "")
        item["kind_label"] = activity_kind_label(item.get("kind"))
        item["status_label"] = activity_status_label(item.get("status"))
        item["detail_label"] = activity_detail_label(item.get("kind"), item.get("detail"))
        rows.append(item)
    for row in conn.execute(
        """
        SELECT created_at_ms AS ts, event_type AS kind, '' AS conversation_key,
               '' AS user_id, '' AS account_id, event_type AS status, detail,
               '' AS display_name, '' AS batch_id, '' AS position
        FROM campaign_events
        WHERE campaign_id=?
        ORDER BY created_at_ms DESC, id DESC LIMIT ?
        """,
        (campaign_id, limit),
    ):
        item = dict(row)
        item["time"] = format_ms(item["ts"])
        item["account_name"] = ""
        item["kind_label"] = activity_kind_label(item.get("kind"))
        item["status_label"] = activity_status_label(item.get("status"))
        item["detail_label"] = activity_detail_label(item.get("kind"), item.get("detail"))
        rows.append(item)
    rows.sort(key=lambda item: int(item.get("ts") or 0), reverse=True)
    rows = rows[:limit]
    return rows


def dashboard_payload(conn: sqlite3.Connection, campaign_id: str | None = None, batch_id: str | None = None) -> dict:
    if not campaign_id and batch_id:
        campaign_id = batch(conn, batch_id)["campaign_id"]
    if not campaign_id:
        latest = conn.execute(
            """
            SELECT campaign_id FROM campaigns
            WHERE state IN ('draft','approved')
            ORDER BY created_at_ms DESC LIMIT 1
            """
        ).fetchone()
        campaign_id = latest["campaign_id"] if latest else None
    if not campaign_id:
        return {"ok": True, "campaign": None}

    summary = summary_payload(conn, campaign_id)
    batch_id = batch_id or current_or_latest_batch_id(conn, campaign_id)
    batch_report = batch_report_payload(conn, batch_id) if batch_id else None
    batches = campaign_batches_dashboard(conn, campaign_id)
    candidate_breakdown = campaign_candidate_breakdown(conn, campaign_id)
    recent = recent_activity_rows(conn, campaign_id)
    return {
        "ok": True,
        "generated_at": format_ms(now_ms()),
        "campaign": summary,
        "candidate_breakdown": candidate_breakdown,
        "batches": batches,
        "current_batch_id": batch_id,
        "current_batch": batch_report,
        "recent_activity": recent,
    }


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def status_class(value: str) -> str:
    text = str(value or "")
    if "已发送" in text or text == "sent":
        return "good"
    if "待发送" in text or text == "pending":
        return "todo"
    if "跳过" in text or text == "skipped":
        return "muted"
    if "失败" in text or "uncertain" in text or "核对" in text:
        return "warn"
    return ""


def write_dashboard_html(data: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not data.get("campaign"):
        output_path.write_text(
            "<!doctype html><meta charset='utf-8'><title>XHS 看板</title><p>当前没有活动。</p>",
            encoding="utf-8",
        )
        return

    campaign = data["campaign"]
    counts = campaign.get("counts", {})
    doctor = campaign.get("doctor", {})
    batch_report = data.get("current_batch")
    batch = batch_report["batch"] if batch_report else None
    rows = batch_report["rows"] if batch_report else []
    candidate = data.get("candidate_breakdown", {})
    batches = data.get("batches", [])
    recent = data.get("recent_activity", [])
    sent = counts.get("sent_users", 0)
    sent_current_campaign = counts.get("sent_current_message_users", 0)
    sent_previous_campaign = counts.get("sent_previous_message_users", 0)
    pending = counts.get("pending_batch_users", 0)
    failed = counts.get("failed_users", 0)
    uncertain = counts.get("uncertain_users", 0)
    assigned = counts.get("assigned_users", 0)
    remaining = counts.get("remaining_queueable_users", 0)
    eligible = counts.get("eligible_conversations", 0)
    total_candidates = counts.get("total", 0)
    batch_counts = (batch or {}).get("counts", {}) if batch else {}
    batch_pending = batch_counts.get("pending", 0)
    batch_skipped = batch_counts.get("skipped", 0)
    batch_failed = batch_counts.get("failed", 0)
    batch_uncertain = batch_counts.get("uncertain", 0)
    batch_total = sum(batch_counts.values()) if batch else 0
    batch_done = batch_total - batch_pending
    batch_progress = round(batch_done / batch_total * 100, 1) if batch_total else 0
    message_preview = " ".join(str(campaign.get("message", "")).split())
    if len(message_preview) > 180:
        message_preview = message_preview[:180] + "..."
    pending_rows = [row for row in rows if row["state"] == "pending"]
    retry_rows = [row for row in rows if int(row.get("retryable_count") or 0) > 0]
    skipped_rows = [row for row in rows if row["state"] == "skipped"]
    review_rows = [row for row in rows if row["state"] in {"failed", "uncertain"}]
    sent_rows = [row for row in rows if row["state"] == "sent"]
    sent_current_version = sum(1 for row in sent_rows if row.get("message_version") == MESSAGE_VERSION_CURRENT)
    sent_previous_version = sum(1 for row in sent_rows if row.get("message_version") == MESSAGE_VERSION_PREVIOUS)
    sent_unknown_version = sum(1 for row in sent_rows if row.get("message_version") == MESSAGE_VERSION_UNKNOWN)
    missing_snapshots = (batch_report or {}).get("missing_message_snapshots", 0)
    allowed_account_names = "、".join(ACCOUNT_NAMES.get(account_id, account_id) for account_id in campaign.get("account_ids", []))
    open_batch_text = "、".join(campaign.get("open_batch_ids", [])) or "无"
    scan_status = "已完成" if campaign.get("scan", {}).get("complete") else "未完成"
    current_batch_state = (batch or {}).get("state", "") or "无"
    report_files = data.get("report_files") or {}
    report_note = ""
    if report_files:
        csv_name = Path(report_files.get("csv", "")).name
        md_name = Path(report_files.get("markdown", "")).name
        report_note = (
            "<div class='note'>完整队列报告："
            f"<a href='{esc(csv_name)}'>CSV</a> · "
            f"<a href='{esc(md_name)}'>Markdown</a></div>"
        )

    def metric(title: str, value: object, hint: str = "") -> str:
        return (
            "<div class='metric'>"
            f"<div class='metric-title'>{esc(title)}</div>"
            f"<div class='metric-value'>{esc(value)}</div>"
            f"<div class='metric-hint'>{esc(hint)}</div>"
            "</div>"
        )

    def table(headers: list[str], body: list[list[object]]) -> str:
        head = "".join(f"<th>{esc(h)}</th>" for h in headers)
        rows_html = []
        for row in body:
            rows_html.append("<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>")
        return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows_html)}</tbody></table>"

    batch_rows = [
        [
            f"b{item['sequence_no']:03d}",
            item["state"],
            item["total"],
            item["sent"],
            item["pending"],
            item["skipped"],
            item["failed"],
            item["uncertain"],
            str(item["progress_pct"]) + "%",
        ]
        for item in batches
    ]
    account_rows = []
    for account in sorted((batch_report or {}).get("by_account", {}).keys()):
        status_counts = (batch_report or {}).get("by_account", {}).get(account, {})
        account_rows.append([
            account,
            status_counts.get("已发送", 0),
            status_counts.get("待发送", 0) + status_counts.get("待发送（曾定位失败）", 0),
            status_counts.get("已跳过", 0),
            status_counts.get("发送失败", 0),
            status_counts.get("待人工核对", 0),
        ])

    queue_headers = ["序号", "状态", "队列账号", "实际账号", "UID", "昵称", "原因", "下一步", "定位次数"]

    def queue_cells(row: dict) -> list[object]:
        return [
            row["position"],
            row["operator_status"],
            row["account_name"],
            row.get("attempt_account_name") or "",
            row["user_id"],
            row.get("display_name", ""),
            row.get("attempt_detail_label") or row.get("reason_label") or row.get("retryable_reasons_label") or "",
            row["next_action"],
            row["retryable_count"],
        ]

    pending_table = table(queue_headers, [queue_cells(row) for row in pending_rows])
    retry_table = table(queue_headers, [queue_cells(row) for row in retry_rows])
    skipped_table = table(queue_headers, [queue_cells(row) for row in skipped_rows])
    review_table = table(queue_headers, [queue_cells(row) for row in review_rows])
    recent_table = table(
        ["时间", "类型", "状态", "账号", "UID", "昵称", "详情"],
        [
            [
                row.get("time", ""),
                row.get("kind_label", row.get("kind", "")),
                row.get("status_label", row.get("status", "")),
                row.get("account_name", ""),
                row.get("user_id", ""),
                row.get("display_name", ""),
                row.get("detail_label", row.get("detail", "")),
            ]
            for row in recent
        ],
    )
    sent_table = table(
        ["序号", "队列账号", "实际账号", "UID", "昵称", "时间", "文案快照", "文案版本"],
        [
            [
                row["position"],
                row["account_name"],
                row.get("attempt_account_name") or row["account_name"],
                row["user_id"],
                row.get("display_name", ""),
                row.get("attempted_at", ""),
                row.get("message_snapshot_recorded", ""),
                row.get("message_version", ""),
            ]
            for row in sent_rows
        ],
    )

    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>小红书私信跟进看板</title>
<style>
:root {{
  color-scheme: light;
  --bg: #f6f7f9;
  --panel: #ffffff;
  --line: #d9dee7;
  --text: #172033;
  --sub: #667085;
  --good: #0f7b44;
  --todo: #245fc8;
  --warn: #b45309;
  --bad: #b42318;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text); font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
header {{ position: sticky; top: 0; z-index: 2; background: rgba(246,247,249,.96); border-bottom: 1px solid var(--line); padding: 14px 24px; }}
h1 {{ margin: 0 0 4px; font-size: 20px; }}
h2 {{ margin: 0 0 12px; font-size: 16px; }}
.sub {{ color: var(--sub); font-size: 13px; }}
.wrap {{ max-width: 1500px; margin: 0 auto; padding: 20px 24px 48px; }}
.grid {{ display: grid; gap: 12px; }}
.metrics {{ grid-template-columns: repeat(6, minmax(140px, 1fr)); }}
.metric, section {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }}
.metric {{ padding: 12px; min-height: 92px; }}
.metric-title {{ color: var(--sub); font-size: 12px; }}
.metric-value {{ font-size: 28px; font-weight: 700; margin-top: 4px; }}
.metric-hint {{ color: var(--sub); font-size: 12px; margin-top: 2px; }}
section {{ padding: 16px; margin-top: 14px; overflow: hidden; }}
.two {{ grid-template-columns: minmax(0, 1.2fr) minmax(360px, .8fr); }}
.bar {{ height: 14px; border-radius: 999px; background: #e6ebf2; overflow: hidden; border: 1px solid var(--line); }}
.bar span {{ display: block; height: 100%; background: #2f6fed; width: {batch_progress}%; }}
.status {{ display: inline-block; border-radius: 999px; padding: 2px 8px; border: 1px solid var(--line); background: #f8fafc; }}
.good {{ color: var(--good); }}
.todo {{ color: var(--todo); }}
.warn {{ color: var(--warn); }}
.bad {{ color: var(--bad); }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th, td {{ border-bottom: 1px solid var(--line); padding: 8px 7px; text-align: left; vertical-align: top; }}
th {{ color: var(--sub); background: #fafbfc; font-weight: 600; position: sticky; top: 63px; z-index: 1; }}
td:nth-child(6) {{ max-width: 320px; overflow-wrap: anywhere; }}
.scroll {{ max-height: 560px; overflow: auto; border: 1px solid var(--line); border-radius: 8px; }}
.note {{ color: var(--sub); margin-top: 8px; }}
.preflight {{ grid-template-columns: repeat(4, minmax(160px, 1fr)); }}
.preflight div {{ border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: #fafbfc; }}
.preflight b {{ display: block; color: var(--sub); font-size: 12px; margin-bottom: 4px; }}
@media (max-width: 1100px) {{ .metrics {{ grid-template-columns: repeat(2, 1fr); }} .two {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<header>
  <h1>小红书私信跟进看板</h1>
  <div class="sub">活动 {esc(campaign['campaign_id'])} · 当前队列 {esc(data.get('current_batch_id') or '未建队列')} · 生成 {esc(data['generated_at'])} · 页面每 5 秒自动刷新</div>
</header>
<main class="wrap">
  <div class="grid metrics">
    {metric("当前队列进度", f"{batch_done}/{batch_total}", f"{batch_progress}% 已处理")}
    {metric("已发送", f"{sent_current_campaign}/{sent}", "当前文案 / 本活动累计")}
    {metric("待发送", pending, "当前已入队 pending")}
    {metric("候选池可排队", remaining, "未进入队列且仍可排队")}
    {metric("候选池当前可发", eligible, f"候选记录 {total_candidates}")}
    {metric("异常", f"{uncertain}/{failed}", "uncertain / failed")}
  </div>

  <section>
    <h2>发送前体检</h2>
    <div><span class="status {status_class(doctor.get('status', ''))}">{esc(doctor.get('status', ''))}</span> {esc(doctor.get('plain_zh', ''))}</div>
    <div class="grid preflight">
      <div><b>扫描状态</b>{esc(scan_status)}</div>
      <div><b>当前队列</b>{esc(data.get('current_batch_id') or '未建队列')} / {esc(current_batch_state)}</div>
      <div><b>Open batch</b>{esc(open_batch_text)}</div>
      <div><b>候选池剩余</b>{esc(remaining)} 可排队</div>
      <div><b>Pending</b>{esc(pending)}</div>
      <div><b>Uncertain / Failed</b>{esc(uncertain)} / {esc(failed)}</div>
      <div><b>授权账号</b>{esc(allowed_account_names)}</div>
      <div><b>历史文案快照债务</b>{esc(counts.get('send_attempts_missing_message_snapshots', 0))}</div>
    </div>
    <div class="note">当前发送文案预览：{esc(message_preview)}</div>
    <div class="note">本活动已发送中：当前文案 {sent_current_campaign}，历史文案 {sent_previous_campaign}。</div>
    <div class="note">本队列已发送中：当前文案 {sent_current_version}，历史文案 {sent_previous_version}，未知 {sent_unknown_version}；文案快照缺失 {missing_snapshots}。</div>
  </section>

  <section>
    <h2>当前队列</h2>
    <div class="bar"><span></span></div>
    <div class="note">已处理 {batch_done} / {batch_total}，待发送 {batch_pending}，已跳过 {batch_skipped}，待人工核对 {batch_uncertain}，失败 {batch_failed}</div>
    {report_note}
  </section>

  <div class="grid two">
    <section>
      <h2>队列历史</h2>
      {table(["队列", "状态", "总数", "已发", "待发", "跳过", "失败", "核对", "进度"], batch_rows)}
    </section>
    <section>
      <h2>账号分布</h2>
      {table(["账号", "已发", "待发", "跳过", "失败", "核对"], account_rows)}
    </section>
  </div>

  <section>
    <h2>候选池分布</h2>
    <div class="grid metrics">
      {metric("扫描入库", candidate.get('total', 0), "candidates 表")}
      {metric("当前符合", eligible, "动态去重后")}
      {metric("已留资", candidate.get('has_lead', 0), "保护排除")}
      {metric("同行/服务商", candidate.get('peer', 0), "保护排除")}
      {metric("客户待我方回复", candidate.get('awaiting_us', 0), "不召回")}
      {metric("未知状态", candidate.get('lead_unknown', 0) + candidate.get('party_unknown', 0) + candidate.get('reply_unknown', 0) + candidate.get('negative_unknown', 0), "需保守处理")}
    </div>
  </section>

  <section>
    <h2>待发送</h2>
    <div class="scroll">{pending_table}</div>
  </section>

  <div class="grid two">
    <section>
      <h2>定位多次</h2>
      <div class="scroll">{retry_table}</div>
    </section>
    <section>
      <h2>已跳过</h2>
      <div class="scroll">{skipped_table}</div>
    </section>
  </div>

  <section>
    <h2>待人工核对 / 失败</h2>
    <div class="scroll">{review_table}</div>
  </section>

  <div class="grid two">
    <section>
      <h2>最近流水</h2>
      <div class="scroll">{recent_table}</div>
    </section>
    <section>
      <h2>本队列已发送</h2>
      <div class="scroll">{sent_table}</div>
    </section>
  </div>
</main>
</body>
</html>
"""
    output_path.write_text(html_text, encoding="utf-8")


def export_dashboard_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    data = dashboard_payload(conn, args.campaign_id, args.batch_id)
    output_path = args.output or Path.cwd() / "reports" / "xhs-followup" / "dashboard.html"
    if data.get("current_batch"):
        data["report_files"] = write_batch_report_files(data["current_batch"], output_path.expanduser().parent)
    write_dashboard_html(data, output_path.expanduser())
    payload = {
        "ok": True,
        "file": str(output_path.expanduser()),
        "report_files": data.get("report_files", {}),
        "generated_at": data.get("generated_at"),
        "campaign_id": data.get("campaign", {}).get("campaign_id") if data.get("campaign") else None,
        "current_batch_id": data.get("current_batch_id"),
    }
    if data.get("campaign"):
        payload["counts"] = data["campaign"].get("counts", {})
    emit(payload)


def summary_payload(conn: sqlite3.Connection, campaign_id: str) -> dict:
    if maybe_complete_campaign(conn, campaign_id):
        conn.commit()
    row = campaign(conn, campaign_id)
    counts = conn.execute(
        """
        SELECT COUNT(*) total, SUM(eligible) eligible_conversations,
          SUM(CASE WHEN lead_status='has_lead' THEN 1 ELSE 0 END) has_lead,
          SUM(CASE WHEN party_status='peer' THEN 1 ELSE 0 END) peer,
          SUM(CASE WHEN reply_status='awaiting_us' THEN 1 ELSE 0 END) awaiting_us,
          SUM(CASE WHEN negative_status='blocked' THEN 1 ELSE 0 END) blocked,
          SUM(CASE WHEN lead_status='unknown' OR party_status='unknown'
                    OR reply_status='unknown' OR negative_status='unknown'
                   THEN 1 ELSE 0 END) unknown
        FROM candidates WHERE campaign_id=?
        """,
        (campaign_id,),
    ).fetchone()
    attempts = {
        r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM send_attempts WHERE campaign_id=? GROUP BY status",
            (campaign_id,),
        )
    }
    queue = eligible_queue(conn, campaign_id)
    remaining = remaining_queue(conn, campaign_id)
    cutoff = now_ms() - row["min_silence_hours"] * 60 * 60 * 1000
    dynamic_eligible_conversations = conn.execute(
        """
        SELECT COUNT(*) FROM candidates
        WHERE campaign_id=?
          AND lead_status='no_lead' AND party_status='customer'
          AND reply_status='awaiting_customer' AND negative_status='safe'
          AND last_message_at_ms<=?
        """,
        (campaign_id, cutoff),
    ).fetchone()[0]
    accounts = [
        dict(r) for r in conn.execute(
            """
            SELECT account_id, COUNT(*) AS conversations
            FROM candidates WHERE campaign_id=?
            GROUP BY account_id ORDER BY account_id
            """,
            (campaign_id,),
        )
    ]
    batches = [
        dict(r) for r in conn.execute(
            """
            SELECT batch_id, sequence_no, max_size, state
            FROM campaign_batches WHERE campaign_id=? ORDER BY sequence_no
            """,
            (campaign_id,),
        )
    ]
    batch_item_counts = {
        r["state"]: r["n"] for r in conn.execute(
            """
            SELECT bi.state, COUNT(*) AS n
            FROM batch_items bi
            JOIN campaign_batches b ON b.batch_id=bi.batch_id
            WHERE b.campaign_id=?
            GROUP BY bi.state
            """,
            (campaign_id,),
        )
    }
    send_version_counts = conn.execute(
        """
        SELECT
          COUNT(DISTINCT CASE WHEN status='sent' THEN user_id END) AS sent_total,
          COUNT(DISTINCT CASE WHEN status='sent' AND message_hash_snapshot=? THEN user_id END) AS sent_current_message,
          COUNT(DISTINCT CASE WHEN status='sent' AND COALESCE(message_hash_snapshot, '')<>? THEN user_id END) AS sent_previous_message,
          SUM(CASE WHEN status IN ('sent','failed','uncertain')
                    AND COALESCE(message_snapshot, '')=''
                   THEN 1 ELSE 0 END) AS missing_attempt_snapshots
        FROM send_attempts
        WHERE campaign_id=?
        """,
        (row["message_hash"], row["message_hash"], campaign_id),
    ).fetchone()
    assigned_users_count = conn.execute(
        """
        SELECT COUNT(DISTINCT bi.user_id)
        FROM batch_items bi
        JOIN campaign_batches b ON b.batch_id=bi.batch_id
        WHERE b.campaign_id=?
        """,
        (campaign_id,),
    ).fetchone()[0]
    normalized = {key: (counts[key] or 0) for key in counts.keys()}
    normalized["eligible_conversations"] = dynamic_eligible_conversations
    normalized["eligible_users"] = len(queue)
    normalized["eligible_unsent_users"] = len(queue)
    normalized["remaining_unassigned_users"] = len(remaining)
    normalized["eligible_users_total"] = len(queue)
    normalized["assigned_users"] = assigned_users_count
    normalized["pending_batch_users"] = batch_item_counts.get("pending", 0)
    normalized["sent_users"] = batch_item_counts.get("sent", 0)
    normalized["sent_current_message_users"] = send_version_counts["sent_current_message"] or 0
    normalized["sent_previous_message_users"] = send_version_counts["sent_previous_message"] or 0
    normalized["send_attempts_missing_message_snapshots"] = send_version_counts["missing_attempt_snapshots"] or 0
    normalized["failed_users"] = batch_item_counts.get("failed", 0)
    normalized["business_skipped_users"] = batch_item_counts.get("skipped", 0)
    normalized["uncertain_users"] = batch_item_counts.get("uncertain", 0)
    normalized["remaining_queueable_users"] = len(remaining)
    open_batch_ids = [
        item["batch_id"] for item in batches
        if item["state"] in {"draft", "approved", "in_progress"}
    ]
    uncertain_rows = [
        dict(r) for r in conn.execute(
            """
            SELECT s.user_id, s.account_id, s.conversation_key, s.detail,
                   MAX(s.attempted_at_ms) AS attempted_at_ms
            FROM send_attempts s
            WHERE s.campaign_id=? AND s.status='uncertain'
            GROUP BY s.user_id, s.account_id, s.conversation_key, s.detail
            ORDER BY attempted_at_ms DESC LIMIT 20
            """,
            (campaign_id,),
        )
    ]
    scan = scan_payload(row)
    coverage = {
        "scope": campaign_coverage_scope(row),
        "ready": not bool(coverage_missing_reason(conn, row)),
        "missing_reason": coverage_missing_reason(conn, row),
        "current_user_list_import": current_user_list_import_status(conn),
        "current_month_window": current_month_window_status(conn, campaign_id),
        "historical_import": historical_import_summary(conn),
        "historical_review": historical_review_status(conn, campaign_id),
    }
    suspicious_scan = recent_baseline_coverage_warning(conn, row) if scan.get("complete") else {}
    doctor_scan = dict(scan)
    if coverage["ready"]:
        doctor_scan["complete"] = True
    doctor = doctor_decision(normalized, doctor_scan)
    if coverage["missing_reason"] == "current_user_list_import_required":
        doctor = {
            "status": "current_user_list_import_required",
            "next_action": "import_latest_user_list_csv",
            "plain_zh": "需要先导入足够新的线索管理用户列表 CSV；表格只负责发现和留资保护，且必须按本轮账号范围过滤归属账号，发送前仍要逐个 UID 复核。",
        }
    elif coverage["missing_reason"] == "historical_import_required":
        doctor = {
            "status": "historical_import_required",
            "next_action": "import_user_list_csv_before_history_scope",
            "plain_zh": "内部历史 UID 复核需要先导入线索管理用户列表 CSV；CSV 只作为 UID 来源，不直接授权发送。",
        }
    elif coverage["missing_reason"] == "historical_review_window_not_completed":
        doctor = {
            "status": "historical_review_window_not_completed",
            "next_action": "review_historical_uid_targets_then_mark_window",
            "plain_zh": "历史 UID 还没有复核完成，不能建队列或发送。",
        }
    elif coverage["missing_reason"] == "current_month_scan_not_completed":
        doctor = {
            "status": "current_month_scan_not_completed",
            "next_action": "scan_current_month_window",
            "plain_zh": "本月私信窗口还没扫完，不能建队列或发送。",
        }
    if suspicious_scan:
        doctor = {
            "status": "scan_suspicious_needs_rescan",
            "next_action": "invalidate_scan_stop_open_batch_then_restart_full_scan",
            "plain_zh": "扫描完成标记可疑：近期同账号基线发送用户几乎没有被本轮扫描覆盖。必须撤销本轮扫描完成并重新全量扫描。",
        }
    return {
        "ok": True, "campaign_id": campaign_id, "state": row["state"],
        "message": row["message"], "batch_size": row["batch_size"],
        "account_ids": campaign_account_ids(row),
        "coverage_scope": campaign_coverage_scope(row),
        "created_at_ms": row["created_at_ms"],
        "expires_at_ms": row["expires_at_ms"], "expired": is_expired(row),
        "scan": scan,
        "coverage": coverage,
        "suspicious_scan": suspicious_scan,
        "counts": normalized, "attempts": attempts, "batches": batches,
        "doctor": doctor,
        "open_batch_ids": open_batch_ids,
        "accounts": accounts,
        "uncertain_samples": uncertain_rows,
        "eligible_samples": [
            {key: r[key] for key in ("conversation_key", "display_name", "evidence")}
            for r in remaining[:10]
        ],
    }


def summary_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    emit(summary_payload(conn, args.campaign_id))


def preflight_payload(conn: sqlite3.Connection, campaign_id: str | None = None, batch_id: str | None = None) -> dict:
    if not campaign_id and batch_id:
        campaign_id = batch(conn, batch_id)["campaign_id"]
    if not campaign_id:
        latest = conn.execute(
            """
            SELECT campaign_id FROM campaigns
            WHERE state IN ('draft','approved')
            ORDER BY created_at_ms DESC LIMIT 1
            """
        ).fetchone()
        campaign_id = latest["campaign_id"] if latest else None
    if not campaign_id:
        return {
            "ok": True,
            "status": "no_active_campaign",
            "plain_zh": "当前没有活动。先确认文案和账号范围，再启动扫描。",
            "next_action": "confirm_message_and_account_ids_then_start",
        }

    summary = summary_payload(conn, campaign_id)
    current_batch_id = batch_id or current_or_latest_batch_id(conn, campaign_id)
    batch_info = None
    if current_batch_id:
        batch_info = batch_payload(conn, current_batch_id)
    message_preview = " ".join(str(summary.get("message") or "").split())
    if len(message_preview) > 180:
        message_preview = message_preview[:180] + "..."
    counts = summary.get("counts", {})
    doctor = summary.get("doctor") or doctor_decision(counts, summary.get("scan", {}))
    account_names = [
        ACCOUNT_NAMES.get(account_id, account_id)
        for account_id in summary.get("account_ids", [])
    ]
    queue_counts = batch_info.get("counts", {}) if batch_info else {}
    return {
        "ok": True,
        "generated_at": format_ms(now_ms()),
        "campaign_id": campaign_id,
        "current_batch_id": current_batch_id or "",
        "current_batch_state": batch_info.get("state", "") if batch_info else "",
        "current_batch_counts": queue_counts,
        "message_preview": message_preview,
        "authorized_accounts": account_names,
        "coverage_scope": summary.get("coverage_scope", "user_list"),
        "coverage": summary.get("coverage", {}),
        "coverage_ready": bool(summary.get("coverage", {}).get("ready")),
        "scan_complete": bool(summary.get("scan", {}).get("complete")),
        "scan": summary.get("scan", {}),
        "open_batch_ids": summary.get("open_batch_ids", []),
        "pending_batch_users": counts.get("pending_batch_users", 0),
        "uncertain_users": counts.get("uncertain_users", 0),
        "failed_users": counts.get("failed_users", 0),
        "remaining_queueable_users": counts.get("remaining_queueable_users", 0),
        "sent_current_message_users": counts.get("sent_current_message_users", 0),
        "sent_previous_message_users": counts.get("sent_previous_message_users", 0),
        "missing_message_snapshots": counts.get("send_attempts_missing_message_snapshots", 0),
        "next_action": doctor.get("next_action", ""),
        "plain_zh": doctor.get("plain_zh", ""),
        "status": doctor.get("status", ""),
    }


def preflight_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    emit(preflight_payload(conn, args.campaign_id, args.batch_id))


def candidate_keys_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    campaign(conn, args.campaign_id)
    rows = conn.execute(
        """
        SELECT conversation_key
        FROM candidates
        WHERE campaign_id=?
        ORDER BY updated_at_ms, conversation_key
        """,
        (args.campaign_id,),
    ).fetchall()
    keys = [row["conversation_key"] for row in rows]
    emit({
        "ok": True,
        "campaign_id": args.campaign_id,
        "count": len(keys),
        "keys": keys,
    })


def latest_cmd(conn: sqlite3.Connection, _args: argparse.Namespace) -> None:
    active_rows = conn.execute(
        """
        SELECT campaign_id FROM campaigns
        WHERE state IN ('draft','approved')
        ORDER BY created_at_ms DESC
        """
    ).fetchall()
    for active in active_rows:
        maybe_complete_campaign(conn, active["campaign_id"])
    if active_rows:
        conn.commit()
    row = conn.execute(
        """
        SELECT c.campaign_id,
          CASE
            WHEN EXISTS (
              SELECT 1 FROM campaign_batches b
              JOIN batch_items bi ON bi.batch_id=b.batch_id
              WHERE b.campaign_id=c.campaign_id
                AND b.state IN ('approved','in_progress')
                AND bi.state='pending'
            ) THEN 0
            WHEN EXISTS (
              SELECT 1 FROM campaign_batches b
              WHERE b.campaign_id=c.campaign_id
                AND b.state IN ('draft','approved','in_progress')
            ) THEN 1
            WHEN c.scan_completed_at_ms IS NULL THEN 2
            ELSE 3
          END AS priority
        FROM campaigns c
        WHERE c.state IN ('draft','approved')
        ORDER BY priority, c.created_at_ms DESC LIMIT 1
        """
    ).fetchone()
    if not row:
        emit({"ok": True, "campaign": None})
    emit({"ok": True, "campaign": summary_payload(conn, row["campaign_id"])})


def stop_campaign_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    camp = campaign(conn, args.campaign_id)
    if camp["state"] in {"completed", "stopped"}:
        emit({
            "ok": True, "campaign_id": args.campaign_id,
            "state": camp["state"], "changed": False,
        })
    stopped_at = now_ms()
    open_batches = stop_open_batches_for_campaign(
        conn,
        args.campaign_id,
        reason=args.reason,
        stopped_at=stopped_at,
    )
    conn.execute(
        "UPDATE campaigns SET state='stopped' WHERE campaign_id=?",
        (args.campaign_id,),
    )
    conn.commit()
    emit({
        "ok": True, "campaign_id": args.campaign_id,
        "state": "stopped", "changed": True,
        "stopped_batches": len(open_batches), "reason": args.reason,
    })


def prepare_batch_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    camp = campaign(conn, args.campaign_id)
    if camp["state"] in {"completed", "stopped"}:
        emit({"ok": False, "error": "campaign_not_active", "state": camp["state"]}, 2)
    if is_expired(camp):
        emit({"ok": False, "error": "campaign_expired"}, 2)
    require_queue_coverage_ready(conn, camp, allow_incomplete=args.allow_incomplete_scan)
    if campaign_has_uncertain_items(conn, args.campaign_id):
        emit({
            "ok": False,
            "error": "batch_has_uncertain_requires_manual_review",
            "campaign_id": args.campaign_id,
            "message": "resolve_uncertain_before_preparing_next_batch",
        }, 2)
    open_batch = conn.execute(
        """
        SELECT batch_id, state FROM campaign_batches
        WHERE campaign_id=? AND state IN ('draft','approved','in_progress')
        ORDER BY sequence_no LIMIT 1
        """,
        (args.campaign_id,),
    ).fetchone()
    if open_batch:
        emit({
            "ok": False, "error": "open_batch_exists",
            "batch_id": open_batch["batch_id"], "state": open_batch["state"],
        }, 2)
    sequence_no = conn.execute(
        "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM campaign_batches WHERE campaign_id=?",
        (args.campaign_id,),
    ).fetchone()[0]
    requested = camp["batch_size"] if args.limit is None else args.limit
    if not 1 <= requested <= min(camp["batch_size"], MAX_BATCH_SIZE):
        emit({
            "ok": False, "error": "invalid_batch_limit",
            "campaign_batch_size": camp["batch_size"],
        }, 2)
    rows = remaining_queue(conn, args.campaign_id)[:requested]
    if not rows:
        conn.execute(
            "UPDATE campaigns SET state='completed' WHERE campaign_id=?",
            (args.campaign_id,),
        )
        conn.commit()
        emit({
            "ok": True, "campaign_id": args.campaign_id,
            "batch_id": None, "state": "completed", "remaining": 0,
        })
    batch_id = f"{args.campaign_id}-b{sequence_no:03d}"
    conn.execute(
        """
        INSERT INTO campaign_batches
          (batch_id, campaign_id, sequence_no, max_size, state,
           created_at_ms, approved_at_ms, completed_at_ms)
        VALUES (?, ?, ?, ?, 'draft', ?, NULL, NULL)
        """,
        (batch_id, args.campaign_id, sequence_no, requested, now_ms()),
    )
    conn.executemany(
        """
        INSERT INTO batch_items
          (batch_id, conversation_key, user_id, account_id, position,
           state, reason, updated_at_ms)
        VALUES (?, ?, ?, ?, ?, 'pending', '', ?)
        """,
        [
            (batch_id, row["conversation_key"], row["user_id"], row["account_id"], pos, now_ms())
            for pos, row in enumerate(rows, start=1)
        ],
    )
    conn.commit()
    emit(batch_payload(conn, batch_id))


def batch_summary_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    emit(batch_payload(conn, args.batch_id))


def approve_batch_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    b = batch(conn, args.batch_id)
    camp = campaign(conn, b["campaign_id"])
    require_queue_coverage_ready(conn, camp, allow_incomplete=getattr(args, "allow_incomplete_scan", False))
    if is_expired(camp):
        emit({"ok": False, "error": "campaign_expired"}, 2)
    if b["state"] != "draft":
        emit({"ok": False, "error": "batch_not_draft", "state": b["state"]}, 2)
    conn.execute(
        "UPDATE campaign_batches SET state='approved', approved_at_ms=? WHERE batch_id=?",
        (now_ms(), args.batch_id),
    )
    conn.execute(
        "UPDATE campaigns SET state='approved', approved_at_ms=COALESCE(approved_at_ms, ?) WHERE campaign_id=?",
        (now_ms(), b["campaign_id"]),
    )
    conn.commit()
    emit(batch_payload(conn, args.batch_id))


def batch_queue_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    b = batch(conn, args.batch_id)
    camp = campaign(conn, b["campaign_id"])
    require_queue_coverage_ready(conn, camp, allow_incomplete=args.allow_incomplete_scan)
    if batch_has_uncertain_items(conn, args.batch_id):
        emit({
            "ok": False,
            "error": "batch_has_uncertain_requires_manual_review",
            "batch_id": args.batch_id,
            "campaign_id": b["campaign_id"],
            "message": "resolve_uncertain_before_resuming_batch",
        }, 2)
    if b["state"] == "completed":
        payload = batch_payload(conn, args.batch_id)
        payload["queue"] = []
        emit(payload)
    if b["state"] not in {"approved", "in_progress"}:
        emit({"ok": False, "error": "batch_not_approved", "state": b["state"]}, 2)
    payload = batch_payload(conn, args.batch_id)
    payload["queue"] = [item for item in payload.pop("items") if item["state"] == "pending"]
    emit(payload)


def queue_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    camp = campaign(conn, args.campaign_id)
    rows = remaining_queue(conn, args.campaign_id)
    missing = coverage_missing_reason(conn, camp)
    emit({
        "ok": True, "campaign_id": args.campaign_id, "state": camp["state"],
        "message": camp["message"], "remaining_unassigned_users": len(rows),
        "coverage_scope": campaign_coverage_scope(camp),
        "coverage_ready": not bool(missing),
        "coverage_missing_reason": missing,
        "queue": [
            {key: r[key] for key in (
                "conversation_key", "user_id", "account_id", "display_name",
                "last_message_at_ms", "evidence"
            )}
            for r in rows
        ],
    })


def can_send(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    camp = campaign(conn, args.campaign_id)
    if camp["state"] != "approved":
        return {"ok": True, "allowed": False, "reason": "campaign_not_approved"}
    if getattr(args, "allow_incomplete_scan", False) and campaign_coverage_scope(camp) == "month":
        if not scan_is_complete(camp):
            missing = incremental_bypass_missing_reason(conn, args.campaign_id)
            if missing:
                return {"ok": True, "allowed": False, "reason": missing}
    else:
        missing = coverage_missing_reason(conn, camp)
        if missing:
            return {"ok": True, "allowed": False, "reason": missing}
    if is_expired(camp):
        return {"ok": True, "allowed": False, "reason": "campaign_expired"}
    cand = conn.execute(
        "SELECT * FROM candidates WHERE campaign_id=? AND conversation_key=?",
        (args.campaign_id, args.conversation_key),
    ).fetchone()
    if not cand:
        return {"ok": True, "allowed": False, "reason": "candidate_not_found"}
    if not account_allowed(camp, cand["account_id"]):
        return {"ok": True, "allowed": False, "reason": "account_not_allowed"}
    if historical_user_has_lead_in_scope(conn, cand["user_id"], campaign_account_ids(camp)):
        return {"ok": True, "allowed": False, "reason": "historical_import_has_lead"}
    checks = (
        (cand["lead_status"] == "no_lead", f"lead_status={cand['lead_status']}"),
        (cand["party_status"] == "customer", f"party_status={cand['party_status']}"),
        (cand["reply_status"] == "awaiting_customer", f"reply_status={cand['reply_status']}"),
        (cand["negative_status"] == "safe", f"negative_status={cand['negative_status']}"),
        (
            now_ms() - cand["last_message_at_ms"]
            >= camp["min_silence_hours"] * 60 * 60 * 1000,
            f"silence_less_than_{camp['min_silence_hours']}h",
        ),
    )
    for passed, reason in checks:
        if not passed:
            return {"ok": True, "allowed": False, "reason": reason}
    if args.batch_id:
        b = batch(conn, args.batch_id)
        if b["campaign_id"] != args.campaign_id:
            return {"ok": True, "allowed": False, "reason": "batch_campaign_mismatch"}
        if b["state"] not in {"approved", "in_progress"}:
            return {"ok": True, "allowed": False, "reason": "batch_not_approved"}
        if batch_has_uncertain_items(conn, args.batch_id):
            return {"ok": True, "allowed": False, "reason": "batch_has_uncertain_requires_manual_review"}
        item = conn.execute(
            """
            SELECT state, user_id, account_id FROM batch_items
            WHERE batch_id=? AND conversation_key=?
            """,
            (args.batch_id, args.conversation_key),
        ).fetchone()
        if not item:
            return {"ok": True, "allowed": False, "reason": "candidate_not_in_batch"}
        if item["state"] != "pending":
            return {"ok": True, "allowed": False, "reason": f"batch_item_{item['state']}"}
        if item["user_id"] != cand["user_id"] or item["account_id"] != cand["account_id"]:
            return {"ok": True, "allowed": False, "reason": "batch_item_identity_mismatch"}
    conflict = conn.execute(
        """
        SELECT 1 FROM candidates
        WHERE campaign_id=? AND user_id=?
          AND (lead_status='has_lead' OR party_status IN ('peer','unknown')
               OR negative_status='blocked')
        LIMIT 1
        """,
        (args.campaign_id, cand["user_id"]),
    ).fetchone()
    if conflict:
        return {"ok": True, "allowed": False, "reason": "user_has_strong_conflict"}
    same_campaign = conn.execute(
        """
        SELECT 1 FROM send_attempts
        WHERE campaign_id=? AND user_id=? AND status IN ('sent','uncertain')
        LIMIT 1
        """,
        (args.campaign_id, cand["user_id"]),
    ).fetchone()
    if same_campaign:
        return {"ok": True, "allowed": False, "reason": "user_already_touched_or_uncertain_in_campaign"}
    same_message = conn.execute(
        """
        SELECT 1 FROM send_attempts s
        WHERE s.message_hash_snapshot=?
          AND s.user_id=?
          AND s.status IN ('sent','uncertain')
        LIMIT 1
        """,
        (camp["message_hash"], cand["user_id"]),
    ).fetchone()
    if same_message:
        return {"ok": True, "allowed": False, "reason": "user_already_touched_or_uncertain_for_same_message"}
    cutoff = now_ms() - camp["min_silence_hours"] * 60 * 60 * 1000
    recent = conn.execute(
        """
        SELECT 1 FROM send_attempts
        WHERE user_id=?
          AND ((status='sent' AND attempted_at_ms>?) OR status='uncertain')
        LIMIT 1
        """,
        (cand["user_id"], cutoff),
    ).fetchone()
    if recent:
        return {"ok": True, "allowed": False, "reason": "user_touched_or_uncertain_within_silence_window"}
    return {
        "ok": True, "allowed": True, "reason": "",
        "user_id": cand["user_id"], "account_id": cand["account_id"],
        "display_name": cand["display_name"], "message": camp["message"],
        "batch_id": args.batch_id,
    }


def can_send_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    emit(can_send(conn, args))


def record_send_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    camp = campaign(conn, args.campaign_id)
    if camp["state"] != "approved":
        emit({"ok": False, "error": "campaign_not_approved", "state": camp["state"]}, 2)
    requires_snapshot = args.status in {"sent", "failed", "uncertain"}
    if requires_snapshot and not str(args.message_snapshot or "").strip():
        emit({
            "ok": False,
            "error": "message_snapshot_required",
            "status": args.status,
            "message": "sent_failed_uncertain_records_must_store_the_exact_message_snapshot",
        }, 2)
    cand = conn.execute(
        "SELECT user_id, account_id FROM candidates WHERE campaign_id=? AND conversation_key=?",
        (args.campaign_id, args.conversation_key),
    ).fetchone()
    if not cand:
        emit({"ok": False, "error": "candidate_not_found"}, 2)
    actual_user_id = args.actual_user_id or cand["user_id"]
    actual_account_id = args.actual_account_id or cand["account_id"]
    if actual_user_id != cand["user_id"]:
        emit({"ok": False, "error": "actual_user_id_mismatch"}, 2)
    if not account_allowed(camp, actual_account_id):
        emit({"ok": False, "error": "actual_account_not_allowed"}, 2)
    if args.status in {"sent", "uncertain"}:
        allowed = can_send(conn, args)
        if not allowed.get("allowed"):
            emit({"ok": False, "error": "send_not_allowed", "reason": allowed.get("reason")}, 2)
    if args.batch_id:
        b = batch(conn, args.batch_id)
        if b["campaign_id"] != args.campaign_id:
            emit({"ok": False, "error": "batch_campaign_mismatch"}, 2)
        if b["state"] not in {"approved", "in_progress"}:
            emit({"ok": False, "error": "batch_not_approved", "state": b["state"]}, 2)
        item = conn.execute(
            "SELECT state FROM batch_items WHERE batch_id=? AND conversation_key=?",
            (args.batch_id, args.conversation_key),
        ).fetchone()
        if not item or item["state"] != "pending":
            emit({"ok": False, "error": "batch_item_not_pending"}, 2)
    conn.execute(
        """
        INSERT INTO send_attempts
          (batch_id, campaign_id, conversation_key, user_id, account_id, status, detail,
           attempted_at_ms, message_snapshot, message_hash_snapshot)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            args.batch_id or "", args.campaign_id, args.conversation_key, actual_user_id, actual_account_id,
            args.status, args.detail, now_ms(), args.message_snapshot,
            (
                message_digest(args.message_snapshot)
                if args.message_snapshot
                else ("" if requires_snapshot else camp["message_hash"])
            ),
        ),
    )
    if args.batch_id:
        conn.execute(
            """
            UPDATE batch_items SET state=?, reason=?, updated_at_ms=?
            WHERE batch_id=? AND conversation_key=?
            """,
            (args.status, args.detail, now_ms(), args.batch_id, args.conversation_key),
        )
        conn.execute(
            """
            UPDATE campaign_batches SET state='in_progress'
            WHERE batch_id=? AND state='approved'
            """,
            (args.batch_id,),
        )
        pending = conn.execute(
            "SELECT COUNT(*) FROM batch_items WHERE batch_id=? AND state='pending'",
            (args.batch_id,),
        ).fetchone()[0]
        if pending == 0:
            conn.execute(
                """
                UPDATE campaign_batches SET state='completed', completed_at_ms=?
                WHERE batch_id=?
                """,
                (now_ms(), args.batch_id),
            )
            maybe_complete_campaign(conn, args.campaign_id)
    conn.commit()
    emit({
        "ok": True, "status": args.status,
        "conversation_key": args.conversation_key, "batch_id": args.batch_id,
    })


def record_retryable_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    b = batch(conn, args.batch_id)
    if b["state"] not in {"approved", "in_progress"}:
        emit({"ok": False, "error": "batch_not_approved", "state": b["state"]}, 2)
    item = conn.execute(
        """
        SELECT state FROM batch_items
        WHERE batch_id=? AND conversation_key=?
        """,
        (args.batch_id, args.conversation_key),
    ).fetchone()
    if not item:
        emit({"ok": False, "error": "candidate_not_in_batch"}, 2)
    if item["state"] != "pending":
        emit({"ok": False, "error": "batch_item_not_pending", "state": item["state"]}, 2)
    conn.execute(
        """
        INSERT INTO batch_item_events
          (batch_id, conversation_key, event_type, reason, created_at_ms)
        VALUES (?, ?, 'retryable_locate_failure', ?, ?)
        """,
        (args.batch_id, args.conversation_key, args.reason, now_ms()),
    )
    conn.execute(
        """
        UPDATE campaign_batches SET state='in_progress'
        WHERE batch_id=? AND state='approved'
        """,
        (args.batch_id,),
    )
    conn.commit()
    emit({
        "ok": True,
        "state": "pending",
        "conversation_key": args.conversation_key,
        "batch_id": args.batch_id,
        "reason": args.reason,
    })


def restore_technical_skips_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    b = batch(conn, args.batch_id)
    camp = campaign(conn, b["campaign_id"])
    if camp["state"] not in {"approved", "completed"}:
        emit({"ok": False, "error": "campaign_not_approved", "state": camp["state"]}, 2)
    if b["state"] not in {"approved", "in_progress", "completed"}:
        emit({"ok": False, "error": "batch_not_resumable", "state": b["state"]}, 2)
    rows = conn.execute(
        """
        SELECT conversation_key, reason FROM batch_items
        WHERE batch_id=? AND state='skipped'
        ORDER BY position
        """,
        (args.batch_id,),
    ).fetchall()
    rows = [row for row in rows if is_restorable_technical_reason(row["reason"])]
    if rows and b["state"] == "completed":
        newer_open = conn.execute(
            """
            SELECT batch_id FROM campaign_batches
            WHERE campaign_id=? AND batch_id<>?
              AND state IN ('draft','approved','in_progress')
            ORDER BY sequence_no LIMIT 1
            """,
            (b["campaign_id"], args.batch_id),
        ).fetchone()
        if newer_open:
            emit({
                "ok": False,
                "error": "another_open_batch_exists",
                "batch_id": newer_open["batch_id"],
            }, 2)
    restored_at = now_ms()
    for row in rows:
        conn.execute(
            """
            UPDATE batch_items
            SET state='pending', reason='', updated_at_ms=?
            WHERE batch_id=? AND conversation_key=? AND state='skipped'
            """,
            (restored_at, args.batch_id, row["conversation_key"]),
        )
        conn.execute(
            """
            INSERT INTO batch_item_events
              (batch_id, conversation_key, event_type, reason, created_at_ms)
            VALUES (?, ?, 'technical_skip_restored', ?, ?)
            """,
            (args.batch_id, row["conversation_key"], row["reason"], restored_at),
        )
    if rows and b["state"] == "completed":
        conn.execute(
            """
            UPDATE campaign_batches
            SET state='in_progress', completed_at_ms=NULL
            WHERE batch_id=? AND state='completed'
            """,
            (args.batch_id,),
        )
        conn.execute(
            "UPDATE campaigns SET state='approved' WHERE campaign_id=? AND state='completed'",
            (b["campaign_id"],),
        )
    conn.commit()
    emit({
        "ok": True,
        "batch_id": args.batch_id,
        "restored": len(rows),
        "state": "in_progress" if rows and b["state"] == "completed" else b["state"],
        "allowed_reasons": sorted(RESTORABLE_TECHNICAL_REASONS),
        "allowed_reason_prefixes": sorted(RESTORABLE_TECHNICAL_REASON_PREFIXES),
    })


def uncertain_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    rows = [
        dict(row) for row in conn.execute(
            """
            WITH uncertain_attempts AS (
              SELECT s.*,
                     COALESCE(NULLIF(s.batch_id, ''), (
                       SELECT bi2.batch_id
                       FROM batch_items bi2
                       JOIN campaign_batches b2 ON b2.batch_id=bi2.batch_id
                       WHERE b2.campaign_id=s.campaign_id
                         AND bi2.conversation_key=s.conversation_key
                         AND bi2.user_id=s.user_id
                         AND bi2.account_id=s.account_id
                       ORDER BY
                         CASE WHEN bi2.state='uncertain' THEN 0 ELSE 1 END,
                         b2.sequence_no DESC
                       LIMIT 1
                     )) AS resolved_batch_id
              FROM send_attempts s
              WHERE s.status='uncertain'
                AND (? IS NULL OR s.campaign_id=?)
                AND (? IS NULL OR s.user_id=?)
            )
            SELECT u.campaign_id, u.conversation_key, u.user_id, u.account_id,
                   u.detail, u.attempted_at_ms,
                   u.resolved_batch_id AS batch_id,
                   bi.state AS batch_item_state, bi.reason AS batch_item_reason
            FROM uncertain_attempts u
            LEFT JOIN batch_items bi
              ON bi.batch_id=u.resolved_batch_id
             AND bi.conversation_key=u.conversation_key
             AND bi.user_id=u.user_id
             AND bi.account_id=u.account_id
            ORDER BY u.attempted_at_ms DESC
            LIMIT ?
            """,
            (args.campaign_id, args.campaign_id, args.user_id, args.user_id, args.limit),
        )
    ]
    emit({"ok": True, "count": len(rows), "uncertain": rows})


def resolve_uncertain_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    if args.resolution not in UNCERTAIN_RESOLUTIONS:
        emit({
            "ok": False,
            "error": "invalid_resolution",
            "allowed": sorted(UNCERTAIN_RESOLUTIONS),
        }, 2)
    camp = campaign(conn, args.campaign_id)
    rows = conn.execute(
        """
        SELECT * FROM send_attempts
        WHERE campaign_id=? AND user_id=? AND status='uncertain'
        ORDER BY attempted_at_ms DESC
        """,
        (args.campaign_id, args.user_id),
    ).fetchall()
    if not rows:
        emit({"ok": False, "error": "uncertain_user_not_found"}, 2)
    resolved_at = now_ms()
    if args.resolution == "confirmed_sent":
        new_status = "sent"
        new_state = "sent"
        reason = "manual_review_confirmed_sent"
    elif args.resolution == "confirmed_not_sent":
        new_status = "failed"
        new_state = "pending"
        reason = "manual_review_confirmed_not_sent"
    else:
        new_status = "skipped"
        new_state = "skipped"
        reason = "manual_review_skip_uncertain"

    conn.execute(
        """
        UPDATE send_attempts
        SET status=?, detail=?
        WHERE campaign_id=? AND user_id=? AND status='uncertain'
        """,
        (new_status, reason, args.campaign_id, args.user_id),
    )
    batch_ids = []
    for row in rows:
        linked = conn.execute(
            """
            SELECT bi.batch_id
            FROM batch_items bi
            JOIN campaign_batches b ON b.batch_id=bi.batch_id
            WHERE b.campaign_id=? AND bi.user_id=? AND bi.account_id=?
              AND bi.state='uncertain'
            """,
            (args.campaign_id, args.user_id, row["account_id"]),
        ).fetchall()
        for item in linked:
            batch_ids.append(item["batch_id"])
            conn.execute(
                """
                UPDATE batch_items
                SET state=?, reason=?, updated_at_ms=?
                WHERE batch_id=? AND user_id=? AND account_id=? AND state='uncertain'
                """,
                (new_state, reason, resolved_at, item["batch_id"], args.user_id, row["account_id"]),
            )
    for batch_id in set(batch_ids):
        if new_state == "pending":
            conn.execute(
                """
                UPDATE campaign_batches
                SET state='in_progress', completed_at_ms=NULL
                WHERE batch_id=? AND state='completed'
                """,
                (batch_id,),
            )
            conn.execute(
                "UPDATE campaigns SET state='approved' WHERE campaign_id=? AND state='completed'",
                (args.campaign_id,),
            )
        pending = conn.execute(
            "SELECT COUNT(*) FROM batch_items WHERE batch_id=? AND state='pending'",
            (batch_id,),
        ).fetchone()[0]
        if pending == 0:
            conn.execute(
                """
                UPDATE campaign_batches
                SET state='completed', completed_at_ms=COALESCE(completed_at_ms, ?)
                WHERE batch_id=? AND state IN ('approved','in_progress')
                """,
                (resolved_at, batch_id),
            )
    maybe_complete_campaign(conn, args.campaign_id)
    conn.execute(
        """
        INSERT INTO campaign_events (campaign_id, event_type, detail, created_at_ms)
        VALUES (?, ?, ?, ?)
        """,
        (
            args.campaign_id,
            "uncertain_resolved",
            json.dumps(
                {
                    "user_id": args.user_id,
                    "resolution": args.resolution,
                    "updated_attempts": len(rows),
                    "updated_batches": sorted(set(batch_ids)),
                    "new_status": new_status,
                    "new_batch_item_state": new_state,
                },
                ensure_ascii=False,
            ),
            resolved_at,
        ),
    )
    conn.commit()
    emit({
        "ok": True,
        "campaign_id": args.campaign_id,
        "user_id": args.user_id,
        "resolution": args.resolution,
        "new_status": new_status,
        "batch_item_state": new_state,
        "updated_attempts": len(rows),
        "updated_batches": sorted(set(batch_ids)),
        "campaign_state": campaign(conn, args.campaign_id)["state"],
        "account_ids": campaign_account_ids(camp),
    })


def resolve_failed_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    if args.resolution not in FAILED_RESOLUTIONS:
        emit({
            "ok": False,
            "error": "invalid_resolution",
            "allowed": sorted(FAILED_RESOLUTIONS),
        }, 2)
    camp = campaign(conn, args.campaign_id)
    rows = conn.execute(
        """
        SELECT * FROM send_attempts
        WHERE campaign_id=? AND user_id=? AND status='failed'
        ORDER BY attempted_at_ms DESC
        """,
        (args.campaign_id, args.user_id),
    ).fetchall()
    if not rows:
        emit({"ok": False, "error": "failed_user_not_found"}, 2)
    resolved_at = now_ms()
    if args.resolution == "retry":
        new_state = "pending"
        reason = "manual_review_retry_failed"
    else:
        new_state = "skipped"
        reason = "manual_review_skip_failed"

    batch_ids = []
    account_ids = set()
    for row in rows:
        account_ids.add(row["account_id"])
        linked = conn.execute(
            """
            SELECT bi.batch_id
            FROM batch_items bi
            JOIN campaign_batches b ON b.batch_id=bi.batch_id
            WHERE b.campaign_id=? AND bi.user_id=? AND bi.account_id=? AND bi.state='failed'
            """,
            (args.campaign_id, args.user_id, row["account_id"]),
        ).fetchall()
        for item in linked:
            batch_ids.append(item["batch_id"])
            conn.execute(
                """
                UPDATE batch_items
                SET state=?, reason=?, updated_at_ms=?
                WHERE batch_id=? AND user_id=? AND account_id=? AND state='failed'
                """,
                (new_state, reason, resolved_at, item["batch_id"], args.user_id, row["account_id"]),
            )
    for batch_id in set(batch_ids):
        if new_state == "pending":
            conn.execute(
                """
                UPDATE campaign_batches
                SET state='in_progress', completed_at_ms=NULL
                WHERE batch_id=? AND state='completed'
                """,
                (batch_id,),
            )
            conn.execute(
                "UPDATE campaigns SET state='approved' WHERE campaign_id=? AND state='completed'",
                (args.campaign_id,),
            )
        pending = conn.execute(
            "SELECT COUNT(*) FROM batch_items WHERE batch_id=? AND state='pending'",
            (batch_id,),
        ).fetchone()[0]
        if pending == 0:
            conn.execute(
                """
                UPDATE campaign_batches
                SET state='completed', completed_at_ms=COALESCE(completed_at_ms, ?)
                WHERE batch_id=? AND state IN ('approved','in_progress')
                """,
                (resolved_at, batch_id),
            )
    maybe_complete_campaign(conn, args.campaign_id)
    conn.execute(
        """
        INSERT INTO campaign_events (campaign_id, event_type, detail, created_at_ms)
        VALUES (?, ?, ?, ?)
        """,
        (
            args.campaign_id,
            "failed_resolved",
            json.dumps(
                {
                    "user_id": args.user_id,
                    "resolution": args.resolution,
                    "updated_attempts": len(rows),
                    "updated_batches": sorted(set(batch_ids)),
                    "new_batch_item_state": new_state,
                },
                ensure_ascii=False,
            ),
            resolved_at,
        ),
    )
    conn.commit()
    emit({
        "ok": True,
        "campaign_id": args.campaign_id,
        "user_id": args.user_id,
        "resolution": args.resolution,
        "new_batch_item_state": new_state,
        "updated_attempts": len(rows),
        "updated_batches": sorted(set(batch_ids)),
        "campaign_state": campaign(conn, args.campaign_id)["state"],
        "account_ids": sorted(account_ids),
    })


def doctor_cmd(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    if args.campaign_id:
        payload = summary_payload(conn, args.campaign_id)
    else:
        latest = conn.execute(
            """
            SELECT campaign_id FROM campaigns
            WHERE state IN ('draft','approved')
            ORDER BY created_at_ms DESC LIMIT 1
            """
        ).fetchone()
        payload = summary_payload(conn, latest["campaign_id"]) if latest else None
    if not payload:
        emit({
            "ok": True,
            "status": "no_active_campaign",
            "next_action": "confirm_message_and_account_ids_then_start",
        })
    if not payload.get("doctor"):
        payload["doctor"] = doctor_decision(payload["counts"], payload["scan"])
    emit(payload)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("start")
    s.add_argument("--message", required=True)
    s.add_argument("--min-silence-hours", type=int, default=24)
    s.add_argument("--batch-size", type=int, default=100)
    s.add_argument("--expires-at-ms", type=int)
    s.add_argument("--account-ids", default="")
    s.add_argument("--coverage-scope", choices=sorted(VALID_COVERAGE_SCOPE), default="user_list")
    s.add_argument("--activate", action="store_true")
    s.add_argument("--keep-other-active", action="store_true")
    s.set_defaults(func=start_cmd)

    update_message = sub.add_parser("update-message")
    update_message.add_argument("--campaign-id", required=True)
    update_message.add_argument("--message", required=True)
    update_message.add_argument("--reason", default="operator_message_change")
    update_message.set_defaults(func=update_message_cmd)

    c = sub.add_parser("candidate")
    c.add_argument("--campaign-id", required=True)
    c.add_argument("--conversation-key", required=True)
    c.add_argument("--user-id", required=True)
    c.add_argument("--account-id", required=True)
    c.add_argument("--display-name", required=True)
    c.add_argument("--lead-status", choices=sorted(VALID_LEAD), required=True)
    c.add_argument("--party-status", choices=sorted(VALID_PARTY), required=True)
    c.add_argument("--reply-status", choices=sorted(VALID_REPLY), required=True)
    c.add_argument("--negative-status", choices=sorted(VALID_NEGATIVE), required=True)
    c.add_argument("--last-message-at-ms", type=int, required=True)
    c.add_argument("--evidence", default="")
    c.set_defaults(func=candidate_cmd)

    bulk = sub.add_parser("bulk-candidates")
    bulk.add_argument("--campaign-id", required=True)
    bulk.add_argument("--file", type=Path, required=True)
    bulk.set_defaults(func=bulk_candidates_cmd)

    candidate_keys = sub.add_parser("candidate-keys")
    candidate_keys.add_argument("--campaign-id", required=True)
    candidate_keys.set_defaults(func=candidate_keys_cmd)

    for name, func in (("summary", summary_cmd), ("queue", queue_cmd)):
        q = sub.add_parser(name)
        q.add_argument("--campaign-id", required=True)
        q.set_defaults(func=func)

    latest = sub.add_parser("latest")
    latest.set_defaults(func=latest_cmd)

    mark_scan = sub.add_parser("mark-scan")
    mark_scan.add_argument("--campaign-id", required=True)
    mark_scan.add_argument("--state", choices=["started", "completed", "stopped"], required=True)
    mark_scan.add_argument("--reason", default="")
    mark_scan.add_argument("--force", action="store_true")
    mark_scan.set_defaults(func=mark_scan_cmd)

    mark_current_month_window = sub.add_parser("mark-current-month-window")
    mark_current_month_window.add_argument("--campaign-id", required=True)
    mark_current_month_window.add_argument("--window-start-date", required=True)
    mark_current_month_window.add_argument("--visible-oldest-date", required=True)
    mark_current_month_window.add_argument("--reason", default="operator_completed_current_month_window")
    mark_current_month_window.set_defaults(func=mark_current_month_window_cmd)

    invalidate_scan = sub.add_parser("invalidate-scan")
    invalidate_scan.add_argument("--campaign-id", required=True)
    invalidate_scan.add_argument("--reason", default="")
    invalidate_scan.set_defaults(func=invalidate_scan_cmd)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--campaign-id")
    doctor.set_defaults(func=doctor_cmd)

    preflight = sub.add_parser("preflight")
    preflight.add_argument("--campaign-id")
    preflight.add_argument("--batch-id")
    preflight.set_defaults(func=preflight_cmd)

    operator_plan = sub.add_parser("operator-plan")
    operator_plan.add_argument("--message", required=True)
    operator_plan.add_argument("--account-ids", default="")
    operator_plan.add_argument("--baseline-hours", type=int, default=48)
    operator_plan.add_argument("--buffer-minutes", type=int, default=10)
    operator_plan.add_argument("--keep-other-active", action="store_true")
    operator_plan.set_defaults(func=operator_plan_cmd)

    incremental = sub.add_parser("incremental-plan")
    incremental.add_argument("--campaign-id", required=True)
    incremental.add_argument("--baseline-hours", type=int, default=48)
    incremental.add_argument("--buffer-minutes", type=int, default=10)
    incremental.set_defaults(func=incremental_plan_cmd)

    seed_incremental = sub.add_parser("seed-incremental-candidates")
    seed_incremental.add_argument("--campaign-id", required=True)
    seed_incremental.add_argument("--source-campaign-id", default="")
    seed_incremental.add_argument("--baseline-hours", type=int, default=48)
    seed_incremental.set_defaults(func=seed_incremental_candidates_cmd)

    mark_incremental_window = sub.add_parser("mark-incremental-window")
    mark_incremental_window.add_argument("--campaign-id", required=True)
    mark_incremental_window.add_argument("--source-campaign-id", default="")
    mark_incremental_window.add_argument("--window-start-ms", type=int, required=True)
    mark_incremental_window.add_argument("--reason", default="operator_completed_strict_incremental_window_scan")
    mark_incremental_window.set_defaults(func=mark_incremental_window_cmd)

    import_user_list = sub.add_parser("import-user-list")
    import_user_list.add_argument("--file", type=Path, required=True)
    import_user_list.add_argument("--cutoff-date", required=True)
    import_user_list.add_argument("--import-id", default="")
    import_user_list.set_defaults(func=import_user_list_cmd)

    historical_imports = sub.add_parser("historical-imports")
    historical_imports.set_defaults(func=historical_imports_cmd)

    historical_targets = sub.add_parser("historical-targets")
    historical_targets.add_argument("--campaign-id", required=True)
    historical_targets.add_argument("--limit", type=int, default=500)
    historical_targets.add_argument("--include-reviewed", action="store_true")
    historical_targets.add_argument("--review-state", choices=sorted(VALID_HISTORICAL_REVIEW), default="")
    historical_targets.set_defaults(func=historical_targets_cmd)

    mark_historical_review = sub.add_parser("mark-historical-review")
    mark_historical_review.add_argument("--campaign-id", required=True)
    mark_historical_review.add_argument("--user-id", required=True)
    mark_historical_review.add_argument("--state", choices=sorted(VALID_HISTORICAL_REVIEW), required=True)
    mark_historical_review.add_argument("--conversation-key", default="")
    mark_historical_review.add_argument("--account-id", default="")
    mark_historical_review.add_argument("--display-name", default="")
    mark_historical_review.add_argument("--last-message-at-ms", type=int, default=0)
    mark_historical_review.add_argument("--reason", default="")
    mark_historical_review.set_defaults(func=mark_historical_review_cmd)

    mark_historical_window = sub.add_parser("mark-historical-window")
    mark_historical_window.add_argument("--campaign-id", required=True)
    mark_historical_window.add_argument("--reason", default="operator_completed_historical_uid_review")
    mark_historical_window.add_argument("--force", action="store_true")
    mark_historical_window.set_defaults(func=mark_historical_window_cmd)

    stop = sub.add_parser("stop-campaign")
    stop.add_argument("--campaign-id", required=True)
    stop.add_argument("--reason", default="stopped_by_user")
    stop.set_defaults(func=stop_campaign_cmd)

    supersede = sub.add_parser("supersede-campaign")
    supersede.add_argument("--campaign-id", required=True)
    supersede.add_argument("--message", required=True)
    supersede.add_argument("--min-silence-hours", type=int, default=24)
    supersede.add_argument("--batch-size", type=int, default=100)
    supersede.add_argument("--expires-at-ms", type=int)
    supersede.add_argument("--account-ids", default="")
    supersede.set_defaults(func=supersede_campaign_cmd)

    prep = sub.add_parser("prepare-batch")
    prep.add_argument("--campaign-id", required=True)
    prep.add_argument("--limit", type=int)
    prep.add_argument("--allow-incomplete-scan", action="store_true")
    prep.set_defaults(func=prepare_batch_cmd)

    for name, func in (
        ("approve-batch", approve_batch_cmd),
        ("batch-summary", batch_summary_cmd),
        ("batch-queue", batch_queue_cmd),
    ):
        b = sub.add_parser(name)
        b.add_argument("--batch-id", required=True)
        if name in {"approve-batch", "batch-queue"}:
            b.add_argument("--allow-incomplete-scan", action="store_true")
        b.set_defaults(func=func)

    export = sub.add_parser("export-batch-report")
    export.add_argument("--batch-id", required=True)
    export.add_argument("--output-dir", type=Path)
    export.set_defaults(func=export_batch_report_cmd)

    dashboard = sub.add_parser("export-dashboard")
    dashboard.add_argument("--campaign-id")
    dashboard.add_argument("--batch-id")
    dashboard.add_argument("--output", type=Path)
    dashboard.set_defaults(func=export_dashboard_cmd)

    check = sub.add_parser("can-send")
    check.add_argument("--campaign-id", required=True)
    check.add_argument("--conversation-key", required=True)
    check.add_argument("--batch-id")
    check.add_argument("--allow-incomplete-scan", action="store_true")
    check.set_defaults(func=can_send_cmd)

    record = sub.add_parser("record-send")
    record.add_argument("--campaign-id", required=True)
    record.add_argument("--conversation-key", required=True)
    record.add_argument("--batch-id")
    record.add_argument("--status", choices=sorted(VALID_SEND), required=True)
    record.add_argument("--detail", default="")
    record.add_argument("--message-snapshot", default="")
    record.add_argument("--actual-user-id", default="")
    record.add_argument("--actual-account-id", default="")
    record.add_argument("--allow-incomplete-scan", action="store_true")
    record.set_defaults(func=record_send_cmd)

    retryable = sub.add_parser("record-retryable")
    retryable.add_argument("--batch-id", required=True)
    retryable.add_argument("--conversation-key", required=True)
    retryable.add_argument("--reason", required=True)
    retryable.set_defaults(func=record_retryable_cmd)

    restore = sub.add_parser("restore-technical-skips")
    restore.add_argument("--batch-id", required=True)
    restore.set_defaults(func=restore_technical_skips_cmd)

    uncertain = sub.add_parser("uncertain")
    uncertain.add_argument("--campaign-id")
    uncertain.add_argument("--user-id")
    uncertain.add_argument("--limit", type=int, default=50)
    uncertain.set_defaults(func=uncertain_cmd)

    resolve = sub.add_parser("resolve-uncertain")
    resolve.add_argument("--campaign-id", required=True)
    resolve.add_argument("--user-id", required=True)
    resolve.add_argument("--resolution", choices=sorted(UNCERTAIN_RESOLUTIONS), required=True)
    resolve.set_defaults(func=resolve_uncertain_cmd)

    resolve_failed = sub.add_parser("resolve-failed")
    resolve_failed.add_argument("--campaign-id", required=True)
    resolve_failed.add_argument("--user-id", required=True)
    resolve_failed.add_argument("--resolution", choices=sorted(FAILED_RESOLUTIONS), required=True)
    resolve_failed.set_defaults(func=resolve_failed_cmd)
    return p


def main() -> None:
    args = parser().parse_args()
    conn = connect(args.db.expanduser())
    try:
        args.func(conn, args)
    except (sqlite3.IntegrityError, json.JSONDecodeError) as exc:
        emit({"ok": False, "error": "state_error", "detail": str(exc)}, 2)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
