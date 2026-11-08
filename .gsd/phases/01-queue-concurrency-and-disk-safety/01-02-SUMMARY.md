# Phase 1 Plan 02: Parallel Segmented Chunk Downloader & Socket Acceleration — Summary

**Executed Date:** 2026-11-08  
**Status:** Completed  
**Plan Reference:** `.gsd/phases/01-queue-concurrency-and-disk-safety/01-02-PLAN.md`

---

## Executive Summary

Implemented the parallel segmented chunk downloader and socket acceleration subsystem in `chunk_downloader.py`, designed to maximize download throughput (up to 40 Mbps) for both progressive media and multi-fragment video/audio streams on low-memory environments (Android Termux, lightweight Linux VPS, Windows).

The implementation guarantees:
1. Multi-stream parallel range downloading (4 to 8 concurrent connections).
2. Strict memory conservation using 64KB streaming buffers (under 150MB RAM ceiling).
3. Zero-loss sequential reassembly validated against source byte lengths and SHA-256 hashes.
4. Auto-detection of `aria2c` across Android Termux, system PATH, and local `bin/` with safe non-allocating flags (`--file-allocation=none`).

---

## Deliverables & Changes

### 1. `chunk_downloader.py`
- **`calculate_byte_ranges`**: Partitions file sizes into non-overlapping `[start, end]` ranges (default 4 to 8 pipes, min 1MB per chunk).
- **`reassemble_chunks`**: Validates each `chunk_XX.part` file size against expected range bytes, sequentially copies in 64KB blocks, verifies total file size against `expected_total_size`, purges temporary chunks, and performs atomic file replacement. Rolls back target files and raises `ChunkIntegrityError` if corruption is detected.
- **`_ProgressTracker`**: Async lock-guarded progress aggregator calculating real-time aggregate bytes downloaded across concurrent workers.
- **`SegmentedChunkDownloader`**: Inspects `Content-Length` and `Accept-Ranges` (via HEAD and probe GET Range). Falls back gracefully to single-stream downloading when ranges are unsupported. Runs concurrent async range downloads and triggers verified reassembly.
- **`detect_aria2c`**: Auto-detects `aria2c` binary on Android Termux (`/data/data/com.termux/files/usr/bin/aria2c`), system PATH (`shutil.which`), and local `bin/` (`aria2c` or `aria2c.exe`).
- **`get_turbo_downloader_opts`**: Generates `yt-dlp` acceleration parameters: 8 concurrent fragments, 1MB buffers, 10MB HTTP chunk size, 5 retries, and injects `aria2c` with `-s 8 -x 8 -k 1M -j 8 --file-allocation=none` if detected.
- **`download_media_turbo`**: Async entry point combining turbo yt-dlp downloading and direct segmented chunk downloading.

### 2. `tests/test_chunk_downloader.py`
- **`test_chunk_split_calculation`**: Asserts 100MB file with concurrency=4 splits into 4 exact non-overlapping ranges covering 0 to 104,857,599.
- **`test_chunk_split_edge_cases`**: Validates boundary conditions (0 bytes, small files < 1MB, concurrency=1).
- **`test_chunk_reassembly_integrity`**: Validates reassembled binary matches original multi-part bytes with identical SHA-256 hash and verifies chunk purge.
- **`test_corrupted_chunk_rollback`**: Verifies `ChunkIntegrityError` and destination file rollback when a chunk is missing, undersized, or total size mismatches.
- **`test_aria2c_detector`**: Verifies detection and injection of external downloader options when `aria2c` is present or omitted when absent.
- **`test_detect_aria2c_path_resolution`**: Tests path resolution across Termux, PATH, and missing states.
- **`test_segmented_download_with_mock_transport`**: End-to-end async integration test using `httpx.MockTransport` simulating 206 Partial Content server and validating byte accuracy and progress callbacks.

---

## Verification & Empirical Proof

```bash
& ".venv/Scripts/python.exe" -m unittest tests/test_chunk_downloader.py
Ran 7 tests in 0.223s - OK

& ".venv/Scripts/python.exe" -c "import chunk_downloader; print('chunk_downloader module ready')"
chunk_downloader module ready
```

All 7 unit tests passed with 100% success without external network dependencies.

---

## Commits Landed

1. `d28fc0a` — `feat(phase-1): Implement SegmentedChunkDownloader and byte-range assembler in chunk_downloader.py`
2. `380d780` — `feat(phase-1): Implement aria2c auto-detection and turbo yt-dlp fragment options`
3. `cfae291` — `feat(phase-1): Build automated unit tests in tests/test_chunk_downloader.py`

---

## Deferred Items

- Unstaged working changes in `bot.py` belong to the parallel execution of Plan 01.01 (Cookieless Media Extraction) and were left uncommitted in accordance with scope isolation rules.
