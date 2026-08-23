from flask import Flask
import sqlite3
from pathlib import Path
from flask import Flask, request, render_template, redirect, url_for

app = Flask(__name__)

DATABASE = Path(__file__).resolve().parent / 'bbs.db'

@app.route('/')
def index() :
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
def detail(post_id) :
    conn = get_db()
    post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    conn.close()
    return render_template('detail.html', post=post)

@app.route("/new")
def new_form() :
    return render_template('new.html')

@app.route("/posts", methods=['POST'])
def create_post() :
    title = request.form.get('title', "").strip()
    content = request.form.get('content', "").strip()

    conn = get_db()
    conn.execute("INSERT INTO posts (title, content) VALUES (?, ?)", (title, content))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route("/posts/<int:post_id>/edit")
def edit_form(post_id) :
    conn = get_db()
    post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    conn.close()
    return render_template('edit.html', post=post)

@app.route("/posts/<int:post_id>/update", methods=['POST'])
def update_post(post_id) :
    title = request.form.get('title', "").strip()
    content = request.form.get('content', "").strip()

    conn = get_db()
    conn.execute("UPDATE posts SET title = ?, content = ? WHERE id = ?", (title, content, post_id))
    conn.commit()
    conn.close()
    return redirect(url_for('detail', post_id=post_id))

@app.route("/posts/<int:post_id>/delete", methods=['POST'])
def delete_post(post_id) :
    conn = get_db()
    conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

def get_db() :
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def create_table() :
    conn = get_db()
    conn.execute("""
    CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()

if __name__ == '__main__' :
    create_table()
    app.run(debug=True, port=5001)
