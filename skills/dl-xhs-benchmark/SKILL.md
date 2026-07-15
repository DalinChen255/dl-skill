---
name: dl-xhs-benchmark
description: >
  Use when the user wants to break down a Xiaohongshu (小红书) benchmark blogger's
  account or a specific set of benchmark notes to extract reusable writing patterns
  (title formula, opening, middle structure, ending, CTA, narrative framework) for
  remixing into their own ad/placement notes.
  Trigger on requests such as "拆解这个小红书博主""分析这几篇小红书笔记的套路"
  "对标笔记怎么写的""帮我看看这条小红书链接怎么二创".
---

# dl-xhs-benchmark：小红书对标笔记拆解器

服务对象是要做小红书投放笔记的线索商家：把一批对标内容（一个博主的笔记，或者你自己
挑的几篇笔记）拆解成可复用的写作套路，供你结合自己的业务二次创作。不分析账号是怎么
运营起来的（不看 IP 定位、不看更新节奏），只吃透"这些笔记是怎么写的"。

数据全部来自 TikHub 的公开 REST API（不模拟登录、不注入 Cookie，只读取公开发布的
内容）。涉及评论正文时会先去掉昵称、userId、头像、IP 等身份信息，只保留文字内容用于
分析。

## 先问用户选模式

有两种输入方式，先让用户选一个：

1. **对标博主**：给一个博主主页链接或分享短链，采集这个博主的笔记来分析
2. **指定笔记**：用户直接把要分析的笔记链接发给你，一行一条，这些笔记允许来自不同
   博主——线索商家收藏的对标笔记本来就可能来自多个不同账号，只是套路类似

选完模式后再要对应的输入：

- 模式一：博主链接 + "要采集多少篇"（默认按点赞数取前 20 篇，用户可以自己指定别的
  数量）
- 模式二：提示用户"把要分析的笔记链接发给我，一行一条"，收到后逐条解析

不用问"这是同行对标还是自我诊断"——不管笔记来自谁，处理方式和产出物都是同一套，
没有分支。也不用主动问用户自己的业务是什么、卖点是什么——报告和 Skill 只产出对标
笔记本身的套路规律，用户以后每次要写新笔记，自己在对话里说明业务，让 Skill 现场
套用。

## 会产出什么

1. **一份 HTML 报告**，浏览器打开就能看懂这批笔记的写作套路
2. **一个写作指南 Skill 文件夹**，装好之后 AI 能照着这套写作公式写东西，也能只挪用
   其中几个维度（比如标题公式）、其余保留用户自己的风格

分工上：能靠代码算出来的东西（均赞均藏、标题公式正则分类）都用脚本跑，不占用 AI 的
判断力；需要读懂"这篇笔记为什么这么写"的部分，才交给 AI 去提炼和写成最终产出物。

## 笔记结构层看什么

| 维度 | 具体看什么 |
|------|---------|
| 叙事框架 | 整篇笔记的叙事结构公式（比如痛点型：痛点场景→尝试过程→反转结果→效果对比→CTA），由 AI 读完这批笔记后自己归纳，不是写死的分类表 |
| 标题公式 | 用现成的 5 类正则规则（故事钩子型/数字身份标签型/时间痛点专业术语型/认知冲突反差型/损失厌恶紧迫型）先跑一遍，AI 再结合全文校对 |
| 开头模式 | 每篇怎么起的头（场景代入/反问/数据冲击……） |
| 中间结构 | 内容怎么推进展开的 |
| 结尾方式 | 怎么收尾 |
| CTA 类型 | 引导互动/转化的具体写法（限时/从众/福利……） |

分析流程：单篇标注（每篇笔记都标注上面 6 个维度）→ 按叙事框架分组 → 提炼规律（框架
组内笔记数 ≥ 2 篇时做交叉提炼，只有 1 篇时直接把这篇的标注结果当结论）→ 每个框架挑
1-2 篇高赞代表作做逐句/逐段拆解 → 产出可执行规则。不区分某个写法是博主个人习惯还是
选题不同造成的差异，只要在框架分组里反复出现、值得抄的规律就直接写进最终产出物。

具体展开到什么颗粒度，见 `docs/superpowers/specs/2026-07-15-dl-xhs-benchmark-design.md`
第 4 节。

## 准备工作

- Python 3.9 及以上，不需要装任何第三方包
- 一个 TikHub API Token（注册地址：https://user.tikhub.io/register?ref=QYnybFaK）
- 能访问 api.tikhub.io 的网络环境

首次运行如果没检测到 Token，`check_env.py` 会自己引导你：提示去上面的地址注册、
登录控制台勾选小红书相关的全部端点权限、生成 Token 后粘贴进去，之后自动存到
`~/.dl-xhs-benchmark/tikhub_config.json`。读取顺序是：先看环境变量
`TIKHUB_API_TOKEN`，没有再看本地配置文件，都没有就交互式询问。

## 跑起来的步骤

**Phase 0 · 环境检查**
```bash
python scripts/check_env.py
```
确认 Python 版本和 Token 都就绪。

**Phase 0.5 · 采集（按模式分叉）**

模式一（对标博主）——先扫描博主主页，按点赞数排序：
```bash
python scripts/scan_blogger.py "<博主链接>" -o ./data
```
脚本会报出总篇数，问用户要采集多少篇（默认按点赞取前 20）。

模式二（指定笔记）——用户逐行发链接后，直接采集：
```bash
python scripts/crawl_notes.py "<链接1>" "<链接2>" ... -o ./data
```
不需要 scan 这一步，用户给几条链接就采几条。

**Phase 1 · 采集笔记详情（仅模式一需要，模式二在上一步已经采完）**
```bash
python scripts/crawl_blogger.py <user_id> --count <选定篇数> -o ./data
```
`user_id` 从 `scan_blogger.py` 的输出或 `<链接>_scan.json` 里的 `user_id` 字段拿。
采集中途断了直接重跑同一条命令，会从上次的 checkpoint 接着采，不用从头来。

**Phase 2 · 跑确定性统计**
```bash
python scripts/analyze.py ./data/<file_id>_notes_details.json -o ./data
```
`file_id` 模式一是 `user_id`，模式二是 `crawl_notes.py` 打印出的批次标识（形如
`20260715_5notes_a1b2`）。

**Phase 3 · 生成拆解产出**

先用脚本产出中间稿：
```bash
python scripts/deep_analyze.py ./data/<file_id>_analysis.json "<产出物文件名标识>" \
  -o ./output --scan ./data/<user_id>_scan.json
```
`<产出物文件名标识>`：模式一用博主展示名，模式二用批次标识。`--scan` 只有模式一需要
传（模式二没有 scan.json，不传这个参数）。这一步会写出 `<标识>_数据底稿.md` 和
`<标识>_AI拆解任务.md` 两份文件。

接下来轮到 AI：读 `AI拆解任务.md`，按里面写的五步流程（单篇标注→框架分组→提炼规律→
代表作精读→产出可执行规则）产出结论，再把两个最终产出物写盘，写完一个就落地一个，
不等另一个：

1. 先写写作指南 Skill 文件夹：`<标识>_写作指南.skill/SKILL.md`——必须是个文件夹，
   不能只是一个 `.skill.md` 文件；要同时支持"照搬这批笔记的套路"和"只挪用其中几个
   维度、其余保留用户自己风格"两种用法。
2. 再写 HTML 报告：`<标识>_拆解报告.html`——视觉上走"拆解简报"这套风格（具体规范
   写在 `AI拆解任务.md` 里：近白底 + 靠紫强调色 + 无衬线字体 + 大圆角浮起卡片 + 侧边
   拆解进度轨，框架为一级节点、标题/开头/中间/结尾/CTA 为二级节点），不要套用灰褐
   底色+砖红强调色+无圆角无阴影的工业档案风，也不要套用暖米白+衬线+陶土色这种常见
   AI 生成默认风格。

**Phase 4 · 落地前查一遍质量**

用 `scripts/utils/quality.py` 里的 `check_outputs(output_dir, name)`：
- HTML 报告不能是空文件，正文里要能找到"框架""标题""开头""中间""结尾""CTA"这
  6 个锚点关键词
- Skill 文件夹本身要存在、必须是文件夹，里面的 `SKILL.md` 不能是空的

有一项没过就得补齐，重新跑一遍校验。

## 出问题时怎么办

| 情况 | 怎么处理 |
|------|----------|
| 没设置 TikHub Token | 引导去 https://user.tikhub.io/register?ref=QYnybFaK 注册、输入 Token，自动存下来 |
| 返回 403 权限不足 | 提示去 TikHub 控制台勾选全部小红书相关端点权限 |
| 返回 402/429（余额或限速） | 提示去控制台确认余额；限速客户端已经自适应处理了，一般不用手动管 |
| 链接解析不出来 | 模式一确认是博主主页链接或分享短链；模式二确认是笔记详情链接（含 note_id）或分享短链 |
| 采集中途断了 | 重新跑一遍对应的采集命令（`crawl_blogger.py` 或 `crawl_notes.py`），会自动从 checkpoint 续上 |

## 目录长什么样

```text
dl-xhs-benchmark/
├── SKILL.md
├── run.py
├── install.py
├── scripts/
│   ├── check_env.py
│   ├── scan_blogger.py
│   ├── crawl_blogger.py
│   ├── crawl_notes.py
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

模式一（对标博主）：
```bash
python run.py "<小红书博主链接>" --count 20
```

模式二（指定笔记）：
```bash
python run.py --notes "<链接1>" "<链接2>" ...
```

这一条命令会自动跑完 Phase 0 到 Phase 3 的脚本部分（Step A）；剩下生成 HTML 报告和
Skill 文件夹的部分（Step B），需要宿主 AI 读取 `AI拆解任务.md` 之后接着完成。
