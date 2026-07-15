"""Phase 0: 环境检查——Python 版本 + TikHub Token 分层读取。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils import common


def check_python_version():
    if sys.version_info < (3, 9):
        print(f"❌ 需要 Python 3.9+，当前版本：{sys.version}")
        return False
    print(f"✅ Python 版本：{sys.version.split()[0]}")
    return True


def check_token():
    token = common.load_token()
    if token:
        print("✅ TikHub Token 已配置")
        return True
    print(
        "⚠️ 未检测到 TikHub API Token。\n"
        "请先注册并获取 Token：https://user.tikhub.io/register?ref=QYnybFaK\n"
        "注册后登录控制台 → API 权限，勾选全部小红书相关端点，生成 Token。\n"
        "获取后可通过环境变量 TIKHUB_API_TOKEN 设置，或在下方直接输入以保存到本地配置文件。"
    )
    try:
        token = input("请输入 TikHub API Token（回车跳过）：").strip()
    except EOFError:
        token = ""
    if token:
        common.save_token(token)
        print(f"✅ Token 已保存到 {common.CONFIG_FILE}")
        return True
    return False


def main():
    ok_python = check_python_version()
    ok_token = check_token()
    if ok_python and ok_token:
        print("✅ 环境准备完成")
        sys.exit(0)
    sys.exit(1)


if __name__ == "__main__":
    main()
