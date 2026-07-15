"""Phase 1（模式二）：按用户提供的笔记链接列表逐条采集详情，每篇自动 checkpoint。"""

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils import common
from scripts.utils.tikhub_client import TikHubClient, TikHubError


def build_batch_id(link_count: int, today: date = None) -> str:
    """生成模式二产出物的批次标识：日期 + 篇数，笔记可能来自不同博主，没有单一博主名可用。"""
    today = today or date.today()
    return f"{today:%Y%m%d}_{link_count}notes"


def crawl(links: list, output_dir: str) -> tuple:
    batch_id = build_batch_id(len(links))
    checkpoint_path = Path(output_dir) / f"{batch_id}_crawl_checkpoint.json"
    details = []
    done_ids = set()
    if checkpoint_path.exists():
        checkpoint = common.load_json(checkpoint_path)
        details = checkpoint.get("details", [])
        done_ids = {d["note_id"] for d in details}
        print(f"↻ 从断点恢复，已完成 {len(done_ids)} 篇")

    client = TikHubClient()
    for i, link in enumerate(links, start=1):
        parsed = common.parse_xhs_note_link(link)
        note_id = parsed["note_id"]
        if note_id in done_ids:
            continue
        detail = client.fetch_note_detail(note_id, xsec_token=parsed.get("xsec_token", ""))
        details.append(detail)
        done_ids.add(note_id)
        print(f"  [{i}/{len(links)}] {detail.get('title', '')[:30]}")
        common.save_json(checkpoint_path, {"details": details})

    out_path = Path(output_dir) / f"{batch_id}_notes_details.json"
    common.save_json(out_path, details)
    if checkpoint_path.exists():
        checkpoint_path.unlink()
    return details, batch_id


def main():
    parser = argparse.ArgumentParser(description="按指定笔记链接列表采集详情")
    parser.add_argument("links", nargs="+", help="笔记链接列表，一个链接一个参数")
    parser.add_argument("-o", "--output", default="./data", help="输出目录，默认 ./data")
    args = parser.parse_args()

    try:
        details, batch_id = crawl(args.links, args.output)
    except ValueError as exc:
        print(f"❌ {exc}")
        sys.exit(1)
    except TikHubError as exc:
        print(f"❌ TikHub 请求失败（HTTP {exc.status_code}）：{exc}")
        print("已采集部分已保存到断点文件，重新运行本命令可继续。")
        sys.exit(1)

    print(f"✅ 采集完成，共 {len(details)} 篇笔记详情，批次标识：{batch_id}")


if __name__ == "__main__":
    main()
