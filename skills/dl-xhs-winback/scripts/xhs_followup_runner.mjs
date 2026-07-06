import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import path from "node:path";
import fs from "node:fs";

const execFileAsync = promisify(execFile);

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const SKILL_DIR = path.dirname(SCRIPT_DIR);

function readJsonIfExists(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return {};
  }
}

function expandHome(dirPath) {
  if (dirPath.startsWith("~/") || dirPath === "~") {
    return path.join(process.env.HOME || "", dirPath.slice(1));
  }
  return dirPath;
}

// 账号 ID、工作区路径都从 config.json 读取（示例见 config.example.json）；
// 没有 config.json 时都是空/默认占位值，调用方必须显式传入账号 ID。
// XHS_FOLLOWUP_CONFIG 可覆盖 config.json 路径（主要用于测试注入夹具配置）。
const CONFIG_PATH = process.env.XHS_FOLLOWUP_CONFIG
  ? path.resolve(process.env.XHS_FOLLOWUP_CONFIG)
  : path.join(SKILL_DIR, "config.json");
const SKILL_CONFIG = readJsonIfExists(CONFIG_PATH);

const DEFAULT_STATE_SCRIPT = path.join(SCRIPT_DIR, "campaign_state.py");
const DEFAULT_DASHBOARD_PATH = path.join(
  expandHome(SKILL_CONFIG.workspace_dir || "~/xhs-followup-workspace"),
  "reports",
  "xhs-followup",
  "dashboard.html",
);
const DEFAULT_ACCOUNT_IDS = (SKILL_CONFIG.accounts || [])
  .map((account) => account.id)
  .filter(Boolean);
const DASHBOARD_MODES = new Set(["off", "final", "always"]);

const STRONG_LEAD_RE = /留客资|对方已点击你的企业微信联系卡|对方已提交信息|访客联系方式已自动记录至「聚光平台-线索管理」|联系方式已自动记录/;
const PLATFORM_LIMIT_RE = /对方关注或回复你之前，?24小时内最多只能发1条文字消息/;
const CONTACT_RE = /(?:1[3-9]\d[\d\s\-*]{7,}\d)|(?:微信(?:号|账号)?\s*[:：]?\s*[A-Za-z0-9_-]*\*+[A-Za-z0-9_-]*)|(?:手机(?:号|号码)?\s*[:：]?\s*1\d{2}\*{2,}\d{2,})/;
const NEGATIVE_RE = /不需要|不用了|别联系|不要联系|别发|别再发|拉黑|投诉|举报|骗子|被骗|骚扰|退订|没兴趣|不感兴趣|滚/;
const PEER_RE = /(?:我是|我们是|这边是|主营|提供|专做|也做).{0,18}(?:代运营|广告|投放|投流|接广|互推|SaaS|软件|系统|培训|课程|招商|代理|服务商|获客服务|询盘服务)|(?:代运营|广告投放|投流|接广|互推|SaaS|软件系统|培训讲师|招商代理|服务商).{0,18}(?:合作|推广|报价|资源|服务)/i;
const SUSPICIOUS_SERVICE_PROFILE_RULES = [
  [/(?:企业出海服务|出海服务|海外落地|海外增长|跨境增长|出海拓客|获客|拓客|询盘|线索|资源对接|项目资源对接|项目对接|私域|引流|代运营|服务商|招商|代理|接广|互推)/i, "commercial_service_profile"],
  [/(?:AI|AIGC|ChatGPT).{0,12}(?:创作|工具|营销|获客|出海|落地|实战|自动化)|(?:创作|营销|获客).{0,12}(?:工具|系统|自动化)/i, "ai_tool_or_marketing_profile"],
  [/(?:SaaS|软件|系统|ERP|CRM|独立站|建站).{0,12}(?:服务|工具|系统|解决方案|搭建|顾问|咨询|获客|营销)|(?:系统|软件).{0,8}(?:开发|搭建|服务)/i, "software_or_system_service_profile"],
  [/(?:谈|聊|教|讲|说|看|研究).{0,8}(?:外贸|跨境|出海|海外社媒|TikTok|获客|询盘)|(?:外贸|跨境|出海).{0,10}(?:之路|日记|实战|笔记|增长|方法|观察|看世界|老鹰|顾问|教练|导师)/i, "industry_content_profile"],
];
const UNREADABLE_LEAD_RISK_TYPES = new Set(["IMAGE", "PICTURE", "VIDEO", "FILE"]);
const CONVERSATION_KEY_PREFIXES = new Set(["Total", "Active", "Favorite"]);
const SCAN_MODES = new Set(["strict", "fastSeek"]);
// 人工复核覆盖表默认清空；真实覆盖记录建议只保存在本地 overrides.json（已加入 .gitignore），
// 格式同这个常量：{ "<真实UID>": { party_status: "peer", evidence: "..." } }。
const MANUAL_USER_OVERRIDES = readJsonIfExists(path.join(SKILL_DIR, "overrides.json"));

function normalizeText(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function compactText(value) {
  return normalizeText(value).replace(/\s+/g, "");
}

function formatDateInShanghai(date) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return values.year + "-" + values.month + "-" + values.day;
}

function shiftDateInShanghai(now, days) {
  return formatDateInShanghai(new Date(now.getTime() + days * 24 * 60 * 60 * 1000));
}

function extractConversationDate(text, { now = new Date() } = {}) {
  const value = normalizeText(text);
  if (!value) return "";
  const explicit = value.match(/(20\d{2})[./-](\d{1,2})[./-](\d{1,2})/);
  if (explicit) {
    return explicit[1] + "-" + explicit[2].padStart(2, "0") + "-" + explicit[3].padStart(2, "0");
  }
  const zhMonthDay = value.match(/(\d{1,2})月(\d{1,2})日/);
  if (zhMonthDay) {
    const year = formatDateInShanghai(now).slice(0, 4);
    return year + "-" + zhMonthDay[1].padStart(2, "0") + "-" + zhMonthDay[2].padStart(2, "0");
  }
  const slashMonthDay = value.match(/(?:^|\D)(\d{1,2})[/-](\d{1,2})(?:\D|$)/);
  if (slashMonthDay) {
    const year = formatDateInShanghai(now).slice(0, 4);
    return year + "-" + slashMonthDay[1].padStart(2, "0") + "-" + slashMonthDay[2].padStart(2, "0");
  }
  if (/前天/.test(value)) return shiftDateInShanghai(now, -2);
  if (/昨天/.test(value)) return shiftDateInShanghai(now, -1);
  if (/刚刚|今天|\d+\s*分钟前|\d+\s*小时前/.test(value)) return formatDateInShanghai(now);
  return "";
}

function resolveScanScrollPages({ scanMode = "strict", scrollPages } = {}) {
  if (scanMode === "fastSeek") {
    const value = Number.isFinite(Number(scrollPages)) ? Number(scrollPages) : 6;
    return Math.max(1, Math.min(10, value));
  }
  return 0.82;
}

function resolveBypassScanGate({ allowIncompleteScan = false, resumeOldQueue = false } = {}) {
  return Boolean(allowIncompleteScan || resumeOldQueue);
}

async function runWithTimeout(promise, timeoutMs, fallbackFactory) {
  // Use only for non-mutating/background work. Browser click/type/search flows must
  // use cooperative deadlines; Promise.race cannot cancel the underlying page action.
  const ms = Math.max(1, Number(timeoutMs) || 1);
  let timeoutId;
  const timeout = new Promise((resolve) => {
    timeoutId = setTimeout(() => resolve(fallbackFactory()), ms);
  });
  try {
    return await Promise.race([promise, timeout]);
  } finally {
    clearTimeout(timeoutId);
  }
}

function deadlineExceeded(deadlineAt = 0, reserveMs = 0, now = Date.now()) {
  const deadline = Number(deadlineAt || 0);
  if (!Number.isFinite(deadline) || deadline <= 0) return false;
  return Number(now) + Math.max(0, Number(reserveMs) || 0) >= deadline;
}

function resolveUidFailureFallback({
  skipListFallbackOnUidFail = false,
  maxMs = 180000,
  elapsedMs = 0,
} = {}) {
  if (skipListFallbackOnUidFail) {
    return {
      useListFallback: false,
      reason: "skipped_by_operator",
      listFallbackMs: 0,
    };
  }
  const remainingMs = Number(maxMs) - Number(elapsedMs);
  return {
    useListFallback: true,
    reason: "",
    listFallbackMs: Math.max(5000, Math.min(60000, remainingMs)),
  };
}

function buildScanProgress({
  view,
  seenConversationKeys = 0,
  loadedResumeSeenKeys = 0,
  scanMode = "strict",
  now = new Date(),
} = {}) {
  const dates = (view && Array.isArray(view.items) ? view.items : [])
    .map((item) => extractConversationDate(item && item.text, { now }))
    .filter(Boolean)
    .sort();
  const scroller = view && view.scroller ? view.scroller : {};
  const scrollTop = Math.round(Number(scroller.scrollTop || 0));
  const scrollHeight = Math.round(Number(scroller.scrollHeight || 0));
  const clientHeight = Math.round(Number(scroller.clientHeight || 0));
  const distanceToBottom = Math.max(0, scrollHeight - scrollTop - clientHeight);
  const scrollable = Math.max(0, scrollHeight - clientHeight);
  return {
    scanMode,
    visibleNewestDate: dates.length ? dates[dates.length - 1] : "",
    visibleOldestDate: dates.length ? dates[0] : "",
    scrollTop,
    scrollHeight,
    clientHeight,
    distanceToBottom,
    scrollPercent: scrollable ? Math.round((scrollTop / scrollable) * 10000) / 100 : 0,
    atBottom: !!scroller.atBottom,
    seenConversationKeys,
    loadedResumeSeenKeys,
  };
}

function currentMonthWindowReached({ scanProgress, windowStartDate } = {}) {
  const visibleOldestDate = String(scanProgress && scanProgress.visibleOldestDate || "");
  const target = String(windowStartDate || "");
  return /^\d{4}-\d{2}-\d{2}$/.test(visibleOldestDate) &&
    /^\d{4}-\d{2}-\d{2}$/.test(target) &&
    visibleOldestDate <= target;
}

function bottomCompletionSignature(view, { now = new Date() } = {}) {
  const items = view && Array.isArray(view.items) ? view.items : [];
  const scroller = view && view.scroller ? view.scroller : {};
  const visibleDates = items
    .map((item) => extractConversationDate(item && item.text, { now }))
    .filter(Boolean)
    .sort();
  return [
    items.map((item) => item && item.key || "").join("|"),
    visibleDates[0] || "",
    visibleDates[visibleDates.length - 1] || "",
    Math.round(Number(scroller.scrollTop || 0)),
    Math.round(Number(scroller.scrollHeight || 0)),
    Math.round(Number(scroller.clientHeight || 0)),
    scroller.atBottom ? "bottom" : "not-bottom",
  ].join("::");
}

function compactTextForSendVerification(value) {
  return compactText(value).replace(/\[[^\]]+R\]/g, "");
}

function sentMessageTextMatches(actual, expectedText, expectedCompact, expectedCompactForVerification) {
  const actualText = normalizeText(actual);
  const actualCompact = compactText(actual);
  return actualText === expectedText ||
    actualCompact === expectedCompact ||
    compactTextForSendVerification(actual) === expectedCompactForVerification;
}

function serviceProfileSignal(value) {
  const text = normalizeText(value).replace(/留客资/g, "");
  if (!text) return "";
  for (const [pattern, reason] of SUSPICIOUS_SERVICE_PROFILE_RULES) {
    if (pattern.test(text)) return reason;
  }
  return "";
}

function customerClassifiableText(messages) {
  return normalizeText(messages
    .filter((m) => {
      if (!m || m.side !== "left") return false;
      const type = String(m.type || "").toUpperCase();
      return type !== "CARD" && type !== "HINT" && type !== "RICH_HINT";
    })
    .map((m) => m.text || "")
    .join(" "));
}

function identityFailureReason(identity, fallback = "pre_send_identity_failed") {
  if (!identity) return fallback;
  if (identity.rightPanelUid && identity.expectedUserId && identity.rightPanelUid !== identity.expectedUserId) {
    return fallback + ":right_panel_uid_mismatch";
  }
  if (identity.expectedUserId && !identity.rightPanelUid) {
    return fallback + ":right_panel_uid_missing";
  }
  const observedAccountIds = Array.isArray(identity.observedAccountIds)
    ? identity.observedAccountIds.map((item) => item && item.accountId).filter(Boolean)
    : [];
  if (identity.expectedAccountId && observedAccountIds.length && !observedAccountIds.includes(identity.expectedAccountId)) {
    return fallback + ":opened_other_account";
  }
  if (identity.expectedAccountId && !identity.hasExactExpectedAccountMessage) {
    return fallback + ":expected_account_not_present";
  }
  return fallback;
}

function identityAllowedForSend(identity, allowedAccountIdSet) {
  return !!identity &&
    identity.rightPanelUid === identity.expectedUserId &&
    !!identity.inferredAccountId &&
    allowedAccountIdSet.has(identity.inferredAccountId);
}

function identityMatchesSearch(identity, allowedAccountIdSet) {
  if (!identity) return false;
  if (allowedAccountIdSet && allowedAccountIdSet.size) {
    return identityAllowedForSend(identity, allowedAccountIdSet);
  }
  return !!identity.identityOk;
}

function canonicalConversationKey(userId, accountId, fallback = "") {
  if (userId && accountId) return "Total-" + userId + "-" + accountId;
  return fallback || "";
}

function isPreClickSendTechnicalReason(reason) {
  const text = String(reason || "");
  return text === "send_identity_not_confirmed_before_type" ||
    text === "send_input_value_mismatch" ||
    text === "send_button_disabled" ||
    text.startsWith("send_input_count_") ||
    text.startsWith("send_button_count_");
}

function cssAttr(value) {
  return String(value).replace(/\\/g, "\\\\").replace(/"/g, "\\\"");
}

function parseConversationKey(key) {
  const parts = String(key || "").split("-");
  if (!CONVERSATION_KEY_PREFIXES.has(parts[0]) || parts.length < 2 || !parts[1]) {
    return { ok: false, prefix: parts[0] || "", userId: "", accountId: "", reason: "invalid_conversation_key" };
  }
  return {
    ok: true,
    prefix: parts[0],
    userId: parts[1],
    accountId: parts.length >= 3 ? parts.slice(2).join("-") : "",
    reason: "",
  };
}

function parseMessageIdentity(id, userId) {
  const raw = String(id || "").replace(/^jarvis-msg-/, "");
  const parts = raw.split(".");
  if (parts.length < 3) return null;
  if (parts[0] === userId) return { userId, accountId: parts[1], order: "user-account" };
  if (parts[1] === userId) return { userId, accountId: parts[0], order: "account-user" };
  return null;
}

function normalizeAccountIds(value) {
  if (value == null) return DEFAULT_ACCOUNT_IDS.slice();
  const input = Array.isArray(value) ? value : String(value).split(/[\n,;]+/);
  const ids = [];
  const seen = new Set();
  for (const item of input) {
    const id = String(item || "").trim();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    ids.push(id);
  }
  return ids;
}

function isRealDialogMessage(message) {
  if (!message || !["left", "right"].includes(message.side)) return false;
  if (!message.text || PLATFORM_LIMIT_RE.test(message.text)) return false;
  const type = String(message.type || "").toUpperCase();
  if (type === "CARD" || type === "HINT" || type === "RICH_HINT") return false;
  return true;
}

function classifyMessages(messages, panelText, minSilenceHours = 24, nowMs = Date.now(), profileText = "") {
  const customerText = customerClassifiableText(messages);
  const systemText = normalizeText(messages.filter((m) => m && m.side === "center").map((m) => m.text || "").join(" "));
  const verifiedPanelText = normalizeText(panelText || "");
  const profileSignalText = normalizeText([profileText, customerText].join(" "));
  const leadSignalText = normalizeText([verifiedPanelText, systemText, customerText].join(" "));
  const unreadableLeadRisk = messages.some((m) =>
    m &&
    m.side === "left" &&
    (UNREADABLE_LEAD_RISK_TYPES.has(String(m.type || "").toUpperCase()) || /\[图片\]|图片/.test(String(m.text || "")))
  );
  const leadStatus = STRONG_LEAD_RE.test(leadSignalText) || CONTACT_RE.test(leadSignalText)
    ? "has_lead"
    : (unreadableLeadRisk ? "unknown" : "no_lead");
  const negativeStatus = NEGATIVE_RE.test(customerText) ? "blocked" : "safe";
  const peerFromMessage = PEER_RE.test(customerText);
  const profileSignal = serviceProfileSignal(profileSignalText);
  const partyStatus = peerFromMessage ? "peer" : (profileSignal ? "unknown" : "customer");
  const classificationEvidence = peerFromMessage
    ? "peer_message_signal"
    : (profileSignal ? "suspected_service_profile:" + profileSignal : "");
  const real = messages.filter(isRealDialogMessage).sort((a, b) => Number(a.timestamp || 0) - Number(b.timestamp || 0));
  if (!real.length) {
    return {
      lead_status: leadStatus,
      party_status: partyStatus,
      reply_status: "unknown",
      negative_status: negativeStatus,
      last_message_at_ms: 0,
      eligible: false,
      exclusion_reason: "no_real_messages",
      classification_evidence: classificationEvidence,
    };
  }
  const last = real[real.length - 1];
  const replyStatus = last.side === "right" ? "awaiting_customer" : "awaiting_us";
  const lastMessageAtMs = Number(last.timestamp || 0);
  let eligible = leadStatus === "no_lead" && partyStatus === "customer" && replyStatus === "awaiting_customer" && negativeStatus === "safe";
  let exclusion = "";
  if (negativeStatus !== "safe") exclusion = "negative_status=" + negativeStatus;
  else if (leadStatus !== "no_lead") exclusion = "lead_status=" + leadStatus;
  else if (partyStatus !== "customer") exclusion = "party_status=" + partyStatus;
  else if (replyStatus !== "awaiting_customer") exclusion = "reply_status=" + replyStatus;
  else if (nowMs - lastMessageAtMs < minSilenceHours * 60 * 60 * 1000) {
    eligible = false;
    exclusion = "silence_less_than_" + minSilenceHours + "h";
  }
  return {
    lead_status: leadStatus,
    party_status: partyStatus,
    reply_status: replyStatus,
    negative_status: negativeStatus,
    last_message_at_ms: lastMessageAtMs,
    eligible,
    exclusion_reason: exclusion,
    classification_evidence: classificationEvidence,
  };
}

function applyManualOverride(userId, classification) {
  const override = MANUAL_USER_OVERRIDES[String(userId || "")];
  if (!override) return { ...classification, manual_evidence: "" };
  const next = { ...classification };
  for (const key of ["lead_status", "party_status", "reply_status", "negative_status"]) {
    if (override[key]) next[key] = override[key];
  }
  let eligible = next.lead_status === "no_lead" &&
    next.party_status === "customer" &&
    next.reply_status === "awaiting_customer" &&
    next.negative_status === "safe";
  let exclusion = "";
  if (next.negative_status !== "safe") exclusion = "negative_status=" + next.negative_status;
  else if (next.lead_status !== "no_lead") exclusion = "lead_status=" + next.lead_status;
  else if (next.party_status !== "customer") exclusion = "party_status=" + next.party_status;
  else if (next.reply_status !== "awaiting_customer") exclusion = "reply_status=" + next.reply_status;
  else if (classification.exclusion_reason && classification.exclusion_reason.startsWith("silence_less_than")) {
    eligible = false;
    exclusion = classification.exclusion_reason;
  }
  next.eligible = eligible;
  next.exclusion_reason = exclusion;
  next.manual_evidence = override.evidence || "manual_review";
  if (!next.classification_evidence) next.classification_evidence = classification.classification_evidence || "";
  return next;
}

async function runState(script, args) {
  let stdout = "";
  let stderr = "";
  let exitCode = 0;
  try {
    const result = await execFileAsync("python3", [script].concat(args.map((x) => String(x == null ? "" : x))), {
      maxBuffer: 1024 * 1024,
    });
    stdout = result.stdout;
    stderr = result.stderr;
  } catch (error) {
    stdout = error.stdout || "";
    stderr = error.stderr || error.message || "";
    exitCode = typeof error.code === "number" ? error.code : 1;
  }
  const text = stdout.trim();
  if (!text) throw new Error("state_script_empty_stdout:" + (stderr || ""));
  try {
    const payload = JSON.parse(text);
    if (exitCode) payload._exitCode = exitCode;
    return payload;
  } catch (_error) {
    throw new Error("state_script_bad_json:" + text.slice(0, 500));
  }
}

function statePayloadReason(payload) {
  if (!payload) return "empty_payload";
  return payload.error || payload.reason || payload.message || payload.status || "unknown";
}

function requireStateOk(payload, context) {
  if (!payload || payload.ok !== true || payload._exitCode) {
    throw new Error(context + ":" + statePayloadReason(payload));
  }
  return payload;
}

async function readVisibleConversationItems(tab) {
  return await tab.playwright.evaluate(() => {
    const strongLead = /留客资|对方已点击你的企业微信联系卡|对方已提交信息|访客联系方式已自动记录至「聚光平台-线索管理」|联系方式已自动记录/;
    const norm = (s) => String(s || "").replace(/\s+/g, " ").trim();
    const rectInfo = (el) => {
      const r = el.getBoundingClientRect();
      return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
    };
    const intersectArea = (a, b) => {
      const left = Math.max(a.left, b.left, 0);
      const top = Math.max(a.top, b.top, 0);
      const right = Math.min(a.right, b.right, innerWidth);
      const bottom = Math.min(a.bottom, b.bottom, innerHeight);
      return Math.max(0, right - left) * Math.max(0, bottom - top);
    };
    const viewport = { left: 0, top: 0, right: innerWidth, bottom: innerHeight };
    const candidates = [...document.querySelectorAll(".vue-recycle-scroller.scroller")]
      .map((scroller, index) => {
        const sr = rectInfo(scroller);
        const style = getComputedStyle(scroller);
        const visible =
          sr.width > 0 &&
          sr.height > 0 &&
          style.display !== "none" &&
          style.visibility !== "hidden" &&
          intersectArea(sr, viewport) > 1000;
        const totalItems = [...scroller.querySelectorAll('.sx-contact-item[data-key]')];
        return {
          index,
          scroller,
          rect: sr,
          visible,
          itemCount: totalItems.length,
          scrollTop: scroller.scrollTop,
          scrollHeight: scroller.scrollHeight,
          clientHeight: scroller.clientHeight,
        };
      })
      .filter((entry) => entry.visible && entry.clientHeight > 100 && entry.itemCount > 0)
      .sort((a, b) => b.itemCount - a.itemCount || b.scrollHeight - a.scrollHeight);

    const selected = candidates[0];
    if (!selected) {
      return { ok: false, reason: "visible_conversation_scroller_not_found", items: [], scroller: null };
    }

    const items = [...selected.scroller.querySelectorAll('.sx-contact-item[data-key]')]
      .map((el) => {
        const r = rectInfo(el);
        const area = Math.max(1, r.width * r.height);
        const overlap = intersectArea(r, selected.rect);
        const text = norm(el.innerText || el.textContent || "");
        return {
          key: el.getAttribute("data-key") || "",
          text: text.slice(0, 600),
          hasQuickLead: strongLead.test(text),
          top: r.top,
          height: r.height,
          overlap,
          overlapRatio: overlap / area,
        };
      })
      .filter((item) => item.key && item.overlap > 800 && item.overlapRatio > 0.18)
      .sort((a, b) => a.top - b.top);

    return {
      ok: true,
      reason: "",
      items,
      scroller: {
        index: selected.index,
        scrollTop: selected.scrollTop,
        scrollHeight: selected.scrollHeight,
        clientHeight: selected.clientHeight,
        atBottom: selected.scrollTop + selected.clientHeight >= selected.scrollHeight - 4,
      },
    };
  }, null, { timeoutMs: 10000 });
}

async function scrollConversationList(tab, { scrollPages = 0.82 } = {}) {
  const target = await tab.playwright.evaluate(() => {
    const rectInfo = (el) => {
      const r = el.getBoundingClientRect();
      return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
    };
    const viewportArea = (r) => {
      const left = Math.max(r.left, 0);
      const top = Math.max(r.top, 0);
      const right = Math.min(r.right, innerWidth);
      const bottom = Math.min(r.bottom, innerHeight);
      return Math.max(0, right - left) * Math.max(0, bottom - top);
    };
    const scrollers = [...document.querySelectorAll(".vue-recycle-scroller.scroller")]
      .map((el) => {
        const r = rectInfo(el);
        return { el, score: viewportArea(r), total: el.querySelectorAll('.sx-contact-item[data-key]').length };
      })
      .filter((x) => x.score > 1000 && x.el.clientHeight > 100 && x.total > 0)
      .sort((a, b) => b.total - a.total || b.el.scrollHeight - a.el.scrollHeight);
    const selected = scrollers[0] && scrollers[0].el;
    if (!selected) return { ok: false, reason: "scroller_not_found" };
    const before = { scrollTop: selected.scrollTop, scrollHeight: selected.scrollHeight, clientHeight: selected.clientHeight };
    const rect = rectInfo(selected);
    return {
      ok: true,
      before,
      rect,
      clientHeight: selected.clientHeight,
    };
  }, null, { timeoutMs: 10000 });

  if (!target.ok) return target;
  const pages = Math.max(0.25, Math.min(10, Number(scrollPages) || 0.82));
  target.scrollY = Math.max(240, Math.floor(target.clientHeight * pages));

  const movedDirectly = await tab.playwright.evaluate(({ scrollY }) => {
    const rectInfo = (el) => {
      const r = el.getBoundingClientRect();
      return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
    };
    const viewportArea = (r) => {
      const left = Math.max(r.left, 0);
      const top = Math.max(r.top, 0);
      const right = Math.min(r.right, innerWidth);
      const bottom = Math.min(r.bottom, innerHeight);
      return Math.max(0, right - left) * Math.max(0, bottom - top);
    };
    const scrollers = [...document.querySelectorAll(".vue-recycle-scroller.scroller")]
      .map((el) => {
        const r = rectInfo(el);
        return { el, score: viewportArea(r), total: el.querySelectorAll('.sx-contact-item[data-key]').length };
      })
      .filter((x) => x.score > 1000 && x.el.clientHeight > 100 && x.total > 0)
      .sort((a, b) => b.total - a.total || b.el.scrollHeight - a.el.scrollHeight);
    const selected = scrollers[0] && scrollers[0].el;
    if (!selected) return { ok: false, reason: "scroller_not_found_for_direct_scroll" };
    const before = selected.scrollTop;
    const maxTop = Math.max(0, selected.scrollHeight - selected.clientHeight);
    const targetTop = Math.min(maxTop, before + scrollY);
    if (typeof selected.scrollTo !== "function") {
      return { ok: false, reason: "scroller_scroll_to_unavailable" };
    }
    selected.scrollTo(0, targetTop);
    return { ok: true, before, after: selected.scrollTop };
  }, { scrollY: target.scrollY }, { timeoutMs: 10000 });
  if (!movedDirectly.ok) return movedDirectly;
  await tab.playwright.waitForTimeout(350);

  const after = await tab.playwright.evaluate(() => {
    const rectInfo = (el) => {
      const r = el.getBoundingClientRect();
      return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
    };
    const viewportArea = (r) => {
      const left = Math.max(r.left, 0);
      const top = Math.max(r.top, 0);
      const right = Math.min(r.right, innerWidth);
      const bottom = Math.min(r.bottom, innerHeight);
      return Math.max(0, right - left) * Math.max(0, bottom - top);
    };
    const scrollers = [...document.querySelectorAll(".vue-recycle-scroller.scroller")]
      .map((el) => {
        const r = rectInfo(el);
        return { el, score: viewportArea(r), total: el.querySelectorAll('.sx-contact-item[data-key]').length };
      })
      .filter((x) => x.score > 1000 && x.el.clientHeight > 100 && x.total > 0)
      .sort((a, b) => b.total - a.total || b.el.scrollHeight - a.el.scrollHeight);
    const selected = scrollers[0] && scrollers[0].el;
    if (!selected) return { ok: false, reason: "scroller_not_found_after_scroll" };
    return {
      ok: true,
      scrollTop: selected.scrollTop,
      scrollHeight: selected.scrollHeight,
      clientHeight: selected.clientHeight,
    };
  }, null, { timeoutMs: 10000 });

  if (!after.ok) return after;
  return {
    ok: true,
    before: target.before,
    after,
    moved: after.scrollTop !== target.before.scrollTop,
  };
}

async function nudgeConversationListAfterApparentBottom(tab, { scrollPages = 0.82 } = {}) {
  const before = await readVisibleConversationItems(tab);
  if (!before.ok) return { ok: false, reason: before.reason || "read_before_bottom_nudge_failed" };
  const beforeSignature = bottomCompletionSignature(before);
  const scroll = await scrollConversationList(tab, { scrollPages });
  if (!scroll.ok) return { ok: false, reason: "bottom_nudge_scroll_failed:" + scroll.reason };
  const wheel = await tab.playwright.evaluate(({ scrollPages }) => {
    const rectInfo = (el) => {
      const r = el.getBoundingClientRect();
      return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
    };
    const viewportArea = (r) => {
      const left = Math.max(r.left, 0);
      const top = Math.max(r.top, 0);
      const right = Math.min(r.right, innerWidth);
      const bottom = Math.min(r.bottom, innerHeight);
      return Math.max(0, right - left) * Math.max(0, bottom - top);
    };
    const scrollers = [...document.querySelectorAll(".vue-recycle-scroller.scroller")]
      .map((el) => {
        const r = rectInfo(el);
        return { el, score: viewportArea(r), total: el.querySelectorAll('.sx-contact-item[data-key]').length };
      })
      .filter((x) => x.score > 1000 && x.el.clientHeight > 100 && x.total > 0)
      .sort((a, b) => b.total - a.total || b.el.scrollHeight - a.el.scrollHeight);
    const selected = scrollers[0] && scrollers[0].el;
    if (!selected) return { ok: false, reason: "scroller_not_found_for_bottom_nudge" };
    const deltaY = Math.max(240, Math.floor(selected.clientHeight * Math.max(0.25, Math.min(10, Number(scrollPages) || 0.82))));
    selected.dispatchEvent(new WheelEvent("wheel", { bubbles: true, cancelable: true, deltaY }));
    selected.scrollTop = Math.max(selected.scrollTop, selected.scrollHeight - selected.clientHeight);
    selected.dispatchEvent(new Event("scroll", { bubbles: true }));
    return {
      ok: true,
      scrollTop: selected.scrollTop,
      scrollHeight: selected.scrollHeight,
      clientHeight: selected.clientHeight,
    };
  }, { scrollPages }, { timeoutMs: 10000 });
  if (!wheel.ok) return wheel;
  await tab.playwright.waitForTimeout(900);
  const after = await readVisibleConversationItems(tab);
  if (!after.ok) return { ok: false, reason: after.reason || "read_after_bottom_nudge_failed" };
  const afterSignature = bottomCompletionSignature(after);
  const beforeScroller = before.scroller || {};
  const afterScroller = after.scroller || {};
  return {
    ok: true,
    before,
    after,
    moved: !!scroll.moved ||
      Math.round(Number(beforeScroller.scrollTop || 0)) !== Math.round(Number(afterScroller.scrollTop || 0)),
    heightChanged: Math.round(Number(beforeScroller.scrollHeight || 0)) !== Math.round(Number(afterScroller.scrollHeight || 0)),
    signatureChanged: beforeSignature !== afterSignature,
    atBottom: !!afterScroller.atBottom,
  };
}

async function readUidSearchResultItems(tab, userId, accountId = "", alreadyTriedKeys = new Set(), allowedAccountIds = []) {
  const tried = [...alreadyTriedKeys].map((key) => String(key));
  const allowed = (allowedAccountIds || []).map((id) => String(id || "")).filter(Boolean);
  return await tab.playwright.evaluate(({ userId, accountId, tried, allowed }) => {
    const prefixes = new Set(["Total", "Active", "Favorite"]);
    const triedSet = new Set(tried || []);
    const allowedSet = new Set(allowed || []);
    const rectInfo = (el) => {
      const r = el.getBoundingClientRect();
      return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
    };
    const visibleArea = (r) => {
      const left = Math.max(r.left, 0);
      const top = Math.max(r.top, 0);
      const right = Math.min(r.right, innerWidth);
      const bottom = Math.min(r.bottom, innerHeight);
      return Math.max(0, right - left) * Math.max(0, bottom - top);
    };
    const parse = (key) => {
      const parts = String(key || "").split("-");
      if (!prefixes.has(parts[0]) || parts.length < 2 || !parts[1]) return null;
      return { prefix: parts[0], userId: parts[1], accountId: parts.length >= 3 ? parts.slice(2).join("-") : "" };
    };
    const norm = (s) => String(s || "").replace(/\s+/g, " ").trim();
    const items = [...document.querySelectorAll(".sx-contact-item[data-key]")]
      .map((el) => {
        const key = el.getAttribute("data-key") || "";
        const parsed = parse(key);
        const rect = rectInfo(el);
        return {
          key,
          parsed,
          text: norm(el.textContent || "").slice(0, 240),
          area: visibleArea(rect),
          rect,
        };
      })
      .filter((item) => {
        if (!item.parsed || triedSet.has(item.key)) return false;
        if (item.parsed.userId !== userId) return false;
        if (!item.parsed.accountId) return true;
        if (allowedSet.size) return allowedSet.has(item.parsed.accountId);
        return !accountId || item.parsed.accountId === accountId;
      })
      .sort((a, b) => {
        const aExact = a.parsed.accountId === accountId ? 1 : 0;
        const bExact = b.parsed.accountId === accountId ? 1 : 0;
        const aAllowed = allowedSet.has(a.parsed.accountId) ? 1 : 0;
        const bAllowed = allowedSet.has(b.parsed.accountId) ? 1 : 0;
        const aTotal = a.parsed.prefix === "Total" ? 1 : 0;
        const bTotal = b.parsed.prefix === "Total" ? 1 : 0;
        return bExact - aExact || bAllowed - aAllowed || b.area - a.area || bTotal - aTotal;
      });
    return { ok: true, items };
  }, { userId, accountId, tried, allowed }, { timeoutMs: 10000 });
}

async function countUidSearchPopupResults(tab) {
  return await tab.playwright.evaluate(() => {
    const rectInfo = (el) => {
      const r = el.getBoundingClientRect();
      return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
    };
    const visibleArea = (r) => {
      const left = Math.max(r.left, 0);
      const top = Math.max(r.top, 0);
      const right = Math.min(r.right, innerWidth);
      const bottom = Math.min(r.bottom, innerHeight);
      return Math.max(0, right - left) * Math.max(0, bottom - top);
    };
    const norm = (s) => String(s || "").replace(/\s+/g, " ").trim();
    const popupElements = [...document.querySelectorAll(".search-contact-item")];
    const findScrollParent = (el) => {
      let cur = el && el.parentElement;
      while (cur && cur !== document.body) {
        const style = getComputedStyle(cur);
        const scrollable = cur.scrollHeight > cur.clientHeight + 4 && /auto|scroll|overlay/.test(style.overflowY || "");
        if (scrollable) return cur;
        cur = cur.parentElement;
      }
      return null;
    };
    const scrollParent = popupElements.length ? findScrollParent(popupElements[0]) : null;
    const readVisibleEntries = () => popupElements
      .map((el, allIndex) => {
        const rect = rectInfo(el);
        const style = getComputedStyle(el);
        return {
          allIndex,
          scrollTop: scrollParent ? scrollParent.scrollTop : 0,
          area: visibleArea(rect),
          rect,
          text: norm(el.innerText || el.textContent || "").slice(0, 240),
          visible: style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0,
        };
      })
      .filter((entry) => entry.visible && entry.area > 50)
      .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
    const collected = [];
    const seen = new Set();
    const addEntries = () => {
      for (const entry of readVisibleEntries()) {
        const key = entry.allIndex + "|" + entry.text;
        if (seen.has(key)) continue;
        seen.add(key);
        collected.push(entry);
      }
    };
    addEntries();
    if (scrollParent) {
      const originalTop = scrollParent.scrollTop;
      const maxTop = Math.max(0, scrollParent.scrollHeight - scrollParent.clientHeight);
      const step = Math.max(40, Math.floor(scrollParent.clientHeight * 0.75));
      for (let top = step; top <= maxTop + step; top += step) {
        const targetTop = Math.min(maxTop, top);
        try {
          if (typeof scrollParent.scrollTo === "function") scrollParent.scrollTo(0, targetTop);
          else scrollParent.scrollTop = targetTop;
        } catch (_error) {
          break;
        }
        addEntries();
        if (scrollParent.scrollTop >= maxTop) break;
      }
      try {
        if (typeof scrollParent.scrollTo === "function") scrollParent.scrollTo(0, originalTop);
        else scrollParent.scrollTop = originalTop;
      } catch (_error) {
        // Best-effort restore only.
      }
    }
    const entries = collected.sort((a, b) => a.scrollTop - b.scrollTop || a.rect.top - b.rect.top || a.rect.left - b.rect.left);
    return { selector: ".search-contact-item", count: entries.length, entries };
  }, null, { timeoutMs: 10000 });
}

function popupEntryCenter(entry) {
  const rect = entry && entry.rect ? entry.rect : {};
  const left = Number(rect.left);
  const top = Number(rect.top);
  const right = Number(rect.right);
  const bottom = Number(rect.bottom);
  if (![left, top, right, bottom].every(Number.isFinite)) return null;
  return {
    x: Math.round((left + right) / 2),
    y: Math.round((top + bottom) / 2),
  };
}

function normalizePopupText(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function resolvePopupCoordinateClickTarget({ desiredIndex = 0, entries = [], refreshedEntries = [] } = {}) {
  const original = Array.isArray(entries) ? entries[desiredIndex] : null;
  if (!original) return { ok: false, reason: "uid_search_popup_missing_entry_" + desiredIndex };
  const originalText = normalizePopupText(original.text);
  const refreshed = Array.isArray(refreshedEntries) ? refreshedEntries : [];
  const target = refreshed.find((entry) => normalizePopupText(entry.text) === originalText && originalText)
    || refreshed.find((entry) => Number(entry.allIndex) === Number(original.allIndex))
    || original;
  const center = popupEntryCenter(target);
  if (!center) return { ok: false, reason: "uid_search_popup_missing_click_coordinates_" + desiredIndex };
  return {
    ok: true,
    allIndex: Number(original.allIndex || 0),
    scrollTop: Number(original.scrollTop || 0),
    x: center.x,
    y: center.y,
    text: original.text || "",
  };
}

async function clickUidSearchPopupResult(tab, index = 0) {
  const popup = await countUidSearchPopupResults(tab);
  const count = popup.count;
  if (count < 1) return { ok: false, reason: "uid_search_popup_count_0" };
  if (index >= count) return { ok: false, reason: "uid_search_popup_index_out_of_range_" + index + "_of_" + count };
  const targetInfo = popup.entries && popup.entries[index];
  if (!targetInfo) return { ok: false, reason: "uid_search_popup_missing_entry_" + index + "_of_" + count };
  const clickResult = await tab.playwright.evaluate(({ selector, index, entries }) => {
    const rectInfo = (el) => {
      const r = el.getBoundingClientRect();
      return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
    };
    const visibleArea = (r) => {
      const left = Math.max(r.left, 0);
      const top = Math.max(r.top, 0);
      const right = Math.min(r.right, innerWidth);
      const bottom = Math.min(r.bottom, innerHeight);
      return Math.max(0, right - left) * Math.max(0, bottom - top);
    };
    const norm = (s) => String(s || "").replace(/\s+/g, " ").trim();
    const findScrollParent = (el) => {
      let cur = el && el.parentElement;
      while (cur && cur !== document.body) {
        const style = getComputedStyle(cur);
        const scrollable = cur.scrollHeight > cur.clientHeight + 4 && /auto|scroll|overlay/.test(style.overflowY || "");
        if (scrollable) return cur;
        cur = cur.parentElement;
      }
      return null;
    };
    const readVisibleEntries = () => [...document.querySelectorAll(selector)]
      .map((el, allIndex) => {
        const rect = rectInfo(el);
        const style = getComputedStyle(el);
        return {
          el,
          allIndex,
          scrollTop: 0,
          area: visibleArea(rect),
          rect,
          text: norm(el.innerText || el.textContent || "").slice(0, 240),
          visible: style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0,
        };
      })
      .filter((entry) => entry.visible && entry.area > 50)
      .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);

    const original = Array.isArray(entries) ? entries[index] : null;
    if (!original) return { ok: false, reason: "uid_search_popup_missing_entry_" + index };
    const firstElement = document.querySelector(selector);
    if (!firstElement) return { ok: false, reason: "uid_search_popup_click_no_elements" };
    const scrollParent = findScrollParent(firstElement);
    if (scrollParent) {
      try {
        if (typeof scrollParent.scrollTo === "function") scrollParent.scrollTo(0, Number(original.scrollTop || 0));
        else scrollParent.scrollTop = Number(original.scrollTop || 0);
      } catch (_error) {
        return { ok: false, reason: "uid_search_popup_scroll_failed" };
      }
    }

    const refreshedEntries = readVisibleEntries().map((entry) => ({
      allIndex: entry.allIndex,
      scrollTop: scrollParent ? scrollParent.scrollTop : 0,
      rect: entry.rect,
      text: entry.text,
    }));
    const targetPlan = window.__xhsResolvePopupCoordinateClickTarget
      ? window.__xhsResolvePopupCoordinateClickTarget({ desiredIndex: index, entries, refreshedEntries })
      : (() => {
          const originalText = norm(original.text);
          const target = refreshedEntries.find((entry) => norm(entry.text) === originalText && originalText)
            || refreshedEntries.find((entry) => Number(entry.allIndex) === Number(original.allIndex))
            || original;
          const r = target.rect || {};
          return {
            ok: true,
            allIndex: Number(original.allIndex || 0),
            scrollTop: Number(original.scrollTop || 0),
            x: Math.round((Number(r.left) + Number(r.right)) / 2),
            y: Math.round((Number(r.top) + Number(r.bottom)) / 2),
            text: original.text || "",
          };
        })();
    if (!targetPlan.ok) return targetPlan;
    if (!Number.isFinite(targetPlan.x) || !Number.isFinite(targetPlan.y)) {
      return { ok: false, reason: "uid_search_popup_invalid_click_coordinates" };
    }
    const hit = document.elementFromPoint(targetPlan.x, targetPlan.y);
    const clickable = hit && hit.closest(selector);
    if (!clickable) return { ok: false, reason: "uid_search_popup_click_target_not_found_at_point" };
    return { ok: true, reason: "uid_search_popup_coordinate_ready", ...targetPlan, refreshedCount: refreshedEntries.length };
  }, { selector: popup.selector, index, entries: popup.entries || [] }, { timeoutMs: 8000 });
  if (!clickResult.ok) return clickResult;
  try {
    await tab.cua.click({ x: clickResult.x, y: clickResult.y });
  } catch (error) {
    return { ok: false, reason: "uid_search_popup_coordinate_click_failed:" + String(error && error.message || error).slice(0, 160) };
  }
  await tab.playwright.waitForTimeout(300);
  return { ok: true, reason: "uid_search_popup_coordinate_clicked", index, count, selector: popup.selector, allIndex: clickResult.allIndex, x: clickResult.x, y: clickResult.y, text: clickResult.text || "" };
}

async function clickVisibleConversation(tab, key) {
  const target = await tab.playwright.evaluate(({ key }) => {
    const rectInfo = (el) => {
      const r = el.getBoundingClientRect();
      return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
    };
    const intersectArea = (a, b) => {
      const left = Math.max(a.left, b.left, 0);
      const top = Math.max(a.top, b.top, 0);
      const right = Math.min(a.right, b.right, innerWidth);
      const bottom = Math.min(a.bottom, b.bottom, innerHeight);
      return Math.max(0, right - left) * Math.max(0, bottom - top);
    };
    const viewport = { left: 0, top: 0, right: innerWidth, bottom: innerHeight };
    const scrollers = [...document.querySelectorAll(".vue-recycle-scroller.scroller")]
      .map((scroller, index) => {
        const rect = rectInfo(scroller);
        const style = getComputedStyle(scroller);
        const visible =
          rect.width > 0 &&
          rect.height > 0 &&
          style.display !== "none" &&
          style.visibility !== "hidden" &&
          intersectArea(rect, viewport) > 1000;
        return {
          scroller,
          index,
          rect,
          visible,
          itemCount: scroller.querySelectorAll('.sx-contact-item[data-key]').length,
          scrollHeight: scroller.scrollHeight,
        };
      })
      .filter((entry) => entry.visible && entry.itemCount > 0)
      .sort((a, b) => b.itemCount - a.itemCount || b.scrollHeight - a.scrollHeight);
    const selected = scrollers[0];
    if (!selected) return { ok: false, reason: "visible_conversation_scroller_not_found" };

    const allMatches = [...document.querySelectorAll('.sx-contact-item[data-key]')]
      .filter((el) => (el.getAttribute("data-key") || "") === key);
    const visibleMatches = allMatches
      .map((el) => {
        const rect = rectInfo(el);
        const area = Math.max(1, rect.width * rect.height);
        const overlap = intersectArea(rect, selected.rect);
        return { el, matchIndex: allMatches.indexOf(el), rect, overlap, overlapRatio: overlap / area };
      })
      .filter((entry) => entry.overlap > 800 && entry.overlapRatio > 0.18)
      .sort((a, b) => b.overlap - a.overlap || b.overlapRatio - a.overlapRatio);
    if (!visibleMatches.length) {
      return {
        ok: false,
        reason: "visible_conversation_key_not_visible",
        key,
        totalMatches: allMatches.length,
      };
    }

    const target = visibleMatches[0];
    return {
      ok: true,
      reason: "",
      key,
      matchIndex: target.matchIndex,
      scrollerIndex: selected.index,
      visibleMatches: visibleMatches.length,
      totalMatches: allMatches.length,
      overlap: target.overlap,
    };
  }, { key }, { timeoutMs: 10000 });
  if (!target.ok) return target;

  const selector = '.sx-contact-item[data-key="' + cssAttr(key) + '"]';
  const locator = tab.playwright.locator(selector);
  const count = await locator.count();
  if (count <= target.matchIndex) {
    return {
      ok: false,
      reason: "visible_conversation_match_index_out_of_range_" + target.matchIndex + "_of_" + count,
      key,
    };
  }
  try {
    await locator.nth(target.matchIndex).click({ timeoutMs: 8000 });
  } catch (error) {
    return { ok: false, reason: "visible_conversation_click_failed:" + String(error && error.message || error).slice(0, 160) };
  }
  await tab.playwright.waitForTimeout(300);
  return target;
}

async function openUidSearchPopup(tab, input, userId, minCount = 1) {
  const header = tab.playwright.locator(".message-header");
  const headerCount = await header.count();
  if (headerCount >= 1) {
    try {
      await header.nth(0).click({ timeoutMs: 3000 });
      await tab.playwright.waitForTimeout(300);
    } catch (_error) {
      // Best-effort blur; continue with direct input handling.
    }
  }
  await input.click({ timeoutMs: 8000 });
  await tab.playwright.waitForTimeout(150);
  try {
    await input.press("Meta+A", { timeoutMs: 3000 });
    await input.press("Backspace", { timeoutMs: 3000 });
    await tab.playwright.waitForTimeout(250);
    await input.type(userId, { timeoutMs: 15000 });
  } catch (_error) {
    await input.fill("");
    await tab.playwright.waitForTimeout(150);
    await input.fill(userId);
  }
  let last = { selector: ".search-contact-item", count: 0 };
  for (let i = 0; i < 8; i += 1) {
    await tab.playwright.waitForTimeout(250);
    last = await countUidSearchPopupResults(tab);
    if (last.count >= minCount) return { ok: true, reason: "uid_search_popup_opened", ...last };
  }
  return { ok: false, reason: "uid_search_popup_not_opened", ...last };
}

async function resetConversationListToTop(tab) {
  const readScroller = async () => await tab.playwright.evaluate(() => {
    const rectInfo = (el) => {
      const r = el.getBoundingClientRect();
      return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
    };
    const viewportArea = (r) => {
      const left = Math.max(r.left, 0);
      const top = Math.max(r.top, 0);
      const right = Math.min(r.right, innerWidth);
      const bottom = Math.min(r.bottom, innerHeight);
      return Math.max(0, right - left) * Math.max(0, bottom - top);
    };
    const scrollers = [...document.querySelectorAll(".vue-recycle-scroller.scroller")]
      .map((el) => ({
        el,
        score: viewportArea(rectInfo(el)),
        total: el.querySelectorAll('.sx-contact-item[data-key]').length,
      }))
      .filter((entry) => entry.score > 1000 && entry.el.clientHeight > 100 && entry.total > 0)
      .sort((a, b) => b.total - a.total || b.el.scrollHeight - a.el.scrollHeight);
    const selected = scrollers[0] && scrollers[0].el;
    if (!selected) return { ok: false, reason: "scroller_not_found_for_reset" };
    return {
      ok: true,
      reason: "",
      scrollTop: selected.scrollTop,
      clientHeight: selected.clientHeight,
      rect: rectInfo(selected),
    };
  }, null, { timeoutMs: 10000 });

  const initial = await readScroller();
  if (!initial.ok) return initial;
  const before = initial.scrollTop;
  let current = initial;
  for (let i = 0; i < 8 && current.scrollTop > 4; i += 1) {
    const moved = await tab.playwright.evaluate(() => {
      const rectInfo = (el) => {
        const r = el.getBoundingClientRect();
        return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
      };
      const viewportArea = (r) => {
        const left = Math.max(r.left, 0);
        const top = Math.max(r.top, 0);
        const right = Math.min(r.right, innerWidth);
        const bottom = Math.min(r.bottom, innerHeight);
        return Math.max(0, right - left) * Math.max(0, bottom - top);
      };
      const scrollers = [...document.querySelectorAll(".vue-recycle-scroller.scroller")]
        .map((el) => ({
          el,
          score: viewportArea(rectInfo(el)),
          total: el.querySelectorAll('.sx-contact-item[data-key]').length,
        }))
        .filter((entry) => entry.score > 1000 && entry.el.clientHeight > 100 && entry.total > 0)
        .sort((a, b) => b.total - a.total || b.el.scrollHeight - a.el.scrollHeight);
      const selected = scrollers[0] && scrollers[0].el;
      if (!selected) return { ok: false, reason: "scroller_not_found_for_reset" };
      const beforeTop = selected.scrollTop;
      if (typeof selected.scrollTo !== "function") {
        return { ok: false, reason: "scroller_scroll_to_unavailable" };
      }
      selected.scrollTo(0, 0);
      return { ok: true, before: beforeTop, after: selected.scrollTop };
    }, null, { timeoutMs: 10000 });
    if (!moved.ok) return moved;
    await tab.playwright.waitForTimeout(500);
    current = await readScroller();
    if (!current.ok) return current;
    if (moved.before === moved.after && current.scrollTop > 4) break;
  }
  return {
    ok: current.scrollTop <= 4,
    reason: current.scrollTop <= 4 ? "" : "scroller_reset_to_top_failed",
    before,
    after: current.scrollTop,
  };
}

async function searchConversationByUid(tab, rawItem, alreadyTriedKeys = new Set(), allowedAccountIds = [], { deadlineAt = 0 } = {}) {
  const item = normalizeQueueItem(rawItem);
  const userId = item.user_id;
  const accountId = item.account_id;
  const allowedAccountIdSet = new Set((allowedAccountIds || []).filter(Boolean));
  const deadlineResult = (phase) => ({ ok: false, reason: "uid_search_deadline_exceeded:" + phase });
  const hasTimeFor = (reserveMs = 0) => !deadlineExceeded(deadlineAt, reserveMs);
  const cappedRetries = (requested, perRetryMs = 350) => {
    if (!deadlineAt) return requested;
    const remaining = Math.max(0, Number(deadlineAt) - Date.now());
    return Math.max(1, Math.min(requested, Math.floor(remaining / perRetryMs)));
  };
  if (!userId) return { ok: false, reason: "missing_saved_queue_user_id_for_uid_search" };
  if (!accountId && !allowedAccountIdSet.size) return { ok: false, reason: "missing_saved_queue_account_id_for_uid_search" };
  if (!hasTimeFor(1200)) return deadlineResult("before_input");
  const input = tab.playwright.getByPlaceholder("输入用户ID/小红书ID", { exact: true });
  const inputCount = await input.count();
  if (inputCount !== 1) return { ok: false, reason: "search_input_count_" + inputCount };

  const waitForSearchIdentity = async (retries = 10) => {
    if (!hasTimeFor(900)) return { identityOk: false, deadlineExceeded: true };
    const safeRetries = cappedRetries(retries);
    let exactIdentity = null;
    if (accountId && (!allowedAccountIdSet.size || allowedAccountIdSet.has(accountId))) {
      exactIdentity = await waitForIdentity(tab, userId, accountId, safeRetries);
      if (identityMatchesSearch(exactIdentity, allowedAccountIdSet)) return exactIdentity;
    }
    if (allowedAccountIdSet.size) return await waitForAllowedIdentity(tab, userId, allowedAccountIdSet, safeRetries);
    return exactIdentity || await waitForIdentity(tab, userId, accountId, safeRetries);
  };

  const tryVisibleUidListResults = async (items, locatedReason, failureReason) => {
    if (!Array.isArray(items) || !items.length) return null;
    let lastIdentity = null;
    for (const candidate of items.slice(0, 20)) {
      if (!hasTimeFor(1800)) {
        return { ok: false, reason: "uid_search_deadline_exceeded:visible_list_results" };
      }
      const clicked = await clickVisibleConversation(tab, candidate.key);
      if (!clicked.ok) continue;
      const identity = await waitForSearchIdentity(10);
      lastIdentity = identity;
      if (identityMatchesSearch(identity, allowedAccountIdSet)) {
        return {
          ok: true,
          reason: locatedReason,
          item,
          visibleKey: candidate.key,
          identity,
        };
      }
    }
    if (!lastIdentity) return null;
    return {
      ok: false,
      reason: failureReason,
      rightPanelUid: lastIdentity.rightPanelUid || "",
      inferredAccountId: lastIdentity.inferredAccountId || "",
    };
  };

  if (!hasTimeFor(2500)) return deadlineResult("before_popup_open");
  await openUidSearchPopup(tab, input, userId);
  const popupCount = await countUidSearchPopupResults(tab);
  let popupIdentity = null;
  let popupFailure = null;
  let visibleListFailure = null;
  if (popupCount.count > 0) {
    const maxPopupAttempts = Math.min(popupCount.count, 20);
    for (let popupIndex = 0; popupIndex < maxPopupAttempts; popupIndex += 1) {
      if (!hasTimeFor(2500)) return deadlineResult("popup_results");
      const popupReady = popupIndex === 0
        ? popupCount
        : await openUidSearchPopup(tab, input, userId, popupIndex + 1);
      if (!popupReady || popupReady.count <= popupIndex) break;
      const popupClicked = await clickUidSearchPopupResult(tab, popupIndex);
      if (!popupClicked.ok) break;
      popupIdentity = await waitForSearchIdentity(10);
      if (identityMatchesSearch(popupIdentity, allowedAccountIdSet)) {
        return {
          ok: true,
          reason: "located_by_uid_search_popup_verified_panel_uid",
          item,
          visibleKey: item.key,
          identity: popupIdentity,
        };
      }
    }
    popupFailure = {
      ok: false,
      reason: "uid_search_popup_identity_not_confirmed",
      rightPanelUid: popupIdentity ? popupIdentity.rightPanelUid : "",
      inferredAccountId: popupIdentity ? popupIdentity.inferredAccountId : "",
    };
  }

  if (!hasTimeFor(2200)) return deadlineResult("before_early_dom_results");
  const earlyDomResults = await readUidSearchResultItems(tab, userId, accountId, alreadyTriedKeys, allowedAccountIds);
  if (earlyDomResults.ok && earlyDomResults.items.length) {
    const located = await tryVisibleUidListResults(
      earlyDomResults.items,
      "located_by_uid_search_visible_list_verified_panel_uid",
      "uid_search_visible_list_identity_not_confirmed"
    );
    if (located && located.ok) return located;
    if (located) visibleListFailure = located;
  }

  if (item.key) {
    if (!hasTimeFor(2200)) return deadlineResult("before_exact_queue_key");
    const exactClicked = await clickVisibleConversation(tab, item.key);
    if (exactClicked.ok) {
      const exactIdentity = await waitForSearchIdentity(16);
      if (identityMatchesSearch(exactIdentity, allowedAccountIdSet)) {
        return {
          ok: true,
          reason: "located_by_uid_search_exact_queue_key_verified_panel_uid",
          item,
          visibleKey: item.key,
          identity: exactIdentity,
        };
      }
    }
  }

  if (!hasTimeFor(1800)) return deadlineResult("before_enter");
  await input.press("Enter", {});
  await tab.playwright.waitForTimeout(500);
  const directIdentity = await waitForSearchIdentity(4);
  if (identityMatchesSearch(directIdentity, allowedAccountIdSet)) {
    return {
      ok: true,
      reason: "located_by_uid_search_auto_open_verified_panel_uid",
      item,
      visibleKey: item.key,
      identity: directIdentity,
    };
  }

  if (!hasTimeFor(2200)) return deadlineResult("before_dom_results");
  const domResults = await readUidSearchResultItems(tab, userId, accountId, alreadyTriedKeys, allowedAccountIds);
  if (domResults.ok && domResults.items.length) {
    const located = await tryVisibleUidListResults(
      domResults.items,
      "located_by_uid_search_result_verified_panel_uid",
      "uid_search_result_identity_not_confirmed"
    );
    if (located && located.ok) return located;
    if (located) visibleListFailure = visibleListFailure || located;
  }

  let stableView = null;
  let lastSignature = "";
  let stableCount = 0;
  for (let i = 0; i < 12; i += 1) {
    if (!hasTimeFor(1200)) return deadlineResult("stable_result_list");
    await tab.playwright.waitForTimeout(350);
    const view = await readVisibleConversationItems(tab);
    if (!view.ok) continue;
    const signature = view.items.map((candidate) => candidate.key).join("|");
    if (signature && signature === lastSignature) stableCount += 1;
    else stableCount = 0;
    lastSignature = signature;
    stableView = view;
    const direct = view.items.find((candidate) => {
      const parsed = parseConversationKey(candidate.key);
      return parsed.ok && parsed.userId === userId && !alreadyTriedKeys.has(candidate.key);
    });
    if (direct || stableCount >= 1) break;
  }

  if (!stableView || !stableView.ok) return { ok: false, reason: "search_result_scroller_missing" };
  const candidates = stableView.items.filter((candidate) => {
    if (alreadyTriedKeys.has(candidate.key)) return false;
    const parsed = parseConversationKey(candidate.key);
    if (!parsed.ok || parsed.userId !== userId) return false;
    if (allowedAccountIdSet.size) return !parsed.accountId || allowedAccountIdSet.has(parsed.accountId);
    return !parsed.accountId || parsed.accountId === accountId;
  });
  if (!candidates.length) return visibleListFailure || popupFailure || { ok: false, reason: "uid_search_no_matching_result" };

  let lastIdentity = null;
  for (const candidate of candidates.slice(0, 20)) {
    if (!hasTimeFor(1800)) return deadlineResult("candidate_results");
    const clicked = await clickVisibleConversation(tab, candidate.key);
    if (!clicked.ok) continue;
    const identity = await waitForSearchIdentity(10);
    lastIdentity = identity;
    if (identityMatchesSearch(identity, allowedAccountIdSet)) {
      return {
        ok: true,
        reason: "located_by_uid_search_verified_panel_uid",
        item,
        visibleKey: candidate.key,
        identity,
      };
    }
  }
  return visibleListFailure || popupFailure || {
    ok: false,
    reason: "uid_search_identity_not_confirmed",
    rightPanelUid: lastIdentity ? lastIdentity.rightPanelUid : "",
    inferredAccountId: lastIdentity ? lastIdentity.inferredAccountId : "",
  };
}

async function readConversationState(tab, expectedUserId, expectedAccountId = "") {
  return await tab.playwright.evaluate(({ expectedUserId, expectedAccountId }) => {
    const norm = (s) => String(s || "").replace(/\s+/g, " ").trim();
    const header = norm(document.querySelector(".message-header")?.innerText || "");
    const panel = document.querySelector("#rightPanel") || [...document.querySelectorAll(".right-side, .chat-tool-wrap")][0] || null;
    const panelText = norm(panel?.innerText || "");
    const uidMatch = panelText.match(/小红书uid\s*([0-9a-zA-Z]+)/);
    const rightPanelUid = uidMatch ? uidMatch[1] : "";
    const messages = [...document.querySelectorAll('[id^="jarvis-msg-"]')]
      .map((el) => {
        const id = el.id || "";
        const parts = id.replace(/^jarvis-msg-/, "").split(".");
        let accountId = "";
        let matched = false;
        let idOrder = "";
        if (parts.length >= 3 && parts[0] === expectedUserId) {
          accountId = parts[1];
          matched = !expectedAccountId || accountId === expectedAccountId;
          idOrder = "user-account";
        } else if (parts.length >= 3 && parts[1] === expectedUserId) {
          accountId = parts[0];
          matched = !expectedAccountId || accountId === expectedAccountId;
          idOrder = "account-user";
        }
        if (!matched) return null;
        const cls = String(el.className || "");
        const side = cls.split(/\s+/).find((c) => c === "left" || c === "right" || c === "center") || "";
        const idTimestampMatch = id.match(/^jarvis-msg-(\d{13})/);
        return {
          id,
          accountId,
          idOrder,
          side,
          type: el.getAttribute("data-msg-type") || "",
          timestamp: Number(el.getAttribute("data-timestamp") || idTimestampMatch?.[1] || 0),
          text: norm(el.innerText || el.textContent || "").slice(0, 1600),
        };
      })
      .filter(Boolean);
    const accountCounts = {};
    for (const message of messages) accountCounts[message.accountId] = (accountCounts[message.accountId] || 0) + 1;
    const observedAccountIds = Object.entries(accountCounts)
      .sort((a, b) => b[1] - a[1])
      .map(([accountId, count]) => ({ accountId, count }));
    const hasExactExpectedAccountMessage = !!expectedAccountId && messages.some((m) => m.accountId === expectedAccountId);
    const inferredAccountId = expectedAccountId
      ? (hasExactExpectedAccountMessage ? expectedAccountId : "")
      : (observedAccountIds[0]?.accountId || "");
    const filtered = inferredAccountId ? messages.filter((m) => m.accountId === inferredAccountId) : messages;
    const textArea = [...document.querySelectorAll("textarea")]
      .map((el) => ({ placeholder: el.getAttribute("placeholder") || "", value: el.value || "" }))
      .find((x) => x.placeholder.includes("回车键") && x.placeholder.includes("发送信息")) || null;
    const sendButtons = [...document.querySelectorAll("button")]
      .map((el) => ({ text: norm(el.innerText || el.textContent || ""), disabled: !!el.disabled || String(el.className || "").includes("disabled") }))
      .filter((x) => x.text === "发送");
    return {
      header,
      panelText,
      rightPanelUid,
      expectedUserId,
      expectedAccountId,
      inferredAccountId,
      observedAccountIds,
      hasExactExpectedAccountMessage,
      identityOk: rightPanelUid === expectedUserId && !!inferredAccountId,
      messageCount: filtered.length,
      messages: filtered,
      hasLeadSignal: /留客资|对方已点击你的企业微信联系卡|对方已提交信息|访客联系方式已自动记录至「聚光平台-线索管理」|联系方式已自动记录/.test(panelText),
      textArea,
      sendButtons,
    };
  }, { expectedUserId, expectedAccountId }, { timeoutMs: 10000 });
}

async function waitForIdentity(tab, userId, accountId = "", retries = 8) {
  let last = null;
  for (let i = 0; i < retries; i += 1) {
    last = await readConversationState(tab, userId, accountId);
    if (last.identityOk) return last;
    await tab.playwright.waitForTimeout(250);
  }
  return last;
}

async function waitForAllowedIdentity(tab, userId, allowedAccountIdSet, retries = 8) {
  let last = null;
  for (let i = 0; i < retries; i += 1) {
    last = await readConversationState(tab, userId, "");
    if (identityAllowedForSend(last, allowedAccountIdSet)) return last;
    await tab.playwright.waitForTimeout(250);
  }
  return last;
}

async function clickConversation(tab, key) {
  const selector = '.sx-contact-item[data-key="' + cssAttr(key) + '"]';
  const locator = tab.playwright.locator(selector);
  const count = await locator.count();
  if (count !== 1) return { ok: false, reason: "conversation_locator_count_" + count };
  try {
    await locator.click({ timeoutMs: 8000 });
  } catch (error) {
    return { ok: false, reason: "conversation_click_failed:" + String(error && error.message || error).slice(0, 160) };
  }
  return { ok: true, reason: "" };
}

async function clickVisibleLoadMore(tab) {
  const visibleCount = await tab.playwright.evaluate(() => {
    const norm = (s) => String(s || "").replace(/\s+/g, " ").trim();
    const visible = (el) => {
      const r = el.getBoundingClientRect();
      const st = getComputedStyle(el);
      const left = Math.max(r.left, 0);
      const top = Math.max(r.top, 0);
      const right = Math.min(r.right, innerWidth);
      const bottom = Math.min(r.bottom, innerHeight);
      return st.display !== "none" && st.visibility !== "hidden" && Math.max(0, right - left) * Math.max(0, bottom - top) > 400;
    };
    return [...document.querySelectorAll("button,div,span")]
      .filter((el) => norm(el.innerText || el.textContent || "") === "加载更多" && visible(el)).length;
  }, null, { timeoutMs: 10000 });
  if (visibleCount !== 1) return { clicked: false, reason: "visible_load_more_count_" + visibleCount };
  const visibleLocator = tab.playwright
    .getByText("加载更多", { exact: true })
    .filter({ visible: true });
  const locatorCount = await visibleLocator.count();
  if (locatorCount !== 1) return { clicked: false, reason: "visible_load_more_locator_count_" + locatorCount };
  await visibleLocator.click({});
  await tab.playwright.waitForTimeout(500);
  return { clicked: true, reason: "" };
}

async function loadMoreHistoryIfNeeded(tab, userId, accountId) {
  let previousCount = -1;
  let stagnant = 0;
  let clicks = 0;
  for (let i = 0; i < 10; i += 1) {
    const before = await readConversationState(tab, userId, accountId);
    const count = before.messages.length;
    if (count === previousCount) stagnant += 1;
    else stagnant = 0;
    previousCount = count;
    if (stagnant >= 2) break;
    const result = await clickVisibleLoadMore(tab);
    if (!result.clicked) break;
    clicks += 1;
  }
  return { clicks };
}

async function recordCandidate(script, campaignId, item, identity, classification) {
  const evidence = [
    "msgs=" + identity.messageCount + ";last=" + classification.last_message_at_ms,
    classification.classification_evidence || "",
    classification.manual_evidence || "",
  ].filter(Boolean).join(";");
  return requireStateOk(await runState(script, [
    "candidate",
    "--campaign-id", campaignId,
    "--conversation-key", item.key,
    "--user-id", identity.expectedUserId,
    "--account-id", identity.inferredAccountId,
    "--display-name", identity.header || item.displayName || "",
    "--lead-status", classification.lead_status,
    "--party-status", classification.party_status,
    "--reply-status", classification.reply_status,
    "--negative-status", classification.negative_status,
    "--last-message-at-ms", classification.last_message_at_ms,
    "--evidence", evidence,
  ]), "record_candidate_failed");
}

async function recordQuickLeadCandidate(script, campaignId, item, parsed, reason) {
  if (!parsed || !parsed.ok || !parsed.accountId) return { ok: true, ignored: true, reason: "quick_lead_account_unknown" };
  return requireStateOk(await runState(script, [
    "candidate",
    "--campaign-id", campaignId,
    "--conversation-key", item.key,
    "--user-id", parsed.userId,
    "--account-id", parsed.accountId,
    "--display-name", item.displayName || "",
    "--lead-status", "has_lead",
    "--party-status", "customer",
    "--reply-status", "unknown",
    "--negative-status", "safe",
    "--last-message-at-ms", "0",
    "--evidence", "quick_left_list_strong_lead:" + reason,
  ]), "record_quick_lead_candidate_failed");
}

async function canSend(script, campaignId, key, batchId = "", { allowIncompleteScan = false } = {}) {
  const args = ["can-send", "--campaign-id", campaignId, "--conversation-key", key];
  if (batchId) args.push("--batch-id", batchId);
  if (allowIncompleteScan) args.push("--allow-incomplete-scan");
  return await runState(script, args);
}

async function campaignSummary(script, campaignId) {
  return await runState(script, ["summary", "--campaign-id", campaignId]);
}

async function candidateKeys(script, campaignId) {
  return await runState(script, ["candidate-keys", "--campaign-id", campaignId]);
}

async function preflightStatus(script, campaignId, batchId = "") {
  const args = ["preflight", "--campaign-id", campaignId];
  if (batchId) args.push("--batch-id", batchId);
  return await runState(script, args);
}

async function recordSend(script, campaignId, key, status, detail = "", batchId = "", { allowIncompleteScan = false, messageSnapshot = "", actualUserId = "", actualAccountId = "" } = {}) {
  const args = ["record-send", "--campaign-id", campaignId, "--conversation-key", key];
  if (batchId) args.push("--batch-id", batchId);
  args.push("--status", status, "--detail", detail);
  if (messageSnapshot) args.push("--message-snapshot", messageSnapshot);
  if (actualUserId) args.push("--actual-user-id", actualUserId);
  if (actualAccountId) args.push("--actual-account-id", actualAccountId);
  if (allowIncompleteScan) args.push("--allow-incomplete-scan");
  return requireStateOk(await runState(script, args), "record_send_failed");
}

async function recordSendSafe(...args) {
  try {
    return { ok: true, payload: await recordSend(...args) };
  } catch (error) {
    return { ok: false, reason: String(error && error.message || error).slice(0, 240) };
  }
}

async function recordRetryable(script, batchId, key, reason) {
  return await runState(script, [
    "record-retryable",
    "--batch-id", batchId,
    "--conversation-key", key,
    "--reason", reason,
  ]);
}

async function markScan(script, campaignId, state, reason = "") {
  const args = ["mark-scan", "--campaign-id", campaignId, "--state", state];
  if (reason) args.push("--reason", reason);
  return await runState(script, args);
}

function markScanFailureReason(payload) {
  return statePayloadReason(payload);
}

async function markCurrentMonthWindow(script, campaignId, { windowStartDate, visibleOldestDate, reason = "" } = {}) {
  const args = [
    "mark-current-month-window",
    "--campaign-id", campaignId,
    "--window-start-date", windowStartDate,
    "--visible-oldest-date", visibleOldestDate,
  ];
  if (reason) args.push("--reason", reason);
  return await runState(script, args);
}

async function restoreTechnicalSkips(script, batchId) {
  return await runState(script, ["restore-technical-skips", "--batch-id", batchId]);
}

async function refreshDashboard(script, batchId) {
  if (!batchId) return { ok: true, skipped: true };
  return await runState(script, [
    "export-dashboard",
    "--batch-id", batchId,
    "--output", DEFAULT_DASHBOARD_PATH,
  ]);
}

async function refreshCampaignDashboard(script, campaignId) {
  if (!campaignId) return { ok: true, skipped: true };
  return await runState(script, [
    "export-dashboard",
    "--campaign-id", campaignId,
    "--output", DEFAULT_DASHBOARD_PATH,
  ]);
}

async function prepareBatch(script, campaignId, limit = 100, { allowIncompleteScan = false } = {}) {
  const args = ["prepare-batch", "--campaign-id", campaignId, "--limit", limit];
  if (allowIncompleteScan) args.push("--allow-incomplete-scan");
  return await runState(script, args);
}

async function approveBatch(script, batchId, { allowIncompleteScan = false } = {}) {
  const args = ["approve-batch", "--batch-id", batchId];
  if (allowIncompleteScan) args.push("--allow-incomplete-scan");
  return await runState(script, args);
}

async function batchQueue(script, batchId, { allowIncompleteScan = false } = {}) {
  const args = ["batch-queue", "--batch-id", batchId];
  if (allowIncompleteScan) args.push("--allow-incomplete-scan");
  return await runState(script, args);
}

async function historicalTargets(script, campaignId, {
  limit = 100,
  includeReviewed = false,
  reviewState = "",
} = {}) {
  const args = ["historical-targets", "--campaign-id", campaignId, "--limit", limit];
  if (includeReviewed) args.push("--include-reviewed");
  if (reviewState) args.push("--review-state", reviewState);
  return await runState(script, args);
}

async function markHistoricalReview(script, campaignId, target, state, {
  conversationKey = "",
  accountId = "",
  displayName = "",
  lastMessageAtMs = 0,
  reason = "",
} = {}) {
  const args = [
    "mark-historical-review",
    "--campaign-id", campaignId,
    "--user-id", target.user_id,
    "--state", state,
  ];
  if (conversationKey) args.push("--conversation-key", conversationKey);
  if (accountId) args.push("--account-id", accountId);
  if (displayName) args.push("--display-name", displayName);
  if (lastMessageAtMs) args.push("--last-message-at-ms", lastMessageAtMs);
  if (reason) args.push("--reason", reason);
  return await runState(script, args);
}

async function markHistoricalWindow(script, campaignId, { force = false, reason = "" } = {}) {
  const args = ["mark-historical-window", "--campaign-id", campaignId];
  if (force) args.push("--force");
  if (reason) args.push("--reason", reason);
  return await runState(script, args);
}

async function readVisibleMessageShells(tab) {
  return await tab.playwright.evaluate(() => {
    const norm = (s) => String(s || "").replace(/\s+/g, " ").trim();
    const isVisible = (el) => {
      const rect = el.getBoundingClientRect();
      return rect.width > 0 &&
        rect.height > 0 &&
        rect.bottom >= 0 &&
        rect.right >= 0 &&
        rect.top <= window.innerHeight &&
        rect.left <= window.innerWidth;
    };
    return [...document.querySelectorAll('[id^="jarvis-msg-"]')]
      .filter(isVisible)
      .map((el) => {
        const id = el.id || "";
        const cls = String(el.className || "");
        const side = cls.split(/\s+/).find((c) => c === "left" || c === "right" || c === "center") || "";
        const idTimestampMatch = id.match(/^jarvis-msg-(\d{13})/);
        return {
          id,
          side,
          type: el.getAttribute("data-msg-type") || "",
          timestamp: Number(el.getAttribute("data-timestamp") || idTimestampMatch?.[1] || 0),
          text: norm(el.innerText || el.textContent || "").slice(0, 1600),
        };
      });
  }, undefined, { timeoutMs: 10000 });
}

async function sendAndVerify(tab, message, userId, accountId) {
  const before = await readConversationState(tab, userId, accountId);
  if (!before.identityOk) {
    return { ok: false, reason: "send_identity_not_confirmed_before_type", possiblySent: false };
  }
  const beforeMaxTs = Math.max(0, ...before.messages.map((m) => Number(m.timestamp || 0)));
  const beforeMessageIds = new Set(before.messages.map((m) => m.id).filter(Boolean));
  const beforeVisibleMessages = await readVisibleMessageShells(tab);
  const beforeVisibleMaxTs = Math.max(0, ...beforeVisibleMessages.map((m) => Number(m.timestamp || 0)));
  const beforeVisibleMessageIds = new Set(beforeVisibleMessages.map((m) => m.id).filter(Boolean));
  const expectedText = normalizeText(message);
  const expectedCompact = compactText(message);
  const expectedCompactForVerification = compactTextForSendVerification(message);
  const input = tab.playwright.getByPlaceholder('按 "回车键" 发送信息...（输入/唤起快捷回复）', { exact: true });
  const inputCount = await input.count();
  if (inputCount !== 1) return { ok: false, reason: "send_input_count_" + inputCount, possiblySent: false };
  await input.fill(message, {});
  const filledState = await readConversationState(tab, userId, accountId);
  const filledValue = filledState.textArea ? filledState.textArea.value : "";
  if (filledValue !== message) {
    await input.fill("", {}).catch(() => {});
    return { ok: false, reason: "send_input_value_mismatch", possiblySent: false };
  }
  const button = tab.playwright.getByRole("button", { name: "发送", exact: true });
  const buttonCount = await button.count();
  if (buttonCount !== 1) {
    await input.fill("", {}).catch(() => {});
    return { ok: false, reason: "send_button_count_" + buttonCount, possiblySent: false };
  }
  const enabled = await button.isEnabled();
  if (!enabled) {
    await input.fill("", {}).catch(() => {});
    return { ok: false, reason: "send_button_disabled", possiblySent: false };
  }
  await button.click({});
  for (let i = 0; i < 12; i += 1) {
    await tab.playwright.waitForTimeout(500);
    const after = await readConversationState(tab, userId, accountId);
    const verifiedIdentityMessage = after.messages.some((m) =>
      m.side === "right" &&
      m.id &&
      !beforeMessageIds.has(m.id) &&
      Number(m.timestamp || 0) > beforeMaxTs &&
      sentMessageTextMatches(m.text, expectedText, expectedCompact, expectedCompactForVerification)
    );
    if (verifiedIdentityMessage) return { ok: true, reason: "verified_new_right_message" };
    if (after.identityOk && after.rightPanelUid === userId && after.inferredAccountId === accountId) {
      const afterVisibleMessages = await readVisibleMessageShells(tab);
      const verifiedVisibleMessage = afterVisibleMessages.some((m) =>
        m.side === "right" &&
        m.id &&
        !beforeVisibleMessageIds.has(m.id) &&
        Number(m.timestamp || 0) > beforeVisibleMaxTs &&
        sentMessageTextMatches(m.text, expectedText, expectedCompact, expectedCompactForVerification)
      );
      if (verifiedVisibleMessage) return { ok: true, reason: "verified_new_right_message_visible_temp_id" };
    }
  }
  return { ok: false, reason: "new_right_message_not_verified", possiblySent: true };
}

async function clearSearchBox(tab) {
  const input = tab.playwright.getByPlaceholder("输入用户ID/小红书ID", { exact: true });
  const count = await input.count();
  if (count !== 1) return { ok: false, reason: "search_input_count_" + count };
  await input.fill("", {});
  return { ok: true, reason: "" };
}

function normalizeQueueItem(item) {
  return {
    key: item.key || item.conversation_key || "",
    conversation_key: item.conversation_key || item.key || "",
    user_id: item.user_id || "",
    account_id: item.account_id || "",
    display_name: item.display_name || "",
    position: Number(item.position || 0),
    retryable_count: Number(item.retryable_count || 0),
  };
}

function matchVisibleQueueItem(visibleItem, rawPendingItems, attemptedKeys = new Set()) {
  const pendingItems = rawPendingItems.map(normalizeQueueItem);
  const exact = pendingItems.find((item) =>
    item.key && item.key === visibleItem.key && !attemptedKeys.has(item.key)
  );
  if (exact) return exact;

  const visibleIdentity = parseConversationKey(visibleItem.key);
  if (!visibleIdentity.ok) return null;
  const identityMatches = pendingItems.filter((item) => {
    if (!item.key || attemptedKeys.has(item.key)) return false;
    const parsed = parseConversationKey(item.key);
    const userId = item.user_id || parsed.userId;
    const accountId = item.account_id || parsed.accountId;
    if (!userId || userId !== visibleIdentity.userId) return false;
    return !accountId || !visibleIdentity.accountId || accountId === visibleIdentity.accountId;
  });
  return identityMatches.length === 1 ? identityMatches[0] : null;
}

async function locateQueueItemByListSweep(tab, targetItem, attemptedKeys = new Set(), { maxMs = 60000, maxScrolls = 80 } = {}) {
  const started = Date.now();
  const item = normalizeQueueItem(targetItem);
  const search = await clearSearchBox(tab);
  if (!search.ok) return { ok: false, reason: "list_fallback_clear_search_failed:" + search.reason };
  await tab.playwright.waitForTimeout(300);
  const reset = await resetConversationListToTop(tab);
  if (!reset.ok) return { ok: false, reason: "list_fallback_reset_failed:" + reset.reason };

  let noNewViewportScans = 0;
  let lastScrollHeight = -1;
  for (let scrolls = 0; Date.now() - started < maxMs && scrolls <= maxScrolls; scrolls += 1) {
    const view = await readVisibleConversationItems(tab);
    if (!view.ok) return { ok: false, reason: "list_fallback:" + view.reason };
    const visibleMatch = view.items
      .map((visibleItem) => ({
        visibleItem,
        item: matchVisibleQueueItem(visibleItem, [item], attemptedKeys),
      }))
      .find((entry) => entry.item);
    if (visibleMatch) {
      return {
        ok: true,
        reason: "located_by_list_fallback_queue_key_or_identity",
        item: visibleMatch.item,
        visibleKey: visibleMatch.visibleItem.key,
        identity: null,
      };
    }

    const heightStable = view.scroller && lastScrollHeight === view.scroller.scrollHeight;
    lastScrollHeight = view.scroller ? view.scroller.scrollHeight : lastScrollHeight;
    if (view.scroller && view.scroller.atBottom) {
      noNewViewportScans += 1;
      if (noNewViewportScans >= 2 && heightStable) {
        return { ok: false, reason: "list_fallback_no_matching_queue_item" };
      }
    } else {
      noNewViewportScans = 0;
    }
    const scrolled = await scrollConversationList(tab);
    if (!scrolled.ok) return { ok: false, reason: "list_fallback_scroll_failed:" + scrolled.reason };
    if (!scrolled.moved && (!view.scroller || !view.scroller.atBottom)) {
      return { ok: false, reason: "list_fallback_scroll_stalled" };
    }
    await tab.playwright.waitForTimeout(250);
  }
  return { ok: false, reason: "list_fallback_timeout_or_scroll_limit" };
}

function createXhsFollowupSession({
  tab,
  campaignId,
  message,
  accountIds = DEFAULT_ACCOUNT_IDS,
  stateScript = DEFAULT_STATE_SCRIPT,
  minSilenceHours = 24,
  dryRun = false,
  scanOnly = false,
  allowLegacyDirectSend = false,
  resumeOldQueue = false,
  resumeScan = false,
  dashboardMode = "off",
} = {}) {
  if (!tab) throw new Error("tab is required");
  if (!campaignId) throw new Error("campaignId is required");
  if (!message) throw new Error("message is required");
  if (!dryRun && !scanOnly && !allowLegacyDirectSend) {
    throw new Error("direct_send_disabled_use_scanOnly_then_prepareAndApproveBatch_and_sendBatch");
  }
  const allowedAccountIds = normalizeAccountIds(accountIds);
  const allowedAccountIdSet = new Set(allowedAccountIds);
  if (!allowedAccountIds.length) throw new Error("accountIds must contain at least one account_id");
  if (!DASHBOARD_MODES.has(dashboardMode)) {
    throw new Error("dashboardMode must be one of: " + [...DASHBOARD_MODES].join(","));
  }

  const seenConversationKeys = new Set();
  const sentIdentities = new Set();
  const stats = {
    scanned: 0,
    eligible: 0,
    sent: 0,
    skipped: 0,
    failed: 0,
    retryable: 0,
    uncertain: 0,
    quickLeadSkipped: 0,
    canSendDenied: 0,
    identityUnknown: 0,
    peerSkipped: 0,
    awaitingUsSkipped: 0,
    negativeSkipped: 0,
    recentSkipped: 0,
    accountSkipped: 0,
  };
  const reasonCounts = {};
  let consecutiveFailures = 0;
  let consecutiveBatchFailures = 0;
  let noNewViewportScans = 0;
  let lastScrollHeight = -1;
  let sendTraversal = null;
  let scanTraversalInitialized = false;
  let scanMarkedStarted = false;
  let scanMarkedCompleted = false;
  let cachedCampaignMessage = message;
  let loadedResumeSeenKeys = 0;
  let fastSeekUsed = false;

  function dashboardModeWants(mode, final = false) {
    return mode === "always" || (mode === "final" && final);
  }

  function resolveDashboardMode(override) {
    if (override == null) return dashboardMode;
    if (override === true) return "always";
    if (override === false) return "off";
    if (DASHBOARD_MODES.has(override)) return override;
    throw new Error("dashboard override must be false, true, or one of: " + [...DASHBOARD_MODES].join(","));
  }

  function countReason(reason) {
    const key = reason || "unknown";
    reasonCounts[key] = (reasonCounts[key] || 0) + 1;
  }

  function countsTowardConversationLimit(event) {
    return event && event.action !== "already_seen" && event.reason !== "account_not_allowed";
  }

  function isAccountAllowed(accountId) {
    return !!accountId && allowedAccountIdSet.has(accountId);
  }

  async function currentMessage() {
    const summary = await campaignSummary(stateScript, campaignId);
    if (summary && summary.ok && summary.message) {
      cachedCampaignMessage = summary.message;
      return cachedCampaignMessage;
    }
    throw new Error("campaign_message_read_failed:" + statePayloadReason(summary));
  }

  async function processItem(item) {
    if (seenConversationKeys.has(item.key)) return { action: "already_seen", key: item.key };
    seenConversationKeys.add(item.key);
    stats.scanned += 1;

    const parsed = parseConversationKey(item.key);
    if (!parsed.ok) {
      stats.skipped += 1;
      countReason(parsed.reason);
      return { action: "skip", key: item.key, reason: parsed.reason };
    }
    if (parsed.accountId && !isAccountAllowed(parsed.accountId)) {
      stats.skipped += 1;
      stats.accountSkipped += 1;
      countReason("account_not_allowed");
      return { action: "skip", key: item.key, reason: "account_not_allowed" };
    }
    const parsedIdentityKey = parsed.accountId ? parsed.userId + "::" + parsed.accountId : "";
    if (parsedIdentityKey && sentIdentities.has(parsedIdentityKey)) {
      stats.skipped += 1;
      countReason("identity_already_sent_in_memory");
      return { action: "skip", key: item.key, reason: "identity_already_sent_in_memory" };
    }
    if (item.hasQuickLead || STRONG_LEAD_RE.test(item.text)) {
      if (!dryRun && parsed.accountId && isAccountAllowed(parsed.accountId)) {
        await recordQuickLeadCandidate(stateScript, campaignId, item, parsed, "quick_has_lead");
      }
      stats.skipped += 1;
      stats.quickLeadSkipped += 1;
      countReason("quick_has_lead");
      return { action: "skip", key: item.key, reason: "quick_has_lead" };
    }

    const clicked = await clickConversation(tab, item.key);
    if (!clicked.ok) {
      stats.skipped += 1;
      stats.identityUnknown += 1;
      consecutiveFailures += 1;
      countReason(clicked.reason);
      return { action: "skip", key: item.key, reason: clicked.reason };
    }

    const identity = await waitForIdentity(tab, parsed.userId, parsed.accountId);
    if (!identity || !identity.identityOk) {
      stats.skipped += 1;
      stats.identityUnknown += 1;
      consecutiveFailures += 1;
      countReason("identity_not_confirmed");
      return { action: "skip", key: item.key, reason: "identity_not_confirmed" };
    }
    if (!isAccountAllowed(identity.inferredAccountId)) {
      stats.skipped += 1;
      stats.accountSkipped += 1;
      countReason("account_not_allowed");
      return { action: "skip", key: item.key, reason: "account_not_allowed" };
    }
    consecutiveFailures = 0;

    if (identity.hasLeadSignal) {
      const panelLeadClassification = applyManualOverride(parsed.userId, classifyMessages(identity.messages, identity.panelText, minSilenceHours, Date.now(), identity.header));
      panelLeadClassification.lead_status = "has_lead";
      panelLeadClassification.eligible = false;
      panelLeadClassification.exclusion_reason = "lead_status=has_lead";
      panelLeadClassification.manual_evidence = "panel_has_lead_signal";
      if (!dryRun) {
        await recordCandidate(stateScript, campaignId, item, identity, panelLeadClassification);
      }
      stats.skipped += 1;
      countReason("panel_has_lead");
      return { action: "skip", key: item.key, reason: "panel_has_lead" };
    }

    let classification = applyManualOverride(parsed.userId, classifyMessages(identity.messages, identity.panelText, minSilenceHours, Date.now(), identity.header));
    if (classification.lead_status === "no_lead") {
      await loadMoreHistoryIfNeeded(tab, parsed.userId, identity.inferredAccountId);
      const reloaded = await readConversationState(tab, parsed.userId, identity.inferredAccountId);
      classification = applyManualOverride(parsed.userId, classifyMessages(reloaded.messages, reloaded.panelText, minSilenceHours, Date.now(), reloaded.header));
      Object.assign(identity, reloaded);
    }

    if (dryRun) {
      if (classification.eligible) stats.eligible += 1;
      countReason("dry_run:" + (classification.exclusion_reason || "eligible"));
      return {
        action: classification.eligible ? "candidate" : "skip",
        key: item.key,
        reason: classification.exclusion_reason || "dry_run_eligible",
      };
    }

    await recordCandidate(stateScript, campaignId, item, identity, classification);

    if (scanOnly) {
      if (classification.eligible) stats.eligible += 1;
      if (!classification.eligible) {
        stats.skipped += 1;
        if (classification.party_status === "peer") stats.peerSkipped += 1;
        if (classification.reply_status === "awaiting_us") stats.awaitingUsSkipped += 1;
        if (classification.negative_status === "blocked") stats.negativeSkipped += 1;
        if (classification.exclusion_reason && classification.exclusion_reason.startsWith("silence_less_than")) stats.recentSkipped += 1;
      }
      countReason("scan:" + (classification.exclusion_reason || "eligible"));
      return {
        action: classification.eligible ? "candidate" : "classified",
        key: item.key,
        reason: classification.exclusion_reason || "eligible",
      };
    }

    if (!classification.eligible) {
      stats.skipped += 1;
      if (classification.party_status === "peer") stats.peerSkipped += 1;
      if (classification.reply_status === "awaiting_us") stats.awaitingUsSkipped += 1;
      if (classification.negative_status === "blocked") stats.negativeSkipped += 1;
      if (classification.exclusion_reason && classification.exclusion_reason.startsWith("silence_less_than")) stats.recentSkipped += 1;
      countReason(classification.exclusion_reason || "not_eligible");
      return { action: "skip", key: item.key, reason: classification.exclusion_reason || "not_eligible" };
    }

    const allowed = await canSend(stateScript, campaignId, item.key);
    if (!allowed.ok) {
      return { action: "skip", key: item.key, reason: "can_send_state_error:" + statePayloadReason(allowed) };
    }
    if (!allowed.allowed) {
      stats.skipped += 1;
      stats.canSendDenied += 1;
      countReason("can_send:" + (allowed.reason || "denied"));
      return { action: "skip", key: item.key, reason: "can_send:" + (allowed.reason || "denied") };
    }

    stats.eligible += 1;

    const beforeSend = await readConversationState(tab, parsed.userId, identity.inferredAccountId);
    const beforeClass = applyManualOverride(parsed.userId, classifyMessages(beforeSend.messages, beforeSend.panelText, minSilenceHours, Date.now(), beforeSend.header));
    if (!beforeSend.identityOk || !beforeClass.eligible) {
      stats.skipped += 1;
      countReason("pre_send_revalidation_failed");
      return { action: "skip", key: item.key, reason: "pre_send_revalidation_failed" };
    }

    const sendMessage = await currentMessage();
    const sent = await sendAndVerify(tab, sendMessage, parsed.userId, identity.inferredAccountId);
    if (sent.ok) {
      const recorded = await recordSendSafe(stateScript, campaignId, item.key, "sent", "已验证同账号同用户的新右侧消息气泡及新时间戳", "", { messageSnapshot: sendMessage });
      if (!recorded.ok) return { action: "error", key: item.key, reason: recorded.reason };
      sentIdentities.add(parsed.userId + "::" + identity.inferredAccountId);
      stats.sent += 1;
      return { action: "sent", key: item.key, reason: sent.reason };
    }

    if (!sent.possiblySent && isPreClickSendTechnicalReason(sent.reason)) {
      stats.retryable += 1;
      stats.identityUnknown += 1;
      consecutiveFailures += 1;
      countReason("send_preclick_retryable:" + sent.reason);
      return { action: "retryable", key: item.key, reason: sent.reason };
    }

    const status = sent.reason === "new_right_message_not_verified" ? "uncertain" : "failed";
    const recorded = await recordSendSafe(stateScript, campaignId, item.key, status, sent.reason, "", { messageSnapshot: sendMessage });
    if (!recorded.ok) return { action: "error", key: item.key, reason: recorded.reason };
    if (status === "uncertain") stats.uncertain += 1;
    else stats.failed += 1;
    consecutiveFailures += 1;
    countReason("send_" + status + ":" + sent.reason);
    return { action: status, key: item.key, reason: sent.reason };
  }

  async function runChunk({
    maxConversations = 30,
    maxSends = 8,
    maxMs = 180000,
    scanMode = "strict",
    targetDate = "",
    currentMonthWindowStartDate = "",
    scrollPages,
  } = {}) {
    if (!SCAN_MODES.has(scanMode)) {
      throw new Error("scanMode must be one of: " + [...SCAN_MODES].join(","));
    }
    if (scanMode === "fastSeek" && !scanOnly) {
      throw new Error("fastSeek requires scanOnly:true");
    }
    const started = Date.now();
    const initialSent = stats.sent;
    const events = [];
    const completionStableScans = scanOnly && !dryRun ? 5 : 2;
    const effectiveScrollPages = resolveScanScrollPages({ scanMode, scrollPages });
    let stoppedReason = "";
    let done = false;
    let dashboard = null;
    let scanProgress = null;
    const search = await clearSearchBox(tab);
    if (!search.ok) {
      return { done: false, stopped: true, stoppedReason: search.reason, stats: { ...stats }, reasonCounts: { ...reasonCounts }, events };
    }
    if (scanOnly && !dryRun && !scanMarkedStarted) {
      const marked = await markScan(stateScript, campaignId, "started");
      if (!marked.ok) {
        return { done: false, stopped: true, stoppedReason: "mark_scan_started_failed", stats: { ...stats }, reasonCounts: { ...reasonCounts }, events };
      }
      scanMarkedStarted = true;
    }
    if (scanOnly && !dryRun && !scanTraversalInitialized) {
      if (resumeScan) {
        const loaded = await candidateKeys(stateScript, campaignId);
        if (!loaded.ok) {
          const reason = "resume_scan_candidate_keys_failed:" + statePayloadReason(loaded);
          await markScan(stateScript, campaignId, "stopped", reason);
          return { done: false, stopped: true, stoppedReason: reason, stats: { ...stats }, reasonCounts: { ...reasonCounts }, events };
        }
        for (const key of loaded.keys || []) {
          if (key) seenConversationKeys.add(key);
        }
        loadedResumeSeenKeys = seenConversationKeys.size;
      } else {
        const reset = await resetConversationListToTop(tab);
        if (!reset.ok) {
          const reason = "scan_reset_to_top_failed:" + (reset.reason || "unknown");
          await markScan(stateScript, campaignId, "stopped", reason);
          return { done: false, stopped: true, stoppedReason: reason, stats: { ...stats }, reasonCounts: { ...reasonCounts }, events };
        }
        seenConversationKeys.clear();
      }
      scanTraversalInitialized = true;
      noNewViewportScans = 0;
      lastScrollHeight = -1;
      await tab.playwright.waitForTimeout(500);
    }

    while (Date.now() - started < maxMs) {
      if (events.filter(countsTowardConversationLimit).length >= maxConversations) break;
      if (!dryRun && stats.sent - initialSent >= maxSends) break;
      if (consecutiveFailures >= 3) {
        stoppedReason = "consecutive_failures";
        break;
      }

      const view = await readVisibleConversationItems(tab);
      if (!view.ok) {
        stoppedReason = view.reason;
        break;
      }
      scanProgress = buildScanProgress({
        view,
        seenConversationKeys: seenConversationKeys.size,
        loadedResumeSeenKeys,
        scanMode,
      });
      if (scanOnly && !dryRun && scanMode === "strict" && currentMonthWindowReached({
        scanProgress,
        windowStartDate: currentMonthWindowStartDate,
      })) {
        done = true;
        stoppedReason = "current_month_window_completed";
        break;
      }
      if (scanMode === "fastSeek") {
        fastSeekUsed = true;
        const reachedTarget = !!targetDate &&
          !!scanProgress.visibleOldestDate &&
          scanProgress.visibleOldestDate <= targetDate;
        if (reachedTarget) {
          stoppedReason = "target_date_reached";
          break;
        }
        if (view.scroller && view.scroller.atBottom) {
          stoppedReason = "fast_seek_bottom_reached";
          break;
        }
        const scrolled = await scrollConversationList(tab, { scrollPages: effectiveScrollPages });
        await tab.playwright.waitForTimeout(250);
        if (!scrolled.ok) {
          stoppedReason = "fast_seek_scroll_failed:" + scrolled.reason;
          break;
        }
        if (!scrolled.moved) {
          stoppedReason = "fast_seek_scroll_stalled";
          break;
        }
        continue;
      }
      const unseen = view.items.filter((item) => !seenConversationKeys.has(item.key));
      if (!unseen.length) {
        if (view.scroller && view.scroller.atBottom) {
          const heightStable = lastScrollHeight === view.scroller.scrollHeight;
          lastScrollHeight = view.scroller.scrollHeight;
          noNewViewportScans += 1;
          if (noNewViewportScans >= completionStableScans && heightStable) {
            const confirmedBottom = await nudgeConversationListAfterApparentBottom(tab, { scrollPages: effectiveScrollPages });
            if (!confirmedBottom.ok) {
              stoppedReason = "bottom_confirmation_failed:" + (confirmedBottom.reason || "unknown");
              break;
            }
            scanProgress = buildScanProgress({
              view: confirmedBottom.after,
              seenConversationKeys: seenConversationKeys.size,
              loadedResumeSeenKeys,
              scanMode,
            });
            if (confirmedBottom.signatureChanged || confirmedBottom.heightChanged || !confirmedBottom.atBottom) {
              noNewViewportScans = 0;
              lastScrollHeight = confirmedBottom.after && confirmedBottom.after.scroller
                ? confirmedBottom.after.scroller.scrollHeight
                : lastScrollHeight;
              await tab.playwright.waitForTimeout(500);
              continue;
            }
            if (fastSeekUsed) {
              done = false;
              stoppedReason = "partial_scan_window_completed";
            } else {
              done = true;
              stoppedReason = "completed";
            }
            break;
          }
          await tab.playwright.waitForTimeout(500);
          continue;
        }
        const scrolled = await scrollConversationList(tab, { scrollPages: effectiveScrollPages });
        await tab.playwright.waitForTimeout(350);
        if (!scrolled.ok) {
          stoppedReason = "scan_scroll_failed:" + scrolled.reason;
          break;
        }
        if (!scrolled.moved) {
          stoppedReason = "scan_scroll_stalled_before_bottom";
          break;
        }
        noNewViewportScans = 0;
        lastScrollHeight = scrolled.after ? scrolled.after.scrollHeight : lastScrollHeight;
        continue;
      }

      noNewViewportScans = 0;
      lastScrollHeight = view.scroller ? view.scroller.scrollHeight : lastScrollHeight;
      for (const item of unseen) {
        if (events.filter(countsTowardConversationLimit).length >= maxConversations) break;
        if (!dryRun && stats.sent - initialSent >= maxSends) break;
        if (Date.now() - started >= maxMs) break;
        let event = null;
        try {
          event = await processItem(item);
        } catch (error) {
          stoppedReason = String(error && error.message || error).slice(0, 240);
          break;
        }
        events.push(event);
        if (consecutiveFailures >= 3) {
          stoppedReason = "consecutive_failures";
          break;
        }
      }
    }

    if (scanOnly && !dryRun && stoppedReason) {
      if (done && stoppedReason === "completed" && !scanMarkedCompleted) {
        const marked = await markScan(stateScript, campaignId, "completed");
        if (marked.ok) scanMarkedCompleted = true;
        else {
          done = false;
          stoppedReason = "mark_scan_completed_failed:" + markScanFailureReason(marked);
        }
      } else if (done && stoppedReason === "current_month_window_completed") {
        const marked = await markCurrentMonthWindow(stateScript, campaignId, {
          windowStartDate: currentMonthWindowStartDate,
          visibleOldestDate: scanProgress && scanProgress.visibleOldestDate || "",
          reason: "runner_completed_current_month_window",
        });
        if (!marked.ok) {
          done = false;
          stoppedReason = "mark_current_month_window_failed:" + statePayloadReason(marked);
        }
      } else if (!["completed", "chunk_limit", "target_date_reached", "fast_seek_bottom_reached"].includes(stoppedReason)) {
        await markScan(stateScript, campaignId, "stopped", stoppedReason);
      }
    }

    if (scanOnly && !dryRun && dashboardModeWants(dashboardMode, done || (!!stoppedReason && stoppedReason !== "chunk_limit"))) {
      try {
        dashboard = await refreshCampaignDashboard(stateScript, campaignId);
      } catch (error) {
        dashboard = {
          ok: false,
          error: "dashboard_refresh_failed",
          detail: String(error && error.message || error).slice(0, 240),
        };
      }
    }

    return {
      done,
      stopped: !!stoppedReason && stoppedReason !== "completed",
      stoppedReason: stoppedReason || (done ? "completed" : "chunk_limit"),
      stats: { ...stats },
      reasonCounts: { ...reasonCounts },
      events: events.slice(-20),
      seenConversationKeys: seenConversationKeys.size,
      sentIdentities: sentIdentities.size,
      accountIds: allowedAccountIds.slice(),
      dryRun,
      resumeScan,
      loadedResumeSeenKeys,
      scanMode,
      targetDate,
      currentMonthWindowStartDate,
      scrollPages: effectiveScrollPages,
      scanProgress,
      partialScanWindow: fastSeekUsed,
      dashboardMode,
      dashboard,
    };
  }

  async function reviewHistoricalTargets({
    maxTargets = 30,
    maxMs = 180000,
    includeReviewed = false,
    reviewState = "",
    markWindowWhenComplete = true,
    forceMarkWindow = false,
  } = {}) {
    const started = Date.now();
    const events = [];
    let stoppedReason = "";
    let done = false;
    const targetsPayload = await historicalTargets(stateScript, campaignId, {
      limit: Math.max(1, maxTargets),
      includeReviewed,
      reviewState,
    });
    if (!targetsPayload.ok) {
      return {
        done: false,
        stopped: true,
        stoppedReason: "historical_targets_failed:" + statePayloadReason(targetsPayload),
        stats: { ...stats },
        reasonCounts: { ...reasonCounts },
        events,
        payload: targetsPayload,
      };
    }
    const targets = (targetsPayload.targets || []).map(normalizeQueueItem);
    for (const target of targets) {
      if (Date.now() - started >= maxMs) {
        stoppedReason = "chunk_limit";
        break;
      }
      if (!target.user_id) {
        events.push({ action: "historical_review_skipped", user_id: "", reason: "missing_user_id" });
        continue;
      }
      const located = await searchConversationByUid(tab, target, new Set(), allowedAccountIds);
      if (!located.ok) {
        const marked = await markHistoricalReview(stateScript, campaignId, target, "retryable", {
          reason: located.reason || "uid_search_failed",
        });
        if (!marked.ok) {
          stoppedReason = "mark_historical_retryable_failed:" + statePayloadReason(marked);
          break;
        }
        stats.retryable += 1;
        stats.identityUnknown += 1;
        countReason("historical_retryable:" + (located.reason || "uid_search_failed"));
        events.push({ action: "historical_retryable", user_id: target.user_id, reason: located.reason || "uid_search_failed" });
        continue;
      }
      const identity = located.identity || await waitForAllowedIdentity(tab, target.user_id, allowedAccountIdSet, 10);
      if (!identityAllowedForSend(identity, allowedAccountIdSet)) {
        const reason = identityFailureReason(identity, "historical_identity_not_confirmed");
        const marked = await markHistoricalReview(stateScript, campaignId, target, "retryable", { reason });
        if (!marked.ok) {
          stoppedReason = "mark_historical_retryable_failed:" + statePayloadReason(marked);
          break;
        }
        stats.retryable += 1;
        stats.identityUnknown += 1;
        countReason("historical_retryable:" + reason);
        events.push({ action: "historical_retryable", user_id: target.user_id, reason });
        continue;
      }
      await loadMoreHistoryIfNeeded(tab, target.user_id, identity.inferredAccountId);
      const current = await readConversationState(tab, target.user_id, identity.inferredAccountId);
      const classification = applyManualOverride(
        target.user_id,
        classifyMessages(current.messages, current.panelText, minSilenceHours, Date.now(), current.header)
      );
      const conversationKey = canonicalConversationKey(target.user_id, current.inferredAccountId || identity.inferredAccountId, located.visibleKey || target.key);
      if (classification.eligible) {
        const marked = await markHistoricalReview(stateScript, campaignId, target, "eligible", {
          conversationKey,
          accountId: current.inferredAccountId || identity.inferredAccountId,
          displayName: current.header || target.display_name || target.user_id,
          lastMessageAtMs: classification.last_message_at_ms,
          reason: "browser_uid_review_eligible",
        });
        if (!marked.ok) {
          stoppedReason = "mark_historical_eligible_failed:" + statePayloadReason(marked);
          break;
        }
        stats.eligible += 1;
        countReason("historical:eligible");
        events.push({ action: "historical_eligible", user_id: target.user_id, key: conversationKey, reason: "eligible" });
      } else {
        const reason = classification.exclusion_reason || "historical_not_eligible";
        const state = classification.lead_status === "unknown" ||
          classification.party_status === "unknown" ||
          classification.reply_status === "unknown" ||
          classification.negative_status === "unknown"
          ? "unknown"
          : "ineligible";
        const marked = await markHistoricalReview(stateScript, campaignId, target, state, {
          conversationKey,
          accountId: current.inferredAccountId || identity.inferredAccountId,
          displayName: current.header || target.display_name || target.user_id,
          reason,
        });
        if (!marked.ok) {
          stoppedReason = "mark_historical_ineligible_failed:" + statePayloadReason(marked);
          break;
        }
        stats.skipped += 1;
        countReason("historical:" + reason);
        events.push({ action: "historical_" + state, user_id: target.user_id, key: conversationKey, reason });
      }
    }

    if (!stoppedReason) {
      const remaining = await historicalTargets(stateScript, campaignId, {
        limit: 1,
        includeReviewed: false,
        reviewState,
      });
      if (!remaining.ok) {
        stoppedReason = "historical_targets_refresh_failed:" + statePayloadReason(remaining);
      } else if ((remaining.targets || []).length === 0) {
        done = true;
        stoppedReason = "completed";
        if (markWindowWhenComplete) {
          const marked = await markHistoricalWindow(stateScript, campaignId, {
            force: forceMarkWindow,
            reason: "runner_completed_historical_uid_review",
          });
          if (!marked.ok) {
            done = false;
            stoppedReason = "mark_historical_window_failed:" + statePayloadReason(marked);
          }
        }
      } else {
        stoppedReason = "chunk_limit";
      }
    }

    return {
      done,
      stopped: !!stoppedReason && stoppedReason !== "completed",
      stoppedReason: stoppedReason || (done ? "completed" : "chunk_limit"),
      stats: { ...stats },
      reasonCounts: { ...reasonCounts },
      events: events.slice(-20),
      reviewedThisRun: events.length,
      accountIds: allowedAccountIds.slice(),
      targetCount: targetsPayload.total_targetable,
      review: targetsPayload.review,
    };
  }


  async function prepareAndApproveBatch({ limit = 100, allowIncompleteScan = false } = {}) {
    const bypass = resolveBypassScanGate({ allowIncompleteScan, resumeOldQueue });
    const withDashboard = async (result) => {
      if (!result || !result.batchId) return result;
      if (!dashboardModeWants(dashboardMode, true)) {
        result.dashboard = { ok: true, skipped: true, reason: "dashboard_refresh_disabled" };
        return result;
      }
      try {
        result.dashboard = await refreshDashboard(stateScript, result.batchId);
      } catch (error) {
        result.dashboard = {
          ok: false,
          error: "dashboard_refresh_failed",
          detail: String(error && error.message || error).slice(0, 240),
        };
      }
      return result;
    };
    const prepared = await prepareBatch(stateScript, campaignId, limit, { allowIncompleteScan: bypass });
    if (!prepared.ok && prepared.error === "open_batch_exists" && prepared.batch_id) {
      if (prepared.state === "draft") {
        const approved = await approveBatch(stateScript, prepared.batch_id, { allowIncompleteScan: bypass });
        return await withDashboard({ ok: approved.ok, reused: true, batchId: prepared.batch_id, payload: approved });
      }
      const queue = await batchQueue(stateScript, prepared.batch_id, { allowIncompleteScan: bypass });
      return await withDashboard({ ok: queue.ok, reused: true, batchId: prepared.batch_id, payload: queue });
    }
    if (!prepared.ok || !prepared.batch_id) {
      return { ok: prepared.ok, batchId: prepared.batch_id || null, payload: prepared };
    }
    const approved = await approveBatch(stateScript, prepared.batch_id, { allowIncompleteScan: bypass });
    return await withDashboard({ ok: approved.ok, reused: false, batchId: prepared.batch_id, payload: approved });
  }

  async function markRetryable(batchId, item, reason, events) {
    const recorded = await recordRetryable(stateScript, batchId, item.key, reason);
    if (!recorded.ok) return { ok: false, reason: "record_retryable_failed:" + (recorded.error || "unknown") };
    stats.retryable += 1;
    stats.identityUnknown += 1;
    countReason("batch_retryable:" + reason);
    events.push({ action: "retryable", key: item.key, reason });
    return { ok: true, reason };
  }

  async function handleLocatedQueueItem({ batchId, item, visibleKey, identity, events, allowIncompleteScan = false }) {
    const bypassScanGate = resolveBypassScanGate({ allowIncompleteScan, resumeOldQueue });
    let sendMessage = "";
    try {
      sendMessage = await currentMessage();
    } catch (error) {
      return { ok: false, stoppedReason: String(error && error.message || error).slice(0, 240) };
    }
    if (!isAccountAllowed(item.account_id)) {
      const recorded = await recordSendSafe(stateScript, campaignId, item.key, "skipped", "account_not_allowed", batchId, { allowIncompleteScan: bypassScanGate, messageSnapshot: sendMessage });
      if (!recorded.ok) return { ok: false, stoppedReason: recorded.reason };
      stats.skipped += 1;
      stats.accountSkipped += 1;
      countReason("batch_account_not_allowed");
      events.push({ action: "skipped", key: item.key, reason: "account_not_allowed" });
      consecutiveBatchFailures = 0;
      return { ok: true, outcome: "skipped", sent: false, retryable: false, attempted: false };
    }

    let confirmedIdentity = identity || null;
    if (!identityAllowedForSend(confirmedIdentity, allowedAccountIdSet)) {
      const clicked = await clickConversation(tab, visibleKey);
      confirmedIdentity = clicked.ok ? await waitForAllowedIdentity(tab, item.user_id, allowedAccountIdSet) : null;
      if (!clicked.ok || !confirmedIdentity || !identityAllowedForSend(confirmedIdentity, allowedAccountIdSet)) {
        const reason = !clicked.ok ? clicked.reason : identityFailureReason(confirmedIdentity, "pre_send_identity_failed");
        const retry = await markRetryable(batchId, item, reason, events);
        if (!retry.ok) return { ok: false, stoppedReason: retry.reason };
        return { ok: true, outcome: "retryable", sent: false, retryable: true, attempted: false };
      }
    }
    consecutiveFailures = 0;

    const userId = item.user_id;
    const accountId = confirmedIdentity.inferredAccountId;
    if (!isAccountAllowed(accountId)) {
      const recorded = await recordSendSafe(stateScript, campaignId, item.key, "skipped", "located_account_not_allowed", batchId, { allowIncompleteScan: bypassScanGate, messageSnapshot: sendMessage, actualUserId: userId, actualAccountId: accountId });
      if (!recorded.ok) return { ok: false, stoppedReason: recorded.reason };
      stats.skipped += 1;
      stats.accountSkipped += 1;
      countReason("batch_located_account_not_allowed");
      events.push({ action: "skipped", key: item.key, reason: "located_account_not_allowed" });
      consecutiveBatchFailures = 0;
      return { ok: true, outcome: "skipped", sent: false, retryable: false, attempted: false };
    }

    await loadMoreHistoryIfNeeded(tab, userId, accountId);
    const current = await readConversationState(tab, userId, accountId);
    if (!current.identityOk || !isAccountAllowed(current.inferredAccountId)) {
      const retry = await markRetryable(batchId, item, identityFailureReason(current, "pre_send_identity_failed"), events);
      if (!retry.ok) return { ok: false, stoppedReason: retry.reason };
      return { ok: true, outcome: "retryable", sent: false, retryable: true, attempted: false };
    }

    const classification = applyManualOverride(userId, classifyMessages(current.messages, current.panelText, minSilenceHours, Date.now(), current.header));
    try {
      const candidateKey = current.inferredAccountId === item.account_id
        ? item.key
        : canonicalConversationKey(userId, current.inferredAccountId, item.key);
      await recordCandidate(stateScript, campaignId, { key: candidateKey, displayName: item.display_name }, current, classification);
    } catch (error) {
      return { ok: false, stoppedReason: String(error && error.message || error).slice(0, 240) };
    }
    if (!classification.eligible) {
      const reason = classification.exclusion_reason || "pre_send_not_eligible";
      const recorded = await recordSendSafe(stateScript, campaignId, item.key, "skipped", reason, batchId, { allowIncompleteScan: bypassScanGate, messageSnapshot: sendMessage, actualUserId: userId, actualAccountId: accountId });
      if (!recorded.ok) return { ok: false, stoppedReason: recorded.reason };
      stats.skipped += 1;
      countReason("batch_revalidate:" + reason);
      events.push({ action: "skipped", key: item.key, reason });
      consecutiveBatchFailures = 0;
      return { ok: true, outcome: "skipped", sent: false, retryable: false, attempted: false };
    }

    const allowed = await canSend(stateScript, campaignId, item.key, batchId, { allowIncompleteScan: bypassScanGate });
    if (!allowed.ok) {
      return { ok: false, stoppedReason: "can_send_state_error:" + statePayloadReason(allowed) };
    }
    if (!allowed.allowed) {
      const reason = allowed.reason || "denied";
      const technicalReasons = new Set([
        "candidate_not_found",
        "batch_campaign_mismatch",
        "candidate_not_in_batch",
        "batch_item_identity_mismatch",
      ]);
      if (technicalReasons.has(reason)) {
        const retry = await markRetryable(batchId, item, "can_send:" + reason, events);
        if (!retry.ok) return { ok: false, stoppedReason: retry.reason };
        return { ok: true, outcome: "retryable", sent: false, retryable: true, attempted: false };
      }
      if (["campaign_not_approved", "campaign_expired", "batch_not_approved"].includes(reason)) {
        return { ok: false, stoppedReason: "can_send:" + reason };
      }
      const detail = "can_send:" + reason;
      const recorded = await recordSendSafe(stateScript, campaignId, item.key, "skipped", detail, batchId, { allowIncompleteScan: bypassScanGate, messageSnapshot: sendMessage, actualUserId: userId, actualAccountId: accountId });
      if (!recorded.ok) return { ok: false, stoppedReason: recorded.reason };
      stats.skipped += 1;
      stats.canSendDenied += 1;
      countReason(detail);
      events.push({ action: "skipped", key: item.key, reason: detail });
      consecutiveBatchFailures = 0;
      return { ok: true, outcome: "skipped", sent: false, retryable: false, attempted: false };
    }

    const sent = await sendAndVerify(tab, sendMessage, userId, accountId);
    if (sent.ok) {
      const recorded = await recordSendSafe(stateScript, campaignId, item.key, "sent", "已验证同账号同用户的新右侧消息气泡及新时间戳", batchId, { allowIncompleteScan: bypassScanGate, messageSnapshot: sendMessage, actualUserId: userId, actualAccountId: accountId });
      if (!recorded.ok) return { ok: false, stoppedReason: recorded.reason };
      sentIdentities.add(userId + "::" + accountId);
      stats.sent += 1;
      events.push({ action: "sent", key: item.key, reason: sent.reason });
      consecutiveBatchFailures = 0;
      return { ok: true, outcome: "sent", sent: true, retryable: false, attempted: true };
    }

    if (!sent.possiblySent && isPreClickSendTechnicalReason(sent.reason)) {
      const retry = await markRetryable(batchId, item, sent.reason, events);
      if (!retry.ok) return { ok: false, stoppedReason: retry.reason };
      return { ok: true, outcome: "retryable", sent: false, retryable: true, attempted: false };
    }

    const status = sent.reason === "new_right_message_not_verified" ? "uncertain" : "failed";
    const recorded = await recordSendSafe(stateScript, campaignId, item.key, status, sent.reason, batchId, { allowIncompleteScan: bypassScanGate, messageSnapshot: sendMessage, actualUserId: userId, actualAccountId: accountId });
    if (!recorded.ok) return { ok: false, stoppedReason: recorded.reason };
    if (status === "uncertain") stats.uncertain += 1;
    else stats.failed += 1;
    consecutiveFailures += 1;
    consecutiveBatchFailures += 1;
    countReason("send_" + status + ":" + sent.reason);
    events.push({ action: status, key: item.key, reason: sent.reason });
    if (status === "uncertain") {
      return { ok: false, stoppedReason: "uncertain_send_requires_manual_review", outcome: status, attempted: true };
    }
    if (consecutiveBatchFailures >= 3) {
      return { ok: false, stoppedReason: "consecutive_batch_failures", outcome: status, attempted: true };
    }
    return { ok: true, outcome: status, sent: false, retryable: false, attempted: true };
  }

  async function sendBatch({ batchId, maxSends = 20, maxMs = 180000, allowIncompleteScan = false, uidSearchFirst = true, onlyUserId = "", dashboardRefresh = null, skipListFallbackOnUidFail = false } = {}) {
    if (!batchId) throw new Error("batchId is required");
    const started = Date.now();
    const events = [];
    let sentThisRun = 0;
    let attemptedThisRun = 0;
    let retryableThisRun = 0;
    let uidSearchThisRun = 0;
    let stoppedReason = "";
    let pendingRemaining = null;
    let dashboardRefreshes = 0;
    let dashboardLast = null;
    let cachedPending = null;
    const effectiveDashboardMode = resolveDashboardMode(dashboardRefresh);
    const preflightBefore = await preflightStatus(stateScript, campaignId, batchId);

    async function refreshDashboardAfterEvent(final = false) {
      if (!dashboardModeWants(effectiveDashboardMode, final)) {
        dashboardLast = { ok: true, skipped: true, reason: "dashboard_refresh_disabled" };
        return dashboardLast;
      }
      try {
        const refreshed = await refreshDashboard(stateScript, batchId);
        dashboardLast = refreshed;
        if (refreshed && refreshed.ok) dashboardRefreshes += 1;
        return refreshed;
      } catch (error) {
        dashboardLast = { ok: false, error: "dashboard_refresh_failed", detail: String(error && error.message || error).slice(0, 240) };
        return dashboardLast;
      }
    }

    async function loadPendingQueue(force = false) {
      if (!force && cachedPending) return { ok: true, pending: cachedPending };
      const queuePayload = await batchQueue(stateScript, batchId, { allowIncompleteScan: resolveBypassScanGate({ allowIncompleteScan, resumeOldQueue }) });
      if (!queuePayload.ok) return { ok: false, reason: queuePayload.error || "batch_queue_failed" };
      cachedPending = (queuePayload.queue || [])
        .map(normalizeQueueItem)
        .sort((a, b) => a.retryable_count - b.retryable_count || a.position - b.position);
      return { ok: true, pending: cachedPending };
    }

    if (!sendTraversal || sendTraversal.batchId !== batchId) {
      if (!uidSearchFirst) {
        const search = await clearSearchBox(tab);
        if (!search.ok) {
          return {
            done: false,
            stopped: true,
            stoppedReason: search.reason,
            batchId,
            stats: { ...stats },
            reasonCounts: { ...reasonCounts },
            events,
            sentThisRun,
            attemptedThisRun,
            retryableThisRun,
            uidSearchThisRun,
            dashboardRefreshes,
            dashboardLast,
            preflightBefore,
            pendingRemaining,
            accountIds: allowedAccountIds.slice(),
          };
        }
        const reset = await resetConversationListToTop(tab);
        if (!reset.ok) {
          return {
            done: false,
            stopped: true,
            stoppedReason: reset.reason,
            batchId,
            stats: { ...stats },
            reasonCounts: { ...reasonCounts },
            events,
            sentThisRun,
            attemptedThisRun,
            retryableThisRun,
            uidSearchThisRun,
            dashboardRefreshes,
            dashboardLast,
            preflightBefore,
            pendingRemaining,
            accountIds: allowedAccountIds.slice(),
          };
        }
      }
      sendTraversal = {
        batchId,
        attemptedKeys: new Set(),
        noNewViewportScans: 0,
        lastScrollHeight: -1,
      };
    }

    while (Date.now() - started < maxMs && attemptedThisRun < maxSends) {
      if (consecutiveFailures >= 3 || consecutiveBatchFailures >= 3) {
        stoppedReason = consecutiveBatchFailures >= 3 ? "consecutive_batch_failures" : "consecutive_failures";
        break;
      }
      const queueState = await loadPendingQueue(false);
      if (!queueState.ok) {
        stoppedReason = queueState.reason;
        break;
      }
      const pending = queueState.pending
        .filter((candidate) => !onlyUserId || candidate.user_id === onlyUserId);
      pendingRemaining = pending.length;
      if (!pending.length) {
        stoppedReason = onlyUserId ? "target_user_not_pending" : "completed";
        sendTraversal = null;
        break;
      }

      if (uidSearchFirst) {
        const uidSearchItem = pending.find((candidate) =>
          candidate.key && candidate.user_id && candidate.account_id && !sendTraversal.attemptedKeys.has(candidate.key)
        );
        if (!uidSearchItem) {
          const refreshed = await loadPendingQueue(true);
          const refreshedPending = refreshed.ok
            ? refreshed.pending.filter((candidate) => !onlyUserId || candidate.user_id === onlyUserId)
            : [];
          pendingRemaining = refreshedPending.length;
          stoppedReason = pendingRemaining ? "batch_list_exhausted_with_pending" : "completed";
          if (!pendingRemaining) sendTraversal = null;
          break;
        }
        const uidSearchTimeoutMs = skipListFallbackOnUidFail
          ? Math.max(5000, Math.min(12000, maxMs - (Date.now() - started)))
          : Math.max(5000, Math.min(60000, maxMs - (Date.now() - started)));
        const locatedByUid = await searchConversationByUid(
          tab,
          uidSearchItem,
          sendTraversal.attemptedKeys,
          allowedAccountIds,
          { deadlineAt: Date.now() + uidSearchTimeoutMs }
        );
        uidSearchThisRun += 1;
        if (!locatedByUid.ok) {
          const fallback = resolveUidFailureFallback({
            skipListFallbackOnUidFail,
            maxMs,
            elapsedMs: Date.now() - started,
          });
          if (!fallback.useListFallback) {
            sendTraversal.attemptedKeys.add(uidSearchItem.key);
            const retry = await markRetryable(
              batchId,
              uidSearchItem,
              locatedByUid.reason + "; list_fallback:" + fallback.reason,
              events
            );
            await refreshDashboardAfterEvent(false);
            if (!retry.ok) {
              stoppedReason = retry.reason;
              break;
            }
            retryableThisRun += 1;
            continue;
          }
          const locatedByList = await locateQueueItemByListSweep(
            tab,
            uidSearchItem,
            sendTraversal.attemptedKeys,
            { maxMs: fallback.listFallbackMs }
          );
          if (!locatedByList.ok) {
            sendTraversal.attemptedKeys.add(uidSearchItem.key);
            const retry = await markRetryable(
              batchId,
              uidSearchItem,
              locatedByUid.reason + "; list_fallback:" + locatedByList.reason,
              events
            );
            await refreshDashboardAfterEvent(false);
            if (!retry.ok) {
              stoppedReason = retry.reason;
              break;
            }
            retryableThisRun += 1;
            continue;
          }
          sendTraversal.attemptedKeys.add(locatedByList.item.key);
          const handled = await handleLocatedQueueItem({
            batchId,
            item: locatedByList.item,
            visibleKey: locatedByList.visibleKey,
            identity: locatedByList.identity,
            events,
            allowIncompleteScan,
          });
          if (handled.attempted) attemptedThisRun += 1;
          await refreshDashboardAfterEvent(false);
          if (!handled.ok) {
            stoppedReason = handled.stoppedReason;
            break;
          }
          if (handled.sent) sentThisRun += 1;
          if (handled.retryable) retryableThisRun += 1;
          continue;
        }
        sendTraversal.attemptedKeys.add(locatedByUid.item.key);
        const handled = await handleLocatedQueueItem({
          batchId,
          item: locatedByUid.item,
          visibleKey: locatedByUid.visibleKey,
          identity: locatedByUid.identity,
          events,
          allowIncompleteScan,
        });
        if (handled.attempted) attemptedThisRun += 1;
        await refreshDashboardAfterEvent(false);
        if (!handled.ok) {
          stoppedReason = handled.stoppedReason;
          break;
        }
        if (handled.sent) sentThisRun += 1;
        if (handled.retryable) retryableThisRun += 1;
        continue;
      }

      const view = await readVisibleConversationItems(tab);
      if (!view.ok) {
        stoppedReason = view.reason;
        break;
      }
      const visibleMatch = view.items
        .map((visibleItem) => ({
          visibleItem,
          item: matchVisibleQueueItem(visibleItem, pending, sendTraversal.attemptedKeys),
        }))
        .find((entry) => entry.item);

      if (!visibleMatch) {
        const heightStable = view.scroller && sendTraversal.lastScrollHeight === view.scroller.scrollHeight;
        sendTraversal.lastScrollHeight = view.scroller ? view.scroller.scrollHeight : sendTraversal.lastScrollHeight;
        if (view.scroller && view.scroller.atBottom) {
          sendTraversal.noNewViewportScans += 1;
          if (sendTraversal.noNewViewportScans >= 2 && heightStable) {
            const uidSearchItem = pending.find((candidate) =>
              candidate.key && candidate.user_id && candidate.account_id && !sendTraversal.attemptedKeys.has(candidate.key)
            );
            if (!uidSearchItem) {
              const refreshed = await loadPendingQueue(true);
              const refreshedPending = refreshed.ok
                ? refreshed.pending.filter((candidate) => !onlyUserId || candidate.user_id === onlyUserId)
                : [];
              pendingRemaining = refreshedPending.length;
              stoppedReason = pendingRemaining ? "batch_list_exhausted_with_pending" : "completed";
              if (!pendingRemaining) sendTraversal = null;
              break;
            }
            const locatedByUid = await searchConversationByUid(tab, uidSearchItem, sendTraversal.attemptedKeys, allowedAccountIds);
            uidSearchThisRun += 1;
              if (!locatedByUid.ok) {
              sendTraversal.attemptedKeys.add(uidSearchItem.key);
              const retry = await markRetryable(batchId, uidSearchItem, locatedByUid.reason, events);
              await refreshDashboardAfterEvent(false);
              if (!retry.ok) {
                stoppedReason = retry.reason;
                break;
              }
              retryableThisRun += 1;
              continue;
            }
            sendTraversal.attemptedKeys.add(locatedByUid.item.key);
            const handled = await handleLocatedQueueItem({
              batchId,
              item: locatedByUid.item,
              visibleKey: locatedByUid.visibleKey,
              identity: locatedByUid.identity,
              events,
              allowIncompleteScan,
            });
            if (handled.attempted) attemptedThisRun += 1;
            await refreshDashboardAfterEvent(false);
            if (!handled.ok) {
              stoppedReason = handled.stoppedReason;
              break;
            }
            if (handled.sent) sentThisRun += 1;
            if (handled.retryable) retryableThisRun += 1;
            continue;
          }
        } else {
          sendTraversal.noNewViewportScans = 0;
        }
        const scrolled = await scrollConversationList(tab);
        if (!scrolled.ok) {
          stoppedReason = scrolled.reason;
          break;
        }
        if (!scrolled.moved && (!view.scroller || !view.scroller.atBottom)) {
          stoppedReason = "batch_list_scroll_stalled";
          break;
        }
        continue;
      }

      sendTraversal.noNewViewportScans = 0;
      const item = visibleMatch.item;
      const visibleKey = visibleMatch.visibleItem.key;
      sendTraversal.attemptedKeys.add(item.key);
      const handled = await handleLocatedQueueItem({ batchId, item, visibleKey, identity: null, events, allowIncompleteScan });
      if (handled.attempted) attemptedThisRun += 1;
      await refreshDashboardAfterEvent(false);
      if (!handled.ok) {
        stoppedReason = handled.stoppedReason;
        break;
      }
      if (handled.sent) sentThisRun += 1;
      if (handled.retryable) retryableThisRun += 1;
    }

    try {
      const finalQueue = await loadPendingQueue(true);
      if (finalQueue && finalQueue.ok) {
        pendingRemaining = finalQueue.pending.length;
        if (!stoppedReason && pendingRemaining === 0) {
          stoppedReason = "completed";
          sendTraversal = null;
        }
      }
    } catch (_error) {
      // Completion reporting is best-effort; state changes and dashboard refreshes already happened per item.
    }
    await refreshDashboardAfterEvent(true);

    return {
      done: stoppedReason === "completed",
      stopped: !!stoppedReason && stoppedReason !== "completed",
      stoppedReason: stoppedReason || "chunk_limit",
      batchId,
      stats: { ...stats },
      reasonCounts: { ...reasonCounts },
      events: events.slice(-20),
      sentThisRun,
      attemptedThisRun,
      retryableThisRun,
      uidSearchThisRun,
      dashboardRefreshes,
      dashboardLast,
      dashboardMode: effectiveDashboardMode,
      preflightBefore,
      pendingRemaining,
      traversalMode: uidSearchFirst ? "uid_search_first_then_list_fallback" : "full_list_queue_key_sweep_then_uid_search_fallback",
      uidSearchFallbackEnabled: true,
      accountIds: allowedAccountIds.slice(),
    };
  }


  return {
    runChunk,
    reviewHistoricalTargets,
    prepareAndApproveBatch,
    sendBatch,
    preflightStatus: (batchId = "") => preflightStatus(stateScript, campaignId, batchId),
    restoreTechnicalSkips: (batchId) => restoreTechnicalSkips(stateScript, batchId),
    stats,
    reasonCounts,
    seenConversationKeys,
    sentIdentities,
    accountIds: allowedAccountIds,
    readVisibleConversationItems: () => readVisibleConversationItems(tab),
    searchConversationByUid: (item, tried = new Set()) => searchConversationByUid(tab, item, tried, allowedAccountIds),
  };
}

export {
  classifyMessages,
  createXhsFollowupSession,
  parseConversationKey,
  parseMessageIdentity,
  DEFAULT_ACCOUNT_IDS,
  bottomCompletionSignature,
  buildScanProgress,
  currentMonthWindowReached,
  deadlineExceeded,
  extractConversationDate,
  matchVisibleQueueItem,
  normalizeQueueItem,
  resolvePopupCoordinateClickTarget,
  readVisibleConversationItems,
  readConversationState,
  resetConversationListToTop,
  resolveBypassScanGate,
  resolveScanScrollPages,
  resolveUidFailureFallback,
  runWithTimeout,
  searchConversationByUid,
};
