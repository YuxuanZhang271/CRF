"""One ASR owner per user, shared between independent installs and Demo adapters."""
import json
import os
from pathlib import Path
from filelock import FileLock, Timeout
from platformdirs import user_cache_path


class ASRInstanceLease:
    def __init__(self, device, *, lock_path=None, compatibility_lock_file=None, **kwargs):
        """Record the per-user lease path and any compatible lock shared with an older deployment."""
        self.device = device
        self.path = Path(lock_path or Path(user_cache_path("StreamingCommand")) / "asr.lock")
        self._lock = None
        self.compatibility_lock_file = Path(compatibility_lock_file) if compatibility_lock_file else None
        self._compatibility_fd = None

    def acquire(self):
        """Acquire both required locks without waiting, rolling back the package lease if either fails."""
        if self._lock is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(self.path))
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise RuntimeError("An ASR instance is already active; close it before starting another") from exc
        self._lock = lock
        try:
            if self.compatibility_lock_file and self.compatibility_lock_file.resolve() != self.path.resolve():
                self._acquire_compatibility_lock()
        except BaseException:
            self.release()
            raise

    def _acquire_compatibility_lock(self):
        """Share a POSIX flock with an existing deployment without truncating its lock file."""
        if os.name != "posix":
            raise ValueError("ASR compatibility_lock_file requires POSIX flock")
        import fcntl

        fd = os.open(self.compatibility_lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise RuntimeError("An existing deployment owns the ASR lock") from exc
        except BaseException:
            os.close(fd)
            raise
        self._compatibility_fd = fd

    def release(self):
        """Drop the locks held by this instance while leaving the lock file in place."""
        if self._compatibility_fd is not None:
            os.close(self._compatibility_fd)
            self._compatibility_fd = None
        if self._lock is not None:
            self._lock.release()
            self._lock = None
