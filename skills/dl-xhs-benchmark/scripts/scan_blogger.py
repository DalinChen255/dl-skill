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
