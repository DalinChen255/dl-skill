// Local-only bridge for CUA REPL. No browser control, network calls, shell, or
// hard-coded workspace paths. Default ledger access is read-only.
//
// const {createXhsLocalLedger, createFileProgressStore} = await import('file:///...');
// const ledger = createXhsLocalLedger({scriptPath, dbPath});
// await ledger.next({run_id, limit: 5});
// Actual authorized execution additionally requires writesEnabled:true on BOTH
// factories, and sendEnabled:true on createXhsFollowupWorkflow.

import {execFile as nodeExecFile} from "node:child_process";
import * as fs from "node:fs/promises";
import {dirname, basename, isAbsolute, join} from "node:path";
import {randomUUID, createHash} from "node:crypto";

function absolutePath(value, name) {
  if (typeof value !== "string" || !isAbsolute(value)) throw new TypeError(`${name}_must_be_absolute`);
  return value;
}
function required(value, name) {
  if (typeof value !== "string" || !value.trim()) throw new TypeError(`${name}_required`);
  return value;
}

export function createXhsLocalLedger({scriptPath, dbPath, python = "python3", writesEnabled = false,
  timeoutMs = 30000, maxBuffer = 8 * 1024 * 1024, execFile = nodeExecFile} = {}) {
  absolutePath(scriptPath, "script_path");
  absolutePath(dbPath, "db_path");
  required(python, "python");
  async function command(name, args = [], payload, mutation = false) {
    if (mutation && writesEnabled !== true) throw new Error("ledger_writes_disabled");
    const argv = [scriptPath, "--db", dbPath, name, ...args];
    if (payload !== undefined) argv.push("--input-json", "-");
    return new Promise((resolve, reject) => {
      let child;
      try {
        child = execFile(python, argv, {encoding: "utf8", timeout: timeoutMs, maxBuffer, shell: false},
          (error, stdout) => {
            let result;
            try { result = JSON.parse(stdout || "null"); }
            catch { reject(new Error(`ledger_${name}_invalid_json`)); return; }
            if (error || !result || result.ok === false) {
              const failure = new Error(`ledger_${name}_failed: ${result?.error || error?.code || "unknown"}`);
              failure.command = name;
              failure.ledgerError = result?.error || "";
              reject(failure);
            } else resolve(result);
          });
        child.stdin?.on("error", () => {}); // The execFile callback reports early exits.
        child.stdin?.end(payload === undefined ? "" : JSON.stringify(payload));
      } catch (error) { reject(error); }
    });
  }
  const runArgs = value => ["--run-id", required(value, "run_id")];
  const touchArgs = value => ["--touch-id", required(value, "touch_id")];
  return {
    pendingTouches: () => command("pending-touches"),
    report: ({run_id}) => command("report", runArgs(run_id)),
    runStatus: ({run_id, include_items = false}) => {
      if (typeof include_items !== "boolean") throw new TypeError("invalid_include_items");
      return command("run-status", [...runArgs(run_id), ...(include_items ? ["--include-items"] : [])]);
    },
    setTouchPolicy: ({run_id, policy, evidence}) => {
      if (!["uid_once", "account_once"].includes(policy)) throw new TypeError("invalid_touch_policy");
      return command("set-touch-policy", [...runArgs(run_id), "--policy", policy,
        "--evidence", required(evidence, "policy_evidence")], undefined, true);
    },
    pauseRun: ({run_id, detail, state = "paused"}) => {
      if (!["paused", "blocked"].includes(state)) throw new TypeError("invalid_run_state");
      return command("pause-run", [...runArgs(run_id), "--detail", required(detail, "pause_detail"),
        "--state", state], undefined, true);
    },
    resumeRun: ({run_id, detail}) => command("resume-run", [...runArgs(run_id),
      "--detail", required(detail, "resume_detail")], undefined, true),
    endRun: ({run_id, detail}) => command("end-run", [...runArgs(run_id),
      "--detail", required(detail, "end_detail")], undefined, true),
    next: ({run_id, limit = 1, exclude_user_ids = []}) => {
      if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw new TypeError("invalid_next_limit");
      if (!Array.isArray(exclude_user_ids) || exclude_user_ids.some(uid => typeof uid !== "string" || !uid.trim())) {
        throw new TypeError("invalid_exclude_user_ids");
      }
      const exclusions = [...new Set(exclude_user_ids)];
      return command("next", [...runArgs(run_id), "--limit", String(limit)],
        exclusions.length ? {exclude_user_ids: exclusions} : undefined);
    },
    sessionStatus: ({session_id, run_id} = {}) => {
      if (Boolean(session_id) === Boolean(run_id)) throw new TypeError("session_id_or_run_id_required");
      return command("session-status", session_id ? ["--session-id", required(session_id, "session_id")] : runArgs(run_id));
    },
    beginSession: ({run_id, session_id}) => command("begin-session",
      [...runArgs(run_id), "--session-id", required(session_id, "session_id")], undefined, true),
    endSession: ({session_id, detail = ""}) => command("end-session",
      ["--session-id", required(session_id, "session_id"), "--detail", String(detail)], undefined, true),
    prepareTouch: payload => command("prepare-touch", [], payload, true),
    recordReviewedHistory: payload => command("record-reviewed-history", [], payload, true),
    cancelUnclickedTouch: payload => command("cancel-unclicked-touch", [], payload, true),
    recordBrowserDefer: payload => command("record-browser-defer", [], payload, true),
    markClicked: ({touch_id}) => command("mark-clicked", touchArgs(touch_id), undefined, true),
    finishTouch: payload => command("finish-touch", [], payload, true),
    recordNoIntent: payload => command("record-no-intent", [], payload, true),
    // Resolution is deliberately not automated by the workflow: caller must
    // first examine the actual chat and provide the evidence in detail.
    resolveTouch: ({touch_id, resolution, detail}) => command("resolve-touch",
      [...touchArgs(touch_id), "--resolution", required(resolution, "resolution"),
        "--detail", required(detail, "detail")], undefined, true),
  };
}

export function createFileReviewStore({directory, writesEnabled = false} = {}) {
  absolutePath(directory, "review_directory");
  const digest = text => createHash("sha256").update(text).digest("hex");
  return {
    async put(value) {
      if (!writesEnabled) throw new Error("review_writes_disabled");
      const text = JSON.stringify(value), reference = digest(text);
      await fs.mkdir(directory, {recursive: true, mode: 0o700});
      const path = join(directory, reference + ".json");
      const temporary = join(directory, "." + reference + "." + randomUUID() + ".tmp");
      let handle;
      try {
        handle = await fs.open(temporary, "wx", 0o600);
        await handle.writeFile(text); await handle.sync(); await handle.close(); handle = null;
        await fs.rename(temporary, path);
        const parent = await fs.open(directory, "r");
        try { await parent.sync(); } finally { await parent.close(); }
      } finally {
        await handle?.close(); await fs.rm(temporary, {force: true});
      }
      return {reference};
    },
    async get({reference} = {}) {
      if (!/^[a-f0-9]{64}$/.test(reference || "")) throw new Error("invalid_review_reference");
      const text = await fs.readFile(join(directory, reference + ".json"), "utf8");
      if (digest(text) !== reference) throw new Error("review_evidence_corrupt");
      return JSON.parse(text);
    },
  };
}

export function createFileProgressStore({path, runId, legacyRecoveryPaths, writesEnabled = false} = {}) {
  absolutePath(path, "progress_path");
  if (writesEnabled === true) required(runId, "progress_run_id");
  const legacyPaths = legacyRecoveryPaths || (runId ? [join(dirname(path), `recovery-progress-${runId}.json`)] : []);
  for (const legacyPath of legacyPaths) absolutePath(legacyPath, "legacy_recovery_path");
  const migrationError = (sourcePath, legacy) => {
    const error = new Error("progress_migrate_required");
    error.code = "progress_migrate_required";
    error.source_path = sourcePath;
    error.legacy_state = legacy;
    return error;
  };
  async function readJson(sourcePath) {
    try { return JSON.parse(await fs.readFile(sourcePath, "utf8")); }
    catch (error) { if (error.code === "ENOENT") return null; throw error; }
  }
  function validState(state) {
    return state?.version === 1 && (!runId || state.run_id === runId) &&
      typeof state.recovery?.used === "boolean" && typeof state.diagnostic?.status === "string" &&
      Array.isArray(state.read_failure_uids) && Array.isArray(state.recent_events) &&
      state.metrics && state.blocked_uids;
  }
  async function validateLegacyPreservation(state) {
    for (const legacyPath of legacyPaths) {
      const legacy = await readJson(legacyPath);
      if (!legacy || (legacyPath === path && validState(legacy))) continue;
      const attempts = Number(legacy.attempts ?? legacy.recovery?.attempts ?? (legacy.recovery?.used ? 1 : 0));
      if (!state || JSON.stringify(state.legacy_recovery) !== JSON.stringify(legacy) ||
          (attempts > 0 && (!state.recovery.used || state.recovery.attempts < attempts))) {
        throw migrationError(legacyPath, legacy);
      }
    }
  }
  let writeActive = false;
  return {
    async withExclusive(operation) {
      // All progress files in one private directory share a page controller.
      // Never expire a lease by age: a slow live operation may still click.
      if (!writesEnabled) return operation();
      await fs.mkdir(dirname(path), {recursive: true, mode: 0o700});
      const lockPath = join(dirname(path), ".xhs-executor.lock");
      let handle;
      try { handle = await fs.open(lockPath, "wx", 0o600); }
      catch (error) {
        if (error.code === "EEXIST") throw new Error("executor_already_owned");
        throw error;
      }
      try {
        await handle.writeFile(JSON.stringify({owner: randomUUID(), progress_path: path, acquired_at_ms: Date.now()}));
        await handle.sync();
        return await operation();
      } finally { await handle.close(); await fs.rm(lockPath); }
    },
    evidence: createFileReviewStore({directory: join(dirname(path), basename(path) + ".reviews"), writesEnabled}),
    async appendEvent(event) {
      if (writesEnabled !== true) throw new Error("progress_writes_disabled");
      if (event?.run_id !== runId) throw new Error("event_run_mismatch");
      if (!Number.isInteger(event.sequence) || event.sequence < 1 ||
          !Number.isFinite(event.at_ms) || event.at_ms < 0 ||
          !Number.isFinite(event.elapsed_ms) || event.elapsed_ms < 0 ||
          typeof event.stage !== "string" || !event.stage) throw new Error("invalid_progress_event");
      const line = JSON.stringify(event) + "\n";
      if (Buffer.byteLength(line, "utf8") > 64 * 1024) throw new Error("progress_event_too_large");
      await fs.mkdir(dirname(path), {recursive: true, mode: 0o700});
      // The atomic progress and ledger are the authoritative checkpoints.
      // This journal keeps timing/diagnostics beyond the last 100 events.
      const handle = await fs.open(path + ".events.jsonl", "a", 0o600);
      try { await handle.writeFile(line, "utf8"); }
      finally { await handle.close(); }
    },
    async load() {
      const state = await readJson(path);
      if (state && !validState(state)) throw migrationError(path, state);
      await validateLegacyPreservation(state);
      return state;
    },
    async save(state) {
      if (writesEnabled !== true) throw new Error("progress_writes_disabled");
      if (!validState(state)) throw migrationError(path, state);
      await validateLegacyPreservation(state);
      if (writeActive) throw new Error("progress_write_already_running");
      writeActive = true;
      let handle;
      const directory = dirname(path);
      const temporary = join(directory, `.${basename(path)}.${randomUUID()}.tmp`);
      try {
        await fs.mkdir(directory, {recursive: true, mode: 0o700});
        handle = await fs.open(temporary, "wx", 0o600);
        await handle.writeFile(JSON.stringify(state) + "\n", "utf8");
        await handle.sync();
        await handle.close(); handle = null;
        await fs.rename(temporary, path);
        const parent = await fs.open(directory, "r");
        try { await parent.sync(); } finally { await parent.close(); }
      } finally {
        await handle?.close();
        await fs.rm(temporary, {force: true});
        writeActive = false;
      }
    },
  };
}
