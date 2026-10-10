#!/usr/bin/env python3
"""Read-only, bounded evidence sampling for XHS follow-up copy analysis.

This program does not open a browser, send messages, change customer labels, or
infer conversion from a CSV lead flag. A browser collector can use sample-plan's
lookup queue and supply verified conversation snapshots to report as JSONL.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


DAY_MS = 86_400_000
DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "private" / "state.sqlite3"
GROUPS = ("ad_tagged", "ad_untagged", "natural_tagged", "natural_untagged")
TOPICS = {
    "price": (r"价格|多少钱|费用|收费|学费|贵", "把费用如何确定讲清楚；具体金额先由业务负责人确认。"),
    "course": (r"课程|教什么|学什么|怎么学|培训内容", "把课程内容和适合对象说具体，避免只写课程名称。"),
    "effect": (r"效果|询盘|获客|转化|保证|有用吗", "先回答客户关心的实际效果问题；不要承诺未经核实的结果。"),
    "time_place": (r"时间|什么时候|地点|哪里|线下|几天", "补上已确认的时间或地点信息；尚未确定时直接说明待确认。"),
    "fit": (r"适合|适不适合|能不能|行业", "明确适合的人群和前提，由业务负责人核对适用条件。"),
    "contact": (r"微信|电话|联系|加你", "说明下一步如何联系，并与实际可用的留资路径一致。"),
}
STAFF_EXPRESSIONS = {
    "contact_request": r"留(?:下)?(?:手机|电话|微信)|添加?(?:我)?(?:的)?(?:企业)?微信|加.{0,4}微信|联系方式",
    "price_explanation": r"价格|费用|收费|学费|多少钱",
    "next_step": r"点击|填写|预约|发你|给您安排|给你安排",
    "question_prompt": r"[?？]",
}
QUESTION = re.compile(r"[?？]|多少钱|怎么|如何|能不能|是否|吗(?:[。！!]|$)|什么时候|哪里")
MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
WECHAT = re.compile(r"(?:加我|我的)?微信\s*(?:号)?\s*[:：]?\s*[A-Za-z][A-Za-z0-9_-]{4,}", re.I)
EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def _redact(text: str) -> str:
    text = MOBILE.sub("[手机号]", text)
    text = WECHAT.sub("[微信号]", text)
    return EMAIL.sub("[邮箱]", text)


def _excerpt(text: str, limit: int = 80) -> str:
    return _redact(re.sub(r"\s+", " ", text).strip())[:limit]


def _account_ids(raw: str) -> list[str]:
    ids = list(dict.fromkeys(x.strip() for x in re.split(r"[,;\n]", raw) if x.strip()))
    if not ids:
        raise ValueError("account_ids_required")
    return ids


def _positive_limit(value: int, ceiling: int, name: str) -> int:
    if not 1 <= value <= ceiling:
        raise ValueError(f"{name}_must_be_1_to_{ceiling}")
    return value


def _read_only_db(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError("database_not_found")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    required = {"historical_user_accounts", "lead_observations"}
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required <= existing:
        conn.close()
        raise ValueError("daily_followup_tables_missing")
    return conn


def _verified_source(row: sqlite3.Row) -> str | None:
    if row["source"] == "ad" and row["attribution_state"] == "concrete":
        return "ad"
    if row["source"] == "natural" and row["attribution_state"] == "empty":
        return "natural"
    return None


def _spread(items: list[dict], limit: int, now_ms: int) -> list[dict]:
    """Stable round-robin across account and inbound week, then newest within bucket."""
    buckets: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for item in sorted(items, key=lambda x: (-x["latest_in_at_ms"], x["user_id"])):
        week = max(0, (now_ms - item["latest_in_at_ms"]) // (7 * DAY_MS))
        buckets[(week, item["account_id"])].append(item)
    selected: list[dict] = []
    keys = sorted(buckets)
    while len(selected) < limit and any(buckets.values()):
        for key in keys:
            if buckets[key]:
                selected.append(buckets[key].pop(0))
                if len(selected) == limit:
                    break
    return selected


def _uid_account_contexts(conn: sqlite3.Connection, account_ids: list[str], now_ms: int) -> dict:
    """Read recent CSV UIDs and recover account contexts from verified evidence.

    CSV times define recency. Evidence establishes additional account identity;
    it never supplies an inbound time or an absent platform label.
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    csv_columns = {row[1] for row in conn.execute("PRAGMA table_info(historical_user_accounts)")}
    name_column = "account_name" if "account_name" in csv_columns else "NULL AS account_name"
    csv_rows = conn.execute(
        f"SELECT user_id,account_id,latest_in_at_ms,{name_column} FROM historical_user_accounts WHERE deleted=0"
    ).fetchall()
    latest: dict[str, int] = {}
    mappings: dict[str, dict[str, int]] = defaultdict(dict)
    csv_contexts: dict[str, set] = defaultdict(set)
    mapped_contexts: dict[str, set] = defaultdict(set)
    unmapped_contexts: dict[str, dict] = defaultdict(dict)
    for index, row in enumerate(csv_rows):
        uid = row["user_id"]
        # Original CSV names distinguish separate account contexts. Old schemas
        # and unnamed rows cannot prove sameness, so preserve each row separately.
        name = row["account_name"]
        context = ("name", name) if name else ("row", index)
        csv_contexts[uid].add(context)
        if row["account_id"]:
            mapped_contexts[uid].add(context)
        else:
            unmapped_contexts[uid][context] = name
        at = row["latest_in_at_ms"]
        if isinstance(at, int) and 0 < at <= now_ms:
            latest[row["user_id"]] = max(latest.get(row["user_id"], 0), at)
        if row["account_id"]:
            mappings[row["user_id"]][row["account_id"]] = max(
                mappings[row["user_id"]].get(row["account_id"], 0),
                at if isinstance(at, int) and 0 < at <= now_ms else 0)
    recent = {uid: at for uid, at in latest.items() if at >= now_ms - 30 * DAY_MS}
    evidence_sources = [
        ("lead_observations", {"status"}, "status IN ('present','absent')"),
        ("conversation_history_cache", {"business_reviewed", "boundary_message_id"},
         "business_reviewed=1 AND TRIM(boundary_message_id)<>''"),
        ("contact_protections", {"evidence"}, "TRIM(evidence)<>''"),
        ("no_intent_updates", {"evidence", "state"}, "TRIM(evidence)<>'' AND state IN ('pending','failed','verified','user_confirmed')"),
        ("daily_touches", {"state"}, "state='sent'"),
        ("legacy_send_events", {"status"}, "status='sent'"),
    ]
    for table, required, predicate in evidence_sources:
        if table not in tables:
            continue
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not {"user_id", "account_id", *required} <= columns:
            continue
        for row in conn.execute(f"SELECT DISTINCT user_id,account_id FROM {table} WHERE account_id<>'' AND {predicate}"):
            if row["user_id"] in recent:
                mappings[row["user_id"]].setdefault(row["account_id"], 0)
    scope = set(account_ids)
    scoped = {uid for uid in recent if scope.intersection(mappings[uid])}
    unresolved_contexts = {}
    for uid in recent:
        pending = {key: name for key, name in unmapped_contexts[uid].items()
                   if key not in mapped_contexts[uid]}
        # Preserve the established recovery of a single unmapped CSV context
        # from verified DB identity. Multiple original contexts require their
        # own persisted mapping; another known account alone cannot bind a name.
        if pending and len(csv_contexts[uid]) == 1 and mappings[uid]:
            pending = {}
        if pending:
            unresolved_contexts[uid] = list(pending.values())
    missing = {uid for uid in recent if not mappings[uid] or uid in unresolved_contexts}
    contexts = {
        uid: {account: at for account, at in mappings[uid].items() if account in scope}
        for uid in scoped | missing
    }
    # Unknown observations cannot establish identity. Once a scoped identity
    # is known, they still warn of an additional account requiring verification.
    for row in conn.execute("SELECT DISTINCT user_id,account_id FROM lead_observations"):
        if row["user_id"] in scoped and row["account_id"] in scope:
            contexts[row["user_id"]].setdefault(row["account_id"], 0)
    counts = {"recent_import_uids": len(recent), "scoped_uids": len(scoped),
              "scope_unknown_uids": len(missing), "mapping_missing_uids": len(missing)}
    return {"contexts": contexts, "unresolved_account_contexts": unresolved_contexts,
            "latest_in_at_ms": recent, "counts": counts,
            "coverage_gaps": {"scope_unknown_uids": len(missing), "mapping_missing_uids": len(missing)}}


def sample_plan(
    conn: sqlite3.Connection,
    account_ids: list[str],
    *,
    now_ms: int,
    per_group: int = 10,
    max_lookup: int = 120,
    max_seconds: int = 1200,
    max_messages: int = 40,
    max_history_days: int = 30,
) -> dict:
    """Select verified four-cell cohorts; unknown evidence becomes a lookup queue."""
    per_group = _positive_limit(per_group, 10, "per_group")
    max_lookup = _positive_limit(max_lookup, 120, "max_lookup")
    max_seconds = _positive_limit(max_seconds, 1200, "max_seconds")
    max_messages = _positive_limit(max_messages, 40, "max_messages")
    max_history_days = _positive_limit(max_history_days, 30, "max_history_days")
    placeholders = ",".join("?" for _ in account_ids)
    source_contexts = _uid_account_contexts(conn, account_ids, now_ms)
    by_user = {
        uid: [{"account_id": account, "latest_in_at_ms": at} for account, at in contexts.items()]
        for uid, contexts in source_contexts["contexts"].items()
    }
    observations: dict[str, dict[str, list[sqlite3.Row]]] = defaultdict(lambda: defaultdict(list))
    outside_confirmed_tag_uids: set[str] = set()
    if by_user:
        for row in conn.execute(
            f"""SELECT id,user_id,account_id,status,label,source,attribution_state,observed_at_ms
                 FROM lead_observations WHERE account_id IN ({placeholders})
                 ORDER BY observed_at_ms DESC,id DESC""", account_ids):
            if row["user_id"] in by_user:
                observations[row["user_id"]][row["account_id"]].append(row)
        outside_confirmed_tag_uids = {
            row["user_id"] for row in conn.execute(
                f"""SELECT DISTINCT user_id FROM lead_observations
                     WHERE status='present' AND label='lead' AND account_id NOT IN ({placeholders})""",
                account_ids) if row["user_id"] in by_user
        }
    pools: dict[str, list[dict]] = {name: [] for name in GROUPS}
    unresolved: list[dict] = []
    ignored_recent = 0
    for user_id, contexts in by_user.items():
        latest_in = source_contexts["latest_in_at_ms"][user_id]
        if not contexts:
            unresolved.append({"user_id": user_id, "account_ids": [], "preferred_account_id": "",
                               "latest_in_at_ms": latest_in, "reason": "account_mapping_missing"})
            continue
        contexts = sorted(contexts, key=lambda row: (-row["latest_in_at_ms"], account_ids.index(row["account_id"])))
        preferred = contexts[0]
        all_observations = [obs for context in contexts for obs in observations[user_id][context["account_id"]]]
        confirmed_tags = [obs for obs in all_observations if obs["status"] == "present" and obs["label"] == "lead"]
        if confirmed_tags:
            chosen = min(confirmed_tags, key=lambda row: (row["observed_at_ms"], row["id"]))
            source = _verified_source(chosen)
            if source:
                pools[f"{source}_tagged"].append({
                    "user_id": user_id, "account_id": chosen["account_id"],
                    "account_ids": [context["account_id"] for context in contexts],
                    "latest_in_at_ms": latest_in,
                    "observed_at_ms": chosen["observed_at_ms"],
                    "evidence": "platform_tag_留客资",
                })
                continue
            reason = "tagged_source_unknown"
        elif user_id in outside_confirmed_tag_uids:
            reason = "confirmed_tag_out_of_scope"
        elif user_id in source_contexts["unresolved_account_contexts"]:
            reason = "account_mapping_incomplete"
        elif now_ms - latest_in < 2 * DAY_MS:
            ignored_recent += 1
            reason = "recent_inbound_wait_48h"
        else:
            latest_by_account = {}
            for context in contexts:
                seen = observations[user_id][context["account_id"]]
                if seen:
                    latest_by_account[context["account_id"]] = seen[0]
            fully_absent = all(
                context["account_id"] in latest_by_account
                and latest_by_account[context["account_id"]]["status"] == "absent"
                and latest_by_account[context["account_id"]]["observed_at_ms"] >= (context["latest_in_at_ms"] or latest_in)
                for context in contexts
            )
            if fully_absent:
                chosen = latest_by_account[preferred["account_id"]]
                source = _verified_source(chosen)
                if source:
                    pools[f"{source}_untagged"].append({
                        "user_id": user_id, "account_id": preferred["account_id"],
                        "account_ids": [context["account_id"] for context in contexts],
                        "latest_in_at_ms": latest_in,
                        "observed_at_ms": chosen["observed_at_ms"],
                        "evidence": "platform_tag_absent_all_known_accounts",
                    })
                    continue
                reason = "attribution_unknown"
            else:
                reason = "label_or_account_context_unknown"
        unresolved.append({
            "user_id": user_id, "account_ids": [row["account_id"] for row in contexts],
            "preferred_account_id": preferred["account_id"],
            "latest_in_at_ms": latest_in, "reason": reason,
        })
    selected = {name: _spread(pools[name], per_group, now_ms) for name in GROUPS}
    need_lookup = any(len(group) < per_group for group in selected.values())
    lookup_candidates = [item for item in unresolved
                         if item["account_ids"] and item["reason"] != "confirmed_tag_out_of_scope"]
    queue = _spread(
        [{**item, "account_id": item["preferred_account_id"]} for item in lookup_candidates],
        max_lookup if need_lookup else 0,
        now_ms,
    )
    for item in queue:
        item.pop("account_id")
    return {
        "ok": True, "generated_at_ms": now_ms, "account_ids": account_ids,
        "limits": {"window_days": 30, "per_group": per_group,
                   "max_lookup": max_lookup, "max_seconds": max_seconds,
                   "max_messages_per_user": max_messages,
                   "max_history_days": max_history_days},
        "groups": selected, "lookup_queue": queue,
        "coverage_gaps": source_contexts["coverage_gaps"],
        "counts": {**source_contexts["counts"], "recent_inbound_wait_48h": ignored_recent,
                   "eligible_pool": {name: len(pools[name]) for name in GROUPS},
                   "out_of_scope_confirmed_tag": sum(item["reason"] == "confirmed_tag_out_of_scope" for item in unresolved),
                   "unresolved": len(unresolved), "lookup_omitted_due_limit": max(0, len(lookup_candidates) - len(queue)) if need_lookup else 0},
        "interpretation": "四格表示平台留客资标签是否已核验，不表示客户是否曾提供任何联系方式；均衡内容样本不能用于计算留资率。",
    }


def _read_transcripts(path: Path) -> dict[tuple[str, str], dict]:
    sessions = {}
    with path.open(encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid_transcript_json_line_{line_no}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"invalid_transcript_line_{line_no}")
            key = (str(record.get("user_id", "")), str(record.get("account_id", "")))
            if not all(key):
                raise ValueError(f"transcript_identity_missing_line_{line_no}")
            old = sessions.get(key)
            if old is None or record.get("captured_at_ms", 0) >= old.get("captured_at_ms", 0):
                sessions[key] = record
    return sessions


def _verified_lookup_reviews(
    sessions: dict[tuple[str, str], dict], user_id: str,
    required_accounts: set[str], generated_at_ms: int,
) -> list[dict]:
    """Use only identity-checked panel evidence from the authorized UID/account contexts."""
    reviews = []
    for (session_uid, session_account), session in sessions.items():
        if (session_uid != user_id or session_account not in required_accounts
                or session.get("uid_verified") is not True
                or session.get("account_verified") is not True):
            continue
        captured = session.get("captured_at_ms")
        if not isinstance(captured, int):
            continue
        for review in session.get("account_reviews", []):
            if not isinstance(review, dict):
                continue
            observed = review.get("observed_at_ms")
            if (review.get("user_id") != user_id
                    or review.get("account_id") not in required_accounts
                    or review.get("uid_verified") is not True
                    or review.get("account_verified") is not True
                    or review.get("panel_loaded") is not True
                    or review.get("label") not in {"lead", "none", "no_intent"}
                    or not isinstance(observed, int)
                    or observed < generated_at_ms
                    or observed > captured + 5_000):
                continue
            reviews.append(review)
    return reviews


def _review_source(review: dict) -> str | None:
    if review.get("source") == "ad" and review.get("attribution_note_state") == "concrete":
        return "ad"
    if review.get("source") == "natural" and review.get("attribution_note_state") == "empty":
        return "natural"
    return None


def _new_tag_in_sessions(
    sessions: dict[tuple[str, str], dict], user_id: str,
    account_ids: list[str], generated_at_ms: int,
) -> bool:
    return any(
        review["label"] == "lead"
        for review in _verified_lookup_reviews(sessions, user_id, set(account_ids), generated_at_ms)
    )


def _current_untagged_check(
    sessions: dict[tuple[str, str], dict], item: dict,
    generated_at_ms: int, expected_source: str,
) -> str:
    """Return verified, new_tag, or unverified for this run's full account scope."""
    uid = item.get("user_id")
    accounts = item.get("account_ids")
    selected = item.get("account_id")
    latest_in = item.get("latest_in_at_ms")
    if (not isinstance(uid, str) or not uid or not isinstance(accounts, list)
            or not accounts or not all(isinstance(account, str) and account for account in accounts)
            or len(accounts) != len(set(accounts))
            or selected not in accounts or not isinstance(generated_at_ms, int)
            or generated_at_ms <= 0 or not isinstance(latest_in, int)):
        return "unverified"
    required = set(accounts)
    reviews = _verified_lookup_reviews(sessions, uid, required, generated_at_ms)
    if any(review["label"] == "lead" for review in reviews):
        return "new_tag"
    if any(
        session_uid == uid and session_account in required
        and session.get("record_type") == "capture_failure"
        and isinstance(session.get("captured_at_ms"), int)
        and session["captured_at_ms"] >= generated_at_ms
        for (session_uid, session_account), session in sessions.items()
    ):
        return "unverified"
    latest_by_account: dict[str, dict] = {}
    for review in reviews:
        account = review["account_id"]
        if (account not in latest_by_account
                or review["observed_at_ms"] > latest_by_account[account]["observed_at_ms"]):
            latest_by_account[account] = review
    if (set(latest_by_account) != required
            or any(review["label"] not in {"none", "no_intent"}
                   or review["observed_at_ms"] < latest_in
                   for review in latest_by_account.values())
            or _review_source(latest_by_account[selected]) != expected_source):
        return "unverified"
    return "verified"


def _usable_lookup_transcript(
    sessions: dict[tuple[str, str], dict], user_id: str, account_id: str,
    limits: dict, *, until_ms: int | None = None,
) -> bool:
    session = sessions.get((user_id, account_id))
    if (not session or session.get("uid_verified") is not True
            or session.get("account_verified") is not True):
        return False
    try:
        messages, _ = _bounded_messages(session, limits, until_ms=until_ms)
    except ValueError:
        return False
    return bool(messages)


def enrich_plan(conn: sqlite3.Connection, plan: dict, sessions: dict[tuple[str, str], dict]) -> dict:
    """Fill short cohorts from verified lookup results without changing the ledger."""
    scope = plan.get("account_ids")
    queue = plan.get("lookup_queue")
    groups = plan.get("groups")
    limits = plan.get("limits")
    generated_at = plan.get("generated_at_ms")
    if (not isinstance(scope, list) or not scope or not all(isinstance(x, str) and x for x in scope)
            or not isinstance(queue, list) or len(queue) > 120
            or not isinstance(groups, dict) or not isinstance(limits, dict)
            or not isinstance(generated_at, int) or generated_at <= 0):
        raise ValueError("invalid_sample_plan")
    per_group = _positive_limit(limits.get("per_group"), 10, "per_group")
    _positive_limit(limits.get("max_lookup"), 120, "max_lookup")
    _positive_limit(limits.get("max_messages_per_user"), 40, "max_messages")
    _positive_limit(limits.get("max_history_days"), 30, "max_history_days")
    if len(queue) > limits["max_lookup"] or any(
        not isinstance(groups.get(name), list) or len(groups[name]) > per_group for name in GROUPS
    ):
        raise ValueError("invalid_sample_plan_limits")
    candidate_uids = {str(item.get("user_id", "")) for item in queue if isinstance(item, dict)}
    candidate_uids.update(item["user_id"] for name in GROUPS for item in groups[name])
    stored_tags: dict[str, list[dict]] = defaultdict(list)
    for row in conn.execute(
        """SELECT user_id,account_id,source,attribution_state,observed_at_ms
           FROM lead_observations WHERE status='present' AND label='lead'"""
    ):
        if row["user_id"] in candidate_uids:
            stored_tags[row["user_id"]].append(dict(row))
    source_contexts = _uid_account_contexts(conn, scope, generated_at)
    required_from_db = defaultdict(dict, source_contexts["contexts"])
    enriched = copy.deepcopy(plan)
    enriched.setdefault("counts", {}).update(source_contexts["counts"])
    enriched["coverage_gaps"] = source_contexts["coverage_gaps"]
    deferred: dict[str, int] = defaultdict(int)
    added = {name: 0 for name in GROUPS}
    already_selected = {item["user_id"] for name in GROUPS for item in enriched["groups"][name]}
    invalidated = 0
    current_check_deferred = 0
    for name in ("ad_untagged", "natural_untagged"):
        kept = []
        for item in enriched["groups"][name]:
            uid = item["user_id"]
            accounts = item.get("account_ids")
            status = _current_untagged_check(sessions, item, generated_at, name.split("_")[0])
            if stored_tags.get(uid) or status == "new_tag":
                invalidated += 1
                already_selected.discard(uid)
            elif (uid in source_contexts["unresolved_account_contexts"]
                  or status != "verified" or set(accounts) != set(required_from_db[uid])
                  or any(value > item["latest_in_at_ms"] for value in required_from_db[uid].values())):
                current_check_deferred += 1
                already_selected.discard(uid)
            else:
                kept.append(item)
        enriched["groups"][name] = kept
    for item in queue:
        if not isinstance(item, dict):
            raise ValueError("invalid_lookup_item")
        uid = str(item.get("user_id", ""))
        accounts = item.get("account_ids")
        preferred = item.get("preferred_account_id")
        latest_in = item.get("latest_in_at_ms")
        if (not uid or not isinstance(accounts, list)
                or not all(isinstance(acc, str) and acc in scope for acc in accounts)
                or (accounts and preferred not in accounts) or (not accounts and preferred != "")
                or not isinstance(latest_in, int)):
            raise ValueError("invalid_lookup_item")
        if not accounts:
            current_accounts = required_from_db[uid]
            if not current_accounts:
                deferred["account_mapping_missing"] += 1
                continue
            accounts = sorted(current_accounts, key=lambda account: (-current_accounts[account], scope.index(account)))
            preferred = accounts[0]
        if uid in already_selected:
            continue
        required = set(accounts) | set(required_from_db[uid])
        if required != set(accounts):
            deferred["account_context_changed"] += 1
            continue
        latest_in = max(latest_in, source_contexts["latest_in_at_ms"].get(uid, 0),
                        *(required_from_db[uid].values() or [0]))
        reviews = _verified_lookup_reviews(sessions, uid, required, generated_at)
        latest_by_account: dict[str, dict] = {}
        for review in reviews:
            account = review["account_id"]
            if (account not in latest_by_account
                    or review["observed_at_ms"] > latest_by_account[account]["observed_at_ms"]):
                latest_by_account[account] = review
        scoped_stored_tags = [tag for tag in stored_tags[uid] if tag["account_id"] in scope]
        scoped_review_tags = [review for review in reviews if review["label"] == "lead"]
        if scoped_stored_tags or scoped_review_tags:
            all_tags = [
                {"account_id": tag["account_id"], "observed_at_ms": tag["observed_at_ms"],
                 "source": tag["source"], "attribution_note_state": tag["attribution_state"]}
                for tag in scoped_stored_tags
            ] + scoped_review_tags
            first = min(all_tags, key=lambda tag: tag["observed_at_ms"])
            account = first["account_id"]
            current = latest_by_account.get(account)
            if current is None or current["label"] != "lead":
                deferred["first_tag_account_not_reverified"] += 1
                continue
            source = _review_source(first) or _review_source(current)
            if not source:
                deferred["tagged_source_unknown"] += 1
                continue
            group = source + "_tagged"
            first_at = first["observed_at_ms"]
            event_at = current.get("tag_event_at_ms")
            valid_event_at = event_at if isinstance(event_at, int) and 0 < event_at <= first_at else None
            until = valid_event_at or first_at
            if not _usable_lookup_transcript(sessions, uid, account, limits, until_ms=until):
                deferred["transcript_missing_or_after_tag"] += 1
                continue
            sample = {"user_id": uid, "account_id": account,
                      "account_ids": accounts, "latest_in_at_ms": latest_in,
                      "observed_at_ms": first_at, "evidence": "lookup_verified_platform_tag_留客资"}
            if valid_event_at:
                sample["tag_event_at_ms"] = valid_event_at
        else:
            if uid in source_contexts["unresolved_account_contexts"]:
                deferred["account_mapping_incomplete"] += 1
                continue
            if any(tag["account_id"] not in scope for tag in stored_tags[uid]):
                deferred["confirmed_tag_out_of_scope"] += 1
                continue
            if set(latest_by_account) != required or any(
                review["label"] not in {"none", "no_intent"}
                or review["observed_at_ms"] - latest_in < 2 * DAY_MS
                for review in latest_by_account.values()
            ):
                deferred["label_or_account_context_unknown_or_recent"] += 1
                continue
            current = latest_by_account[preferred]
            source = _review_source(current)
            if not source:
                deferred["attribution_unknown"] += 1
                continue
            group = source + "_untagged"
            if not _usable_lookup_transcript(sessions, uid, preferred, limits):
                deferred["transcript_missing"] += 1
                continue
            sample = {"user_id": uid, "account_id": preferred,
                      "account_ids": accounts, "latest_in_at_ms": latest_in,
                      "observed_at_ms": current["observed_at_ms"],
                      "evidence": "lookup_verified_no_tag_all_scoped_accounts"}
        if len(enriched["groups"][group]) >= per_group:
            deferred["group_full"] += 1
            continue
        enriched["groups"][group].append(sample)
        already_selected.add(uid)
        added[group] += 1
    enriched["enrichment"] = {
        "lookup_considered": len(queue), "added_by_group": added,
        "existing_samples_invalidated_by_new_tag": invalidated,
        "existing_samples_deferred_without_current_check": current_check_deferred,
        "deferred_reasons": dict(sorted(deferred.items())),
        "interpretation": "只用 UID、账号、平台标签、归因均核验的补查记录填充样本；缺项不推定为未留资。",
    }
    return enriched


def _bounded_messages(record: dict, limits: dict, *, until_ms: int | None = None) -> tuple[list[dict], bool]:
    captured = record.get("captured_at_ms")
    if not isinstance(captured, int) or captured <= 0:
        raise ValueError("transcript_captured_at_ms_required")
    raw = record.get("messages")
    if not isinstance(raw, list):
        raise ValueError("transcript_messages_required")
    end_at = min(captured, until_ms) if isinstance(until_ms, int) and until_ms > 0 else captured
    cutoff = end_at - limits["max_history_days"] * DAY_MS
    valid = [
        item for item in raw
        if isinstance(item, dict) and item.get("sender") in ("customer", "staff")
        and isinstance(item.get("at_ms"), int) and cutoff <= item["at_ms"] <= end_at
        and isinstance(item.get("text"), str)
    ]
    valid.sort(key=lambda item: item["at_ms"])
    max_messages = limits["max_messages_per_user"]
    truncated = len(valid) > max_messages or not record.get("history_complete", False)
    return valid[-max_messages:], truncated


def report(plan: dict, sessions: dict[tuple[str, str], dict]) -> dict:
    limits = plan.get("limits", {})
    if not all(isinstance(limits.get(key), int) for key in ("max_messages_per_user", "max_history_days")):
        raise ValueError("invalid_sample_plan_limits")
    _positive_limit(limits["max_messages_per_user"], 40, "max_messages")
    _positive_limit(limits["max_history_days"], 30, "max_history_days")
    if not all(isinstance(plan.get("groups", {}).get(name), list) and
               len(plan["groups"][name]) <= 10 for name in GROUPS):
        raise ValueError("invalid_sample_plan_groups")
    result = {"ok": True, "account_ids": plan.get("account_ids", []), "groups": {},
              "coverage_gaps": copy.deepcopy(plan.get("coverage_gaps", {})),
              "counts": copy.deepcopy(plan.get("counts", {})),
              "method": "仅对四格抽中且 UID、实际账号已核对的聊天做主题提示；人工复核后才能改话术。",
              "limitations": ["四格表示平台留客资标签，不表示客户是否曾提供任何联系方式。", "均衡样本不可计算留资率。", "提问主题和时间顺序无法证明哪句话导致留资。",
                              "未看到回复的提问仍可能在其他渠道得到处理。"]}
    enrichment = plan.get("enrichment")
    if isinstance(enrichment, dict):
        result["enrichment"] = {
            key: value for key in ("existing_samples_deferred_without_current_check",
                                   "existing_samples_invalidated_by_new_tag")
            if isinstance((value := enrichment.get(key)), int) and value >= 0
        }
    for group_name in GROUPS:
        items = plan.get("groups", {}).get(group_name, [])
        topic_users: dict[str, set[str]] = {topic: set() for topic in TOPICS}
        staff_expression_users: dict[str, set[str]] = {pattern: set() for pattern in STAFF_EXPRESSIONS}
        excerpts = []
        questions_to_review = []
        covered = 0
        truncated_count = 0
        stale_excluded = 0
        current_label_unverified_excluded = 0
        for item in items:
            uid = item["user_id"]
            account_id = item["account_id"]
            if group_name.endswith("_untagged"):
                status = _current_untagged_check(
                    sessions, item, plan.get("generated_at_ms", 0), group_name.split("_")[0])
                if status == "new_tag":
                    stale_excluded += 1
                    continue
                if status != "verified":
                    current_label_unverified_excluded += 1
                    continue
            session = sessions.get((uid, account_id))
            if not session or session.get("uid_verified") is not True or session.get("account_verified") is not True:
                continue
            until_ms = None
            if group_name.endswith("_tagged"):
                first_seen_at = item["observed_at_ms"]
                event_at = item.get("tag_event_at_ms")
                until_ms = event_at if isinstance(event_at, int) and 0 < event_at <= first_seen_at else first_seen_at
            messages, truncated = _bounded_messages(session, limits, until_ms=until_ms)
            if not messages:
                continue
            covered += 1
            truncated_count += int(truncated)
            sample_id = hashlib.sha256(f"{uid}:{account_id}".encode()).hexdigest()[:10]
            for index, msg in enumerate(messages):
                clean = _excerpt(msg["text"])
                if clean and len(excerpts) < 12:
                    excerpts.append({"sample_id": sample_id, "sender": msg["sender"], "text": clean})
                if msg["sender"] != "customer":
                    for pattern_name, pattern in STAFF_EXPRESSIONS.items():
                        if re.search(pattern, msg["text"]):
                            staff_expression_users[pattern_name].add(sample_id)
                    continue
                for topic, (pattern, _) in TOPICS.items():
                    if re.search(pattern, msg["text"]):
                        topic_users[topic].add(sample_id)
                if QUESTION.search(msg["text"]):
                    later_staff = next((m for m in messages[index + 1:] if m["sender"] == "staff"), None)
                    later_customer = next((m for m in messages[index + 1:] if m["sender"] == "customer"), None)
                    if later_staff is None or (later_customer and later_customer["at_ms"] < later_staff["at_ms"]):
                        if len(questions_to_review) < 8:
                            questions_to_review.append({"sample_id": sample_id, "question": clean,
                                                        "status": "需人工核实是否已回答"})
        topic_mentions = {topic: len(users) for topic, users in topic_users.items() if users}
        changes = [
            {"topic": topic, "proposal_for_review": TOPICS[topic][1],
             "supporting_sample_count": len(topic_users[topic])}
            for topic in sorted(topic_mentions, key=lambda key: (-topic_mentions[key], key))
        ]
        result["groups"][group_name] = {
            "sampled": len(items) - stale_excluded - current_label_unverified_excluded,
            "planned": len(items), "with_verified_transcripts": covered,
            "missing_or_unverified": len(items) - stale_excluded - current_label_unverified_excluded - covered,
            "stale_group_excluded": stale_excluded,
            "current_label_unverified_excluded": current_label_unverified_excluded,
            "truncated_transcripts": truncated_count,
            "topic_mentions": topic_mentions,
            "staff_expression_patterns": {
                name: len(users) for name, users in staff_expression_users.items() if users
            },
            "questions_to_review": questions_to_review,
            "candidate_changes": changes,
            "evidence_excerpts": excerpts,
        }
    result["comparison"] = {
        source: {
            "tagged": {"verified_samples": result["groups"][source + "_tagged"]["with_verified_transcripts"],
                       **result["groups"][source + "_tagged"]["staff_expression_patterns"]},
            "untagged": {"verified_samples": result["groups"][source + "_untagged"]["with_verified_transcripts"],
                         **result["groups"][source + "_untagged"]["staff_expression_patterns"]},
        }
        for source in ("ad", "natural")
    }
    return result


def _write_or_print(payload: dict, output: str | None) -> None:
    rendered = _json(payload)
    if output:
        _private_write(Path(output).expanduser(), rendered + "\n")
    print(rendered)


def _private_write(target: Path, content: str) -> None:
    """Create new output directories as 0700 and atomically replace a 0600 file."""
    missing = []
    parent = target.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    if not parent.is_dir():
        raise ValueError("output_parent_not_directory")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
    fd, temp_name = tempfile.mkstemp(prefix=".followup-analysis-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _markdown_report(payload: dict) -> str:
    names = {"ad_tagged": "广告｜有留客资标签", "ad_untagged": "广告｜无留客资标签",
             "natural_tagged": "自然流｜有留客资标签", "natural_untagged": "自然流｜无留客资标签"}
    topic_names = {"price": "费用", "course": "课程内容", "effect": "效果与获客",
                   "time_place": "时间地点", "fit": "适合对象", "contact": "联系方式"}
    expression_names = {"contact_request": "索取联系方式", "price_explanation": "解释费用",
                        "next_step": "说明下一步", "question_prompt": "提问引导"}
    lines = ["# 私信话术分析：四格会话样本", "", payload["method"], "",
             "| 样本组 | 初选 | 本次核实后纳入 | 有可分析聊天 | 标签待核实移出 | 新留资移出 | 聊天缺口 | 历史不完整 |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for key in GROUPS:
        group = payload["groups"][key]
        lines.append(f"| {names[key]} | {group['planned']} | {group['sampled']} | "
                     f"{group['with_verified_transcripts']} | {group['current_label_unverified_excluded']} | "
                     f"{group['stale_group_excluded']} | {group['missing_or_unverified']} | "
                     f"{group['truncated_transcripts']} |")
    gaps = payload.get("coverage_gaps", {})
    lines.extend(["", f"账号映射缺失 {gaps.get('mapping_missing_uids', 0)} 人；范围未知 {gaps.get('scope_unknown_uids', 0)} 人。",
                  "四格表示平台标签是否已核验，不能据此判断客户是否曾提供任何联系方式。"])
    if "enrichment" in payload:
        pending = payload["enrichment"].get("existing_samples_deferred_without_current_check", 0)
        newly_tagged = payload["enrichment"].get("existing_samples_invalidated_by_new_tag", 0)
        lines.extend(["", f"初选未留资样本：本次标签未完成核验 {pending} 人，已排除；"
                      f"新发现留客资 {newly_tagged} 人，已移出未留资组。"])
    for key in GROUPS:
        group = payload["groups"][key]
        lines.extend(["", f"## {names[key]}", ""])
        if not group["with_verified_transcripts"]:
            lines.append("目前缺少可用聊天证据，暂不提出话术结论。")
            continue
        topics = "、".join(f"{topic_names.get(k, k)} {v} 人" for k, v in group["topic_mentions"].items()) or "未识别到固定主题"
        expressions = "、".join(f"{expression_names.get(k, k)} {v} 人" for k, v in group["staff_expression_patterns"].items()) or "未识别到固定表达"
        lines.extend([f"- 客户提及：{topics}。", f"- 员工表达：{expressions}。"])
        if group["questions_to_review"]:
            lines.extend(["", "### 待核实的问题", ""])
            for question in group["questions_to_review"]:
                lines.append(f"- 样本 {question['sample_id']}：{question['question']}（{question['status']}）")
        if group["candidate_changes"]:
            lines.extend(["", "### 候选改法", ""])
            for change in group["candidate_changes"]:
                lines.append(f"- {topic_names.get(change['topic'], change['topic'])}："
                             f"{change['proposal_for_review']}（{change['supporting_sample_count']} 人提及）")
        if group["evidence_excerpts"]:
            lines.extend(["", "### 脱敏证据摘录", ""])
            for item in group["evidence_excerpts"]:
                speaker = "客户" if item["sender"] == "customer" else "员工"
                excerpt = item["text"].replace("`", "\\`").replace("|", "\\|")
                lines.append(f"- 样本 {item['sample_id']}，{speaker}：{excerpt}")
    lines.extend(["", "## 解读边界", ""])
    lines.extend(f"- {note}" for note in payload["limitations"])
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan_cmd = sub.add_parser("sample-plan", help="Read only: choose four verified samples and bounded lookup queue")
    plan_cmd.add_argument("--db", type=Path, default=DEFAULT_DB)
    plan_cmd.add_argument("--account-ids", required=True, help="Explicit managed account IDs, comma-separated")
    plan_cmd.add_argument("--now-ms", type=int, default=None)
    plan_cmd.add_argument("--per-group", type=int, default=10)
    plan_cmd.add_argument("--max-lookup", type=int, default=120)
    plan_cmd.add_argument("--max-seconds", type=int, default=1200)
    plan_cmd.add_argument("--max-messages", type=int, default=40)
    plan_cmd.add_argument("--max-history-days", type=int, default=30)
    plan_cmd.add_argument("--output")
    enrich_cmd = sub.add_parser("enrich-plan", help="Fill short sample cohorts from verified bounded lookup JSONL")
    enrich_cmd.add_argument("--db", type=Path, default=DEFAULT_DB)
    enrich_cmd.add_argument("--plan", required=True, type=Path)
    enrich_cmd.add_argument("--transcripts", required=True, type=Path)
    enrich_cmd.add_argument("--output")
    report_cmd = sub.add_parser("report", help="Analyze only verified bounded conversation JSONL")
    report_cmd.add_argument("--plan", required=True, type=Path)
    report_cmd.add_argument("--transcripts", required=True, type=Path)
    report_cmd.add_argument("--output")
    report_cmd.add_argument("--markdown-output", help="Optional readable Chinese report path")
    args = parser.parse_args(argv)
    try:
        if args.command == "sample-plan":
            with _read_only_db(args.db) as conn:
                payload = sample_plan(
                    conn, _account_ids(args.account_ids),
                    now_ms=args.now_ms if args.now_ms is not None else int(time.time() * 1000),
                    per_group=args.per_group, max_lookup=args.max_lookup,
                    max_seconds=args.max_seconds, max_messages=args.max_messages,
                    max_history_days=args.max_history_days,
                )
        elif args.command == "enrich-plan":
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            with _read_only_db(args.db) as conn:
                payload = enrich_plan(conn, plan, _read_transcripts(args.transcripts))
        else:
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            if plan.get("ok") is not True:
                raise ValueError("invalid_sample_plan")
            payload = report(plan, _read_transcripts(args.transcripts))
        _write_or_print(payload, args.output)
        if args.command == "report" and args.markdown_output:
            _private_write(Path(args.markdown_output).expanduser(), _markdown_report(payload))
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as exc:
        print(_json({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
