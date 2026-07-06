# 执行 Runbook

本文件用于实际执行小红书私信每日跟进。默认路径是：表格 CSV 导入 -> 按本轮账号范围过滤 `归属账号` -> UID 搜索复核 -> 建队列 -> 发送。运行命令时默认当前目录不可靠，全部使用绝对路径。

## 常量

`$SKILL_DIR` 替换为你本地实际安装这个 skill 的绝对路径；`workspace_dir` 取自 `$SKILL_DIR/config.json`（首次使用先复制 `config.example.json` 并填入自己的账号信息和工作区路径）。

```bash
SKILL_DIR="<替换为本地 skill 绝对路径>"
STATE_SCRIPT="$SKILL_DIR/scripts/campaign_state.py"
REPORT_DIR="<config.json 里的 workspace_dir>/reports/xhs-followup"
DASHBOARD="<config.json 里的 workspace_dir>/reports/xhs-followup/dashboard.html"
```

默认账号来自 `config.json` 的 `accounts` 字段：

```js
// accountIds 来自 config.json 的 accounts 字段，或用户本轮明确确认的账号 ID 列表
const accountIds = [
  "<账号ID1>",
  "<账号ID2>",
];
```

## 0. 授权与计划

先确认完整文案和账号范围。没有确认前，不导入表格、不打开浏览器、不创建活动、不建队列、不发送。

确认后先生成无副作用计划：

```bash
python3 "$STATE_SCRIPT" operator-plan --message "<原文>" --account-ids "<逗号分隔账号ID>"
```

`operator-plan` 会返回：

- `recommended_mode=latest_user_list_followup`：默认推荐表格跟进。
- `coverage_modes`：给用户看的 4 个模式。
- `requires_manual_mode_choice=true`：必须等用户明确选 1/2/3/4。
- `would_reuse_campaign_id`：默认按 `user_list` scope 预览可复用活动。
- `would_reuse_campaigns_by_scope`：内部 scope 复用预览。
- `current_user_list_import`：最新 CSV 是否足够新。
- `would_stop_active_campaigns`：如果之后执行 `start` 会停止的其他活动预览。

推荐只用于提示用户；用户回复“默认/继续/你决定”不算选择，必须追问明确选择 1/2/3/4。用户选 1 后，还必须追问 `1A 最新单表` 或 `1B 历史多表`。

可以同时查现有状态，但仍然不能做副作用操作：

```bash
python3 "$STATE_SCRIPT" doctor
python3 "$STATE_SCRIPT" latest
```

## 1. 用户模式

向用户只展示这 4 个模式：

1. `表格跟进`（推荐）：继续让用户选择 `1A 最新单表` 或 `1B 历史多表`，再用 UID 搜索逐个复核、建队列发送。
2. `继续未完成任务`：继续已有 UID 复核、pending 队列、retryable 定位失败或中断任务。
3. `处理异常/补发`：处理 `uncertain`、`failed`、技术定位失败、用户明确要求的旧队列补发。
4. `只查状态`：不打开浏览器、不发送。

不要把 `month/history/combined/fastSeek/每日增量` 展示成用户模式。它们只在内部兜底章节使用。

## 2. 表格准备

用户选 `表格跟进` 后，继续确认数据来源：

- `1A 最新单表`：上传小红书专业号后台 `线索管理 > 用户列表` 执行前刚导出的最新 CSV。最新导入的 `cutoff-date` 必须至少到昨天（Asia/Shanghai，示例见 SKILL.md「1A 最新单表」）。如果用户只给到上个月月底，不能跑 `1A`，只能说明需要再导出当前月截至昨天的表格。
- `1B 历史多表`：导入用户指定的多张 CSV 或文件夹内 CSV，不要求最新 cutoff，但必须来自同一类用户列表导出。

导入：

```bash
python3 "$STATE_SCRIPT" import-user-list --file "<CSV路径>" --cutoff-date "YYYY-MM-DD"
python3 "$STATE_SCRIPT" historical-imports
```

导入规则：

- `用户ID` 是 UID 复核主键。
- 必须先按本轮确认账号范围过滤 `归属账号`(默认账号 ID 与昵称见 SKILL.md「先问用户两件事」，以本轮确认为准)；未确认账号不得进入目标池。
- 在本轮账号范围内，`手机号`、`微信号`、`最近留资时间`、`留资方式` 任一有值，按已留资保护。
- 在本轮账号范围内无留资 UID 只进入 UID 复核池，不等于可发送。
- `用户已注销` 或注销类型不进入目标池。
- `1A` 活动使用 `--coverage-scope user_list`，只以最新 import id + 本轮账号范围交集作为目标窗口，避免旧表格/旧复核混进当天任务。
- `1B` 可以重复导入多张 CSV；同一 UID 只在本轮账号范围内合并，任一授权账号行已留资就保护。
- `1B` 活动使用 `--coverage-scope history`，不要求最新 cutoff，但仍要求 UID 复核窗口完成。

## 3. 开始表格活动

只有用户明确选择 `表格跟进` 且确认 `1A/1B` 后，才启动或复用活动：

```bash
python3 "$STATE_SCRIPT" start --message "<原文>" --activate --account-ids "<逗号分隔账号ID>" --coverage-scope <user_list|history>
python3 "$STATE_SCRIPT" preflight --campaign-id <活动ID>
python3 "$STATE_SCRIPT" historical-targets --campaign-id <活动ID>
```

`1A 最新单表` 使用 `--coverage-scope user_list`；`1B 历史多表` 使用 `--coverage-scope history`。`start` 的默认 scope 已是 `user_list`，但热路径仍显式写 scope，防止误读。`start` 会自动处理：

- 同文案 + 同账号范围 + 同 scope：复用已有活动，并重置覆盖状态。
- 不同 scope 不混用。
- 不同文案旧活动：自动停止，避免新旧任务混在一起。
- 同文案 + 不同账号范围：不默认停止，避免误伤用户有意分开的账号任务。
- 同一用户 24 小时内成功发送、以及未人工处理的 `uncertain`，由状态脚本统一保护。

如果 `1A` 的 `preflight/summary/prepare-batch` 返回 `current_user_list_import_required`，说明没有导入足够新的 CSV，回到第 2 步。不要改跑扫描来绕过。`1B` 如果返回 `historical_import_required`，说明尚未导入历史表。

## 4. 浏览器会话

在已登录 Chrome 的 `/im/multiCustomerService` 页面，用内置执行器。

硬性入口：

- 必须使用 `chrome:control-chrome` 的 browser-client，在 `mcp__node_repl__js` 里连接 Chrome 并认领现有私信页。
- 禁止打开 Chrome DevTools Console 来粘贴 `import("file://...")`。
- 禁止用 Computer Use 操作 DevTools 控制台。Computer Use 只可作为最后的视觉定位辅助。

在已经通过 browser-client 得到 `tab` 后执行：

```js
const runnerUrl = "file://$SKILL_DIR/scripts/xhs_followup_runner.mjs";
const { createXhsFollowupSession } = await import(runnerUrl);

globalThis.xhsScan = createXhsFollowupSession({
  tab,
  campaignId,
  message,
  accountIds,
  scanOnly: true,
  dashboardMode: "off",
});
```

如果刚改过执行器且同一 JS 内核缓存了旧模块，重置内核，或只在 `file://` URL 上使用显式缓存参数。不要给普通绝对路径追加 `?v=`。

## 5. UID 复核

表格跟进的核心动作是 UID 复核，不是滚动扫描。

```js
await xhsScan.reviewHistoricalTargets({
  maxTargets: 30,
  maxMs: 180000,
  markWindowWhenComplete: true,
});
```

每个 UID 必须通过搜索打开会话，逐项走 SKILL.md 的【复核清单】第 1–8 项。

复核结果写入 `mark-historical-review`。技术定位失败写成 `retryable`，下次继续；不要当成业务跳过。

所有目标 UID 进入终态后，执行器会调用 `mark-historical-window`。没有窗口完成标记时，状态脚本会用 `historical_review_window_not_completed` 阻止队列。

返回 `chunk_limit` 就继续同一命令。页面刷新或 Chrome 断开后，重新连接并继续 `reviewHistoricalTargets()`；不要改成长历史滚动。

## 6. 建队列

UID 复核窗口完成后，生成并批准队列：

```js
const batch = await xhsScan.prepareAndApproveBatch({
  limit: 100,
});
await xhsScan.preflightStatus(batch.batchId);
```

也可以用状态命令检查：

```bash
python3 "$STATE_SCRIPT" summary --campaign-id <活动ID>
python3 "$STATE_SCRIPT" prepare-batch --campaign-id <活动ID>
python3 "$STATE_SCRIPT" approve-batch --batch-id <批次ID>
```

队列只包含：

- 在本轮账号 ID 白名单内；
- 未留资；
- 非同行/非服务商/非推销方；
- 最后一条真实消息来自我方，客户之后没回复；
- 没有拒绝、投诉、辱骂、停止联系等负面信号；
- 距最后真实消息至少 24 小时；
- 本活动和最近 24 小时全局去重允许；
- 该用户没有未人工处理的 `uncertain`；
- 属于本轮账号范围过滤后的无留资 UID；`1A` 还必须属于最新 import id，`1B` 来自历史多表合并池。

默认不导出看板/报告。用户明确要求或人工审查需要时再导出：

```bash
python3 "$STATE_SCRIPT" export-batch-report --batch-id <批次ID> --output-dir "$REPORT_DIR"
python3 "$STATE_SCRIPT" export-dashboard --batch-id <批次ID> --output "$DASHBOARD"
```

## 7. 发送队列

发送前先体检：

```js
await xhsScan.preflightStatus(batch.batchId);
```

然后按队列发送：

```js
await xhsScan.sendBatch({
  batchId: batch.batchId,
  maxSends: 20,
  maxMs: 180000,
  uidSearchFirst: true,
  dashboardRefresh: false,
});
```

在 Codex/Chrome 工具单次调用容易超时的环境中，使用小分片发送，避免一个难定位 UID 卡住全局：

```js
await xhsScan.sendBatch({
  batchId: batch.batchId,
  maxSends: 4,
  maxMs: 25000,
  uidSearchFirst: true,
  skipListFallbackOnUidFail: true,
  dashboardRefresh: false,
});
```

分片返回 `chunk_limit`、工具超时或 Chrome 会话重置后，不要猜测结果；立即运行 `batch-summary --batch-id` 和 `preflight --campaign-id`，以状态库为准继续下一片。若 pending 数减少，说明已写入成功；若出现 `uncertain`，停止自动发送并进入人工核对；若 pending 不变且 Chrome 标签页读取超时，按「浏览器恢复」处理。

发送前必须逐项走 SKILL.md 的【复核清单】(含发送前额外确认输入框和发送按钮可用)。

定位顺序：

1. 默认使用队列里保存的目标客户 `user_id` 搜索。
2. 输入 UID 后如果出现搜索结果浮层 `.search-contact-item`，优先点击可见浮层结果打开会话，不先按 Enter，不先滚动左侧列表。
3. 同 UID 多个浮层结果时，逐个点击并复核；失败后点消息头/空白处退出结果状态，再全选、删除、逐字重新输入同一 UID，打开下一项。单个 UID 最多尝试 20 个浮层结果。
4. 只有浮层不存在、浮层全部点完仍不匹配，或浮层点击不可用时，才读取左侧真实会话列表 `.sx-contact-item[data-key]` 兜底。
5. 打开后必须复核右侧 UID 和消息 ID 里的实际账号。本轮白名单内任一账号都可发；队列来源账号不是发送硬锁。
6. 禁止用当前右侧面板显示的“小红书uid”当搜索输入；那可能是上一位客户。

技术定位问题只记为 `retryable` 并保持 pending，例如搜索无结果、身份暂时无法确认、点击失败。业务不符合才记为 `skipped`。

`skipListFallbackOnUidFail: true` 只允许在短时限分片发送中使用：UID 搜索无法打开目标时，快速记录技术定位失败，继续处理后面的队列项。它不得跳过发送前复核；它只跳过耗时的左侧列表兜底，避免单个 UID 阻塞整批。

浏览器里的 UID 搜索、点击、输入和发送验证都是会改变页面状态的动作，不能用 `Promise.race` 这类硬超时包装后继续跑下一位；硬超时不会取消底层页面动作，可能造成两个定位流程重叠。短窗口发送必须使用执行器内置的协作式截止时间：每个阶段开始前检查剩余时间，不够就干净返回技术定位失败或暂停；不要在外层临场再套一层不可取消超时。

每处理一个队列项后必须写入状态库；不默认刷新看板或 CSV/Markdown。用户明确要求留档时，暂停后导出一次即可。

## 8. 发送后验证

只有同时看到以下证据，才记为 `sent`：

- 新增右侧消息气泡；
- 内容与用户文案一致；页面自动插入空白/换行时允许压缩空白后一致；
- 消息属于当前 `user_id + account_id`；
- 消息 ID 是发送前不存在的新 ID；
- 时间戳晚于发送前该会话最新消息。

小红书有时会先生成不带 `user_id/account_id` 的临时消息 ID，例如 `jarvis-msg-{13位时间戳}-{随机数}`。这类 ID 只能在发送前后身份都确认、消息确实新增、时间戳更新、文案一致时作为发送成功证据；不得用于扫描分类或身份推断。

如果点击发送后无法验证新增右侧消息，记为 `uncertain`，停止当前发送动作并汇报。不得自动重发这个用户。

## 9. 继续未完成任务

用户选择 `继续未完成任务`，或说“继续、刷新了、你看下、直接把符合发就好”等恢复型指令时，先只查状态：

```bash
python3 "$STATE_SCRIPT" latest
python3 "$STATE_SCRIPT" doctor
python3 "$STATE_SCRIPT" preflight --campaign-id <活动ID>
```

按状态继续：

- `current_user_list_import_required`：先导入最新 CSV。
- `historical_review_window_not_completed`：继续 `reviewHistoricalTargets()`。
- `has_pending_batch`：体检后继续 `sendBatch()`。
- `needs_uncertain_review`：先人工处理 `uncertain`。
- `needs_failed_review`：先人工处理 failed。
- `ready_to_prepare_batch`：建下一批队列。

不要因为页面刷新就重新扫描；优先按状态库恢复 UID 复核或队列。

### 浏览器恢复

如果 Chrome 控制出现超时、标签页读取失败、`openTabs()` / `claimTab()` 卡住：

1. 先查状态库，确认是否已有新增 sent、uncertain、failed 或 pending 变化。
2. 按 Chrome skill 的 troubleshooting 读取故障文档并做一次轻量重试。
3. 若仍失败，检查 Chrome 正在运行、扩展已启用、native host 正常。
4. 检查都正常但仍拿不到 tab 时，让用户只做一件事：刷新小红书私信页；刷新无效再让用户重开 Chrome 并重新打开私信页。
5. 用户回复“好了/刷新了”后，重新从状态库恢复，不重建活动、不重建队列。

给用户的表达必须是人话，例如“页面控制通道卡住了，请刷新私信页，回来回复我好了”，不要输出内部错误栈。

## 10. 处理异常/补发

查看不确定记录：

```bash
python3 "$STATE_SCRIPT" uncertain --campaign-id <活动ID>
```

人工核对 `uncertain` 后，只能三选一：

```bash
python3 "$STATE_SCRIPT" resolve-uncertain --campaign-id <活动ID> --user-id <用户ID> --resolution confirmed_sent
python3 "$STATE_SCRIPT" resolve-uncertain --campaign-id <活动ID> --user-id <用户ID> --resolution confirmed_not_sent
python3 "$STATE_SCRIPT" resolve-uncertain --campaign-id <活动ID> --user-id <用户ID> --resolution skip
```

- `confirmed_sent`：人工确认已经发出，保持保护，不再发。
- `confirmed_not_sent`：人工确认没发出，把该用户放回 pending。
- `skip`：人工决定不处理该用户。

发送失败后，只能二选一：

```bash
python3 "$STATE_SCRIPT" resolve-failed --campaign-id <活动ID> --user-id <用户ID> --resolution retry
python3 "$STATE_SCRIPT" resolve-failed --campaign-id <活动ID> --user-id <用户ID> --resolution skip
```

- `retry`：把失败的 batch item 放回 pending，重新走发送前复核。
- `skip`：把失败的 batch item 记为跳过，不再进入队列。

只有用户明确要求“恢复旧队列”“继续上次没发完的 pending”“不用重新复核直接继续旧 batch”时，才进入旧队列恢复模式。恢复模式不重新建活动、不重建队列，但每条发送前仍重新复核身份、账号、留资、客户回复、负面和 can-send。

旧版执行器曾把技术定位问题记为 skipped。只有恢复旧队列时，才可运行：

```bash
python3 "$STATE_SCRIPT" restore-technical-skips --batch-id <原批次ID>
```

不得自动清空 `uncertain` 或 `failed`。

## 11. 只查状态

用户选择 `只查状态` 时，只运行：

```bash
python3 "$STATE_SCRIPT" operator-plan --message "<原文>" --account-ids "<逗号分隔账号ID>"
python3 "$STATE_SCRIPT" doctor
python3 "$STATE_SCRIPT" latest
python3 "$STATE_SCRIPT" preflight --campaign-id <活动ID>
```

不要打开浏览器，不导入表格，不启动活动，不扫描，不发送。

## 12. 内部扫描兜底

只有 CSV/UID 搜索不可用、状态不可信、换账号后需要完整覆盖、或用户明确要求恢复旧扫描活动时，才使用内部扫描兜底。

内部 scope：

```bash
python3 "$STATE_SCRIPT" start --message "<原文>" --activate --account-ids "<逗号分隔账号ID>" --coverage-scope month
python3 "$STATE_SCRIPT" start --message "<原文>" --activate --account-ids "<逗号分隔账号ID>" --coverage-scope history
python3 "$STATE_SCRIPT" start --message "<原文>" --activate --account-ids "<逗号分隔账号ID>" --coverage-scope combined
```

本月扫描兜底：

```js
globalThis.xhsScan = createXhsFollowupSession({
  tab,
  campaignId,
  message,
  accountIds,
  scanOnly: true,
  dashboardMode: "off",
});
await xhsScan.runChunk({
  maxConversations: 80,
  maxSends: 1,
  maxMs: 180000,
  currentMonthWindowStartDate: "YYYY-MM-01",
});
```

如果严格扫描已经覆盖到本月窗口边界，执行器会调用 `mark-current-month-window`，状态脚本允许建队列，即使没有滚到整个历史列表底部。不要为了追求全站 bottom 一直追滚动列表。

`fastSeek` 只移动列表并读取可见日期，不写候选、不建队列、不发送：

```js
await xhsScan.runChunk({
  scanMode: "fastSeek",
  targetDate: "2026-05-31",
  scrollPages: 6,
  maxConversations: 1,
  maxSends: 1,
  maxMs: 60000,
});
```

`target_date_reached`、`fast_seek_bottom_reached`、`partial_scan_window_completed`、`chunk_limit` 都不是发送许可。扫描兜底仍必须满足状态脚本 coverage 门禁。

旧每日增量扫描只作为内部优化保留。只有状态脚本确认 48 小时内同账号范围的全量基线，并完成 `incremental-plan -> seed-incremental-candidates -> 窗口严格扫描 -> mark-incremental-window` 后，才可显式传 `allowIncompleteScan:true` 建队列和发送。

## 13. 第二天继续

第二天仍然先确认文案和账号范围，然后运行无副作用 `operator-plan`，展示 4 个用户模式并等待明确选择。

日常默认选择 `表格跟进 -> 1A 最新单表`：让用户重新导出截至昨天的用户列表 CSV，导入后跑 UID 复核。用户明确要求跑全部历史时，选择 `1B 历史多表`。不要默认滚动私信列表。

如果文案和账号范围不变，`start --coverage-scope user_list` 可能复用同一个活动。这样：

- 已发送的人不会重复发；
- `uncertain` 的人继续被保护；
- pending 的人仍在同一张账上；
- 新 CSV 和新 UID 复核结果会约束当天队列。

不要因为存在旧 open batch 就跳过今天的模式选择。只有用户明确要求恢复某个旧队列，才按旧队列恢复处理。

## 14. 看板和报告

看板和报告是可选留档，不是默认执行反馈。用户明确要求、人工审查需要、或最终交付需要文件时再导出：

```bash
python3 "$STATE_SCRIPT" export-dashboard --campaign-id <活动ID> --output "$DASHBOARD"
python3 "$STATE_SCRIPT" export-batch-report --batch-id <批次ID> --output-dir "$REPORT_DIR"
```

看板刷新失败只记录到返回值，不得改变发送状态或触发重发。报告里不要复制手机号、微信号或完整私信原文。
