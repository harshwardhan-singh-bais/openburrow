"""Share links: redact first, then sign. Never the other way round.

The ordering in that sentence is the whole design, and getting it backwards
produces one of two failures that both look like success:

**Sign-then-redact** means the signature covers bytes that are no longer in the
document. Verification fails against the redacted copy, so either the share link
breaks — or someone "fixes" it by verifying against the original, and now the
signature attests to content the recipient cannot see. A signature that vouches
for hidden content is worse than no signature, because it is trusted.

**Redact-then-sign** means the signature covers exactly what the recipient gets.
Anyone can verify that this is the document OpenBurrow produced, and nobody can
smuggle in a claim about content that was stripped.

So :func:`redact` runs to completion before :func:`sign` sees anything. The
function that builds a token takes the *redacted* payload, and there is no code
path that signs unredacted bytes.

The second thing worth stating: **a share link is not an access control system.**
It is a signed pointer with an expiry, which is a reasonable way to hand a reel to
a colleague and a bad way to protect a secret. A session reel contains real source
code and real prompts, so redaction is best-effort and the honest guidance is
"review before you share", which is why :func:`preview_redactions` exists — it
shows what will be stripped so a human can check before a link leaves the machine.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from openburrow.core.logging import get_logger

log = get_logger(__name__)

#: Default link lifetime. Short on purpose: a reel is a debugging artifact, and a
#: link that still works in six months is a link nobody remembers creating.
DEFAULT_TTL_HOURS = 168  # one week

#: Patterns scrubbed from a reel before it is signed.
#:
#: Each entry is ``(label, compiled_pattern, replacement)``. The replacements keep
#: the *shape* of what was removed — ``sk-…`` stays recognisable as an API key —
#: because a reader needs to know that a secret was there, and a redaction that
#: removes the fact entirely makes the reel misleading about what happened.
REDACTION_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "openai_key",
        re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
        "sk-«redacted»",
    ),
    (
        "anthropic_key",
        re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}\b"),
        "sk-ant-«redacted»",
    ),
    (
        "github_token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        "gh«redacted»",
    ),
    (
        "aws_key",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        "AKIA«redacted»",
    ),
    (
        "slack_token",
        re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
        "xox«redacted»",
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
        "«redacted-jwt»",
    ),
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        "«redacted-private-key»",
    ),
    (
        "connection_string",
        re.compile(r"\b(?:postgres|postgresql|mysql|mongodb|redis)(?:\+\w+)?://[^\s\"']+"),
        "«redacted-connection-string»",
    ),
    (
        "bearer_header",
        # The optional `(?:bearer\s+)?` matters: without it the pattern matches
        # "Authorization: Bearer" and leaves the token itself in the output, which
        # is a redaction that looks like it worked.
        re.compile(r"(?i)\b(?:authorization|bearer)\s*[:=]\s*(?:bearer\s+)?[^\s\"',]+"),
        "Authorization: «redacted»",
    ),
    (
        "assignment",
        re.compile(
            r"(?i)\b([A-Za-z0-9_]*(?:api[_-]?key|secret|token|password|passwd|credential)"
            r"[A-Za-z0-9_]*)\s*[:=]\s*[\"']?([^\s\"',]{6,})[\"']?"
        ),
        r"\1=«redacted»",
    ),
    (
        "email",
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        "«redacted-email»",
    ),
)

#: Home-directory paths are replaced with a stable placeholder rather than
#: removed, so file paths in a reel stay readable without leaking a username.
_PATH_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b[A-Za-z]:\\Users\\[^\\\s\"']+"), "«home»"),
    (re.compile(r"/(?:home|Users)/[^/\s\"']+"), "«home»"),
)


@dataclass
class RedactionReport:
    """What redaction changed, for the pre-share preview."""

    counts: dict[str, int] = field(default_factory=dict)
    samples: dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def render(self) -> str:
        if not self.counts:
            return "no redactions applied"
        lines = [f"{self.total} redaction(s):"]
        for label, count in sorted(self.counts.items(), key=lambda p: p[1], reverse=True):
            sample = self.samples.get(label, "")
            lines.append(f"  {label}: {count}" + (f"  e.g. {sample}" if sample else ""))
        return "\n".join(lines)

    def summary(self) -> dict[str, Any]:
        return {"total": self.total, "counts": dict(self.counts)}


#: Label → the replacement text used for it. Built from the rules so a sample can be
#: located in the redacted output without re-running redaction over its own output,
#: which recurses.
_REPLACEMENT_BY_LABEL: dict[str, str] = {
    label: replacement for label, _, replacement in REDACTION_RULES
} | {"home_path": "«home»"}


def redact(text: str) -> tuple[str, RedactionReport]:
    """Scrub credentials and identifying paths from ``text``.

    Returns the cleaned text and a report of what was changed. The report is the
    important half: a silent redaction gives a person no way to judge whether the
    result is safe to share, and "it looked fine" is not a security review.

    The report's samples are taken from the *redacted* output, never from the
    matches. A preview that shows the first 40 characters of the thing it is about
    to remove has just printed the secret, which is how a redaction feature becomes
    a leak vector.
    """
    report = RedactionReport()
    result = text

    for label, pattern, replacement in REDACTION_RULES:
        matches = pattern.findall(result)
        if not matches:
            continue
        result = pattern.sub(replacement, result)
        report.counts[label] = report.counts.get(label, 0) + len(matches)

    for pattern, replacement in _PATH_RULES:
        matches = pattern.findall(result)
        if not matches:
            continue
        result = pattern.sub(replacement, result)
        report.counts["home_path"] = report.counts.get("home_path", 0) + len(matches)

    for label in report.counts:
        report.samples[label] = _context(result, _REPLACEMENT_BY_LABEL.get(label, ""))

    return result, report


def redact_payload(payload: Any) -> tuple[Any, RedactionReport]:
    """Redact every string inside a nested structure.

    Recurses through dicts and lists so a caller cannot accidentally share a
    nested field by redacting only the top level — which is exactly the bug that
    makes a redaction feature dangerous, because it works on the test fixture and
    fails on the real document.
    """
    combined = RedactionReport()

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            cleaned, report = redact(value)
            for label, count in report.counts.items():
                combined.counts[label] = combined.counts.get(label, 0) + count
                combined.samples.setdefault(label, report.samples.get(label, ""))
            return cleaned
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return walk(payload), combined


def preview_redactions(payload: Any) -> RedactionReport:
    """Report what would be redacted, without producing the redacted copy."""
    _, report = redact_payload(payload)
    return report


# --------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ShareToken:
    """A signed, expiring pointer to a reel."""

    reel_id: str
    issued_at: datetime
    expires_at: datetime
    allowed_orgs: tuple[str, ...] = ()
    signature: str = ""

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at

    def payload(self) -> dict[str, Any]:
        """The exact bytes that are signed.

        Sorted keys and no whitespace, because the signature has to be stable
        across processes and Python versions. Serialising the same data two ways
        would produce two different signatures for one token.
        """
        return {
            "reel_id": self.reel_id,
            "iat": int(self.issued_at.timestamp()),
            "exp": int(self.expires_at.timestamp()),
            "orgs": sorted(self.allowed_orgs),
        }

    def encode(self) -> str:
        """``base64url(payload).base64url(signature)`` — compact and copy-pasteable."""
        body = _b64(json.dumps(self.payload(), sort_keys=True, separators=(",", ":")).encode())
        return f"{body}.{self.signature}"

    @classmethod
    def decode(cls, token: str, *, secret: str) -> ShareToken:
        """Parse and verify a token. Raises on any tampering."""
        body, _, signature = token.partition(".")
        if not body or not signature:
            raise ValueError("malformed share token")
        try:
            payload = json.loads(_unb64(body))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"malformed share token payload: {exc}") from exc

        expected = _sign(payload, secret)
        if not hmac.compare_digest(signature, expected):
            # Constant-time comparison. A byte-wise early return would leak the
            # signature one character at a time to anyone who can measure.
            raise ValueError("share token signature does not match")

        return cls(
            reel_id=str(payload.get("reel_id", "")),
            issued_at=datetime.fromtimestamp(int(payload.get("iat", 0)), tz=UTC),
            expires_at=datetime.fromtimestamp(int(payload.get("exp", 0)), tz=UTC),
            allowed_orgs=tuple(payload.get("orgs") or ()),
            signature=signature,
        )


def sign(
    reel_id: str,
    *,
    secret: str,
    ttl_hours: int = DEFAULT_TTL_HOURS,
    allowed_orgs: list[str] | None = None,
) -> ShareToken:
    """Create a signed token for a reel.

    Call this *after* redacting. There is no argument here for the reel's content
    precisely so that signing unredacted data is not expressible.
    """
    if not secret:
        raise ValueError(
            "a signing secret is required; set OPENBURROW_REEL_SHARE_SECRET "
            "(a link signed with a default secret is not signed at all)"
        )
    issued = datetime.now(UTC)
    expires = issued + timedelta(hours=ttl_hours)
    payload = {
        "reel_id": reel_id,
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "orgs": sorted(allowed_orgs or []),
    }
    return ShareToken(
        reel_id=reel_id,
        issued_at=issued,
        expires_at=expires,
        allowed_orgs=tuple(sorted(allowed_orgs or [])),
        signature=_sign(payload, secret),
    )


def verify(token: str, *, secret: str, org: str = "") -> ShareToken:
    """Verify a token and, when given, check the viewer's organisation."""
    parsed = ShareToken.decode(token, secret=secret)
    if parsed.is_expired:
        raise ValueError(f"share token expired at {parsed.expires_at.isoformat()}")
    if parsed.allowed_orgs and org and org not in parsed.allowed_orgs:
        raise ValueError(f"organisation {org!r} is not permitted to view this reel")
    return parsed


def build_share_url(base_url: str, token: ShareToken, *, path: str = "/reel") -> str:
    """Assemble a viewer URL."""
    return f"{base_url.rstrip('/')}{path}/{token.reel_id}?t={token.encode()}"


def new_secret() -> str:
    """Generate a signing secret. Offered so nobody is tempted to invent one."""
    return secrets.token_urlsafe(48)


def _sign(payload: dict[str, Any], secret: str) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    digest = hmac.new(secret.encode(), canonical, hashlib.sha256).digest()
    return _b64(digest)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode()


def _context(redacted: str, replacement: str, *, width: int = 24) -> str:
    """A short window of context around a redaction marker.

    Reads from the already-redacted string on purpose. Showing an operator the
    surrounding text is what lets them confirm the right thing was caught, and
    reading from the redacted string means there is no code path by which a secret
    reaches the report.
    """
    if not replacement:
        return ""
    index = redacted.find(replacement)
    if index == -1:
        return ""
    start = max(0, index - width)
    end = min(len(redacted), index + len(replacement) + width)
    snippet = redacted[start:end].replace("\n", " ").strip()
    return ("…" if start else "") + snippet + ("…" if end < len(redacted) else "")


__all__ = [
    "DEFAULT_TTL_HOURS",
    "REDACTION_RULES",
    "RedactionReport",
    "ShareToken",
    "build_share_url",
    "new_secret",
    "preview_redactions",
    "redact",
    "redact_payload",
    "sign",
    "verify",
]
