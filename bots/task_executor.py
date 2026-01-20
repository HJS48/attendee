"""
Task Executor - ThreadPoolExecutor-based task queue for Kubernetes mode.

Replaces Celery for async task execution when running in Kubernetes,
where a separate worker container is not available.

Usage:
    from bots.task_executor import task_executor

    # Submit a task immediately
    task_executor.submit(my_sync_function, arg1, arg2)

    # Submit with delay (countdown in seconds)
    task_executor.submit_delayed(my_sync_function, countdown=60, args=(arg1,))

    # Celery-compatible wrappers for existing code:
    task_executor.delay(my_sync_function, arg1, arg2)
    task_executor.apply_async(my_sync_function, args=(arg1,), countdown=60)
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from typing import Any, Callable, Optional, Tuple

logger = logging.getLogger(__name__)


class TaskExecutor:
    """
    Singleton ThreadPoolExecutor-based task executor.
    Provides Celery-compatible .delay() and .apply_async() methods.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, max_workers: int = 10):
        if self._initialized:
            return

        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="task_executor")
        self._delayed_tasks = []
        self._delay_thread = None
        self._shutdown = False
        self._initialized = True

        # Start the delay processor thread
        self._start_delay_processor()

        logger.info(f"TaskExecutor initialized with {max_workers} workers")

    def _start_delay_processor(self):
        """Start background thread to process delayed tasks."""
        def process_delayed():
            while not self._shutdown:
                now = time.time()
                tasks_to_run = []

                with self._lock:
                    remaining = []
                    for scheduled_time, func, args, kwargs in self._delayed_tasks:
                        if now >= scheduled_time:
                            tasks_to_run.append((func, args, kwargs))
                        else:
                            remaining.append((scheduled_time, func, args, kwargs))
                    self._delayed_tasks = remaining

                for func, args, kwargs in tasks_to_run:
                    self.submit(func, *args, **kwargs)

                time.sleep(1)  # Check every second

        self._delay_thread = threading.Thread(target=process_delayed, daemon=True, name="task_executor_delay")
        self._delay_thread.start()

    def submit(self, func: Callable, *args, **kwargs) -> None:
        """Submit a task for immediate execution."""
        def wrapped_task():
            try:
                logger.debug(f"Executing task: {func.__name__}")
                func(*args, **kwargs)
                logger.debug(f"Task completed: {func.__name__}")
            except Exception as e:
                logger.exception(f"Task failed: {func.__name__}: {e}")

        self._executor.submit(wrapped_task)

    def submit_delayed(self, func: Callable, countdown: int, args: Tuple = (), kwargs: dict = None) -> None:
        """Submit a task to be executed after countdown seconds."""
        if kwargs is None:
            kwargs = {}

        scheduled_time = time.time() + countdown
        with self._lock:
            self._delayed_tasks.append((scheduled_time, func, args, kwargs))

        logger.debug(f"Scheduled task {func.__name__} to run in {countdown} seconds")

    def delay(self, func: Callable, *args, **kwargs) -> None:
        """Celery-compatible .delay() method for immediate execution."""
        self.submit(func, *args, **kwargs)

    def apply_async(self, func: Callable, args: Tuple = (), kwargs: dict = None, countdown: int = 0) -> None:
        """Celery-compatible .apply_async() method with optional countdown."""
        if kwargs is None:
            kwargs = {}

        if countdown > 0:
            self.submit_delayed(func, countdown=countdown, args=args, kwargs=kwargs)
        else:
            self.submit(func, *args, **kwargs)

    def shutdown(self, wait: bool = True):
        """Shutdown the executor."""
        self._shutdown = True
        self._executor.shutdown(wait=wait)
        logger.info("TaskExecutor shutdown complete")


# Singleton instance
task_executor = TaskExecutor()


def is_kubernetes_mode() -> bool:
    """Check if running in Kubernetes mode (no Celery worker available)."""
    from django.conf import settings
    return getattr(settings, 'LAUNCH_BOT_METHOD', 'docker') == 'kubernetes'


class TaskWrapper:
    """
    Wrapper that provides Celery-compatible interface for a sync function.
    Automatically routes to ThreadPoolExecutor in K8s mode, or Celery otherwise.
    """

    def __init__(self, func: Callable, celery_task: Optional[Any] = None):
        self.func = func
        self.celery_task = celery_task
        self.__name__ = func.__name__
        self.__doc__ = func.__doc__

    def __call__(self, *args, **kwargs):
        """Direct synchronous call."""
        return self.func(*args, **kwargs)

    def delay(self, *args, **kwargs):
        """
        Celery-compatible .delay() method.
        Routes to TaskExecutor in K8s mode, Celery otherwise.
        """
        if is_kubernetes_mode():
            task_executor.delay(self.func, *args, **kwargs)
        elif self.celery_task:
            self.celery_task.delay(*args, **kwargs)
        else:
            # Fallback to direct execution if no Celery task
            task_executor.delay(self.func, *args, **kwargs)

    def apply_async(self, args: Tuple = (), kwargs: dict = None, countdown: int = 0):
        """
        Celery-compatible .apply_async() method.
        Routes to TaskExecutor in K8s mode, Celery otherwise.
        """
        if kwargs is None:
            kwargs = {}

        if is_kubernetes_mode():
            task_executor.apply_async(self.func, args=args, kwargs=kwargs, countdown=countdown)
        elif self.celery_task:
            self.celery_task.apply_async(args=args, kwargs=kwargs, countdown=countdown)
        else:
            task_executor.apply_async(self.func, args=args, kwargs=kwargs, countdown=countdown)
