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
