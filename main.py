import asyncio
import logging
import os
import random
import time
from typing import Dict
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update
from aiogram.utils.keyboard import InlineKeyboardBuilder

from fastapi import FastAPI, Request
import uvicorn

load_dotenv()

# ---------------- CONFIG ----------------
TOKEN = os.getenv('TG_BOT_TOKEN')
ADMIN_IDS = [int(x) for x in os.getenv('ADMIN_IDS', '123456789').split(',')]
GROUP_CHAT_ID = int(os.getenv('TG_GROUP_CHAT_ID', "0"))

WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # ВАЖНО: укажешь свой Render URL

RATE_LIMIT_PER_MIN = 6
INVITE_EXPIRE_SECONDS = 300
INVITE_MEMBER_LIMIT = 1

# Баннер через file_id
BANNER_FILE_ID = "AgACAgQAAxkBAAMHaRpp_OjCCZsBPbpW207YOrlenpsAArINaxuq4NBQWfO04PncVXYBAAMCAAN4AAM2BA"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ---------------- In-memory stores ----------------
pending_requests: Dict[int, Dict] = {}
local_rate: Dict[int, list] = {}
banned_users = set()
user_messages: Dict[int, int] = {}  # хранение id сообщения с баннером для редактирования

# ---------------- Helpers ----------------
async def is_banned(user_id: int) -> bool:
    return user_id in banned_users

async def check_rate_limit(user_id: int) -> bool:
    now = int(time.time())
    window_start = now - 60
    arr = local_rate.setdefault(user_id, [])
    arr.append(now)
    while arr and arr[0] < window_start:
        arr.pop(0)
    return len(arr) > RATE_LIMIT_PER_MIN

async def require_not_banned_or_rate_limited(event: types.Message | types.CallbackQuery):
    user = event.from_user
    if not user:
        return False
    uid = user.id

    if await is_banned(uid):
        try:
            msg = "🚫 Вы забанены."
            if isinstance(event, types.CallbackQuery):
                await event.answer(msg, show_alert=True)
            else:
                await event.answer(msg)
        except:
            pass
        return False

    if await check_rate_limit(uid):
        try:
            msg = "⏳ Слишком много запросов. Попробуйте через минуту."
            if isinstance(event, types.CallbackQuery):
                await event.answer(msg, show_alert=True)
            else:
                await event.answer(msg)
        except:
            pass
        return False

    return True

# ---------------- UI ----------------
def main_menu_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text='👥 Вступить в команду', callback_data='role_menu'))
    return kb.as_markup()

def roles_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text='Медведь', callback_data='role_work'),
        InlineKeyboardButton(text='Пентестер', callback_data='role_pentester')
    )
    kb.row(
        InlineKeyboardButton(text='Кодер', callback_data='role_coder'),
        InlineKeyboardButton(text='Трафер', callback_data='role_trafer')
    )
    return kb.as_markup()

def captcha_keyboard(correct: int) -> InlineKeyboardMarkup:
    options = {correct}
    while len(options) < 4:
        options.add(random.randint(2, 18))
    options = list(options)
    random.shuffle(options)

    kb = InlineKeyboardBuilder()
    kb.row(*[InlineKeyboardButton(text=str(x), callback_data=f'captcha_{x}') for x in options])
    return kb.as_markup()

def admin_keyboard(uid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text='✅ Принять', callback_data=f'approve:{uid}'),
        InlineKeyboardButton(text='❌ Отклонить', callback_data=f'deny:{uid}')
    )
    return kb.as_markup()

# ---------------- Handlers ----------------
@dp.message(Command('start'))
async def cmd_start(message: types.Message):
    if not await require_not_banned_or_rate_limited(message):
        return

    text = (
        "👋 Добро пожаловать в наш бот!\n\n"
        "Здесь вы можете:\n"
        "• Получить доступ к закрытой группе\n"
        "• Представить свои навыки и выбрать роль\n"
        "• Участвовать в проектах\n\n"
        "⬇ Нажмите кнопку ниже:"
    )

    sent = await bot.send_photo(
        chat_id=message.chat.id,
        photo=BANNER_FILE_ID,
        caption=text,
        reply_markup=main_menu_keyboard()
    )

    user_messages[message.from_user.id] = sent.message_id

@dp.callback_query(lambda c: c.data == 'role_menu')
async def cb_role_menu(callback: types.CallbackQuery):
    if not await require_not_banned_or_rate_limited(callback):
        return
    uid = callback.from_user.id
    msg_id = user_messages.get(uid)

    if msg_id:
        await bot.edit_message_caption(
            chat_id=uid,
            message_id=msg_id,
            caption="Выберите вашу роль:",
            reply_markup=roles_keyboard()
        )

    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith('role_'))
async def cb_role(callback: types.CallbackQuery):
    if not await require_not_banned_or_rate_limited(callback):
        return

    uid = callback.from_user.id
    role = callback.data.split('_')[1]

    a, b = random.randint(2, 9), random.randint(1, 9)
    answer = a + b

    pending_requests[uid] = {
        'role': role,
        'captcha_answer': answer,
        'captcha_done': False,
        'skills': None
    }

    msg_id = user_messages.get(uid)
    if msg_id:
        await bot.edit_message_caption(
            chat_id=uid,
            message_id=msg_id,
            caption=f"Вы выбрали роль: {role}\n\nРешите капчу: {a} + {b}",
            reply_markup=captcha_keyboard(answer)
        )

    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith('captcha_'))
async def cb_captcha(callback: types.CallbackQuery):
    uid = callback.from_user.id

    if uid not in pending_requests:
        await callback.answer("Сначала выберите роль.", show_alert=True)
        return

    req = pending_requests[uid]
    provided = int(callback.data.split('_')[1])

    if provided != req['captcha_answer']:
        await callback.answer("❌ Неправильно!", show_alert=True)
        return

    req['captcha_done'] = True

    msg_id = user_messages.get(uid)
    if msg_id:
        await bot.edit_message_caption(
            chat_id=uid,
            message_id=msg_id,
            caption="✅ Верно!\n\nТеперь напишите ваши навыки:"
        )

    await callback.answer()

@dp.message()
async def cb_skills(message: types.Message):
    uid = message.from_user.id

    if uid not in pending_requests:
        return

    req = pending_requests[uid]

    if not req['captcha_done']:
        return

    req['skills'] = message.text
    req['username'] = message.from_user.username or message.from_user.full_name

    admin_text = (
        f"🆕 Новая заявка от @{req['username']} (ID: {uid})\n"
        f"Роль: {req['role']}\n"
        f"Навыки: {req['skills']}"
    )

    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, admin_text, reply_markup=admin_keyboard(uid))
        except:
            pass

    await message.answer("🔔 Ваша заявка отправлена админам.")
    del pending_requests[uid]

@dp.callback_query(lambda c: c.data.startswith('approve:') or c.data.startswith('deny:'))
async def cb_admin_decision(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Только админ.", show_alert=True)
        return

    await callback.answer()

    action, uid_s = callback.data.split(':')
    uid = int(uid_s)
    msg_id = user_messages.get(uid)

    if action == 'deny':
        await bot.send_message(uid, "❌ Ваша заявка отклонена.")
        if msg_id:
            await bot.delete_message(chat_id=uid, message_id=msg_id)
        return

    try:
        invite = await bot.create_chat_invite_link(
            chat_id=GROUP_CHAT_ID,
            expire_date=int(time.time()) + INVITE_EXPIRE_SECONDS,
            member_limit=INVITE_MEMBER_LIMIT
        )
        await bot.send_message(uid, f"🎉 Заявка одобрена!\n🔗 Ссылка: {invite.invite_link}")

        if msg_id:
            await bot.delete_message(chat_id=uid, message_id=msg_id)

    except Exception as e:
        logger.error(f"Ошибка: {e}")

# ---------------- FASTAPI + WEBHOOK ----------------
app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update(**data)
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/")
async def root():
    return {"status": "running"}

async def on_startup():
    await bot.delete_webhook()
    await bot.set_webhook(WEBHOOK_URL)

def start():
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 10000)),
        reload=False
    )

if __name__ == "__main__":
    asyncio.run(on_startup())
    start()
