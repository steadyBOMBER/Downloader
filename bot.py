# Python 3.10+
# pip install aiogram[fast] yt-dlp python-dotenv fastapi uvicorn[standard]

import asyncio
import logging
import random
import string
import os
import tempfile
import time
import socket
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import FSInputFile, InputMediaVideo, InputMediaAudio, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from yt_dlp import YoutubeDL

# ─── Keep-alive server ─────────────────────────────────────
import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

# ────────────────────────────────────────────────────────────

load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise ValueError("BOT_TOKEN not found in .env file")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

MAX_MEDIA_GROUP = 8
MAX_SINGLE_SIZE_MB = 48
RATE_LIMIT_SECONDS = 60  # 1 request per minute per user
QUEUE_WARNING = "You're already downloading. This link is added to queue (processed in order)."

# ─── FastAPI keep-alive app ────────────────────────────────

keepalive_app = FastAPI(
    title="Telegram Downloader Keep-Alive",
    docs_url=None,
    redoc_url=None,
    openapi_url=None
)

@keepalive_app.get("/", response_class=PlainTextResponse)
async def root():
    return "Telegram downloader bot is alive 🚀"

@keepalive_app.get("/ping", response_class=PlainTextResponse)
async def ping():
    return "pong"

# ─── States ────────────────────────────────────────────────

class DownloadState(StatesGroup):
    waiting = State()      # has pending downloads
    downloading = State()  # currently processing one

# ─── Per-user data ─────────────────────────────────────────

user_queues: Dict[int, asyncio.Queue] = defaultdict(asyncio.Queue)
user_last_action: Dict[int, datetime] = {}

# ─── Throttling Middleware ─────────────────────────────────

class ThrottlingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler,
        event: types.TelegramObject,
        data: Dict[str, Any]
    ):
        if not isinstance(event, types.Message):
            return await handler(event, data)

        user_id = event.from_user.id
        now = datetime.utcnow()

        if user_id in user_last_action:
            delta = now - user_last_action[user_id]
            if delta < timedelta(seconds=RATE_LIMIT_SECONDS):
                remaining = RATE_LIMIT_SECONDS - int(delta.total_seconds())
                await event.reply(f"⏳ Wait {remaining}s before new request.")
                return

        user_last_action[user_id] = now
        return await handler(event, data)

dp.message.middleware(ThrottlingMiddleware())

# ─── Helpers ───────────────────────────────────────────────

def random_filename(ext="mp4"):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=12)) + f".{ext}"

def make_progress_bar(percentage: float, width: int = 10) -> str:
    filled = int(width * percentage / 100)
    return f"[{'🟩' * filled}{'⬜' * (width - filled)}] {percentage:.1f}%"

async def is_supported_url(url: str) -> bool:
    domains = ["youtube.com", "youtu.be", "tiktok.com", "instagram.com", "twitter.com", "x.com"]
    return any(d in url.lower() for d in domains)

# ─── Start & URL handler ───────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "🎥 Video/Audio downloader\n\n"
        "Send YouTube / TikTok / Instagram / X link\n"
        f"Limit: ~{MAX_SINGLE_SIZE_MB} MB per file • 1 download/min"
    )

@dp.message()
async def handle_url(message: types.Message, state: FSMContext):
    text = message.text.strip()
    url = text if not text.startswith("/") else (text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else "")

    if not url or not await is_supported_url(url):
        await message.reply("Supported: YouTube, TikTok, Instagram, Twitter/X.\nSend valid link.")
        return

    await state.update_data(url=url)

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🎥 Video", callback_data="dl:video"),
        InlineKeyboardButton(text="🎵 Audio", callback_data="dl:audio"),
    )
    builder.row(
        InlineKeyboardButton(text="🖼️ Thumbnail", callback_data="dl:thumb"),
        InlineKeyboardButton(text="❌ Cancel", callback_data="dl:cancel")
    )

    await message.reply("What do you want?", reply_markup=builder.as_markup())

# ─── Callback processor ────────────────────────────────────

@dp.callback_query(F.data.startswith("dl:"))
async def process_callback(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    url = data.get("url")

    parts = callback.data.split(":")
    if len(parts) < 2:
        await callback.answer("Error", show_alert=True)
        return

    mode = parts[1]

    if mode == "cancel":
        await state.clear()
        await callback.message.edit_text("Cancelled.")
        await callback.answer()
        return

    if not url:
        await callback.answer("No URL saved", show_alert=True)
        return

    user_id = callback.from_user.id

    # Video format selection
    if mode == "video":
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="720p (recommended)", callback_data="fmt:720"),
            InlineKeyboardButton(text="1080p", callback_data="fmt:1080"),
        )
        builder.row(
            InlineKeyboardButton(text="Best (may be large)", callback_data="fmt:best"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="dl:cancel")
        )
        await callback.message.edit_text("Choose video quality:", reply_markup=builder.as_markup())
        await callback.answer()
        return

    # Format chosen → start download
    if parts[0] == "fmt":
        quality = parts[1]
        await state.update_data(quality=quality)
        mode = "video"

    queue = user_queues[user_id]

    current_state = await state.get_state()
    if current_state in [DownloadState.waiting.state, DownloadState.downloading.state]:
        await queue.put((mode, url, quality if mode == "video" else None))
        await callback.message.edit_text(QUEUE_WARNING)
        await callback.answer()
        return

    await state.set_state(DownloadState.downloading)
    await queue.put((mode, url, quality if mode == "video" else None))
    asyncio.create_task(process_user_queue(user_id, callback.message, state))

    await callback.answer()

# ─── Queue processor ───────────────────────────────────────

async def process_user_queue(user_id: int, original_msg: types.Message, state: FSMContext):
    queue = user_queues[user_id]

    while not queue.empty():
        try:
            mode, url, quality = await queue.get()

            await original_msg.edit_text(f"🔄 Queue position: {queue.qsize() + 1}\nStarting {mode.upper()}...")

            await process_content(url, mode, original_msg, user_id, quality)

        except Exception as e:
            logging.exception("Queue item error")
            try:
                await original_msg.answer(f"Error in queued item: {str(e)[:120]}")
            except:
                pass

        finally:
            queue.task_done()

    await state.clear()
    try:
        await original_msg.edit_text("✅ All downloads processed!")
    except:
        pass

# ─── Core download logic ───────────────────────────────────

class ProgressTracker:
    def __init__(self, msg: types.Message):
        self.msg = msg
        self.last_update = time.time()
        self.last_pct = -1

    async def __call__(self, d):
        if d['status'] != 'downloading':
            return
        try:
            pct = float(d.get('_percent_str', '0%').rstrip('%'))
            if abs(pct - self.last_pct) < 2 and (time.time() - self.last_update) < 2:
                return

            bar = make_progress_bar(pct)
            text = f"📥 {bar} {pct:.1f}%\n⚡ {d.get('_speed_str', '?')}\n⏳ {d.get('_eta_str', '?')}"
            await self.msg.edit_text(text)
            self.last_pct = pct
            self.last_update = time.time()
        except:
            pass

async def process_content(url: str, mode: str, msg: types.Message, user_id: int, quality: str = None):
    is_audio = mode == "audio"
    progress = ProgressTracker(msg)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "retries": 10,
        "progress_hooks": [progress],
    }

    if is_audio:
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    else:
        height_map = {"720": 720, "1080": 1080, "best": None}
        max_h = height_map.get(quality, 1080)
        fmt = f"bestvideo[height<={max_h}][ext=mp4]+bestaudio[ext=m4a]/best[height<={max_h}][ext=mp4]/best" if max_h else "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        ydl_opts["format"] = fmt
        ydl_opts["merge_output_format"] = "mp4"

    temp_paths = []

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            is_playlist = 'entries' in info

            if is_playlist:
                entries = list(info['entries'])[:MAX_MEDIA_GROUP]
                await msg.edit_text(f"Playlist: {len(info['entries'])} items → taking first {len(entries)}")
                medias = []
                for i, e in enumerate(entries, 1):
                    tmp = await download_single(e, ydl, is_audio)
                    temp_paths.append(tmp["path"])
                    if tmp["size_mb"] > MAX_SINGLE_SIZE_MB:
                        await msg.answer(f"Skipped #{i} — too big ({tmp['size_mb']:.1f} MB)")
                        continue
                    caption = f"#{i} • {e.get('title','?')[:80]}\n🕒 {e.get('duration_string','?')}"
                    if is_audio:
                        medias.append(InputMediaAudio(media=FSInputFile(tmp["path"]), caption=caption))
                    else:
                        medias.append(InputMediaVideo(media=FSInputFile(tmp["path"]), caption=caption, supports_streaming=True))
                if medias:
                    await msg.answer_media_group(medias)
            else:
                tmp = await download_single(info, ydl, is_audio)
                temp_paths.append(tmp["path"])
                if tmp["size_mb"] > MAX_SINGLE_SIZE_MB:
                    await msg.edit_text(f"Too large ({tmp['size_mb']:.1f} MB)")
                    return
                caption = f"{'🎵' if is_audio else '🎬'} {info.get('title','?')[:90]}\n🕒 {info.get('duration_string','?')}\n📏 ~{tmp['size_mb']:.1f} MB"
                if is_audio:
                    await msg.answer_audio(FSInputFile(tmp["path"]), caption=caption, filename=random_filename("mp3"))
                else:
                    await msg.answer_video(FSInputFile(tmp["path"]), caption=caption, supports_streaming=True, filename=random_filename("mp4"))

    finally:
        for p in temp_paths:
            if os.path.exists(p):
                try: os.remove(p)
                except: pass

async def download_single(info, ydl, is_audio):
    ext = "mp3" if is_audio else "mp4"
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tf:
        path = tf.name
    ydl.params["outtmpl"] = path
    ydl.download([info["webpage_url"] or info["url"]])
    return {"path": path, "size_mb": os.path.getsize(path) / (1024**2)}

# ─── Keep-alive server runner ──────────────────────────────

async def run_keepalive_server():
    port = int(os.getenv("PORT", 8000))
    host = "0.0.0.0"

    print(f"[Keep-alive] Starting HTTP server on {host}:{port}")

    # Optional: log detected outbound IP (useful for debugging)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        print(f"[Keep-alive] Detected outbound IP: {s.getsockname()[0]}")
        s.close()
    except Exception:
        print("[Keep-alive] Could not detect outbound IP")

    config = uvicorn.Config(
        keepalive_app,
        host=host,
        port=port,
        log_level="info",
        workers=1,
        timeout_keep_alive=120,
    )
    server = uvicorn.Server(config)
    await server.serve()

# ─── Main ──────────────────────────────────────────────────

async def main():
    # Start keep-alive HTTP server in background
    asyncio.create_task(run_keepalive_server())

    print("[Bot] Starting aiogram polling...")
    await dp.start_polling(
        bot,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True  # clean start
    )

if __name__ == "__main__":
    asyncio.run(main())