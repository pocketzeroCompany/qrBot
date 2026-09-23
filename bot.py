import asyncio
import io
import logging
import os
import sys
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
import qrcode

TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Стани для FSM (покроковий діалог)
class QRStates(StatesGroup):
    waiting_for_text = State()
    choosing_style = State()
    choosing_color = State()

# Словник кольорів (назва: (колір QR, колір фону))
COLORS = {
    "black": ("black", "white"),
    "blue": ("#1E3A8A", "white"),
    "green": ("#065F46", "white"),
    "purple": ("#581C87", "white"),
    "matrix": ("#00FF00", "black") # Стиль хакера: зелений на чорному
}

def generate_qr_bytes(text: str, fill_color: str, back_color: str) -> bytes:
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(text)
    qr.make(fit=True)

    img = qr.make_image(fill_color=fill_color, back_color=back_color)

    output = io.BytesIO()
    img.save(output, format="PNG")
    output.seek(0)
    return output.getvalue()

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🜲Hi! I'm @qrhunt_bot.
Ready to track down any text or link into a clean QR code. Just send it over!"
    )
    await state.set_state(QRStates.waiting_for_text)

@dp.message(QRStates.waiting_for_text, F.text)
async def process_text(message: Message, state: FSMContext):
    await state.update_data(qr_text=message.text)
    
    # Кнопки вибору стилю
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Classic (Black & White)", callback_data="style:classic")],
        [InlineKeyboardButton(text="Colored Design", callback_data="style:colored")]
    ])
    
    await message.answer("Choose the design style for your QR code:", reply_markup=keyboard)
    await state.set_state(QRStates.choosing_style)

@dp.callback_query(QRStates.choosing_style, F.data.startswith("style:"))
async def process_style(callback: CallbackQuery, state: FSMContext):
    style = callback.data.split(":")[1]
    
    if style == "classic":
        data = await state.get_data()
        text = data.get("qr_text")
        
        qr_bytes = generate_qr_bytes(text, "black", "white")
        photo = BufferedInputFile(qr_bytes, filename="qrcode.png")
        
        await callback.message.answer_photo(photo=photo, caption="Your classic QR code is ready.")
        await callback.message.delete()
        await state.clear()
    else:
        # Якщо обрано кольоровий, показуємо вибір кольорів
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Blue", callback_data="color:blue"), InlineKeyboardButton(text="Green", callback_data="color:green")],
            [InlineKeyboardButton(text="Purple", callback_data="color:purple"), InlineKeyboardButton(text="Matrix (Green on Black)", callback_data="color:matrix")]
        ])
        await callback.message.edit_text("Choose a color scheme:", reply_markup=keyboard)
        await state.set_state(QRStates.choosing_color)
    
    await callback.answer()

@dp.callback_query(QRStates.choosing_color, F.data.startswith("color:"))
async def process_color(callback: CallbackQuery, state: FSMContext):
    color_key = callback.data.split(":")[1]
    data = await state.get_data()
    text = data.get("qr_text")
    
    fill_color, back_color = COLORS.get(color_key, ("black", "white"))
    
    qr_bytes = generate_qr_bytes(text, fill_color, back_color)
    photo = BufferedInputFile(qr_bytes, filename="qrcode.png")
    
    await callback.message.answer_photo(photo=photo, caption="Your colored QR code is ready.")
    await callback.message.delete()
    await state.clear()
    await callback.answer()

async def main():
    logging.basicConfig(level=logging.INFO)
    print("qr_huntbot is running!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
                                                                                                     
