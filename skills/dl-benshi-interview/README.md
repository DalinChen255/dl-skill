# 商业模式诊断

想把自己会的事做成一门小生意，可以先聊最近有人找你帮的一个忙。约二十分钟，AI 会和你一起看清你帮了谁、做了什么、哪些环节你还想再做，再写一份《商业模式诊断报告》。没有卖过东西也能开始。

## 怎么开始

**普通 AI 聊天**　打开[提示词文件](提示词-通用版.md)，复制“提示词正文”下方框里的全部文字，发到你平时用的 AI 的新对话。可以打字，也可以用语音回答。想中途停一下，就说“先停”，保留它给的续接卡。

**Claude Code 插件**　第一次使用这个市场，在终端依次运行下面两行。以前添加过市场的人，把第一行换成 `claude plugin marketplace update dl-skill`。装好后，在新对话里说“帮我找找我能卖什么”。

```bash
claude plugin marketplace add DalinChen255/dl-skill
claude plugin install dl-benshi-interview@dl-skill
```

**手动下载**　[下载 dl-benshi-interview.zip](https://github.com/DalinChen255/dl-skill/releases/latest/download/dl-benshi-interview.zip)，把里面的文件一起解压到名为 `dl-benshi-interview` 的文件夹，打开这一层就能看到 `SKILL.md`。

- 用普通 AI，打开同一文件夹里的 `提示词-通用版.md`，按上面的说明复制正文。
- 用 Codex，把整个文件夹放进 `~/.agents/skills/`。用 Claude Code，把整个文件夹放进 `~/.claude/skills/`。新开对话，说“帮我找找我能卖什么”。保留文件夹里的[访谈规则](SKILL.md)、[诊断依据](测评题和类型.md)和[报告模板](报告模板.html)。

## 报告里有什么

先读首页的诊断结论，看看眼下最值得试什么、依据在哪里。完整报告把你的经历和做法放在一起对照，用一张图说明生意如何运转，再把产品、价格、交付和每单工时摆出来比较。风险、备选和行动处方各自成节。想先看个大概，也可以要一页简报。

试过一单，把客户的回应和自己做完的感受带回来。AI 会陪你看下次该改范围、价格、交付方式，还是另试一个方向。

## 版本

工具合集 [v1.3.0](https://github.com/DalinChen255/dl-skill/releases/tag/v1.3.0) 首次将商业模式诊断第七版打包成 ZIP，并加入 Claude Code 插件市场。

## 授权

CC BY-NC 4.0

个人和学习：不需要署名，不需要申请  
公开发布衍生作品：请注明来源  
商业用途：需要单独授权，联系作者
