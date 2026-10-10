// Pure evidence handling. No browser, external model, or ledger side effects.
import {createHash} from "node:crypto";

export const REVIEW_POLICY = "2026-09-30-batch-v1";
const plain = value => String(value || "").normalize("NFKC").trim();
// Deliberately excludes numbers and acknowledgements: “好/是/OK” may confirm
// that a contact was added, and several numeric messages may form one phone.
const simpleQuestions = new Set([
  "你好", "您好", "哈喽", "课程介绍", "了解线下课", "参加沙龙", "报名线下课",
  "在哪举办", "在哪里举办", "请问在哪举办", "怎么参加", "如何参加",
  "请问如何参加", "请问怎么参加", "要付费吗", "是免费的吗", "请问是免费参加吗",
]);
function simple(message) {
  return (message.type === "TEXT" || (message.type === "GENERAL" && message.plainText === true)) &&
    simpleQuestions.has(plain(message.text).replace(/[？?！!。]+$/u, ""));
}
function readableAddition(message) {
  return Boolean(plain(message.text)) && !/\[图片\]/u.test(message.text) &&
    (message.type === "TEXT" || (message.type === "GENERAL" && message.plainText === true) ||
      (message.type === "GENERAL" && message.side === "right" &&
        message.extracted_text_complete === true && message.decorative_media_only === true) ||
      (message.type === "HINT" && message.side === "center"));
}
const messageValue = m => [m.id, m.side, m.type, m.plainText === true, m.timestamp, m.text,
  m.extracted_text_complete === true, m.decorative_media_only === true];
const sameMessage = (a, b) => JSON.stringify(messageValue(a)) === JSON.stringify(messageValue(b));
const validReview = r => r.identity_verified === true && r.panel_loaded === true &&
  r.history_complete === true && !r.draft_present && Boolean(r.history_boundary_message_id) &&
  Array.isArray(r.messages) && r.messages.length > 0 && r.messages.every(m => typeof m.id === "string" && m.id);

export function reviewedHistoryCache(entry) {
  if (entry?.policy !== REVIEW_POLICY || !entry.approved) return {};
  return Object.fromEntries(entry.reviews.filter(r => r.context_complete).map(r => [r.account_id, {
    boundary_message_id: r.history_boundary_message_id,
    outbound_text_hashes: r.outbound_text_hashes || [],
  }]));
}

export function makeReviewPacket(observation, previous = null) {
  if (observation.status !== "observed" || !observation.search_complete || !observation.coverage_complete ||
      !observation.uid || !observation.reviews?.length ||
      observation.reviews.some(r => !validReview(r) || r.user_id !== observation.uid)) return null;
  const accounts = observation.reviews.map(r => r.account_id);
  if (new Set(accounts).size !== accounts.length ||
      JSON.stringify([...accounts].sort()) !== JSON.stringify([...observation.discovered_account_ids].sort())) return null;
  const prior = previous?.policy === REVIEW_POLICY && previous.uid === observation.uid && previous.approved ? previous : null;
  const reviews = observation.reviews.map(r => {
    const old = prior?.reviews.find(p => p.account_id === r.account_id);
    const boundary = old && r.messages.find(m => m.id === old.history_boundary_message_id);
    const oldBoundary = old?.messages.find(m => m.id === old.history_boundary_message_id);
    const incrementalProof = !!(old?.context_complete && r.history_reached_prior_boundary &&
      boundary && oldBoundary && sameMessage(boundary, oldBoundary));
    // A full current snapshot is authoritative, including removed messages.
    // Cached history fills only an explicitly incremental observation.
    const combined = new Map(incrementalProof && r.history_from_start_complete !== true
      ? old.messages.map(m => [m.id, m]) : []);
    for (const m of r.messages) combined.set(m.id, structuredClone(m));
    const messages = [...combined.values()].sort((a,b) => a.timestamp - b.timestamp || a.id.localeCompare(b.id));
    return {...structuredClone(r), messages,
      context_complete: r.history_from_start_complete === true || incrementalProof};
  });
  // No token or selected page state is durable authority to click.
  const evidence = {policy: REVIEW_POLICY, uid: observation.uid,
    accounts: [...accounts].sort(), reviews: reviews.map(r => ({
      account_id: r.account_id, label: r.label, no_intent: r.no_intent_label,
      visual: r.needs_visual_review, complete: r.context_complete,
      boundary: r.history_boundary_message_id, messages: r.messages.map(messageValue),
    })).sort((a,b) => a.account_id.localeCompare(b.account_id))};
  const evidence_id = createHash("sha256").update(JSON.stringify(evidence)).digest("hex");
  return {policy: REVIEW_POLICY, uid: observation.uid, evidence_id,
    observed_at_ms: observation.finished_at_ms, approved: false, reviews};
}

export function automaticDecisions(packet, previous = null) {
  if (!packet || packet.reviews.some(r => !r.context_complete ||
      r.label !== "none" || r.no_intent_label || r.platform_lead_signal)) return null;
  const prior = previous?.policy === REVIEW_POLICY && previous.uid === packet.uid && previous.approved ? previous : null;
  const decisions = [];
  for (const r of packet.reviews) {
    const old = prior?.reviews.find(p => p.account_id === r.account_id);
    const decision = prior?.decisions.find(d => d.account_id === r.account_id);
    if (old && decision && old.context_complete) {
      const current = new Map(r.messages.map(m => [m.id, m]));
      const unchanged = old.messages.every(m => current.has(m.id) && sameMessage(m, current.get(m.id)));
      if (!unchanged) return null;
      const oldIds = new Set(old.messages.map(m => m.id));
      const added = r.messages.filter(m => !oldIds.has(m.id));
      // Never erase a proved historical contact/opt-out. Otherwise every new
      // customer message must itself be an unambiguous complete question.
      // Prior visual approval applies only to unchanged historical material.
      // Every addition must be extracted text; new media always needs reading.
      const visualCleared = added.every(readableAddition) &&
        (!r.needs_visual_review || prior.visual_reviewed === true);
      if (visualCleared && (decision.contact_shared || decision.stop_contact || added.every(m => m.side !== "left" || simple(m)))) {
        decisions.push(structuredClone(decision));
        continue;
      }
      return null;
    }
    if (!r.needs_visual_review && r.messages.filter(m => m.side === "left").every(simple)) {
      decisions.push({account_id: r.account_id, contact_shared: false, stop_contact: false, evidence: ""});
    } else return null;
  }
  return decisions;
}

export function approvePacket(packet, {evidence_id, decisions, visual_reviewed = false} = {}) {
  if (!packet || evidence_id !== packet.evidence_id) throw new Error("review_evidence_changed");
  if (packet.reviews.some(r => !r.context_complete)) throw new Error("review_context_incomplete");
  if (packet.reviews.some(r => r.needs_visual_review) && visual_reviewed !== true) throw new Error("visual_review_required");
  if (!Array.isArray(decisions) || decisions.length !== packet.reviews.length ||
      new Set(decisions.map(d => d.account_id)).size !== decisions.length) throw new Error("business_review_incomplete");
  for (const r of packet.reviews) {
    const d = decisions.find(d => d.account_id === r.account_id);
    if (!d || typeof d.contact_shared !== "boolean" || typeof d.stop_contact !== "boolean") throw new Error("business_decision_required");
    if ((d.stop_contact || d.contact_shared) && !plain(d.evidence)) throw new Error("protective_evidence_required");
    if (d.stop_contact && !r.messages.some(m => m.side === "left" && m.text.includes(d.evidence))) {
      throw new Error("stop_contact_customer_quote_required");
    }
  }
  return {...structuredClone(packet), approved: true, visual_reviewed, decisions: structuredClone(decisions)};
}
