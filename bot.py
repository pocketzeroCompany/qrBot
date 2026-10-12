import asyncio
import logging
import os
from datetime import datetime

aiosqlite_available = True
try:
    import aiosqlite
except ImportError:
    aiosqlite_available = False

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
DB_FILE = "profile_history.db"

# --- DATABASE SETUP ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS profile_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                change_type TEXT,
                old_value TEXT,
                new_value TEXT,
                timestamp TEXT
            )
        """)
        await db.commit()

# --- KEYBOARDS (SELECTIVE COLORING) ---
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱  View timeline history", callback_data="view_history", style="success")],
        [InlineKeyboardButton(text="📊  Profile statistics", callback_data="view_stats")],
        [InlineKeyboardButton(text="⎋  Exit", callback_data="menu_exit", style="danger")]
    ])

def history_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↻  Refresh timeline", callback_data="view_history", style="success")],
        [InlineKeyboardButton(text="⎋  Main menu", callback_data="menu_main", style="danger")]
    ])

# --- HANDLERS ---

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await init_db()
    await message.answer(
        "Welcome to Trace.\n\n"
        "Your personal digital footprint archive and profile timeline tracker.\n"
        "Choose an option below:",
        reply_markup=main_menu_kb()
    )

@router.callback_query(F.data == "menu_main")
async def cb_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "Main menu.\n\nChoose an option below:", 
        reply_markup=main_menu_kb()
    )
    await callback.answer()

@router.callback_query(F.data == "view_history")
async def cb_history(callback: CallbackQuery):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT change_type, old_value, new_value, timestamp FROM profile_logs ORDER BY id DESC LIMIT 10"
        ) as cursor:
            rows = await cursor.fetchall()
            
    if not rows:
        text = "Timeline is currently empty.\n\nNo changes have been logged yet."
    else:
        text = "Trace timeline (recent changes):\n\n"
        for change_type, old_val, new_val, timestamp in rows:
            if change_type == "USERNAME":
                text += f"• Username: `{old_val}` → `{new_val}`\n  ⏱ {timestamp}\n\n"
            else:
                text += f"• {change_type} updated\n  ⏱ {timestamp}\n\n"
                
    await callback.message.edit_text(text, reply_markup=history_kb(), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data == "view_stats")
async def cb_stats(callback: CallbackQuery):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT COUNT(*) FROM profile_logs") as cursor:
            total_records = (await cursor.fetchone())[0]
            
        async with db.execute("SELECT COUNT(*) FROM profile_logs WHERE change_type = 'USERNAME'") as cursor:
            username_changes = (await cursor.fetchone())[0]

    text = (
        "Profile Statistics\n\n"
        f"• Total recorded events: {total_records}\n"
        f"• Username changes: {username_changes}\n\n"
        "Your tracker is active and listening for updates."
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⎋  Main menu", callback_data="menu_main", style="danger")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "menu_exit")
async def cb_exit(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Trace session closed. Send /start to reopen.")
    await callback.answer()

async def main():
    if not aiosqlite_available:
        print("Warning: aiosqlite missing.")
        return
        
    await init_db()
    
    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    
    dp.include_router(router)
    
    await bot.delete_webhook(drop_pending_updates=True)
    print("Trace bot successfully started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    
