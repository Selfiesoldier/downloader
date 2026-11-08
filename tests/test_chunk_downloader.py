"""
Unit tests for chunk_downloader.py:
- Byte-range calculation and slicing
- Zero-loss sequential reassembly and SHA-256 verification
- Corrupted / missing chunk rollback and integrity errors
- aria2c detection and turbo downloader options
- End-to-end async mock range download and reassembly
"""

import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import httpx

from chunk_downloader import (
    calculate_byte_ranges,
    reassemble_chunks,
    detect_aria2c,
    get_turbo_downloader_opts,
    ChunkIntegrityError,
    SegmentedChunkDownloader,
)


class TestChunkDownloader(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_chunk_dl_")
        self.chunks_dir = Path(self.temp_dir) / "chunks"
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        self.dest_file = Path(self.temp_dir) / "output.bin"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_chunk_split_calculation(self):
        """
        Verify that a 100MB file with concurrency=4 calculates 4 non-overlapping
        byte ranges covering 0 to 104857599 exactly.
        """
        total_size = 100 * 1024 * 1024  # 104,857,600 bytes
        concurrency = 4
        ranges = calculate_byte_ranges(total_size=total_size, concurrency=concurrency)

        self.assertEqual(len(ranges), 4)

        # Check total range bounds
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], 104857599)

        # Verify exact continuity and non-overlapping coverage
        total_covered = 0
        for i in range(len(ranges)):
            start, end = ranges[i]
            self.assertLess(start, end)
            total_covered += (end - start + 1)
            if i > 0:
                self.assertEqual(start, ranges[i - 1][1] + 1)

        self.assertEqual(total_covered, total_size)

    def test_chunk_split_edge_cases(self):
        """Test file sizes smaller than min_chunk_size or invalid sizes."""
        # 0 or negative bytes
        self.assertEqual(calculate_byte_ranges(0), [])
        self.assertEqual(calculate_byte_ranges(-500), [])

        # File smaller than 1MB min_chunk_size
        small_size = 500 * 1024
        ranges = calculate_byte_ranges(small_size, concurrency=4)
        self.assertEqual(ranges, [(0, small_size - 1)])

        # Concurrency 1
        ranges = calculate_byte_ranges(10 * 1024 * 1024, concurrency=1)
        self.assertEqual(len(ranges), 1)
        self.assertEqual(ranges[0], (0, 10 * 1024 * 1024 - 1))

    def test_chunk_reassembly_integrity(self):
        """
        Generate dummy data chunks (chunk_00.part to chunk_03.part) with known
        sha256 hashes, run reassemble_chunks, and verify the assembled file sha256
        matches the original concatenated content.
        """
        chunk_data = [
            b"PART0_HEADER_" + os.urandom(256 * 1024),
            b"PART1_PAYLOAD_" + os.urandom(512 * 1024),
            b"PART2_DATA_" + os.urandom(128 * 1024),
            b"PART3_TRAILER_" + os.urandom(64 * 1024),
        ]

        expected_chunks = []
        offset = 0
        original_bytes = b"".join(chunk_data)
        original_sha256 = hashlib.sha256(original_bytes).hexdigest()

        for idx, data in enumerate(chunk_data):
            part_path = self.chunks_dir / f"chunk_{idx:02d}.part"
            with open(part_path, "wb") as f:
                f.write(data)
            expected_chunks.append((offset, offset + len(data) - 1))
            offset += len(data)

        total_bytes = len(original_bytes)

        # Run reassembly
        written = reassemble_chunks(
            chunks_dir=self.chunks_dir,
            destination_file=self.dest_file,
            expected_total_size=total_bytes,
            expected_chunks=expected_chunks,
        )

        self.assertEqual(written, total_bytes)
        self.assertTrue(self.dest_file.exists())
        self.assertEqual(self.dest_file.stat().st_size, total_bytes)

        # Verify sha256 matches
        with open(self.dest_file, "rb") as f:
            reassembled_sha256 = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(reassembled_sha256, original_sha256)

        # Verify chunk parts were purged
        for idx in range(len(chunk_data)):
            part_path = self.chunks_dir / f"chunk_{idx:02d}.part"
            self.assertFalse(part_path.exists())

    def test_corrupted_chunk_rollback(self):
        """
        Simulate missing or undersized chunk parts; verify reassembly aborts,
        cleans up target, and raises an explicit integrity error.
        """
        chunk_00 = self.chunks_dir / "chunk_00.part"
        chunk_01 = self.chunks_dir / "chunk_01.part"

        with open(chunk_00, "wb") as f:
            f.write(b"X" * 100)
        # Undersized: expected 100 bytes, write only 40 bytes
        with open(chunk_01, "wb") as f:
            f.write(b"Y" * 40)

        expected_chunks = [(0, 99), (100, 199)]

        with self.assertRaises(ChunkIntegrityError) as ctx:
            reassemble_chunks(
                chunks_dir=self.chunks_dir,
                destination_file=self.dest_file,
                expected_total_size=200,
                expected_chunks=expected_chunks,
            )

        self.assertIn("Corrupt chunk", str(ctx.exception))
        self.assertFalse(self.dest_file.exists())

        # Test missing chunk
        chunk_01.unlink()
        with self.assertRaises(ChunkIntegrityError) as ctx2:
            reassemble_chunks(
                chunks_dir=self.chunks_dir,
                destination_file=self.dest_file,
                expected_total_size=200,
                expected_chunks=expected_chunks,
            )

        self.assertIn("Missing required chunk", str(ctx2.exception))
        self.assertFalse(self.dest_file.exists())

        # Test total size mismatch
        with open(chunk_01, "wb") as f:
            f.write(b"Y" * 100)
        with self.assertRaises(ChunkIntegrityError) as ctx3:
            reassemble_chunks(
                chunks_dir=self.chunks_dir,
                destination_file=self.dest_file,
                expected_total_size=999,  # Mismatched expected total
                expected_chunks=expected_chunks,
            )
        self.assertIn("does not match expected 999 bytes", str(ctx3.exception))
        self.assertFalse(self.dest_file.exists())

    def test_aria2c_detector(self):
        """
        Mock shutil.which returning a path and returning None; verify
        get_turbo_downloader_opts adds or omits aria2c external downloader args accordingly.
        """
        # Case A: aria2c available on PATH
        with patch("chunk_downloader.detect_aria2c", return_value="/usr/bin/aria2c"):
            opts = get_turbo_downloader_opts(concurrency=8)
            self.assertEqual(opts["concurrent_fragment_downloads"], 8)
            self.assertEqual(opts["buffersize"], 1048576)
            self.assertEqual(opts["http_chunk_size"], 10485760)
            self.assertEqual(opts["retries"], 5)
            self.assertEqual(opts["fragment_retries"], 5)
            self.assertIn("external_downloader", opts)
            self.assertEqual(opts["external_downloader"]["default"], "/usr/bin/aria2c")
            self.assertIn("external_downloader_args", opts)
            aria_args = opts["external_downloader_args"]["aria2c"]
            self.assertIn("-s", aria_args)
            self.assertIn("-x", aria_args)
            self.assertIn("--file-allocation=none", aria_args)

        # Case B: aria2c NOT available
        with patch("chunk_downloader.detect_aria2c", return_value=None):
            opts_no_aria = get_turbo_downloader_opts(concurrency=8)
            self.assertEqual(opts_no_aria["concurrent_fragment_downloads"], 8)
            self.assertNotIn("external_downloader", opts_no_aria)
            self.assertNotIn("external_downloader_args", opts_no_aria)

    def test_detect_aria2c_path_resolution(self):
        """Test detect_aria2c checking Termux, shutil.which, and local bin."""
        # 1. System PATH hit
        with patch("os.path.exists", return_value=False), \
             patch("shutil.which", return_value="/usr/local/bin/aria2c"):
            self.assertEqual(detect_aria2c(), "/usr/local/bin/aria2c")

        # 2. None found
        with patch("os.path.exists", return_value=False), \
             patch("shutil.which", return_value=None):
            self.assertIsNone(detect_aria2c())

    async def test_segmented_download_with_mock_transport(self):
        """
        Verify end-to-end multi-stream parallel chunk downloading using
        httpx.MockTransport to simulate a range-supporting web server.
        """
        total_size = 4 * 1024 * 1024  # 4MB
        dummy_content = os.urandom(total_size)
        original_hash = hashlib.sha256(dummy_content).hexdigest()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "HEAD":
                return httpx.Response(
                    200,
                    headers={
                        "Content-Length": str(total_size),
                        "Accept-Ranges": "bytes",
                    },
                )
            elif request.method == "GET":
                range_header = request.headers.get("Range")
                if range_header and range_header.startswith("bytes="):
                    rng = range_header[len("bytes=") :]
                    parts = rng.split("-")
                    start = int(parts[0])
                    end = int(parts[1]) if parts[1] else total_size - 1
                    chunk = dummy_content[start : end + 1]
                    return httpx.Response(
                        206,
                        content=chunk,
                        headers={
                            "Content-Range": f"bytes {start}-{end}/{total_size}",
                            "Content-Length": str(len(chunk)),
                        },
                    )
                return httpx.Response(200, content=dummy_content)
            return httpx.Response(405)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as mock_client:
            downloader = SegmentedChunkDownloader(
                concurrency=4,
                min_chunk_size=1024 * 1024,  # 1MB
            )
            progress_events = []

            def on_progress(downloaded, total):
                progress_events.append((downloaded, total))

            dest = Path(self.temp_dir) / "mock_download.bin"
            result_path = await downloader.download(
                url="https://mock.cdn.test/video.mp4",
                destination_file=dest,
                progress_callback=on_progress,
                client=mock_client,
            )

            self.assertTrue(result_path.exists())
            self.assertEqual(result_path.stat().st_size, total_size)
            with open(result_path, "rb") as f:
                assembled_hash = hashlib.sha256(f.read()).hexdigest()
            self.assertEqual(assembled_hash, original_hash)
            self.assertTrue(len(progress_events) > 0)
            self.assertEqual(progress_events[-1][0], total_size)


if __name__ == "__main__":
    unittest.main()
