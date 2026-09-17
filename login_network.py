"""로그인 시 일시적 네트워크 오류와 인증 오류를 구분한다."""
from __future__ import annotations

AUTOSTART_NET_DELAYS = (0, 3, 6, 12, 20)

AUTH_MARKERS = (
    "401",
    "403",
    "invalid_grant",
    "invalid credentials",
    "authentication failed",
    "unauthorized",
    "permission denied",
    "forbidden",
    "계정 정보가 틀립니다",
)

NET_MARKERS = (
    "timeout",
    "timed out",
    "temporarily unavailable",
    "connection",
    "network",
    "getaddrinfo",
    "dns",
    "10060",
    "10061",
    "10054",
    "unreachable",
    "reset",
    "ssl",
    "proxy",
    "name or service not known",
    "failed to resolve",
    "max retries exceeded",
    "winerror 10051",
    "winerror 10065",
)

NET_EXC_NAMES = (
    "ConnectionError",
    "ConnectTimeout",
    "ReadTimeout",
    "Timeout",
    "URLError",
    "TransportError",
    "SSLError",
    "gaierror",
    "OSError",
    "TimeoutError",
)


def is_auth_error(exc) -> bool:
    msg = str(exc or "").lower()
    return any(m in msg for m in AUTH_MARKERS)


def is_transient_network_error(exc) -> bool:
    if exc is None:
        return False
    if is_auth_error(exc):
        return False
    name = type(exc).__name__
    msg = str(exc).lower()
    if name in NET_EXC_NAMES:
        if "password" in msg or ("login" in msg and "smtp" in msg):
            return False
        return True
    return any(m in msg for m in NET_MARKERS)
