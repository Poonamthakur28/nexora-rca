"""Order storage behind a small database connection pool. Contains the planted bug `db_pool_leak`."""

import threading
import time

from .bugs import is_enabled

POOL_SIZE = 5
ACQUIRE_TIMEOUT_SECONDS = 2.0


class PoolTimeoutError(Exception):
    pass


class ConnectionPool:
    def __init__(self, size: int):
        self.size = size
        self._sem = threading.BoundedSemaphore(size)
        self._lock = threading.Lock()
        self.in_use = 0

    def acquire(self, timeout: float = ACQUIRE_TIMEOUT_SECONDS) -> int:
        if not self._sem.acquire(timeout=timeout):
            raise PoolTimeoutError(
                f"QueuePool limit of size {self.size} overflow 0 reached, connection timed out, timeout {timeout:.2f}"
            )
        with self._lock:
            self.in_use += 1
            return self.in_use

    def release(self) -> None:
        with self._lock:
            if self.in_use == 0:
                return
            self.in_use -= 1
        self._sem.release()

    def reset(self) -> None:
        """Returns every leaked connection (what a restart would do)."""
        while self.in_use:
            self.release()


POOL = ConnectionPool(POOL_SIZE)


def save_order(order: dict) -> str:
    POOL.acquire()
    time.sleep(0.01)  # write the order
    if not is_enabled("db_pool_leak"):
        POOL.release()
    # Planted bug: with `db_pool_leak` enabled the connection above is never released.
    return f"order-{int(time.time() * 1000)}"
