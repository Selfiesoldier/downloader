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
OWNER_USER_ID=your_numeric_telegram_id_here
MAX_FILE_SIZE_MB=2000
```

---

## 🍪 Bypassing YouTube Anti-Bot & Instagram Login

YouTube and Instagram frequently block unauthenticated cloud/datacenter/VPN requests with:
- *YouTube: "Sign in to confirm you're not a bot"*
- *Instagram: "Login required"*

### How to Authenticate Your Bot via Telegram Chat:
1. **Export Cookies from Your Browser:**
   - On PC (Chrome/Firefox/Brave) or Android (Kiwi Browser), install the free extension **[Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)** (open source).
   - Log into your YouTube and/or Instagram account.
   - Click the extension icon and click **Export**.
2. **Send to Your Telegram Bot:**
   - Send the exported `.txt` file directly as a document in your Telegram bot chat:
     - `youtube_cookies.txt` (for YouTube)
     - `instagram_cookies.txt` (for Instagram)
     - Or `cookies.txt` (universal)
   - The bot will validate, save to `cookies/`, and activate your session instantly without restarting!
3. **Check Status:**
   - Type `/cookies` in chat to see which sessions are currently active.

### Start Bot:
```bash
python bot.py
```

To run 24/7 in the background on Termux:
```bash
termux-wake-lock
nohup python bot.py > bot.log 2>&1 &
```
