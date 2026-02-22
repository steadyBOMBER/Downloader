import asyncio
import logging
import random
import string
import os
import tempfile
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any
from collections import defaultdict

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import FSInputFile, InputMediaVideo, InputMediaAudio, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from yt_dlp import YoutubeDL
from yt_dlp.version import __version__ as ytdlp_version

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

MAX_MEDIA_GROUP     = 10
MAX_SINGLE_SIZE_MB  = 45     # safe value under Telegram bots' 50 MB upload limit
MAX_QUEUE_SIZE      = 5
RATE_LIMIT_SECONDS  = 60

# ─── FastAPI keep-alive ────────────────────────────────────

keepalive_app = FastAPI(
    title="Telegram Downloader Keep-Alive",
    docs_url=None, redoc_url=None, openapi_url=None
)

@keepalive_app.get("/", response_class=PlainTextResponse)
async def root():
    return "Telegram downloader bot is alive 🚀"

@keepalive_app.get("/ping", response_class=PlainTextResponse)
async def ping():
    return "pong"

# ─── States ────────────────────────────────────────────────

class DownloadState(StatesGroup):
    waiting     = State()
    downloading = State()

# ─── Per-user data ─────────────────────────────────────────

user_queues: Dict[int, asyncio.Queue] = defaultdict(asyncio.Queue)
user_last_action: Dict[int, datetime] = {}

# ─── Throttling Middleware ─────────────────────────────────

class ThrottlingMiddleware:
    async def __call__(self, handler, event: types.TelegramObject, data: Dict[str, Any]):
        if not isinstance(event, types.Message):
            return await handler(event, data)

        user_id = event.from_user.id
        now = datetime.utcnow()

        if user_id in user_last_action:
            delta = now - user_last_action[user_id]
            if delta < timedelta(seconds=RATE_LIMIT_SECONDS):
                remaining = RATE_LIMIT_SECONDS - int(delta.total_seconds())
                await event.reply(f"⏳ Please wait {remaining} seconds before next request.")
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
    return any(domain in url.lower() for domain in domains)

# ─── Start & URL handler ───────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "🎥 Video / Audio Downloader Bot\n\n"
        "Send a link from:\n"
        "• YouTube\n"
        "• TikTok\n"
        "• Instagram\n"
        "• Twitter/X\n\n"
        f"• Max file size: ~{MAX_SINGLE_SIZE_MB} MB (Telegram bot limit)\n"
        f"• Cooldown: 1 request / {RATE_LIMIT_SECONDS} seconds\n"
        f"• yt-dlp version: {ytdlp_version}\n\n"
        "Just send a link and choose format!"
    )

@dp.message()
async def handle_url(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if not text or text.startswith("/"):
        return

    if not await is_supported_url(text):
        await message.reply("Only YouTube, TikTok, Instagram, Twitter/X links are supported.")
        return

    await state.update_data(url=text)

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🎥 Video", callback_data="dl:video"),
        InlineKeyboardButton(text="🎵 Audio",  callback_data="dl:audio"),
    )
    builder.row(
        InlineKeyboardButton(text="❌ Cancel", callback_data="dl:cancel")
    )

    await message.reply("Choose what you want:", reply_markup=builder.as_markup())

# ─── Callback processor ────────────────────────────────────

@dp.callback_query(F.data.startswith("dl:"))
async def process_callback(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    data = await state.get_data()
    url = data.get("url")

    if not url:
        await callback.answer("No link found. Please send a link again.", show_alert=True)
        return

    action = callback.data.split(":", 1)[1]

    if action == "cancel":
        await state.clear()
        await callback.message.edit_text("Cancelled.")
        await callback.answer()
        return

    # Video quality selection
    if action == "video":
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="720p (recommended)", callback_data="fmt:720"),
            InlineKeyboardButton(text="1080p",              callback_data="fmt:1080"),
        )
        builder.row(
            InlineKeyboardButton(text="480p (smaller file)", callback_data="fmt:480"),
            InlineKeyboardButton(text="360p (smallest)",     callback_data="fmt:360"),
        )
        builder.row(
            InlineKeyboardButton(text="Best (may exceed limit)", callback_data="fmt:best"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="dl:cancel")
        )
        await callback.message.edit_text("Choose video quality:", reply_markup=builder.as_markup())
        await callback.answer()
        return

    # Format selected → add to queue
    quality = None
    mode = action
    if action.startswith("fmt:"):
        quality = action.split(":", 1)[1]
        mode = "video"

    queue = user_queues[user_id]

    if queue.qsize() >= MAX_QUEUE_SIZE:
        await callback.message.edit_text(f"⚠️ Queue full (max {MAX_QUEUE_SIZE} items). Wait for current downloads to finish.")
        await callback.answer()
        return

    current_state = await state.get_state()
    is_already_running = current_state in [DownloadState.waiting.state, DownloadState.downloading.state]

    await queue.put((mode, url, quality))

    if is_already_running:
        await callback.message.edit_text("Added to queue (processed in order).")
    else:
        await state.set_state(DownloadState.downloading)
        asyncio.create_task(process_user_queue(user_id, callback.message, state))

    await callback.answer()

# ─── Queue processor ───────────────────────────────────────

async def process_user_queue(user_id: int, msg: types.Message, state: FSMContext):
    queue = user_queues[user_id]

    while not queue.empty():
        mode, url, quality = await queue.get()

        try:
            remaining = queue.qsize()
            await msg.edit_text(
                f"Processing {mode.upper()}...\n"
                f"{'Remaining in queue: ' + str(remaining) if remaining > 0 else 'Last item'}"
            )
            await process_content(url, mode, msg, user_id, quality)
        except Exception as e:
            logging.exception("Processing failed")
            try:
                await msg.answer(f"Error: {str(e)[:180]}")
            except:
                pass
        finally:
            queue.task_done()

    await state.clear()
    try:
        await msg.edit_text("✅ All downloads completed!")
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
            pct_str = d.get('_percent_str', '0%')
            pct = float(pct_str.rstrip('%')) if pct_str else 0.0

            if abs(pct - self.last_pct) < 2 and (time.time() - self.last_update) < 3:
                return

            bar = make_progress_bar(pct)
            text = f"📥 {bar} {pct:.1f}%"
            if '_speed_str' in d:
                text += f" • {d['_speed_str']}"
            if '_eta_str' in d:
                text += f" • ETA {d['_eta_str']}"

            await self.msg.edit_text(text)
            self.last_pct = pct
            self.last_update = time.time()
        except Exception:
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
        ydl_opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })
    else:
        height_map = {
            "360": 360,
            "480": 480,
            "720": 720,
            "1080": 1080,
            "best": None
        }
        max_h = height_map.get(quality, 720)  # default to 720p if quality missing
        if max_h:
            fmt = f"bestvideo[height<={max_h}][ext=mp4]+bestaudio[ext=m4a]/best[height<={max_h}][ext=mp4]/best[ext=mp4]/best"
        else:
            fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        ydl_opts.update({
            "format": fmt,
            "merge_output_format": "mp4",
        })

    temp_paths = []

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            is_playlist = 'entries' in info

            if is_playlist:
                entries = list(info['entries'])[:MAX_MEDIA_GROUP]
                await msg.edit_text(f"Playlist detected → processing first {len(entries)} items")
                medias = []
                for i, entry in enumerate(entries, 1):
                    path_info = await download_single(entry, ydl, is_audio)
                    temp_paths.append(path_info["path"])

                    if path_info["size_mb"] > MAX_SINGLE_SIZE_MB:
                        await msg.answer(f"#{i} skipped — too large ({path_info['size_mb']:.1f} MB)")
                        continue

                    caption = f"#{i} • {entry.get('title', '?')[:80]}\n🕒 {entry.get('duration_string', '?')}"
                    if is_audio:
                        medias.append(InputMediaAudio(media=FSInputFile(path_info["path"]), caption=caption))
                    else:
                        medias.append(InputMediaVideo(media=FSInputFile(path_info["path"]), caption=caption, supports_streaming=True))

                if medias:
                    await msg.answer_media_group(medias)

            else:
                path_info = await download_single(info, ydl, is_audio)
                temp_paths.append(path_info["path"])

                if path_info["size_mb"] > MAX_SINGLE_SIZE_MB:
                    await msg.edit_text(
                        f"File too large ({path_info['size_mb']:.1f} MB).\n"
                        "Try a lower quality (360p or 480p) or a shorter video."
                    )
                    return

                caption = (
                    f"{'🎵' if is_audio else '🎬'} {info.get('title', '?')[:90]}\n"
                    f"🕒 {info.get('duration_string', '?')}\n"
                    f"📏 ~{path_info['size_mb']:.1f} MB"
                )
                if is_audio:
                    await msg.answer_audio(
                        FSInputFile(path_info["path"]),
                        caption=caption,
                        filename=random_filename("mp3")
                    )
                else:
                    await msg.answer_video(
                        FSInputFile(path_info["path"]),
                        caption=caption,
                        supports_streaming=True,
                        filename=random_filename("mp4")
                    )

    finally:
        for p_str in temp_paths:
            p = Path(p_str)
            try:
                if p.exists():
                    p.unlink(missing_ok=True)
            except Exception:
                pass

async def download_single(info, ydl, is_audio: bool):
    ext = "mp3" if is_audio else "mp4"
    fd, temp_path_str = tempfile.mkstemp(suffix=f".{ext}")
    os.close(fd)
    temp_path = Path(temp_path_str)

    try:
        ydl.params["outtmpl"] = str(temp_path)
        ydl.download([info.get("webpage_url") or info["url"]])

        if not temp_path.exists() or temp_path.stat().st_size == 0:
            raise RuntimeError("Downloaded file is empty or missing")

        return {
            "path": str(temp_path),
            "size_mb": temp_path.stat().st_size / (1024 ** 2)
        }

    except Exception as e:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise e

# ─── Keep-alive server runner ──────────────────────────────

async def run_keepalive_server():
    port = int(os.getenv("PORT", 8000))
    config = uvicorn.Config(
        keepalive_app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        workers=1,
    )
    server = uvicorn.Server(config)
    await server.serve()

# ─── Main ──────────────────────────────────────────────────

async def main():
    print(f"[Bot] Starting • yt-dlp {ytdlp_version}")
    asyncio.create_task(run_keepalive_server())
    await dp.start_polling(
        bot,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True
    )

if __name__ == "__main__":
    asyncio.run(main())