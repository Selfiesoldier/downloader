# Project Roadmap: Universal 2GB Downloader Bot

> **Current Milestone:** v1.0 Production Hardening  
> **Current Phase:** Phase 1: Download Queueing, Concurrency Limits & Termux Disk Safety  
> **Status:** in_progress  

---

## Must-Haves (from SPEC)
- [x] 2 GB parallel MTProto upload engine via Telethon (`fast_telethon.py`).
- [x] In-chat Netscape cookie management (`/cookies` + document upload).
- [x] Anti-bot mobile client rotation (`android`, `ios`).
- [ ] Bounded concurrency and user download queueing to prevent Termux memory/disk exhaustion.
- [ ] User download cancellation (`/cancel` / inline button).
- [ ] Automated orphan temp directory and `.part` file cleanup.

---

## Phases

### Phase 1: Download Queueing, Concurrency Limits & Cookieless Extraction
**Status:** 🟡 Ready for Planning  
**Objective:** Eliminate user cookie requirements by utilizing modern client emulation and resolver fallbacks, while protecting Android Termux resources via a strict single-worker FIFO download queue, user cancellation, and automated temporary disk space cleanup.  
**Deliverables:**
- Cookieless YouTube extraction using `visionos` and `android_vr` client rotation.
- Automated cookieless fallback resolver for Instagram (embed CDN / Cobalt resolver) and Twitter (`api.fxtwitter.com`).
- Async download task queue manager with single-worker concurrency (`asyncio.Queue` / semaphore).
- Interactive "Cancel Download" inline button terminating active `yt-dlp` processes.
- Startup and post-download garbage collection for orphaned `dl_*` temp directories and stale `.part` files.
- Disk headroom check (aborts if free space < target file size or < 500 MB).

### Phase 2: Media Metadata, Thumbnails & Audio Tagging
**Status:** ⬜ Not Started  
**Objective:** Elevate user experience with rich Telegram media cards, video thumbnail attachments, and complete ID3 tags for audio files.  
**Deliverables:**
- High-resolution thumbnail generation and dynamic width/height extraction for native Telegram video streaming.
- Automatic ID3 tag embedding (album art, artist, title) for MP3 downloads from YouTube and SoundCloud.
- Audio duration metadata validation for seamless lockscreen playback.

### Phase 3: Multi-Post Carousels & Batch Playlist Delivery
**Status:** ⬜ Not Started  
**Objective:** Support batch extraction and delivery of multi-item media collections without race conditions or memory bottlenecks.  
**Deliverables:**
- Instagram multi-item carousel downloader delivering full photo/video albums as Telegram media groups.
- Controlled playlist extraction for YouTube and SoundCloud with sequential item delivery.
- Batch progress notifications.

### Phase 4: Lossless Video Splitting (> 2GB) & Production Hardening
**Status:** ⬜ Not Started  
**Objective:** Enable handling of ultra-long videos (e.g., 4K movies or podcasts > 2,000 MB) by splitting into streamable parts using zero-re-encode FFmpeg stream copying, plus automated health monitoring.  
**Deliverables:**
- Fast FFmpeg stream-copy chunk splitter for media files exceeding 2,000 MB.
- Cookie expiration pro-active notification.
- Automated watchdog and process recovery.

