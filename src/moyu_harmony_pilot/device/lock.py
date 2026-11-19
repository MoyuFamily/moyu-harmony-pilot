"""Exclusive per-device file + thread lock to serialize HarmonyOS `uitest` calls."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

from moyu_harmony_pilot.device.errors import DeviceBusyError

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

_IN_PROCESS_GUARD = threading.Lock()
_IN_PROCESS_LOCKS: dict[str, threading.Lock] = {}


def _device_lock_key(device_serial: str) -> str:
    return hashlib.sha256(device_serial.encode("utf-8")).hexdigest()[:16]


def _get_thread_lock(lock_key: str) -> threading.Lock:
    with _IN_PROCESS_GUARD:
        lock = _IN_PROCESS_LOCKS.get(lock_key)
        if lock is None:
            lock = threading.Lock()
            _IN_PROCESS_LOCKS[lock_key] = lock
        return lock


def ensure_secure_dir(path: Path) -> Path:
    """Create directory if needed and tighten permissions to `0700` on POSIX."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o700)
    return path


def lock_file_path(device_serial: str, lock_dir: Path) -> Path:
    """Return a privacy-safe lock file path hashed from the raw device serial."""
    return lock_dir / f"device_{_device_lock_key(device_serial)}.lock"


def is_device_busy(device_serial: str, lock_dir: str | Path) -> bool:
    """Non-blocking probe indicating whether another holder currently locks this device."""
    directory = Path(lock_dir)
    if not directory.is_dir():
        return False

    key = _device_lock_key(device_serial)
    thread_lock = _get_thread_lock(key)
    if not thread_lock.acquire(blocking=False):
        return True
    try:
        path = lock_file_path(device_serial, directory)
        if not path.exists():
            return False
        if fcntl is None:
            return False
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
                return False
        finally:
            os.close(fd)
    finally:
        thread_lock.release()


class DeviceLock:
    """Exclusive cross-process and cross-thread lock scoped to one physical device."""

    def __init__(
        self,
        device_serial: str,
        *,
        alias: str,
        lock_dir: str | Path,
        wait_timeout: float = 0.0,
        poll_interval: float = 0.05,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._serial = device_serial
        self._alias = alias
        self._lock_dir = Path(lock_dir)
        self._wait_timeout = max(0.0, float(wait_timeout))
        self._poll_interval = max(0.005, float(poll_interval))
        self._clock = clock
        self._sleeper = sleeper
        self._lock_key = _device_lock_key(device_serial)
        self._thread_lock = _get_thread_lock(self._lock_key)
        self._fd: int | None = None
        self._acquired = False

    @property
    def path(self) -> Path:
        return lock_file_path(self._serial, self._lock_dir)

    def acquire(self) -> DeviceLock:
        """Acquire exclusive lock or raise `DeviceBusyError`."""
        ensure_secure_dir(self._lock_dir)
        deadline = self._clock() + self._wait_timeout

        while True:
            if self._try_acquire_once():
                return self
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise DeviceBusyError(
                    f"Device alias {self._alias!r} is currently locked by another active operation."
                )
            self._sleeper(min(self._poll_interval, remaining))

    def _try_acquire_once(self) -> bool:
        if not self._thread_lock.acquire(blocking=False):
            return False

        fd: int | None = None
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            if os.name == "posix":
                os.chmod(self.path, 0o600)
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    self._thread_lock.release()
                    return False

            # Write non-sensitive diagnostic ownership info (PID + alias, never raw serial).
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"pid={os.getpid()} alias={self._alias}\n".encode())
            self._fd = fd
            self._acquired = True
            return True
        except Exception:
            if fd is not None:
                os.close(fd)
            self._thread_lock.release()
            raise

    def release(self) -> None:
        """Release exclusive lock safely."""
        if not self._acquired:
            return
        try:
            if self._fd is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(self._fd, fcntl.LOCK_UN)
                finally:
                    os.close(self._fd)
                    self._fd = None
        finally:
            self._acquired = False
            self._thread_lock.release()

    def __enter__(self) -> DeviceLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
