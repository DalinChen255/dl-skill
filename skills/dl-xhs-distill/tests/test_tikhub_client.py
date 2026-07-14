import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts.utils import tikhub_client as tc


def _fake_response(payload):
    body = json.dumps(payload).encode("utf-8")
    mock_resp = mock.MagicMock()
    mock_resp.read.return_value = body
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    return mock_resp


def test_client_raises_without_token():
    if os.environ.get("TIKHUB_API_TOKEN"):
        return
    try:
        tc.TikHubClient(token="")
    except tc.TikHubError as exc:
        assert exc.status_code == 401
        return
    assert False, "expected TikHubError when no token available"


@mock.patch("scripts.utils.tikhub_client.urllib.request.urlopen")
def test_normalize_count_handles_wan_suffix(mock_urlopen):
    client = tc.TikHubClient(token="fake-token")
    assert client._normalize_count("6.4万") == 64000
    assert client._normalize_count("13008") == 13008
    assert client._normalize_count(None) == 0
    assert client._normalize_count("1,234") == 1234


@mock.patch("scripts.utils.tikhub_client.urllib.request.urlopen")
def test_fetch_user_info_parses_web_v3_shape(mock_urlopen):
    payload = {
        "code": 200,
        "data": {
            "data": {
                "basic_info": {
                    "nickname": "测试博主",
                    "red_id": "12345",
                    "desc": "简介文本",
                    "gender": 0,
                    "ip_location": "上海",
                    "images": "http://example.com/avatar.jpg",
                },
                "interactions": [
                    {"type": "fans", "count": "1.2万"},
                    {"type": "follows", "count": "100"},
                    {"type": "interaction", "count": "6.4万"},
                ],
            }
        },
    }
    mock_urlopen.return_value = _fake_response(payload)
    client = tc.TikHubClient(token="fake-token")
    info = client.fetch_user_info("user123")
    assert info["nickname"] == "测试博主"
    assert info["fans"] == 12000
    assert info["liked_and_collected"] == 64000


@mock.patch("scripts.utils.tikhub_client.urllib.request.urlopen")
def test_fetch_user_info_falls_back_to_app_v2_shape(mock_urlopen):
    """web_v3 端点失败（非致命状态码）时，应降级到 app_v2/get_user_info 并按其扁平结构解析。"""
    err = tc.urllib.error.HTTPError(
        url="https://api.tikhub.io", code=404, msg="Not Found",
        hdrs=None, fp=mock.MagicMock(read=lambda: b'{"detail":"Not Found"}'),
    )
    fallback_payload = {
        "code": 200,
        "data": {
            "data": {
                "nickname": "测试博主二号",
                "red_id": "67890",
                "desc": "",
                "gender": 1,
                "ip_location": "北京",
                "images": "http://example.com/avatar2.jpg",
                "fans": 500,
                "follows": 20,
                "liked": 300,
                "collected": 100,
            }
        },
    }
    mock_urlopen.side_effect = [err, _fake_response(fallback_payload)]
    client = tc.TikHubClient(token="fake-token")
    info = client.fetch_user_info("user123")
    assert info["nickname"] == "测试博主二号"
    assert info["fans"] == 500
    assert info["liked_and_collected"] == 400


@mock.patch("scripts.utils.tikhub_client.urllib.request.urlopen")
def test_fetch_user_info_raises_tikhub_error_on_403(mock_urlopen):
    err = tc.urllib.error.HTTPError(
        url="https://api.tikhub.io", code=403, msg="Forbidden",
        hdrs=None, fp=mock.MagicMock(read=lambda: b'{"detail":"no permission"}'),
    )
    mock_urlopen.side_effect = err
    client = tc.TikHubClient(token="fake-token")
    try:
        client.fetch_user_info("user123")
        assert False, "expected TikHubError"
    except tc.TikHubError as exc:
        assert exc.status_code == 403


@mock.patch("scripts.utils.tikhub_client.urllib.request.urlopen")
def test_fetch_user_info_fails_fast_on_401_without_trying_fallback(mock_urlopen):
    """401（token 无效）对所有端点都会一样失败，不应浪费请求尝试降级端点。"""
    err = tc.urllib.error.HTTPError(
        url="https://api.tikhub.io", code=401, msg="Unauthorized",
        hdrs=None, fp=mock.MagicMock(read=lambda: b'{"detail":"bad token"}'),
    )
    mock_urlopen.side_effect = err
    client = tc.TikHubClient(token="fake-token")
    try:
        client.fetch_user_info("user123")
        assert False, "expected TikHubError"
    except tc.TikHubError as exc:
        assert exc.status_code == 401
    assert mock_urlopen.call_count == 1


@mock.patch("scripts.utils.tikhub_client.urllib.request.urlopen")
def test_fetch_user_notes_parses_app_v2_shape_and_paginates_by_last_item_cursor(mock_urlopen):
    payload = {
        "code": 200,
        "data": {
            "data": {
                "has_more": True,
                "notes": [
                    {
                        "id": "note_aaa",
                        "display_title": "示例标题一",
                        "type": "normal",
                        "likes": 42,
                        "images_list": [{"url_size_large": "http://example.com/a.jpg", "url": "http://example.com/a_small.jpg"}],
                        "cursor": "cursor_after_note_aaa",
                    },
                    {
                        "id": "note_bbb",
                        "display_title": "示例标题二",
                        "type": "normal",
                        "likes": 7,
                        "images_list": [],
                        "cursor": "cursor_after_note_bbb",
                    },
                ],
            }
        },
    }
    mock_urlopen.return_value = _fake_response(payload)
    client = tc.TikHubClient(token="fake-token")
    page = client.fetch_user_notes("user123")
    assert page["has_more"] is True
    assert page["cursor"] == "cursor_after_note_bbb"
    assert [n["note_id"] for n in page["notes"]] == ["note_aaa", "note_bbb"]
    assert page["notes"][0]["liked_count"] == 42
    assert page["notes"][0]["cover"] == "http://example.com/a.jpg"


@mock.patch("scripts.utils.tikhub_client.urllib.request.urlopen")
def test_fetch_note_detail_falls_back_from_image_to_video_endpoint(mock_urlopen):
    """图文详情接口对视频笔记返回空 note_list 时，应视为失败并降级到视频详情接口。"""
    empty_image_payload = {"code": 200, "data": {"data": [{"note_list": [], "comment_list": []}]}}
    video_payload = {
        "code": 200,
        "data": {
            "data": [{
                "note_list": [{
                    "id": "note_video_1", "title": "视频笔记标题", "desc": "视频笔记正文",
                    "type": "video", "time": 1700000000, "liked_count": 88,
                    "collected_count": 10, "comments_count": 5, "shared_count": 2,
                    "hash_tag": ["tagA", "tagB"], "images_list": [{"url": "http://example.com/cover.jpg"}],
                }],
                "comment_list": [],
            }],
        },
    }
    mock_urlopen.side_effect = [_fake_response(empty_image_payload), _fake_response(video_payload)]
    client = tc.TikHubClient(token="fake-token")
    detail = client.fetch_note_detail("note_video_1")
    assert detail["title"] == "视频笔记标题"
    assert detail["liked_count"] == 88
    assert detail["tags"] == ["tagA", "tagB"]


@mock.patch("scripts.utils.tikhub_client.urllib.request.urlopen")
def test_fetch_note_detail_skips_web_v3_fallback_when_xsec_token_missing(mock_urlopen):
    """两个 app_v2 端点都失败、且没有 xsec_token 时，不应再尝试注定 422 的 web_v3 端点。"""
    err = tc.urllib.error.HTTPError(
        url="https://api.tikhub.io", code=404, msg="Not Found",
        hdrs=None, fp=mock.MagicMock(read=lambda: b'{"detail":"Not Found"}'),
    )
    mock_urlopen.side_effect = [err, err]
    client = tc.TikHubClient(token="fake-token")
    try:
        client.fetch_note_detail("note_missing", xsec_token="")
        assert False, "expected TikHubError"
    except tc.TikHubError:
        pass
    assert mock_urlopen.call_count == 2


@mock.patch("scripts.utils.tikhub_client.urllib.request.urlopen")
def test_get_retries_once_on_429_then_raises_tikhub_error(mock_urlopen):
    """429 应重试一次；若仍失败，必须抛出 TikHubError 而不是裸 HTTPError。"""
    err = tc.urllib.error.HTTPError(
        url="https://api.tikhub.io", code=429, msg="Too Many Requests",
        hdrs=None, fp=mock.MagicMock(read=lambda: b'{"detail":"rate limited"}'),
    )
    mock_urlopen.side_effect = [err, err]
    client = tc.TikHubClient(token="fake-token")
    client._min_interval = 0.01
    try:
        client._get("/api/v1/xiaohongshu/web_v3/fetch_user_info", {"user_id": "user123"})
        assert False, "expected TikHubError"
    except tc.TikHubError as exc:
        assert exc.status_code == 429
    assert mock_urlopen.call_count == 2
