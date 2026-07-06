---
name: dl-xhs-winback
description: 用于小红书专业号私信通的每日客户跟进/私信触达场景。触发：小红书跟进、给客户发私信、继续昨天没发完的、处理 uncertain/failed、查跟进状态。此类操作向真实客户发送不可撤销消息，加载后必须完整遵循正文的授权门禁与逐条复核，不得凭本描述推断步骤直接行动。
---

# 小红书私信每日跟进

目标：稳定完成每日跟进。默认发现来源是小红书 `线索管理 > 用户列表` 导出 CSV，再用私信页 UID 搜索逐个复核；不要把长时间滚动私信列表当作主路径。

CSV 只负责发现和留资保护，永远不直接授权发送。真正发送前必须在浏览器里逐项走【复核清单】。

账号范围只来自用户本轮确认。CSV 导入/目标池生成必须先用 `归属账号` 与本轮确认账号范围取交集；浏览器 UID 搜索只是二次验证，不能把未确认账号扩进候选池。

## 【复核清单】

UID 复核和每条发送前都逐项核对(这是唯一权威清单，其它章节引用本清单，不再重列):

1. 右侧「小红书uid」= 目标/队列 `user_id`。
2. 当前消息 ID 能确认实际 `account_id`，且账号在本轮白名单内。
3. 无留资强信号（客户没有新增留资）。
4. 回复方向 `awaiting_customer`：最后一条真实消息来自我方，客户之后没回复。
5. 无负面：没有拒绝、投诉、辱骂、停止联系。
6. 非同行/服务商/推销方，疑似商业服务号记 `unknown`。
7. 距最后真实消息至少 24 小时。
8. `can-send` 允许。

**发送前额外确认**:输入框和发送按钮可用。

## 固定路径

以下 `$SKILL_DIR` 统一替换为你本地实际安装这个 skill 的绝对路径（例如 `~/.codex/skills/dl-xhs-winback`）：

- Skill：`$SKILL_DIR`
- 状态脚本：`$SKILL_DIR/scripts/campaign_state.py`
- 执行器：`file://$SKILL_DIR/scripts/xhs_followup_runner.mjs`
- 配置：`$SKILL_DIR/config.json`（账号 ID/昵称、工作区路径；首次使用先复制 `config.example.json` 并填入自己的账号信息）
- 工作区：`config.json` 里的 `workspace_dir`
- 看板（可选）：`<workspace_dir>/reports/xhs-followup/dashboard.html`

命令示例必须使用绝对路径。不要写成相对路径，除非当前工作目录已明确切到 skill 根目录。

## 默认协作方式

用户通常不理解 \`pending\`、\`retryable\`、\`uncertain\`、批次、活动、UID 搜索等内部概念；不要把内部状态当成问题甩给用户。用户说“继续、默认、刷新了、你看下、直接发、符合的发”等，应先按状态库判断下一步，再用一句人话说明正在做什么。

允许自动判断并继续的情况：

- 已有本轮明确文案、账号范围、模式和数据来源，且状态库显示有未完成 UID 复核或已审批 pending 队列。
- 用户刷新页面、重新打开 Chrome、询问状态或让“继续”时，优先恢复未完成任务，不重新建活动、不重新导入、不重新扫描。
- 技术定位失败、页面超时、Chrome 标签页读取异常时，先做安全诊断和恢复；不要要求用户理解错误栈。只有需要用户实际操作时，明确说“请刷新私信页/重开 Chrome/重新登录”，并说明回来后回复什么。

仍必须显式确认的情况：

- 新文案、新账号范围、首次选择 1/2/3/4、选择 1 后的 1A/1B。
- \`uncertain\` 可能已发但无法验证，必须人工核对后才能继续自动发送。
- 需要扩大账号范围、修改发送文案、跳过安全门禁或对外部状态做不可逆变更。

报告给用户时使用业务语言：已成功发送多少、还剩多少、没发原因是“客户不符合/技术定位失败/需要人工核对/页面需要刷新”，不要只说内部枚举值。

## 先问用户两件事

未完成这两次确认前，禁止打开浏览器、启动活动、导入表格、扫描、建队列或发送。

1. 如果用户还没给完整文案，只问：`这一次需要给符合条件的客户发送什么消息？`
2. 用户给出完整文案后，只确认账号范围：读取 `config.json` 里的 `accounts` 列表，向用户报出具体账号名单并问 `本轮默认只操作这 N 个账号：<名单>。要使用默认账号 ID，还是手动调整？`。`config.json` 不存在或 `accounts` 为空时，必须先问用户要操作哪些账号 ID，不能凭空假设任何默认值。

默认账号 ID/昵称来自 `config.json` 的 `accounts` 字段（格式见 `config.example.json`）。

用户回复“默认、确认、可以、没问题、就这几个”等明确同意时，使用 `config.json` 里的默认账号列表。用户手动新增、删除或替换账号 ID 时，以用户本轮确认的账号范围为准。

文案必须原样发送，不润色、不缩写、不追加。`今晚`、`明晚` 等相对时间只保留原文；除非用户明确要求设置过期时间，否则不得自动设置过期时间。

## 用户只看 4 个模式

确认文案和账号范围后，先跑无副作用计划：

```bash
python3 $SKILL_DIR/scripts/campaign_state.py operator-plan --message "<原文>" --account-ids "<逗号分隔账号ID>"
```

然后给用户展示 1/2/3/4。系统必须标注推荐项，但用户说“默认/继续/你决定”不算授权，必须追问明确选择。

1. `表格跟进`（推荐）：先让用户选择数据来源子选项：
   - `1A 最新单表`：只使用执行前刚从 `线索管理 > 用户列表` 导出的最新 CSV。
   - `1B 历史多表`：合并用户指定的多张 CSV 或文件夹内 CSV，找本轮账号范围内仍未留资的 UID。
2. `继续未完成任务`：继续当前未完成的 UID 复核、pending 队列、retryable 定位失败或中断任务。不要因为没有历史 CSV 就隐藏该模式；可用性看状态库里的未完成活动和队列。
3. `处理异常/补发`：处理 `uncertain`、`failed`、技术定位失败、用户明确要求的旧队列补发。不得自动重发 `uncertain`。
4. `只查状态`：跑 `operator-plan`、`doctor`、必要时 `preflight`，文本汇报；不要打开浏览器做副作用操作。

用户选择 1 后，必须继续追问 `1A` 或 `1B`；用户说“默认/你决定”不算子选项授权。`month`、`history`、`combined`、`fastSeek`、旧每日增量扫描都是内部兜底/兼容能力，不作为用户主模式展示。只有 CSV/UID 搜索不可用、状态不可信、或用户明确要求恢复旧扫描活动时，才按 runbook 的内部兜底章节执行。

中途换文案：先用 `update-message` 更新状态账本里的活动文案，再继续当前候选池或队列；不要只改浏览器 session 变量。

## 表格跟进

### 1A 最新单表

用户选择 `1A 最新单表` 后，要求用户上传执行前刚导出的 `线索管理 > 用户列表` CSV。最新导入的 `cutoff-date` 必须至少到昨天（Asia/Shanghai）。例如今天是 `2026-06-26`，最新表格至少应覆盖到 `2026-06-25`。

“最新”由用户本次刚导出的单张 CSV 和 `cutoff-date` 定义，不靠目录文件名或修改时间猜测。

导入表格：

```bash
python3 $SKILL_DIR/scripts/campaign_state.py import-user-list --file "<CSV路径>" --cutoff-date "YYYY-MM-DD"
python3 $SKILL_DIR/scripts/campaign_state.py historical-imports
```

导入规则：

- `用户ID` 是 UID 复核主键。
- `归属账号` 必须先匹配本轮确认账号范围。只允许 `config.json` 里配置或用户本轮明确确认的账号；其余未确认账号不得进入复核池。
- 在本轮账号范围内，`手机号`、`微信号`、`最近留资时间`、`留资方式` 任一有值，按已留资保护。
- 在本轮账号范围内无留资的 UID 只进入复核池，不等于可发送。
- `用户已注销` 或注销类型不进入目标池。
- `user_list` 活动只使用最新 import id + 本轮账号范围交集对应的 UID，避免旧 CSV/旧复核或非授权账号混进当天任务。

### 1B 历史多表

用户选择 `1B 历史多表` 后，导入用户指定的多张 CSV 或文件夹内 CSV。每张 CSV 都必须来自 `线索管理 > 用户列表`，且包含 `用户ID`、`归属账号`、留资字段和用户类型字段。

历史多表规则：

- 多张表按 UID 合并，但只在本轮确认账号范围内合并。
- 同一 UID 在本轮账号范围内任一表有 `手机号`、`微信号`、`最近留资时间`、`留资方式`，整体按已留资保护。
- 同一 UID 只在未确认账号中留资，不直接污染本轮账号范围；浏览器复核和发送前仍必须重新检查当前授权会话是否留资。
- 同一 UID 在本轮最多发送一次。
- 已注销用户不进入目标池。
- 多表导入后启动 `--coverage-scope history`，用 `historical-targets --campaign-id` 读取按本轮账号范围过滤后的目标池。

启动活动：

```bash
python3 $SKILL_DIR/scripts/campaign_state.py start --message "<原文>" --activate --account-ids "<逗号分隔账号ID>" --coverage-scope <user_list|history>
python3 $SKILL_DIR/scripts/campaign_state.py preflight --campaign-id <活动ID>
python3 $SKILL_DIR/scripts/campaign_state.py historical-targets --campaign-id <活动ID>
```

`1A 最新单表` 使用 `--coverage-scope user_list`；`1B 历史多表` 使用 `--coverage-scope history`。

浏览器 UID 复核：

```js
await xhsScan.reviewHistoricalTargets({
  maxTargets: 30,
  maxMs: 180000,
  markWindowWhenComplete: true,
});
```

复核必须按 UID 搜索打开私信会话，逐项走【复核清单】第 1–8 项。返回 `chunk_limit` 就继续同一命令；返回 `completed` 后才允许建队列。

## 运行前读取

执行任何浏览器 UID 复核、扫描或发送前，必须读取：

- `references/execution-runbook.md`：完整执行命令、JS 会话代码、恢复模式和人工处理。
- `references/browser-contract.md`：页面结构、UID 搜索、消息身份和发送验证约定。
- `references/eligibility-rules.md`：留资、同行、回复方向、负面、24 小时和人工覆盖规则。

只做状态诊断或解释时，优先读取 `references/execution-runbook.md`；调试页面定位或筛选误判时，再读取另外两个 reference。不要临场重写 DOM 循环；默认使用内置执行器。

## 硬门禁

- 不依赖小红书后台账号筛选；后台筛选会丢消息。账号范围只由本轮确认的账号 ID 白名单决定。
- CSV 候选池必须先按 `归属账号` 映射到本轮确认账号范围；浏览器 UID 搜索不能扩大账号范围。
- 最新表格导入不算复核完成。只有 UID 逐个复核并完成 `mark-historical-window`，才允许建队列或发送。
- CSV 没有留资字段不等于 `no_lead` 已经可发送；必须浏览器复核。
- 队列只来自状态脚本；不要手写候选名单直接发。
- 每条发送前必须逐项走【复核清单】(含发送前额外确认)。
- 当前发送文案必须从状态账本读取，并为每次 `sent`、`failed`、`uncertain` 记录保存文案快照。
- `uncertain` 表示可能已发但未验证。不得自动重发；同批次存在 `uncertain` 时必须先人工处理，状态脚本会阻塞继续取队列或发送。
- 技术定位失败记为 retryable，保持 pending；业务不符合才记为 skipped。
- 同 UID 多账号结果允许发送到任一白名单账号会话，但必须重新验证当前实际账号并记录实际发送账号。
- 内部扫描兜底下，`chunk_limit`、超时、滚动暂停、`fastSeek` 到达日期都不表示扫描完成。

## 状态命令速查

```bash
python3 $SKILL_DIR/scripts/campaign_state.py operator-plan --message "<原文>" --account-ids "<逗号分隔账号ID>"
python3 $SKILL_DIR/scripts/campaign_state.py doctor
python3 $SKILL_DIR/scripts/campaign_state.py latest
python3 $SKILL_DIR/scripts/campaign_state.py preflight --campaign-id <活动ID>
python3 $SKILL_DIR/scripts/campaign_state.py import-user-list --file "<CSV路径>" --cutoff-date "YYYY-MM-DD"
python3 $SKILL_DIR/scripts/campaign_state.py historical-imports
python3 $SKILL_DIR/scripts/campaign_state.py historical-targets --campaign-id <活动ID>
python3 $SKILL_DIR/scripts/campaign_state.py mark-historical-review --campaign-id <活动ID> --user-id <UID> --state <eligible|ineligible|retryable|unknown|skipped>
python3 $SKILL_DIR/scripts/campaign_state.py mark-historical-window --campaign-id <活动ID>
python3 $SKILL_DIR/scripts/campaign_state.py mark-current-month-window --campaign-id <活动ID> --window-start-date YYYY-MM-DD --visible-oldest-date YYYY-MM-DD
python3 $SKILL_DIR/scripts/campaign_state.py candidate-keys --campaign-id <活动ID>
```

`summary`、`candidate-keys` 和 `historical-targets` 需要 `--campaign-id`；`batch-summary` 和 `batch-queue` 需要 `--batch-id`。出现 `current_user_list_import_required`、`historical_review_window_not_completed`、`batch_has_uncertain_requires_manual_review`、`uncertain`、`failed`、路径/import 错误时，先把精确 blocker 告诉用户，再处理。

## 浏览器执行入口

在已登录 Chrome 的 `/im/multiCustomerService` 页面上使用执行器。必须通过 `chrome:control-chrome` 的 browser-client / `mcp__node_repl__js` 认领标签页后执行，不要打开 Chrome DevTools，不要在页面 Console 里粘贴 import 代码，不要用 Computer Use 往控制台输入执行器代码。

正确入口是：先按 Chrome skill bootstrap 连接浏览器、读取 browser documentation、`browser.nameSession(...)`、`browser.user.openTabs()`、`browser.user.claimTab(...)` 得到 `tab`，然后在该 JS 会话里导入执行器：

```js
const runnerUrl = "file://$SKILL_DIR/scripts/xhs_followup_runner.mjs";
const { createXhsFollowupSession } = await import(runnerUrl);
// accountIds 用本轮确认的账号 ID 列表（来自 config.json 的 accounts 字段，或用户手动指定）
const accountIds = [
  "<账号ID1>",
  "<账号ID2>",
];
```

不要给普通绝对路径追加 `?v=`。如果刚改过执行器且同一 JS 内核缓存了旧模块，重置内核，或只在 `file://` URL 上使用显式缓存参数。

## 完成报告

只报告：本轮计划、表格数据来源与导入情况、本轮账号范围过滤后的 UID 复核总数/完成数/可重试数、符合发送人数、成功发送数、跳过数及主要原因、可重试技术异常数及主要原因、uncertain 人数、失败数及主要原因、停止原因、下一步建议。只有实际导出过看板/CSV/Markdown 时才报告路径。

不要复制手机号、微信号或完整私信原文。
