import asyncio
import html
import json
import logging
import os
import re
import secrets
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputSticker,
    LinkPreviewOptions,
    Message,
)
from PIL import Image, ImageOps

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cornerlessbot")

SIZE = 512
MAX_IMAGE_BYTES = 512 * 1024
MAX_VIDEO_BYTES = 256 * 1024
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # Bot API download limit
VIDEO_SEM = asyncio.Semaphore(2)  # keep ffmpeg from overloading the server
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# Short link name: starts with a letter, only letters/digits/underscores,
# no double underscores, doesn't end with an underscore.
NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*")

# Where user -> active pack is stored. On Railway, mount a Volume and set
# DATA_DIR to its path (e.g. /data), otherwise it resets on every redeploy.
DATA_FILE = Path(os.getenv("DATA_DIR", ".")) / "packs.json"

router = Router()


class NewPack(StatesGroup):
    title = State()
    name = State()


# ---------- keyboards ----------

CREATE_KB = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="➕ Create pack", callback_data="newpack")]]
)
SKIP_KB = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="⏭ Skip", callback_data="skip_name")]]
)
MODE_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="✂️ Crop to center", callback_data="mode:crop")],
        [InlineKeyboardButton(text="🖼 Fit (transparent edges)", callback_data="mode:fit")],
    ]
)


def open_pack_kb(name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 Open pack", url=f"https://t.me/addstickers/{name}")]
        ]
    )


# ---------- storage ----------

def _load() -> dict:
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_pack(user_id: int):
    return _load().get(str(user_id))


def save_pack(user_id: int, title: str, name: str) -> None:
    data = _load()
    data[str(user_id)] = {"title": title, "name": name}
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ---------- conversion ----------

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


# ---------- helpers ----------

def get_media(msg: Message):
    """Returns (file_id, kind, size) where kind is 'image' or 'video', or None."""
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


async def pack_exists(bot: Bot, name: str) -> bool:
    try:
        await bot.get_sticker_set(name)
        return True
    except TelegramBadRequest:
        return False


async def add_to_pack(bot: Bot, user_id: int, pack: dict, path: Path, is_video: bool) -> None:
    sticker = InputSticker(
        sticker=FSInputFile(path),
        format="video" if is_video else "static",
        emoji_list=["🟦"],
    )
    if await pack_exists(bot, pack["name"]):
        await bot.add_sticker_to_set(user_id=user_id, name=pack["name"], sticker=sticker)
    else:
        # Telegram creates a pack only together with its first sticker
        await bot.create_new_sticker_set(
            user_id=user_id, name=pack["name"], title=pack["title"], stickers=[sticker]
        )


# ---------- commands ----------

@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    text = (
        "👋 <b>Welcome to Cornerless!</b>\n\n"
        "I make stickers in <b>1:1 with no rounded corners</b> "
        "from photos, videos and GIFs.\n\n"
        "1️⃣ Create a sticker pack\n"
        "2️⃣ Send me photos, videos or GIFs\n"
        "3️⃣ Pick <b>Crop</b> or <b>Fit</b>, and I'll add them to your pack\n\n"
        "Videos and GIFs are trimmed to 3 seconds."
    )
    pack = get_pack(message.from_user.id)
    if pack:
        text += (
            f'\n\n📦 Current pack: <a href="https://t.me/addstickers/{pack["name"]}">'
            f'{html.escape(pack["title"])}</a>'
        )
    await message.answer(text, reply_markup=CREATE_KB, link_preview_options=NO_PREVIEW)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled 👌")


@router.message(Command("newpack"))
async def cmd_newpack(message: Message, state: FSMContext):
    await ask_title(message, state)


# ---------- pack creation flow ----------

async def ask_title(message: Message, state: FSMContext):
    await state.set_state(NewPack.title)
    await message.answer(
        "📝 Send me a <b>title</b> for your pack (1–64 characters).\n\n"
        "For example: <i>My Cats 🐱</i>\n\n"
        "Send /cancel to stop."
    )


@router.callback_query(F.data == "newpack")
async def on_newpack(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await ask_title(call.message, state)


@router.message(NewPack.title, F.text, ~F.text.startswith("/"))
async def on_title(message: Message, state: FSMContext, bot: Bot):
    title = message.text.strip()
    if not 1 <= len(title) <= 64:
        await message.reply("The title must be 1–64 characters. Try again.")
        return
    await state.update_data(title=title)
    await state.set_state(NewPack.name)
    me = await bot.me()
    await message.answer(
        f"Title: <b>{html.escape(title)}</b>\n\n"
        "🔗 Now send me a <b>link name</b> for your pack.\n\n"
        "For example: <code>https://t.me/addstickers/Cats</code>\n"
        "Only English letters, digits and underscores.\n"
        f"I'll add <code>_by_{me.username}</code> at the end, Telegram requires it.\n\n"
        "Or press <b>Skip</b> and I'll generate the link for you.",
        reply_markup=SKIP_KB,
    )


async def finish_pack(msg: Message, state: FSMContext, bot: Bot, user_id: int, short):
    data = await state.get_data()
    title = data.get("title")
    if not title:
        await state.clear()
        await msg.answer("Something went wrong. Press /newpack to start again.")
        return

    me = await bot.me()
    suffix = f"_by_{me.username}"

    if short is None:
        short = f"pack_{secrets.token_hex(4)}"
    else:
        if not NAME_RE.fullmatch(short):
            await msg.answer(
                "❌ Only English letters, digits and underscores. "
                "It must start with a letter, with no double or trailing underscores.\n\n"
                "Try again or press Skip.",
                reply_markup=SKIP_KB,
            )
            return
        if len(short) + len(suffix) > 64:
            await msg.answer("❌ That name is too long. Try a shorter one.", reply_markup=SKIP_KB)
            return

    full = short + suffix
    if await pack_exists(bot, full):
        await msg.answer("❌ This link is already taken. Try another one.", reply_markup=SKIP_KB)
        return

    save_pack(user_id, title, full)
    await state.clear()
    await msg.answer(
        "✅ <b>Your pack is ready!</b>\n\n"
        f"<b>Title:</b> {html.escape(title)}\n"
        f"<b>Link:</b> https://t.me/addstickers/{full}\n\n"
        "Now send me photos, videos or GIFs and I'll add them to your pack.\n"
        "<i>The link starts working after your first sticker.</i>",
        link_preview_options=NO_PREVIEW,
    )


@router.message(NewPack.name, F.text, ~F.text.startswith("/"))
async def on_name(message: Message, state: FSMContext, bot: Bot):
    await finish_pack(message, state, bot, message.from_user.id, message.text.strip())


@router.callback_query(NewPack.name, F.data == "skip_name")
async def on_skip(call: CallbackQuery, state: FSMContext, bot: Bot):
    await call.answer()
    await finish_pack(call.message, state, bot, call.from_user.id, None)


@router.callback_query(F.data == "skip_name")
async def on_skip_stale(call: CallbackQuery):
    await call.answer("This step has expired. Send /newpack to start again.", show_alert=True)


# ---------- media -> sticker ----------

@router.message(F.photo | F.video | F.animation | F.video_note | F.document)
async def on_media(message: Message, state: FSMContext):
    if await state.get_state():
        await message.reply("Let's finish creating your pack first, or send /cancel.")
        return

    if not get_pack(message.from_user.id):
        await message.reply("You don't have a pack yet. Create one first 👇", reply_markup=CREATE_KB)
        return

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
    user_id = call.from_user.id

    pack = get_pack(user_id)
    if not pack:
        await call.answer("Create a pack first: /newpack", show_alert=True)
        return

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

        try:
            await add_to_pack(bot, user_id, pack, dst, kind == "video")
        except TelegramBadRequest as e:
            log.exception("Failed to add to pack")
            if "STICKERS_TOO_MUCH" in e.message:
                await msg.edit_text(
                    "📦 Your pack is full (120 stickers). Create a new one with /newpack."
                )
            else:
                await msg.edit_text(f"😔 Couldn't add to the pack: {e.message}")
            return

    await msg.edit_text(
        f"✅ Added to <b>{html.escape(pack['title'])}</b>!",
        reply_markup=open_pack_kb(pack["name"]),
    )


@router.message(F.text)
async def on_text(message: Message):
    await message.answer("Send me a photo, video or GIF 🙂\nTo make a new pack, use /newpack.")


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
        
