"""dl-xhs-benchmark 一键运行入口：串联 Phase 0 → Phase 3 Step A。"""

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.crawl_notes import build_batch_id
from scripts.utils import common

DEFAULT_COUNT = 20


def run_step(cmd: list):
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description="dl-xhs-benchmark 一键运行入口")
    parser.add_argument("link", nargs="?", default=None, help="模式一：小红书博主主页链接或分享短链")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT, help=f"模式一采集篇数，默认按点赞取前 {DEFAULT_COUNT} 篇")
    parser.add_argument("--notes", nargs="+", default=None, help="模式二：指定笔记链接列表，一次性传入多个")
    parser.add_argument("-o", "--output", default="./output", help="最终产出物目录，默认 ./output")
    parser.add_argument("--data-dir", default="./data", help="中间数据目录，默认 ./data")
    args = parser.parse_args()

    if bool(args.link) == bool(args.notes):
        print("❌ 请二选一：要么传一个博主链接（模式一），要么用 --notes 传指定笔记链接列表（模式二）")
        sys.exit(1)

    root = Path(__file__).resolve().parent
    run_step([sys.executable, str(root / "scripts" / "check_env.py")])

    if args.link:
        run_step([sys.executable, str(root / "scripts" / "scan_blogger.py"), args.link, "-o", args.data_dir])
        file_id = common.parse_xhs_user_link(args.link)
        scan_path = Path(args.data_dir) / f"{file_id}_scan.json"
        scan_data = common.load_json(scan_path)
        name = scan_data["profile"].get("nickname") or file_id

        run_step([
            sys.executable, str(root / "scripts" / "crawl_blogger.py"), file_id,
            "--count", str(args.count), "-o", args.data_dir,
        ])
        scan_arg = ["--scan", str(scan_path)]
    else:
        file_id = build_batch_id(args.notes)
        name = file_id
        run_step([
            sys.executable, str(root / "scripts" / "crawl_notes.py"), *args.notes, "-o", args.data_dir,
        ])
        scan_arg = []

    details_path = Path(args.data_dir) / f"{file_id}_notes_details.json"
    run_step([sys.executable, str(root / "scripts" / "analyze.py"), str(details_path), "-o", args.data_dir])

    analysis_path = Path(args.data_dir) / f"{file_id}_analysis.json"
    run_step([
        sys.executable, str(root / "scripts" / "deep_analyze.py"),
        str(analysis_path), name, "-o", args.output, *scan_arg,
    ])

    print(
        f"\n✅ Phase 0-3 Step A 完成。请让宿主 AI 读取 "
        f"{args.output}/{name}_AI拆解任务.md 生成最终的 HTML 报告与写作指南 Skill 文件夹。"
    )


if __name__ == "__main__":
    main()
