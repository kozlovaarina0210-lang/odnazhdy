import asyncio
import logging
import os
import random
import re
import sqlite3
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.isdigit() else 0
DB_PATH = "stories.db"

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()

CATEGORIES = [
    "Работа и первые деньги",
    "Деньги и покупки",
    "Учёба и будущее",
    "Дружба и отношения",
    "Интернет и мошенники",
    "Решения и возможности",
    "Ошибки в общении",
    "Другое",
]
MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📚 Читать истории"), KeyboardButton(text="✍️ Рассказать свою")],
        [KeyboardButton(text="🎲 Случайная история"), KeyboardButton(text="🗂 Мои публикации")],
        [KeyboardButton(text="🔎 Найти по словам"), KeyboardButton(text="ℹ️ Правила и анонимность")],
    ],
    resize_keyboard=True,
)

class StoryForm(StatesGroup):
    category = State()
    title = State()
    happened = State()
    mistake = State()
    lesson = State()
    confirm = State()

class SearchForm(StatesGroup):
    query = State()

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS stories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            happened TEXT NOT NULL,
            mistake TEXT NOT NULL,
            lesson TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            published_at TEXT,
            helpful_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS reactions (
            user_id INTEGER NOT NULL,
            story_id INTEGER NOT NULL,
            PRIMARY KEY(user_id, story_id),
            FOREIGN KEY(story_id) REFERENCES stories(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id INTEGER NOT NULL,
            reporter_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            UNIQUE(story_id, reporter_id),
            FOREIGN KEY(story_id) REFERENCES stories(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            last_submission_at TEXT
        );
        """)

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def safe_text(text, max_len=1200):
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_len]

def contains_contact_details(text):
    # Basic safety net only; moderation must still check every submission.
    patterns = [
        r"[\w.+-]+@[\w-]+\.[\w.-]+",
        r"(?:\+?\d[\d\s()\-]{7,}\d)",
        r"https?://\S+",
        r"t\.me/\S+",
        r"@\w{4,}",
    ]
    return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)

def story_keyboard(story_id, user_id=None):
    buttons = [
        [InlineKeyboardButton(text="💚 Это было полезно", callback_data=f"helpful:{story_id}")],
        [InlineKeyboardButton(text="🔎 Похожие истории", callback_data=f"similar:{story_id}")],
        [InlineKeyboardButton(text="🚩 Пожаловаться", callback_data=f"report:{story_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def moderation_keyboard(story_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"approve:{story_id}")],
        [InlineKeyboardButton(text="✏️ Вернуть на исправление", callback_data=f"revise:{story_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{story_id}")],
    ])

def category_keyboard():
    rows = [[InlineKeyboardButton(text=cat, callback_data=f"cat:{i}")] for i, cat in enumerate(CATEGORIES)]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="cancel_form")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def format_story(row):
    lesson = row["lesson"] if row["lesson"] else "Пока не знаю, чему меня научила эта ситуация."
    return (
        f"<b>{row['category']}</b> · История № {row['id']:03d}\n\n"
        f"<b>{row['title']}</b>\n\n"
        f"<b>Что произошло</b>\n{row['happened']}\n\n"
        f"<b>В чём была ошибка</b>\n{row['mistake']}\n\n"
        f"<b>Что я понял</b>\n{lesson}\n\n"
        f"<i>История опубликована анонимно. Не осуждаем — делимся опытом.</i>"
    )

async def show_story(target, row):
    await target.answer(format_story(row), reply_markup=story_keyboard(row["id"]))

async def get_story(story_id, published_only=True):
    with db() as con:
        if published_only:
            return con.execute("SELECT * FROM stories WHERE id=? AND status='published'", (story_id,)).fetchone()
        return con.execute("SELECT * FROM stories WHERE id=?", (story_id,)).fetchone()

async def tell_author(bot, user_id, text):
    try:
        await bot.send_message(user_id, text)
    except Exception:
        pass

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Это <b>«Однажды я ошибся»</b> — анонимный архив человеческого опыта.\n\n"
        "Здесь не осуждают за ошибки. Здесь сохраняют опыт, который может пригодиться кому-то ещё.\n\n"
        "Все истории проходят модерацию перед публикацией.",
        reply_markup=MAIN_MENU
    )

@dp.message(Command("id"))
async def show_id(message: Message):
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>\nНе публикуй токен бота. Этот ID нужен владельцу для настройки модерации.")

@dp.message(F.text == "ℹ️ Правила и анонимность")
async def rules(message: Message):
    await message.answer(
        "<b>Правила и анонимность</b>\n\n"
        "• В публичной истории не показываются имя, username или ссылка на профиль автора.\n"
        "• Технически бот получает Telegram ID — он нужен для статусов, управления публикациями и защиты от спама.\n"
        "• Не указывай имена, телефоны, адреса, названия школ/работодателей и другие детали, по которым можно узнать человека.\n"
        "• Запрещены травля, угрозы, интимные материалы, раскрытие чужих секретов и обвинения с данными конкретных людей.\n"
        "• История появится в архиве только после проверки модератором.\n"
        "• Можно запросить удаление своей истории через «Мои публикации».\n\n"
        "Пожалуйста, не отправляй сведения, которые не хочешь хранить в базе бота."
    )

@dp.message(F.text == "✍️ Рассказать свою")
async def begin_story(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(StoryForm.category)
    await message.answer(
        "Расскажи о ситуации без имён и личных данных.\n"
        "Сначала выбери категорию. История не появится публично, пока модератор её не одобрит.",
        reply_markup=category_keyboard()
    )

@dp.callback_query(F.data.startswith("cat:"))
async def choose_category(call: CallbackQuery, state: FSMContext):
    if await state.get_state() != StoryForm.category.state:
        await call.answer("Сначала нажми «Рассказать свою».", show_alert=True)
        return
    idx = int(call.data.split(":")[1])
    if idx < 0 or idx >= len(CATEGORIES):
        await call.answer("Неизвестная категория.", show_alert=True)
        return
    await state.update_data(category=CATEGORIES[idx])
    await state.set_state(StoryForm.title)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("Придумай короткий заголовок (до 100 символов).")
    await call.answer()

@dp.callback_query(F.data == "cancel_form")
async def cancel_form(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Создание истории отменено.", reply_markup=MAIN_MENU)
    await call.answer()

@dp.message(StoryForm.title)
async def form_title(message: Message, state: FSMContext):
    value = safe_text(message.text, 100)
    if len(value) < 4:
        await message.answer("Заголовок слишком короткий. Напиши хотя бы 4 символа.")
        return
    if contains_contact_details(value):
        await message.answer("Похоже, здесь есть контактные данные или ссылка. Удали их и попробуй снова.")
        return
    await state.update_data(title=value)
    await state.set_state(StoryForm.happened)
    await message.answer("Что произошло? Опиши ситуацию своими словами (до 1200 символов).")

@dp.message(StoryForm.happened)
async def form_happened(message: Message, state: FSMContext):
    value = safe_text(message.text, 1200)
    if len(value) < 20:
        await message.answer("Добавь немного подробностей — нужно хотя бы 20 символов.")
        return
    if contains_contact_details(value):
        await message.answer("Похоже, в тексте есть контактные данные или ссылка. Удали их, а также имена и узнаваемые детали.")
        return
    await state.update_data(happened=value)
    await state.set_state(StoryForm.mistake)
    await message.answer("В чём, по твоему мнению, была ошибка? (до 800 символов)")

@dp.message(StoryForm.mistake)
async def form_mistake(message: Message, state: FSMContext):
    value = safe_text(message.text, 800)
    if len(value) < 5:
        await message.answer("Напиши немного подробнее — хотя бы 5 символов.")
        return
    if contains_contact_details(value):
        await message.answer("Удали контактные данные и ссылки, затем отправь текст ещё раз.")
        return
    await state.update_data(mistake=value)
    await state.set_state(StoryForm.lesson)
    await message.answer("Что ты понял после этого? Если пока не знаешь, напиши: «Пока не знаю».")

@dp.message(StoryForm.lesson)
async def form_lesson(message: Message, state: FSMContext):
    value = safe_text(message.text, 800)
    if not value:
        value = "Пока не знаю, чему меня научила эта ситуация."
    if contains_contact_details(value):
        await message.answer("Удали контактные данные и ссылки, затем отправь текст ещё раз.")
        return
    await state.update_data(lesson=value)
    data = await state.get_data()
    preview = (
        f"<b>Проверь свою историю</b>\n\n<b>{data['category']}</b>\n"
        f"<b>{data['title']}</b>\n\n<b>Что произошло</b>\n{data['happened']}\n\n"
        f"<b>В чём была ошибка</b>\n{data['mistake']}\n\n<b>Что я понял</b>\n{data['lesson']}\n\n"
        "После отправки историю проверит модератор."
    )
    await state.set_state(StoryForm.confirm)
    await message.answer(preview, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Отправить на модерацию", callback_data="submit_story")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_form")],
    ]))

@dp.callback_query(F.data == "submit_story")
async def submit_story(call: CallbackQuery, state: FSMContext, bot: Bot):
    if await state.get_state() != StoryForm.confirm.state:
        await call.answer("Эта форма уже закрыта.", show_alert=True)
        return
    data = await state.get_data()
    # One pending submission per user to reduce spam.
    with db() as con:
        pending = con.execute(
            "SELECT id FROM stories WHERE user_id=? AND status='pending'", (call.from_user.id,)
        ).fetchone()
        if pending:
            await call.answer("У тебя уже есть история на проверке.", show_alert=True)
            await state.clear()
            return
        cur = con.execute(
            "INSERT INTO stories(user_id,category,title,happened,mistake,lesson,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (call.from_user.id, data["category"], data["title"], data["happened"],
             data["mistake"], data["lesson"], "pending", now_iso())
        )
        story_id = cur.lastrowid
    await state.clear()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("Спасибо! История отправлена на модерацию. Она станет публичной только после одобрения.", reply_markup=MAIN_MENU)
    if ADMIN_ID:
        row = await get_story(story_id, published_only=False)
        try:
            await bot.send_message(
                ADMIN_ID,
                f"<b>Новая история на проверке · № {story_id:03d}</b>\n"
                f"Категория: {row['category']}\n"
                f"Заголовок: {row['title']}\n\n"
                f"<b>Что произошло</b>\n{row['happened']}\n\n"
                f"<b>Ошибка</b>\n{row['mistake']}\n\n"
                f"<b>Вывод</b>\n{row['lesson']}\n\n"
                f"<i>Внутренний ID автора: {row['user_id']} (не публиковать)</i>",
                reply_markup=moderation_keyboard(story_id)
            )
        except Exception:
            logging.exception("Не удалось отправить историю модератору")
    else:
        logging.warning("ADMIN_ID не настроен: история сохранена, но модератор не уведомлён.")
    await call.answer("Отправлено!")

@dp.callback_query(F.data.startswith("approve:"))
async def approve_story(call: CallbackQuery, bot: Bot):
    if not ADMIN_ID or call.from_user.id != ADMIN_ID:
        await call.answer("Эта кнопка доступна только модератору.", show_alert=True)
        return
    story_id = int(call.data.split(":")[1])
    with db() as con:
        row = con.execute("SELECT * FROM stories WHERE id=? AND status='pending'", (story_id,)).fetchone()
        if not row:
            await call.answer("История уже обработана или не найдена.", show_alert=True)
            return
        con.execute("UPDATE stories SET status='published', published_at=? WHERE id=?", (now_iso(), story_id))
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(f"История № {story_id:03d} опубликована.")
    await tell_author(bot, row["user_id"], f"Хорошие новости: твоя история № {story_id:03d} прошла модерацию и опубликована. Спасибо за вклад в архив!")
    await call.answer("Опубликовано")

@dp.callback_query(F.data.startswith("reject:"))
async def reject_story(call: CallbackQuery, bot: Bot):
    if not ADMIN_ID or call.from_user.id != ADMIN_ID:
        await call.answer("Эта кнопка доступна только модератору.", show_alert=True)
        return
    story_id = int(call.data.split(":")[1])
    with db() as con:
        row = con.execute("SELECT * FROM stories WHERE id=? AND status='pending'", (story_id,)).fetchone()
        if not row:
            await call.answer("История уже обработана или не найдена.", show_alert=True)
            return
        con.execute("UPDATE stories SET status='rejected' WHERE id=?", (story_id,))
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(f"История № {story_id:03d} отклонена.")
    await tell_author(bot, row["user_id"], f"История № {story_id:03d} не прошла модерацию. Если хочешь, можешь отправить новую версию. Пожалуйста, не включай личные данные других людей.")
    await call.answer("Отклонено")

@dp.callback_query(F.data.startswith("revise:"))
async def revise_story(call: CallbackQuery, bot: Bot):
    if not ADMIN_ID or call.from_user.id != ADMIN_ID:
        await call.answer("Эта кнопка доступна только модератору.", show_alert=True)
        return
    story_id = int(call.data.split(":")[1])
    with db() as con:
        row = con.execute("SELECT * FROM stories WHERE id=? AND status='pending'", (story_id,)).fetchone()
        if not row:
            await call.answer("История уже обработана или не найдена.", show_alert=True)
            return
        con.execute("UPDATE stories SET status='revision' WHERE id=?", (story_id,))
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(f"История № {story_id:03d} возвращена автору на исправление.")
    await tell_author(bot, row["user_id"], f"История № {story_id:03d} пока не опубликована. Пожалуйста, пересмотри её и отправь исправленную версию новой публикацией. Проверь, нет ли личных данных, узнаваемых подробностей или обвинений против конкретных людей.")
    await call.answer("Возвращено")

@dp.message(F.text == "📚 Читать истории")
async def read_menu(message: Message):
    with db() as con:
        counts = {r["category"]: r["n"] for r in con.execute(
            "SELECT category, COUNT(*) n FROM stories WHERE status='published' GROUP BY category"
        ).fetchall()}
    rows = [[InlineKeyboardButton(text=f"{cat} ({counts.get(cat, 0)})", callback_data=f"readcat:{i}")]
            for i, cat in enumerate(CATEGORIES)]
    rows.append([InlineKeyboardButton(text="📖 Все категории", callback_data="readall")])
    await message.answer("Выбери тему:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

async def send_story_list(target, category=None, search=None):
    sql = "SELECT * FROM stories WHERE status='published'"
    params = []
    if category:
        sql += " AND category=?"
        params.append(category)
    if search:
        sql += " AND (title LIKE ? OR happened LIKE ? OR mistake LIKE ? OR lesson LIKE ?)"
        term = f"%{search}%"
        params.extend([term, term, term, term])
    sql += " ORDER BY id DESC LIMIT 10"
    with db() as con:
        rows = con.execute(sql, params).fetchall()
    if not rows:
        await target.answer("Пока историй здесь нет. Можно стать одним из первых авторов 💚")
        return
    for row in rows:
        await show_story(target, row)

@dp.callback_query(F.data.startswith("readcat:"))
async def read_category(call: CallbackQuery):
    idx = int(call.data.split(":")[1])
    if idx < 0 or idx >= len(CATEGORIES):
        await call.answer()
        return
    await call.answer()
    await send_story_list(call.message, category=CATEGORIES[idx])

@dp.callback_query(F.data == "readall")
async def read_all(call: CallbackQuery):
    await call.answer()
    await send_story_list(call.message)

@dp.message(F.text == "🎲 Случайная история")
async def random_story(message: Message):
    with db() as con:
        row = con.execute("SELECT * FROM stories WHERE status='published' ORDER BY RANDOM() LIMIT 1").fetchone()
    if row:
        await show_story(message, row)
    else:
        await message.answer("Архив пока пуст. Можешь помочь ему вырасти — рассказать свою историю.")

@dp.message(F.text == "🔎 Найти по словам")
async def search_begin(message: Message, state: FSMContext):
    await state.set_state(SearchForm.query)
    await message.answer("Напиши слово или короткую фразу для поиска. Например: «первая работа».")

@dp.message(SearchForm.query)
async def search_run(message: Message, state: FSMContext):
    query = safe_text(message.text, 80)
    await state.clear()
    if len(query) < 2:
        await message.answer("Для поиска нужно хотя бы 2 символа.", reply_markup=MAIN_MENU)
        return
    await message.answer(f"Ищу истории по запросу: <b>{query}</b>")
    await send_story_list(message, search=query)

@dp.callback_query(F.data.startswith("helpful:"))
async def helpful(call: CallbackQuery):
    story_id = int(call.data.split(":")[1])
    with db() as con:
        row = con.execute("SELECT id FROM stories WHERE id=? AND status='published'", (story_id,)).fetchone()
        if not row:
            await call.answer("История недоступна.", show_alert=True)
            return
        exists = con.execute("SELECT 1 FROM reactions WHERE user_id=? AND story_id=?", (call.from_user.id, story_id)).fetchone()
        if exists:
            await call.answer("Ты уже отмечал(а) эту историю 💚")
            return
        con.execute("INSERT INTO reactions(user_id,story_id) VALUES(?,?)", (call.from_user.id, story_id))
        con.execute("UPDATE stories SET helpful_count=helpful_count+1 WHERE id=?", (story_id,))
        count = con.execute("SELECT helpful_count FROM stories WHERE id=?", (story_id,)).fetchone()["helpful_count"]
    await call.answer(f"Спасибо! Полезно отметили: {count}")

@dp.callback_query(F.data.startswith("report:"))
async def report_story(call: CallbackQuery, bot: Bot):
    story_id = int(call.data.split(":")[1])
    with db() as con:
        row = con.execute("SELECT id FROM stories WHERE id=? AND status='published'", (story_id,)).fetchone()
        if not row:
            await call.answer("История недоступна.", show_alert=True)
            return
        try:
            con.execute("INSERT INTO reports(story_id,reporter_id,created_at) VALUES(?,?,?)",
                        (story_id, call.from_user.id, now_iso()))
        except sqlite3.IntegrityError:
            await call.answer("Ты уже отправлял(а) жалобу на эту историю.")
            return
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, f"🚩 Жалоба на историю № {story_id:03d}.\nПроверь её в базе/архиве.")
        except Exception:
            pass
    await call.answer("Жалоба отправлена модератору. Спасибо.", show_alert=True)

@dp.callback_query(F.data.startswith("similar:"))
async def similar_stories(call: CallbackQuery):
    story_id = int(call.data.split(":")[1])
    row = await get_story(story_id)
    if not row:
        await call.answer("История недоступна.", show_alert=True)
        return
    await call.answer()
    await send_story_list(call.message, category=row["category"])

@dp.message(F.text == "🗂 Мои публикации")
async def my_stories(message: Message):
    with db() as con:
        rows = con.execute("SELECT * FROM stories WHERE user_id=? ORDER BY id DESC LIMIT 20",
                           (message.from_user.id,)).fetchall()
    if not rows:
        await message.answer("У тебя пока нет отправленных историй.")
        return
    status_names = {"pending": "на проверке", "published": "опубликована", "rejected": "отклонена", "revision": "нужно исправить"}
    for row in rows:
        status = status_names.get(row["status"], row["status"])
        text = f"<b>История № {row['id']:03d}</b> · {status}\n{row['title']}"
        buttons = []
        if row["status"] in ("pending", "published", "revision", "rejected"):
            buttons.append(InlineKeyboardButton(text="🗑 Удалить мою историю", callback_data=f"delete:{row['id']}"))
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None)

@dp.callback_query(F.data.startswith("delete:"))
async def delete_my_story(call: CallbackQuery):
    story_id = int(call.data.split(":")[1])
    with db() as con:
        row = con.execute("SELECT id FROM stories WHERE id=? AND user_id=?", (story_id, call.from_user.id)).fetchone()
        if not row:
            await call.answer("История не найдена в твоих публикациях.", show_alert=True)
            return
        con.execute("DELETE FROM stories WHERE id=? AND user_id=?", (story_id, call.from_user.id))
    await call.message.edit_text(f"История № {story_id:03d} удалена из архива.")
    await call.answer("Удалено")

@dp.message(Command("pending"))
async def pending_count(message: Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    with db() as con:
        rows = con.execute("SELECT id,category,title FROM stories WHERE status='pending' ORDER BY id").fetchall()
    if not rows:
        await message.answer("Очередь модерации пуста.")
        return
    await message.answer("Ожидают проверки:\n" + "\n".join(f"№ {r['id']:03d} · {r['category']} · {r['title']}" for r in rows))

async def main():
    if not TOKEN:
        raise SystemExit("Не найден BOT_TOKEN. Заполни файл .env по инструкции.")
    init_db()
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    logging.info("Бот запущен: @%s", me.username)
    if not ADMIN_ID:
        logging.warning("ADMIN_ID не настроен. Получи свой ID командой /id и внеси его в .env.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
