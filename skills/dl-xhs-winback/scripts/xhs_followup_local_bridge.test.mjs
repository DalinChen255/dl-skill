import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {fileURLToPath} from "node:url";
import {execFile} from "node:child_process";
import {promisify} from "node:util";
import {createXhsLocalLedger, createFileProgressStore, createFileReviewStore} from "./xhs_followup_local_bridge.mjs";
import {createXhsFollowupWorkflow, migrateLegacyRecovery} from "./xhs_followup_workflow.mjs";

async function temporary(t) {
  const root = await fs.mkdtemp(join(tmpdir(), "xhs-bridge-test-"));
  t.after(() => fs.rm(root, {recursive: true, force: true}));
  const scriptPath = join(root, "本地 fake ledger.py");
  await fs.writeFile(scriptPath, `import json, sys\nargs = sys.argv[1:]\npayload = json.load(sys.stdin) if '--input-json' in args else None\nprint(json.dumps({'ok': True, 'argv': args, 'payload': payload}, ensure_ascii=False))\n`, {mode: 0o600});
  return {root, scriptPath, dbPath: join(root, "fake only.sqlite3")};
}

test("different progress stores share an exclusive controller and release it on errors", async t => {
  const {root} = await temporary(t);
  const a = createFileProgressStore({path:join(root,"a.json"),runId:"a",writesEnabled:true});
  const b = createFileProgressStore({path:join(root,"b.json"),runId:"b",writesEnabled:true});
  await a.withExclusive(async () => {
    await assert.rejects(b.withExclusive(async () => assert.fail("second controller entered")), /executor_already_owned/);
  });
  await assert.rejects(a.withExclusive(async () => {throw new Error("controlled failure");}), /controlled failure/);
  assert.equal(await b.withExclusive(async () => "released"), "released");
});

test("an existing batch instance reloads a pause persisted by another instance", async t => {
  const {root} = await temporary(t);
  const path = join(root,"progress.json"), run={run_id:"pause-run"};
  const create = () => createXhsFollowupWorkflow({run,batchMode:true,
    adapter:{observeUid:async()=>assert.fail("paused controller touched page")},
    ledger:{next:async()=>({items:[]})},
    progressStore:createFileProgressStore({path,runId:run.run_id,writesEnabled:true})});
  const a=create(), b=create();
  await a.summary();
  await b.pause();
  assert.equal((await a.runSlice()).status,"paused");
});

test("default bridge only reads: mutation fails before any subprocess", async t => {
  const paths = await temporary(t);
  const ledger = createXhsLocalLedger(paths);
  const read = await ledger.next({run_id: "run1", limit: 3});
  assert.deepEqual(read.argv, ["--db", paths.dbPath, "next", "--run-id", "run1", "--limit", "3"]);
  await assert.rejects(ledger.beginSession({run_id: "run1", session_id: "request1"}), /writes_disabled/);
  await assert.rejects(ledger.prepareTouch({run_id: "run1"}), /writes_disabled/);
  await assert.rejects(ledger.recordReviewedHistory({run_id: "run1"}), /writes_disabled/);
  await assert.rejects(ledger.cancelUnclickedTouch({touch_id: "t1"}), /writes_disabled/);
  await assert.rejects(ledger.finishTouch({touch_id: "t1", status: "sent"}), /writes_disabled/);
  await assert.rejects(fs.stat(paths.dbPath), {code: "ENOENT"});
});

test("review evidence is private, immutable by reference, and survives a new store instance",async t=>{
  const {root}=await temporary(t);
  const directory=join(root,"reviews");
  const store=createFileReviewStore({directory,writesEnabled:true});
  const value={uid:"u1",text:"完整聊天证据".repeat(10000)};
  const pointer=await store.put(value);
  assert.ok(JSON.stringify(pointer).length<100);
  assert.deepEqual(await createFileReviewStore({directory}).get(pointer),value);
  assert.equal((await fs.stat(join(directory,pointer.reference+".json"))).mode&0o777,0o600);
  await assert.rejects(createFileReviewStore({directory}).put(value),/writes_disabled/);
  await assert.rejects(store.get({reference:"../state"}),/invalid_review_reference/);
  await fs.writeFile(join(directory,pointer.reference+".json"),"{}");
  await assert.rejects(store.get(pointer),/review_evidence_corrupt/);
});

test("argv and stdin preserve Unicode, line breaks, quotes and shell syntax literally", async t => {
  const paths = await temporary(t);
  const ledger = createXhsLocalLedger({...paths, writesEnabled: true});
  const payload = {user_id: "中文 UID", detail: "第一行\n第二行 $HOME $(touch /never-run) `id` ' \""};
  const out = await ledger.prepareTouch(payload);
  assert.deepEqual(out.payload, payload);
  assert.deepEqual(out.argv.slice(-3), ["prepare-touch", "--input-json", "-"]);
  const sessionId = "request ' $(echo no) 中文";
  const begin = await ledger.beginSession({run_id: "run1", session_id: sessionId});
  assert.equal(begin.argv.at(-1), sessionId);
});

test("session status selector and bounded next reject ambiguous arguments", async t => {
  const paths = await temporary(t);
  const ledger = createXhsLocalLedger(paths);
  assert.throws(() => ledger.next({run_id: "r", limit: 0}), /invalid_next_limit/);
  assert.throws(() => ledger.next({run_id: "r", limit: 101}), /invalid_next_limit/);
  assert.throws(() => ledger.sessionStatus({session_id: "s", run_id: "r"}), /session_id_or_run_id/);
  const status = await ledger.sessionStatus({session_id: "original-user-request"});
  assert.deepEqual(status.argv.slice(-3), ["session-status", "--session-id", "original-user-request"]);
});

test("complete status is read-only and lifecycle changes require an explicit writable bridge",async t=>{
  const paths=await temporary(t);
  const readonly=createXhsLocalLedger(paths);
  const compact=await readonly.runStatus({run_id:"r"});
  assert.deepEqual(compact.argv.slice(-3),["run-status","--run-id","r"]);
  const full=await readonly.runStatus({run_id:"r",include_items:true});
  assert.deepEqual(full.argv.slice(-4),["run-status","--run-id","r","--include-items"]);
  await assert.rejects(readonly.setTouchPolicy({run_id:"r",policy:"uid_once",evidence:"用户授权"}),/writes_disabled/);
  await assert.rejects(readonly.pauseRun({run_id:"r",detail:"用户暂停"}),/writes_disabled/);
  await assert.rejects(readonly.resumeRun({run_id:"r",detail:"用户恢复"}),/writes_disabled/);
  await assert.rejects(readonly.endRun({run_id:"r",detail:"主队列完成"}),/writes_disabled/);
  const writer=createXhsLocalLedger({...paths,writesEnabled:true});
  const literal="用户授权\n' $(touch /never-run) `id`";
  const changed=await writer.setTouchPolicy({run_id:"r",policy:"uid_once",evidence:literal});
  assert.deepEqual(changed.argv.slice(-7),["set-touch-policy","--run-id","r","--policy","uid_once","--evidence",literal]);
  assert.equal((await writer.endRun({run_id:"r",detail:literal})).argv.at(-1),literal);
  await assert.rejects(fs.stat(paths.dbPath),{code:"ENOENT"});
});

test("read-only next sends deduplicated UID exclusions through stdin literally", async t => {
  const paths = await temporary(t);
  const ledger = createXhsLocalLedger(paths);
  const uid = "中文 UID ' $(touch /never-run) `id`";
  const out = await ledger.next({run_id:"r",limit:2,exclude_user_ids:[uid,"u1",uid]});
  assert.deepEqual(out.payload, {exclude_user_ids:[uid,"u1"]});
  assert.deepEqual(out.argv, ["--db",paths.dbPath,"next","--run-id","r","--limit","2","--input-json","-"]);
  await assert.rejects(fs.stat(paths.dbPath), {code:"ENOENT"});
  for (const exclude_user_ids of [null,"u1",{},[""],["  "],[1],[true],["u1",null]]) {
    assert.throws(() => ledger.next({run_id:"r",exclude_user_ids}), /invalid_exclude_user_ids/);
  }
});

test("progress store is read-only by default and reads do not create files", async t => {
  const {root} = await temporary(t);
  const path = join(root, "not-created", "progress.json");
  const store = createFileProgressStore({path});
  assert.equal(await store.load(), null);
  await assert.rejects(store.save({run_id: "r"}), /writes_disabled/);
  await assert.rejects(fs.stat(join(root, "not-created")), {code: "ENOENT"});
});

test("atomic progress overwrite is private, complete, and leaves no temporary file", async t => {
  const {root} = await temporary(t);
  const path = join(root, "private", "progress.json");
  const store = createFileProgressStore({path, runId: "r", writesEnabled: true});
  const first = migrateLegacyRecovery({run_id: "r", attempts: 0}, {runId: "r", sessionId: "same-request"});
  first.sent = 104;
  await store.save(first);
  assert.deepEqual(await store.load(), first);
  const second = {...first, sent: 159};
  await store.save(second);
  assert.deepEqual(await store.load(), second);
  assert.equal((await fs.stat(path)).mode & 0o777, 0o600);
  assert.deepEqual(await fs.readdir(join(root, "private")), ["progress.json"]);
});

test("an append-only diagnostic journal retains events across store reloads",async t=>{
  const {root}=await temporary(t);
  const path=join(root,"private","progress.json");
  const first=createFileProgressStore({path,runId:"r",writesEnabled:true});
  const event={run_id:"r",session_id:"s",sequence:1,at_ms:1000,stage:"observe",
    user_id:"u1",outcome:"deferred",elapsed_ms:2500,reason:"conversation_identity_not_ready"};
  await first.appendEvent(event);
  await createFileProgressStore({path,runId:"r",writesEnabled:true}).appendEvent({
    ...event,sequence:2,at_ms:2000,stage:"control",outcome:"paused",elapsed_ms:0});
  const journal=path+".events.jsonl";
  const rows=(await fs.readFile(journal,"utf8")).trim().split("\n").map(JSON.parse);
  assert.equal(rows.length,2);
  assert.deepEqual(rows[0],event);
  assert.equal(rows[1].outcome,"paused");
  assert.equal((await fs.stat(journal)).mode&0o777,0o600);
  await assert.rejects(createFileProgressStore({path,runId:"r"}).appendEvent(event),/writes_disabled/);
  await assert.rejects(first.appendEvent({...event,run_id:"another-run"}),/event_run_mismatch/);
  assert.equal((await fs.readFile(journal,"utf8")).trim().split("\n").length,2);
});

test("new progress filename cannot silently bypass legacy recovery attempts", async t => {
  const {root} = await temporary(t);
  const legacy = {run_id: "r", attempts: 2, result: "failed", at_ms: 2000, reason: "search_failed"};
  const legacyPath = join(root, "recovery-progress-r.json");
  await fs.writeFile(legacyPath, JSON.stringify(legacy));
  const path = join(root, "brand-new-workflow.json");
  const store = createFileProgressStore({path, runId: "r", writesEnabled: true});
  await assert.rejects(store.load(), {code: "progress_migrate_required"});
  const migrated = migrateLegacyRecovery(legacy, {runId: "r", sessionId: "same-request"});
  await store.save(migrated);
  assert.deepEqual((await store.load()).legacy_recovery, legacy);
  assert.equal((await store.load()).recovery.attempts, 2);
  assert.equal((await store.load()).diagnostic.status, "blocked");
  await assert.rejects(store.save({...migrated, recovery: {used: false, attempts: 0}}), {code: "progress_migrate_required"});
  assert.deepEqual(JSON.parse(await fs.readFile(legacyPath, "utf8")), legacy);
});

test("unknown existing progress format requires explicit migration", async t => {
  const {root} = await temporary(t);
  const path = join(root, "progress.json");
  await fs.writeFile(path, JSON.stringify({run_id: "r", attempts: 2}));
  await assert.rejects(createFileProgressStore({path, runId: "r"}).load(), {code: "progress_migrate_required"});
  await fs.writeFile(path, JSON.stringify({version: 1, run_id: "r"}));
  await assert.rejects(createFileProgressStore({path, runId: "r"}).load(), {code: "progress_migrate_required"});
});

test("invalid JSON and subprocess errors never look like durable acknowledgement", async t => {
  const paths = await temporary(t);
  await fs.writeFile(paths.scriptPath, "print('not json')\n");
  await assert.rejects(createXhsLocalLedger(paths).report({run_id: "r"}), /invalid_json/);
  await fs.writeFile(paths.scriptPath, "import sys, json\nprint(json.dumps({'ok': False, 'error': 'database_locked'}))\nsys.exit(2)\n");
  await assert.rejects(createXhsLocalLedger(paths).report({run_id: "r"}), /database_locked/);
});

test("real temporary Python ledger next/session JSON integrates with runNext", async t => {
  const {root} = await temporary(t);
  const scriptPath = fileURLToPath(new URL("./daily_followup_state.py", import.meta.url));
  const scriptDirectory = fileURLToPath(new URL("./", import.meta.url));
  const dbPath = join(root, "synthetic-integration.sqlite3");
  const setup = `import json,sys\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nimport daily_followup_state as state\nc=state.connect(Path(sys.argv[2]))\nnow=state.epoch_ms()\nc.execute("INSERT INTO historical_imports VALUES ('i1','synthetic.csv','2026-09-30',?,1,1,0,1,0)",(now,))\nc.execute("INSERT INTO historical_user_accounts (user_id,account_name,account_id,latest_in_at_ms,lead_status_from_import,deleted,source_import_ids_json,updated_at_ms) VALUES ('synthetic-u1','test','a1',?,'no_lead',0,'[\\\"i1\\\"]',?)",(now-86400000,now))\nc.commit()\nr=state.register_run(c,'all',[],{'ad':'test','natural':'test'},[],now_ms=now,require_fresh_import=False)\nc.close()\nprint(json.dumps(r))\n`;
  const result = await promisify(execFile)("python3", ["-c", setup, scriptDirectory, dbPath], {encoding: "utf8"});
  const registered = JSON.parse(result.stdout);
  const writable = createXhsLocalLedger({scriptPath, dbPath, writesEnabled: true});
  const session = await writable.beginSession({run_id: registered.run_id, session_id: "synthetic-original-request"});
  const repeated = await writable.beginSession({run_id: registered.run_id, session_id: "synthetic-original-request"});
  assert.equal(repeated.started_at_ms, session.started_at_ms);
  const ledger = createXhsLocalLedger({scriptPath, dbPath});
  const next = await ledger.next({run_id: registered.run_id, limit: 1});
  assert.equal(next.items[0].user_id, "synthetic-u1");
  assert.equal(next.actionable_items, undefined);
  const beforeRead = await fs.readFile(dbPath);
  assert.deepEqual((await ledger.next({run_id:registered.run_id,limit:1,
    exclude_user_ids:["synthetic-u1","synthetic-u1"]})).items, []);
  assert.deepEqual(await fs.readFile(dbPath), beforeRead);
  const observed = [];
  const workflow = createXhsFollowupWorkflow({ledger, run: registered, adapter: {
    observeUid: async input => {
      observed.push(input.uid);
      return {status: "observed", token: "read-only-token", coverage_complete: true, search_complete: true,
        reviews: [{identity_verified: true, panel_loaded: true, history_complete: true}]};
    },
  }});
  const read = await workflow.runNext({limit: 1});
  assert.deepEqual(observed, ["synthetic-u1"]);
  assert.equal(read.results[0].status, "read_only");
  assert.equal((await ledger.sessionStatus({session_id: "synthetic-original-request"})).delta.sent, 0);
  assert.equal((await ledger.pendingTouches()).items.length, 0);

  // A new user request may start after the previous request is explicitly
  // ended, while the same run retains its recovery and quarantine history.
  await writable.endSession({session_id: "synthetic-original-request", detail: "Synthetic request completed"});
  const oldState = migrateLegacyRecovery({run_id: registered.run_id, attempts: 2, at_ms: 1000, result: "failed"}, {
    runId: registered.run_id, sessionId: "synthetic-original-request", repairValidation: {
      result: "passed", validated_at_ms: 2000, evidence_path: join(root, "synthetic-evidence.json"),
      uid_checks: ["test-a", "test-b", "test-c"].map(user_id => ({user_id, status: "observed"})),
    },
  });
  oldState.read_failure_uids = ["previous-read-failure"];
  oldState.blocked_uids = {"previous-send-failure": "failed"};
  const progressStore = createFileProgressStore({path: join(root, "workflow.json"),
    runId: registered.run_id, writesEnabled: true});
  await progressStore.save(oldState);
  const continued = createXhsFollowupWorkflow({ledger: writable, run: registered, sendEnabled: true,
    sessionId: "synthetic-next-user-request", progressStore, adapter: {
      observeUid: async input => ({status: "observed", uid: input.uid, token: "new-review-token",
        coverage_complete: true, search_complete: true, reviews: [{account_id: "a1",
          identity_verified: true, panel_loaded: true, history_complete: true}]}),
    }});
  const nextSession = await continued.summary();
  assert.equal(nextSession.session.session_id, "synthetic-next-user-request");
  assert.equal(nextSession.session.state, "active");
  assert.equal(nextSession.state.recovery.attempts, 2);
  assert.equal(nextSession.state.recovery.used, true);
  assert.deepEqual(nextSession.state.read_failure_uids, ["previous-read-failure"]);
  assert.deepEqual(nextSession.state.blocked_uids, {"previous-send-failure": "failed"});
  assert.equal(nextSession.state.session_history[0].session_id, "synthetic-original-request");
  assert.equal(nextSession.since_start.sent_this_run, 0);
  assert.equal((await continued.runNext({limit: 1})).results[0].status, "review_required");
  assert.equal((await continued.summary()).state.recovery.attempts, 2);
  assert.equal((await ledger.pendingTouches()).items.length, 0);
});

test("completed summary reads the last real Python session for old, new and absent request/progress IDs", async t => {
  const {root} = await temporary(t);
  const scriptPath = fileURLToPath(new URL("./daily_followup_state.py", import.meta.url));
  const scriptDirectory = fileURLToPath(new URL("./", import.meta.url));
  const dbPath = join(root, "completed.sqlite3");
  const setup = `import json,sys\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nimport daily_followup_state as s\nc=s.connect(Path(sys.argv[2]))\nr=s.register_run(c,'all',[],{'ad':'synthetic','natural':'synthetic'},[],now_ms=1000,require_fresh_import=False)\ns.begin_session(c,r['run_id'],'old-request',now_ms=2000)\ns.end_session(c,'old-request',now_ms=3000)\ns.begin_session(c,r['run_id'],'last-request',now_ms=4000)\ns.end_session(c,'last-request',now_ms=5000)\nc.execute(\"UPDATE daily_runs SET state='completed' WHERE run_id=?\",(r['run_id'],))\nc.commit()\nc.close()\nprint(json.dumps(r))\n`;
  const result = await promisify(execFile)("python3", ["-c", setup, scriptDirectory, dbPath], {encoding:"utf8"});
  const run = JSON.parse(result.stdout);
  const ledger = createXhsLocalLedger({scriptPath, dbPath});
  for (const runState of ["completed", "completed_with_exceptions"]) {
    await promisify(execFile)("python3", ["-c",
      "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('UPDATE daily_runs SET state=? WHERE run_id=?',(sys.argv[2],sys.argv[3])); c.commit(); c.close()",
      dbPath,runState,run.run_id]);
    const before = await fs.readFile(dbPath);
    for (const savedId of [null, "old-request", "last-request"]) {
      const progressPath = join(root, `progress-${runState}-${savedId}.json`);
      if (savedId !== null) {
        const saved = migrateLegacyRecovery({run_id:run.run_id,attempts:0}, {runId:run.run_id,sessionId:savedId});
        await createFileProgressStore({path:progressPath,runId:run.run_id,writesEnabled:true}).save(saved);
      }
      const previousProgress = savedId === null ? null : await fs.readFile(progressPath);
      for (const requestId of [undefined, "old-request", "new-request"]) {
        for (const sendEnabled of [false,true]) {
          await t.test(`${runState} progress ${savedId} request ${requestId} enabled ${sendEnabled}`, async () => {
            const flow = createXhsFollowupWorkflow({ledger,run,sessionId:requestId,sendEnabled,
              progressStore:createFileProgressStore({path:progressPath,runId:run.run_id}),
              adapter:{observeUid:async()=>assert.fail("completed summary touched browser")}});
            flow.requestPause();
            const summary = await flow.summary();
            assert.equal(summary.session.session_id,"last-request");
            assert.equal(summary.session.state,"closed");
            assert.equal(summary.since_start.sent_this_run,0);
            assert.equal(summary.state.run_state,runState);
          });
        }
      }
      if (previousProgress) assert.deepEqual(await fs.readFile(progressPath),previousProgress);
      else await assert.rejects(fs.stat(progressPath),{code:"ENOENT"});
    }
    assert.deepEqual(await fs.readFile(dbPath),before);
  }
  // A completed run that never had a session returns null only for the
  // ledger's explicit session_not_found response; it cannot create a session.
  await promisify(execFile)("python3", ["-c",
    "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('DELETE FROM daily_execution_sessions'); c.commit(); c.close()",dbPath]);
  const beforeMissing = await fs.readFile(dbPath);
  const flow = createXhsFollowupWorkflow({ledger,run,sessionId:"new-after-completion",sendEnabled:true,
    progressStore:createFileProgressStore({path:join(root,"absent-progress.json"),runId:run.run_id}),
    adapter:{observeUid:async()=>assert.fail("sessionless summary touched browser")}});
  assert.equal((await flow.summary()).session,null);
  assert.deepEqual(await fs.readFile(dbPath),beforeMissing);
});
