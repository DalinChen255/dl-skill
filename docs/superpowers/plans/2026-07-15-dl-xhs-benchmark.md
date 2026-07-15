# dl-xhs-benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild `dl-xhs-distill` into `dl-xhs-benchmark` — from "distill a blogger's whole account" to "break down benchmark note writing patterns (title/opening/middle/ending/CTA/narrative framework) so lead merchants can remix them into their own ad notes", with two collection entry points (single blogger vs. user-specified note links).

**Architecture:** Single skill, two collection entry points, shared downstream pipeline. Collection diverges (`scan_blogger.py`+`crawl_blogger.py` for mode one, new `crawl_notes.py` for mode two) but both produce the same `notes_details.json` shape, so `analyze.py` → `deep_analyze.py` → quality check → AI-authored report/Skill folder are mode-agnostic.

**Tech Stack:** Python 3.9+ standard library only (no third-party packages), TikHub REST API (`api.tikhub.io`), pytest for tests.

## Global Constraints

- Python 3.9+, standard library only — no third-party dependencies anywhere in `skills/dl-xhs-benchmark/`
- TikHub API token required; all HTTP calls go through `scripts/utils/tikhub_client.py`'s existing client, unchanged
- Drop the old IP定位/运营策略 (account-level) analysis layer entirely — do not reintroduce it, simplified or otherwise
- Do not collect or persist the user's own business info (product/selling points/target audience) anywhere in the pipeline
- Rename sweep: directory `skills/dl-xhs-distill/` → `skills/dl-xhs-benchmark/`; all "蒸馏" wording in code/docs/output filenames → "拆解"
- Token config path becomes `~/.dl-xhs-benchmark/tikhub_config.json`; do not write migration code from the old `~/.dl-xhs-distill/` path
- Mode two (user-specified notes) allows links from different bloggers; do not add cross-blogger consistency logic — treat all analyzed notes as one pool
- Final artifact filenames (exact, used by both `deep_analyze.py` and `quality.py`, must match byte-for-byte): `{name}_数据底稿.md`, `{name}_AI拆解任务.md`, `{name}_拆解报告.html`, `{name}_写作指南.skill/SKILL.md`
- HTML report visual spec (colors `#FAFAF9`/`#5546FF`/`#ECE9FF`/`#16151A`/`#6E6B76`/`#E5E3E0`, fonts Manrope/Noto Sans SC/Inter, 16px rounded cards, single-file hand-written CSS) stays unchanged — only the sidebar progress-track structure and style name change

---

## Task 1: Rename skill directory + Token config path

**Files:**
- Modify (via `git mv`): `skills/dl-xhs-distill/` → `skills/dl-xhs-benchmark/` (entire directory)
- Modify: `skills/dl-xhs-benchmark/scripts/utils/common.py`
- Modify: `skills/dl-xhs-benchmark/install.py`
- Modify: `skills/dl-xhs-benchmark/scripts/utils/tikhub_client.py`
- Test: `skills/dl-xhs-benchmark/tests/test_common.py` (existing tests must still pass, no new tests needed for this task)

**Interfaces:**
- Produces: `common.CONFIG_DIR = Path.home() / ".dl-xhs-benchmark"`, `common.CONFIG_FILE = CONFIG_DIR / "tikhub_config.json"` — every later task that touches `common.py` builds on this.

- [ ] **Step 1: Rename the directory with git mv**

```bash
git mv skills/dl-xhs-distill skills/dl-xhs-benchmark
```

- [ ] **Step 2: Update `common.py`'s config path and docstring**

In `skills/dl-xhs-benchmark/scripts/utils/common.py`, change:

```python
"""dl-xhs-distill 共用工具：链接解析、动态采集档位、配置与 JSON 读写。"""
```

to:

```python
"""dl-xhs-benchmark 共用工具：链接解析、配置与 JSON 读写。"""
```

and change:

```python
CONFIG_DIR = Path.home() / ".dl-xhs-distill"
```

to:

```python
CONFIG_DIR = Path.home() / ".dl-xhs-benchmark"
```

- [ ] **Step 3: Update `install.py` wording**

In `skills/dl-xhs-benchmark/install.py`, change:

```python
"""dl-xhs-distill 环境安装脚本。本工具仅依赖 Python 标准库，无需安装第三方包。"""
```

to:

```python
"""dl-xhs-benchmark 环境安装脚本。本工具仅依赖 Python 标准库，无需安装第三方包。"""
```

and change:

```python
    print("✅ dl-xhs-distill 仅依赖 Python 标准库，无需额外安装依赖")
```

to:

```python
    print("✅ dl-xhs-benchmark 仅依赖 Python 标准库，无需额外安装依赖")
```

- [ ] **Step 4: Update `tikhub_client.py` docstring**

In `skills/dl-xhs-benchmark/scripts/utils/tikhub_client.py`, change:

```python
"""dl-xhs-distill 的 TikHub REST 客户端，仅覆盖小红书三个端点，主选+备选两级降级。"""
```

to:

```python
"""dl-xhs-benchmark 的 TikHub REST 客户端，仅覆盖小红书三个端点，主选+备选两级降级。"""
```

- [ ] **Step 5: Run the existing test suite to confirm the rename didn't break anything**

```bash
cd skills/dl-xhs-benchmark && python3 -m pytest tests/ -v
```

Expected: all existing tests still PASS (imports are relative to `tests/__init__`-style `sys.path.insert` based on `__file__`, unaffected by the parent directory rename).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: rename dl-xhs-distill to dl-xhs-benchmark, retarget token config path"
```

---

## Task 2: common.py — drop tier helper, add note-link parser

**Files:**
- Modify: `skills/dl-xhs-benchmark/scripts/utils/common.py`
- Test: `skills/dl-xhs-benchmark/tests/test_common.py`

**Interfaces:**
- Consumes: nothing new
- Produces: `common.parse_xhs_note_link(url: str) -> dict` returning `{"note_id": str, "xsec_token": str}`, raises `ValueError` if the link doesn't contain a parseable note ID. Removes `common.compute_collection_tiers` (no longer exists — later tasks must not call it).

- [ ] **Step 1: Write the failing tests**

In `skills/dl-xhs-benchmark/tests/test_common.py`, replace the three `compute_collection_tiers` tests:

```python
def test_compute_collection_tiers_normal():
    tiers = common.compute_collection_tiers(90)
    assert tiers == {"快速": 30, "推荐": 45, "深度": 90}


def test_compute_collection_tiers_small_total_floors_at_one():
    tiers = common.compute_collection_tiers(2)
    assert tiers == {"快速": 1, "推荐": 1, "深度": 2}


def test_compute_collection_tiers_zero_total():
    tiers = common.compute_collection_tiers(0)
    assert tiers == {"快速": 0, "推荐": 0, "深度": 0}
```

with:

```python
def test_parse_xhs_note_link_explore_url():
    url = "https://www.xiaohongshu.com/explore/64f1234567890abcdef12345?xsec_token=ABC123&xsec_source=pc_feed"
    result = common.parse_xhs_note_link(url)
    assert result == {"note_id": "64f1234567890abcdef12345", "xsec_token": "ABC123"}


def test_parse_xhs_note_link_discovery_item_url_no_token():
    url = "https://www.xiaohongshu.com/discovery/item/64f1234567890abcdef12345"
    result = common.parse_xhs_note_link(url)
    assert result == {"note_id": "64f1234567890abcdef12345", "xsec_token": ""}


def test_parse_xhs_note_link_invalid_raises():
    try:
        common.parse_xhs_note_link("https://example.com/not-a-note")
        assert False, "expected ValueError"
    except ValueError:
        pass
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd skills/dl-xhs-benchmark && python3 -m pytest tests/test_common.py -v
```

Expected: FAIL with `AttributeError: module 'scripts.utils.common' has no attribute 'parse_xhs_note_link'` (and the old tier tests are already gone from the file, so they no longer run).

- [ ] **Step 3: Implement `parse_xhs_note_link`, remove `compute_collection_tiers`**

In `skills/dl-xhs-benchmark/scripts/utils/common.py`:

Add `import urllib.parse` next to the existing `urllib` imports at the top:

```python
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
```

Add a note-link regex next to `_PROFILE_RE`:

```python
_PROFILE_RE = re.compile(r"xiaohongshu\.com/user/profile/([0-9a-fA-F]{20,32})")
_NOTE_RE = re.compile(r"xiaohongshu\.com/(?:explore|discovery/item)/([0-9a-fA-F]{20,32})")
_SHORT_LINK_RE = re.compile(r"xhslink\.com/")
```

Replace the `compute_collection_tiers` function:

```python
def compute_collection_tiers(total_notes: int) -> dict:
    """按总笔记数计算三档动态采集量：快速=1/3，推荐=1/2，深度=全量。"""
    if total_notes <= 0:
        return {"快速": 0, "推荐": 0, "深度": 0}
    fast = max(1, total_notes // 3)
    recommended = max(1, total_notes // 2)
    return {"快速": fast, "推荐": recommended, "深度": total_notes}
```

with:

```python
def parse_xhs_note_link(url: str) -> dict:
    """从小红书笔记链接或分享短链中提取 note_id 与 xsec_token。"""
    resolved = resolve_short_link(url)
    match = _NOTE_RE.search(resolved)
    if not match:
        raise ValueError(
            f"无法从链接中解析出小红书笔记 ID：{url}\n"
            "请确认链接是笔记详情链接（形如 https://www.xiaohongshu.com/explore/<id>）"
            "或分享短链（形如 http://xhslink.com/xxxx）"
        )
    note_id = match.group(1)
    parsed = urllib.parse.urlparse(resolved)
    query = urllib.parse.parse_qs(parsed.query)
    xsec_token = query.get("xsec_token", [""])[0]
    return {"note_id": note_id, "xsec_token": xsec_token}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd skills/dl-xhs-benchmark && python3 -m pytest tests/test_common.py -v
```

Expected: PASS (all `test_parse_xhs_note_link_*` tests green; no leftover references to `compute_collection_tiers` remain in the test file).

- [ ] **Step 5: Commit**

```bash
git add skills/dl-xhs-benchmark/scripts/utils/common.py skills/dl-xhs-benchmark/tests/test_common.py
git commit -m "feat: replace collection-tier helper with note-link parser in common.py"
```

---

## Task 3: scan_blogger.py — sort by likes instead of tiering; crawl_blogger.py wording cleanup

**Files:**
- Modify: `skills/dl-xhs-benchmark/scripts/scan_blogger.py`
- Modify: `skills/dl-xhs-benchmark/scripts/crawl_blogger.py`
- Create: `skills/dl-xhs-benchmark/tests/test_scan_blogger.py`

**Interfaces:**
- Consumes: nothing new
- Produces: `scan_blogger.sort_by_likes(notes_list: list) -> list` (pure, testable); `scan_blogger.scan()`'s output JSON drops the `"tiers"` key entirely, and `"notes_list"` is now sorted by `liked_count` descending. `crawl_blogger.crawl(user_id, count, output_dir)`'s second parameter is renamed from `tier_count` to `count` (same behavior, no signature-breaking change for callers since it's positional-compatible — Task 8's `run.py` calls it via CLI flags, not by keyword).

- [ ] **Step 1: Write the failing test for the new sort helper**

Create `skills/dl-xhs-benchmark/tests/test_scan_blogger.py`:

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts import scan_blogger


def test_sort_by_likes_orders_descending():
    notes = [
        {"note_id": "1", "liked_count": 50},
        {"note_id": "2", "liked_count": 300},
        {"note_id": "3", "liked_count": 120},
    ]
    result = scan_blogger.sort_by_likes(notes)
    assert [n["note_id"] for n in result] == ["2", "3", "1"]


def test_sort_by_likes_handles_missing_liked_count():
    notes = [{"note_id": "1"}, {"note_id": "2", "liked_count": 10}]
    result = scan_blogger.sort_by_likes(notes)
    assert [n["note_id"] for n in result] == ["2", "1"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd skills/dl-xhs-benchmark && python3 -m pytest tests/test_scan_blogger.py -v
```

Expected: FAIL with `AttributeError: module 'scripts.scan_blogger' has no attribute 'sort_by_likes'`.

- [ ] **Step 3: Rewrite `scan_blogger.py`**

Replace the entire contents of `skills/dl-xhs-benchmark/scripts/scan_blogger.py` with:

```python
"""Phase 0.5: 解析链接 + 分页扫描博主全部笔记 → 按点赞数排序，供后续按数量截取。"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils import common
from scripts.utils.tikhub_client import TikHubClient, TikHubError

DEFAULT_TOP_N = 20


def sort_by_likes(notes_list: list) -> list:
    """按点赞数从高到低排序，供 crawl_blogger.py 按 --count 截取高赞笔记。"""
    return sorted(notes_list, key=lambda n: n.get("liked_count", 0), reverse=True)


def scan(link: str, output_dir: str) -> dict:
    user_id = common.parse_xhs_user_link(link)
    client = TikHubClient()

    profile = client.fetch_user_info(user_id)

    notes_list = []
    cursor = ""
    while True:
        page = client.fetch_user_notes(user_id, cursor=cursor)
        notes_list.extend(page["notes"])
        if not page["has_more"] or not page.get("cursor"):
            break
        cursor = page["cursor"]

    notes_list = sort_by_likes(notes_list)
    total_notes = len(notes_list)

    result = {
        "user_id": user_id,
        "profile": profile,
        "notes_list": notes_list,
        "total_notes": total_notes,
    }
    out_path = Path(output_dir) / f"{user_id}_scan.json"
    common.save_json(out_path, result)
    return result


def main():
    parser = argparse.ArgumentParser(description="扫描小红书博主全部笔记并按点赞数排序")
    parser.add_argument("link", help="博主主页链接或分享短链")
    parser.add_argument("-o", "--output", default="./data", help="输出目录，默认 ./data")
    args = parser.parse_args()

    try:
        result = scan(args.link, args.output)
    except ValueError as exc:
        print(f"❌ {exc}")
        sys.exit(1)
    except TikHubError as exc:
        print(f"❌ TikHub 请求失败（HTTP {exc.status_code}）：{exc}")
        sys.exit(1)

    profile = result["profile"]
    print(f"✅ 博主：{profile.get('nickname', '(未知昵称)')}（{result['user_id']}）")
    print(f"✅ 共扫描到 {result['total_notes']} 篇笔记，已按点赞数从高到低排序")
    print(f"请告诉我要采集多少篇（默认按点赞数取前 {DEFAULT_TOP_N} 篇，也可以自己指定数量）")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd skills/dl-xhs-benchmark && python3 -m pytest tests/test_scan_blogger.py -v
```

Expected: PASS.

- [ ] **Step 5: Clean up stale "档位" wording in `crawl_blogger.py`**

In `skills/dl-xhs-benchmark/scripts/crawl_blogger.py`, change the docstring:

```python
"""Phase 1: 按选定档位采集笔记详情，每 10 条自动 checkpoint。"""
```

to:

```python
"""Phase 1: 按指定篇数采集笔记详情，每 10 条自动 checkpoint。"""
```

Rename the `tier_count` parameter to `count` throughout `crawl()`:

```python
def crawl(user_id: str, tier_count: int, output_dir: str) -> list:
    scan_path = Path(output_dir) / f"{user_id}_scan.json"
    if not scan_path.exists():
        raise FileNotFoundError(f"找不到扫描结果 {scan_path}，请先运行 scan_blogger.py")
    scan_data = common.load_json(scan_path)
    target_notes = scan_data["notes_list"][:tier_count]
```

to:

```python
def crawl(user_id: str, count: int, output_dir: str) -> list:
    scan_path = Path(output_dir) / f"{user_id}_scan.json"
    if not scan_path.exists():
        raise FileNotFoundError(f"找不到扫描结果 {scan_path}，请先运行 scan_blogger.py")
    scan_data = common.load_json(scan_path)
    target_notes = scan_data["notes_list"][:count]
```

And update the `main()` call site and argparse help text:

```python
    parser.add_argument("--count", type=int, required=True, help="本次采集的笔记篇数（选定档位对应的具体数字）")
```

to:

```python
    parser.add_argument("--count", type=int, required=True, help="本次要采集的笔记篇数")
```

and:

```python
    try:
        details = crawl(args.user_id, args.count, args.output)
```

stays the same (positional call site unaffected by the parameter rename).

- [ ] **Step 6: Run the full test suite**

```bash
cd skills/dl-xhs-benchmark && python3 -m pytest tests/ -v
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add skills/dl-xhs-benchmark/scripts/scan_blogger.py skills/dl-xhs-benchmark/scripts/crawl_blogger.py skills/dl-xhs-benchmark/tests/test_scan_blogger.py
git commit -m "feat: sort scanned notes by likes instead of three-tier split"
```

---

## Task 4: analyze.py — drop publish-rhythm stats, replace TOP10 with full-text all_notes

**Files:**
- Modify: `skills/dl-xhs-benchmark/scripts/analyze.py`
- Modify: `skills/dl-xhs-benchmark/tests/test_analyze.py`

**Interfaces:**
- Consumes: nothing new
- Produces: `analyze.analyze(details: list) -> dict` with keys `total, avg_liked, avg_collected, avg_comment, collect_like_ratio, image_video_ratio, title_formula_distribution, tag_frequency, all_notes` — `all_notes` is the full `details` list (untruncated `desc` text, all fields preserved) sorted by `liked_count` descending. `top10` and `publish_rhythm` no longer exist in the return value; `classify_title_formula` is unchanged.

- [ ] **Step 1: Write the failing tests**

In `skills/dl-xhs-benchmark/tests/test_analyze.py`, replace `test_analyze_computes_averages_and_top10`:

```python
def test_analyze_computes_averages_and_top10():
    result = analyze.analyze(_sample_notes())
    assert result["total"] == 2
    assert result["avg_liked"] == 200.0
    assert result["top10"][0]["note_id"] == "2"
    assert result["image_video_ratio"] == {"image": 1, "video": 1}
    assert {"tag": "猫粮", "count": 2} in result["tag_frequency"]
```

with:

```python
def test_analyze_computes_averages_and_all_notes_sorted_by_likes():
    result = analyze.analyze(_sample_notes())
    assert result["total"] == 2
    assert result["avg_liked"] == 200.0
    assert result["all_notes"][0]["note_id"] == "2"
    assert result["image_video_ratio"] == {"image": 1, "video": 1}
    assert {"tag": "猫粮", "count": 2} in result["tag_frequency"]
    assert "publish_rhythm" not in result
    assert "top10" not in result


def test_analyze_keeps_full_desc_text_untruncated():
    long_desc = "x" * 500
    notes = [{
        "note_id": "1", "title": "标题", "desc": long_desc, "liked_count": 10,
        "collected_count": 1, "comment_count": 1, "tags": [], "image_count": 1,
        "has_video": False, "create_time": 1700000000,
    }]
    result = analyze.analyze(notes)
    assert result["all_notes"][0]["desc"] == long_desc
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd skills/dl-xhs-benchmark && python3 -m pytest tests/test_analyze.py -v
```

Expected: FAIL — `KeyError: 'all_notes'` (current code still returns `top10`/`publish_rhythm`).

- [ ] **Step 3: Rewrite `analyze.py`**

Replace the entire contents of `skills/dl-xhs-benchmark/scripts/analyze.py` with:

```python
"""Phase 2: 全量统计 + 标题公式分类，笔记全文交给 AI 做框架/结构拆解。"""

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils import common

_STORY_HOOK_PATTERNS = [r"那天", r"直到.*才", r"后来", r"没想到", r"结果发生了", r"故事"]
_NUMBER_IDENTITY_PATTERNS = [r"\d+年经验", r"\d+岁", r"[\d一二三四五六七八九十]+招", r"\d.{0,8}(硕士|博士|医生|老师|工程师|从业者)"]
_TIME_PAIN_PATTERNS = [r"\d+天没", r"\d+年后", r"熬夜", r"失眠", r"专业术语", r"干货"]
_COGNITIVE_CONFLICT_PATTERNS = [r"真相", r"其实是", r"别再", r"千万不要", r"以为.*结果"]
_LOSS_AVERSION_PATTERNS = [r"不看后悔", r"最后\d+天", r"限时", r"错过", r"再不.*就"]


def classify_title_formula(title: str) -> str:
    checks = [
        (_STORY_HOOK_PATTERNS, "故事钩子型"),
        (_NUMBER_IDENTITY_PATTERNS, "数字身份标签型"),
        (_TIME_PAIN_PATTERNS, "时间痛点专业术语型"),
        (_COGNITIVE_CONFLICT_PATTERNS, "认知冲突反差型"),
        (_LOSS_AVERSION_PATTERNS, "损失厌恶紧迫型"),
    ]
    for patterns, label in checks:
        for pattern in patterns:
            if re.search(pattern, title):
                return label
    return "未归类"


def analyze(details: list) -> dict:
    total = len(details)
    if total == 0:
        return {
            "total": 0, "avg_liked": 0.0, "avg_collected": 0.0, "avg_comment": 0.0,
            "collect_like_ratio": 0.0, "image_video_ratio": {"image": 0, "video": 0},
            "title_formula_distribution": {}, "all_notes": [], "tag_frequency": [],
        }

    avg_liked = sum(n.get("liked_count", 0) for n in details) / total
    avg_collected = sum(n.get("collected_count", 0) for n in details) / total
    avg_comment = sum(n.get("comment_count", 0) for n in details) / total
    collect_like_ratio = round(avg_collected / avg_liked, 3) if avg_liked else 0.0

    image_count = sum(1 for n in details if not n.get("has_video") and n.get("image_count", 0) > 0)
    video_count = sum(1 for n in details if n.get("has_video"))

    formula_counter = Counter(classify_title_formula(n.get("title", "")) for n in details)

    all_notes = sorted(details, key=lambda n: n.get("liked_count", 0), reverse=True)

    tag_counter = Counter()
    for n in details:
        for tag in n.get("tags", []):
            if tag:
                tag_counter[tag] += 1
    tag_frequency = [{"tag": t, "count": c} for t, c in tag_counter.most_common(20)]

    return {
        "total": total,
        "avg_liked": round(avg_liked, 2),
        "avg_collected": round(avg_collected, 2),
        "avg_comment": round(avg_comment, 2),
        "collect_like_ratio": collect_like_ratio,
        "image_video_ratio": {"image": image_count, "video": video_count},
        "title_formula_distribution": dict(formula_counter),
        "all_notes": all_notes,
        "tag_frequency": tag_frequency,
    }


def main():
    parser = argparse.ArgumentParser(description="全量统计 + 标题公式分类")
    parser.add_argument("details_path", help="<id>_notes_details.json 路径")
    parser.add_argument("-o", "--output", default="./data", help="输出目录，默认 ./data")
    args = parser.parse_args()

    details = common.load_json(args.details_path)
    result = analyze(details)

    file_id = Path(args.details_path).name.split("_notes_details.json")[0]
    out_path = Path(args.output) / f"{file_id}_analysis.json"
    common.save_json(out_path, result)
    print(f"✅ 分析完成：{out_path}")
    print(f"  均赞 {result['avg_liked']} / 均藏 {result['avg_collected']} / 藏赞比 {result['collect_like_ratio']}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd skills/dl-xhs-benchmark && python3 -m pytest tests/test_analyze.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/dl-xhs-benchmark/scripts/analyze.py skills/dl-xhs-benchmark/tests/test_analyze.py
git commit -m "feat: replace publish-rhythm stats and TOP10 excerpt with full-text all_notes"
```

---

## Task 5: crawl_notes.py — new collection script for mode two (user-specified notes)

**Files:**
- Create: `skills/dl-xhs-benchmark/scripts/crawl_notes.py`
- Create: `skills/dl-xhs-benchmark/tests/test_crawl_notes.py`

**Interfaces:**
- Consumes: `common.parse_xhs_note_link` (Task 2), `common.save_json`/`load_json`, `TikHubClient.fetch_note_detail(note_id, xsec_token)` (unchanged, existing method)
- Produces: `crawl_notes.build_batch_id(link_count: int, today: date = None) -> str` (pure, testable); `crawl_notes.crawl(links: list, output_dir: str) -> tuple[list, str]` returning `(details, batch_id)`; CLI writes `<batch_id>_notes_details.json` with the same per-note dict shape as `crawl_blogger.py`'s output (so `analyze.py` can consume either).

- [ ] **Step 1: Write the failing test**

Create `skills/dl-xhs-benchmark/tests/test_crawl_notes.py`:

```python
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts import crawl_notes


def test_build_batch_id_formats_date_and_count():
    result = crawl_notes.build_batch_id(5, today=date(2026, 7, 15))
    assert result == "20260715_5notes"


def test_build_batch_id_uses_today_by_default():
    result = crawl_notes.build_batch_id(3)
    assert result.endswith("_3notes")
    assert len(result.split("_")[0]) == 8
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd skills/dl-xhs-benchmark && python3 -m pytest tests/test_crawl_notes.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.crawl_notes'`.

- [ ] **Step 3: Create `crawl_notes.py`**

Create `skills/dl-xhs-benchmark/scripts/crawl_notes.py`:

```python
"""Phase 1（模式二）：按用户提供的笔记链接列表逐条采集详情，每篇自动 checkpoint。"""

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils import common
from scripts.utils.tikhub_client import TikHubClient, TikHubError


def build_batch_id(link_count: int, today: date = None) -> str:
    """生成模式二产出物的批次标识：日期 + 篇数，笔记可能来自不同博主，没有单一博主名可用。"""
    today = today or date.today()
    return f"{today:%Y%m%d}_{link_count}notes"


def crawl(links: list, output_dir: str) -> tuple:
    batch_id = build_batch_id(len(links))
    checkpoint_path = Path(output_dir) / f"{batch_id}_crawl_checkpoint.json"
    details = []
    done_ids = set()
    if checkpoint_path.exists():
        checkpoint = common.load_json(checkpoint_path)
        details = checkpoint.get("details", [])
        done_ids = {d["note_id"] for d in details}
        print(f"↻ 从断点恢复，已完成 {len(done_ids)} 篇")

    client = TikHubClient()
    for i, link in enumerate(links, start=1):
        parsed = common.parse_xhs_note_link(link)
        note_id = parsed["note_id"]
        if note_id in done_ids:
            continue
        detail = client.fetch_note_detail(note_id, xsec_token=parsed.get("xsec_token", ""))
        details.append(detail)
        done_ids.add(note_id)
        print(f"  [{i}/{len(links)}] {detail.get('title', '')[:30]}")
        common.save_json(checkpoint_path, {"details": details})

    out_path = Path(output_dir) / f"{batch_id}_notes_details.json"
    common.save_json(out_path, details)
    if checkpoint_path.exists():
        checkpoint_path.unlink()
    return details, batch_id


def main():
    parser = argparse.ArgumentParser(description="按指定笔记链接列表采集详情")
    parser.add_argument("links", nargs="+", help="笔记链接列表，一个链接一个参数")
    parser.add_argument("-o", "--output", default="./data", help="输出目录，默认 ./data")
    args = parser.parse_args()

    try:
        details, batch_id = crawl(args.links, args.output)
    except ValueError as exc:
        print(f"❌ {exc}")
        sys.exit(1)
    except TikHubError as exc:
        print(f"❌ TikHub 请求失败（HTTP {exc.status_code}）：{exc}")
        print("已采集部分已保存到断点文件，重新运行本命令可继续。")
        sys.exit(1)

    print(f"✅ 采集完成，共 {len(details)} 篇笔记详情，批次标识：{batch_id}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd skills/dl-xhs-benchmark && python3 -m pytest tests/test_crawl_notes.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/dl-xhs-benchmark/scripts/crawl_notes.py skills/dl-xhs-benchmark/tests/test_crawl_notes.py
git commit -m "feat: add crawl_notes.py for mode-two (user-specified note links) collection"
```

---

## Task 6: deep_analyze.py — framework-driven AI task, new visual/skill-folder spec, optional profile

**Files:**
- Modify: `skills/dl-xhs-benchmark/scripts/deep_analyze.py`
- Create: `skills/dl-xhs-benchmark/tests/test_deep_analyze.py`

**Interfaces:**
- Consumes: `analyze.analyze()`'s new shape from Task 4 (`all_notes` instead of `top10`, no `publish_rhythm`)
- Produces: `deep_analyze.build_data_digest(name: str, profile: dict, analysis: dict) -> str` (skips the "账号基础信息" section when `profile` is falsy/empty — this is how mode two, which has no blogger profile, is handled); `deep_analyze.build_ai_task(name: str) -> str`; CLI signature `deep_analyze.py <analysis_path> <name> -o <dir> [--scan <path>]` where `--scan` is now **optional** (mode two has no scan.json). Output filenames the rest of the pipeline (Task 7's `quality.py`) must match exactly: `{name}_拆解报告.html`, `{name}_写作指南.skill/SKILL.md`.

- [ ] **Step 1: Write the failing tests**

Create `skills/dl-xhs-benchmark/tests/test_deep_analyze.py`:

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts import deep_analyze


def _sample_analysis():
    return {
        "total": 1, "avg_liked": 100.0, "avg_collected": 50.0, "avg_comment": 10.0,
        "collect_like_ratio": 0.5, "image_video_ratio": {"image": 1, "video": 0},
        "title_formula_distribution": {"故事钩子型": 1}, "tag_frequency": [{"tag": "猫粮", "count": 1}],
        "all_notes": [{
            "note_id": "1", "title": "那天我决定辞职", "desc": "x" * 300,
            "liked_count": 100, "collected_count": 50, "comment_count": 10, "tags": ["猫粮"],
        }],
    }


def test_build_data_digest_includes_full_note_text_not_truncated():
    digest = deep_analyze.build_data_digest("测试博主", {"nickname": "测试博主"}, _sample_analysis())
    assert "x" * 300 in digest


def test_build_data_digest_skips_profile_section_when_profile_empty():
    digest = deep_analyze.build_data_digest("20260715_3notes", {}, _sample_analysis())
    assert "账号基础信息" not in digest


def test_build_ai_task_mentions_six_dimensions():
    task = deep_analyze.build_ai_task("测试博主")
    for keyword in ["标题", "开头", "中间", "结尾", "CTA", "框架"]:
        assert keyword in task


def test_build_ai_task_output_filenames_match_new_naming():
    task = deep_analyze.build_ai_task("测试博主")
    assert "测试博主_拆解报告.html" in task
    assert "测试博主_写作指南.skill" in task
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd skills/dl-xhs-benchmark && python3 -m pytest tests/test_deep_analyze.py -v
```

Expected: FAIL (`build_data_digest`/`build_ai_task` still reference `top10`, three-layer wording, and old filenames).

- [ ] **Step 3: Rewrite `deep_analyze.py`**

Replace the entire contents of `skills/dl-xhs-benchmark/scripts/deep_analyze.py` with:

```python
"""Phase 3 Step A: 生成数据底稿 + AI 拆解任务说明（按叙事框架组织的笔记结构层拆解）。"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils import common

VISUAL_STYLE_SPEC = """## HTML 报告视觉风格规范——"拆解简报"

严格按以下规范生成，不得使用灰褐底色+砖红强调色+无圆角无阴影无白卡的工业档案风，
也不得沿用暖米白底+高对比衬线+陶土色点缀这种常见 AI 生成设计默认风格：

- 背景色 `#FAFAF9`（近白）；强调色 `#5546FF`（靠紫，唯一强调色，克制使用）；
  强调色浅调 `#ECE9FF`（用于底色高亮/进度条填充）；正文色 `#16151A`（近黑）；
  次要文字 `#6E6B76`；分割线 `#E5E3E0`
- 标题与大数字字体 Google Fonts `Manrope`（几何无衬线，不用衬线体）；
  中文全部用 `Noto Sans SC`（黑体，按粗细分级，标题 700/正文 400）；
  英文正文与数据标签用 `Inter`
- 模块外观：大圆角 16px + 极轻柔阴影（不用描边，不用无阴影硬边框）的白色浮起卡片
- 首屏：这批笔记的核心数据用大字号数字排版呈现（Apple 式的巨大自信数字），不是"大数字+小标签+渐变"模板化写法
- 招牌元素：桌面端加一条侧边"拆解进度轨"——一级节点是归纳出来的几种叙事框架（比如痛点型/测评型/教程型），
  每个框架展开后是标题公式/开头模式/中间结构/结尾方式/CTA 类型五个二级节点，
  外加一个"代表作逐句拆解"节点；随滚动位置高亮推进，兼具导航和"逐层拆解"的视觉隐喻；
  移动端可退化为顶部进度条
- 动效（原生 JS，无外部库）：首屏一次性编排好的进场动画（数字滚动计数 + 标题浮现），
  滚动时侧边进度轨跟随高亮；克制，不叠加额外的散点特效（如卡片 hover 描边变色之类）
- 折叠面板用原生 `<details><summary>`；响应式断点 768px，移动端隐藏侧边进度轨
- 技术要求：单文件 HTML，手写 CSS，禁止引入 Tailwind CDN 等外部框架
"""

SKILL_FOLDER_SPEC = """## 写作指南 Skill 文件夹规范

产出路径：`{name}_写作指南.skill/SKILL.md`（必须是文件夹，不能是单个 `.skill.md` 文件）

SKILL.md 必须支持两种调用方式，在文档开头明确写出调用说明：

1. **1:1 模仿模式**（默认）：直接照搬本文档记录的对标公式与写法创作。
2. **融合风格模式**：用户在触发时指定"保留我自己的哪些维度（如语言习惯/结尾方式），其余维度套用对标笔记的公式"。
   AI 按用户指定的维度做混合，而不是全盘照搬。文档中需要列出可供用户选择混合的维度清单
   （至少涵盖：标题公式、开头模板、中间结构、结尾方式、CTA 策略、框架选择、语言习惯），
   每个维度给出对标笔记的对应结论，供用户挑选保留/替换。

内容章节按叙事框架分组组织，每个框架下给出可执行结论（不是原始统计数字，是给 AI 创作时直接可用的规则）：
- 这个框架的适用场景（什么样的产品/话题适合用这个框架）
- 标题公式（带示例）/ 开头模式 / 中间结构 / 结尾方式 / CTA 类型，各自的可执行写法规则
- 该框架下 1-2 篇代表作的逐句/逐段拆解

报告和 Skill 都只产出对标笔记的套路规律，不主动询问或写入用户自己的业务信息——
用户以后每次要写新笔记，自己在对话里说明业务，由这份 Skill 现场套用规律。
"""


def build_data_digest(name: str, profile: dict, analysis: dict) -> str:
    lines = [f"# {name} 数据底稿\n"]

    if profile:
        lines.append("## 账号基础信息")
        lines.append(f"- 昵称：{profile.get('nickname', '')}")
        lines.append(f"- 小红书号：{profile.get('red_id', '')}")
        lines.append(f"- 简介：{profile.get('desc', '')}")
        lines.append(f"- 粉丝数：{profile.get('fans', 0)}")
        lines.append(f"- 获赞与收藏：{profile.get('liked_and_collected', 0)}\n")

    lines.append("## 全量统计")
    lines.append(f"- 分析笔记总数：{analysis['total']}")
    lines.append(f"- 均赞：{analysis['avg_liked']} / 均藏：{analysis['avg_collected']} / 均评：{analysis['avg_comment']}")
    lines.append(f"- 藏赞比：{analysis['collect_like_ratio']}")
    lines.append(f"- 图文 vs 视频：{analysis['image_video_ratio']}")
    lines.append(f"- 标题公式分布：{analysis['title_formula_distribution']}")
    lines.append(f"- 高频标签 TOP20：{analysis['tag_frequency']}\n")

    lines.append("## 全部笔记全文（按点赞数从高到低排列，供逐篇归类框架与拆解结构）")
    for i, note in enumerate(analysis["all_notes"], start=1):
        lines.append(
            f"{i}. 《{note.get('title', '')}》 赞{note.get('liked_count', 0)} "
            f"藏{note.get('collected_count', 0)} 评{note.get('comment_count', 0)}"
        )
        desc = note.get("desc", "")
        if desc:
            lines.append(f"   正文：{desc}")
        tags = note.get("tags", [])
        if tags:
            lines.append(f"   标签：{', '.join(tags)}")
    return "\n".join(lines)


def build_ai_task(name: str) -> str:
    lines = [f"# {name} AI 拆解任务\n"]
    lines.append(
        "请基于同目录下的 `{}_数据底稿.md`，按以下流程产出拆解结论，"
        "再生成两个最终产出物。每完成一个立即写入磁盘，不等另一个完成。\n".format(name)
    )

    lines.append("## 第一步：单篇标注")
    lines.append(
        "逐篇阅读“全部笔记全文”里的每一篇，标注 6 个维度：标题公式、开头模式、"
        "中间结构、结尾方式、CTA 类型、叙事框架。标题公式可参考数据底稿里的正则分类结果，"
        "其余 5 项由你阅读全文后判断。叙事框架不是写死的分类表，由你读完这批笔记后自己归纳"
        "（比如痛点型/测评型/教程型），命名要贴合实际内容\n"
    )

    lines.append("## 第二步：框架分组")
    lines.append("把标注完的笔记按叙事框架分组，同一框架下的笔记归到一起\n")

    lines.append("## 第三步：提炼规律")
    lines.append(
        "不用区分某个写法是因为博主个人习惯还是选题不同造成的——只要在这个框架分组里"
        "反复出现、值得抄的写法规律，就直接提炼成可执行规则。框架组内笔记数 ≥ 2 篇时做交叉提炼；"
        "只有 1 篇时直接把这篇的标注结果当结论用，跳过对比\n"
    )

    lines.append("## 第四步：代表作精读")
    lines.append("每个框架挑 1-2 篇高赞代表作，做逐句/逐段拆解，展示具体怎么写的，不是抽象规律\n")

    lines.append("## 第五步：产出可执行规则")
    lines.append(
        "结果导向：把第三步、第四步的结论合并成能直接套用的规则，不停留在“观察到的现象”描述。"
        "不需要询问或写入用户自己的业务信息，只产出对标笔记本身的套路规律\n"
    )

    lines.append(VISUAL_STYLE_SPEC)
    lines.append(SKILL_FOLDER_SPEC.format(name=name))

    lines.append("## 质量红线")
    lines.append(
        f"- HTML 报告文件名：`{name}_拆解报告.html`，报告正文必须包含"
        "「框架」「标题」「开头」「中间」「结尾」「CTA」这 6 个关键词锚点，且各部分均有实质内容"
    )
    lines.append(f"- Skill 文件夹路径：`{name}_写作指南.skill/SKILL.md`，必须是文件夹，不能是单个文件")
    lines.append("- 生成完毕后运行 `python scripts/utils/quality.py` 对应的校验逻辑（或等价手动检查）确认无遗漏")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="生成数据底稿 + AI 拆解任务说明")
    parser.add_argument("analysis_path", help="<id>_analysis.json 路径")
    parser.add_argument("name", help="产出物文件名标识：博主展示名（模式一）或批次标识（模式二）")
    parser.add_argument("-o", "--output", default="./output", help="输出目录，默认 ./output")
    parser.add_argument("--scan", default=None, help="<user_id>_scan.json 路径（模式一用于读取 profile，模式二可不传）")
    args = parser.parse_args()

    analysis = common.load_json(args.analysis_path)
    profile = {}
    if args.scan:
        scan_data = common.load_json(args.scan)
        profile = scan_data.get("profile", {})

    digest = build_data_digest(args.name, profile, analysis)
    task = build_ai_task(args.name)

    out_dir = Path(args.output)
    digest_path = out_dir / f"{args.name}_数据底稿.md"
    task_path = out_dir / f"{args.name}_AI拆解任务.md"
    out_dir.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(digest, encoding="utf-8")
    task_path.write_text(task, encoding="utf-8")

    print(f"✅ 数据底稿：{digest_path}")
    print(f"✅ AI拆解任务：{task_path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd skills/dl-xhs-benchmark && python3 -m pytest tests/test_deep_analyze.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/dl-xhs-benchmark/scripts/deep_analyze.py skills/dl-xhs-benchmark/tests/test_deep_analyze.py
git commit -m "feat: rewrite deep_analyze.py around framework-driven note-structure analysis"
```

---

## Task 7: quality.py — update required keywords and output filenames

**Files:**
- Modify: `skills/dl-xhs-benchmark/scripts/utils/quality.py`
- Modify: `skills/dl-xhs-benchmark/tests/test_quality.py`

**Interfaces:**
- Consumes: the exact filenames decided in Task 6 (`{name}_拆解报告.html`, `{name}_写作指南.skill/SKILL.md`)
- Produces: `quality.check_outputs(output_dir: str, name: str) -> list` — same signature, new keyword list `["框架", "标题", "开头", "中间", "结尾", "CTA"]`.

- [ ] **Step 1: Write the failing tests**

Replace the contents of `skills/dl-xhs-benchmark/tests/test_quality.py`:

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts.utils import quality


def test_check_outputs_reports_missing_html(tmp_path):
    skill_dir = tmp_path / "博主A_写作指南.skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 写作指南\n内容", encoding="utf-8")

    issues = quality.check_outputs(str(tmp_path), "博主A")
    assert any("拆解报告.html" in issue for issue in issues)


def test_check_outputs_reports_skill_as_file_not_folder(tmp_path):
    (tmp_path / "博主A_拆解报告.html").write_text(
        "<html>框架 标题 开头 中间 结尾 CTA</html>", encoding="utf-8"
    )
    (tmp_path / "博主A_写作指南.skill").write_text("不应该是文件", encoding="utf-8")

    issues = quality.check_outputs(str(tmp_path), "博主A")
    assert any("必须是文件夹" in issue for issue in issues)


def test_check_outputs_passes_when_all_valid(tmp_path):
    (tmp_path / "博主A_拆解报告.html").write_text(
        "<html>框架 标题 开头 中间 结尾 CTA</html>", encoding="utf-8"
    )
    skill_dir = tmp_path / "博主A_写作指南.skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 写作指南\n内容", encoding="utf-8")

    issues = quality.check_outputs(str(tmp_path), "博主A")
    assert issues == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd skills/dl-xhs-benchmark && python3 -m pytest tests/test_quality.py -v
```

Expected: FAIL (current `quality.py` still looks for `_蒸馏报告.html`/`_创作指南.skill` and the old three keywords).

- [ ] **Step 3: Update `quality.py`**

Replace the entire contents of `skills/dl-xhs-benchmark/scripts/utils/quality.py` with:

```python
"""Phase 4: 产出物质量校验——报告与 Skill 文件夹是否完整、非空。"""

from pathlib import Path

_REQUIRED_KEYWORDS = ["框架", "标题", "开头", "中间", "结尾", "CTA"]


def check_outputs(output_dir: str, name: str) -> list:
    issues = []
    base = Path(output_dir)

    html_path = base / f"{name}_拆解报告.html"
    if not html_path.exists() or html_path.stat().st_size == 0:
        issues.append(f"缺失或为空：{html_path.name}")
    else:
        text = html_path.read_text(encoding="utf-8", errors="ignore")
        missing_keywords = [kw for kw in _REQUIRED_KEYWORDS if kw not in text]
        if missing_keywords:
            issues.append(f"{html_path.name} 缺少必要模块关键词：{', '.join(missing_keywords)}")

    skill_path = base / f"{name}_写作指南.skill"
    if not skill_path.exists():
        issues.append(f"缺失：{skill_path.name}")
    elif not skill_path.is_dir():
        issues.append(f"{skill_path.name} 必须是文件夹，不能是单个文件")
    else:
        skill_md = skill_path / "SKILL.md"
        if not skill_md.exists() or skill_md.stat().st_size == 0:
            issues.append(f"缺失或为空：{skill_path.name}/SKILL.md")

    return issues
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd skills/dl-xhs-benchmark && python3 -m pytest tests/test_quality.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/dl-xhs-benchmark/scripts/utils/quality.py skills/dl-xhs-benchmark/tests/test_quality.py
git commit -m "feat: update quality gate keywords and filenames for note-structure report"
```

---

## Task 8: run.py — branch between mode one (blogger) and mode two (specified notes)

**Files:**
- Modify: `skills/dl-xhs-benchmark/run.py`

**Interfaces:**
- Consumes: `scan_blogger.scan` (Task 3), `crawl_blogger.crawl` (Task 3, via CLI), `crawl_notes.crawl`/`build_batch_id` (Task 5), `analyze.analyze` (Task 4, via CLI), `deep_analyze.py`'s CLI with optional `--scan` (Task 6)
- Produces: `run.py` CLI — mode one: `python run.py <博主链接> [--count N]`; mode two: `python run.py --notes <link1> <link2> ...`. Mutually exclusive; exactly one must be given.

- [ ] **Step 1: Rewrite `run.py`**

Replace the entire contents of `skills/dl-xhs-benchmark/run.py` with:

```python
"""dl-xhs-benchmark 一键运行入口：串联 Phase 0 → Phase 3 Step A。"""

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.crawl_notes import build_batch_id
from scripts.utils import common

DEFAULT_COUNT = 20


def run_step(cmd: list):
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description="dl-xhs-benchmark 一键运行入口")
    parser.add_argument("link", nargs="?", default=None, help="模式一：小红书博主主页链接或分享短链")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT, help=f"模式一采集篇数，默认按点赞取前 {DEFAULT_COUNT} 篇")
    parser.add_argument("--notes", nargs="+", default=None, help="模式二：指定笔记链接列表，一次性传入多个")
    parser.add_argument("-o", "--output", default="./output", help="最终产出物目录，默认 ./output")
    parser.add_argument("--data-dir", default="./data", help="中间数据目录，默认 ./data")
    args = parser.parse_args()

    if bool(args.link) == bool(args.notes):
        print("❌ 请二选一：要么传一个博主链接（模式一），要么用 --notes 传指定笔记链接列表（模式二）")
        sys.exit(1)

    root = Path(__file__).resolve().parent
    run_step([sys.executable, str(root / "scripts" / "check_env.py")])

    if args.link:
        run_step([sys.executable, str(root / "scripts" / "scan_blogger.py"), args.link, "-o", args.data_dir])
        file_id = common.parse_xhs_user_link(args.link)
        scan_path = Path(args.data_dir) / f"{file_id}_scan.json"
        scan_data = common.load_json(scan_path)
        name = scan_data["profile"].get("nickname") or file_id

        run_step([
            sys.executable, str(root / "scripts" / "crawl_blogger.py"), file_id,
            "--count", str(args.count), "-o", args.data_dir,
        ])
        scan_arg = ["--scan", str(scan_path)]
    else:
        file_id = build_batch_id(len(args.notes))
        name = file_id
        run_step([
            sys.executable, str(root / "scripts" / "crawl_notes.py"), *args.notes, "-o", args.data_dir,
        ])
        scan_arg = []

    details_path = Path(args.data_dir) / f"{file_id}_notes_details.json"
    run_step([sys.executable, str(root / "scripts" / "analyze.py"), str(details_path), "-o", args.data_dir])

    analysis_path = Path(args.data_dir) / f"{file_id}_analysis.json"
    run_step([
        sys.executable, str(root / "scripts" / "deep_analyze.py"),
        str(analysis_path), name, "-o", args.output, *scan_arg,
    ])

    print(
        f"\n✅ Phase 0-3 Step A 完成。请让宿主 AI 读取 "
        f"{args.output}/{name}_AI拆解任务.md 生成最终的 HTML 报告与写作指南 Skill 文件夹。"
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Manually verify the CLI wiring (no network calls needed for these checks)**

```bash
cd skills/dl-xhs-benchmark
python3 run.py --help
python3 run.py
```

Expected: `--help` prints the argument list without error; running with no arguments prints `❌ 请二选一：...` and exits non-zero (argparse's `nargs="?"` default `None` for `link` and `--notes` default `None` means `bool(None) == bool(None)` is `True`, triggering the mutual-exclusion error).

```bash
python3 run.py "https://www.xiaohongshu.com/user/profile/x" --notes "https://www.xiaohongshu.com/explore/y"
```

Expected: also prints the same `❌ 请二选一` error (both given).

- [ ] **Step 3: Run the full test suite to confirm nothing else broke**

```bash
cd skills/dl-xhs-benchmark && python3 -m pytest tests/ -v
```

Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add skills/dl-xhs-benchmark/run.py
git commit -m "feat: branch run.py between blogger mode and specified-notes mode"
```

---

## Task 9: SKILL.md — full rewrite around the new positioning and two-mode flow

**Files:**
- Modify: `skills/dl-xhs-benchmark/SKILL.md`

**Interfaces:**
- Consumes: every CLI/behavior decision from Tasks 1–8 (must document them accurately: `scan_blogger.py` no longer prints tiers; `crawl_notes.py` exists; `deep_analyze.py`'s `--scan` is optional; output filenames use "拆解"/"写作指南"; config path is `~/.dl-xhs-benchmark/`)
- Produces: the skill's user-facing entry point — no other task consumes this file's content programmatically, but its accuracy against the actual scripts is what the executing agent/reviewer checks in Step 2 below.

- [ ] **Step 1: Replace `SKILL.md` contents**

Replace the entire contents of `skills/dl-xhs-benchmark/SKILL.md` with:

```markdown
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
`20260715_5notes`）。

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
```

- [ ] **Step 2: Verify no stale wording remains in this file**

```bash
grep -nE "蒸馏|IP定位|运营策略|快速档|推荐档|深度档|dl-xhs-distill" skills/dl-xhs-benchmark/SKILL.md
```

Expected: no output (empty grep result means no matches).

- [ ] **Step 3: Commit**

```bash
git add skills/dl-xhs-benchmark/SKILL.md
git commit -m "docs: rewrite SKILL.md around two-mode note-structure benchmarking"
```

---

## Task 10: references/产出物质量标杆.md — rewrite acceptance criteria

**Files:**
- Modify: `skills/dl-xhs-benchmark/references/产出物质量标杆.md`

**Interfaces:**
- Consumes: the naming/keyword decisions from Tasks 6–7
- Produces: nothing consumed programmatically by other tasks; this is a human-facing reference doc.

- [ ] **Step 1: Replace the file contents**

Replace the entire contents of `skills/dl-xhs-benchmark/references/产出物质量标杆.md` with:

```markdown
# 产出物质量标杆

> 状态：占位版本。首次完整拆解跑通后，请用真实产出的 HTML 报告和 Skill 文件夹
> 替换本文档中的示例片段，作为后续产出的质量基准（脱敏处理，去除真实账号/笔记信息）。

## HTML 拆解报告验收标准

- 视觉风格严格遵循 `SKILL.md` 中"拆解简报"规范，不得混用灰褐+砖红工业档案风的配色/字体，也不得混用暖米白+衬线+陶土色这类常见 AI 生成默认风格
- 侧边拆解进度轨以叙事框架为一级节点，标题公式/开头模式/中间结构/结尾方式/CTA 类型为二级节点
- 每个叙事框架下的结论都有实质内容，不是空模块或占位文字
- 每个框架挑出的代表作附逐句/逐段拆解说明，不是简单列表
- 结论是"可执行规则"，不是原始统计数字的堆砌
- 不包含用户自己的业务信息——报告只讲对标笔记本身的套路
- 可在浏览器直接打开，无样式错乱，移动端（768px 断点）布局正常

## 写作指南 Skill 文件夹验收标准

- 是文件夹，包含 `SKILL.md`，不是单个 `.skill.md` 文件
- 文档开头清晰说明两种调用方式（1:1 模仿 / 融合用户自己风格）及触发方法
- 融合风格模式下列出至少 7 个可选混合维度（标题公式/开头模板/中间结构/结尾方式/
  CTA策略/框架选择/语言习惯），每个维度给出对标笔记对应的结论
- 内容层结论是"可执行规则"，不是原始统计数字的堆砌
- 不包含用户自己的业务信息

## 待补充

- [ ] 首次真实拆解跑通后，补充一份脱敏后的 HTML 报告关键截图/片段
- [ ] 首次真实拆解跑通后，补充一份脱敏后的 Skill 文件夹 SKILL.md 完整示例
```

- [ ] **Step 2: Commit**

```bash
git add skills/dl-xhs-benchmark/references/产出物质量标杆.md
git commit -m "docs: update quality benchmark doc for note-structure acceptance criteria"
```

---

## Task 11: README.md + marketplace.json — update public naming and install commands

**Files:**
- Modify: `README.md` (repo root)
- Modify: `.claude-plugin/marketplace.json` (repo root)

**Interfaces:**
- Consumes: `dl-xhs-benchmark` as the final directory/skill name (Task 1)
- Produces: nothing consumed by other tasks — these are the repo's public-facing listing files.

- [ ] **Step 1: Update `.claude-plugin/marketplace.json`**

In `.claude-plugin/marketplace.json`, replace the `dl-xhs-distill` plugin entry:

```json
    {
      "name": "dl-xhs-distill",
      "description": "小红书博主蒸馏器：输入博主主页链接，产出 IP 定位/运营策略/内容形式三层蒸馏的 HTML 报告，以及可 1:1 模仿或融合风格创作的 Skill 文件夹。",
      "source": "./",
      "strict": false,
      "version": "1.0.0",
      "category": "content-creation",
      "keywords": [
        "xiaohongshu",
        "content-analysis",
        "stylometry",
        "creation-skill",
        "chinese"
      ],
      "skills": [
        "./skills/dl-xhs-distill"
      ]
    }
```

with:

```json
    {
      "name": "dl-xhs-benchmark",
      "description": "小红书对标笔记拆解器：输入对标博主链接或指定笔记链接，产出笔记结构层（标题/开头/中间/结尾/CTA/叙事框架）拆解报告，以及可 1:1 模仿或融合风格创作的写作指南 Skill 文件夹。",
      "source": "./",
      "strict": false,
      "version": "1.0.0",
      "category": "content-creation",
      "keywords": [
        "xiaohongshu",
        "content-analysis",
        "benchmark",
        "creation-skill",
        "chinese"
      ],
      "skills": [
        "./skills/dl-xhs-benchmark"
      ]
    }
```

- [ ] **Step 2: Update root `README.md`**

In `README.md`, replace the table row:

```markdown
| `dl-xhs-distill` | 小红书博主蒸馏器：输入博主主页链接，产出 IP 定位/运营策略/内容形式三层蒸馏的 HTML 报告，以及可 1:1 模仿或融合风格创作的 Skill 文件夹 |
```

with:

```markdown
| `dl-xhs-benchmark` | 小红书对标笔记拆解器：输入对标博主链接或你自己挑的几篇笔记链接，产出笔记结构层（标题/开头/中间/结尾/CTA/叙事框架）拆解报告，以及可 1:1 模仿或融合风格创作的写作指南 Skill 文件夹 |
```

Replace the install command:

```bash
claude plugin install dl-xhs-distill@dl-skill
```

with:

```bash
claude plugin install dl-xhs-benchmark@dl-skill
```

Replace the manual-install example:

```bash
cp -R dl-skill/skills/dl-xhs-winback ~/.claude/skills/dl-xhs-winback
```

Leave this line as-is (it's an example for a different skill), but update the TikHub-specific paragraph further down:

```markdown
`dl-xhs-distill` 需要一个 [TikHub](https://user.tikhub.io/register?ref=QYnybFaK) API Token（用于通过公开 REST API 拉取小红书公开数据），首次运行 `scripts/check_env.py` 会引导你注册并输入 Token，自动保存到 `~/.dl-xhs-distill/tikhub_config.json`（不在 skill 目录内，不涉及 `config.json`/`config.example.json`），也可以提前设置环境变量 `TIKHUB_API_TOKEN` 跳过交互式引导。
```

with:

```markdown
`dl-xhs-benchmark` 需要一个 [TikHub](https://user.tikhub.io/register?ref=QYnybFaK) API Token（用于通过公开 REST API 拉取小红书公开数据），首次运行 `scripts/check_env.py` 会引导你注册并输入 Token，自动保存到 `~/.dl-xhs-benchmark/tikhub_config.json`（不在 skill 目录内，不涉及 `config.json`/`config.example.json`），也可以提前设置环境变量 `TIKHUB_API_TOKEN` 跳过交互式引导。
```

- [ ] **Step 3: Verify no stale mentions remain in these two files**

```bash
grep -n "dl-xhs-distill" README.md .claude-plugin/marketplace.json
```

Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add README.md .claude-plugin/marketplace.json
git commit -m "docs: rename dl-xhs-distill to dl-xhs-benchmark in public listings"
```

---

## Task 12: Final verification sweep

**Files:**
- None expected to change (this task only fixes stragglers if the sweep finds any)

**Interfaces:**
- Consumes: everything from Tasks 1–11
- Produces: a clean repo state ready for `superpowers:finishing-a-development-branch`

- [ ] **Step 1: Run the full test suite**

```bash
cd skills/dl-xhs-benchmark && python3 -m pytest tests/ -v
```

Expected: all tests PASS (across `test_common.py`, `test_scan_blogger.py`, `test_analyze.py`, `test_crawl_notes.py`, `test_deep_analyze.py`, `test_quality.py`, `test_tikhub_client.py`).

- [ ] **Step 2: Grep the whole repo for stray old-name references**

```bash
cd /Users/dalin/Desktop/dl-skill
grep -rn "dl-xhs-distill" . --exclude-dir=.git --exclude-dir=docs
grep -rln "蒸馏" skills/dl-xhs-benchmark
```

Expected: the first command returns nothing (every functional reference to the old skill name has been renamed — the `docs/` directory is excluded on purpose, since the design doc's own title and prose intentionally narrate the rename from `dl-xhs-distill`). The second command returns nothing (no "蒸馏" wording left inside the renamed skill directory itself).

- [ ] **Step 3: Fix any stragglers found in Step 2, re-run Steps 1–2 until clean**

If the greps above return matches, open each file, apply the same rename pattern used in the task where that file's sibling content was already updated, then re-run Steps 1 and 2.

- [ ] **Step 4: Confirm `tools/build-skills.sh` picks up the renamed skill automatically**

```bash
bash tools/build-skills.sh /tmp/dl-skill-dist-check
ls /tmp/dl-skill-dist-check
```

Expected: `dl-xhs-benchmark.zip` is listed (and no `dl-xhs-distill.zip`), since the build script globs `skills/*/SKILL.md` and needs no separate registration.

- [ ] **Step 5: Final commit (only if Step 3 required fixes; otherwise skip — nothing to commit)**

```bash
git add -A
git commit -m "chore: fix stray references found in final verification sweep"
```
