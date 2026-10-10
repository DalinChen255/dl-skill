// CUA-only orchestration: this module never opens a browser or invokes a CLI.
// Inject an adapter bound to one CUA tab and an asynchronous ledger client.
// ledger methods use the JSON payloads of daily_followup_state.py:
// pendingTouches(), prepareTouch(payload), recordBrowserDefer(payload),
// markClicked({touch_id}), finishTouch(payload), report({run_id}).
// For execution, progressStore.load()/save(state) must persist atomically in
// private storage. Browser tokens are deliberately never persisted or replayed.

import {makeReviewPacket, automaticDecisions, approvePacket, reviewedHistoryCache} from "./xhs_business_review.mjs";

const copy = value => structuredClone(value);
const reportFields = ["sent_this_run", "failed_this_run", "uncertain_this_run"];
const counts = report => Object.fromEntries(reportFields.map(key => [key, Number(report?.[key] || 0)]));
const connectionFailure = error => /Debugger unattached|browser (?:has )?disconnected|browser connection (?:closed|lost)/i.test(String(error?.message || error));
const immediateStops = new Set([
  "user_draft_present", "draft_present_before_route_recovery", "private_message_page_unavailable",
  "private_message_page_recovery_timeout", "login_required", "identity_mismatch",
  "customer_annotation_tab_missing", "customer_annotation_tab_not_restored",
]);
const pendingStops = new Set([
  "unfinished_touch_requires_reconciliation", "reservation_requires_reconciliation",
  "click_marker_requires_reconciliation", "mark_clicked_not_durable", "finish_not_durable",
]);

function initialState(runId, sessionId, atMs) {
  return {
    version: 1, run_id: runId, session_id: sessionId || "", started_at_ms: atMs, baseline: null,
    read_failure_uids: [], recovery: {used: false, result: "not_started"},
    diagnostic: {status: "ready", reason: ""}, blocked_uids: {},
    metrics: {}, recent_events: [], read_failures: [],
    health_controls: [], health_window: {failed_uids: [], read_elapsed_ms: 0},
    candidate_health_check: null, sent_uids: {}, event_sequence: 0,
  };
}

// Explicit, pure migration. Persist its result with the progress store only
// after inspecting the old file. The old record is retained byte-for-field;
// even a passed repair validation never grants another automatic recovery.
export function migrateLegacyRecovery(legacy, {runId, sessionId = "", atMs = Date.now(), repairValidation} = {}) {
  if (!legacy || legacy.run_id !== runId || !Number.isInteger(legacy.attempts) || legacy.attempts < 0) {
    throw new Error("progress_migrate_required_unknown_legacy_format");
  }
  const state = initialState(runId, sessionId, atMs);
  state.legacy_recovery = copy(legacy);
  state.recovery = {used: legacy.attempts > 0, attempts: legacy.attempts,
    result: legacy.result || "unknown", migrated_at_ms: atMs};
  state.diagnostic = {status: "blocked", reason: "legacy_recovery_requires_read_only_repair_validation"};
  if (repairValidation) {
    if (repairValidation.result !== "passed" || !Number.isInteger(repairValidation.validated_at_ms) ||
        repairValidation.validated_at_ms < Number(legacy.at_ms || 0) ||
        !repairValidation.evidence_path || !Array.isArray(repairValidation.uid_checks) ||
        repairValidation.uid_checks.length < 3 ||
        new Set(repairValidation.uid_checks.map(check => check.user_id)).size < 3 ||
        !repairValidation.uid_checks.every(check => check.user_id && ["observed", "stopped"].includes(check.status))) {
      throw new Error("read_only_repair_evidence_required");
    }
    state.repair_validations = [copy(repairValidation)];
    state.diagnostic = {status: "ready", reason: ""};
  }
  return state;
}

function verifiedStop(observation) {
  return observation.status === "stopped" && observation.reviews?.some(review =>
    review.identity_verified === true && review.panel_loaded === true &&
    (["lead", "no_intent"].includes(review.label) || review.platform_lead_signal === true));
}

function complete(observation) {
  return observation.status === "observed" && observation.coverage_complete === true &&
    observation.search_complete === true && Boolean(observation.token) &&
    observation.reviews?.length > 0 && observation.reviews.every(review =>
      review.identity_verified === true && review.panel_loaded === true && review.history_complete === true);
}

// The only automatic semantic decision is absence of ANY customer content in
// history read from its beginning. Cached/incremental histories are valid but
// still require a supplied review; missing old text cannot prove absence.
export function decisionsForNoCustomerContent(observation) {
  if (!complete(observation) || !observation.reviews.every(review =>
    review.history_from_start_complete === true && review.needs_visual_review === false &&
    review.label === "none" && !review.no_intent_label && !review.platform_lead_signal &&
    !review.draft_present && Array.isArray(review.messages) && review.messages.length > 0 &&
    !review.messages.some(message => message.side === "left"))) return null;
  return observation.reviews.map(review => ({account_id: review.account_id,
    contact_shared: false, stop_contact: false, evidence: ""}));
}

function recentAccount(observation, item) {
  const reviews = observation.reviews || [];
  const time = review => Math.max(0, ...[review.last_contact_at_ms, review.last_customer_at_ms,
    review.last_outbound_text_at_ms, ...(review.messages || []).filter(message =>
      ["left", "right"].includes(message.side)).map(message => message.timestamp)]
    .map(value => Number(value)).filter(Number.isFinite));
  const latest = Math.max(0, ...reviews.map(time));
  const candidates = reviews.filter(review => time(review) === latest).map(review => review.account_id);
  // Fresh complete UI history wins over the CSV's historical preference. At
  // equal verified times the historical preference then the already selected
  // conversation avoids an unnecessary switch without choosing an older one.
  return candidates.includes(item.preferred_account_id) ? item.preferred_account_id
    : candidates.includes(observation.selected_account_id) ? observation.selected_account_id : candidates[0];
}

export function createXhsFollowupWorkflow({
  adapter, ledger, run, sendEnabled = false, progressStore, review,
  sessionId, now = () => Date.now(), readFailureLimit = 3, budgetMs = 30000, batchMode = false,
  noProgressUidLimit = Math.max(9, readFailureLimit * 3), noProgressReadMs = 60000, healthProbeBudgetMs = 6000,
} = {}) {
  if (!adapter || typeof adapter.observeUid !== "function") throw new TypeError("adapter_required");
  if (!run?.run_id) throw new TypeError("run_id_required");
  if (!Number.isInteger(readFailureLimit) || readFailureLimit < 1) throw new TypeError("invalid_read_failure_limit");
  if (!Number.isInteger(noProgressUidLimit) || noProgressUidLimit < readFailureLimit ||
      !Number.isFinite(noProgressReadMs) || noProgressReadMs < 1 ||
      !Number.isFinite(healthProbeBudgetMs) || healthProbeBudgetMs < 1 || healthProbeBudgetMs > 10000) {
    throw new TypeError("invalid_health_check_limits");
  }
  if (run.touch_policy && !["uid_once", "account_once"].includes(run.touch_policy)) throw new TypeError("invalid_touch_policy");
  if (sendEnabled === true && (!ledger || !progressStore?.load || !progressStore?.save)) {
    throw new TypeError("execution_requires_ledger_and_durable_progress_store");
  }
  if (sendEnabled === true && batchMode && !ledger.recordReviewedHistory) throw new Error("batch_review_ledger_required");
  const enabled = sendEnabled === true;
  const evidenceStore = progressStore?.evidence;
  const evidenceMemory = new Map();
  let initialized = false, busy = false, held = null, pauseRequested = false, sliceDeadline = Infinity;
  let state = initialState(run.run_id, sessionId, now());

  async function save() { if (progressStore?.save) await progressStore.save(copy(state)); }
  async function readEvidence(value) {
    if (!value?.reference) return value;
    if (!evidenceStore) throw new Error("review_evidence_store_required");
    if (!evidenceMemory.has(value.reference)) evidenceMemory.set(value.reference, await evidenceStore.get(value));
    return evidenceMemory.get(value.reference);
  }
  async function writeEvidence(value) {
    if (!evidenceStore) return value; // In-memory fixtures/read-only diagnosis.
    const pointer = await evidenceStore.put(value);
    evidenceMemory.set(pointer.reference, value);
    return pointer;
  }
  async function event(stage, uid, outcome, elapsedMs, detail = {}) {
    const metric = state.metrics[stage] ||= {count: 0, total_ms: 0, max_ms: 0};
    metric.count += 1;
    metric.total_ms += elapsedMs;
    metric.max_ms = Math.max(metric.max_ms, elapsedMs);
    const entry = {run_id: run.run_id, session_id: state.session_id, sequence: ++state.event_sequence,
      at_ms: now(), stage, user_id: uid, outcome, elapsed_ms: elapsedMs, ...detail};
    state.recent_events.push(entry);
    state.recent_events = state.recent_events.slice(-100);
    await save();
    if (progressStore?.appendEvent) {
      try { await progressStore.appendEvent(copy(entry)); }
      catch (error) {
        // The ledger and atomic progress remain authoritative. A diagnostic
        // journal failure must never turn an already executed click into a
        // second attempt or misreport its receipt.
        state.event_log_error = {at_ms: now(), sequence: entry.sequence,
          reason: String(error?.message || error)};
        await save();
      }
    }
  }
  async function timed(stage, uid, fn) {
    const start = now();
    let result;
    try { result = await fn(); }
    catch (error) {
      await event(stage, uid, "error", Math.max(0, now() - start),
        {reason: String(error?.message || error).slice(0, 240)});
      throw error;
    }
    const detail = result?.reason ? {reason: result.reason} : {};
    if (result?.performance) detail.performance = Object.fromEntries(Object.entries(result.performance)
      .filter(([, value]) => typeof value === "number" && Number.isFinite(value)));
    if (["observe", "select_account", "recover_observe"].includes(stage) && Array.isArray(result?.reviews)) {
      detail.history = {accounts: result.reviews.length,
        from_start: result.reviews.filter(r => r.history_from_start_complete).length,
        cached_boundaries: result.reviews.filter(r => r.history_reached_prior_boundary).length,
        incomplete: result.reviews.filter(r => !r.history_complete).length,
        ...(result.performance?.history_cache_reread_reasons
          ? {reread_reasons: copy(result.performance.history_cache_reread_reasons)} : {})};
    }
    // Progress/diagnostic writes are outside the browser exception boundary.
    await event(stage, uid, result?.status || result?.action || "ok", Math.max(0, now() - start), detail);
    return result;
  }
  async function adapterCall(stage, uid, fn, {status = "deferred", reason = "browser_read_exception"} = {}) {
    return timed(stage, uid, async () => {
      try { return await fn(); }
      catch (error) {
        const disconnected = connectionFailure(error);
        return {status: disconnected && status !== "uncertain" ? "blocked" : status,
          reason: disconnected ? "browser_connection_unavailable" : reason,
          failure_stage: stage === "select_account" || stage === "confirm_review" ? "identity" : "observe",
          detail: String(error?.message || error), reviews: []};
      }
    });
  }
  async function refreshRunStatus({persist = true, completedReadOnly = false} = {}) {
    const status = ledger?.runStatus ? await ledger.runStatus({run_id: run.run_id}) : null;
    if (status?.run_id && status.run_id !== run.run_id) throw new Error("ledger_run_status_mismatch");
    const policy = status?.touch_policy || run.touch_policy || state.touch_policy || "account_once";
    if (!["uid_once", "account_once"].includes(policy)) throw new Error("ledger_touch_policy_invalid");
    const runState = status?.run_state || status?.state || state.run_state || "active";
    const changed = state.touch_policy !== policy || state.run_state !== runState;
    state.touch_policy = policy;
    state.run_state = runState;
    if (changed && persist && !(completedReadOnly && ["completed", "completed_with_exceptions"].includes(runState))) await save();
    return status;
  }
  function runGuard() {
    if (state.run_state === "paused") return {status: "paused", reason: "ledger_run_paused"};
    if (["completed", "completed_with_exceptions"].includes(state.run_state)) {
      return {status: "blocked", reason: "run_already_completed"};
    }
    if (state.run_state === "blocked") return {status: "blocked", reason: "ledger_run_blocked"};
    return null;
  }
  async function initialize({readOnly = false, completedReadOnly = false} = {}) {
    if (initialized) {
      await refreshRunStatus({persist: !readOnly, completedReadOnly});
      return readOnly || (completedReadOnly && ["completed", "completed_with_exceptions"].includes(state.run_state));
    }
    const saved = await progressStore?.load?.();
    if (saved) {
      if (saved.version !== 1 || saved.run_id !== run.run_id) throw new Error("progress_run_mismatch");
      state = {...state, ...copy(saved)};
    }
    await refreshRunStatus({persist: false});
    const completedRun = ["completed", "completed_with_exceptions"].includes(state.run_state);
    readOnly ||= completedReadOnly && completedRun;
    if (saved && !completedRun) {
      if (sessionId && saved.session_id && sessionId !== saved.session_id) {
        if (!enabled || !ledger?.sessionStatus) throw new Error("progress_session_mismatch");
        const previousSession = await ledger.sessionStatus({session_id: saved.session_id});
        if (previousSession.run_id !== run.run_id || previousSession.session_id !== saved.session_id) {
          throw new Error("ledger_previous_session_mismatch");
        }
        if (previousSession.state !== "closed") throw new Error("progress_session_mismatch_active_session");
        // Only the request baseline changes. Recovery limits, blocked UIDs,
        // diagnostics and timing records belong to the run and remain intact.
        state.session_history = [...(state.session_history || []), {
          session_id: previousSession.session_id, started_at_ms: previousSession.started_at_ms,
          ended_at_ms: previousSession.ended_at_ms, baseline: previousSession.baseline,
          delta: previousSession.delta,
        }];
        state.session_id = sessionId;
        state.baseline = null;
      }
    }
    if (enabled && !readOnly && ledger.beginSession && !completedRun) {
      state.session_id ||= sessionId || "";
      if (!state.session_id) throw new Error("stable_user_request_session_id_required");
      const session = await ledger.beginSession({run_id: run.run_id, session_id: state.session_id});
      if (session.run_id !== run.run_id || session.session_id !== state.session_id) throw new Error("ledger_session_mismatch");
      if (session.state !== "active") throw new Error("ledger_session_closed");
      state.session_started_at_ms = session.started_at_ms;
      state.baseline = Object.fromEntries(reportFields.map(key => [key, Number(session.baseline[key.replace("_this_run", "")] || 0)]));
    } else if (enabled && !state.baseline) state.baseline = counts(await ledger.report({run_id: run.run_id}));
    state.pending_reviews ||= {};
    state.held_reviews ||= {};
    state.review_cache ||= {};
    state.ready_jobs ||= {};
    state.inspected_uids ||= {};
    state.control ||= {paused: false};
    state.health_controls ||= [];
    state.health_window ||= {failed_uids: [], read_elapsed_ms: 0};
    state.sent_uids ||= {};
    state.event_sequence ||= 0;
    initialized = !readOnly;
    if (!readOnly && !completedRun) await save();
    return readOnly;
  }
  async function exclusive(fn, {readOnly = false, completedReadOnly = false} = {}) {
    if (busy) throw new Error("workflow_already_running");
    busy = true;
    const operation = async () => {
      // Another instance may have persisted a pause or approval since our last
      // slice. Reload only under the shared executor lock; never overwrite it
      // with this instance's stale state.
      if (batchMode && progressStore?.withExclusive) initialized = false;
      const operationReadOnly = await initialize({readOnly, completedReadOnly});
      if (pauseRequested && !operationReadOnly) await persistPause();
      try { return await fn(); }
      finally {
        // A pause can arrive while approvals or ledger calls are awaiting.
        // Persist it before releasing ownership, including early/failed exits.
        if (pauseRequested && !operationReadOnly) await persistPause();
      }
    };
    try { return await (progressStore?.withExclusive ? progressStore.withExclusive(operation) : operation()); }
    finally { busy = false; sliceDeadline = Infinity; }
  }
  async function persistPause({forceLifecycle = false} = {}) {
    const wasPaused = state.control.paused;
    if (wasPaused && !forceLifecycle) return;
    if (!wasPaused) {
      state.control = {paused: true, paused_at_ms: now()};
      await event("control", "", "paused", 0);
    }
    if (enabled && ledger.pauseRun && state.run_state !== "paused") {
      try {
        const result = await ledger.pauseRun({run_id: run.run_id, detail: "workflow_user_pause"});
        if (result.ok === false) throw new Error(result.reason || "ledger_pause_not_durable");
        state.run_state = result.run_state || result.state || "paused";
        if (state.lifecycle_error?.action === "pause") delete state.lifecycle_error;
      } catch (error) {
        // Even an open clicked touch that prevents a DB lifecycle change must
        // leave the local pause durable, with its reconciliation gate intact.
        state.lifecycle_error = {action: "pause", at_ms: now(), reason: String(error?.message || error)};
      }
      await save();
    }
  }
  async function block(reason, extra = {}) {
    state.diagnostic = {status: "blocked", reason, ...extra};
    await save();
    return {status: "blocked", reason, ...extra};
  }
  async function pendingGuard() {
    if (!enabled) return null;
    // Query globally: an unfinished touch in another run also blocks writes.
    const pending = await ledger.pendingTouches();
    if (pending.blocked_by_unfinished_touch || pending.items?.length) {
      const previous = pendingStops.has(state.diagnostic.reason)
        ? state.diagnostic.previous_diagnostic
        : state.diagnostic.status !== "ready" ? copy(state.diagnostic) : null;
      return block("unfinished_touch_requires_reconciliation", {touches: pending.items || [],
        ...(previous ? {previous_diagnostic: previous} : {})});
    }
    if (state.diagnostic.status === "blocked" && pendingStops.has(state.diagnostic.reason)) {
      // Only live ledger evidence can release these blocks. Search failures,
      // drafts, and other diagnostics are never cleared by an empty ledger.
      state.diagnostic = state.diagnostic.previous_diagnostic || {status: "ready", reason: ""};
      await save();
    }
    return null;
  }
  async function observeInput(item, timeBudget = budgetMs) {
    return {uid: item.user_id, scopeMode: run.scope_mode, authorizedAccountIds: run.account_ids || [],
      relevantAccountIds: item.account_ids || [],
      // Old ledger caches lack the original text/semantic decision. The batch
      // path initially reads them fully; its own approved evidence is reusable.
      historyCache: batchMode ? reviewedHistoryCache(await readEvidence(state.review_cache[item.user_id])) : item.history_cache || {},
      budgetMs: timeBudget};
  }
  async function retainApproval(packet, decisions, visualReviewed = false) {
    const approved = approvePacket(packet, {evidence_id: packet.evidence_id, decisions, visual_reviewed: visualReviewed});
    if (enabled) {
      const recorded = await timed("save_review", packet.uid, () => ledger.recordReviewedHistory({
      run_id: run.run_id, user_id: packet.uid,
      reviews: approved.reviews.map(r => {
        const d = decisions.find(d => d.account_id === r.account_id);
        return {...r, business_reviewed: true, protective_lead_signal: d.contact_shared,
          negative_status: d.stop_contact ? "blocked" : "safe",
          contact_evidence: d.contact_shared ? d.evidence : "", stop_contact_evidence: d.stop_contact ? d.evidence : ""};
      }),
      }));
      approved.label_updates = recorded.label_updates || [];
    }
    state.review_cache[packet.uid] = await writeEvidence(approved);
    await save();
    return approved;
  }
  function prepareInput(item, observation) {
    return {run_id: run.run_id, user_id: item.user_id, selected_account_id: observation.selected_account_id,
      reviews: observation.reviews, discovered_account_ids: observation.discovered_account_ids,
      search_complete: observation.search_complete};
  }
  function recoveryRequired(extra = {}) {
    state.candidate_health_check = null;
    state.diagnostic = {status: state.recovery.used ? "blocked" : "recovery_required",
      reason: state.recovery.used ? "repeated_read_failures_after_recovery" : "consecutive_read_failures",
      user_ids: [...state.read_failure_uids], ...extra};
  }
  function healthyProbe(result, uid) {
    return result?.status === "healthy" && (result.uid || result.user_id) === uid &&
      result.identity_verified === true && result.panel_loaded === true &&
      result.search_complete === true && Boolean(result.selected_account_id);
  }
  async function probe(item) {
    const input = {uid: item.user_id, scopeMode: run.scope_mode,
      authorizedAccountIds: run.account_ids || [], relevantAccountIds: item.account_ids || [],
      budgetMs: healthProbeBudgetMs};
    const result = await adapterCall("health_probe", item.user_id, () => adapter.probeUid(input),
      {reason: "health_probe_exception"});
    if (immediateStops.has(result.reason) || result.status === "blocked") {
      return block(result.reason || "page_health_probe_blocked", {user_id: item.user_id});
    }
    return result;
  }
  async function checkNormalControls() {
    // Probe actual recently verified customers, never one of the failed UIDs.
    // Search/panel identity proves page usability, without full history reads
    // or replaying business/send tokens from the earlier visit.
    const controls = state.health_controls.filter(control =>
      !state.health_window.failed_uids.includes(control.user_id)).slice(0, 2);
    if (!adapter.probeUid) {
      recoveryRequired({health_reason: "page_health_probe_unavailable"});
      return;
    }
    if (!controls.length) {
      state.candidate_health_check = {remaining: readFailureLimit, checked_uids: []};
      state.diagnostic = {status: "ready", reason: "normal_control_sample_unavailable"};
      return;
    }
    const checks = [];
    for (const control of controls) {
      const result = await probe(control);
      checks.push({user_id: control.user_id, at_ms: now(), status: result.status,
        reason: result.reason || "", identity_verified: result.identity_verified === true});
      if (state.diagnostic.status === "blocked") return;
      if (healthyProbe(result, control.user_id)) {
        state.read_failure_uids = [];
        state.diagnostic = {status: "ready", reason: "individual_read_failures_isolated", health_checks: checks};
        return;
      }
    }
    recoveryRequired({health_reason: "normal_controls_unavailable", health_checks: checks});
  }
  async function readFailed(item, observation, readElapsedMs = 0) {
    const reason = observation.reason || "account_or_history_coverage_incomplete";
    const searchDiagnostics = copy(observation.search_diagnostics || []);
    const failureStage = observation.failure_stage || (/^(search|popup)/.test(reason) ? "search" :
      /history/.test(reason) ? "history" : /identity|conversation|customer_annotation_panel/.test(reason) ? "identity" : "observe");
    const failure = {user_id: item.user_id, failure_stage: failureStage, reason,
      retry_stage: item.retry_stage || "main", at_ms: now(), search_diagnostics: searchDiagnostics,
      ...(observation.detail ? {detail: String(observation.detail)} : {}),
      ...(observation.identity_diagnostics ? {identity_diagnostics: copy(observation.identity_diagnostics)} : {}),
      ...(observation.performance ? {performance: copy(observation.performance)} : {})};
    state.read_failures.push(failure);
    await event("read_failure", item.user_id, "deferred", 0, failure);
    if (enabled) await timed("record_defer", item.user_id, () => ledger.recordBrowserDefer({
      run_id: run.run_id, user_id: item.user_id, reason,
      retry_stage: item.retry_stage || "main", observed_at_ms: now(), reviews: observation.reviews || [],
      evidence: JSON.stringify({failure_stage: failureStage, search_diagnostics: searchDiagnostics,
        performance: observation.performance || {}, detail: observation.detail || "",
        identity_diagnostics: observation.identity_diagnostics || null}),
    }));
    if (immediateStops.has(reason)) return block(reason, {user_id: item.user_id});
    if (["search", "history", "identity"].includes(failureStage) || reason === "browser_read_exception") {
      if (!state.read_failure_uids.includes(item.user_id)) state.read_failure_uids.push(item.user_id);
      const window = state.health_window;
      if (!window.failed_uids.includes(item.user_id)) window.failed_uids.push(item.user_id);
      window.read_elapsed_ms += Math.max(0, readElapsedMs);
      if (state.candidate_health_check?.remaining === 0) {
        recoveryRequired({health_reason: "new_candidate_probes_unavailable"});
      } else if (window.failed_uids.length >= noProgressUidLimit ||
          (window.failed_uids.length >= readFailureLimit * 2 && window.read_elapsed_ms >= noProgressReadMs)) {
        // This is absence of new customer progress, even with a healthy page.
        // Diagnostic/UI pauses and model review waits are excluded from time.
        state.diagnostic = {status: "blocked", reason: "no_progress_read_window",
          user_ids: [...window.failed_uids], read_elapsed_ms: window.read_elapsed_ms};
      } else if (!state.candidate_health_check && state.read_failure_uids.length >= readFailureLimit) {
        await checkNormalControls();
      }
    }
    await save();
    return {status: "deferred", user_id: item.user_id, reason, failure_stage: failureStage, observation,
      diagnostic: copy(state.diagnostic)};
  }
  async function readSucceeded(item, observation) {
    state.read_failure_uids = [];
    state.health_window = {failed_uids: [], read_elapsed_ms: 0};
    state.candidate_health_check = null;
    state.diagnostic = {status: "ready", reason: ""};
    if (item?.user_id && observation) {
      state.health_controls = [{user_id: item.user_id,
        account_ids: observation.reviews?.map(r => r.account_id) || item.account_ids || [],
        selected_account_id: observation.selected_account_id || "", verified_at_ms: now()},
        ...state.health_controls.filter(control => control.user_id !== item.user_id)].slice(0, 3);
    }
    await save();
  }
  async function processInternal(item) {
    if (!item?.user_id) throw new TypeError("user_id_required");
    if (state.control.paused || pauseRequested) return {status: "paused"};
    const runBlocked = runGuard();
    if (runBlocked) return runBlocked;
    if (state.held_reviews[item.user_id]) return {status: "review_held", user_id: item.user_id,
      reason: state.held_reviews[item.user_id].hold_reason};
    if (held) return {status: "review_required", user_id: held.item.user_id, observation: copy(held.observation)};
    const pending = await pendingGuard();
    if (pending) return pending;
    if (state.diagnostic.status !== "ready") return copy(state.diagnostic);
    if (state.blocked_uids[item.user_id]) return {status: "skipped", user_id: item.user_id,
      reason: state.blocked_uids[item.user_id]};
    if (state.touch_policy === "uid_once" && state.sent_uids[item.user_id]) return {
      status: "skipped", user_id: item.user_id, reason: "already_sent_this_run"};
    if (state.candidate_health_check) {
      const check = state.candidate_health_check;
      if (check.checked_uids.includes(item.user_id)) return {status: "deferred", user_id: item.user_id,
        reason: "health_candidate_already_checked"};
      check.checked_uids.push(item.user_id);
      check.remaining -= 1;
      const probeStarted = now();
      const result = await probe(item);
      if (state.diagnostic.status === "blocked") return result;
      if (!healthyProbe(result, item.user_id)) return readFailed(item, {status: "deferred",
        reason: result.reason || "search_health_probe_unconfirmed", failure_stage: "search",
        search_diagnostics: result.search_diagnostics || [], identity_diagnostics: result.identity_diagnostics,
        detail: result.detail}, now() - probeStarted);
      state.candidate_health_check = null;
    }
    let observation, input;
    try { input = await observeInput(item); }
    catch (error) { return block("local_review_evidence_unavailable", {
      user_id: item.user_id, detail: String(error?.message || error)}); }
    const readStarted = now();
    observation = await adapterCall("observe", item.user_id, () => adapter.observeUid(input));
    if (observation.status === "blocked") return block(observation.reason, {
      user_id: item.user_id, detail: observation.detail || ""});
    if (!complete(observation) && !verifiedStop(observation)) return readFailed(item, observation, now() - readStarted);
    await readSucceeded(item, observation);
    if (!enabled && !batchMode) return {status: "read_only", user_id: item.user_id, observation};
    if (verifiedStop(observation)) {
      if (!enabled) return {status: "protected_read_only", user_id: item.user_id, observation};
      const prepared = await timed("prepare_stop", item.user_id, () => ledger.prepareTouch(prepareInput(item, observation)));
      if (prepared.action === "send") return block("protected_uid_unexpectedly_reserved", {user_id: item.user_id});
      return {status: "skipped", user_id: item.user_id, prepared};
    }
    held = {item: copy(item), observation};
    if (batchMode) {
      const prior = await readEvidence(state.review_cache[item.user_id]);
      const reviewStarted = now();
      const packet = makeReviewPacket(observation, prior);
      if (!packet || packet.reviews.some(r => !r.context_complete)) {
        held = null;
        return readFailed(item, {...observation, reason: "review_context_incomplete"});
      }
      const decisions = automaticDecisions(packet, prior);
      await event("semantic_review", item.user_id, decisions ? "approved" : "review_required", Math.max(0, now() - reviewStarted));
      const reuse = Boolean(decisions && prior?.approved);
      const reason = reuse ? "approved_evidence_reused" : prior?.approved ? "approved_evidence_changed" : "no_approved_evidence";
      const metric = state.metrics.review_cache ||= {count: 0, total_ms: 0, max_ms: 0, hit_count: 0, reread_reasons: {}};
      if (reuse) metric.hit_count += 1;
      else metric.reread_reasons[reason] = (metric.reread_reasons[reason] || 0) + 1;
      await event("review_cache", item.user_id, reuse ? "hit" : "reread", 0, {reason});
      if (!decisions) {
        state.pending_reviews[item.user_id] = {item: copy(item), packet: await writeEvidence(packet), queued_at_ms: now()};
        held = null;
        await save();
        return {status: "review_queued", user_id: item.user_id, evidence_id: packet.evidence_id};
      }
      const approved = await retainApproval(packet, decisions, prior?.visual_reviewed === true);
      if (!enabled) {
        held = null;
        return {status: "ready_read_only", user_id: item.user_id};
      }
      if (decisions.some(d => d.contact_shared || d.stop_contact)) {
        held = null;
        return {status: "skipped", user_id: item.user_id, reason: "reviewed_protection", label_updates: approved.label_updates || []};
      }
      return continueInternal(decisions);
    }
    const decisions = await timed("semantic_review", item.user_id, async () =>
      decisionsForNoCustomerContent(observation) || (review ? await review(copy(observation), copy(item)) : null));
    if (!decisions) return {status: "review_required", user_id: item.user_id, observation: copy(observation)};
    return continueInternal(decisions);
  }

  async function continueInternal(decisions) {
    if (!enabled) throw new Error("send_disabled");
    if (!held) throw new Error("no_pending_review");
    const runBlocked = runGuard();
    if (runBlocked) return runBlocked;
    const pending = await pendingGuard();
    if (pending) return pending;
    const {item} = held;
    let observation = held.observation;
    if (observation.reviews.some(r => Number.isFinite(r.observed_at_ms) && now() - r.observed_at_ms > 120000)) {
      held = null;
      return readFailed(item, {...observation, status: "deferred", reason: "account_reviews_stale_reobserve_required"});
    }
    // Adapter validates all-account decisions and exact stop-contact quotes.
    const confirmed = await adapterCall("confirm_review", item.user_id,
      () => adapter.confirmBusinessReview({token: observation.token, decisions}));
    if (!Array.isArray(confirmed)) {
      held = null;
      if (confirmed.status === "blocked") return block(confirmed.reason, {user_id: item.user_id, detail: confirmed.detail});
      return readFailed(item, confirmed);
    }
    observation.reviews = confirmed;
    const accounts = state.touch_policy === "uid_once" ? [recentAccount(observation, item)]
      : [observation.selected_account_id,
        ...observation.reviews.map(r => r.account_id).filter(id => id !== observation.selected_account_id)];
    const results = [];
    held = null;
    for (const accountId of accounts) {
      if (pauseRequested || state.control.paused) return {status: "paused", user_id: item.user_id, results};
      if (now() >= sliceDeadline) return {status: "yielded", user_id: item.user_id, results};
      const blocked = await pendingGuard();
      if (blocked) return {...blocked, results};
      if (observation.selected_account_id !== accountId) {
        const selectStarted = now();
        observation = await adapterCall("select_account", item.user_id, () => adapter.selectReviewedAccount({
          token: observation.token, accountId}));
        if (observation.status === "blocked") return {...await block(observation.reason, {
          user_id: item.user_id, detail: observation.detail}), results};
        if (!complete(observation)) return {...await readFailed(item, observation, now() - selectStarted), results};
      }
      const prepared = await timed("prepare", item.user_id, () => ledger.prepareTouch(prepareInput(item, observation)));
      if (prepared.action !== "send") {
        results.push({account_id: accountId, status: prepared.action, reason: prepared.reason,
          label_updates: prepared.label_updates || []});
        if (prepared.reason === "already_sent_this_run") {
          state.sent_uids[item.user_id] ||= {at_ms: now(), account_id: accountId, source: "ledger"};
          await save();
          if (state.touch_policy === "account_once") continue;
        }
        return {status: prepared.action, user_id: item.user_id, results};
      }
      const stage = await adapterCall("stage", item.user_id, () => adapter.revalidateAndStage({token: observation.token, prepared}));
      const safeAbortReasons = new Set(["page_or_customer_changed", "user_draft_present", "reply_input_not_unique",
        "account_reviews_stale_reobserve_required", "attribution_changed_for_distinct_copies",
        "message_exceeds_platform_limit", "preflight_changed_auto_draft_cleared"]);
      if (batchMode && stage.status === "not_staged" && safeAbortReasons.has(stage.reason) && ledger.cancelUnclickedTouch) {
        const cancelled = await timed("cancel_before_click", item.user_id, () => ledger.cancelUnclickedTouch({
          touch_id: prepared.touch_id, stage_status: stage.status, reason: stage.reason}));
        if (cancelled.ok !== true || cancelled.touch_id !== prepared.touch_id || cancelled.state !== "failed") {
          return block("reservation_requires_reconciliation", {user_id:item.user_id,touch_id:prepared.touch_id,stage});
        }
        state.blocked_uids[item.user_id] = "cancelled_before_click";
        await save();
        if (stage.reason === "user_draft_present") return block(stage.reason, {user_id:item.user_id});
        results.push({account_id:accountId,touch_id:prepared.touch_id,status:"failed",reason:"cancelled_before_click",stage});
        return {status:"processed",user_id:item.user_id,results};
      }
      if (stage.status !== "staged") return {...await block("reservation_requires_reconciliation", {
        user_id: item.user_id, touch_id: prepared.touch_id, stage}), results};
      const marked = await timed("mark_clicked", item.user_id, () => ledger.markClicked({touch_id: prepared.touch_id}));
      if (marked.ok !== true || marked.state !== "clicked" || marked.touch_id !== prepared.touch_id) {
        return {...await block("mark_clicked_not_durable", {user_id: item.user_id,
          touch_id: prepared.touch_id}), results};
      }
      const receipt = await adapterCall("click_and_verify", item.user_id,
        () => adapter.clickAndVerify({stageToken: stage.stageToken, markClicked: marked}),
        {status: "uncertain", reason: "click_or_receipt_exception"});
      if (receipt.status === "not_clicked") return {...await block("click_marker_requires_reconciliation", {
        user_id: item.user_id, touch_id: prepared.touch_id, receipt}), results};
      const status = ["sent", "failed"].includes(receipt.status) ? receipt.status : "uncertain";
      const detail = receipt.platform_notices?.length ? JSON.stringify({reason: receipt.reason || status,
        detail: receipt.detail || "", platform_notices: receipt.platform_notices}) : receipt.detail || receipt.reason || status;
      const finished = await timed("finish", item.user_id, () => ledger.finishTouch({touch_id: prepared.touch_id, status,
        reason: receipt.reason || status, detail,
        verified_at_ms: receipt.sent_at_ms || now()}));
      if (finished.ok !== true) return {...await block("finish_not_durable", {user_id: item.user_id,
        touch_id: prepared.touch_id}), results};
      results.push({account_id: accountId, touch_id: prepared.touch_id, status, receipt});
      if (status === "sent") {
        state.sent_uids[item.user_id] = {at_ms: receipt.sent_at_ms || now(), account_id: accountId,
          touch_id: prepared.touch_id};
        await save();
      }
      if (receipt.reason === "browser_connection_unavailable") {
        state.blocked_uids[item.user_id] = "uncertain";
        return {...await block("browser_connection_unavailable", {user_id: item.user_id, detail: receipt.detail}), results};
      }
      if (status !== "sent") {
        state.blocked_uids[item.user_id] = status;
        await save();
        break; // No retry and no other account for this UID; next UID may run.
      }
    }
    return {status: "processed", user_id: item.user_id, results};
  }

  async function recover({item, replacementAdapter} = {}) {
    return exclusive(async () => {
      const pending = await pendingGuard();
      if (pending) return pending;
      if (state.recovery.used || state.diagnostic.status !== "recovery_required") {
        return {status: "blocked", reason: "recovery_not_allowed"};
      }
      if (!item?.user_id || !state.read_failure_uids.includes(item.user_id) || item.retry_stage !== "slow") {
        throw new Error("failed_uid_with_slow_retry_required");
      }
      state.recovery = {used: true, result: "started", started_at_ms: now()};
      await save(); // A restarted process must not silently grant another recovery.
      if (replacementAdapter) adapter = replacementAdapter;
      const page = await adapterCall("recover_page", item.user_id, () => adapter.ensurePage());
      if (page.status === "blocked") return block(page.reason, {user_id: item.user_id, detail: page.detail});
      const controls = state.health_controls.filter(control =>
        !state.health_window.failed_uids.includes(control.user_id)).slice(0, 2);
      if (page.ok && adapter.probeUid && controls.length) {
        const checks = [];
        for (const control of controls) {
          const result = await probe(control);
          checks.push({user_id: control.user_id, status: result.status, reason: result.reason || "", at_ms: now()});
          if (healthyProbe(result, control.user_id)) {
            state.recovery = {...state.recovery, result: "passed", health_checks: checks};
            await readSucceeded();
            return {status: "recovered", observation: result, control_uid: control.user_id, sends: 0, read_only: true};
          }
          if (immediateStops.has(result.reason) || result.reason === "browser_connection_unavailable") break;
        }
        state.recovery = {...state.recovery, result: "failed", health_checks: checks};
        return block("read_only_recovery_failed", {user_id: item.user_id, health_checks: checks});
      }
      const recoveryInput = page.ok ? await observeInput(item, 45000) : null;
      const observation = page.ok
        ? await adapterCall("recover_observe", item.user_id, () => adapter.observeUid(recoveryInput))
        : {status: "deferred", reason: page.reason, reviews: []};
      if (observation.status === "blocked") return block(observation.reason, {user_id: item.user_id, detail: observation.detail});
      state.recovery.result = complete(observation) || verifiedStop(observation) ? "passed" : "failed";
      if (state.recovery.result === "failed") {
        await readFailed(item, observation);
        return block("read_only_recovery_failed", {user_id: item.user_id, observation});
      }
      await readSucceeded(item, observation);
      // Recovery is always read-only; fresh normal process() is required to send.
      return {status: "recovered", observation, sends: 0, read_only: true};
    });
  }

  async function batchInternal(items, limit) {
    if (!Number.isInteger(limit) || limit < 1) throw new TypeError("invalid_batch_limit");
    const results = [];
    for (const item of items.slice(0, limit)) {
      const result = await processInternal(item);
      results.push(result);
      if (held || state.diagnostic.status !== "ready") break;
    }
    return {results, diagnostic: copy(state.diagnostic)};
  }

  function validateSliceOptions({limit = 50, maxDurationMs = 20000, maxReviews = 12} = {}) {
    if (!batchMode) throw new Error("batch_mode_required");
    if (!Number.isInteger(limit) || limit < 1 || limit > 100 ||
        !Number.isInteger(maxReviews) || maxReviews < 1 || maxReviews > 30 ||
        !Number.isFinite(maxDurationMs) || maxDurationMs < 1 || maxDurationMs > 30000) throw new Error("invalid_slice_limits");
    return {limit, maxDurationMs, maxReviews};
  }

  async function sliceInternal(options = {}) {
    const {limit, maxDurationMs, maxReviews} = validateSliceOptions(options);
    const started = now();
    sliceDeadline = started + maxDurationMs;
    const pending = await pendingGuard();
    if (pending) return pending;
    if (state.control.paused || pauseRequested) return {status: "paused", results: []};
    const runBlocked = runGuard();
    if (runBlocked) return {...runBlocked, results: []};
    if (state.last_yield_at_ms && !state.control.paused) {
      await event("between_slices", "", "elapsed", Math.max(0, started - state.last_yield_at_ms));
    }
    const results = [];
    // Exclude local waiting jobs before the ledger applies its output limit;
    // otherwise a full first window can hide every later fresh customer.
    const excludeUserIds = [...new Set([...Object.keys(state.pending_reviews),
      ...Object.keys(state.held_reviews), ...Object.keys(state.ready_jobs),
      ...(state.candidate_health_check?.checked_uids || []),
      ...(!enabled ? Object.keys(state.inspected_uids) : [])])];
    const next = await ledger.next({run_id: run.run_id, limit: 100, exclude_user_ids: excludeUserIds});
    if (!Array.isArray(next.items)) throw new Error("ledger_next_items_missing");
    const queued = [...Object.values(state.ready_jobs), ...next.items];
    const visited = new Set();
    for (const item of queued) {
      if (results.length >= limit || now() - started >= maxDurationMs || pauseRequested || state.control.paused ||
          state.diagnostic.status !== "ready") break;
      if (visited.has(item.user_id) || state.pending_reviews[item.user_id] || state.held_reviews[item.user_id] ||
          (!enabled && state.inspected_uids[item.user_id] && !state.ready_jobs[item.user_id])) continue;
      // Backpressure limits fresh admission, not execution of approved work.
      // Reobservation may legitimately return an approved job to review.
      if (!state.ready_jobs[item.user_id] && Object.keys(state.pending_reviews).length >= maxReviews) break;
      visited.add(item.user_id);
      const result = await processInternal(item);
      results.push(result);
      delete state.ready_jobs[item.user_id];
      if (!enabled) state.inspected_uids[item.user_id] = result.status;
      await save();
    }
    if (pauseRequested) await persistPause();
    state.last_yield_at_ms = now();
    await save();
    return {mode: enabled ? "execute" : "read_only", results, elapsed_ms: now() - started,
      pending_review_count: Object.keys(state.pending_reviews).length,
      held_review_count: Object.keys(state.held_reviews).length,
      ready_count: Object.keys(state.ready_jobs).length, paused: state.control.paused, diagnostic: copy(state.diagnostic)};
  }

  async function submitReviewsInternal(approvals) {
    if (!batchMode || !Array.isArray(approvals)) throw new Error("batch_approvals_required");
    const accepted = [], label_updates = [];
    for (const approval of approvals) {
      const job = state.pending_reviews[approval.uid] || state.held_reviews[approval.uid];
      if (!job) throw new Error("pending_review_not_found");
      const packet = await readEvidence(job.packet);
      const checked = approvePacket(packet, approval);
      const retained = await retainApproval(packet, checked.decisions, approval.visual_reviewed);
      label_updates.push(...(retained.label_updates || []));
      if (!checked.decisions.some(d => d.contact_shared || d.stop_contact)) state.ready_jobs[approval.uid] = job.item;
      delete state.pending_reviews[approval.uid];
      delete state.held_reviews[approval.uid];
      await event("review_wait", approval.uid, "approved", Math.max(0, now() - job.queued_at_ms));
      accepted.push(approval.uid);
    }
    return {accepted, sends: 0, label_updates};
  }

  return {
    verifyConnection: item => exclusive(async () => {
      if (state.diagnostic.reason !== "browser_connection_unavailable") throw new Error("connection_check_not_required");
      const pending = await pendingGuard();
      if (pending) return pending;
      if (!item?.user_id) throw new Error("user_id_required");
      // A read-only proof, not a resend or a reset of prior search failures.
      const input = await observeInput(item);
      const observation = await adapterCall("connection_check", item.user_id, () => adapter.observeUid(input));
      if (observation.status === "blocked") return block(observation.reason, {
        user_id: item.user_id, detail: observation.detail});
      if (!complete(observation) && !verifiedStop(observation)) return {
        status:"blocked", reason:"connection_check_incomplete", observation};
      state.diagnostic = {status:"ready",reason:""};
      await save();
      return {status:"connection_verified",user_id:item.user_id,sends:0,paused:state.control.paused};
    }),
    runSlice: options => exclusive(() => sliceInternal(options)),
    reviewPackets: ({queue = "pending", limit = 12, offset = 0} = {}) => exclusive(async () => {
      if (!["pending", "held", "all"].includes(queue) || !Number.isInteger(limit) || limit < 1 || limit > 30 ||
          !Number.isSafeInteger(offset) || offset < 0) {
        throw new Error("invalid_review_packet_selection");
      }
      const jobs = queue === "pending" ? Object.values(state.pending_reviews) : queue === "held"
        ? Object.values(state.held_reviews) : [...Object.values(state.pending_reviews), ...Object.values(state.held_reviews)];
      return Promise.all(jobs.slice(offset, offset + limit).map(async job => copy(await readEvidence(job.packet))));
    }),
    holdReviews: holds => exclusive(async () => {
      if (!batchMode || !Array.isArray(holds)) throw new Error("batch_holds_required");
      const checked = [], seen = new Set();
      for (const hold of holds) {
        if (!hold?.uid || seen.has(hold.uid)) throw new Error("invalid_hold_uid");
        if (typeof hold.reason !== "string" || !hold.reason.trim() || hold.reason.length > 500) {
          throw new Error("hold_reason_required");
        }
        seen.add(hold.uid);
        const job = state.pending_reviews[hold.uid];
        if (!job) throw new Error("pending_review_not_found");
        const packet = await readEvidence(job.packet);
        if (hold.evidence_id !== packet.evidence_id) throw new Error("hold_evidence_mismatch");
        checked.push({uid: hold.uid, job, reason: hold.reason.trim()});
      }
      for (const {uid, job, reason} of checked) {
        state.held_reviews[uid] = {...job, held_at_ms: now(), hold_reason: reason};
        delete state.pending_reviews[uid];
      }
      await save();
      return {held: checked.map(h => h.uid), sends: 0, held_review_count: Object.keys(state.held_reviews).length};
    }),
    submitReviews: approvals => exclusive(() => submitReviewsInternal(approvals)),
    submitReviewsAndRunSlice: (approvals, options) => exclusive(async () => {
      validateSliceOptions(options);
      const approval = await submitReviewsInternal(approvals);
      return {approval, slice: await sliceInternal(options)};
    }),
    requestPause: () => {pauseRequested = true;},
    pause: () => exclusive(async () => {
      pauseRequested = true;
      await persistPause({forceLifecycle: true});
      return {status: "paused"};
    }),
    resume: () => exclusive(async () => {
      if (enabled && ledger.resumeRun) {
        try {
          const result = await ledger.resumeRun({run_id: run.run_id, detail: "workflow_user_resume"});
          if (result.ok === false) throw new Error(result.reason || "ledger_resume_not_durable");
          state.run_state = result.run_state || result.state || "active";
          delete state.lifecycle_error;
        } catch (error) {
          state.lifecycle_error = {action: "resume", at_ms: now(), reason: String(error?.message || error)};
          await save();
          return {status: "blocked", reason: "lifecycle_resume_failed", detail: state.lifecycle_error.reason};
        }
      } else if (enabled && state.run_state === "paused") {
        return {status: "blocked", reason: "ledger_resume_required"};
      }
      pauseRequested = false;
      state.control = {paused: false};
      state.last_yield_at_ms = null;
      await event("control", "", "resumed", 0);
      return {status: "ready"};
    }),
    process: item => exclusive(() => processInternal(item)),
    continueReview: decisions => exclusive(() => continueInternal(decisions)),
    runBatch: (items, {limit = 10} = {}) => exclusive(() => batchInternal(items, limit)),
    runNext: ({limit = 5} = {}) => exclusive(async () => {
      if (!ledger?.next) throw new Error("ledger_next_required");
      const next = await ledger.next({run_id: run.run_id, limit});
      if (!Array.isArray(next.items)) throw new Error("ledger_next_items_missing");
      return batchInternal(next.items, limit);
    }),
    recover,
    summary: ({includeItems = false} = {}) => exclusive(async () => {
      const report = ledger?.report ? await ledger.report({run_id: run.run_id}) : null;
      let session = null;
      const completedRun = ["completed", "completed_with_exceptions"].includes(state.run_state);
      if (ledger?.sessionStatus && (completedRun || state.session_id)) {
        try { session = await ledger.sessionStatus(completedRun ? {run_id: run.run_id} : {session_id: state.session_id}); }
        catch (error) {
          if (!completedRun || error.ledgerError !== "session_not_found") throw error;
        }
        if (session && (session.run_id !== run.run_id || !session.session_id)) throw new Error("ledger_summary_session_mismatch");
      }
      const runStatus = ledger?.runStatus ? await ledger.runStatus({run_id: run.run_id,
        ...(includeItems ? {include_items: true} : {})}) : null;
      const localUserIds = [...new Set([...Object.keys(state.pending_reviews), ...Object.keys(state.held_reviews),
        ...Object.keys(state.ready_jobs)])];
      const uninitialized = runStatus?.initialized === false || runStatus?.initialization_required === true;
      const unresolvedIds = !uninitialized && Array.isArray(runStatus?.items) ? [...new Set([
        ...runStatus.items.map(item => item.user_id), ...localUserIds])] : null;
      const queueStatus = {pending_review_count: Object.keys(state.pending_reviews).length,
        held_review_count: Object.keys(state.held_reviews).length, ready_count: Object.keys(state.ready_jobs).length,
        // These local jobs are views of the same run's customer queue. They
        // must never simply be added to the ledger's unresolved UID count.
        uncompleted_customers: uninitialized ? null
          : unresolvedIds?.length ?? runStatus?.counts?.uncompleted_customers ?? localUserIds.length,
        ...(runStatus?.initialized !== undefined ? {initialized: runStatus.initialized} : {}),
        ...(runStatus?.initialization_required !== undefined
          ? {initialization_required: runStatus.initialization_required} : {}),
        ...(unresolvedIds ? {uncompleted_user_ids: unresolvedIds} : {})};
      return {mode: enabled ? "execute" : "read_only", state: copy(state), report, session, run_status: runStatus,
        ...(runStatus?.startup ? {startup: {...copy(runStatus.startup), send_enabled: enabled,
          paused: state.control.paused || pauseRequested || state.run_state === "paused"}} : {}),
        queue_status: queueStatus,
        failures: copy(state.read_failures), since_start: session
          ? Object.fromEntries(reportFields.map(key => [key, Number(session.delta[key.replace("_this_run", "")] || 0)]))
          : state.baseline && report ? Object.fromEntries(reportFields.map(key =>
            [key, Number(report[key] || 0) - state.baseline[key]])) : null};
    }, {readOnly: !enabled, completedReadOnly: true}),
  };
}
