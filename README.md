# dl-skill

陈大Lin的开源 Agent skill 合集。可在 Claude Code、Codex 等任意支持 skill / system prompt 的 Agent 上使用。

## 工具箱

| Skill | 做什么 |
|---|---|
| `dl-xhs-winback` | 小红书专业号私信通每日客户跟进/召回：从线索表和私信历史里筛出已留资、非同行、24 小时内无回复、无负面的客户，逐条复核后发送跟进私信，避免误伤已转化或已拒绝的客户 |

每个 skill 目录下都有自己的 `SKILL.md`（含使用说明和授权门禁）和 `config.example.json`（配置示例）。

## 如何安装

#### Claude Code

```bash
claude plugin marketplace add DalinChen255/dl-skill
claude plugin install dl-xhs-winback@dl-skill
```

#### 手动安装（适用于 Claude Code / Codex）

把对应 skill 目录整个复制到你本地的 skill 目录下即可，例如：

```bash
git clone https://github.com/DalinChen255/dl-skill.git
cp -R dl-skill/skills/dl-xhs-winback ~/.claude/skills/dl-xhs-winback
# 或 Codex: cp -R dl-skill/skills/dl-xhs-winback ~/.codex/skills/dl-xhs-winback
```

首次使用前，先在 skill 目录里把 `config.example.json` 复制一份改名为 `config.json`，填入你自己的账号 ID/昵称、工作区路径——`config.json` 已加入 `.gitignore`，不会被提交，你的真实配置只留在本地。

#### 从 Release 下载

也可以直接去 [GitHub Releases](https://github.com/DalinChen255/dl-skill/releases) 下载打包好的 `<skill>.zip`，解压后根级就是 `SKILL.md`。

如果想本地构建，运行 `bash tools/build-skills.sh`，产物在 `dist/skills/`。

## 许可证

本项目采用 [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) 许可证。

- 个人使用、学习、研究、非商业项目：不需要署名，不需要申请
- 公开发布衍生作品（文章、工具、课程等）：请注明来源
- 商业用途：需要单独授权，请联系作者
