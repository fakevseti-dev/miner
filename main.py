from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import sqlite3
import psycopg2
from psycopg2.extras import DictCursor
import datetime
import os
import hashlib
from functools import wraps
from urllib.parse import urlparse

app = Flask(__name__)

# ─── CONFIG ───────────────────────────────────────────────────
DEBUG = os.environ.get('DEBUG', 'True') == 'True'
PORT = int(os.environ.get('PORT', 5000))

# Получаем URL базы из настроек Render. Если его нет — используем sqlite
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///data/crypto.db')
DB_PATH = 'data/crypto.db'

app.secret_key = os.environ.get('SECRET_KEY', 'neon-secret-key-2024')

USDT_WALLET_ADDRESS = 'TQCz49r91qcFJ8wvcKxJqRNWJNSH24YqcK'

ADMIN_USERNAME = 'admin'
ADMIN_PASSWORD = 'neon_admin_2024'

os.makedirs('data', exist_ok=True)
os.makedirs('logs', exist_ok=True)


# ─── DATABASE HELPERS ──────────────────────────────────────────
def get_db():
    """Универсальное подключение к DB (SQLite или PostgreSQL)"""
    if DATABASE_URL.startswith('sqlite'):
        db_file = DATABASE_URL.replace('sqlite:///', '')
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        conn.is_sqlite = True
    else:
        # Для PostgreSQL на Render (исправляем префикс если нужно)
        url = DATABASE_URL.replace("postgres://", "postgresql://")
        conn = psycopg2.connect(url, sslmode='require', cursor_factory=DictCursor)
        conn.is_sqlite = False
    return conn

class SmartCursor:
    """Обертка для курсора, которая автоматически меняет синтаксис ? на %s для Postgres"""
    def __init__(self, cursor, is_sqlite):
        self._cursor = cursor
        self.is_sqlite = is_sqlite

    def execute(self, query, params=()):
        if not self.is_sqlite:
            query = query.replace('?', '%s')
        return self._cursor.execute(query, params)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def close(self):
        self._cursor.close()

def get_cursor(conn):
    return SmartCursor(conn.cursor(), getattr(conn, 'is_sqlite', True))


# ─── DATABASE INIT ────────────────────────────────────────────
def init_db():
    conn = get_db()
    c = get_cursor(conn)

    # В PostgreSQL вместо AUTOINCREMENT используется SERIAL
    id_col = "INTEGER PRIMARY KEY AUTOINCREMENT" if getattr(conn, 'is_sqlite', True) else "SERIAL PRIMARY KEY"

    c.execute(f'''CREATE TABLE IF NOT EXISTS users (
        id {id_col},
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        balance REAL DEFAULT 0.0,
        total_earned REAL DEFAULT 0.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_login TIMESTAMP,
        is_active BOOLEAN DEFAULT TRUE
    )''')

    c.execute(f'''CREATE TABLE IF NOT EXISTS active_plans (
        id {id_col},
        user_id INTEGER NOT NULL,
        plan_name TEXT NOT NULL,
        plan_price REAL NOT NULL,
        activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        is_active BOOLEAN DEFAULT TRUE,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')

    c.execute(f'''CREATE TABLE IF NOT EXISTS transactions (
        id {id_col},
        user_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        amount REAL NOT NULL,
        status TEXT DEFAULT 'pending',
        description TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')

    c.execute(f'''CREATE TABLE IF NOT EXISTS activity_log (
        id {id_col},
        user_id INTEGER,
        module TEXT,
        action TEXT,
        status TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')

    c.execute(f'''CREATE TABLE IF NOT EXISTS mining_stats (
        id {id_col},
        user_id INTEGER,
        session_id TEXT UNIQUE,
        total_hash_rate REAL,
        total_earnings REAL,
        duration_seconds INTEGER,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')

    c.execute(f'''CREATE TABLE IF NOT EXISTS notifications (
        id {id_col},
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        is_read BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')

    conn.commit()
    conn.close()


init_db()


# ─── HELPERS ──────────────────────────────────────────────────
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password, password_hash):
    return hash_password(password) == password_hash

def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def get_user_by_id(user_id):
    try:
        conn = get_db()
        c = get_cursor(conn)
        c.execute("SELECT * FROM users WHERE id = ? AND is_active = TRUE", (user_id,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({"status": "error", "message": "Not authenticated"}), 401
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_admin'):
            return jsonify({"status": "error", "message": "Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated


# ═══════════════════════════════════════════════════════════════
#  AUTH ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('miner'))
    return redirect(url_for('login'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        data = request.json or {}
        username = data.get('username', '').strip()
        email    = data.get('email', '').strip()
        password = data.get('password', '')
        confirm  = data.get('confirm_password', '')

        if not all([username, email, password, confirm]):
            return jsonify({"status": "error", "message": "Все поля обязательны"}), 400
        if len(username) < 3:
            return jsonify({"status": "error", "message": "Имя пользователя минимум 3 символа"}), 400
        if len(password) < 6:
            return jsonify({"status": "error", "message": "Пароль минимум 6 символов"}), 400
        if password != confirm:
            return jsonify({"status": "error", "message": "Пароли не совпадают"}), 400

        try:
            conn = get_db()
            c = get_cursor(conn)
            c.execute("SELECT id FROM users WHERE username = ? OR email = ?", (username, email))
            if c.fetchone():
                conn.close()
                return jsonify({"status": "error", "message": "Пользователь уже существует"}), 400

            c.execute(
                "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
                (username, email, hash_password(password))
            )
            conn.commit()
            conn.close()
            return jsonify({"status": "success", "message": "Регистрация успешна!"}), 201
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.json or {}
        username = data.get('username', '').strip()
        password = data.get('password', '')

        if not username or not password:
            return jsonify({"status": "error", "message": "Заполните все поля"}), 400

        try:
            conn = get_db()
            c = get_cursor(conn)
            c.execute("SELECT id, username, password_hash, is_active FROM users WHERE username = ?", (username,))
            user = c.fetchone()
            conn.close()

            if not user or not user['is_active']:
                return jsonify({"status": "error", "message": "Неверные учётные данные"}), 401
            if not verify_password(password, user['password_hash']):
                return jsonify({"status": "error", "message": "Неверные учётные данные"}), 401

            session['user_id']  = user['id']
            session['username'] = user['username']

            conn = get_db()
            c = get_cursor(conn)
            c.execute("UPDATE users SET last_login = ? WHERE id = ?", (now(), user['id']))
            conn.commit()
            conn.close()

            return jsonify({"status": "success", "message": "Успешный вход"}), 200
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/miner')
def miner():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = get_user_by_id(session['user_id'])
    if not user:
        session.clear()
        return redirect(url_for('login'))
    return render_template('miner_premium.html', user=user)


@app.route('/scanner')
def scanner():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = get_user_by_id(session['user_id'])
    if not user:
        session.clear()
        return redirect(url_for('login'))
    return render_template('crypto_scanner.html', user=user)


# ═══════════════════════════════════════════════════════════════
#  SCANNER SESSION API
# ═══════════════════════════════════════════════════════════════

@app.route('/api/scanner/start', methods=['POST'])
@login_required
def scanner_start():
    try:
        conn = get_db()
        c = get_cursor(conn)
        c.execute(
            "INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
            (session['user_id'], 'NETWORK_SCANNER', 'SESSION_START', 'OK', now())
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/scanner/stop', methods=['POST'])
@login_required
def scanner_stop():
    try:
        data     = request.json or {}
        minutes  = int(data.get('minutes', 0))
        found    = int(data.get('found', 0))
        saved    = int(data.get('saved', 0))

        conn = get_db()
        c = get_cursor(conn)

        for _ in range(max(minutes, 1)):
            c.execute(
                "INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
                (session['user_id'], 'NETWORK_SCANNER', 'SESSION_TICK', 'OK', now())
            )

        c.execute(
            "INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
            (session['user_id'], 'NETWORK_SCANNER',
             f'SESSION_END found={found} saved={saved} min={minutes}', 'OK', now())
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/scanner/save-bundle', methods=['POST'])
@login_required
def scanner_save_bundle():
    try:
        data   = request.json or {}
        coin   = data.get('coin', '')
        ex1    = data.get('ex1', '')
        ex2    = data.get('ex2', '')
        spread = float(data.get('spread', 0))
        quote  = data.get('quote', 'USDT')

        if not coin or not ex1 or not ex2:
            return jsonify({"status": "error", "message": "Неверные параметры"}), 400

        conn = get_db()
        c = get_cursor(conn)
        c.execute(
            "INSERT INTO scanner_bundles (user_id, coin, quote, exchange_buy, exchange_sell, spread, saved_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (session['user_id'], coin, quote, ex1, ex2, spread, now())
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/scanner/bundles')
@login_required
def scanner_get_bundles():
    try:
        conn = get_db()
        c = get_cursor(conn)
        c.execute(
            "SELECT id, coin, quote, exchange_buy, exchange_sell, spread, saved_at "
            "FROM scanner_bundles WHERE user_id = ? ORDER BY saved_at DESC LIMIT 200",
            (session['user_id'],)
        )
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({"status": "success", "bundles": rows})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
#  USER API
# ═══════════════════════════════════════════════════════════════

@app.route('/api/user/profile')
@login_required
def get_profile():
    user = get_user_by_id(session['user_id'])
    if not user:
        return jsonify({"status": "error"}), 401
    return jsonify({
        "id":           user['id'],
        "username":     user['username'],
        "email":        user['email'],
        "balance":      user['balance'],
        "total_earned": user['total_earned'],
        "created_at":   user['created_at'],
        "last_login":   user['last_login'],
    })


@app.route('/api/user/balance')
@login_required
def get_balance():
    user = get_user_by_id(session['user_id'])
    return jsonify({"balance": user['balance'], "total_earned": user['total_earned']})


@app.route('/api/user/deposit', methods=['POST'])
@login_required
def deposit():
    try:
        amount = float((request.json or {}).get('amount', 0))
        if amount <= 0:
            return jsonify({"status": "error", "message": "Сумма должна быть больше нуля"}), 400

        conn = get_db()
        c = get_cursor(conn)
        c.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, session['user_id']))
        c.execute(
            "INSERT INTO transactions (user_id, type, amount, status, description) VALUES (?,?,?,?,?)",
            (session['user_id'], 'deposit', amount, 'completed', 'Пополнение баланса')
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "Баланс пополнен"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/user/withdraw', methods=['POST'])
@login_required
def withdraw():
    try:
        data          = request.json or {}
        amount        = float(data.get('amount', 0))
        wallet        = data.get('wallet_address', '').strip()

        if amount <= 0:
            return jsonify({"status": "error", "message": "Сумма должна быть больше нуля"}), 400
        if not wallet:
            return jsonify({"status": "error", "message": "Укажите адрес кошелька"}), 400

        user = get_user_by_id(session['user_id'])
        if user['balance'] < amount:
            return jsonify({"status": "error", "message": "Недостаточно средств"}), 400

        conn = get_db()
        c = get_cursor(conn)
        c.execute("UPDATE users SET balance = balance - ? WHERE id = ?", (amount, session['user_id']))
        c.execute(
            "INSERT INTO transactions (user_id, type, amount, status, description) VALUES (?,?,?,?,?)",
            (session['user_id'], 'withdraw', amount, 'pending', f'Кошелёк: {wallet}')
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "Запрос на вывод создан"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/user/transactions')
@login_required
def get_transactions():
    try:
        conn = get_db()
        c = get_cursor(conn)
        c.execute(
            "SELECT id, type, amount, status, description, timestamp FROM transactions "
            "WHERE user_id = ? ORDER BY timestamp DESC LIMIT 50",
            (session['user_id'],)
        )
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({"status": "success", "transactions": rows, "total": len(rows)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/user/notifications')
@login_required
def get_notifications():
    try:
        conn = get_db()
        c = get_cursor(conn)
        c.execute(
            "SELECT id, title, message, is_read, created_at FROM notifications "
            "WHERE user_id = ? AND is_read = FALSE ORDER BY created_at DESC LIMIT 20",
            (session['user_id'],)
        )
        notifs = [dict(r) for r in c.fetchall()]
        if notifs:
            c.execute("UPDATE notifications SET is_read = TRUE WHERE user_id = ? AND is_read = FALSE",
                      (session['user_id'],))
            conn.commit()
        conn.close()
        return jsonify({"status": "success", "notifications": notifs, "count": len(notifs)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


# ═══════════════════════════════════════════════════════════════
#  PLAN ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route('/api/plan/active')
@login_required
def get_active_plan():
    try:
        conn = get_db()
        c = get_cursor(conn)
        c.execute(
            "SELECT plan_name, plan_price, activated_at FROM active_plans "
            "WHERE user_id = ? AND is_active = TRUE ORDER BY activated_at DESC LIMIT 1",
            (session['user_id'],)
        )
        row = c.fetchone()
        conn.close()
        if row:
            return jsonify({
                "status":       "success",
                "has_plan":     True,
                "plan_name":    row['plan_name'],
                "plan_price":   row['plan_price'],
                "activated_at": row['activated_at'],
            })
        return jsonify({"status": "success", "has_plan": False})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/plan/purchase', methods=['POST'])
@login_required
def purchase_plan():
    try:
        data       = request.json or {}
        plan_name  = data.get('plan_name', '').strip()
        plan_price = float(data.get('plan_price', 0))

        if not plan_name or plan_price <= 0:
            return jsonify({"status": "error", "message": "Неверные параметры плана"}), 400

        user = get_user_by_id(session['user_id'])
        if user['balance'] < plan_price:
            return jsonify({"status": "error", "message": "Недостаточно средств"}), 400

        conn = get_db()
        c = get_cursor(conn)

        c.execute("UPDATE active_plans SET is_active = FALSE WHERE user_id = ? AND is_active = TRUE",
                  (session['user_id'],))

        c.execute("UPDATE users SET balance = balance - ? WHERE id = ?",
                  (plan_price, session['user_id']))

        c.execute("INSERT INTO active_plans (user_id, plan_name, plan_price) VALUES (?,?,?)",
                  (session['user_id'], plan_name, plan_price))

        c.execute(
            "INSERT INTO transactions (user_id, type, amount, status, description) VALUES (?,?,?,?,?)",
            (session['user_id'], 'plan_purchase', plan_price, 'completed', f'Покупка плана {plan_name}')
        )

        c.execute(
            "INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
            (session['user_id'], 'MINER_NODE', f'PURCHASE_PLAN_{plan_name.upper()}', 'OK', now())
        )

        conn.commit()
        conn.close()

        return jsonify({
            "status":      "success",
            "message":     f"План {plan_name} успешно куплен!",
            "plan_name":   plan_name,
            "new_balance": user['balance'] - plan_price,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/plan/change', methods=['POST'])
@login_required
def change_plan():
    try:
        data       = request.json or {}
        plan_name  = data.get('plan_name', '').strip()
        plan_price = float(data.get('plan_price', 0))

        if not plan_name or plan_price <= 0:
            return jsonify({"status": "error", "message": "Неверные параметры плана"}), 400

        user = get_user_by_id(session['user_id'])
        if user['balance'] < plan_price:
            return jsonify({"status": "error", "message": "Недостаточно средств"}), 400

        conn = get_db()
        c = get_cursor(conn)

        c.execute("UPDATE active_plans SET is_active = FALSE WHERE user_id = ? AND is_active = TRUE",
                  (session['user_id'],))
        c.execute("UPDATE users SET balance = balance - ? WHERE id = ?",
                  (plan_price, session['user_id']))
        c.execute("INSERT INTO active_plans (user_id, plan_name, plan_price) VALUES (?,?,?)",
                  (session['user_id'], plan_name, plan_price))
        c.execute(
            "INSERT INTO transactions (user_id, type, amount, status, description) VALUES (?,?,?,?,?)",
            (session['user_id'], 'plan_purchase', plan_price, 'completed', f'Смена плана → {plan_name}')
        )
        c.execute(
            "INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
            (session['user_id'], 'MINER_NODE', f'CHANGE_PLAN_{plan_name.upper()}', 'OK', now())
        )

        conn.commit()
        conn.close()

        return jsonify({
            "status":      "success",
            "message":     f"План изменён на {plan_name}",
            "plan_name":   plan_name,
            "new_balance": user['balance'] - plan_price,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
#  MINING / ACTIVITY API
# ═══════════════════════════════════════════════════════════════

@app.route('/api/save', methods=['POST'])
@login_required
def save_action():
    try:
        data = request.json or {}
        conn = get_db()
        c = get_cursor(conn)
        c.execute(
            "INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
            (session['user_id'], data.get('module', ''), data.get('action', ''), 'OK', now())
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "saved"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/mining/save-earnings', methods=['POST'])
@login_required
def save_mining_earnings():
    try:
        data     = request.json or {}
        earned   = float(data.get('earned', 0))
        duration = int(data.get('duration_seconds', 0))

        if earned <= 0:
            return jsonify({"status": "ok"})

        conn = get_db()
        c = get_cursor(conn)
        c.execute(
            "UPDATE users SET total_earned = total_earned + ? WHERE id = ?",
            (earned, session['user_id'])
        )
        c.execute(
            "INSERT INTO mining_stats (user_id, total_earnings, duration_seconds, timestamp) VALUES (?,?,?,?)",
            (session['user_id'], earned, duration, now())
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/history')
@login_required
def get_history():
    try:
        conn = get_db()
        c = get_cursor(conn)
        c.execute(
            "SELECT * FROM activity_log WHERE user_id = ? ORDER BY id DESC LIMIT 20",
            (session['user_id'],)
        )
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify(rows)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/payment/usdt-address')
def get_usdt_address():
    return jsonify({
        "status":   "success",
        "address":  USDT_WALLET_ADDRESS,
        "network":  "TRON (TRC-20)",
        "currency": "USDT",
    })


@app.route('/api/health')
def health_check():
    return jsonify({
        "status":        "running",
        "version":       "4.0.0",
        "db_connected":  True,
        "authenticated": 'user_id' in session,
    })


# ═══════════════════════════════════════════════════════════════
#  ADMIN ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route('/admin')
def admin_panel():
    if not session.get('is_admin'):
        return redirect(url_for('admin_login'))
    return render_template('admin.html')


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        data = request.json or {}
        if data.get('username') == ADMIN_USERNAME and data.get('password') == ADMIN_PASSWORD:
            session['is_admin']       = True
            session['admin_username'] = ADMIN_USERNAME
            return jsonify({"status": "success"})
        return jsonify({"status": "error", "message": "Неверные данные"}), 401
    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    session.pop('admin_username', None)
    return redirect(url_for('admin_login'))


@app.route('/api/admin/stats')
@admin_required
def admin_stats():
    try:
        conn  = get_db()
        c     = get_cursor(conn)
        today = datetime.datetime.now().strftime('%Y-%m-%d')

        c.execute("SELECT COUNT(*) as n FROM users WHERE is_active = TRUE")
        total_users = c.fetchone()['n']

        c.execute("SELECT COALESCE(SUM(balance),0) as s FROM users")
        total_balance = c.fetchone()['s']

        c.execute("SELECT COUNT(*) as n FROM transactions")
        total_transactions = c.fetchone()['n']

        c.execute("SELECT COUNT(DISTINCT user_id) as n FROM activity_log WHERE timestamp >= ?", (today,))
        active_users = c.fetchone()['n']

        c.execute("SELECT COUNT(*) as n FROM activity_log WHERE module='MINER_NODE' AND timestamp >= ?", (today,))
        mining_minutes_today = c.fetchone()['n']

        c.execute("SELECT id, username, email, balance, total_earned, last_login, is_active "
                  "FROM users ORDER BY created_at DESC LIMIT 5")
        recent_users = [dict(r) for r in c.fetchall()]

        c.execute("SELECT al.module, al.action, al.status, al.timestamp, u.username "
                  "FROM activity_log al LEFT JOIN users u ON al.user_id = u.id "
                  "ORDER BY al.timestamp DESC LIMIT 10")
        recent_activity = [dict(r) for r in c.fetchall()]

        conn.close()
        return jsonify({
            "status":               "success",
            "total_users":          total_users,
            "total_balance":        total_balance,
            "total_transactions":   total_transactions,
            "active_users":         active_users,
            "mining_minutes_today": mining_minutes_today,
            "recent_users":         recent_users,
            "recent_activity":      recent_activity,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/admin/users')
@admin_required
def admin_users():
    try:
        conn = get_db()
        c    = get_cursor(conn)
        c.execute("SELECT id, username, email, balance, total_earned, created_at, last_login, is_active "
                  "FROM users ORDER BY created_at DESC")
        users = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({"status": "success", "users": users})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/admin/fund-user', methods=['POST'])
@admin_required
def admin_fund_user():
    try:
        data    = request.json or {}
        user_id = int(data.get('user_id'))
        amount  = float(data.get('amount', 0))
        comment = data.get('comment', 'Начисление от администратора')

        if amount <= 0:
            return jsonify({"status": "error", "message": "Сумма должна быть больше нуля"}), 400

        conn = get_db()
        c    = get_cursor(conn)
        c.execute("SELECT id, username FROM users WHERE id = ? AND is_active = TRUE", (user_id,))
        user = c.fetchone()
        if not user:
            conn.close()
            return jsonify({"status": "error", "message": "Пользователь не найден"}), 404

        c.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, user_id))
        c.execute("INSERT INTO transactions (user_id, type, amount, status, description) VALUES (?,?,?,?,?)",
                  (user_id, 'admin_credit', amount, 'completed', comment))
        c.execute("INSERT INTO notifications (user_id, title, message) VALUES (?,?,?)",
                  (user_id, '💰 Баланс пополнен',
                   f'Ваш баланс успешно пополнен на ${amount:.2f}. {comment}'))
        c.execute("INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
                  (user_id, 'ADMIN', 'BALANCE_CREDITED', 'OK', now()))

        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": f"Начислено ${amount:.2f} пользователю {user['username']}"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/admin/confirm-payment', methods=['POST'])
@admin_required
def admin_confirm_payment():
    try:
        data    = request.json or {}
        user_id = int(data.get('user_id'))
        amount  = float(data.get('amount', 0))

        if amount <= 0:
            return jsonify({"status": "error", "message": "Сумма должна быть больше нуля"}), 400

        conn = get_db()
        c    = get_cursor(conn)
        c.execute("SELECT id, username FROM users WHERE id = ? AND is_active = TRUE", (user_id,))
        user = c.fetchone()
        if not user:
            conn.close()
            return jsonify({"status": "error", "message": "Пользователь не найден"}), 404

        c.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, user_id))
        c.execute("INSERT INTO transactions (user_id, type, amount, status, description) VALUES (?,?,?,?,?)",
                  (user_id, 'deposit', amount, 'completed', 'USDT TRC-20 подтверждён администратором'))
        c.execute("INSERT INTO notifications (user_id, title, message) VALUES (?,?,?)",
                  (user_id, '💰 Баланс пополнен',
                   f'Ваш платёж на сумму ${amount:.2f} USDT подтверждён. Средства зачислены на баланс.'))
        c.execute("INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
                  (user_id, 'ADMIN', 'PAYMENT_CONFIRMED', 'OK', now()))

        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": f"Платёж ${amount:.2f} подтверждён для {user['username']}"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/admin/toggle-user', methods=['POST'])
@admin_required
def admin_toggle_user():
    try:
        data    = request.json or {}
        user_id = int(data.get('user_id'))
        active  = bool(data.get('active', True))
        conn = get_db()
        c    = get_cursor(conn)
        c.execute("UPDATE users SET is_active = ? WHERE id = ?", (True if active else False, user_id))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/admin/activity')
@admin_required
def admin_activity():
    try:
        conn  = get_db()
        c     = get_cursor(conn)
        today = datetime.datetime.now().strftime('%Y-%m-%d')

        c.execute("SELECT al.id, al.module, al.action, al.status, al.timestamp, u.username "
                  "FROM activity_log al LEFT JOIN users u ON al.user_id = u.id "
                  "ORDER BY al.timestamp DESC LIMIT 200")
        activity = [dict(r) for r in c.fetchall()]

        c.execute("""
            SELECT u.username,
                SUM(CASE WHEN al.module='MINER_NODE' THEN 1 ELSE 0 END)       AS miner_minutes,
                SUM(CASE WHEN al.module='NETWORK_SCANNER' THEN 1 ELSE 0 END)  AS scanner_minutes,
                COUNT(*) AS total_events,
                MAX(al.timestamp) AS last_activity
            FROM activity_log al
            LEFT JOIN users u ON al.user_id = u.id
            WHERE al.timestamp >= ?
            GROUP BY al.user_id
            ORDER BY total_events DESC
        """, (today,))
        sessions = [dict(r) for r in c.fetchall()]

        conn.close()
        return jsonify({"status": "success", "activity": activity, "sessions": sessions})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/admin/transactions')
@admin_required
def admin_transactions():
    try:
        conn = get_db()
        c    = get_cursor(conn)
        c.execute("SELECT t.id, t.type, t.amount, t.status, t.description, t.timestamp, u.username "
                  "FROM transactions t LEFT JOIN users u ON t.user_id = u.id "
                  "ORDER BY t.timestamp DESC LIMIT 100")
        txs = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({"status": "success", "transactions": txs})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
#  ERROR HANDLERS
# ═══════════════════════════════════════════════════════════════

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not Found"}), 404

@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Internal Server Error"}), 500


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print(f"🚀  Crypto Miner Pro  →  http://localhost:{PORT}")
    print(f"🔑  Admin panel       →  http://localhost:{PORT}/admin")
    print(f"📁  Database          →  {DB_PATH}")
    app.run(debug=DEBUG, port=PORT, host='0.0.0.0')