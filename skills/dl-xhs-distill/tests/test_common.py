import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts.utils import common


def test_parse_xhs_user_link_profile_url():
    url = "https://www.xiaohongshu.com/user/profile/5f1234567890abcde1234567?xsec_token=ABC&xsec_source=pc_note"
    assert common.parse_xhs_user_link(url) == "5f1234567890abcde1234567"


def test_parse_xhs_user_link_profile_url_no_query():
    url = "https://www.xiaohongshu.com/user/profile/5f1234567890abcde1234567"
    assert common.parse_xhs_user_link(url) == "5f1234567890abcde1234567"


def test_parse_xhs_user_link_invalid_raises():
    try:
        common.parse_xhs_user_link("https://example.com/not-a-profile")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_compute_collection_tiers_normal():
    tiers = common.compute_collection_tiers(90)
    assert tiers == {"快速": 30, "推荐": 45, "深度": 90}


def test_compute_collection_tiers_small_total_floors_at_one():
    tiers = common.compute_collection_tiers(2)
    assert tiers == {"快速": 1, "推荐": 1, "深度": 2}


def test_compute_collection_tiers_zero_total():
    tiers = common.compute_collection_tiers(0)
    assert tiers == {"快速": 0, "推荐": 0, "深度": 0}


def test_save_and_load_json_roundtrip(tmp_path):
    p = tmp_path / "sample.json"
    common.save_json(p, {"a": 1, "b": "中文"})
    assert common.load_json(p) == {"a": 1, "b": "中文"}
