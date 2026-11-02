import time
from collections import OrderedDict, deque
from threading import Lock

from app.errors import AppError


class LoginRateLimiter:
    """Per-process, per-peer failed-login limit for the initial single API worker.

    No forwarded headers are trusted. State resets on service restart; this does
    not claim distributed protection or replace the authenticated HTTPS ingress.
    """

    def __init__(self, attempts: int = 10, window_seconds: int = 300, max_peers: int = 2048):
        self.attempts, self.window_seconds, self.max_peers = attempts, window_seconds, max_peers
        self.failures: OrderedDict[str, deque[float]] = OrderedDict()
        self.lock = Lock()

    def check(self, peer: str) -> None:
        with self.lock:
            failures = self.failures.get(peer)
            if failures is None:
                return
            now = time.monotonic()
            while failures and failures[0] <= now - self.window_seconds:
                failures.popleft()
            if len(failures) >= self.attempts:
                raise AppError(429, "login_rate_limited", "登录失败次数过多，请稍后重试")

    def failed(self, peer: str) -> None:
        with self.lock:
            if peer not in self.failures:
                if len(self.failures) >= self.max_peers:
                    self.failures.popitem(last=False)
                self.failures[peer] = deque(maxlen=self.attempts)
            self.failures[peer].append(time.monotonic())
            self.failures.move_to_end(peer)

    def succeeded(self, peer: str) -> None:
        with self.lock:
            self.failures.pop(peer, None)
