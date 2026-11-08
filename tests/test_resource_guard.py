import asyncio
import io
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from resource_guard import (
    DiskHeadroomGuard,
    OrphanCleaner,
    SmartQualityBadges,
    check_disk_headroom,
    clean_orphaned_temp_dirs,
    format_quality_badge,
)
from fast_telethon import (
    MAX_CHUNK_SIZE,
    MTProtoConnectionPool,
    PipelinedStreamUploader,
    fast_upload_file,
    pipelined_upload_stream,
)
from telethon.tl.functions.upload import SaveBigFilePartRequest
from telethon.tl.types import InputFileBig


class TestResourceGuard(unittest.TestCase):
    """Unit tests for DiskHeadroomGuard, OrphanCleaner, and SmartQualityBadges."""

    def test_disk_headroom_insufficient(self):
        """Mock shutil.disk_usage to return 200MB free space. Verify check_disk_headroom returns (False, msg)."""
        mock_usage = MagicMock(total=10 * 1024**3, used=9.8 * 1024**3, free=200 * 1024 * 1024)
        with patch("shutil.disk_usage", return_value=mock_usage):
            ok, msg = check_disk_headroom(target_dir=".", estimated_bytes=0, min_headroom_mb=500)
            self.assertFalse(ok)
            self.assertIn("Insufficient disk space", msg)
            self.assertIn("200.0 MB free", msg)
            self.assertIn("free up device storage", msg.lower())

    def test_disk_headroom_insufficient_with_large_estimate(self):
        """Verify check_disk_headroom rejects when free space is under 1.5x expected download size."""
        # 600 MB free, but 500 MB download requires max(500MB, 1.5 * 500MB = 750MB)
        mock_usage = MagicMock(total=10 * 1024**3, used=9.4 * 1024**3, free=600 * 1024 * 1024)
        with patch("shutil.disk_usage", return_value=mock_usage):
            ok, msg = check_disk_headroom(target_dir=".", estimated_bytes=500 * 1024 * 1024, min_headroom_mb=500)
            self.assertFalse(ok)
            self.assertIn("750.0 MB required", msg)

    def test_disk_headroom_sufficient(self):
        """Mock shutil.disk_usage to return 2GB free space. Verify check_disk_headroom returns (True, '')."""
        mock_usage = MagicMock(total=10 * 1024**3, used=8 * 1024**3, free=2 * 1024 * 1024 * 1024)
        with patch("shutil.disk_usage", return_value=mock_usage):
            ok, msg = check_disk_headroom(target_dir=".", estimated_bytes=100 * 1024 * 1024, min_headroom_mb=500)
            self.assertTrue(ok)
            self.assertEqual(msg, "")

    def test_orphan_cleaner_sweeps_temp_dirs(self):
        """Verify clean_orphaned_temp_dirs purges stale dl_* folders and .part files, keeping active/unrelated files."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)

            # 1. Stale dl_* directory (2 hours old)
            dl_old = base / "dl_old_123"
            dl_old.mkdir()
            (dl_old / "unfinished.mp4").write_bytes(b"temp")

            # 2. Recent dl_* directory (10 seconds old)
            dl_recent = base / "dl_recent_456"
            dl_recent.mkdir()
            (dl_recent / "active.mp4").write_bytes(b"active")

            # 3. Stray stale .part file
            stale_part = base / "video.mp4.part"
            stale_part.write_bytes(b"part data")

            # 4. Stray recent .part file
            recent_part = base / "downloading.mp4.part"
            recent_part.write_bytes(b"current part data")

            # 5. Stale .ytdl file
            stale_ytdl = base / "audio.m4a.ytdl"
            stale_ytdl.write_bytes(b"ytdl metadata")

            # 6. Unrelated project file
            unrelated = base / "keep_me.txt"
            unrelated.write_text("critical project file")

            # Adjust timestamps
            now = time.time()
            old_time = now - 7200
            recent_time = now - 10

            os.utime(str(dl_old), (old_time, old_time))
            os.utime(str(dl_old / "unfinished.mp4"), (old_time, old_time))
            os.utime(str(dl_recent), (recent_time, recent_time))
            os.utime(str(stale_part), (old_time, old_time))
            os.utime(str(recent_part), (recent_time, recent_time))
            os.utime(str(stale_ytdl), (old_time, old_time))
            os.utime(str(unrelated), (old_time, old_time))

            # Execute cleanup with 1 hour threshold (3600s), system temp scanning disabled
            purged = clean_orphaned_temp_dirs(base_dir=str(base), max_age_seconds=3600, scan_system_temp=False)

            self.assertFalse(dl_old.exists(), "dl_old should be removed")
            self.assertFalse(stale_part.exists(), "stale_part should be removed")
            self.assertFalse(stale_ytdl.exists(), "stale_ytdl should be removed")

            self.assertTrue(dl_recent.exists(), "dl_recent must not be removed")
            self.assertTrue(recent_part.exists(), "recent_part must not be removed")
            self.assertTrue(unrelated.exists(), "unrelated file must not be removed")
            self.assertIn(str(dl_old), purged)

    def test_orphan_cleaner_protects_critical_files(self):
        """Verify cookies/, .env, and .session files are never deleted under any sweep condition."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)

            cookies_dir = base / "cookies"
            cookies_dir.mkdir()
            cookie_file = cookies_dir / "youtube_cookies.txt"
            cookie_file.write_text("netscape cookie data")

            env_file = base / ".env"
            env_file.write_text("API_ID=12345")

            session_file = base / "bot_mtproto.session"
            session_file.write_bytes(b"sqlite mtproto session data")

            session_journal = base / "bot_mtproto.session-journal"
            session_journal.write_bytes(b"journal")

            py_file = base / "bot.py"
            py_file.write_text("# main bot entrypoint")

            # A legitimate stale dl_* folder that SHOULD be deleted
            dl_stale = base / "dl_stale_target"
            dl_stale.mkdir()
            (dl_stale / "chunk.bin").write_bytes(b"test")

            # Set old mtime to everything
            old_time = time.time() - 100000
            for item in [cookie_file, env_file, session_file, session_journal, py_file, dl_stale]:
                os.utime(str(item), (old_time, old_time))

            # Run aggressive sweep (max_age_seconds=0 sweeps all eligible candidates)
            purged = clean_orphaned_temp_dirs(base_dir=str(base), max_age_seconds=0, scan_system_temp=False)

            # Protected assets must survive
            self.assertTrue(cookie_file.exists(), "Cookie file must be preserved!")
            self.assertTrue(env_file.exists(), ".env file must be preserved!")
            self.assertTrue(session_file.exists(), ".session file must be preserved!")
            self.assertTrue(session_journal.exists(), ".session-journal file must be preserved!")
            self.assertTrue(py_file.exists(), "Python source code must be preserved!")

            # Target directory must be cleaned
            self.assertFalse(dl_stale.exists(), "dl_stale folder should be purged")
            self.assertIn(str(dl_stale), purged)

    def test_smart_quality_badges(self):
        """Verify format_quality_badge output conforms to expected bandwidth-aware labels."""
        self.assertEqual(format_quality_badge(720), "⚡ Fast 720p (~30MB)")
        self.assertEqual(format_quality_badge(1080), "🎬 Full 1080p (~150MB)")
        self.assertEqual(format_quality_badge(480), "💾 Data Saver 480p (~15MB)")
        self.assertEqual(format_quality_badge(360), "💾 Data Saver 360p (~15MB)")
        self.assertEqual(format_quality_badge(0, is_audio=True), "🎵 Audio (MP3) (~8MB)")

        # With explicit file size
        badge_720_sized = format_quality_badge(720, file_size_bytes=50 * 1024 * 1024)
        self.assertEqual(badge_720_sized, "⚡ Fast 720p (~50MB)")

        badge_audio_sized = format_quality_badge(0, file_size_bytes=12 * 1024 * 1024, is_audio=True)
        self.assertEqual(badge_audio_sized, "🎵 Audio (MP3) (~12MB)")


class TestPipelinedUpload(unittest.IsolatedAsyncioTestCase):
    """Unit tests for PipelinedStreamUploader and MTProto connection pooling."""

    async def test_pipelined_upload_chunking(self):
        """
        Mock a 1.2MB byte stream. Verify PipelinedStreamUploader segments stream into
        two 512KB chunks and one 200KB chunk, dispatching SaveBigFilePartRequest for
        each part sequentially.
        """
        chunk_1_size = 512 * 1024
        chunk_2_size = 512 * 1024
        chunk_3_size = 200 * 1024
        total_bytes = chunk_1_size + chunk_2_size + chunk_3_size  # 1,253,376 bytes (~1.2MB)

        payload = (b"A" * chunk_1_size) + (b"B" * chunk_2_size) + (b"C" * chunk_3_size)
        stream = io.BytesIO(payload)

        mock_client = AsyncMock()
        mock_client.return_value = True

        progress_calls = []

        def on_progress(current, total):
            progress_calls.append((current, total))

        uploader = PipelinedStreamUploader(
            client=mock_client,
            file_name="streamed_video.mp4",
            total_size=total_bytes,
            concurrency=2,
            progress_callback=on_progress,
        )

        result = await uploader.upload_stream(stream, total_size=total_bytes)

        # 1. Verify returned InputFileBig
        self.assertIsInstance(result, InputFileBig)
        self.assertEqual(result.name, "streamed_video.mp4")
        self.assertEqual(result.parts, 3)

        # 2. Verify SaveBigFilePartRequest calls
        self.assertEqual(mock_client.call_count, 3)
        dispatched_requests = [call.args[0] for call in mock_client.call_args_list]

        for req in dispatched_requests:
            self.assertIsInstance(req, SaveBigFilePartRequest)
            self.assertEqual(req.file_id, result.id)
            self.assertEqual(req.file_total_parts, 3)

        # Verify part indices and chunk sizes
        self.assertEqual(dispatched_requests[0].file_part, 0)
        self.assertEqual(len(dispatched_requests[0].bytes), chunk_1_size)
        self.assertEqual(dispatched_requests[0].bytes, b"A" * chunk_1_size)

        self.assertEqual(dispatched_requests[1].file_part, 1)
        self.assertEqual(len(dispatched_requests[1].bytes), chunk_2_size)
        self.assertEqual(dispatched_requests[1].bytes, b"B" * chunk_2_size)

        self.assertEqual(dispatched_requests[2].file_part, 2)
        self.assertEqual(len(dispatched_requests[2].bytes), chunk_3_size)
        self.assertEqual(dispatched_requests[2].bytes, b"C" * chunk_3_size)

        # 3. Verify progress updates
        self.assertTrue(len(progress_calls) >= 3)
        self.assertEqual(progress_calls[-1][0], total_bytes)
        self.assertEqual(progress_calls[-1][1], total_bytes)

    async def test_mtproto_connection_pool_round_robin(self):
        """Verify MTProtoConnectionPool distributes requests across pooled connections."""
        mock_client = AsyncMock()
        mock_client.return_value = True

        pool = MTProtoConnectionPool(client=mock_client, pool_size=4)
        await pool.initialize()

        req1 = SaveBigFilePartRequest(1, 0, 2, b"part0")
        req2 = SaveBigFilePartRequest(1, 1, 2, b"part1")

        await pool.send(req1)
        await pool.send(req2)

        self.assertEqual(mock_client.call_count, 2)
        await pool.close()

    async def test_fast_upload_file_integration(self):
        """Verify backwards-compatible fast_upload_file functions properly with temporary test file."""
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(b"0" * (600 * 1024))  # 600KB -> 2 parts (512KB + 88KB)
            tf_path = tf.name

        try:
            mock_client = AsyncMock()
            mock_client.return_value = True

            result = await fast_upload_file(mock_client, tf_path, concurrency=4)
            self.assertIsInstance(result, InputFileBig)
            self.assertEqual(result.parts, 2)
            self.assertEqual(mock_client.call_count, 2)
        finally:
            if os.path.exists(tf_path):
                os.remove(tf_path)


if __name__ == "__main__":
    unittest.main()
