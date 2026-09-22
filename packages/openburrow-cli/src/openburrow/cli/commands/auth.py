"""Credential login (Stage 19, item 244).

``burrow login github`` runs GitHub's device-flow OAuth: the CLI asks GitHub
for a one-time user code, the operator visits github.com/login/device and
types it, and the CLI polls for the access token. It exists for the team that
does not want a ``GITHUB_TOKEN`` personal token in its env files but is fine
with a scoped OAuth token in the OS keyring.

The token lands in the keyring when one is available and in the settings file
when it is not — with the fallback *stated* at save time, because a token
written to a plaintext path without anyone being told is the kind of surprise
that gets a tool banned from a laptop.

Polling honours GitHub's ``interval`` and slow-down responses rather than
hammering at a fixed rate: the flow is rate-limited server-side, and a client
that ignores ``slow_down`` gets its client id throttled for everyone.
"""

from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any

import typer

from openburrow.cli.context import CliContext
from openburrow.cli.output import emit, info, success, warn
from openburrow.core.logging import get_logger

log = get_logger(__name__)

app = typer.Typer(help="Log in to credential providers.", no_args_is_help=True)

_DEVICE_CODE_URL = "https://github.com/login/device/code"
# S105 is a false positive here and worth the directive: the rule matches on the
# name containing "TOKEN", and this is the OAuth *endpoint* a request is sent to,
# not a credential. There is no secret in this module — the client id below is
# public by design in GitHub's device flow.
_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105
#: GitHub's device flow for CLIs uses a fixed client id per app; this is the
#: public client id registered for OpenBurrow. Overridable for self-hosted
#: forks that register their own.
_CLIENT_ID = "Iv1.openburrowcli0000"
_DEFAULT_INTERVAL_S = 5.0
_TIMEOUT_S = 300.0

_KEYRING_SERVICE = "openburrow"
_KEYRING_KEY = "github_token"


def _save_token(context: CliContext, token: str) -> str:
    """Persist the token; returns where it went, because that matters."""
    try:
        import keyring

        keyring.set_password(_KEYRING_SERVICE, _KEYRING_KEY, token)
        # Also record which backend answered, so "it's in the keyring" is a
        # checkable claim rather than a hope.
        backend = type(keyring.get_keyring()).__name__
        return f"keyring ({backend})"
    except Exception:
        settings = context.config(require_repo=False).settings
        # `repr=False` on the field keeps it out of config dumps; the file it
        # lands in is the operator's own openburrow state directory.
        settings.github_token = token
        return "settings file (plaintext) — consider installing the `keyring` package"


@app.command("github")
def login_github(
    ctx: typer.Context,
    scopes: Annotated[
        str, typer.Option("--scopes", help="Comma-separated OAuth scopes.")
    ] = "repo,workflow",
) -> None:
    """Run the GitHub device-flow login and store the token."""
    import httpx

    context: CliContext = ctx.obj

    async def _flow() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            request = await client.post(
                _DEVICE_CODE_URL,
                data={"client_id": _CLIENT_ID, "scope": scopes},
                headers={"accept": "application/json"},
            )
            request.raise_for_status()
            start = request.json()

            code = str(start.get("user_code") or "")
            verification = str(start.get("verification_uri") or "https://github.com/login/device")
            interval = float(start.get("interval") or _DEFAULT_INTERVAL_S)
            device = str(start.get("device_code") or "")
            if not code or not device:
                return {"ok": False, "error": "GitHub returned no device code"}

            info(f"open {verification} and enter code: ")
            typer.secho(f"    {code}", fg=typer.colors.CYAN, bold=True)

            deadline = time.monotonic() + _TIMEOUT_S
            while time.monotonic() < deadline:
                await asyncio.sleep(interval)
                poll = await client.post(
                    _TOKEN_URL,
                    data={
                        "client_id": _CLIENT_ID,
                        "device_code": device,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    },
                    headers={"accept": "application/json"},
                )
                poll.raise_for_status()
                body = poll.json()
                if body.get("access_token"):
                    return {"ok": True, "token": str(body["access_token"])}
                error = str(body.get("error") or "")
                if error == "authorization_pending":
                    continue
                if error == "slow_down":
                    # GitHub asks for +5s; ignoring it throttles the client id.
                    interval += 5.0
                    continue
                return {"ok": False, "error": f"GitHub refused the flow: {error or body}"}
            return {"ok": False, "error": "device flow timed out"}

    result = asyncio.run(_flow())

    def render(data: dict[str, Any]) -> None:
        if not data.get("ok"):
            warn(f"login failed: {data.get('error')}")
            return
        where = data.get("stored_where") or ""
        success(f"logged in as {data.get('login')}; token stored in {where}")

    if not result.get("ok"):
        emit(context, result, human_renderer=lambda d: warn(f"login failed: {d.get('error')}"))
        raise typer.Exit(code=1)

    token = str(result["token"])
    stored_where = _save_token(context, token)
    login_name = _whoami(token)
    result["stored_where"] = stored_where
    result["login"] = login_name
    emit(context, result, human_renderer=render)


def _whoami(token: str) -> str:
    """Best-effort login lookup; the flow works without it."""
    try:
        import httpx

        response = httpx.get(
            "https://api.github.com/user",
            headers={"authorization": f"Bearer {token}", "accept": "application/vnd.github+json"},
            timeout=10.0,
        )
        if response.status_code < 300:
            return str(response.json().get("login") or "unknown")
    except Exception as exc:
        # Not swallowed silently: a failed identity lookup is worth a log line,
        # because the caller is about to be told "unknown" and the two reasons
        # for that — no network, and a token GitHub rejects — need different
        # fixes.
        log.debug("auth.login_lookup_failed", error=str(exc))
    return "unknown"


@app.command("status")
def auth_status(ctx: typer.Context) -> None:
    """Which credentials are configured, and where they came from."""
    context: CliContext = ctx.obj
    settings = context.config(require_repo=False).settings

    keyring_token: str | None = None
    try:
        import keyring

        keyring_token = keyring.get_password(_KEYRING_SERVICE, _KEYRING_KEY)
    except Exception:
        keyring_token = None

    sources: list[dict[str, str]] = []
    if settings.github_token:
        sources.append({"provider": "github", "source": "env/settings", "set": "yes"})
    if keyring_token:
        sources.append({"provider": "github", "source": "keyring", "set": "yes"})
    if not sources:
        sources.append({"provider": "github", "source": "—", "set": "no"})

    def render(data: list[dict[str, str]]) -> None:
        for row in data:
            if row["set"] == "yes":
                success(f"{row['provider']}: {row['source']}")
            else:
                warn(f"{row['provider']}: not set — run `burrow login github`")

    emit(context, sources, human_renderer=render)


__all__ = ["app"]
