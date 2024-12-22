# ==================== Telegram Bot: Dating and Messaging ====================
# Этот бот предназначен для знакомств и обмена сообщениями между пользователями.
# Включает инлайн кнопки, логирование, поддержку медиа, управление интересами,
# систему рейтинга пользователей, алгоритм совпадений, блокировку и жалобы,
# уведомления, поддержку голосовых сообщений и видео, пользовательские профили с фотографиями,
# а также систему лайков и дизлайков.

import logging
import aiosqlite
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ConversationHandler,
    ContextTypes,
    CallbackContext,
)
import asyncio
import time
from collections import defaultdict
import re

# ==================== Установка nest_asyncio ====================
# Это необходимо, если вы запускаете скрипт в среде с уже запущенным циклом событий.
try:
    import nest_asyncio
    nest_asyncio.apply()
except ImportError:
    pass  # Если библиотека не установлена и не требуется, продолжайте

# ==================== Токен Бота ====================
TOKEN = "7143620102:AAGhjfFixqiJE6xGAzTo9aBns_NA-_th95E"  # Замените на ваш новый токен, полученный от @BotFather

# ==================== Настройка Логирования ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,  # Устанавливаем общий уровень логирования на INFO
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Подавление логов от 'httpx'
logging.getLogger("httpx").setLevel(logging.WARNING)

# ==================== Константы для состояний ====================
REGISTER = 1
SEND_MESSAGE = 2
SET_MODE = 3
SELECT_RECIPIENT = 4
SET_USERNAME_VISIBILITY = 5
REPLY_MESSAGE = 6
SET_PROFILE_PHOTO = 10
TOGGLE_PROFILE_VISIBILITY = 7

# ==================== Админ ID ====================
ADMIN_ID = 1753361154  # ID администратора, замените на ваш ID

# ==================== Путь к Базе Данных ====================
DB_PATH = 'users.db'

# ==================== Инициализация Базы Данных ====================
async def init_db():
    """Создает таблицы users, messages, rankings, matches, blocks и likes, если они еще не существуют."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")  # Включение поддержки внешних ключей
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                interests TEXT,
                anonymous_mode BOOLEAN DEFAULT 0,
                show_username BOOLEAN DEFAULT 1,
                profile_photo TEXT,
                profile_visible BOOLEAN DEFAULT 1
            )
        ''')
        # Проверка и добавление столбца profile_visible, если его нет
        cursor = await db.execute("PRAGMA table_info(users);")
        columns = await cursor.fetchall()
        column_names = [column[1] for column in columns]
        if 'profile_visible' not in column_names:
            await db.execute("ALTER TABLE users ADD COLUMN profile_visible BOOLEAN DEFAULT 1;")
            await db.commit()
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                receiver_id INTEGER NOT NULL,
                message TEXT,
                media_type TEXT,
                media_id TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(sender_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY(receiver_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS rankings (
                user_id INTEGER PRIMARY KEY,
                sent_messages INTEGER DEFAULT 0,
                received_messages INTEGER DEFAULT 0,
                likes INTEGER DEFAULT 0,
                dislikes INTEGER DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS matches (
                user1_id INTEGER,
                user2_id INTEGER,
                match_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user1_id, user2_id),
                FOREIGN KEY(user1_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY(user2_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS blocks (
                blocked_user_id INTEGER,
                blocked_by INTEGER,
                block_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (blocked_user_id, blocked_by),
                FOREIGN KEY(blocked_user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY(blocked_by) REFERENCES users(user_id) ON DELETE CASCADE
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS likes (
                liker_id INTEGER,
                liked_id INTEGER,
                like_type TEXT,  -- 'like' или 'dislike'
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (liker_id, liked_id),
                FOREIGN KEY(liker_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY(liked_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        ''')
        await db.commit()
    logger.info("База данных инициализирована.")

# ==================== Функции для Валидации и Фильтрации ====================
def is_valid_message(text: str) -> bool:
    """Проверяет, что сообщение не содержит запрещенных слов."""
    forbidden_words = ['spam', 'malware', 'phishing']
    if text and not text.strip():
        return False
    if text:
        for word in forbidden_words:
            if re.search(rf'\b{word}\b', text, re.IGNORECASE):
                return False
    return True

# ==================== Ограничение Количества Сообщений (Троттлинг) ====================
message_timestamps = defaultdict(list)
RATE_LIMIT = 20  # Максимум 20 сообщений
TIME_WINDOW = 60  # За последние 60 секунд

def check_rate_limit(user_id: int) -> bool:
    """Проверяет, не превысил ли пользователь лимит сообщений."""
    current_time = time.time()
    # Очищаем старые временные метки
    message_timestamps[user_id] = [
        timestamp for timestamp in message_timestamps[user_id]
        if current_time - timestamp < TIME_WINDOW
    ]
    if len(message_timestamps[user_id]) >= RATE_LIMIT:
        return False
    message_timestamps[user_id].append(current_time)
    return True

# ==================== Команда /start ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начальная команда, запускающая процесс регистрации."""
    user = update.message.from_user
    logger.info(f"Пользователь {user.id} инициировал /start.")
    
    # Проверка, зарегистрирован ли пользователь уже
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user.id,))
        result = await cursor.fetchone()
        if result:
            await update.message.reply_text(
                f"🎉 Вы уже зарегистрированы как {result[1]}.\n\n"
                "✨ Доступные команды:\n"
                "/send - Отправить сообщение другому пользователю\n"
                "/send_anonymous - Отправить анонимное сообщение\n"
                "/set_mode - Изменить режим отправки сообщений\n"
                "/set_username_visibility - Настроить видимость вашего юзернейма\n"
                "/toggle_profile_visibility - Настроить видимость вашего профиля\n"
                "/rankings - Посмотреть топ пользователей\n"
                "/myrank - Узнать свой ранг\n"
                "/help - Справка по командам"
            )
            return ConversationHandler.END
    
    # Запрос юзернейма
    await update.message.reply_text(
        "👋 Привет! Я бот для знакомств и обмена сообщениями.\n\n"
        "Для начала отправьте свой юзернейм (например, @myusername)."
    )
    return REGISTER

# ==================== Обработка Регистрации ====================
async def register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает отправленный юзернейм и регистрирует пользователя."""
    user = update.message.from_user
    username = (update.message.text or "").strip()
    
    if not username.startswith('@'):
        await update.message.reply_text("❌ Пожалуйста, введите валидный юзернейм (начинается с @).")
        return REGISTER
    
    # Проверка уникальности юзернейма
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cursor = await db.execute("SELECT * FROM users WHERE username = ?", (username,))
        result = await cursor.fetchone()
        if result:
            await update.message.reply_text("⚠️ Этот юзернейм уже зарегистрирован. Пожалуйста, выберите другой.")
            return REGISTER
    
        # Регистрация пользователя
        try:
            await db.execute("INSERT INTO users (user_id, username) VALUES (?, ?)", (user.id, username))
            await db.execute("INSERT INTO rankings (user_id) VALUES (?)", (user.id,))
            await db.commit()
            await update.message.reply_text(
                f"✅ Вы успешно зарегистрированы как {username}.\n\n"
                "✨ Теперь вы можете отправлять сообщения другим пользователям.\n"
                "✨ Чтобы изменить режим отправки сообщений, используйте команду:\n"
                "/set_mode\n"
                "✨ Чтобы добавить свои интересы, используйте команду:\n"
                "/add_interests интерес1, интерес2\n"
                "✨ Чтобы настроить видимость вашего юзернейма, используйте команду:\n"
                "/set_username_visibility\n"
                "✨ Чтобы настроить видимость вашего профиля, используйте команду:\n"
                "/toggle_profile_visibility\n"
                "✨ Чтобы установить фотографию профиля, используйте команду:\n"
                "/set_profile_photo"
            )
            logger.info(f"Пользователь {user.id} зарегистрирован как {username}.")
            return ConversationHandler.END
        except Exception as e:
            logger.error(f"Ошибка при регистрации пользователя {user.id}: {e}")
            await update.message.reply_text("❌ Произошла ошибка при регистрации. Пожалуйста, попробуйте позже.")
            return ConversationHandler.END

# ==================== Получение Инлайн Клавиатуры Пользователей ====================
async def get_users_keyboard(exclude_user_id: int) -> InlineKeyboardMarkup:
    """Создает инлайн клавиатуру со списком зарегистрированных пользователей."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cursor = await db.execute("""
            SELECT user_id, username, show_username 
            FROM users 
            WHERE user_id != ? 
              AND profile_visible = 1
              AND user_id NOT IN (
                  SELECT blocked_user_id FROM blocks WHERE blocked_by = ?
              )
              AND ? NOT IN (
                  SELECT blocked_by FROM blocks WHERE blocked_user_id = ?
              )
        """, (exclude_user_id, exclude_user_id, exclude_user_id, exclude_user_id))
        users = await cursor.fetchall()
    
    if not users:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🔕 Нет доступных пользователей", callback_data='no_users')]])
    
    buttons = []
    for user in users:
        user_id, username, show_username = user
        display_name = username if show_username else f"Пользователь {user_id}"
        buttons.append([
            InlineKeyboardButton(display_name, callback_data=f"send_{user_id}"),
            InlineKeyboardButton("🔍 Посмотреть профиль", callback_data=f"view_{user_id}")
        ])
    
    return InlineKeyboardMarkup(buttons)

# ==================== Обработчик Инлайн Кнопок для Отправки Сообщений ====================
async def handle_send_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает нажатия инлайн кнопок при отправке сообщений."""
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    
    await query.answer()
    
    data = query.data
    user_id = query.from_user.id
    
    if data.startswith('send_'):
        # Выбор получателя для отправки сообщения
        target_user_id = int(data.split('send_')[1])
        context.user_data['target_user_id'] = target_user_id
        await query.message.reply_text(f"✍️ Введите ваше сообщение или отправьте медиа (фото, видео, документ):")
        return SEND_MESSAGE
    else:
        await query.message.reply_text("❌ Некорректный выбор получателя.")
        return ConversationHandler.END

# ==================== Обработчик Инлайн Кнопок для Просмотра Профиля ====================
async def handle_view_profile_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия инлайн кнопок для просмотра профиля."""
    query = update.callback_query
    if query is None:
        return
    
    await query.answer()
    
    data = query.data
    if not data.startswith('view_'):
        await query.message.reply_text("❌ Некорректный запрос.")
        return
    
    target_user_id = int(data.split('view_')[1])
    
    # Получение информации о целевом пользователе
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cursor = await db.execute("SELECT username, interests, show_username, profile_photo FROM users WHERE user_id = ?", (target_user_id,))
        target_info = await cursor.fetchone()
        if not target_info:
            await query.message.reply_text("❌ Пользователь не найден или его профиль скрыт.")
            return
        target_username, interests, show_username, profile_photo = target_info
        if not show_username:
            display_name = f"Пользователь {target_user_id}"
        else:
            display_name = target_username
        interests = interests if interests else "Не указаны"
        photo_text = f"📸 Фото профиля: Да" if profile_photo else "📸 Фото профиля: Нет"
    
    profile_message = (
        f"👤 <b>Профиль пользователя:</b>\n\n"
        f"🔹 <b>Юзернейм:</b> {display_name}\n"
        f"🔹 <b>ID:</b> {target_user_id}\n"
        f"🔹 <b>Интересы:</b> {interests}\n"
        f"{photo_text}\n\n"
        f"📬 Вы можете отправить этому пользователю сообщение с помощью команды /send или воспользоваться кнопками ниже."
    )
    
    # Инлайн кнопки для отправки сообщения или закрытия профиля
    keyboard = [
        [InlineKeyboardButton("✉️ Отправить сообщение", callback_data=f"send_{target_user_id}")],
        [InlineKeyboardButton("🔒 Закрыть профиль", callback_data=f"hide_profile_{target_user_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if profile_photo:
        # Используем file_id напрямую
        try:
            await query.message.reply_photo(
                photo=profile_photo,
                caption=profile_message,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке фото профиля пользователю {target_user_id}: {e}")
            # Отправляем сообщение без фото
            await query.message.reply_text(profile_message, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    else:
        await query.message.reply_text(profile_message, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    
    logger.info(f"Пользователь {query.from_user.id} просмотрел профиль пользователя {target_user_id}.")

# ==================== Обработчик Закрытия Профиля из Просмотра ====================
async def handle_hide_profile_from_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Позволяет пользователю закрыть профиль при просмотре другим пользователем."""
    query = update.callback_query
    if query is None:
        return
    
    await query.answer()
    
    data = query.data
    if not data.startswith('hide_profile_'):
        await query.message.reply_text("❌ Некорректный запрос.")
        return
    
    target_user_id = int(data.split('hide_profile_')[1])
    user_id = query.from_user.id
    
    if target_user_id != user_id:
        await query.message.reply_text("❌ Вы можете изменять только свой профиль.")
        return
    
    # Скрытие профиля
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("UPDATE users SET profile_visible = 0 WHERE user_id = ?", (user_id,))
        await db.commit()
    
    await query.message.reply_text("🔒 Ваш профиль теперь скрыт от других пользователей.")
    logger.info(f"Пользователь {user_id} скрыл свой профиль.")
    
    # Обновление интерфейса или меню пользователя, если необходимо

# ==================== Обработчик Инлайн Кнопок для Установки Режима ====================
async def handle_set_mode_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия инлайн кнопок при установке режима отправки сообщений."""
    query = update.callback_query
    if query is None:
        return
    
    await query.answer()
    
    data = query.data
    user = query.from_user
    
    if data == 'mode_anonymous':
        await set_mode(update, context, anonymous=True)
    elif data == 'mode_non_anonymous':
        await set_mode(update, context, anonymous=False)
    else:
        await query.message.reply_text("❌ Некорректный выбор режима.")
    
    return ConversationHandler.END

# ==================== Установка Режима Отправки Сообщений ====================
async def set_mode(update: Update, context: ContextTypes.DEFAULT_TYPE, anonymous: bool) -> None:
    """Устанавливает режим отправки сообщений для пользователя."""
    query = update.callback_query
    user = update.effective_user
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("UPDATE users SET anonymous_mode = ? WHERE user_id = ?", (int(anonymous), user.id))
        await db.commit()
    
    mode_text = "🕶️ Анонимный" if anonymous else "📝 Неанонимный"
    await query.message.reply_text(f"✨ Режим отправки сообщений установлен на: **{mode_text}**", parse_mode=ParseMode.MARKDOWN)
    logger.info(f"Пользователь {user.id} установил режим отправки на: {mode_text}")

# ==================== Команда /set_username_visibility ====================
async def set_username_visibility_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Команда /set_username_visibility для настройки видимости юзернейма."""
    keyboard = [
        [InlineKeyboardButton("✅ Показывать мой юзернейм", callback_data='show_username')],
        [InlineKeyboardButton("❌ Скрыть мой юзернейм", callback_data='hide_username')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("✨ Хотите ли вы, чтобы ваш юзернейм отображался в списке пользователей?", reply_markup=reply_markup)
    return SET_USERNAME_VISIBILITY

# ==================== Обработчик Инлайн Кнопок для Видимости Юзернейма ====================
async def handle_username_visibility_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия инлайн кнопок при настройке видимости юзернейма."""
    query = update.callback_query
    if query is None:
        return
    
    await query.answer()
    
    data = query.data
    user = query.from_user
    
    if data == 'show_username':
        show = True
    elif data == 'hide_username':
        show = False
    else:
        await query.message.reply_text("❌ Некорректный выбор.")
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("UPDATE users SET show_username = ? WHERE user_id = ?", (int(show), user.id))
        await db.commit()
    
    status = "✅ Ваш юзернейм теперь отображается в списке пользователей." if show else "❌ Ваш юзернейм теперь скрыт от других пользователей."
    await query.message.reply_text(status)
    logger.info(f"Пользователь {user.id} установил show_username = {show}.")

# ==================== Команда /toggle_profile_visibility ====================
async def toggle_profile_visibility_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Команда /toggle_profile_visibility для настройки видимости профиля."""
    user = update.message.from_user
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cursor = await db.execute("SELECT profile_visible FROM users WHERE user_id = ?", (user.id,))
        result = await cursor.fetchone()
        if not result:
            await update.message.reply_text("❌ Пожалуйста, сначала зарегистрируйтесь с помощью /start.")
            return ConversationHandler.END
        profile_visible = bool(result[0])
    
    if profile_visible:
        # Профиль видим, предлагаем скрыть
        keyboard = [
            [InlineKeyboardButton("🔒 Скрыть профиль", callback_data='hide_profile')],
            [InlineKeyboardButton("❌ Отмена", callback_data='cancel')]
        ]
        status = "✨ Ваш профиль в данный момент **видим**. Хотите ли вы его скрыть?"
    else:
        # Профиль скрыт, предлагаем показать
        keyboard = [
            [InlineKeyboardButton("🔓 Показать профиль", callback_data='show_profile')],
            [InlineKeyboardButton("❌ Отмена", callback_data='cancel')]
        ]
        status = "✨ Ваш профиль в данный момент **скрыт**. Хотите ли вы его показать?"
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(status, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
    return TOGGLE_PROFILE_VISIBILITY

# ==================== Обработчик Инлайн Кнопок для Настройки Видимости Профиля ====================
async def handle_toggle_profile_visibility_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает нажатия инлайн кнопок при настройке видимости профиля."""
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    
    await query.answer()
    
    data = query.data
    user = query.from_user
    
    if data == 'hide_profile':
        new_visibility = 0
        status = "🔒 Ваш профиль теперь скрыт от других пользователей."
    elif data == 'show_profile':
        new_visibility = 1
        status = "🔓 Ваш профиль теперь видим для других пользователей."
    elif data == 'cancel':
        await query.message.reply_text("❌ Процесс отменен.")
        return ConversationHandler.END
    else:
        await query.message.reply_text("❌ Некорректный выбор.")
        return ConversationHandler.END
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("UPDATE users SET profile_visible = ? WHERE user_id = ?", (new_visibility, user.id))
        await db.commit()
    
    await query.message.reply_text(status)
    logger.info(f"Пользователь {user.id} изменил видимость профиля на {new_visibility}.")
    return ConversationHandler.END

# ==================== Команда /send ====================
async def send_message_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Команда /send для отправки сообщений."""
    # Запрашиваем выбор получателя
    await update.message.reply_text(
        "📬 Пожалуйста, выберите получателя:",
        reply_markup=await get_users_keyboard(exclude_user_id=update.message.from_user.id)
    )
    return SELECT_RECIPIENT

# ==================== Команда /send_anonymous ====================
async def send_anonymous_message_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Команда /send_anonymous для отправки анонимных сообщений."""
    # Запрашиваем выбор получателя
    await update.message.reply_text(
        "📬 Пожалуйста, выберите получателя:",
        reply_markup=await get_users_keyboard(exclude_user_id=update.message.from_user.id)
    )
    # Устанавливаем флаг анонимности
    context.user_data['send_anonymous'] = True
    return SELECT_RECIPIENT

# ==================== Обработка Отправки Сообщений ====================
async def send_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отправляет сообщение выбранному пользователю в соответствии с режимом."""
    user = update.message.from_user
    # Извлечение текста из сообщения или подписи (caption)
    message_text = (update.message.text or update.message.caption or "").strip()
    
    # Определение наличия медиа
    has_media = False
    if update.message.photo or update.message.video or update.message.document or update.message.voice or update.message.video_note:
        has_media = True
    
    if not message_text and not has_media:
        await update.message.reply_text("❌ Ваше сообщение пусто. Пожалуйста, введите текст или отправьте медиа.")
        return SEND_MESSAGE
    
    if message_text:
        if not is_valid_message(message_text):
            await update.message.reply_text("❌ Ваше сообщение содержит запрещенные слова или является пустым.")
            return SEND_MESSAGE
    
    target_user_id = context.user_data.get('target_user_id')
    if not target_user_id:
        await update.message.reply_text("❌ Произошла ошибка при выборе получателя.")
        return ConversationHandler.END
    
    # Проверка лимита сообщений
    if not check_rate_limit(user.id):
        await update.message.reply_text("⚠️ Вы превысили лимит отправки сообщений. Попробуйте позже.")
        return ConversationHandler.END
    
    # Получение режима отправки (анонимный или неанонимный)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cursor = await db.execute("SELECT anonymous_mode, username FROM users WHERE user_id = ?", (user.id,))
        user_mode = await cursor.fetchone()
        if not user_mode:
            await update.message.reply_text("❌ Пожалуйста, сначала зарегистрируйтесь с помощью /start.")
            return ConversationHandler.END
        anonymous = bool(user_mode[0])
        username = user_mode[1]
    
    # Получение информации о получателе
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT username, show_username FROM users WHERE user_id = ?", (target_user_id,))
        target_info = await cursor.fetchone()
        if not target_info:
            await update.message.reply_text("❌ Пользователь с таким ID не найден или не зарегистрирован.")
            return ConversationHandler.END
        target_username, show_username = target_info
    
    # Проверка блокировки: Проверяем, заблокирован ли отправитель получателем
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT * FROM blocks WHERE blocked_user_id = ? AND blocked_by = ?", (user.id, target_user_id))
        is_blocked = await cursor.fetchone()
        if is_blocked:
            await update.message.reply_text("❌ Вы не можете отправлять сообщения этому пользователю, так как он вас заблокировал.")
            return ConversationHandler.END
    
    # Определение типа медиа
    media_type = None
    media_id = None
    if update.message.photo:
        media_type = 'photo'
        media_id = update.message.photo[-1].file_id  # Самое высокое качество фото
    elif update.message.document:
        media_type = 'document'
        media_id = update.message.document.file_id
    elif update.message.video:
        media_type = 'video'
        media_id = update.message.video.file_id
    elif update.message.voice:
        media_type = 'voice'
        media_id = update.message.voice.file_id
    elif update.message.video_note:
        media_type = 'video_note'
        media_id = update.message.video_note.file_id
    # Добавьте другие типы медиа по необходимости
    
    # Определение режима отправки: анонимный или нет
    # Если команда /send_anonymous была использована, использовать анонимный режим
    # Иначе, использовать режим из базы данных
    send_anonymous = context.user_data.get('send_anonymous', False)
    if send_anonymous:
        anonymous = True
        context.user_data['send_anonymous'] = False  # Сброс флага
    
    # Определение display_name
    if anonymous:
        display_name = "🕶️ Аноним"
    else:
        display_name = username or "Пользователь"
    
    # Отправка сообщения целевому пользователю
    try:
        if media_type and media_id:
            if media_type == 'photo':
                sent_message = await context.bot.send_photo(
                    chat_id=target_user_id,
                    photo=media_id,
                    caption=f"<b>📩 Сообщение от {display_name}:</b>\n\n{message_text}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✉️ Ответить", callback_data=f"reply_{user.id}")]
                    ])
                )
            elif media_type == 'document':
                sent_message = await context.bot.send_document(
                    chat_id=target_user_id,
                    document=media_id,
                    caption=f"<b>📩 Сообщение от {display_name}:</b>\n\n{message_text}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✉️ Ответить", callback_data=f"reply_{user.id}")]
                    ])
                )
            elif media_type == 'video':
                sent_message = await context.bot.send_video(
                    chat_id=target_user_id,
                    video=media_id,
                    caption=f"<b>📩 Сообщение от {display_name}:</b>\n\n{message_text}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✉️ Ответить", callback_data=f"reply_{user.id}")]
                    ])
                )
            elif media_type == 'voice':
                sent_message = await context.bot.send_voice(
                    chat_id=target_user_id,
                    voice=media_id,
                    caption=f"<b>📩 Голосовое сообщение от {display_name}:</b>\n\n{message_text}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✉️ Ответить", callback_data=f"reply_{user.id}")]
                    ])
                )
            elif media_type == 'video_note':
                sent_message = await context.bot.send_video_note(
                    chat_id=target_user_id,
                    video_note=media_id,
                    caption=f"<b>📩 Видео сообщение от {display_name}:</b>\n\n{message_text}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✉️ Ответить", callback_data=f"reply_{user.id}")]
                    ])
                )
            # Добавьте обработку других типов медиа
        else:
            sent_message = await context.bot.send_message(
                chat_id=target_user_id,
                text=f"<b>📩 Сообщение от {display_name}:</b>\n\n{message_text}",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✉️ Ответить", callback_data=f"reply_{user.id}")]
                ])
            )
        
        # Сохранение сообщения в базе данных
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            await db.execute(
                "INSERT INTO messages (sender_id, receiver_id, message, media_type, media_id) VALUES (?, ?, ?, ?, ?)",
                (user.id, target_user_id, message_text, media_type, media_id)
            )
            # Обновление рейтинга: увеличение счетчика отправленных сообщений
            await db.execute(
                "UPDATE rankings SET sent_messages = sent_messages + 1 WHERE user_id = ?",
                (user.id,)
            )
            # Обновление рейтинга получателя: увеличение счетчика полученных сообщений
            await db.execute(
                "UPDATE rankings SET received_messages = received_messages + 1 WHERE user_id = ?",
                (target_user_id,)
            )
            await db.commit()
        
        await update.message.reply_text(f"✅ Сообщение успешно отправлено пользователю {target_username}.")
        logger.info(f"Пользователь {user.id} отправил сообщение пользователю {target_username} ({target_user_id}) анонимно={anonymous}.")
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения от {user.id} к {target_user_id}: {e}")
        await update.message.reply_text("❌ Произошла ошибка при отправке сообщения. Пожалуйста, попробуйте позже.")
        return ConversationHandler.END

# ==================== Команда /reply ====================
async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Команда /reply для ответа на последнее полученное сообщение."""
    user = update.message.from_user
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('''
            SELECT sender_id, message FROM messages
            WHERE receiver_id = ?
            ORDER BY timestamp DESC
            LIMIT 1
        ''', (user.id,))
        last_message = await cursor.fetchone()
        if not last_message:
            await update.message.reply_text("❌ У вас нет сообщений для ответа.")
            return ConversationHandler.END
        sender_id, message = last_message
        context.user_data['reply_to_user_id'] = sender_id
        await update.message.reply_text("✉️ Введите ваше ответное сообщение или отправьте медиа (фото, видео, документ):")
        return REPLY_MESSAGE

# ==================== Обработка Ответа на Сообщение ====================
async def handle_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отправляет ответное сообщение пользователю."""
    user = update.message.from_user
    # Извлечение текста из сообщения или подписи (caption)
    message_text = (update.message.text or update.message.caption or "").strip()
    
    # Определение наличия медиа
    has_media = False
    if update.message.photo or update.message.video or update.message.document or update.message.voice or update.message.video_note:
        has_media = True
    
    if not message_text and not has_media:
        await update.message.reply_text("❌ Ваше сообщение пусто. Пожалуйста, введите текст или отправьте медиа.")
        return REPLY_MESSAGE
    
    if message_text:
        if not is_valid_message(message_text):
            await update.message.reply_text("❌ Ваше сообщение содержит запрещенные слова или является пустым.")
            return REPLY_MESSAGE
    
    target_user_id = context.user_data.get('reply_to_user_id')
    if not target_user_id:
        await update.message.reply_text("❌ Произошла ошибка при выборе получателя.")
        return ConversationHandler.END
    
    # Проверка лимита сообщений
    if not check_rate_limit(user.id):
        await update.message.reply_text("⚠️ Вы превысили лимит отправки сообщений. Попробуйте позже.")
        return ConversationHandler.END
    
    # Получение режима отправки
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cursor = await db.execute("SELECT anonymous_mode, username FROM users WHERE user_id = ?", (user.id,))
        user_mode = await cursor.fetchone()
        if not user_mode:
            await update.message.reply_text("❌ Пожалуйста, сначала зарегистрируйтесь с помощью /start.")
            return ConversationHandler.END
        anonymous = bool(user_mode[0])
        username = user_mode[1]
    
    # Получение информации о получателе
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT username, show_username FROM users WHERE user_id = ?", (target_user_id,))
        target_info = await cursor.fetchone()
        if not target_info:
            await update.message.reply_text("❌ Пользователь с таким ID не найден или не зарегистрирован.")
            return ConversationHandler.END
        target_username, show_username = target_info
    
    # Проверка блокировки: Проверяем, заблокирован ли отправитель получателем
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT * FROM blocks WHERE blocked_user_id = ? AND blocked_by = ?", (user.id, target_user_id))
        is_blocked = await cursor.fetchone()
        if is_blocked:
            await update.message.reply_text("❌ Вы не можете отправлять сообщения этому пользователю, так как он вас заблокировал.")
            return ConversationHandler.END
    
    # Определение типа медиа
    media_type = None
    media_id = None
    if update.message.photo:
        media_type = 'photo'
        media_id = update.message.photo[-1].file_id  # Самое высокое качество фото
    elif update.message.document:
        media_type = 'document'
        media_id = update.message.document.file_id
    elif update.message.video:
        media_type = 'video'
        media_id = update.message.video.file_id
    elif update.message.voice:
        media_type = 'voice'
        media_id = update.message.voice.file_id
    elif update.message.video_note:
        media_type = 'video_note'
        media_id = update.message.video_note.file_id
    # Добавьте другие типы медиа по необходимости
    
    # Определение режима отправки: анонимный или нет
    # Читаем из команды /send_anonymous, если установлен
    send_anonymous = context.user_data.get('send_anonymous', False)
    if send_anonymous:
        anonymous = True
        context.user_data['send_anonymous'] = False  # Сброс флага
    
    # Определение display_name
    if anonymous:
        display_name = "🕶️ Аноним"
    else:
        display_name = username or "Пользователь"
    
    # Отправка ответного сообщения целевому пользователю
    try:
        if media_type and media_id:
            if media_type == 'photo':
                sent_message = await context.bot.send_photo(
                    chat_id=target_user_id,
                    photo=media_id,
                    caption=f"<b>📩 Ответ от {display_name}:</b>\n\n{message_text}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✉️ Ответить", callback_data=f"reply_{user.id}")]
                    ])
                )
            elif media_type == 'document':
                sent_message = await context.bot.send_document(
                    chat_id=target_user_id,
                    document=media_id,
                    caption=f"<b>📩 Ответ от {display_name}:</b>\n\n{message_text}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✉️ Ответить", callback_data=f"reply_{user.id}")]
                    ])
                )
            elif media_type == 'video':
                sent_message = await context.bot.send_video(
                    chat_id=target_user_id,
                    video=media_id,
                    caption=f"<b>📩 Ответ от {display_name}:</b>\n\n{message_text}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✉️ Ответить", callback_data=f"reply_{user.id}")]
                    ])
                )
            elif media_type == 'voice':
                sent_message = await context.bot.send_voice(
                    chat_id=target_user_id,
                    voice=media_id,
                    caption=f"<b>📩 Голосовое сообщение от {display_name}:</b>\n\n{message_text}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✉️ Ответить", callback_data=f"reply_{user.id}")]
                    ])
                )
            elif media_type == 'video_note':
                sent_message = await context.bot.send_video_note(
                    chat_id=target_user_id,
                    video_note=media_id,
                    caption=f"<b>📩 Видео сообщение от {display_name}:</b>\n\n{message_text}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✉️ Ответить", callback_data=f"reply_{user.id}")]
                    ])
                )
            # Добавьте обработку других типов медиа
        else:
            sent_message = await context.bot.send_message(
                chat_id=target_user_id,
                text=f"<b>📩 Ответ от {display_name}:</b>\n\n{message_text}",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✉️ Ответить", callback_data=f"reply_{user.id}")]
                ])
            )
        
        # Сохранение сообщения в базе данных
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            await db.execute(
                "INSERT INTO messages (sender_id, receiver_id, message, media_type, media_id) VALUES (?, ?, ?, ?, ?)",
                (user.id, target_user_id, message_text, media_type, media_id)
            )
            # Обновление рейтинга: увеличение счетчика отправленных сообщений
            await db.execute(
                "UPDATE rankings SET sent_messages = sent_messages + 1 WHERE user_id = ?",
                (user.id,)
            )
            # Обновление рейтинга получателя: увеличение счетчика полученных сообщений
            await db.execute(
                "UPDATE rankings SET received_messages = received_messages + 1 WHERE user_id = ?",
                (target_user_id,)
            )
            await db.commit()
        
        await update.message.reply_text(f"✅ Ответ успешно отправлен пользователю {target_username}.")
        logger.info(f"Пользователь {user.id} ответил пользователю {target_username} ({target_user_id}) анонимно={anonymous}.")
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Ошибка при отправке ответа от {user.id} к {target_user_id}: {e}")
        await update.message.reply_text("❌ Произошла ошибка при отправке ответа. Пожалуйста, попробуйте позже.")
        return ConversationHandler.END

# ==================== Обработчик Инлайн Кнопок для Ответа ====================
async def handle_reply_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает нажатия кнопки 'Ответить' на полученном сообщении."""
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    
    await query.answer()
    
    data = query.data
    if not data.startswith('reply_'):
        await query.message.reply_text("❌ Некорректный формат ответа.")
        return ConversationHandler.END
    
    target_user_id = int(data.split('reply_')[1])
    context.user_data['reply_to_user_id'] = target_user_id
    await query.message.reply_text("✉️ Введите ваше ответное сообщение или отправьте медиа (фото, видео, документ):")
    return REPLY_MESSAGE

# ==================== Команда /set_mode ====================
async def set_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Команда /set_mode для установки режима отправки сообщений."""
    # Запрашиваем выбор режима через инлайн кнопки
    keyboard = [
        [InlineKeyboardButton("📝 Неанонимный", callback_data='mode_non_anonymous')],
        [InlineKeyboardButton("🕶️ Анонимный", callback_data='mode_anonymous')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("✨ Выберите режим отправки сообщений:", reply_markup=reply_markup)
    return SET_MODE

# ==================== Команда /toggle_profile_visibility ====================
async def toggle_profile_visibility_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Команда /toggle_profile_visibility для настройки видимости профиля."""
    user = update.message.from_user
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cursor = await db.execute("SELECT profile_visible FROM users WHERE user_id = ?", (user.id,))
        result = await cursor.fetchone()
        if not result:
            await update.message.reply_text("❌ Пожалуйста, сначала зарегистрируйтесь с помощью /start.")
            return ConversationHandler.END
        profile_visible = bool(result[0])
    
    if profile_visible:
        # Профиль видим, предлагаем скрыть
        keyboard = [
            [InlineKeyboardButton("🔒 Скрыть профиль", callback_data='hide_profile')],
            [InlineKeyboardButton("❌ Отмена", callback_data='cancel')]
        ]
        status = "✨ Ваш профиль в данный момент **видим**. Хотите ли вы его скрыть?"
    else:
        # Профиль скрыт, предлагаем показать
        keyboard = [
            [InlineKeyboardButton("🔓 Показать профиль", callback_data='show_profile')],
            [InlineKeyboardButton("❌ Отмена", callback_data='cancel')]
        ]
        status = "✨ Ваш профиль в данный момент **скрыт**. Хотите ли вы его показать?"
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(status, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
    return TOGGLE_PROFILE_VISIBILITY

# ==================== Обработчик Инлайн Кнопок для Настройки Видимости Профиля ====================
async def handle_toggle_profile_visibility_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает нажатия инлайн кнопок при настройке видимости профиля."""
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    
    await query.answer()
    
    data = query.data
    user = query.from_user
    
    if data == 'hide_profile':
        new_visibility = 0
        status = "🔒 Ваш профиль теперь скрыт от других пользователей."
    elif data == 'show_profile':
        new_visibility = 1
        status = "🔓 Ваш профиль теперь видим для других пользователей."
    elif data == 'cancel':
        await query.message.reply_text("❌ Процесс отменен.")
        return ConversationHandler.END
    else:
        await query.message.reply_text("❌ Некорректный выбор.")
        return ConversationHandler.END
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("UPDATE users SET profile_visible = ? WHERE user_id = ?", (new_visibility, user.id))
        await db.commit()
    
    await query.message.reply_text(status)
    logger.info(f"Пользователь {user.id} изменил видимость профиля на {new_visibility}.")
    return ConversationHandler.END

# ==================== Обработчик Просмотра Профиля ====================
async def handle_view_profile_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия инлайн кнопок для просмотра профиля."""
    query = update.callback_query
    if query is None:
        return
    
    await query.answer()
    
    data = query.data
    if not data.startswith('view_'):
        await query.message.reply_text("❌ Некорректный запрос.")
        return
    
    target_user_id = int(data.split('view_')[1])
    
    # Получение информации о целевом пользователе
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cursor = await db.execute("SELECT username, interests, show_username, profile_photo FROM users WHERE user_id = ?", (target_user_id,))
        target_info = await cursor.fetchone()
        if not target_info:
            await query.message.reply_text("❌ Пользователь не найден или его профиль скрыт.")
            return
        target_username, interests, show_username, profile_photo = target_info
        if not show_username:
            display_name = f"Пользователь {target_user_id}"
        else:
            display_name = target_username
        interests = interests if interests else "Не указаны"
        photo_text = f"📸 Фото профиля: Да" if profile_photo else "📸 Фото профиля: Нет"
    
    profile_message = (
        f"👤 <b>Профиль пользователя:</b>\n\n"
        f"🔹 <b>Юзернейм:</b> {display_name}\n"
        f"🔹 <b>ID:</b> {target_user_id}\n"
        f"🔹 <b>Интересы:</b> {interests}\n"
        f"{photo_text}\n\n"
        f"📬 Вы можете отправить этому пользователю сообщение с помощью команды /send или воспользоваться кнопками ниже."
    )
    
    # Инлайн кнопки для отправки сообщения или закрытия профиля
    keyboard = [
        [InlineKeyboardButton("✉️ Отправить сообщение", callback_data=f"send_{target_user_id}")],
        [InlineKeyboardButton("🔍 Посмотреть профиль", callback_data=f"view_{target_user_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if profile_photo:
        # Используем file_id напрямую
        try:
            await query.message.reply_photo(
                photo=profile_photo,
                caption=profile_message,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке фото профиля пользователю {target_user_id}: {e}")
            # Отправляем сообщение без фото
            await query.message.reply_text(profile_message, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    else:
        await query.message.reply_text(profile_message, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    
    logger.info(f"Пользователь {query.from_user.id} просмотрел профиль пользователя {target_user_id}.")

# ==================== Команда /list ====================
async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет список всех зарегистрированных пользователей."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cursor = await db.execute("SELECT user_id, username, show_username FROM users WHERE profile_visible = 1")
        users_list = await cursor.fetchall()
        if not users_list:
            await update.message.reply_text("📋 Нет зарегистрированных пользователей.")
            return
    
        response = "📋 <b>Зарегистрированные пользователи:</b>\n\n"
        for user in users_list:
            user_id, username, show_username = user
            if show_username:
                display_name = username
            else:
                display_name = f"Пользователь {user_id}"
            response += f"{display_name}\n"
        await update.message.reply_text(response, parse_mode=ParseMode.HTML)
        logger.info("Команда /list вызвана.")

# ==================== Команда /view_profile ====================
async def view_profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /view_profile позволяет пользователю выбрать и просмотреть профиль другого пользователя."""
    await update.message.reply_text(
        "📬 Пожалуйста, выберите пользователя, чей профиль вы хотите просмотреть:",
        reply_markup=await get_users_keyboard(exclude_user_id=update.message.from_user.id)
    )

# ==================== Команда /info ====================
async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет информацию о текущем пользователе."""
    user = update.message.from_user
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cursor = await db.execute("SELECT username, interests, anonymous_mode, show_username, profile_photo, profile_visible FROM users WHERE user_id = ?", (user.id,))
        result = await cursor.fetchone()
        if not result:
            await update.message.reply_text("❌ Вы не зарегистрированы. Пожалуйста, используйте /start для регистрации.")
            return
        username, interests, anonymous_mode, show_username, profile_photo, profile_visible = result
        interests = interests if interests else "Не указаны"
        mode_text = "🕶️ Анонимный" if anonymous_mode else "📝 Неанонимный"
        visibility = "✅ Показывать" if show_username else "❌ Скрывать"
        profile_status = "✅ Видим" if profile_visible else "❌ Скрыт"
        photo_text = f"📸 Фото профиля: Да" if profile_photo else "📸 Фото профиля: Нет"
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cursor = await db.execute('''
            SELECT sent_messages, received_messages, likes, dislikes FROM rankings WHERE user_id = ?
        ''', (user.id,))
        rank_info = await cursor.fetchone()
        sent = rank_info[0] if rank_info else 0
        received = rank_info[1] if rank_info else 0
        likes = rank_info[2] if rank_info else 0
        dislikes = rank_info[3] if rank_info else 0
    
    profile_message = (
        f"👤 <b>Ваш профиль:</b>\n\n"
        f"🔹 <b>Юзернейм:</b> {username}\n"
        f"🔹 <b>ID:</b> {user.id}\n"
        f"🔹 <b>Интересы:</b> {interests}\n"
        f"🔹 <b>Режим отправки сообщений:</b> {mode_text}\n"
        f"🔹 <b>Видимость юзернейма:</b> {visibility}\n"
        f"🔹 <b>Статус профиля:</b> {profile_status}\n"
        f"{photo_text}\n\n"
        f"📈 <b>Рейтинг:</b>\n"
        f"💬 Отправлено сообщений: {sent}\n"
        f"📥 Получено сообщений: {received}\n"
        f"❤️ Лайков: {likes}\n"
        f"❌ Дизлайков: {dislikes}"
    )
    
    if profile_photo:
        # Используем file_id напрямую
        try:
            await update.message.reply_photo(
                photo=profile_photo,
                caption=profile_message,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке фото профиля пользователю {user.id}: {e}")
            # Отправляем сообщение без фото
            await update.message.reply_text(profile_message, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(profile_message, parse_mode=ParseMode.HTML)
    
    logger.info(f"Команда /info вызвана пользователем {username} ({user.id}).")

# ==================== Команда /delete ====================
async def delete_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Позволяет пользователю удалить свою регистрацию."""
    user = update.message.from_user
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cursor = await db.execute("SELECT username FROM users WHERE user_id = ?", (user.id,))
        result = await cursor.fetchone()
        if not result:
            await update.message.reply_text("❌ Вы не зарегистрированы.")
            return
        username = result[0]
        await db.execute("DELETE FROM users WHERE user_id = ?", (user.id,))
        await db.execute("DELETE FROM messages WHERE sender_id = ? OR receiver_id = ?", (user.id, user.id))
        await db.execute("DELETE FROM rankings WHERE user_id = ?", (user.id,))
        await db.execute("DELETE FROM matches WHERE user1_id = ? OR user2_id = ?", (user.id, user.id))
        await db.execute("DELETE FROM blocks WHERE blocked_user_id = ? OR blocked_by = ?", (user.id, user.id))
        await db.execute("DELETE FROM likes WHERE liker_id = ? OR liked_id = ?", (user.id, user.id))
        await db.commit()
    await update.message.reply_text(f"❌ Ваша регистрация ({username}) и все связанные данные были удалены.")
    logger.info(f"Пользователь {username} ({user.id}) удалил свою регистрацию и связанные данные.")

# ==================== Команда /help ====================
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет справку с доступными командами."""
    help_text = (
        "📌 <b>Доступные команды:</b>\n\n"
        "/send - Отправить сообщение другому пользователю\n"
        "/send_anonymous - Отправить анонимное сообщение другому пользователю\n"
        "/set_mode - Изменить режим отправки сообщений (анонимный/неанонимный)\n"
        "/set_username_visibility - Настроить видимость вашего юзернейма\n"
        "/toggle_profile_visibility - Настроить видимость вашего профиля\n"
        "/set_profile_photo - Установить или изменить фотографию профиля\n"
        "/list - Показать список зарегистрированных пользователей\n"
        "/view_profile - Просмотреть профиль другого пользователя\n"
        "/info - Показать информацию о вашем профиле\n"
        "/delete - Удалить свою регистрацию и данные\n"
        "/rankings - Посмотреть топ пользователей по количеству отправленных сообщений\n"
        "/myrank - Узнать свой ранг и количество отправленных/полученных сообщений\n"
        "/add_interests интерес1, интерес2 - Добавить свои интересы\n"
        "/search интерес - Поиск пользователей по интересам\n"
        "/match - Найти подходящих партнёров\n"
        "/block @username - Заблокировать пользователя (только админ)\n"
        "/report @username причина - Отправить жалобу на пользователя\n"
        "/unblock @username - Разблокировать пользователя (только админ)\n"
        "/like @username - Поставить лайк пользователю\n"
        "/dislike @username - Поставить дизлайк пользователю\n"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)
    logger.info("Команда /help вызвана.")

# ==================== Команда /add_interests ====================
async def add_interests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Позволяет пользователю добавить свои интересы."""
    user = update.message.from_user
    interests = ' '.join(context.args)
    
    if not interests:
        await update.message.reply_text("❌ Пожалуйста, укажите свои интересы после команды. Пример:\n/add_interests программирование, музыка, спорт")
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cursor = await db.execute("SELECT username FROM users WHERE user_id = ?", (user.id,))
        result = await cursor.fetchone()
        if not result:
            await update.message.reply_text("❌ Пожалуйста, сначала зарегистрируйтесь с помощью /start.")
            return
        username = result[0]
        await db.execute("UPDATE users SET interests = ? WHERE user_id = ?", (interests, user.id))
        await db.commit()
    
    await update.message.reply_text(f"✅ Ваши интересы обновлены: {interests}")
    logger.info(f"Пользователь {username} ({user.id}) обновил свои интересы: {interests}")

# ==================== Команда /search ====================
async def search_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Позволяет пользователю искать других пользователей по интересам."""
    query = ' '.join(context.args).lower()
    if not query:
        await update.message.reply_text("❌ Пожалуйста, укажите интересы для поиска. Пример:\n/search программирование")
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cursor = await db.execute("SELECT username, interests FROM users WHERE LOWER(interests) LIKE ? AND profile_visible = 1", (f"%{query}%",))
        results = await cursor.fetchall()
    
    if not results:
        await update.message.reply_text("❌ Пользователи с такими интересами не найдены.")
        return
    
    response = "🔍 <b>Пользователи с интересами, похожими на ваш запрос:</b>\n"
    for username, interests in results:
        response += f"{username} - {interests}\n"
    
    await update.message.reply_text(response, parse_mode=ParseMode.HTML)
    logger.info(f"Пользователь {update.message.from_user.id} искал пользователей по интересам: {query}")

# ==================== Команда /rankings ====================
async def rankings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет рейтинг пользователей по количеству отправленных сообщений."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        # Получаем топ 10 пользователей по количеству отправленных сообщений
        cursor = await db.execute('''
            SELECT users.username, rankings.sent_messages
            FROM users
            JOIN rankings ON users.user_id = rankings.user_id
            WHERE users.profile_visible = 1
            ORDER BY rankings.sent_messages DESC
            LIMIT 10
        ''')
        rankings = await cursor.fetchall()
    
    if not rankings or all(sent == 0 for _, sent in rankings):
        await update.message.reply_text("📭 Пока нет отправленных сообщений для формирования рейтинга.")
        return
    
    ranking_text = "🏆 <b>Топ 10 пользователей по количеству отправленных сообщений:</b>\n\n"
    for idx, (username, sent) in enumerate(rankings, start=1):
        ranking_text += f"{idx}. {username} - {sent} сообщений\n"
    
    await update.message.reply_text(ranking_text, parse_mode=ParseMode.HTML)
    logger.info("Команда /rankings вызвана.")

# ==================== Команда /myrank ====================
async def my_rank_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет ранг текущего пользователя по количеству отправленных сообщений."""
    user = update.message.from_user
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        # Получаем количество сообщений пользователя
        cursor = await db.execute("SELECT sent_messages, received_messages, likes, dislikes FROM rankings WHERE user_id = ?", (user.id,))
        rank_info = await cursor.fetchone()
        if not rank_info:
            await update.message.reply_text("❌ Вы не зарегистрированы. Пожалуйста, используйте /start для регистрации.")
            return
        sent, received, likes, dislikes = rank_info
        
        # Получаем ранг пользователя
        cursor = await db.execute('''
            SELECT COUNT(*) + 1 FROM rankings
            JOIN users ON users.user_id = rankings.user_id
            WHERE rankings.sent_messages > ?
              AND users.profile_visible = 1
        ''', (sent,))
        rank = await cursor.fetchone()
        rank = rank[0] if rank else "Не ранжирован"
    
    await update.message.reply_text(
        f"🏅 <b>Ваш ранг:</b> {rank}\n💬 <b>Отправлено сообщений:</b> {sent}\n📥 <b>Получено сообщений:</b> {received}\n❤️ <b>Лайков:</b> {likes}\n❌ <b>Дизлайков:</b> {dislikes}",
        parse_mode=ParseMode.HTML
    )
    logger.info(f"Команда /myrank вызвана пользователем {user.id}.")

# ==================== Команда /match ====================
async def match_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Находит потенциальных партнёров на основе интересов и других параметров."""
    user = update.message.from_user
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        # Получаем интересы пользователя
        cursor = await db.execute("SELECT interests FROM users WHERE user_id = ?", (user.id,))
        result = await cursor.fetchone()
        if not result or not result[0]:
            await update.message.reply_text("❌ Укажите ваши интересы с помощью команды /add_interests перед поиском совпадений.")
            return
        
        user_interests = set([interest.strip().lower() for interest in result[0].split(',')])
        
        # Находим пользователей с общими интересами
        cursor = await db.execute("SELECT user_id, username, interests FROM users WHERE user_id != ? AND profile_visible = 1", (user.id,))
        all_users = await cursor.fetchall()
        
        matches = []
        for other_user in all_users:
            other_user_id, other_username, other_interests = other_user
            if not other_interests:
                continue
            other_user_interests = set([interest.strip().lower() for interest in other_interests.split(',')])
            common_interests = user_interests.intersection(other_user_interests)
            if len(common_interests) >= 1:  # Минимум одно общее интерес
                matches.append((other_user_id, other_username, len(common_interests)))
        
        if not matches:
            await update.message.reply_text("🔍 К сожалению, не найдено подходящих партнёров на основе ваших интересов.")
            return
        
        # Сортируем по количеству общих интересов
        matches.sort(key=lambda x: x[2], reverse=True)
        
        # Ограничиваем до топ 5 совпадений
        top_matches = matches[:5]
        
        response = "💖 <b>Возможные совпадения для вас:</b>\n\n"
        for match in top_matches:
            match_id, match_username, common = match
            response += f"• {match_username} (Общие интересы: {common})\n"
        
        await update.message.reply_text(response, parse_mode=ParseMode.HTML)
        logger.info(f"Пользователь {user.id} запросил совпадения.")

# ==================== Команда /block ====================
async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Позволяет администратору заблокировать пользователя."""
    user = update.message.from_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав для выполнения этой команды.")
        return
    
    if not context.args:
        await update.message.reply_text("❌ Пожалуйста, укажите юзернейм пользователя для блокировки. Пример:\n/block @username")
        return
    
    target_username = context.args[0]
    if not target_username.startswith('@'):
        await update.message.reply_text("❌ Пожалуйста, укажите валидный юзернейм (начинается с @).")
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        # Получаем user_id целевого пользователя
        cursor = await db.execute("SELECT user_id FROM users WHERE username = ?", (target_username,))
        result = await cursor.fetchone()
        if not result:
            await update.message.reply_text("❌ Пользователь с таким юзернеймом не найден.")
            return
        target_user_id = result[0]
        
        if target_user_id == user.id:
            await update.message.reply_text("❌ Вы не можете заблокировать самого себя.")
            return
        
        # Добавляем в блоки
        await db.execute("INSERT OR IGNORE INTO blocks (blocked_user_id, blocked_by) VALUES (?, ?)", (target_user_id, user.id))
        await db.commit()
    
    await update.message.reply_text(f"🔒 Вы успешно заблокировали пользователя {target_username}.")
    logger.info(f"Пользователь {user.id} заблокировал пользователя {target_user_id} ({target_username}).")

# ==================== Команда /report ====================
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Позволяет пользователю отправить жалобу на другого пользователя."""
    user = update.message.from_user
    if len(context.args) < 2:
        await update.message.reply_text("❌ Пожалуйста, укажите юзернейм пользователя и причину жалобы. Пример:\n/report @username Спам")
        return
    
    target_username = context.args[0]
    reason = ' '.join(context.args[1:])
    
    if not target_username.startswith('@'):
        await update.message.reply_text("❌ Пожалуйста, укажите валидный юзернейм (начинается с @).")
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        # Получаем user_id целевого пользователя
        cursor = await db.execute("SELECT user_id FROM users WHERE username = ?", (target_username,))
        result = await cursor.fetchone()
        if not result:
            await update.message.reply_text("❌ Пользователь с таким юзернеймом не найден.")
            return
        target_user_id = result[0]
        
        if target_user_id == user.id:
            await update.message.reply_text("❌ Вы не можете пожаловаться на самого себя.")
            return
        
        # Логирование жалобы (можно расширить для сохранения в БД)
        logger.info(f"Жалоба от {user.id} на {target_user_id}: {reason}")
    
    await update.message.reply_text(f"📌 Ваше сообщение о жалобе на пользователя {target_username} отправлено администрации.")
    
    # Отправка уведомления администратору
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"📢 <b>Жалоба</b>\n\nПользователь @{user.username} ({user.id}) пожаловался на пользователя @{target_username} ({target_user_id}).\nПричина: {reason}",
            parse_mode=ParseMode.HTML
        )
        logger.info(f"Жалоба от {user.id} на {target_user_id} отправлена администратору.")
    except Exception as e:
        logger.error(f"Не удалось отправить жалобу администратору: {e}")

# ==================== Команда /unblock ====================
async def unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Позволяет администратору разблокировать пользователя."""
    user = update.message.from_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав для выполнения этой команды.")
        return
    
    if not context.args:
        await update.message.reply_text("❌ Пожалуйста, укажите юзернейм пользователя для разблокировки. Пример:\n/unblock @username")
        return
    
    target_username = context.args[0]
    if not target_username.startswith('@'):
        await update.message.reply_text("❌ Пожалуйста, укажите валидный юзернейм (начинается с @).")
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        # Получаем user_id целевого пользователя
        cursor = await db.execute("SELECT user_id FROM users WHERE username = ?", (target_username,))
        result = await cursor.fetchone()
        if not result:
            await update.message.reply_text("❌ Пользователь с таким юзернеймом не найден.")
            return
        target_user_id = result[0]
        
        # Удаляем блокировку
        cursor = await db.execute("SELECT * FROM blocks WHERE blocked_user_id = ? AND blocked_by = ?", (target_user_id, user.id))
        is_blocked = await cursor.fetchone()
        if not is_blocked:
            await update.message.reply_text(f"❌ Пользователь {target_username} не был заблокирован.")
            return
        
        await db.execute("DELETE FROM blocks WHERE blocked_user_id = ? AND blocked_by = ?", (target_user_id, user.id))
        await db.commit()
    
    await update.message.reply_text(f"🔓 Пользователь {target_username} успешно разблокирован.")
    logger.info(f"Администратор {ADMIN_ID} разблокировал пользователя {target_user_id} ({target_username}).")

# ==================== Команда /like ====================
async def like_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Позволяет пользователю поставить лайк другому пользователю."""
    user = update.message.from_user
    if not context.args:
        await update.message.reply_text("❌ Пожалуйста, укажите юзернейм пользователя для лайка. Пример:\n/like @username")
        return
    
    target_username = context.args[0]
    if not target_username.startswith('@'):
        await update.message.reply_text("❌ Пожалуйста, укажите валидный юзернейм (начинается с @).")
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        # Получаем user_id целевого пользователя
        cursor = await db.execute("SELECT user_id FROM users WHERE username = ?", (target_username,))
        result = await cursor.fetchone()
        if not result:
            await update.message.reply_text("❌ Пользователь с таким юзернеймом не найден.")
            return
        target_user_id = result[0]
        
        if target_user_id == user.id:
            await update.message.reply_text("❌ Вы не можете поставить лайк самому себе.")
            return
        
        # Проверка, уже ли поставлен лайк или дизлайк
        cursor = await db.execute("SELECT like_type FROM likes WHERE liker_id = ? AND liked_id = ?", (user.id, target_user_id))
        existing = await cursor.fetchone()
        if existing:
            await update.message.reply_text("❌ Вы уже оценивали этого пользователя.")
            return
        
        # Добавляем лайк
        await db.execute("INSERT INTO likes (liker_id, liked_id, like_type) VALUES (?, ?, 'like')", (user.id, target_user_id))
        # Увеличиваем счетчик лайков
        await db.execute("UPDATE rankings SET likes = likes + 1 WHERE user_id = ?", (target_user_id,))
        
        # Проверка на взаимный лайк
        cursor = await db.execute("SELECT like_type FROM likes WHERE liker_id = ? AND liked_id = ?", (target_user_id, user.id))
        mutual = await cursor.fetchone()
        if mutual and mutual[0] == 'like':
            # Добавляем матч
            await db.execute("INSERT OR IGNORE INTO matches (user1_id, user2_id) VALUES (?, ?)", (min(user.id, target_user_id), max(user.id, target_user_id)))
            await db.commit()
            await update.message.reply_text(f"❤️ Вы поставили лайк пользователю {target_username}. У вас взаимный лайк! 🎉")
            logger.info(f"Взаимный лайк между {user.id} и {target_user_id}.")
            return
        
        await db.commit()
    
    await update.message.reply_text(f"❤️ Вы поставили лайк пользователю {target_username}.")
    logger.info(f"Пользователь {user.id} поставил лайк пользователю {target_user_id} ({target_username}).")

# ==================== Команда /dislike ====================
async def dislike_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Позволяет пользователю поставить дизлайк другому пользователю."""
    user = update.message.from_user
    if not context.args:
        await update.message.reply_text("❌ Пожалуйста, укажите юзернейм пользователя для дизлайка. Пример:\n/dislike @username")
        return
    
    target_username = context.args[0]
    if not target_username.startswith('@'):
        await update.message.reply_text("❌ Пожалуйста, укажите валидный юзернейм (начинается с @).")
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        # Получаем user_id целевого пользователя
        cursor = await db.execute("SELECT user_id FROM users WHERE username = ?", (target_username,))
        result = await cursor.fetchone()
        if not result:
            await update.message.reply_text("❌ Пользователь с таким юзернеймом не найден.")
            return
        target_user_id = result[0]
        
        if target_user_id == user.id:
            await update.message.reply_text("❌ Вы не можете поставить дизлайк самому себе.")
            return
        
        # Проверка, уже ли поставлен лайк или дизлайк
        cursor = await db.execute("SELECT like_type FROM likes WHERE liker_id = ? AND liked_id = ?", (user.id, target_user_id))
        existing = await cursor.fetchone()
        if existing:
            await update.message.reply_text("❌ Вы уже оценивали этого пользователя.")
            return
        
        # Добавляем дизлайк
        await db.execute("INSERT INTO likes (liker_id, liked_id, like_type) VALUES (?, ?, 'dislike')", (user.id, target_user_id))
        # Увеличиваем счетчик дизлайков
        await db.execute("UPDATE rankings SET dislikes = dislikes + 1 WHERE user_id = ?", (target_user_id,))
        await db.commit()
    
    await update.message.reply_text(f"❌ Вы поставили дизлайк пользователю {target_username}.")
    logger.info(f"Пользователь {user.id} поставил дизлайк пользователю {target_user_id} ({target_username}).")

# ==================== Команда /set_profile_photo ====================
async def set_profile_photo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Команда для установки или изменения фотографии профиля."""
    await update.message.reply_text("📸 Пожалуйста, отправьте фотографию для вашего профиля.")
    return SET_PROFILE_PHOTO

# ==================== Обработка Фотографии Профиля ====================
async def set_profile_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает отправленную фотографию и сохраняет её как фото профиля."""
    user = update.message.from_user
    if not update.message.photo:
        await update.message.reply_text("❌ Пожалуйста, отправьте фотографию.")
        return SET_PROFILE_PHOTO
    
    photo_file = update.message.photo[-1].file_id
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        # Сохраняем file_id как ссылку на фото
        await db.execute("UPDATE users SET profile_photo = ? WHERE user_id = ?", (photo_file, user.id))
        await db.commit()
    
    await update.message.reply_text("✅ Фотография профиля успешно установлена.")
    logger.info(f"Пользователь {user.id} установил фотографию профиля.")
    return ConversationHandler.END

# ==================== Глобальный Обработчик Ошибок ====================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик ошибок."""
    logger.error(msg="Обнаружена ошибка:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("⚠️ Произошла ошибка. Пожалуйста, попробуйте позже.")

# ==================== Главная Асинхронная Функция ====================
async def main_async():
    """Основная асинхронная функция, инициализирующая и запускающая бота."""
    # Инициализация базы данных
    await init_db()
    
    # Создание приложения
    application = ApplicationBuilder().token(TOKEN).build()
    
    # ==================== ConversationHandler для Регистрации ====================
    registration_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            REGISTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, register)],
        },
        fallbacks=[CommandHandler('cancel', lambda update, context: update.message.reply_text("❌ Процесс отменен."))],
        per_user=True,
        # per_message=False по умолчанию, можно опустить
    )
    
    # ==================== ConversationHandler для Отправки Сообщений ====================
    send_message_handler_conv = ConversationHandler(
        entry_points=[CommandHandler('send', send_message_command),
                      CommandHandler('send_anonymous', send_anonymous_message_command)],
        states={
            SELECT_RECIPIENT: [
                CallbackQueryHandler(handle_send_buttons, pattern=r'^send_\d+$'),
                CallbackQueryHandler(handle_view_profile_buttons, pattern=r'^view_\d+$'),
                CallbackQueryHandler(lambda update, context: update.callback_query.answer() or update.callback_query.message.reply_text("❌ Некорректный выбор."), pattern=r'^no_users$')
            ],
            SEND_MESSAGE: [MessageHandler(filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.VOICE | filters.VIDEO_NOTE & ~filters.COMMAND, send_message_handler)],
        },
        fallbacks=[CommandHandler('cancel', lambda update, context: update.message.reply_text("❌ Процесс отменен."))],
        per_user=True,
        # per_message=False по умолчанию
    )
    
    # ==================== ConversationHandler для Ответа ====================
    reply_handler_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_reply_buttons, pattern=r'^reply_\d+$'),
                      CommandHandler('reply', reply_command)],
        states={
            REPLY_MESSAGE: [MessageHandler(filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.VOICE | filters.VIDEO_NOTE & ~filters.COMMAND, handle_reply)],
        },
        fallbacks=[CommandHandler('cancel', lambda update, context: update.message.reply_text("❌ Процесс отменен."))],
        per_user=True,
        # per_message=False по умолчанию
    )
    
    # ==================== ConversationHandler для Видимости Юзернейма ====================
    username_visibility_handler = ConversationHandler(
        entry_points=[CommandHandler('set_username_visibility', set_username_visibility_command)],
        states={
            SET_USERNAME_VISIBILITY: [CallbackQueryHandler(handle_username_visibility_buttons, pattern=r'^(show_username|hide_username)$')],
        },
        fallbacks=[CommandHandler('cancel', lambda update, context: update.message.reply_text("❌ Процесс отменен."))],
        per_user=True,
        # per_message=False по умолчанию
    )
    
    # ==================== ConversationHandler для Установки Фотографии Профиля ====================
    profile_photo_handler = ConversationHandler(
        entry_points=[CommandHandler('set_profile_photo', set_profile_photo_command)],
        states={
            SET_PROFILE_PHOTO: [MessageHandler(filters.PHOTO, set_profile_photo_handler)],
        },
        fallbacks=[CommandHandler('cancel', lambda update, context: update.message.reply_text("❌ Процесс отменен."))],
        per_user=True,
    )
    
    # ==================== ConversationHandler для Настройки Видимости Профиля ====================
    toggle_profile_visibility_handler = ConversationHandler(
        entry_points=[CommandHandler('toggle_profile_visibility', toggle_profile_visibility_command)],
        states={
            TOGGLE_PROFILE_VISIBILITY: [CallbackQueryHandler(handle_toggle_profile_visibility_buttons, pattern=r'^(hide_profile|show_profile|cancel)$')],
        },
        fallbacks=[CommandHandler('cancel', lambda update, context: update.message.reply_text("❌ Процесс отменен."))],
        per_user=True,
    )
    
    # ==================== CallbackQueryHandler для set_mode ====================
    set_mode_handler = CallbackQueryHandler(handle_set_mode_buttons, pattern=r'^mode_')
    
    # ==================== CallbackQueryHandler для просмотра профиля ====================
    view_profile_handler = CallbackQueryHandler(handle_view_profile_buttons, pattern=r'^view_\d+$')
    
    # ==================== CallbackQueryHandler для скрытия профиля из просмотра ====================
    hide_profile_from_view_handler = CallbackQueryHandler(handle_hide_profile_from_view, pattern=r'^hide_profile_\d+$')
    
    # ==================== Добавление Обработчиков ====================
    application.add_handler(registration_handler)
    application.add_handler(send_message_handler_conv)
    application.add_handler(reply_handler_conv)
    application.add_handler(username_visibility_handler)
    application.add_handler(profile_photo_handler)
    application.add_handler(toggle_profile_visibility_handler)
    application.add_handler(set_mode_handler)
    application.add_handler(CommandHandler('list', list_users))
    application.add_handler(CommandHandler('view_profile', view_profile_command))
    application.add_handler(CommandHandler('info', info_command))
    application.add_handler(CommandHandler('delete', delete_registration))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('add_interests', add_interests))
    application.add_handler(CommandHandler('search', search_users))
    application.add_handler(CommandHandler('rankings', rankings_command))
    application.add_handler(CommandHandler('myrank', my_rank_command))
    application.add_handler(CommandHandler('match', match_command))
    application.add_handler(CommandHandler('block', block_command))
    application.add_handler(CommandHandler('report', report_command))
    application.add_handler(CommandHandler('unblock', unblock_command))
    application.add_handler(CommandHandler('like', like_command))
    application.add_handler(CommandHandler('dislike', dislike_command))
    
    # Добавление глобального обработчика ошибок
    application.add_error_handler(error_handler)
    
    logger.info("Обработчики добавлены. Бот запускается.")
    
    # Запуск бота
    await application.run_polling()

# ==================== Главная Точка Входа ====================
def main():
    """Главная функция, запускающая асинхронную основную функцию."""
    try:
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен пользователем.")

if __name__ == '__main__':
    main()
