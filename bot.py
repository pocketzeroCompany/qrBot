import asyncio
import io
import logging
import sys
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, Message
import qrcode

# Встав свій токен від @BotFather
import os

TOKEN = os.getenv("BOT_TOKEN")


bot = Bot(token=TOKEN)
dp = Dispatcher()


def generate_qr_bytes(text: str) -> bytes:
  """Generates a QR code and returns its bytes for Telegram."""
  qr = qrcode.QRCode(
      version=1,
      error_correction=qrcode.constants.ERROR_CORRECT_M,
      box_size=10,
      border=4,
  )
  qr.add_data(text)
  qr.make(fit=True)

  img = qr.make_image(fill_color="black", back_color="white")

  output = io.BytesIO()
  img.save(output, format="PNG")
  output.seek(0)
  return output.getvalue()


@dp.message(CommandStart())
async def cmd_start(message: Message):
  await message.answer(
      "Hello! I am @qr_huntbot.\n\n"
      "Send me any text, link, or contact info, and I will instantly turn it"
      " into a QR code."
  )


@dp.message(F.text)
async def make_qr_handler(message: Message):
  text = message.text

  try:
    qr_bytes = generate_qr_bytes(text)
    photo = BufferedInputFile(qr_bytes, filename="qrcode.png")

    await message.answer_photo(
        photo=photo, caption="Your QR code has been generated."
    )
  except Exception as e:
    await message.answer(f"An error occurred: {e}")


async def main():
  logging.basicConfig(level=logging.INFO)
  print("qr_huntbot is running!")
  await dp.start_polling(bot)


if __name__ == "__main__":
  if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
  asyncio.run(main())
