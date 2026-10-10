import { createHash } from "node:crypto";

// This module deliberately has no browser bootstrapping or subprocess access. In
// cua_repl: `const { createXhsCuaAdapter } = await import('file:///.../xhs_followup_cua_adapter.mjs')`.
// Pass the already selected CUA Tab. evaluate() below only reads DOM; every UI
// mutation uses a documented CUA action (locator, click, scroll, or goto).

const ROUTE = "/im/multiCustomerService";
const SEARCH_PLACEHOLDER = "输入用户ID/小红书ID";
const REPLY_PLACEHOLDER = '按 "回车键" 发送信息...（输入/唤起快捷回复）';
const NEGATIVE = /别再?联系|不要再?联系|别再?发|不要再?发|停止联系|勿扰|退订/; // Candidate hints only; operator confirms meaning.
const PEER = /(?:我是|我们是|这边是|主营|提供|专做|也做).{0,18}(?:代运营|广告|投放|投流|接广|互推|SaaS|软件|系统|培训|课程|招商|代理|服务商|获客服务|询盘服务)|(?:代运营|广告投放|投流|接广|互推|SaaS|软件系统|培训讲师|招商代理|服务商).{0,18}(?:合作|推广|报价|资源|服务)|(?:好巧|我是|我们是|同为).{0,8}同行|(?:^|[，。！？\s])同行(?:$|[，。！？\s])/i;
const PROFILE_PEER = /(?:企业出海服务|海外增长|跨境增长|出海拓客|获客|询盘|代运营|服务商|招商|代理|接广|互推|SaaS|CRM|ERP|软件系统)/i;
const PROTECTIVE_LEAD = /对方已提交信息|访客联系方式已自动记录|(?<!未)已添加企微|(?<!无)已提交留资卡/;
const CONTACT = /(?:1[3-9]\d[\d\s\-*]{7,}\d)|(?:微信(?:号|账号)?\s*[:：]?\s*[A-Za-z0-9_-]*\*+[A-Za-z0-9_-]*)|(?:手机(?:号|号码)?\s*[:：]?\s*1\d{2}\*{2,}\d{2,})/;
const UNREADABLE = new Set(["IMAGE", "PICTURE", "VIDEO", "FILE"]);
const NON_TEXT = new Set(["CARD", "HINT", "RICH_HINT", "IMAGE", "PICTURE", "VIDEO", "FILE"]);
const KNOWN_MESSAGE_TYPES = new Set(["TEXT", "GENERAL", ...NON_TEXT]);

const connectionFailure = error => /Debugger unattached|browser (?:has )?disconnected|browser connection (?:closed|lost)/i.test(String(error?.message || error));

async function restoreAnnotationTab(tab, deadlineAt) {
  const candidate = tab.playwright.locator?.("#rightPanel")?.getByText?.("客资标注", {exact: true});
  const visible = candidate?.filter ? candidate.filter({visible: true}) : candidate;
  if (!visible || await visible.count() !== 1) return {ok: false, reason: "customer_annotation_tab_missing"};
  try {
    await visible.click({timeoutMs: Math.max(1, Math.min(1800, deadlineAt - Date.now()))});
    await tab.playwright.domSnapshot();
    return {ok: true};
  } catch (error) {
    if (connectionFailure(error)) throw error;
    return {ok: false, reason: "customer_annotation_tab_not_restored", detail: String(error?.message || error)};
  }
}

function norm(value) {
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

export function normalizeMessageForHash(value) {
  return norm(value);
}

function hashMessage(value) {
  return createHash("sha256").update(normalizeMessageForHash(value), "utf8").digest("hex");
}

function parseKey(key) {
  const match = String(key || "").match(/^(?:Total|Active|Favorite)-([^-]+)(?:-(.+))?$/);
  return match ? { uid: match[1], accountId: match[2] || "" } : { uid: "", accountId: "" };
}

function parseMessageAccount(id, uid) {
  const parts = String(id || "").replace(/^jarvis-msg-/, "").split(".");
  if (parts.length < 3) return "";
  if (parts[0] === uid) return parts[1];
  if (parts[1] === uid) return parts[0];
  return "";
}

function identityDiagnostics(raw, uid, accountId = "") {
  const active = parseKey(raw?.activeKey);
  const messageIdentities = raw?.messageIdentities || (raw?.messages || []).map(message => {
    const [first = "", second = ""] = String(message.id || "").replace(/^jarvis-msg-/, "").split(".");
    const reversed = second === uid || (!!active.accountId && first === active.accountId);
    return { id: String(message.id || ""), uid: reversed ? second : first,
      account_id: reversed ? first : second, identity_tokens: [first, second] };
  });
  return {
    target_uid: String(uid || ""), right_panel_uid: String(raw?.rightPanelUid || ""),
    target_account_id: String(accountId || active.accountId || ""), active_key: String(raw?.activeKey || ""),
    active_uid: active.uid, active_account_id: active.accountId, header: norm(raw?.headerText).slice(0, 200),
    message_identities: messageIdentities.slice(0, 12),
    matching_message_count: (raw?.messages || []).filter(message =>
      parseMessageAccount(message.id, uid) && (!accountId || parseMessageAccount(message.id, uid) === accountId)).length,
    panel_tab: String(raw?.panelTab || ""), label_present: raw?.labelPresent === true,
  };
}

const SEND_REFUSALS = [
  ["platform_privacy_restriction", /由于对方隐私设置[，,]?\s*你无法发送消息/],
  ["platform_daily_text_limit", /你今天已经发送\s*1\s*条文字消息[，,]?\s*请耐心等待回复[~～]?/],
];

function toastKey(notice) {
  return [notice.source || "dom", notice.dom_id || "", norm(notice.text)].join("|");
}

function relevantReceiptNotices(notices) {
  return (notices || []).filter(notice => norm(notice.text) &&
    notice.severity !== "success" && !/客资信息保存成功/.test(notice.text));
}

function isTextMessage(message) {
  const kind = String(message.type || "").toUpperCase();
  return ["left", "right"].includes(message.side) &&
    !!norm(message.text) &&
    (kind === "TEXT" || (kind === "GENERAL" && (message.plainText === true ||
      (message.side === "right" && message.extracted_text_complete === true && message.decorative_media_only === true)))) &&
    !/对方关注或回复你之前，?24小时内最多只能发1条文字消息/.test(message.text);
}

function isEmptyAttribution(value) {
  return ["", "-", "—", "暂无", "无", "暂无归因笔记"].includes(norm(value));
}

function hasExactLabel(value, label) {
  return norm(value).split(/[、,，;；|\s]+/).includes(label);
}

function newBoundary(messages) {
  return [...messages]
    .filter((m) => m.id && !/^jarvis-msg-\d{13}[-.]/.test(m.id))
    .sort((a, b) => Number(b.timestamp || 0) - Number(a.timestamp || 0))[0]?.id || "";
}

export function buildReview(raw, {
  uid,
  authorizedAccountIds = [],
  accountId = "",
  historyComplete,
  historyFromStartComplete,
  historyReachedPriorBoundary = false,
  cachedOutboundTextHashes = [],
  automatedMessageIds = [],
  analysisMode = false,
  analysisMessageLimit = 40,
  nowMs = Date.now(),
} = {}) {
  const key = parseKey(raw?.activeKey);
  const expectedUid = String(uid || "");
  const allForUid = (raw?.messages || []).map((message) => ({
    ...message,
    account_id: parseMessageAccount(message.id, expectedUid),
    timestamp: Number(message.timestamp || 0),
  })).filter((message) => message.account_id);
  const accountCandidates = [...new Set(allForUid.map((message) => message.account_id))];
  const resolvedAccount = key.accountId || (accountCandidates.length === 1 ? accountCandidates[0] : "");
  const targetAccount = accountId || resolvedAccount;
  const messages = allForUid
    .filter((message) => message.account_id === targetAccount)
    .sort((a, b) => a.timestamp - b.timestamp || a.id.localeCompare(b.id));
  const hasMatchingMessage = messages.length > 0;
  const identity = raw?.routeReady === true && !!expectedUid &&
    raw?.rightPanelUid === expectedUid &&
    key.uid === expectedUid &&
    !!targetAccount &&
    (!key.accountId || key.accountId === targetAccount) &&
    hasMatchingMessage;
  const panelLoaded = identity && raw?.panelTab === "客资标注" &&
    raw?.labelPresent === true;
  const badges = (raw?.leadBadges || []).map(norm);
  const badgeLead = badges.includes("留客资");
  const rightLead = hasExactLabel(raw?.labelValue, "留客资");
  const rightNoIntent = hasExactLabel(raw?.labelValue, "无意向");
  const label = !panelLoaded
    ? "unknown" : (badgeLead || rightLead) ? "lead" : rightNoIntent ? "no_intent" : "none";
  const note = norm(raw?.noteValue);
  const source = !panelLoaded || !raw?.notePresent ? "unknown" : isEmptyAttribution(note) ? "natural" : "ad";
  const attributionState = !panelLoaded || !raw?.notePresent ? "unknown" : isEmptyAttribution(note) ? "empty" : "concrete";
  const real = messages.filter(isTextMessage);
  const last = real.at(-1);
  const customer = real.filter((message) => message.side === "left");
  const outbound = real.filter((message) => message.side === "right");
  const customerText = customer.map((message) => message.text).join(" ");
  const unreadable = messages.some((message) => message.side === "left" &&
    (UNREADABLE.has(String(message.type || "").toUpperCase()) || /\[图片\]/.test(message.text || "")));
  const unknownMessageType = messages.some((message) => ["left", "right"].includes(message.side) &&
    (!KNOWN_MESSAGE_TYPES.has(String(message.type || "").toUpperCase()) ||
      (String(message.type || "").toUpperCase() === "GENERAL" && message.plainText !== true &&
        !(message.side === "right" && message.extracted_text_complete === true && message.decorative_media_only === true))));
  const invalidMessageTime = messages.some((message) => ["left", "right"].includes(message.side) &&
    (!Number.isFinite(message.timestamp) || message.timestamp <= 0 || message.timestamp > nowMs + 5 * 60 * 1000));
  const fromStart = historyFromStartComplete ?? (raw?.loadMoreVisible === false &&
    !raw?.historyViewport?.olderRemaining && messages.length > 0);
  const reliableHistory = !raw?.messageTextTruncated;
  const complete = (historyComplete ?? (fromStart || historyReachedPriorBoundary)) && reliableHistory;
  const outgoingHashes = complete
    ? [...new Set([...cachedOutboundTextHashes, ...outbound.map((message) => hashMessage(message.text))])]
    : [];
  const automated = new Set(automatedMessageIds);
  const manualOutgoing = automated.size
    ? outbound.filter((message) => !automated.has(message.id))
    : [];
  const analysisCutoff = nowMs - 30 * 24 * 60 * 60 * 1000;
  const boundedAnalysisLimit = Math.max(0, Math.min(40, Number(analysisMessageLimit) || 0));
  const selectedMessages = (analysisMode
    ? boundedAnalysisLimit > 0
      ? real.filter((message) => message.timestamp >= analysisCutoff).slice(-boundedAnalysisLimit)
      : []
    : messages);
  return {
    user_id: expectedUid,
    account_id: targetAccount,
    observed_at_ms: Number(raw?.observedAtMs || nowMs),
    identity_verified: identity,
    identity_diagnostics: identityDiagnostics(raw, expectedUid, targetAccount),
    panel_loaded: panelLoaded,
    label,
    no_intent_label: rightNoIntent,
    business_reviewed: false,
    needs_visual_review: unreadable || unknownMessageType,
    source,
    attribution_note_state: attributionState,
    source_evidence: panelLoaded ? note.slice(0, 300) : "",
    history_complete: !!(identity && complete),
    history_from_start_complete: !!(identity && fromStart && reliableHistory),
    history_reached_prior_boundary: !!(identity && historyReachedPriorBoundary && reliableHistory),
    analysis_truncated: !!(analysisMode && raw?.analysisTruncated),
    history_boundary_message_id: identity ? newBoundary(messages) : "",
    outbound_text_hashes: identity && complete ? outgoingHashes : [],
    reply_status: !identity || !complete || !last || !customer.length ? "unknown" :
      last.side === "right" ? "awaiting_customer" : "awaiting_us",
    negative_status: !identity || !complete || unreadable ? "unknown" : NEGATIVE.test(customerText) ? "blocked" : "safe",
    party_status: !identity || !complete || !customer.length ? "unknown" : PEER.test(customerText) ? "peer" :
      PROFILE_PEER.test(norm(raw?.headerText)) ? "unknown" : "customer",
    draft_present: !!norm(raw?.draft),
    last_outbound_text_at_ms: outbound.length ? outbound.at(-1).timestamp : null,
    last_customer_at_ms: customer.length ? customer.at(-1).timestamp : null,
    last_contact_at_ms: real.length ? real.at(-1).timestamp : null,
    manual_outbound_at_ms: manualOutgoing.length ? manualOutgoing.at(-1).timestamp : null,
    platform_lead_signal: !!(identity && (raw?.contactFieldsPresent ||
      PROTECTIVE_LEAD.test([raw?.panelText || "", messages.filter((m) => m.side === "center")
        .map((m) => m.text).join(" ")].join(" ")))),
    protective_lead_signal: false,
    contact_shared_hint: CONTACT.test(customerText) || /已经加|加过了|加好了/.test(customerText),
    messages: selectedMessages.map(({ id, account_id, side, type, plainText, extracted_text_complete,
      decorative_media_only, timestamp, text }) => ({
      id, account_id, side, type, plainText, extracted_text_complete: extracted_text_complete === true,
      decorative_media_only: decorative_media_only === true, timestamp, text: String(text || ""),
    })),
    authorized_for_send: authorizedAccountIds.includes(targetAccount),
  };
}

export function chooseSendingAccount(reviews, { authorizedAccountIds = [], lastSenderAccountId = "" } = {}) {
  const order = new Map(authorizedAccountIds.map((id, index) => [id, index]));
  const candidates = (reviews || []).filter((review) =>
    review.identity_verified && order.has(review.account_id));
  candidates.sort((a, b) =>
    Number(b.last_customer_at_ms || 0) - Number(a.last_customer_at_ms || 0) ||
    Number(b.account_id === lastSenderAccountId) - Number(a.account_id === lastSenderAccountId) ||
    order.get(a.account_id) - order.get(b.account_id));
  return candidates[0]?.account_id || "";
}

export function verifyNewOutbound(before, after, message, uid, accountId) {
  if (!before || !after || !message) return { ok: false, reason: "missing_verification_input" };
  const verifiedAfter = buildReview(after, { uid, accountId, authorizedAccountIds: [accountId] });
  if (!verifiedAfter.identity_verified) return { ok: false, reason: "identity_changed_after_click" };
  const oldIds = new Set((before.messages || []).map((m) => m.id));
  const failedIds = new Set((after.failedMessages || []).map((m) => m.id));
  const oldMax = Math.max(0, ...(before.messages || []).map((m) => Number(m.timestamp || 0)));
  const expected = norm(message);
  const matched = (after.messages || []).find((candidate) => {
    if (candidate.side !== "right" || !candidate.id || oldIds.has(candidate.id) || failedIds.has(candidate.id)) return false;
    if (Number(candidate.timestamp || 0) <= oldMax || norm(candidate.text) !== expected) return false;
    const idAccount = parseMessageAccount(candidate.id, uid);
    // A timestamp-only ID can be the browser's optimistic bubble. It is not
    // evidence that the server accepted the message.
    return idAccount === accountId && !/^jarvis-msg-\d{13}[-.]/.test(candidate.id);
  });
  return matched
    ? { ok: true, reason: "verified_new_right_message", message_id: matched.id, sent_at_ms: Number(matched.timestamp) }
    : { ok: false, reason: "new_right_message_not_verified" };
}

export function captureForAnalysis(observation, accountId) {
  const review = (observation?.reviews || []).find((item) => item.account_id === accountId);
  if (!review?.identity_verified || !review.panel_loaded) return null;
  return {
    user_id: review.user_id,
    account_id: review.account_id,
    uid_verified: true,
    account_verified: true,
    captured_at_ms: Math.max(Number(observation.finished_at_ms || 0),
      ...(observation.reviews || []).map((item) => Number(item.observed_at_ms || 0))),
    history_complete: review.history_complete,
    source: review.source,
    label: review.label,
    attribution_note_state: review.attribution_note_state,
    account_reviews: (observation.reviews || []).map((item) => ({
      user_id: item.user_id,
      account_id: item.account_id,
      uid_verified: !!item.identity_verified,
      account_verified: !!item.identity_verified,
      panel_loaded: !!item.panel_loaded,
      label: item.label,
      source: item.source,
      attribution_note_state: item.attribution_note_state,
      observed_at_ms: item.observed_at_ms,
    })),
    messages: (review.messages || [])
      .filter((message) => ["left", "right"].includes(message.side) && isTextMessage(message))
      .slice(-40)
      .map((message) => ({
        at_ms: Number(message.timestamp || 0),
        sender: message.side === "left" ? "customer" : "staff",
        text: message.text,
      })),
  };
}

function percentile(values, fraction) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.max(0, Math.ceil(sorted.length * fraction) - 1)];
}

export async function readSnapshot(tab, uid, { messageTextLimit = null, timeoutMs = 10000 } = {}) {
  return tab.playwright.evaluate(({ expectedUid, messageTextLimit }) => {
    const text = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const panel = document.querySelector("#rightPanel");
    const active = document.querySelector(".sx-contact-item.active[data-key]");
    const header = document.querySelector(".message-header");
    const input = document.querySelector('textarea.reply-textarea');
    const info = (name) => {
      if (!panel) return { present: false, value: "" };
      const row = [...panel.querySelectorAll(".info-item")].find((el) =>
        text(el.querySelector(".info-item-title")?.textContent) === name);
      const valueNode = row?.querySelector(".info-item-value-wrap") ||
        row?.querySelector(":scope > span:not(.info-item-title)");
      const value = text(valueNode?.innerText);
      const loading = valueNode?.querySelector('.d-skeleton,.ant-skeleton,.loading,[aria-busy="true"]');
      return { present: !!valueNode && !!value && !loading, value };
    };
    const note = info("归因笔记");
    const label = info("客资标签");
    const uidNode = [...(panel?.querySelectorAll(".id-group") || [])].find((el) =>
      text(el.querySelector(".id-label")?.textContent).replace(/[:：]/g, "").toUpperCase() === "UID");
    const rightPanelUid = text(uidNode?.querySelector(".ellipsis")?.textContent);
    const tabLabels = [...(panel?.querySelectorAll(".d-tabs-header-label") || [])];
    const selectedTab = tabLabels.find((el) => el.classList.contains("bold"))?.textContent;
    const badgeRoot = active || header;
    const leadBadges = [...(badgeRoot?.querySelectorAll(".d-tag-content") || [])]
      .map((el) => text(el.textContent)).filter(Boolean);
    const chat = document.querySelector("#pro-msg-box");
    const activeMessageElements = [...(chat?.querySelectorAll?.('[id^="jarvis-msg-"]') ||
      document.querySelectorAll('[id^="jarvis-msg-"]'))].filter((el) => {
      if (el.closest?.('[data-active]')?.getAttribute("data-active") === "false") return false;
      // Hidden measurement copies are not chat evidence. Geometry alone is
      // insufficient: real older history may legitimately be offscreen.
      for (let node = el; node && node !== document.body; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse" ||
            node.getAttribute?.("aria-hidden") === "true") return false;
      }
      return true;
    });
    const messageElements = activeMessageElements.filter((el) => {
      const parts = el.id.replace(/^jarvis-msg-/, "").split(".");
      return !expectedUid || parts[0] === expectedUid || parts[1] === expectedUid;
    });
    const chatRect = chat?.getBoundingClientRect();
    const visibleAccounts = new Set(messageElements.flatMap((el) => {
      const parts = el.id.replace(/^jarvis-msg-/, "").split(".");
      const account = parts[0] === expectedUid ? parts[1] : parts[1] === expectedUid ? parts[0] : "";
      const rect = el.getBoundingClientRect?.();
      return account && chatRect && rect &&
        Math.min(rect.right, chatRect.right) > Math.max(rect.left, chatRect.left) &&
        Math.min(rect.bottom, chatRect.bottom) > Math.max(rect.top, chatRect.top) ? [account] : [];
    }));
    const searchedChatKey = !active && header && rightPanelUid === expectedUid && visibleAccounts.size === 1
      ? `Active-${expectedUid}-${[...visibleAccounts][0]}` : "";
    const ownDecorativeLikeReply = (el, side, type) => {
      if (side !== "right" || type.toUpperCase() !== "GENERAL" || el.getAttribute("data-normal") !== "true" ||
          messageTextLimit === 0) return false;
      const spaces = [...(el.querySelectorAll?.('.d-space[content][msgcontainertype=""]') || [])];
      const tips = [...(el.querySelectorAll?.(".tip-text") || [])];
      const thanks = [...(el.querySelectorAll?.(".thank-content") || [])];
      const bodies = [...(el.querySelectorAll?.("p.text-message") || [])];
      const media = [...(el.querySelectorAll?.("img,video,iframe,canvas,svg,object,embed,audio") || [])];
      if (spaces.length !== 1 || tips.length !== 1 || thanks.length !== 1 || bodies.length !== 1 || media.length !== 1 ||
          (el.querySelectorAll?.('a,[role="link"]') || []).length !== 0) return false;
      const [space] = spaces, [tip] = tips, [thank] = thanks, [body] = bodies, [image] = media;
      if (space.querySelector(".tip-text") !== tip || space.querySelector(".thank-content") !== thank ||
          space.querySelector("p.text-message") !== body || text(tip.innerText || tip.textContent) !== "回复了对方的点赞" ||
          image.parentElement !== thank || !image.classList?.contains("thank-note-img") || image.getAttribute("alt") !== "笔记首图" ||
          body.querySelector?.('.ellipsis,.text-ellipsis,[aria-expanded="false"]') ||
          (Number(body.clientHeight) > 0 && Number(body.scrollHeight) > Number(body.clientHeight) + 1)) return false;
      const content = text(space.getAttribute("content"));
      return !!content && content === text(body.innerText || body.textContent) &&
        text(el.innerText || el.textContent) === text(`回复了对方的点赞 ${content}`);
    };
    const messageMetadata = messageElements.map((el, index) => {
      const className = String(el.className || "");
      const side = className.split(/\s+/).find((name) => ["left", "right", "center"].includes(name)) || "";
      const type = el.getAttribute("data-msg-type") || "";
      const decorative = ownDecorativeLikeReply(el, side, type);
      return {
        element: el,
        index,
        id: el.id,
        side,
        type,
        extracted_text_complete: decorative,
        decorative_media_only: decorative,
        plainText: el.getAttribute("data-normal") === "true" &&
          !!el.querySelector('.text-message[msgcontainertype=""],.msg-container[msgcontainertype=""]') &&
          !el.querySelector("img,video,iframe,canvas"),
        timestamp: Number(el.getAttribute("data-timestamp") || 0),
      };
    });
    const boundedTextLimit = Number.isInteger(messageTextLimit) && messageTextLimit >= 0
      ? messageTextLimit : null;
    const textCandidates = boundedTextLimit === null ? [] : messageMetadata.filter((message) =>
      ["left", "right"].includes(message.side) &&
      (message.type.toUpperCase() === "TEXT" ||
        (message.type.toUpperCase() === "GENERAL" && (message.plainText || message.decorative_media_only))));
    const textElements = boundedTextLimit === null ? null : new Set([...textCandidates]
      .sort((a, b) => b.timestamp - a.timestamp || b.index - a.index)
      .slice(0, boundedTextLimit).map((message) => message.element));
    const messageTextTruncated = boundedTextLimit !== null && textCandidates.length > boundedTextLimit;
    const messages = messageMetadata.map(({ element, index: _index, ...metadata }) => ({
      ...metadata,
      extracted_text_complete: metadata.extracted_text_complete && (textElements === null || textElements.has(element)),
      decorative_media_only: metadata.decorative_media_only && (textElements === null || textElements.has(element)),
      text: textElements === null || textElements.has(element)
        ? text(element.innerText || element.textContent) : "",
    }));
    const rendered = (el) => {
      const style = getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden";
    };
    const loadControls = [...(chat?.querySelectorAll?.("button,span,div") || [])].filter((el) =>
      !el.closest?.('[id^="jarvis-msg-"]') && !el.querySelector?.('[id^="jarvis-msg-"]') &&
      el.closest?.('[data-active]')?.getAttribute("data-active") !== "false" &&
      text(el.textContent) === "加载更多" && rendered(el) &&
      ![...el.children].some((child) => text(child.textContent) === "加载更多"));
    const loadMoreVisible = loadControls.length > 0;
    const clickableControls = loadControls.filter(el => !el.closest?.('[data-active="false"]')).map(el => {
      const rect = el.getBoundingClientRect?.();
      if (!rect || rect.width <= 0 || rect.height <= 0 || !document.elementFromPoint) return null;
      const point = [(rect.left + rect.right) / 2, (rect.top + rect.bottom) / 2];
      return el.contains?.(document.elementFromPoint(...point)) ? point : null;
    }).filter(Boolean);
    const loadMoreTarget = clickableControls.length === 1 ? clickableControls[0] : null;
    // recyScroll1 renders newest-first and reverses the visual list. Its oldest
    // edge is maxTop, not zero. An unmounted topBar does not prove history ends.
    const scroller = chat?.querySelector?.(".vue-recycle-scroller");
    const scrollBox = scroller?.getBoundingClientRect();
    const maxTop = scroller ? Math.max(0, scroller.scrollHeight - scroller.clientHeight) : 0;
    const reversed = scroller?.classList.contains("recyScroll1");
    const historyViewport = scroller ? {
      scrollTop: scroller.scrollTop, maxTop,
      olderRemaining: reversed ? scroller.scrollTop < maxTop - 3 : scroller.scrollTop > 3,
      newerRemaining: reversed ? scroller.scrollTop > 3 : scroller.scrollTop < maxTop - 3,
      target: [Math.round((scrollBox.left + scrollBox.right) / 2),
        Math.round((scrollBox.top + scrollBox.bottom) / 2)],
    } : null;
    // Failed local bubbles use temporary IDs and are deliberately excluded
    // from stable messages. Read their explicit retry marker separately.
    const failedMessages = [...(chat?.querySelectorAll?.('[title="重新发送"]') || [])].flatMap((el) => {
      const item = el.closest?.(".im-msg-item");
      if (!item || item.closest?.('[data-active]')?.getAttribute("data-active") === "false") return [];
      const bubble = item.querySelector('[id^="jarvis-msg-"]');
      const id = bubble?.id || item.id || "";
      const parts = id.replace(/^jarvis-msg-/, "").split(".");
      if (parts.length >= 3 && expectedUid && parts[0] !== expectedUid && parts[1] !== expectedUid) return [];
      const temporaryTime = id.match(/^(?:id_|jarvis-msg-)(\d{13})[-.]/)?.[1];
      return [{ id, timestamp: Number(bubble?.getAttribute?.("data-timestamp") || temporaryTime || 0),
        text: text(bubble?.innerText || bubble?.textContent ||
          el.parentElement?.parentElement?.parentElement?.innerText) }];
    });
    const notificationVisible = el => {
      for (let node = el; node && node !== document.body; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse" ||
            style.opacity === "0" || node.getAttribute?.("aria-hidden") === "true") return false;
      }
      return true;
    };
    const toasts = [...document.querySelectorAll('[role="alert"],[role="status"],.d-message,.ant-message-notice,.el-message')]
      .filter(el => notificationVisible(el) && !el.closest?.('#pro-msg-box,#rightPanel,.search-user'))
      .flatMap(el => {
        const rect = el.getBoundingClientRect?.();
        const value = text(el.innerText || el.textContent);
        // Capture only a rendered notification in the top strip. Body text,
        // customer quotes and panel save indicators cannot be send receipts.
        if (!value || !rect || rect.width <= 0 || rect.height <= 0 || rect.bottom <= 0 ||
            rect.top > Math.min(300, innerHeight * 0.35)) return [];
        const classes = String(el.className || "") + " " + String(el.querySelector?.('[class*="error"],[class*="warning"],[class*="success"]')?.className || "");
        return [{ text: value.slice(0, 600), severity: /success/.test(classes) ? "success" :
          /error/.test(classes) ? "error" : /warning/.test(classes) ? "warning" : "unknown",
          dom_id: el.id || "", source: "dom", top: Math.round(rect.top) }];
      }).slice(0, 10);
    const activeAccount = (active?.getAttribute("data-key") || searchedChatKey).match(/^(?:Total|Active|Favorite)-[^-]+-(.+)$/)?.[1] || "";
    const messageIdentities = activeMessageElements.slice(0, 12).map(el => {
      const [first = "", second = ""] = el.id.replace(/^jarvis-msg-/, "").split(".");
      const reversed = second === expectedUid || (!!activeAccount && first === activeAccount);
      return { id: el.id, uid: reversed ? second : first, account_id: reversed ? first : second,
        identity_tokens: [first, second] };
    });
    return {
      routeReady: location.pathname === "/im/multiCustomerService",
      searchReady: [...document.querySelectorAll("input")].some((el) =>
        el.getAttribute("placeholder") === "输入用户ID/小红书ID"),
      panelTab: text(selectedTab),
      panelText: text(panel?.innerText),
      rightPanelUid,
      activeKey: active?.getAttribute("data-key") || searchedChatKey,
      headerText: text(header?.innerText),
      labelPresent: label.present,
      labelValue: label.value,
      notePresent: note.present,
      noteValue: note.value,
      leadBadges,
      draft: input?.value || "",
      messages,
      messageIdentities,
      messageTextTruncated,
      contactFieldsPresent: [...(panel?.querySelectorAll('input[placeholder="请填写手机号码"],input[placeholder="请填写微信账号"]') || [])]
        .some((el) => text(el.value) && text(el.value) !== "-"),
      loadMoreVisible,
      loadMoreTarget,
      historyViewport,
      failedMessages,
      toasts,
      observedAtMs: Date.now(),
      expectedUid,
    };
  }, { expectedUid: uid, messageTextLimit }, { timeoutMs: Math.max(1, Math.min(10000, Number(timeoutMs) || 10000)) });
}

async function readPopup(tab, { deadlineAt = Date.now() + 10000 } = {}) {
  return tab.playwright.evaluate(() => {
    const elements = [...document.querySelectorAll(".search-contact-item")];
    const input = document.querySelector?.('input[placeholder="输入用户ID/小红书ID"]');
    const searchText = document.querySelector?.(".search-user")?.innerText || "";
    const scrollParent = (el) => {
      let current = el?.parentElement;
      const searchRoot = el?.closest?.(".search-user");
      const itemRect = el?.getBoundingClientRect();
      while (current && current !== document.body) {
        const style = getComputedStyle(current);
        const rect = current.getBoundingClientRect();
        const overlapsItem = itemRect && Math.min(rect.right, itemRect.right) > Math.max(rect.left, itemRect.left) &&
          Math.min(rect.bottom, itemRect.bottom) > Math.max(rect.top, itemRect.top);
        if (overlapsItem && current.scrollHeight > current.clientHeight + 4 &&
            /auto|scroll|overlay/.test(style.overflowY || "")) return current;
        if (current === searchRoot) break;
        current = current.parentElement;
      }
      return null;
    };
    const isShown = el => {
      for (let node = el; node && node !== document.body; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse" ||
            style.opacity === "0" || node.getAttribute?.("aria-hidden") === "true") return false;
      }
      return true;
    };
    // A hidden result from the previous query can belong to another scroll
    // container. Its rectangle must not clip the currently visible results.
    const parent = scrollParent(elements.find(isShown));
    const box = parent?.getBoundingClientRect();
    const cx = box ? Math.round((box.left + box.right) / 2) : 0;
    const cy = box ? Math.round((box.top + box.bottom) / 2) : 0;
    const entries = elements.map((el, allIndex) => {
      const r = el.getBoundingClientRect();
      const shown = isShown(el);
      const clip = box || { left: 0, top: 0, right: innerWidth, bottom: innerHeight };
      const area = Math.max(0, Math.min(r.right, clip.right, innerWidth) - Math.max(r.left, clip.left, 0)) *
        Math.max(0, Math.min(r.bottom, clip.bottom, innerHeight) - Math.max(r.top, clip.top, 0));
      return {
        allIndex,
        text: String(el.innerText || el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 200),
        dataKey: el.getAttribute("data-key") || "",
        absoluteY: Math.round((parent?.scrollTop || 0) + r.top - (box?.top || 0)),
        x: Math.round((Math.max(r.left, clip.left, 0) + Math.min(r.right, clip.right, innerWidth)) / 2),
        y: Math.round((Math.max(r.top, clip.top, 0) + Math.min(r.bottom, clip.bottom, innerHeight)) / 2),
        area: shown ? area : 0,
      };
    }).filter((entry) => entry.area > 50).sort((a, b) => a.absoluteY - b.absoluteY);
    return {
      inputValue: input ? input.value : undefined,
      rawCount: elements.length,
      visibleCount: entries.length,
      emptyText: searchText.match(/暂无匹配的用户/)?.[0] || "",
      loadingText: searchText.match(/正在搜索|搜索中|加载中/)?.[0] || "",
      entries,
      scrollTop: parent?.scrollTop || 0,
      maxTop: parent ? Math.max(0, parent.scrollHeight - parent.clientHeight) : 0,
      scrollTarget: [cx, cy],
    };
  }, undefined, { timeoutMs: Math.max(1, Math.min(10000, deadlineAt - Date.now())) });
}

function popupKey(entry) {
  return [entry.dataKey || "", entry.text, Math.round(entry.absoluteY / 20)].join("|");
}

async function openSearch(tab, uid, { deadlineAt = Date.now() + 6500 } = {}) {
  const started = Date.now();
  const errors = [];
  const input = tab.playwright.getByPlaceholder(SEARCH_PLACEHOLDER, { exact: true });
  const count = await input.count();
  if (count !== 1) return { ok: false, reason: "search_input_count_" + count };
  // Reset focus even for the first search after a different customer. A typed
  // query is not proof that the reactive search component has been triggered.
  if (tab.playwright.locator) {
    const header = tab.playwright.locator(".message-header");
    if (header.count && await header.count()) {
      try { await header.first().click({ timeoutMs: Math.max(1, Math.min(3000, deadlineAt - Date.now())) }); }
      catch (error) { if (connectionFailure(error)) throw error; errors.push("focus_reset: " + String(error.message || error).slice(0, 160)); }
    }
  }
  if (input.fill) {
    await input.fill("");
    await input.fill(uid);
  } else {
    await input.click();
    await input.press("Meta+A");
    await input.press("Backspace");
    await input.pressSequentially(uid);
  }
  let popup = await readPopup(tab, { deadlineAt });
  let submitted = false;
  const searchDeadline = Math.min(deadlineAt - 300, started + 6500);
  for (let attempt = 0; (!popup.entries.length || popup.loadingText) && attempt < 2; attempt += 1) {
    if (popup.inputValue !== undefined && popup.inputValue !== uid) break;
    if (attempt === 1 && !popup.loadingText && input.press) {
      // Some page states need explicit submission. Do not press Enter when a
      // result is already present, and never infer absence from a transient
      // "暂无匹配的用户" rendered while the request is still in flight.
      await input.press("Enter");
      submitted = true;
    }
    const waitMs = Math.max(0, Math.min(attempt === 0 ? 1600 : 4500, searchDeadline - Date.now()));
    if (!waitMs) break;
    if (popup.loadingText && tab.playwright.getByText) {
      try { await tab.playwright.getByText(popup.loadingText, {exact: true}).first()
        .waitFor({state: "hidden", timeoutMs: waitMs}); }
      catch (error) { if (connectionFailure(error)) throw error; errors.push(String(error.message || error).slice(0, 180)); }
    }
    // waitFor(state=visible) already applies visibility. CUA's additional
    // synthetic `visible=true` filter can report no matches for real results;
    // the fresh DOM query below remains the query and visibility authority.
    const results = tab.playwright.locator?.(".search-contact-item");
    const remainingWaitMs = Math.min(waitMs, searchDeadline - Date.now());
    if (results?.first?.().waitFor && remainingWaitMs > 0) {
      try { await results.first().waitFor({state: "visible", timeoutMs: remainingWaitMs}); }
      catch (error) { if (connectionFailure(error)) throw error; errors.push(String(error.message || error).slice(0, 180)); }
    } else await tab.playwright.domSnapshot();
    popup = await readPopup(tab, { deadlineAt });
  }
  const diagnostic = {
    uid, input_value: popup.inputValue ?? null, raw_count: popup.rawCount ?? null,
    visible_count: popup.entries.length, empty_text: popup.emptyText || "",
    loading_text: popup.loadingText || "", submitted, elapsed_ms: Date.now() - started,
    state: popup.inputValue !== undefined && popup.inputValue !== uid ? "input_changed" :
      popup.loadingText ? "loading_timeout" : popup.entries.length ? "results_visible" :
      popup.rawCount > 0 ? "results_not_visible" : popup.emptyText ? "empty_hint_unconfirmed" : "search_not_confirmed",
    errors,
  };
  return { ok: !["input_changed", "loading_timeout"].includes(diagnostic.state),
    reason: diagnostic.state === "input_changed" ? "search_input_changed" : diagnostic.state === "loading_timeout" ? "search_loading_timeout" : "",
    popup, diagnostic };
}

async function clickPopupCandidate(tab, candidate, { deadlineAt = Date.now() + 8000 } = {}) {
  if (!Number.isInteger(candidate.allIndex)) {
    // Compatibility with older injected snapshots; readPopup always returns allIndex.
    await tab.click([candidate.x, candidate.y]);
    return { ok: true };
  }
  const fresh = await readPopup(tab, { deadlineAt });
  const entry = fresh.entries.find((item) => item.allIndex === candidate.allIndex);
  if (!entry || popupKey(entry) !== popupKey(candidate)) {
    return { ok: false, reason: "search_result_changed_before_click" };
  }
  const locator = tab.playwright.locator(".search-contact-item");
  let failure = "search_result_disappeared_before_click";
  try {
    if (await locator.count() > candidate.allIndex) {
      await locator.nth(candidate.allIndex).click({ timeoutMs: Math.max(1, Math.min(8000, deadlineAt - Date.now())) });
      return { ok: true };
    }
  } catch (error) {
    if (connectionFailure(error)) throw error;
    failure = "search_result_click_failed:" + String(error?.message || error).slice(0, 120);
  }
  // The browser locator can report zero despite a visible DOM result. Native
  // input is allowed only after another read proves the same query and entry.
  // Conversation UID/account verification still follows this navigation.
  const current = await readPopup(tab, { deadlineAt });
  const target = current.entries.find(item => item.allIndex === candidate.allIndex);
  if (!fresh.inputValue || current.inputValue !== fresh.inputValue || !target ||
      popupKey(target) !== popupKey(candidate) ||
      !Number.isFinite(target.x) || !Number.isFinite(target.y) || target.area <= 50 || !tab.click) {
    return { ok: false, reason: failure };
  }
  try {
    await tab.click([target.x, target.y]);
    return { ok: true };
  } catch (error) {
    if (connectionFailure(error)) throw error;
    return { ok: false, reason: "search_result_native_click_failed:" + String(error?.message || error).slice(0, 120) };
  }
}

async function readOpenedConversation(tab, uid, deadlineAt, { accountId = "", read = (id, options) => readSnapshot(tab, id, options),
  snapshotOptions = {}, authorizedAccountIds = [] } = {}) {
  const until = Math.min(deadlineAt - 300, Date.now() + 3000);
  let raw, review;
  let annotationAttempted = false, panelFailureReason = "";
  for (let attempt = 0; attempt < 16; attempt += 1) {
    raw = await read(uid, snapshotOptions);
    review = buildReview(raw, {uid, accountId, authorizedAccountIds});
    // Searching can mount a new conversation directly in the tools tab even
    // when ensurePage saw no right panel. Switching this display tab is a UI
    // read action; no customer label, notes, draft or message is changed.
    if (raw.panelTab && raw.panelTab !== "客资标注" && !annotationAttempted && !norm(raw.draft) && Date.now() < until) {
      annotationAttempted = true;
      const restored = await restoreAnnotationTab(tab, until);
      if (!restored.ok) panelFailureReason = restored.reason;
    }
    if (review.identity_verified && review.panel_loaded) break;
    if (norm(raw.draft)) break;
    if (Date.now() >= until) break;
    const panel = tab.playwright.locator?.("#rightPanel");
    const uidNode = panel?.getByText?.(uid, {exact: true});
    if (attempt === 0 && uidNode?.waitFor) {
      try { await uidNode.waitFor({state: "visible", timeoutMs: Math.max(1, until - Date.now())}); }
      catch (error) {
        if (connectionFailure(error)) throw error;
        // Retain the final observed identity; never substitute the requested UID.
      }
    } else if (tab.playwright.waitForTimeout) {
      await tab.playwright.waitForTimeout(Math.min(120, Math.max(1, until - Date.now())));
    } else await tab.playwright.domSnapshot();
  }
  if (annotationAttempted && raw.panelTab !== "客资标注" && !panelFailureReason) {
    panelFailureReason = "customer_annotation_tab_not_restored";
  }
  return {raw, review, reason: review.panel_loaded ? "" : panelFailureReason};
}

async function nextPopupCandidate(tab, tried, deadlineAt, {initialPopup, preferredKey = ""} = {}) {
  let lastTop = -1;
  for (let pages = 0; pages < 24 && Date.now() < deadlineAt; pages += 1) {
    let popup = pages === 0 && initialPopup ? initialPopup : await readPopup(tab, { deadlineAt });
    if (pages === 0 && popup.entries.length === 0) {
      return { candidate: null, exhausted: false, reason: "search_result_not_confirmed", popup };
    }
    const next = popup.entries.find((entry) => preferredKey && popupKey(entry) === preferredKey && !tried.has(preferredKey)) ||
      popup.entries.find((entry) => !tried.has(popupKey(entry)));
    if (next) return { candidate: next, exhausted: false, popup };
    if (popup.scrollTop >= popup.maxTop - 3 || popup.scrollTop === lastTop || !popup.scrollTarget[0]) {
      return { candidate: null, exhausted: true, popup };
    }
    lastTop = popup.scrollTop;
    await tab.scroll(popup.scrollTarget, "down", 0.75);
    await tab.playwright.domSnapshot();
  }
  return { candidate: null, exhausted: false, reason: "popup_scan_limit" };
}

async function listFallback(tab, uid, triedKeys) {
  const candidates = await tab.playwright.evaluate(({ uid }) => {
    const result = [];
    const scrollers = [...document.querySelectorAll(".vue-recycle-scroller.scroller")]
      .filter((el) => el.querySelector('.sx-contact-item[data-key]'))
      .sort((a, b) => b.querySelectorAll('.sx-contact-item[data-key]').length -
        a.querySelectorAll('.sx-contact-item[data-key]').length);
    const clip = scrollers[0]?.getBoundingClientRect();
    for (const el of document.querySelectorAll(".sx-contact-item[data-key]")) {
      const key = el.getAttribute("data-key") || "";
      if (!key.startsWith("Total-" + uid) && !key.startsWith("Active-" + uid)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1 || !clip ||
          Math.min(r.right, clip.right, innerWidth) <= Math.max(r.left, clip.left, 0) ||
          Math.min(r.bottom, clip.bottom, innerHeight) <= Math.max(r.top, clip.top, 0)) continue;
      result.push(key);
    }
    return result;
  }, { uid }, { timeoutMs: 10000 });
  const next = candidates.find((key) => !triedKeys.has(key));
  if (!next) return { found: false };
  const selector = `.sx-contact-item[data-key="${next.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"]`;
  const locator = tab.playwright.locator(selector);
  if (await locator.count() !== 1) return { found: false };
  await locator.click();
  await tab.playwright.domSnapshot();
  return { found: true, key: next };
}

async function loadHistory(tab, uid, accountId, cache, deadlineAt, {
  maxLoads = 30, analysisMode = false, nowMs = Date.now(), maxRealMessages = 40,
  initialRaw = null, read = (id, options) => readSnapshot(tab, id, options),
} = {}) {
  const startMarkerVisible = (messages) => {
    const accountMessages = (messages || []).filter((message) =>
      parseMessageAccount(message.id, uid) === accountId)
      .sort((a, b) => a.timestamp - b.timestamp);
    if (accountMessages.some(message => !Number.isFinite(message.timestamp) || message.timestamp <= 0 ||
        message.timestamp > nowMs + 5 * 60 * 1000)) return false;
    const first = accountMessages[0];
    return first?.side === "center" && first?.type?.toUpperCase() === "HINT" &&
      /^(?:「温馨提示」|\[温馨提示\]|【温馨提示】|温馨提示)\s*(?:有用户进入会话|您已进入企业员工会话|您已进入企业会话)[，,]\s*为保障消费者权益[，,]/
        .test(norm(first.text)) && /记录/.test(first.text || "");
  };
  const snapshots = [];
  let raw = initialRaw || await read(uid, {
    messageTextLimit: analysisMode ? maxRealMessages : null,
  });
  const oldBoundary = cache?.boundary_message_id || "";
  let count = 0;
  let priorBoundaryReached = false;
  let fromStartComplete = false;
  let analysisTruncated = false;
  let scrolls = 0;
  let failureReason = "";
  const accountMessages = (value) => (value.messages || []).filter((message) =>
    parseMessageAccount(message.id, uid) === accountId);
  const identityMatches = (value) => buildReview(value, { uid, accountId }).identity_verified;
  const settleHistory = async (initial, ready) => {
    const until = Math.min(deadlineAt - 1000, Date.now() + 1800);
    let current = initial;
    for (;;) {
      if (!identityMatches(current) || await ready(current) || Date.now() >= until) return current;
      // Wait on elapsed time rather than a number of snapshots: a pending
      // history request can outlive many fast reads of the unchanged DOM.
      await new Promise(resolve => setTimeout(resolve, Math.min(120, Math.max(1, until - Date.now()))));
      current = await read(uid, {messageTextLimit: analysisMode ? maxRealMessages : null});
    }
  };
  for (;;) {
    snapshots.push(raw);
    if (analysisMode) {
      const currentReal = (raw.messages || []).filter((message) =>
        parseMessageAccount(message.id, uid) === accountId && isTextMessage(message));
      const cutoff = nowMs - 30 * 24 * 60 * 60 * 1000;
      if (maxRealMessages <= 0 || raw.messageTextTruncated || currentReal.length > maxRealMessages ||
          currentReal.some((message) =>
            Number.isFinite(Number(message.timestamp)) && Number(message.timestamp) > 0 &&
            Number(message.timestamp) <= cutoff)) {
        analysisTruncated = true;
        break;
      }
      if (!raw.loadMoreVisible && !raw.historyViewport?.olderRemaining) {
        fromStartComplete = true;
        break;
      }
      if (currentReal.length >= maxRealMessages) {
        analysisTruncated = true;
        break;
      }
    } else {
      const seen = snapshots.flatMap((item) => item.messages || [])
        .some((message) => message.id === oldBoundary && parseMessageAccount(message.id, uid) === accountId);
      if (oldBoundary && seen) {
        priorBoundaryReached = true;
        break;
      }
      if (!raw.loadMoreVisible && !raw.historyViewport?.olderRemaining) {
        fromStartComplete = true;
        break;
      }
    }
    if (count + scrolls >= maxLoads || Date.now() >= deadlineAt - 1000) {
      failureReason = "history_read_budget_exhausted";
      break;
    }
    if (!raw.loadMoreVisible && raw.historyViewport?.olderRemaining) {
      await tab.scroll(raw.historyViewport.target, "up", 0.5);
      scrolls += 1;
      await tab.playwright.domSnapshot();
      const next = await read(uid, { messageTextLimit: analysisMode ? maxRealMessages : null });
      if (!identityMatches(next)) { failureReason = "history_identity_changed"; break; }
      const known = new Set(accountMessages(raw).map(m => m.id));
      const progressed = accountMessages(next).some(m => !known.has(m.id)) ||
        next.historyViewport?.scrollTop !== raw.historyViewport.scrollTop || next.loadMoreVisible;
      raw = next;
      if (!progressed && next.historyViewport?.olderRemaining) {
        failureReason = "history_scroll_stalled";
        break;
      }
      continue;
    }
    const moreRoot = tab.playwright.locator ? tab.playwright.locator("#pro-msg-box") : tab.playwright;
    let more = moreRoot.getByText("加载更多", { exact: true }).filter({ visible: true });
    if (tab.playwright.locator) {
      // Recycled conversations can keep an old customer's load control inside
      // the viewport. Match readSnapshot's exclusion of inactive ancestors;
      // ordinary, nonvirtual history controls also satisfy this selector.
      more = more.and(tab.playwright.locator('#pro-msg-box :not([data-active="false"]):not([data-active="false"] *)'));
    }
    let moreCount = await more.count();
    // A recycled load control can be mounted just outside the chat viewport.
    // Finish scrolling to the older edge before requiring a clickable control.
    if (moreCount !== 1 && !raw.loadMoreTarget && raw.historyViewport?.olderRemaining) {
      await tab.scroll(raw.historyViewport.target, "up", 0.5);
      scrolls += 1;
      await tab.playwright.domSnapshot();
      const next = await read(uid, {messageTextLimit: analysisMode ? maxRealMessages : null});
      if (!identityMatches(next)) { failureReason = "history_identity_changed"; break; }
      if (next.historyViewport?.scrollTop === raw.historyViewport.scrollTop && next.historyViewport?.olderRemaining) {
        failureReason = "history_scroll_stalled"; break;
      }
      raw = next;
      continue;
    }
    let nativeMoreTarget = null;
    if (moreCount !== 1 && raw.loadMoreTarget) {
      const current = await read(uid, {messageTextLimit: analysisMode ? maxRealMessages : null});
      if (!identityMatches(current)) { failureReason = "history_identity_changed"; break; }
      if (current.loadMoreTarget?.length === 2 && current.loadMoreTarget.every(Number.isFinite) && tab.click) {
        nativeMoreTarget = current.loadMoreTarget;
      }
    }
    if (moreCount !== 1 && !nativeMoreTarget) {
      // The load control can disappear while the opened conversation settles.
      // Keep the same identity and wait briefly for the control or actual
      // history progress; disappearance alone can be a loading transition.
      const known = new Set(accountMessages(raw).map(message => message.id));
      const initial = await read(uid, {
        messageTextLimit: analysisMode ? maxRealMessages : null,
      });
      const settled = await settleHistory(initial, async value => {
        moreCount = await more.count();
        return moreCount === 1 || accountMessages(value).some(message => !known.has(message.id)) ||
          startMarkerVisible(value.messages) || (!value.loadMoreVisible && value.historyViewport?.olderRemaining);
      });
      if (!identityMatches(settled)) { failureReason = "history_identity_changed"; break; }
      if (settled.activeKey === raw.activeKey &&
          !settled.loadMoreVisible && !settled.historyViewport?.olderRemaining &&
          accountMessages(settled).length >= accountMessages(raw).length) {
        raw = settled;
        fromStartComplete = true;
        break;
      }
      raw = settled;
      if (!raw.loadMoreVisible && raw.historyViewport?.olderRemaining) continue;
      if (moreCount !== 1) { failureReason = "history_control_not_ready"; break; }
    }
    if (nativeMoreTarget) await tab.click(nativeMoreTarget);
    else await more.click();
    count += 1;
    await tab.playwright.domSnapshot();
    const initial = await read(uid, {
      messageTextLimit: analysisMode ? maxRealMessages : null,
    });
    const knownIds = new Set(snapshots.flatMap(accountMessages).map(m => m.id));
    const next = await settleHistory(initial, value =>
      accountMessages(value).some(message => !knownIds.has(message.id)) ||
      (!analysisMode && startMarkerVisible(value.messages)));
    if (!identityMatches(next)) { failureReason = "history_identity_changed"; break; }
    if (!accountMessages(next).some(m => !knownIds.has(m.id)) && next.loadMoreVisible) {
      // The platform sometimes keeps the load-more control after the first
      // conversation-entry hint. One unchanged load plus that explicit start
      // marker proves there are no earlier messages for this account.
      if (!analysisMode && startMarkerVisible(next.messages)) fromStartComplete = true;
      if (!fromStartComplete) failureReason = "history_load_stalled";
      raw = next;
      break;
    }
    raw = next;
  }
  snapshots.push(raw);
  // Restore the latest edge for final identity/boundary validation. Keep the
  // merged older messages for the operator's business review.
  for (let restores = 0; !analysisMode && raw.historyViewport?.newerRemaining &&
      restores < maxLoads && Date.now() < deadlineAt - 1000; restores += 1) {
    await tab.scroll(raw.historyViewport.target, "down", 0.75);
    await tab.playwright.domSnapshot();
    const next = await read(uid);
    if (!identityMatches(next)) { failureReason = "history_identity_changed"; break; }
    snapshots.push(next);
    if (next.historyViewport?.scrollTop === raw.historyViewport.scrollTop) {
      failureReason = "history_restore_stalled";
      break;
    }
    raw = next;
  }
  const merged = new Map();
  for (const snapshot of snapshots) for (const message of snapshot.messages || []) merged.set(message.id, message);
  return {
    raw: { ...raw, messages: [...merged.values()] },
    loads: count,
    priorBoundaryReached,
    fromStartComplete,
    analysisTruncated,
    failureReason,
    scrolls,
    complete: !failureReason && (priorBoundaryReached || fromStartComplete),
  };
}

function samePreflight(a, b) {
  return !!a && !!b && a.identity_verified && b.identity_verified &&
    a.history_complete && b.history_complete &&
    a.user_id === b.user_id && a.account_id === b.account_id &&
    a.panel_loaded && b.panel_loaded &&
    a.label === b.label && a.no_intent_label === b.no_intent_label &&
    a.negative_status === b.negative_status &&
    a.protective_lead_signal === b.protective_lead_signal &&
    a.panel_loaded === b.panel_loaded &&
    a.history_boundary_message_id === b.history_boundary_message_id &&
    a.last_outbound_text_at_ms === b.last_outbound_text_at_ms &&
    a.last_customer_at_ms === b.last_customer_at_ms;
}

function revalidationReview(raw, baseline) {
  const fresh = buildReview(raw, {
    uid: baseline.user_id,
    accountId: baseline.account_id,
    authorizedAccountIds: [baseline.account_id],
    historyComplete: baseline.history_complete,
  });
  if (!fresh.identity_verified || !fresh.history_boundary_message_id ||
      fresh.history_boundary_message_id !== baseline.history_boundary_message_id) return fresh;
  // The full history was already proved in observeUid. This light read must
  // still catch any newly visible refusal, peer signal or unknown message type.
  return {
    ...fresh,
    history_from_start_complete: baseline.history_from_start_complete,
    history_reached_prior_boundary: baseline.history_reached_prior_boundary,
    // The chat virtualizes older bubbles after a full read. With the same
    // newest message ID, omitted older timestamps are still proven history.
    last_outbound_text_at_ms: Math.max(baseline.last_outbound_text_at_ms || 0, fresh.last_outbound_text_at_ms || 0) || null,
    last_customer_at_ms: Math.max(baseline.last_customer_at_ms || 0, fresh.last_customer_at_ms || 0) || null,
    manual_outbound_at_ms: Math.max(baseline.manual_outbound_at_ms || 0, fresh.manual_outbound_at_ms || 0) || null,
    outbound_text_hashes: [...new Set([...baseline.outbound_text_hashes, ...fresh.outbound_text_hashes])],
    business_reviewed: baseline.business_reviewed,
    negative_status: baseline.negative_status,
    protective_lead_signal: baseline.protective_lead_signal || fresh.platform_lead_signal,
  };
}

export function createXhsCuaAdapter(tab, {
  privateMessageUrl = "https://pro.xiaohongshu.com/im/multiCustomerService",
  now = () => Date.now(),
  receiptWindowMs = 8000,
  receiptPollMs = 250,
  wait = (ms) => tab.playwright.waitForTimeout ? tab.playwright.waitForTimeout(ms) :
    new Promise(resolve => setTimeout(resolve, ms)),
} = {}) {
  if (!tab?.playwright || typeof tab.playwright.evaluate !== "function") {
    throw new TypeError("A selected CUA browser Tab is required");
  }
  let selected = null;
  let staged = null;
  let knownCandidates = new Map();
  let sequence = 0;
  const totals = { search_opens: 0, history_load_clicks: 0, page_recoveries: 0, snapshot_reads: 0,
    page_prepare_ms: 0, search_ms: 0, identity_read_ms: 0, history_read_ms: 0,
    selection_ms: 0, history_cache_hits: 0, history_cache_misses: 0 };
  const snapshot = async (uid, options = {}) => {
    totals.snapshot_reads += 1;
    return readSnapshot(tab, uid, options);
  };

  async function ensurePage({ messageTextLimit = null } = {}) {
    let currentUrl = await tab.url();
    if (!new URL(currentUrl).pathname.startsWith(ROUTE)) {
      const menu = tab.playwright.getByText("私信通", { exact: true }).filter({ visible: true });
      if (await menu.count() === 1) {
        await menu.click();
        await tab.playwright.domSnapshot();
      }
      currentUrl = await tab.url();
      totals.page_recoveries += 1;
    }
    let ready = await snapshot("", { messageTextLimit });
    if ((!ready.routeReady || !ready.searchReady) && currentUrl.startsWith("https://pro.xiaohongshu.com/")) {
      if (norm(ready.draft)) return { ok: false, reason: "draft_present_before_route_recovery" };
      await tab.goto(privateMessageUrl);
      const searchInput = tab.playwright.getByPlaceholder(SEARCH_PLACEHOLDER, { exact: true });
      try {
        await searchInput.waitFor({ state: "visible", timeoutMs: 10000 });
      } catch (error) {
        if (connectionFailure(error)) throw error;
        return { ok: false, reason: "private_message_page_recovery_timeout" };
      }
      await tab.playwright.domSnapshot();
      totals.page_recoveries += 1;
      ready = await snapshot("", { messageTextLimit });
    }
    if (!ready.routeReady || !ready.searchReady) return { ok: false, reason: "private_message_page_unavailable" };
    // On a fresh page there may be no selected customer and therefore no right
    // panel yet. Restore the annotation tab only when that tab is mounted.
    if (ready.panelTab && ready.panelTab !== "客资标注") {
      const restored = await restoreAnnotationTab(tab, Date.now() + 1800);
      if (!restored.ok) return restored;
      totals.page_recoveries += 1;
      ready = await snapshot("", { messageTextLimit });
      if (ready.panelTab !== "客资标注") return { ok: false, reason: "customer_annotation_tab_not_restored" };
    }
    return { ok: true };
  }

  function adoptObservedSelection({ uid, accountId, snapshot: raw, review, allReviews = [] }) {
    const verified = review || buildReview(raw, { uid, accountId, authorizedAccountIds: [accountId] });
    if (!verified.identity_verified || verified.account_id !== accountId) return "";
    selected = { token: `selection-${++sequence}-${now()}`, uid, accountId, raw, review: verified,
      allReviews: allReviews.length ? allReviews : [verified] };
    staged = null;
    return selected.token;
  }

  async function probeUid({ uid, scopeMode = "all", authorizedAccountIds = [], relevantAccountIds = [],
    budgetMs = 6000 } = {}) {
    // A page control probe is navigation-only and must not authorize a send.
    // Invalidate any previous page-bound selection before leaving that chat.
    selected = null;
    staged = null;
    knownCandidates = new Map();
    const started = Date.now();
    const deadlineAt = started + Math.min(10000, Math.max(1000, Number(budgetMs) || 6000));
    const probeRead = (id, options = {}) => snapshot(id, { ...options,
      timeoutMs: Math.max(1, deadlineAt - Date.now()) });
    const authorized = [...new Set(authorizedAccountIds.map(String).filter(Boolean))];
    const required = [...new Set(relevantAccountIds.map(String).filter(Boolean))];
    const allScope = scopeMode === "all";
    const base = { uid: String(uid || ""), read_only: true, identity_verified: false,
      panel_loaded: false, search_complete: false };
    if (!uid || (!allScope && (!authorized.length || !required.length))) {
      return { ...base, status: "deferred", reason: "missing_uid_or_explicit_account_scope" };
    }
    let raw = await probeRead(uid, { messageTextLimit: 0 });
    if (norm(raw.draft)) return { ...base, status: "blocked", reason: "user_draft_present" };
    if (!raw.routeReady || !raw.searchReady) {
      return { ...base, status: "blocked", reason: "private_message_page_unavailable" };
    }
    const tried = new Set();
    let reason = "search_result_not_confirmed", diagnostic = null;
    const searchDiagnostics = [];
    for (let attempt = 0; attempt < 20 && Date.now() < deadlineAt - 300; attempt += 1) {
      const opened = await openSearch(tab, uid, { deadlineAt });
      totals.search_opens += 1;
      if (opened.diagnostic) searchDiagnostics.push(opened.diagnostic);
      if (!opened.ok) { reason = opened.reason; break; }
      const next = await nextPopupCandidate(tab, tried, deadlineAt, { initialPopup: opened.popup });
      if (!next.candidate) { reason = next.reason || "probe_account_not_found"; break; }
      const clicked = await clickPopupCandidate(tab, next.candidate, { deadlineAt });
      if (!clicked.ok) { reason = clicked.reason; break; }
      tried.add(popupKey(next.candidate));
      const result = await readOpenedConversation(tab, uid, deadlineAt, {
        read: probeRead, snapshotOptions: { messageTextLimit: 0 }, authorizedAccountIds: authorized,
      });
      raw = result.raw;
      diagnostic = result.review.identity_diagnostics;
      if (norm(raw.draft)) return { ...base, status: "blocked", reason: "user_draft_present",
        identity_diagnostics: diagnostic, search_diagnostics: searchDiagnostics };
      if (result.reason) return {...base, status: "blocked", reason: result.reason,
        identity_diagnostics: diagnostic, search_diagnostics: searchDiagnostics,
        elapsed_ms: Date.now() - started};
      if (!result.review.identity_verified) { reason = "conversation_identity_not_ready"; break; }
      if (!result.review.panel_loaded) { reason = "customer_annotation_panel_not_ready"; break; }
      const accountId = result.review.account_id;
      if (!allScope && (!authorized.includes(accountId) || !required.includes(accountId))) {
        reason = "probe_account_out_of_scope";
        continue;
      }
      return { ...base, status: "healthy", reason: "verified_read_only_control", identity_verified: true,
        panel_loaded: true, search_complete: true, selected_account_id: accountId,
        // Exact-result probe coverage is not full cross-account send coverage.
        probe_scope: "single_verified_conversation", identity_diagnostics: diagnostic,
        search_diagnostics: searchDiagnostics, elapsed_ms: Date.now() - started };
    }
    return { ...base, status: "deferred", reason, identity_diagnostics: diagnostic || identityDiagnostics(raw, uid),
      search_diagnostics: searchDiagnostics, elapsed_ms: Date.now() - started };
  }

  async function observeUid({
    uid,
    scopeMode = "specified",
    authorizedAccountIds,
    relevantAccountIds,
    historyCache = {},
    lastSenderAccountId = "",
    analysisMode = false,
    targetAccountId = "",
    budgetMs = 30000,
  } = {}) {
    const started = now();
    const beforeCounters = {...totals};
    const historyCacheReasons = {};
    const measure = async (key, fn) => {
      const at = now();
      try { return await fn(); }
      finally { totals[key] += Math.max(0, now() - at); }
    };
    selected = null;
    staged = null;
    knownCandidates = new Map();
    const deadlineAt = started + Math.max(1000, Number(budgetMs) || 30000);
    const allScope = scopeMode === "all" && !analysisMode;
    const required = allScope ? [] : [...new Set((relevantAccountIds || []).map(String).filter(Boolean))];
    const authorized = [...new Set((authorizedAccountIds || []).map(String).filter(Boolean))];
    const analysisTarget = analysisMode ? String(targetAccountId || "") : "";
    const sendTarget = analysisMode ? "" : String(targetAccountId || "");
    if (!uid || (!allScope && (!authorized.length || !required.length))) {
      return { status: "deferred", reason: "missing_uid_or_explicit_account_scope", uid: uid || "", reviews: [] };
    }
    if (!allScope && (analysisTarget || sendTarget) &&
        (!required.includes(analysisTarget || sendTarget) || !authorized.includes(analysisTarget || sendTarget))) {
      return { status: "deferred", reason: analysisMode ? "analysis_target_out_of_scope" : "send_target_out_of_scope", uid, reviews: [] };
    }
    const page = await measure("page_prepare_ms", () => ensurePage({ messageTextLimit: analysisMode ? 0 : null }));
    if (!page.ok) return { status: "deferred", reason: page.reason, uid, reviews: [] };
    const initialPage = await snapshot(uid, { messageTextLimit: analysisMode ? 0 : null });
    if (norm(initialPage.draft)) {
      return { status: "deferred", reason: "user_draft_present", uid, reviews: [] };
    }
    const reviews = new Map();
    const rawByAccount = new Map();
    const triedPopup = new Set();
    const candidateReadFailures = new Map();
    const triedList = new Set();
    let popupExhausted = false;
    let searchAttempts = 0;
    let unresolvedCandidates = 0;
    let searchFailure = "";
    let globalStop = "";
    let analysisRemaining = 40;
    const searchDiagnostics = [];
    let lastIdentityDiagnostics = null;
    while (searchAttempts < 20 && now() < deadlineAt - 1000) {
      if (searchAttempts > 0) {
        const beforeSearch = await snapshot(uid, { messageTextLimit: analysisMode ? 0 : null });
        if (norm(beforeSearch.draft)) {
          unresolvedCandidates += 1;
          break;
        }
      }
      const opened = await measure("search_ms", () => openSearch(tab, uid, { deadlineAt }));
      if (opened.diagnostic) searchDiagnostics.push(opened.diagnostic);
      totals.search_opens += 1;
      searchAttempts += 1;
      if (!opened.ok) return { status: "deferred", reason: opened.reason, uid, reviews: [...reviews.values()],
        search_diagnostics: searchDiagnostics, performance: {elapsed_ms: now() - started, search_opens: searchAttempts} };
      const next = await nextPopupCandidate(tab, triedPopup, deadlineAt, {initialPopup: opened.popup});
      if (next.candidate) {
        const clicked = await clickPopupCandidate(tab, next.candidate);
        if (!clicked.ok) {
          unresolvedCandidates += 1;
          searchFailure = clicked.reason;
          break;
        }
        await tab.playwright.domSnapshot();
      } else if (next.exhausted) {
        popupExhausted = true;
        if (reviews.size > 0 && required.every((accountId) => reviews.has(accountId))) break;
        const fallback = await listFallback(tab, uid, triedList);
        if (!fallback.found) break;
        triedList.add(fallback.key);
      } else {
        unresolvedCandidates += 1;
        searchFailure = next.reason || "search_result_not_confirmed";
        break;
      }
      const openedConversation = await measure("identity_read_ms", () => readOpenedConversation(tab, uid, deadlineAt, {
        read: snapshot, authorizedAccountIds: authorized,
        snapshotOptions: {messageTextLimit: analysisMode && analysisTarget ? 0 : analysisMode ? analysisRemaining : null},
      }));
      let openedRaw = openedConversation.raw;
      const initial = openedConversation.review;
      lastIdentityDiagnostics = initial.identity_diagnostics;
      if (openedConversation.reason) return {status: "blocked", reason: openedConversation.reason, uid,
        reviews: [...reviews.values()], token: "", coverage_complete: false, search_complete: false,
        search_diagnostics: searchDiagnostics, identity_diagnostics: lastIdentityDiagnostics};
      if (!initial.identity_verified) {
        const key = next.candidate ? popupKey(next.candidate) : openedRaw?.activeKey || "unknown";
        const failures = (candidateReadFailures.get(key) || 0) + 1;
        candidateReadFailures.set(key, failures);
        if (failures >= 2) {
          unresolvedCandidates += 1;
          searchFailure = openedConversation.reason || "conversation_identity_not_ready";
          break;
        }
        continue;
      }
      if (next.candidate) triedPopup.add(popupKey(next.candidate));
      if (next.candidate) knownCandidates.set(initial.account_id, popupKey(next.candidate));
      if (reviews.has(initial.account_id)) continue;
      const account = initial.account_id;
      if (allScope && !required.includes(account)) required.push(account);
      if (allScope && !authorized.includes(account)) authorized.push(account);
      if (!analysisMode && (initial.label === "lead" || initial.label === "no_intent" || initial.platform_lead_signal)) {
        globalStop = initial.label === "no_intent" ? "no_intent" : "lead_protection";
        reviews.set(account, initial);
        rawByAccount.set(account, openedRaw);
        break;
      }
      const accountMessageBudget = analysisMode && analysisTarget && account !== analysisTarget
        ? 0 : analysisRemaining;
      if (analysisMode && analysisTarget && account === analysisTarget) {
        openedRaw = await snapshot(uid, { messageTextLimit: accountMessageBudget });
      }
      const cache = historyCache[account] || {};
      const history = await measure("history_read_ms", () => loadHistory(tab, uid, account, cache, deadlineAt, {
        analysisMode, nowMs: now(), maxRealMessages: accountMessageBudget,
        initialRaw: openedRaw, read: snapshot,
      }));
      if (cache.boundary_message_id && history.priorBoundaryReached) totals.history_cache_hits += 1;
      else {
        totals.history_cache_misses += 1;
        const reason = cache.boundary_message_id ? "prior_boundary_not_reached" : "no_approved_boundary";
        historyCacheReasons[reason] = (historyCacheReasons[reason] || 0) + 1;
      }
      totals.history_load_clicks += history.loads;
      const observedRaw = { ...history.raw, analysisTruncated: history.analysisTruncated };
      const review = buildReview(observedRaw, {
        uid, accountId: account, authorizedAccountIds: authorized,
        historyComplete: history.complete,
        historyFromStartComplete: history.fromStartComplete,
        historyReachedPriorBoundary: history.priorBoundaryReached,
        cachedOutboundTextHashes: cache.outbound_text_hashes || [],
        analysisMode,
        analysisMessageLimit: accountMessageBudget,
        nowMs: now(),
      });
      if (analysisMode) analysisRemaining = Math.max(0, analysisRemaining - review.messages.length);
      reviews.set(account, review);
      rawByAccount.set(account, observedRaw);
      if (history.failureReason) searchFailure = history.failureReason;
      // A single exact result is the common path: no second UID search.
      if (required.every((accountId) => reviews.has(accountId)) &&
          (next.exhausted || (next.popup?.maxTop === 0 && next.popup?.entries.length > 0 &&
            (next.popup.rawCount === undefined || next.popup.rawCount === next.popup.entries.length) &&
            next.popup.entries.every(entry => triedPopup.has(popupKey(entry)))))) {
        popupExhausted = true;
        break;
      }
    }
    for (const accountId of required) {
      if (!reviews.has(accountId)) reviews.set(accountId, buildReview({}, {
        uid, accountId, authorizedAccountIds: authorized, historyComplete: false,
      }));
    }
    const unexpectedAccounts = [...reviews.keys()].filter((accountId) => !required.includes(accountId));
    const reviewArray = required.map((accountId) => reviews.get(accountId));
    const selectedAccountId = analysisTarget || sendTarget || (allScope ? reviewArray.at(-1)?.account_id : "") || chooseSendingAccount(reviewArray, {
      authorizedAccountIds: authorized, lastSenderAccountId,
    });
    let token = "";
    if (!analysisMode && !globalStop && selectedAccountId && rawByAccount.has(selectedAccountId)) {
      let selectedRaw = rawByAccount.get(selectedAccountId);
      const current = await snapshot(uid);
      const currentReview = buildReview(current, { uid, accountId: selectedAccountId, authorizedAccountIds: authorized });
      let senderIsOpen = currentReview.identity_verified;
      if (!currentReview.identity_verified) {
        const located = await measure("selection_ms", () => locateKnownAccount(tab, uid, selectedAccountId, deadlineAt, totals, knownCandidates.get(selectedAccountId)));
        if (located) {
          selectedRaw = located;
          senderIsOpen = true;
        }
      } else selectedRaw = current;
      const finalIdentity = buildReview(selectedRaw, {
        uid, accountId: selectedAccountId, authorizedAccountIds: authorized,
      });
      if (senderIsOpen && finalIdentity.identity_verified &&
          finalIdentity.history_boundary_message_id === reviews.get(selectedAccountId)?.history_boundary_message_id) {
        token = adoptObservedSelection({
          uid, accountId: selectedAccountId, snapshot: selectedRaw,
          review: reviews.get(selectedAccountId),
          allReviews: reviewArray,
        });
      }
    }
    const sampleComplete = analysisMode && popupExhausted && unresolvedCandidates === 0 &&
      unexpectedAccounts.length === 0 &&
      required.every((accountId) => reviews.get(accountId)?.identity_verified && reviews.get(accountId)?.panel_loaded) &&
      (!analysisTarget || reviews.get(analysisTarget)?.messages.length > 0);
    const coverageComplete = !analysisMode && popupExhausted && unresolvedCandidates === 0 && unexpectedAccounts.length === 0 &&
      required.every((accountId) => reviews.get(accountId)?.identity_verified &&
        reviews.get(accountId)?.panel_loaded && reviews.get(accountId)?.history_complete) &&
      !!token;
    if (!coverageComplete) {
      token = "";
      selected = null;
    }
    const finishedAt = now();
    return {
      status: globalStop ? "stopped" : sampleComplete ? "sampled" : coverageComplete ? "observed" : "deferred",
      reason: globalStop || searchFailure || (sampleComplete || coverageComplete ? "" : unexpectedAccounts.length ? "unexpected_account_requires_queue_refresh" :
        analysisTarget && reviews.get(analysisTarget)?.identity_verified &&
          reviews.get(analysisTarget)?.panel_loaded && !reviews.get(analysisTarget)?.messages.length
          ? "analysis_target_chat_unavailable" :
        "account_or_history_coverage_incomplete"),
      uid,
      reviews: reviewArray,
      unexpected_account_ids: unexpectedAccounts,
      selected_account_id: selectedAccountId || reviewArray[0]?.account_id || "",
      discovered_account_ids: required,
      search_complete: popupExhausted && unresolvedCandidates === 0,
      token,
      coverage_complete: coverageComplete,
      finished_at_ms: finishedAt,
      search_diagnostics: searchDiagnostics,
      identity_diagnostics: lastIdentityDiagnostics,
      performance: {
        elapsed_ms: finishedAt - started,
        search_opens: searchAttempts,
        history_load_clicks: totals.history_load_clicks - beforeCounters.history_load_clicks,
        snapshot_reads: totals.snapshot_reads - beforeCounters.snapshot_reads,
        page_recoveries: totals.page_recoveries - beforeCounters.page_recoveries,
        ...Object.fromEntries(["page_prepare_ms", "search_ms", "identity_read_ms", "history_read_ms", "selection_ms",
          "history_cache_hits", "history_cache_misses"].map(key => [key, totals[key] - beforeCounters[key]])),
        history_cache_reread_reasons: historyCacheReasons,
      },
    };
  }

  function confirmBusinessReview({token, decisions} = {}) {
    if (!selected || token !== selected.token) throw new Error("selection_token_mismatch");
    const updates = new Map((decisions || []).map((d) => [d.account_id, d]));
    if (updates.size !== selected.allReviews.length) throw new Error("business_review_incomplete");
    selected.allReviews = selected.allReviews.map((review) => {
      const decision = updates.get(review.account_id);
      if (!decision || typeof decision.contact_shared !== "boolean" || typeof decision.stop_contact !== "boolean") {
        throw new Error("business_decision_required");
      }
      if (decision.stop_contact && (!norm(decision.evidence) || !review.messages.some((message) =>
          message.side === "left" && message.text.includes(decision.evidence)))) {
        throw new Error("stop_contact_customer_quote_required");
      }
      return {...review, business_reviewed: true,
        protective_lead_signal: !!review.platform_lead_signal || decision.contact_shared,
        negative_status: decision.stop_contact ? "blocked" : "safe",
        contact_evidence: decision.contact_shared ? String(decision.evidence || "") : "",
        stop_contact_evidence: decision.stop_contact ? decision.evidence : ""};
    });
    selected.review = selected.allReviews.find((r) => r.account_id === selected.accountId);
    return selected.allReviews;
  }

  async function selectReviewedAccount({token, accountId, budgetMs = 15000} = {}) {
    if (!selected || token !== selected.token) return {status: "deferred", reason: "selection_token_mismatch"};
    const baseline = selected.allReviews.find((review) => review.account_id === accountId);
    if (!baseline || now()-baseline.observed_at_ms > 120000) return {status: "deferred", reason: "account_reviews_stale_reobserve_required"};
    const allReviews = selected.allReviews;
    const uid = selected.uid;
    const raw = await locateKnownAccount(tab, uid, accountId, now()+budgetMs, totals, knownCandidates.get(accountId));
    if (!raw) return {status: "deferred", reason: "reviewed_account_not_found"};
    const fresh = revalidationReview(raw, baseline);
    if (!samePreflight(baseline, fresh)) return {status: "deferred", reason: "page_or_customer_changed"};
    const reviews = allReviews.map((review) => review.account_id === accountId ? fresh : review);
    const nextToken = adoptObservedSelection({uid, accountId, snapshot: raw, review: fresh, allReviews: reviews});
    return {status: "observed", token: nextToken, reviews, selected_account_id: accountId,
      discovered_account_ids: reviews.map((r) => r.account_id), search_complete: true, coverage_complete: true};
  }

  async function revalidateAndStage({ token, prepared } = {}) {
    if (!selected || token !== selected.token) return { status: "not_staged", reason: "selection_token_mismatch" };
    if (prepared?.action !== "send" || prepared.account_id !== selected.accountId || !prepared.touch_id || !norm(prepared.message)) {
      return { status: "not_staged", reason: "prepare_touch_not_authorized" };
    }
    if (selected.allReviews.some((r) => r.business_reviewed !== true || r.negative_status !== "safe" ||
        r.protective_lead_signal || r.label !== "none" || r.no_intent_label)) {
      return { status: "not_staged", reason: "business_review_not_clear" };
    }
    const oldestOtherAccountReview = Math.min(...selected.allReviews.map((review) => Number(review.observed_at_ms || 0)));
    const freshnessLimit = 120000;
    if (!oldestOtherAccountReview || now() - oldestOtherAccountReview > freshnessLimit) {
      return { status: "not_staged", reason: "account_reviews_stale_reobserve_required" };
    }
    const message = String(prepared.message);
    if (message.length > 500) return { status: "not_staged", reason: "message_exceeds_platform_limit" };
    const before = await snapshot(selected.uid);
    const current = revalidationReview(before, selected.review);
    if (prepared.source_sensitive && current.source !== prepared.source) {
      return { status: "not_staged", reason: "attribution_changed_for_distinct_copies" };
    }
    if (!samePreflight(selected.review, current) || current.label !== "none" || current.no_intent_label || current.protective_lead_signal || current.draft_present) {
      return { status: "not_staged", reason: current.draft_present ? "user_draft_present" : "page_or_customer_changed" };
    }
    const input = tab.playwright.getByPlaceholder(REPLY_PLACEHOLDER, { exact: true });
    if (await input.count() !== 1) return { status: "not_staged", reason: "reply_input_not_unique" };
    await input.fill(message);
    const filled = await snapshot(selected.uid);
    const filledReview = revalidationReview(filled, current);
    if (filled.draft !== message || !samePreflight(current, filledReview) ||
        (prepared.source_sensitive && filledReview.source !== prepared.source)) {
      staged = null;
      if (!filledReview.identity_verified || filledReview.account_id !== selected.accountId ||
          filled.draft !== message) {
        return { status: "uncertain", reason: "identity_or_draft_changed_after_fill",
          touch_id: prepared.touch_id };
      }
      // Recheck immediately before clearing. A changed draft may be the user's
      // own work; only remove the exact text we just filled in this same chat.
      const beforeClear = await snapshot(selected.uid);
      const sameConversation = buildReview(beforeClear, {
        uid: selected.uid, accountId: selected.accountId,
        authorizedAccountIds: [selected.accountId],
      }).identity_verified;
      if (!sameConversation || beforeClear.draft !== message) {
        return { status: "uncertain", reason: "identity_or_draft_changed_before_cleanup",
          touch_id: prepared.touch_id };
      }
      try {
        await input.fill("");
      } catch (_error) {
        return { status: "uncertain", reason: "auto_draft_cleanup_unverified",
          touch_id: prepared.touch_id };
      }
      const cleared = await snapshot(selected.uid);
      const clearedSameConversation = buildReview(cleared, {
        uid: selected.uid, accountId: selected.accountId,
        authorizedAccountIds: [selected.accountId],
      }).identity_verified;
      return clearedSameConversation && cleared.draft === ""
        ? { status: "not_staged", reason: "preflight_changed_auto_draft_cleared",
          touch_id: prepared.touch_id }
        : { status: "uncertain", reason: "auto_draft_cleanup_unverified",
          touch_id: prepared.touch_id };
    }
    staged = {
      token: `stage-${++sequence}-${now()}`,
      selectionToken: token,
      touchId: prepared.touch_id,
      uid: selected.uid,
      accountId: selected.accountId,
      message,
      before: filled,
      review: filledReview,
      sourceSensitive: !!prepared.source_sensitive,
    };
    return { status: "staged", stageToken: staged.token, touch_id: staged.touchId, uid: staged.uid,
      account_id: staged.accountId };
  }

  async function clickAndVerify({ stageToken, markClicked } = {}) {
    if (!staged || staged.token !== stageToken) return { status: "not_clicked", reason: "stage_token_mismatch" };
    if (markClicked?.ok !== true || markClicked?.state !== "clicked" || markClicked?.touch_id !== staged.touchId) {
      return { status: "not_clicked", reason: "mark_clicked_not_durable" };
    }
    const before = await snapshot(staged.uid);
    const current = revalidationReview(before, staged.review);
    if (before.draft !== staged.message || !samePreflight(staged.review, current) || current.label !== "none" ||
        (staged.sourceSensitive && current.source !== staged.review.source)) {
      staged = null;
      return { status: "not_clicked", reason: "page_changed_after_mark_clicked", touch_id: markClicked.touch_id };
    }
    const button = tab.playwright.getByRole("button", { name: "发送", exact: true });
    if (await button.count() !== 1 || !(await button.isEnabled())) {
      staged = null;
      return { status: "not_clicked", reason: "send_button_unavailable", touch_id: markClicked.touch_id };
    }
    const attempted = staged;
    const beforePageState = await tab.playwright.domSnapshot();
    const priorNoticeCounts = new Map();
    for (const notice of relevantReceiptNotices(before.toasts)) {
      const key = toastKey(notice);
      priorNoticeCounts.set(key, (priorNoticeCounts.get(key) || 0) + 1);
    }
    const capturedNotices = new Map();
    staged = null;
    const clickedAtMs = now();
    try {
      await button.click();
    } catch (error) {
      return { status: "uncertain", reason: connectionFailure(error) ? "browser_connection_unavailable" : "click_result_unknown", touch_id: attempted.touchId,
        detail: String(error?.message || error).slice(0, 160) };
    }
    try {
      const receiptStarted = now();
      // Counted reads can finish in milliseconds on a fast local bridge. Bound
      // the actual elapsed window; confirmed success/refusal exits immediately.
      const receiptBudget = Math.min(15000, Math.max(1000, Number(receiptWindowMs) || 8000));
      const pollMs = Math.min(1000, Math.max(100, Number(receiptPollMs) || 250));
      const maxReads = Math.ceil(receiptBudget / pollMs) + 2;
      for (let i = 0; i < maxReads; i += 1) {
        const pageState = await tab.playwright.domSnapshot();
        for (const [reason, pattern] of SEND_REFUSALS) {
          const notice = pageState.match(pattern);
          if (notice && !beforePageState.includes(notice[0]) &&
              !(before.toasts || []).some(prior => norm(prior.text).includes(norm(notice[0])))) return {
            status: "failed", touch_id: attempted.touchId, reason, detail: notice[0],
            platform_notices: [{ text: notice[0], severity: "error", source: "accessibility",
              observed_at_ms: now() }],
          };
        }
        const after = await snapshot(attempted.uid);
        const currentNoticeCounts = new Map();
        for (const notice of relevantReceiptNotices(after.toasts)) {
          const key = toastKey(notice);
          const occurrence = (currentNoticeCounts.get(key) || 0) + 1;
          currentNoticeCounts.set(key, occurrence);
          if (occurrence > (priorNoticeCounts.get(key) || 0) && !capturedNotices.has(key)) {
            capturedNotices.set(key, { ...notice, text: norm(notice.text).slice(0, 600), observed_at_ms: now() });
          }
        }
        const platformNotices = [...capturedNotices.values()];
        const refusal = platformNotices.flatMap(notice => SEND_REFUSALS.flatMap(([reason, pattern]) =>
          pattern.test(notice.text) ? [{ reason, notice }] : []))[0];
        if (refusal) return { status: "failed", touch_id: attempted.touchId, reason: refusal.reason,
          detail: refusal.notice.text, platform_notices: platformNotices };
        const result = verifyNewOutbound(before, after, attempted.message, attempted.uid, attempted.accountId);
        if (result.ok) {
          if (selected && selected.uid === attempted.uid && selected.accountId === attempted.accountId) {
            const refreshed = {...buildReview(after, {uid: attempted.uid, accountId: attempted.accountId,
              authorizedAccountIds: [attempted.accountId], historyComplete: true}),
              history_from_start_complete: attempted.review.history_from_start_complete,
              history_reached_prior_boundary: attempted.review.history_reached_prior_boundary,
              outbound_text_hashes: [...new Set([...attempted.review.outbound_text_hashes, hashMessage(attempted.message)])],
              business_reviewed: true, negative_status: attempted.review.negative_status,
              protective_lead_signal: attempted.review.protective_lead_signal};
            selected.review = refreshed;
            selected.allReviews = selected.allReviews.map((r) => r.account_id === attempted.accountId ? refreshed : r);
          }
          return { status: "sent", touch_id: attempted.touchId, account_id: attempted.accountId,
            platform_notices: platformNotices, ...result };
        }
        if (result.reason === "identity_changed_after_click") return { status: "uncertain", touch_id: attempted.touchId,
          reason: result.reason, identity_diagnostics: identityDiagnostics(after, attempted.uid, attempted.accountId),
          platform_notices: platformNotices };
        const priorFailures = new Set((before.failedMessages || []).map(m => m.id));
        const failedBubble = (after.failedMessages || []).find(m => m.id && !priorFailures.has(m.id) &&
          Number(m.timestamp || 0) >= clickedAtMs &&
          (parseMessageAccount(m.id, attempted.uid) === attempted.accountId || /^(?:id_|jarvis-msg-)\d{13}[-.]/.test(m.id)) &&
          normalizeMessageForHash(m.text) === normalizeMessageForHash(attempted.message));
        if (failedBubble) return { status: "failed", touch_id: attempted.touchId,
          reason: "platform_send_failed", failed_message_id: failedBubble.id,
          detail: platformNotices.map(notice => notice.text).join("；") ||
            "本次消息明确显示“重新发送”；未捕获具体平台拒发提示。",
          platform_notices: platformNotices };
        const remaining = receiptBudget - (now() - receiptStarted);
        if (remaining <= 0) break;
        await wait(Math.min(pollMs, remaining));
      }
    } catch (error) {
      return { status: "uncertain", touch_id: attempted.touchId, reason: connectionFailure(error) ? "browser_connection_unavailable" : "receipt_read_failed",
        detail: String(error?.message || error).slice(0, 160), platform_notices: [...capturedNotices.values()] };
    }
    return { status: "uncertain", touch_id: attempted.touchId, reason: "receipt_not_verified",
      platform_notices: [...capturedNotices.values()] };
  }

  async function readOnlyPilot(items, { limit = 3, budgetMsPerUid = 30000, maxMs = 3 * 60 * 1000 } = {}) {
    const started = now();
    const results = [];
    for (const item of (items || []).slice(0, Math.min(20, Math.max(0, Number(limit) || 0)))) {
      if (now() - started >= maxMs) break;
      try {
        const result = await observeUid({
          uid: item.user_id || item.uid,
          scopeMode: item.scope_mode || "all",
          authorizedAccountIds: item.authorized_account_ids || item.account_ids,
          relevantAccountIds: item.account_ids,
          historyCache: item.history_cache || {},
          analysisMode: false,
          budgetMs: Math.min(budgetMsPerUid, maxMs - (now() - started)),
        });
        results.push({ uid: item.user_id || item.uid, status: result.status, reason: result.reason || "",
          elapsed_ms: result.performance?.elapsed_ms || 0,
          search_opens: result.performance?.search_opens || 0,
          history_load_clicks: result.performance?.history_load_clicks || 0 });
        if (results.length >= 3 && results.slice(-3).every((r) => r.status === "deferred")) break;
        if (["user_draft_present", "private_message_page_unavailable"].includes(result.reason)) break;
      } catch (_error) {
        results.push({ uid: item.user_id || item.uid, status: "deferred", reason: "browser_use_interrupted",
          elapsed_ms: 0, search_opens: 0, history_load_clicks: 0 });
        break;
      }
    }
    const elapsed = results.map((result) => result.elapsed_ms).filter((value) => value > 0);
    return {
      read_only: true,
      requested: Math.min(20, Math.max(0, Number(limit) || 0), (items || []).length),
      checked: results.length,
      observed: results.filter((result) => result.status === "observed").length,
      stopped: results.filter((result) => result.status === "stopped").length,
      deferred: results.filter((result) => result.status === "deferred").length,
      results,
      elapsed_ms: now() - started,
      p50_ms: percentile(elapsed, 0.5),
      p95_ms: percentile(elapsed, 0.95),
      search_opens: results.reduce((sum, result) => sum + result.search_opens, 0),
      history_load_clicks: results.reduce((sum, result) => sum + result.history_load_clicks, 0),
      reasons: Object.fromEntries([...new Set(results.map((result) => result.reason).filter(Boolean))]
        .map((reason) => [reason, results.filter((result) => result.reason === reason).length])),
    };
  }

  return { ensurePage, observeUid, probeUid, adoptObservedSelection, confirmBusinessReview, selectReviewedAccount, revalidateAndStage, clickAndVerify,
    readOnlyPilot, performance: () => ({ ...totals }) };
}

async function locateKnownAccount(tab, uid, accountId, deadlineAt, totals, preferredKey = "") {
  const tried = new Set();
  for (let i = 0; i < 20 && Date.now() < deadlineAt - 1000; i += 1) {
    const opened = await openSearch(tab, uid, { deadlineAt });
    totals.search_opens += 1;
    if (!opened.ok) return null;
    const next = await nextPopupCandidate(tab, tried, deadlineAt, {initialPopup: opened.popup, preferredKey});
    if (!next.candidate) return null;
    const clicked = await clickPopupCandidate(tab, next.candidate);
    if (!clicked.ok) return null;
    tried.add(popupKey(next.candidate));
    await tab.playwright.domSnapshot();
    const {raw} = await readOpenedConversation(tab, uid, deadlineAt, {accountId,
      authorizedAccountIds: [accountId]});
    if (buildReview(raw, { uid, accountId, authorizedAccountIds: [accountId] }).identity_verified) return raw;
  }
  return null;
}
