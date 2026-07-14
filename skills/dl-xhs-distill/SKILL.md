---
name: dl-xhs-distill
description: >
  Use when the user wants to analyze or distill a Xiaohongshu (小红书) blogger/account
  from a profile link, benchmark that blogger's style, or generate a creation skill
  that mimics or blends with that blogger's style.
  Trigger on requests such as "蒸馏这个小红书博主""拆解小红书博主""分析小红书账号"
  "对标这个小红书博主""帮我分析这个小红书链接".
---

# dl-xhs-distill：小红书博主蒸馏器

把一个小红书博主主页链接，变成一份可读的分析报告和一份可复用的创作能力。
数据全部来自 TikHub 的公开 REST API（不模拟登录、不注入 Cookie，只读取博主本人
已公开发布的内容）。涉及评论正文时会先去掉昵称、userId、头像、IP 等身份信息，
只保留文字内容用于分析。

## 触发这个技能前，先拿到两个答案

用户必须自己说清楚这两点，AI 不能替用户猜：

1. 要分析哪个博主——主页链接或者分享短链
2. 采集多少篇——快速 / 推荐 / 深度三档里选一个；如果用户还没想好，先跑一遍
   `scan_blogger.py` 把三档各自对应的具体篇数报出来，再让用户挑

不用问"这是要对标学习还是自我诊断"——不管链接指向谁的账号，处理方式和产出物
都是同一套，没有分支。

## 会产出什么

1. **一份 HTML 报告**，浏览器打开就能看懂这个博主的账号定位、运营节奏、内容套路
2. **一个创作 Skill 文件夹**，装好之后 AI 能照着这个博主的风格写东西，也能只挪用
   其中几个维度（比如标题公式）、其余保留用户自己的风格

分工上：能靠代码算出来的东西（发布频率、标题分类、互动数据统计）都用脚本跑，
不占用 AI 的判断力；需要读懂"这个博主为什么这么做"的部分，才交给 AI 去提炼和
写成最终产出物。

## 三层拆解看什么

| 层级 | 具体看什么 |
|------|---------|
| IP 定位 | 账号怎么搭建的 / 定位三问（面向谁、解决什么问题、卖什么）/ 差异化人设 / 叙事逻辑 / 商业模式 |
| 运营策略 | 更新频率、发布时段 / 账号处在哪个阶段 |
| 内容形式 | 全量笔记的标题公式归类 / 内容里的钩子-情绪锚点-认知缺口-CTA 四个原子怎么摆位 / 挑几篇代表作做跨笔记对比精读 |

每一项具体展开到什么颗粒度，见 `docs/2026-07-14-dl-xhs-distill-design.md` 第三节。

## 准备工作

- Python 3.9 及以上，不需要装任何第三方包
- 一个 TikHub API Token（注册地址：https://user.tikhub.io/register?ref=QYnybFaK）
- 能访问 api.tikhub.io 的网络环境

首次运行如果没检测到 Token，`check_env.py` 会自己引导你：提示去上面的地址注册、
登录控制台勾选小红书相关的全部端点权限、生成 Token 后粘贴进去，之后自动存到
`~/.dl-xhs-distill/tikhub_config.json`。读取顺序是：先看环境变量
`TIKHUB_API_TOKEN`，没有再看本地配置文件，都没有就交互式询问。

## 跑起来的步骤

**Phase 0 · 环境检查**
```bash
python scripts/check_env.py
```
确认 Python 版本和 Token 都就绪。

**Phase 0.5 · 问清楚要采多少**

先扫描博主主页拿到笔记总数：
```bash
python scripts/scan_blogger.py "<链接>" -o ./data
```
脚本会算出快速（1/3）、推荐（1/2）、深度（全量）三档各自对应的具体篇数，交给
用户挑一档。

**Phase 1 · 采集笔记详情**
```bash
python scripts/crawl_blogger.py <user_id> --count <选定篇数> -o ./data
```
`user_id` 从上一步的输出或 `<链接>_scan.json` 里的 `user_id` 字段拿。采集中途断了
直接重跑同一条命令，会从上次的 checkpoint 接着采，不用从头来。

**Phase 2 · 跑确定性统计**
```bash
python scripts/analyze.py ./data/<user_id>_notes_details.json -o ./data
```

**Phase 3 · 生成蒸馏产出**

先用脚本产出中间稿：
```bash
python scripts/deep_analyze.py ./data/<user_id>_analysis.json "<博主展示名>" \
  -o ./output --scan ./data/<user_id>_scan.json
```
这一步会写出 `<博主展示名>_数据底稿.md` 和 `<博主展示名>_AI蒸馏任务.md` 两份文件。

接下来轮到 AI：读 `AI蒸馏任务.md`，按下面的顺序把两个最终产出物写盘，写完一个
就落地一个，不等另一个：

1. 先写创作 Skill 文件夹：`<博主展示名>_创作指南.skill/SKILL.md`——必须是个
   文件夹，不能只是一个 `.skill.md` 文件；要同时支持"照搬这个博主的风格"和
   "只挪用其中几个维度、其余保留用户自己风格"两种用法。
2. 再写 HTML 报告：`<博主展示名>_蒸馏报告.html`——视觉上走"蒸馏简报"这套风格
   （具体规范写在 `AI蒸馏任务.md` 里：近白底 + 靠紫强调色 + 无衬线字体 + 大圆角
   浮起卡片 + 侧边蒸馏进度轨），不要套用灰褐底色+砖红强调色+无圆角无阴影的工业
   档案风，也不要套用暖米白+衬线+陶土色这种常见 AI 生成默认风格。

**Phase 4 · 落地前查一遍质量**

用 `scripts/utils/quality.py` 里的 `check_outputs(output_dir, blogger_name)`：
- HTML 报告不能是空文件，正文里要能找到"IP定位"「运营策略」「内容形式」这三个
  锚点关键词
- Skill 文件夹本身要存在、必须是文件夹、里面的 `SKILL.md` 不能是空的

有一项没过就得补齐，重新跑一遍校验。

## 出问题时怎么办

| 情况 | 怎么处理 |
|------|---------|
| 没设置 TikHub Token | 引导去 https://user.tikhub.io/register?ref=QYnybFaK 注册、输入 Token，自动存下来 |
| 返回 403 权限不足 | 提示去 TikHub 控制台勾选全部小红书相关端点权限 |
| 返回 402/429（余额或限速） | 提示去控制台确认余额；限速客户端已经自适应处理了，一般不用手动管 |
| 链接解析不出来 | 提示确认格式——主页链接或分享短链都行，但得包含一个有效的用户标识 |
| 采集中途断了 | 重新跑一遍 `crawl_blogger.py` 那条命令，会自动从 checkpoint 续上 |

## 目录长什么样

```text
dl-xhs-distill/
├── SKILL.md
├── run.py
├── install.py
├── scripts/
│   ├── check_env.py
│   ├── scan_blogger.py
│   ├── crawl_blogger.py
│   ├── analyze.py
│   ├── deep_analyze.py
│   └── utils/
│       ├── common.py
│       ├── tikhub_client.py
│       └── quality.py
├── tests/
└── references/
    └── 产出物质量标杆.md
```

## 想省事直接一键跑

```bash
python run.py "<小红书博主链接>" --tier 推荐
```

这一条命令会自动跑完 Phase 0 到 Phase 3 的脚本部分（Step A）；剩下生成 HTML 报告
和 Skill 文件夹的部分（Step B），需要宿主 AI 读取 `AI蒸馏任务.md` 之后接着完成。
