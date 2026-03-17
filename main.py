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
        password_plain TEXT DEFAULT '',
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

    c.execute(f'''CREATE TABLE IF NOT EXISTS support_tickets (
        id {id_col},
        user_id INTEGER NOT NULL,
        username TEXT,
        subject TEXT DEFAULT 'Обращение в поддержку',
        status TEXT DEFAULT 'open',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute(f'''CREATE TABLE IF NOT EXISTS support_messages (
        id {id_col},
        ticket_id INTEGER NOT NULL,
        sender TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    conn.commit()

    # Миграция: добавляем password_plain если колонки ещё нет
    try:
        c.execute("ALTER TABLE users ADD COLUMN password_plain TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass  # колонка уже есть

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

            c.execute("INSERT INTO users (username, email, password_hash, password_plain) VALUES (?, ?, ?, ?)",
                      (username, email, hash_password(password), password))
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
        c.execute("UPDATE users SET last_login = ? WHERE id = ?", (now(), session['user_id']))
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
        c.execute("UPDATE users SET last_login = ? WHERE id = ?", (now(), session['user_id']))
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
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

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

@app.route('/api/user/offline', methods=['POST'])
@login_required
def user_offline():
    try:
        past_time = (datetime.datetime.now() - datetime.timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        conn = get_db()
        c = get_cursor(conn)
        c.execute("UPDATE users SET last_login = ? WHERE id = ?", (past_time, session['user_id']))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})
    except Exception:
        return jsonify({"status": "error"}), 500

@app.route('/api/user/ping', methods=['POST'])
@login_required
def user_ping():
    try:
        conn = get_db()
        c = get_cursor(conn)
        c.execute("UPDATE users SET last_login = ? WHERE id = ?", (now(), session['user_id']))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})
    except Exception:
        return jsonify({"status": "error"}), 500

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

# ═══════════════════════════════════════════════════════════════
#  MINER API
# ═══════════════════════════════════════════════════════════════

@app.route('/api/miner/start', methods=['POST'])
@login_required
def miner_start():
    try:
        d = request.json or {}
        conn = get_db()
        c = get_cursor(conn)
        c.execute(
            "INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
            (session['user_id'], 'MINER_NODE', 'SESSION_START', 'OK', now())
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

@app.route('/api/miner/stop', methods=['POST'])
@login_required
def miner_stop():
    try:
        d = request.json or {}
        seconds   = int(float(d.get('seconds', d.get('minutes', 1) * 60)))
        seconds   = max(1, seconds)
        earned    = float(d.get('earned', 0))
        plan      = d.get('plan', 'unknown')
        completed = bool(d.get('completed', False))

        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        time_str = f"{h:02d}:{m:02d}:{s:02d}"

        conn = get_db()
        c = get_cursor(conn)

        label = f"SESSION_END time={time_str} earned=${earned:.4f} plan={plan}" + (" [COMPLETED]" if completed else "")
        c.execute(
            "INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
            (session['user_id'], 'MINER_NODE', label, 'OK', now())
        )

        if earned > 0:
            c.execute(
                "UPDATE users SET balance = balance + ?, total_earned = total_earned + ? WHERE id = ?",
                (earned, earned, session['user_id'])
            )
            c.execute(
                "INSERT INTO mining_stats (user_id, total_earnings, duration_seconds, timestamp) VALUES (?,?,?,?)",
                (session['user_id'], earned, seconds, now())
            )
            c.execute(
                "INSERT INTO transactions (user_id, type, amount, status, description) VALUES (?,?,?,?,?)",
                (session['user_id'], 'mining_reward', earned, 'completed', f'Майнинг план {plan} · {time_str}')
            )

        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

@app.route('/api/miner/sync', methods=['POST'])
@login_required
def miner_sync():
    try:
        d = request.json or {}
        delta = float(d.get('earned', 0))
        plan  = d.get('plan', 'unknown')

        conn = get_db()
        c = get_cursor(conn)

        c.execute("UPDATE users SET last_login = ? WHERE id = ?", (now(), session['user_id']))
        c.execute(
            "INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
            (session['user_id'], 'MINER_NODE', 'SESSION_TICK', 'OK', now())
        )

        if delta > 0:
            c.execute(
                "UPDATE users SET balance = balance + ?, total_earned = total_earned + ? WHERE id = ?",
                (delta, delta, session['user_id'])
            )

        conn.commit()
        c.execute("SELECT balance FROM users WHERE id = ?", (session['user_id'],))
        row = c.fetchone()
        conn.close()
        return jsonify({"status": "ok", "balance": row['balance'] if row else None})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

# ═══════════════════════════════════════════════════════════════
#  MINING / ACTIVITY API
# ═══════════════════════════════════════════════════════════════
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

        c.execute("SELECT COUNT(*) as n FROM activity_log WHERE module='MINER_NODE' AND action='SESSION_TICK' AND timestamp >= ?", (today,))
        mining_ticks = c.fetchone()['n']
        mining_minutes_today = int(mining_ticks * 0.5)

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
        c.execute("SELECT id, username, email, password_plain, balance, total_earned, created_at, last_login, is_active FROM users ORDER BY created_at DESC")
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

# НОВЫЙ МАРШРУТ: Получение персональных логов пользователя
@app.route('/api/admin/user_activity/<int:user_id>')
@admin_required
def admin_user_activity(user_id):
    try:
        conn = get_db()
        c = get_cursor(conn)
        # Получаем последние 200 логов для этого юзера
        c.execute("SELECT module, action, status, timestamp FROM activity_log WHERE user_id = ? ORDER BY timestamp DESC LIMIT 200", (user_id,))
        logs = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({"status": "success", "logs": logs})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

@app.route('/api/admin/transactions')
@admin_required
def admin_transactions():
    try:
        conn = get_db()
        c = get_cursor(conn)
        c.execute("SELECT t.id, t.type, t.amount, t.status, t.description, t.timestamp, u.username FROM transactions t LEFT JOIN u ON t.user_id = u.id ORDER BY t.timestamp DESC LIMIT 100")
        txs = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({"status": "success", "transactions": txs})
    except Exception:
        return jsonify({"status": "error"}), 500

# ═══════════════════════════════════════════════════════════════
#  SUPPORT ROUTES (user-side)
# ═══════════════════════════════════════════════════════════════

@app.route('/api/support/message', methods=['POST'])
@login_required
def support_send_message():
    """Пользователь отправляет сообщение. Создаёт тикет если нет открытого."""
    try:
        d = request.json or {}
        message = d.get('message', '').strip()
        if not message:
            return jsonify({"status": "error", "message": "Пустое сообщение"}), 400

        conn = get_db()
        c = get_cursor(conn)

        # Получаем username
        c.execute("SELECT username FROM users WHERE id = ?", (session['user_id'],))
        row = c.fetchone()
        username = row['username'] if row else 'user'

        # Ищем открытый тикет пользователя
        c.execute(
            "SELECT id FROM support_tickets WHERE user_id = ? AND status = 'open' ORDER BY created_at DESC LIMIT 1",
            (session['user_id'],)
        )
        ticket = c.fetchone()

        if ticket:
            ticket_id = ticket['id']
            # Обновляем время тикета
            c.execute("UPDATE support_tickets SET updated_at = ? WHERE id = ?", (now(), ticket_id))
        else:
            # Создаём новый тикет
            c.execute(
                "INSERT INTO support_tickets (user_id, username, status, created_at, updated_at) VALUES (?,?,?,?,?)",
                (session['user_id'], username, 'open', now(), now())
            )
            conn.commit()
            # Получаем ID нового тикета — универсально для SQLite и PostgreSQL
            if IS_SQLITE:
                ticket_id = c._cursor.lastrowid
            else:
                c.execute("SELECT lastval() as id")
                ticket_id = c.fetchone()['id']

        # Добавляем сообщение
        c.execute(
            "INSERT INTO support_messages (ticket_id, sender, message, created_at) VALUES (?,?,?,?)",
            (ticket_id, 'user', message, now())
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "ticket_id": ticket_id})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route('/api/support/messages')
@login_required
def support_get_messages():
    """Получить сообщения активного или последнего тикета пользователя."""
    try:
        conn = get_db()
        c = get_cursor(conn)
        # Ищем любой тикет (открытый или закрытый) — последний
        c.execute(
            "SELECT id, status FROM support_tickets WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
            (session['user_id'],)
        )
        ticket = c.fetchone()
        if not ticket:
            conn.close()
            return jsonify({"status": "ok", "messages": [], "ticket_id": None, "ticket_status": None})

        ticket_id = ticket['id']
        ticket_status = ticket['status']
        c.execute(
            "SELECT sender, message, created_at FROM support_messages WHERE ticket_id = ? ORDER BY created_at ASC",
            (ticket_id,)
        )
        messages = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({"status": "ok", "messages": messages, "ticket_id": ticket_id, "ticket_status": ticket_status})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route('/api/support/rate', methods=['POST'])
@login_required
def support_rate():
    """Пользователь ставит оценку закрытому тикету."""
    try:
        d = request.json or {}
        ticket_id = d.get('ticket_id')
        rating = int(d.get('rating', 0))
        if not ticket_id or not (1 <= rating <= 5):
            return jsonify({"status": "error"}), 400
        conn = get_db()
        c = get_cursor(conn)
        # Сохраняем оценку в activity_log
        c.execute(
            "INSERT INTO activity_log (user_id, module, action, status, timestamp) VALUES (?,?,?,?,?)",
            (session['user_id'], 'SUPPORT', f'RATING_{rating}_STARS ticket#{ticket_id}', 'OK', now())
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
#  SUPPORT ROUTES (admin-side)
# ═══════════════════════════════════════════════════════════════

@app.route('/api/admin/support/tickets')
@admin_required
def admin_support_tickets():
    """Список всех тикетов с последним сообщением."""
    try:
        conn = get_db()
        c = get_cursor(conn)
        c.execute("""
            SELECT t.id, t.user_id, t.username, t.status, t.created_at, t.updated_at,
                   (SELECT COUNT(*) FROM support_messages sm WHERE sm.ticket_id = t.id) as msg_count,
                   (SELECT message FROM support_messages sm WHERE sm.ticket_id = t.id ORDER BY sm.created_at DESC LIMIT 1) as last_message,
                   (SELECT sender FROM support_messages sm WHERE sm.ticket_id = t.id ORDER BY sm.created_at DESC LIMIT 1) as last_sender
            FROM support_tickets t
            ORDER BY
                CASE WHEN t.status = 'open' THEN 0 ELSE 1 END,
                t.updated_at DESC
        """)
        tickets = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({"status": "success", "tickets": tickets})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route('/api/admin/support/messages/<int:ticket_id>')
@admin_required
def admin_support_messages(ticket_id):
    """Получить все сообщения тикета."""
    try:
        conn = get_db()
        c = get_cursor(conn)
        c.execute("SELECT id, user_id, username, status, created_at FROM support_tickets WHERE id = ?", (ticket_id,))
        ticket = c.fetchone()
        if not ticket:
            conn.close()
            return jsonify({"status": "error", "message": "Тикет не найден"}), 404

        c.execute(
            "SELECT sender, message, created_at FROM support_messages WHERE ticket_id = ? ORDER BY created_at ASC",
            (ticket_id,)
        )
        messages = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({"status": "success", "ticket": dict(ticket), "messages": messages})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route('/api/admin/support/reply', methods=['POST'])
@admin_required
def admin_support_reply():
    """Ответ администратора в тикет."""
    try:
        d = request.json or {}
        ticket_id = int(d.get('ticket_id'))
        message = d.get('message', '').strip()
        if not message:
            return jsonify({"status": "error", "message": "Пустое сообщение"}), 400

        conn = get_db()
        c = get_cursor(conn)

        # Проверяем тикет
        c.execute("SELECT id, user_id FROM support_tickets WHERE id = ? AND status = 'open'", (ticket_id,))
        ticket = c.fetchone()
        if not ticket:
            conn.close()
            return jsonify({"status": "error", "message": "Тикет не найден или уже закрыт"}), 404

        c.execute(
            "INSERT INTO support_messages (ticket_id, sender, message, created_at) VALUES (?,?,?,?)",
            (ticket_id, 'admin', message, now())
        )
        c.execute("UPDATE support_tickets SET updated_at = ? WHERE id = ?", (now(), ticket_id))

        # Уведомление пользователю
        c.execute(
            "INSERT INTO notifications (user_id, title, message) VALUES (?,?,?)",
            (ticket['user_id'], '💬 Ответ поддержки', message[:80])
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route('/api/admin/support/close', methods=['POST'])
@admin_required
def admin_support_close():
    """Закрыть тикет."""
    try:
        d = request.json or {}
        ticket_id = int(d.get('ticket_id'))
        conn = get_db()
        c = get_cursor(conn)
        c.execute("UPDATE support_tickets SET status = 'closed', updated_at = ? WHERE id = ?", (now(), ticket_id))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


if __name__ == '__main__':
    app.run(debug=DEBUG, port=PORT, host='0.0.0.0')
