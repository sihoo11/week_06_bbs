import base64
import math
import os
import re
import sqlite3
import time
import uuid
from collections import defaultdict
from datetime import date
from pathlib import Path

import openai
from dotenv import load_dotenv
from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_wtf.csrf import CSRFError, CSRFProtect
from openai import OpenAI
from werkzeug.security import check_password_hash, generate_password_hash

import neis
from opendata import fetch_air_quality

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.config["WTF_CSRF_TIME_LIMIT"] = None
csrf = CSRFProtect(app)
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

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / 'bbs.db'
UPLOAD_DIR = BASE_DIR / 'static' / 'uploads'

TIMEOUT_DURATIONS = {"5": "+5 minutes", "10": "+10 minutes", "60": "+1 hours"}
ROOM_PERMISSION_KEYS = ["manage_room", "manage_members", "manage_messages", "announce", "manage_roles"]
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MENTION_RE = re.compile(r"@([^\s@]+)")

CATEGORIES = ["자유", "질문", "과제", "분실물"]
SORT_OPTIONS = {
    "latest": ("최신순", "posts.id DESC"),
    "likes": ("좋아요순", "like_count DESC, posts.id DESC"),
    "views": ("조회순", "posts.views DESC, posts.id DESC"),
}
POSTS_PER_PAGE = 15

# 확장자 → 파일 시그니처(매직 바이트). 확장자만 바꾼 가짜 이미지를 걸러낸다.
UPLOAD_IMAGE_SIGNATURES = {
    ".png": [b"\x89PNG"],
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".gif": [b"GIF87a", b"GIF89a"],
    ".webp": [b"RIFF"],
}

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


def save_uploaded_image(file_storage, subdir):
    """이미지 파일을 static/uploads/<subdir>/ 에 저장하고 static 기준 경로를 반환. 이미지가 아니면 ValueError."""
    ext = os.path.splitext(file_storage.filename or "")[1].lower()
    signatures = UPLOAD_IMAGE_SIGNATURES.get(ext)
    if signatures is None:
        raise ValueError("JPG, PNG, GIF, WEBP 이미지만 올릴 수 있어요.")

    data = file_storage.read()
    if not any(data.startswith(sig) for sig in signatures) or (ext == ".webp" and data[8:12] != b"WEBP"):
        raise ValueError("올바른 이미지 파일이 아니에요.")

    target_dir = UPLOAD_DIR / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{ext}"
    (target_dir / filename).write_bytes(data)
    return f"uploads/{subdir}/{filename}"


def delete_uploaded_file(relative_path):
    if not relative_path:
        return
    path = (UPLOAD_DIR.parent / relative_path).resolve()
    if UPLOAD_DIR.resolve() in path.parents and path.exists():
        path.unlink()


def delete_post_cascade(conn, post_id):
    """게시글과 딸린 댓글·좋아요·투표·첨부 이미지를 함께 지운다."""
    post = conn.execute("SELECT image FROM posts WHERE id = ?", (post_id,)).fetchone()
    poll = conn.execute("SELECT id FROM polls WHERE post_id = ?", (post_id,)).fetchone()
    if poll is not None:
        conn.execute("DELETE FROM poll_votes WHERE poll_id = ?", (poll["id"],))
        conn.execute("DELETE FROM poll_options WHERE poll_id = ?", (poll["id"],))
        conn.execute("DELETE FROM polls WHERE id = ?", (poll["id"],))
    conn.execute("DELETE FROM comments WHERE post_id = ?", (post_id,))
    conn.execute("DELETE FROM post_likes WHERE post_id = ?", (post_id,))
    conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    if post is not None:
        delete_uploaded_file(post["image"])


def notify(conn, user_id, message, link):
    """알림을 저장하고, 접속 중이면 개인 소켓 룸으로 즉시 보낸다. 호출한 쪽에서 commit 한다."""
    if user_id is None:
        return
    conn.execute(
        "INSERT INTO notifications (user_id, message, link) VALUES (?, ?, ?)",
        (user_id, message, link),
    )
    socketio.emit("notification", {"message": message, "link": link}, to=f"user_{user_id}")


def notify_mentions(conn, text, sender_username, message, link, allowed_usernames=None):
    """본문에서 @아이디 를 찾아 해당 사용자에게 알림. allowed_usernames 가 있으면 그 안의 사용자만."""
    mentioned = {name.rstrip(".,!?)") for name in MENTION_RE.findall(text)} - {sender_username}
    for name in mentioned:
        if allowed_usernames is not None and name not in allowed_usernames:
            continue
        target = conn.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()
        if target is not None:
            notify(conn, target["id"], message, link)


def build_pagination(page, total_count, per_page, window=2):
    total_pages = max(1, math.ceil(total_count / per_page))
    page = min(max(1, page), total_pages)
    start, end = max(1, page - window), min(total_pages, page + window)
    return {
        "page": page, "total_pages": total_pages,
        "pages": list(range(start, end + 1)),
        "has_prev": page > 1, "has_next": page < total_pages,
    }


def d_day_label(due_date_str):
    days = (date.fromisoformat(due_date_str) - date.today()).days
    if days == 0:
        return "D-DAY"
    return f"D-{days}" if days > 0 else f"D+{-days}"


def safe_redirect_target(link, fallback):
    """내부 경로(/로 시작)만 허용해 외부 사이트로의 리다이렉트를 막는다."""
    link = link or ""
    return link if link.startswith("/") and not link.startswith("//") else fallback


def is_room_member(conn, room_id, username):
    member = conn.execute(
        "SELECT 1 FROM chat_room_members WHERE room_id = ? AND username = ?",
        (room_id, username),
    ).fetchone()
    return member is not None


def can_access_room(conn, room, username):
    """방 멤버이거나, 관리자면 입장 가능. 단 1:1 DM 은 관리자도 당사자만 볼 수 있다."""
    if is_room_member(conn, room["id"], username):
        return True
    return bool(session.get("is_admin")) and not room["is_dm"]


def dm_partner(conn, room_id, username):
    row = conn.execute(
        "SELECT username FROM chat_room_members WHERE room_id = ? AND username != ?",
        (room_id, username),
    ).fetchone()
    return row["username"] if row else None


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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_type TEXT NOT NULL CHECK (target_type IN ('post', 'comment')),
            target_id INTEGER NOT NULL,
            reporter_username TEXT NOT NULL,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_unique_pending
        ON reports(target_type, target_id, reporter_username)
        WHERE status = 'pending'
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS post_likes (
            post_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            PRIMARY KEY (post_id, user_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL UNIQUE,
            question TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS poll_options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id INTEGER NOT NULL,
            text TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS poll_votes (
            poll_id INTEGER NOT NULL,
            option_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (poll_id, user_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            subject TEXT,
            due_date TEXT NOT NULL,
            description TEXT,
            user_id INTEGER,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            link TEXT,
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read)")
    # 8주차까지 없던 컬럼을 이어쓰는 DB에 추가 (한 번만 실행됨)
    for statement in [
        "ALTER TABLE posts ADD COLUMN user_id INTEGER",
        "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'",
        "ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0",
        "ALTER TABLE posts ADD COLUMN is_notice INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE chat_messages ADD COLUMN room_id INTEGER",
        "ALTER TABLE chat_room_members ADD COLUMN timeout_until TEXT",
        "ALTER TABLE chat_rooms ADD COLUMN is_public INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE chat_rooms ADD COLUMN slow_mode_seconds INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE chat_rooms ADD COLUMN pinned_message_id INTEGER",
        "ALTER TABLE chat_rooms ADD COLUMN announcement TEXT",
        "ALTER TABLE chat_room_members ADD COLUMN role_id INTEGER",
        # 게시판 확장: 조회수, 카테고리, 수정 시각, 첨부 이미지
        "ALTER TABLE posts ADD COLUMN views INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE posts ADD COLUMN category TEXT NOT NULL DEFAULT '자유'",
        "ALTER TABLE posts ADD COLUMN updated_at TEXT",
        "ALTER TABLE posts ADD COLUMN image TEXT",
        "ALTER TABLE comments ADD COLUMN updated_at TEXT",
        # 회원 확장: 프로필 사진, 학교 정보
        "ALTER TABLE users ADD COLUMN avatar TEXT",
        "ALTER TABLE users ADD COLUMN school_name TEXT",
        "ALTER TABLE users ADD COLUMN grade INTEGER",
        "ALTER TABLE users ADD COLUMN class_nm TEXT",
        # 채팅 확장: 1:1 DM, 이미지 메시지
        "ALTER TABLE chat_rooms ADD COLUMN is_dm INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE chat_messages ADD COLUMN image TEXT",
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


@app.before_request
def load_current_user():
    """매 요청마다 로그인 사용자를 다시 읽어 탈퇴·권한 변경을 즉시 반영한다."""
    g.user = None
    if request.endpoint == "static" or "user_id" not in session:
        return
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    conn.close()
    if user is None:
        session.clear()
        return
    session["is_admin"] = user["is_admin"]
    g.user = user


@app.context_processor
def inject_globals():
    unread = 0
    if g.get("user") is not None:
        conn = get_db()
        unread = conn.execute(
            "SELECT COUNT(*) AS c FROM notifications WHERE user_id = ? AND is_read = 0", (g.user["id"],)
        ).fetchone()["c"]
        conn.close()
    return {"unread_notifications": unread, "current_user": g.get("user"), "CATEGORIES": CATEGORIES}


def read_poll_form():
    """글쓰기 폼의 투표 입력값을 (질문, [선택지]) 로 반환. 투표를 만들지 않으면 None."""
    question = request.form.get("poll_question", "").strip()
    options = [line.strip() for line in request.form.get("poll_options", "").splitlines() if line.strip()]
    if not question:
        return None
    if len(options) < 2:
        raise ValueError("투표 선택지는 두 개 이상 입력하세요.")
    return question, options[:10]


@app.route('/')
def index():
    q = request.args.get('q', '').strip()
    category = request.args.get('category', '')
    if category not in CATEGORIES:
        category = ''
    sort = request.args.get('sort', 'latest')
    if sort not in SORT_OPTIONS:
        sort = 'latest'
    page = request.args.get('page', 1, type=int)

    where = ["posts.is_notice = 0"]
    params = []
    if q:
        where.append("(posts.title LIKE ? OR posts.content LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if category:
        where.append("posts.category = ?")
        params.append(category)
    where_sql = " AND ".join(where)

    conn = get_db()
    total = conn.execute(f"SELECT COUNT(*) AS c FROM posts WHERE {where_sql}", params).fetchone()["c"]
    pagination = build_pagination(page, total, POSTS_PER_PAGE)
    posts = conn.execute(f"""
        SELECT posts.*, users.username,
               (SELECT COUNT(*) FROM post_likes WHERE post_likes.post_id = posts.id) AS like_count,
               (SELECT COUNT(*) FROM comments WHERE comments.post_id = posts.id) AS comment_count,
               EXISTS (SELECT 1 FROM polls WHERE polls.post_id = posts.id) AS has_poll
        FROM posts
        LEFT JOIN users ON posts.user_id = users.id
        WHERE {where_sql}
        ORDER BY {SORT_OPTIONS[sort][1]}
        LIMIT ? OFFSET ?
    """, (*params, POSTS_PER_PAGE, (pagination["page"] - 1) * POSTS_PER_PAGE)).fetchall()

    latest_notice = conn.execute(
        "SELECT * FROM posts WHERE is_notice = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    upcoming = conn.execute("""
        SELECT * FROM assignments
        WHERE due_date BETWEEN date('now', 'localtime') AND date('now', 'localtime', '+7 days')
        ORDER BY due_date ASC LIMIT 3
    """).fetchall()
    conn.close()

    return render_template(
        "list.html", posts=posts, q=q, category=category, sort=sort, total=total,
        sort_options=SORT_OPTIONS, pagination=pagination, latest_notice=latest_notice,
        upcoming=[dict(a, d_day=d_day_label(a["due_date"])) for a in upcoming],
    )

@app.route("/posts/<int:post_id>")
def detail(post_id):
    post = get_post_or_404(post_id)
    if post is None:
        return "글 없음", 404

    conn = get_db()
    # 같은 세션에서 새로고침할 때마다 조회수가 오르지 않도록 본 글을 기억한다.
    viewed = session.get("viewed_posts", [])
    if post_id not in viewed:
        conn.execute("UPDATE posts SET views = views + 1 WHERE id = ?", (post_id,))
        conn.commit()
        session["viewed_posts"] = (viewed + [post_id])[-200:]
        post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()

    author = conn.execute("SELECT * FROM users WHERE id = ?", (post["user_id"],)).fetchone()
    user = g.user
    comments = conn.execute("""
        SELECT comments.*, users.username, users.is_admin, users.avatar
        FROM comments
        LEFT JOIN users ON comments.user_id = users.id
        WHERE comments.post_id = ?
        ORDER BY comments.id ASC
    """, (post_id,)).fetchall()

    like_count = conn.execute("SELECT COUNT(*) AS c FROM post_likes WHERE post_id = ?", (post_id,)).fetchone()["c"]
    liked = user is not None and conn.execute(
        "SELECT 1 FROM post_likes WHERE post_id = ? AND user_id = ?", (post_id, user["id"])
    ).fetchone() is not None

    poll = conn.execute("SELECT * FROM polls WHERE post_id = ?", (post_id,)).fetchone()
    poll_view = None
    if poll is not None:
        options = conn.execute("""
            SELECT poll_options.*,
                   (SELECT COUNT(*) FROM poll_votes WHERE poll_votes.option_id = poll_options.id) AS votes
            FROM poll_options WHERE poll_id = ? ORDER BY id ASC
        """, (poll["id"],)).fetchall()
        my_vote = None
        if user is not None:
            row = conn.execute(
                "SELECT option_id FROM poll_votes WHERE poll_id = ? AND user_id = ?", (poll["id"], user["id"])
            ).fetchone()
            my_vote = row["option_id"] if row else None
        total_votes = sum(o["votes"] for o in options)
        poll_view = {
            "id": poll["id"], "question": poll["question"], "total": total_votes, "my_vote": my_vote,
            "options": [
                dict(o, percent=round(o["votes"] * 100 / total_votes) if total_votes else 0) for o in options
            ],
        }
    conn.close()

    return render_template(
        "detail.html", post=post, author=author, user=user, comments=comments,
        like_count=like_count, liked=liked, poll=poll_view,
    )

@app.route("/new", methods=["GET", "POST"])
def new():
    if "user_id" not in session:
        return redirect("/login")

    if request.method == "POST":
        title = request.form["title"]
        content = request.form["content"]
        category = request.form.get("category", "자유")
        if category not in CATEGORIES:
            category = "자유"

        try:
            poll = read_poll_form()
            photo = request.files.get("image")
            image = save_uploaded_image(photo, "posts") if photo and photo.filename else None
        except ValueError as e:
            return render_template("new.html", error=str(e), form=request.form)

        user_id = session["user_id"]
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO posts (title, content, user_id, category, image) VALUES (?, ?, ?, ?, ?)",
            (title, content, user_id, category, image),
        )
        post_id = cur.lastrowid
        if poll is not None:
            question, options = poll
            poll_id = conn.execute(
                "INSERT INTO polls (post_id, question) VALUES (?, ?)", (post_id, question)
            ).lastrowid
            conn.executemany(
                "INSERT INTO poll_options (poll_id, text) VALUES (?, ?)", [(poll_id, o) for o in options]
            )
        notify_mentions(
            conn, content, session["username"],
            f"{session['username']}님이 게시글에서 회원님을 언급했어요: {title}",
            url_for("detail", post_id=post_id),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("detail", post_id=post_id))
    return render_template("new.html", form={})

@app.route("/posts/<int:post_id>/edit", methods=["GET", "POST"])
def edit(post_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    conn.close()
    if post is None:
        return "글 없음", 404

    is_owner = post["user_id"] == session["user_id"]
    is_admin = bool(session.get("is_admin"))

    if not (is_owner or is_admin):
        return "본인 또는 관리자만 수정 가능합니다.", 403

    if request.method == "POST":
        title = request.form["title"]
        content = request.form["content"]
        category = request.form.get("category", post["category"])
        if category not in CATEGORIES:
            category = post["category"]

        image = post["image"]
        photo = request.files.get("image")
        try:
            if photo and photo.filename:
                image = save_uploaded_image(photo, "posts")
            elif request.form.get("remove_image"):
                image = None
        except ValueError as e:
            return render_template("edit.html", post=post, error=str(e))
        if image != post["image"]:
            delete_uploaded_file(post["image"])

        conn = get_db()
        conn.execute("""
            UPDATE posts SET title = ?, content = ?, category = ?, image = ?,
                             updated_at = datetime('now', 'localtime')
            WHERE id = ?
        """, (title, content, category, image, post_id))
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
    if post is None:
        conn.close()
        return "글 없음", 404

    is_owner = post["user_id"] == session["user_id"]
    is_admin = bool(session.get("is_admin"))

    if not (is_owner or is_admin):
        conn.close()
        return "본인 또는 관리자만 삭제 가능합니다.", 403

    delete_post_cascade(conn, post_id)
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route("/posts/<int:post_id>/like", methods=["POST"])
def toggle_like(post_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if post is None:
        conn.close()
        return "글 없음", 404

    removed = conn.execute(
        "DELETE FROM post_likes WHERE post_id = ? AND user_id = ?", (post_id, session["user_id"])
    ).rowcount
    if not removed:
        conn.execute("INSERT INTO post_likes (post_id, user_id) VALUES (?, ?)", (post_id, session["user_id"]))
        if post["user_id"] != session["user_id"]:
            notify(
                conn, post["user_id"], f"{session['username']}님이 회원님의 글을 좋아해요: {post['title']}",
                url_for("detail", post_id=post_id),
            )
    conn.commit()
    conn.close()
    return redirect(url_for("detail", post_id=post_id))

@app.route("/polls/<int:poll_id>/vote", methods=["POST"])
def vote_poll(poll_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    poll = conn.execute("SELECT * FROM polls WHERE id = ?", (poll_id,)).fetchone()
    if poll is None:
        conn.close()
        return "투표 없음", 404

    option_id = request.form.get("option_id", type=int)
    option = conn.execute(
        "SELECT 1 FROM poll_options WHERE id = ? AND poll_id = ?", (option_id, poll_id)
    ).fetchone()
    if option is not None:
        # 다시 투표하면 선택을 바꾼다
        conn.execute("""
            INSERT INTO poll_votes (poll_id, option_id, user_id) VALUES (?, ?, ?)
            ON CONFLICT (poll_id, user_id) DO UPDATE SET option_id = excluded.option_id
        """, (poll_id, option_id, session["user_id"]))
        conn.commit()
    conn.close()
    return redirect(url_for("detail", post_id=poll["post_id"]))

USERNAME_RE = re.compile(r"^[A-Za-z0-9_가-힣]{2,20}$")

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        # @멘션과 프로필 주소에 쓰이므로 공백·특수문자를 막는다
        if not USERNAME_RE.match(username):
            return render_template('signup.html', error='아이디는 2~20자의 한글, 영문, 숫자, _ 만 쓸 수 있습니다.')
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
        except sqlite3.IntegrityError:
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

@app.route('/account', methods=['GET', 'POST'])
def account():
    if "user_id" not in session:
        return redirect('/login')

    user = g.user
    if request.method == 'POST':
        school_name = request.form.get("school_name", "").strip() or None
        grade = request.form.get("grade", type=int)
        class_nm = request.form.get("class_nm", "").strip() or None

        avatar = user["avatar"]
        photo = request.files.get("avatar")
        try:
            if photo and photo.filename:
                avatar = save_uploaded_image(photo, "avatars")
            elif request.form.get("remove_avatar"):
                avatar = None
        except ValueError as e:
            return render_template('account.html', user=user, error=str(e))
        if avatar != user["avatar"]:
            delete_uploaded_file(user["avatar"])

        conn = get_db()
        conn.execute(
            "UPDATE users SET school_name = ?, grade = ?, class_nm = ?, avatar = ? WHERE id = ?",
            (school_name, grade, class_nm, avatar, user["id"]),
        )
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        conn.close()
        return render_template('account.html', user=user, success='프로필이 저장되었습니다.')

    return render_template('account.html', user=user)

@app.route('/users/<username>')
def profile(username):
    conn = get_db()
    target = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if target is None:
        conn.close()
        return "사용자를 찾을 수 없습니다.", 404

    posts = conn.execute("""
        SELECT posts.*,
               (SELECT COUNT(*) FROM post_likes WHERE post_likes.post_id = posts.id) AS like_count,
               (SELECT COUNT(*) FROM comments WHERE comments.post_id = posts.id) AS comment_count
        FROM posts WHERE user_id = ? ORDER BY id DESC LIMIT 20
    """, (target["id"],)).fetchall()
    comments = conn.execute("""
        SELECT comments.*, posts.title AS post_title
        FROM comments JOIN posts ON posts.id = comments.post_id
        WHERE comments.user_id = ? ORDER BY comments.id DESC LIMIT 20
    """, (target["id"],)).fetchall()
    stats = conn.execute("""
        SELECT (SELECT COUNT(*) FROM posts WHERE user_id = :id) AS posts,
               (SELECT COUNT(*) FROM comments WHERE user_id = :id) AS comments,
               (SELECT COUNT(*) FROM post_likes JOIN posts ON posts.id = post_likes.post_id
                WHERE posts.user_id = :id) AS likes_received
    """, {"id": target["id"]}).fetchone()
    conn.close()
    return render_template("profile.html", target=target, posts=posts, comments=comments, stats=stats)

@app.route('/notifications')
def notifications():
    if "user_id" not in session:
        return redirect('/login')
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 50", (session["user_id"],)
    ).fetchall()
    conn.close()
    return render_template("notifications.html", notifications=rows)

@app.route('/notifications/<int:notification_id>/open')
def open_notification(notification_id):
    if "user_id" not in session:
        return redirect('/login')
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM notifications WHERE id = ? AND user_id = ?", (notification_id, session["user_id"])
    ).fetchone()
    if row is None:
        conn.close()
        return redirect(url_for("notifications"))
    conn.execute("UPDATE notifications SET is_read = 1 WHERE id = ?", (notification_id,))
    conn.commit()
    conn.close()
    return redirect(safe_redirect_target(row["link"], url_for("notifications")))

@app.route('/notifications/read-all', methods=['POST'])
def read_all_notifications():
    if "user_id" not in session:
        return redirect('/login')
    conn = get_db()
    conn.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (session["user_id"],))
    conn.commit()
    conn.close()
    return redirect(url_for("notifications"))

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
    post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if post is None:
        conn.close()
        return "글 없음", 404

    cur = conn.execute(
        "INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)",
        (post_id, session["user_id"], content)
    )
    link = url_for("detail", post_id=post_id) + f"#comment-{cur.lastrowid}"
    if post["user_id"] != session["user_id"]:
        notify(conn, post["user_id"], f"{session['username']}님이 회원님의 글에 댓글을 남겼어요: {post['title']}", link)
    notify_mentions(conn, content, session["username"], f"{session['username']}님이 댓글에서 회원님을 언급했어요", link)
    conn.commit()
    conn.close()
    return redirect(link)


@app.route("/comments/<int:comment_id>/edit", methods=["POST"])
def edit_comment(comment_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    comment = conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
    if comment is None:
        conn.close()
        return "댓글 없음", 404
    if comment["user_id"] != session["user_id"]:
        conn.close()
        return "본인 댓글만 수정할 수 있습니다.", 403

    content = request.form.get("content", "").strip()
    if content:
        conn.execute(
            "UPDATE comments SET content = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (content, comment_id),
        )
        conn.commit()
    conn.close()
    return redirect(url_for("detail", post_id=comment["post_id"]) + f"#comment-{comment_id}")


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

@app.route("/posts/<int:post_id>/report", methods=["POST"])
def report_post(post_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if post is None:
        conn.close()
        return "글 없음", 404

    reason = request.form.get("reason", "").strip()
    try:
        conn.execute(
            "INSERT INTO reports (target_type, target_id, reporter_username, reason) VALUES ('post', ?, ?, ?)",
            (post_id, session["username"], reason),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()
    return redirect(url_for("detail", post_id=post_id))

@app.route("/comments/<int:comment_id>/report", methods=["POST"])
def report_comment(comment_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    comment = conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
    if comment is None:
        conn.close()
        return "댓글 없음", 404

    reason = request.form.get("reason", "").strip()
    try:
        conn.execute(
            "INSERT INTO reports (target_type, target_id, reporter_username, reason) VALUES ('comment', ?, ?, ?)",
            (comment_id, session["username"], reason),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
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
            "UPDATE posts SET title = ?, content = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
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
    delete_post_cascade(conn, post_id)
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

@app.route("/admin/users")
def admin_users():
    if "user_id" not in session:
        return redirect("/login")
    if not session.get("is_admin"):
        return "관리자만 접근할 수 있습니다.", 403

    conn = get_db()
    users = conn.execute("""
        SELECT users.*,
               (SELECT COUNT(*) FROM posts WHERE posts.user_id = users.id) AS post_count
        FROM users
        ORDER BY id ASC
    """).fetchall()
    admin_count = sum(1 for u in users if u["is_admin"])
    conn.close()
    return render_template("admin_users.html", users=users, admin_count=admin_count)

@app.route("/admin/users/<int:user_id>/toggle-admin", methods=["POST"])
def toggle_user_admin(user_id):
    if "user_id" not in session:
        return redirect("/login")
    if not session.get("is_admin"):
        return "관리자만 접근할 수 있습니다.", 403
    if user_id == session["user_id"]:
        return "본인의 권한은 변경할 수 없습니다.", 400

    conn = get_db()
    target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if target is None:
        conn.close()
        return "사용자를 찾을 수 없습니다.", 404

    if target["is_admin"]:
        admin_count = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_admin = 1").fetchone()["c"]
        if admin_count <= 1:
            conn.close()
            return "마지막 관리자는 권한을 해제할 수 없습니다.", 400
        conn.execute("UPDATE users SET is_admin = 0 WHERE id = ?", (user_id,))
    else:
        conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_users"))

@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
def delete_user(user_id):
    if "user_id" not in session:
        return redirect("/login")
    if not session.get("is_admin"):
        return "관리자만 접근할 수 있습니다.", 403
    if user_id == session["user_id"]:
        return "본인 계정은 여기서 삭제할 수 없습니다.", 400

    conn = get_db()
    target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if target is None:
        conn.close()
        return "사용자를 찾을 수 없습니다.", 404
    if target["is_admin"]:
        conn.close()
        return "다른 관리자는 삭제할 수 없습니다. 먼저 권한을 해제하세요.", 400

    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_users"))

@app.route("/admin/reports")
def admin_reports():
    if "user_id" not in session:
        return redirect("/login")
    if not session.get("is_admin"):
        return "관리자만 접근할 수 있습니다.", 403

    conn = get_db()
    rows = conn.execute("""
        SELECT reports.*,
               posts.title AS post_title,
               comments.content AS comment_content,
               comments.post_id AS comment_post_id
        FROM reports
        LEFT JOIN posts ON reports.target_type = 'post' AND reports.target_id = posts.id
        LEFT JOIN comments ON reports.target_type = 'comment' AND reports.target_id = comments.id
        WHERE reports.status = 'pending'
        ORDER BY reports.id DESC
    """).fetchall()
    conn.close()

    reports = []
    for r in rows:
        if r["target_type"] == "post":
            exists = r["post_title"] is not None
            preview = r["post_title"] if exists else "(삭제된 게시글)"
            link_post_id = r["target_id"] if exists else None
        else:
            exists = r["comment_content"] is not None
            preview = r["comment_content"] if exists else "(삭제된 댓글)"
            link_post_id = r["comment_post_id"] if exists else None
        reports.append({
            "id": r["id"], "target_type": r["target_type"],
            "reporter_username": r["reporter_username"], "reason": r["reason"],
            "created_at": r["created_at"], "preview": preview,
            "exists": exists, "link_post_id": link_post_id,
        })

    return render_template("admin_reports.html", reports=reports)

@app.route("/admin/reports/<int:report_id>/dismiss", methods=["POST"])
def dismiss_report(report_id):
    if "user_id" not in session:
        return redirect("/login")
    if not session.get("is_admin"):
        return "관리자만 접근할 수 있습니다.", 403

    conn = get_db()
    conn.execute("UPDATE reports SET status = 'dismissed' WHERE id = ?", (report_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_reports"))

@app.route("/admin/reports/<int:report_id>/remove-content", methods=["POST"])
def remove_reported_content(report_id):
    if "user_id" not in session:
        return redirect("/login")
    if not session.get("is_admin"):
        return "관리자만 접근할 수 있습니다.", 403

    conn = get_db()
    report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    if report is None:
        conn.close()
        return "신고 내역을 찾을 수 없습니다.", 404

    if report["target_type"] == "post":
        delete_post_cascade(conn, report["target_id"])
    else:
        conn.execute("DELETE FROM comments WHERE id = ?", (report["target_id"],))

    # 같은 대상을 신고한 모든 사람에게 처리 결과를 알린다
    reporters = conn.execute("""
        SELECT DISTINCT users.id FROM reports JOIN users ON users.username = reports.reporter_username
        WHERE reports.target_type = ? AND reports.target_id = ? AND reports.status = 'pending'
    """, (report["target_type"], report["target_id"])).fetchall()
    target_label = "게시글" if report["target_type"] == "post" else "댓글"
    for reporter in reporters:
        notify(conn, reporter["id"], f"신고하신 {target_label}이 관리자에 의해 삭제되었어요. 감사합니다!", url_for("index"))

    conn.execute(
        "UPDATE reports SET status = 'resolved' WHERE target_type = ? AND target_id = ? AND status = 'pending'",
        (report["target_type"], report["target_id"]),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("admin_reports"))

SCHOOL_CACHE_SECONDS = 10 * 60
school_cache: dict[tuple, tuple[float, object]] = {}

def cached_school_data(key, loader):
    """나이스 API 응답을 10분간 메모리에 캐시 (페이지를 열 때마다 외부 API를 부르지 않도록)"""
    hit = school_cache.get(key)
    if hit and time.time() - hit[0] < SCHOOL_CACHE_SECONDS:
        return hit[1]
    value = loader()
    school_cache[key] = (time.time(), value)
    return value

@app.route("/school")
def school():
    if "user_id" not in session:
        return redirect("/login")

    user = g.user
    school_info = timetable = None
    schedule = []
    error = None
    if user["school_name"]:
        school_info = cached_school_data(("school", user["school_name"]), lambda: neis.get_school(user["school_name"]))
        if school_info is None:
            error = "학교를 찾지 못했어요. 계정관리에서 학교 이름을 정확히 입력했는지 확인하세요."
        else:
            schedule = cached_school_data(
                ("schedule", school_info["school_code"], date.today()),
                lambda: neis.get_school_schedule(school_info),
            )
            if user["grade"] and user["class_nm"]:
                timetable = cached_school_data(
                    ("timetable", school_info["school_code"], user["grade"], user["class_nm"], date.today()),
                    lambda: neis.get_week_timetable(school_info, user["grade"], user["class_nm"]),
                )

    conn = get_db()
    assignments = conn.execute("""
        SELECT assignments.*, users.username FROM assignments
        LEFT JOIN users ON users.id = assignments.user_id
        WHERE due_date >= date('now', 'localtime', '-7 days')
        ORDER BY due_date ASC, id ASC
    """).fetchall()
    conn.close()

    return render_template(
        "school.html", school_info=school_info, timetable=timetable, schedule=schedule, error=error,
        neis_key_missing=not os.environ.get("NEIS_API_KEY"),
        assignments=[dict(a, d_day=d_day_label(a["due_date"]), is_past=a["due_date"] < date.today().isoformat())
                     for a in assignments],
        today=date.today().isoformat(),
    )

@app.route("/assignments", methods=["POST"])
def create_assignment():
    if "user_id" not in session:
        return redirect("/login")

    title = request.form.get("title", "").strip()
    subject = request.form.get("subject", "").strip() or None
    description = request.form.get("description", "").strip() or None
    due_date = request.form.get("due_date", "")
    try:
        date.fromisoformat(due_date)
    except ValueError:
        due_date = ""
    if title and due_date:
        conn = get_db()
        conn.execute(
            "INSERT INTO assignments (title, subject, due_date, description, user_id) VALUES (?, ?, ?, ?, ?)",
            (title, subject, due_date, description, session["user_id"]),
        )
        conn.commit()
        conn.close()
    return redirect(url_for("school") + "#assignments")

@app.route("/assignments/<int:assignment_id>/delete", methods=["POST"])
def delete_assignment(assignment_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    row = conn.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
    if row is None:
        conn.close()
        return "과제를 찾을 수 없습니다.", 404
    if row["user_id"] != session["user_id"] and not session.get("is_admin"):
        conn.close()
        return "등록한 사람 또는 관리자만 삭제할 수 있습니다.", 403
    conn.execute("DELETE FROM assignments WHERE id = ?", (assignment_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("school") + "#assignments")

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
            WHERE is_dm = 0
            ORDER BY chat_rooms.id DESC
        """, (session["username"],)).fetchall()
    else:
        rooms = conn.execute(f"""
            SELECT * FROM (
                SELECT chat_rooms.*, {member_flag_sql}
                FROM chat_rooms
                WHERE is_dm = 0
            )
            WHERE is_member = 1 OR is_public = 1
            ORDER BY id DESC
        """, (session["username"],)).fetchall()

    dms = conn.execute("""
        SELECT chat_rooms.id,
               (SELECT username FROM chat_room_members o
                WHERE o.room_id = chat_rooms.id AND o.username != :me) AS partner,
               (SELECT content FROM chat_messages WHERE room_id = chat_rooms.id ORDER BY id DESC LIMIT 1) AS last_content,
               (SELECT image FROM chat_messages WHERE room_id = chat_rooms.id ORDER BY id DESC LIMIT 1) AS last_image,
               (SELECT created_at FROM chat_messages WHERE room_id = chat_rooms.id ORDER BY id DESC LIMIT 1) AS last_at
        FROM chat_rooms
        JOIN chat_room_members m ON m.room_id = chat_rooms.id AND m.username = :me
        WHERE chat_rooms.is_dm = 1
        ORDER BY COALESCE(last_at, chat_rooms.created_at) DESC
    """, {"me": session["username"]}).fetchall()
    conn.close()
    return render_template("chat_rooms.html", rooms=rooms, dms=dms)

@app.route("/dm/<username>", methods=["POST"])
def open_dm(username):
    if "user_id" not in session:
        return redirect("/login")
    if username == session["username"]:
        return "자기 자신에게는 메시지를 보낼 수 없습니다.", 400

    conn = get_db()
    target = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if target is None:
        conn.close()
        return "사용자를 찾을 수 없습니다.", 404

    existing = conn.execute("""
        SELECT chat_rooms.id FROM chat_rooms
        JOIN chat_room_members a ON a.room_id = chat_rooms.id AND a.username = ?
        JOIN chat_room_members b ON b.room_id = chat_rooms.id AND b.username = ?
        WHERE chat_rooms.is_dm = 1
    """, (session["username"], username)).fetchone()
    if existing is not None:
        room_id = existing["id"]
    else:
        room_id = conn.execute(
            "INSERT INTO chat_rooms (name, created_by, is_dm) VALUES (?, ?, 1)",
            (f"{session['username']}, {username}", session["username"]),
        ).lastrowid
        conn.executemany(
            "INSERT INTO chat_room_members (room_id, username) VALUES (?, ?)",
            [(room_id, session["username"]), (room_id, username)],
        )
        conn.commit()
    conn.close()
    return redirect(url_for("chat_room", room_id=room_id))

@app.route("/chat/<int:room_id>/image", methods=["POST"])
def upload_chat_image(room_id):
    if "user_id" not in session:
        return jsonify({"error": "로그인이 필요합니다."}), 401

    conn = get_db()
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        return jsonify({"error": "방이 없습니다."}), 404

    error = chat_send_error(conn, room, session["username"])
    if error is not None:
        conn.close()
        return jsonify({"error": error[1]}), 403

    photo = request.files.get("image")
    if photo is None or not photo.filename:
        conn.close()
        return jsonify({"error": "사진 파일을 선택하세요."}), 400
    try:
        image = save_uploaded_image(photo, "chat")
    except ValueError as e:
        conn.close()
        return jsonify({"error": str(e)}), 400

    post_chat_message(conn, room, request.form.get("content", "").strip(), image=image)
    conn.close()
    return jsonify({"ok": True})

@app.route("/chat/<int:room_id>/join", methods=["POST"])
def join_room_self(room_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None:
        conn.close()
        return "방 없음", 404

    if room["is_dm"] or (not room["is_public"] and not session.get("is_admin")):
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
    if room["is_dm"]:
        allowed = is_room_member(conn, room_id, session["username"])
    else:
        allowed = is_owner or is_admin
    if not allowed:
        conn.close()
        return "본인 또는 관리자만 삭제 가능합니다.", 403

    for msg in conn.execute("SELECT image FROM chat_messages WHERE room_id = ? AND image IS NOT NULL", (room_id,)):
        delete_uploaded_file(msg["image"])
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

    if not can_access_room(conn, room, session["username"]):
        if room["is_dm"] or not room["is_public"]:
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
    partner = dm_partner(conn, room_id, session["username"]) if room["is_dm"] else None
    conn.close()

    is_owner = room["created_by"] == session.get("username")
    messages = list(reversed(rows))
    return render_template(
        "chat.html", room=room, messages=messages, pinned=pinned, partner=partner,
        is_owner=is_owner, can_announce=perms["announce"] and not room["is_dm"],
        can_manage_settings=any(perms.values()) and not room["is_dm"],
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
    if room["is_dm"] or not any(perms.values()):
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

@app.errorhandler(CSRFError)
def csrf_error(_):
    return "보안 토큰이 만료되었거나 올바르지 않아요. 페이지를 새로고침한 뒤 다시 시도하세요.", 400

@app.errorhandler(413)
def too_large(_):
    return "사진 용량이 너무 커요(5MB 이하). 뒤로 가서 작은 사진을 올려주세요.", 413

@socketio.on("connect")
def handle_connect():
    if "user_id" not in session:
        return False
    # 실시간 알림을 받기 위한 개인 룸
    join_room(f"user_{session['user_id']}")

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
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    allowed = room is not None and can_access_room(conn, room, session["username"])
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

def chat_send_error(conn, room, username):
    """메시지를 보낼 수 없으면 (이벤트 이름, 안내 문구) 를, 보낼 수 있으면 None 을 반환한다."""
    if not can_access_room(conn, room, username):
        return ("send_error", "이 방에 메시지를 보낼 수 없습니다.")

    is_admin = bool(session.get("is_admin"))
    is_owner = room["created_by"] == username
    if not is_admin and is_timed_out(conn, room["id"], username):
        return ("timeout_error", "타임아웃 상태에서는 메시지를 보낼 수 없습니다.")

    if not is_admin and not is_owner and room["slow_mode_seconds"] > 0:
        still_waiting = conn.execute("""
            SELECT datetime(created_at, '+' || ? || ' seconds') > datetime('now', 'localtime') AS waiting
            FROM chat_messages
            WHERE room_id = ? AND username = ?
            ORDER BY id DESC LIMIT 1
        """, (room["slow_mode_seconds"], room["id"], username)).fetchone()
        if still_waiting and still_waiting["waiting"]:
            return ("slowmode_error", f"슬로우 모드: {room['slow_mode_seconds']}초마다 한 번만 보낼 수 있습니다.")
    return None


def post_chat_message(conn, room, content, image=None):
    """메시지를 저장하고 방 전체에 보낸 뒤, @멘션·DM 알림을 보낸다. 소켓/HTTP 양쪽에서 쓴다."""
    username = session["username"]
    cur = conn.execute(
        "INSERT INTO chat_messages (username, content, room_id, image) VALUES (?, ?, ?, ?)",
        (username, content, room["id"], image),
    )
    row = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (cur.lastrowid,)).fetchone()

    link = url_for("chat_room", room_id=room["id"])
    online = set(room_online_users[str(room["id"])].values())
    if room["is_dm"]:
        partner = dm_partner(conn, room["id"], username)
        target = conn.execute("SELECT id FROM users WHERE username = ?", (partner,)).fetchone()
        if target is not None and partner not in online:
            preview = content[:30] if content else "사진"
            notify(conn, target["id"], f"💬 {username}님의 메시지: {preview}", link)
    elif content:
        members = {r["username"] for r in conn.execute(
            "SELECT username FROM chat_room_members WHERE room_id = ?", (room["id"],)
        )}
        notify_mentions(
            conn, content, username, f"{username}님이 '{room['name']}' 채팅방에서 회원님을 언급했어요",
            link, allowed_usernames=members,
        )
    conn.commit()

    socketio.emit("new_message", {
        "id": row["id"],
        "username": row["username"],
        "content": row["content"],
        "image": url_for("static", filename=row["image"]) if row["image"] else None,
        "created_at": row["created_at"],
    }, to=str(room["id"]))


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

    error = chat_send_error(conn, room, session["username"])
    if error is not None:
        conn.close()
        emit(error[0], {"message": error[1]})
        return

    post_chat_message(conn, room, content)
    conn.close()

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
    delete_uploaded_file(message["image"])
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
    room = conn.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None or not can_access_room(conn, room, session["username"]):
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

    if not can_access_room(conn, room, session["username"]):
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
