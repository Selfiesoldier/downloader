import asyncio
import html
import logging
import math
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from dotenv import load_dotenv

import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Telethon for high-speed 2GB MTProto parallel uploads
from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeVideo
from fast_telethon import fast_upload_file

import yt_dlp
from extractors import (
    CookielessExtractorEngine,
    get_cookieless_ydl_opts,
    extract_smart_formats,
    extract_media_info,
)

# --- LOGGING CONFIGURATION ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("UniversalDownloader")

# --- ENVIRONMENT & CONFIGURATION ---
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is missing in .env file!")

# Telethon API credentials for unlocking 2,000 MB (2 GB) direct MTProto uploads
API_ID_STR = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
API_ID = int(API_ID_STR) if API_ID_STR and API_ID_STR.isdigit() else None

# Optional Owner Telegram ID for administrative access (cookie management)
OWNER_USER_ID_STR = os.getenv("OWNER_USER_ID")
OWNER_USER_ID = int(OWNER_USER_ID_STR) if OWNER_USER_ID_STR and OWNER_USER_ID_STR.strip().isdigit() else None

# Dedicated cookies storage directory
COOKIES_DIR = BASE_DIR / "cookies"
COOKIES_DIR.mkdir(parents=True, exist_ok=True)

# Helper to locate FFmpeg binary safely across Windows, Linux, and Android Termux
def resolve_ffmpeg() -> str | None:
    termux_ffmpeg = "/data/data/com.termux/files/usr/bin/ffmpeg"
    if os.path.exists(termux_ffmpeg) and os.access(termux_ffmpeg, os.X_OK):
        return termux_ffmpeg
    which_ffmpeg = shutil.which("ffmpeg")
    if which_ffmpeg:
        return which_ffmpeg
    local_bin = BASE_DIR / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if local_bin.exists():
        return str(local_bin)
    return None

FFMPEG_PATH = resolve_ffmpeg()

MAX_TELEGRAM_FILE_MB = int(os.getenv("MAX_FILE_SIZE_MB", 2000))  # 2 GB limit
MAX_TELEGRAM_BYTES = MAX_TELEGRAM_FILE_MB * 1024 * 1024

# Regex to find URLs
URL_REGEX = re.compile(
    r'(https?://(?:www\.|(?!www))[a-zA-Z0-9][a-zA-Z0-9-]+[a-zA-Z0-9]\.[^\s]{2,}|www\.[a-zA-Z0-9][a-zA-Z0-9-]+[a-zA-Z0-9]\.[^\s]{2,}|https?://[^\s]+)',
    re.IGNORECASE
)

def resolve_cookies(url: str | None = None) -> str | None:
    """
    Locates the most specific active Netscape cookie file:
    1. COOKIES_FILE from .env (if set and file exists)
    2. Domain-specific cookie files:
       - Instagram: cookies/instagram_cookies.txt, instagram_cookies.txt
       - YouTube: cookies/youtube_cookies.txt, youtube_cookies.txt
    3. Universal cookie file: cookies/cookies.txt, cookies.txt
    """
    env_cookies = os.getenv("COOKIES_FILE")
    if env_cookies:
        p = Path(env_cookies)
        if not p.is_absolute():
            p = BASE_DIR / p
        if p.exists() and p.stat().st_size > 0:
            return str(p)

    domain_candidates = []
    if url:
        url_l = url.lower()
        if "instagram.com" in url_l:
            domain_candidates = [
                COOKIES_DIR / "instagram_cookies.txt",
                BASE_DIR / "instagram_cookies.txt",
                COOKIES_DIR / "instagram.cookies",
                COOKIES_DIR / "ig_cookies.txt",
            ]
        elif "youtube.com" in url_l or "youtu.be" in url_l:
            domain_candidates = [
                COOKIES_DIR / "youtube_cookies.txt",
                BASE_DIR / "youtube_cookies.txt",
                COOKIES_DIR / "youtube.cookies",
                COOKIES_DIR / "yt_cookies.txt",
            ]

    for candidate in domain_candidates:
        if candidate.exists() and candidate.stat().st_size > 0:
            return str(candidate)

    universal_candidates = [
        COOKIES_DIR / "cookies.txt",
        BASE_DIR / "cookies.txt",
        COOKIES_DIR / "universal_cookies.txt",
    ]
    for candidate in universal_candidates:
        if candidate.exists() and candidate.stat().st_size > 0:
            return str(candidate)

    return None

def validate_netscape_cookies(content: str) -> bool:
    """Verifies that the content contains standard Netscape/HTTP cookie records."""
    if "# Netscape HTTP Cookie File" in content or "# HTTP Cookie File" in content:
        return True
    lines = [l.strip() for l in content.splitlines() if l.strip() and not l.startswith('#')]
    valid_records = 0
    for l in lines:
        parts = l.split('\t')
        if len(parts) >= 7:
            valid_records += 1
    return valid_records >= 2

async def safe_edit_message(message, text: str, **kwargs):
    """Safely edit a Telegram message with retry on mobile network drops."""
    for attempt in range(2):
        try:
            return await message.edit_text(text, **kwargs)
        except (telegram.error.NetworkError, telegram.error.TimedOut) as err:
            logger.warning(f"Telegram network blip editing message (attempt {attempt+1}): {err}")
            await asyncio.sleep(1.0)
        except Exception as e:
            logger.warning(f"Failed to edit message: {e}")
            break
    return None

# Active cache of media info for callback handling
MEDIA_CACHE = {}

# Global Telethon client instance (for high-speed 2GB uploads)
telethon_client: TelegramClient | None = None


def detect_platform(url: str) -> str:
    url_l = url.lower()
    if "instagram.com" in url_l:
        return "📸 Instagram"
    elif "tiktok.com" in url_l:
        return "🎵 TikTok"
    elif "youtube.com" in url_l or "youtu.be" in url_l:
        return "📺 YouTube"
    elif "twitter.com" in url_l or "x.com" in url_l:
        return "🐦 X / Twitter"
    elif "pinterest.com" in url_l or "pin.it" in url_l:
        return "📌 Pinterest"
    elif "reddit.com" in url_l or "redd.it" in url_l:
        return "🤖 Reddit"
    elif "facebook.com" in url_l or "fb.watch" in url_l:
        return "📘 Facebook"
    elif "spotify.com" in url_l:
        return "🟢 Spotify"
    elif "soundcloud.com" in url_l:
        return "☁️ SoundCloud"
    return "🌐 Web Media"


def format_size_str(bytes_val: int) -> str:
    """Formats bytes into human-readable MB / GB estimate."""
    if not bytes_val or bytes_val <= 0:
        return ""
    mb = bytes_val / (1024 * 1024)
    if mb >= 1024:
        return f" (~{mb / 1024:.1f}GB)"
    return f" (~{int(mb)}MB)"


def get_ydl_opts(extra_opts: dict = None, url: str = None) -> dict:
    """Cookieless ydl options wrapper with optional admin cookie injection."""
    opts = get_cookieless_ydl_opts(url=url, extra_opts=extra_opts)
    cookie_path = resolve_cookies(url)
    if cookie_path:
        opts["cookiefile"] = cookie_path
        logger.info(f"Admin cookie active: {Path(cookie_path).name}")
    return opts


async def fetch_media_info(url: str) -> dict:
    """Cookieless metadata extraction with platform resolver cascades and client rotation."""
    loop = asyncio.get_running_loop()

    def _extract():
        cookie_path = resolve_cookies(url)
        extra = {
            "extract_flat": False,
            "skip_download": True,
            "no_playlist": True,
            "check_formats": False,
            "youtube_include_dash_manifest": True,
            "youtube_include_hls_manifest": False,
        }
        if cookie_path:
            extra["cookiefile"] = cookie_path
        return CookielessExtractorEngine.extract_info(url, extra_opts=extra)

    return await loop.run_in_executor(None, _extract)


def extract_available_formats(info: dict) -> tuple[list[dict], str]:
    """Compatibility wrapper around extract_smart_formats."""
    return extract_smart_formats(info)


def download_media_sync(url: str, mode: str, temp_dir: str, direct_url: str | None = None) -> tuple[list[str], str | None]:
    """
    Downloads media to isolated temp_dir synchronously.
    Handles single videos, audio, movies, and multi-video carousels cleanly,
    with automatic fallback to direct stream URL if native extraction meets login walls.
    """
    out_tmpl = os.path.join(temp_dir, "%(title).50s [%(id)s].%(ext)s")

    def _build_opts(target_url: str) -> dict:
        if mode == "audio":
            return get_ydl_opts({
                "format": "bestaudio/best",
                "outtmpl": out_tmpl,
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            }, url=target_url)
        elif mode.isdigit():
            height = int(mode)
            # Match both height and width so vertical shorts/reels (e.g. 1080x1920) work perfectly
            return get_ydl_opts({
                "format": (
                    f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
                    f"bestvideo[width<={height}][ext=mp4]+bestaudio[ext=m4a]/"
                    f"bestvideo[height<={height}]+bestaudio/"
                    f"bestvideo[width<={height}]+bestaudio/"
                    f"best[height<={height}]/"
                    f"best[width<={height}]/"
                    f"best"
                ),
                "outtmpl": out_tmpl,
                "merge_output_format": "mp4",
            }, url=target_url)
        else:  # video_best
            return get_ydl_opts({
                "format": "bestvideo+bestaudio/best",
                "outtmpl": out_tmpl,
                "merge_output_format": "mp4",
            }, url=target_url)

    valid_exts = {".mp4", ".m4a", ".webm", ".mp3", ".mkv", ".mov"}

    def _find_files() -> list[str]:
        found = []
        for root, _, files in os.walk(temp_dir):
            for f in sorted(files):
                if not f.endswith((".part", ".ytdl", ".temp")):
                    ext = os.path.splitext(f)[1].lower()
                    if ext in valid_exts:
                        full_path = os.path.join(root, f)
                        if os.path.getsize(full_path) > 1000:
                            found.append(full_path)
        return found

    opts = _build_opts(url)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
        files = _find_files()
        if files:
            return files, None
    except Exception as e:
        logger.warning(f"Download with primary URL failed ({e}). Checking direct fallback URL...")
        if direct_url and direct_url != url:
            try:
                fallback_opts = _build_opts(direct_url)
                with yt_dlp.YoutubeDL(fallback_opts) as fallback_ydl:
                    fallback_ydl.extract_info(direct_url, download=True)
                files = _find_files()
                if files:
                    return files, None
            except Exception as fb_err:
                logger.error(f"Fallback download also failed: {fb_err}")
                return [], str(fb_err)
        return [], str(e)

    return [], "No downloaded media files found in output directory"


# --- TELEGRAM HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name or "Friend"
    mtproto_status = "⚡ **2 GB High-Speed Parallel Engine Active**" if telethon_client else "📦 **50 MB Standard Engine Active**"
    welcome_text = (
        f"👋 **Hello {user_name}!**\n\n"
        f"I am your **Universal 2GB Content Downloader Bot** 🚀\n"
        f"{mtproto_status}\n\n"
        "Send me a link from **any** platform:\n"
        "• 📸 **Instagram** (Reels, Posts, Carousels)\n"
        "• 🎵 **TikTok** (HD without watermark)\n"
        "• 📺 **YouTube** (Shorts, HD Videos, 4K, Movies, MP3)\n"
        "• 🐦 **X / Twitter** (Videos, Multi-video tweets)\n"
        "• 📌 **Pinterest** (Video Pins)\n"
        "• 🤖 **Reddit** (Clips with sound)\n"
        "• 📘 **Facebook** (Reels & Videos)\n"
        "• ☁️ **SoundCloud** (High quality MP3)\n\n"
        "✨ **Instant Direct Downloads:** No accounts, logins, or setup required.\n"
        "👉 **Just paste your link below to download!**"
    )
    await update.message.reply_text(welcome_text, parse_mode=constants.ParseMode.MARKDOWN)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "💡 **How to use this bot:**\n\n"
        "1. Copy the URL of any video, reel, song, or movie.\n"
        "2. Paste it in this chat.\n"
        "3. Choose your preferred quality (4K, 1080p, 720p, 480p, 360p, or MP3).\n"
        "4. The bot will download and deliver it directly to you!\n\n"
        "🚀 **2 GB File Limit:** Supports files up to **2,000 MB (2 GB)** with parallel MTProto upload speed!\n\n"
        "⚡ **Direct Download Engine:** Instant downloads powered by smart player client rotation and automatic resolver fallbacks."
    )
    await update.message.reply_text(help_text, parse_mode=constants.ParseMode.MARKDOWN)


async def cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if OWNER_USER_ID and user_id != OWNER_USER_ID:
        await update.message.reply_text("⛔ This command is reserved for the bot owner.")
        return

    yt_cookie = resolve_cookies("https://www.youtube.com")
    ig_cookie = resolve_cookies("https://www.instagram.com")
    univ_cookie = resolve_cookies()

    yt_status = f"✅ Active (`{Path(yt_cookie).name}`)" if yt_cookie else "❌ Not configured"
    ig_status = f"✅ Active (`{Path(ig_cookie).name}`)" if ig_cookie else "❌ Not configured"
    univ_status = f"✅ Active (`{Path(univ_cookie).name}`)" if univ_cookie else "❌ Not configured"

    msg = (
        "🍪 **Session Cookies Dashboard**\n\n"
        f"• 📺 **YouTube:** {yt_status}\n"
        f"• 📸 **Instagram:** {ig_status}\n"
        f"• 🌐 **Universal:** {univ_status}\n\n"
        "**How to add or update cookies:**\n"
        "1. Log in to YouTube / Instagram in your browser.\n"
        "2. Use an extension (e.g. *Get cookies.txt LOCALLY*) to export Netscape cookies.\n"
        "3. Send the `.txt` file directly into this chat as a document:\n"
        "   • `youtube_cookies.txt` (for YouTube)\n"
        "   • `instagram_cookies.txt` (for Instagram)\n"
        "   • `cookies.txt` (universal for all sites)\n\n"
        "✨ The bot will validate the format and activate your session immediately!"
    )
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.MARKDOWN)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        return

    user_id = update.effective_user.id
    if OWNER_USER_ID and user_id != OWNER_USER_ID:
        return

    file_name = (doc.file_name or "").lower()
    if not (file_name.endswith(".txt") or file_name.endswith(".cookies")):
        return

    caption = (update.message.caption or "").strip().lower()
    is_cookie_file = "cookie" in file_name or caption in ("/setcookies", "/cookies", "cookies")
    if not is_cookie_file:
        return

    status_msg = await update.message.reply_text("⏳ **Inspecting cookie file...**", parse_mode=constants.ParseMode.MARKDOWN)

    try:
        tg_file = await doc.get_file()
        file_bytes = await tg_file.download_as_bytearray()
        content = file_bytes.decode("utf-8", errors="replace")

        if not validate_netscape_cookies(content):
            await safe_edit_message(
                status_msg,
                "❌ **Invalid Cookie File Format!**\n\n"
                "The file does not appear to be a standard Netscape/HTTP cookie format.\n"
                "Please export using *Get cookies.txt LOCALLY* or standard Netscape format.",
                parse_mode=constants.ParseMode.MARKDOWN
            )
            return

        # Determine target filename
        if "instagram" in file_name or "instagram.com" in content:
            target_name = "instagram_cookies.txt"
            platform_name = "📸 Instagram"
        elif "youtube" in file_name or "youtube.com" in content:
            target_name = "youtube_cookies.txt"
            platform_name = "📺 YouTube"
        else:
            target_name = "cookies.txt"
            platform_name = "🌐 Universal (YouTube, Instagram & Web)"

        target_path = COOKIES_DIR / target_name
        with open(target_path, "wb") as f:
            f.write(file_bytes)

        await safe_edit_message(
            status_msg,
            f"✅ **Cookies Saved & Activated Successfully!**\n\n"
            f"Platform: **{platform_name}**\n"
            f"Saved to: `{target_path.name}` ({len(file_bytes)} bytes)\n\n"
            f"🚀 All subsequent downloads will automatically use this authenticated session!",
            parse_mode=constants.ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error saving cookie document: {e}")
        await safe_edit_message(status_msg, f"❌ Failed to save cookie file: `{e}`", parse_mode=constants.ParseMode.MARKDOWN)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or update.message.caption or ""
    urls = URL_REGEX.findall(text)

    if not urls:
        await update.message.reply_text(
            "⚠️ Please send a valid link (e.g. YouTube, Instagram Reel, TikTok, Twitter/X, Pinterest, etc.)."
        )
        return

    url = urls[0]
    platform = detect_platform(url)
    user_id = update.effective_user.id

    status_msg = await update.message.reply_text(
        f"🔍 **Analyzing link...**\nPlatform: {platform}",
        parse_mode=constants.ParseMode.MARKDOWN
    )

    try:
        info = await fetch_media_info(url)
    except Exception as e:
        logger.error(f"Error fetching info for {url}: {e}")
        err_str = str(e)
        err_lower = err_str.lower()
        if "confirm you're not a bot" in err_lower or "sign in to confirm" in err_lower:
            error_guidance = (
                "❌ **Download Unavailable**\n\n"
                "The media host has temporarily restricted direct stream access for this item.\n"
                "Please try again in a few moments or choose a different link."
            )
        elif "login required" in err_lower or "checkpoint_required" in err_lower or "rate-limit" in err_lower:
            error_guidance = (
                "❌ **Post Is Private or Restricted**\n\n"
                "This post cannot be accessed without special permissions or is private.\n"
                "Please ensure the link is public and still available."
            )
        else:
            error_guidance = (
                f"❌ **Failed to fetch media.**\n\n"
                f"Reason: `{err_str[:150]}`\n\n"
                "Please make sure the link is public and still available."
            )

        await safe_edit_message(
            status_msg,
            error_guidance,
            parse_mode=constants.ParseMode.MARKDOWN
        )
        return

    title = info.get("title", "Media")
    duration = info.get("duration", 0)
    duration_str = f"{int(duration // 60)}:{int(duration % 60):02d}" if duration else "Unknown"
    author = info.get("uploader") or info.get("channel") or "Unknown"

    cache_key = f"{user_id}_{abs(hash(url))}"
    MEDIA_CACHE[cache_key] = {
        "url": url,
        "direct_url": info.get("url"),
        "title": title,
        "author": author,
        "duration": duration,
        "platform": platform,
        "width": info.get("width"),
        "height": info.get("height"),
    }

    if len(MEDIA_CACHE) > 100:
        MEDIA_CACHE.pop(next(iter(MEDIA_CACHE)))

    resolutions, audio_size_str = extract_available_formats(info)
    keyboard = []

    # Organize resolution buttons into rows of 2
    row = []
    for r in resolutions:
        row.append(InlineKeyboardButton(r["label"], callback_data=f"dl:{r['height']}:{cache_key}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Audio MP3 button with calculated file size
    keyboard.append([
        InlineKeyboardButton(f"🎵 Audio Only (MP3){audio_size_str}", callback_data=f"dl:audio:{cache_key}")
    ])

    caption_text = (
        f"✨ **{platform}**\n\n"
        f"📌 **Title:** {title[:70]}\n"
        f"👤 **Author:** {author}\n"
        f"⏱️ **Duration:** {duration_str}\n\n"
        "👇 **Choose your preferred format / quality:**"
    )

    await safe_edit_message(
        status_msg,
        caption_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=constants.ParseMode.MARKDOWN
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data or not data.startswith("dl:"):
        return

    parts = data.split(":")
    if len(parts) != 3:
        await query.edit_message_text("❌ Invalid action.")
        return

    _, mode, cache_key = parts
    media_data = MEDIA_CACHE.get(cache_key)

    if not media_data:
        await query.edit_message_text("⚠️ Download session expired. Please send the link again.")
        return

    url = media_data["url"]
    title = media_data["title"]
    author = media_data["author"]
    platform = media_data["platform"]
    duration = media_data.get("duration", 0)

    mode_label = f"🎬 {mode}p Video" if mode.isdigit() else ("🎵 MP3 Audio" if mode == "audio" else "🎬 Video")

    await query.edit_message_text(
        f"📥 **Downloading {mode_label}...**\n"
        f"Title: `{title[:60]}`\n\n"
        "⏳ Please wait a few moments...",
        parse_mode=constants.ParseMode.MARKDOWN
    )

    temp_dir = tempfile.mkdtemp(prefix="dl_")
    loop = asyncio.get_running_loop()

    try:
        direct_url = media_data.get("direct_url")
        file_paths, err = await loop.run_in_executor(None, download_media_sync, url, mode, temp_dir, direct_url)

        if err or not file_paths:
            logger.error(f"Download failed for {url}: {err}")
            await query.edit_message_text(
                f"❌ **Download failed:** `{str(err)[:120]}`\n\n"
                "Try choosing **Audio (MP3)** or another resolution.",
                parse_mode=constants.ParseMode.MARKDOWN
            )
            return

        chat_id = query.message.chat_id
        total_files = len(file_paths)

        for idx, file_path in enumerate(file_paths):
            file_size = os.path.getsize(file_path)
            file_size_mb = file_size / (1024 * 1024)

            # Check 2GB hard ceiling
            if file_size > MAX_TELEGRAM_BYTES:
                await query.edit_message_text(
                    f"⚠️ **File exceeds 2,000 MB ({file_size_mb:.1f} MB)!**\n\n"
                    "Telegram has a 2 GB absolute maximum file limit.\n"
                    "💡 Try selecting **Audio (MP3)** or a lower resolution.",
                    parse_mode=constants.ParseMode.MARKDOWN
                )
                return

            prefix = f"[{idx + 1}/{total_files}] " if total_files > 1 else ""
            caption = (
                f"🎬 <b>{prefix}{html.escape(title[:65])}</b>\n"
                f"👤 {html.escape(author[:40])} • {platform}\n"
                f"🤖 Downloaded with @Download_agent01_bot"
            )

            # 🚀 HIGH-SPEED TELETHON MTPROTO ENGINE (for ALL uploads when connected)
            if telethon_client and telethon_client.is_connected():
                await query.edit_message_text(
                    f"🚀 **Uploading {prefix}({file_size_mb:.1f} MB)...**\n"
                    "⚡ High-Speed MTProto Engine active\n\n"
                    "⏳ Initializing upload stream...",
                    parse_mode=constants.ParseMode.MARKDOWN
                )

                upload_stats = {"current": 0, "total": file_size}
                upload_done = asyncio.Event()

                def sync_progress(cur, tot):
                    upload_stats["current"] = cur
                    upload_stats["total"] = tot

                async def progress_notifier():
                    while not upload_done.is_set():
                        await asyncio.sleep(2.5)
                        if upload_done.is_set():
                            break
                        cur = upload_stats["current"]
                        tot = upload_stats["total"]
                        pct = int((cur / tot) * 100) if tot else 0
                        cur_mb = cur / (1024 * 1024)
                        tot_mb = tot / (1024 * 1024)
                        bar = '█' * (pct // 10) + '░' * (10 - pct // 10)
                        try:
                            await query.edit_message_text(
                                f"📤 **Uploading {prefix}to Telegram ({pct}%)**\n"
                                f"`[{bar}]` ({cur_mb:.1f} / {tot_mb:.1f} MB)",
                                parse_mode=constants.ParseMode.MARKDOWN
                            )
                        except Exception:
                            pass

                notifier_task = asyncio.create_task(progress_notifier())

                try:
                    # Turbo upload: 512KB chunks with 8 concurrent upload pipes
                    input_file = await fast_upload_file(
                        telethon_client,
                        file_path,
                        progress_callback=sync_progress,
                        concurrency=8
                    )
                finally:
                    upload_done.set()
                    notifier_task.cancel()

                if mode == "audio" or file_path.endswith(".mp3"):
                    attributes = [DocumentAttributeAudio(duration=int(duration or 0), title=title[:60], performer=author[:40])]
                    await telethon_client.send_file(
                        chat_id,
                        input_file,
                        caption=caption,
                        parse_mode="html",
                        attributes=attributes,
                    )
                else:
                    vid_w = media_data.get("width")
                    vid_h = media_data.get("height")
                    if mode.isdigit():
                        target_h = int(mode)
                        if vid_h and vid_w:
                            vid_w = int(target_h * (vid_w / vid_h))
                            vid_h = target_h
                        else:
                            vid_h = target_h
                            vid_w = int(target_h * 16 / 9)
                    vid_w = int(vid_w or 1280)
                    vid_h = int(vid_h or 720)

                    attributes = [
                        DocumentAttributeVideo(
                            duration=int(duration or 0),
                            w=vid_w,
                            h=vid_h,
                            supports_streaming=True
                        )
                    ]
                    await telethon_client.send_file(
                        chat_id,
                        input_file,
                        caption=caption,
                        parse_mode="html",
                        supports_streaming=True,
                        attributes=attributes,
                    )
            else:
                # 📦 FALLBACK: STANDARD BOT API (for files <= 50MB if Telethon is offline)
                if file_size > 50 * 1024 * 1024:
                    await query.edit_message_text(
                        f"❌ **File is {file_size_mb:.1f} MB.**\n"
                        "Telethon MTProto engine is offline, and standard Bot API is capped at 50 MB.",
                        parse_mode=constants.ParseMode.MARKDOWN
                    )
                    return

                await query.edit_message_text(
                    f"📤 **Uploading {prefix}to Telegram ({file_size_mb:.1f} MB)...**",
                    parse_mode=constants.ParseMode.MARKDOWN
                )

                if mode == "audio" or file_path.endswith(".mp3"):
                    with open(file_path, "rb") as f:
                        await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=f,
                            title=f"{prefix}{title[:55]}",
                            performer=author[:40],
                            caption=caption,
                            parse_mode=constants.ParseMode.HTML,
                            read_timeout=300,
                            write_timeout=300
                        )
                else:
                    with open(file_path, "rb") as f:
                        await context.bot.send_video(
                            chat_id=chat_id,
                            video=f,
                            caption=caption,
                            supports_streaming=True,
                            parse_mode=constants.ParseMode.HTML,
                            read_timeout=300,
                            write_timeout=300
                        )

        try:
            await query.delete_message()
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Error processing callback: {e}", exc_info=True)
        await query.edit_message_text(
            f"❌ **Error sending file:** `{str(e)[:120]}`",
            parse_mode=constants.ParseMode.MARKDOWN
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Graceful error logging and user notifications."""
    if isinstance(context.error, telegram.error.Conflict):
        logger.warning(
            "⚠️ Conflict: Another bot instance is already running with this token! "
            "Please run 'pkill -f python' in Termux to terminate conflicting processes."
        )
    else:
        logger.error(f"Telegram error: {context.error}", exc_info=context.error)


async def post_init(application: Application):
    """Start Telethon MTProto client inside the same asyncio event loop."""
    global telethon_client
    if API_ID and API_HASH:
        logger.info("🚀 Initializing Telethon MTProto engine for 2GB file uploads...")
        try:
            session_path = str(BASE_DIR / "bot_mtproto")
            telethon_client = TelegramClient(session_path, API_ID, API_HASH)
            await telethon_client.start(bot_token=BOT_TOKEN)
            me = await telethon_client.get_me()
            logger.info(f"✅ Telethon MTProto 2GB Engine connected as: @{me.username}")
        except Exception as err:
            logger.error(f"⚠️ Telethon MTProto initialization warning: {err}")
            telethon_client = None
    else:
        logger.info("ℹ️ TELEGRAM_API_ID / TELEGRAM_API_HASH not set. Standard 50MB mode active.")


async def post_shutdown(application: Application):
    """Gracefully disconnect Telethon."""
    global telethon_client
    if telethon_client and telethon_client.is_connected():
        await telethon_client.disconnect()
        logger.info("Telethon MTProto engine cleanly disconnected.")


def main():
    print("=" * 60)
    print("    UNIVERSAL 2GB CONTENT DOWNLOADER TELEGRAM BOT")
    print("=" * 60)
    print(f"🤖 Bot Token: {BOT_TOKEN[:10]}...{BOT_TOKEN[-5:]}")
    print(f"🔑 API ID: {API_ID}")
    print(f"📁 Working Directory: {BASE_DIR}")
    print(f"🎬 FFmpeg Path: {FFMPEG_PATH}")
    print(f"📦 Max File Size: {MAX_TELEGRAM_FILE_MB} MB (2 GB MTProto Support)")
    print("🚀 Connecting to Telegram and starting polling...")

    t_request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=30.0,
        read_timeout=300.0,
        write_timeout=300.0,
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(t_request)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cookies", cookies_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
