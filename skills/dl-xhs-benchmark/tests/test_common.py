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


def test_save_and_load_json_roundtrip(tmp_path):
    p = tmp_path / "sample.json"
    common.save_json(p, {"a": 1, "b": "中文"})
    assert common.load_json(p) == {"a": 1, "b": "中文"}
