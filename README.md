# 🚀 Universal 2GB Content Downloader Telegram Bot

An all-in-one media downloader Telegram Bot powered by `python-telegram-bot`, `yt-dlp`, and `Telethon`.

Automatically extracts and delivers media up to **2,000 MB (2 GB)** directly to your Telegram chat without crashing or 50MB limitations!

## 🌟 Supported Platforms
- 📺 **YouTube**: Full Movies, HD Videos (4K/2K/1080p/720p), Shorts, Audio/MP3
- 📸 **Instagram**: Reels, Posts, Carousels, Stories
- 🎵 **TikTok**: HD watermark-free videos and audio
- 🐦 **Twitter / X**: Videos, Multi-video tweets, GIFs
- 📌 **Pinterest**: Video pins and media
- 🤖 **Reddit**: Clips with audio merged
- 📘 **Facebook**: Reels and public videos
- ☁️ **SoundCloud / Bandcamp**: High-quality MP3 audio

---

## 📱 Termux Quick Start (Android Phone)

In Termux on your phone, update your repo:

```bash
cd ~/downloader || cd downloader
git pull
pip install -r requirements.txt
```

### Configure `.env`:
```bash
nano .env
```
Ensure your `.env` contains your private credentials (never share these publicly):
```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_API_ID=your_api_id_here
TELEGRAM_API_HASH=your_api_hash_here
MAX_FILE_SIZE_MB=2000
```

### Start Bot:
```bash
python bot.py
```

To run 24/7 in the background on Termux:
```bash
termux-wake-lock
nohup python bot.py > bot.log 2>&1 &
```
