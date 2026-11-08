# SPEC.md — Project Specification: Universal 2GB Downloader Bot

> **Status**: `FINALIZED`

## Vision
A resilient, high-speed Universal Media Downloader Telegram Bot capable of downloading and delivering media up to 2,000 MB (2 GB) on low-resource environments (Android Termux, lightweight Linux VPS, or Windows), featuring anti-bot session bypasses, multi-threaded extraction, robust download queueing, and intelligent resource protection.

## Goals
1. **Universal Multi-Platform Media Extraction:** Seamlessly download video and audio from YouTube (Videos, Shorts, Music), Instagram (Reels, Posts, Carousels), TikTok (HD watermark-free), X/Twitter, Reddit, Pinterest, Facebook, and SoundCloud.
2. **2 GB MTProto Turbo Uploads:** Direct Telegram MTProto integration via Telethon with 512KB chunks and 8 concurrent upload pipes, delivering files up to 2,000 MB with real-time percentage progress bars.
3. **Queueing, Concurrency & Disk Safety:** Protect low-memory and low-storage mobile environments (Termux) via bounded concurrency, per-user task queues, active download cancellation, and automated orphan file cleanup.
4. **Dynamic Anti-Bot & Cookie Management:** Hot-swappable Netscape cookie ingestion via Telegram chat (`/cookies`, document uploads) and mobile client rotation (`android`, `ios`) to bypass YouTube SABR bot checks and Instagram login walls without restarting.

## Non-Goals (Out of Scope)
- Developing a standalone web UI dashboard (bot interaction is purely Telegram-native).
- Intensive CPU re-encoding on device (all format merging and stream packaging uses lossless stream copying and lightweight FFmpeg audio extraction).
- Persistent file storage or archiving (all files are purged from disk immediately after transmission).

## Users
- Mobile Termux and personal Telegram users seeking an unconstrained 2GB media downloader that doesn't hit 50MB Bot API caps, doesn't drop during network blips, and allows easy bypass of platform bot gates.

## Constraints
- **Resource Constraints:** Must run stably on Android Termux with under 150 MB RAM and bounded local disk utilization.
- **Protocol Constraints:** Telegram MTProto file limit is hard-capped at 2,000 MB (2 GB). Bot API fallback is capped at 50 MB.
- **Network Constraints:** Resilient against cellular network blips, socket timeouts, and Telegram rate-limits (`RetryAfter`).

## Success Criteria
- [x] Zero 50MB file size limit blocks (files up to 2GB upload via MTProto).
- [x] YouTube bot challenge (`HFHl_tXSyaE`) and Instagram login blocks resolved via mobile client rotation & `/cookies`.
- [ ] Concurrency limits and queue supervisor prevent disk exhaustion and process crashes on Termux.
- [ ] Active in-flight downloads can be cancelled cleanly by users.
- [ ] Multi-part carousels and playlists deliver sequentially without race conditions.
