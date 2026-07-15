"""Phase 1: 按指定篇数采集笔记详情，每 10 条自动 checkpoint。"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils import common
from scripts.utils.tikhub_client import TikHubClient, TikHubError

CHECKPOINT_EVERY = 10


def crawl(user_id: str, count: int, output_dir: str) -> list:
    scan_path = Path(output_dir) / f"{user_id}_scan.json"
    if not scan_path.exists():
        raise FileNotFoundError(f"找不到扫描结果 {scan_path}，请先运行 scan_blogger.py")
    scan_data = common.load_json(scan_path)
    target_notes = scan_data["notes_list"][:count]

    checkpoint_path = Path(output_dir) / f"{user_id}_crawl_checkpoint.json"
    details = []
    done_ids = set()
    if checkpoint_path.exists():
        checkpoint = common.load_json(checkpoint_path)
        details = checkpoint.get("details", [])
        done_ids = {d["note_id"] for d in details}
        print(f"↻ 从断点恢复，已完成 {len(done_ids)} 篇")

    client = TikHubClient()
    for i, note in enumerate(target_notes, start=1):
        note_id = note["note_id"]
        if note_id in done_ids:
            continue
        detail = client.fetch_note_detail(note_id, xsec_token=note.get("xsec_token", ""))
        detail["liked_count_from_list"] = note.get("liked_count", 0)
        details.append(detail)
        done_ids.add(note_id)
        print(f"  [{i}/{len(target_notes)}] {detail.get('title', '')[:30]}")
        if len(details) % CHECKPOINT_EVERY == 0:
            common.save_json(checkpoint_path, {"details": details})

    out_path = Path(output_dir) / f"{user_id}_notes_details.json"
    common.save_json(out_path, details)
    if checkpoint_path.exists():
        checkpoint_path.unlink()
    return details


def main():
    parser = argparse.ArgumentParser(description="按指定篇数采集小红书笔记详情")
    parser.add_argument("user_id", help="小红书用户 ID（来自 scan_blogger.py 的输出）")
    parser.add_argument("--count", type=int, required=True, help="本次要采集的笔记篇数")
    parser.add_argument("-o", "--output", default="./data", help="输出目录，默认 ./data")
    args = parser.parse_args()

    try:
        details = crawl(args.user_id, args.count, args.output)
    except FileNotFoundError as exc:
        print(f"❌ {exc}")
        sys.exit(1)
    except TikHubError as exc:
        print(f"❌ TikHub 请求失败（HTTP {exc.status_code}）：{exc}")
        print("已采集部分已保存到断点文件，重新运行本命令可继续。")
        sys.exit(1)

    print(f"✅ 采集完成，共 {len(details)} 篇笔记详情")


if __name__ == "__main__":
    main()
