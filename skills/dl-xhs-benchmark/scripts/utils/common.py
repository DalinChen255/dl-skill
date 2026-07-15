"""dl-xhs-benchmark 共用工具：链接解析、配置与 JSON 读写。"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_DIR = Path.home() / ".dl-xhs-benchmark"
CONFIG_FILE = CONFIG_DIR / "tikhub_config.json"

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

_PROFILE_RE = re.compile(r"xiaohongshu\.com/user/profile/([0-9a-fA-F]{20,32})")
_NOTE_RE = re.compile(r"xiaohongshu\.com/(?:explore|discovery/item)/([0-9a-fA-F]{20,32})")
_SHORT_LINK_RE = re.compile(r"xhslink\.com/")


def resolve_short_link(url: str) -> str:
    """跟随重定向解析 xhslink.com 短链，返回最终 URL；非短链原样返回。"""
    if not _SHORT_LINK_RE.search(url):
        return url
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": BROWSER_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.url
    except urllib.error.HTTPError as exc:
        return getattr(exc, "url", url) or url


def parse_xhs_user_link(url: str) -> str:
    """从小红书主页链接或分享短链中提取 user_id。"""
    resolved = resolve_short_link(url)
    match = _PROFILE_RE.search(resolved)
    if not match:
        raise ValueError(
            f"无法从链接中解析出小红书用户 ID：{url}\n"
            "请确认链接是博主主页链接（形如 https://www.xiaohongshu.com/user/profile/<id>）"
            "或分享短链（形如 http://xhslink.com/xxxx）"
        )
    return match.group(1)


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


def get_config_dir() -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR


def load_token():
    import os

    env_token = os.environ.get("TIKHUB_API_TOKEN")
    if env_token:
        return env_token
    if CONFIG_FILE.exists():
        data = load_json(CONFIG_FILE)
        token = data.get("tikhub_api_token")
        if token:
            return token
    return None


def save_token(token: str) -> None:
    get_config_dir()
    data = {}
    if CONFIG_FILE.exists():
        data = load_json(CONFIG_FILE)
    data["tikhub_api_token"] = token
    save_json(CONFIG_FILE, data)


def load_json(path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
