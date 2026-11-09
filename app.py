import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
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
TAIPEI = timezone(timedelta(hours=8))


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE=str(Path(app.instance_path) / 'review.sqlite3'),
        MAX_CONTENT_LENGTH=256 * 1024,
        SESSION_COOKIE_SAMESITE='Lax',
        # 換用新的 Cookie 名稱，避免舊版已作廢的登入重新生效。
        SESSION_COOKIE_NAME='review_session',
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
        migrate_db()

    @app.before_request
    def prepare_request():
        user_id = session.get('user_id')
        g.user = None
        if user_id is not None:
            g.user = db().execute(
                'SELECT id, username, role FROM users WHERE id = ?', (user_id,)
            ).fetchone()
        # 所有修改資料的表單都必須帶有這個瀏覽器工作階段的 CSRF token。
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
        # 每次送審重新計時，退回後修改草稿的時間不算進本輪等待。
        change(
            "UPDATE drafts SET category = ?, title = ?, body = ?, submitted_at = ?, status = 'pending', version = version + 1 "
            "WHERE id = ? AND author_id = ? AND status = 'draft' AND version = ?",
            (values['category'], values['title'], values['body'], datetime.now(timezone.utc).isoformat(),
             draft_id, g.user['id'], version),
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
            "WHERE drafts.status = 'pending' ORDER BY drafts.submitted_at IS NOT NULL, drafts.submitted_at, drafts.id"
        )
        # 舊稿件沒有歷史時間，排在已知時間之前，並明確標示。
        now = datetime.now(timezone.utc)
        return render_template(
            'list.html', review=True, rows=[
                {**dict(row), **submission_time(row['submitted_at'], now)} for row in rows
            ],
        )

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
        item = db().execute(
            'SELECT drafts.*, users.username FROM drafts JOIN users ON users.id = drafts.author_id '
            'WHERE drafts.id = ?', (draft_id,)
        ).fetchone()
        if status is None or item is None:
            abort(404)
        version = current_version(item, 'pending')
        reason = request.form.get('reason', '').strip() if action == 'return' else ''
        if action == 'return' and not 1 <= len(reason) <= 1000:
            return render_template(
                'draft.html', item=item, review=True, reason=reason,
                error='退回稿件時，請填寫 1 到 1,000 字的原因。',
            ), 422
        # 同一條 SQL 一起寫入退稿原因和狀態。版本條件可阻擋過期的審查頁。
        change(
            "UPDATE drafts SET status = ?, return_reason = ?, version = version + 1 "
            "WHERE id = ? AND status = 'pending' AND version = ?", (status, reason, draft_id, version)
        )
        flash('稿件已通過。' if action == 'approve' else '稿件已退回，作者可修改後重新送審。')
        return redirect(url_for('review_list'), 303)

    return app


def migrate_db():
    # CREATE TABLE IF NOT EXISTS 不會替現有資料表增加欄位。
    # 先取得寫入鎖，再檢查及新增欄位，讓更新可以重跑且保留舊資料。
    columns = {
        'drafts': {'submitted_at': 'TEXT', 'return_reason': "TEXT NOT NULL DEFAULT ''"},
    }
    with db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        for table, additions in columns.items():
            existing = {row['name'] for row in connection.execute(f'PRAGMA table_info({table})')}
            for name, definition in additions.items():
                if name not in existing:
                    connection.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')


def submission_time(value, now):
    if value is None:
        return dict(sent_label='舊資料未記錄', wait_label='無法計算')
    sent_at = datetime.fromisoformat(value)
    minutes = max(0, int((now - sent_at).total_seconds()) // 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        wait_label = f'{days} 天 {hours} 小時'
    elif hours:
        wait_label = f'{hours} 小時 {minutes} 分鐘'
    else:
        wait_label = f'{minutes} 分鐘' if minutes else '未滿 1 分鐘'
    return dict(sent_label=sent_at.astimezone(TAIPEI).strftime('%Y/%m/%d %H:%M'), wait_label=wait_label)


def db():
    # 同一個請求共用 SQLite 連線，請求結束後關閉。
    if 'db' not in g:
        g.db = sqlite3.connect(current_app.config['DATABASE'])
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


def requires(role):
    # 後端檢查角色，直接輸入網址也不能繞過。
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
    # 作者 ID 與稿件 ID 都要符合，避免讀到別人的稿件。
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
