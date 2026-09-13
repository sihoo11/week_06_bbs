import sqlite3
from pathlib import Path

DATABASE = Path(__file__).resolve().parent / 'bbs.db'

def make_admin(username):
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row

    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    if user is None:
        print(f"❌ '{username}' 사용자가 없습니다.")
        conn.close()
        return False

    try:
        conn.execute("UPDATE users SET is_admin = 1 WHERE username = ?", (username,))
        conn.commit()
        print(f"✅ '{username}' 사용자를 관리자로 설정했습니다.")
        conn.close()
        return True
    except Exception as e:
        print(f"❌ 오류 발생: {e}")
        conn.close()
        return False

def remove_admin(username):
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row

    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    if user is None:
        print(f"❌ '{username}' 사용자가 없습니다.")
        conn.close()
        return False

    try:
        conn.execute("UPDATE users SET is_admin = 0 WHERE username = ?", (username,))
        conn.commit()
        print(f"✅ '{username}' 사용자의 관리자 권한을 제거했습니다.")
        conn.close()
        return True
    except Exception as e:
        print(f"❌ 오류 발생: {e}")
        conn.close()
        return False

def list_admins():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row

    admins = conn.execute("SELECT username FROM users WHERE is_admin = 1").fetchall()
    conn.close()

    if not admins:
        print("관리자가 없습니다.")
    else:
        print("현재 관리자:")
        for admin in admins:
            print(f"  - {admin['username']}")

if __name__ == '__main__':
    import sys

    if len(sys.argv) < 2:
        print("사용법:")
        print("  python make_admin.py <username>          # 관리자 추가")
        print("  python make_admin.py remove <username>   # 관리자 제거")
        print("  python make_admin.py list                # 관리자 목록")
        sys.exit(1)

    command = sys.argv[1]

    if command == "list":
        list_admins()
    elif command == "remove":
        if len(sys.argv) < 3:
            print("사용법: python make_admin.py remove <username>")
            sys.exit(1)
        remove_admin(sys.argv[2])
    else:
        make_admin(command)
