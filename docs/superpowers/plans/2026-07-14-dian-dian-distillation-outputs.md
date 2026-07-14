# 点点小红书账号蒸馏产出 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 基于已采集的 11 篇公开笔记，生成强模仿型创作 Skill 与单文件 HTML 蒸馏报告，并通过结构、内容和视觉验证。

**Architecture:** 数据底稿是唯一事实来源，创作 Skill 将事实翻译为可执行写作规则，HTML 报告将相同证据组织成三层诊断。两个产物独立生成、独立检查，最后统一运行仓库现有质量函数并做浏览器视觉检查。

**Tech Stack:** Markdown、单文件 HTML5、手写 CSS、原生 JavaScript、Python 3.9 质量脚本。

## Global Constraints

- 样本固定为点点账号已采集的 11 篇公开图文笔记。
- 用户选择“强模仿型”，创作指南默认 1:1 模仿，同时必须支持融合风格模式。
- 不把单篇高互动直接写成稳定爆款规律，不编造简介、头像设计或商业模式。
- HTML 使用 `#FAFAF9` 背景、`#5546FF` 唯一强调色、`#ECE9FF` 浅强调、`#16151A` 正文、`#6E6B76` 次要文字和 `#E5E3E0` 分割线。
- HTML 是单文件，手写 CSS 与原生 JavaScript，不使用 Tailwind 或其他外部框架。
- 输出路径固定为 `skills/dl-xhs-distill/output/点点_创作指南.skill/SKILL.md` 和 `skills/dl-xhs-distill/output/点点_蒸馏报告.html`。

---

### Task 1: 生成强模仿型创作 Skill

**Files:**
- Read: `skills/dl-xhs-distill/output/点点_数据底稿.md`
- Read: `skills/dl-xhs-distill/output/点点_AI蒸馏任务.md`
- Create: `skills/dl-xhs-distill/output/点点_创作指南.skill/SKILL.md`

**Interfaces:**
- Consumes: 账号基础信息、11 篇统计、TOP10 标题与正文摘录、发布节奏。
- Produces: 包含两种调用方式、七个混合维度、五类标题公式、四原子规则和限制条件的 Markdown 创作规则。

- [ ] **Step 1: 创建目标目录**

Run: `mkdir -p skills/dl-xhs-distill/output/点点_创作指南.skill`

Expected: 目录存在，且没有生成同名 `.skill.md` 文件。

- [ ] **Step 2: 写入创作指南**

使用 `apply_patch` 新建 `SKILL.md`。章节固定为：调用方式、证据边界、IP定位、运营策略、五类标题公式、四原子、恒定风格、主题适应性、七维融合表、生成流程、发布前检查。五类公式固定为开放求助型、争议判断型、脆弱暴露型、现实落差型、事件暗号型。

- [ ] **Step 3: 校验 Skill 结构与关键内容**

Run:

```bash
test -d skills/dl-xhs-distill/output/点点_创作指南.skill
test -s skills/dl-xhs-distill/output/点点_创作指南.skill/SKILL.md
rg -n '1:1 模仿模式|融合风格模式|标题公式|开头模板|正文结构|情感节奏|语言习惯|CTA 策略|发布节奏建议' skills/dl-xhs-distill/output/点点_创作指南.skill/SKILL.md
```

Expected: 两个 `test` 命令退出码为 0，`rg` 命中所有调用方式与七个维度。

### Task 2: 生成单文件 HTML 蒸馏报告

**Files:**
- Read: `skills/dl-xhs-distill/output/点点_数据底稿.md`
- Read: `skills/dl-xhs-distill/output/点点_AI蒸馏任务.md`
- Create: `skills/dl-xhs-distill/output/点点_蒸馏报告.html`

**Interfaces:**
- Consumes: Task 1 已确认的五类标题公式命名，以及数据底稿全部事实。
- Produces: 浏览器可直接打开的报告，DOM 锚点固定为 `overview`、`ip`、`operations`、`content`。

- [ ] **Step 1: 写入 HTML 语义结构与正文**

使用 `apply_patch` 新建单文件 HTML。正文固定包含首屏证据提醒、核心数字、IP定位、运营策略、内容形式、TOP10 逐条标题分类、四原子、跨笔记恒定特征、主题适应性特征和使用限制。TOP10 每一篇都展示标题、互动数据、公式类别和分类理由。

- [ ] **Step 2: 写入视觉系统与响应式布局**

在同一文件内写入 CSS 变量、16px 圆角白卡、柔和阴影、桌面端侧边进度轨、768px 断点下的移动端顶部进度条。中文字体栈以 `Noto Sans SC` 为首选，数字与标题以 `Manrope` 为首选，英文标签以 `Inter` 为首选，并提供系统无衬线回退。

- [ ] **Step 3: 写入克制的原生交互**

在同一文件内写入原生 JavaScript：首屏标题浮现、核心数字从 0 计数到目标值、`IntersectionObserver` 驱动三层导航高亮、滚动位置驱动移动端顶部进度条。脚本在缺少 `IntersectionObserver` 时保持静态内容可用。

- [ ] **Step 4: 校验 HTML 结构与禁用项**

Run:

```bash
test -s skills/dl-xhs-distill/output/点点_蒸馏报告.html
rg -n 'IP定位|运营策略|内容形式|<details|IntersectionObserver|@media \(max-width: 768px\)' skills/dl-xhs-distill/output/点点_蒸馏报告.html
! rg -n 'tailwind|bootstrap|砖红|陶土' skills/dl-xhs-distill/output/点点_蒸馏报告.html
```

Expected: 文件非空，所有必需元素有命中，禁用项无命中。

### Task 3: 运行质量与视觉验证

**Files:**
- Inspect: `skills/dl-xhs-distill/output/点点_创作指南.skill/SKILL.md`
- Inspect: `skills/dl-xhs-distill/output/点点_蒸馏报告.html`
- Use: `skills/dl-xhs-distill/scripts/utils/quality.py`

**Interfaces:**
- Consumes: Tasks 1-2 的最终文件。
- Produces: 质量校验结果、桌面端与移动端渲染检查结论。

- [ ] **Step 1: 调用仓库质量函数**

Run:

```bash
cd skills/dl-xhs-distill
python3 - <<'PY'
from scripts.utils.quality import check_outputs
print(check_outputs('./output', '点点'))
PY
```

Expected: 返回值表示 HTML、Skill 目录和 `SKILL.md` 均通过；如函数返回布尔值，应为 `True`。

- [ ] **Step 2: 启动本地静态服务器并检查渲染**

Run: `cd skills/dl-xhs-distill/output && python3 -m http.server 8765`

Expected: 浏览器访问报告 URL 返回 200；桌面端无横向溢出，侧边进度轨可点击，移动端隐藏侧轨并显示顶部进度条。

- [ ] **Step 3: 最终一致性核对**

Run:

```bash
rg -n '10 位粉丝|11 篇|28\.82|11\.45|47\.55|0\.397' skills/dl-xhs-distill/output/点点_蒸馏报告.html
rg -n '暂未形成|无法确认|起步期|强模仿' skills/dl-xhs-distill/output/点点_创作指南.skill/SKILL.md skills/dl-xhs-distill/output/点点_蒸馏报告.html
```

Expected: 核心数字与证据边界在最终产物中一致，不出现与数据底稿冲突的结论。
