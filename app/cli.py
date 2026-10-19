"""Local-only administrator initialization for headless/container installations."""
import argparse
import getpass
import os
from pathlib import Path

from app.storage import Store, password_hash


def main():
    parser = argparse.ArgumentParser(description="Session Watch 本地管理员初始化")
    parser.add_argument("command", choices=["init-admin"])
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("SESSION_WATCH_DATA", "data")))
    args = parser.parse_args()
    store = Store(args.data_dir)
    try:
        if store.get("admin"):
            parser.error("管理员已存在。请通过页面修改密码。")
        username = input("管理员账号: ").strip()
        password = getpass.getpass("密码（至少 12 字符）: ")
        if not username or len(username) > 80 or not 12 <= len(password) <= 256:
            parser.error("账号或密码长度不符合要求")
        if password != getpass.getpass("确认密码: "):
            parser.error("两次密码不一致")
        store.set("admin", {"username": username, "password": password_hash(password)})
        print("管理员已创建。")
    finally:
        store.close()


if __name__ == "__main__":
    main()

