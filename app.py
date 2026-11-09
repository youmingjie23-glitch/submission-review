import os
import re
import secrets
import sqlite3
from functools import wraps
from pathlib import Path

import click
from flask import (
    Flask, abort, current_app, flash, g, redirect, render_template, request, session, url_for,
)
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

CATEGORIES = ('小說', '散文', '詩歌', '評論', '其他')
STATUSES = {'draft': '編輯中', 'pending': '審查中', 'approved': '已通過'}


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE=str(Path(app.instance_path) / 'review.sqlite3'),
        MAX_CONTENT_LENGTH=256 * 1024,
        SESSION_COOKIE_SAMESITE='Lax',
    )
    if config:
        app.config.update(config)
    app.secret_key = app.secret_key or os.getenv('SECRET_KEY') or secrets.token_hex(32)
    database = Path(app.config['DATABASE'])
    database.parent.mkdir(parents=True, exist_ok=True)
    schema = Path(app.root_path, 'schema.sql').read_text(encoding='utf-8')
    @app.teardown_appcontext
    def close_db(_error):
        connection = g.pop('db', None)
        if connection is not None:
            connection.close()

    with app.app_context():
        db().executescript(schema)

    @app.before_request
    def prepare_request():
        user_id = session.get('user_id')
        g.user = db().execute('SELECT id, username, role FROM users WHERE id = ?', (user_id,)).fetchone() if user_id else None
        token = session.setdefault('csrf', secrets.token_hex(32))
        if request.method == 'POST' and not secrets.compare_digest(
            request.form.get('csrf', '').encode(), token.encode()
        ):
            abort(400, '表單已失效，請重新開啟頁面再試。')

    @app.context_processor
    def template_values():
        return dict(statuses=STATUSES, categories=CATEGORIES, csrf=session.get('csrf'))

    @app.errorhandler(HTTPException)
    def show_error(error):
        messages = {403: '你的帳號沒有操作權限。', 404: '找不到這個頁面或稿件。', 413: '送出的資料太大。'}
        return render_template('error.html', message=messages.get(error.code, error.description)), error.code

    @app.cli.command('create-editor')
    @click.argument('username')
    @click.password_option()
    def create_editor(username, password):
        error = account_error(username, password)
        if error:
            raise click.ClickException(error)
        if not add_user(username, password, 'editor'):
            raise click.ClickException('這個帳號已被使用。')
        click.echo('編輯帳號已建立。')

    @app.get('/')
    def home():
        if g.user is None:
            return redirect(url_for('login'))
        return redirect(url_for('review_list' if g.user['role'] == 'editor' else 'draft_list'))

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if g.user:
            return redirect(url_for('home'))
        username = request.form.get('username', '').strip()
        if request.method == 'POST':
            password = request.form.get('password', '')
            error = account_error(username, password)
            if not error and password != request.form.get('confirmation', ''):
                error = '兩次輸入的密碼不一致。'
            if not error:
                if add_user(username, password, 'author'):
                    flash('註冊完成，請登入。')
                    return redirect(url_for('login'), 303)
                error = '這個帳號已被使用。'
            return render_template('auth.html', register=True, error=error, username=username), 422
        return render_template('auth.html', register=True, username=username)

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if g.user:
            return redirect(url_for('home'))
        username = request.form.get('username', '').strip()
        if request.method == 'POST':
            password = request.form.get('password', '')
            user = db().execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
            if user and len(password) <= 128 and check_password_hash(user['password_hash'], password):
                session.clear()
                session['user_id'] = user['id']
                return redirect(url_for('review_list' if user['role'] == 'editor' else 'draft_list'), 303)
            return render_template('auth.html', error='帳號或密碼錯誤。', username=username), 422
        return render_template('auth.html', username=username)

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'), 303)

    @app.get('/drafts')
    @requires('author')
    def draft_list():
        rows = db().execute('SELECT * FROM drafts WHERE author_id = ? ORDER BY id DESC', (g.user['id'],))
        return render_template('list.html', rows=rows.fetchall())

    @app.route('/drafts/new', methods=['GET', 'POST'])
    @requires('author')
    def new_draft():
        if request.method == 'POST':
            values = draft_fields()
            if error := draft_error(values):
                return render_template('draft.html', item=values, error=error), 422
            with db() as connection:
                cursor = connection.execute(
                    'INSERT INTO drafts (author_id, category, title, body) VALUES (?, ?, ?, ?)',
                    (g.user['id'], values['category'], values['title'], values['body']),
                )
            flash('草稿已儲存。')
            return redirect(url_for('draft_detail', draft_id=cursor.lastrowid), 303)
        return render_template('draft.html', item={})

    @app.route('/drafts/<int:draft_id>', methods=['GET', 'POST'])
    @requires('author')
    def draft_detail(draft_id):
        item = author_draft(draft_id)
        if request.method == 'POST':
            version = current_version(item, 'draft')
            values = draft_fields()
            if error := draft_error(values):
                return render_template('draft.html', item={**dict(item), **values}, error=error), 422
            change(
                "UPDATE drafts SET category = ?, title = ?, body = ?, version = version + 1 "
                "WHERE id = ? AND author_id = ? AND status = 'draft' AND version = ?",
                (values['category'], values['title'], values['body'], draft_id, g.user['id'], version),
            )
            flash('草稿已儲存。')
            return redirect(url_for('draft_detail', draft_id=draft_id), 303)
        return render_template('draft.html', item=item)

    @app.post('/drafts/<int:draft_id>/submit')
    @requires('author')
    def submit(draft_id):
        item = author_draft(draft_id)
        version = current_version(item, 'draft')
        values = draft_fields() if 'title' in request.form else item
        error = draft_error(values)
        if not error and not all(values[field] for field in ('category', 'title', 'body')):
            error = '送審前請填完類別、標題與文章內容。'
        if error:
            return render_template('draft.html', item={**dict(item), **dict(values)}, error=error), 422
        change(
            "UPDATE drafts SET category = ?, title = ?, body = ?, status = 'pending', version = version + 1 "
            "WHERE id = ? AND author_id = ? AND status = 'draft' AND version = ?",
            (values['category'], values['title'], values['body'], draft_id, g.user['id'], version),
        )
        flash('已送出，等待編輯審查。')
        return redirect(url_for('draft_list'), 303)

    @app.post('/drafts/<int:draft_id>/delete')
    @requires('author')
    def delete(draft_id):
        version = current_version(author_draft(draft_id), 'draft')
        change(
            "DELETE FROM drafts WHERE id = ? AND author_id = ? AND status = 'draft' AND version = ?",
            (draft_id, g.user['id'], version),
        )
        flash('草稿已刪除。')
        return redirect(url_for('draft_list'), 303)

    @app.get('/review')
    @requires('editor')
    def review_list():
        rows = db().execute(
            "SELECT drafts.*, users.username FROM drafts JOIN users ON users.id = drafts.author_id "
            "WHERE drafts.status = 'pending' ORDER BY drafts.id"
        )
        return render_template('list.html', rows=rows.fetchall(), review=True)

    @app.get('/review/<int:draft_id>')
    @requires('editor')
    def review_detail(draft_id):
        item = db().execute(
            "SELECT drafts.*, users.username FROM drafts JOIN users ON users.id = drafts.author_id "
            "WHERE drafts.id = ? AND drafts.status = 'pending'", (draft_id,)
        ).fetchone()
        if item is None:
            abort(404)
        return render_template('draft.html', item=item, review=True)

    @app.post('/review/<int:draft_id>/<action>')
    @requires('editor')
    def decide(draft_id, action):
        status = {'approve': 'approved', 'return': 'draft'}.get(action)
        item = db().execute('SELECT * FROM drafts WHERE id = ?', (draft_id,)).fetchone()
        if status is None or item is None:
            abort(404)
        version = current_version(item, 'pending')
        change(
            "UPDATE drafts SET status = ?, version = version + 1 "
            "WHERE id = ? AND status = 'pending' AND version = ?", (status, draft_id, version)
        )
        flash('稿件已通過。' if action == 'approve' else '稿件已退回，作者可修改後重新送審。')
        return redirect(url_for('review_list'), 303)

    return app


def db():
    if 'db' not in g:
        g.db = sqlite3.connect(current_app.config['DATABASE'])
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


def requires(role):
    def decorate(view):
        @wraps(view)
        def guarded(*args, **kwargs):
            if g.user is None:
                return redirect(url_for('login'), 303)
            if g.user['role'] != role:
                abort(403)
            return view(*args, **kwargs)
        return guarded
    return decorate


def account_error(username, password):
    if not re.fullmatch(r'[a-zA-Z0-9_]{3,24}', username):
        return '帳號須為 3 到 24 個英文字母、數字或底線。'
    if not 8 <= len(password) <= 128:
        return '密碼長度須為 8 到 128 個字元。'


def add_user(username, password, role):
    with db() as connection:
        cursor = connection.execute(
            'INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?) ON CONFLICT(username) DO NOTHING',
            (username, generate_password_hash(password), role),
        )
    return cursor.rowcount == 1


def author_draft(draft_id):
    item = db().execute(
        'SELECT * FROM drafts WHERE id = ? AND author_id = ?', (draft_id, g.user['id'])
    ).fetchone()
    if item is None:
        abort(404)
    return item


def draft_fields():
    return {field: request.form.get(field, '').strip() for field in ('category', 'title', 'body')}


def draft_error(values):
    if values['category'] and values['category'] not in CATEGORIES:
        return '請選擇清單上的類別。'
    if len(values['title']) > 120 or len(values['body']) > 20000:
        return '標題最多 120 字，內文最多 20,000 字。'


def current_version(item, status):
    if item['status'] != status or request.form.get('version', type=int) != item['version']:
        abort(409, '稿件已更新或狀態已改變，請返回清單後重新開啟。')
    return item['version']


def change(sql, parameters):
    with db() as connection:
        if connection.execute(sql, parameters).rowcount != 1:
            abort(409, '稿件已更新或狀態已改變，請返回清單後重新開啟。')
