"""Phase 0.5: 解析链接 + 分页扫描博主全部笔记 → 计算三档动态采集量。"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils import common
from scripts.utils.tikhub_client import TikHubClient, TikHubError


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

    total_notes = len(notes_list)
    tiers = common.compute_collection_tiers(total_notes)

    result = {
        "user_id": user_id,
        "profile": profile,
        "notes_list": notes_list,
        "total_notes": total_notes,
        "tiers": tiers,
    }
    out_path = Path(output_dir) / f"{user_id}_scan.json"
    common.save_json(out_path, result)
    return result


def main():
    parser = argparse.ArgumentParser(description="扫描小红书博主总笔记数并计算三档采集量")
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
    tiers = result["tiers"]
    print(f"✅ 博主：{profile.get('nickname', '(未知昵称)')}（{result['user_id']}）")
    print(f"✅ 共扫描到 {result['total_notes']} 篇笔记")
    print("请选择采集档位：")
    print(f"  快速档：{tiers['快速']} 篇")
    print(f"  推荐档：{tiers['推荐']} 篇")
    print(f"  深度档：{tiers['深度']} 篇（全量）")


if __name__ == "__main__":
    main()
