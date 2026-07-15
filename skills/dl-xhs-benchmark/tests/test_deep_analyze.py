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
