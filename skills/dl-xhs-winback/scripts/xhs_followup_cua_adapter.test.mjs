// All UID/account fixtures in this file are synthetic; never use them in a live browser.
import test from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";

import {
  buildReview,
  captureForAnalysis,
  chooseSendingAccount,
  createXhsCuaAdapter,
  normalizeMessageForHash,
  readSnapshot,
  verifyNewOutbound,
} from "./xhs_followup_cua_adapter.mjs";

const UID = "f00000000000000000000001";
const ACCOUNT = "f00000000000000000000002";

function raw(overrides = {}) {
  return {
    routeReady: true,
    searchReady: true,
    panelTab: "客资标注",
    rightPanelUid: UID,
    activeKey: `Active-${UID}-${ACCOUNT}`,
    labelPresent: true,
    labelValue: "请选择线索标签",
    notePresent: true,
    noteValue: "-",
    leadBadges: [],
    draft: "",
    messages: [
      { id: `jarvis-msg-${UID}.${ACCOUNT}.1`, side: "left", type: "TEXT", timestamp: 1_000, text: "我想了解课程" },
      { id: `jarvis-msg-${UID}.${ACCOUNT}.2`, side: "right", type: "TEXT", timestamp: 2_000, text: "您好，方便留个联系方式吗" },
    ],
    loadMoreVisible: false,
    ...overrides,
  };
}

test("requires right UID, active account and matching message ID together", () => {
  const good = buildReview(raw(), { uid: UID, authorizedAccountIds: [ACCOUNT] });
  assert.equal(good.identity_verified, true);
  assert.equal(good.account_id, ACCOUNT);
  assert.equal(good.source, "natural");

  const residual = buildReview(raw({
    activeKey: `Active-${UID}-different-account`,
  }), { uid: UID, authorizedAccountIds: [ACCOUNT, "different-account"] });
  assert.equal(residual.identity_verified, false);

  const wrongUid = buildReview(raw({ rightPanelUid: "another-user" }), { uid: UID, authorizedAccountIds: [ACCOUNT] });
  assert.equal(wrongUid.identity_verified, false);
});

test("reads exact lead and no-intent labels, and never mistakes an unloaded panel for natural", () => {
  assert.equal(buildReview(raw({ leadBadges: ["留客资"] }), { uid: UID, authorizedAccountIds: [ACCOUNT] }).label, "lead");
  assert.equal(buildReview(raw({ labelValue: "留客资" }), { uid: UID, authorizedAccountIds: [ACCOUNT] }).label, "lead");
  assert.equal(buildReview(raw({ labelValue: "无意向" }), { uid: UID, authorizedAccountIds: [ACCOUNT] }).label, "no_intent");
  assert.equal(buildReview(raw({ labelValue: "无意向", leadBadges: ["留客资"] }), {
    uid: UID, authorizedAccountIds: [ACCOUNT],
  }).label, "lead");
  assert.equal(buildReview(raw({ noteValue: "外贸老板请上车" }), { uid: UID, authorizedAccountIds: [ACCOUNT] }).source, "ad");
  const unknown = buildReview(raw({ panelTab: "获客工具", notePresent: false }), { uid: UID, authorizedAccountIds: [ACCOUNT] });
  assert.equal(unknown.source, "unknown");
  assert.equal(unknown.panel_loaded, false);
  assert.equal(buildReview(raw({ panelText: "企业微信 - 未添加企微 留资卡 无提交留资卡" }), {
    uid: UID, authorizedAccountIds: [ACCOUNT],
  }).protective_lead_signal, false);
  assert.equal(buildReview(raw({ panelText: "对方已点击你的企业微信联系卡" }), {
    uid: UID, authorizedAccountIds: [ACCOUNT],
  }).platform_lead_signal, false);
  assert.equal(buildReview(raw({ messages: [...raw().messages, {
    id: `jarvis-msg-${UID}.${ACCOUNT}.3`, side: "left", type: "TEXT", timestamp: 3_000,
    text: "我的手机号是 13812345678",
  }] }), { uid: UID, authorizedAccountIds: [ACCOUNT] }).contact_shared_hint, true);
});

test("full customer text remains available and unknown message types require visual review", () => {
  const longRefusal = "普通文字".repeat(800) + "别再发";
  const refused = buildReview(raw({ messages: [...raw().messages, {
    id: `jarvis-msg-${UID}.${ACCOUNT}.3`, side: "left", type: "TEXT", timestamp: 3_000, text: longRefusal,
  }] }), { uid: UID, authorizedAccountIds: [ACCOUNT] });
  assert.equal(refused.negative_status, "blocked");
  assert.equal(refused.messages.at(-1).text.length, 3203);
  const unknownType = buildReview(raw({ messages: [...raw().messages, {
    id: `jarvis-msg-${UID}.${ACCOUNT}.3`, side: "right", type: "UNRECOGNIZED", timestamp: 3_000, text: "跟进文案",
  }] }), { uid: UID, authorizedAccountIds: [ACCOUNT] });
  assert.equal(unknownType.history_complete, true);
  assert.equal(unknownType.needs_visual_review, true);
  assert.equal(unknownType.reply_status, "awaiting_customer");
  assert.ok(unknownType.outbound_text_hashes.length);
  const generalText = buildReview(raw({ messages: [...raw().messages, {
    id: `jarvis-msg-${UID}.${ACCOUNT}.3`, side: "right", type: "GENERAL", plainText: true,
    timestamp: 3_000, text: "员工发出的普通文字",
  }] }), { uid: UID, authorizedAccountIds: [ACCOUNT] });
  assert.equal(generalText.history_complete, true);
  assert.equal(generalText.last_outbound_text_at_ms, 3_000);
  const generalCard = buildReview(raw({ messages: [...raw().messages, {
    id: `jarvis-msg-${UID}.${ACCOUNT}.3`, side: "right", type: "GENERAL", plainText: false,
    timestamp: 3_000, text: "未知卡片",
  }] }), { uid: UID, authorizedAccountIds: [ACCOUNT] });
  assert.equal(generalCard.needs_visual_review, true);
});

test("customer reply, manual outbound and draft all remain visible in the review", () => {
  const review = buildReview(raw({
    draft: "用户正在输入",
    messages: [...raw().messages, {
      id: `jarvis-msg-${UID}.${ACCOUNT}.3`, side: "left", type: "TEXT", timestamp: 3_000, text: "我有个问题",
    }],
  }), { uid: UID, authorizedAccountIds: [ACCOUNT] });
  assert.equal(review.reply_status, "awaiting_us");
  assert.equal(review.last_outbound_text_at_ms, 2_000);
  assert.equal(review.last_customer_at_ms, 3_000);
  assert.equal(review.draft_present, true);
});

test("peer classification is informational and not a stop-contact decision", () => {
  const peer = buildReview(raw({ messages: [
    { id: `jarvis-msg-${UID}.${ACCOUNT}.1`, side: "left", type: "TEXT", timestamp: 1_000, text: "好巧 同行" },
    { id: `jarvis-msg-${UID}.${ACCOUNT}.2`, side: "right", type: "TEXT", timestamp: 2_000, text: "谢谢" },
  ] }), { uid: UID, authorizedAccountIds: [ACCOUNT] });
  assert.equal(peer.party_status, "peer");
});

test("account choice uses latest incoming, previous sender then explicit order", () => {
  const accounts = [
    { account_id: "a", identity_verified: true, last_customer_at_ms: 500 },
    { account_id: "b", identity_verified: true, last_customer_at_ms: 800 },
  ];
  assert.equal(chooseSendingAccount(accounts, { authorizedAccountIds: ["a", "b"] }), "b");
  assert.equal(chooseSendingAccount(accounts.map((x) => ({ ...x, last_customer_at_ms: 800 })), {
    authorizedAccountIds: ["a", "b"], lastSenderAccountId: "a",
  }), "a");
  assert.equal(chooseSendingAccount(accounts.map((x) => ({ ...x, last_customer_at_ms: 800 })), {
    authorizedAccountIds: ["a", "b"],
  }), "a");
});

test("duplicate hash canonicalization folds whitespace while preserving punctuation", () => {
  assert.equal(normalizeMessageForHash("  你好\n\n 世界  "), "你好 世界");
  assert.notEqual(normalizeMessageForHash("你好，世界"), normalizeMessageForHash("你好,世界"));
});

test("incremental review keeps cached outbound hashes only after a proven boundary", () => {
  const cached = "a".repeat(64);
  const observed = buildReview(raw({ loadMoreVisible: true }), {
    uid: UID, authorizedAccountIds: [ACCOUNT],
    historyFromStartComplete: false,
    historyReachedPriorBoundary: true,
    cachedOutboundTextHashes: [cached],
  });
  assert.equal(observed.history_complete, true);
  assert.equal(observed.history_reached_prior_boundary, true);
  assert.equal(observed.outbound_text_hashes.includes(cached), true);
  assert.equal(observed.outbound_text_hashes.length, 2);
  const incomplete = buildReview(raw({ loadMoreVisible: true }), {
    uid: UID, authorizedAccountIds: [ACCOUNT], historyComplete: false,
    cachedOutboundTextHashes: [cached],
  });
  assert.equal(incomplete.history_complete, false);
  assert.deepEqual(incomplete.outbound_text_hashes, []);
});

test("analysis capture emits at most forty real messages without changing conversion evidence", () => {
  const review = buildReview(raw({ messages: [
    ...raw().messages,
    { id: `jarvis-msg-${UID}.${ACCOUNT}.3`, side: "right", type: "CARD", timestamp: 3_000, text: "企业微信名片" },
  ] }), { uid: UID, authorizedAccountIds: [ACCOUNT], analysisMode: true, nowMs: 4_000 });
  const captured = captureForAnalysis({ reviews: [review], finished_at_ms: review.observed_at_ms + 100 }, ACCOUNT);
  assert.deepEqual(captured.messages.map((message) => message.sender), ["customer", "staff"]);
  assert.equal(captured.uid_verified, true);
  assert.equal(captured.account_verified, true);
  assert.equal(captured.label, "none");
  assert.equal(captured.captured_at_ms, review.observed_at_ms + 100);
  assert.deepEqual(captured.account_reviews.map((item) => ({
    account_id: item.account_id, panel_loaded: item.panel_loaded, label: item.label,
  })), [{ account_id: ACCOUNT, panel_loaded: true, label: "none" }]);
});

test("analysis keeps verified GENERAL text and does not let cards use the forty-message allowance", () => {
  const extra = Array.from({ length: 45 }, (_, index) => ({
    id: `jarvis-msg-${UID}.${ACCOUNT}.card${index}`, side: "right", type: "CARD",
    timestamp: 3_000 + index, text: "企业微信名片",
  }));
  const review = buildReview(raw({ messages: [...raw().messages, {
    id: `jarvis-msg-${UID}.${ACCOUNT}.general`, side: "left", type: "GENERAL",
    plainText: true, timestamp: 2_500, text: "我还有一个问题",
  }, ...extra] }), { uid: UID, authorizedAccountIds: [ACCOUNT], analysisMode: true, nowMs: 4_000 });
  const captured = captureForAnalysis({ reviews: [review] }, ACCOUNT);
  assert.deepEqual(captured.messages.map((message) => message.text), [
    "我想了解课程", "您好，方便留个联系方式吗", "我还有一个问题",
  ]);
});

test("message times and absent customer inquiry are informational, not eligibility gates", () => {
  const missingTime = buildReview(raw({ messages: [
    { ...raw().messages[0], timestamp: 0 }, raw().messages[1],
  ] }), { uid: UID, authorizedAccountIds: [ACCOUNT], nowMs: 3_000 });
  assert.equal(missingTime.history_complete, true);
  assert.equal(missingTime.reply_status, "awaiting_customer");
  const futureTime = buildReview(raw({ messages: [
    raw().messages[0], { ...raw().messages[1], timestamp: 3_000 + 10 * 60 * 1000 },
  ] }), { uid: UID, authorizedAccountIds: [ACCOUNT], nowMs: 3_000 });
  assert.equal(futureTime.history_complete, true);
  const noInquiry = buildReview(raw({ messages: [raw().messages[1]] }), {
    uid: UID, authorizedAccountIds: [ACCOUNT], nowMs: 3_000,
  });
  assert.equal(noInquiry.history_complete, true);
  assert.equal(noInquiry.reply_status, "unknown");
  assert.equal(noInquiry.party_status, "unknown");
});

test("missing right-panel field value nodes keep source and label unknown", () => {
  const missingNote = buildReview(raw({ notePresent: false, noteValue: "" }), {
    uid: UID, authorizedAccountIds: [ACCOUNT],
  });
  assert.equal(missingNote.panel_loaded, true);
  assert.equal(missingNote.source, "unknown");
  const missingLabel = buildReview(raw({ labelPresent: false, labelValue: "" }), {
    uid: UID, authorizedAccountIds: [ACCOUNT],
  });
  assert.equal(missingLabel.label, "unknown");
});

test("DOM snapshot does not treat a right-panel row without its value node as loaded", async () => {
  const titleNode = (value) => ({ textContent: value });
  const valueNode = { innerText: "请选择线索标签", querySelector: () => null };
  const row = (name, value) => ({ querySelector: (selector) =>
    selector === ".info-item-title" ? titleNode(name) :
      selector === ".info-item-value-wrap" ? value : null });
  const panel = {
    innerText: "客资标注",
    querySelectorAll: (selector) => selector === ".info-item"
      ? [row("归因笔记", null), row("客资标签", valueNode)]
      : selector === ".id-group" ? [{ querySelector: (key) => key === ".id-label"
        ? titleNode("UID") : key === ".ellipsis" ? titleNode(UID) : null }]
        : selector === ".d-tabs-header-label" ? [{ textContent: "客资标注", classList: { contains: () => true } }]
          : [],
  };
  const document = {
    querySelector: (selector) => selector === "#rightPanel" ? panel :
      selector === ".sx-contact-item.active[data-key]" ? {
        getAttribute: () => `Active-${UID}-${ACCOUNT}`, querySelectorAll: () => [],
      } : null,
    querySelectorAll: (selector) => selector === "input"
      ? [{ getAttribute: () => "输入用户ID/小红书ID" }] : [],
  };
  const tab = { playwright: { evaluate: async (pageFunction, arg) => vm.runInNewContext(
    `(${pageFunction.toString()})(${JSON.stringify(arg)})`,
    { document, location: { pathname: "/im/multiCustomerService" }, getComputedStyle: () => ({}) },
  ) } };
  const snapshot = await readSnapshot(tab, UID);
  assert.equal(snapshot.notePresent, false);
  assert.equal(snapshot.labelPresent, true);
  assert.equal(snapshot.noteValue, "");
});

test("DOM snapshot accepts the visible dash in a natural-flow attribution row", async () => {
  const title = { textContent: "归因笔记" };
  const dash = { innerText: "-", textContent: "-", querySelector: () => null };
  const attribution = { querySelector: (selector) => selector === ".info-item-title" ? title :
    selector === ".info-item-value-wrap" ? null : dash };
  const panel = {
    innerText: "客资标注 归因笔记 -",
    querySelectorAll: (selector) => selector === ".info-item" ? [attribution] : [],
  };
  const document = {
    querySelector: (selector) => selector === "#rightPanel" ? panel : null,
    querySelectorAll: () => [],
  };
  const tab = { playwright: { evaluate: async (pageFunction, arg) => vm.runInNewContext(
    `(${pageFunction.toString()})(${JSON.stringify(arg)})`,
    { document, location: { pathname: "/im/multiCustomerService" }, getComputedStyle: () => ({}) },
  ) } };
  const snapshot = await readSnapshot(tab, UID);
  assert.equal(snapshot.notePresent, true);
  assert.equal(snapshot.noteValue, "-");
});

test("bounded DOM snapshot reads at most its remaining quota of actual chat text", async () => {
  let chatTextReads = 0;
  const elements = Array.from({ length: 3 }, (_, index) => ({
    id: `jarvis-msg-${UID}.${ACCOUNT}.${index + 1}`,
    className: index % 2 ? "right" : "left",
    getAttribute: (name) => name === "data-msg-type" ? "TEXT" :
      name === "data-timestamp" ? String(index + 1) : null,
    querySelector: () => null,
    get innerText() { chatTextReads += 1; return `正文${index}`; },
  }));
  const document = {
    querySelector: () => null,
    querySelectorAll: (selector) => selector === '[id^="jarvis-msg-"]' ? elements : [],
  };
  const tab = { playwright: { evaluate: async (pageFunction, arg) => vm.runInNewContext(
    `(${pageFunction.toString()})(${JSON.stringify(arg)})`,
    { document, location: { pathname: "/im/multiCustomerService" }, getComputedStyle: () => ({}) },
  ) } };
  const snapshot = await readSnapshot(tab, UID, { messageTextLimit: 1 });
  assert.equal(chatTextReads, 1);
  assert.equal(snapshot.messages.filter((message) => message.text).length, 1);
  assert.equal(snapshot.messageTextTruncated, true);
});

test("DOM snapshot recognizes the platform's text-message markup for GENERAL replies", async () => {
  const message = {
    id: `jarvis-msg-${UID}.${ACCOUNT}.general`, className: "right",
    getAttribute: (name) => ({ "data-msg-type": "GENERAL", "data-timestamp": "1000",
      "data-normal": "true" })[name] || null,
    querySelector: (selector) => selector.includes('.text-message[msgcontainertype=""]') ? {} : null,
    innerText: "自动欢迎语",
  };
  const document = {
    querySelector: () => null,
    querySelectorAll: (selector) => selector === '[id^="jarvis-msg-"]' ? [message] : [],
  };
  const tab = { playwright: { evaluate: async (pageFunction, arg) => vm.runInNewContext(
    `(${pageFunction.toString()})(${JSON.stringify(arg)})`,
    { document, location: { pathname: "/im/multiCustomerService" }, getComputedStyle: () => ({}) },
  ) } };
  const snapshot = await readSnapshot(tab, UID);
  assert.equal(snapshot.messages[0].plainText, true);
});

// Structure copied from the inspected like-reply DOM. Customer content, UID
// and image URL are replaced; selector decisions still use the actual classes
// and attributes (d-space/content, thank-content, thank-note-img, text-message).
const LIKE_REPLY_FIXTURE = '<div class="right" data-normal="true" data-msg-type="GENERAL">' +
  '<div><div class="d-space d-space-vertical d-space-align-end" content="完整回复正文" msgcontainertype="">' +
  '<div class="tip-text">回复了对方的点赞</div><div class="thank-content">' +
  '<img class="thank-note-img" alt="笔记首图" src="https://example.invalid/note.jpg"><div class="thank-line"></div>' +
  '</div><p class="text-message"><span><span>完整回复正文</span></span></p></div></div></div>';

function likeReplySnapshotTab({ side = "right", extraMedia = false, imageAlt = "笔记首图", content = "完整回复正文",
  renderedText = "完整回复正文", extraText = "", truncated = false } = {}) {
  assert.match(LIKE_REPLY_FIXTURE, /thank-note-img/);
  const tip = { innerText: "回复了对方的点赞" };
  const body = { innerText: renderedText, textContent: renderedText,
    scrollHeight: truncated ? 60 : 20, clientHeight: 20,
    querySelector: () => truncated ? {} : null };
  const thank = {};
  const image = { parentElement: thank, getAttribute: name => name === "alt" ? imageAlt : "",
    classList: { contains: name => name === "thank-note-img" } };
  const space = { getAttribute: name => name === "content" ? content : "",
    querySelector: selector => selector === ".tip-text" ? tip : selector === ".thank-content" ? thank :
      selector === "p.text-message" ? body : null };
  const message = { id: `jarvis-msg-${UID}.${ACCOUNT}.like`, className: side,
    innerText: `回复了对方的点赞 ${renderedText}${extraText}`, textContent: `回复了对方的点赞 ${renderedText}${extraText}`,
    closest: () => null,
    getAttribute: name => ({ "data-normal": "true", "data-msg-type": "GENERAL", "data-timestamp": "1000" })[name] || "",
    querySelector: selector => selector.includes("img,") ? image : null,
    querySelectorAll: selector => selector === '.d-space[content][msgcontainertype=""]' ? [space] :
      selector === ".tip-text" ? [tip] : selector === ".thank-content" ? [thank] :
      selector === "p.text-message" ? [body] : selector === "img,video,iframe,canvas,svg,object,embed,audio" ?
        [image, ...(extraMedia ? [{}] : [])] : [] };
  const document = { querySelector: () => null,
    querySelectorAll: selector => selector === '[id^="jarvis-msg-"]' ? [message] : [] };
  return { playwright: { evaluate: async (fn, arg) => vm.runInNewContext(
    `(${fn.toString()})(${JSON.stringify(arg)})`, { document, location: { pathname: "/im/multiCustomerService" },
      getComputedStyle: () => ({}) }) } };
}

test("only the inspected own complete like-reply structure marks its thumbnail decorative", async () => {
  const snapshot = await readSnapshot(likeReplySnapshotTab(), UID);
  const message = snapshot.messages[0];
  assert.equal(message.plainText, false);
  assert.equal(message.extracted_text_complete, true);
  assert.equal(message.decorative_media_only, true);
  assert.equal(message.text, "回复了对方的点赞 完整回复正文");
  const review = buildReview(raw({ messages: [message] }), { uid: UID });
  assert.equal(review.needs_visual_review, false);
  assert.equal(review.messages[0].decorative_media_only, true);
});

for (const [name, options] of [
  ["customer media", { side: "left" }], ["additional image", { extraMedia: true }],
  ["QR or unknown image", { imageAlt: "二维码" }], ["incomplete text", { renderedText: "完整回复…" }],
  ["unknown card text", { extraText: " 未知附件内容" }],
  ["clipped text", { truncated: true }],
]) test(`like reply retains visual review for ${name}`, async () => {
  const snapshot = await readSnapshot(likeReplySnapshotTab(options), UID);
  const message = snapshot.messages[0];
  assert.equal(message.decorative_media_only, false);
  assert.equal(buildReview(raw({ messages: [message] }), { uid: UID }).needs_visual_review, true);
});

test("receipt requires a new right-hand message ID, later timestamp and exact wording", () => {
  const before = raw();
  const accepted = raw({ messages: [...before.messages, {
    id: `jarvis-msg-${UID}.${ACCOUNT}.3`, side: "right", type: "TEXT", timestamp: 3_000, text: "跟进文案",
  }] });
  assert.equal(verifyNewOutbound(before, accepted, "跟进文案", UID, ACCOUNT).ok, true);
  assert.equal(verifyNewOutbound(before, accepted, "另一句", UID, ACCOUNT).ok, false);
  assert.equal(verifyNewOutbound(before, raw(), "跟进文案", UID, ACCOUNT).ok, false);
  const optimistic = raw({ messages: [...before.messages, {
    id: "jarvis-msg-1790694573000-temp", side: "right", type: "TEXT", timestamp: 3_000,
    text: "跟进文案",
  }] });
  assert.equal(verifyNewOutbound(before, optimistic, "跟进文案", UID, ACCOUNT).ok, false);
  assert.equal(verifyNewOutbound(before, {...accepted,
    failedMessages: [{id: accepted.messages.at(-1).id, text: "跟进文案"}]}, "跟进文案", UID, ACCOUNT).ok, false);
});

test("stage never clicks and click phase requires a matching durable mark-clicked response", async () => {
  let clicked = 0;
  let filled = "";
  let state = raw();
  const tab = {
    url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService",
    playwright: {
      evaluate: async () => ({ ...state, draft: filled }),
      getByPlaceholder: () => ({
        count: async () => 1,
        fill: async (value) => { filled = value; },
      }),
      getByRole: () => ({
        count: async () => 1,
        isEnabled: async () => true,
        click: async () => {
          clicked += 1;
          state = raw({ messages: [...raw().messages, {
            id: `jarvis-msg-${UID}.${ACCOUNT}.3`, side: "right", type: "TEXT", timestamp: 3_000, text: "跟进文案",
          }] });
        },
      }),
      domSnapshot: async () => "",
    },
  };
  const flow = createXhsCuaAdapter(tab);
  const token = flow.adoptObservedSelection({ uid: UID, accountId: ACCOUNT, snapshot: raw(), review: buildReview(raw(), { uid: UID, authorizedAccountIds: [ACCOUNT] }) });
  flow.confirmBusinessReview({token, decisions: [{account_id: ACCOUNT, contact_shared: false, stop_contact: false}]});
  const stage = await flow.revalidateAndStage({
    token,
    prepared: { action: "send", touch_id: "touch-1", account_id: ACCOUNT, message: "跟进文案" },
  });
  assert.equal(stage.status, "staged");
  assert.equal(clicked, 0);
  const denied = await flow.clickAndVerify({ stageToken: stage.stageToken, markClicked: { ok: false } });
  assert.equal(denied.status, "not_clicked");
  assert.equal(clicked, 0);
  const result = await flow.clickAndVerify({ stageToken: stage.stageToken, markClicked: { ok: true, touch_id: "touch-1", state: "clicked" } });
  assert.equal(result.status, "sent");
  assert.equal(clicked, 1);
});

test("click phase waits for a stable receipt beyond the first eight page reads", async () => {
  let draft = "";
  let clicks = 0;
  let receiptReads = 0;
  let clockMs = Date.now();
  const startMs = clockMs;
  const accepted = { id: `jarvis-msg-${UID}.${ACCOUNT}.3`, side: "right", type: "TEXT",
    timestamp: 3_000, text: "跟进文案" };
  const tab = { playwright: {
    evaluate: async () => raw({ draft, messages: clicks && ++receiptReads && clockMs - startMs >= 2750
      ? [...raw().messages, accepted] : raw().messages }),
    getByPlaceholder: () => ({ count: async () => 1, fill: async value => { draft = value; } }),
    getByRole: () => ({ count: async () => 1, isEnabled: async () => true,
      click: async () => { clicks += 1; draft = ""; } }),
    domSnapshot: async () => "",
  } };
  const flow = createXhsCuaAdapter(tab, {now: () => clockMs, wait: async ms => {clockMs += ms;}});
  const review = buildReview(raw(), { uid: UID, authorizedAccountIds: [ACCOUNT] });
  const token = flow.adoptObservedSelection({ uid: UID, accountId: ACCOUNT, snapshot: raw(), review });
  flow.confirmBusinessReview({ token, decisions: [{ account_id: ACCOUNT, contact_shared: false, stop_contact: false }] });
  const stage = await flow.revalidateAndStage({ token,
    prepared: { action: "send", touch_id: "touch-delayed", account_id: ACCOUNT, message: "跟进文案" } });
  const result = await flow.clickAndVerify({ stageToken: stage.stageToken,
    markClicked: { ok: true, touch_id: "touch-delayed", state: "clicked" } });
  assert.equal(result.status, "sent");
  assert.equal(clicks, 1);
  assert.ok(receiptReads >= 10);
  assert.equal(clockMs - startMs, 2750);
});

test("a visible daily text limit toast records a platform rejection", async () => {
  let draft = "";
  let clicked = false;
  const notice = "你今天已经发送1条文字消息，请耐心等待回复~";
  const tab = {
    playwright: {
      evaluate: async () => raw({ draft }),
      getByPlaceholder: () => ({ count: async () => 1, fill: async (value) => { draft = value; } }),
      getByRole: () => ({ count: async () => 1, isEnabled: async () => true,
        click: async () => { clicked = true; draft = ""; } }),
      domSnapshot: async () => clicked ? `- alert: ${notice}` : "",
    },
  };
  const flow = createXhsCuaAdapter(tab);
  const review = buildReview(raw(), { uid: UID, authorizedAccountIds: [ACCOUNT] });
  const token = flow.adoptObservedSelection({ uid: UID, accountId: ACCOUNT, snapshot: raw(), review });
  flow.confirmBusinessReview({ token, decisions: [{ account_id: ACCOUNT, contact_shared: false, stop_contact: false }] });
  const stage = await flow.revalidateAndStage({ token,
    prepared: { action: "send", touch_id: "touch-limited", account_id: ACCOUNT, message: "跟进文案" } });
  const result = await flow.clickAndVerify({ stageToken: stage.stageToken,
    markClicked: { ok: true, touch_id: "touch-limited", state: "clicked" } });
  assert.equal(result.status, "failed");
  assert.equal(result.reason, "platform_daily_text_limit");
  assert.equal(result.detail, notice);
});

async function sendReceiptCase({ notice = "", priorNotice = "", beforeFailures = [], afterFailures = [],
  priorToasts = [], toasts = [], receiptReadError = "" } = {}) {
  let clockMs = 1800000000000;
  const startMs = clockMs;
  const snapshot = changes => raw({observedAtMs: clockMs, ...changes});
  let draft = "";
  let clicks = 0;
  const tab = { playwright: {
    evaluate: async () => snapshot({ draft, failedMessages: clicks ? afterFailures : beforeFailures,
      toasts: clicks ? toasts : priorToasts }),
    getByPlaceholder: () => ({ count: async () => 1, fill: async value => { draft = value; } }),
    getByRole: () => ({ count: async () => 1, isEnabled: async () => true,
      click: async () => { clicks += 1; draft = ""; } }),
    domSnapshot: async () => {
      if (clicks && receiptReadError) throw new Error(receiptReadError);
      return `- generic: ${clicks ? notice : priorNotice}`;
    },
  } };
  const flow = createXhsCuaAdapter(tab, {now: () => clockMs, wait: async ms => {clockMs += ms;}});
  const token = flow.adoptObservedSelection({ uid: UID, accountId: ACCOUNT, snapshot: snapshot(),
    review: buildReview(snapshot(), { uid: UID, authorizedAccountIds: [ACCOUNT] }) });
  flow.confirmBusinessReview({ token, decisions: [{ account_id: ACCOUNT, contact_shared: false, stop_contact: false }] });
  const stage = await flow.revalidateAndStage({ token,
    prepared: { action: "send", touch_id: "touch-receipt", account_id: ACCOUNT, message: "跟进文案" } });
  const result = await flow.clickAndVerify({ stageToken: stage.stageToken,
    markClicked: { ok: true, touch_id: "touch-receipt", state: "clicked" } });
  assert.equal(clicks, 1);
  if (result.reason === "receipt_not_verified") assert.equal(clockMs - startMs, 8000);
  if (result.status === "failed") assert.equal(clockMs - startMs, 0);
  return result;
}

test("privacy refusal is a failed send with the original platform notice", async () => {
  const notice = "由于对方隐私设置，你无法发送消息";
  const result = await sendReceiptCase({ notice });
  assert.equal(result.status, "failed");
  assert.equal(result.reason, "platform_privacy_restriction");
  assert.equal(result.detail, notice);
});

test("a new matching retry bubble proves send failure even when the toast disappears", async () => {
  const result = await sendReceiptCase({ afterFailures: [{ id: "jarvis-msg-1800000000331-10000001", timestamp: 1800000000331, text: "跟进文案" }] });
  assert.equal(result.status, "failed");
  assert.equal(result.reason, "platform_send_failed");
  assert.match(result.detail, /重新发送/);
});

test("a new unknown warning is preserved with the matching failed bubble", async () => {
  const notice = "该用户暂时无法接收你的消息";
  const result = await sendReceiptCase({ toasts: [{ text: notice, severity: "warning", dom_id: "notice-new" }],
    afterFailures: [{ id: "jarvis-msg-1800000000331-10000001", timestamp: 1800000000331, text: "跟进文案" }] });
  assert.equal(result.status, "failed");
  assert.equal(result.reason, "platform_send_failed");
  assert.equal(result.detail, notice);
  assert.equal(result.platform_notices[0].text, notice);
});

test("old and unrelated success toasts cannot explain a new failed bubble", async () => {
  const old = { text: "该用户暂时无法接收你的消息", severity: "warning", dom_id: "notice-old" };
  const result = await sendReceiptCase({ priorToasts: [old], toasts: [old,
    { text: "客资信息保存成功", severity: "success", dom_id: "notice-save" }],
    afterFailures: [{ id: "jarvis-msg-1800000000331-10000001", timestamp: 1800000000331, text: "跟进文案" }] });
  assert.equal(result.status, "failed");
  assert.match(result.detail, /未捕获具体/);
  assert.deepEqual(result.platform_notices, []);
});

test("a new unknown toast without a stable or failed bubble stays uncertain", async () => {
  const result = await sendReceiptCase({ toasts: [{ text: "消息处理中", severity: "warning", dom_id: "notice-new" }] });
  assert.equal(result.status, "uncertain");
  assert.equal(result.platform_notices[0].text, "消息处理中");
});

test("an old retry bubble or different failed text cannot decide the new receipt", async () => {
  const old = { id: "id_old-failure", text: "跟进文案" };
  const result = await sendReceiptCase({ beforeFailures: [old], afterFailures: [old,
    { id: "id_new-failure", text: "另外一条消息" }] });
  assert.equal(result.status, "uncertain");
  assert.equal(result.reason, "receipt_not_verified");
});

test("missing success and failure evidence remains uncertain without a resend", async () => {
  const result = await sendReceiptCase();
  assert.equal(result.status, "uncertain");
});

test("a privacy notice already on the page is not attributed to the new click", async () => {
  const notice = "由于对方隐私设置，你无法发送消息";
  const result = await sendReceiptCase({ notice, priorNotice: notice });
  assert.equal(result.status, "uncertain");
});

test("an old DOM refusal is not fresh when accessibility exposes it after the click", async () => {
  const notice = "由于对方隐私设置，你无法发送消息";
  const old = { text: notice, severity: "error", dom_id: "notice-old" };
  const result = await sendReceiptCase({ notice, priorToasts: [old], toasts: [old] });
  assert.equal(result.status, "uncertain");
});

test("a receipt read error after clicking is an uncertain result without another click", async () => {
  const result = await sendReceiptCase({ receiptReadError: "snapshot unavailable" });
  assert.equal(result.status, "uncertain");
  assert.equal(result.reason, "receipt_read_failed");
  assert.equal(result.detail, "snapshot unavailable");
});

test("an old matching failed bubble mounted after the click remains uncertain", async () => {
  const result = await sendReceiptCase({ afterFailures: [{
    id: "jarvis-msg-1700000000000-123", timestamp: 1700000000000, text: "跟进文案" }] });
  assert.equal(result.status, "uncertain");
});

test("DOM snapshot reads a failed temporary bubble separately from stable message IDs", async () => {
  const bubble = { id: "jarvis-msg-1800000000331-10000001", innerText: "跟进文案" };
  const item = { id: "id_1800000000331-10000001", closest: () => null, querySelector: () => bubble };
  const retry = { closest: () => item };
  const chat = { querySelector: () => null, getBoundingClientRect: () => ({ left: 0, right: 100, top: 0, bottom: 100 }),
    querySelectorAll: selector => selector === '[title="重新发送"]' ? [retry] : [] };
  const document = { querySelector: selector => selector === "#pro-msg-box" ? chat : null,
    querySelectorAll: () => [] };
  const tab = { playwright: { evaluate: async (fn, arg) => vm.runInNewContext(
    `(${fn.toString()})(${JSON.stringify(arg)})`, { document,
      location: { pathname: "/im/multiCustomerService" }, getComputedStyle: () => ({}) }) } };
  const snapshot = await readSnapshot(tab, UID);
  assert.equal(snapshot.messages.length, 0);
  assert.equal(snapshot.failedMessages.length, 1);
  assert.equal(snapshot.failedMessages[0].id, bubble.id);
  assert.equal(snapshot.failedMessages[0].text, "跟进文案");
  assert.equal(snapshot.failedMessages[0].timestamp, 1800000000331);
});

test("DOM snapshot captures visible top notifications without reading chat quotes or hidden notices", async () => {
  const notice = ({ text = "暂时无法发送", top = 50, hidden = false, ancestorHidden = false,
    chat = false, severity = "warning" } = {}) => ({
    id: "toast-1", className: `d-message d-message-${severity}`, innerText: text, hidden,
    closest: () => chat ? {} : null, querySelector: () => null,
    parentElement: ancestorHidden ? { hidden: true } : null,
    getBoundingClientRect: () => ({ top, bottom: top + 50, width: 400, height: 50 }),
  });
  const nodes = [notice(), notice({ text: "客资信息保存成功", severity: "success" }),
    notice({ hidden: true }), notice({ ancestorHidden: true }), notice({ top: 600 }), notice({ chat: true })];
  const document = { body: {}, querySelector: () => null,
    querySelectorAll: selector => selector.includes('[role="alert"]') ? nodes : [] };
  const tab = { playwright: { evaluate: async (fn, arg) => vm.runInNewContext(
    `(${fn.toString()})(${JSON.stringify(arg)})`, { document, innerHeight: 900,
      location: { pathname: "/im/multiCustomerService" },
      getComputedStyle: el => ({ display: el.hidden ? "none" : "block" }) }) } };
  const snapshot = await readSnapshot(tab, UID);
  assert.equal(snapshot.toasts.length, 2);
  assert.equal(snapshot.toasts[0].text, "暂时无法发送");
  assert.equal(snapshot.toasts[0].severity, "warning");
  assert.equal(snapshot.toasts[1].severity, "success");
});

test("ordinary one-account observation opens one UID search and leaves message untouched", async () => {
  let searches = 0;
  let selectedClicks = 0;
  let sends = 0;
  const tab = {
    url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService",
    click: async () => { selectedClicks += 1; },
    playwright: {
      evaluate: async (pageFunction) => String(pageFunction).includes(".search-contact-item")
        ? { entries: [{ text: "客户", dataKey: "one", absoluteY: 0, x: 10, y: 10, area: 200 }], scrollTop: 0, maxTop: 0, scrollTarget: [10, 10] }
        : raw(),
      getByPlaceholder: () => ({
        count: async () => 1,
        fill: async (value) => { if (value === UID) searches += 1; },
        pressSequentially: async () => { searches += 1; },
      }),
      getByRole: () => ({ click: async () => { sends += 1; } }),
      domSnapshot: async () => "",
    },
  };
  const observed = await createXhsCuaAdapter(tab).observeUid({
    uid: UID, authorizedAccountIds: [ACCOUNT], relevantAccountIds: [ACCOUNT], targetAccountId: ACCOUNT,
  });
  assert.equal(observed.status, "observed");
  assert.equal(observed.selected_account_id, ACCOUNT);
  assert.equal(observed.coverage_complete, true);
  assert.equal(searches, 1);
  assert.equal(selectedClicks, 1);
  assert.equal(sends, 0);
  assert.equal(observed.reviews.length, 1);
  assert.equal(observed.reviews[0].history_complete, true);
});

test("send target must be one of the reviewed authorized accounts", async () => {
  const result = await createXhsCuaAdapter({ playwright: { evaluate: async () => {} } }).observeUid({
    uid: UID, authorizedAccountIds: [ACCOUNT], relevantAccountIds: [ACCOUNT],
    targetAccountId: "other-account",
  });
  assert.equal(result.status, "deferred");
  assert.equal(result.reason, "send_target_out_of_scope");
});

test("observation waits for the selected UID panel after a search click", async () => {
  let staleReads = 0;
  const popup = { entries: [{ text: "客户", dataKey: "one", absoluteY: 0, x: 10, y: 10, area: 200 }],
    scrollTop: 0, maxTop: 0, scrollTarget: [10, 10] };
  const tab = {
    url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService",
    click: async () => { staleReads = 1; },
    playwright: {
      evaluate: async (pageFunction) => String(pageFunction).includes(".search-contact-item") ? popup :
        String(pageFunction).includes(".vue-recycle-scroller.scroller") ? [] :
          staleReads-- > 0 ? raw({ rightPanelUid: "previous-customer" }) : raw(),
      getByPlaceholder: () => ({ count: async () => 1, fill: async () => {}, pressSequentially: async () => {} }),
      domSnapshot: async () => "",
    },
  };
  const observed = await createXhsCuaAdapter(tab).observeUid({
    uid: UID, authorizedAccountIds: [ACCOUNT], relevantAccountIds: [ACCOUNT],
  });
  assert.equal(observed.status, "observed");
  assert.equal(observed.reviews[0].identity_verified, true);
});

test("observation waits for a delayed UID search result before declaring no match", async () => {
  let resultVisible = false;
  const popup = { entries: [{ text: "客户", dataKey: "one", absoluteY: 0, x: 10, y: 10, area: 200 }],
    scrollTop: 0, maxTop: 0, scrollTarget: [10, 10] };
  const tab = {
    url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService",
    click: async () => {},
    playwright: {
      evaluate: async (pageFunction) => String(pageFunction).includes(".search-contact-item")
        ? resultVisible ? popup : { ...popup, entries: [] }
        : String(pageFunction).includes(".vue-recycle-scroller.scroller") ? [] : raw(),
      getByPlaceholder: () => ({ count: async () => 1, fill: async () => {}, pressSequentially: async () => {} }),
      locator: () => ({ first: () => ({ waitFor: async () => { resultVisible = true; } }) }),
      domSnapshot: async () => "",
    },
  };
  const observed = await createXhsCuaAdapter(tab).observeUid({
    uid: UID, authorizedAccountIds: [ACCOUNT], relevantAccountIds: [ACCOUNT],
  });
  assert.equal(observed.status, "observed");
  assert.equal(observed.reviews[0].identity_verified, true);
});

test("observation defers when customer annotation fields have not loaded", async () => {
  const popup = { entries: [{ text: "客户", dataKey: "one", absoluteY: 0, x: 10, y: 10, area: 200 }],
    scrollTop: 0, maxTop: 0, scrollTarget: [10, 10] };
  const tab = {
    url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService",
    click: async () => {},
    playwright: {
      evaluate: async (pageFunction) => String(pageFunction).includes(".search-contact-item") ? popup :
        raw({ labelPresent: false }),
      getByPlaceholder: () => ({ count: async () => 1, fill: async () => {}, pressSequentially: async () => {} }),
      domSnapshot: async () => "",
    },
  };
  const observed = await createXhsCuaAdapter(tab).observeUid({
    uid: UID, authorizedAccountIds: [ACCOUNT], relevantAccountIds: [ACCOUNT],
  });
  assert.equal(observed.status, "deferred");
  assert.equal(observed.coverage_complete, false);
  assert.equal(observed.reviews[0].panel_loaded, false);
});

test("unchanged load-more with the first conversation-entry hint proves the history start", async () => {
  let clicks = 0;
  const popup = { entries: [{ text: "客户", dataKey: "one", absoluteY: 0, x: 10, y: 10, area: 200 }],
    scrollTop: 0, maxTop: 0, scrollTarget: [10, 10] };
  const messages = [
    { id: `jarvis-msg-${UID}.${ACCOUNT}.start`, side: "center", type: "HINT", timestamp: 100,
      text: "「温馨提示」有用户进入会话，为保障消费者权益，沟通等服务信息将被记录" },
    { id: `jarvis-msg-${UID}.${ACCOUNT}.next`, side: "right", type: "TEXT", timestamp: 200, text: "以前的回复" },
  ];
  const tab = {
    url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService",
    click: async () => {},
    playwright: {
      evaluate: async (pageFunction) => String(pageFunction).includes(".search-contact-item") ? popup :
        raw({ messages, loadMoreVisible: true }),
      getByPlaceholder: () => ({ count: async () => 1, fill: async () => {}, pressSequentially: async () => {} }),
      getByText: () => ({ filter: () => ({ count: async () => 1, click: async () => { clicks += 1; } }) }),
      domSnapshot: async () => "",
    },
  };
  const observed = await createXhsCuaAdapter(tab).observeUid({
    uid: UID, authorizedAccountIds: [ACCOUNT], relevantAccountIds: [ACCOUNT],
  });
  assert.equal(observed.status, "observed");
  assert.equal(observed.reviews[0].history_complete, true);
  assert.equal(clicks, 1);
});

for (const text of [
  "「温馨提示」您已进入企业员工会话，为保障消费者权益，会话信息将被员工及企业记录",
  "「温馨提示」您已进入企业会话，为保障消费者权益，会话信息将被企业记录",
]) test(`official history entry marker is recognized: ${text}`, async () => {
  let clicks = 0;
  const messages = [{ id: `jarvis-msg-${UID}.${ACCOUNT}.start`, side: "center", type: "HINT", timestamp: 100, text },
    ...raw().messages];
  const tab = asynchronousHistoryTab({ snapshot: () => raw({ messages, loadMoreVisible: true }),
    countMore: async () => 1, clickMore: async () => { clicks += 1; } });
  const result = await createXhsCuaAdapter(tab).observeUid({ uid: UID, scopeMode: "all" });
  assert.equal(result.status, "observed");
  assert.equal(result.reviews[0].history_from_start_complete, true);
  assert.equal(clicks, 1);
});

for (const [name, changes] of [
  ["customer quote", { side: "left" }],
  ["wrong kind", { type: "TEXT" }],
  ["not earliest", { timestamp: 1500 }],
  ["invalid time", { timestamp: 0 }],
  ["another account", { id: `jarvis-msg-${UID}.another-account.start` }],
  ["unverified format", { text: "温馨提示用户进入会话，为保障消费者权益，会话信息将被记录" }],
]) test(`an official-looking entry cannot prove history start: ${name}`, async () => {
  const messages = [{ id: `jarvis-msg-${UID}.${ACCOUNT}.start`, side: "center", type: "HINT", timestamp: 100,
    text: "「温馨提示」您已进入企业员工会话，为保障消费者权益，会话信息将被员工及企业记录", ...changes },
    ...raw().messages];
  const tab = asynchronousHistoryTab({ snapshot: () => raw({ messages, loadMoreVisible: true }),
    countMore: async () => 1, clickMore: async () => {} });
  const result = await createXhsCuaAdapter(tab).observeUid({ uid: UID, scopeMode: "all", budgetMs: 1400 });
  assert.equal(result.status, "deferred");
  assert.equal(result.reviews[0].history_complete, false);
});

test("a page health probe never loads history or creates a sending selection", async () => {
  let loads = 0, fills = 0;
  const tab = asynchronousHistoryTab({ snapshot: () => raw({ loadMoreVisible: true }),
    countMore: async () => 1, clickMore: async () => { loads += 1; } });
  const fill = tab.playwright.getByPlaceholder;
  tab.playwright.getByPlaceholder = placeholder => {
    if (placeholder.includes("回车键")) throw new Error("probe must never touch the draft");
    return { ...fill(placeholder), fill: async () => { fills += 1; } };
  };
  const flow = createXhsCuaAdapter(tab);
  const oldToken = flow.adoptObservedSelection({ uid: UID, accountId: ACCOUNT, snapshot: raw() });
  const result = await flow.probeUid({ uid: UID, scopeMode: "all", budgetMs: 6000 });
  assert.equal(result.status, "healthy");
  assert.equal(result.identity_verified, true);
  assert.equal(result.panel_loaded, true);
  assert.equal(result.selected_account_id, ACCOUNT);
  assert.equal(result.token, undefined);
  assert.equal(loads, 0);
  assert.equal(fills, 2);
  assert.equal((await flow.revalidateAndStage({ token: oldToken, prepared: {} })).reason, "selection_token_mismatch");
});

test("a health probe refuses a target panel with another customer's body", async () => {
  const tab = asynchronousHistoryTab({ snapshot: () => raw({ messages: raw().messages.map(message =>
    ({ ...message, id: message.id.replace(UID, "f00000000000000000000005") })) }),
    countMore: async () => 1, clickMore: async () => { throw new Error("no history loads"); } });
  const result = await createXhsCuaAdapter(tab).probeUid({ uid: UID, scopeMode: "all", budgetMs: 6000 });
  assert.equal(result.status, "deferred");
  assert.equal(result.reason, "conversation_identity_not_ready");
  assert.equal(result.identity_diagnostics.target_uid, UID);
  assert.equal(result.identity_diagnostics.right_panel_uid, UID);
  assert.ok(result.identity_diagnostics.message_identities.some(value => value.uid === "f00000000000000000000005"));
});

test("a health probe stops for a user draft without changing the query", async () => {
  let fills = 0;
  const tab = asynchronousHistoryTab({ snapshot: () => raw({ draft: "用户草稿" }),
    countMore: async () => 1, clickMore: async () => {} });
  tab.playwright.getByPlaceholder = () => ({ count: async () => 1, fill: async () => { fills += 1; } });
  const result = await createXhsCuaAdapter(tab).probeUid({ uid: UID, scopeMode: "all" });
  assert.equal(result.status, "blocked");
  assert.equal(result.reason, "user_draft_present");
  assert.equal(fills, 0);
});

for (const method of ["probeUid", "observeUid"]) test(`${method} restores annotation after a searched conversation mounts the tools tab`, async () => {
  let opened = false, annotation = false, tabClicks = 0;
  const popup = { inputValue: UID, rawCount: 1, entries: [{ text: "客户", dataKey: "one", absoluteY: 0,
    x: 10, y: 10, area: 200 }], scrollTop: 0, maxTop: 0, scrollTarget: [10, 10] };
  const current = () => raw({ panelTab: opened ? annotation ? "客资标注" : "获客工具" : "",
    rightPanelUid: annotation ? UID : "", labelPresent: annotation });
  const tab = { url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService",
    click: async () => { opened = true; },
    playwright: {
      evaluate: async fn => String(fn).includes(".search-contact-item") ? popup : current(),
      getByPlaceholder: () => ({ count: async () => 1, fill: async () => {} }),
      locator: selector => selector === ".message-header" ? { count: async () => 0 } :
        selector === "#rightPanel" ? { getByText: text => ({ count: async () => text === "客资标注" ? 1 : 0,
          click: async () => { annotation = true; tabClicks += 1; } }) } :
        { count: async () => 1, nth: () => ({ click: async () => { opened = true; } }) },
      domSnapshot: async () => "",
    } };
  const result = await createXhsCuaAdapter(tab)[method]({ uid: UID, scopeMode: "all", budgetMs: 6000 });
  assert.equal(result.status, method === "probeUid" ? "healthy" : "observed");
  assert.equal(tabClicks, 1);
  assert.equal(method === "probeUid" ? result.identity_verified : result.reviews[0].identity_verified, true);
});

test("UID result waiting avoids the CUA synthetic visible filter and trusts a fresh visible DOM result", async () => {
  let visible = false, filters = 0, waitSelectors = [], selected = 0;
  const entry = { text: "测试客户", dataKey: "one", absoluteY: 0, allIndex: 0, x: 210, y: 72, area: 14848 };
  const popup = () => ({ inputValue: UID, rawCount: visible ? 1 : 0, entries: visible ? [entry] : [],
    scrollTop: 0, maxTop: 0, scrollTarget: [210, 72] });
  const plain = { count: async () => visible ? 1 : 0,
    first: () => ({ waitFor: async () => { waitSelectors.push(".search-contact-item"); visible = true; } }),
    nth: () => ({ click: async () => { selected += 1; } }),
    filter: () => { filters += 1; return { first: () => ({ waitFor: async () => {
      waitSelectors.push(".search-contact-item >> visible=true"); throw Error("no_matches"); } }) }; } };
  const tab = { url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService",
    playwright: {
      evaluate: async fn => String(fn).includes(".search-contact-item") ? popup() : raw(),
      getByPlaceholder: () => ({ count: async () => 1, fill: async () => {} }),
      locator: selector => selector === ".message-header" ? { count: async () => 0 } : plain,
      domSnapshot: async () => "",
    } };
  const result = await createXhsCuaAdapter(tab).probeUid({ uid: UID, scopeMode: "all", budgetMs: 6000 });
  assert.equal(result.status, "healthy");
  assert.equal(filters, 0);
  assert.deepEqual(waitSelectors, [".search-contact-item"]);
  assert.equal(selected, 1);
});

test("review keeps recent contact time and diagnostic identities without accepting a mismatched body", () => {
  const good = buildReview(raw(), { uid: UID });
  assert.equal(good.last_contact_at_ms, 2000);
  const wrong = buildReview(raw({ messages: raw().messages.map(message =>
    ({ ...message, id: message.id.replace(UID, "f00000000000000000000005") })) }), { uid: UID });
  assert.equal(wrong.identity_verified, false);
  assert.equal(wrong.identity_diagnostics.active_key, `Active-${UID}-${ACCOUNT}`);
  assert.ok(wrong.identity_diagnostics.message_identities.some(value => value.uid === "f00000000000000000000005"));
});

test("history is eligible only after the load-more boundary is reached", async () => {
  let loaded = false;
  const popup = { entries: [{ text: "客户", dataKey: "one", absoluteY: 0, x: 10, y: 10, area: 200 }],
    scrollTop: 0, maxTop: 0, scrollTarget: [10, 10] };
  const tab = {
    url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService",
    click: async () => {},
    playwright: {
      evaluate: async (pageFunction) => String(pageFunction).includes(".search-contact-item") ? popup :
        raw({ loadMoreVisible: !loaded, messages: loaded ? [
          { id: `jarvis-msg-${UID}.${ACCOUNT}.0`, side: "left", type: "TEXT", timestamp: 500, text: "早期咨询" },
          ...raw().messages,
        ] : raw().messages }),
      getByPlaceholder: () => ({ count: async () => 1, fill: async () => {}, pressSequentially: async () => {} }),
      getByText: () => ({ filter: () => ({ count: async () => 1, click: async () => { loaded = true; } }) }),
      domSnapshot: async () => "",
    },
  };
  const result = await createXhsCuaAdapter(tab).observeUid({
    uid: UID, authorizedAccountIds: [ACCOUNT], relevantAccountIds: [ACCOUNT],
  });
  assert.equal(result.status, "observed");
  assert.equal(result.reviews[0].history_from_start_complete, true);
  assert.equal(result.performance.history_load_clicks, 1);
});

function asynchronousHistoryTab({snapshot, countMore, clickMore}) {
  const popup = {entries: [{text: "客户", dataKey: "one", absoluteY: 0, x: 10, y: 10, area: 200}],
    scrollTop: 0, maxTop: 0, scrollTarget: [10, 10]};
  return {url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService", click: async () => {},
    playwright: {
      evaluate: async fn => String(fn).includes(".search-contact-item") ? popup : snapshot(),
      getByPlaceholder: () => ({count: async () => 1, fill: async () => {}}),
      getByText: () => ({filter: () => ({count: countMore, click: clickMore})}),
      domSnapshot: async () => "",
    }};
}

test("history waits for an asynchronously mounted load-more control without another click", async () => {
  let mounted = false, loaded = false, clicks = 0;
  const timer = setTimeout(() => { mounted = true; }, 240);
  const tab = asynchronousHistoryTab({
    snapshot: () => raw({loadMoreVisible: !loaded}),
    countMore: async () => Number(mounted),
    clickMore: async () => { clicks += 1; loaded = true; },
  });
  try {
    const result = await createXhsCuaAdapter(tab).observeUid({uid: UID, scopeMode: "all"});
    assert.equal(result.status, "observed");
    assert.equal(result.reviews[0].history_from_start_complete, true);
    assert.equal(clicks, 1);
  } finally { clearTimeout(timer); }
});

test("history waits for asynchronously rendered older messages after one load click", async () => {
  let clicked = false, loaded = false, clicks = 0, timer;
  const older = {id: `jarvis-msg-${UID}.${ACCOUNT}.older`, side: "left", type: "TEXT", timestamp: 500,
    text: "早先留下的联系方式"};
  const tab = asynchronousHistoryTab({
    snapshot: () => raw({loadMoreVisible: !loaded, messages: loaded ? [older, ...raw().messages] : raw().messages}),
    countMore: async () => 1,
    clickMore: async () => { clicked = true; clicks += 1; timer = setTimeout(() => { loaded = true; }, 240); },
  });
  try {
    const result = await createXhsCuaAdapter(tab).observeUid({uid: UID, scopeMode: "all"});
    assert.equal(clicked, true);
    assert.equal(result.status, "observed");
    assert.ok(result.reviews[0].messages.some(message => message.id === older.id));
    assert.equal(clicks, 1);
  } finally { clearTimeout(timer); }
});

test("a temporarily hidden load control does not prove the history ended while messages load", async () => {
  let clicked = false, loaded = false, timer;
  const older = {id: `jarvis-msg-${UID}.${ACCOUNT}.older`, side: "left", type: "TEXT", timestamp: 500,
    text: "之前已加微信"};
  const tab = asynchronousHistoryTab({
    snapshot: () => raw({loadMoreVisible: !clicked, messages: loaded ? [older, ...raw().messages] : raw().messages}),
    countMore: async () => 1,
    clickMore: async () => { clicked = true; timer = setTimeout(() => { loaded = true; }, 240); },
  });
  try {
    const result = await createXhsCuaAdapter(tab).observeUid({uid: UID, scopeMode: "all"});
    assert.equal(result.status, "observed");
    assert.ok(result.reviews[0].messages.some(message => message.id === older.id));
  } finally { clearTimeout(timer); }
});

test("history settling stops immediately when the conversation identity changes", async () => {
  let changed = false, clicked = false;
  const tab = asynchronousHistoryTab({
    snapshot: () => raw({loadMoreVisible: true, rightPanelUid: changed ? "other-customer" : UID}),
    countMore: async () => { changed = true; return 0; },
    clickMore: async () => { clicked = true; },
  });
  const result = await createXhsCuaAdapter(tab).observeUid({uid: UID, scopeMode: "all"});
  assert.equal(result.status, "deferred");
  assert.equal(result.reason, "history_identity_changed");
  assert.equal(clicked, false);
});

test("history settling respects the UID deadline instead of waiting the full local window", async () => {
  const tab = asynchronousHistoryTab({snapshot: () => raw({loadMoreVisible: true}),
    countMore: async () => 0, clickMore: async () => { throw Error("missing control must not be clicked"); }});
  const start = Date.now();
  const result = await createXhsCuaAdapter(tab).observeUid({uid: UID, scopeMode: "all", budgetMs: 1300});
  assert.equal(result.status, "deferred");
  assert.ok(Date.now() - start < 1000, "settling must preserve the overall UID deadline margin");
});

test("load-more ignores a geometrically visible control in an inactive recycled customer", async () => {
  let loads = 0;
  const current = {inactive: false, click: async () => { loads += 1; }};
  const cached = {inactive: true, click: async () => { throw Error("must not click a cached customer's load control"); }};
  const buttons = [cached, current];
  const locatorFor = nodes => ({
    count: async () => nodes.length,
    click: async () => { assert.equal(nodes.length, 1); await nodes[0].click(); },
    and: other => locatorFor(nodes.filter(node => other.matches(node))),
  });
  const tab = asynchronousHistoryTab({
    snapshot: () => raw({loadMoreVisible: loads === 0, messages: loads ? [
      {id: `jarvis-msg-${UID}.${ACCOUNT}.older`, side: "left", type: "TEXT", timestamp: 500, text: "较早的咨询"},
      ...raw().messages,
    ] : raw().messages}),
    countMore: async () => 2,
    clickMore: async () => { throw Error("unscoped load control must not be clicked"); },
  });
  tab.playwright.locator = selector => {
    if (selector === ".message-header") return {count: async () => 0};
    if (selector === "#pro-msg-box") return {getByText: () => ({filter: () => locatorFor(buttons)})};
    assert.match(selector, /:not\(\[data-active="false"\]/);
    return {matches: node => !node.inactive};
  };
  const result = await createXhsCuaAdapter(tab).observeUid({uid: UID, scopeMode: "all"});
  assert.equal(result.status, "observed");
  assert.equal(result.reviews[0].history_complete, true);
  assert.equal(result.performance.history_load_clicks, 1);
  assert.equal(loads, 1);
});

test("analysis stops at forty real messages without loading older history and retains a verified sample", async () => {
  let historyClicks = 0;
  const nowMs = Date.now();
  const messages = Array.from({ length: 40 }, (_, index) => ({
    id: `jarvis-msg-${UID}.${ACCOUNT}.${index + 1}`,
    side: index % 2 ? "right" : "left", type: "TEXT",
    timestamp: nowMs - (40 - index) * 1000, text: `正文${index}`,
  }));
  const popup = { entries: [{ text: "客户", dataKey: "one", absoluteY: 0, x: 10, y: 10, area: 200 }],
    scrollTop: 0, maxTop: 0, scrollTarget: [10, 10] };
  const tab = {
    url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService",
    click: async () => {},
    playwright: {
      evaluate: async (pageFunction) => String(pageFunction).includes(".search-contact-item") ? popup :
        raw({ messages, loadMoreVisible: true }),
      getByPlaceholder: () => ({ count: async () => 1, fill: async () => {}, pressSequentially: async () => {} }),
      getByText: () => ({ filter: () => ({ count: async () => 1, click: async () => { historyClicks += 1; } }) }),
      domSnapshot: async () => "",
    },
  };
  const observation = await createXhsCuaAdapter(tab).observeUid({
    uid: UID, authorizedAccountIds: [ACCOUNT], relevantAccountIds: [ACCOUNT], analysisMode: true,
  });
  assert.equal(observation.status, "sampled");
  assert.equal(observation.reviews[0].history_complete, false);
  assert.equal(observation.reviews[0].analysis_truncated, true);
  assert.equal(historyClicks, 0);
  assert.equal(captureForAnalysis(observation, ACCOUNT).messages.length, 40);
});

test("analysis stops at the thirty-day boundary even with fewer than forty real messages", async () => {
  let historyClicks = 0;
  const nowMs = Date.now();
  const old = nowMs - 31 * 24 * 60 * 60 * 1000;
  const popup = { entries: [{ text: "客户", dataKey: "one", absoluteY: 0, x: 10, y: 10, area: 200 }],
    scrollTop: 0, maxTop: 0, scrollTarget: [10, 10] };
  const tab = {
    url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService",
    click: async () => {},
    playwright: {
      evaluate: async (pageFunction) => String(pageFunction).includes(".search-contact-item") ? popup :
        raw({ loadMoreVisible: true, messages: [
          { ...raw().messages[0], timestamp: old },
          { ...raw().messages[1], timestamp: nowMs - 1000 },
        ] }),
      getByPlaceholder: () => ({ count: async () => 1, fill: async () => {}, pressSequentially: async () => {} }),
      getByText: () => ({ filter: () => ({ count: async () => 1, click: async () => { historyClicks += 1; } }) }),
      domSnapshot: async () => "",
    },
  };
  const observation = await createXhsCuaAdapter(tab).observeUid({
    uid: UID, authorizedAccountIds: [ACCOUNT], relevantAccountIds: [ACCOUNT], analysisMode: true,
  });
  assert.equal(observation.status, "sampled");
  assert.equal(observation.reviews[0].history_complete, false);
  assert.equal(historyClicks, 0);
  assert.deepEqual(captureForAnalysis(observation, ACCOUNT).messages.map((message) => message.sender), ["staff"]);
});

test("analysis shares one forty-message budget across all conversations for the UID", async () => {
  const secondAccount = "f00000000000000000000003";
  const thirdAccount = "f00000000000000000000004";
  const nowMs = Date.now();
  let activeAccount = ACCOUNT;
  let historyClicks = 0;
  let targetHasText = true;
  const accountRaw = (accountId, loadMoreVisible) => raw({
    activeKey: `Active-${UID}-${accountId}`,
    messages: Array.from({ length: 30 }, (_, index) => ({
      id: `jarvis-msg-${UID}.${accountId}.${index + 1}`,
      side: index % 2 ? "right" : "left",
      type: accountId === thirdAccount && !targetHasText ? "CARD" : "TEXT",
      timestamp: nowMs - (30 - index) * 1000,
      text: `账号${accountId.slice(-2)}消息${index}`,
    })),
    loadMoreVisible,
  });
  const popup = { entries: [
    { text: "账号一", dataKey: "first", absoluteY: 0, x: 10, y: 10, area: 200 },
    { text: "账号二", dataKey: "second", absoluteY: 30, x: 20, y: 20, area: 200 },
    { text: "账号三", dataKey: "third", absoluteY: 60, x: 30, y: 30, area: 200 },
  ], scrollTop: 0, maxTop: 0, scrollTarget: [10, 10] };
  const tab = {
    url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService",
    click: async ([x]) => { activeAccount = x === 10 ? ACCOUNT : x === 20 ? secondAccount : thirdAccount; },
    playwright: {
      evaluate: async (pageFunction) => String(pageFunction).includes(".search-contact-item") ? popup :
        String(pageFunction).includes(".vue-recycle-scroller.scroller") ? [] :
          accountRaw(activeAccount, activeAccount === secondAccount),
      getByPlaceholder: () => ({ count: async () => 1, fill: async () => {}, pressSequentially: async () => {} }),
      getByText: () => ({ filter: () => ({ count: async () => 1, click: async () => { historyClicks += 1; } }) }),
      domSnapshot: async () => "",
    },
  };
  const observation = await createXhsCuaAdapter(tab).observeUid({
    uid: UID, authorizedAccountIds: [ACCOUNT, secondAccount, thirdAccount],
    relevantAccountIds: [ACCOUNT, secondAccount, thirdAccount], analysisMode: true,
  });
  assert.equal(observation.status, "sampled");
  assert.deepEqual(observation.reviews.map((review) => review.messages.length), [30, 10, 0]);
  assert.equal(observation.reviews[1].history_complete, false);
  assert.equal(observation.reviews[1].analysis_truncated, true);
  assert.equal(observation.reviews[2].history_complete, false);
  assert.equal(observation.reviews[2].analysis_truncated, true);
  assert.equal(historyClicks, 0);
  assert.equal([ACCOUNT, secondAccount, thirdAccount].reduce((count, accountId) =>
    count + captureForAnalysis(observation, accountId).messages.length, 0), 40);

  activeAccount = ACCOUNT;
  const focused = await createXhsCuaAdapter(tab).observeUid({
    uid: UID, authorizedAccountIds: [ACCOUNT, secondAccount, thirdAccount],
    relevantAccountIds: [ACCOUNT, secondAccount, thirdAccount],
    analysisMode: true, targetAccountId: thirdAccount,
  });
  assert.equal(focused.status, "sampled");
  assert.deepEqual(focused.reviews.map((review) => review.messages.length), [0, 0, 30]);
  assert.equal(focused.selected_account_id, thirdAccount);
  assert.equal(historyClicks, 0);

  targetHasText = false;
  activeAccount = ACCOUNT;
  const noTargetChat = await createXhsCuaAdapter(tab).observeUid({
    uid: UID, authorizedAccountIds: [ACCOUNT, secondAccount, thirdAccount],
    relevantAccountIds: [ACCOUNT, secondAccount, thirdAccount],
    analysisMode: true, targetAccountId: thirdAccount,
  });
  assert.equal(noTargetChat.status, "deferred");
  assert.equal(noTargetChat.reason, "analysis_target_chat_unavailable");
});

test("failed post-fill recheck clears only its own unchanged draft in the same conversation", async () => {
  let filled = "";
  const writes = [];
  let page = raw();
  const tab = {
    playwright: {
      evaluate: async () => ({ ...page, draft: filled }),
      getByPlaceholder: () => ({ count: async () => 1, fill: async (value) => {
        writes.push(value); filled = value;
        if (value) page = raw({ labelValue: "无意向" });
      } }),
    },
  };
  const flow = createXhsCuaAdapter(tab);
  const review = buildReview(raw(), { uid: UID, authorizedAccountIds: [ACCOUNT] });
  const token = flow.adoptObservedSelection({ uid: UID, accountId: ACCOUNT, snapshot: raw(), review });
  flow.confirmBusinessReview({token, decisions: [{account_id: ACCOUNT, contact_shared: false, stop_contact: false}]});
  const result = await flow.revalidateAndStage({ token,
    prepared: { action: "send", touch_id: "touch-clean", account_id: ACCOUNT, message: "跟进文案" } });
  assert.equal(result.status, "not_staged");
  assert.deepEqual(writes, ["跟进文案", ""]);
  assert.equal(filled, "");
});

test("preflight keeps proved older customer history when the virtual list drops it", async () => {
  const full = raw();
  let page = raw({messages: [full.messages.at(-1)]});
  let draft = "";
  const tab = {playwright: {
    evaluate: async () => ({...page, draft}),
    getByPlaceholder: () => ({count: async () => 1, fill: async value => {draft = value;}}),
  }};
  const flow = createXhsCuaAdapter(tab);
  const review = buildReview(full, {uid: UID, authorizedAccountIds: [ACCOUNT]});
  const token = flow.adoptObservedSelection({uid: UID, accountId: ACCOUNT, snapshot: full, review});
  flow.confirmBusinessReview({token, decisions: [{account_id: ACCOUNT, contact_shared: false, stop_contact: false}]});
  const prepared = {action: "send", touch_id: "touch-sparse-history", account_id: ACCOUNT, message: "跟进文案"};
  const staged = await flow.revalidateAndStage({token, prepared});
  assert.equal(staged.status, "staged");
  assert.equal(draft, prepared.message);

  draft = "";
  page = raw({messages: [...full.messages, {
    id: `jarvis-msg-${UID}.${ACCOUNT}.3`, side: "left", type: "TEXT", timestamp: 3000, text: "新回复",
  }]});
  const changed = await flow.revalidateAndStage({token, prepared});
  assert.equal(changed.status, "not_staged");
  assert.equal(changed.reason, "page_or_customer_changed");
});

test("post-fill identity or draft changes preserve the field and return uncertainty", async () => {
  for (const change of ["identity", "draft"]) {
    let filled = "";
    const writes = [];
    let page = raw();
    const tab = {
      playwright: {
        evaluate: async () => ({ ...page, draft: filled }),
        getByPlaceholder: () => ({ count: async () => 1, fill: async (value) => {
          writes.push(value); filled = value;
          if (value && change === "identity") page = raw({ rightPanelUid: "other" });
          if (value && change === "draft") filled = "用户改写了草稿";
        } }),
      },
    };
    const flow = createXhsCuaAdapter(tab);
    const review = buildReview(raw(), { uid: UID, authorizedAccountIds: [ACCOUNT] });
    const token = flow.adoptObservedSelection({ uid: UID, accountId: ACCOUNT, snapshot: raw(), review });
    flow.confirmBusinessReview({token, decisions: [{account_id: ACCOUNT, contact_shared: false, stop_contact: false}]});
  const result = await flow.revalidateAndStage({ token,
      prepared: { action: "send", touch_id: "touch-preserve", account_id: ACCOUNT, message: "跟进文案" } });
    assert.equal(result.status, "uncertain");
    assert.deepEqual(writes, ["跟进文案"]);
    assert.ok(filled);
  }
});

test("all-account search discovers both conversations without a configured account list", async () => {
  let current = ACCOUNT;
  const second = "new-account";
  const clicks = [];
  const popup = {entries: [ACCOUNT, second].map((account, i) => ({text: account, dataKey: account,
    absoluteY: i * 30, x: 10, y: 10+i*30, area: 200})), scrollTop:0, maxTop:0, scrollTarget:[10,10]};
  const page = () => raw({activeKey:`Active-${UID}-${current}`, messages:raw().messages.map((m) =>
    ({...m,id:m.id.replace(ACCOUNT,current)}))});
  const tab = {url:async()=>"https://pro.xiaohongshu.com/im/multiCustomerService",
    click:async([x,y])=>{ current = y===10 ? ACCOUNT : second; clicks.push(current); },
    playwright:{evaluate:async(fn)=>String(fn).includes(".search-contact-item") ? popup : page(),
      getByPlaceholder:()=>({count:async()=>1,fill:async()=>{},pressSequentially:async()=>{}}),
      domSnapshot:async()=>""}};
  const flow = createXhsCuaAdapter(tab);
  const result = await flow.observeUid({uid:UID,scopeMode:"all",authorizedAccountIds:[],relevantAccountIds:[]});
  assert.equal(result.status,"observed");
  assert.equal(result.search_complete,true);
  assert.deepEqual(result.discovered_account_ids,[ACCOUNT,second]);
  assert.deepEqual(clicks,[ACCOUNT,second]);
  assert.equal(result.selected_account_id,second);
  const reviews = flow.confirmBusinessReview({token:result.token, decisions:[ACCOUNT,second].map((account_id)=>
    ({account_id,contact_shared:false,stop_contact:false}))});
  assert.ok(reviews.every((r)=>r.business_reviewed));
});

test("same UID reopens the popup after a selected result and clicks each indexed result", async () => {
  const second = "second-account";
  let current = ACCOUNT;
  let popupOpen = false;
  let selectedOnce = false;
  let blurred = false;
  const events = [];
  const popup = { entries: [ACCOUNT, second].map((account, allIndex) => ({
    text: account, dataKey: account, allIndex, absoluteY: allIndex * 30,
    x: 10, y: 10 + allIndex * 30, area: 200,
  })), scrollTop: 0, maxTop: 0, scrollTarget: [10, 10] };
  const page = () => raw({ activeKey: `Active-${UID}-${current}`,
    messages: raw().messages.map((message) => ({ ...message, id: message.id.replace(ACCOUNT, current) })) });
  const input = {
    count: async () => 1,
    click: async () => { events.push("focus"); },
    press: async (key) => { events.push(key); },
    pressSequentially: async () => {
      events.push("type");
      popupOpen = !selectedOnce || blurred;
    },
    fill: async (value) => { events.push(`fill-${value}`); popupOpen = !!value && (!selectedOnce || blurred); },
  };
  const tab = {
    url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService",
    click: async () => { throw Error("coordinate click is unsafe for popup results"); },
    playwright: {
      evaluate: async (fn) => String(fn).includes(".search-contact-item")
        ? { ...popup, entries: popupOpen ? popup.entries : [] } : page(),
      getByPlaceholder: () => input,
      locator: (selector) => selector === ".message-header"
        ? { count: async () => 1, first: () => ({ click: async () => { blurred = true; events.push("blur"); } }) }
        : { count: async () => popupOpen ? 2 : 0,
          nth: (index) => ({ click: async () => {
            assert.equal(popupOpen, true);
            current = [ACCOUNT, second][index];
            events.push(`select-${index}`);
            selectedOnce = true;
            popupOpen = false;
            blurred = false;
          } }),
          first: () => ({ waitFor: async () => { throw Error("popup did not reopen"); } }) },
      domSnapshot: async () => "",
    },
  };
  const result = await createXhsCuaAdapter(tab).observeUid({ uid: UID, scopeMode: "all" });
  assert.equal(result.status, "observed");
  assert.equal(result.search_complete, true);
  assert.deepEqual(result.discovered_account_ids, [ACCOUNT, second]);
  assert.deepEqual(events.filter((event) => event.startsWith("select-")), ["select-0", "select-1"]);
  assert.ok(events.indexOf("blur") < events.indexOf("select-1"));
  assert.ok(events.includes("fill-"));
  assert.ok(events.includes(`fill-${UID}`));
  assert.equal(result.performance.search_opens, 2);
});

test("missing search popup is a technical failure, never a completed empty search", async () => {
  const popup = {entries:[],scrollTop:0,maxTop:0,scrollTarget:[0,0]};
  const tab = {url:async()=>"https://pro.xiaohongshu.com/im/multiCustomerService",
    playwright:{evaluate:async(fn)=>String(fn).includes(".search-contact-item") ? popup : raw(),
      getByPlaceholder:()=>({count:async()=>1,fill:async()=>{},pressSequentially:async()=>{}}),
      locator:()=>({first:()=>({waitFor:async()=>{throw Error("timeout");}})}),domSnapshot:async()=>""}};
  const result = await createXhsCuaAdapter(tab).observeUid({uid:UID,scopeMode:"all"});
  assert.equal(result.status,"deferred");
  assert.equal(result.reason,"search_result_not_confirmed");
  assert.equal(result.search_complete,false);
});

test("UID search submits with Enter when typing alone does not show results", async () => {
  let popupOpen = false;
  let selected = false;
  const pressed = [];
  const popup = { entries: [{ text: "客户", dataKey: "", absoluteY: 0, x: 10, y: 10, area: 200 }],
    scrollTop: 0, maxTop: 0, scrollTarget: [10, 10] };
  const tab = {
    url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService",
    click: async () => { selected = true; popupOpen = false; },
    playwright: {
      evaluate: async (fn) => String(fn).includes(".search-contact-item")
        ? { ...popup, entries: popupOpen ? popup.entries : [] } : raw(),
      getByPlaceholder: () => ({ count: async () => 1, fill: async () => {},
        click: async () => {}, pressSequentially: async () => {},
        press: async (key) => { pressed.push(key); if (key === "Enter") popupOpen = true; } }),
      domSnapshot: async () => "",
    },
  };
  const result = await createXhsCuaAdapter(tab).observeUid({ uid: UID, scopeMode: "all" });
  assert.equal(result.status, "observed");
  assert.equal(selected, true);
  assert.ok(pressed.includes("Enter"));
});

test("search fills the whole UID, resets focus, and waits past a transient empty result", async () => {
  let value = "previous-uid", elapsed = 0, blurred = false, clicked = false;
  const events = [];
  const popup = () => ({ inputValue: value, rawCount: elapsed >= 2800 ? 1 : 0,
    emptyText: elapsed < 2800 ? "暂无匹配的用户" : "", loading: false,
    entries: elapsed >= 2800 ? [{ text: "客户", dataKey: "one", allIndex: 0,
      absoluteY: 0, x: 10, y: 10, area: 200 }] : [],
    scrollTop: 0, maxTop: 0, scrollTarget: [10, 10] });
  const tab = { url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService",
    playwright: {
      evaluate: async (fn) => String(fn).includes(".search-contact-item") ? popup() : raw(),
      getByPlaceholder: () => ({ count: async () => 1,
        fill: async (text) => { assert.equal(blurred, true); value = text; events.push(text); },
        press: async (key) => events.push(key),
        pressSequentially: async () => { throw Error("slow per-character search must not be used"); } }),
      locator: (selector) => selector === ".message-header"
        ? { count: async () => 1, first: () => ({ click: async () => { blurred = true; } }) }
        : { count: async () => 1,
          first: () => ({ waitFor: async ({timeoutMs}) => { elapsed += timeoutMs;
            if (elapsed < 2800) throw Error("timeout"); } }),
          nth: () => ({ click: async () => { clicked = true; } }) },
      domSnapshot: async () => "",
    } };
  const result = await createXhsCuaAdapter(tab).observeUid({uid:UID, scopeMode:"all"});
  assert.equal(result.status, "observed");
  assert.equal(clicked, true);
  assert.deepEqual(events.slice(0, 2), ["", UID]);
  assert.ok(result.search_diagnostics?.length);
});

test("search does not press Enter when direct filling already produces results", async () => {
  let value = "", enters = 0;
  const popup = () => ({ inputValue: value, rawCount: value === UID ? 1 : 0,
    entries: value === UID ? [{ text:"客户", dataKey:"one", absoluteY:0, x:10,y:10,area:200 }] : [],
    scrollTop:0,maxTop:0,scrollTarget:[0,0] });
  const tab = {url:async()=>"https://pro.xiaohongshu.com/im/multiCustomerService",click:async()=>{},
    playwright:{evaluate:async(fn)=>String(fn).includes(".search-contact-item")?popup():raw(),
      getByPlaceholder:()=>({count:async()=>1,fill:async(v)=>{value=v;},
        press:async(k)=>{if(k==="Enter"){enters++;value="";}},pressSequentially:async()=>{}}),
      domSnapshot:async()=>""}};
  const result=await createXhsCuaAdapter(tab).observeUid({uid:UID,scopeMode:"all"});
  assert.equal(result.status,"observed");
  assert.equal(enters,0);
});

test("search result overlay remains selectable outside a slightly scrollable page wrapper", async () => {
  const wrapper = {
    parentElement: null, scrollHeight: 613, clientHeight: 603, scrollTop: 0,
    getBoundingClientRect: () => ({ left: 276, top: 134, right: 1628, bottom: 737 }),
  };
  const entry = {
    parentElement: wrapper, innerText: "客户", textContent: "客户",
    getAttribute: () => `Active-${UID}-${ACCOUNT}`,
    getBoundingClientRect: () => ({ left: 82, top: 43, right: 338, bottom: 101 }),
  };
  const document = {
    body: {},
    querySelectorAll: (selector) => selector === ".search-contact-item" ? [entry] : [],
  };
  wrapper.parentElement = document.body;
  let clicks = 0;
  const tab = {
    url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService",
    click: async () => { throw Error("popup must use its locator"); },
    playwright: {
      evaluate: async (pageFunction) => String(pageFunction).includes(".search-contact-item")
        ? vm.runInNewContext(`(${pageFunction.toString()})()`, {
          document, innerWidth: 1700, innerHeight: 900,
          getComputedStyle: () => ({ overflowY: "auto" }),
        }) : raw(),
      getByPlaceholder: () => ({ count: async () => 1, fill: async () => {}, pressSequentially: async () => {} }),
      locator: () => ({ count: async () => 1, nth: () => ({ click: async () => { clicks += 1; } }) }),
      domSnapshot: async () => "",
    },
  };
  const observed = await createXhsCuaAdapter(tab).observeUid({
    uid: UID, scopeMode: "all", authorizedAccountIds: [], relevantAccountIds: [],
  });
  assert.equal(observed.status, "observed");
  assert.equal(clicks, 1);
});

test("search ignores hidden stale nodes even when their rectangles overlap the viewport", async () => {
  const body = {};
  const hiddenParent = {parentElement: body, getAttribute: () => "", scrollHeight: 500, clientHeight: 100,
    getBoundingClientRect: () => ({left: 580, top: 40, right: 900, bottom: 140})};
  const makeEntry = (parentElement, name) => ({parentElement, innerText: name, textContent: name,
    getAttribute: key => key === "data-key" ? name : "",
    getBoundingClientRect: () => ({left: 80, top: 40, right: 340, bottom: 100})});
  const entries = [makeEntry(hiddenParent, "旧查询客户"), makeEntry(body, "当前客户")];
  entries[0].getBoundingClientRect = () => ({left: 600, top: 40, right: 860, bottom: 100});
  const document = {body, querySelectorAll: selector => selector === ".search-contact-item" ? entries : []};
  const clicked = [];
  const tab = {url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService",
    playwright: {
      evaluate: async fn => String(fn).includes(".search-contact-item")
        ? vm.runInNewContext(`(${fn.toString()})()`, {document, innerWidth: 1000, innerHeight: 800,
          getComputedStyle: node => ({visibility: node === hiddenParent ? "hidden" : "visible", overflowY: "auto"})}) : raw(),
      getByPlaceholder: () => ({count: async () => 1, fill: async () => {}}),
      locator: selector => selector === ".message-header" ? {count: async () => 0} : {
        count: async () => entries.length, nth: index => ({click: async () => {clicked.push(index);}})},
      domSnapshot: async () => "",
    }};
  const observed = await createXhsCuaAdapter(tab).observeUid({uid: UID, scopeMode: "all"});
  assert.equal(observed.status, "observed");
  assert.deepEqual(clicked, [1]);
  assert.equal(observed.search_diagnostics[0].raw_count, 2);
  assert.equal(observed.search_diagnostics[0].visible_count, 1);
});

test("a searched conversation uses its visible messages when no contact-list row is active", async () => {
  const row = (name, value) => ({ querySelector: (selector) => selector === ".info-item-title"
    ? { textContent: name } : selector === ".info-item-value-wrap"
      ? { innerText: value, querySelector: () => null } : null });
  const panel = {
    innerText: "客资标注", querySelectorAll: (selector) => selector === ".info-item"
      ? [row("归因笔记", "-"), row("客资标签", "请选择线索标签")]
      : selector === ".id-group" ? [{ querySelector: (key) => key === ".id-label"
        ? { textContent: "UID" } : key === ".ellipsis" ? { textContent: UID } : null }]
        : selector === ".d-tabs-header-label" ? [{ textContent: "客资标注", classList: { contains: () => true } }] : [],
  };
  const message = (uid, y) => ({
    id: `jarvis-msg-${uid}.${ACCOUNT}.1`, className: "left", innerText: "想了解课程",
    getAttribute: (key) => key === "data-msg-type" ? "TEXT" : key === "data-timestamp" ? "1000" : "",
    querySelector: () => null,
    getBoundingClientRect: () => ({ left: 700, right: 900, top: y, bottom: y + 40 }),
  });
  const document = {
    querySelector: (selector) => selector === "#rightPanel" ? panel : selector === ".message-header"
      ? { innerText: "客户", querySelectorAll: () => [] } : selector === "#pro-msg-box"
        ? { getBoundingClientRect: () => ({ left: 565, right: 1271, top: 182, bottom: 593 }) } : null,
    querySelectorAll: (selector) => selector === '[id^="jarvis-msg-"]'
      ? [message("other-user", 10428), message(UID, 250)]
      : selector === "input" ? [{ getAttribute: () => "输入用户ID/小红书ID" }] : [],
  };
  const tab = { playwright: { evaluate: async (pageFunction, arg) => vm.runInNewContext(
    `(${pageFunction.toString()})(${JSON.stringify(arg)})`,
    { document, location: { pathname: "/im/multiCustomerService" }, getComputedStyle: () => ({}) },
  ) } };
  const snapshot = await readSnapshot(tab, UID);
  const review = buildReview(snapshot, { uid: UID, authorizedAccountIds: [ACCOUNT] });
  assert.equal(review.identity_verified, true);
  assert.equal(review.account_id, ACCOUNT);
});

test("opt-out tagging requires an actual customer quote; ordinary refusals are not automatic labels", () => {
  const page=raw({messages:[...raw().messages,{id:`jarvis-msg-${UID}.${ACCOUNT}.3`,side:"left",type:"TEXT",
    timestamp:3000,text:"请不要再联系我"}]});
  const flow=createXhsCuaAdapter({playwright:{evaluate:async()=>page}});
  const token=flow.adoptObservedSelection({uid:UID,accountId:ACCOUNT,snapshot:page});
  assert.throws(()=>flow.confirmBusinessReview({token,decisions:[{account_id:ACCOUNT,contact_shared:false,
    stop_contact:true,evidence:"编造的拒绝"}]}),/customer_quote_required/);
  const reviews=flow.confirmBusinessReview({token,decisions:[{account_id:ACCOUNT,contact_shared:false,
    stop_contact:true,evidence:"请不要再联系我"}]});
  assert.equal(reviews[0].negative_status,"blocked");
  assert.equal(reviews[0].stop_contact_evidence,"请不要再联系我");
});

test("popup scrolling never escapes the search component into the page wrapper", async () => {
  const outer = { scrollHeight: 613, clientHeight: 603, scrollTop: 0,
    getBoundingClientRect: () => ({ left: 276, top: 80, right: 1446, bottom: 683 }) };
  const search = { parentElement: outer, scrollHeight: 72, clientHeight: 72,
    getBoundingClientRect: () => ({ left: 292, top: 90, right: 548, bottom: 162 }) };
  const entry = { parentElement: search, closest: () => search, innerText: "客户", textContent: "客户",
    getAttribute: () => "", getBoundingClientRect: () => ({ left: 82, top: 43, right: 338, bottom: 101 }) };
  const document = { body: {}, querySelectorAll: () => [entry] };
  outer.parentElement = document.body;
  let clicks = 0;
  const tab = {
    url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService",
    scroll: async () => { throw Error("must not scroll the page to exhaust one search result"); },
    playwright: {
      evaluate: async (fn) => String(fn).includes(".search-contact-item")
        ? vm.runInNewContext(`(${fn.toString()})()`, { document, innerWidth: 1700, innerHeight: 900,
          getComputedStyle: (el) => ({ overflowY: el === outer ? "auto" : "visible" }) }) : raw(),
      getByPlaceholder: () => ({ count: async () => 1, fill: async () => {}, pressSequentially: async () => {} }),
      locator: () => ({ count: async () => 1, nth: () => ({ click: async () => { clicks += 1; } }) }),
      domSnapshot: async () => "",
    },
  };
  const result = await createXhsCuaAdapter(tab).observeUid({ uid: UID, scopeMode: "all" });
  assert.equal(result.search_complete, true);
  assert.equal(clicks, 1);
});

function historyTab(page, { scroll = async () => {}, load = async () => {} } = {}) {
  const popup = { entries: [{ text: "客户", dataKey: "one", absoluteY: 0, x: 10, y: 10, area: 200 }],
    scrollTop: 0, maxTop: 0, scrollTarget: [10, 10] };
  return {
    url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService", click: async () => {}, scroll,
    playwright: { evaluate: async (fn) => String(fn).includes(".search-contact-item") ? popup :
      String(fn).includes(".vue-recycle-scroller.scroller") ? [] : page(),
      getByPlaceholder: () => ({ count: async () => 1, fill: async () => {}, pressSequentially: async () => {} }),
      getByText: () => ({ filter: () => ({ count: async () => 1, click: load }) }),
      domSnapshot: async () => "" },
  };
}

test("virtual history must scroll older even when load-more is not mounted", async () => {
  let scrolls = 0;
  const older = { ...raw().messages[0], id: `jarvis-msg-${UID}.${ACCOUNT}.older`, timestamp: 500,
    text: "别再联系我" };
  const tab = historyTab(() => raw({
    messages: scrolls ? [older, raw().messages[0]] : raw().messages,
    historyViewport: { olderRemaining: scrolls === 0, scrollTop: scrolls * 500, maxTop: 500, target: [800, 300] },
  }), { scroll: async () => { scrolls += 1; } });
  const result = await createXhsCuaAdapter(tab).observeUid({ uid: UID, scopeMode: "all" });
  assert.equal(scrolls, 1);
  assert.equal(result.reviews[0].history_complete, true);
  assert.equal(result.reviews[0].messages.length, 3);
  assert.ok(result.reviews[0].messages.some(m => m.text === "别再联系我"));
});

test("mounted load control below the viewport must finish scrolling to the older edge", async () => {
  let scrolls = 0;
  const older = {...raw().messages[0], id:`jarvis-msg-${UID}.${ACCOUNT}.older`, timestamp:500, text:'别再联系我'};
  const tab = historyTab(() => raw({loadMoreVisible:scrolls === 0, loadMoreTarget:null,
    messages:scrolls ? [older,...raw().messages] : raw().messages,
    historyViewport:{olderRemaining:scrolls === 0,scrollTop:scrolls ? 500 : 470,maxTop:500,target:[800,300]}
  }),{scroll:async()=>{scrolls += 1;}});
  tab.playwright.getByText = () => ({filter:()=>({count:async()=>0})});
  const result=await createXhsCuaAdapter(tab).observeUid({uid:UID,scopeMode:'all'});
  assert.equal(result.status,'observed');
  assert.ok(result.reviews[0].messages.some(m=>m.text==='别再联系我'));
});

test("unchanged virtual node count is progress when older message IDs change", async () => {
  let loads = 0;
  const page = () => raw({ loadMoreVisible: loads < 2,
    messages: loads === 0 ? raw().messages : [
      { ...raw().messages[0], id: `jarvis-msg-${UID}.${ACCOUNT}.older${loads}`, timestamp: 500 - loads,
        text: loads === 2 ? "微信已加过了" : "以前咨询" }, raw().messages[1],
    ] });
  const result = await createXhsCuaAdapter(historyTab(page, { load: async () => { loads += 1; } }))
    .observeUid({ uid: UID, scopeMode: "all" });
  assert.equal(result.status, "observed");
  assert.equal(loads, 2);
  assert.ok(result.reviews[0].messages.some(m => m.text === "微信已加过了"));
});

test("a recovered identity read does not leave a permanently unresolved search candidate", async () => {
  let clicks = 0;
  const tab = historyTab(() => clicks === 1 ? raw({ rightPanelUid: "previous-customer" }) : raw());
  tab.click = async () => { clicks += 1; };
  const result = await createXhsCuaAdapter(tab).observeUid({ uid: UID, scopeMode: "all" });
  assert.equal(result.status, "observed");
  assert.equal(result.search_complete, true);
  assert.equal(clicks, 2);
});

test("an unmounted load control does not imply complete history away from the oldest edge", () => {
  const review = buildReview(raw({ historyViewport: { olderRemaining: true } }), { uid: UID });
  assert.equal(review.history_complete, false);
});

test("DOM snapshot excludes inactive recycled messages and messages for another UID", async () => {
  const element = (uid, active) => ({ id: `jarvis-msg-${uid}.${ACCOUNT}.${active}`,
    className: "left", innerText: "旧缓存",
    closest: () => ({ getAttribute: () => active }), querySelector: () => null,
    getAttribute: name => name === "data-msg-type" ? "TEXT" : "1000" });
  const document = { querySelector: () => null,
    querySelectorAll: selector => selector === '[id^="jarvis-msg-"]'
      ? [element(UID, "true"), element(UID, "false"), element("other-user", "true")] : [] };
  const tab = { playwright: { evaluate: async (fn, arg) => vm.runInNewContext(
    `(${fn.toString()})(${JSON.stringify(arg)})`,
    { document, location: { pathname: "/im/multiCustomerService" }, getComputedStyle: () => ({}) }) } };
  const snapshot = await readSnapshot(tab, UID);
  assert.equal(snapshot.messages.length, 1);
  assert.equal(snapshot.messages[0].id, `jarvis-msg-${UID}.${ACCOUNT}.true`);
  assert.equal(snapshot.messageIdentities.length, 2);
  assert.ok(snapshot.messageIdentities.some(message => message.uid === "other-user"));
});

test("a stalled history scroll stays incomplete and reports the concrete failure", async () => {
  const result = await createXhsCuaAdapter(historyTab(() => raw({
    historyViewport: { olderRemaining: true, scrollTop: 0, maxTop: 500, target: [800, 300] },
  }))).observeUid({ uid: UID, scopeMode: "all" });
  assert.equal(result.status, "deferred");
  assert.equal(result.reason, "history_scroll_stalled");
  assert.equal(result.reviews[0].history_complete, false);
  assert.equal(result.token, "");
});

test("history restores the newest edge while preserving older protective evidence", async () => {
  let atOlder = false;
  const directions = [];
  const older = { ...raw().messages[0], id: `jarvis-msg-${UID}.${ACCOUNT}.old`, timestamp: 500,
    text: "不要再发了" };
  const tab = historyTab(() => raw({
    messages: atOlder ? [older, raw().messages[0]] : raw().messages,
    historyViewport: { olderRemaining: !atOlder, newerRemaining: atOlder,
      scrollTop: atOlder ? 500 : 0, maxTop: 500, target: [800, 300] },
  }), { scroll: async (_target, direction) => { directions.push(direction); atOlder = direction === "up"; } });
  const result = await createXhsCuaAdapter(tab).observeUid({ uid: UID, scopeMode: "all" });
  assert.equal(result.status, "observed");
  assert.deepEqual(directions, ["up", "down"]);
  assert.equal(result.reviews[0].messages.length, 3);
  assert.ok(result.reviews[0].messages.some(m => m.text === "不要再发了"));
});

test("identity still missing after one recovery stays deferred without unlimited reopening", async () => {
  let clicks = 0;
  const tab = historyTab(() => raw({ rightPanelUid: "previous-customer" }));
  tab.click = async () => { clicks += 1; };
  const result = await createXhsCuaAdapter(tab).observeUid({ uid: UID, scopeMode: "all" });
  assert.equal(result.status, "deferred");
  assert.equal(result.reason, "conversation_identity_not_ready");
  assert.equal(clicks, 2);
  assert.equal(result.token, "");
});

for (const execution of ["manual", "batch", "manual_uid_once"]) test(`${execution}: real CLI ledger preserves explicit account_once and defaults to one UID touch`, async () => {
  const fs = await import("node:fs/promises");
  const {tmpdir} = await import("node:os");
  const {join,dirname} = await import("node:path");
  const {fileURLToPath} = await import("node:url");
  const {spawnSync} = await import("node:child_process");
  const directory = await fs.mkdtemp(join(tmpdir(),"xhs-workflow-"));
  const scripts = dirname(fileURLToPath(import.meta.url));
  const db = join(directory,"test.sqlite3");
  const cli = (script,args,input) => {
    const result=spawnSync("python3",[join(scripts,script),"--db",db,...args],
      {input:input===undefined?undefined:JSON.stringify(input),encoding:"utf8"});
    assert.equal(result.status,0,result.stderr+result.stdout);
    return JSON.parse(result.stdout);
  };
  try {
    const csv=join(directory,"historical.csv");
    await fs.writeFile(csv,"用户ID,用户昵称,归属账号,最新进线时间,最近开口时间,最近留资时间,手机号,微信号,留资方式,用户类型\n"+
      `${UID},客户,未配置的旧账号,,,,,,,普通用户\n`);
    const imported=cli("daily_import.py",["--file",csv]);
    const run=cli("daily_followup_state.py",["register-run","--input-json","-"],{
      scope_mode:"all",account_ids:[],source_import_ids:imported.source_import_ids,
      ...(execution === "manual_uid_once" ? {} : {touch_policy:"account_once"}),
      messages:{ad:"同一跟进原文",natural:"同一跟进原文"}});
    const second="another-live-account";
    let current=ACCOUNT;
    let draft="";
    const histories=new Map([ACCOUNT,second].map((account)=>[account,raw().messages.map((m)=>({...m,id:m.id.replace(ACCOUNT,account)}))]));
    const sent=[];
    const page=()=>raw({activeKey:`Active-${UID}-${current}`,notePresent:false,
      messages:histories.get(current).map((m)=>({...m})),draft,loadMoreVisible:sent.includes(current)});
    const popup={entries:[ACCOUNT,second].map((account,i)=>({text:account,dataKey:account,
      absoluteY:i*30,x:10,y:10+i*30,area:200})),scrollTop:0,maxTop:0,scrollTarget:[10,10]};
    const tab={url:async()=>"https://pro.xiaohongshu.com/im/multiCustomerService",
      click:async([x,y])=>{current=y===10?ACCOUNT:second;},
      playwright:{evaluate:async(fn)=>String(fn).includes(".search-contact-item")?popup:page(),
        getByPlaceholder:(placeholder)=>({count:async()=>1,
          fill:async(value)=>{if(placeholder.includes("回车键"))draft=value;},pressSequentially:async()=>{}}),
        getByRole:()=>({count:async()=>1,isEnabled:async()=>true,click:async()=>{
          sent.push(current); histories.get(current).push({id:`jarvis-msg-${UID}.${current}.server-${sent.length}`,
            side:"right",type:"TEXT",timestamp:Date.now(),text:draft});draft="";
        }}),domSnapshot:async()=>""}};
    const flow=createXhsCuaAdapter(tab);
    if (execution === "batch") {
      const {createXhsFollowupWorkflow}=await import("./xhs_followup_workflow.mjs");
      const {createXhsLocalLedger,createFileProgressStore}=await import("./xhs_followup_local_bridge.mjs");
      const ledger=createXhsLocalLedger({scriptPath:join(scripts,"daily_followup_state.py"),dbPath:db,writesEnabled:true});
      const progressStore=createFileProgressStore({path:join(directory,"progress.json"),runId:run.run_id,writesEnabled:true});
      let workflow=createXhsFollowupWorkflow({adapter:flow,ledger,run,progressStore,sendEnabled:true,batchMode:true,sessionId:"batch-test"});
      const first=await workflow.runSlice();
      assert.equal(first.pending_review_count,1);
      assert.deepEqual(sent,[]);
      const [packet]=await workflow.reviewPackets();
      const savedText=await fs.readFile(join(directory,"progress.json"),"utf8");
      assert.equal(savedText.includes("我想了解课程"),false);
      assert.ok(JSON.parse(savedText).pending_reviews[UID].packet.reference);
      await workflow.submitReviews([{uid:UID,evidence_id:packet.evidence_id,decisions:[ACCOUNT,second].map(account_id=>
        ({account_id,contact_shared:false,stop_contact:false}))}]);
      assert.deepEqual(sent,[]);
      assert.equal((await ledger.pendingTouches()).items.length,0);
      // Rebind a fresh adapter: durable review evidence is not a browser token.
      workflow=createXhsFollowupWorkflow({adapter:createXhsCuaAdapter(tab),ledger,run,progressStore,
        sendEnabled:true,batchMode:true,sessionId:"batch-test"});
      const resumed=await workflow.runSlice();
      assert.equal(resumed.results[0].status,"processed",JSON.stringify(resumed));
      assert.deepEqual(sent,[second,ACCOUNT]);
      assert.equal((await ledger.report({run_id:run.run_id})).sent_this_run,2);
      assert.equal((await workflow.runSlice()).results.length,0);
      assert.equal((await ledger.pendingTouches()).items.length,0);
      return;
    }
    let observed=await flow.observeUid({uid:UID,scopeMode:"all"});
    assert.equal(observed.status,"observed");
    observed.reviews=flow.confirmBusinessReview({token:observed.token,decisions:[ACCOUNT,second].map((account_id)=>
      ({account_id,contact_shared:false,stop_contact:false}))});
    for (const account of [second,ACCOUNT]) {
      if(observed.selected_account_id!==account) observed=await flow.selectReviewedAccount({token:observed.token,accountId:account});
      assert.equal(observed.status,"observed");
      const prepared=cli("daily_followup_state.py",["prepare-touch","--input-json","-"],{
        run_id:run.run_id,user_id:UID,selected_account_id:account,reviews:observed.reviews,
        search_complete:observed.search_complete,discovered_account_ids:observed.discovered_account_ids});
      if (execution === "manual_uid_once" && sent.length === 1) {
        assert.equal(prepared.action,"skip");
        assert.deepEqual(sent,[second]);
        continue;
      }
      assert.equal(prepared.action,"send");
      assert.equal(prepared.source,"unknown");
      const stage=await flow.revalidateAndStage({token:observed.token,prepared});
      assert.equal(stage.status,"staged");
      const markClicked=cli("daily_followup_state.py",["mark-clicked","--touch-id",prepared.touch_id]);
      const receipt=await flow.clickAndVerify({stageToken:stage.stageToken,markClicked});
      assert.equal(receipt.status,"sent");
      cli("daily_followup_state.py",["finish-touch","--input-json","-"],{
        touch_id:prepared.touch_id,status:receipt.status,verified_at_ms:receipt.sent_at_ms,detail:receipt.message_id});
    }
    assert.deepEqual(sent,execution === "manual_uid_once" ? [second] : [second,ACCOUNT]);
    assert.equal(cli("daily_followup_state.py",["report","--run-id",run.run_id]).sent_this_run,
      execution === "manual_uid_once" ? 1 : 2);
    assert.deepEqual(cli("daily_followup_state.py",["queue","--run-id",run.run_id]).actionable_items,[]);
  } finally {
    await fs.rm(directory,{recursive:true,force:true});
  }
});

function popupLocatorFailureTab({count = 0, changeOnFailure = false} = {}) {
  let changed = false;
  let clicked = false;
  const coordinates = [];
  const entry = {text:'客户',dataKey:'target',allIndex:0,absoluteY:0,x:100,y:80,area:200};
  return {
    coordinates,
    tab: {
      url: async () => 'https://pro.xiaohongshu.com/im/multiCustomerService',
      click: async point => {coordinates.push(point); clicked = true;},
      playwright: {
        evaluate: async fn => String(fn).includes('.search-contact-item')
          ? {inputValue:UID,entries:clicked ? [] : [{...entry,dataKey:changed ? 'other' : 'target'}],rawCount:1,scrollTop:0,maxTop:0,scrollTarget:[100,80]}
          : raw(),
        getByPlaceholder: () => ({count:async()=>1,fill:async()=>{}}),
        locator: selector => selector === '.message-header' ? {count:async()=>0}
          : {count:async()=>{changed=changeOnFailure;return count;},
             nth:()=>({click:async()=>{changed=changeOnFailure;throw Error('selector deadline exceeded');}})},
        domSnapshot:async()=>'',
      },
    },
  };
}

test('visible DOM search result remains usable when locator count falsely returns zero', async () => {
  const {tab,coordinates}=popupLocatorFailureTab();
  const result=await createXhsCuaAdapter(tab).observeUid({uid:UID,scopeMode:'all'});
  assert.equal(result.status,'observed');
  assert.equal(result.search_complete,true);
  assert.deepEqual(coordinates,[[100,80]]);
});

test('failed locator click can open an unchanged freshly verified popup via native input', async () => {
  const {tab,coordinates}=popupLocatorFailureTab({count:1});
  const result=await createXhsCuaAdapter(tab).observeUid({uid:UID,scopeMode:'all'});
  assert.equal(result.status,'observed');
  assert.deepEqual(coordinates,[[100,80]]);
});

test('popup changing during locator failure prevents native fallback from clicking another user', async () => {
  const {tab,coordinates}=popupLocatorFailureTab({count:1,changeOnFailure:true});
  const result=await createXhsCuaAdapter(tab).observeUid({uid:UID,scopeMode:'all'});
  assert.equal(result.status,'deferred');
  assert.deepEqual(coordinates,[]);
});

test('visible verified history control loads older messages despite false locator count', async () => {
  let loaded=false,clicks=0;
  const older={id:`jarvis-msg-${UID}.${ACCOUNT}.older`,side:'left',type:'TEXT',timestamp:500,text:'之前已加微信'};
  const tab=asynchronousHistoryTab({snapshot:()=>raw({loadMoreVisible:!loaded,loadMoreTarget:loaded?null:[100,80],messages:loaded?[older,...raw().messages]:raw().messages}),countMore:async()=>0,clickMore:async()=>{throw Error('unavailable locator');}});
  tab.click=async point=>{if(point[0]===100){clicks++;loaded=true;}};
  const result=await createXhsCuaAdapter(tab).observeUid({uid:UID,scopeMode:'all'});
  assert.equal(result.status,'observed');
  assert.ok(result.reviews[0].messages.some(m=>m.id===older.id));
  assert.equal(clicks,1);
});

test('history native fallback refuses a changed customer identity', async () => {
  let changed=false,clicks=0;
  const tab=asynchronousHistoryTab({snapshot:()=>raw({rightPanelUid:changed?'other-customer':UID,loadMoreVisible:true,loadMoreTarget:[100,80]}),countMore:async()=>{changed=true;return 0;},clickMore:async()=>{}});
  tab.click=async point=>{if(point[0]===100)clicks++;};
  const result=await createXhsCuaAdapter(tab).observeUid({uid:UID,scopeMode:'all'});
  assert.equal(result.status,'deferred');
  assert.equal(result.reason,'history_identity_changed');
  assert.equal(clicks,0);
});

for (const method of ["ensurePage", "probeUid"]) {
  test(`${method} uses the same visible annotation tab and preserves detach errors`, async () => {
    let annotation = false;
    const popup = {inputValue: UID, rawCount: 1, entries: [{text: "客户", dataKey: "one", absoluteY: 0,
      x: 10, y: 10, area: 200}], scrollTop: 0, maxTop: 0, scrollTarget: [10,10]};
    const tab = {url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService", click: async () => {},
      playwright: {
        evaluate: async fn => String(fn).includes(".search-contact-item") ? popup
          : raw({panelTab: annotation ? "客资标注" : "获客工具"}),
        getByPlaceholder: () => ({count: async () => 1, fill: async () => {}}),
        locator: selector => selector === ".message-header" ? {count: async () => 0}
          : selector === "#rightPanel" ? {getByText: () => ({count: async () => 2,
            filter: () => ({count: async () => 1, click: async () => {annotation = true;}})})}
          : {count: async () => 1, nth: () => ({click: async () => {}})},
        domSnapshot: async () => "",
      }};
    const result = await createXhsCuaAdapter(tab)[method]({uid: UID, scopeMode: "all", budgetMs: 6000});
    assert.equal(method === "ensurePage" ? result.ok : result.status, method === "ensurePage" ? true : "healthy");
    annotation = false;
    tab.playwright.locator = selector => selector === ".message-header" ? {count: async () => 0}
      : selector === "#rightPanel" ? {getByText: () => ({count: async () => 1,
        click: async () => {throw new Error("Debugger unattached");},
        filter: () => ({count: async () => 1, click: async () => {throw new Error("Debugger unattached");}})})}
      : {count: async () => 1, nth: () => ({click: async () => {}})};
    await assert.rejects(createXhsCuaAdapter(tab)[method]({uid: UID, scopeMode: "all", budgetMs: 6000}), /Debugger unattached/);
  });
}

test("DOM snapshot excludes hidden measurement copies but retains real offscreen history", async () => {
  const element = (id, style, text) => ({id, className: "left", innerText: text, style,
    closest: () => null, querySelector: () => null,
    getAttribute: key => key === "data-msg-type" ? "TEXT" : key === "data-timestamp" ? "1000" : ""});
  const document = {querySelector: () => null, querySelectorAll: selector => selector === '[id^="jarvis-msg-"]'
    ? [element(`jarvis-msg-${UID}.${ACCOUNT}.real`, {}, "你好"),
      element(`jarvis-msg-${UID}.${ACCOUNT}.measure`, {visibility: "hidden"}, "不要发了")]
    : []};
  const tab = {playwright: {evaluate: async (fn,arg) => vm.runInNewContext(`(${fn.toString()})(${JSON.stringify(arg)})`,
    {document, location: {pathname: "/im/multiCustomerService"}, getComputedStyle: el => el.style || {}})}};
  const snapshot = await readSnapshot(tab, UID);
  assert.equal(snapshot.messages.length, 1);
  assert.equal(snapshot.messages[0].text, "你好");
  assert.equal(snapshot.messageIdentities.length, 1);
});

test("observation reports stage durations and approved history boundary reuse", async () => {
  const page = raw({loadMoreVisible: true});
  const result = await createXhsCuaAdapter(asynchronousHistoryTab({snapshot: () => page,
    countMore: async () => 1, clickMore: async () => assert.fail("cached boundary must avoid older loading")}))
    .observeUid({uid: UID, scopeMode: "all", historyCache: {[ACCOUNT]: {boundary_message_id: page.messages[1].id}}});
  assert.equal(result.status, "observed");
  assert.equal(result.performance.history_cache_hits, 1);
  assert.ok(result.performance.history_read_ms >= 0);
  assert.ok(result.performance.page_prepare_ms >= 0);
  assert.ok(result.performance.search_ms >= 0);
  assert.ok(result.performance.identity_read_ms >= 0);
  assert.equal(result.performance.history_load_clicks, 0);
});

test("receipt connection failure retains uncertainty with a global connection classification", async () => {
  const result = await sendReceiptCase({receiptReadError: "Debugger unattached"});
  assert.equal(result.status, "uncertain");
  assert.equal(result.reason, "browser_connection_unavailable");
});

test("popup locator detach cannot use native fallback to mask the disconnected controller", async () => {
  const {tab} = popupLocatorFailureTab();
  const originalLocator = tab.playwright.locator;
  tab.playwright.locator = selector => selector === ".search-contact-item" ? {
    count: async () => 1, nth: () => ({click: async () => {throw new Error("Debugger unattached");}})
  } : originalLocator(selector);
  await assert.rejects(createXhsCuaAdapter(tab).probeUid({uid: UID, scopeMode: "all", budgetMs: 6000}), /Debugger unattached/);
});

function annotationFailureTab(failure) {
  let opened = false, clicks = 0, searches = 0;
  const popup = {inputValue: UID, rawCount: 1, entries: [{text: "客户", dataKey: "one", absoluteY: 0,
    x: 10, y: 10, area: 200}], scrollTop: 0, maxTop: 0, scrollTarget: [10,10]};
  const annotation = {count: async () => failure === "missing" ? 0 : 1,
    click: async () => {clicks++;}};
  annotation.filter = () => annotation;
  return {counts: () => ({clicks, searches}), tab: {
    url: async () => "https://pro.xiaohongshu.com/im/multiCustomerService",
    click: async () => {opened = true;}, playwright: {
      evaluate: async fn => String(fn).includes(".search-contact-item") ? popup
        : raw({panelTab: opened ? "获客工具" : ""}),
      getByPlaceholder: () => ({count: async () => 1, fill: async value => {if (value === UID) searches++;}}),
      locator: selector => selector === ".message-header" ? {count: async () => 0}
        : selector === "#rightPanel" ? {getByText: text => text === "客资标注" ? annotation
          : {waitFor: async () => {}}}
        : {count: async () => 1, nth: () => ({click: async () => {opened = true;}})},
      domSnapshot: async () => "",
    }}};
}

for (const failure of ["missing", "no_switch"]) {
  for (const method of ["probeUid", "observeUid"]) {
    test(`${method} preserves annotation ${failure} failure despite verified UID identity`, async () => {
      const {tab, counts} = annotationFailureTab(failure);
      const result = await createXhsCuaAdapter(tab)[method]({uid: UID, scopeMode: "all", budgetMs: 6000});
      assert.equal(result.status, "blocked");
      assert.equal(result.reason, failure === "missing" ? "customer_annotation_tab_missing" : "customer_annotation_tab_not_restored");
      assert.equal(result.token || "", "");
      assert.equal(counts().clicks, failure === "missing" ? 0 : 1);
      assert.equal(counts().searches, 1);
    });
  }
  test(`real adapter annotation ${failure} failure stops a read-only workflow batch`, async () => {
    const {createXhsFollowupWorkflow} = await import("./xhs_followup_workflow.mjs");
    const {tab, counts} = annotationFailureTab(failure);
    const flow = createXhsFollowupWorkflow({run: {run_id: "annotation-failure-test", scope_mode: "all"},
      adapter: createXhsCuaAdapter(tab)});
    const result = await flow.runBatch([{user_id: UID}, {user_id: "another-uid"}]);
    assert.equal(result.results.length, 1);
    assert.equal(result.diagnostic.status, "blocked");
    assert.equal(result.diagnostic.reason, failure === "missing" ? "customer_annotation_tab_missing" : "customer_annotation_tab_not_restored");
    assert.equal(counts().searches, 1);
  });
}
