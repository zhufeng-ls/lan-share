import getpass
import sys

from .admin_security import password_hash
from .main import database


def main():
    if not sys.stdin.isatty():
        raise SystemExit("请在交互终端运行，密码不会显示或保存到命令历史。")
    print("设置管理员 admin 的密码；普通用户和文件不受影响。")
    password = getpass.getpass("新密码（至少 10 位）：")
    if len(password) < 10 or len(password) > 256:
        raise SystemExit("密码长度需为 10–256 位。")
    if password != getpass.getpass("再次输入新密码："):
        raise SystemExit("两次密码不一致，未修改。")
    hashed = password_hash(password)
    with database() as db:
        cursor = db.execute("UPDATE administrators SET password_hash=?, failures=0, locked_until=0 WHERE id=1", (hashed,))
        if cursor.rowcount != 1:
            raise SystemExit("请先启动新版文件站，再设置密码。")
        db.execute("DELETE FROM admin_sessions")
    print("管理员密码已设置，旧的管理员登录凭证已失效。首页输入 admin 后即可验证密码。")


if __name__ == "__main__":
    main()
