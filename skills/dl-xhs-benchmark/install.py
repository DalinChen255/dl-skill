"""dl-xhs-benchmark 环境安装脚本。本工具仅依赖 Python 标准库，无需安装第三方包。"""

import sys


def main():
    if sys.version_info < (3, 9):
        print(f"❌ 需要 Python 3.9+，当前版本：{sys.version}")
        sys.exit(1)
    print("✅ dl-xhs-benchmark 仅依赖 Python 标准库，无需额外安装依赖")
    print("✅ 环境检查通过，可直接使用（首次运行会引导配置 TikHub API Token）")


if __name__ == "__main__":
    main()
