# Phase 1: Download Queueing, Concurrency Limits & Cookieless Extraction — Context

**Gathered:** 2026-11-08  
**Status:** Ready for planning  

<domain>
## Phase Boundary

Eliminate all user cookie requirements by utilizing modern client emulation and resolver fallbacks, while protecting Android Termux resources via a strict single-worker FIFO download queue, user cancellation, and automated temporary disk space cleanup.

</domain>

<decisions>
## Implementation Decisions

### 1. Cookieless Media Extraction (Zero Cookies Strategy)
- **D-01 (YouTube):** Emulate `visionos` and `android_vr` player clients in `yt-dlp` instead of `web`. Confirmed empirically to return all 48 resolutions (360p up to 4K 2160p + MP3) on residential IP with zero bot challenge, zero PO token, and zero cookies required.
- **D-02 (Instagram):** Implement an automated fallback cascade:
  1. Primary: Native `yt-dlp` with mobile user-agent headers.
  2. Secondary: Public embed resolver (`eeinstagram`, `instagramez`, or public Cobalt endpoints) returning direct CDN MP4 links (`scontent.cdninstagram.com`).
  3. Remove `/cookies` command and cookie upload requirements from user UI.
- **D-03 (Twitter / X):** Fallback cascade integrating `api.fxtwitter.com` (extracts direct MP4 URLs without auth) when `yt-dlp` meets rate limits.
- **D-04 (TikTok, Reddit, SoundCloud, Pinterest):** Direct extraction with `curl_cffi` browser TLS impersonation and `tikwm` fallback.

### 2. Concurrency & Queue Management
- **D-05 (Concurrency Limit):** Strict **Single Worker (1 concurrent download at a time)** on Android Termux. New requests enter a FIFO queue to prevent RAM spikes (keeps bot < 100MB RAM) and disk I/O thrashing.
- **D-06 (Queue Feedback):** When a download is enqueued, the user receives an interactive ticket:
  `⏳ You are in queue (#1). Download will start automatically when current task finishes.`
  Includes an interactive `[❌ Cancel Queue]` button.
- **D-07 (Download Cancellation):** Users can cancel queued or active downloads via button click or `/cancel`. Cancelling an active download immediately terminates the worker thread and removes the temporary download folder.

### 3. Termux Disk Safety & Headroom
- **D-08 (Disk Headroom Guard):** Before downloading, check available storage in the working volume (`shutil.disk_usage`). If free disk space is less than 500 MB (or 1.5x expected media size), reject download with a clear advisory to free up phone storage.
- **D-09 (Garbage Collection):** On bot startup and after every download completion/failure/cancellation, sweep and delete orphaned `.part`, `.ytdl`, `.temp`, and stale `dl_*` folders.

### Antigravity's Discretion
- Choice of async queue implementation (using `asyncio.Queue` or a bounded semaphore).
- Progress bar styling and update throttling (maintain 2.5s interval to avoid Telegram `RetryAfter`).

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing:**

- `.gsd/SPEC.md` — Vision, platform goals, and constraints.
- `.gsd/ROADMAP.md` — Phase structure and dependencies.
- `.gsd/ARCHITECTURE.md` — System architecture, module map, and data flow.
- `bot.py` — Current entry point and handlers.
- `fast_telethon.py` — Parallel MTProto upload engine.

</canonical_refs>
