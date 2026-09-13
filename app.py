from flask import Flask, session, request, render_template, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
import os
import sqlite3
from pathlib import Path
from opendata import fetch_air_quality

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")

DATABASE = Path(__file__).resolve().parent / 'bbs.db'

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
        
def get_db() :
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def get_post_or_404(post_id):
    conn = get_db()
    post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    conn.close()
    return post

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
    # 8주차까지 없던 컬럼을 이어쓰는 DB에 추가 (한 번만 실행됨)
    for statement in [
        "ALTER TABLE posts ADD COLUMN user_id INTEGER",
        "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'",
        "ALTER TABLE posts ADD COLUMN is_notice INTEGER NOT NULL DEFAULT 0",
    ]:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()

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

if __name__ == '__main__' :
    create_tables()
    app.run(debug=True, port=5001)
