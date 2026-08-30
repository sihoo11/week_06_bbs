from flask import Flask, session
from werkzeug.security import generate_password_hash, check_password_hash
import os
import sqlite3
from pathlib import Path
from flask import Flask, request, render_template, redirect, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")

DATABASE = Path(__file__).resolve().parent / 'bbs.db'

@app.route('/')
def index():
    search_query = request.args.get('search', '').strip()
    conn = get_db()

    if search_query:
        posts = conn.execute("SELECT * FROM posts WHERE title LIKE ? ORDER BY created_at DESC",
                           (f"%{search_query}%",)).fetchall()
    else:
        posts = conn.execute("SELECT * FROM posts ORDER BY created_at DESC").fetchall()

    conn.close()
    return render_template('list.html', posts=posts, search_query=search_query)

@app.route("/posts/<int:post_id>")
def detail(post_id):
    conn = get_db()
    post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    conn.close()
    return render_template('detail.html', post=post)

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
    conn.close()

    if post["user_id"] != session["user_id"]:
        return "본인만 수정 가능합니다.", 403

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

    if post["user_id"] != session["user_id"]:
        conn.close()
        return "본인만 삭제 가능합니다.", 403

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
            return redirect('/')
        return render_template('login.html', error='아이디 또는 비밀번호가 틀렸습니다.')
    return render_template('login.html')

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect('/')
        
def get_db() :
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def create_table() :
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            user_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    try:
        conn.execute("ALTER TABLE posts ADD COLUMN user_id INTEGER")
    except sqlite3.OperationalError :
        pass
    conn.commit()
    conn.close()

if __name__ == '__main__' :
    create_table()
    app.run(debug=True, port=5001)
