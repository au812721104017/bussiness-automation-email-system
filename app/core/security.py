import re
import html
import ipaddress
from typing import Optional, List
from urllib.parse import urlparse

# HTML sanitization — strip dangerous tags, keep safe text
DANGEROUS_TAGS = re.compile(r"<(script|style|iframe|object|embed|form|meta|link)[^>]*>.*?</\1>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
# Links: detect markdown and html links
URL_RE = re.compile(r"https?://[^\s<\"']+", re.I)
# Private IP ranges for SSRF
_PRIVATE_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]

def sanitize_html(text: Optional[str], max_len: int = 10000) -> str:
    if not text:
        return ""
    # Remove dangerous tags entirely
    cleaned = DANGEROUS_TAGS.sub("", text)
    # Strip remaining tags
    cleaned = TAG_RE.sub(" ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + " [truncated]"
    return cleaned

def extract_safe_links(text: str) -> List[str]:
    """Extract links but flag suspicious ones (e.g., javascript:, data:). Do not fetch."""
    links = URL_RE.findall(text or "")
    safe = []
    for u in links[:10]:
        low = u.lower()
        if low.startswith("javascript:") or low.startswith("data:") or low.startswith("file:"):
            continue
        safe.append(u[:500])
    return safe

def is_ssrf_url(url: str) -> bool:
    """Return True if url targets private/internal IP or localhost — block for SSRF."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            return True  # no host — suspicious
        if host.lower() in ("localhost", "metadata.google.internal"):
            return True
        # Try IP literal
        try:
            ip = ipaddress.ip_address(host)
            for net in _PRIVATE_NETS:
                if ip in net:
                    return True
        except ValueError:
            # hostname, not IP — check if resolves to private? For now only literal check; hostname SSRF via DNS rebinding out of scope for Phase 12 but we block localhost
            pass
        if host.endswith(".internal") or host.endswith(".local"):
            return True
        return False
    except Exception:
        return True

def validate_email_address(addr: str) -> bool:
    if not addr or "@" not in addr or len(addr) > 254:
        return False
    # Simple but strict
    return re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", addr.strip()) is not None

def redact_pii(text: str) -> str:
    """Redact emails and long bodies for logging — safe for audit logs."""
    if not text:
        return text
    # Redact emails: keep domain
    text = re.sub(r"([a-zA-Z0-9._%+\-]+)@([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})", r"***@\2", text)
    # Redact tokens
    text = re.sub(r"sk-[a-zA-Z0-9]{10,}", "sk-***", text)
    text = re.sub(r"ya29\.[a-zA-Z0-9_\-]+", "ya29.***", text)
    return text

def has_malicious_attachment(filename: Optional[str]) -> bool:
    if not filename:
        return False
    bad_exts = {".exe", ".bat", ".sh", ".js", ".vbs", ".scr", ".dll", ".com", ".pif"}
    low = filename.lower()
    return any(low.endswith(ext) for ext in bad_exts)
