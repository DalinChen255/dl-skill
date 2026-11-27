# dl-skill

陈大Lin的开源 Agent skill 合集，包含商业模式诊断、内容深挖、小红书对标分析和私信跟进四个工具。

**免安装体验商业模式诊断**　打开[通用提示词](skills/dl-benshi-interview/提示词-通用版.md#提示词正文)，把“提示词正文”整段复制到豆包、ChatGPT 或 DeepSeek，就能开始访谈。

## 解决什么问题

| 你的处境 | 你会得到什么 | 用哪个 Skill |
|---|---|---|
| 别人常找你帮忙，你想把其中一件事做成收费产品，却拿不准卖什么、卖给谁 | 讲三件你帮过人的真事。AI 根据这些经历和你的反馈，整理出一份《商业模式诊断报告》，给出第一个产品、客户和定价建议，以及第一步怎么验证 | [商业模式诊断（`dl-benshi-interview`）](skills/dl-benshi-interview/README.md) |
| 按小红书客户名单跟进时，需要逐一核对留资、拒绝联系和多账号会话，暂停后还要接着处理 | 合并 CSV 名单并按 UID 去重；保护已留资、有无意向标签或明确拒绝联系的客户，按使用者授权的原文跟进，记录成功、失败和待核实结果。新批次默认每人每轮一次；同行或固定 24 小时间隔不作为单独排除条件 | [`dl-xhs-winback`](skills/dl-xhs-winback/README.md) |
| 脑子里有个模糊想法，想发内容但写不出来，或者写出来总不像自己说的话 | 20-30 分钟的访谈式对话把想法挖深，产出保留你原话和表达习惯的口播稿/小红书笔记/公众号长文 | `dl-content-deepdive` |
| 看到同行的爆款笔记想模仿，但说不清它的"套路"到底在哪 | 一份拆解报告讲透这批笔记的标题/开头/中间/结尾/CTA/叙事框架怎么写，外加一个能照着写（或只借部分套路）的 AI 写作指南 Skill | `dl-xhs-benchmark` |

每个工具的目录里都有 `SKILL.md`。`dl-content-deepdive` 和 `dl-xhs-benchmark` 可在 Claude Code、Codex 等支持 skill / system prompt 的 Agent 上使用。`dl-xhs-winback` 需要 Codex Desktop 的 CUA 环境。

## 如何安装

#### Claude Code

```bash
claude plugin marketplace add DalinChen255/dl-skill
claude plugin install dl-content-deepdive@dl-skill
claude plugin install dl-xhs-benchmark@dl-skill
```

以上 Claude Code 安装方式用于 `dl-content-deepdive` 和 `dl-xhs-benchmark`。`dl-xhs-winback` 目前依赖 Codex Desktop 的 CUA tab API；安装其文件本身不会提供浏览器控制能力。

#### 手动安装

先获取源码，再按照所选 Skill 的运行环境安装：

```bash
git clone https://github.com/DalinChen255/dl-skill.git
# 其他 Skill 可复制到对应宿主的 skill 目录，例如：
cp -R dl-skill/skills/dl-content-deepdive ~/.claude/skills/dl-content-deepdive
# 或 Codex: cp -R dl-skill/skills/dl-content-deepdive ~/.codex/skills/dl-content-deepdive
```

使用 Claude Code 或 Codex 做商业模式诊断时，将整个 `dl-skill/skills/dl-benshi-interview` 文件夹复制到 `~/.claude/skills/` 或 `~/.codex/skills/`，保留里面的诊断依据和报告模板。然后在新对话里说“帮我找找我能卖什么”。

`dl-xhs-winback`：把 `skills/dl-xhs-winback/` 放到你选定项目的 `.agents/skills/dl-xhs-winback/`，在 Codex Desktop 中打开该项目。建议使用 Python 3.10+ 和 Node.js 24，并具备 CUA 浏览器控制、本地文件访问及启动 Python 的权限；当前只在 macOS 环境完成验证。先运行 `scripts/self_check.py` 做临时数据自检，再只读核查自己已登录的小红书私信通页面。完整步骤见[安装与使用说明](skills/dl-xhs-winback/README.md)。

该 Skill 的客户名单、账本、配置与执行记录放在项目的 `.xhs-winback-private/`，不要放进 Skill 安装目录。全部账号模式无需账号配置；指定账号时，将 `config.example.json` 复制到 `.xhs-winback-private/config.json`，填写自己的真实账号 ID、名称与 CSV 名称映射。导入时传 `--config`，登记时设置 `XHS_FOLLOWUP_CONFIG`，所有数据库命令显式指定私密账本路径。先按[执行手册](skills/dl-xhs-winback/references/execution-runbook.md)核对参数位置。该目录须加入自己的项目 `.gitignore`。

`dl-content-deepdive` 不需要预先配置：第一次使用时会直接问你保存路径，确认后自动写入当前工作目录下的配置文件，不用手动编辑任何文件。

`dl-xhs-benchmark` 需要一个 [TikHub](https://user.tikhub.io/register?ref=QYnybFaK) API Token（用于通过公开 REST API 拉取小红书公开数据），首次运行 `scripts/check_env.py` 会引导你注册并输入 Token，自动保存到 `~/.dl-xhs-benchmark/tikhub_config.json`（不在 skill 目录内，不涉及 `config.json`/`config.example.json`），也可以提前设置环境变量 `TIKHUB_API_TOKEN` 跳过交互式引导。

#### 从 Release 下载

不熟悉 GitHub 时，可以按这三步下载：

1. 打开 [最新版本下载页](https://github.com/DalinChen255/dl-skill/releases/latest)。
2. 展开该版本的 **Assets**，下载对应 Skill 的 ZIP。小红书私信跟进用 `dl-xhs-winback.zip`；内容深挖用 `dl-content-deepdive.zip`；小红书对标分析用 `dl-xhs-benchmark.zip`。GitHub 自动生成的 `Source code` 是整个仓库源码。
3. 解压后根级就是 `SKILL.md`，按对应 Skill 的安装说明放入自己的项目。

小红书私信跟进的运行环境与使用方法见其 [README](skills/dl-xhs-winback/README.md)。

如果想本地构建，运行 `bash tools/build-skills.sh`，产物在 `dist/skills/`。`dl-xhs-winback.zip` 由独立白名单构建器校验文件范围后生成，另附校验和与文件清单；其余 Skill 仍按 git 跟踪的文件构建。不要手动压缩包含客户数据或本地运行产物的目录。

## 版本与发布

版本以根目录 [VERSION](VERSION) 为准。维护者运行 `python3 tools/set_version.py <版本号>` 同步版本，再用 `python3 tools/check_versions.py` 检查。代码通过测试后，推送 `v<版本号>` tag，GitHub Actions 会自动构建并发布安装包。

## 许可证

本项目采用 [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) 许可证。

- 个人使用、学习、研究、非商业项目：不需要署名，不需要申请
- 公开发布衍生作品（文章、工具、课程等）：请注明来源
- 商业用途：需要单独授权，请联系作者
