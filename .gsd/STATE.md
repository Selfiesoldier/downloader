# GSD State Tracking — Universal 2GB Downloader Bot

**Milestone:** v1.0 Production Hardening  
**Current Phase:** Phase 1: Download Queueing, Concurrency Limits & Termux Disk Safety  
**Status:** in_progress  

## Architectural Principle Verified & Enforced
1. **2 GB MTProto Parallel Engine:** Uses Telethon `TelegramClient` inside the same event loop, with 512KB chunks and concurrency=8 via `fast_telethon.py`. Bypasses standard Bot API 50MB caps.
2. **Anti-Bot Mobile Client Rotation:** Default unauthenticated extractor uses `["android", "ios"]` to bypass YouTube SABR web anti-bot challenge.
3. **In-Chat Cookie Management:** Owner can upload `youtube_cookies.txt`, `instagram_cookies.txt`, or `cookies.txt` directly to the bot to activate authenticated sessions instantly.
4. **Resilient Network Handlers:** Uses `safe_edit_message` with retries on mobile network drops (`httpx.ConnectError`).

## Current Milestone Goals
- [x] Phase 1: Download Queueing, Concurrency Limits & Turbo Parallel Chunking (Complete ✅)
- [ ] Phase 2: Media Metadata, Thumbnails & Audio Tagging
- [ ] Phase 3: Multi-Post Carousels & Batch Playlist Delivery
- [ ] Phase 4: Lossless Video Splitting (> 2GB) & Production Hardening

## Verified Architectures in Phase 1
1. **Cookieless Extractor Engine (`extractors.py`):**
   - YouTube: Emulates `visionos` and `android_vr` client rotation with zero cookies, returning full resolutions (360p - 4K 2160p + MP3).
   - Instagram: Direct mobile extraction with automatic embed CDN fallback (`eeinstagram`, `instagramez`, Cobalt).
   - Twitter / X: Native extraction with automatic fallback to `api.fxtwitter.com`.
   - TikTok / Reddit: Direct extraction with `curl_cffi` browser TLS impersonation and `tikwm` fallback.
   - Clean UI: Completely purged `/cookies` and cookie document uploads from user flow.
2. **Parallel Segmented Chunk Downloader (`chunk_downloader.py`):**
   - Direct HTTP byte-range parallel downloading (`Range: bytes=start-end`) across 4-8 concurrent streams.
   - Ordered chunk reassembly (`chunk_00.part` -> final) with strict `Content-Length` validation (zero data loss).
   - `aria2c` auto-detection and execution flags for Termux C-level socket acceleration.
3. **Single-Worker FIFO Queue & Cancellation (`queue_manager.py`):**
   - Bounded `max_concurrency = 1` for Android Termux to prevent RAM spikes (< 100MB RAM) and disk contention.
   - Live queue position tickets (`⏳ In queue (#1)`) with `[❌ Cancel Queue]` button.
   - Interactive `/cancel` terminates active subprocesses / workers and purges temp directories.
4. **Pipelined Upload & Resource Guard (`resource_guard.py`, `fast_telethon.py`):**
   - Pipelined streaming: Sends 512KB chunks directly to Telegram (`SaveBigFilePartRequest`) concurrently while downloading.
   - Multi-TCP MTProto pooling: Multiplexes upload parts across parallel TCP connections to bypass Telegram's single-socket speed caps.
   - 500 MB disk headroom check before downloading + automatic orphan cleanup on startup and post-download.

