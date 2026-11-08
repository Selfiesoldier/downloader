"""
Parallel Segmented Chunk Downloader & Socket Acceleration Module.

Provides:
- SegmentedChunkDownloader: Multi-stream byte-range HTTP downloader for progressive media.
- reassemble_chunks: Zero-loss sequential reassembly and validation of indexed chunk parts.
- calculate_byte_ranges: Slices file size into non-overlapping byte ranges.
- ChunkIntegrityError: Explicit exception raised when chunk or reassembly validation fails.
- detect_aria2c: Cross-platform aria2c binary locator (Termux, PATH, local bin).
- get_turbo_downloader_opts: High-performance fragment & socket acceleration options for yt-dlp.
- download_media_turbo: Unified entry point for turbo-accelerated media retrieval.
"""

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Any

import httpx

logger = logging.getLogger("ChunkDownloader")


class ChunkIntegrityError(Exception):
    """Raised when downloaded chunks are missing, corrupted, or reassembly fails."""
    pass


def calculate_byte_ranges(
    total_size: int,
    concurrency: int = 4,
    min_chunk_size: int = 1024 * 1024,  # 1 MB min chunk
) -> List[Tuple[int, int]]:
    """
    Calculates non-overlapping byte ranges covering 0 to total_size - 1.

    Args:
        total_size: Total file size in bytes.
        concurrency: Target number of parallel chunk streams (default 4).
        min_chunk_size: Minimum bytes per chunk to avoid over-segmentation (default 1MB).

    Returns:
        List of (start_byte, end_byte) inclusive tuples.
    """
    if total_size <= 0:
        return []

    if total_size < min_chunk_size:
        return [(0, total_size - 1)]

    max_chunks = max(1, total_size // min_chunk_size)
    num_chunks = min(max(1, concurrency), max_chunks)

    if num_chunks <= 1:
        return [(0, total_size - 1)]

    chunk_size = total_size // num_chunks
    ranges: List[Tuple[int, int]] = []

    for i in range(num_chunks):
        start = i * chunk_size
        if i == num_chunks - 1:
            end = total_size - 1
        else:
            end = (i + 1) * chunk_size - 1
        ranges.append((start, end))

    return ranges


def reassemble_chunks(
    chunks_dir: str | Path,
    destination_file: str | Path,
    expected_total_size: Optional[int] = None,
    expected_chunks: Optional[List[Tuple[int, int]]] = None,
    buffer_size: int = 64 * 1024,
) -> int:
    """
    Verifies and sequentially concatenates indexed chunk parts (chunk_00.part, ...)
    into the destination file with zero byte loss. Purges chunk parts upon success.
    Aborts, cleans up destination file, and raises ChunkIntegrityError on any mismatch.

    Args:
        chunks_dir: Directory containing chunk_XX.part files.
        destination_file: Path to final reassembled output file.
        expected_total_size: Expected total size in bytes for strict verification.
        expected_chunks: List of (start, end) tuples matching chunk indexes.
        buffer_size: Buffer size for binary stream copy (default 64KB for RAM safety).

    Returns:
        Total bytes written.
    """
    chunks_path = Path(chunks_dir)
    dest_path = Path(destination_file)

    if not chunks_path.is_dir():
        raise ChunkIntegrityError(f"Chunks directory does not exist: {chunks_path}")

    # Determine chunk files and validate presence / sizes
    chunk_files: List[Path] = []

    if expected_chunks is not None:
        if not expected_chunks:
            raise ChunkIntegrityError("Expected chunks list is empty")

        for idx, (start, end) in enumerate(expected_chunks):
            chunk_file = chunks_path / f"chunk_{idx:02d}.part"
            if not chunk_file.exists():
                raise ChunkIntegrityError(f"Missing required chunk file: {chunk_file.name}")

            expected_size = end - start + 1
            actual_size = chunk_file.stat().st_size
            if actual_size != expected_size:
                raise ChunkIntegrityError(
                    f"Corrupt chunk {chunk_file.name}: expected {expected_size} bytes, got {actual_size} bytes"
                )
            chunk_files.append(chunk_file)
    else:
        chunk_files = sorted(chunks_path.glob("chunk_*.part"))
        if not chunk_files:
            raise ChunkIntegrityError(f"No chunk files found in {chunks_path}")

        # Verify sequential naming: chunk_00.part, chunk_01.part, etc.
        for idx, cf in enumerate(chunk_files):
            expected_name = f"chunk_{idx:02d}.part"
            if cf.name != expected_name:
                raise ChunkIntegrityError(
                    f"Invalid chunk sequence: found {cf.name}, expected {expected_name}"
                )

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dest = dest_path.with_name(f"{dest_path.name}.reassembling")

    total_written = 0
    try:
        with open(temp_dest, "wb") as outfile:
            for cf in chunk_files:
                with open(cf, "rb") as infile:
                    while True:
                        buf = infile.read(buffer_size)
                        if not buf:
                            break
                        outfile.write(buf)
                        total_written += len(buf)

        if expected_total_size is not None and total_written != expected_total_size:
            raise ChunkIntegrityError(
                f"Reassembled size {total_written} bytes does not match expected {expected_total_size} bytes"
            )

        # Atomic replacement of target file
        if dest_path.exists():
            dest_path.unlink()
        temp_dest.replace(dest_path)

        # Purge temporary chunk parts immediately after verified reassembly
        for cf in chunk_files:
            try:
                cf.unlink(missing_ok=True)
            except OSError as e:
                logger.debug(f"Could not purge {cf.name}: {e}")

        return total_written

    except Exception as e:
        # Rollback temporary files
        if temp_dest.exists():
            try:
                temp_dest.unlink(missing_ok=True)
            except OSError:
                pass
        if dest_path.exists():
            try:
                dest_path.unlink(missing_ok=True)
            except OSError:
                pass

        if isinstance(e, ChunkIntegrityError):
            raise
        raise ChunkIntegrityError(f"Failed to reassemble chunks: {e}") from e


class _ProgressTracker:
    """Thread-safe progress aggregator across concurrent chunk downloads."""

    def __init__(self, total_bytes: int, callback: Optional[Callable[[int, int], Any]] = None):
        self.total_bytes = total_bytes
        self.callback = callback
        self.chunk_progress: dict[int, int] = {}
        self._lock = asyncio.Lock()

    async def update(self, chunk_index: int, downloaded_bytes: int):
        if not self.callback:
            return

        async with self._lock:
            self.chunk_progress[chunk_index] = downloaded_bytes
            aggregate = sum(self.chunk_progress.values())

        try:
            res = self.callback(aggregate, self.total_bytes)
            if asyncio.iscoroutine(res):
                await res
        except Exception as e:
            logger.debug(f"Progress callback error: {e}")


class SegmentedChunkDownloader:
    """
    Parallel multi-chunk byte-range HTTP downloader.
    Inspects Content-Length and Accept-Ranges, downloads parts concurrently,
    and reassembles them sequentially with zero byte loss.
    """

    def __init__(
        self,
        concurrency: int = 4,
        min_chunk_size: int = 1024 * 1024,  # 1 MB
        chunk_timeout: float = 300.0,
        buffer_size: int = 64 * 1024,        # 64 KB
    ):
        self.concurrency = max(1, min(concurrency, 16))
        self.min_chunk_size = min_chunk_size
        self.chunk_timeout = chunk_timeout
        self.buffer_size = buffer_size

    async def inspect_url(
        self,
        url: str,
        headers: Optional[dict] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> Tuple[Optional[int], bool]:
        """
        Inspects headers via HEAD (or probe GET Range) to determine Content-Length
        and byte-range support.
        """
        req_headers = headers.copy() if headers else {}
        close_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=self.chunk_timeout, follow_redirects=True)
            close_client = True

        try:
            content_length: Optional[int] = None
            supports_ranges = False

            try:
                resp = await client.head(url, headers=req_headers)
                if resp.status_code < 400:
                    cl_header = resp.headers.get("content-length")
                    if cl_header and cl_header.isdigit():
                        content_length = int(cl_header)

                    accept_ranges = resp.headers.get("accept-ranges", "").lower()
                    if "bytes" in accept_ranges:
                        supports_ranges = True
            except Exception as e:
                logger.debug(f"HEAD request failed: {e}")

            # Probe range support if HEAD didn't confirm Accept-Ranges
            if not supports_ranges:
                try:
                    probe_headers = req_headers.copy()
                    probe_headers["Range"] = "bytes=0-0"
                    probe_resp = await client.get(url, headers=probe_headers)
                    if probe_resp.status_code == 206:
                        supports_ranges = True
                        cr = probe_resp.headers.get("content-range", "")
                        if "/" in cr:
                            total_str = cr.split("/")[-1].strip()
                            if total_str.isdigit():
                                content_length = int(total_str)
                except Exception as e:
                    logger.debug(f"Range probe request failed: {e}")

            return content_length, supports_ranges
        finally:
            if close_client:
                await client.aclose()

    async def _download_segment(
        self,
        url: str,
        chunk_index: int,
        start_byte: int,
        end_byte: int,
        part_path: Path,
        tracker: _ProgressTracker,
        client: httpx.AsyncClient,
        base_headers: Optional[dict] = None,
        max_retries: int = 3,
    ) -> Path:
        """Downloads a single byte-range segment directly to disk."""
        headers = base_headers.copy() if base_headers else {}
        headers["Range"] = f"bytes={start_byte}-{end_byte}"
        expected_size = end_byte - start_byte + 1

        for attempt in range(1, max_retries + 1):
            downloaded = 0
            try:
                async with client.stream("GET", url, headers=headers) as response:
                    if response.status_code not in (200, 206):
                        response.raise_for_status()

                    with open(part_path, "wb") as f:
                        async for chunk in response.aiter_bytes(chunk_size=self.buffer_size):
                            f.write(chunk)
                            downloaded += len(chunk)
                            await tracker.update(chunk_index, downloaded)

                actual_size = part_path.stat().st_size
                if actual_size == expected_size:
                    return part_path

                logger.warning(
                    f"Chunk {chunk_index} size mismatch (attempt {attempt}/{max_retries}): "
                    f"expected {expected_size}, got {actual_size}"
                )
            except Exception as e:
                logger.warning(f"Chunk {chunk_index} download error (attempt {attempt}/{max_retries}): {e}")

            if attempt < max_retries:
                await asyncio.sleep(1.0 * attempt)

        raise ChunkIntegrityError(
            f"Chunk {chunk_index} ({start_byte}-{end_byte}) failed after {max_retries} attempts"
        )

    async def download(
        self,
        url: str,
        destination_file: str | Path,
        progress_callback: Optional[Callable[[int, int], Any]] = None,
        headers: Optional[dict] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> Path:
        """
        Executes parallel multi-chunk download if supported, or streams single file directly.
        Reassembles chunks with zero loss and validates final byte size.
        """
        dest_path = Path(destination_file).resolve()
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        close_client = False
        client_to_use = client
        if client_to_use is None:
            client_to_use = httpx.AsyncClient(timeout=self.chunk_timeout, follow_redirects=True)
            close_client = True

        try:
            content_length, supports_ranges = await self.inspect_url(url, headers=headers, client=client_to_use)

            # Fallback to single-stream download if ranges not supported or size unknown
            if not supports_ranges or content_length is None or content_length <= 0:
                logger.info("Direct range download not supported; falling back to single-stream")
                req_headers = headers.copy() if headers else {}
                async with client_to_use.stream("GET", url, headers=req_headers) as response:
                    response.raise_for_status()
                    total_len = int(response.headers.get("content-length", 0)) or 0
                    downloaded = 0
                    temp_file = dest_path.with_name(f"{dest_path.name}.stream")
                    try:
                        with open(temp_file, "wb") as f:
                            async for chunk in response.aiter_bytes(chunk_size=self.buffer_size):
                                f.write(chunk)
                                downloaded += len(chunk)
                                if progress_callback:
                                    res = progress_callback(downloaded, total_len or downloaded)
                                    if asyncio.iscoroutine(res):
                                        await res

                        if dest_path.exists():
                            dest_path.unlink()
                        temp_file.replace(dest_path)
                        return dest_path
                    finally:
                        if temp_file.exists():
                            temp_file.unlink(missing_ok=True)

            # Parallel multi-chunk segmented download
            ranges = calculate_byte_ranges(
                total_size=content_length,
                concurrency=self.concurrency,
                min_chunk_size=self.min_chunk_size,
            )

            chunks_dir = dest_path.parent / f".chunks_{dest_path.stem}_{int(time.time() * 1000)}"
            chunks_dir.mkdir(parents=True, exist_ok=True)
            tracker = _ProgressTracker(total_bytes=content_length, callback=progress_callback)

            try:
                tasks = [
                    self._download_segment(
                        url=url,
                        chunk_index=idx,
                        start_byte=start,
                        end_byte=end,
                        part_path=chunks_dir / f"chunk_{idx:02d}.part",
                        tracker=tracker,
                        client=client_to_use,
                        base_headers=headers,
                    )
                    for idx, (start, end) in enumerate(ranges)
                ]
                await asyncio.gather(*tasks)

                reassemble_chunks(
                    chunks_dir=chunks_dir,
                    destination_file=dest_path,
                    expected_total_size=content_length,
                    expected_chunks=ranges,
                    buffer_size=self.buffer_size,
                )
                return dest_path
            finally:
                if chunks_dir.exists():
                    shutil.rmtree(chunks_dir, ignore_errors=True)
        finally:
            if close_client:
                await client_to_use.aclose()


def detect_aria2c() -> Optional[str]:
    """
    Checks Termux binary path (/data/data/com.termux/files/usr/bin/aria2c).
    Checks system PATH via shutil.which("aria2c").
    Checks local bin directory (bin/aria2c or bin/aria2c.exe).
    Returns verified executable path or None.
    """
    termux_aria2c = "/data/data/com.termux/files/usr/bin/aria2c"
    if os.path.exists(termux_aria2c) and os.access(termux_aria2c, os.X_OK):
        return termux_aria2c

    which_aria2c = shutil.which("aria2c")
    if which_aria2c:
        return which_aria2c

    base_dir = Path(__file__).resolve().parent
    local_bin = base_dir / "bin" / ("aria2c.exe" if os.name == "nt" else "aria2c")
    if local_bin.exists() and (os.name == "nt" or os.access(local_bin, os.X_OK)):
        return str(local_bin)

    return None


def get_turbo_downloader_opts(base_opts: Optional[dict] = None, concurrency: int = 8) -> dict:
    """
    Configures turbo downloader options for yt-dlp:
    - 8 concurrent fragment downloads
    - 1MB buffersize, 10MB http_chunk_size
    - 5 retries and fragment retries
    - aria2c external downloader with --file-allocation=none if detected
    """
    opts = dict(base_opts) if base_opts else {}

    opts["concurrent_fragment_downloads"] = concurrency
    opts["buffersize"] = 1048576       # 1MB
    opts["http_chunk_size"] = 10485760 # 10MB
    opts["retries"] = 5
    opts["fragment_retries"] = 5

    aria2c_path = detect_aria2c()
    if aria2c_path:
        opts["external_downloader"] = {"default": aria2c_path}
        opts["external_downloader_args"] = {
            "aria2c": [
                "-s", str(concurrency),
                "-x", str(concurrency),
                "-k", "1M",
                "-j", str(concurrency),
                "--file-allocation=none",
                "--summary-interval=0",
            ]
        }

    return opts


async def download_media_turbo(
    url: str,
    mode: str = "auto",
    temp_dir: str | Path = "temp",
    ydl_opts: Optional[dict] = None,
    progress_callback: Optional[Callable[[int, int], Any]] = None,
) -> Path:
    """
    Wrapper combining turbo downloader options with ytdl or segmented direct downloader.
    """
    temp_path = Path(temp_dir).resolve()
    temp_path.mkdir(parents=True, exist_ok=True)

    if mode == "direct":
        # Extract filename from URL or assign fallback
        clean_url = url.split("?")[0].rstrip("/")
        name = clean_url.split("/")[-1] or f"media_{int(time.time())}"
        dest_file = temp_path / name
        downloader = SegmentedChunkDownloader(concurrency=8)
        return await downloader.download(
            url=url,
            destination_file=dest_file,
            progress_callback=progress_callback,
        )

    # yt-dlp turbo fragment extraction
    import yt_dlp

    opts = get_turbo_downloader_opts(base_opts=ydl_opts, concurrency=8)
    if "outtmpl" not in opts:
        opts["outtmpl"] = str(temp_path / "%(id)s.%(ext)s")

    def _sync_ytdl_download():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                if "requested_downloads" in info and info["requested_downloads"]:
                    target = info["requested_downloads"][0].get("filepath")
                    if target and Path(target).exists():
                        return Path(target)
                target = ydl.prepare_filename(info)
                if Path(target).exists():
                    return Path(target)
                candidates = list(temp_path.glob(f"{info.get('id', '')}.*"))
                if candidates:
                    return candidates[0]
            files = [p for p in temp_path.iterdir() if p.is_file() and not p.name.endswith(".part")]
            if files:
                return max(files, key=lambda f: f.stat().st_mtime)
            raise FileNotFoundError(f"yt-dlp downloaded media file not found in {temp_path}")

    return await asyncio.to_thread(_sync_ytdl_download)

