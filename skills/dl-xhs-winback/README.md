# 小红书私信跟进

手里有一份小红书私信通导出的用户名单，想知道还有谁可以跟进？这个工具会按用户 ID 合并重复记录，核对不同账号下的会话，把已经留资或明确拒绝联系的人排除。你确认名单、账号范围和话术并明确要求发送后，它才会开始跟进，并记录谁收到了、谁还需要核实。

## 第一次用，先在自己的电脑上跑通示例

你需要 **Codex Desktop、Python 和 Node.js**。推荐 Python 3.10 或更高版本、Node.js 24，已在 macOS 上验证。先用包里的两位虚构用户练习，这一步用不到客户名单或私信通账号。

1. [下载小红书私信跟进 ZIP](https://github.com/DalinChen255/dl-skill/releases/latest/download/dl-xhs-winback.zip)。在 Codex Desktop 打开你打算用来做跟进的项目文件夹。
2. 把 ZIP 的内容解压到这个项目的 `.agents/skills/dl-xhs-winback/`。如果用的是仓库源码，就把 `skills/dl-xhs-winback/` 整个文件夹复制到项目的 `.agents/skills/`。装好后应能看到 `.agents/skills/dl-xhs-winback/SKILL.md`。在 Mac 的访达中按 `⌘⇧.` 可以显示以点开头的文件夹。
3. 在那个项目里新开一个 Codex 对话，把下面这句话发给它。

> 使用 $dl-xhs-winback，运行工具自带的离线安装自检，把结果告诉我。只用虚构示例，不打开私信通。

自检会把[两位虚构用户](examples/users.example.csv)导入临时账本，登记一个示例批次，读回统计，并检查浏览器适配模块能否加载。成功时会看到 `"ok": true`、`"messages_sent": 0`、`"labels_changed": 0`。`"browser_verified": false` 表示这一步尚未打开你的浏览器。

如果 Codex 找不到工具，先核对上面的安装路径，解压后容易多出一层同名文件夹。也可以在**项目根目录**打开终端，直接运行下面这行。出现 `"ok": false` 时，看 `error` 指出的缺项。

```bash
python3 .agents/skills/dl-xhs-winback/scripts/self_check.py
```

## 换成自己的名单，先看只读预览

1. 在同一项目里建文件夹 `.xhs-winback-private/`，用于保存真实 CSV、账本和记录。项目用 Git 时，在项目根目录的 `.gitignore` 里加一行 `/.xhs-winback-private/`。把私信通导出的 CSV 放进去，可以放多个月份的表。目录权限设置见[执行手册](references/execution-runbook.md)。
2. 在 Codex Desktop 能控制的浏览器中登录自己的小红书私信通，打开私信页面。工具依赖 Codex 的浏览器控制（CUA）和本地脚本权限。只有普通网页聊天或 Claude Code 的环境无法操作这个页面。
3. 在刚才的项目里发下面这段话，把文件名换成你自己的。第一次先看一个能找到的会话就够了。

> 使用 $dl-xhs-winback。名单在这个项目的 .xhs-winback-private/客户导出.csv。这次看全部账号。先告诉我表里有多少位不同用户、多少人因为已留资被排除。选一位待核对的用户，只读打开会话，告诉我你核对到了什么。不要发送或改标签。

先看两件事。**名单读对了吗？** 数字按用户 ID 去重，同一个人出现在两张表里只算一次。**页面能核对吗？** 应明确匹配到了哪个用户和账号、是否能读到标签及聊天。看不到页面或找不到会话时，工具应给出具体原因。有候选人只表示还需要核对，不能直接当成可发送人数。

## 以后怎么用

| 你要做什么 | 可以怎么说 |
| --- | --- |
| 发起一轮跟进 | 核对名单后告诉它“使用刚才的名单，范围是……，按这段原文开始本轮跟进：……”。每位客户每轮默认只联系一次。补标“无意向”需要单独授权。 |
| 暂停或继续 | 说“暂停”。当前正在处理的发送可能完成，以返回的暂停状态为准。下次在同一项目说“继续原批次”，沿用原账本。 |
| 看结果或复盘 | 说“只看原批次汇总”，可看到成功、失败和待核实记录。想看话术时说“复盘这次话术”。抽样结果仅适用于抽到的会话。 |

在一个会话里发现留资、“无意向”标签或明确拒绝联系，同一用户的所有账号都会停发。平台拒发会记录原因；点击后回执不清楚会单列为“待核实”，两种情况均不会自动重发。[跟进资格与客户保护](references/eligibility-rules.md)

## CSV 和账号怎么准备

保留导出文件的以下表头，编码使用 UTF-8（可带 BOM）。没有内容的单元格可以留空。按“用户ID”匹配会话，时间按上海时区解析，支持 `YYYY-MM-DD HH:MM:SS`、`YYYY/MM/DD HH:MM:SS` 或对应的纯日期。保留原有留资信息。[虚构 CSV 示例](examples/users.example.csv)

```text
用户ID,用户昵称,归属账号,最新进线时间,最近开口时间,最近留资时间,手机号,微信号,留资方式,用户类型
```

**全部账号**按搜索该用户时实际出现的会话核查，无需填写账号配置。**指定账号**时，按[配置示例](config.example.json)填入自己的真实账号 ID 和导出表里的账号名称；告诉 Codex 按[执行手册](references/execution-runbook.md)使用该配置导入和登记。

## 数据、环境和详细说明

真实数据留在本项目的 `.xhs-winback-private/`，每个项目单独使用一套。备份时一起保存账本、进度文件和相应的 `.reviews/` 证据目录。恢复原批次要使用原记录。对外分享前人工检查报告中的客户信息。

不同账号权限、登录状态和私信通页面更新，都可能影响搜索与回执核对。当前只在 macOS 验证过；浏览器必须支持 Codex Desktop 的 CUA 接口，并允许本地文件访问和启动 Python。了解一次核查具体怎样进行，可看[页面核对方式](references/browser-contract.md)和[批次运行说明](references/batch-design.md)。

[操作与故障处理手册](references/execution-runbook.md) · [工具完整指令](SKILL.md) · [工具合集版本](VERSION) · [私信跟进更新记录](CHANGELOG.md) · [使用许可](LICENSE)
