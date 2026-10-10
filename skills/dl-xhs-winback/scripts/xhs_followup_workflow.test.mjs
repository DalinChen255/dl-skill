import test from "node:test";
import assert from "node:assert/strict";
import {createXhsFollowupWorkflow, decisionsForNoCustomerContent, migrateLegacyRecovery} from "./xhs_followup_workflow.mjs";

const run = {run_id: "test-run", scope_mode: "all", account_ids: []};
const item = (uid = "u1", extra = {}) => ({user_id: uid, retry_stage: "main", account_ids: ["a1"], ...extra});
const decision = (extra = {}) => ({account_id: "a1", contact_shared: false, stop_contact: false, evidence: "", ...extra});
const makeReview = (account = "a1", extra = {}) => ({user_id: "u1", account_id: account,
  identity_verified: true, panel_loaded: true, history_complete: true,
  history_from_start_complete: true, needs_visual_review: false, label: "none",
  messages: [{id: "m1", side: "right", type: "TEXT", text: "我方此前消息"}], ...extra});
const observation = (extra = {}) => ({status: "observed", token: "token1", uid: "u1",
  coverage_complete: true, search_complete: true, selected_account_id: "a1",
  discovered_account_ids: ["a1"], reviews: [makeReview()], ...extra});

const batchObservation = (uid, text = "我们做建材", time = 1000) => observation({uid,
  reviews: [makeReview("a1", {user_id: uid, observed_at_ms: time, history_boundary_message_id: "m1",
    messages: [{id: "m1", timestamp: 1, side: "left", type: "TEXT", text}], outbound_text_hashes: []})]});

test("a detached browser pauses globally without consuming any UID retry, recovery only reads",async()=>{
  let attached=false;
  const f=fixture({observe:()=>{
    if(!attached) throw new Error("Debugger unattached");
    return observation();
  }});
  const flow=f.workflow();
  assert.equal((await flow.process(item())).reason,"browser_connection_unavailable");
  assert.equal((await flow.process(item("u2"))).reason,"browser_connection_unavailable");
  assert.equal((await flow.summary()).state.read_failure_uids.length,0);
  assert.equal(f.calls.filter(c=>c[0]==="defer").length,0);
  assert.equal(f.calls.filter(c=>c[0]==="observe").length,1);
  assert.equal((await flow.verifyConnection(item())).reason,"browser_connection_unavailable");
  attached=true;
  await flow.pause();
  assert.equal((await flow.verifyConnection(item())).status,"connection_verified");
  assert.equal((await flow.process(item())).status,"paused");
  assert.equal(f.calls.filter(c=>["prepare","click"].includes(c[0])).length,0);
});

test("missing local review evidence blocks without reporting a browser failure",async()=>{
  const f=fixture();
  const flow=f.workflow({batchMode:true,sendEnabled:false});
  const saved=(await flow.summary()).state;
  saved.review_cache.u1={reference:"missing-evidence"};
  const broken={load:async()=>saved,save:async()=>{},evidence:{get:async()=>{throw new Error("ENOENT");}}};
  const restarted=f.workflow({batchMode:true,sendEnabled:false,progressStore:broken});
  assert.equal((await restarted.process(item())).reason,"local_review_evidence_unavailable");
  assert.equal(f.calls.filter(c=>["observe","defer","click"].includes(c[0])).length,0);
  assert.equal((await restarted.summary()).state.read_failures.length,0);
});

function fixture({observe = () => observation(), receipt = () => ({status: "sent"}), stage,
  saved = null, ledgerExtras = {}, adapterExtras = {}} = {}) {
  const calls = [], pending = [], counters = {sent_this_run: 104, failed_this_run: 7, uncertain_this_run: 0};
  let current, seq = 0, stored = saved;
  const store = {load: async () => structuredClone(stored), save: async value => {stored = structuredClone(value);}};
  const adapter = {
    observeUid: async input => {calls.push(["observe", input]); current = await observe(input); return current;},
    confirmBusinessReview: ({token, decisions}) => {
      calls.push(["confirm", decisions]);
      assert.equal(token, current.token);
      assert.equal(decisions.length, current.reviews.length);
      return current.reviews.map(r => ({...r, business_reviewed: true,
        protective_lead_signal: decisions.find(d => d.account_id === r.account_id)?.contact_shared,
        negative_status: decisions.find(d => d.account_id === r.account_id)?.stop_contact ? "blocked" : "safe"}));
    },
    revalidateAndStage: async payload => {calls.push(["stage", payload]); return stage || {status: "staged", stageToken: "stage1"};},
    clickAndVerify: async payload => {calls.push(["click", payload]); return receipt(payload);},
    selectReviewedAccount: async ({accountId}) => {
      calls.push(["select", accountId]); current = {...current, selected_account_id: accountId}; return current;
    },
    ensurePage: async () => {calls.push(["ensure"]); return {ok: true};},
    ...adapterExtras,
  };
  const ledger = {
    pendingTouches: async () => {calls.push(["pending"]); return {items: structuredClone(pending)};},
    report: async () => structuredClone(counters),
    prepareTouch: async payload => {
      calls.push(["prepare", payload]);
      if (payload.reviews.some(r => ["lead", "no_intent"].includes(r.label) || r.protective_lead_signal)) {
        return {action: "skip", reason: "protected"};
      }
      const touch = {touch_id: `t${++seq}`, state: "reserved"}; pending.push(touch);
      return {action: "send", touch_id: touch.touch_id, account_id: payload.selected_account_id, message: "原文"};
    },
    recordBrowserDefer: async payload => {calls.push(["defer", payload]); return {action: "defer"};},
    markClicked: async ({touch_id}) => {
      calls.push(["mark", touch_id]); pending.find(t => t.touch_id === touch_id).state = "clicked";
      return {ok: true, state: "clicked", touch_id};
    },
    finishTouch: async payload => {
      calls.push(["finish", payload]); const index = pending.findIndex(t => t.touch_id === payload.touch_id);
      assert.equal(pending[index].state, "clicked"); pending.splice(index, 1); counters[payload.status + "_this_run"]++;
      return {ok: true};
    },
    ...ledgerExtras,
  };
  return {calls, pending, counters, adapter, ledger, store, stored: () => structuredClone(stored),
    workflow: (options = {}) => createXhsFollowupWorkflow({adapter, ledger, progressStore: store, run,
      sendEnabled: true, ...options})};
}

test("default mode observes without any ledger or send mutation", async () => {
  const f = fixture();
  const flow = f.workflow({sendEnabled: undefined});
  assert.equal((await flow.process(item())).status, "read_only");
  assert.deepEqual(f.calls.map(c => c[0]), ["observe"]);
  assert.equal((await flow.summary()).mode, "read_only");
  await assert.rejects(flow.continueReview([decision()]), /send_disabled/);
});

test("batch queues several complex reviews while simple customers continue in one call", async () => {
  const f = fixture({observe: ({uid}) => batchObservation(uid, uid === "u2" ? "你好" : "我们做建材"),
    ledgerExtras: {recordReviewedHistory: async () => ({ok: true}), next: async () => ({items: [item(),item("u2"),item("u3")]})}});
  const flow=f.workflow({batchMode:true,now:()=>1000});
  const result=await flow.runSlice();
  assert.deepEqual(result.results.map(r=>r.status),["review_queued","processed","review_queued"]);
  assert.equal(result.pending_review_count,2);
  assert.equal(f.calls.filter(c=>c[0]==="click").length,1);
  assert.equal(f.pending.length,0);
  assert.equal(JSON.stringify(f.stored().pending_reviews).includes('"token"'),false);
});

test("approved batch survives restart and waiting over two minutes, then reobserves before sending",async()=>{
  let time=1000;
  const f=fixture({observe:({uid})=>batchObservation(uid,"我们做建材",time),
    ledgerExtras:{recordReviewedHistory:async()=>({ok:true}),next:async()=>({items:[item(),item("u2")]})}});
  const first=f.workflow({batchMode:true,now:()=>time});
  await first.runSlice();
  const packets=await first.reviewPackets();
  time=301000;
  const restarted=f.workflow({batchMode:true,now:()=>time});
  const approved=await restarted.submitReviews(packets.map(p=>({uid:p.uid,evidence_id:p.evidence_id,decisions:[decision()]})));
  assert.equal(approved.sends,0);
  assert.equal(f.calls.filter(c=>c[0]==="click").length,0);
  const result=await restarted.runSlice({limit:1});
  assert.equal(result.results[0].status,"processed");
  assert.equal(f.calls.filter(c=>c[0]==="observe").length,3);
  assert.equal(f.calls.filter(c=>c[0]==="defer").length,0);
  assert.equal(f.calls.filter(c=>c[0]==="click").length,1);
});

test("new customer words after queued approval go back to review instead of using an old clearance",async()=>{
  let text="我们做建材";
  const f=fixture({observe:({uid})=>batchObservation(uid,text),
    ledgerExtras:{recordReviewedHistory:async()=>({ok:true}),next:async()=>({items:[item()]})}});
  const flow=f.workflow({batchMode:true,now:()=>1000});
  await flow.runSlice();
  const [p]=await flow.reviewPackets();
  await flow.submitReviews([{uid:p.uid,evidence_id:p.evidence_id,decisions:[decision()]}]);
  text="不要再发";
  const result=await flow.runSlice();
  assert.equal(result.results[0].status,"review_queued");
  assert.equal(f.calls.some(c=>c[0]==="click"),false);
});

test("read-only batch classifies and queues without initializing sends or mutating the ledger",async()=>{
  const f=fixture({observe:({uid})=>batchObservation(uid,uid==="u1"?"你好":"建材"),
    ledgerExtras:{recordReviewedHistory:async()=>{throw new Error("must not write");},next:async()=>({items:[item(),item("u2")]})}});
  const flow=f.workflow({batchMode:true,sendEnabled:false,now:()=>1000});
  assert.deepEqual((await flow.runSlice()).results.map(r=>r.status),["ready_read_only","review_queued"]);
  assert.equal(f.calls.some(c=>["prepare","click","finish","defer"].includes(c[0])),false);
  assert.equal((await flow.runSlice()).results.length,0);
});

test("pause at a UID boundary is durable and restarting does not resume automatically",async()=>{
  let flow;
  const f=fixture({observe:({uid})=>{flow.requestPause();return batchObservation(uid,"你好");},
    ledgerExtras:{recordReviewedHistory:async()=>({ok:true}),next:async()=>({items:[item(),item("u2")]})}});
  flow=f.workflow({batchMode:true,now:()=>1000});
  assert.equal((await flow.runSlice()).paused,true);
  assert.equal(f.calls.some(c=>c[0]==="click"),false);
  const again=f.workflow({batchMode:true,now:()=>1000});
  assert.equal((await again.runSlice()).status,"paused");
  assert.equal(f.calls.filter(c=>c[0]==="observe").length,1);
});

test("review backpressure bounds collected material, and slice time stops before another UID",async()=>{
  let time=1000;
  const f=fixture({observe:({uid})=>{time+=20;return batchObservation(uid,"建材",time);},
    ledgerExtras:{recordReviewedHistory:async()=>({ok:true}),next:async()=>({items:[item(),item("u2"),item("u3")]})}});
  const flow=f.workflow({batchMode:true,now:()=>time});
  assert.equal((await flow.runSlice({maxReviews:1})).results.length,1);
  assert.equal((await flow.runSlice({maxDurationMs:1})).results.length,1);
  assert.equal(f.calls.some(c=>c[0]==="click"),false);
});

test("approved jobs execute before fresh admission even while the review queue is full", async () => {
  const f = fixture({observe: ({uid}) => batchObservation(uid),
    ledgerExtras: {recordReviewedHistory: async () => ({ok: true}),
      next: async () => ({items: [item(), item("u2"), item("u3")]})}});
  const flow = f.workflow({batchMode: true, now: () => 1000});
  await flow.runSlice({limit: 2});
  const [packet] = await flow.reviewPackets();
  await flow.submitReviews([{uid: packet.uid, evidence_id: packet.evidence_id, decisions: [decision()]}]);
  const result = await flow.runSlice({maxReviews: 1});
  assert.deepEqual(result.results.map(r => [r.user_id, r.status]), [["u1", "processed"]]);
  assert.equal(f.counters.sent_this_run, 105);
  assert.equal(f.pending.length, 0);
  assert.equal(f.calls.some(c => c[0] === "observe" && c[1].uid === "u3"), false);
});

test("held reviews preserve evidence across restart and free fresh review admission", async () => {
  const f = fixture({observe: ({uid}) => batchObservation(uid),
    ledgerExtras: {recordReviewedHistory: async () => ({ok: true}),
      next: async () => ({items: [item(), item("u2")]})}});
  let flow = f.workflow({batchMode: true, now: () => 1000});
  await flow.runSlice({maxReviews: 1});
  const [packet] = await flow.reviewPackets();
  assert.equal(typeof flow.holdReviews, "function");
  await flow.holdReviews([{uid: packet.uid, evidence_id: packet.evidence_id, reason: "Needs image interpretation"}]);
  flow = f.workflow({batchMode: true, now: () => 2000});
  assert.deepEqual(await flow.reviewPackets(), []);
  assert.deepEqual(await flow.reviewPackets({queue: "held"}), [packet]);
  const result = await flow.runSlice({maxReviews: 1});
  assert.deepEqual(result.results.map(r => r.user_id), ["u2"]);
  assert.equal(result.pending_review_count, 1);
  assert.equal(result.held_review_count, 1);
  assert.equal((await flow.process(item())).status, "review_held");
  assert.equal(f.counters.sent_this_run, 104);
  assert.equal(f.calls.filter(c => c[0] === "observe" && c[1].uid === "u1").length, 1);
});

test("held review pagination can reach later evidence without discarding unresolved earlier packets", async () => {
  const f = fixture({observe: ({uid}) => batchObservation(uid),
    ledgerExtras: {recordReviewedHistory: async () => ({ok: true}),
      next: async input => ({items: Array.from({length: 35}, (_, i) => item(`u${i + 1}`))
        .filter(i => !(input.exclude_user_ids || []).includes(i.user_id))})}});
  const flow = f.workflow({batchMode: true, now: () => 1000});
  for (let i = 0; i < 2; i++) {
    await flow.runSlice({maxReviews: 30});
    const packets = await flow.reviewPackets({limit: 30});
    await flow.holdReviews(packets.map(p => ({uid: p.uid, evidence_id: p.evidence_id, reason: "Unresolved"})));
  }
  const later = await flow.reviewPackets({queue: "held", offset: 30, limit: 5});
  assert.deepEqual(later.map(p => p.uid), ["u31", "u32", "u33", "u34", "u35"]);
  assert.equal(Object.keys((await flow.summary()).state.held_reviews).length, 35);
  for (const offset of [-1, 0.5, "0", NaN]) {
    await assert.rejects(flow.reviewPackets({queue: "held", offset}), /invalid_review_packet_selection/);
  }
  assert.equal(f.counters.sent_this_run, 104);
});

test("holding a batch validates all evidence before moving any review", async () => {
  const f = fixture({observe: ({uid}) => batchObservation(uid),
    ledgerExtras: {recordReviewedHistory: async () => ({ok: true}),
      next: async () => ({items: [item(), item("u2")]})}});
  const flow = f.workflow({batchMode: true, now: () => 1000});
  await flow.runSlice();
  const packets = await flow.reviewPackets();
  assert.equal(typeof flow.holdReviews, "function");
  await assert.rejects(flow.holdReviews(packets.map((p, i) => ({uid: p.uid,
    evidence_id: i ? "stale" : p.evidence_id, reason: "Unresolved"}))), /evidence/);
  assert.deepEqual(await flow.reviewPackets(), packets);
  assert.deepEqual(await flow.reviewPackets({queue: "held"}), []);
  await assert.rejects(flow.holdReviews([{uid: packets[0].uid, evidence_id: packets[0].evidence_id,
    reason: " "}]), /reason/);
});

test("held evidence can be approved without resuming and then is freshly checked", async () => {
  let time = 1000;
  const f = fixture({observe: ({uid}) => batchObservation(uid, "我们做建材", time),
    ledgerExtras: {recordReviewedHistory: async () => ({ok: true}), next: async () => ({items: [item()]})}});
  const flow = f.workflow({batchMode: true, now: () => time});
  await flow.runSlice();
  const [packet] = await flow.reviewPackets();
  assert.equal(typeof flow.holdReviews, "function");
  await flow.holdReviews([{uid: packet.uid, evidence_id: packet.evidence_id, reason: "Needs further reading"}]);
  await flow.pause();
  time = 301000;
  const approval = await flow.submitReviews([{uid: packet.uid, evidence_id: packet.evidence_id, decisions: [decision()]}]);
  assert.equal(approval.sends, 0);
  assert.deepEqual(await flow.reviewPackets({queue: "held"}), []);
  assert.equal((await flow.runSlice()).status, "paused");
  assert.equal(f.counters.sent_this_run, 104);
  await flow.resume();
  assert.equal((await flow.runSlice({limit: 1})).results[0].status, "processed");
  assert.equal(f.calls.filter(c => c[0] === "observe").length, 2);
  assert.equal(f.counters.sent_this_run, 105);
});

test("approval and bounded execution share one handoff but still reobserve aged evidence", async () => {
  let time = 1000;
  const f = fixture({observe: ({uid}) => batchObservation(uid, "我们做建材", time),
    ledgerExtras: {recordReviewedHistory: async () => ({ok: true}), next: async () => ({items: [item()]})}});
  let locks = 0;
  const store = {...f.store, withExclusive: async operation => {locks++; return operation();}};
  const flow = f.workflow({batchMode: true, now: () => time, progressStore: store});
  await flow.runSlice();
  const [packet] = await flow.reviewPackets();
  time = 301000;
  const beforeLocks = locks;
  assert.equal(typeof flow.submitReviewsAndRunSlice, "function");
  const result = await flow.submitReviewsAndRunSlice([{uid: packet.uid,
    evidence_id: packet.evidence_id, decisions: [decision()]}], {limit: 1});
  assert.equal(locks - beforeLocks, 1);
  assert.deepEqual(result.approval.accepted, ["u1"]);
  assert.equal(result.approval.sends, 0);
  assert.equal(result.slice.results[0].status, "processed");
  assert.equal(f.calls.filter(c => c[0] === "observe").length, 2);
  assert.equal(f.counters.sent_this_run, 105);
  assert.equal(f.pending.length, 0);
});

test("combined approval preserves pause and validates slice options before approvals", async () => {
  const f = fixture({observe: ({uid}) => batchObservation(uid),
    ledgerExtras: {recordReviewedHistory: async () => ({ok: true}), next: async () => ({items: [item()]})}});
  const flow = f.workflow({batchMode: true, now: () => 1000});
  await flow.runSlice();
  const [packet] = await flow.reviewPackets();
  const approvals = [{uid: packet.uid, evidence_id: packet.evidence_id, decisions: [decision()]}];
  assert.equal(typeof flow.submitReviewsAndRunSlice, "function");
  await assert.rejects(flow.submitReviewsAndRunSlice(approvals, {maxDurationMs: 0}), /invalid_slice_limits/);
  assert.equal((await flow.reviewPackets()).length, 1);
  await flow.pause();
  const result = await flow.submitReviewsAndRunSlice(approvals);
  assert.equal(result.slice.status, "paused");
  assert.equal(f.counters.sent_this_run, 104);
  assert.equal(f.calls.filter(c => c[0] === "observe").length, 1);
  assert.equal(f.calls.some(c => c[0] === "prepare"), false);
  const restarted = f.workflow({batchMode: true, now: () => 1000});
  assert.equal((await restarted.runSlice()).status, "paused");
});

test("combined approval requeues new customer words rather than clicking stale approval", async () => {
  let text = "我们做建材";
  const f = fixture({observe: ({uid}) => batchObservation(uid, text),
    ledgerExtras: {recordReviewedHistory: async () => ({ok: true}), next: async () => ({items: [item()]})}});
  const flow = f.workflow({batchMode: true, now: () => 1000});
  await flow.runSlice();
  const [packet] = await flow.reviewPackets();
  text = "不要再联系我";
  assert.equal(typeof flow.submitReviewsAndRunSlice, "function");
  const result = await flow.submitReviewsAndRunSlice([{uid: packet.uid,
    evidence_id: packet.evidence_id, decisions: [decision()]}]);
  assert.equal(result.slice.results[0].status, "review_queued");
  assert.equal(result.slice.pending_review_count, 1);
  assert.equal(f.counters.sent_this_run, 104);
  assert.equal(f.calls.some(c => c[0] === "prepare"), false);
});

test("a pause requested before the next slice remains paused after restart", async () => {
  const f = fixture({observe: ({uid}) => batchObservation(uid, "你好"),
    ledgerExtras: {recordReviewedHistory: async () => ({ok: true}), next: async () => ({items: [item()]})}});
  const flow = f.workflow({batchMode: true, now: () => 1000});
  flow.requestPause();
  assert.equal((await flow.runSlice()).status, "paused");
  const restarted = f.workflow({batchMode: true, now: () => 1000});
  assert.equal((await restarted.runSlice()).status, "paused");
  assert.equal(f.counters.sent_this_run, 104);
  assert.equal(f.calls.some(c => c[0] === "observe"), false);
});

test("pause during approval persists even when unfinished ledger work returns early", async () => {
  let flow;
  const f = fixture({observe: ({uid}) => batchObservation(uid),
    ledgerExtras: {recordReviewedHistory: async () => {
      flow.requestPause();
      f.pending.push({touch_id: "external", state: "reserved"});
      return {ok: true};
    }, next: async () => ({items: [item()]})}});
  flow = f.workflow({batchMode: true, now: () => 1000});
  await flow.runSlice();
  const [packet] = await flow.reviewPackets();
  const result = await flow.submitReviewsAndRunSlice([{uid: packet.uid,
    evidence_id: packet.evidence_id, decisions: [decision()]}]);
  assert.equal(result.slice.reason, "unfinished_touch_requires_reconciliation");
  assert.equal(f.stored().control.paused, true);
  f.pending.length = 0; // Independent evidence-backed reconciliation.
  const restarted = f.workflow({batchMode: true, now: () => 1000});
  assert.equal((await restarted.runSlice()).status, "paused");
  assert.equal(f.counters.sent_this_run, 104);
  assert.equal(f.calls.some(c => c[0] === "prepare"), false);
});

test("combined read-only approval never prepares a message or writes business ledger history", async () => {
  const f = fixture({observe: ({uid}) => batchObservation(uid),
    ledgerExtras: {recordReviewedHistory: async () => {throw new Error("read-only ledger mutation");},
      next: async () => ({items: [item()]})}});
  const flow = f.workflow({batchMode: true, sendEnabled: false, now: () => 1000});
  await flow.runSlice();
  const [packet] = await flow.reviewPackets();
  const result = await flow.submitReviewsAndRunSlice([{uid: packet.uid,
    evidence_id: packet.evidence_id, decisions: [decision()]}]);
  assert.equal(result.slice.mode, "read_only");
  assert.equal(result.slice.results[0].status, "ready_read_only");
  assert.equal(f.counters.sent_this_run, 104);
  assert.equal(f.calls.some(c => ["prepare", "mark", "click", "finish"].includes(c[0])), false);
});

test("bounded next excludes local waiting UIDs before selecting the next queue window", async () => {
  let excluded;
  const f = fixture({observe: ({uid}) => batchObservation(uid),
    ledgerExtras: {recordReviewedHistory: async () => ({ok: true}), next: async input => {
      excluded = input.exclude_user_ids || [];
      return {items: [item(), item("u2"), item("u3")].filter(i => !excluded.includes(i.user_id))};
    }}});
  const flow = f.workflow({batchMode: true, now: () => 1000});
  await flow.runSlice({limit: 2});
  const [packet] = await flow.reviewPackets();
  assert.equal(typeof flow.holdReviews, "function");
  await flow.holdReviews([{uid: packet.uid, evidence_id: packet.evidence_id, reason: "Unknown photo"}]);
  const result = await flow.runSlice({maxReviews: 2});
  assert.deepEqual(excluded, ["u2", "u1"]);
  assert.deepEqual(result.results.map(r => r.user_id), ["u3"]);
});

test("a proved pre-click cancellation is recorded and the next customer continues",async()=>{
  const f=fixture({observe:({uid})=>batchObservation(uid,"你好"),stage:{status:"not_staged",reason:"page_or_customer_changed"},
    ledgerExtras:{recordReviewedHistory:async()=>({ok:true}),next:async()=>({items:[item(),item("u2")]})}});
  f.ledger.cancelUnclickedTouch=async ({touch_id})=>{
    const i=f.pending.findIndex(t=>t.touch_id===touch_id);
    assert.equal(f.pending[i].state,"reserved");f.pending.splice(i,1);
    return {ok:true,touch_id,state:"failed"};
  };
  const results=(await f.workflow({batchMode:true,now:()=>1000}).runSlice()).results;
  assert.equal(results.length,2);
  assert.equal(results[0].results[0].reason,"cancelled_before_click");
  assert.equal(f.pending.length,0);
  assert.equal(f.calls.some(c=>c[0]==="click"),false);
});

test("an uncertain filled draft cannot use automatic pre-click cancellation",async()=>{
  const f=fixture({observe:({uid})=>batchObservation(uid,"你好"),stage:{status:"uncertain",reason:"auto_draft_cleanup_unverified"},
    ledgerExtras:{recordReviewedHistory:async()=>({ok:true}),cancelUnclickedTouch:async()=>{throw new Error("must not cancel");},
      next:async()=>({items:[item(),item("u2")]})}});
  const result=await f.workflow({batchMode:true,now:()=>1000}).runSlice();
  assert.equal(result.results[0].reason,"reservation_requires_reconciliation");
  assert.equal(result.results.length,1);
  assert.equal(f.pending.length,1);
});

test("execution requires durable progress, which read-only diagnostics do not", () => {
  const f = fixture();
  assert.throws(() => createXhsFollowupWorkflow({adapter: f.adapter, ledger: f.ledger, run, sendEnabled: true}),
    /durable_progress/);
  assert.doesNotThrow(() => createXhsFollowupWorkflow({adapter: f.adapter, run}));
});

test("cached history boundary is valid and can be reviewed without rereading from start", async () => {
  const f = fixture({observe: () => observation({reviews: [makeReview("a1", {
    history_from_start_complete: false, history_reached_prior_boundary: true,
    messages: [{side: "left", type: "TEXT", text: "我想了解课程"}],
  })]})});
  const flow = f.workflow();
  assert.equal((await flow.process(item())).status, "review_required");
  assert.equal((await flow.continueReview([decision()])).results[0].status, "sent");
  assert.equal(f.calls.filter(c => c[0] === "observe").length, 1);
  assert.equal(f.calls.filter(c => c[0] === "defer").length, 0);
});

test("customer words including a contact hint always require semantic review", async () => {
  const f = fixture({observe: () => observation({reviews: [makeReview("a1", {
    messages: [{side: "left", type: "TEXT", text: "你先给我微信，我准备加你"}], contact_shared_hint: true,
  })]})});
  const flow = f.workflow();
  assert.equal((await flow.process(item())).status, "review_required");
  assert.equal(f.calls.some(c => c[0] === "prepare"), false);
  await flow.continueReview([decision()]);
  assert.equal(f.counters.sent_this_run, 105);
});

test("visual content and cached no-left history do not get automatic decisions", () => {
  assert.equal(decisionsForNoCustomerContent(observation({reviews: [makeReview("a1", {needs_visual_review: true})]})), null);
  assert.equal(decisionsForNoCustomerContent(observation({reviews: [makeReview("a1", {history_from_start_complete: false})]})), null);
  assert.deepEqual(decisionsForNoCustomerContent(observation()), [decision()]);
});

test("verified exact stop labels persist immediately even without complete history", async () => {
  const f = fixture({observe: () => observation({status: "stopped", token: "", coverage_complete: false,
    search_complete: false, reviews: [makeReview("a1", {label: "no_intent", history_complete: false})]})});
  const flow = f.workflow();
  assert.equal((await flow.process(item())).status, "skipped");
  assert.equal(f.calls.filter(c => c[0] === "prepare").length, 1);
  assert.equal(f.calls.some(c => c[0] === "click"), false);
});

test("unverified stopped result is deferred, not trusted", async () => {
  const f = fixture({observe: () => observation({status: "stopped", reviews: [makeReview("a1", {
    label: "lead", identity_verified: false,
  })]})});
  assert.equal((await f.workflow().process(item())).status, "deferred");
  assert.equal(f.calls.some(c => c[0] === "prepare"), false);
});

for (const status of ["failed", "uncertain"]) test(`${status} is finalized once and continues next UID, skips remaining accounts`, async () => {
  const f = fixture({observe: input => observation({uid: input.uid, discovered_account_ids: ["a1", "a2"],
    reviews: [makeReview(), makeReview("a2")]}), receipt: () => ({status, reason: "platform_detail"})});
  const flow = f.workflow();
  const result = await flow.runBatch([item(), item("u2")]);
  assert.equal(result.results.length, 2);
  assert.equal(f.calls.filter(c => c[0] === "click").length, 2);
  assert.equal(f.calls.filter(c => c[0] === "select").length, 0);
  assert.equal((await flow.process(item())).status, "skipped");
  assert.equal(f.calls.filter(c => c[0] === "click").length, 2);
  assert.equal(f.pending.length, 0);
  assert.deepEqual((await flow.summary()).state.read_failure_uids, []);
});

test("pending reserved/clicked block before browser operation", async () => {
  const f = fixture(); f.pending.push({touch_id: "old", state: "clicked"});
  assert.equal((await f.workflow().process(item())).reason, "unfinished_touch_requires_reconciliation");
  assert.equal(f.calls.some(c => c[0] === "observe"), false);
});

test("staging failure preserves reservation for reconciliation and never marks clicked", async () => {
  const f = fixture({stage: {status: "not_staged", reason: "user_draft_present"}});
  const flow = f.workflow();
  assert.equal((await flow.process(item())).reason, "reservation_requires_reconciliation");
  assert.equal(f.pending[0].state, "reserved");
  assert.equal((await flow.process(item("u2"))).status, "blocked");
  assert.equal(f.calls.some(c => c[0] === "mark"), false);
});

test("click marker plus not_clicked preserves unresolved record instead of pretending failure", async () => {
  const f = fixture({receipt: () => ({status: "not_clicked", reason: "draft_changed"})});
  assert.equal((await f.workflow().process(item())).reason, "click_marker_requires_reconciliation");
  assert.equal(f.pending[0].state, "clicked");
  assert.equal(f.calls.some(c => c[0] === "finish"), false);
});

test("receipt exception becomes uncertain exactly once after click marker", async () => {
  const f = fixture({receipt: () => {throw new Error("connection lost");}});
  const result = await f.workflow().process(item());
  assert.equal(result.results[0].status, "uncertain");
  assert.equal(f.counters.uncertain_this_run, 1);
  assert.equal(f.pending.length, 0);
});

test("ledger finish failure prevents any subsequent customer", async () => {
  const f = fixture({ledgerExtras: {finishTouch: async () => {throw new Error("disk full");}}});
  const flow = f.workflow();
  await assert.rejects(flow.process(item()), /disk full/);
  assert.equal((await flow.process(item("u2"))).status, "blocked");
  assert.equal(f.calls.filter(c => c[0] === "click").length, 1);
});

test("a false durable click acknowledgement cannot reach the send button", async () => {
  const f = fixture({ledgerExtras: {markClicked: async () => ({ok: false})}});
  assert.equal((await f.workflow().process(item())).reason, "mark_clicked_not_durable");
  assert.equal(f.calls.some(c => c[0] === "click"), false);
  assert.equal(f.pending[0].state, "reserved");
});

test("expired held review is deferred before creating a reservation", async () => {
  let time = 1000;
  const f = fixture({observe: () => observation({reviews: [makeReview("a1", {
    observed_at_ms: 1000, messages: [{side: "left", text: "课程是什么"}],
  })]})});
  const flow = f.workflow({now: () => time});
  assert.equal((await flow.process(item())).status, "review_required");
  time = 121001;
  assert.equal((await flow.continueReview([decision()])).reason, "account_reviews_stale_reobserve_required");
  assert.equal(f.calls.some(c => c[0] === "prepare"), false);
});

test("each UID contributes once to read-failure threshold; exact stopped resets it", async () => {
  const f = fixture({observe: input => input.uid === "good" ? observation({status: "stopped",
    reviews: [makeReview("a1", {label: "lead"})]}) : {status: "deferred", reason: "search_loading", reviews: []}});
  const flow = f.workflow();
  await flow.process(item()); await flow.process(item()); await flow.process(item("u2"));
  assert.equal((await flow.summary()).state.read_failure_uids.length, 2);
  await flow.process(item("good"));
  assert.deepEqual((await flow.summary()).state.read_failure_uids, []);
  await flow.runBatch([item("u3"), item("u4"), item("u5"), item("u6")]);
  assert.equal((await flow.summary()).state.diagnostic.status, "recovery_required");
  assert.equal(f.calls.some(c => c[0] === "observe" && c[1].uid === "u6"), false);
});

test("recovery attempt survives restart; successful read-only recovery never sends", async () => {
  let failing = true;
  const f = fixture({observe: () => failing ? {status: "deferred", reason: "search_loading", reviews: []} : observation()});
  let flow = f.workflow();
  await flow.runBatch([item(), item("u2"), item("u3")]);
  failing = false;
  assert.equal((await flow.recover({item: item("u3", {retry_stage: "slow"})})).status, "recovered");
  assert.equal(f.calls.some(c => c[0] === "click"), false);
  flow = f.workflow(); failing = true;
  await flow.runBatch([item("u4"), item("u5"), item("u6")]);
  assert.equal((await flow.summary()).state.diagnostic.reason, "repeated_read_failures_after_recovery");
  assert.equal((await flow.recover({item: item("u6", {retry_stage: "slow"})})).reason, "recovery_not_allowed");
});

test("baseline counts persist across coordinator instances and metrics include stages", async () => {
  let time = 1000;
  const f = fixture();
  let flow = f.workflow({now: () => ++time});
  await flow.process(item());
  flow = f.workflow({now: () => ++time});
  await flow.process(item("u2"));
  const summary = await flow.summary();
  assert.equal(summary.report.sent_this_run, 106);
  assert.equal(summary.since_start.sent_this_run, 2);
  assert.equal(summary.state.metrics.observe.count, 2);
  assert.ok(summary.state.metrics.click_and_verify.total_ms > 0);
  assert.equal(JSON.stringify(f.stored()).includes("token1"), false);
});

test("two successful accounts reuse observed evidence and only switch the reviewed account", async () => {
  const f = fixture({observe: () => observation({reviews: [makeReview(), makeReview("a2")],
    discovered_account_ids: ["a1", "a2"]})});
  const result = await f.workflow().process(item());
  assert.equal(result.results.length, 2);
  assert.equal(f.calls.filter(c => c[0] === "observe").length, 1);
  assert.equal(f.calls.filter(c => c[0] === "select").length, 1);
});

test("one executor cannot interleave two CUA operations", async () => {
  let release;
  const f = fixture({observe: () => new Promise(resolve => {release = resolve;})});
  const flow = f.workflow({sendEnabled: false});
  const first = flow.process(item());
  await assert.rejects(flow.process(item("u2")), /already_running/);
  while (!release) await new Promise(resolve => setImmediate(resolve));
  release(observation());
  await first;
});

test("live resolution releases pending block while unrelated blocking diagnoses stay blocked", async () => {
  const f = fixture(); f.pending.push({touch_id: "old", state: "clicked"});
  const flow = f.workflow();
  assert.equal((await flow.process(item())).status, "blocked");
  f.pending.length = 0; // Simulates independent, evidence-backed resolve-touch.
  assert.equal((await flow.process(item())).status, "processed");
  assert.equal(f.calls.filter(c => c[0] === "observe").length, 1);
  const f2 = fixture({observe: () => ({status: "deferred", reason: "user_draft_present", reviews: []})});
  const flow2 = f2.workflow();
  await flow2.process(item());
  assert.equal((await flow2.process(item("u2"))).reason, "user_draft_present");
  assert.equal(f2.calls.filter(c => c[0] === "observe").length, 1);
  f2.pending.push({touch_id: "external", state: "reserved"});
  assert.equal((await flow2.process(item("u3"))).reason, "unfinished_touch_requires_reconciliation");
  f2.pending.length = 0;
  assert.equal((await flow2.process(item("u4"))).reason, "user_draft_present");
  assert.equal(f2.calls.filter(c => c[0] === "observe").length, 1);
});

test("search diagnostics retain UID, stage, input and counts in durable progress and summary", async () => {
  const detail = {uid: "u1", input_value: "u1", raw_count: 2, visible_count: 0, state: "results_not_visible"};
  const f = fixture({observe: () => ({status: "deferred", reason: "search_result_not_confirmed",
    reviews: [], search_diagnostics: [detail]})});
  const flow = f.workflow();
  assert.equal((await flow.process(item())).failure_stage, "search");
  const summary = await flow.summary();
  assert.equal(summary.failures[0].user_id, "u1");
  assert.equal(summary.failures[0].failure_stage, "search");
  assert.deepEqual(summary.failures[0].search_diagnostics, [detail]);
  assert.deepEqual(f.stored().read_failures[0].search_diagnostics, [detail]);
  assert.match(f.calls.find(c => c[0] === "defer")[1].evidence, /results_not_visible/);
});

test("ledger session survives lost local progress and bounded next replaces full queue reload", async () => {
  const f = fixture();
  let session;
  const makeSession = () => ({ok: true, session_id: "request-stable", run_id: run.run_id, state: "active",
    baseline: {sent: 104, failed: 7, uncertain: 0}, delta: {
      sent: f.counters.sent_this_run - 104, failed: f.counters.failed_this_run - 7, uncertain: f.counters.uncertain_this_run,
    }});
  f.ledger.beginSession = async payload => {
    f.calls.push(["begin_session", payload]); session ||= makeSession(); return session;
  };
  f.ledger.sessionStatus = async () => makeSession();
  f.ledger.next = async input => {f.calls.push(["next", input]); return {items: [item("u2")]};};
  let flow = f.workflow({sessionId: "request-stable"});
  await flow.process(item());
  flow = f.workflow({sessionId: "request-stable", progressStore: {load: async () => null, save: async () => {}}});
  await flow.runNext({limit: 1});
  const summary = await flow.summary();
  assert.equal(summary.since_start.sent_this_run, 2);
  assert.equal(summary.session.session_id, "request-stable");
  assert.equal(f.calls.filter(c => c[0] === "begin_session").length, 2);
  assert.deepEqual(f.calls.find(c => c[0] === "next")[1], {run_id: run.run_id, limit: 1});
});

test("explicit false finish acknowledgement preserves pending record and blocks next customer", async () => {
  const f = fixture({ledgerExtras: {finishTouch: async () => ({ok: false})}});
  const flow = f.workflow();
  assert.equal((await flow.process(item())).reason, "finish_not_durable");
  assert.equal(f.pending.length, 1);
  assert.equal((await flow.process(item("u2"))).reason, "unfinished_touch_requires_reconciliation");
  assert.equal(f.calls.filter(c => c[0] === "click").length, 1);
});

test("repair validation retains the prior two attempts and does not grant another recovery", async () => {
  const legacy = {run_id: run.run_id, at_ms: 2000, attempts: 2, result: "failed", reason: "old_search_failure"};
  const evidence = {result: "passed", validated_at_ms: 3000, evidence_path: "/test/private/read-only-checks.json",
    uid_checks: ["u1", "u2", "u3"].map(user_id => ({user_id, status: "observed"}))};
  const migrated = migrateLegacyRecovery(legacy, {runId: run.run_id, repairValidation: evidence});
  assert.deepEqual(migrated.legacy_recovery, legacy);
  assert.equal(migrated.recovery.attempts, 2);
  assert.equal(migrated.recovery.used, true);
  const f = fixture({saved: migrated, observe: () => ({status: "deferred", reason: "search_loading", reviews: []})});
  const flow = f.workflow();
  await flow.runBatch([item("u4"), item("u5"), item("u6")]);
  assert.equal((await flow.summary()).state.diagnostic.reason, "repeated_read_failures_after_recovery");
  assert.throws(() => migrateLegacyRecovery(legacy, {runId: run.run_id,
    repairValidation: {...evidence, uid_checks: [{user_id: "u1", status: "observed"}]}}), /repair_evidence_required/);
  assert.throws(() => migrateLegacyRecovery({run_id: run.run_id}, {runId: run.run_id}), /unknown_legacy_format/);
});

test("a different request cannot replace an active ledger session", async () => {
  const saved = migrateLegacyRecovery({run_id: run.run_id, attempts: 2}, {runId: run.run_id, sessionId: "old-request"});
  let beginCalls = 0;
  const f = fixture({saved, ledgerExtras: {
    sessionStatus: async () => ({run_id: run.run_id, session_id: "old-request", state: "active"}),
    beginSession: async () => {beginCalls++;},
  }});
  await assert.rejects(f.workflow({sessionId: "new-request"}).summary(), /mismatch_active_session/);
  assert.equal(beginCalls, 0);
  assert.deepEqual(f.stored(), saved);
});

const healthyProbe = uid => ({status: "healthy", uid, identity_verified: true,
  panel_loaded: true, search_complete: true, selected_account_id: "a1"});

test("three individual read failures use a known healthy control and continue without sending the control", async () => {
  const probes = [];
  const f = fixture({observe: ({uid}) => uid === "good" || uid === "next"
    ? observation({uid}) : {status: "deferred", reason: "search_result_not_confirmed", reviews: []},
    adapterExtras: {probeUid: async ({uid}) => {probes.push(uid); return healthyProbe(uid);}}});
  const flow = f.workflow();
  await flow.process(item("good"));
  const results = await flow.runBatch([item("bad1"), item("bad2"), item("bad3"), item("next")]);
  assert.equal(results.results.length, 4);
  assert.deepEqual(probes, ["good"]);
  assert.equal(results.diagnostic.status, "ready");
  assert.equal(f.calls.filter(c => c[0] === "click").length, 2);
  assert.deepEqual((await flow.summary()).failures.map(f => f.user_id), ["bad1", "bad2", "bad3"]);
});

test("failed normal controls require bounded read-only recovery and retain pause across restart", async () => {
  const probes = [];
  const f = fixture({observe: ({uid}) => uid.startsWith("good") ? observation({uid})
    : {status: "deferred", reason: "history_load_stalled", reviews: []},
    adapterExtras: {probeUid: async ({uid}) => {probes.push(uid); return {status: "deferred", reason: "search_empty"};}}});
  let flow = f.workflow();
  await flow.process(item("good1")); await flow.process(item("good2"));
  const result = await flow.runBatch([item("bad1"), item("bad2"), item("bad3"), item("unvisited")]);
  assert.equal(result.results.length, 3);
  assert.equal(result.diagnostic.status, "recovery_required");
  assert.deepEqual(probes, ["good2", "good1"]);
  await flow.pause();
  flow = f.workflow();
  assert.equal((await flow.process(item("unvisited"))).status, "paused");
  assert.equal(f.calls.filter(c => c[0] === "click").length, 2);
});

test("without a normal control sample only three new candidates may be probed before recovery", async () => {
  const probes = [];
  const f = fixture({observe: () => ({status: "deferred", reason: "search_result_not_confirmed", reviews: []}),
    adapterExtras: {probeUid: async ({uid}) => {probes.push(uid); return {status: "deferred", reason: "search_empty"};}}});
  const result = await f.workflow().runBatch(Array.from({length: 8}, (_, i) => item(`bad${i + 1}`)));
  assert.equal(result.results.length, 6);
  assert.deepEqual(probes, ["bad4", "bad5", "bad6"]);
  assert.equal(f.calls.filter(c => c[0] === "observe").length, 3);
  assert.equal(result.diagnostic.status, "recovery_required");
  assert.equal(f.calls.some(c => ["prepare", "click"].includes(c[0])), false);
});

test("healthy controls cannot allow an unlimited window without a new successfully inspected UID", async () => {
  const f = fixture({observe: ({uid}) => uid === "good" ? observation({uid})
    : {status: "deferred", reason: "search_result_not_confirmed", reviews: []},
    adapterExtras: {probeUid: async ({uid}) => healthyProbe(uid)}});
  const flow = f.workflow(); await flow.process(item("good"));
  const result = await flow.runBatch(Array.from({length: 12}, (_, i) => item(`bad${i + 1}`)), {limit: 12});
  assert.equal(result.results.length, 9);
  assert.equal(result.diagnostic.reason, "no_progress_read_window");
  assert.equal(f.calls.filter(c => c[0] === "click").length, 1);
});

test("a search-only probe with an incorrect UID or incomplete identity is never accepted as healthy", async () => {
  const f = fixture({observe: ({uid}) => uid === "good" ? observation({uid})
    : {status: "deferred", reason: "search_result_not_confirmed", reviews: []},
    adapterExtras: {probeUid: async () => healthyProbe("wrong")}});
  const flow = f.workflow(); await flow.process(item("good"));
  await flow.runBatch([item("bad1"), item("bad2"), item("bad3")]);
  assert.equal((await flow.summary()).state.diagnostic.status, "recovery_required");
});

test("UID policy checks all accounts and selects only the most recently contacted account", async () => {
  const f = fixture({observe: ({uid}) => observation({uid, selected_account_id: "a1",
    discovered_account_ids: ["a1", "a2"], reviews: [makeReview("a1", {last_contact_at_ms: 1000}),
      makeReview("a2", {last_contact_at_ms: 2000})]})});
  const flow = f.workflow({run: {...run, touch_policy: "uid_once"}});
  const result = await flow.process(item("u1", {preferred_account_id: "a1"}));
  assert.equal(result.results.length, 1);
  assert.equal(result.results[0].account_id, "a2");
  assert.equal(f.calls.find(c => c[0] === "confirm")[1].length, 2);
  assert.equal(f.calls.filter(c => c[0] === "click").length, 1);
  assert.equal((await flow.process(item())).reason, "already_sent_this_run");
  assert.equal(f.calls.filter(c => c[0] === "observe").length, 1);
});

test("UID policy prefers the current account on equal verified contact times and persists successful UID isolation", async () => {
  const f = fixture({observe: ({uid}) => observation({uid, selected_account_id: "a2",
    discovered_account_ids: ["a1", "a2"], reviews: [makeReview("a1", {last_customer_at_ms: 1000}),
      makeReview("a2", {last_customer_at_ms: 1000})]})});
  await f.workflow({run: {...run, touch_policy: "uid_once"}}).process(item());
  const flow = f.workflow({run: {...run, touch_policy: "uid_once"}});
  assert.equal((await flow.process(item())).status, "skipped");
  assert.equal(f.calls.filter(c => c[0] === "select").length, 0);
  assert.equal(f.calls.filter(c => c[0] === "click").length, 1);
});

test("diagnostics remain valid complete JSON and the summary retains more than one hundred failed UIDs", async () => {
  const diagnostic = {uid: "u1", input_value: "u1", visible_count: 0, evidence_text: "搜索异常".repeat(250)};
  const f = fixture({observe: () => ({status: "deferred", reason: "review_context_incomplete",
    search_diagnostics: [diagnostic], reviews: []})});
  const flow = f.workflow();
  for (let i = 0; i < 105; i++) await flow.process(item(`u${i + 1}`));
  const evidence = JSON.parse(f.calls.find(c => c[0] === "defer")[1].evidence);
  assert.deepEqual(evidence.search_diagnostics, [diagnostic]);
  assert.equal((await flow.summary()).failures.length, 105);
});

test("optional append-only events preserve sequence across restart and logging failure cannot invent a failed send", async () => {
  const entries = [];
  const f = fixture();
  let logBroken = false;
  const store = {...f.store, appendEvent: async entry => {
    if (logBroken) throw new Error("event log unavailable"); entries.push(entry);
  }};
  await f.workflow({progressStore: store}).process(item());
  const firstLast = entries.at(-1).sequence;
  logBroken = true;
  const flow = f.workflow({progressStore: store});
  assert.equal((await flow.process(item("u2"))).results[0].status, "sent");
  assert.ok(f.stored().event_sequence > firstLast);
  assert.equal((await flow.summary()).state.event_log_error.reason, "event log unavailable");
  assert.equal(f.pending.length, 0);
});

test("failure evidence preserves the exact identity diagnostics and platform notices survive finalization", async () => {
  const identity = {expected_uid: "u1", right_panel_uid: "u1", visible_message_uid: "old", identity_verified: false};
  const f = fixture({observe: () => ({status: "deferred", reason: "conversation_identity_not_ready",
    identity_diagnostics: identity, reviews: []})});
  const flow = f.workflow(); await flow.process(item());
  assert.deepEqual((await flow.summary()).failures[0].identity_diagnostics, identity);
  assert.deepEqual(JSON.parse(f.calls.find(c => c[0] === "defer")[1].evidence).identity_diagnostics, identity);
  const notices = [{text: "由于对方隐私设置，你无法发送消息", severity: "error", source: ".toast", observed_at_ms: 1000}];
  const f2 = fixture({receipt: () => ({status: "failed", reason: "privacy_restricted", platform_notices: notices})});
  await f2.workflow().process(item());
  assert.deepEqual(JSON.parse(f2.calls.find(c => c[0] === "finish")[1].detail).platform_notices, notices);
});

test("long first read failures still probe a healthy control before diagnosing a bounded no-progress window", async () => {
  let time = 1000, probed = false;
  const f = fixture({observe: ({uid}) => {
    if (uid === "good") return observation({uid});
    time += 45000;
    return {status: "deferred", reason: "history_read_budget_exhausted", reviews: []};
  }, adapterExtras: {probeUid: async ({uid}) => {probed = true; return healthyProbe(uid);}}});
  const flow = f.workflow({now: () => time}); await flow.process(item("good"));
  await flow.runBatch([item("bad1"), item("bad2"), item("bad3")]);
  assert.equal(probed, true);
  assert.equal((await flow.summary()).state.diagnostic.status, "ready");
});

test("generic read exceptions use health checks while semantic review gaps do not imply a browser outage", async () => {
  let probes = 0;
  const f = fixture({observe: ({uid}) => {
    if (uid === "good") return observation({uid}); throw new Error("snapshot timeout");
  }, adapterExtras: {probeUid: async ({uid}) => {probes++; return healthyProbe(uid);}}});
  const flow = f.workflow(); await flow.process(item("good"));
  await flow.runBatch([item("bad1"), item("bad2"), item("bad3")]);
  assert.equal(probes, 1);
  assert.equal((await flow.summary()).state.diagnostic.status, "ready");
});

test("append-only metrics distinguish full history and cached boundary without retaining customer words", async () => {
  const entries = [];
  const f = fixture({observe: ({uid}) => observation({uid, performance: {snapshot_reads: 3},
    reviews: [makeReview("a1", {history_from_start_complete: false, history_reached_prior_boundary: true})]})});
  await f.workflow({progressStore: {...f.store, appendEvent: async entry => entries.push(entry)},
    review: () => [decision()]}).process(item());
  const entry = entries.find(entry => entry.stage === "observe");
  assert.equal(entry.history.cached_boundaries, 1);
  assert.equal(entry.history.from_start, 0);
  assert.equal(entry.performance.snapshot_reads, 3);
  assert.equal(JSON.stringify(entries).includes("我方此前消息"), false);
});

test("a boundary pause requested during observation is written to the event journal once", async () => {
  const entries = []; let flow;
  const f = fixture({observe: () => {flow.requestPause(); return observation();}});
  flow = f.workflow({progressStore: {...f.store, appendEvent: async entry => entries.push(entry)}});
  await flow.process(item());
  assert.equal(entries.filter(entry => entry.stage === "control" && entry.outcome === "paused").length, 1);
  assert.equal(f.calls.some(c => c[0] === "click"), false);
});

test("recovery validates a formerly healthy control instead of requiring an individually failed UID to become readable", async () => {
  let pageHealthy = false;
  const observed = [], probed = [];
  const f = fixture({observe: ({uid}) => {
    observed.push(uid);
    return uid === "good" ? observation({uid}) : {status: "deferred", reason: "search_result_not_confirmed", reviews: []};
  }, adapterExtras: {probeUid: async ({uid}) => {
    probed.push(uid); return pageHealthy ? healthyProbe(uid) : {status: "deferred", reason: "search_empty"};
  }}});
  const flow = f.workflow(); await flow.process(item("good"));
  await flow.runBatch([item("bad1"), item("bad2"), item("bad3")]);
  pageHealthy = true;
  const result = await flow.recover({item: item("bad3", {retry_stage: "slow"})});
  assert.equal(result.status, "recovered");
  assert.equal(result.control_uid, "good");
  assert.equal(result.sends, 0);
  assert.deepEqual(observed, ["good", "bad1", "bad2", "bad3"]);
  assert.deepEqual(probed, ["good", "good"]);
  assert.equal(f.calls.filter(c => c[0] === "click").length, 1);
});

test("durable run status overrides an old run JSON policy and a later external pause before the next UID", async () => {
  let policy = "uid_once", runState = "active";
  const f = fixture({observe: ({uid}) => observation({uid, discovered_account_ids: ["a1", "a2"],
    reviews: [makeReview(), makeReview("a2")]}), ledgerExtras: {
      runStatus: async () => ({run_id: run.run_id, touch_policy: policy, run_state: runState}),
  }});
  const flow = f.workflow({run: {...run, touch_policy: "account_once"}});
  assert.equal((await flow.process(item())).results.length, 1);
  runState = "paused";
  assert.equal((await flow.process(item("u2"))).status, "paused");
  assert.equal(f.calls.filter(c => c[0] === "observe").length, 1);
  assert.equal((await flow.summary()).run_status.run_state, "paused");
});

test("completed runs remain readable but execution cannot open a new ledger session or observe customers", async () => {
  const f = fixture({ledgerExtras: {runStatus: async () => ({run_id: run.run_id,
    touch_policy: "uid_once", run_state: "completed_with_exceptions"}),
    beginSession: async () => {throw new Error("closed run must not start a session");}}});
  const flow = f.workflow({sessionId: "closed-run"});
  assert.equal((await flow.summary()).state.run_state, "completed_with_exceptions");
  assert.equal((await flow.process(item())).reason, "run_already_completed");
  assert.equal(f.calls.some(c => c[0] === "observe"), false);
});

test("pausing preserves an open clicked touch even when ledger lifecycle persistence refuses it", async () => {
  const f = fixture({ledgerExtras: {pauseRun: async () => {throw new Error("open_touch_requires_reconciliation");}}});
  f.pending.push({touch_id: "existing", state: "clicked"});
  const flow = f.workflow();
  assert.equal((await flow.pause()).status, "paused");
  const summary = await flow.summary();
  assert.equal(summary.state.control.paused, true);
  assert.equal(summary.state.lifecycle_error.action, "pause");
  assert.equal(f.pending[0].state, "clicked");
  assert.equal((await flow.process(item())).status, "paused");
  assert.equal(f.calls.some(c => c[0] === "click"), false);
});

test("read-only workflow pause and resume never writes run lifecycle or begins a session", async () => {
  const forbidden = async () => {throw new Error("read-only lifecycle write");};
  const f = fixture({ledgerExtras: {pauseRun: forbidden, resumeRun: forbidden, beginSession: forbidden}});
  const flow = f.workflow({sendEnabled: false});
  assert.equal((await flow.pause()).status, "paused");
  assert.equal((await flow.resume()).status, "ready");
  assert.equal((await flow.process(item())).status, "read_only");
  assert.equal((await flow.summary()).state.lifecycle_error, undefined);
});

test("summary keeps the ledger's total unresolved customers and does not add an already included local held customer", async () => {
  const f = fixture({observe: ({uid}) => batchObservation(uid), ledgerExtras: {
    recordReviewedHistory: async () => ({ok: true}), runStatus: async ({include_items}) => ({run_id: run.run_id,
      touch_policy: "uid_once", run_state: "active", counts: {uncompleted_customers: 38},
      ...(include_items ? {items: Array.from({length: 38}, (_, i) => ({user_id: `u${i + 1}`}))} : {}),
    }),
  }});
  const flow = f.workflow({batchMode: true, now: () => 1000});
  await flow.process(item()); const [packet] = await flow.reviewPackets();
  await flow.holdReviews([{uid: packet.uid, evidence_id: packet.evidence_id, reason: "image review"}]);
  const compact = await flow.summary();
  assert.equal(compact.queue_status.uncompleted_customers, 38);
  assert.equal(compact.queue_status.held_review_count, 1);
  assert.equal(compact.run_status.items, undefined);
  const detailed = await flow.summary({includeItems: true});
  assert.equal(detailed.queue_status.uncompleted_customers, 38);
  assert.equal(detailed.queue_status.uncompleted_user_ids.length, 38);
});

test("an explicit second pause can synchronize run state after an independently reconciled clicked touch", async () => {
  let reconciled = false, pauses = 0;
  const f = fixture({ledgerExtras: {pauseRun: async () => {
    pauses++; if (!reconciled) throw new Error("open_touch_requires_reconciliation");
    return {ok: true, run_id: run.run_id, state: "paused"};
  }}});
  const flow = f.workflow(); await flow.pause();
  reconciled = true;
  await flow.pause();
  const state = (await flow.summary()).state;
  assert.equal(pauses, 2);
  assert.equal(state.run_state, "paused");
  assert.equal(state.lifecycle_error, undefined);
});

// Regression: adapter selection failures must not escape the UID scheduler.
for (const errorText of ["Timeout 1000ms exceeded", "Debugger unattached"]) {
  test(`account switch ${errorText} is classified before another UID runs`, async () => {
    const f = fixture({observe: ({uid}) => observation({uid,
      reviews: uid === "u1" ? [makeReview(), makeReview("a2")] : [makeReview()],
      discovered_account_ids: uid === "u1" ? ["a1", "a2"] : ["a1"]}),
      adapterExtras: {selectReviewedAccount: async () => {throw new Error(errorText);}}});
    const flow = f.workflow();
    const result = await flow.runBatch([item(), item("u2")]);
    const disconnected = errorText === "Debugger unattached";
    assert.equal(result.results[0].status, disconnected ? "blocked" : "deferred");
    assert.equal(result.results[0].reason, disconnected ? "browser_connection_unavailable" : "browser_read_exception");
    assert.equal(result.results.length, disconnected ? 1 : 2);
    assert.equal(f.calls.filter(c => c[0] === "defer").length, disconnected ? 0 : 1);
    assert.equal(f.pending.length, 0);
    assert.equal((await flow.summary()).state.read_failure_uids.length, 0);
    assert.equal(f.counters.sent_this_run, disconnected ? 105 : 106);
  });
}

test("observation event persistence failure propagates without consuming browser retries", async () => {
  const f = fixture();
  let fail = false;
  const store = {...f.store, save: async state => {
    if (fail) throw new Error("progress_storage_unavailable");
    return f.store.save(state);
  }};
  f.adapter.observeUid = async () => {fail = true; return observation();};
  const flow = f.workflow({progressStore: store});
  await assert.rejects(flow.process(item()), /progress_storage_unavailable/);
  assert.equal(f.calls.some(c => ["defer", "prepare", "click"].includes(c[0])), false);
});

test("click event persistence failure retains clicked touch instead of fabricating an uncertain receipt", async () => {
  const f = fixture();
  let fail = false;
  const store = {...f.store, save: async state => {
    if (fail) throw new Error("receipt_progress_unavailable");
    return f.store.save(state);
  }};
  f.adapter.clickAndVerify = async () => {fail = true; return {status: "sent"};};
  await assert.rejects(f.workflow({progressStore: store}).process(item()), /receipt_progress_unavailable/);
  assert.equal(f.pending[0].state, "clicked");
  assert.equal(f.calls.some(c => c[0] === "finish"), false);
});

for (const method of ["confirmBusinessReview", "revalidateAndStage"]) {
  test(`${method} exception stops safely at its actual ledger boundary`, async () => {
    const f = fixture({adapterExtras: {[method]: async () => {throw new Error("Timeout exceeded");}}});
    const flow = f.workflow();
    const result = await flow.process(item());
    assert.equal(result.status, method === "confirmBusinessReview" ? "deferred" : "blocked");
    assert.equal(result.reason, method === "confirmBusinessReview" ? "browser_read_exception" : "reservation_requires_reconciliation");
    assert.equal(f.calls.some(c => c[0] === "click"), false);
    assert.equal(f.pending.length, method === "confirmBusinessReview" ? 0 : 1);
  });
}

test("cache statistics distinguish approved unchanged history from new customer risk", async () => {
  let text = "我们做建材";
  const f = fixture({observe: ({uid}) => batchObservation(uid, text),
    ledgerExtras: {recordReviewedHistory: async () => ({ok: true}), next: async () => ({items: [item()]})}});
  const flow = f.workflow({batchMode: true, now: () => 1000});
  await flow.runSlice();
  const [packet] = await flow.reviewPackets();
  await flow.submitReviews([{uid: packet.uid, evidence_id: packet.evidence_id, decisions: [decision()]}]);
  await flow.runSlice();
  assert.equal((await flow.summary()).state.metrics.review_cache.hit_count, 1);
  text = "不要再发";
  assert.equal((await flow.process(item())).status, "review_queued");
  const metric = (await flow.summary()).state.metrics.review_cache;
  assert.equal(metric.hit_count, 1);
  assert.equal(metric.reread_reasons.approved_evidence_changed, 1);
});

test("summary exposes startup controller state and leaves read-only progress untouched", async () => {
  const f = fixture({ledgerExtras: {runStatus: async () => ({run_id: run.run_id,
    run_state: "active", startup: {version: "test", run_state: "active", scope_mode: "all"}})}});
  await f.workflow({sendEnabled: false}).summary();
  assert.equal(f.stored(), null);
  const flow = f.workflow({sendEnabled: false});
  await flow.pause();
  const summary = await flow.summary();
  assert.deepEqual(summary.startup, {version: "test", run_state: "active", scope_mode: "all",
    send_enabled: false, paused: true});
});

test("connection loss after the click records uncertainty once then blocks every next UID", async () => {
  const f = fixture({receipt: () => {throw new Error("Debugger unattached");}});
  const flow = f.workflow();
  const batch = await flow.runBatch([item(), item("u2")]);
  assert.equal(batch.results.length, 1);
  assert.equal(batch.diagnostic.reason, "browser_connection_unavailable");
  assert.equal(f.counters.uncertain_this_run, 1);
  assert.equal(f.calls.filter(c => c[0] === "finish").length, 1);
  assert.equal(f.calls.filter(c => c[0] === "defer").length, 0);
  assert.equal((await flow.process(item("u3"))).reason, "browser_connection_unavailable");
});

for (const reason of ["customer_annotation_tab_missing", "customer_annotation_tab_not_restored"]) {
  test(`unconfirmed page safety ${reason} globally blocks before further customer work`, async () => {
    const f = fixture({observe: () => ({status: "deferred", reason, reviews: []})});
    const flow = f.workflow();
    const result = await flow.runBatch([item(), item("u2")]);
    assert.equal(result.diagnostic.status, "blocked");
    assert.equal(result.diagnostic.reason, reason);
    assert.equal(result.results.length, 1);
    assert.equal(f.calls.some(c => ["prepare", "click"].includes(c[0])), false);
  });
}

for (const failure of ["session_not_found", "database_locked", "session_schema_missing_run_init"]) {
  test(`completed summary handles explicit Python session failure ${failure}`, async () => {
    const f = fixture({ledgerExtras: {
      runStatus: async () => ({run_id: run.run_id, run_state: "completed"}),
      sessionStatus: async () => {const error = new Error("ledger_session-status_failed: " + failure); error.ledgerError = failure; throw error;},
      beginSession: async () => assert.fail("completed summary began a session"),
    }});
    const flow = f.workflow({sessionId: "irrelevant-new-request"});
    if (failure === "session_not_found") assert.equal((await flow.summary()).session, null);
    else await assert.rejects(flow.summary(), new RegExp(failure));
  });
}

for (const batchMode of [false, true]) {
  test(`business review timing is recorded for automatic decisions in batch ${batchMode}`, async () => {
    let time = 1000;
    const f = fixture({observe: ({uid}) => batchMode ? batchObservation(uid, "你好") : observation(),
      ledgerExtras: {recordReviewedHistory: async () => ({ok: true})}});
    const flow = f.workflow({batchMode, now: () => ++time});
    assert.equal((await flow.process(item())).status, "processed");
    const metric = (await flow.summary()).state.metrics.semantic_review;
    assert.equal(metric.count, 1);
    assert.ok(metric.total_ms > 0);
  });
}

for (const finalState of ["completed", "completed_with_exceptions"]) {
  for (const alreadyInitialized of [false, true]) {
    for (const pausePhase of ["before", "during"]) {
      test(`completed summary stays read-only after requestPause in ${finalState} initialized ${alreadyInitialized} pause ${pausePhase}`, async () => {
        let ledgerState = alreadyInitialized ? "active" : finalState, writes = 0, pauses = 0, injectPause = false, flow;
        const f = fixture({ledgerExtras: {
          runStatus: async () => ({run_id: run.run_id, run_state: ledgerState,
            startup: {run_state: ledgerState}}),
          report: async () => {
            if (injectPause && pausePhase === "during") flow.requestPause();
            return {sent_this_run:104, failed_this_run:7, uncertain_this_run:0};
          },
          pauseRun: async () => {pauses++; return {run_state: "paused"};},
        }});
        const store = {...f.store, save: async value => {writes++; return f.store.save(value);}};
        flow = f.workflow({progressStore: store, sendEnabled: true});
        if (alreadyInitialized) await flow.summary();
        const before = writes;
        ledgerState = finalState;
        injectPause = true;
        if (pausePhase === "before") flow.requestPause();
        const summary = await flow.summary();
        assert.equal(writes, before);
        assert.equal(pauses, 0);
        assert.equal(summary.state.run_state, finalState);
        assert.equal(summary.startup.paused, true);
      });
    }
  }
}

for (const includeItems of [false, true]) {
  for (const localJobs of [false, true]) {
    test(`legacy uninitialized summary preserves unknown count, detailed ${includeItems}, local jobs ${localJobs}`, async () => {
      const saved = {version: 1, run_id: run.run_id,
        pending_reviews: localJobs ? {u1: {}} : {}, held_reviews: localJobs ? {u2: {}} : {},
        ready_jobs: localJobs ? {u3: {}} : {}};
      const before = structuredClone(saved);
      let saves = 0;
      const f = fixture({ledgerExtras: {
        runStatus: async () => ({run_id: run.run_id, run_state: "active", touch_policy: "account_once",
          initialized: false, initialization_required: true, counts: null,
          startup: {initialized: false, counts: null}}),
        beginSession: async () => assert.fail("summary began session"),
      }});
      const flow = f.workflow({sendEnabled: false, progressStore: {
        load: async () => saved, save: async () => {saves++;},
      }});
      const summary = await flow.summary({includeItems});
      assert.equal(summary.queue_status.uncompleted_customers, null);
      assert.equal(summary.queue_status.initialized, false);
      assert.equal(summary.queue_status.initialization_required, true);
      assert.equal(summary.queue_status.pending_review_count, localJobs ? 1 : 0);
      assert.equal(summary.queue_status.held_review_count, localJobs ? 1 : 0);
      assert.equal(summary.queue_status.ready_count, localJobs ? 1 : 0);
      assert.equal(summary.queue_status.uncompleted_user_ids, undefined);
      assert.equal(summary.startup.counts, null);
      assert.equal(saves, 0);
      assert.deepEqual(saved, before);
      assert.deepEqual(f.calls, []);
    });
  }
}

test("summary without legacy mock runStatus retains known local count fallback", async () => {
  const f = fixture({saved: {version: 1, run_id: run.run_id,
    pending_reviews: {u1: {}}, held_reviews: {u2: {}}, ready_jobs: {u1: {}}}});
  const summary = await f.workflow({sendEnabled: false}).summary({includeItems: true});
  assert.equal(summary.queue_status.uncompleted_customers, 2);
});
