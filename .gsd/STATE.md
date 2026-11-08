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
- [ ] Phase 1: Download Queueing, Concurrency Limits & Termux Disk Safety (Active)
- [ ] Phase 2: Media Metadata, Thumbnails & Audio Tagging
- [ ] Phase 3: Multi-Post Carousels & Batch Playlist Delivery
- [ ] Phase 4: Lossless Video Splitting (> 2GB) & Production Hardening

