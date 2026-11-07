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


def get_ydl_opts(extra_opts: dict = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "no_color": True,
        "geo_bypass": True,
        "socket_timeout": 30,
        "retries": 3,
    }
    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH

    if extra_opts:
        opts.update(extra_opts)
    return opts


async def fetch_media_info(url: str) -> dict:
    """Fast, optimized metadata extraction without redundant HTTP format checks."""
    loop = asyncio.get_running_loop()

    def _extract():
        opts = get_ydl_opts({
            "extract_flat": False,
            "skip_download": True,
            "no_playlist": True,
            "check_formats": False,
            "youtube_include_dash_manifest": True,
            "youtube_include_hls_manifest": False,
        })
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    return await loop.run_in_executor(None, _extract)


def extract_available_formats(info: dict) -> tuple[list[dict], str]:
    """
    Extracts available resolutions (1080p, 720p, 480p, 360p, etc.)
    and estimates accurate file sizes for both landscape and vertical videos.
    """
    formats = info.get("formats", [])
    duration = info.get("duration") or 0

    # Determine audio size
    best_audio_size = 0
    for f in formats:
        if f.get("vcodec") == "none" and f.get("acodec") != "none":
            sz = f.get("filesize") or f.get("filesize_approx")
            if not sz and f.get("tbr") and duration:
                sz = int((f["tbr"] * 1000 / 8) * duration)
            if sz and sz > best_audio_size:
                best_audio_size = sz
    if not best_audio_size and duration:
        best_audio_size = int((192 * 1000 / 8) * duration)

    # Standard bitrate fallback estimates in bytes per second
    bitrate_estimates = {
        2160: int(18.0 * 1024 * 1024 / 8),
        1440: int(8.0 * 1024 * 1024 / 8),
        1080: int(3.5 * 1024 * 1024 / 8),
        720:  int(1.8 * 1024 * 1024 / 8),
        480:  int(0.8 * 1024 * 1024 / 8),
        360:  int(0.4 * 1024 * 1024 / 8),
    }

    # Map effective height for both landscape and portrait/vertical (Shorts/Reels)
    available_eff_heights = {}
    for f in formats:
        h = f.get("height")
        w = f.get("width")
        if (h or w) and f.get("vcodec") != "none":
            eff = min(h, w) if (h and w) else (h or w)
            sz = f.get("filesize") or f.get("filesize_approx")
            tbr = f.get("tbr") or f.get("vbr")
            if not sz and tbr and duration:
                sz = int((tbr * 1000 / 8) * duration)
            if eff not in available_eff_heights or (sz and sz > available_eff_heights[eff]):
                available_eff_heights[eff] = sz or 0

    brackets = [
        (2160, "🌟 4K (2160p)"),
        (1440, "💎 2K (1440p)"),
        (1080, "🎬 1080p FHD"),
        (720,  "📺 720p HD"),
        (480,  "📱 480p SD"),
        (360,  "💾 360p Low"),
    ]

    resolutions = []
    if available_eff_heights:
        max_source_height = max(available_eff_heights.keys())
        for target, label in brackets:
            # Include bracket if source supports it or is within reasonable range
            if target <= max_source_height + 60:
                matching_sizes = [
                    sz for eff, sz in available_eff_heights.items()
                    if abs(eff - target) <= 60 and sz > 0
                ]
                if matching_sizes:
                    sz = max(matching_sizes)
                    if best_audio_size:
                        sz += best_audio_size
                elif duration > 0:
                    sz = bitrate_estimates.get(target, int(1.0 * 1024 * 1024 / 8)) * duration
                else:
                    sz = 0

                size_str = format_size_str(sz)
                resolutions.append({
                    "height": target,
                    "label": f"{label}{size_str}"
                })
    else:
        # Fallback for single-stream sites (Instagram, TikTok, Twitter, web links)
        for target, label in [
            (1080, "🎬 Best HD (1080p)"),
            (720,  "📺 Standard (720p)"),
            (480,  "📱 Data Saver (480p)"),
        ]:
            sz = bitrate_estimates.get(target, int(1.0 * 1024 * 1024 / 8)) * duration if duration else 0
            size_str = format_size_str(sz)
            resolutions.append({
                "height": target,
                "label": f"{label}{size_str}"
            })

    audio_size_str = format_size_str(best_audio_size)
    return resolutions, audio_size_str


def download_media_sync(url: str, mode: str, temp_dir: str) -> tuple[list[str], str | None]:
    """
    Downloads media to isolated temp_dir synchronously.
    Handles single videos, audio, movies, and multi-video carousels cleanly.
    """
    out_tmpl = os.path.join(temp_dir, "%(title).50s [%(id)s].%(ext)s")

    if mode == "audio":
        opts = get_ydl_opts({
            "format": "bestaudio/best",
            "outtmpl": out_tmpl,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })
    elif mode.isdigit():
        height = int(mode)
        # Match both height and width so vertical shorts/reels (e.g. 1080x1920) work perfectly
        opts = get_ydl_opts({
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
        })
    else:  # video_best
        opts = get_ydl_opts({
            "format": "bestvideo+bestaudio/best",
            "outtmpl": out_tmpl,
            "merge_output_format": "mp4",
        })

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)

        valid_exts = {".mp4", ".m4a", ".webm", ".mp3", ".mkv", ".mov"}
        found_files = []
        for root, _, files in os.walk(temp_dir):
            for f in sorted(files):
                if not f.endswith((".part", ".ytdl", ".temp")):
                    ext = os.path.splitext(f)[1].lower()
                    if ext in valid_exts:
                        full_path = os.path.join(root, f)
                        if os.path.getsize(full_path) > 1000:
                            found_files.append(full_path)

        if found_files:
            return found_files, None
        return [], "No downloaded media files found in output directory"
    except Exception as e:
        return [], str(e)


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
        "🚀 **2 GB File Limit:** Unlike standard Telegram bots that crash on files over 50MB, this bot supports files up to **2,000 MB (2 GB)** with parallel MTProto upload speed!"
    )
    await update.message.reply_text(help_text, parse_mode=constants.ParseMode.MARKDOWN)


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
        await status_msg.edit_text(
            f"❌ **Failed to fetch media.**\n\n"
            f"Reason: `{str(e)[:150]}`\n\n"
            "Please make sure the link is public and still available.",
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

    await status_msg.edit_text(
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
        file_paths, err = await loop.run_in_executor(None, download_media_sync, url, mode, temp_dir)

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

                last_progress_edit = [0]

                async def upload_progress(current, total):
                    now = time.time()
                    if now - last_progress_edit[0] >= 2.5:
                        last_progress_edit[0] = now
                        pct = int((current / total) * 100) if total else 0
                        cur_mb = current / (1024 * 1024)
                        tot_mb = total / (1024 * 1024)
                        bar = '█' * (pct // 10) + '░' * (10 - pct // 10)
                        try:
                            await query.edit_message_text(
                                f"📤 **Uploading {prefix}to Telegram ({pct}%)**\n"
                                f"`[{bar}]` ({cur_mb:.1f} / {tot_mb:.1f} MB)",
                                parse_mode=constants.ParseMode.MARKDOWN
                            )
                        except Exception:
                            pass

                # Upload with 512KB chunks and 6-way concurrency
                input_file = await fast_upload_file(
                    telethon_client,
                    file_path,
                    progress_callback=upload_progress,
                    concurrency=6
                )

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
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
