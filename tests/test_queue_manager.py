"""
Automated unit tests for Single-Worker FIFO Queue & Process Supervisor (queue_manager.py).

Covers:
1. FIFO queue order and dynamic position tracking (#1, #2, etc.).
2. Queued job cancellation and immediate temp directory purge.
3. Active job cancellation with child process SIGTERM/SIGKILL escalation and clean transition.
4. Strict single-worker concurrency limit (max_concurrency=1) under rapid bursts.
5. User-scoped batch cancellation (/cancel).
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from queue_manager import DownloadJob, DownloadQueueManager


class TestDownloadQueueManager(unittest.IsolatedAsyncioTestCase):
    """Test suite for DownloadQueueManager and DownloadJob."""

    async def asyncSetUp(self):
        """Ensure a clean singleton state for each test."""
        DownloadQueueManager.reset()
        self.qm = DownloadQueueManager()
        self.qm.start_worker()

    async def asyncTearDown(self):
        """Stop worker and reset singleton."""
        await self.qm.stop_worker()
        DownloadQueueManager.reset()

    async def test_fifo_queue_order(self):
        """
        Enqueue 3 dummy jobs. Verify job 1 runs first, while job 2 reports
        position #1 and job 3 reports position #2. Verify jobs execute in strict
        sequential order.
        """
        execution_order = []
        job1_running = asyncio.Event()
        job1_can_finish = asyncio.Event()

        async def job1_action(job: DownloadJob):
            execution_order.append(job.job_id)
            job1_running.set()
            await job1_can_finish.wait()

        async def job2_action(job: DownloadJob):
            execution_order.append(job.job_id)

        async def job3_action(job: DownloadJob):
            execution_order.append(job.job_id)

        job1 = DownloadJob(job_id="job_1", action=job1_action)
        job2 = DownloadJob(job_id="job_2", action=job2_action)
        job3 = DownloadJob(job_id="job_3", action=job3_action)

        # Enqueue job 1 and wait for it to become active
        await self.qm.enqueue(job1)
        await job1_running.wait()

        self.assertEqual(self.qm.get_queue_position("job_1"), 0)  # 0 indicates active

        # Enqueue job 2 and job 3 while job 1 is running
        await self.qm.enqueue(job2)
        await self.qm.enqueue(job3)

        # Verify dynamic positions in queue
        self.assertEqual(self.qm.get_queue_position("job_2"), 1)  # #1 in line
        self.assertEqual(self.qm.get_queue_position("job_3"), 2)  # #2 in line

        # Allow job 1 to finish
        job1_can_finish.set()

        # Wait for all jobs to complete
        for _ in range(50):
            if len(execution_order) == 3:
                break
            await asyncio.sleep(0.01)

        self.assertEqual(execution_order, ["job_1", "job_2", "job_3"])
        self.assertEqual(job1.status, "completed")
        self.assertEqual(job2.status, "completed")
        self.assertEqual(job3.status, "completed")

    async def test_queued_job_cancellation(self):
        """
        Enqueue job 1 (slow job) and job 2 (waiting). Cancel job 2 while job 1
        is active. Verify job 2 status is 'cancelled', its temp directory is deleted,
        and it is skipped when job 1 finishes.
        """
        job1_running = asyncio.Event()
        job1_can_finish = asyncio.Event()
        job2_executed = False

        async def slow_job_action(job: DownloadJob):
            job1_running.set()
            await job1_can_finish.wait()

        async def job2_action(job: DownloadJob):
            nonlocal job2_executed
            job2_executed = True

        temp_dir_2 = tempfile.mkdtemp(prefix="test_job2_")
        self.assertTrue(os.path.exists(temp_dir_2))

        job1 = DownloadJob(job_id="job_slow", action=slow_job_action)
        job2 = DownloadJob(job_id="job_waiting", temp_dir=temp_dir_2, action=job2_action)

        await self.qm.enqueue(job1)
        await job1_running.wait()

        await self.qm.enqueue(job2)
        self.assertEqual(self.qm.get_queue_position("job_waiting"), 1)

        # Cancel job 2 while job 1 is active
        cancelled = await self.qm.cancel_job("job_waiting")
        self.assertTrue(cancelled)

        # Verify job 2 status and temp directory deletion
        self.assertEqual(job2.status, "cancelled")
        self.assertTrue(job2.cancel_event.is_set())
        self.assertFalse(os.path.exists(temp_dir_2))

        # Let job 1 finish
        job1_can_finish.set()

        for _ in range(50):
            if self.qm.total_count == 0:
                break
            await asyncio.sleep(0.01)

        # Confirm job 2 was skipped
        self.assertFalse(job2_executed)
        self.assertEqual(job2.status, "cancelled")

    async def test_active_job_cancellation(self):
        """
        Start job 1 with a mock subprocess. Call cancel_job(job_1.job_id).
        Verify terminate() / kill() is invoked on the process, cancel_event is set,
        temp directory is removed, and worker cleanly transitions to next job.
        """
        temp_dir_1 = tempfile.mkdtemp(prefix="test_job1_")
        self.assertTrue(os.path.exists(temp_dir_1))

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Process alive

        job1_started = asyncio.Event()

        async def job1_action(job: DownloadJob):
            self.qm.register_process(job.job_id, mock_proc)
            job1_started.set()
            try:
                while not job.cancel_event.is_set():
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                pass

        job2_executed = False

        async def job2_action(job: DownloadJob):
            nonlocal job2_executed
            job2_executed = True

        job1 = DownloadJob(job_id="job_active", temp_dir=temp_dir_1, action=job1_action)
        job2 = DownloadJob(job_id="job_next", action=job2_action)

        await self.qm.enqueue(job1)
        await self.qm.enqueue(job2)

        await job1_started.wait()

        # Cancel active job 1
        cancelled = await self.qm.cancel_job("job_active", grace_period=0.05)
        self.assertTrue(cancelled)

        # Verify terminate() or kill() was called on child process
        self.assertTrue(mock_proc.terminate.called or mock_proc.kill.called)

        # Verify cancel_event is set and temp directory is purged
        self.assertTrue(job1.cancel_event.is_set())
        self.assertFalse(os.path.exists(temp_dir_1))

        # Verify worker cleanly transitions to next job (job 2)
        for _ in range(50):
            if job2_executed:
                break
            await asyncio.sleep(0.01)

        self.assertTrue(job2_executed)
        self.assertEqual(job2.status, "completed")

    async def test_single_concurrency_limit(self):
        """
        Enqueue multiple jobs rapidly and verify at no point does active_count exceed 1.
        """
        max_concurrency_seen = 0
        current_concurrency = 0
        completed_count = 0

        async def burst_action(job: DownloadJob):
            nonlocal max_concurrency_seen, current_concurrency, completed_count
            current_concurrency += 1
            max_concurrency_seen = max(max_concurrency_seen, current_concurrency)
            await asyncio.sleep(0.02)
            current_concurrency -= 1
            completed_count += 1

        total_jobs = 6
        for i in range(total_jobs):
            job = DownloadJob(job_id=f"job_burst_{i}", action=burst_action)
            await self.qm.enqueue(job)

        for _ in range(150):
            if completed_count == total_jobs:
                break
            await asyncio.sleep(0.02)

        self.assertEqual(completed_count, total_jobs)
        self.assertEqual(max_concurrency_seen, 1)
        self.assertEqual(self.qm.max_concurrency, 1)

    async def test_cancel_user_jobs(self):
        """
        Verify /cancel batch handler cancels only the requested user's jobs.
        """
        blocker = asyncio.Event()

        async def blocking_action(job: DownloadJob):
            await blocker.wait()

        user1_job1 = DownloadJob(job_id="u1_j1", user_id=101, action=blocking_action)
        user1_job2 = DownloadJob(job_id="u1_j2", user_id=101, action=blocking_action)
        user2_job = DownloadJob(job_id="u2_j1", user_id=202, action=blocking_action)

        await self.qm.enqueue(user1_job1)
        await self.qm.enqueue(user1_job2)
        await self.qm.enqueue(user2_job)

        cancelled_ids = await self.qm.cancel_user_jobs(101)

        self.assertIn("u1_j1", cancelled_ids)
        self.assertIn("u1_j2", cancelled_ids)
        self.assertNotIn("u2_j1", cancelled_ids)
        self.assertEqual(user2_job.status, "queued")

        blocker.set()


if __name__ == "__main__":
    unittest.main()
