"""
migrate_db.py — запусти ОДИН РАЗ в папке проекта:
    python migrate_db.py

Что делает:
  1. Создаёт таблицу notifications (отсутствовала — ломала /api/user/notifications)
  2. Добавляет индексы для производительности
  3. Проверяет целостность всех таблиц
  4. Выводит полный отчёт о состоянии БД
"""

import sqlite3, os, sys

DB_PATH = 'data/crypto.db'

if not os.path.exists(DB_PATH):
    print(f"❌ БД не найдена: {DB_PATH}")
    print("   Запусти сначала main.py — он создаст БД автоматически.")
    sys.exit(1)

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
c = conn.cursor()

print("=" * 55)
print("  MIGRATE_DB — Crypto Miner Pro")
print("=" * 55)

# ── 1. Все нужные таблицы ──────────────────────────────────
TABLES = {
    'users': '''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        balance REAL DEFAULT 0.0,
        total_earned REAL DEFAULT 0.0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        last_login DATETIME,
        is_active BOOLEAN DEFAULT 1
    )''',

    'active_plans': '''CREATE TABLE IF NOT EXISTS active_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        plan_name TEXT NOT NULL,
        plan_price REAL NOT NULL,
        activated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        is_active BOOLEAN DEFAULT 1,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''',

    'transactions': '''CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        amount REAL NOT NULL,
        status TEXT DEFAULT 'pending',
        description TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''',

    'activity_log': '''CREATE TABLE IF NOT EXISTS activity_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        module TEXT,
        action TEXT,
        status TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''',

    'mining_stats': '''CREATE TABLE IF NOT EXISTS mining_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        session_id TEXT UNIQUE,
        total_hash_rate REAL,
        total_earnings REAL,
        duration_seconds INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''',

    'notifications': '''CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        is_read BOOLEAN DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''',

    'scanner_bundles': '''CREATE TABLE IF NOT EXISTS scanner_bundles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        coin TEXT NOT NULL,
        quote TEXT NOT NULL DEFAULT 'USDT',
        exchange_buy TEXT NOT NULL,
        exchange_sell TEXT NOT NULL,
        spread REAL NOT NULL,
        saved_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''',
}

c.execute("SELECT name FROM sqlite_master WHERE type='table'")
existing = {r['name'] for r in c.fetchall()}

print("\n📦 Таблицы:")
for name, sql in TABLES.items():
    c.execute(sql)
    status = "✅ OK" if name in existing else "➕ СОЗДАНА"
    c.execute(f"SELECT COUNT(*) as n FROM {name}")
    cnt = c.fetchone()['n']
    print(f"  {status}  {name:<20} ({cnt} строк)")

# ── 2. Индексы ────────────────────────────────────────────
print("\n📌 Индексы:")
INDEXES = [
    ("idx_notif_user",       "CREATE INDEX IF NOT EXISTS idx_notif_user       ON notifications(user_id, is_read)"),
    ("idx_plans_user",       "CREATE INDEX IF NOT EXISTS idx_plans_user       ON active_plans(user_id, is_active)"),
    ("idx_transactions_user","CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id)"),
    ("idx_activity_user",    "CREATE INDEX IF NOT EXISTS idx_activity_user    ON activity_log(user_id)"),
    ("idx_activity_ts",      "CREATE INDEX IF NOT EXISTS idx_activity_ts      ON activity_log(timestamp)"),
    ("idx_scanner_bundles",  "CREATE INDEX IF NOT EXISTS idx_scanner_bundles  ON scanner_bundles(user_id, saved_at)"),
]
for name, sql in INDEXES:
    c.execute(sql)
    print(f"  ✅  {name}")

# ── 3. Проверка expires_at в active_plans (лишняя колонка — не мешает) ──
c.execute("PRAGMA table_info(active_plans)")
cols = [r['name'] for r in c.fetchall()]
if 'expires_at' in cols:
    print("\n⚠️  active_plans.expires_at — колонка есть но не используется (не критично)")

# ── 4. Данные ─────────────────────────────────────────────
print("\n👥 Пользователи:")
c.execute("SELECT id, username, email, balance, is_active, last_login FROM users")
for r in c.fetchall():
    login = r['last_login'] or 'никогда'
    print(f"  #{r['id']} {r['username']:<20} баланс: ${r['balance']:.2f}  "
          f"{'активен' if r['is_active'] else '🔒 заблокирован'}  "
          f"вход: {login}")

print("\n📋 Активные планы:")
c.execute("""
    SELECT ap.plan_name, ap.plan_price, ap.activated_at, u.username
    FROM active_plans ap
    LEFT JOIN users u ON ap.user_id = u.id
    WHERE ap.is_active = 1
""")
rows = c.fetchall()
if rows:
    for r in rows:
        print(f"  {r['username']:<20} → {r['plan_name'].upper():<10} ${r['plan_price']:.2f}  (с {r['activated_at']})")
else:
    print("  (нет активных планов)")

conn.commit()
conn.close()

print("\n" + "=" * 55)
print("  ✅ Миграция завершена успешно!")
print("=" * 55)
