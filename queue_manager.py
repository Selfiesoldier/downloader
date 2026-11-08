"""
Single-Worker FIFO Queue & Process Supervisor for Low-Resource Environments (Termux/Mobile).

Features:
- DownloadJob: Dataclass tracking lifecycle, cancellation, and metadata.
- DownloadQueueManager: Strict single-worker (max_concurrency=1) FIFO queue.
- Interactive Cancellation: SIGTERM/SIGKILL process supervision and immediate temp_dir purge.
"""

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("QueueManager")


@dataclass
class DownloadJob:
    """Represents a download job in the queue."""
    job_id: str
    chat_id: int = 0
    user_id: int = 0
    message_id: int = 0
    url: str = ""
    mode: str = ""
    title: str = ""
    status: str = "queued"  # "queued", "active", "cancelled", "completed", "failed"
    temp_dir: str = ""
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    process: Any = None
    created_at: float = field(default_factory=time.time)
    action: Optional[Callable[["DownloadJob"], Any]] = None
    task: Optional[asyncio.Task] = None
    extra: Dict[str, Any] = field(default_factory=dict)


class DownloadQueueManager:
    """
    Singleton FIFO Queue Manager enforcing strict single-worker concurrency
    to protect RAM (< 150MB) and disk I/O on Android Termux devices.
    """
    _instance: Optional["DownloadQueueManager"] = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, max_concurrency: int = 1):
        if getattr(self, "_initialized", False):
            return
        self.max_concurrency: int = max_concurrency
        self._queue: asyncio.Queue[DownloadJob] = asyncio.Queue()
        self._active_jobs: Dict[str, DownloadJob] = {}
        self._waiting_jobs: List[DownloadJob] = []
        self._all_jobs: Dict[str, DownloadJob] = {}
        self._worker_task: Optional[asyncio.Task] = None
        self._stop_event: asyncio.Event = asyncio.Event()
        self._default_handler: Optional[Callable[[DownloadJob], Any]] = None
        self._initialized: bool = True

    @classmethod
    def get_instance(cls) -> "DownloadQueueManager":
        """Returns the singleton queue manager instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Resets the singleton instance and cancels running workers (for test isolation)."""
        if cls._instance is not None:
            if cls._instance._worker_task and not cls._instance._worker_task.done():
                cls._instance._worker_task.cancel()
            cls._instance._initialized = False
        cls._instance = None

    @property
    def active_count(self) -> int:
        """Number of currently active download jobs."""
        return len(self._active_jobs)

    @property
    def waiting_count(self) -> int:
        """Number of download jobs waiting in the queue."""
        return len(self._waiting_jobs)

    @property
    def total_count(self) -> int:
        """Total number of active and waiting jobs."""
        return self.active_count + self.waiting_count

    def is_busy(self) -> bool:
        """Returns True if the worker is currently executing a job."""
        return self.active_count >= self.max_concurrency

    def get_job(self, job_id: str) -> Optional[DownloadJob]:
        """Retrieves a job by its unique ID."""
        return self._all_jobs.get(job_id)

    def set_default_handler(self, handler: Callable[[DownloadJob], Any]) -> None:
        """Sets the default job handler when job.action is not specified."""
        self._default_handler = handler

    def start_worker(self) -> None:
        """Starts the background worker loop if not already running."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        if self._worker_task is None or self._worker_task.done():
            self._stop_event.clear()
            self._worker_task = loop.create_task(self._worker_loop())
            logger.info("DownloadQueueManager worker task started.")

    async def stop_worker(self) -> None:
        """Gracefully stops the worker loop."""
        self._stop_event.set()
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        self._worker_task = None
        logger.info("DownloadQueueManager worker task stopped.")

    def get_queue_position(self, job_id: str) -> int:
        """
        Computes dynamic 1-based queue position for waiting jobs (#1, #2, etc.).
        Returns 0 if the job is currently active.
        Returns -1 if the job is not found, cancelled, or completed.
        """
        for idx, job in enumerate(self._waiting_jobs):
            if job.job_id == job_id:
                return idx + 1
        if job_id in self._active_jobs:
            return 0
        return -1

    def register_process(self, job_id: str, proc: Any) -> None:
        """Links an active child subprocess / pid to the download job."""
        job = self._all_jobs.get(job_id)
        if job:
            job.process = proc

    async def enqueue(self, job: DownloadJob) -> int:
        """
        Submits a job to the FIFO queue.
        Returns the initial dynamic queue position (#1, #2, ... or 0 if immediate).
        """
        self._all_jobs[job.job_id] = job
        self._waiting_jobs.append(job)
        await self._queue.put(job)
        self.start_worker()
        return self.get_queue_position(job.job_id)

    async def cancel_job(self, job_id: str, grace_period: float = 2.0) -> bool:
        """
        Cancels a queued or active job:
        - Queued: Marks as cancelled, removes from waiting list, and purges temp dir.
        - Active: Sets cancel_event, terminates child process (SIGTERM), waits up to grace_period,
          escalates to SIGKILL if still alive, cancels async task, and purges temp dir.
        Returns True if the job was cancelled, False if not found or already completed.
        """
        job = self._all_jobs.get(job_id)
        if not job:
            logger.warning(f"Cannot cancel unknown job ID: {job_id}")
            return False

        if job.status in ("completed", "failed"):
            logger.info(f"Job {job_id} is already '{job.status}', cancellation skipped.")
            return False

        logger.info(f"Cancelling job {job_id} (current status: '{job.status}')")
        job.status = "cancelled"
        job.cancel_event.set()

        # If job is waiting in queue:
        if job in self._waiting_jobs:
            self._waiting_jobs.remove(job)
            if job.temp_dir and os.path.exists(job.temp_dir):
                shutil.rmtree(job.temp_dir, ignore_errors=True)
                job.temp_dir = ""
            return True

        # If job is currently active:
        proc = job.process
        if proc:
            try:
                if hasattr(proc, "terminate"):
                    proc.terminate()
                elif hasattr(proc, "kill"):
                    proc.kill()
            except Exception as err:
                logger.warning(f"Error terminating process for job {job_id}: {err}")

            poll_fn = getattr(proc, "poll", None)
            kill_fn = getattr(proc, "kill", None)

            if kill_fn:
                elapsed = 0.0
                step = 0.05
                while elapsed < grace_period:
                    if callable(poll_fn):
                        if poll_fn() is not None:
                            break
                    await asyncio.sleep(step)
                    elapsed += step
                else:
                    # Escalate to kill
                    try:
                        kill_fn()
                    except Exception as err:
                        logger.warning(f"Error killing process for job {job_id}: {err}")

        # Cancel async task if active
        if job.task and not job.task.done():
            job.task.cancel()

        # Clean up temp_dir immediately to free disk space
        if job.temp_dir and os.path.exists(job.temp_dir):
            shutil.rmtree(job.temp_dir, ignore_errors=True)
            job.temp_dir = ""

        return True

    async def cancel_user_jobs(self, user_id: int) -> List[str]:
        """Cancels all active or queued jobs for a specific user ID."""
        matching_jobs = [
            j for j in self._all_jobs.values()
            if j.user_id == user_id and j.status in ("queued", "active")
        ]
        cancelled_ids = []
        for j in matching_jobs:
            if await self.cancel_job(j.job_id):
                cancelled_ids.append(j.job_id)
        return cancelled_ids

    async def _worker_loop(self) -> None:
        """Main single-worker FIFO loop executing exactly 1 job at a time."""
        logger.info("DownloadQueueManager worker loop started.")
        while not self._stop_event.is_set():
            try:
                try:
                    job = await asyncio.wait_for(self._queue.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue

                # Skip job if cancelled while in queue
                if job.cancel_event.is_set() or job.status == "cancelled":
                    logger.info(f"Skipping cancelled job from queue: {job.job_id}")
                    if job in self._waiting_jobs:
                        self._waiting_jobs.remove(job)
                    if job.temp_dir and os.path.exists(job.temp_dir):
                        shutil.rmtree(job.temp_dir, ignore_errors=True)
                        job.temp_dir = ""
                    self._queue.task_done()
                    continue

                # Transition from waiting to active
                if job in self._waiting_jobs:
                    self._waiting_jobs.remove(job)

                job.status = "active"
                self._active_jobs[job.job_id] = job
                logger.info(f"Job {job.job_id} activated (remaining waiting: {len(self._waiting_jobs)})")

                try:
                    if job.cancel_event.is_set():
                        job.status = "cancelled"
                    else:
                        if job.action:
                            if asyncio.iscoroutinefunction(job.action):
                                task = asyncio.create_task(job.action(job))
                                job.task = task
                                await task
                            else:
                                await asyncio.to_thread(job.action, job)
                        elif self._default_handler:
                            if asyncio.iscoroutinefunction(self._default_handler):
                                task = asyncio.create_task(self._default_handler(job))
                                job.task = task
                                await task
                            else:
                                await asyncio.to_thread(self._default_handler, job)

                        if job.status == "active":
                            job.status = "completed"
                except asyncio.CancelledError:
                    job.status = "cancelled"
                    logger.info(f"Job {job.job_id} task was cancelled.")
                except Exception as exc:
                    if job.cancel_event.is_set():
                        job.status = "cancelled"
                    else:
                        logger.error(f"Job {job.job_id} failed with error: {exc}", exc_info=True)
                        job.status = "failed"
                finally:
                    if job.status == "cancelled" and job.temp_dir and os.path.exists(job.temp_dir):
                        shutil.rmtree(job.temp_dir, ignore_errors=True)
                        job.temp_dir = ""
                    self._active_jobs.pop(job.job_id, None)
                    self._queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Unexpected error in DownloadQueueManager worker loop: {e}", exc_info=True)
                await asyncio.sleep(0.1)

        logger.info("DownloadQueueManager worker loop exited.")
