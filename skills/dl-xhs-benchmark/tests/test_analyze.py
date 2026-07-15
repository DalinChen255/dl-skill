import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts import analyze


def test_classify_title_formula_number_identity():
    assert analyze.classify_title_formula("985硕士教你选猫粮") == "数字身份标签型"


def test_classify_title_formula_loss_aversion():
    assert analyze.classify_title_formula("不看后悔！最后3天福利") == "损失厌恶紧迫型"


def test_classify_title_formula_story_hook():
    assert analyze.classify_title_formula("那天我决定辞职，结果发生了这件事") == "故事钩子型"


def test_classify_title_formula_unclassified():
    assert analyze.classify_title_formula("今日穿搭分享") == "未归类"


def _sample_notes():
    return [
        {
            "note_id": "1", "title": "985硕士教你选猫粮", "type": "normal",
            "liked_count": 100, "collected_count": 50, "comment_count": 10, "share_count": 2,
            "tags": ["猫粮", "宠物"], "image_count": 3, "has_video": False,
            "create_time": 1700000000,
        },
        {
            "note_id": "2", "title": "3天没洗头的真相", "type": "normal",
            "liked_count": 300, "collected_count": 150, "comment_count": 30, "share_count": 5,
            "tags": ["猫粮"], "image_count": 0, "has_video": True,
            "create_time": 1700100000,
        },
    ]


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
