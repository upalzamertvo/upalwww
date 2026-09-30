import asyncio
import sqlite3
import logging
import json
from datetime import datetime, timedelta, date
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO)

TOKEN = "8637561638:AAFVtPws0Q20tfo-p6Lu-5yDvDpPIqZRJuM"
ADMIN_ID = 6827569074

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

MIN_PHOTO_INTERVAL_SEC = 5
AUTO_REPORT_HOUR = 21

album_buffer = {}

# ====================== БАЗА ДАННЫХ ======================

conn = sqlite3.connect("bot.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'not_started',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    done_at TEXT,
    reviewed_at TEXT,
    last_photo_at TEXT,
    task_version INTEGER DEFAULT 1,
    referred_by INTEGER,
    ref_bonus_sent INTEGER DEFAULT 0,
    is_moderator INTEGER DEFAULT 0
)""")

cursor.execute("""CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    file_id TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
)""")

cursor.execute("""CREATE TABLE IF NOT EXISTS admin_sessions (
    admin_id INTEGER,
    user_id INTEGER,
    current_index INTEGER DEFAULT 0,
    PRIMARY KEY (admin_id, user_id)
)""")

cursor.execute("""CREATE TABLE IF NOT EXISTS broadcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT,
    sent_count INTEGER DEFAULT 0,
    failed_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
)""")

cursor.execute("""CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
)""")

cursor.execute("""CREATE TABLE IF NOT EXISTS task_versions (
    version INTEGER PRIMARY KEY,
    target_count INTEGER,
    prize_text TEXT,
    comment_template TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
)""")

cursor.execute("""CREATE TABLE IF NOT EXISTS auto_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at TEXT DEFAULT (datetime('now', 'localtime')),
    report_text TEXT
)""")

def migrate_database():
    cursor.execute("PRAGMA table_info(users)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    for col, col_type, default in [
        ("last_photo_at", "TEXT", None),
        ("task_version", "INTEGER", "1"),
        ("done_at", "TEXT", None),
        ("reviewed_at", "TEXT", None),
        ("created_at", "TEXT", None),
        ("referred_by", "INTEGER", None),
        ("ref_bonus_sent", "INTEGER", "0"),
        ("is_moderator", "INTEGER", "0"),
    ]:
        if col not in existing_cols:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type}")
            if default is not None:
                cursor.execute(f"UPDATE users SET {col} = {default}")
            elif col == "created_at":
                cursor.execute("UPDATE users SET created_at = datetime('now', 'localtime')")
    cursor.execute("PRAGMA table_info(photos)")
    photo_cols = {row[1] for row in cursor.fetchall()}
    if "created_at" not in photo_cols:
        cursor.execute("ALTER TABLE photos ADD COLUMN created_at TEXT")
        cursor.execute("UPDATE photos SET created_at = datetime('now', 'localtime')")
    conn.commit()

migrate_database()

cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ("current_task_version", "1"))
cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ("auto_report_hour", str(AUTO_REPORT_HOUR)))
cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ("auto_report_enabled", "1"))
cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ("required_channels", "[]"))
cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ("referral_bonus", "1"))
cursor.execute("INSERT OR IGNORE INTO task_versions (version, target_count, prize_text, comment_template) VALUES (1, 50, '🎉 Поздравляем! Ты прошёл проверку и получаешь своего мишку! 🧸', '')")
conn.commit()


# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================

def parse_db_datetime(dt_str):
    if not dt_str:
        return datetime.min
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(dt_str, fmt)
        except ValueError:
            continue
    return datetime.min

def get_setting(key, default=""):
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    return row[0] if row else default

def set_setting(key, value):
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()

def get_required_channels():
    try:
        return json.loads(get_setting("required_channels", "[]"))
    except Exception:
        return []

def set_required_channels(channels):
    set_setting("required_channels", json.dumps(channels))

def get_current_task_version():
    return int(get_setting("current_task_version", "1"))

def get_task_info(version):
    cursor.execute("SELECT target_count, prize_text, comment_template FROM task_versions WHERE version = ?", (version,))
    row = cursor.fetchone()
    if not row:
        return {"target_count": 50, "prize_text": "", "comment_template": ""}
    return {"target_count": row[0], "prize_text": row[1], "comment_template": row[2]}

def get_current_task_info():
    return get_task_info(get_current_task_version())

def get_user_task_version(user_id):
    cursor.execute("SELECT task_version FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else 1

def create_new_task_version(target_count, prize_text, comment_template):
    new_ver = get_current_task_version() + 1
    cursor.execute("INSERT INTO task_versions (version, target_count, prize_text, comment_template) VALUES (?, ?, ?, ?)",
                   (new_ver, target_count, prize_text, comment_template))
    set_setting("current_task_version", str(new_ver))
    return new_ver

def apply_new_task_version(new_ver, new_target):
    cursor.execute("SELECT user_id, count, task_version FROM users WHERE status IN ('not_started', 'in_progress')")
    for user_id, count, task_ver in cursor.fetchall():
        old_target = get_task_info(task_ver)["target_count"]
        if old_target > 0 and count > old_target * 0.5:
            continue
        cursor.execute("DELETE FROM photos WHERE user_id = ?", (user_id,))
        cursor.execute("UPDATE users SET count = 0, status = 'not_started', task_version = ?, last_photo_at = NULL, done_at = NULL, reviewed_at = NULL, ref_bonus_sent = 0 WHERE user_id = ?",
                       (new_ver, user_id))
    conn.commit()

def make_bar(current, total, length=10):
    if total <= 0:
        return f"[{'░' * length}] {current}/{total}"
    filled = min(int((current / total) * length), length)
    return f"[{'█' * filled}{'░' * (length - filled)}] {current}/{total}"

async def check_user_subscriptions(user_id):
    channels = get_required_channels()
    if not channels:
        return []
    unsub = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch, user_id)
            if member.status in ("left", "kicked"):
                unsub.append(ch)
        except Exception as e:
            logging.error(f"check_sub {ch}: {e}")
            unsub.append(ch)
    return unsub

def subscription_keyboard(unsub):
    builder = InlineKeyboardBuilder()
    for ch in unsub:
        d = ch if ch.startswith("@") else f"@{ch}"
        builder.button(text=f"📋 Подписаться: {d}", url=f"https://t.me/{ch.lstrip('@')}")
    builder.button(text="✅ Я подписался", callback_data="check_sub")
    builder.adjust(1)
    return builder.as_markup()

def format_subscription_message(unsub):
    ch_text = "\n".join(f"• {ch if ch.startswith('@') else '@'+ch}" for ch in unsub)
    return f"🔒 Подпишись на канал(ы):\n\n{ch_text}\n\nПосле подписки нажми кнопку ниже 👇"


# ====================== РЕФЕРАЛЬНАЯ СИСТЕМА ======================

def get_active_referrals_count(user_id):
    cursor.execute("SELECT COUNT(*) FROM users WHERE referred_by = ? AND count >= 3", (user_id,))
    return cursor.fetchone()[0]

def get_referral_bonus_count(user_id):
    return min(20, get_active_referrals_count(user_id))

def get_user_target(user_id):
    return get_task_info(get_user_task_version(user_id))["target_count"]

def get_effective_target(user_id):
    return max(1, get_user_target(user_id) - get_referral_bonus_count(user_id))

async def try_notify_referrer(user_id):
    cursor.execute("SELECT referred_by, ref_bonus_sent FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row or not row[0] or row[1]:
        return
    referrer_id = row[0]
    old_bonus = get_referral_bonus_count(referrer_id)
    new_bonus = min(20, old_bonus + 1) if old_bonus < 20 else 20
    if new_bonus > old_bonus:
        ref_target = get_effective_target(referrer_id)
        try:
            await bot.send_message(
                referrer_id,
                f"🎉 Твой друг стал активным!\n🎁 Бонус: -1 скриншот\n📊 Новая цель: {ref_target}\n\nПриглашай больше друзей — до 20 бонусов!"
            )
        except Exception:
            pass
    cursor.execute("UPDATE users SET ref_bonus_sent = 1 WHERE user_id = ?", (user_id,))
    conn.commit()


# ====================== МОДЕРАТОРЫ ======================

def is_moderator(user_id):
    cursor.execute("SELECT is_moderator FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    return bool(row and row[0] == 1)

def is_admin_or_moderator(user_id):
    return user_id == ADMIN_ID or is_moderator(user_id)

def add_moderator(user_id):
    cursor.execute("UPDATE users SET is_moderator = 1 WHERE user_id = ?", (user_id,))
    conn.commit()

def remove_moderator(user_id):
    cursor.execute("UPDATE users SET is_moderator = 0 WHERE user_id = ?", (user_id,))
    conn.commit()

def get_moderators():
    cursor.execute("SELECT user_id, username FROM users WHERE is_moderator = 1")
    return cursor.fetchall()


# ====================== ФУНКЦИИ БД ======================

def add_user(user_id, username, referred_by=None):
    ver = get_current_task_version()
    if referred_by:
        cursor.execute("SELECT username FROM users WHERE user_id = ?", (referred_by,))
        row = cursor.fetchone()
        if row and row[0] and username and row[0].lstrip("@").lower() == username.lstrip("@").lower():
            referred_by = None
        cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (referred_by,))
        if not cursor.fetchone():
            referred_by = None
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username, status, task_version, referred_by) VALUES (?, ?, 'not_started', ?, ?)",
                   (user_id, username, ver, referred_by))
    conn.commit()

def can_send_photo(user_id):
    cursor.execute("SELECT last_photo_at FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row or not row[0]:
        return True
    last = parse_db_datetime(row[0])
    return last == datetime.min or (datetime.now() - last).total_seconds() >= MIN_PHOTO_INTERVAL_SEC

def add_photo(user_id, file_id):
    cursor.execute("SELECT 1 FROM photos WHERE user_id = ? AND file_id = ?", (user_id, file_id))
    if cursor.fetchone():
        return False
    cursor.execute("INSERT INTO photos (user_id, file_id) VALUES (?, ?)", (user_id, file_id))
    cursor.execute("UPDATE users SET count = count + 1, status = 'in_progress', last_photo_at = datetime('now', 'localtime') WHERE user_id = ?", (user_id,))
    conn.commit()
    return True

def delete_photo_by_index(user_id, index):
    cursor.execute("SELECT id FROM photos WHERE user_id = ? ORDER BY id LIMIT 1 OFFSET ?", (user_id, index))
    row = cursor.fetchone()
    if not row:
        return False
    cursor.execute("DELETE FROM photos WHERE id = ?", (row[0],))
    cursor.execute("UPDATE users SET count = CASE WHEN count > 0 THEN count - 1 ELSE 0 END WHERE user_id = ?", (user_id,))
    conn.commit()
    return True

def reset_user_progress(user_id):
    ver = get_current_task_version()
    cursor.execute("DELETE FROM photos WHERE user_id = ?", (user_id,))
    cursor.execute("UPDATE users SET count = 0, status = 'not_started', last_photo_at = NULL, done_at = NULL, reviewed_at = NULL, ref_bonus_sent = 0, task_version = ? WHERE user_id = ?",
                   (ver, user_id))
    conn.commit()

def get_user_count(user_id):
    cursor.execute("SELECT count FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else 0

def get_user_photos(user_id):
    cursor.execute("SELECT file_id FROM photos WHERE user_id = ? ORDER BY id", (user_id,))
    return [row[0] for row in cursor.fetchall()]

def get_user_username(user_id):
    cursor.execute("SELECT username FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else None

def get_all_users():
    cursor.execute("SELECT user_id, username, count, status FROM users ORDER BY count DESC")
    return cursor.fetchall()

def get_users_by_status(status):
    cursor.execute("SELECT user_id, username, count, status FROM users WHERE status = ? ORDER BY count DESC", (status,))
    return cursor.fetchall()

def set_user_status(user_id, status):
    if status == "done":
        cursor.execute("UPDATE users SET status = ?, done_at = datetime('now', 'localtime') WHERE user_id = ?", (status, user_id))
    elif status in ("approved", "rejected"):
        cursor.execute("UPDATE users SET status = ?, reviewed_at = datetime('now', 'localtime') WHERE user_id = ?", (status, user_id))
    else:
        cursor.execute("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))
    conn.commit()

def save_admin_session(admin_id, user_id, index):
    cursor.execute("INSERT OR REPLACE INTO admin_sessions (admin_id, user_id, current_index) VALUES (?, ?, ?)",
                   (admin_id, user_id, index))
    conn.commit()

def get_admin_session(admin_id):
    cursor.execute("SELECT user_id, current_index FROM admin_sessions WHERE admin_id = ?", (admin_id,))
    return cursor.fetchone()

def get_all_user_ids():
    cursor.execute("SELECT user_id FROM users")
    return [row[0] for row in cursor.fetchall()]

def get_inactive_users(days):
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("SELECT user_id, username, count FROM users WHERE status IN ('in_progress', 'not_started') AND (last_photo_at IS NULL OR last_photo_at < ?)", (cutoff,))
    return cursor.fetchall()


# ====================== СТАТИСТИКА ======================

def get_general_stats():
    s = {}
    cursor.execute("SELECT COUNT(*) FROM users")
    s["total"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE status = 'done'")
    s["done"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE status = 'approved'")
    s["approved"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE status = 'rejected'")
    s["rejected"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE status = 'in_progress'")
    s["in_progress"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE status = 'not_started'")
    s["not_started"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM photos")
    s["total_photos"] = cursor.fetchone()[0]
    return s

def get_week_start(d):
    return d - timedelta(days=d.weekday())

def get_stats_for_period(start, end):
    ss = start.strftime("%Y-%m-%d") + " 00:00:00"
    se = end.strftime("%Y-%m-%d") + " 23:59:59"
    s = {}
    cursor.execute("SELECT COUNT(*) FROM users WHERE created_at >= ? AND created_at <= ?", (ss, se))
    s["new_users"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM photos WHERE created_at >= ? AND created_at <= ?", (ss, se))
    s["photos"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE done_at IS NOT NULL AND done_at >= ? AND done_at <= ?", (ss, se))
    s["done"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE reviewed_at IS NOT NULL AND status = 'approved' AND reviewed_at >= ? AND reviewed_at <= ?", (ss, se))
    s["approved"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE reviewed_at IS NOT NULL AND status = 'rejected' AND reviewed_at >= ? AND reviewed_at <= ?", (ss, se))
    s["rejected"] = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(DISTINCT user_id) FROM photos WHERE created_at >= ? AND created_at <= ?", (ss, se))
    s["active_users"] = cursor.fetchone()[0]
    return s

def get_daily_report_text():
    today = get_stats_for_period(date.today(), date.today())
    gen = get_general_stats()
    task = get_current_task_info()
    ch = get_required_channels()
    cursor.execute("SELECT COUNT(*) FROM users WHERE referred_by IS NOT NULL")
    tr = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE referred_by IS NOT NULL AND count >= 3")
    ar = cursor.fetchone()[0]
    return (
        f"📅 Авто-отчёт — {date.today().strftime('%d.%m.%Y')}\n\n"
        f"📊 За сегодня:\nНовых: {today['new_users']} | Активных: {today['active_users']} | Фото: {today['photos']}\n"
        f"Завершили: {today['done']} | Одобрено: {today['approved']} | Отклонено: {today['rejected']}\n\n"
        f"📊 Всего:\nПользователей: {gen['total']} | В процессе: {gen['in_progress']} | На проверке: {gen['done']}\n"
        f"Одобрено: {gen['approved']} | Отклонено: {gen['rejected']} | Не начали: {gen['not_started']}\n\n"
        f"👥 Рефералы: {tr} всего, {ar} активных\n\n"
        f"Задание v{get_current_task_version()} | Цель: {task['target_count']} | Каналов: {len(ch)}"
    )


# ====================== КЛАВИАТУРЫ ======================

def main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📝 Задание", callback_data="main_task")
    builder.button(text="📊 Прогресс", callback_data="main_progress")
    builder.button(text="👥 Друзья", callback_data="main_referral")
    builder.button(text="❓ Помощь", callback_data="main_help")
    builder.button(text="🌐 VPN", url="https://t.me/WsquadVPN_bot")
    builder.button(text="⭐ Звёзды", url="https://t.me/wsquadstars_bot")
    builder.adjust(2, 2, 2)
    return builder.as_markup()

def stats_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Статистика", callback_data="time_stats")
    builder.button(text="📋 Участники", callback_data="user_list")
    builder.button(text="📨 Рассылка", callback_data="broadcast_start")
    builder.button(text="🔔 Напомнить", callback_data="remind_inactive")
    builder.button(text="⚙️ Настройки", callback_data="admin_settings")
    builder.button(text="👥 Модераторы", callback_data="mod_list")
    builder.button(text="🔄 Обновить", callback_data="admin_refresh")
    builder.adjust(1)
    return builder.as_markup()

def moderator_keyboard():
    builder = InlineKeyboardBuilder()
    cursor.execute("SELECT COUNT(*) FROM users WHERE status = 'done'")
    done_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE status = 'in_progress'")
    in_progress_count = cursor.fetchone()[0]
    builder.button(text=f"📋 На проверке ({done_count})", callback_data="filter_done")
    builder.button(text=f"🔄 В процессе ({in_progress_count})", callback_data="filter_in_progress")
    builder.button(text="🔄 Обновить", callback_data="mod_refresh")
    builder.adjust(1)
    return builder.as_markup()

def time_stats_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Сегодня", callback_data="ts_today")
    builder.button(text="Вчера", callback_data="ts_yesterday")
    builder.button(text="Эта неделя", callback_data="ts_thisweek")
    builder.button(text="Прошлая неделя", callback_data="ts_lastweek")
    builder.button(text="Этот месяц", callback_data="ts_thismonth")
    builder.button(text="Свой период", callback_data="ts_custom")
    builder.button(text="🔙 Назад", callback_data="admin_refresh")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()

def filter_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Все", callback_data="filter_all")
    builder.button(text="На проверке", callback_data="filter_done")
    builder.button(text="В процессе", callback_data="filter_in_progress")
    builder.button(text="Не начали", callback_data="filter_not_started")
    builder.button(text="Одобрены", callback_data="filter_approved")
    builder.button(text="Отклонены", callback_data="filter_rejected")
    builder.button(text="🔙 Назад", callback_data="admin_refresh")
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup()

def users_list_keyboard(users):
    builder = InlineKeyboardBuilder()
    cv = get_current_task_version()
    for uid, uname, count, status in users:
        name = f"@{uname}" if uname else f"id:{uid}"
        icons = {"not_started": "⚪", "in_progress": "🔄", "done": "✅", "approved": "🎁", "rejected": "❌"}
        icon = icons.get(status, "❓")
        ver = get_user_task_version(uid)
        vt = "" if ver == cv else f" v{ver}"
        bar = make_bar(count, get_user_target(uid), 12)
        builder.button(text=f"{name} — {bar} {icon}{vt}", callback_data=f"view_{uid}")
    builder.button(text="🔙 Назад", callback_data="user_list")
    builder.adjust(1)
    return builder.as_markup()

def photo_nav_keyboard(index, total, user_id):
    builder = InlineKeyboardBuilder()
    row = []
    if index > 0:
        row.append(InlineKeyboardButton(text="⬅️", callback_data=f"phprev_{user_id}_{index}"))
    row.append(InlineKeyboardButton(text=f"{index + 1}/{total}", callback_data="noop"))
    if index < total - 1:
        row.append(InlineKeyboardButton(text="➡️", callback_data=f"phnext_{user_id}_{index}"))
    builder.row(*row)
    uname = get_user_username(user_id)
    if uname:
        builder.row(InlineKeyboardButton(text=f"👤 Написать @{uname}", url=f"https://t.me/{uname}"))
    else:
        builder.row(InlineKeyboardButton(text=f"👤 ID: {user_id}", callback_data="noop"))
    builder.row(InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{user_id}"))
    builder.row(
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rej_{user_id}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"phdel_{user_id}_{index}")
    )
    builder.row(InlineKeyboardButton(text="🔄 Отклонить и сбросить", callback_data=f"rejreset_{user_id}"))
    builder.row(InlineKeyboardButton(text="🔙 К списку", callback_data="back_to_list"))
    return builder.as_markup()

def confirm_broadcast_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Отправить", callback_data="broadcast_confirm")
    builder.button(text="❌ Отмена", callback_data="broadcast_cancel")
    builder.adjust(2)
    return builder.as_markup()

def settings_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🎯 Цель", callback_data="set_target")
    builder.button(text="🎁 Текст мишки", callback_data="set_prize")
    builder.button(text="📝 Шаблон комментария", callback_data="set_template")
    builder.button(text="🆕 Новое задание", callback_data="new_task")
    builder.button(text="⏰ Авто-отчёт", callback_data="auto_report_settings")
    builder.button(text="📺 Каналы", callback_data="channels_settings")
    builder.button(text="📊 Реферальная статистика", callback_data="ref_stats")
    builder.button(text="👥 Модераторы", callback_data="mod_list")
    builder.button(text="🔙 Назад", callback_data="admin_refresh")
    builder.adjust(1)
    return builder.as_markup()

def moderators_keyboard():
    builder = InlineKeyboardBuilder()
    mods = get_moderators()
    for uid, uname in mods:
        name = f"@{uname}" if uname else f"id:{uid}"
        builder.button(text=f"🗑 Убрать: {name}", callback_data=f"modrm_{uid}")
    builder.button(text="➕ Добавить модератора", callback_data="mod_add")
    builder.button(text="🔙 Назад", callback_data="admin_settings")
    builder.adjust(1)
    return builder.as_markup()

def auto_report_keyboard():
    builder = InlineKeyboardBuilder()
    enabled = get_setting("auto_report_enabled", "1") == "1"
    builder.button(text=f"Авто-отчёт: {'ВКЛ ✅' if enabled else 'ВЫКЛ ❌'}", callback_data="toggle_auto_report")
    builder.button(text="⏰ Изменить время", callback_data="set_report_time")
    builder.button(text="📤 Отправить сейчас", callback_data="send_report_now")
    builder.button(text="🔙 Назад", callback_data="admin_settings")
    builder.adjust(1)
    return builder.as_markup()

def confirm_new_task_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Создать", callback_data="new_task_confirm")
    builder.button(text="❌ Отмена", callback_data="admin_settings")
    builder.adjust(2)
    return builder.as_markup()

def remind_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="1 день", callback_data="remind_1")
    builder.button(text="3 дня", callback_data="remind_3")
    builder.button(text="7 дней", callback_data="remind_7")
    builder.button(text="🔙 Назад", callback_data="admin_refresh")
    builder.adjust(3, 1)
    return builder.as_markup()

def channels_keyboard():
    builder = InlineKeyboardBuilder()
    for i, ch in enumerate(get_required_channels()):
        d = ch if ch.startswith("@") else f"@{ch}"
        builder.button(text=f"🗑 {d}", callback_data=f"delchannel_{i}")
    builder.button(text="➕ Добавить канал", callback_data="add_channel")
    builder.button(text="🔙 Назад", callback_data="admin_settings")
    builder.adjust(1)
    return builder.as_markup()


# ====================== ФОРМАТИРОВАНИЕ ======================

def format_general_stats(s):
    return (
        f"📊 Общая статистика\n\n"
        f"Всего: {s['total']} | На проверке: {s['done']} | 🎁 Одобрены: {s['approved']}\n"
        f"❌ Отклонены: {s['rejected']} | 🔄 В процессе: {s['in_progress']} | ⚪ Не начали: {s['not_started']}\n\n"
        f"📷 Скриншотов: {s['total_photos']}"
    )

def format_period_stats(s, name):
    return (
        f"📊 {name}\n\n"
        f"Новых: {s['new_users']} | Активных: {s['active_users']} | Фото: {s['photos']}\n"
        f"Завершили: {s['done']} | Одобрено: {s['approved']} | Отклонено: {s['rejected']}"
    )

def format_settings():
    t = get_current_task_info()
    v = get_current_task_version()
    rh = get_setting("auto_report_hour", "21")
    re = get_setting("auto_report_enabled", "1") == "1"
    td = t["comment_template"] if t["comment_template"] else "(не задан)"
    ch = get_required_channels()
    cd = "\n".join(f"  {c if c.startswith('@') else '@'+c}" for c in ch) if ch else "  (нет)"
    return (
        f"⚙️ Настройки\n\n"
        f"📋 Версия: v{v} | 🎯 Цель: {t['target_count']}\n"
        f"🎁 Мишка:\n{t['prize_text']}\n\n"
        f"📝 Шаблон:\n{td}\n\n"
        f"⏰ Авто-отчёт: {'ВКЛ' if re else 'ВЫКЛ'} в {rh}:00\n\n"
        f"📺 Каналы ({len(ch)}/5):\n{cd}"
    )

def format_new_task_preview():
    t = get_current_task_info()
    v = get_current_task_version()
    return (
        f"🆕 Новое задание\n\n"
        f"Текущее (v{v}): цель {t['target_count']}, мишка: {t['prize_text'][:60]}...\n\n"
        f"Будет создано v{v + 1} с теми же настройками.\n\n"
        f"⚠️ Пользователи с прогрессом ≤50% будут сброшены.\n"
        f"Пользователи >50% останутся на старом.\n\n"
        f"После создания измени цель, мишку и шаблон."
    )

def format_channels():
    ch = get_required_channels()
    if not ch:
        return "📺 Каналы\n\nКаналов нет. Добавь до 5.\n\nОтправь @username или ссылку t.me/..."
    return "📺 Каналы\n\n" + "\n".join(f"{i+1}. {c if c.startswith('@') else '@'+c}" for i, c in enumerate(ch))

def format_moderators():
    mods = get_moderators()
    if not mods:
        return "👥 Модераторы\n\nМодераторов нет.\n\nНажми «➕ Добавить модератора» и отправь ID пользователя."
    text = "👥 Модераторы\n\n"
    for uid, uname in mods:
        name = f"@{uname}" if uname else f"id:{uid}"
        text += f"• {name}\n"
    return text

def get_moderator_panel_text():
    cursor.execute("SELECT COUNT(*) FROM users WHERE status = 'done'")
    dc = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE status = 'in_progress'")
    ipc = cursor.fetchone()[0]
    return f"📋 Панель модератора\n\nНа проверке: {dc}\nВ процессе: {ipc}"


# ====================== FSM СОСТОЯНИЯ ======================

class AdminStates(StatesGroup):
    waiting_broadcast_text = State()
    waiting_custom_start = State()
    waiting_custom_end = State()
    waiting_target_count = State()
    waiting_prize_text = State()
    waiting_comment_template = State()
    waiting_report_time = State()
    waiting_channel_add = State()
    waiting_mod_id = State()


# ====================== ХЕНДЛЕРЫ FSM ======================

@dp.message(AdminStates.waiting_broadcast_text)
async def handle_broadcast_text(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    if message.photo:
        file_id = message.photo[-1].file_id
        await state.update_data(broadcast_photo=file_id, broadcast_caption=message.caption or "", broadcast_text=None)
    elif message.text:
        await state.update_data(broadcast_text=message.text, broadcast_photo=None, broadcast_caption=None)
    else:
        await message.answer("Пришли текст или фото.")
        return
    await message.answer(f"Отправить {len(get_all_user_ids())} пользователям?", reply_markup=confirm_broadcast_keyboard())

@dp.message(AdminStates.waiting_custom_start)
async def handle_custom_start(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    try:
        d = datetime.strptime(message.text.strip(), "%d.%m.%Y").date()
        await state.update_data(start_date=d.isoformat())
        await state.set_state(AdminStates.waiting_custom_end)
        await message.answer(f"Начало: {d.strftime('%d.%m.%Y')}\nКонечная дата (ДД.ММ.ГГГГ):")
    except ValueError:
        await message.answer("Формат: ДД.ММ.ГГГГ, например: 01.09.2026")

@dp.message(AdminStates.waiting_custom_end)
async def handle_custom_end(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    try:
        end_d = datetime.strptime(message.text.strip(), "%d.%m.%Y").date()
        data = await state.get_data()
        start_d = datetime.fromisoformat(data["start_date"]).date()
        if end_d < start_d:
            await message.answer("Конечная дата раньше начальной.")
            await state.clear()
            return
        stats = get_stats_for_period(start_d, end_d)
        await message.answer(format_period_stats(stats, f"Период ({start_d.strftime('%d.%m')} — {end_d.strftime('%d.%m')})"), reply_markup=time_stats_keyboard())
        await state.clear()
    except ValueError:
        await message.answer("Формат: ДД.ММ.ГГГГ")

@dp.message(AdminStates.waiting_target_count)
async def handle_target_count(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    try:
        val = int(message.text.strip())
        if val < 1 or val > 10000:
            await message.answer("Число от 1 до 10000:")
            return
        ver = get_current_task_version()
        cursor.execute("UPDATE task_versions SET target_count = ? WHERE version = ?", (val, ver))
        conn.commit()
        await message.answer(f"✅ Цель: {val}", reply_markup=settings_keyboard())
        await state.clear()
    except ValueError:
        await message.answer("Пришли число, например: 50")

@dp.message(AdminStates.waiting_prize_text)
async def handle_prize_text(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    text = message.text or ""
    if not text.strip():
        await message.answer("Текст не может быть пустым:")
        return
    ver = get_current_task_version()
    cursor.execute("UPDATE task_versions SET prize_text = ? WHERE version = ?", (text, ver))
    conn.commit()
    await message.answer(f"✅ Текст мишки обновлён:\n\n{text}", reply_markup=settings_keyboard())
    await state.clear()

@dp.message(AdminStates.waiting_comment_template)
async def handle_comment_template(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    text = message.text or ""
    if not text.strip():
        await message.answer("Текст не может быть пустым:")
        return
    ver = get_current_task_version()
    cursor.execute("UPDATE task_versions SET comment_template = ? WHERE version = ?", (text, ver))
    conn.commit()
    await message.answer(f"✅ Шаблон обновлён:\n\n{text}", reply_markup=settings_keyboard())
    await state.clear()

@dp.message(AdminStates.waiting_report_time)
async def handle_report_time(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    try:
        hour = int(message.text.strip())
        if hour < 0 or hour > 23:
            await message.answer("Число от 0 до 23:")
            return
        set_setting("auto_report_hour", str(hour))
        await message.answer(f"✅ Время: {hour}:00", reply_markup=auto_report_keyboard())
        await state.clear()
    except ValueError:
        await message.answer("Число от 0 до 23, например: 21")

@dp.message(AdminStates.waiting_channel_add)
async def handle_channel_add(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    raw = message.text.strip()
    if not raw:
        await message.answer("Отправь @username или ссылку t.me/...")
        return
    if raw.startswith("https://t.me/"):
        channel = "@" + raw.split("t.me/")[1].split("/")[0]
    elif raw.startswith("@") or raw.startswith("-100"):
        channel = raw
    else:
        channel = "@" + raw
    channels = get_required_channels()
    if channel in channels:
        await message.answer("Уже добавлен.", reply_markup=channels_keyboard())
        await state.clear()
        return
    if len(channels) >= 5:
        await message.answer("Максимум 5 каналов.", reply_markup=channels_keyboard())
        await state.clear()
        return
    channels.append(channel)
    set_required_channels(channels)
    await message.answer(f"✅ Канал {channel} добавлен.\n\n{format_channels()}", reply_markup=channels_keyboard())
    await state.clear()

@dp.message(AdminStates.waiting_mod_id)
async def handle_mod_id(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    try:
        mod_id = int(message.text.strip())
        if mod_id == ADMIN_ID:
            await message.answer("Ты уже админ! 😉", reply_markup=moderators_keyboard())
            await state.clear()
            return
        cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (mod_id,))
        if not cursor.fetchone():
            await message.answer("Пользователь не найден. Он должен сначала запустить бота.", reply_markup=moderators_keyboard())
            await state.clear()
            return
        if is_moderator(mod_id):
            await message.answer("Уже модератор.", reply_markup=moderators_keyboard())
            await state.clear()
            return
        add_moderator(mod_id)
        try:
            await bot.send_message(mod_id, "🎉 Ты назначен модератором!\n\nТеперь ты можешь проверять скриншоты. Напиши /admin для входа.")
        except Exception:
            pass
        await message.answer(f"✅ Модератор добавлен: {mod_id}", reply_markup=moderators_keyboard())
        await state.clear()
    except ValueError:
        await message.answer("Отправь числовой ID пользователя:")


# ====================== /cancel, /debug, /help ======================

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer("❌ Отменено.", reply_markup=stats_keyboard())

@dp.message(Command("debug"), StateFilter(None))
async def cmd_debug(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    cursor.execute("SELECT COUNT(*) FROM users")
    uc = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM photos")
    pc = cursor.fetchone()[0]
    await message.answer(f"🔧 Отладка\n\nUsers: {uc}\nPhotos: {pc}\nModerators: {len(get_moderators())}")

@dp.message(Command("help"), StateFilter(None))
async def cmd_help(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("🔧 Админ-команды:\n\n/admin — панель\n/debug — отладка\n/cancel — отменить")
        return
    if is_moderator(message.from_user.id):
        await message.answer("🔧 Команды модератора:\n\n/admin — панель модератора\n/cancel — отменить")
        return
    unsub = await check_user_subscriptions(message.from_user.id)
    if unsub:
        await message.answer(format_subscription_message(unsub), reply_markup=subscription_keyboard(unsub))
        return
    t = get_current_task_info()
    template = t["comment_template"]
    tpl = f"\n{template}" if template else "\n(админ ещё не задал шаблон)"
    await message.answer(
        f"ℹ️ Помощь\n\n1. Подпишись на каналы\n2. Оставь комментарии по шаблону:{tpl}\n"
        f"3. Сделай скриншоты\n4. Отправь боту (можно альбомом!)\n5. Наберёшь {t['target_count']} — жми /done\n\n"
        f"Команды: /start /progress /done /help",
        reply_markup=main_menu_keyboard()
    )

@dp.callback_query(F.data == "main_help")
async def cb_main_help(callback: types.CallbackQuery):
    if callback.from_user.id == ADMIN_ID:
        await callback.answer("Админ: /admin")
        return
    t = get_current_task_info()
    template = t["comment_template"]
    tpl = f"\n{template}" if template else "\n(админ ещё не задал шаблон)"
    text = (
        f"ℹ️ Помощь\n\n1. Подпишись на каналы\n2. Оставь комментарии:{tpl}\n"
        f"3. Сделай скриншоты\n4. Отправь боту\n5. /done когда наберёшь {t['target_count']}"
    )
    try:
        await callback.message.edit_text(text, reply_markup=main_menu_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=main_menu_keyboard())
    await callback.answer()


# ====================== ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЯ ======================

@dp.message(Command("start"), StateFilter(None))
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        stats = get_general_stats()
        await message.answer(format_general_stats(stats), reply_markup=stats_keyboard())
        return

    referred_by = None
    if message.text and message.text.startswith("/start ref_"):
        try:
            referred_by = int(message.text.split("ref_")[1])
            if referred_by == message.from_user.id:
                referred_by = None
        except Exception:
            referred_by = None

    add_user(message.from_user.id, message.from_user.username or "", referred_by)
    unsub = await check_user_subscriptions(message.from_user.id)
    if unsub:
        await message.answer(format_subscription_message(unsub), reply_markup=subscription_keyboard(unsub))
        return

    uid = message.from_user.id
    ver = get_user_task_version(uid)
    task = get_task_info(ver)
    target = get_effective_target(uid)
    template = task["comment_template"]
    tpl = f"\n\n📝 Шаблон:\n{template}" if template else "\n\n📝 Шаблон: (админ ещё не задал)"
    cv = get_current_task_version()
    vi = f"\n\n📋 Ты на v{ver} (актуальное v{cv}). Доделай текущее!" if ver != cv else ""
    await message.answer(
        f"👋 Привет!\n\nЗадача: {target} скриншотов.{tpl}{vi}\n\nПрисылай фото (можно альбомом).\nКогда закончишь — /done",
        reply_markup=main_menu_keyboard()
    )

@dp.message(Command("progress"), StateFilter(None))
async def cmd_progress(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        return
    unsub = await check_user_subscriptions(message.from_user.id)
    if unsub:
        await message.answer(format_subscription_message(unsub), reply_markup=subscription_keyboard(unsub))
        return
    count = get_user_count(message.from_user.id)
    target = get_effective_target(message.from_user.id)
    left = target - count
    bar = make_bar(count, target)
    await message.answer(f"📊 Прогресс: {bar}\nОсталось: {left}", reply_markup=main_menu_keyboard())

@dp.message(Command("done"), StateFilter(None))
async def cmd_done(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        return
    unsub = await check_user_subscriptions(message.from_user.id)
    if unsub:
        await message.answer(format_subscription_message(unsub), reply_markup=subscription_keyboard(unsub))
        return
    uid = message.from_user.id
    count = get_user_count(uid)
    target = get_effective_target(uid)
    if count < target:
        await message.answer(f"Ты прислал {count} из {target}. Нужно больше!\n{make_bar(count, target)}", reply_markup=main_menu_keyboard())
        return
    set_user_status(uid, "done")
    await message.answer("Заявка на проверку! Ожидай результат 🎯", reply_markup=main_menu_keyboard())
    uname = message.from_user.username or f"id:{uid}"
    ver = get_user_task_version(uid)
    try:
        await bot.send_message(ADMIN_ID, f"🔔 @{uname} (id:{uid}) завершил v{ver}: {count}/{target}")
    except Exception as e:
        logging.error(f"notify_admin done: {e}")
    for mod_id, _ in get_moderators():
        try:
            await bot.send_message(mod_id, f"🔔 @{uname} (id:{uid}) завершил v{ver}: {count}/{target}")
        except Exception:
            pass

@dp.message(Command("admin"), StateFilter(None))
async def cmd_admin(message: types.Message):
    uid = message.from_user.id
    if uid == ADMIN_ID:
        stats = get_general_stats()
        await message.answer(format_general_stats(stats), reply_markup=stats_keyboard())
    elif is_moderator(uid):
        await message.answer(get_moderator_panel_text(), reply_markup=moderator_keyboard())

@dp.message(F.media_group_id, StateFilter(None))
async def handle_album(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        return
    unsub = await check_user_subscriptions(message.from_user.id)
    if unsub:
        await message.answer(format_subscription_message(unsub), reply_markup=subscription_keyboard(unsub))
        return
    uid = message.from_user.id
    add_user(uid, message.from_user.username or "")
    mg_id = message.media_group_id
    file_id = message.photo[-1].file_id
    if mg_id not in album_buffer:
        album_buffer[mg_id] = []
    album_buffer[mg_id].append(file_id)
    await asyncio.sleep(0.5)
    if album_buffer.get(mg_id, [None])[0] != file_id:
        return
    all_fids = album_buffer.pop(mg_id, [])
    added, skipped, hit_bonus = 0, 0, False
    for fid in all_fids:
        if add_photo(uid, fid):
            added += 1
            if get_user_count(uid) == 3:
                hit_bonus = True
        else:
            skipped += 1
    if added == 0:
        await message.answer("Все фото уже были засчитаны!", reply_markup=main_menu_keyboard())
        return
    count = get_user_count(uid)
    if hit_bonus:
        await try_notify_referrer(uid)
    target = get_effective_target(uid)
    bar = make_bar(count, target)
    skip = f"\n⏭ {skipped} уже было" if skipped > 0 else ""
    if count >= target:
        await message.answer(f"Принято {added}!{skip}\n{bar}\nСобрал всё! Жми /done 🎯", reply_markup=main_menu_keyboard())
    else:
        await message.answer(f"Принято {added}!{skip}\n{bar}\nОсталось: {target - count}", reply_markup=main_menu_keyboard())

@dp.message(F.photo, StateFilter(None))
async def handle_photo(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        return
    if message.media_group_id:
        return
    unsub = await check_user_subscriptions(message.from_user.id)
    if unsub:
        await message.answer(format_subscription_message(unsub), reply_markup=subscription_keyboard(unsub))
        return
    uid = message.from_user.id
    add_user(uid, message.from_user.username or "")
    if not can_send_photo(uid):
        await message.answer("⏳ Подожди пару секунд...", reply_markup=main_menu_keyboard())
        return
    file_id = message.photo[-1].file_id
    if not add_photo(uid, file_id):
        await message.answer("Это фото уже засчитано!", reply_markup=main_menu_keyboard())
        return
    count = get_user_count(uid)
    if count == 3:
        await try_notify_referrer(uid)
    target = get_effective_target(uid)
    bar = make_bar(count, target)
    if count >= target:
        await message.answer(f"{bar}\nСобрал всё! Жми /done 🎯", reply_markup=main_menu_keyboard())
    else:
        await message.answer(f"Принято! {bar}\nОсталось: {target - count}", reply_markup=main_menu_keyboard())

@dp.message(StateFilter(None))
async def handle_other(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        return
    unsub = await check_user_subscriptions(message.from_user.id)
    if unsub:
        await message.answer(format_subscription_message(unsub), reply_markup=subscription_keyboard(unsub))
        return
    await message.answer("Присылай скриншоты. Закончил — /done.\nПомощь — /help", reply_markup=main_menu_keyboard())


# ====================== CALLBACKS ПОЛЬЗОВАТЕЛЯ ======================

@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: types.CallbackQuery):
    if callback.from_user.id == ADMIN_ID:
        await callback.answer()
        return
    unsub = await check_user_subscriptions(callback.from_user.id)
    if unsub:
        await callback.answer("Ещё не подписан!", show_alert=True)
        try:
            await callback.message.edit_text(format_subscription_message(unsub), reply_markup=subscription_keyboard(unsub))
        except Exception:
            await callback.message.answer(format_subscription_message(unsub), reply_markup=subscription_keyboard(unsub))
    else:
        await callback.answer("✅ Подписка подтверждена!")
        add_user(callback.from_user.id, callback.from_user.username or "")
        uid = callback.from_user.id
        ver = get_user_task_version(uid)
        task = get_task_info(ver)
        target = get_effective_target(uid)
        tpl = f"\n\n📝 Шаблон:\n{task['comment_template']}" if task["comment_template"] else "\n\n📝 Шаблон: (админ ещё не задал)"
        cv = get_current_task_version()
        vi = f"\n\n📋 Ты на v{ver} (актуальное v{cv})!" if ver != cv else ""
        text = f"✅ Спасибо за подписку!\n\nЗадача: {target} скриншотов.{tpl}{vi}\n\nПрисылай фото. Когда закончишь — /done"
        try:
            await callback.message.edit_text(text, reply_markup=main_menu_keyboard())
        except Exception:
            await callback.message.answer(text, reply_markup=main_menu_keyboard())

@dp.callback_query(F.data == "main_task")
async def cb_main_task(callback: types.CallbackQuery):
    if callback.from_user.id == ADMIN_ID:
        await callback.answer()
        return
    unsub = await check_user_subscriptions(callback.from_user.id)
    if unsub:
        try:
            await callback.message.edit_text(format_subscription_message(unsub), reply_markup=subscription_keyboard(unsub))
        except Exception:
            await callback.message.answer(format_subscription_message(unsub), reply_markup=subscription_keyboard(unsub))
        await callback.answer()
        return
    uid = callback.from_user.id
    ver = get_user_task_version(uid)
    task = get_task_info(ver)
    bt = task["target_count"]
    et = get_effective_target(uid)
    count = get_user_count(uid)
    bc = get_referral_bonus_count(uid)
    cv = get_current_task_version()
    vi = f"\n📋 v{ver} (актуальная v{cv})" if ver != cv else ""
    bonus = f"\n🎁 Бонус: -{bc} (цель {bt} → {et})" if bc > 0 else ""
    tpl = task["comment_template"] or "(не задан)"
    text = f"📝 Задание\n\nЦель: {et}\nПрогресс: {make_bar(count, et, 14)}\nОсталось: {et - count}{bonus}{vi}\n\nШаблон:\n{tpl}\n\nПрисылай фото. /done когда закончишь."
    try:
        await callback.message.edit_text(text, reply_markup=main_menu_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "main_progress")
async def cb_main_progress(callback: types.CallbackQuery):
    if callback.from_user.id == ADMIN_ID:
        await callback.answer()
        return
    unsub = await check_user_subscriptions(callback.from_user.id)
    if unsub:
        try:
            await callback.message.edit_text(format_subscription_message(unsub), reply_markup=subscription_keyboard(unsub))
        except Exception:
            await callback.message.answer(format_subscription_message(unsub), reply_markup=subscription_keyboard(unsub))
        await callback.answer()
        return
    uid = callback.from_user.id
    count = get_user_count(uid)
    bt = get_user_target(uid)
    et = get_effective_target(uid)
    refs = get_active_referrals_count(uid)
    bc = get_referral_bonus_count(uid)
    bonus = f"\n🎁 Бонус: -{bc} (цель {bt} → {et})" if bc > 0 else ""
    text = f"📊 Прогресс\n\n{make_bar(count, et, 14)}\nОсталось: {et - count}{bonus}\n\n👥 Активных друзей: {refs}\n🎁 Бонусов: {bc}/20"
    try:
        await callback.message.edit_text(text, reply_markup=main_menu_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "main_referral")
async def cb_main_referral(callback: types.CallbackQuery):
    if callback.from_user.id == ADMIN_ID:
        await callback.answer()
        return
    unsub = await check_user_subscriptions(callback.from_user.id)
    if unsub:
        try:
            await callback.message.edit_text(format_subscription_message(unsub), reply_markup=subscription_keyboard(unsub))
        except Exception:
            await callback.message.answer(format_subscription_message(unsub), reply_markup=subscription_keyboard(unsub))
        await callback.answer()
        return
    uid = callback.from_user.id
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{uid}"
    ar = get_active_referrals_count(uid)
    bc = get_referral_bonus_count(uid)
    bt = get_user_target(uid)
    et = get_effective_target(uid)
    text = (
        f"👥 Друзья\n\n🔗 Твоя ссылка:\n{link}\n\n"
        f"━━━━━━━━━━━━━\n🎁 Как работает:\n\n1. Отправь ссылку другу\n2. Друг подписывается\n3. Друг присылает 3 скриншота\n4. Ты получаешь -1 к цели!\n\n"
        f"━━━━━━━━━━━━━\n📊 Статистика:\n• Активных: {ar}\n• Бонус: -{bc}\n• Цель: {bt} → {et}\n\n"
        f"⚠️ Макс. бонус: 20\nМин. цель: {max(1, bt - 20)}\n\nБольше друзей — меньше скриншотов!"
    )
    try:
        await callback.message.edit_text(text, reply_markup=main_menu_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=main_menu_keyboard())
    await callback.answer()


# ====================== CALLBACKS АДМИНА И МОДЕРАТОРА ======================

@dp.callback_query(F.data == "admin_refresh")
async def cb_admin_refresh(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    try:
        await state.clear()
        stats = get_general_stats()
        try:
            await callback.message.edit_text(format_general_stats(stats), reply_markup=stats_keyboard())
        except Exception:
            await callback.message.answer(format_general_stats(stats), reply_markup=stats_keyboard())
    except Exception as e:
        logging.error(f"admin_refresh: {e}")
    await callback.answer()

@dp.callback_query(F.data == "mod_refresh")
async def cb_mod_refresh(callback: types.CallbackQuery):
    if not is_moderator(callback.from_user.id):
        await callback.answer()
        return
    try:
        await callback.message.edit_text(get_moderator_panel_text(), reply_markup=moderator_keyboard())
    except Exception:
        await callback.message.answer(get_moderator_panel_text(), reply_markup=moderator_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "time_stats")
async def cb_time_stats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    try:
        await callback.message.edit_text("📊 Выбери период:", reply_markup=time_stats_keyboard())
    except Exception:
        await callback.message.answer("📊 Выбери период:", reply_markup=time_stats_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "ts_today")
async def cb_ts_today(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    text = format_period_stats(get_stats_for_period(date.today(), date.today()), "Сегодня")
    try:
        await callback.message.edit_text(text, reply_markup=time_stats_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=time_stats_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "ts_yesterday")
async def cb_ts_yesterday(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    y = date.today() - timedelta(days=1)
    text = format_period_stats(get_stats_for_period(y, y), "Вчера")
    try:
        await callback.message.edit_text(text, reply_markup=time_stats_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=time_stats_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "ts_thisweek")
async def cb_ts_thisweek(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    ws = get_week_start(date.today())
    text = format_period_stats(get_stats_for_period(ws, date.today()), f"Эта неделя ({ws.strftime('%d.%m')} — сегодня)")
    try:
        await callback.message.edit_text(text, reply_markup=time_stats_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=time_stats_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "ts_lastweek")
async def cb_ts_lastweek(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    ws = get_week_start(date.today()) - timedelta(days=7)
    we = ws + timedelta(days=6)
    text = format_period_stats(get_stats_for_period(ws, we), f"Прошлая неделя ({ws.strftime('%d.%m')} — {we.strftime('%d.%m')})")
    try:
        await callback.message.edit_text(text, reply_markup=time_stats_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=time_stats_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "ts_thismonth")
async def cb_ts_thismonth(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    today = date.today()
    text = format_period_stats(get_stats_for_period(date(today.year, today.month, 1), today), f"Этот месяц ({today.strftime('%m.%Y')})")
    try:
        await callback.message.edit_text(text, reply_markup=time_stats_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=time_stats_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "ts_custom")
async def cb_ts_custom(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await state.set_state(AdminStates.waiting_custom_start)
    try:
        await callback.message.edit_text("Начальная дата (ДД.ММ.ГГГГ):\n/cancel — отмена")
    except Exception:
        await callback.message.answer("Начальная дата (ДД.ММ.ГГГГ):\n/cancel — отмена")
    await callback.answer()

@dp.callback_query(F.data == "user_list")
async def cb_user_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    try:
        await callback.message.edit_text("Выбери фильтр:", reply_markup=filter_keyboard())
    except Exception:
        await callback.message.answer("Выбери фильтр:", reply_markup=filter_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("filter_"))
async def cb_filter(callback: types.CallbackQuery):
    if not is_admin_or_moderator(callback.from_user.id):
        await callback.answer()
        return
    if is_moderator(callback.from_user.id) and callback.data not in ("filter_done", "filter_in_progress"):
        await callback.answer("Недоступно")
        return
    status = callback.data.replace("filter_", "")
    if status == "all":
        users = get_all_users()
        title = "Все"
    else:
        users = get_users_by_status(status)
        titles = {"done": "На проверке", "in_progress": "В процессе", "not_started": "Не начали", "approved": "Одобрены", "rejected": "Отклонены"}
        title = titles.get(status, status)
    if not users:
        await callback.answer("Пусто")
        try:
            kb = filter_keyboard() if callback.from_user.id == ADMIN_ID else moderator_keyboard()
            await callback.message.edit_text(f"{title}: пусто", reply_markup=kb)
        except Exception:
            pass
        return
    kb = users_list_keyboard(users)
    text = f"{title} ({len(users)}):"
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()

# ---- ПРОСМОТР СКРИНШОТОВ ----

@dp.callback_query(F.data.startswith("view_"))
async def cb_view_user(callback: types.CallbackQuery):
    if not is_admin_or_moderator(callback.from_user.id):
        await callback.answer()
        return
    try:
        uid = int(callback.data.split("_")[1])
        photos = get_user_photos(uid)
        if not photos:
            await callback.answer("Нет фото")
            return
        save_admin_session(callback.from_user.id, uid, 0)
        await callback.message.answer_photo(photos[0], caption=f"Фото 1 из {len(photos)}", reply_markup=photo_nav_keyboard(0, len(photos), uid))
    except Exception as e:
        logging.error(f"view: {e}")
    await callback.answer()

@dp.callback_query(F.data.startswith("phprev_"))
async def cb_photo_prev(callback: types.CallbackQuery):
    if not is_admin_or_moderator(callback.from_user.id):
        await callback.answer()
        return
    try:
        parts = callback.data.split("_")
        uid = int(parts[1])
        cur = int(parts[2])
        photos = get_user_photos(uid)
        total = len(photos)
        if total == 0:
            await callback.answer("Нет фото")
            return
        ni = cur - 1
        if ni < 0:
            await callback.answer("Первое фото")
            return
        save_admin_session(callback.from_user.id, uid, ni)
        kb = photo_nav_keyboard(ni, total, uid)
        cap = f"Фото {ni + 1} из {total}"
        try:
            await callback.message.edit_media(media=types.InputMediaPhoto(media=photos[ni], caption=cap), reply_markup=kb)
        except Exception:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.answer_photo(photos[ni], caption=cap, reply_markup=kb)
    except Exception as e:
        logging.error(f"prev: {e}")
    await callback.answer()

@dp.callback_query(F.data.startswith("phnext_"))
async def cb_photo_next(callback: types.CallbackQuery):
    if not is_admin_or_moderator(callback.from_user.id):
        await callback.answer()
        return
    try:
        parts = callback.data.split("_")
        uid = int(parts[1])
        cur = int(parts[2])
        photos = get_user_photos(uid)
        total = len(photos)
        if total == 0:
            await callback.answer("Нет фото")
            return
        ni = cur + 1
        if ni >= total:
            await callback.answer("Последнее фото")
            return
        save_admin_session(callback.from_user.id, uid, ni)
        kb = photo_nav_keyboard(ni, total, uid)
        cap = f"Фото {ni + 1} из {total}"
        try:
            await callback.message.edit_media(media=types.InputMediaPhoto(media=photos[ni], caption=cap), reply_markup=kb)
        except Exception:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.answer_photo(photos[ni], caption=cap, reply_markup=kb)
    except Exception as e:
        logging.error(f"next: {e}")
    await callback.answer()

@dp.callback_query(F.data.startswith("phdel_"))
async def cb_photo_del(callback: types.CallbackQuery):
    if not is_admin_or_moderator(callback.from_user.id):
        await callback.answer()
        return
    try:
        parts = callback.data.split("_")
        uid = int(parts[1])
        cur = int(parts[2])
        if not delete_photo_by_index(uid, cur):
            await callback.answer("Не удалось")
            return
        photos = get_user_photos(uid)
        if not photos:
            await callback.answer("Удалено. Больше фото нет.")
            try:
                await callback.message.delete()
            except Exception:
                pass
            kb = filter_keyboard() if callback.from_user.id == ADMIN_ID else moderator_keyboard()
            await callback.message.answer("Больше нет фото.", reply_markup=kb)
            return
        ni = min(cur, len(photos) - 1)
        save_admin_session(callback.from_user.id, uid, ni)
        kb = photo_nav_keyboard(ni, len(photos), uid)
        cap = f"Фото {ni + 1} из {len(photos)} (удалено)"
        try:
            await callback.message.edit_media(media=types.InputMediaPhoto(media=photos[ni], caption=cap), reply_markup=kb)
        except Exception:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.answer_photo(photos[ni], caption=cap, reply_markup=kb)
        await callback.answer("🗑 Удалено")
        return
    except Exception as e:
        logging.error(f"del: {e}")
    await callback.answer()

@dp.callback_query(F.data == "back_to_list")
async def cb_back_to_list(callback: types.CallbackQuery):
    if not is_admin_or_moderator(callback.from_user.id):
        await callback.answer()
        return
    try:
        await callback.message.delete()
    except Exception:
        pass
    if callback.from_user.id == ADMIN_ID:
        await callback.message.answer("Выбери фильтр:", reply_markup=filter_keyboard())
    else:
        await callback.message.answer(get_moderator_panel_text(), reply_markup=moderator_keyboard())
    await callback.answer()

# ---- Одобрение / Отклонение ----

@dp.callback_query(F.data.startswith("approve_"))
async def cb_approve(callback: types.CallbackQuery):
    if not is_admin_or_moderator(callback.from_user.id):
        await callback.answer()
        return
    try:
        uid = int(callback.data.split("_")[1])
        uver = get_user_task_version(uid)
        prize = get_task_info(uver)["prize_text"]
        set_user_status(uid, "approved")
        try:
            await bot.send_message(uid, prize)
        except Exception:
            pass
        cv = get_current_task_version()
        if uver != cv:
            cursor.execute("DELETE FROM photos WHERE user_id = ?", (uid,))
            cursor.execute("UPDATE users SET count = 0, status = 'not_started', task_version = ?, last_photo_at = NULL, done_at = NULL, reviewed_at = NULL, ref_bonus_sent = 0 WHERE user_id = ?", (cv, uid))
            conn.commit()
            nt = get_current_task_info()
            tpl = nt["comment_template"] if nt["comment_template"] else "(не задан)"
            try:
                await bot.send_message(uid, f"📋 Новое задание v{cv}!\n🎯 Цель: {nt['target_count']}\n📝 Шаблон: {tpl}")
            except Exception:
                pass
        try:
            await callback.message.delete()
        except Exception:
            pass
        if callback.from_user.id == ADMIN_ID:
            await callback.message.answer(format_general_stats(get_general_stats()), reply_markup=stats_keyboard())
        else:
            await callback.message.answer(get_moderator_panel_text(), reply_markup=moderator_keyboard())
    except Exception as e:
        logging.error(f"approve: {e}")
    await callback.answer("✅ Мишка отправлен!")

@dp.callback_query(F.data.startswith("rejreset_"))
async def cb_rejreset(callback: types.CallbackQuery):
    if not is_admin_or_moderator(callback.from_user.id):
        await callback.answer()
        return
    try:
        uid = int(callback.data.split("_")[1])
        reset_user_progress(uid)
        try:
            await callback.message.delete()
        except Exception:
            pass
        if callback.from_user.id == ADMIN_ID:
            await callback.message.answer(format_general_stats(get_general_stats()), reply_markup=stats_keyboard())
        else:
            await callback.message.answer(get_moderator_panel_text(), reply_markup=moderator_keyboard())
    except Exception as e:
        logging.error(f"rejreset: {e}")
    await callback.answer("Сброшено")

@dp.callback_query(F.data.startswith("rej_"))
async def cb_rej(callback: types.CallbackQuery):
    if not is_admin_or_moderator(callback.from_user.id):
        await callback.answer()
        return
    try:
        uid = int(callback.data.split("_")[1])
        set_user_status(uid, "rejected")
        try:
            await callback.message.delete()
        except Exception:
            pass
        if callback.from_user.id == ADMIN_ID:
            await callback.message.answer(format_general_stats(get_general_stats()), reply_markup=stats_keyboard())
        else:
            await callback.message.answer(get_moderator_panel_text(), reply_markup=moderator_keyboard())
    except Exception as e:
        logging.error(f"rej: {e}")
    await callback.answer("Отклонено")

@dp.callback_query(F.data == "noop")
async def cb_noop(callback: types.CallbackQuery):
    await callback.answer()

# ---- Рассылка ----

@dp.callback_query(F.data == "broadcast_start")
async def cb_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await state.set_state(AdminStates.waiting_broadcast_text)
    try:
        await callback.message.edit_text("📨 Текст или фото для рассылки:\n/cancel — отмена")
    except Exception:
        await callback.message.answer("📨 Текст или фото:\n/cancel — отмена")
    await callback.answer()

@dp.callback_query(F.data == "broadcast_confirm")
async def cb_broadcast_confirm(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    data = await state.get_data()
    uids = get_all_user_ids()
    sent, failed = 0, 0
    photo = data.get("broadcast_photo")
    text = data.get("broadcast_text") or data.get("broadcast_caption") or ""
    for uid in uids:
        try:
            if photo:
                await bot.send_photo(uid, photo, caption=text)
            elif text:
                await bot.send_message(uid, text)
            else:
                failed += 1
                continue
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    cursor.execute("INSERT INTO broadcasts (text, sent_count, failed_count) VALUES (?, ?, ?)", (text, sent, failed))
    conn.commit()
    try:
        await callback.message.edit_text(f"📨 Отправлено: {sent} | Не доставлено: {failed}", reply_markup=stats_keyboard())
    except Exception:
        await callback.message.answer(f"📨 Отправлено: {sent} | Не доставлено: {failed}", reply_markup=stats_keyboard())
    await state.clear()
    await callback.answer()

@dp.callback_query(F.data == "broadcast_cancel")
async def cb_broadcast_cancel(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await state.clear()
    try:
        await callback.message.edit_text("Отменено.", reply_markup=stats_keyboard())
    except Exception:
        await callback.message.answer("Отменено.", reply_markup=stats_keyboard())
    await callback.answer()

# ---- Настройки ----

@dp.callback_query(F.data == "admin_settings")
async def cb_settings(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    try:
        await callback.message.edit_text(format_settings(), reply_markup=settings_keyboard())
    except Exception:
        await callback.message.answer(format_settings(), reply_markup=settings_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "set_target")
async def cb_set_target(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await state.set_state(AdminStates.waiting_target_count)
    t = get_current_task_info()
    try:
        await callback.message.edit_text(f"Текущая цель: {t['target_count']}\nНовое число:\n/cancel — отмена")
    except Exception:
        await callback.message.answer(f"Текущая цель: {t['target_count']}\nНовое число:\n/cancel — отмена")
    await callback.answer()

@dp.callback_query(F.data == "set_prize")
async def cb_set_prize(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await state.set_state(AdminStates.waiting_prize_text)
    t = get_current_task_info()
    try:
        await callback.message.edit_text(f"Текущий текст мишки:\n\n{t['prize_text']}\n\nНовый текст:\n/cancel — отмена")
    except Exception:
        await callback.message.answer(f"Текущий текст мишки:\n\n{t['prize_text']}\n\nНовый текст:\n/cancel — отмена")
    await callback.answer()

@dp.callback_query(F.data == "set_template")
async def cb_set_template(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await state.set_state(AdminStates.waiting_comment_template)
    t = get_current_task_info()
    cur = t["comment_template"] if t["comment_template"] else "(не задан)"
    try:
        await callback.message.edit_text(f"Текущий шаблон:\n\n{cur}\n\nНовый шаблон:\n/cancel — отмена")
    except Exception:
        await callback.message.answer(f"Текущий шаблон:\n\n{cur}\n\nНовый шаблон:\n/cancel — отмена")
    await callback.answer()

@dp.callback_query(F.data == "new_task")
async def cb_new_task(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    try:
        await callback.message.edit_text(format_new_task_preview(), reply_markup=confirm_new_task_keyboard())
    except Exception:
        await callback.message.answer(format_new_task_preview(), reply_markup=confirm_new_task_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "new_task_confirm")
async def cb_new_task_confirm(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    t = get_current_task_info()
    nv = create_new_task_version(t["target_count"], t["prize_text"], t["comment_template"])
    apply_new_task_version(nv, t["target_count"])
    cursor.execute("SELECT COUNT(*) FROM users WHERE task_version = ?", (nv,))
    rc = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE task_version != ? AND status IN ('in_progress', 'not_started')", (nv,))
    kc = cursor.fetchone()[0]
    text = f"✅ Создано v{nv}!\n🔄 Сброшено: {rc} | Остались: {kc}\n\nИзмени цель, мишку и шаблон."
    try:
        await callback.message.edit_text(text, reply_markup=settings_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=settings_keyboard())
    await callback.answer()

# ---- Авто-отчёт ----

@dp.callback_query(F.data == "auto_report_settings")
async def cb_auto_report_settings(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    h = get_setting("auto_report_hour", "21")
    e = get_setting("auto_report_enabled", "1") == "1"
    text = f"⏰ Авто-отчёт: {'ВКЛ ✅' if e else 'ВЫКЛ ❌'} в {h}:00"
    try:
        await callback.message.edit_text(text, reply_markup=auto_report_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=auto_report_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "toggle_auto_report")
async def cb_toggle_auto_report(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    nv = "0" if get_setting("auto_report_enabled", "1") == "1" else "1"
    set_setting("auto_report_enabled", nv)
    h = get_setting("auto_report_hour", "21")
    text = f"⏰ Авто-отчёт: {'ВКЛ ✅' if nv == '1' else 'ВЫКЛ ❌'} в {h}:00"
    try:
        await callback.message.edit_text(text, reply_markup=auto_report_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=auto_report_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "set_report_time")
async def cb_set_report_time(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await state.set_state(AdminStates.waiting_report_time)
    c = get_setting("auto_report_hour", "21")
    try:
        await callback.message.edit_text(f"Текущее: {c}:00\nНовый час (0-23):\n/cancel — отмена")
    except Exception:
        await callback.message.answer(f"Текущее: {c}:00\nНовый час (0-23):\n/cancel — отмена")
    await callback.answer()

@dp.callback_query(F.data == "send_report_now")
async def cb_send_report_now(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    r = get_daily_report_text()
    cursor.execute("INSERT INTO auto_reports (report_text) VALUES (?)", (r,))
    conn.commit()
    await callback.message.answer(r)
    await callback.answer("Отправлен!")

# ---- Реферальная статистика ----

@dp.callback_query(F.data == "ref_stats")
async def cb_ref_stats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    cursor.execute("SELECT COUNT(*) FROM users WHERE referred_by IS NOT NULL")
    tr = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE referred_by IS NOT NULL AND count >= 3")
    ar = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(DISTINCT referred_by) FROM users WHERE referred_by IS NOT NULL")
    trs = cursor.fetchone()[0]
    cursor.execute("""SELECT u.user_id, u.username, COUNT(r.user_id) as rc,
                      SUM(CASE WHEN r.count >= 3 THEN 1 ELSE 0 END) as ac
                      FROM users u JOIN users r ON r.referred_by = u.user_id
                      GROUP BY u.user_id ORDER BY rc DESC LIMIT 10""")
    top = cursor.fetchall()
    text = f"📊 Рефералы\n\nВсего: {tr} | Активных: {ar} | Рефоводов: {trs}\n\n🏆 Топ-10:\n\n"
    if not top:
        text += "Пока никого не пригласили."
    else:
        for i, (uid, un, rc, ac) in enumerate(top, 1):
            n = f"@{un}" if un else f"id:{uid}"
            text += f"{i}. {n} — {rc} всего, {ac} активных\n"
    try:
        await callback.message.edit_text(text, reply_markup=settings_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=settings_keyboard())
    await callback.answer()

# ---- Модераторы (управление) ----

@dp.callback_query(F.data == "mod_list")
async def cb_mod_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    try:
        await callback.message.edit_text(format_moderators(), reply_markup=moderators_keyboard())
    except Exception:
        await callback.message.answer(format_moderators(), reply_markup=moderators_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "mod_add")
async def cb_mod_add(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await state.set_state(AdminStates.waiting_mod_id)
    try:
        await callback.message.edit_text("Отправь ID пользователя:\n(он должен запустить бота)\n/cancel — отмена")
    except Exception:
        await callback.message.answer("Отправь ID пользователя:\n/cancel — отмена")
    await callback.answer()

@dp.callback_query(F.data.startswith("modrm_"))
async def cb_mod_remove(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    uid = int(callback.data.split("_")[1])
    remove_moderator(uid)
    try:
        await bot.send_message(uid, "❌ Ты больше не модератор.")
    except Exception:
        pass
    try:
        await callback.message.edit_text(format_moderators(), reply_markup=moderators_keyboard())
    except Exception:
        await callback.message.answer(format_moderators(), reply_markup=moderators_keyboard())
    await callback.answer("Убран")

# ---- Каналы ----

@dp.callback_query(F.data == "channels_settings")
async def cb_channels_settings(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    try:
        await callback.message.edit_text(format_channels(), reply_markup=channels_keyboard())
    except Exception:
        await callback.message.answer(format_channels(), reply_markup=channels_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "add_channel")
async def cb_add_channel(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    if len(get_required_channels()) >= 5:
        await callback.answer("Максимум 5!")
        return
    await state.set_state(AdminStates.waiting_channel_add)
    try:
        await callback.message.edit_text("@username или ссылку t.me/...\n/cancel — отмена")
    except Exception:
        await callback.message.answer("@username или ссылку t.me/...\n/cancel — отмена")
    await callback.answer()

@dp.callback_query(F.data.startswith("delchannel_"))
async def cb_del_channel(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    idx = int(callback.data.split("_")[1])
    ch = get_required_channels()
    if 0 <= idx < len(ch):
        ch.pop(idx)
        set_required_channels(ch)
    try:
        await callback.message.edit_text(format_channels(), reply_markup=channels_keyboard())
    except Exception:
        await callback.message.answer(format_channels(), reply_markup=channels_keyboard())
    await callback.answer()

# ---- Напоминания ----

@dp.callback_query(F.data == "remind_inactive")
async def cb_remind_inactive(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    try:
        await callback.message.edit_text("🔔 Кому напомнить?", reply_markup=remind_keyboard())
    except Exception:
        await callback.message.answer("🔔 Кому напомнить?", reply_markup=remind_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("remind_"))
async def cb_remind_execute(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    days = int(callback.data.split("_")[1])
    inactive = get_inactive_users(days)
    if not inactive:
        try:
            await callback.message.edit_text(f"Нет неактивных за {days} дн.", reply_markup=stats_keyboard())
        except Exception:
            await callback.message.answer(f"Нет неактивных за {days} дн.", reply_markup=stats_keyboard())
        await callback.answer()
        return
    sent, failed = 0, 0
    for uid, un, count in inactive:
        target = get_effective_target(uid)
        text = f"🔔 Привет! Ты на {make_bar(count, target)}. Не бросай! Допришли скриншоты и жми /done."
        try:
            await bot.send_message(uid, text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    try:
        await callback.message.edit_text(f"🔔 Отправлено: {sent} | Не доставлено: {failed}", reply_markup=stats_keyboard())
    except Exception:
        await callback.message.answer(f"🔔 Отправлено: {sent} | Не доставлено: {failed}", reply_markup=stats_keyboard())
    await callback.answer("Готово!")


# ====================== АВТО-ОТЧЁТ ======================

async def auto_report_task():
    last = None
    while True:
        await asyncio.sleep(60)
        try:
            if get_setting("auto_report_enabled", "1") != "1":
                continue
            h = int(get_setting("auto_report_hour", "21"))
            now = datetime.now()
            if now.hour == h and now.minute == 0:
                ts = now.strftime("%Y-%m-%d")
                if last != ts:
                    last = ts
                    r = get_daily_report_text()
                    cursor.execute("INSERT INTO auto_reports (report_text) VALUES (?)", (r,))
                    conn.commit()
                    try:
                        await bot.send_message(ADMIN_ID, r)
                    except Exception as e:
                        logging.error(f"auto_report: {e}")
        except Exception as e:
            logging.error(f"auto_report_task: {e}")


# ====================== ЗАПУСК ======================

async def main():
    print("Бот запущен!")
    asyncio.create_task(auto_report_task())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
