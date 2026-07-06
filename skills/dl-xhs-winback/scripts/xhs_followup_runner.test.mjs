import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  bottomCompletionSignature,
  extractConversationDate,
  buildScanProgress,
  currentMonthWindowReached,
  deadlineExceeded,
  normalizeQueueItem,
  resolvePopupCoordinateClickTarget,
  runWithTimeout,
  resolveUidFailureFallback,
  resolveBypassScanGate,
  resolveScanScrollPages,
} from "./xhs_followup_runner.mjs";

test("DEFAULT_ACCOUNT_IDS authorizes every account id listed under one config entry's name, even when the same person has two ids on record (legacy + current)", () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "xhs-config-"));
  const configPath = path.join(tmpDir, "config.json");
  fs.writeFileSync(
    configPath,
    JSON.stringify({
      accounts: [
        { id: "cccc3333000000000d000003", name: "Carol-legacy" },
        { id: "dddd4444000000000d000004", name: "Carol" },
      ],
    }),
  );

  const output = execFileSync(
    process.execPath,
    [
      "--input-type=module",
      "-e",
      "const { DEFAULT_ACCOUNT_IDS } = await import(process.argv[1]); console.log(JSON.stringify(DEFAULT_ACCOUNT_IDS));",
      new URL("./xhs_followup_runner.mjs", import.meta.url).href,
    ],
    { env: { ...process.env, XHS_FOLLOWUP_CONFIG: configPath }, encoding: "utf8" },
  );
  const defaultAccountIds = JSON.parse(output);

  fs.rmSync(tmpDir, { recursive: true, force: true });

  assert.equal(defaultAccountIds.includes("cccc3333000000000d000003"), true);
  assert.equal(defaultAccountIds.includes("dddd4444000000000d000004"), true);
});

test("extractConversationDate reads Xiaohongshu visible date labels", () => {
  assert.equal(extractConversationDate("张三\n6月25日\n你好", { now: new Date("2026-06-25T08:00:00+08:00") }), "2026-06-25");
  assert.equal(extractConversationDate("李四\n05-31\n已咨询", { now: new Date("2026-06-25T08:00:00+08:00") }), "2026-05-31");
  assert.equal(extractConversationDate("王五\n昨天\n消息", { now: new Date("2026-06-25T08:00:00+08:00") }), "2026-06-24");
  assert.equal(extractConversationDate("赵六\n刚刚\n消息", { now: new Date("2026-06-25T08:00:00+08:00") }), "2026-06-25");
});

test("bottomCompletionSignature changes when virtual list reveals older rows", () => {
  const now = new Date("2026-06-25T08:00:00+08:00");
  const first = bottomCompletionSignature({
    items: [
      { key: "a", text: "A\n05-31\nhello" },
      { key: "b", text: "B\n03-30\nhello" },
    ],
    scroller: {
      scrollTop: 1000,
      scrollHeight: 1500,
      clientHeight: 500,
      atBottom: true,
    },
  }, { now });
  const second = bottomCompletionSignature({
    items: [
      { key: "b", text: "B\n03-30\nhello" },
      { key: "c", text: "C\n01-12\nhello" },
    ],
    scroller: {
      scrollTop: 1000,
      scrollHeight: 1500,
      clientHeight: 500,
      atBottom: true,
    },
  }, { now });

  assert.notEqual(first, second);
});

test("resolveScanScrollPages keeps strict full scan safe and makes seek mode fast", () => {
  assert.equal(resolveScanScrollPages({ scanMode: "strict" }), 0.82);
  assert.equal(resolveScanScrollPages({ scanMode: "fastSeek" }), 6);
  assert.equal(resolveScanScrollPages({ scanMode: "fastSeek", scrollPages: 4 }), 4);
  assert.equal(resolveScanScrollPages({ scanMode: "fastSeek", scrollPages: 99 }), 10);
});

test("resolveBypassScanGate carries incremental bypass through send revalidation", () => {
  assert.equal(resolveBypassScanGate({ allowIncompleteScan: true, resumeOldQueue: false }), true);
  assert.equal(resolveBypassScanGate({ allowIncompleteScan: false, resumeOldQueue: true }), true);
  assert.equal(resolveBypassScanGate({ allowIncompleteScan: false, resumeOldQueue: false }), false);
});

test("buildScanProgress exposes date and scroll visibility", () => {
  const progress = buildScanProgress({
    view: {
      items: [
        { text: "A\n6月25日\nhello" },
        { text: "B\n6月20日\nhello" },
        { text: "C\n05-31\nhello" },
      ],
      scroller: {
        scrollTop: 500,
        scrollHeight: 2000,
        clientHeight: 500,
        atBottom: false,
      },
    },
    seenConversationKeys: 123,
    loadedResumeSeenKeys: 100,
    scanMode: "fastSeek",
    now: new Date("2026-06-25T08:00:00+08:00"),
  });

  assert.deepEqual(progress, {
    scanMode: "fastSeek",
    visibleNewestDate: "2026-06-25",
    visibleOldestDate: "2026-05-31",
    scrollTop: 500,
    scrollHeight: 2000,
    clientHeight: 500,
    distanceToBottom: 1000,
    scrollPercent: 33.33,
    atBottom: false,
    seenConversationKeys: 123,
    loadedResumeSeenKeys: 100,
  });
});

test("currentMonthWindowReached requires visible oldest date on or before window start", () => {
  assert.equal(currentMonthWindowReached({
    scanProgress: { visibleOldestDate: "2026-06-01" },
    windowStartDate: "2026-06-01",
  }), true);
  assert.equal(currentMonthWindowReached({
    scanProgress: { visibleOldestDate: "2026-05-31" },
    windowStartDate: "2026-06-01",
  }), true);
  assert.equal(currentMonthWindowReached({
    scanProgress: { visibleOldestDate: "2026-06-02" },
    windowStartDate: "2026-06-01",
  }), false);
  assert.equal(currentMonthWindowReached({
    scanProgress: { visibleOldestDate: "" },
    windowStartDate: "2026-06-01",
  }), false);
});

test("normalizeQueueItem accepts historical UID targets without account ids", () => {
  const item = normalizeQueueItem({
    user_id: "uhistory",
    display_name: "历史客户",
  });

  assert.deepEqual(item, {
    key: "",
    conversation_key: "",
    user_id: "uhistory",
    account_id: "",
    display_name: "历史客户",
    position: 0,
    retryable_count: 0,
  });
});

test("resolveUidFailureFallback skips long list fallback in short operator windows", () => {
  assert.deepEqual(resolveUidFailureFallback({
    skipListFallbackOnUidFail: true,
    maxMs: 25000,
    elapsedMs: 12000,
  }), {
    useListFallback: false,
    reason: "skipped_by_operator",
    listFallbackMs: 0,
  });

  assert.deepEqual(resolveUidFailureFallback({
    skipListFallbackOnUidFail: false,
    maxMs: 180000,
    elapsedMs: 1000,
  }), {
    useListFallback: true,
    reason: "",
    listFallbackMs: 60000,
  });
});

test("runWithTimeout returns fallback before a hung operation resolves", async () => {
  const started = Date.now();
  const result = await runWithTimeout(
    new Promise((resolve) => setTimeout(() => resolve({ ok: true }), 200)),
    20,
    () => ({ ok: false, reason: "operation_timeout" })
  );

  assert.deepEqual(result, { ok: false, reason: "operation_timeout" });
  assert.ok(Date.now() - started < 150);
});

test("deadlineExceeded reserves enough time before starting another browser action", () => {
  assert.equal(deadlineExceeded(10_000, 500, 9_400), false);
  assert.equal(deadlineExceeded(10_000, 500, 9_500), true);
  assert.equal(deadlineExceeded(10_000, 500, 10_001), true);
  assert.equal(deadlineExceeded(0, 500, 10_001), false);
});

test("resolvePopupCoordinateClickTarget uses the scrolled candidate center instead of a stale top-row nth", () => {
  const result = resolvePopupCoordinateClickTarget({
    desiredIndex: 4,
    entries: [
      { allIndex: 0, scrollTop: 0, rect: { left: 10, top: 100, right: 210, bottom: 140 }, text: "row 0" },
      { allIndex: 1, scrollTop: 0, rect: { left: 10, top: 150, right: 210, bottom: 190 }, text: "row 1" },
      { allIndex: 2, scrollTop: 0, rect: { left: 10, top: 200, right: 210, bottom: 240 }, text: "row 2" },
      { allIndex: 3, scrollTop: 180, rect: { left: 10, top: 110, right: 210, bottom: 150 }, text: "row 3" },
      { allIndex: 4, scrollTop: 180, rect: { left: 10, top: 160, right: 210, bottom: 200 }, text: "row 4" },
      { allIndex: 5, scrollTop: 180, rect: { left: 10, top: 210, right: 210, bottom: 250 }, text: "row 5" },
    ],
    refreshedEntries: [
      { allIndex: 3, scrollTop: 180, rect: { left: 12, top: 112, right: 212, bottom: 152 }, text: "row 3" },
      { allIndex: 4, scrollTop: 180, rect: { left: 12, top: 162, right: 212, bottom: 202 }, text: "row 4" },
      { allIndex: 5, scrollTop: 180, rect: { left: 12, top: 212, right: 212, bottom: 252 }, text: "row 5" },
    ],
  });

  assert.deepEqual(result, {
    ok: true,
    allIndex: 4,
    scrollTop: 180,
    x: 112,
    y: 182,
    text: "row 4",
  });
});
