# dl-skill

陈大Lin的开源 Agent skill 合集。可在 Claude Code、Codex 等任意支持 skill / system prompt 的 Agent 上使用。

## 解决什么问题

| 你的处境 | 你会得到什么 | 用哪个 Skill |
|---|---|---|
| 在小红书接了留资线索，每天手动翻私信跟进客户太费时，还怕误发给已成交、已拒绝或同行账号 | 每天自动从线索表里筛出"该跟进的客户"（已留资、非同行、24 小时内无回复、无负面），逐条复核后代发跟进私信 | `dl-xhs-winback` |
| 脑子里有个模糊想法，想发内容但写不出来，或者写出来总不像自己说的话 | 20-30 分钟的访谈式对话把想法挖深，产出保留你原话和表达习惯的口播稿/小红书笔记/公众号长文 | `dl-content-deepdive` |
| 看到同行的爆款笔记想模仿，但说不清它的"套路"到底在哪 | 一份拆解报告讲透这批笔记的标题/开头/中间/结尾/CTA/叙事框架怎么写，外加一个能照着写（或只借部分套路）的 AI 写作指南 Skill | `dl-xhs-benchmark` |

每个 skill 目录下都有自己的 `SKILL.md`（含使用说明和授权门禁）；部分 skill 另有 `config.example.json`（配置示例，具体哪个 skill 需要见下方安装说明）。

## 如何安装

#### Claude Code

```bash
claude plugin marketplace add DalinChen255/dl-skill
claude plugin install dl-xhs-winback@dl-skill
claude plugin install dl-content-deepdive@dl-skill
claude plugin install dl-xhs-benchmark@dl-skill
```

#### 手动安装（适用于 Claude Code / Codex）

把对应 skill 目录整个复制到你本地的 skill 目录下即可，例如：

```bash
git clone https://github.com/DalinChen255/dl-skill.git
cp -R dl-skill/skills/dl-xhs-winback ~/.claude/skills/dl-xhs-winback
# 或 Codex: cp -R dl-skill/skills/dl-xhs-winback ~/.codex/skills/dl-xhs-winback
```

首次使用前，先在 skill 目录里把 `config.example.json` 复制一份改名为 `config.json`，填入你自己的账号 ID/昵称、工作区路径——`config.json` 已加入 `.gitignore`，不会被提交，你的真实配置只留在本地。

`dl-content-deepdive` 不需要预先配置：第一次使用时会直接问你保存路径，确认后自动写入当前工作目录下的配置文件，不用手动编辑任何文件。

`dl-xhs-benchmark` 需要一个 [TikHub](https://user.tikhub.io/register?ref=QYnybFaK) API Token（用于通过公开 REST API 拉取小红书公开数据），首次运行 `scripts/check_env.py` 会引导你注册并输入 Token，自动保存到 `~/.dl-xhs-benchmark/tikhub_config.json`（不在 skill 目录内，不涉及 `config.json`/`config.example.json`），也可以提前设置环境变量 `TIKHUB_API_TOKEN` 跳过交互式引导。

#### 从 Release 下载

也可以直接去 [GitHub Releases](https://github.com/DalinChen255/dl-skill/releases) 下载打包好的 `<skill>.zip`，解压后根级就是 `SKILL.md`。

如果想本地构建，运行 `bash tools/build-skills.sh`，产物在 `dist/skills/`（只会打包 git 跟踪的文件，本地运行产物不会进包）。

## 版本与发布

`VERSION` 文件是唯一权威版本号，`tools/check_versions.py` 会在 CI 和发布时校验它与 `marketplace.json` 里所有版本号一致。发布方式：改 `VERSION` 后打 `v<版本号>` tag 推送，GitHub Actions 会自动构建、校验并创建 Release。

## 许可证

本项目采用 [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) 许可证。

- 个人使用、学习、研究、非商业项目：不需要署名，不需要申请
- 公开发布衍生作品（文章、工具、课程等）：请注明来源
- 商业用途：需要单独授权，请联系作者
