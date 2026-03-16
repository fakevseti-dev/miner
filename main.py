from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import sqlite3
import psycopg2
from psycopg2.extras import DictCursor
import datetime
import os
import hashlib
from functools import wraps

app = Flask(__name__)

# ─── CONFIG ───────────────────────────────────────────────────
DEBUG = os.environ.get('DEBUG', 'True') == 'True'
PORT = int(os.environ.get('PORT', 5000))

# Получаем URL базы из настроек Render. Если его нет — используем sqlite
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///data/crypto.db')
DB_PATH = 'data/crypto.db'

app.secret_key = os.environ.get('SECRET_KEY', 'neon-secret-key-2026')
USDT_WALLET_ADDRESS = 'TQCz49r91qcFJ8wvcKxJqRNWJNSH24YqcK'
ADMIN_USERNAME = 'admin'
ADMIN_PASSWORD = 'neon_admin_2024'

os.makedirs('data', exist_ok=True)
os.makedirs('logs', exist_ok=True)


# ─── DATABASE HELPERS ──────────────────────────────────────────
IS_SQLITE = DATABASE_URL.startswith('sqlite')

def get_db():
    """Универсальное подключение к DB (SQLite или PostgreSQL)"""
    if IS_SQLITE:
        db_file = DATABASE_URL.replace('sqlite:///', '')
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
    else:
        url = DATABASE_URL.replace("postgres://", "postgresql://")
        # Если URL содержит 'internal', отключаем требование SSL
        ssl_mode = 'prefer' if 'internal' in url else 'require'
        conn = psycopg2.connect(url, sslmode=ssl_mode, cursor_factory=DictCursor)
    return conn

class SmartCursor:
    """Обертка для автоматической замены ? на %s для Postgres"""
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
    return SmartCursor(conn.cursor(), IS_SQLITE)


# ─── DATABASE INIT ────────────────────────────────────────────
def init_db():
    conn = get_db()
    c = get_cursor(conn)

    id_col = "INTEGER PRIMARY KEY AUTOINCREMENT" if IS_SQLITE else "SERIAL PRIMARY KEY"

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
        is_active BOOLEAN DEFAULT TRUE
    )''')

    c.execute(f'''CREATE TABLE IF NOT EXISTS transactions (
        id {id_col},
        user_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        amount REAL NOT NULL,
        status TEXT DEFAULT 'pending',
        description TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute(f'''CREATE TABLE IF NOT EXISTS activity_log (
        id {id_col},
        user_id INTEGER,
        module TEXT,
        action TEXT,
        status TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute(f'''CREATE TABLE IF NOT EXISTS mining_stats (
        id {id_col},
        user_id INTEGER,
        session_id TEXT UNIQUE,
        total_hash_rate REAL,
        total_earnings REAL,
        duration_seconds INTEGER,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute(f'''CREATE TABLE IF NOT EXISTS notifications (
        id {id_col},
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        is_read BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    c.execute(f'''CREATE TABLE IF NOT EXISTS scanner_bundles (
        id {id_col},
        user_id INTEGER NOT NULL,
        coin TEXT,
        quote TEXT,
        exchange_buy TEXT,
        exchange_sell TEXT,
        spread REAL,
        saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    if 'user_id' in session: return redirect(url_for('miner'))
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        data = request.json or {}
        username, email = data.get('username', '').strip(), data.get('email', '').strip()
        password, confirm = data.get('password', ''), data.get('confirm_password', '')

        if not all([username, email, password, confirm]):
            return jsonify({"status": "error", "message": "Все поля обязательны"}), 400
        if len(username) < 3 or len(password) < 6 or password != confirm:
            return jsonify({"status": "error", "message": "Неверный формат данных"}), 400

        try:
            conn = get_db()
            c = get_cursor(conn)
            c.execute("SELECT id FROM users WHERE username = ? OR email = ?", (username, email))
            if c.fetchone():
                conn.close()
                return jsonify({"status": "error", "message": "Пользователь уже существует"}), 400

            c.execute("INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
                      (username, email, hash_password(password)))
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
        username, password = data.get('username', '').strip(), data.get('password', '')

        try:
            conn = get_db()
            c = get_cursor(conn)
            c.execute("SELECT id, username, password_hash, is_active FROM users WHERE username = ?", (username,))
            user = c.fetchone()
            conn.close()

            if not user or not user['is_active'] or not verify_password(password, user['password_hash']):
                return jsonify({"status": "error", "message": "Неверные данные"}), 401

            session['user_id'], session['username'] = user['id'], user['username']

            conn = get_db()
            c = get_cursor(conn)
            c.execute("UPDATE users SET last_login = ? WHERE id = ?", (now(), user['id']))
            conn.commit()
            conn.close()
            return jsonify({"status": "success"}), 200
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/miner')
def miner():
    if 'user_id' not in session: return redirect(url_for('login'))
    user = get_user_by_id(session['user_id'])
    if not user:
        session.clear()
        return redirect(url_for('login'))
    return render_template('miner_premium.html', user=user)

@app.route('/scanner')
def scanner():
    if 'user_id' not in session: return redirect(url_for('login'))
    user = get_user_by_id(session['user_id'])
    if not user:
        session.clear()
        return redirect(url_for('login'))
    return render_template('crypto_scanner.html', user=user)

# ═══════════════════════════════════════════════════════════════
#  SCANNER API
# ═══════════════════════════════════════════════════════════════
@app.route('/api/scanner/start', methods=['POST'])
@login_required
def scanner_start():
    try:
        conn = get_db()
        c = get_cursor(conn)
        c.execute("INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
                  (session['user_id'], 'NETWORK_SCANNER', 'SESSION_START', 'OK', now()))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error"}), 500

@app.route('/api/scanner/stop', methods=['POST'])
@login_required
def scanner_stop():
    try:
        data = request.json or {}
        minutes, found, saved = int(data.get('minutes', 0)), int(data.get('found', 0)), int(data.get('saved', 0))
        conn = get_db()
        c = get_cursor(conn)
        for _ in range(max(minutes, 1)):
            c.execute("INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
                      (session['user_id'], 'NETWORK_SCANNER', 'SESSION_TICK', 'OK', now()))
        c.execute("INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
                  (session['user_id'], 'NETWORK_SCANNER', f'SESSION_END found={found} saved={saved}', 'OK', now()))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception:
        return jsonify({"status": "error"}), 500

@app.route('/api/scanner/save-bundle', methods=['POST'])
@login_required
def scanner_save_bundle():
    try:
        d = request.json or {}
        conn = get_db()
        c = get_cursor(conn)
        c.execute("INSERT INTO scanner_bundles (user_id, coin, quote, exchange_buy, exchange_sell, spread, saved_at) VALUES (?,?,?,?,?,?,?)",
                  (session['user_id'], d.get('coin'), d.get('quote','USDT'), d.get('ex1'), d.get('ex2'), float(d.get('spread',0)), now()))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception:
        return jsonify({"status": "error"}), 500

@app.route('/api/scanner/bundles')
@login_required
def scanner_get_bundles():
    try:
        conn = get_db()
        c = get_cursor(conn)
        c.execute("SELECT id, coin, quote, exchange_buy, exchange_sell, spread, saved_at FROM scanner_bundles WHERE user_id = ? ORDER BY saved_at DESC LIMIT 200", (session['user_id'],))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({"status": "success", "bundles": rows})
    except Exception:
        return jsonify({"status": "error"}), 500

# ═══════════════════════════════════════════════════════════════
#  USER API
# ═══════════════════════════════════════════════════════════════
@app.route('/api/user/profile')
@login_required
def get_profile():
    u = get_user_by_id(session['user_id'])
    if not u: return jsonify({"status": "error"}), 401
    return jsonify({"id": u['id'], "username": u['username'], "email": u['email'], "balance": u['balance'], "total_earned": u['total_earned']})

@app.route('/api/user/balance')
@login_required
def get_balance():
    u = get_user_by_id(session['user_id'])
    return jsonify({"balance": u['balance'], "total_earned": u['total_earned']})

@app.route('/api/user/deposit', methods=['POST'])
@login_required
def deposit():
    try:
        amount = float((request.json or {}).get('amount', 0))
        if amount <= 0: return jsonify({"status": "error"}), 400
        conn = get_db()
        c = get_cursor(conn)
        c.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, session['user_id']))
        c.execute("INSERT INTO transactions (user_id, type, amount, status, description) VALUES (?,?,?,?,?)",
                  (session['user_id'], 'deposit', amount, 'completed', 'Пополнение'))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception:
        return jsonify({"status": "error"}), 400

@app.route('/api/user/withdraw', methods=['POST'])
@login_required
def withdraw():
    try:
        d = request.json or {}
        amount = float(d.get('amount', 0))
        wallet = d.get('wallet_address', '').strip()
        user = get_user_by_id(session['user_id'])
        if amount <= 0 or user['balance'] < amount: return jsonify({"status": "error"}), 400

        conn = get_db()
        c = get_cursor(conn)
        c.execute("UPDATE users SET balance = balance - ? WHERE id = ?", (amount, session['user_id']))
        c.execute("INSERT INTO transactions (user_id, type, amount, status, description) VALUES (?,?,?,?,?)",
                  (session['user_id'], 'withdraw', amount, 'pending', f'Кошелёк: {wallet}'))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception:
        return jsonify({"status": "error"}), 400

@app.route('/api/user/transactions')
@login_required
def get_transactions():
    try:
        conn = get_db()
        c = get_cursor(conn)
        c.execute("SELECT id, type, amount, status, description, timestamp FROM transactions WHERE user_id = ? ORDER BY timestamp DESC LIMIT 50", (session['user_id'],))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({"status": "success", "transactions": rows})
    except Exception:
        return jsonify({"status": "error"}), 400

@app.route('/api/user/notifications')
@login_required
def get_notifications():
    try:
        conn = get_db()
        c = get_cursor(conn)
        c.execute("SELECT id, title, message, is_read, created_at FROM notifications WHERE user_id = ? AND is_read = FALSE ORDER BY created_at DESC LIMIT 20", (session['user_id'],))
        notifs = [dict(r) for r in c.fetchall()]
        if notifs:
            c.execute("UPDATE notifications SET is_read = TRUE WHERE user_id = ? AND is_read = FALSE", (session['user_id'],))
            conn.commit()
        conn.close()
        return jsonify({"status": "success", "notifications": notifs})
    except Exception:
        return jsonify({"status": "error"}), 400

# ═══════════════════════════════════════════════════════════════
#  PLAN ROUTES
# ═══════════════════════════════════════════════════════════════
@app.route('/api/plan/active')
@login_required
def get_active_plan():
    try:
        conn = get_db()
        c = get_cursor(conn)
        c.execute("SELECT plan_name, plan_price, activated_at FROM active_plans WHERE user_id = ? AND is_active = TRUE ORDER BY activated_at DESC LIMIT 1", (session['user_id'],))
        row = c.fetchone()
        conn.close()
        if row: return jsonify({"status": "success", "has_plan": True, "plan_name": row['plan_name'], "plan_price": row['plan_price']})
        return jsonify({"status": "success", "has_plan": False})
    except Exception:
        return jsonify({"status": "error"}), 500

@app.route('/api/plan/purchase', methods=['POST'])
@login_required
def purchase_plan():
    try:
        d = request.json or {}
        name, price = d.get('plan_name', '').strip(), float(d.get('plan_price', 0))
        user = get_user_by_id(session['user_id'])
        if price <= 0 or user['balance'] < price: return jsonify({"status": "error", "message": "Недостаточно средств"}), 400

        conn = get_db()
        c = get_cursor(conn)
        c.execute("UPDATE active_plans SET is_active = FALSE WHERE user_id = ? AND is_active = TRUE", (session['user_id'],))
        c.execute("UPDATE users SET balance = balance - ? WHERE id = ?", (price, session['user_id']))
        c.execute("INSERT INTO active_plans (user_id, plan_name, plan_price) VALUES (?,?,?)", (session['user_id'], name, price))
        c.execute("INSERT INTO transactions (user_id, type, amount, status, description) VALUES (?,?,?,?,?)", (session['user_id'], 'plan_purchase', price, 'completed', f'План {name}'))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "new_balance": user['balance'] - price})
    except Exception:
        return jsonify({"status": "error"}), 400

@app.route('/api/plan/change', methods=['POST'])
@login_required
def change_plan():
    return purchase_plan()

@app.route('/api/scanner/bundles/clear', methods=['POST'])
@login_required
def scanner_clear_bundles():
    try:
        conn = get_db()
        c = get_cursor(conn)
        c.execute("DELETE FROM scanner_bundles WHERE user_id = ?", (session['user_id'],))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception:
        return jsonify({"status": "error"}), 500

# ═══════════════════════════════════════════════════════════════
#  MINER API  (старт / стоп сессии — для трекинга в админке)
# ═══════════════════════════════════════════════════════════════
@app.route('/api/miner/sync', methods=['POST'])
@login_required
def miner_sync():
    """Периодическое сохранение прироста баланса во время майнинга (каждые ~30 сек)"""
    try:
        d = request.json or {}
        delta = float(d.get('earned', 0))
        plan  = d.get('plan', 'unknown')
        if delta <= 0:
            return jsonify({"status": "ok"})

        conn = get_db()
        c = get_cursor(conn)
        # Обновляем баланс и total_earned в реальном времени
        c.execute(
            "UPDATE users SET balance = balance + ?, total_earned = total_earned + ? WHERE id = ?",
            (delta, delta, session['user_id'])
        )
        conn.commit()

        # Возвращаем актуальный баланс из БД — фронт использует его при восстановлении
        c.execute("SELECT balance FROM users WHERE id = ?", (session['user_id'],))
        row = c.fetchone()
        conn.close()

        return jsonify({"status": "ok", "balance": row['balance'] if row else None})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


    try:
        d = request.json or {}
        plan = d.get('plan', 'unknown')
        conn = get_db()
        c = get_cursor(conn)
        c.execute(
            "INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
            (session['user_id'], 'MINER_NODE', 'SESSION_START', 'OK', now())
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception:
        return jsonify({"status": "error"}), 500

@app.route('/api/miner/stop', methods=['POST'])
@login_required
def miner_stop():
    try:
        d = request.json or {}
        minutes   = max(1, int(float(d.get('minutes', 1))))
        earned    = float(d.get('earned', 0))
        plan      = d.get('plan', 'unknown')
        completed = bool(d.get('completed', False))

        conn = get_db()
        c = get_cursor(conn)

        # Пишем SESSION_TICK за каждую минуту — именно так считает админка
        for _ in range(minutes):
            c.execute(
                "INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
                (session['user_id'], 'MINER_NODE', 'SESSION_TICK', 'OK', now())
            )

        # SESSION_END с деталями
        action_label = f"SESSION_END earned=${earned:.4f} plan={plan}" + (" [COMPLETED]" if completed else "")
        c.execute(
            "INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
            (session['user_id'], 'MINER_NODE', action_label, 'OK', now())
        )

        # Если есть заработок — обновляем баланс и total_earned
        if earned > 0:
            c.execute(
                "UPDATE users SET balance = balance + ?, total_earned = total_earned + ? WHERE id = ?",
                (earned, earned, session['user_id'])
            )
            c.execute(
                "INSERT INTO mining_stats (user_id, total_earnings, duration_seconds, timestamp) VALUES (?,?,?,?)",
                (session['user_id'], earned, minutes * 60, now())
            )
            c.execute(
                "INSERT INTO transactions (user_id, type, amount, status, description) VALUES (?,?,?,?,?)",
                (session['user_id'], 'mining_reward', earned, 'completed', f'Майнинг план {plan}')
            )

        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route('/api/save', methods=['POST'])
@login_required
def save_action():
    try:
        d = request.json or {}
        conn = get_db()
        c = get_cursor(conn)
        c.execute("INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
                  (session['user_id'], d.get('module', ''), d.get('action', ''), 'OK', now()))
        conn.commit()
        conn.close()
        return jsonify({"status": "saved"})
    except Exception:
        return jsonify({"status": "error"}), 400

@app.route('/api/mining/save-earnings', methods=['POST'])
@login_required
def save_mining_earnings():
    try:
        data = request.json or {}
        earned = float(data.get('earned', 0))
        duration = int(data.get('duration_seconds', 0))
        minutes = max(1, duration // 60)

        conn = get_db()
        c = get_cursor(conn)
        
        if earned > 0:
            c.execute("UPDATE users SET total_earned = total_earned + ?, balance = balance + ? WHERE id = ?", (earned, earned, session['user_id']))
            c.execute("INSERT INTO mining_stats (user_id, total_earnings, duration_seconds, timestamp) VALUES (?,?,?,?)", (session['user_id'], earned, duration, now()))

        for _ in range(minutes):
            c.execute("INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
                      (session['user_id'], 'MINER_NODE', 'SESSION_TICK', 'OK', now()))
        
        c.execute("INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
                  (session['user_id'], 'MINER_NODE', f'SESSION_END earned=${earned:.4f}', 'OK', now()))

        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception:
        return jsonify({"status": "error"}), 400

@app.route('/api/payment/usdt-address')
def get_usdt_address():
    return jsonify({"status": "success", "address": USDT_WALLET_ADDRESS})

# ═══════════════════════════════════════════════════════════════
#  ADMIN ROUTES
# ═══════════════════════════════════════════════════════════════
@app.route('/admin')
def admin_panel():
    if not session.get('is_admin'): return redirect(url_for('admin_login'))
    return render_template('admin.html')

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        d = request.json or {}
        if d.get('username') == ADMIN_USERNAME and d.get('password') == ADMIN_PASSWORD:
            session['is_admin'], session['admin_username'] = True, ADMIN_USERNAME
            return jsonify({"status": "success"})
        return jsonify({"status": "error"}), 401
    return render_template('admin_login.html')

@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))

@app.route('/api/admin/stats')
@admin_required
def admin_stats():
    try:
        conn = get_db()
        c = get_cursor(conn)
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

        c.execute("SELECT id, username, email, balance, total_earned, last_login, is_active FROM users ORDER BY created_at DESC LIMIT 5")
        recent_users = [dict(r) for r in c.fetchall()]

        c.execute("SELECT al.module, al.action, al.status, al.timestamp, u.username FROM activity_log al LEFT JOIN users u ON al.user_id = u.id ORDER BY al.timestamp DESC LIMIT 10")
        recent_activity = [dict(r) for r in c.fetchall()]

        conn.close()
        return jsonify({
            "status": "success", "total_users": total_users, "total_balance": total_balance,
            "total_transactions": total_transactions, "active_users": active_users,
            "mining_minutes_today": mining_minutes_today, "recent_users": recent_users,
            "recent_activity": recent_activity
        })
    except Exception:
        return jsonify({"status": "error"}), 500

@app.route('/api/admin/users')
@admin_required
def admin_users():
    try:
        conn = get_db()
        c = get_cursor(conn)
        c.execute("SELECT id, username, email, balance, total_earned, created_at, last_login, is_active FROM users ORDER BY created_at DESC")
        users = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({"status": "success", "users": users})
    except Exception:
        return jsonify({"status": "error"}), 500

@app.route('/api/admin/fund-user', methods=['POST'])
@admin_required
def admin_fund_user():
    try:
        d = request.json or {}
        user_id, amount, comment = int(d.get('user_id')), float(d.get('amount', 0)), d.get('comment', 'Бонус')
        if amount <= 0: return jsonify({"status": "error"}), 400
        
        conn = get_db()
        c = get_cursor(conn)
        c.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, user_id))
        c.execute("INSERT INTO transactions (user_id, type, amount, status, description) VALUES (?,?,?,?,?)",
                  (user_id, 'admin_credit', amount, 'completed', comment))
        c.execute("INSERT INTO notifications (user_id, title, message) VALUES (?,?,?)",
                  (user_id, '💰 Баланс пополнен', f'Начислено ${amount:.2f}. {comment}'))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception:
        return jsonify({"status": "error"}), 500

@app.route('/api/admin/confirm-payment', methods=['POST'])
@admin_required
def admin_confirm_payment():
    return admin_fund_user()

@app.route('/api/admin/toggle-user', methods=['POST'])
@admin_required
def admin_toggle_user():
    try:
        d = request.json or {}
        user_id, active = int(d.get('user_id')), bool(d.get('active', True))
        conn = get_db()
        c = get_cursor(conn)
        c.execute("UPDATE users SET is_active = ? WHERE id = ?", (True if active else False, user_id))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception:
        return jsonify({"status": "error"}), 500

@app.route('/api/admin/activity')
@admin_required
def admin_activity():
    try:
        conn = get_db()
        c = get_cursor(conn)
        today = datetime.datetime.now().strftime('%Y-%m-%d')
        c.execute("SELECT al.id, al.module, al.action, al.status, al.timestamp, u.username FROM activity_log al LEFT JOIN users u ON al.user_id = u.id ORDER BY al.timestamp DESC LIMIT 200")
        activity = [dict(r) for r in c.fetchall()]

        c.execute("""
            SELECT u.username,
                SUM(CASE WHEN al.module='MINER_NODE' THEN 1 ELSE 0 END) AS miner_minutes,
                SUM(CASE WHEN al.module='NETWORK_SCANNER' THEN 1 ELSE 0 END) AS scanner_minutes,
                COUNT(*) AS total_events, MAX(al.timestamp) AS last_activity
            FROM activity_log al
            LEFT JOIN users u ON al.user_id = u.id
            WHERE al.timestamp >= ?
            GROUP BY al.user_id, u.username
            ORDER BY total_events DESC
        """, (today,))
        sessions = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({"status": "success", "activity": activity, "sessions": sessions})
    except Exception:
        return jsonify({"status": "error"}), 500

@app.route('/api/admin/transactions')
@admin_required
def admin_transactions():
    try:
        conn = get_db()
        c = get_cursor(conn)
        c.execute("SELECT t.id, t.type, t.amount, t.status, t.description, t.timestamp, u.username FROM transactions t LEFT JOIN users u ON t.user_id = u.id ORDER BY t.timestamp DESC LIMIT 100")
        txs = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({"status": "success", "transactions": txs})
    except Exception:
        return jsonify({"status": "error"}), 500

if __name__ == '__main__':
    app.run(debug=DEBUG, port=PORT, host='0.0.0.0')
