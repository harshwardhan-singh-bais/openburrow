"""Stage 15 tests: the per-harness sandbox (items 198 and 200).

The settings this exercises — ``sandbox_enabled``, ``sandbox_backend``,
``sandbox_cpu_limit``, ``sandbox_mem_limit_mb``, ``sandbox_network``,
``sandbox_network_allowlist`` — were declared, documented in ``.env.example``,
and read by nothing. ``sandbox_network == "allow"`` even had a validator warning
that it "defeats sandboxing in production": a warning about a control that did
not exist. This file is what makes the settings mean something.

Two properties get most of the attention, because they are the ones that decide
whether the feature is a boundary or a word in a config file:

* **Fail closed.** ``sandbox_enabled=true`` with an unusable backend refuses the
  lane. Starting it unsandboxed and logging would leave the operator behaving as
  though a boundary exists. An action taken on the strength of a control that
  could not be applied is the failure mode this repository keeps rediscovering.
* **Report what is enforced, not what was configured.** ``plan()`` separates the
  two, and the tests pin the cases where they differ — a host allowlist on a
  backend that can only turn the network off, and resource limits on a backend
  that was never asked to apply them.

The platform and ``which`` are injected throughout. Without that every test of a
non-native backend would be skipped on whichever machine runs the suite, and a
skipped test is a claim nobody has checked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openburrow.adapters.base import SpawnSpec
from openburrow.core.config.settings import Settings
from openburrow.daemon import sandbox
from openburrow.daemon.sandbox import SandboxUnavailable

pytestmark = pytest.mark.unit


def present(binary: str) -> str:
    """A ``which`` that reports every backend binary as installed."""
    return f"/usr/bin/{binary}"


def absent(binary: str) -> None:
    """A ``which`` that reports nothing installed."""
    return


def spec_for(command: list[str] | None = None) -> SpawnSpec:
    return SpawnSpec(
        command=command or ["claude", "--print"],
        cwd=Path("/repo/worktrees/alice"),
        env={"ANTHROPIC_API_KEY": "sk-test", "PATH": "/usr/bin"},
        use_pty=True,
    )


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "sandbox_enabled": True,
        "sandbox_backend": "bubblewrap",
        "sandbox_network": "deny",
    }
    base.update(overrides)
    return Settings(**base)


# --------------------------------------------------------------------------
# the plan: what would actually be enforced
# --------------------------------------------------------------------------
class TestPlanReportsWhatIsEnforced:
    def test_disabled_says_disabled(self) -> None:
        described = sandbox.plan(Settings())
        assert described.enabled is False
        assert described.available is True
        assert described.enforced == ()

    def test_a_backend_from_another_platform_is_unavailable_not_substituted(self) -> None:
        """bubblewrap does not exist on macOS or Windows, and saying so beats
        silently running the harness unsandboxed."""
        described = sandbox.plan(
            settings(sandbox_backend="bubblewrap"), platform="darwin", which=present
        )
        assert described.available is False
        assert "not a backend on this platform" in described.reason

    def test_the_reason_names_what_is_available_here(self) -> None:
        described = sandbox.plan(
            settings(sandbox_backend="firejail"), platform="win32", which=present
        )
        assert "process" in described.reason
        assert "docker" in described.reason

    def test_a_missing_binary_is_unavailable(self) -> None:
        described = sandbox.plan(settings(), platform="linux", which=absent)
        assert described.available is False
        assert "bwrap" in described.reason

    def test_a_present_binary_is_available(self) -> None:
        described = sandbox.plan(settings(), platform="linux", which=present)
        assert described.available is True

    def test_bubblewrap_does_not_claim_resource_limits(self) -> None:
        """bubblewrap bounds the filesystem and the network. Claiming a memory
        cap it was never asked to apply is how a report stops being evidence."""
        described = sandbox.plan(settings(), platform="linux", which=present)
        assert "filesystem_scoped" in described.enforced
        assert "network" in described.enforced
        assert "mem_limit" in described.unenforced

    def test_process_reports_limits_and_says_it_is_not_isolation(self) -> None:
        described = sandbox.plan(
            settings(sandbox_backend="process"), platform="linux", which=present
        )
        assert described.enforced == ("cpu_limit", "mem_limit")
        assert "no filesystem or network isolation" in described.reason

    def test_process_on_windows_enforces_nothing_and_admits_it(self) -> None:
        """There is no RLIMIT_AS on Windows. Reporting the limits as enforced
        would be a lie the operator cannot see."""
        described = sandbox.plan(
            settings(sandbox_backend="process"), platform="win32", which=present
        )
        assert described.enforced == ()
        assert "cpu_limit" in described.unenforced
        assert "mem_limit" in described.unenforced

    def test_docker_without_an_image_is_unavailable(self) -> None:
        """No default image: the harness must run in something that has the
        harness in it, and guessing that on the operator's behalf would run
        their code in a container they never chose."""
        described = sandbox.plan(
            settings(sandbox_backend="docker"), platform="linux", which=present
        )
        assert described.available is False
        assert "sandbox_docker_image" in described.reason

    def test_docker_with_an_image_is_available_and_caps_resources(self) -> None:
        described = sandbox.plan(
            settings(sandbox_backend="docker", sandbox_docker_image="acme/harness:1"),
            platform="linux",
            which=present,
        )
        assert described.available is True
        assert "cpu_limit" in described.enforced
        assert "mem_limit" in described.enforced

    def test_an_allowlist_is_not_claimed_by_a_backend_that_cannot_honour_it(self) -> None:
        """The important negative. seatbelt can turn the network off or leave it
        on; it cannot allow ``api.openai.com`` and nothing else. Granting full
        egress because the list could not be applied is exactly the class of bug
        this module exists to prevent."""
        described = sandbox.plan(
            settings(sandbox_backend="seatbelt", sandbox_network="allowlist"),
            platform="darwin",
            which=present,
        )
        assert described.available is True
        assert "network" not in described.enforced
        assert "network_allowlist" in described.unenforced

    def test_allow_network_is_reported_as_unenforced_not_enforced(self) -> None:
        described = sandbox.plan(settings(sandbox_network="allow"), platform="linux", which=present)
        assert "network" not in described.enforced
        assert "network" in described.unenforced

    def test_the_plan_serialises_for_a_report(self) -> None:
        payload = sandbox.plan(settings(), platform="linux", which=present).as_dict()
        assert payload["backend"] == "bubblewrap"
        assert payload["enforced"] == ["filesystem_scoped", "network"]


# --------------------------------------------------------------------------
# fail closed
# --------------------------------------------------------------------------
class TestWrapFailsClosed:
    def test_disabled_returns_the_spec_untouched(self) -> None:
        original = spec_for()
        assert sandbox.wrap(Settings(), original) is original

    def test_an_unavailable_backend_raises_rather_than_running_unsandboxed(self) -> None:
        with pytest.raises(SandboxUnavailable) as caught:
            sandbox.wrap(settings(), spec_for(), platform="linux", which=absent)
        assert "bwrap" in str(caught.value)
        assert "cannot be used" in str(caught.value)

    def test_the_error_tells_the_operator_how_to_proceed(self) -> None:
        """Refusing is only useful if the way out is stated, and the way out has
        to include 'run it unsandboxed on purpose' — otherwise the next person
        sets the backend to something that exists and learns nothing."""
        with pytest.raises(SandboxUnavailable) as caught:
            sandbox.wrap(settings(), spec_for(), platform="win32", which=present)
        hint = caught.value.hint or ""
        assert "OPENBURROW_SANDBOX_ENABLED=false" in hint

    def test_a_wrong_platform_backend_raises(self) -> None:
        with pytest.raises(SandboxUnavailable):
            sandbox.wrap(
                settings(sandbox_backend="seatbelt"), spec_for(), platform="linux", which=present
            )


# --------------------------------------------------------------------------
# the wrapping itself
# --------------------------------------------------------------------------
class TestWrapping:
    def test_bubblewrap_prefixes_the_harness_rather_than_replacing_it(self) -> None:
        wrapped = sandbox.wrap(settings(), spec_for(), platform="linux", which=present)
        assert wrapped.command[0] == "bwrap"
        assert wrapped.command[-2:] == ["claude", "--print"]

    def test_network_deny_adds_the_network_namespace_flag(self) -> None:
        wrapped = sandbox.wrap(
            settings(sandbox_network="deny"), spec_for(), platform="linux", which=present
        )
        assert "--unshare-net" in wrapped.command

    def test_network_allow_omits_it(self) -> None:
        wrapped = sandbox.wrap(
            settings(sandbox_network="allow"), spec_for(), platform="linux", which=present
        )
        assert "--unshare-net" not in wrapped.command

    def test_the_worktree_is_the_writable_path(self) -> None:
        wrapped = sandbox.wrap(
            settings(), spec_for(), platform="linux", which=present, repo_root=Path("/repo")
        )
        assert "--bind" in wrapped.command
        assert "/repo" in wrapped.command

    def test_firejail_uses_net_none_for_a_denied_network(self) -> None:
        wrapped = sandbox.wrap(
            settings(sandbox_backend="firejail"), spec_for(), platform="linux", which=present
        )
        assert wrapped.command[0] == "firejail"
        assert "--net=none" in wrapped.command

    def test_seatbelt_grants_network_only_when_network_is_allowed(self) -> None:
        denied = sandbox.wrap(
            settings(sandbox_backend="seatbelt"), spec_for(), platform="darwin", which=present
        )
        assert "allow network*" not in " ".join(denied.command)

        allowed = sandbox.wrap(
            settings(sandbox_backend="seatbelt", sandbox_network="allow"),
            spec_for(),
            platform="darwin",
            which=present,
        )
        assert "allow network*" in " ".join(allowed.command)

    def test_seatbelt_is_deny_by_default_with_a_worktree_write_exception(self) -> None:
        wrapped = sandbox.wrap(
            settings(sandbox_backend="seatbelt"),
            spec_for(),
            platform="darwin",
            which=present,
            repo_root=Path("/repo"),
        )
        profile = " ".join(wrapped.command)
        assert "(deny default)" in profile
        assert '(allow file-write* (subpath "/repo"))' in profile

    def test_docker_passes_the_lanes_own_environment_not_the_developers(self) -> None:
        """Credential isolation happened upstream on this spec. What crosses into
        the container must be the lane's slice — passing the whole environment
        would undo the isolation the sandbox is meant to sit under."""
        wrapped = sandbox.wrap(
            settings(sandbox_backend="docker", sandbox_docker_image="acme/harness:1"),
            spec_for(),
            platform="linux",
            which=present,
        )
        assert "-eANTHROPIC_API_KEY" in wrapped.command
        assert "-ePATH" in wrapped.command
        assert "--network" in wrapped.command
        assert "none" in wrapped.command

    def test_docker_uses_bridge_when_network_is_allowed(self) -> None:
        wrapped = sandbox.wrap(
            settings(
                sandbox_backend="docker",
                sandbox_docker_image="acme/harness:1",
                sandbox_network="allow",
            ),
            spec_for(),
            platform="linux",
            which=present,
        )
        assert "bridge" in wrapped.command

    def test_process_on_windows_is_a_no_op(self) -> None:
        original = spec_for()
        wrapped = sandbox.wrap(
            settings(sandbox_backend="process"), original, platform="win32", which=present
        )
        assert wrapped.command == original.command

    def test_process_on_posix_installs_resource_limits(self) -> None:
        wrapped = sandbox.wrap(
            settings(sandbox_backend="process"),
            spec_for(),
            platform="linux",
            which=present,
        )
        joined = " ".join(wrapped.command)
        assert "RLIMIT_CPU" in joined
        assert "RLIMIT_AS" in joined
        assert "execvp" in joined

    def test_the_original_spec_is_not_mutated(self) -> None:
        """The pre-gate spec is what the policy verdict and the audit record
        refer to. Rewriting it in place would make the logged command differ from
        the gated one — the same defect the gate's single-producer rule fixes."""
        original = spec_for()
        before = list(original.command)

        sandbox.wrap(settings(), original, platform="linux", which=present)

        assert original.command == before
        assert original.command[0] == "claude"

    def test_wrapping_preserves_cwd_env_and_pty(self) -> None:
        original = spec_for()
        wrapped = sandbox.wrap(settings(), original, platform="linux", which=present)
        assert wrapped.cwd == original.cwd
        assert wrapped.env == original.env
        assert wrapped.use_pty is True
        assert wrapped.stdin_pipe is True


class TestPlatformDetection:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("linux", "linux"), ("linux2", "linux"), ("darwin", "darwin"), ("win32", "win32")],
    )
    def test_platform_names_are_normalised(
        self, raw: str, expected: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sandbox.sys, "platform", raw)
        assert sandbox.current_platform() == expected
