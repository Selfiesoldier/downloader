import html
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

import yt_dlp

logger = logging.getLogger("UniversalDownloader.Extractors")

BASE_DIR = Path(__file__).resolve().parent


def resolve_ffmpeg() -> str | None:
    """Locate FFmpeg binary across Android Termux, system PATH, or local bin."""
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


def resolve_js_runtimes() -> dict[str, Any] | None:
    """Locate available JavaScript runtimes (Node.js, Deno, Bun, QuickJS) for YouTube signature challenge solving."""
    runtimes = {}
    candidates = [
        ("node", "/data/data/com.termux/files/usr/bin/node"),
        ("deno", "/data/data/com.termux/files/usr/bin/deno"),
        ("bun", "/data/data/com.termux/files/usr/bin/bun"),
        ("quickjs", "/data/data/com.termux/files/usr/bin/qjs"),
    ]
    for name, termux_path in candidates:
        if shutil.which(name):
            runtimes[name] = {}
            break
        elif os.path.exists(termux_path) and os.access(termux_path, os.X_OK):
            runtimes[name] = {"path": termux_path}
            break
    return runtimes if runtimes else None


JS_RUNTIMES = resolve_js_runtimes()


def get_cookieless_ydl_opts(url: str | None = None, extra_opts: dict | None = None) -> dict[str, Any]:
    """
    Constructs cookieless yt-dlp configuration with modern player client rotation
    and native JavaScript challenge solving (via Node.js/Deno/QuickJS).
    Bypasses bot checks without requiring cookies or account authentication.
    """
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "no_color": True,
        "geo_bypass": True,
        "socket_timeout": 20,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 8,
        "buffersize": 1024 * 1024,
        "http_chunk_size": 10485760,
        "check_formats": False,
        "youtube_include_dash_manifest": True,
        "youtube_include_hls_manifest": False,
        "remote_components": ["ejs:github"],
        "trim_file_name": 60,
        "windowsfilenames": True,
        "restrictfilenames": False,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    if JS_RUNTIMES:
        opts["js_runtimes"] = JS_RUNTIMES

    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH

    if extra_opts:
        for k, v in extra_opts.items():
            if k == "extractor_args" and isinstance(v, dict):
                current_ext_args = opts.setdefault("extractor_args", {})
                for ext_name, ext_val in v.items():
                    if ext_name not in current_ext_args:
                        current_ext_args[ext_name] = ext_val
                    elif isinstance(ext_val, dict) and isinstance(current_ext_args[ext_name], dict):
                        current_ext_args[ext_name].update(ext_val)
                    else:
                        current_ext_args[ext_name] = ext_val
            else:
                opts[k] = v

    return opts


def _http_get(url: str, params: dict | None = None, headers: dict | None = None, timeout: int = 15):
    """
    Resilient HTTP GET request using curl_cffi TLS impersonation where possible,
    falling back seamlessly to standard requests.
    """
    req_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        req_headers.update(headers)

    try:
        from curl_cffi import requests as c_requests
        return c_requests.get(url, params=params, headers=req_headers, impersonate="chrome124", timeout=timeout)
    except Exception:
        import requests
        return requests.get(url, params=params, headers=req_headers, timeout=timeout)


def resolve_twitter(url: str, extra_opts: dict | None = None) -> dict[str, Any]:
    """
    Twitter/X extraction cascade:
    1. Primary: Native yt-dlp with cookieless mobile/desktop configuration.
    2. Secondary: Fallback to https://api.fxtwitter.com/status/{id} which provides
       direct CDN MP4 links without requiring API keys or cookies when rate-limited.
    """
    try:
        opts = get_cookieless_ydl_opts(url, extra_opts)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info and (info.get("url") or info.get("formats")):
                return info
    except Exception as yt_err:
        logger.warning(f"Native yt-dlp Twitter extraction failed ({yt_err}). Cascading to fxtwitter fallback...")

    # Extract tweet ID
    match = re.search(r'(?:twitter\.com|x\.com)/[^/]+/status/(\d+)', url)
    if not match:
        raise ValueError(f"Could not parse Tweet ID from URL: {url}")

    tweet_id = match.group(1)
    fxtwitter_url = f"https://api.fxtwitter.com/status/{tweet_id}"

    try:
        resp = _http_get(fxtwitter_url, timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            tweet = data.get("tweet", {})
            media = tweet.get("media", {})
            videos = media.get("videos") or []
            if not videos:
                # Check for single video object or 'all' list
                for m in media.get("all", []):
                    if m.get("type") == "video":
                        videos.append(m)
                if not videos and tweet.get("video"):
                    videos.append(tweet["video"])

            if videos:
                best_video = videos[0]
                video_url = best_video.get("url")
                if video_url:
                    width = best_video.get("width")
                    height = best_video.get("height")
                    duration = best_video.get("duration") or 0
                    title = tweet.get("text") or f"X Video {tweet_id}"
                    author = tweet.get("author", {}).get("name") or tweet.get("author", {}).get("screen_name") or "X User"

                    return {
                        "id": tweet_id,
                        "title": title,
                        "uploader": author,
                        "channel": author,
                        "url": video_url,
                        "thumbnail": best_video.get("thumbnail_url"),
                        "duration": duration,
                        "width": width,
                        "height": height,
                        "formats": [
                            {
                                "url": video_url,
                                "ext": "mp4",
                                "format_id": "fxtwitter_mp4",
                                "vcodec": "h264",
                                "acodec": "aac",
                                "width": width,
                                "height": height,
                                "duration": duration,
                            }
                        ],
                        "extractor": "twitter:fxtwitter",
                        "extractor_key": "TwitterFX",
                        "webpage_url": url,
                    }
    except Exception as fx_err:
        logger.error(f"fxtwitter fallback resolution failed: {fx_err}")

    raise RuntimeError(f"Failed to extract Twitter/X media from {url} via both native yt-dlp and fxtwitter fallback.")


def resolve_instagram(url: str, extra_opts: dict | None = None) -> dict[str, Any]:
    """
    Instagram extraction cascade:
    1. Primary: Native yt-dlp with mobile user-agent headers.
    2. Secondary: Public embed resolver endpoint querying Instagram embed CDN
       (scontent.cdninstagram.com direct MP4) or public fallback endpoints.
    """
    try:
        opts = get_cookieless_ydl_opts(url, extra_opts)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info and (info.get("url") or info.get("formats")):
                return info
    except Exception as yt_err:
        logger.warning(f"Native yt-dlp Instagram extraction failed ({yt_err}). Cascading to embed resolver fallback...")

    # Extract shortcode
    match = re.search(r'(?:instagram\.com|instagr\.am)/(?:p|reel|reels|tv|share/reel)/([A-Za-z0-9_-]+)', url)
    if not match:
        raise ValueError(f"Could not parse Instagram shortcode from URL: {url}")

    shortcode = match.group(1)

    # 1. Instagram embed HTML inspection
    embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
    try:
        resp = _http_get(embed_url, timeout=12)
        if resp.status_code == 200:
            text = resp.text
            # Look for direct video URL in embed HTML
            video_matches = (
                re.findall(r'<video[^>]+src=["\']([^"\']+)["\']', text) or
                re.findall(r'\\?"video_url\\?":\s*\\?"([^"]+)\\?"', text) or
                re.findall(r'"video_url":\s*"([^"]+)"', text) or
                re.findall(r'contentUrl["\']\s*:\s*["\']([^"\']+)["\']', text)
            )
            for v_url in video_matches:
                cleaned_url = html.unescape(v_url).replace("\\/", "/").replace("\\u0026", "&")
                if "scontent" in cleaned_url or "cdninstagram" in cleaned_url or ".mp4" in cleaned_url:
                    return {
                        "id": shortcode,
                        "title": f"Instagram Reel {shortcode}",
                        "uploader": "Instagram Creator",
                        "channel": "Instagram",
                        "url": cleaned_url,
                        "duration": 0,
                        "formats": [
                            {
                                "url": cleaned_url,
                                "ext": "mp4",
                                "format_id": "instagram_embed_mp4",
                                "vcodec": "h264",
                                "acodec": "aac",
                            }
                        ],
                        "extractor": "instagram:embed_fallback",
                        "extractor_key": "InstagramEmbed",
                        "webpage_url": url,
                    }
    except Exception as embed_err:
        logger.warning(f"Instagram embed HTML resolution failed: {embed_err}")

    # 2. Public API fallback (instagramez / cobalt)
    try:
        ez_url = f"https://api.instagramez.com/v1/post?url={url}"
        ez_resp = _http_get(ez_url, timeout=10)
        if ez_resp.status_code == 200:
            ez_data = ez_resp.json()
            direct_url = ez_data.get("url") or ez_data.get("video_url")
            if direct_url:
                return {
                    "id": shortcode,
                    "title": ez_data.get("title") or f"Instagram Reel {shortcode}",
                    "uploader": ez_data.get("author") or "Instagram Creator",
                    "channel": "Instagram",
                    "url": direct_url,
                    "duration": ez_data.get("duration", 0),
                    "formats": [
                        {
                            "url": direct_url,
                            "ext": "mp4",
                            "format_id": "instagramez_mp4",
                            "vcodec": "h264",
                            "acodec": "aac",
                        }
                    ],
                    "extractor": "instagram:api_fallback",
                    "extractor_key": "InstagramAPI",
                    "webpage_url": url,
                }
    except Exception as ez_err:
        logger.warning(f"Instagram public API fallback failed: {ez_err}")

    raise RuntimeError(f"Failed to extract Instagram media from {url} via both native yt-dlp and embed fallback.")


def resolve_tiktok(url: str, extra_opts: dict | None = None) -> dict[str, Any]:
    """
    TikTok extraction cascade:
    1. Primary: Native yt-dlp with cookieless configuration.
    2. Secondary: Public tikwm API (https://www.tikwm.com/api/) for watermark-free MP4 URLs.
    """
    try:
        opts = get_cookieless_ydl_opts(url, extra_opts)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info and (info.get("url") or info.get("formats")):
                return info
    except Exception as yt_err:
        logger.warning(f"Native yt-dlp TikTok extraction failed ({yt_err}). Cascading to tikwm fallback...")

    # Secondary: TikWM API fallback
    try:
        tikwm_url = "https://www.tikwm.com/api/"
        resp = _http_get(tikwm_url, params={"url": url}, timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == 0 and data.get("data"):
                d = data["data"]
                video_url = d.get("hdplay") or d.get("play")
                if video_url:
                    video_id = str(d.get("id", "tiktok_media"))
                    title = d.get("title") or "TikTok Video"
                    author_data = d.get("author", {})
                    author = author_data.get("nickname") or author_data.get("unique_id") or "TikTok Creator"
                    duration = d.get("duration") or 0

                    return {
                        "id": video_id,
                        "title": title,
                        "uploader": author,
                        "channel": author,
                        "url": video_url,
                        "duration": duration,
                        "formats": [
                            {
                                "url": video_url,
                                "ext": "mp4",
                                "format_id": "tikwm_hd",
                                "vcodec": "h264",
                                "acodec": "aac",
                                "width": 1080,
                                "height": 1920,
                                "duration": duration,
                            }
                        ],
                        "extractor": "tiktok:tikwm",
                        "extractor_key": "TikTokTikwm",
                        "webpage_url": url,
                    }
    except Exception as tikwm_err:
        logger.error(f"TikWM fallback resolution failed: {tikwm_err}")

    raise RuntimeError(f"Failed to extract TikTok media from {url} via both native yt-dlp and tikwm fallback.")


def resolve_reddit(url: str, extra_opts: dict | None = None) -> dict[str, Any]:
    """
    Reddit extraction cascade:
    1. Primary: Native yt-dlp with TLS/curl_cffi impersonation.
    2. Secondary: Reddit JSON endpoint extraction.
    """
    try:
        opts = get_cookieless_ydl_opts(url, extra_opts)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info and (info.get("url") or info.get("formats")):
                return info
    except Exception as yt_err:
        logger.warning(f"Native yt-dlp Reddit extraction failed ({yt_err}). Cascading to Reddit JSON fallback...")

    # Secondary: Reddit JSON endpoint
    try:
        clean_url = url.split("?")[0].rstrip("/") + ".json"
        resp = _http_get(clean_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}, timeout=12)
        if resp.status_code == 200:
            posts = resp.json()
            if isinstance(posts, list) and len(posts) > 0:
                child = posts[0].get("data", {}).get("children", [{}])[0].get("data", {})
                media = child.get("secure_media") or child.get("media") or {}
                reddit_video = media.get("reddit_video", {})
                fallback_url = reddit_video.get("fallback_url")

                if fallback_url:
                    post_id = child.get("id", "reddit_media")
                    title = child.get("title", f"Reddit Video {post_id}")
                    author = child.get("author", "Reddit User")
                    duration = reddit_video.get("duration", 0)
                    w = reddit_video.get("width")
                    h = reddit_video.get("height")

                    return {
                        "id": post_id,
                        "title": title,
                        "uploader": author,
                        "channel": author,
                        "url": fallback_url,
                        "duration": duration,
                        "width": w,
                        "height": h,
                        "formats": [
                            {
                                "url": fallback_url,
                                "ext": "mp4",
                                "format_id": "reddit_fallback",
                                "vcodec": "h264",
                                "acodec": "aac",
                                "width": w,
                                "height": h,
                                "duration": duration,
                            }
                        ],
                        "extractor": "reddit:json_fallback",
                        "extractor_key": "RedditJSON",
                        "webpage_url": url,
                    }
    except Exception as reddit_err:
        logger.error(f"Reddit JSON fallback failed: {reddit_err}")

    raise RuntimeError(f"Failed to extract Reddit media from {url} via both native yt-dlp and JSON fallback.")


def format_size_str(bytes_val: int | float) -> str:
    """Formats bytes into human-readable MB / GB estimate."""
    if not bytes_val or bytes_val <= 0:
        return ""
    mb = bytes_val / (1024 * 1024)
    if mb >= 1024:
        return f" (~{mb / 1024:.1f}GB)"
    return f" (~{int(round(mb))}MB)"


def extract_smart_formats(info: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """
    Parses video formats and computes bandwidth-aware smart badges:
    - 🌟 Ultra 4K (2160p)
    - 💎 2K (1440p)
    - 🎬 Full 1080p (~150MB)
    - ⚡ Fast 720p (~30MB)
    - 💾 Data Saver 480p (~15MB)
    - 💾 Data Saver 360p (~8MB)
    - 🎵 Audio Only (MP3)
    Estimates accurate file sizes for both vertical (9:16 Shorts/Reels) and landscape streams.
    """
    formats = info.get("formats", [])
    duration = info.get("duration") or 0

    # Determine best audio stream size
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

    # Map effective height for both landscape and vertical (Shorts/Reels)
    available_eff_heights: dict[int, int] = {}
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
        (2160, "🌟 Ultra 4K (2160p)"),
        (1440, "💎 2K (1440p)"),
        (1080, "🎬 Full 1080p"),
        (720,  "⚡ Fast 720p"),
        (480,  "💾 Data Saver 480p"),
        (360,  "💾 Data Saver 360p"),
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
                    if best_audio_size:
                        sz += best_audio_size
                else:
                    sz = 0

                size_str = format_size_str(sz)
                resolutions.append({
                    "height": target,
                    "label": f"{label}{size_str}"
                })
    else:
        # Fallback for single-stream platforms (Instagram, TikTok, Twitter, web links)
        for target, label in [
            (1080, "🎬 Full 1080p"),
            (720,  "⚡ Fast 720p"),
            (480,  "💾 Data Saver 480p"),
        ]:
            sz = (bitrate_estimates.get(target, int(1.0 * 1024 * 1024 / 8)) * duration) if duration else 0
            if sz and best_audio_size:
                sz += best_audio_size
            size_str = format_size_str(sz)
            resolutions.append({
                "height": target,
                "label": f"{label}{size_str}"
            })

    audio_size_str = format_size_str(best_audio_size)
    return resolutions, audio_size_str


def resolve_youtube(url: str, extra_opts: dict | None = None) -> dict[str, Any]:
    """
    YouTube extraction cascade:
    1. Primary: Native multi-client extraction with JS challenge solving (provides 4K/1080p/720p/480p/audio).
    2. Secondary: Fallback to Android + TV-embedded + iOS player client rotation if primary faces bot challenge.
    """
    primary_opts = get_cookieless_ydl_opts(url, extra_opts)
    primary_opts.setdefault("extract_flat", False)
    primary_opts.setdefault("skip_download", True)
    primary_opts.setdefault("no_playlist", True)

    try:
        with yt_dlp.YoutubeDL(primary_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info and (info.get("url") or info.get("formats")):
                return info
    except Exception as primary_err:
        logger.warning(f"Primary YouTube extraction failed ({primary_err}). Cascading to mobile/TV client rotation...")
        fallback_opts = get_cookieless_ydl_opts(url, extra_opts)
        fallback_opts.setdefault("extract_flat", False)
        fallback_opts.setdefault("skip_download", True)
        fallback_opts.setdefault("no_playlist", True)
        fallback_opts.setdefault("extractor_args", {})
        fallback_opts["extractor_args"]["youtube"] = {
            "player_client": ["android", "tv_embedded", "ios"]
        }
        try:
            with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info and (info.get("url") or info.get("formats")):
                    return info
        except Exception as fb_err:
            logger.error(f"Fallback YouTube extraction also failed: {fb_err}")
            raise primary_err

    raise RuntimeError(f"Could not extract playable stream formats from YouTube URL: {url}")


def extract_media_info(url: str, extra_opts: dict | None = None) -> dict[str, Any]:
    """
    Main entry point for extracting media metadata without cookies:
    Routes URLs to their specialized platform resolvers or native yt-dlp with
    cookieless cascades.
    """
    url_lower = url.lower()

    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        return resolve_youtube(url, extra_opts)
    elif "instagram.com" in url_lower or "instagr.am" in url_lower:
        return resolve_instagram(url, extra_opts)
    elif "twitter.com" in url_lower or "x.com" in url_lower:
        return resolve_twitter(url, extra_opts)
    elif "tiktok.com" in url_lower:
        return resolve_tiktok(url, extra_opts)
    elif "reddit.com" in url_lower or "redd.it" in url_lower:
        return resolve_reddit(url, extra_opts)

    # General platforms
    opts = get_cookieless_ydl_opts(url, extra_opts)
    opts.setdefault("extract_flat", False)
    opts.setdefault("skip_download", True)
    opts.setdefault("no_playlist", True)

    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


class CookielessExtractorEngine:
    """
    Unified Cookieless Extractor Engine.
    Coordinates platform resolver cascades and player client rotations.
    """
    @staticmethod
    def get_ydl_opts(url: str | None = None, extra_opts: dict | None = None) -> dict[str, Any]:
        return get_cookieless_ydl_opts(url, extra_opts)

    @classmethod
    def resolve_youtube(cls, url: str, extra_opts: dict | None = None) -> dict[str, Any]:
        return resolve_youtube(url, extra_opts)

    @classmethod
    def resolve_instagram(cls, url: str, extra_opts: dict | None = None) -> dict[str, Any]:
        return resolve_instagram(url, extra_opts)

    @classmethod
    def resolve_twitter(cls, url: str, extra_opts: dict | None = None) -> dict[str, Any]:
        return resolve_twitter(url, extra_opts)

    @classmethod
    def resolve_tiktok(cls, url: str, extra_opts: dict | None = None) -> dict[str, Any]:
        return resolve_tiktok(url, extra_opts)

    @classmethod
    def resolve_reddit(cls, url: str, extra_opts: dict | None = None) -> dict[str, Any]:
        return resolve_reddit(url, extra_opts)

    @classmethod
    def extract_info(cls, url: str, extra_opts: dict | None = None) -> dict[str, Any]:
        return extract_media_info(url, extra_opts)

    @staticmethod
    def extract_smart_formats(info: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        return extract_smart_formats(info)
