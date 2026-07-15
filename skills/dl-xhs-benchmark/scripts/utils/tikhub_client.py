"""dl-xhs-benchmark 的 TikHub REST 客户端，仅覆盖小红书三个端点，主选+备选两级降级。"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import common

BASE_URL = "https://api.tikhub.io"
DEFAULT_RPS = 5
RATE_SAFETY_MARGIN = 0.8


class TikHubError(Exception):
    def __init__(self, message, status_code=None, response_body=""):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class TikHubClient:
    def __init__(self, token=None):
        self.token = token if token is not None else common.load_token()
        if not self.token:
            raise TikHubError("未设置 TikHub API Token", status_code=401, response_body="")
        self._min_interval = 1.0 / (DEFAULT_RPS * RATE_SAFETY_MARGIN)
        self._last_request_at = 0.0

    def _throttle(self):
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _get(self, path, params):
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        url = f"{BASE_URL}{path}?{query}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": common.BROWSER_USER_AGENT,
        }
        for attempt in range(2):
            self._throttle()
            req = urllib.request.Request(url, method="GET", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.fp.read().decode("utf-8", errors="ignore") if exc.fp else ""
                if exc.code == 429 and attempt == 0:
                    time.sleep(self._min_interval * 2)
                    continue
                raise TikHubError(f"TikHub 请求失败：HTTP {exc.code}", status_code=exc.code, response_body=body)

    _COUNT_MULTIPLIERS = {"万": 10_000, "亿": 100_000_000, "千": 1_000, "w": 10_000, "k": 1_000}

    @classmethod
    def _normalize_count(cls, value):
        if value is None or value == "":
            return 0
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip().replace(",", "").rstrip("+")
        for suffix, multiplier in cls._COUNT_MULTIPLIERS.items():
            if text.lower().endswith(suffix):
                try:
                    return int(float(text[: -len(suffix)]) * multiplier)
                except ValueError:
                    return 0
        try:
            return int(float(text))
        except ValueError:
            return 0

    @staticmethod
    def _extract_avatar(d):
        return d.get("images") or d.get("imageb") or ""

    @staticmethod
    def _normalize_tags(tags):
        return [t.get("name", t) if isinstance(t, dict) else t for t in (tags or [])]

    @staticmethod
    def _count_images(images):
        return len(images) if isinstance(images, list) else 0

    @staticmethod
    def _dig(d, *path, default=None):
        cur = d
        for p in path:
            if isinstance(cur, dict):
                cur = cur.get(p)
            else:
                return default
            if cur is None:
                return default
        return cur

    # 401 未授权 / 402 余额不足对每个端点都会同样失败，命中即应立即报错，
    # 不必浪费请求逐个尝试降级端点。
    _FATAL_STATUS_CODES = {401, 402}

    def _get_with_fallback(self, attempts):
        """依次尝试 attempts 中的 (path, params, parser)，返回第一个成功结果的解析值。"""
        last_error = None
        for path, params, parser in attempts:
            try:
                raw = self._get(path, params)
                return parser(raw)
            except TikHubError as exc:
                if exc.status_code in self._FATAL_STATUS_CODES:
                    raise
                last_error = exc
        raise last_error

    def fetch_user_info(self, user_id):
        return self._get_with_fallback([
            ("/api/v1/xiaohongshu/web_v3/fetch_user_info", {"user_id": user_id}, self._parse_user_info_web_v3),
            ("/api/v1/xiaohongshu/app_v2/get_user_info", {"user_id": user_id}, self._parse_user_info_app_v2),
        ])

    def _parse_user_info_web_v3(self, raw):
        basic = self._dig(raw, "data", "data", "basic_info", default={}) or {}
        interactions = self._dig(raw, "data", "data", "interactions", default=[]) or []
        counts = {item.get("type"): self._normalize_count(item.get("count")) for item in interactions}
        return {
            "nickname": basic.get("nickname", ""),
            "red_id": basic.get("red_id", ""),
            "desc": basic.get("desc", ""),
            "gender": basic.get("gender", 0),
            "ip_location": basic.get("ip_location", ""),
            "avatar": self._extract_avatar(basic),
            "fans": counts.get("fans", 0),
            "follows": counts.get("follows", 0),
            "liked_and_collected": counts.get("interaction", 0),
        }

    def _parse_user_info_app_v2(self, raw):
        data = self._dig(raw, "data", "data", default={}) or {}
        return {
            "nickname": data.get("nickname", ""),
            "red_id": data.get("red_id", ""),
            "desc": data.get("desc", ""),
            "gender": data.get("gender", 0),
            "ip_location": data.get("ip_location", ""),
            "avatar": self._extract_avatar(data),
            "fans": self._normalize_count(data.get("fans")),
            "follows": self._normalize_count(data.get("follows")),
            "liked_and_collected": self._normalize_count(data.get("liked")) + self._normalize_count(data.get("collected")),
        }

    def fetch_user_notes(self, user_id, cursor=""):
        raw = self._get(
            "/api/v1/xiaohongshu/app_v2/get_user_posted_notes",
            {"user_id": user_id, "cursor": cursor},
        )
        return self._parse_user_notes_app_v2(raw)

    def _parse_user_notes_app_v2(self, raw):
        data = self._dig(raw, "data", "data", default={}) or {}
        items = data.get("notes", []) or []
        notes = []
        for item in items:
            images = item.get("images_list") or []
            cover = (images[0].get("url_size_large") or images[0].get("url", "")) if images else ""
            notes.append({
                "note_id": item.get("id", ""),
                "title": item.get("display_title") or item.get("title", ""),
                "type": item.get("type", ""),
                "liked_count": self._normalize_count(item.get("likes")),
                "cover": cover,
                "xsec_token": item.get("xsec_token", ""),
            })
        next_cursor = items[-1].get("cursor", "") if items else ""
        return {"has_more": bool(data.get("has_more", False)), "cursor": next_cursor, "notes": notes}

    def fetch_note_detail(self, note_id, xsec_token=""):
        attempts = [
            (
                "/api/v1/xiaohongshu/app_v2/get_image_note_detail",
                {"note_id": note_id},
                lambda raw: self._parse_note_detail_app(raw, note_id),
            ),
            (
                "/api/v1/xiaohongshu/app_v2/get_video_note_detail",
                {"note_id": note_id},
                lambda raw: self._parse_note_detail_app(raw, note_id),
            ),
        ]
        if xsec_token:
            attempts.append((
                "/api/v1/xiaohongshu/web_v3/fetch_note_detail",
                {"note_id": note_id, "xsec_token": xsec_token},
                lambda raw: self._parse_note_detail_web_v3(raw, note_id),
            ))
        return self._get_with_fallback(attempts)

    def _extract_note_raw_app(self, raw):
        data = self._dig(raw, "data", "data", default=None)
        if data is None:
            data = self._dig(raw, "data", default={}) or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            return {}, []
        comment_list = data.get("comment_list", []) or []
        if "note_list" in data:
            # note_list 存在但为空代表“查错端点、没有这篇笔记”，不能把外层包装
            # 壳子（只有 note_list/comment_list 键）误当成笔记数据。
            note_list = data.get("note_list") or []
            note_raw = note_list[0] if note_list else {}
        else:
            note_raw = data
        return note_raw or {}, comment_list

    def _parse_note_detail_app(self, raw, note_id):
        note_raw, comment_list = self._extract_note_raw_app(raw)
        if not note_raw:
            raise TikHubError(
                f"未获取到笔记详情（note_id={note_id} 可能是笔记类型不匹配）",
                status_code=None, response_body="",
            )
        tags = note_raw.get("tag_list") or note_raw.get("hash_tag") or []
        images = note_raw.get("images_list") or note_raw.get("image_list") or []
        return {
            "note_id": note_raw.get("note_id", note_raw.get("id", note_id)),
            "title": note_raw.get("title", note_raw.get("display_title", "")),
            "desc": note_raw.get("desc", note_raw.get("content", "")),
            "type": note_raw.get("type", "normal"),
            "create_time": note_raw.get("time", note_raw.get("create_time", 0)),
            "liked_count": self._normalize_count(note_raw.get("liked_count")),
            "collected_count": self._normalize_count(note_raw.get("collected_count")),
            "comment_count": self._normalize_count(note_raw.get("comments_count", note_raw.get("comment_count"))),
            "share_count": self._normalize_count(note_raw.get("shared_count")),
            "tags": self._normalize_tags(tags),
            "image_count": self._count_images(images),
            "has_video": bool(note_raw.get("video")),
            "comments": [
                {"content": c.get("content", "")}
                for c in comment_list if isinstance(c, dict) and c.get("content")
            ],
        }

    def _parse_note_detail_web_v3(self, raw, note_id):
        items = self._dig(raw, "data", "data", "items", default=[]) or []
        if not items:
            return {
                "note_id": note_id, "title": "", "desc": "", "type": "normal", "create_time": 0,
                "liked_count": 0, "collected_count": 0, "comment_count": 0, "share_count": 0,
                "tags": [], "image_count": 0, "has_video": False, "comments": [],
            }
        card = items[0].get("noteCard", {}) or {}
        interact = card.get("interactInfo", {}) or {}
        tags = card.get("tagList") or []
        images = card.get("imageList") or []
        return {
            "note_id": items[0].get("id", note_id),
            "title": card.get("title", ""),
            "desc": card.get("desc", ""),
            "type": card.get("type", "normal"),
            "create_time": card.get("time", 0),
            "liked_count": self._normalize_count(interact.get("likedCount")),
            "collected_count": self._normalize_count(interact.get("collectedCount")),
            "comment_count": self._normalize_count(interact.get("commentCount")),
            "share_count": self._normalize_count(interact.get("shareCount")),
            "tags": self._normalize_tags(tags),
            "image_count": self._count_images(images),
            "has_video": bool(card.get("video")),
            "comments": [],
        }
