# Telegram Media Downloader Bot

Download videos, audio, playlists from YouTube, TikTok, Instagram, Twitter/X.

Features:
- Per-user queue & rate limiting
- Video quality selection (720p / 1080p / best)
- Progress bar during download
- Keep-alive HTTP server for Render/Railway/Fly.io

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env         # then edit BOT_TOKEN
python bot.py