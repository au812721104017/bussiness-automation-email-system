import time
from collections import defaultdict, deque
from typing import Dict, Deque
from fastapi import Request, HTTPException

class RateLimiter:
    def __init__(self, per_minute: int = 60):
        self.per_minute = per_minute
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    def check(self, key: str):
        now = time.time()
        dq = self._hits[key]
        # Expire older than 60s
        while dq and dq[0] < now - 60:
            dq.popleft()
        if len(dq) >= self.per_minute:
            raise HTTPException(status_code=429, detail=f"Rate limit exceeded: {self.per_minute}/minute")
        dq.append(now)

# Global limiter (per-IP)
limiter = RateLimiter()

def get_rate_limiter():
    from app.core.config import get_settings
    s = get_settings()
    return RateLimiter(per_minute=s.rate_limit_per_minute)

# Dependency for FastAPI routes
async def rate_limit_dependency(request: Request):
    from app.core.config import get_settings
    s = get_settings()
    limiter = RateLimiter(per_minute=s.rate_limit_per_minute)
    key = request.client.host if request.client else "unknown"
    # Also include path to avoid global burst: per-IP per-path
    limiter.check(key + ":" + request.url.path)
