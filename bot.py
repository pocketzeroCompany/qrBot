import asyncio
import io
import logging
import os
import sys
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import qrcode
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.colormasks import SolidFillColorMask

TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Стан для покрокового створення різних типів QR
class QRStates(StatesGroup):
    waiting_for_input = State()

# Кольорові палітри для кастомізації
COLOR_PALETTES = {
    "classic": {"fill": "black", "back": "white", "name": "Classic Black"},
    "matrix": {"fill": "#00FF66", "back": "#0A0A0A", "name": "Matrix Green"},
    "blue": {"fill": "#0055FF", "back": "#F0F4FF", "name": "Deep Blue"},
    "purple": {"fill": "#7B00FF", "back": "#FAF5FF", "name": "Cyber Purple"}
}

def generate_qr_bytes(data: str, color_key: str = "classic") -> bytes:
    palette = COLOR_PALETTES.get(color_key, COLOR_PALETTES["classic"])
    
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)

    # Конвертація кольорів з hex у RGB або назви
    fill_c = palette["fill"]
    back_c = palette["back"]

    img = qr.make_image(
        image_factory=StyledPilImage,
        color_mask=SolidFillColorMask(
            front_color=tuple(int(fill_c.lstrip('#')[i:i+2], 16) for i in (0, 2, 4)) if fill_c.startswith('#') else (0, 0, 0),
            back_color=tuple(int(back_c.lstrip('#')[i:i+2], 16) for i in (0, 2, 4)) if back_c.startswith('#') else (255, 255, 255)
        )
    )
    
    output = io.BytesIO()
    img.save(output, format="PNG")
    output.seek(0)
    return output.getvalue()

def get_start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 URL / Link", callback_data="type:url"),
         InlineKeyboardButton(text="💬 Simple Text", callback_data="type:text")],
        [InlineKeyboardButton(text="📶 Wi-Fi Network", callback_data="type:wifi"),
         InlineKeyboardButton(text="✈️ Telegram Link", callback_data="type:tg")],
        [InlineKeyboardButton(text="🎨 Change Style / Color", callback_data="menu_colors")]
    ])

def get_color_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◼️ Classic Black", callback_data="color:classic"),
         InlineKeyboardButton(text="🟩 Matrix Green", callback_data="color:matrix")],
        [InlineKeyboardButton(text="🟦 Deep Blue", callback_data="color:blue"),
         InlineKeyboardButton(text="🟪 Cyber Purple", callback_data="color:purple")],
        [InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="back_main")]
    ])

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    text = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "▪ QR // HUNTBOT ‖ UTILITY\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Instant minimalist QR generator.\n"
        "Select data type or pick a color scheme below:"
    )
    await message.answer(text, reply_markup=get_start_keyboard(), parse_mode="Markdown")

@dp.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "▪ QR // HUNTBOT ‖ UTILITY\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Select data type or pick a color scheme below:"
    )
    await callback.message.edit_text(text, reply_markup=get_start_keyboard(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "menu_colors")
async def cb_menu_colors(callback: CallbackQuery, state: FSMContext):
    # Зберігаємо вибраний стиль за замовчуванням у стейт (або беремо дефолт)
    data = await state.get_data()
    current_color = data.get("color", "classic")
    
    await callback.message.edit_text(
        f"🎨 **Select QR Color Theme:**\nCurrent: `{COLOR_PALETTES[current_color]['name']}`",
        reply_markup=get_color_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("color:"))
async def cb_set_color(callback: CallbackQuery, state: FSMContext):
    color_key = callback.data.split(":")[1]
    await state.update_data(color=color_key)
    
    await callback.message.edit_text(
        f"✅ Theme updated to: `{COLOR_PALETTES[color_key]['name']}`\n\nNow choose QR type or send text:",
        reply_markup=get_start_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer("Theme updated!")

@dp.callback_query(F.data.startswith("type:"))
async def cb_select_type(callback: CallbackQuery, state: FSMContext):
    qr_type = callback.data.split(":")[1]
    await state.update_data(qr_type=qr_type)
    
    prompts = {
        "url": "🌐 Send me the URL (e.g., https://github.com):",
        "text": "💬 Send me the text you want to encode:",
        "wifi": "📶 Send Wi-Fi details in format:\n`SSID;Password;WPA`",
        "tg": "✈️ Send Telegram username or link (e.g., @username):"
    }
    
    await state.set_state(QRStates.waiting_for_input)
    await callback.message.edit_text(
        prompts.get(qr_type, "Send data for QR code:") + "\n\n_(Press /start to cancel)_",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(QRStates.waiting_for_input)
async def process_qr_input(message: Message, state: FSMContext):
    user_data = await state.get_data()
    qr_type = user_data.get("qr_type", "url")
    color_key = user_data.get("color", "classic")
    
    raw_text = message.text.strip()
    
    # Форматування даних залежно від типу
    if qr_type == "wifi":
        # Формат WiFi для QR: WIFI:S:<SSID>;T:<WPA|WEP|nopass>;P:<password>;;
        parts = raw_text.split(";")
        ssid = parts[0] if len(parts) > 0 else "Network"
        password = parts[1] if len(parts) > 1 else ""
        encryption = parts[2] if len(parts) > 2 else "WPA"
        content = f"WIFI:S:{ssid};T:{encryption};P:{password};;"
    elif qr_type == "tg":
        if not raw_text.startswith("http"):
            clean_name = raw_text.lstrip("@")
            content = f"https://t.me/{clean_name}"
        else:
            content = raw_text
    else:
        content = raw_text

    # Генерація картинки
    qr_bytes = generate_qr_bytes(content, color_key)
    photo = BufferedInputFile(qr_bytes, filename="qrcode.png")
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Generate Another", callback_data="back_main")]
    ])
  
    await message.answer_photo(
        photo=photo,
        caption=f"✨ **QR Generated Successfully**\n▪ Type: `{qr_type.upper()}`\n▪ Theme: `{COLOR_PALETTES[color_key]['name']}`",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    
    )
    await state.clear()

async def main():
    logging.basicConfig(level=logging.INFO)
    print("qr_huntbot is running with enhanced UI & features!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
    
