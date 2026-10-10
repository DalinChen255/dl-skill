# 跟进执行手册

本手册使用当前使用者明确指定的独立 runtime 和账本。名单、账号范围、原文话术明确且使用者授权启动后才发送；补标需要独立授权。只读检查、讨论、安装、修改 Skill 或复盘不授权发送，也不恢复已暂停的批次。本地测试使用临时数据库，不向真实客户发送测试消息。

下面 Shell 示例从使用者选定的项目根目录运行。`SKILL_DIR` 是实际安装目录，`PRIVATE_DIR` 是该项目专属 runtime；若安装位置不同先修改它们。每次 Shell 会话都重新设置并检查，不能依赖其他会话的变量。

## 1. 导入和登记

接受用户给出的一个或多个历史月份 CSV，重复 UID 合并，任意表内留资信号全局保护。`all` 不依赖 config，不做账号枚举或筛选；`specified` 必须配置实际管理账号和导出名称映射。以下先展示 `all` 路径。

```bash
umask 077
SKILL_DIR="$PWD/.agents/skills/dl-xhs-winback"
PRIVATE_DIR="$PWD/.xhs-winback-private"
mkdir -p "$PRIVATE_DIR"
chmod 700 "$PRIVATE_DIR"
python3 "$SKILL_DIR/scripts/daily_import.py" --db "$PRIVATE_DIR/state.sqlite3" --file "$PRIVATE_DIR/客户导出.csv" > "$PRIVATE_DIR/import-result.json"
```

先把当前使用者的真实导出文件放入 runtime；示例中的文件名须与实际一致。需要多张表时重复 `--file`。所有 `daily_*` 命令都显式指定 `--db`，其中 `daily_followup_state.py` 的全局 `--db` 必须位于子命令之前。切勿使用脚本的历史默认目录或全局安装目录旁的共享 `private/`。CSV 格式见 [README](../README.md)。

把导入结果返回的所有 `source_import_ids` 放进运行输入，确保每张表都纳入。cutoff-date 可保留真实导出截止日期，仅作来源信息；不拿它否定用户指定的历史范围。在 `$PRIVATE_DIR/run-input.json` 保存当前使用者批准的输入。以下仅展示结构，不能原样用于发送：

```json
{
  "scope_mode": "all",
  "account_ids": [],
  "source_import_ids": ["本次导入返回的每一个ID"],
  "messages": {"ad": "用户广告原文", "natural": "用户自然流原文"}
}
```

相同原文自动生成 unknown 来源版本；来源不明也可发送但不假装自然流。不同原文才需要来源明确。

```bash
python3 "$SKILL_DIR/scripts/daily_followup_state.py" --db "$PRIVATE_DIR/state.sqlite3" register-run --input-json "$PRIVATE_DIR/run-input.json" > "$PRIVATE_DIR/run.json"
python3 "$SKILL_DIR/scripts/daily_followup_state.py" --db "$PRIVATE_DIR/state.sqlite3" pending-touches
python3 "$SKILL_DIR/scripts/daily_followup_state.py" --db "$PRIVATE_DIR/state.sqlite3" next --run-id '<run_id>' --limit 5
```

若当前使用者选择 `specified`，先在 `$PRIVATE_DIR/config.json` 保存真实账号配置。结构如下；占位值必须换成现场核实的账号 ID、名称和 CSV 中的归属账号名称：

```json
{
  "accounts": [
    {"id": "实际账号ID", "name": "实际账号名称", "csv_names": ["CSV中的归属账号名称"]}
  ]
}
```

指定账号的导入与登记使用以下命令替代上面的对应步骤。`daily_import.py` 只读取显式 `--config`，登记命令通过 `XHS_FOLLOWUP_CONFIG` 读取配置，两处都要指定。`run-input.json` 同时设置 `scope_mode:"specified"`、实际 `account_ids` 和本次导入返回的 `source_import_ids`，不能继续使用 `all` 示例。

```bash
python3 "$SKILL_DIR/scripts/daily_import.py" --db "$PRIVATE_DIR/state.sqlite3" --config "$PRIVATE_DIR/config.json" --file "$PRIVATE_DIR/客户导出.csv" > "$PRIVATE_DIR/import-result.json"
XHS_FOLLOWUP_CONFIG="$PRIVATE_DIR/config.json" python3 "$SKILL_DIR/scripts/daily_followup_state.py" --db "$PRIVATE_DIR/state.sqlite3" register-run --input-json "$PRIVATE_DIR/run-input.json" > "$PRIVATE_DIR/run.json"
```

新建 `register-run` 在同一事务登记批次并初始化队列，成功返回 `initialized:true`、`queue_count`；初始化失败整笔回滚。`next`、`run-status`、`report` 保持只读。旧批次确实缺队列时才用显式 `queue` 或正式执行的 `begin-session` 初始化，不能为了看状态写入账本。

启动/恢复前先处理 `pending-touches.items` 中的 reserved/clicked，打开真实会话核对后 `resolve-touch`，不能直接重发。`uncertain_items` 和 `legacy_uncertain_items` 是已隔离的待核实 UID，只在报告列出，不阻塞其他客户。日常只取 `next.items`（已过滤可处理项）；完整诊断才导出旧 `queue`，其中只有 `queue.actionable_items` 可循环，`queue.items` 还包含已排除和已完成记录。每项旧账号 ID 仅是缓存线索，all 的实际范围以此次搜索为准。

用户明确要求跨日继续原名单时，执行：

```bash
python3 "$SKILL_DIR/scripts/daily_followup_state.py" --db "$PRIVATE_DIR/state.sqlite3" authorize-remaining --run-id '<原批次ID>' --evidence '<当前使用者的实际授权原话>'
```

授权只属于该批次，队列、准备和点击共同读取；不改开始时间、不另建批次，既有失败、待核实、留资、无意向及成功去重仍生效。

## 2. 搜索、业务阅读、准备

先按 Codex Desktop 当前 CUA 文档绑定使用者已登录的 tab；本项目只提供这个接口的适配。REPL 需要允许本地模块导入、文件访问和 `node:child_process` 启动 Python。普通网页 Console 不具备这些能力，其他代理环境须另行实现兼容 tab 接口并验证。所有 UI 动作只由一个执行器操作。下列 JavaScript 在同一 CUA 会话保存变量。Shell 的 `SKILL_DIR` / `PRIVATE_DIR` 不会自动传入 REPL，必须分别设置 `skillDir` / `privateDir` 为实际绝对路径；所有文件路径由这两个值组合。客户原文保存在 runtime，避免整段打印给用户。

### 正常执行入口

使用持久工作流，避免每轮临时重写搜索、落账和续接函数。先建立只读实例完成核查；以下代码不会发送，也不会初始化或迁移真实账本：

```js
var fs = await import('node:fs/promises');
var path = await import('node:path');
var {pathToFileURL} = await import('node:url');
// 替换成当前安装位置和当前项目 runtime 的实际绝对路径。
var skillDir = '/absolute/path/to/project/.agents/skills/dl-xhs-winback';
var privateDir = '/absolute/path/to/project/.xhs-winback-private';
var {createXhsCuaAdapter,captureForAnalysis} = await import(pathToFileURL(path.join(skillDir, 'scripts/xhs_followup_cua_adapter.mjs')).href);
var {createXhsLocalLedger,createFileProgressStore} = await import(pathToFileURL(path.join(skillDir, 'scripts/xhs_followup_local_bridge.mjs')).href);
var {createXhsFollowupWorkflow,migrateLegacyRecovery} = await import(pathToFileURL(path.join(skillDir, 'scripts/xhs_followup_workflow.mjs')).href);
var run = JSON.parse(await fs.readFile(privateDir + '/run.json','utf8'));
var flow = createXhsCuaAdapter(tab);
var ledger = createXhsLocalLedger({scriptPath:skillDir+'/scripts/daily_followup_state.py',dbPath:privateDir+'/state.sqlite3'});
var store = createFileProgressStore({path:privateDir+'/workflow-progress-'+run.run_id+'.json',runId:run.run_id,writesEnabled:false});
var workflow = createXhsFollowupWorkflow({adapter:flow,ledger,run,batchMode:true,progressStore:store,sendEnabled:false});
var startup = (await workflow.summary()).startup;
// 这里只读取原进度和汇总，不执行页面动作。
```

只读检查真实页面时使用后文 `flow.observeUid`，不要在禁止进度写入的文件存储实例上运行会保存事件的 `runSlice`。

用户当前已授权发送且没有暂停时，才把 ledger 的 `writesEnabled`、进度存储的 `writesEnabled` 和 workflow 的 `sendEnabled` 设为 true。进度存储参数是 `{path:privateDir+'/workflow-progress-'+run.run_id+'.json',runId:run.run_id,writesEnabled:true}`；传入 workflow 的 `progressStore`。`sessionId` 使用原用户请求 ID；没有可取 ID 时只生成一次并保存，后续重启/上下文压缩仍用原值。先 `ledger.sessionStatus({run_id:run.run_id})` 查已有活跃 session，不能因重启另建基线。确实结束该请求时才 `ledger.endSession`。

`sessionStatus` 返回 `session_id`、`state`、`baseline` 和 `delta` 等字段；已有 `state:'active'` 时沿用返回的 `session_id`。`workflow.summary()` 是异步方法，读取状态使用 `(await workflow.summary()).state`。启动先读取 `summary.startup`，向用户显示版本、名单与账号范围、原文话术、`touch_policy`、待处理数、`send_enabled` 和 `paused`。`run_state:active` 只表示批次未结束，不能据此宣称发送已恢复。旧批次未初始化时，`queue_status.initialized:false`、`initialization_required:true`，总未完成人数为 `null`（未知）；本地待审、暂挂、ready 数量仍单独显示。不能把未知值写成 0。已结束批次通过 `run_id` 读取最后一次执行汇总；重启、换聊天或传入新会话 ID 均不新建执行会话，不重开批次。

暂停期间需要持久保存审核时，使用原进度文件，设置 `sendEnabled:false`、ledger `writesEnabled:false`、progressStore `writesEnabled:true`；只执行本地 `submitReviews` / `holdReviews`，保留已有 `control.paused`。审核证据保存在本地，正式恢复实例再核对现场并将保护结论落入账本。该配置不初始化发送 session，也不执行 `resume()`。恢复发送需用户明确指令。

- 正常执行 `workflow.runSlice({limit:50,maxDurationMs:20000,maxReviews:12})`，依当前状态串行处理多位，返回简短统计。已审核 `ready_jobs` 优先；`maxReviews` 是新客户入审上限，不能挡住已审核客户的现场复核。复核发现新风险可重新入审，待审数因此可能暂时超过上限。时间片在 UID/账号边界检查，当前原子动作可能延长本段；不是 20 秒强杀。`runNext/continueReview` 只保留单步诊断兼容，不再作为正式批次默认路径。
- `review_queued` 表示完整聊天已入持久待审队列，不是读页失败。用 `await workflow.reviewPackets({queue:'pending',limit:12})` 一次获取多位，输出须 `JSON.stringify` 展开全文，不能根据 `[Object]` 或截断摘要审批。`queue` 可取 `pending` / `held` / `all`，默认 `pending`；`limit` 为 1–30，默认 12；`offset` 为非负整数，默认 0。固定队列仅取材料时用 `offset:12,24,...` 分页；每次审批/暂挂会移除已处理项，继续普通队列时重新从 `offset:0` 取，避免漏项。逐 UID、逐账号阅读真实原文；新增图片/未知媒体必须看页面。按实际内容提交 `{uid,evidence_id,decisions:[{account_id,contact_shared,stop_contact,evidence}],visual_reviewed}`。证据指纹必须匹配；有未知视觉内容时未真正看过不能传 `visual_reviewed:true`。
- `submitReviews` 保存历史判断与停发保护，但不点击或预占发送。下一段重新核查当前 UID、账号范围、标签、草稿和新增消息，再发。审批等待超过 120 秒也不会作为搜索失败；120 秒仅约束这次发送所依赖的现场观察。
- 审核完成且当前允许发送时，优先 `await workflow.submitReviewsAndRunSlice(approvals,{limit:50,maxDurationMs:20000,maxReviews:12})`。返回 `{approval,slice}`：审核部分仍为 `sends:0`，实际操作在 `slice.results`；一次调用共享排他锁并执行现场复核。暂停时该调用只保存审核，`slice.status` 为 `paused`，不会解除暂停。日常主动选择暂停下的 `submitReviews`，便于明确只保存本地判断。
- 读完仍有歧义的客户，调用 `await workflow.holdReviews([{uid:packet.uid,evidence_id:packet.evidence_id,reason:'具体未确认内容'}])`。整批指纹和原因验证后才移入 `held_reviews`；保留原包和入队时间，不预占、不发送，也不记为失败或无意向。`reviewPackets({queue:'held',limit:12})` 获取完整暂挂材料；审核明确后仍用 `submitReviews` / `submitReviewsAndRunSlice`，自动移出暂挂并重新核对现场。结束时从 `(await workflow.summary()).state.held_reviews` 报告 UID、原因和数量，未解决的仍保持停发。
- `next` 在截取数量前排除本地普通待审、暂挂、已排入 ready 的 UID；只读检查还排除已检查的 UID。已审核任务独立在队首执行，排除名单不是账本跳过结论。返回 `pending_review_count`、`held_review_count`、`ready_count` 分别报告；暂挂积累不会使下一窗口反复返回同一批等待客户。
- 没有客户内容或客户每条文字都是 `xhs_business_review.mjs` 明确列出的完整无歧义问询，可自动判断；数字、“好/是”、陌生媒体、任意其他表述进待审。历史完整性和跨账号资格仍必须通过。不得用宽松关键词或批量 false 绕过。
- 复用旧视觉审核要求原账号历史消息 ID、文字、类型、时间等均未变化，新增内容也必须是可提取纯文字；我方新增文字、平台中心文字提示、白名单客户问询可复用。客户其他新表述、新媒体、新账号须重新判定；完整历史缺少已审核消息时不从缓存补回。缓存授权历史证据，当前 UI token 始终重新取得。
- 新媒体入审后仍阅读完整上下文：从原审核证据确认旧媒体已看过且未变时，可沿用其历史判断，并实际查看新增媒体。原审核证据无法核实，或新内容需要重新解释旧图时，再查看旧媒体。`visual_reviewed:true` 表示当前包需要视觉判断的内容均已通过真实核实，不只表示新图已看。
- 已审批原文按不可变证据文件保存于 `<progress path>.reviews/`，进度只保存 SHA256 引用；须一起备份/恢复，不删除其中一个。证据缺失/损坏按本地证据故障处理，不计平台搜索故障，不猜测原判断。
- 用户暂停时先 `workflow.requestPause()`，已经进入当前账号发送段的消息可能仍完成点击和回执落账；暂停在账号/客户操作边界生效，不能承诺中途撤销该条消息。边界停住后 `await workflow.pause()` 保存；重启不会自动恢复。只有用户恢复发送后才 `workflow.resume()`。新批次 `uid_once` 每 UID 本轮只联系一次；旧批次按启动摘要显示的原规则续接。只有旧 `account_once` 批次才继续该 UID 未完成账号，不重复已成功会话。所有相关账号仍核查留资和拒绝证据。
- 文件进度存储使用同目录 `.xhs-executor.lock` 排他锁；禁止第二控制器抢页面。遇 `executor_already_owned` 先核实旧调用是否仍在运行，不按时间自动抢锁。只有确认旧执行器已结束且核销 reserved/clicked 后，才把孤立锁保存为诊断副本并解除。不能删除进度来绕过锁或业务保护。
- 工作流负责点击前落账、一次点击、回执落账，以及 failed/uncertain 后跳过 UID。`label_updates` 仍按第 3 节补标；`blocked` 必须看具体原因，不能创建新实例绕开。
- 外部核销 pending 后再调用即可解除对应阻塞；其他搜索、草稿故障不会一起清除。批次模式中已证明未填入/未点击的准备失败可由 `cancelUnclickedTouch` 耐久取消并跳过该 UID；无法证明、已经点击或残留不明草稿仍须核销。
- 同一批次的下一次独立请求：旧 session 已结束时传新 `sessionId`，工作流会核实关闭状态再换统计基线，保留恢复次数、故障与隔离记录。旧 session 仍 active 时必须沿用，不允许借新 ID 清零。
- 进度里保留每段耗时和最近失败的 UID、阶段、搜索输入、结果数量。失败截图另存当前 `privateDir`。`review_wait` 是每项任务各自等待的累计，任务可能重叠，不能相加作为整段墙钟耗时。速度用请求起止时间和确认送达计数计算。每约 60 秒向用户报告账本确认数和具体变化；避免逐条播报机械动作。
- 修改模块后必须重新导入并新建适配器；改变下游依赖时仅给父模块加 URL 参数不够，旧依赖仍可能被缓存。先保存暂停、确认无运行调用/待落账发送，再重置 CUA 会话并从原进度新建实例。浏览器连接若无法重建，记整体连接故障，保留进度，不算 UID 搜索失败。不能混用新旧 `flow`。

若 `createFileProgressStore.load()` 报 `progress_migrate_required`，先保留错误携带的 `legacy_state`。使用 `migrateLegacyRecovery(legacy_state,{runId,sessionId})` 会保留旧恢复次数并保持阻塞。修复已完成现场验收时，可附 `repairValidation:{result:'passed',validated_at_ms:实际时间,evidence_path:真实证据文件绝对路径,uid_checks:[{user_id:实际UID,status:'observed或stopped'},至少3个不同UID]}`，再显式 `store.save()`。不删除旧文件，不把旧 attempts 重置成零；再次连续异常仍进入修复。验收对象不得凭本地测试伪造。

### 单步诊断接口

控制连接明确报 `Debugger unattached` 或 browser disconnected 时，工作流记 `browser_connection_unavailable` 并暂停，不登记客户搜索暂缓、不消耗 UID 慢查。连接恢复后调用 `workflow.verifyConnection(实际队列项)`；目标身份、范围和历史重新读取通过才解除连接阻塞。该方法只读，不发送、不清空旧搜索故障，也不解除用户暂停。

以下手工步骤只用于诊断或核销，不与工作流同时控制页面。完整诊断队列可用下列命令导出（旧批次缺队列时该命令可能初始化，因此纯状态查询使用 `next` 或 `run-status`）；不能把 `next.items` 和 `queue.items` 混用：

```bash
python3 "$SKILL_DIR/scripts/daily_followup_state.py" --db "$PRIVATE_DIR/state.sqlite3" queue --run-id '<run_id>' > "$PRIVATE_DIR/queue.json"
```

```js
// 先执行上面的 REPL 路径和模块初始化；只在没有其他执行器操作页面时运行。
var flow = createXhsCuaAdapter(tab);
var run = JSON.parse(await fs.readFile(path.join(privateDir, 'run.json'),'utf8'));
var queue = JSON.parse(await fs.readFile(path.join(privateDir, 'queue.json'),'utf8'));
var item = queue.actionable_items[0];
var observed = await flow.observeUid({uid:item.user_id, scopeMode:run.scope_mode,
  authorizedAccountIds:run.account_ids, relevantAccountIds:item.account_ids,
  historyCache:item.history_cache || {}, budgetMs:30000});
```

- `stopped`：已经有核实过的标签/平台留资证据，直接传 prepare 落账，不继续读取无关历史。
- `observed` 且 coverage_complete：阅读各 review 的客户聊天/必要图片，只确认是否留过联系方式、是否明确要求停止联系。脚本的 hints 不替代语义判断。用下列方法确认后才 prepare；这是代理的阅读步骤，不向用户重复要批准。
- `history_complete=true` 可来自读到已核实的旧边界；`history_from_start_complete=false` 不能单独成为暂缓理由。确认旧误判时，使用 `reopen-browser-defer` 先预览再带审计纠正，不能直接改数据库或放开已发送/已留资的 UID。
- `deferred`：根据 reason 检查具体页面。未出现搜索浮层不等于无结果；同一 UID 最多恢复一次。最终仍未读全才 `record-browser-defer`，`retry_stage` 使用当前 queue 项的 main/slow。内部重读不重复计数；批量恢复见第 5 节。

```js
// decisions 由代理读完真实内容逐账号填写；以下仅为结构，不能直接复制 false。
var decisions = [{account_id:'当前实际账号ID', contact_shared:false, stop_contact:false, evidence:''}];
observed.reviews = flow.confirmBusinessReview({token:observed.token,decisions});
var input = {run_id:run.run_id,user_id:item.user_id,
  selected_account_id:observed.selected_account_id,reviews:observed.reviews,
  discovered_account_ids:observed.discovered_account_ids,search_complete:observed.search_complete};
await fs.writeFile(path.join(privateDir, 'prepare-input.json'),JSON.stringify(input),{mode:0o600});
```

拒绝联系的 `stop_contact:true` 必须附客户实际原话 evidence。联系方式已给或明确已加为 contact_shared:true；不要把我方联系方式、产品编号、提问或仅点击企微卡算留资。

```bash
python3 "$SKILL_DIR/scripts/daily_followup_state.py" --db "$PRIVATE_DIR/state.sqlite3" prepare-touch --input-json "$PRIVATE_DIR/prepare-input.json" > "$PRIVATE_DIR/prepared.json"
```

prepare 先存观察，再返回 send/skip/defer/observed_only。任何 `label_updates` 按下一节核对当前补标授权，绝不继续该 UID 的发送。

## 3. 明确拒绝时补标无意向

prepare 已耐久保存 UID 停发，返回需要补标的账号和拒绝原文。只有当前使用者已授权补标时才继续；未授权则保留 pending、报告待补标，跳过页面写入。按 [页面约定](browser-contract.md) 重新核对 UID/会话，打开当前可见的“客资标签”，选择“无意向”，按页面保存并读回。保留已有标签及联系信息。控件未现场验收时依据新快照操作，不猜选择器、不把点击当作成功。

将读回证据存到 private JSON：

```json
{"run_id":"本轮ID","user_id":"目标UID","account_id":"实际会话账号",
 "uid_verified":true,"account_verified":true,"label":"no_intent",
 "observed_at_ms":0,"detail":"页面重新读取已显示无意向"}
```

示例中的 0 是占位值，必须换成实际观察时间。失败时填实际 detail，verified 字段为 false，不伪造标签；pending/failed 同样停发。

```bash
python3 "$SKILL_DIR/scripts/daily_followup_state.py" --db "$PRIVATE_DIR/state.sqlite3" record-no-intent --input-json "$PRIVATE_DIR/无意向回执.json"
```

报告的 no_intent_updates 区分 pending/verified/failed。仅出现客户退订时执行补标；同行、没有主动说话或普通“不需要某份资料”不补标。

## 4. 发送及回执

以下手动步骤只在当前使用者明确授权发送且未暂停时使用，不能当作安装自检。只在 prepare.action=send 时继续，并在同一 CUA 会话使用本次准备结果：

```js
var prepared=JSON.parse(await fs.readFile(path.join(privateDir, 'prepared.json'),'utf8'));
var stage=await flow.revalidateAndStage({token:observed.token,prepared});
```

stage.status 必须是 staged。否则确认是否未点击、自动草稿是否已清理，再决定是否 `resolve-touch confirmed_not_sent`；不能凭异常直接核销。

```bash
python3 "$SKILL_DIR/scripts/daily_followup_state.py" --db "$PRIVATE_DIR/state.sqlite3" mark-clicked --touch-id '<本次touch_id>' > "$PRIVATE_DIR/marked-click.json"
```

```js
var markClicked=JSON.parse(await fs.readFile(path.join(privateDir, 'marked-click.json'),'utf8'));
var receipt=await flow.clickAndVerify({stageToken:stage.stageToken,markClicked});
await fs.writeFile(path.join(privateDir, 'finish-input.json'),JSON.stringify({
  touch_id:prepared.touch_id,
  status:['sent','failed'].includes(receipt.status)?receipt.status:'uncertain',
  reason:receipt.reason || receipt.status,
  detail:receipt.detail || receipt.reason || receipt.status,
  verified_at_ms:receipt.sent_at_ms || Date.now()
}),{mode:0o600});
```

```bash
python3 "$SKILL_DIR/scripts/daily_followup_state.py" --db "$PRIVATE_DIR/state.sqlite3" finish-touch --input-json "$PRIVATE_DIR/finish-input.json"
```

只有稳定的新消息匹配原文才成功。隐私、限频和其他拒发都记 failed；本次消息明确显示“重新发送”也记 failed，未捕获原因就保留未知。没有明确成功或失败证据记 uncertain。两者均落账后跳过该 UID、继续其他客户，不重发、不要求先追出原因；uncertain UID 在后续运行仍隔离，核实后才可解除。平台限制不自行绕过。不因显示主账号名称就推断要切换账号。

恢复时核对发送前落账和回执记录正常；单条失败按跳过流程继续。新批次 `uid_once` 成功后结束该 UID，本轮不切其他账号再发。仅旧 `account_once` 批次可继续其他已检查且仍有效的会话（当前 UID 已 failed/uncertain 时跳过其剩余会话）：

```js
observed=await flow.selectReviewedAccount({token:observed.token,accountId:'下一个未发送的实际会话账号'});
```

返回 observed 后复用更新后的 reviews 和搜索范围，再 prepare/发送；发生页面变化则重新观察。超过 120 秒需重新核查当前页面，不延长旧页面证据；已审核的历史原文和语义结论可复用，不需要重复逐人审核。

## 5. 故障停止和报告

按 UID 统计最终核查结果：内部重读同一 UID 不增加连续故障数，`observed` 或有明确保护证据的 `stopped` 清零。单个失败先落账暂缓，主队列结束后按 queue 的 `retry_stage:slow` 最多补查一次；不重试已点击、失败或回执不明的发送。

连续 3 个不同 UID 核查失败时，先用已知正常样本只读检查页面。对照样本可靠时隔离单客户问题并继续；无法确认整体可靠时，暂停发送并在最多 60 秒内做一次只读恢复：

1. 先检查账本未核销的 reserved/clicked 和当前草稿；有未核销点击、用户草稿、登录失效或整体页面已无法操作则保存现场并报告，不刷新。已登记 uncertain 的客户仅隔离，不阻止页面恢复或其他客户发送。
2. 调用 `flow.ensurePage()` 恢复私信通/客资标注，再重新绑定适配器。按最新页面状态重新触发搜索，选本轮刚失败且仍有 slow 次数的一位，用 `budgetMs:45000` 做只读核查。
3. 仍失败：落账 slow 结果并报告具体 reason，结束本次批量。核查成功：保存检查证据，清零计数并继续同一轮；发送前按正常 prepare 和核验流程重新检查，不能直接复用过期 token。
4. 同一轮页面恢复后再次出现连续 3 个不同 UID 失败，直接报告并进入修复，不重复恢复循环。重启执行器也不能清除本轮已有的恢复记录。恢复开始/结果写入当前 runtime 的本轮进度文件。

切账号超时属于当前客户读取异常，按 main/slow 的次数上限暂缓，保留此前成功结果并继续其他客户。浏览器断连、用户草稿、未核销点击和本地保存失败分别阻断；不可归入普通搜索失败或通过新实例绕过。搜索未完成、客资面板未加载、身份不足与历史未读全分别保留诊断原因。

5 分钟没有完成首个有效会话核查就报告具体阻塞。修代码期间暂停实际业务，测试使用临时数据；原故障客户可做只读复验，不能发测试消息。每次进度至少包含确认成功数、实际故障、下一步；不声称本地测试证明了平台发送稳定或速度。

```bash
python3 "$SKILL_DIR/scripts/daily_followup_state.py" --db "$PRIVATE_DIR/state.sqlite3" report --run-id '<run_id>' > "$PRIVATE_DIR/daily-report.json"
```

结束时明确范围：本轮累计使用`sent_this_run`、`failed_this_run`、`uncertain_this_run`；本次继续新增使用 `session-status.delta` 或 `workflow.summary().since_start`，不能拿上下文最后一小段冒充整个用户请求的新增。失败原因汇总用 `send_failure_reasons`，待核实原因汇总用 `send_uncertain_reasons`，问题名单用 `send_issues` 的 user_id/account_id/state/reason/detail/finished_at_ms。消息条数和去重客户数分开说明。逐条失败不用打断用户，原因未知就写未知。另报告按 UID 业务停发、标签补标和成果观察。来源 unknown 单列，标签时间只能报首次发现，关联不等于因果。历史迁移只在确实需要时另做，不作为每次启动前置。

## 6. 独立话术分析（仅在用户要求复盘话术时阅读）

深度分析独立发起：最近 30 天进线，广告/自然流 × 有/无平台“留客资”标签，每格最多 10 人。候选覆盖导入名单中的已留资、拒绝联系、失败和未识别客户；抽样与补查共用已核验会话的账号映射。缺映射或账号范围不明列入 `coverage_gaps`，不能解释成没有可分析客户。同一 UID 的不同原账号仅部分完成映射时，也保留缺口；已知账号均 absent 不能代替未解析账号的标签核验。`mapping_missing_uids`、`scope_unknown_uids` 也包含部分账号未解析者，可与 `scoped_uids` 重叠，不能相加当总人数。存在多个不同原账号上下文时，需要实际补齐其映射后才能清除缺口；新增一个核验账号不能证明它对应哪个原账号名称。平台无标签也不能证明聊天里未留联系方式。先用已缓存的明确观察；浏览器补查最多 120 UID 或 20 分钟，每人最多取 40 条真实消息或回溯 30 天。未留资样本距最新进线至少 48 小时。缺格、历史截断、首次留客资时间不明，都在报告写清；抽样不能算真实留资率。

```bash
mkdir -p "$PRIVATE_DIR/analysis"
chmod 700 "$PRIVATE_DIR/analysis"
python3 "$SKILL_DIR/scripts/followup_analysis.py" sample-plan --db "$PRIVATE_DIR/state.sqlite3" --account-ids '<已确认账号ID,用逗号分隔>' --output "$PRIVATE_DIR/analysis/plan.json" > /dev/null
```

分析命令的 `--db` 位于 `sample-plan` / `enrich-plan` 子命令之后；`report` 只读取传入文件，无需数据库参数。

先按第 2 节选好 tab、建立 `fs`、`path`、`privateDir` 与 `flow`，不执行发送步骤。在**同一个** CUA tab 上只读采集 `plan.groups` 中缺聊天的样本，再采 `plan.lookup_queue`；本次补查**所有去重 UID 合计最多 120 人**。已有已核验聊天记录可以复用，但未留资客户仍需这次重新核标签。按 `plan.limits.max_seconds` 从采集开始设置总截止时间；接近 20 分钟就停止启动新 UID，不靠离线报表命令事后限时。每人 `analysisMode:true`，最多读取该 UID 的 40 条真实消息、向前 30 天；只保存 `captureForAnalysis` 给出的已核验结构到 `path.join(privateDir, "analysis", "transcripts.jsonl")`。单人超时/账号不全直接记缺口，不延长总截止时间。示意循环：

```js
var plan = JSON.parse(await fs.readFile(path.join(privateDir, 'analysis', 'plan.json'), 'utf8'));
var transcriptsPath = path.join(privateDir, 'analysis', 'transcripts.jsonl');
var saved = await fs.readFile(transcriptsPath, 'utf8').catch((e) => {if (e.code === 'ENOENT') return ''; throw e;});
if (!saved) await fs.writeFile(transcriptsPath, '', {mode: 0o600});
await fs.chmod(transcriptsPath, 0o600);
var reusable = new Map(saved.split('\n').filter(Boolean).map((line) => JSON.parse(line)).filter((record) =>
  record.uid_verified === true && record.account_verified === true && Array.isArray(record.messages) && record.messages.length > 0)
  .map((record) => [record.user_id + ':' + record.account_id, record]));
var deadline = performance.now() + Math.min(1200, plan.limits.max_seconds) * 1000;
var selected = Object.entries(plan.groups).flatMap(([group, items]) => items.map((item) => ({...item, group})));
var targets = [...selected, ...plan.lookup_queue];
var visited = new Set();
var checked = 0;
for (var candidate of targets) {
  var remainingMs = deadline - performance.now();
  if (remainingMs <= 31000 || checked >= 120) break;
  if (visited.has(candidate.user_id)) continue;
  visited.add(candidate.user_id);
  var accountId = candidate.account_id || candidate.preferred_account_id;
  var old = reusable.get(candidate.user_id + ':' + accountId);
  var tagCutoff = candidate.tag_event_at_ms || candidate.observed_at_ms;
  var priorRelevant = old?.messages.some((message) => Number.isFinite(message.at_ms) &&
    message.at_ms <= tagCutoff && message.at_ms >= tagCutoff - 30 * 86400000);
  if (candidate.group?.endsWith('_tagged') && old?.captured_at_ms >= candidate.latest_in_at_ms && priorRelevant) continue;
  var accounts = candidate.account_ids || [accountId];
  checked += 1;
  var sample = await flow.observeUid({
    uid: candidate.user_id,
    authorizedAccountIds: plan.account_ids,
    relevantAccountIds: accounts,
    targetAccountId: accountId,
    analysisMode: true,
    budgetMs: Math.min(30000, Math.floor(remainingMs - 1000))
  });
  if (sample.status !== 'sampled') {
    await fs.appendFile(transcriptsPath, JSON.stringify({record_type: 'capture_failure',
      user_id: candidate.user_id, account_id: accountId, account_ids: accounts,
      captured_at_ms: Date.now(), status: 'deferred', reason: sample.reason || 'coverage_incomplete'}) + '\n');
    continue;
  }
  for (var account of accounts) {
    var transcript = captureForAnalysis(sample, account);
    if (transcript) await fs.appendFile(transcriptsPath, JSON.stringify(transcript) + '\n');
  }
}
```

采完后使用补查结果更新四格，再生成脱敏报告；原先被列为未留资、补查却发现“留客资”的客户会移出旧分组。分析脚本只读账本，不自动改写或发送话术。

```bash
python3 "$SKILL_DIR/scripts/followup_analysis.py" enrich-plan --db "$PRIVATE_DIR/state.sqlite3" --plan "$PRIVATE_DIR/analysis/plan.json" --transcripts "$PRIVATE_DIR/analysis/transcripts.jsonl" --output "$PRIVATE_DIR/analysis/enriched-plan.json" > /dev/null
python3 "$SKILL_DIR/scripts/followup_analysis.py" report --plan "$PRIVATE_DIR/analysis/enriched-plan.json" --transcripts "$PRIVATE_DIR/analysis/transcripts.jsonl" --output "$PRIVATE_DIR/analysis/report.json" --markdown-output "$PRIVATE_DIR/analysis/话术复盘.md" > /dev/null
```

版本统计 `versions` 的范围为 `versions_scope:all_recorded_runs`。用户报告展示 `sent_messages`（确认发送消息数）、`unique_sent_customers`（该话术去重客户数）和 `observation_windows`（观察窗口数）；旧 `sent_customers` 仅作接口兼容。一个客户可出现在多个话术中，各话术人数不可相加为全局人数。平台留资标签、聊天中提供的联系方式和七天内话术关联分别呈现；这些记录不证明话术带来了确定效果。
