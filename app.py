import base64
import os
import re
import sqlite3
import uuid
from collections import defaultdict
from pathlib import Path

import openai
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
from openai import OpenAI
from werkzeug.security import check_password_hash, generate_password_hash

import neis
from opendata import fetch_air_quality

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
socketio = SocketIO(app, async_mode="threading")

client = OpenAI()
MODEL = os.environ.get("OPENAI_MODEL") or "gpt-4.1-mini"

SYSTEM_PROMPT = (
    "너는 학교생활 도우미 '반장'이야. "
    "오직 제공된 공지와 데이터에 근거해서만 답변해. "
    "공지에 명시되지 않은 날짜, 장소, 준비물은 절대로 추측하지 말고 '확인 필요'라고 표시해."
)

CHAT_PROMPT = (
    "너는 학교생활 도우미 '반장'이야. 친절하고 간결하게 답해. "
    "사용자가 대화 중에 알려준 공지·일정·준비물은 기억해서 답에 사용해. "
    "대화에 없는 날짜, 장소, 준비물은 절대로 추측하지 말고 '확인 필요'라고 표시해."
)

IMAGE_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
MAX_HISTORY = 20
user_chats: dict[str, list[dict]] = {}

# room_id(str) -> {sid: username}, 서버 메모리에만 유지되는 접속자 목록
room_online_users = defaultdict(dict)

DATABASE = Path(__file__).resolve().parent / 'bbs.db'

TIMEOUT_DURATIONS = {"5": "+5 minutes", "10": "+10 minutes", "60": "+1 hours"}
ROOM_PERMISSION_KEYS = ["manage_room", "manage_members", "manage_messages", "announce", "manage_roles"]
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class AIError(Exception):
    """사용자 화면 표시용 AI 에러 클래스"""


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def get_post_or_404(post_id):
    conn = get_db()
    post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    conn.close()
    return post


def is_room_member(conn, room_id, username):
    member = conn.execute(
        "SELECT 1 FROM chat_room_members WHERE room_id = ? AND username = ?",
        (room_id, username),
    ).fetchone()
    return member is not None


def is_timed_out(conn, room_id, username):
    row = conn.execute("""
        SELECT 1 FROM chat_room_members
        WHERE room_id = ? AND username = ?
          AND timeout_until IS NOT NULL
          AND timeout_until > datetime('now', 'localtime')
    """, (room_id, username)).fetchone()
    return row is not None


def get_effective_permissions(conn, room, username):
    if bool(session.get("is_admin")) or room["created_by"] == username:
        return {key: True for key in ROOM_PERMISSION_KEYS}

    role = conn.execute("""
        SELECT r.* FROM chat_room_members m
        JOIN chat_room_roles r ON r.id = m.role_id
        WHERE m.room_id = ? AND m.username = ?
    """, (room["id"], username)).fetchone()

    if role is None:
        return {key: False for key in ROOM_PERMISSION_KEYS}
    return {key: bool(role[f"perm_{key}"]) for key in ROOM_PERMISSION_KEYS}


def create_tables():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            user_id INTEGER,
            is_notice INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            user_id INTEGER,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (post_id) REFERENCES posts(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_by TEXT,
            is_public INTEGER NOT NULL DEFAULT 0,
            slow_mode_seconds INTEGER NOT NULL DEFAULT 0,
            pinned_message_id INTEGER,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_room_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            timeout_until TEXT,
            UNIQUE(room_id, username)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_room_roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#6b7280',
            perm_manage_room INTEGER NOT NULL DEFAULT 0,
            perm_manage_members INTEGER NOT NULL DEFAULT 0,
            perm_manage_messages INTEGER NOT NULL DEFAULT 0,
            perm_announce INTEGER NOT NULL DEFAULT 0,
            perm_manage_roles INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    # 8주차까지 없던 컬럼을 이어쓰는 DB에 추가 (한 번만 실행됨)
    for statement in [
        "ALTER TABLE posts ADD COLUMN user_id INTEGER",
        "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'",
        "ALTER TABLE posts ADD COLUMN is_notice INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE chat_messages ADD COLUMN room_id INTEGER",
        "ALTER TABLE chat_room_members ADD COLUMN timeout_until TEXT",
        "ALTER TABLE chat_rooms ADD COLUMN is_public INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE chat_rooms ADD COLUMN slow_mode_seconds INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE chat_rooms ADD COLUMN pinned_message_id INTEGER",
        "ALTER TABLE chat_rooms ADD COLUMN announcement TEXT",
        "ALTER TABLE chat_room_members ADD COLUMN role_id INTEGER",
    ]:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


def ask(user_input, instructions=SYSTEM_PROMPT) -> str:
    try:
        response = client.responses.create(
            model=MODEL,
            instructions=instructions,
            input=user_input,
            temperature=0.2,
        )
    except openai.AuthenticationError:
        raise AIError("API 키가 올바르지 않아요. OPENAI_API_KEY 환경 변수를 확인하세요.")
    except openai.RateLimitError:
        raise AIError("요청이 너무 많거나 사용 한도를 넘었어요. 잠시 뒤에 다시 시도하세요.")
    except openai.OpenAIError as e:
        raise AIError(f"AI 호출 중 오류가 났어요. ({type(e).__name__})")
    return response.output_text


def get_user_key() -> str:
    if "user_key" not in session:
        session["user_key"] = str(uuid.uuid4())
    return session["user_key"]


def chat_with_history(user_key: str, message: str) -> str:
    history = user_chats.setdefault(user_key, [])
    history.append({"role": "user", "content": message})
    try:
        reply = ask(history, instructions=CHAT_PROMPT)
    except AIError:
        history.pop()
        raise
    history.append({"role": "assistant", "content": reply})
    del history[:-MAX_HISTORY]
    return reply


def brief_meal(school_name: str, meal: dict) -> str:
    allergy_table = ", ".join(f"{n}.{name}" for n, name in neis.ALLERGY_CODES.items())
    prompt = (
        f"아래는 나이스(NEIS) 공식 API에서 가져온 {school_name}의 오늘 {meal['meal_name']} 원문이야.\n"
        "이 원문에만 근거해서 답하고, 원문에 없는 메뉴·수치는 지어내지 말고 '확인 필요'라고 써.\n"
        f"[알레르기 번호표] {allergy_table}\n"
        f"[메뉴 원문]\n{meal['menu']}\n"
        f"[칼로리] {meal['calorie'] or '정보 없음'}\n"
        f"[영양정보]\n{meal['nutrition'] or '정보 없음'}\n\n"
        "1) 오늘 급식을 1~2문장으로 요약해줘.\n"
        "2) 메뉴 뒤 괄호 숫자는 알레르기 번호야. 번호표로 풀이해서 주의할 메뉴를 알려줘.\n"
        "3) 칼로리·영양정보가 있으면 짧게 해설해줘.\n"
        "마크다운 기호(#, *, **)는 쓰지 말고 일반 문장과 줄바꿈으로만 써."
    )
    return ask(prompt)


def summarize_notice(notice: str) -> str:
    prompt = (
        "아래 공지를 [일정 / 할 일 / 준비물] 순서로 간결하게 정리해줘.\n"
        "공지에 없는 항목은 '확인 필요'라고 써. 마크다운 기호는 쓰지 마.\n"
        f"공지: {notice}"
    )
    return ask(prompt)


def analyze_image(file_storage) -> str:
    ext = os.path.splitext(file_storage.filename or "")[1].lower()
    mime = IMAGE_MIME.get(ext)
    if mime is None:
        raise AIError("JPG 또는 PNG 사진만 올릴 수 있어요.")

    image_b64 = base64.b64encode(file_storage.read()).decode("ascii")
    prompt = (
        "이 시간표/안내문 사진을 분석해 줘. "
        "1) 전체 일정이나 시간표 목록을 텍스트로 표기하고, "
        "2) 챙겨야 할 준비물을 요약해 줘. "
        "글자가 흐려 식별할 수 없는 부분은 '확인 필요'라고 표시하고, 사진에 없는 내용은 절대 지어내지 마. "
        "마크다운 기호는 쓰지 마."
    )
    message = [{
        "role": "user",
        "content": [
            {"type": "input_text", "text": prompt},
            {"type": "input_image", "image_url": f"data:{mime};base64,{image_b64}"},
        ],
    }]
    return ask(message)


@app.route('/')
def index():
    q = request.args.get('q', '').strip()
    conn = get_db()

    if q :
        keyword = f"%{q}%"
        posts = conn.execute("""
            SELECT posts.*, users.username
            FROM posts
            LEFT JOIN users ON posts.user_id = users.id
            WHERE posts.title LIKE ? OR posts.content LIKE ?
            ORDER BY posts.is_notice DESC, posts.id DESC
        """, (keyword, keyword)).fetchall()
    else:
        posts = conn.execute("""
            SELECT posts.*, users.username
            FROM posts
            LEFT JOIN users ON posts.user_id = users.id
            ORDER BY posts.is_notice DESC, posts.id DESC
        """).fetchall()

    user = None
    if "user_id" in session:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()

    conn.close()
    return render_template("list.html", posts=posts, q=q, user=user)

@app.route("/posts/<int:post_id>")
def detail(post_id):
    post = get_post_or_404(post_id)
    if post is None:
        return "글 없음", 404

    conn = get_db()
    author = conn.execute("SELECT * FROM users WHERE id = ?", (post["user_id"],)).fetchone()
    user = None
    if "user_id" in session:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    comments = conn.execute("""
        SELECT comments.*, users.username, users.is_admin
        FROM comments
        LEFT JOIN users ON comments.user_id = users.id
        WHERE comments.post_id = ?
        ORDER BY comments.id ASC
    """, (post_id,)).fetchall()
    conn.close()

    return render_template("detail.html", post=post, author=author, user=user, comments=comments)

@app.route("/new", methods=["GET", "POST"])
def new():
    if "user_id" not in session:
        return redirect("/login")

    if request.method == "POST":
        title = request.form["title"]
        content = request.form["content"]
        user_id = session["user_id"]
        conn = get_db()
        conn.execute("""
         INSERT INTO posts (title, content, user_id) VALUES (?, ?, ?)
         """,
         (title, content, user_id)
        )
        conn.commit()
        conn.close()
        return redirect("/")
    return render_template("new.html")

@app.route("/posts/<int:post_id>/edit", methods=["GET", "POST"])
def edit(post_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    conn.close()

    is_owner = post["user_id"] == session["user_id"]
    is_admin = user and user["is_admin"]

    if not (is_owner or is_admin):
        return "본인 또는 관리자만 수정 가능합니다.", 403

    if request.method == "POST":
        title = request.form["title"]
        content = request.form["content"]
        conn = get_db()
        conn.execute("UPDATE posts SET title = ?, content = ? WHERE id = ?", (title, content, post_id))
        conn.commit()
        conn.close()
        return redirect(url_for('detail', post_id=post_id))

    return render_template('edit.html', post=post)

@app.route("/posts/<int:post_id>/delete", methods=['POST'])
def delete_post(post_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()

    is_owner = post["user_id"] == session["user_id"]
    is_admin = user and user["is_admin"]

    if not (is_owner or is_admin):
        conn.close()
        return "본인 또는 관리자만 삭제 가능합니다.", 403

    conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        hashed_pw = generate_password_hash(password)
        try:
            conn = get_db()
            conn.execute('''
            INSERT INTO users (username, password_hash) VALUES (?, ?)
            ''',
            (username, hashed_pw)
            )
            conn.commit()
            conn.close()
            return redirect('/login')
        except:
            return render_template('signup.html', error='이미 존재하는 아이디입니다.')
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        conn = get_db()
        user = conn.execute('''
         SELECT * FROM users WHERE username = ?
        ''',
        (username, )
        ).fetchone()
        conn.close()
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['is_admin'] = user['is_admin']
            return redirect('/')
        return render_template('login.html', error='아이디 또는 비밀번호가 틀렸습니다.')
    return render_template('login.html')

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect('/')

@app.route('/account')
def account():
    if "user_id" not in session:
        return redirect('/login')
    return render_template('account.html')

@app.route('/change-password', methods=['GET', 'POST'])
def change_password():
    if "user_id" not in session:
        return redirect('/login')

    if request.method == 'POST':
        old_password = request.form['old_password']
        new_password = request.form['new_password']
        new_password_confirm = request.form['new_password_confirm']

        if new_password != new_password_confirm:
            return render_template('change_password.html', error='새 비밀번호가 일치하지 않습니다.')

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        conn.close()

        if not check_password_hash(user['password_hash'], old_password):
            return render_template('change_password.html', error='현재 비밀번호가 틀렸습니다.')

        hashed_pw = generate_password_hash(new_password)
        conn = get_db()
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hashed_pw, session["user_id"]))
        conn.commit()
        conn.close()

        return render_template('change_password.html', success='비밀번호가 변경되었습니다.')

    return render_template('change_password.html')

@app.route("/posts/<int:post_id>/comments", methods=["POST"])
def create_comment(post_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    content = request.form.get("content", "").strip()
    if not content:
        return redirect(url_for("detail", post_id=post_id))

    conn = get_db()
    conn.execute(
        "INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)",
        (post_id, session["user_id"], content)
    )
    conn.commit()
    conn.close()
    return redirect(url_for("detail", post_id=post_id))


@app.route("/comments/<int:comment_id>/delete", methods=["POST"])
def delete_comment(comment_id):
    conn = get_db()
    comment = conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
    if comment is None:
        conn.close()
        return "댓글 없음", 404

    user = conn.execute("SELECT * FROM users WHERE id = ?", (session.get("user_id"),)).fetchone()
    is_owner = comment["user_id"] == session.get("user_id")
    is_admin = user and user["is_admin"]

    if not (is_owner or is_admin):
        conn.close()
        return "권한 없음", 403

    conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("detail", post_id=comment["post_id"]))

@app.route("/notice/new", methods=["GET", "POST"])
def new_notice():
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    conn.close()

    if not (user and user["is_admin"]):
        return "관리자만 공지를 작성할 수 있습니다.", 403

    if request.method == "POST":
        title = request.form["title"]
        content = request.form["content"]
        user_id = session["user_id"]
        conn = get_db()
        conn.execute(
            "INSERT INTO posts (title, content, user_id, is_notice) VALUES (?, ?, ?, ?)",
            (title, content, user_id, 1)
        )
        conn.commit()
        conn.close()
        return redirect("/")

    return render_template("notice_new.html")

@app.route("/notice/<int:post_id>/edit", methods=["GET", "POST"])
def edit_notice(post_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    conn.close()

    if not post or not post["is_notice"]:
        return "공지를 찾을 수 없습니다.", 404

    if not (user and user["is_admin"]):
        return "관리자만 공지를 수정할 수 있습니다.", 403

    if request.method == "POST":
        title = request.form["title"]
        content = request.form["content"]
        conn = get_db()
        conn.execute(
            "UPDATE posts SET title = ?, content = ? WHERE id = ?",
            (title, content, post_id)
        )
        conn.commit()
        conn.close()
        return redirect(url_for('detail', post_id=post_id))

    return render_template('notice_edit.html', post=post)

@app.route("/notice/<int:post_id>/delete", methods=["POST"])
def delete_notice(post_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    conn.close()

    if not post or not post["is_notice"]:
        return "공지를 찾을 수 없습니다.", 404

    if not (user and user["is_admin"]):
        return "관리자만 공지를 삭제할 수 있습니다.", 403

    conn = get_db()
    conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route("/notices")
def notices():
    conn = get_db()
    notices = conn.execute("""
        SELECT posts.*, users.username
        FROM posts
        LEFT JOIN users ON posts.user_id = users.id
        WHERE posts.is_notice = 1
        ORDER BY posts.id DESC
    """).fetchall()
    conn.close()
    return render_template("notices.html", notices=notices)

@app.route("/dashboard")
def dashboard():
    sido = request.args.get("sido", "서울")
    rows, source = fetch_air_quality(sido)
    return render_template('dashboard.html', rows=rows, sido=sido, source=source)

@app.route("/chat", methods=["GET", "POST"])
def chat_rooms():
    if "user_id" not in session:
        return redirect("/login")

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        is_public = 1 if request.form.get("is_public") else 0
        if name:
            conn = get_db()
            cur = conn.execute(
                "INSERT INTO chat_rooms (name, created_by, is_public) VALUES (?, ?, ?)",
                (name, session["username"], is_public),
            )
            conn.execute(
                "INSERT INTO chat_room_members (room_id, username) VALUES (?, ?)",
                (cur.lastrowid, session["username"]),
            )
            conn.commit()
            conn.close()
        return redirect("/chat")

    conn = get_db()
    member_flag_sql = """
        CASE WHEN EXISTS (
            SELECT 1 FROM chat_room_members m
            WHERE m.room_id = chat_rooms.id AND m.username = ?
        ) THEN 1 ELSE 0 END AS is_member
    """
    if session.get("is_admin"):
        rooms = conn.execute(f"""
            SELECT chat_rooms.*, {member_flag_sql}
            FROM chat_rooms
            ORDER BY chat_rooms.id DESC
        """, (session["username"],)).fetchall()
    else:
        rooms = conn.execute(f"""
            SELECT * FROM (
                SELECT chat_rooms.*, {member_flag_sql}
                FROM chat_rooms
            )
            WHERE is_member = 1 OR is_public = 1
            ORDER BY id DESC
        """, (session["username"],)).fetchall()
    conn.close()
    return render_template("chat_rooms.html", rooms=rooms)

@app.route("/chat/<int:room_id>/join", methods=["POST"])
def join_room_self(room_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        return "방 없음", 404

    if not room["is_public"] and not session.get("is_admin"):
        conn.close()
        return "초대된 사용자만 입장할 수 있습니다.", 403

    try:
        conn.execute(
            "INSERT INTO chat_room_members (room_id, username) VALUES (?, ?)",
            (room_id, session["username"]),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()
    return redirect(url_for("chat_room", room_id=room_id))

@app.route("/chat/rooms/<int:room_id>/delete", methods=["POST"])
def delete_room(room_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        return "방 없음", 404

    is_owner = room["created_by"] == session.get("username")
    is_admin = bool(session.get("is_admin"))
    if not (is_owner or is_admin):
        conn.close()
        return "본인 또는 관리자만 삭제 가능합니다.", 403

    conn.execute("DELETE FROM chat_rooms WHERE id = ?", (room_id,))
    conn.execute("DELETE FROM chat_messages WHERE room_id = ?", (room_id,))
    conn.execute("DELETE FROM chat_room_members WHERE room_id = ?", (room_id,))
    conn.commit()
    conn.close()
    return redirect("/chat")

@app.route("/chat/<int:room_id>")
def chat_room(room_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        return "방 없음", 404

    is_admin = bool(session.get("is_admin"))
    member = is_room_member(conn, room_id, session["username"])

    if not is_admin and not member:
        if not room["is_public"]:
            conn.close()
            return "초대된 사용자만 입장할 수 있습니다.", 403
        try:
            conn.execute(
                "INSERT INTO chat_room_members (room_id, username) VALUES (?, ?)",
                (room_id, session["username"]),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass

    rows = conn.execute(
        "SELECT * FROM chat_messages WHERE room_id = ? ORDER BY id DESC LIMIT 100",
        (room_id,),
    ).fetchall()

    pinned = None
    if room["pinned_message_id"]:
        pinned = conn.execute(
            "SELECT * FROM chat_messages WHERE id = ?", (room["pinned_message_id"],)
        ).fetchone()

    perms = get_effective_permissions(conn, room, session["username"])
    conn.close()

    is_owner = room["created_by"] == session.get("username")
    messages = list(reversed(rows))
    return render_template(
        "chat.html", room=room, messages=messages, pinned=pinned,
        is_owner=is_owner, can_announce=perms["announce"],
        can_manage_settings=any(perms.values()),
        can_manage_messages=perms["manage_messages"],
    )

@app.route("/chat/<int:room_id>/members", methods=["GET", "POST"])
def room_members(room_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        return "방 없음", 404

    perms = get_effective_permissions(conn, room, session["username"])
    if not any(perms.values()):
        conn.close()
        return "방 설정을 관리할 권한이 없습니다.", 403

    if request.method == "POST":
        if not perms["manage_members"]:
            conn.close()
            return "멤버를 초대할 권한이 없습니다.", 403

        username = request.form.get("username", "").strip()
        target = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if target is None:
            roles = conn.execute("SELECT * FROM chat_room_roles WHERE room_id = ? ORDER BY id ASC", (room_id,)).fetchall()
            conn.close()
            return render_template(
                "room_members.html", room=room, members=[], roles=roles, perms=perms,
                error="존재하지 않는 사용자입니다.",
            )
        try:
            conn.execute(
                "INSERT INTO chat_room_members (room_id, username) VALUES (?, ?)",
                (room_id, username),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass
        conn.close()
        return redirect(url_for("room_members", room_id=room_id))

    members = conn.execute("""
        SELECT m.username, m.timeout_until, m.role_id,
               r.name AS role_name, r.color AS role_color,
               CASE WHEN m.timeout_until IS NOT NULL AND m.timeout_until > datetime('now', 'localtime')
                    THEN 1 ELSE 0 END AS is_timed_out
        FROM chat_room_members m
        LEFT JOIN chat_room_roles r ON r.id = m.role_id
        WHERE m.room_id = ?
        ORDER BY m.id ASC
    """, (room_id,)).fetchall()
    roles = conn.execute("SELECT * FROM chat_room_roles WHERE room_id = ? ORDER BY id ASC", (room_id,)).fetchall()
    conn.close()
    return render_template("room_members.html", room=room, members=members, roles=roles, perms=perms)

@app.route("/chat/<int:room_id>/members/<username>/remove", methods=["POST"])
def remove_room_member(room_id, username):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        return "방 없음", 404

    perms = get_effective_permissions(conn, room, session["username"])
    if not perms["manage_members"]:
        conn.close()
        return "멤버를 관리할 권한이 없습니다.", 403

    if username == room["created_by"]:
        conn.close()
        return "방을 만든 사람은 제외할 수 없습니다.", 400

    conn.execute(
        "DELETE FROM chat_room_members WHERE room_id = ? AND username = ?",
        (room_id, username),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("room_members", room_id=room_id))

@app.route("/chat/<int:room_id>/members/<username>/timeout", methods=["POST"])
def timeout_room_member(room_id, username):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        return "방 없음", 404

    perms = get_effective_permissions(conn, room, session["username"])
    if not perms["manage_members"]:
        conn.close()
        return "멤버를 관리할 권한이 없습니다.", 403

    if username == room["created_by"]:
        conn.close()
        return "방을 만든 사람은 타임아웃할 수 없습니다.", 400

    duration = request.form.get("duration")
    modifier = TIMEOUT_DURATIONS.get(duration)
    if modifier is None:
        conn.close()
        return "잘못된 시간입니다.", 400

    conn.execute(
        f"UPDATE chat_room_members SET timeout_until = datetime('now', 'localtime', '{modifier}') "
        "WHERE room_id = ? AND username = ?",
        (room_id, username),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("room_members", room_id=room_id))

@app.route("/chat/<int:room_id>/members/<username>/untimeout", methods=["POST"])
def untimeout_room_member(room_id, username):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        return "방 없음", 404

    perms = get_effective_permissions(conn, room, session["username"])
    if not perms["manage_members"]:
        conn.close()
        return "멤버를 관리할 권한이 없습니다.", 403

    conn.execute(
        "UPDATE chat_room_members SET timeout_until = NULL WHERE room_id = ? AND username = ?",
        (room_id, username),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("room_members", room_id=room_id))

@app.route("/chat/<int:room_id>/visibility", methods=["POST"])
def toggle_room_visibility(room_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        return "방 없음", 404

    perms = get_effective_permissions(conn, room, session["username"])
    if not perms["manage_room"]:
        conn.close()
        return "방 설정을 변경할 권한이 없습니다.", 403

    new_value = 0 if room["is_public"] else 1
    conn.execute("UPDATE chat_rooms SET is_public = ? WHERE id = ?", (new_value, room_id))
    conn.commit()
    conn.close()
    return redirect(url_for("room_members", room_id=room_id))

@app.route("/chat/<int:room_id>/slowmode", methods=["POST"])
def set_slow_mode(room_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        return "방 없음", 404

    perms = get_effective_permissions(conn, room, session["username"])
    if not perms["manage_room"]:
        conn.close()
        return "방 설정을 변경할 권한이 없습니다.", 403

    try:
        seconds = max(0, int(request.form.get("seconds", 0)))
    except ValueError:
        seconds = 0

    conn.execute("UPDATE chat_rooms SET slow_mode_seconds = ? WHERE id = ?", (seconds, room_id))
    conn.commit()
    conn.close()
    return redirect(url_for("room_members", room_id=room_id))

@app.route("/chat/<int:room_id>/roles", methods=["POST"])
def create_room_role(room_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        return "방 없음", 404

    perms = get_effective_permissions(conn, room, session["username"])
    if not perms["manage_roles"]:
        conn.close()
        return "역할을 관리할 권한이 없습니다.", 403

    name = request.form.get("name", "").strip()
    if not name:
        conn.close()
        return redirect(url_for("room_members", room_id=room_id))

    color = request.form.get("color", "#6b7280").strip()
    if not HEX_COLOR_RE.match(color):
        color = "#6b7280"

    conn.execute(
        "INSERT INTO chat_room_roles (room_id, name, color) VALUES (?, ?, ?)",
        (room_id, name, color),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("room_members", room_id=room_id))

@app.route("/chat/<int:room_id>/roles/<int:role_id>/update", methods=["POST"])
def update_room_role(room_id, role_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        return "방 없음", 404

    perms = get_effective_permissions(conn, room, session["username"])
    if not perms["manage_roles"]:
        conn.close()
        return "역할을 관리할 권한이 없습니다.", 403

    role = conn.execute(
        "SELECT * FROM chat_room_roles WHERE id = ? AND room_id = ?", (role_id, room_id)
    ).fetchone()
    if role is None:
        conn.close()
        return "역할을 찾을 수 없습니다.", 404

    name = request.form.get("name", "").strip() or role["name"]
    color = request.form.get("color", role["color"]).strip()
    if not HEX_COLOR_RE.match(color):
        color = role["color"]

    perm_values = [1 if request.form.get(f"perm_{key}") == "on" else 0 for key in ROOM_PERMISSION_KEYS]

    conn.execute("""
        UPDATE chat_room_roles
        SET name = ?, color = ?,
            perm_manage_room = ?, perm_manage_members = ?, perm_manage_messages = ?,
            perm_announce = ?, perm_manage_roles = ?
        WHERE id = ?
    """, (name, color, *perm_values, role_id))
    conn.commit()
    conn.close()
    return redirect(url_for("room_members", room_id=room_id))

@app.route("/chat/<int:room_id>/roles/<int:role_id>/delete", methods=["POST"])
def delete_room_role(room_id, role_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        return "방 없음", 404

    perms = get_effective_permissions(conn, room, session["username"])
    if not perms["manage_roles"]:
        conn.close()
        return "역할을 관리할 권한이 없습니다.", 403

    conn.execute("UPDATE chat_room_members SET role_id = NULL WHERE role_id = ?", (role_id,))
    conn.execute("DELETE FROM chat_room_roles WHERE id = ? AND room_id = ?", (role_id, room_id))
    conn.commit()
    conn.close()
    return redirect(url_for("room_members", room_id=room_id))

@app.route("/chat/<int:room_id>/members/<username>/role", methods=["POST"])
def assign_room_member_role(room_id, username):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        return "방 없음", 404

    perms = get_effective_permissions(conn, room, session["username"])
    if not perms["manage_roles"]:
        conn.close()
        return "역할을 관리할 권한이 없습니다.", 403

    if username == room["created_by"]:
        conn.close()
        return "방을 만든 사람에게는 역할을 지정할 수 없습니다.", 400

    role_id = request.form.get("role_id", "").strip()
    if role_id:
        role = conn.execute(
            "SELECT * FROM chat_room_roles WHERE id = ? AND room_id = ?", (role_id, room_id)
        ).fetchone()
        if role is None:
            conn.close()
            return "역할을 찾을 수 없습니다.", 404
        conn.execute(
            "UPDATE chat_room_members SET role_id = ? WHERE room_id = ? AND username = ?",
            (role_id, room_id, username),
        )
    else:
        conn.execute(
            "UPDATE chat_room_members SET role_id = NULL WHERE room_id = ? AND username = ?",
            (room_id, username),
        )
    conn.commit()
    conn.close()
    return redirect(url_for("room_members", room_id=room_id))

@app.route("/assistant", methods=["GET", "POST"])
def assistant():
    user_key = get_user_key()
    view = {
        "school_name": "", "meal_school": None, "meal": None, "ai_meal": None,
        "notice_text": "", "summary": None, "image_result": None, "errors": {},
    }

    if request.method == "POST":
        action = request.form.get("action")

        if action == "meal":
            view["school_name"] = request.form.get("school_name", "").strip()
            school = neis.get_school_code(view["school_name"]) if view["school_name"] else None
            if school is None:
                view["errors"]["meal"] = "학교를 찾지 못했어요. 정확한 학교 이름(예: 경기고등학교)을 입력하세요."
            else:
                office_code, school_code, school_full_name = school
                view["meal_school"] = school_full_name
                view["meal"] = neis.get_today_meal(office_code, school_code)
                if view["meal"] is None:
                    view["errors"]["meal"] = "오늘 급식 정보가 등록되지 않았습니다. (주말·방학·휴업일일 수 있어요)"
                else:
                    try:
                        view["ai_meal"] = brief_meal(school_full_name, view["meal"])
                    except AIError as e:
                        view["errors"]["ai_meal"] = str(e)

        elif action == "notice":
            view["notice_text"] = request.form.get("notice_text", "").strip()
            if not view["notice_text"]:
                view["errors"]["notice"] = "공지 내용을 입력하세요."
            else:
                try:
                    view["summary"] = summarize_notice(view["notice_text"])
                except AIError as e:
                    view["errors"]["notice"] = str(e)

        elif action == "image":
            photo = request.files.get("photo")
            if photo is None or photo.filename == "":
                view["errors"]["image"] = "사진 파일을 선택하세요."
            else:
                try:
                    view["image_result"] = analyze_image(photo)
                except AIError as e:
                    view["errors"]["image"] = str(e)

    return render_template("assistant.html", history=user_chats.get(user_key, []), **view)

@app.route("/assistant/chat", methods=["POST"])
def assistant_chat():
    user_key = get_user_key()
    message = request.form.get("message", "").strip()

    if not message:
        return jsonify({"reply": "질문을 입력하세요."}), 400
    if message == "초기화":
        user_chats.pop(user_key, None)
        return jsonify({"reply": "[시스템] 대화 문맥이 초기화되었습니다."})

    try:
        return jsonify({"reply": chat_with_history(user_key, message)})
    except AIError as e:
        return jsonify({"reply": str(e)}), 502

@app.errorhandler(413)
def too_large(_):
    return "사진 용량이 너무 커요(5MB 이하). 뒤로 가서 작은 사진을 올려주세요.", 413

@socketio.on("connect")
def handle_connect():
    if "user_id" not in session:
        return False

def broadcast_online_users(room_id_str):
    usernames = sorted(set(room_online_users[room_id_str].values()))
    emit("online_users", {"usernames": usernames}, room=room_id_str)

@socketio.on("join")
def handle_join(data):
    if "user_id" not in session:
        return
    room_id = (data or {}).get("room_id")
    if room_id is None:
        return
    conn = get_db()
    allowed = bool(session.get("is_admin")) or is_room_member(conn, room_id, session["username"])
    conn.close()
    if allowed:
        room_id_str = str(room_id)
        join_room(room_id_str)
        room_online_users[room_id_str][request.sid] = session["username"]
        broadcast_online_users(room_id_str)

@socketio.on("leave")
def handle_leave(data):
    room_id = (data or {}).get("room_id")
    if room_id is not None:
        room_id_str = str(room_id)
        leave_room(room_id_str)
        room_online_users[room_id_str].pop(request.sid, None)
        broadcast_online_users(room_id_str)

@socketio.on("disconnect")
def handle_disconnect():
    sid = request.sid
    for room_id_str, sids in list(room_online_users.items()):
        if sid in sids:
            sids.pop(sid, None)
            broadcast_online_users(room_id_str)

@socketio.on("send_message")
def handle_send_message(data):
    if "user_id" not in session:
        return
    room_id = (data or {}).get("room_id")
    content = (data or {}).get("content", "").strip()
    if not content or room_id is None:
        return

    conn = get_db()
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        return

    is_admin = bool(session.get("is_admin"))
    is_owner = room["created_by"] == session.get("username")
    allowed = is_admin or is_room_member(conn, room_id, session["username"])
    if not allowed:
        conn.close()
        return

    if not is_admin and is_timed_out(conn, room_id, session["username"]):
        conn.close()
        emit("timeout_error", {"message": "타임아웃 상태에서는 메시지를 보낼 수 없습니다."})
        return

    if not is_admin and not is_owner and room["slow_mode_seconds"] > 0:
        blocked = conn.execute("""
            SELECT 1 FROM chat_messages
            WHERE room_id = ? AND username = ?
            ORDER BY id DESC LIMIT 1
        """, (room_id, session["username"])).fetchone()
        if blocked:
            still_waiting = conn.execute("""
                SELECT datetime(created_at, '+' || ? || ' seconds') > datetime('now', 'localtime') AS waiting
                FROM chat_messages
                WHERE room_id = ? AND username = ?
                ORDER BY id DESC LIMIT 1
            """, (room["slow_mode_seconds"], room_id, session["username"])).fetchone()
            if still_waiting["waiting"]:
                conn.close()
                emit("slowmode_error", {
                    "message": f"슬로우 모드: {room['slow_mode_seconds']}초마다 한 번만 보낼 수 있습니다."
                })
                return

    cur = conn.execute(
        "INSERT INTO chat_messages (username, content, room_id) VALUES (?, ?, ?)",
        (session["username"], content, room_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()

    emit("new_message", {
        "id": row["id"],
        "username": row["username"],
        "content": row["content"],
        "created_at": row["created_at"],
    }, room=str(room_id))

@socketio.on("delete_message")
def handle_delete_message(data):
    if "user_id" not in session:
        return
    message_id = (data or {}).get("id")

    conn = get_db()
    message = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
    if message is None:
        conn.close()
        return

    allowed = message["username"] == session.get("username")
    if not allowed and message["room_id"] is not None:
        room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (message["room_id"],)).fetchone()
        if room is not None:
            allowed = get_effective_permissions(conn, room, session["username"])["manage_messages"]
    if not allowed:
        conn.close()
        return

    room_id = message["room_id"]
    conn.execute("DELETE FROM chat_messages WHERE id = ?", (message_id,))
    conn.commit()
    conn.close()
    emit("message_deleted", {"id": message_id}, room=str(room_id))

@socketio.on("pin_message")
def handle_pin_message(data):
    if "user_id" not in session:
        return
    message_id = (data or {}).get("id")

    conn = get_db()
    message = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
    if message is None or message["room_id"] is None:
        conn.close()
        return

    room_id = message["room_id"]
    is_admin = bool(session.get("is_admin"))
    allowed = is_admin or is_room_member(conn, room_id, session["username"])
    if not allowed:
        conn.close()
        return

    conn.execute("UPDATE chat_rooms SET pinned_message_id = ? WHERE id = ?", (message_id, room_id))
    conn.commit()
    conn.close()

    emit("message_pinned", {
        "id": message["id"],
        "username": message["username"],
        "content": message["content"],
        "created_at": message["created_at"],
    }, room=str(room_id))

@socketio.on("unpin_message")
def handle_unpin_message(data):
    if "user_id" not in session:
        return
    room_id = (data or {}).get("room_id")
    if room_id is None:
        return

    conn = get_db()
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        return

    is_admin = bool(session.get("is_admin"))
    allowed = is_admin or is_room_member(conn, room_id, session["username"])
    if not allowed:
        conn.close()
        return

    conn.execute("UPDATE chat_rooms SET pinned_message_id = NULL WHERE id = ?", (room_id,))
    conn.commit()
    conn.close()
    emit("message_unpinned", {}, room=str(room_id))

@socketio.on("set_announcement")
def handle_set_announcement(data):
    if "user_id" not in session:
        return
    room_id = (data or {}).get("room_id")
    content = (data or {}).get("content", "").strip()
    if room_id is None or not content:
        return

    conn = get_db()
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        return

    if not get_effective_permissions(conn, room, session["username"])["announce"]:
        conn.close()
        return

    conn.execute("UPDATE chat_rooms SET announcement = ? WHERE id = ?", (content, room_id))
    conn.commit()
    conn.close()
    emit("announcement_set", {"content": content}, room=str(room_id))

@socketio.on("clear_announcement")
def handle_clear_announcement(data):
    if "user_id" not in session:
        return
    room_id = (data or {}).get("room_id")
    if room_id is None:
        return

    conn = get_db()
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        return

    if not get_effective_permissions(conn, room, session["username"])["announce"]:
        conn.close()
        return

    conn.execute("UPDATE chat_rooms SET announcement = NULL WHERE id = ?", (room_id,))
    conn.commit()
    conn.close()
    emit("announcement_cleared", {}, room=str(room_id))

if __name__ == '__main__':
    create_tables()
    socketio.run(app, debug=True, port=5001)
