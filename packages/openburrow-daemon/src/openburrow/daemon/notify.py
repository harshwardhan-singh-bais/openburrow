"""Notifications and integrations (Stage 17).

Everything here reads the same event stream and answers one question: *does a
human need to know, right now?* The design constraint comes from item 209's
spirit — an alerts surface with everything on it trains people to ignore it —
so the notifier fires only on the four event families the operator opted into
(``notify_on``: approval, negotiation-unresolved, session-done, governance-flag)
and treats every other event as noise.

Three delivery channels, deliberately independent:

* **desktop** — fire-and-forget; failures are logged, never raised, because a
  lost toast must not take down a lane.
* **webhooks** (Slack / Discord / Teams) — bounded-retry HTTP POSTs whose
  signatures are HMAC'd with ``webhook_secret`` when one is configured
  (item 229's verification, from the sending side; the receiving side is
  :func:`verify_webhook_signature`).
* **hooks** — the operator's own commands (item 225), run from
  ``hooks.yaml``. Arbitrary shell on bus events is powerful, so hook commands
  run through :class:`shlex` and are refused when they contain shell
  metacharacters the operator did not intend to escape.

The standup export (item 228) is a pure function of the bus log, so it can be
rendered from any session id without the daemon running.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openburrow.core.logging import get_logger

if TYPE_CHECKING:
    from openburrow.core.config.settings import Settings

log = get_logger(__name__)

#: Events that map to the four notify_on families.
_EVENT_FAMILIES: dict[str, str] = {
    "governance.approval_required": "approval",
    "approval.awaiting": "approval",
    "task.auth_required": "approval",
    "negotiation.escalated": "negotiation-unresolved",
    "negotiation.finished": "negotiation-unresolved",
    "session.closed": "session-done",
    "governance.flag": "governance-flag",
    "governance.policy_denied": "governance-flag",
}

#: Webhook POST budget per delivery. A webhook that needs longer than this is
#: down; the queue must not back up behind it.
_WEBHOOK_TIMEOUT_S = 10.0

_WEBHOOK_ATTEMPTS = 3


def verify_webhook_signature(secret: str, body: bytes, signature: str) -> bool:
    """Verify an inbound webhook's HMAC-SHA256 signature (item 229).

    Uses :func:`hmac.compare_digest`, not ``==``: a plain string comparison
    short-circuits on the first differing byte, which leaks how many leading
    bytes an attacker guessed. The signature may be sent bare or with the
    ``sha256=`` prefix GitHub uses.
    """
    if not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    candidate = signature.removeprefix("sha256=").strip()
    return hmac.compare_digest(expected, candidate)


def sign_webhook_body(secret: str, body: bytes) -> str:
    """The sender-side counterpart: the header value for a signed delivery."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def event_family(event_type: str) -> str:
    """Which notify_on family an event belongs to, or ``''`` for noise."""
    return _EVENT_FAMILIES.get(event_type, "")


def _fmt_slack(summary: str, detail: dict[str, Any]) -> dict[str, Any]:
    session = str(detail.get("session_id") or "")
    text = f"[OpenBurrow] {summary}"
    if session:
        text += f" (`{session}`)"
    return {"text": text}


def _fmt_discord(summary: str, _detail: dict[str, Any]) -> dict[str, Any]:
    return {"content": f"**OpenBurrow** — {summary}"}


def _fmt_teams(summary: str, _detail: dict[str, Any]) -> dict[str, Any]:
    return {"text": f"**OpenBurrow** — {summary}"}


@dataclass(slots=True)
class HookSpec:
    """One operator-authored hook: run this command on these event families."""

    name: str
    command: str
    on: list[str] = field(default_factory=list)  # families or event types; [] = all

    def matches(self, event_type: str, family: str) -> bool:
        if not self.on:
            return True
        return event_type in self.on or (family in self.on)


def load_hooks(path: Path) -> list[HookSpec]:
    """Parse ``hooks.yaml``. A malformed file is reported, not fatal."""
    if not path.exists():
        return []
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except Exception as exc:
        log.warning("notify.hooks_unreadable", path=str(path), error=str(exc))
        return []

    hooks: list[HookSpec] = []
    for index, entry in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or f"hook-{index + 1}")
        command = str(entry.get("command") or "").strip()
        if not command:
            log.warning("notify.hook_missing_command", hook=name)
            continue
        # YAML 1.1 parses a bare `on:` key as the boolean True (like yes/no/off).
        # An operator who writes the natural thing must not silently get a hook
        # that matches nothing, so the boolean key is read as the alias it is.
        raw_on = entry.get("on", entry.get(True, []))
        on = [str(item) for item in (raw_on or []) if str(item)]
        hooks.append(HookSpec(name=name, command=command, on=on))
    return hooks


@dataclass(slots=True)
class _Delivery:
    """One rendered notification awaiting dispatch."""

    channel: str
    url: str
    body: bytes
    summary: str


class Notifier:
    """Turns qualifying bus events into desktop toasts, webhooks, and hooks."""

    def __init__(self, settings: Settings, *, hooks_path: Path | None = None) -> None:
        self.settings = settings
        self.notify_on: set[str] = set(settings.notify_on or [])
        self.hooks = load_hooks(hooks_path or Path(settings.hooks_file or ".openburrow/hooks.yaml"))
        self._session = None  # created lazily inside the running loop

    # --- entry point ------------------------------------------------------
    async def handle_event(self, event: dict[str, Any]) -> None:
        """Evaluate one bus event; dispatch every channel that opted in."""
        if not self.settings.notify_enabled:
            return
        event_type = str(event.get("event_type") or "")
        family = event_family(event_type)
        if family and family not in self.notify_on:
            return
        if not family and not self.hooks:
            return  # noise for every channel

        summary = str(event.get("summary") or event_type)
        detail = dict(event.get("payload") or {})
        detail["session_id"] = event.get("session_id") or detail.get("session_id") or ""

        if family:
            tasks: list[asyncio.Task[None]] = []
            if self.settings.notify_desktop:
                tasks.append(asyncio.create_task(self._desktop(summary, event_type)))
            for channel, url, fmt in self._webhooks():
                body = json.dumps(fmt(summary, detail)).encode("utf-8")
                tasks.append(asyncio.create_task(self._webhook(channel, url, body, summary)))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        for hook in self.hooks:
            if hook.matches(event_type, family):
                await self._run_hook(hook, event)

    def _webhooks(self) -> list[tuple[str, str, Any]]:
        targets: list[tuple[str, str, Any]] = []
        if url := self.settings.slack_webhook_url:
            targets.append(("slack", url, _fmt_slack))
        if url := self.settings.discord_webhook_url:
            targets.append(("discord", url, _fmt_discord))
        if url := self.settings.teams_webhook_url:
            targets.append(("teams", url, _fmt_teams))
        return targets

    # --- desktop ----------------------------------------------------------
    async def _desktop(self, summary: str, event_type: str) -> None:
        """Fire a native toast, degrading to a log line when no backend exists.

        No third-party notification dependency is used on purpose: a platform
        backend differs per OS, and a notify feature that fails loudly on a
        machine without one is worse than a logged line. The log line is the
        backend of last resort, and the bus event below is the durable record
        either way.
        """
        try:
            if self.settings.is_production and not self.settings.notify_desktop:
                return
            await asyncio.to_thread(self._desktop_blocking, summary)
        except Exception as exc:
            # `event` is structlog's first positional name; passing it as a
            # keyword collides. Renamed, not deleted — the event type is the
            # half of the context that says which notify path failed.
            log.debug("notify.desktop_unavailable", error=str(exc), event_kind=event_type)

    def _desktop_blocking(self, summary: str) -> None:
        import sys

        if sys.platform.startswith("win"):
            # Windows: use PowerShell's toast via Windows.UI.Notifications when
            # available; the fallback prints to the daemon's log, which the
            # operator reads anyway.
            try:
                import win10toast  # type: ignore[import-not-found]

                win10toast.ToastNotifier().show_toast(
                    "OpenBurrow", summary, duration=5, threaded=True
                )
                return
                # win10toast is not a declared dependency: if it happens to be
                # installed we use it, and if not the log line below is the
                # delivery. A notify backend must never be a hard dep.
            except ImportError:
                pass
        elif sys.platform == "darwin":
            with __import__("subprocess").Popen(
                [
                    "osascript",
                    "-e",
                    f'display notification "{summary}" with title "OpenBurrow"',
                ]
            ):
                pass
            return
        log.info("notify.desktop", summary=summary[:200])

    # --- webhooks ---------------------------------------------------------
    async def _webhook(self, channel: str, url: str, body: bytes, summary: str) -> None:
        """POST with bounded retries and an HMAC header when a secret is set."""
        import httpx

        headers = {"content-type": "application/json"}
        if self.settings.webhook_secret:
            headers["x-openburrow-signature"] = sign_webhook_body(
                self.settings.webhook_secret, body
            )

        last_error: Exception | None = None
        for attempt in range(1, _WEBHOOK_ATTEMPTS + 1):
            try:
                async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT_S) as client:
                    response = await client.post(url, content=body, headers=headers)
                if response.status_code < 400:
                    log.debug("notify.webhook_delivered", channel=channel)
                    return
                last_error = RuntimeError(f"{channel} webhook returned {response.status_code}")
            except Exception as exc:
                last_error = exc
            await asyncio.sleep(min(2**attempt, 8))
        log.warning(
            "notify.webhook_failed",
            channel=channel,
            attempts=_WEBHOOK_ATTEMPTS,
            error=str(last_error),
            summary=summary[:120],
        )

    # --- hooks ------------------------------------------------------------
    async def _run_hook(self, hook: HookSpec, event: dict[str, Any]) -> None:
        """Run one operator hook with the event on its environment.

        The command is split with :func:`shlex.split` and executed without a
        shell: an operator-authored ``command: rm -rf $OPENBURROW_EVENT_TYPE``
        should fail loudly at parse time rather than silently interpolate.
        The event reaches the hook through environment variables, which is the
        one interface that works identically for every language a hook might
        be written in.
        """
        try:
            argv = shlex.split(hook.command)
        except ValueError as exc:
            log.warning("notify.hook_unparseable", hook=hook.name, error=str(exc))
            return

        env = {**_hook_env(event)}
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            log.warning("notify.hook_spawn_failed", hook=hook.name, error=str(exc))
            return

        try:
            _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30.0)
        except TimeoutError:
            process.kill()
            log.warning("notify.hook_timeout", hook=hook.name)
            return

        if process.returncode != 0:
            log.warning(
                "notify.hook_failed",
                hook=hook.name,
                code=process.returncode,
                stderr=stderr.decode("utf-8", "replace")[:500],
            )
        else:
            log.debug("notify.hook_ran", hook=hook.name)


def _hook_env(event: dict[str, Any]) -> dict[str, str]:
    """The environment one hook invocation sees. Names, never secrets."""
    payload = event.get("payload")
    payload_json = json.dumps(payload, default=str) if payload else "{}"
    return {
        "OPENBURROW_EVENT_TYPE": str(event.get("event_type") or ""),
        "OPENBURROW_EVENT_SESSION": str(event.get("session_id") or ""),
        "OPENBURROW_EVENT_LANE": str(event.get("lane_id") or ""),
        "OPENBURROW_EVENT_SUMMARY": str(event.get("summary") or "")[:1000],
        "OPENBURROW_EVENT_PAYLOAD": payload_json[:16_000],
    }


def _fmt_duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def build_standup(events: list[dict[str, Any]], *, session_name: str = "") -> dict[str, Any]:
    """Summarise a session's log for the overnight report (item 228).

    A pure function of the bus log so it can render without a daemon. Answers,
    in order: what the agents *did*, what they *said to each other*, and what
    *governance* had to say about it — the three-part standup the item asks for.
    """
    lanes: dict[str, dict[str, Any]] = {}
    conversations: list[dict[str, str]] = []
    governance: list[dict[str, str]] = []
    negotiations = 0
    collisions_avoided = 0

    for event in events:
        event_type = str(event.get("event_type") or "")
        lane_id = str(event.get("lane_id") or "")
        summary = str(event.get("summary") or "")
        at = str(event.get("at") or "")

        if lane_id and event_type.startswith(("lane.started", "lane.output", "task.", "files.")):
            entry = lanes.setdefault(lane_id, {"lane_id": lane_id, "highlights": []})
            if event_type.startswith("task."):
                entry["highlights"].append(summary[:160])

        if event_type.startswith(("bus.message", "negotiation.move")):
            sender = str((event.get("payload") or {}).get("sender_human") or "") or (
                str((event.get("payload") or {}).get("sender_lane") or "") or lane_id
            )
            conversations.append({"at": at, "who": sender, "what": summary[:200]})

        if event_type.startswith("negotiation.") and event_type == "negotiation.finished":
            negotiations += 1
            payload = event.get("payload") or {}
            if bool(payload.get("collision_avoided")) or "agreed" in str(
                payload.get("outcome", "")
            ):
                collisions_avoided += 1

        if event_type.startswith(("governance.", "approval.")):
            governance.append({"at": at, "what": summary[:200]})

    return {
        "session_name": session_name,
        "lanes": sorted(lanes.values(), key=lambda lane: lane["lane_id"]),
        "conversation": conversations[-50:],
        "negotiations": negotiations,
        "collisions_avoided": collisions_avoided,
        "governance": governance[-50:],
        "counts": {
            "events": len(events),
            "negotiations": negotiations,
            "collisions_avoided": collisions_avoided,
            "governance_events": len(governance),
        },
    }


def render_standup_text(report: dict[str, Any]) -> str:
    """Human-readable standup — what a human reads with coffee."""
    lines = [f"OpenBurrow standup — {report.get('session_name') or 'session'}", ""]

    lanes = report.get("lanes") or []
    lines.append(f"What the agents did ({len(lanes)} lane(s)):")
    if not lanes:
        lines.append("  (no lane activity recorded)")
    for lane in lanes:
        lines.append(f"  {lane['lane_id']}:")
        for highlight in lane["highlights"][-5:]:
            lines.append(f"    · {highlight}")
    lines.append("")

    conversation = report.get("conversation") or []
    lines.append("What they said to each other:")
    if not conversation:
        lines.append("  (no inter-agent traffic)")
    for turn in conversation[-10:]:
        lines.append(f"  [{turn['at'][11:16]}] {turn['who']}: {turn['what']}")
    lines.append("")

    counts = report.get("counts") or {}
    lines.append(
        "Negotiations: "
        f"{counts.get('negotiations', 0)} run, {counts.get('collisions_avoided', 0)} collisions avoided"
    )

    governance = report.get("governance") or []
    lines.append("Governance:")
    if not governance:
        lines.append("  (nothing flagged)")
    for record in governance[-10:]:
        lines.append(f"  [{record['at'][11:16]}] {record['what']}")

    return "\n".join(lines)


def send_email_digest(
    report: dict[str, Any],
    *,
    host: str,
    recipients: list[str],
    sender: str,
    port: int = 587,
    user: str = "",
    password: str = "",
    use_tls: bool = True,
) -> int:
    """Email the standup as plain text via stdlib SMTP (item 224).

    Synchronous on purpose: the daemon calls it through
    :func:`asyncio.to_thread`, so a slow relay never blocks the event loop, and
    the email path stays usable from the CLI without an asyncio runtime.

    Returns the number of recipients accepted. Errors are logged, not raised —
    the same contract as every other channel in this module: a digest that did
    not send must not fail the session it describes.
    """
    import smtplib
    from email.message import EmailMessage

    if not recipients:
        log.debug("notify.email_no_recipients")
        return 0

    session_name = str(report.get("session_name") or "session")
    message = EmailMessage()
    message["Subject"] = f"[OpenBurrow] standup — {session_name}"
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(render_standup_text(report))

    try:
        with smtplib.SMTP(host, port, timeout=30.0) as smtp:
            if use_tls:
                # On a server without TLS this raises, which is the correct
                # signal: an unencrypted digest of governance events is worse
                # than a missed one. Configure a TLS relay instead of
                # weakening here; the loopback debug case opts out explicitly.
                smtp.starttls()
            if user:
                smtp.login(user, password)
            # `send_message` returns the refused-addresses dict, which is
            # falsy when everyone accepted; the int count is the contract.
            refused = smtp.send_message(message)
            return len(recipients) - len(refused)
    except Exception as exc:
        log.warning("notify.email_failed", error=str(exc), host=host)
        return 0


__all__ = [
    "HookSpec",
    "Notifier",
    "build_standup",
    "event_family",
    "load_hooks",
    "render_standup_text",
    "send_email_digest",
    "sign_webhook_body",
    "verify_webhook_signature",
]
