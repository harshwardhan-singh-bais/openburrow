"""Per-harness execution sandboxing (Stage 15, items 198 and 200).

``sandbox_enabled``, ``sandbox_backend``, ``sandbox_cpu_limit``,
``sandbox_mem_limit_mb``, ``sandbox_network`` and ``sandbox_network_allowlist``
were declared in ``Settings``, documented in ``.env.example``, and read by
**nothing**. ``sandbox_network == "allow"`` even had a validator warning that it
"defeats sandboxing in production" — a warning about a control that did not
exist. Eight settings that only the config layer knew about is the same shape as
the policy gate before Stage 13: the feature reads as done in every document and
does nothing at runtime.

The sandbox wraps the finished :class:`SpawnSpec` between the policy gate and the
spawn. That ordering is deliberate and is the only ordering that works:

* **after** ``prepare_spawn_spec``, because the wrapper has to see the real argv
  — a sandbox that decides based on a plan it built separately is sandboxing a
  different command;
* **after** the gate, so a denied command is refused rather than wrapped and then
  refused, which would put a sandbox-availability check on the path of a command
  that was never going to run;
* **before** ``start``, so there is exactly one object that both runs and was
  inspected.

**Fail closed, and refuse rather than warn.** If the operator sets
``sandbox_enabled=true`` with a backend whose binary is absent, the lane does not
start. The alternative — start it unsandboxed and log — is the failure this
project keeps finding: an action taken on the strength of a detection that could
not see. Someone who asks for a sandbox and silently does not get one is worse
off than someone who never asked, because they will behave accordingly.

``process`` is the one backend that is honest about being no isolation at all. It
applies POSIX resource limits where the platform supports them and reports the
rest as not enforced, rather than implying a boundary that is not there.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openburrow.core.errors import ConfigError
from openburrow.core.logging import get_logger

log = get_logger(__name__)

#: Backends that are a command prefix: they wrap argv and leave the harness
#: binary in place, so a spec can be rewritten without knowing anything about the
#: harness. ``process`` is handled separately because it has no prefix.
_PREFIX_BACKENDS: dict[str, str] = {
    "bubblewrap": "bwrap",
    "firejail": "firejail",
    "seatbelt": "sandbox-exec",
    "docker": "docker",
}

#: Which backends exist on which platform, so the error message can say why
#: rather than only that it failed. A macOS user told "bubblewrap not found" will
#: go install bubblewrap and find out it does not exist there.
_PLATFORM_BACKENDS: dict[str, frozenset[str]] = {
    "linux": frozenset({"process", "bubblewrap", "firejail", "docker"}),
    "darwin": frozenset({"process", "seatbelt", "docker"}),
    "win32": frozenset({"process", "docker"}),
}

#: Exit status a wrapped process returns when the sandbox itself refused. Chosen
#: to match ``EX_SOFTWARE`` so a crash report says "the sandbox failed", not
#: "the harness crashed", which would send someone to the wrong logs.
SANDBOX_REFUSED_EXIT = 70


class SandboxUnavailable(ConfigError):
    """The requested isolation cannot be provided on this machine."""


@dataclass(frozen=True)
class SandboxPlan:
    """What the sandbox would actually do, for ``burrow doctor`` and the audit log.

    Separate from the wrapping so a report can describe the boundary without
    building one — the same reason ``build_spawn_spec`` is pure and
    ``burrow doctor`` can describe a spawn it never performs.
    """

    enabled: bool
    backend: str
    available: bool
    #: What is genuinely enforced. Not what was configured — what will happen.
    enforced: tuple[str, ...] = ()
    #: Configured but not enforceable by this backend, stated rather than omitted.
    unenforced: tuple[str, ...] = ()
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "available": self.available,
            "enforced": list(self.enforced),
            "unenforced": list(self.unenforced),
            "reason": self.reason,
        }


@dataclass
class _Backend:
    """One backend's argv construction and its honest coverage list."""

    name: str
    enforced: tuple[str, ...]
    unenforced: tuple[str, ...] = ()
    #: ``argv`` builder, given the settings and the command to wrap.
    build: Callable[..., list[str]] | None = None
    notes: list[str] = field(default_factory=list)


def current_platform() -> str:
    """``sys.platform`` reduced to the three names the backend table uses."""
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    return "win32"


def _which(name: str) -> str | None:
    return shutil.which(name)


def plan(
    settings: Any,
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> SandboxPlan:
    """Describe what the sandbox would enforce. Pure — it spawns nothing.

    ``which`` and ``platform`` are injectable so the platform matrix can be
    tested on whichever machine happens to be running the suite. Without that,
    every test of the non-native backends is skipped on the machine of whoever
    is reading it, and a skipped test is a claim nobody has checked.
    """
    if not settings.sandbox_enabled:
        return SandboxPlan(enabled=False, backend="none", available=True, reason="disabled")

    platform = platform or current_platform()
    which = which or _which
    backend = settings.sandbox_backend

    allowed = _PLATFORM_BACKENDS.get(platform, frozenset({"process"}))
    if backend not in allowed:
        return SandboxPlan(
            enabled=True,
            backend=backend,
            available=False,
            reason=(
                f"{backend!r} is not a backend on this platform ({platform}). "
                f"Available here: {', '.join(sorted(allowed))}."
            ),
        )

    if backend == "process":
        # The honest one. On POSIX the resource limits below are real; there is
        # no filesystem or network boundary at all, and saying otherwise is how
        # a sandbox becomes a reassuring word in a config file.
        posix = platform in {"linux", "darwin"}
        return SandboxPlan(
            enabled=True,
            backend="process",
            available=True,
            enforced=("cpu_limit", "mem_limit") if posix else (),
            unenforced=() if posix else ("cpu_limit", "mem_limit"),
            reason=(
                "resource limits only; no filesystem or network isolation. "
                "Use bubblewrap, firejail or seatbelt for a real boundary."
            ),
        )

    binary = _PREFIX_BACKENDS[backend]
    if which(binary) is None:
        return SandboxPlan(
            enabled=True,
            backend=backend,
            available=False,
            reason=f"backend binary {binary!r} is not on PATH",
        )

    if backend == "docker" and not getattr(settings, "sandbox_docker_image", ""):
        return SandboxPlan(
            enabled=True,
            backend="docker",
            available=False,
            reason=(
                "sandbox_docker_image is empty. The docker backend runs the harness "
                "in an image you choose; there is no safe default to guess."
            ),
        )

    network_enforced = settings.sandbox_network in {"deny", "allowlist"}
    enforced = ["filesystem_scoped"]
    unenforced: list[str] = []
    if network_enforced:
        enforced.append("network")
    else:
        unenforced.append("network")

    if backend == "docker":
        enforced.extend(("cpu_limit", "mem_limit"))
    else:
        # bubblewrap/firejail/seatbelt bound the filesystem and the network;
        # CPU and memory caps are not part of what they are being asked to do
        # here, so they are reported as unenforced rather than claimed.
        unenforced.extend(("cpu_limit", "mem_limit"))

    if settings.sandbox_network == "allowlist" and backend != "docker":
        # An allowlist needs a proxy or a firewall rule per host. bubblewrap and
        # seatbelt can only turn the network off or leave it on, so a list of
        # hosts is not something they can honour — and silently granting full
        # egress because the list could not be applied is the exact class of bug
        # this module exists to avoid.
        unenforced.append("network_allowlist")
        enforced.remove("network")

    return SandboxPlan(
        enabled=True,
        backend=backend,
        available=True,
        enforced=tuple(enforced),
        unenforced=tuple(unenforced),
        reason="",
    )


def wrap(
    settings: Any,
    spec: Any,
    *,
    repo_root: Path | None = None,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> Any:
    """Return a spec whose command runs inside the configured sandbox.

    ``settings`` comes first, matching :func:`plan`. Returns ``spec`` unchanged
    when sandboxing is off, and **raises** :class:`SandboxUnavailable` when it is
    on and cannot be honoured. It never returns an unsandboxed spec for a
    sandboxed request.
    """
    described = plan(settings, platform=platform, which=which)
    if not described.enabled:
        return spec

    if not described.available:
        raise SandboxUnavailable(
            f"sandbox backend {settings.sandbox_backend!r} cannot be used",
            hint=(
                f"{described.reason} Fix the backend, or set "
                "OPENBURROW_SANDBOX_ENABLED=false to run the harness unsandboxed "
                "on purpose."
            ),
            context={"backend": settings.sandbox_backend, "reason": described.reason},
        )

    if settings.sandbox_backend == "process":
        wrapped = _wrap_process(spec, settings, platform=platform or current_platform())
    else:
        wrapped = _wrap_prefix(spec, settings, repo_root=repo_root)

    # Logged here rather than inside each builder, so the record is written once
    # per spawn and always names what was enforced. A log line inside the
    # builders would have to be repeated for each backend, and the first one
    # somebody added without it would be a lane that was sandboxed invisibly.
    log.info(
        "sandbox.applied",
        backend=settings.sandbox_backend,
        enforced=list(described.enforced),
        unenforced=list(described.unenforced),
        network=settings.sandbox_network,
    )
    return wrapped


def _wrap_process(spec: Any, settings: Any, *, platform: str) -> Any:
    """Resource limits via a short interpreter preamble, or an honest no-op.

    There is no filesystem or network boundary to install here. What can be
    installed is an ``RLIMIT_CPU`` and an ``RLIMIT_AS``, and on Windows neither
    exists — so on Windows this is a no-op that says so in the plan rather than
    a no-op that looks like a sandbox.
    """
    if platform == "win32":
        return spec

    cpu_seconds = max(1, int(settings.sandbox_cpu_limit * 3600))
    mem_bytes = max(16, settings.sandbox_mem_limit_mb) * 1024 * 1024
    preamble = (
        "import os, resource, sys;"
        f"resource.setrlimit(resource.RLIMIT_CPU, ({cpu_seconds}, {cpu_seconds}));"
        f"resource.setrlimit(resource.RLIMIT_AS, ({mem_bytes}, {mem_bytes}));"
        "os.execvp(sys.argv[1], sys.argv[1:])"
    )
    command = [sys.executable, "-c", preamble, *spec.command]
    return _with_command(spec, command)


def _wrap_prefix(spec: Any, settings: Any, *, repo_root: Path | None) -> Any:
    """Wrap argv with a backend that is a command prefix."""
    backend = settings.sandbox_backend
    # ``as_posix()`` rather than ``str()``, and this is not cosmetic. Every prefix
    # backend is a POSIX program: bubblewrap and firejail exist only on Linux,
    # seatbelt only on macOS, and their arguments are paths those kernels
    # resolve. ``str(Path)`` on a Windows host produces backslashes, which a
    # POSIX kernel reads as literal filename characters — so a seatbelt profile
    # built on a Windows machine would name a directory that cannot exist. Docker
    # accepts forward slashes everywhere, so it takes the same form.
    cwd = Path(spec.cwd)
    cwd_arg = cwd.as_posix()
    writable = Path(repo_root or cwd).as_posix()

    if backend == "bubblewrap":
        prefix = [
            "bwrap",
            "--die-with-parent",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--bind",
            writable,
            writable,
            "--chdir",
            cwd_arg,
        ]
        if settings.sandbox_network == "deny":
            prefix.append("--unshare-net")
    elif backend == "firejail":
        prefix = ["firejail", "--quiet", "--private-tmp", f"--whitelist={writable}"]
        if settings.sandbox_network == "deny":
            prefix.append("--net=none")
    elif backend == "seatbelt":
        # seatbelt's profile language is deny-by-default, so the profile below
        # is the whole policy: read everywhere, write only under the worktree,
        # and (unless network is allowed) no outbound sockets at all.
        allow_net = "(allow network*)" if settings.sandbox_network == "allow" else ""
        profile = (
            "(version 1)"
            "(deny default)"
            "(allow process*)"
            "(allow file-read*)"
            f'(allow file-write* (subpath "{writable}"))'
            f"{allow_net}"
        )
        prefix = ["sandbox-exec", "-p", profile]
    else:  # docker
        image = settings.sandbox_docker_image
        prefix = [
            "docker",
            "run",
            "--rm",
            "-i",
            f"--cpus={settings.sandbox_cpu_limit}",
            f"--memory={settings.sandbox_mem_limit_mb}m",
            "--network",
            "none" if settings.sandbox_network == "deny" else "bridge",
            "-v",
            f"{writable}:{writable}",
            "-w",
            cwd_arg,
            # The harness's own environment has to cross into the container or it
            # cannot reach its provider. Credential isolation already happened
            # upstream on this spec, so what is passed is the lane's slice, not
            # the developer's whole environment.
            *[f"-e{k}" for k in sorted(spec.env)],
            image,
        ]

    return _with_command(spec, [*prefix, *spec.command])


def _with_command(spec: Any, command: list[str]) -> Any:
    """A copy of ``spec`` with a rewritten command.

    A copy rather than a mutation: the pre-gate spec is what the audit record
    and the policy verdict refer to, and rewriting it in place would make the
    logged command differ from the gated one.
    """
    from openburrow.adapters.base import SpawnSpec

    if isinstance(spec, SpawnSpec):
        return SpawnSpec(
            command=command,
            cwd=spec.cwd,
            env=dict(spec.env),
            use_pty=spec.use_pty,
            stdin_pipe=spec.stdin_pipe,
        )
    # Duck-typed fallback for the tests' stand-ins and for any adapter that
    # grows its own spec type: copy, do not mutate.
    return type(spec)(**{**vars(spec), "command": command})


__all__ = [
    "SANDBOX_REFUSED_EXIT",
    "SandboxPlan",
    "SandboxUnavailable",
    "current_platform",
    "plan",
    "wrap",
]
