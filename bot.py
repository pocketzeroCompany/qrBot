import asyncio
import logging
import os
from datetime import datetime

# Перевірка бібліотек
try:
    import aiosqlite
    aiosqlite_available = True
except ImportError:
    aiosqlite_available = False

try:
    from duckduckgo_search import DDGS
    ddg_available = True
except ImportError:
    ddg_available = False

try:
    import g4f
    g4f_available = True
except ImportError:
    g4f_available = False

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    logging.error("❌ ERROR: BOT_TOKEN environment variable is not set!")
    exit(1)

router = Router()
DB_FILE = "core_assistant.db"

# --- DATABASE SETUP ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS conversation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                role TEXT,
                content TEXT,
                timestamp TEXT
            )
        """)
        await db.commit()

async def save_message(user_id: int, role: str, content: str):
    async with aiosqlite.connect(DB_FILE) as db:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await db.execute(
            "INSERT INTO conversation_history (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (user_id, role, content, now)
        )
        await db.commit()

async def clear_user_history(user_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM conversation_history WHERE user_id = ?", (user_id,))
        await db.commit()

# --- WEB SEARCH HELPER ---
def search_web(query: str) -> str:
    if not ddg_available:
        return ""
    try:
        with DDGS() as ddgs:
            results = [r.get('body', '') for r in ddgs.text(query, max_results=3)]
            return "\n".join(results)
    except Exception as e:
        logging.error(f"Search error: {e}")
        return ""

# --- AI HELPER WITH SEARCH & LANGUAGE ADAPTATION ---
async def get_ai_response(prompt: str, user_id: int) -> str:
    # Перевіряємо, чи потрібен пошук в інтернеті (запит містить питання про факти, події тощо)
    context_info = ""
    if any(keyword in prompt.lower() for keyword in ["що таке", "хто такий", "новини", "погода", "курс", "коли", "де", "search", "what is", "who is", "news"]):
        search_results = await asyncio.to_thread(search_web, prompt)
        if search_results:
            context_info = f"\n\n[Context from web search]:\n{search_results}"

    system_instruction = (
        "You are Core, a minimalist, highly intelligent personal AI companion and assistant. "
        "Rules:\n"
        "1. Always reply in the exact same language the user writes in (Ukrainian if Ukrainian, English if English, etc.).\n"
        "2. If web context is provided, use it to give accurate, factual answers.\n"
        "3. If it's a casual chat, act as a friendly, sharp companion ('ai buddy').\n"
    )

    full_prompt = f"{system_instruction}\nUser query: {prompt}{context_info}"

    if not g4f_available:
        return f"[Demo AI Mode]: {prompt}"
    
    try:
        response = await asyncio.to_thread(
            g4f.ChatCompletion.create,
            model=g4f.models.gpt_4o,
            messages=[{"role": "user", "content": full_prompt}]
        )
        return str(response)
    except Exception as e:
        return f"AI Service error: {e}"

# --- KEYBOARDS ---
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬  New session", callback_data="session_new", style="success")],
        [InlineKeyboardButton(text="🗑  Clear memory", callback_data="clear_context", style="danger")]
    ])

def response_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑  Clear memory", callback_data="clear_context", style="danger")]
    ])

# --- HANDLERS ---

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await init_db()
    
    await message.answer(
        "Core initialized.\n\n"
        "• Automatic language matching\n"
        "• Real-time web search integration\n"
        "• Companion mode active",
        reply_markup=main_menu_kb()
    )

@router.callback_query(F.data == "clear_context")
async def cb_clear(callback: CallbackQuery):
    await clear_user_history(callback.from_user.id)
    await callback.answer("Context cleared!", show_alert=True)
    await callback.message.edit_text("Memory wiped. Ready for new inputs:", reply_markup=main_menu_kb())

@router.callback_query(F.data == "session_new")
async def cb_new(callback: CallbackQuery):
    await clear_user_history(callback.from_user.id)
    await callback.message.edit_text("New session started. Type anything:")
    await callback.answer()

@router.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    user_text = message.text

    await save_message(user_id, "user", user_text)
    status_msg = await message.answer("⏳ ...")
    
    ai_reply = await get_ai_response(user_text, user_id)
    await save_message(user_id, "assistant", ai_reply)
    
    await status_msg.edit_text(ai_reply, reply_markup=response_kb())

async def main():
    if not aiosqlite_available:
        print("Warning: aiosqlite missing.")
        return
        
    await init_db()
    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    
    await bot.delete_webhook(drop_pending_updates=True)
    print("Core AI Bot with Web Search started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    
