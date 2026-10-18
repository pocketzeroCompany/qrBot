import asyncio
import logging
import os
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputSticker,
    Message,
)
from PIL import Image, ImageOps

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cornerlessbot")

SIZE = 512
MAX_IMAGE_BYTES = 512 * 1024
MAX_VIDEO_BYTES = 256 * 1024
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # ліміт Bot API на скачування
VIDEO_SEM = asyncio.Semaphore(2)  # щоб ffmpeg не вбив сервер

router = Router()

MODE_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="✂️ Crop to center", callback_data="mode:crop")],
        [InlineKeyboardButton(text="🖼 Fit (transparent edges)", callback_data="mode:fit")],
    ]
)
ADD_KB = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="➕ Add to my pack", callback_data="addpack")]]
)


# ---------- конвертація ----------

def convert_image(src: Path, dst: Path, mode: str) -> None:
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGBA")
        if mode == "crop":
            out = ImageOps.fit(im, (SIZE, SIZE), Image.LANCZOS)
        else:
            im = ImageOps.contain(im, (SIZE, SIZE), Image.LANCZOS)
            out = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
            out.paste(im, ((SIZE - im.width) // 2, (SIZE - im.height) // 2))

    for quality in (95, 85, 75, 60, 45, 30):
        out.save(dst, "WEBP", quality=quality, method=6)
        if dst.stat().st_size <= MAX_IMAGE_BYTES:
            return
    raise ValueError("Could not compress the image under 512 KB")


async def convert_video(src: Path, dst: Path, mode: str) -> None:
    if mode == "crop":
        vf = (
            f"scale={SIZE}:{SIZE}:force_original_aspect_ratio=increase,"
            f"crop={SIZE}:{SIZE},fps=30,format=yuva420p"
        )
    else:
        vf = (
            f"format=rgba,scale={SIZE}:{SIZE}:force_original_aspect_ratio=decrease,"
            f"pad={SIZE}:{SIZE}:(ow-iw)/2:(oh-ih)/2:color=black@0,fps=30,format=yuva420p"
        )

    for bitrate in ("600k", "450k", "300k", "200k", "120k"):
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(src),
            "-t", "3",
            "-an",
            "-vf", vf,
            "-c:v", "libvpx-vp9",
            "-pix_fmt", "yuva420p",
            "-b:v", bitrate,
            "-maxrate", bitrate,
            "-auto-alt-ref", "0",
            "-deadline", "good",
            "-cpu-used", "4",
            "-row-mt", "1",
            "-metadata:s:v:0", "alpha_mode=1",
            str(dst),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(stderr.decode(errors="ignore"))
        if dst.stat().st_size <= MAX_VIDEO_BYTES:
            return
    raise ValueError("Could not compress the video under 256 KB")


# ---------- хелпери ----------

def get_media(msg: Message):
    """Повертає (file_id, kind, size) де kind = 'image' | 'video', або None."""
    if msg.photo:
        p = msg.photo[-1]
        return p.file_id, "image", p.file_size
    if msg.animation:
        return msg.animation.file_id, "video", msg.animation.file_size
    if msg.video:
        return msg.video.file_id, "video", msg.video.file_size
    if msg.video_note:
        return msg.video_note.file_id, "video", msg.video_note.file_size
    if msg.document:
        mime = msg.document.mime_type or ""
        if mime == "image/gif" or mime.startswith("video/"):
            return msg.document.file_id, "video", msg.document.file_size
        if mime.startswith("image/"):
            return msg.document.file_id, "image", msg.document.file_size
    return None


# ---------- хендлери ----------

@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 <b>Cornerless</b> makes stickers in <b>1:1 with no rounded corners</b>.\n\n"
        "Send me a <b>photo, video or GIF</b>, pick a mode, and I'll give you a ready sticker.\n\n"
        "✂️ <b>Crop</b>: fills the whole square, edges get cut off\n"
        "🖼 <b>Fit</b>: the whole frame stays, extra space is transparent\n\n"
        "Videos and GIFs are trimmed to 3 seconds.\n"
        "You can add the finished sticker to your own pack with the button under the file."
    )


@router.message(F.photo | F.video | F.animation | F.video_note | F.document)
async def on_media(message: Message):
    media = get_media(message)
    if not media:
        await message.reply("Send me a photo, video or GIF 🙂")
        return
    _, _, size = media
    if size and size > MAX_DOWNLOAD_BYTES:
        await message.reply("File is too large (20 MB max) 😔")
        return
    await message.reply("How should I make it square?", reply_markup=MODE_KB)


@router.callback_query(F.data.startswith("mode:"))
async def on_mode(call: CallbackQuery, bot: Bot):
    mode = call.data.split(":", 1)[1]
    msg = call.message
    orig = msg.reply_to_message if isinstance(msg, Message) else None
    media = get_media(orig) if orig else None
    if not media:
        await call.answer("Couldn't find the file, please send it again", show_alert=True)
        return

    file_id, kind, _ = media
    await call.answer()
    await msg.edit_text("⏳ Making your sticker...")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / "src"
        dst = tmp_dir / ("sticker.webp" if kind == "image" else "sticker.webm")
        try:
            await bot.download(file_id, destination=src)
            if kind == "image":
                await asyncio.to_thread(convert_image, src, dst, mode)
            else:
                async with VIDEO_SEM:
                    await convert_video(src, dst, mode)
        except Exception:
            log.exception("Conversion failed")
            await msg.edit_text("😔 Couldn't make a sticker from this file.")
            return

        await msg.answer_document(
            FSInputFile(dst, filename=dst.name),
            caption="Done ✅",
            reply_markup=ADD_KB,
        )
    await msg.delete()


@router.callback_query(F.data == "addpack")
async def on_addpack(call: CallbackQuery, bot: Bot):
    msg = call.message
    doc = msg.document if isinstance(msg, Message) else None
    if not doc:
        await call.answer("File not found", show_alert=True)
        return

    is_video = (doc.file_name or "").endswith(".webm")
    me = await bot.me()
    user_id = call.from_user.id
    # окремі паки для фото і відео, щоб не змішувати формати
    prefix = "vid" if is_video else "img"
    pack_name = f"{prefix}{user_id}_by_{me.username}"
    pack_title = "Cornerless videos" if is_video else "Cornerless images"

    await call.answer("Adding...")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ("s.webm" if is_video else "s.webp")
        await bot.download(doc.file_id, destination=path)
        sticker = InputSticker(
            sticker=FSInputFile(path),
            format="video" if is_video else "static",
            emoji_list=["🟦"],
        )
        try:
            try:
                await bot.get_sticker_set(pack_name)
                exists = True
            except TelegramBadRequest:
                exists = False

            if exists:
                await bot.add_sticker_to_set(user_id=user_id, name=pack_name, sticker=sticker)
            else:
                await bot.create_new_sticker_set(
                    user_id=user_id, name=pack_name, title=pack_title, stickers=[sticker]
                )
        except TelegramBadRequest as e:
            log.exception("Failed to add to pack")
            await msg.answer(f"😔 Couldn't add to the pack: {e.message}")
            return

    await msg.answer(
        f'✅ Added! <a href="https://t.me/addstickers/{pack_name}">Open pack</a>'
    )


async def main():
    bot = Bot(
        token=os.environ["BOT_TOKEN"],
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
    
