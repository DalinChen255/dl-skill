"""dl-xhs-distill 一键运行入口：串联 Phase 0 → Phase 3 Step A。"""

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.utils import common

TIER_NAMES = ("快速", "推荐", "深度")


def run_step(cmd: list):
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description="dl-xhs-distill 一键运行入口")
    parser.add_argument("link", help="小红书博主主页链接或分享短链")
    parser.add_argument("--tier", choices=TIER_NAMES, required=True, help="采集档位：快速/推荐/深度")
    parser.add_argument("-o", "--output", default="./output", help="最终产出物目录，默认 ./output")
    parser.add_argument("--data-dir", default="./data", help="中间数据目录，默认 ./data")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    run_step([sys.executable, str(root / "scripts" / "check_env.py")])
    run_step([sys.executable, str(root / "scripts" / "scan_blogger.py"), args.link, "-o", args.data_dir])

    user_id = common.parse_xhs_user_link(args.link)
    scan_path = Path(args.data_dir) / f"{user_id}_scan.json"
    scan_data = common.load_json(scan_path)
    tier_count = scan_data["tiers"][args.tier]

    run_step([
        sys.executable, str(root / "scripts" / "crawl_blogger.py"), user_id,
        "--count", str(tier_count), "-o", args.data_dir,
    ])
    details_path = Path(args.data_dir) / f"{user_id}_notes_details.json"
    run_step([sys.executable, str(root / "scripts" / "analyze.py"), str(details_path), "-o", args.data_dir])

    analysis_path = Path(args.data_dir) / f"{user_id}_analysis.json"
    blogger_name = scan_data["profile"].get("nickname") or user_id
    run_step([
        sys.executable, str(root / "scripts" / "deep_analyze.py"),
        str(analysis_path), blogger_name, "-o", args.output, "--scan", str(scan_path),
    ])

    print(
        f"\n✅ Phase 0-3 Step A 完成。请让宿主 AI 读取 "
        f"{args.output}/{blogger_name}_AI蒸馏任务.md 生成最终的 HTML 报告与创作 Skill 文件夹。"
    )


if __name__ == "__main__":
    main()
