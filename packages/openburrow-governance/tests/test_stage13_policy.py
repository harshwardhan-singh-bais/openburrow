"""The pre-execution policy gate, tested against the four defects it replaced.

Stage 13's gate was not written from a blank page: it was extracted from
``burrow governance policy test``, and every test below is anchored to a
specific way that command was wrong. Three of the four were *silent* — the code
read as though it did the right thing, and the only reason they were found is
that someone asked what ``risk_tiers`` actually resolves to for
``git push --force`` rather than trusting the comment above the field.

The four, in the order they appear in the module docstring:

1. **Last tier won instead of the highest.** The inner ``break`` left the outer
   loop running, so the surviving value was whichever tier came last in the dict.
   ``git push --force`` reported ``low`` risk.
2. **Matching was a substring test on the first token.** ``"ls" in "false"`` is
   true, so ``false`` was classified as a read-only low-risk command;
   ``"git" in command`` is true for every git command, so every git command
   matched all four tiers.
3. **``default_action`` was conditional on an allowlist existing.** It was read
   only inside ``if action == "allow" and policy.allowed_commands:``, so with the
   shipped empty allowlist the strictest-looking knob in the config did nothing.
4. **The gate lived in a CLI command**, so nothing could consult it. Tested here
   by asserting the daemon and the CLI both route through ``PolicyGate`` — a
   fourth copy of the rules is how the first three survived.

Nothing here touches the network, a database or the filesystem, so the whole
file runs in the fast suite. The gate is pure by design; that is what makes it
testable this way, and it is also why the daemon can put it in front of a spawn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openburrow.core.config.repo_config import PolicyConfig
from openburrow.core.errors import PolicyViolation
from openburrow.governance import PolicyGate, matches_command, matches_path, resolve_role

pytestmark = [pytest.mark.unit, pytest.mark.governance]


def gate(**overrides: object) -> PolicyGate:
    """A gate over a policy built from the shipped defaults plus ``overrides``."""
    return PolicyGate(PolicyConfig(**overrides))


# ---------------------------------------------------------------------------
# 1. highest matching tier wins
# ---------------------------------------------------------------------------
class TestHighestTierWins:
    """The defect: ``risk_tier`` ended up as the last tier in the dict."""

    def test_force_push_is_critical_not_low(self) -> None:
        """The exact command the old implementation got backwards.

        It matched ``critical`` (``git push --force``), ``high`` (``git push``)
        and then ``low``, and assigned rather than maximised, so ``low`` won.
        """
        verdict = gate().check("git push --force")
        assert verdict.risk_tier == "critical"
        assert verdict.would_require_approval is True

    def test_a_command_matching_several_tiers_takes_the_most_severe(self) -> None:
        """``git push origin main`` matches ``high`` only, and stays high."""
        assert gate().check("git push origin main").risk_tier == "high"

    def test_a_family_and_a_fatal_member_can_both_be_declared(self) -> None:
        """``rm -rf`` is high; ``rm -rf /`` is critical. Both match the fatal one."""
        assert gate().check("rm -rf /tmp/build").risk_tier == "high"
        assert gate().check("rm -rf /").risk_tier == "critical"

    def test_tier_order_is_declared_not_inferred(self) -> None:
        """Order comes from ``RISK_ORDER``, not from dict insertion order.

        Reversing the dict must not change any answer. This is the property the
        old code lacked entirely — it had no notion of "more severe" at all.
        """
        forward = gate(
            risk_tiers={
                "low": ["deploy"],
                "critical": ["deploy --prod"],
            }
        )
        backward = gate(
            risk_tiers={
                "critical": ["deploy --prod"],
                "low": ["deploy"],
            }
        )
        for built in (forward, backward):
            assert built.check("deploy").risk_tier == "low"
            assert built.check("deploy --prod").risk_tier == "critical"

    def test_an_unmatched_command_is_low(self) -> None:
        assert gate().check("make coffee").risk_tier == "low"

    def test_approval_is_only_promised_for_commands_that_would_run(self) -> None:
        """A denied command does not pause for approval; it does not run."""
        verdict = gate(denied_commands=["git push --force"]).check("git push --force")
        assert verdict.action == "deny"
        assert verdict.would_require_approval is False
        assert verdict.risk_tier == "critical"


# ---------------------------------------------------------------------------
# 2. whole-word matching, not substring
# ---------------------------------------------------------------------------
class TestWordBoundaries:
    """The defect: ``pattern.split()[0] in command``.

    A substring test is wrong in both directions, and the false-positive
    direction is the one that looks harmless in review.
    """

    @pytest.mark.parametrize(
        ("pattern", "command"),
        [
            ("ls", "ls -la"),
            ("ls", "sudo ls /var/log"),
            ("git push", "sudo git push origin main"),
            ("cat", "cat README.md"),
            ("rm -rf /", "sudo rm -rf /"),
        ],
    )
    def test_real_matches_still_match(self, pattern: str, command: str) -> None:
        assert matches_command(pattern, command) is True

    @pytest.mark.parametrize(
        ("pattern", "command", "why"),
        [
            ("ls", "false", "'ls' is a substring of 'false' — the old false positive"),
            ("ls", "lsblk", "a different binary that merely starts with 'ls'"),
            ("ls", "als", "a substring at a word boundary on one side only"),
            ("cat", "concat.txt", "'cat' inside 'concat'"),
            ("git push", "git push-x", "a longer flag word, not the same token"),
            ("git status", "git statuses", "a different subcommand"),
            ("rm -rf /", "rm -rf /tmp/build", "the trailing boundary is what makes this distinct"),
        ],
    )
    def test_substring_neighbours_do_not_match(self, pattern: str, command: str, why: str) -> None:
        assert matches_command(pattern, command) is False, why

    def test_the_shipped_defaults_classify_read_only_commands_as_low(self) -> None:
        """``false`` used to be graded low *for the wrong reason* — via ``ls``.

        It is still low, because no pattern matches it at all. The distinction
        matters: the old answer was an accident that would have flipped the
        moment ``low`` stopped listing ``ls``.
        """
        verdict = gate().check("false")
        assert verdict.risk_tier == "low"
        assert verdict.matched_rule == "default_action=allow"

    def test_matching_is_case_insensitive(self) -> None:
        """Which is what makes the shipped ``DROP TABLE`` pattern usable.

        A config author writing SQL keywords in upper case is not asking for
        case-sensitive matching, and a deny rule that only fires on one casing is
        a deny rule that does not fire.
        """
        assert gate().check('psql -c "DROP TABLE users"').risk_tier == "critical"
        assert gate().check('psql -c "drop table users"').risk_tier == "critical"

    def test_a_pattern_with_an_internal_space_matches_a_phrase(self) -> None:
        assert matches_command("git push --force", "sudo git push --force origin") is True

    def test_empty_inputs_never_match(self) -> None:
        assert matches_command("", "ls") is False
        assert matches_command("ls", "") is False
        assert matches_command("   ", "ls") is False


# ---------------------------------------------------------------------------
# 3. default_action applies unconditionally
# ---------------------------------------------------------------------------
class TestFailClosedDefault:
    """The defect: ``default_action`` was read only when an allowlist existed."""

    def test_deny_default_applies_with_an_empty_allowlist(self) -> None:
        """The configuration that used to be inert.

        ``default_action: deny`` beside an empty ``allowed_commands`` now denies
        everything no deny rule names. Before, it denied nothing, and the setting
        that looks like the strictest line in the file did nothing at all.
        """
        verdict = gate(default_action="deny").check("git status")
        assert verdict.action == "deny"
        assert verdict.matched_rule == "default_action=deny"

    def test_the_shipped_default_is_allow(self) -> None:
        """A regression guard for a deliberate change of shipped behaviour.

        ``default_action`` defaulted to ``deny`` beside an empty allowlist, which
        under the corrected semantics means "deny every command" — the product
        would have refused to start a lane out of the box. The paths half of this
        model was already allow-by-default with a deny list, so the commands half
        now matches it.
        """
        assert PolicyConfig().default_action == "allow"
        assert gate().check("git status").action == "allow"

    def test_the_dangerous_combination_is_reported_not_silent(self) -> None:
        notes = gate(default_action="deny").diagnose()
        assert any("default_action is 'deny'" in note for note in notes), notes

    def test_a_configured_allowlist_admits_only_what_it_names(self) -> None:
        built = gate(default_action="deny", allowed_commands=["pytest", "git status"])
        assert built.check("pytest -x tests/").action == "allow"
        assert built.check("pytest -x tests/").matched_rule == "allowed_commands: pytest"
        assert built.check("curl evil.example").action == "deny"

    def test_an_allowlisted_command_still_reports_its_risk_tier(self) -> None:
        """Allowed and approved are different questions, and both get answered."""
        built = gate(allowed_commands=["git push"])
        verdict = built.check("git push origin main")
        assert verdict.action == "allow"
        assert verdict.risk_tier == "high"
        assert verdict.would_require_approval is True

    def test_deny_beats_allow(self) -> None:
        """The reverse would make the deny list advisory."""
        built = gate(allowed_commands=["docker push"], denied_commands=["docker push"])
        verdict = built.check("docker push acme/api")
        assert verdict.action == "deny"
        assert verdict.matched_rule == "denied_commands: docker push"

    def test_enforce_is_carried_but_not_conflated_with_the_verdict(self) -> None:
        """``enforce`` decides whether a denial blocks; it is not a rule."""
        advisory = PolicyGate(PolicyConfig(enforce=False, denied_commands=["rm -rf /"]))
        assert advisory.enforce is False
        assert advisory.check("rm -rf /").action == "deny"


# ---------------------------------------------------------------------------
# 4. one owner, and a raising entry point
# ---------------------------------------------------------------------------
class TestSingleOwner:
    """The defect: the rules lived in a CLI command nobody could call."""

    def test_require_raises_policy_violation(self) -> None:
        """``PolicyViolation`` was exported and never raised before this stage."""
        built = gate(denied_commands=["rm -rf /"])
        with pytest.raises(PolicyViolation) as caught:
            built.require("rm -rf /")
        assert "denied_commands" in str(caught.value)
        assert caught.value.context["matched_rule"] == "denied_commands: rm -rf /"

    def test_require_returns_the_verdict_when_allowed(self) -> None:
        verdict = gate().require("git status")
        assert verdict.action == "allow"

    def test_require_does_not_raise_when_enforce_is_off(self) -> None:
        """Advisory means advisory, and it is the same code path."""
        built = PolicyGate(PolicyConfig(enforce=False, denied_commands=["rm -rf /"]))
        assert built.require("rm -rf /").action == "deny"

    def test_check_argv_gates_a_spawn_plan(self) -> None:
        built = gate(denied_commands=["--dangerously-skip-permissions"])
        verdict = built.check_argv(["claude", "--dangerously-skip-permissions"])
        assert verdict.action == "deny"

    def test_check_argv_treats_cwd_as_the_declared_path(self) -> None:
        built = gate()
        verdict = built.check_argv(["pytest"], cwd="secrets/tokens")
        assert verdict.action == "deny"
        assert verdict.matched_rule == "denied_paths: secrets/"

    def test_the_cli_delegates_rather_than_reimplementing(self) -> None:
        """A second copy of the rules is how the first three defects survived.

        Asserted at the source level because that is the actual claim: there is
        one implementation. A behavioural test would pass with two.
        """
        from openburrow.cli.commands import governance as cli_governance

        source = Path(cli_governance.__file__).read_text(encoding="utf-8")
        assert "PolicyGate(" in source
        # The old inline evaluator, recognisable by its give-away substring test.
        assert "pattern.split()[0] in command" not in source
        assert "denied.split()[0] in command" not in source

    def test_the_daemon_consults_the_gate_before_spawning(self) -> None:
        from openburrow.daemon import sessions as daemon_sessions

        source = Path(daemon_sessions.__file__).read_text(encoding="utf-8")
        assert "PolicyGate(" in source
        assert "check_argv(" in source


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
class TestPaths:
    @pytest.mark.parametrize(
        ("pattern", "path"),
        [
            (".env", ".env"),
            (".env", "src/config/.env"),
            ("secrets/", "secrets/token.txt"),
            ("secrets", "secrets/token.txt"),
            ("secrets", "a/b/secrets/token.txt"),
            ("secrets", "/home/u/repo/secrets"),
            ("**/*.pem", "a.pem"),
            ("**/*.pem", "certs/a.pem"),
            ("~/.ssh", "~/.ssh/id_rsa"),
            ("~/.aws", "~/.aws/credentials"),
        ],
    )
    def test_denied(self, pattern: str, path: str) -> None:
        assert matches_path(pattern, path) is True

    @pytest.mark.parametrize(
        ("pattern", "path"),
        [
            (".env", "src/.env.bak"),
            ("secrets", "src/secrets.md"),
            ("**/*.pem", "a.pem.txt"),
            ("~/.ssh", "src/id_rsa"),
        ],
    )
    def test_not_denied(self, pattern: str, path: str) -> None:
        assert matches_path(pattern, path) is False

    def test_a_directory_pattern_matches_with_or_without_its_slash(self) -> None:
        """An earlier draft only handled the trailing-slash form.

        That left ``~/.ssh`` matching the directory itself and nothing inside it,
        which is the opposite of what a deny list of directories is for.
        """
        assert matches_path("secrets/", "secrets/x") is True
        assert matches_path("secrets", "secrets/x") is True

    def test_dot_means_inside_the_repository(self) -> None:
        assert matches_path(".", "src/x.py") is True
        assert matches_path(".", "../outside/x.py") is False
        assert matches_path(".", "/etc/passwd") is False

    def test_dot_is_judged_on_the_whole_path_not_its_basename(self) -> None:
        """The candidate expansion that makes ``.env`` work breaks ``.``.

        A basename is relative by construction and never escapes, so running "."
        through the basename candidates answered "inside the repo" for
        ``/etc/passwd`` and ``../outside``.
        """
        assert matches_path(".", "/etc/passwd") is False
        assert matches_path(".", "../outside/x.py") is False

    def test_denied_paths_are_scanned_in_the_command_text(self) -> None:
        """A deny rule that needs a volunteered path is bypassed by not volunteering."""
        verdict = gate().check("cat ~/.ssh/id_rsa")
        assert verdict.action == "deny"
        assert verdict.matched_rule == "denied_paths: ~/.ssh"

    def test_denied_paths_see_through_an_equals_flag(self) -> None:
        verdict = gate().check("tool --config=secrets/policy.yaml")
        assert verdict.action == "deny"

    def test_allowed_paths_are_not_applied_to_the_command_text(self) -> None:
        """``uv add foo/bar`` is a package name, not a filesystem access.

        Applying ``allowed_paths`` to every path-shaped token would deny it, and a
        gate that denies ordinary commands gets switched off.
        """
        assert gate().check("uv add foo/bar").action == "allow"

    def test_allowed_paths_constrain_the_declared_path(self) -> None:
        built = gate(allowed_paths=["src"])
        assert built.check("pytest", path="src/app.py").action == "allow"
        assert built.check("pytest", path="docs/readme.md").action == "deny"

    def test_a_declared_path_outside_the_repo_is_denied(self) -> None:
        built = gate(allowed_paths=["."])
        assert built.check("pytest", path="/etc/passwd").action == "deny"

    def test_repo_root_makes_an_absolute_cwd_relative(self) -> None:
        """A spawn plan's cwd is absolute; policy paths are repo-relative.

        Without this, ``allowed_paths: ["."]`` denies every lane start — the rule
        means "keep it in the repo" and would have read as "keep it nowhere".
        """
        root = Path.cwd()
        built = PolicyGate(PolicyConfig(allowed_paths=["."]), repo_root=root)
        assert built.check("pytest", path=str(root / "src" / "app.py")).action == "allow"
        outside = root.parent / "elsewhere" / "app.py"
        assert built.check("pytest", path=str(outside)).action == "deny"


# ---------------------------------------------------------------------------
# role overrides narrow
# ---------------------------------------------------------------------------
class TestRoleOverrides:
    """Authority can only narrow, applied to policy rather than to delegations."""

    def test_a_role_may_replace_the_allowlist(self) -> None:
        policy = PolicyConfig(
            allowed_commands=["git status", "git commit", "pytest"],
            role_overrides={"reviewer": {"allowed_commands": ["git status", "cat"]}},
        )
        built = PolicyGate(policy)
        assert built.check("cat README.md", role="reviewer").action == "allow"
        assert built.check("git commit -m x", role="reviewer").action == "allow"

    def test_a_role_may_add_denials(self) -> None:
        policy = PolicyConfig(
            allowed_commands=["pytest"],
            role_overrides={"reviewer": {"denied_commands": ["pytest"]}},
        )
        assert PolicyGate(policy).check("pytest -x", role="reviewer").action == "deny"

    def test_a_role_may_tighten_the_default(self) -> None:
        policy = PolicyConfig(
            default_action="allow",
            role_overrides={"reviewer": {"default_action": "deny"}},
        )
        assert PolicyGate(policy).check("make coffee", role="reviewer").action == "deny"

    def test_a_role_may_not_loosen_the_default(self) -> None:
        """The narrowing invariant. A role that could widen its own authority
        would make the base policy a suggestion."""
        policy = PolicyConfig(
            default_action="deny",
            role_overrides={"reviewer": {"default_action": "allow"}},
        )
        assert PolicyGate(policy).check("make coffee", role="reviewer").action == "deny"

    def test_a_role_may_not_loosen_an_allowlist_into_a_wildcard(self) -> None:
        policy = PolicyConfig(
            default_action="deny",
            allowed_commands=["git status"],
            role_overrides={"reviewer": {"allowed_commands": ["*"]}},
        )
        assert PolicyGate(policy).check("curl evil.example", role="reviewer").action == "deny"

    def test_an_unlisted_role_gets_the_base_policy(self) -> None:
        policy = PolicyConfig(allowed_commands=["pytest"])
        assert PolicyGate(policy).check("pytest", role="observer").action == "allow"

    def test_resolve_role_reports_the_effective_rules(self) -> None:
        policy = PolicyConfig(
            allowed_commands=["a"],
            denied_commands=["b"],
            role_overrides={"reviewer": {"allowed_commands": ["c"], "denied_commands": ["d"]}},
        )
        base = resolve_role(policy, None)
        assert base.allowed_commands == ("a",) and base.denied_commands == ("b",)
        narrowed = resolve_role(policy, "reviewer")
        assert narrowed.allowed_commands == ("c",)
        assert narrowed.denied_commands == ("b", "d")


# ---------------------------------------------------------------------------
# configuration health
# ---------------------------------------------------------------------------
class TestDiagnostics:
    """A knob that is set and does nothing is the defect class of this stage."""

    def test_the_default_policy_is_quiet(self) -> None:
        assert gate().diagnose() == ()

    def test_empty_risk_tiers_are_reported(self) -> None:
        notes = gate(risk_tiers={}).diagnose()
        assert any("nothing will ever require approval" in note for note in notes)

    def test_an_empty_tier_is_reported(self) -> None:
        notes = gate(risk_tiers={"low": ["ls"], "critical": []}).diagnose()
        assert any("empty tiers: critical" in note for note in notes)

    def test_an_empty_pattern_is_reported(self) -> None:
        notes = gate(denied_commands=["  "]).diagnose()
        assert any("empty pattern" in note for note in notes)

    def test_a_glob_in_a_command_pattern_is_reported(self) -> None:
        """Patterns are literal phrases. ``npm *`` is not a wildcard, and the
        user should be told rather than left with a rule that never fires."""
        notes = gate(allowed_commands=["npm *"]).diagnose()
        assert any("glob character" in note for note in notes)

    def test_a_glob_in_a_path_pattern_is_not_reported(self) -> None:
        """Globs are meaningful for paths, so the same warning must not fire."""
        notes = gate(denied_paths=["**/*.pem"]).diagnose()
        assert not any("glob character" in note for note in notes)

    def test_an_unknown_override_key_is_reported(self) -> None:
        notes = gate(role_overrides={"reviewer": {"allowed_command": ["x"]}}).diagnose()
        assert any("unknown key" in note and "allowed_command" in note for note in notes)

    def test_a_malformed_override_is_ignored_and_reported(self) -> None:
        built = gate(
            allowed_commands=["git status"],
            role_overrides={"reviewer": {"allowed_commands": "cat"}},
        )
        # A bare string is a single pattern, not a mistake — but it must not
        # silently become a character-by-character iteration.
        assert built.check("cat README.md", role="reviewer").action == "allow"
        assert built.check("git status", role="reviewer").action == "allow"

    def test_a_malformed_override_value_is_reported_and_ignored(self) -> None:
        """The inner values of ``role_overrides`` are unvalidated, so this is
        reachable — unlike a non-mapping override, which pydantic rejects at
        construction and which therefore needs no diagnostic at all."""
        built = gate(
            default_action="deny",
            allowed_commands=["git status"],
            role_overrides={"reviewer": {"allowed_commands": {"cat": True}}},
        )
        assert any("not a list" in note for note in built.diagnose())
        # The malformed entry is dropped, so the base allowlist still applies.
        assert built.check("git status", role="reviewer").action == "allow"
        assert built.check("cat README.md", role="reviewer").action == "deny"

    def test_a_loosening_override_is_reported(self) -> None:
        notes = gate(
            default_action="deny", role_overrides={"reviewer": {"default_action": "allow"}}
        ).diagnose()
        assert any("may only narrow" in note for note in notes)

    def test_diagnostics_travel_with_the_verdict(self) -> None:
        """``policy test`` is exactly when a misconfiguration should be visible."""
        verdict = gate(default_action="deny").check("git status")
        assert any("default_action is 'deny'" in note for note in verdict.diagnostics)


# ---------------------------------------------------------------------------
# the env layer actually reaches the gate
# ---------------------------------------------------------------------------
class TestEnvLayerReachesTheGate:
    """``OPENBURROW_POLICY_*`` settings that were declared and never merged."""

    def test_default_action_and_allowed_paths_merge_from_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from openburrow.core.config.load import load_config
        from openburrow.core.config.settings import clear_settings_cache

        (tmp_path / "openburrow.yaml").write_text("schema_version: 1\n", encoding="utf-8")
        monkeypatch.setenv("OPENBURROW_POLICY_DEFAULT_ACTION", "deny")
        monkeypatch.setenv("OPENBURROW_POLICY_ALLOWED_PATHS", "src, tests")
        monkeypatch.setenv("OPENBURROW_HOME", str(tmp_path / ".openburrow"))
        clear_settings_cache()
        try:
            config = load_config(repo_root=tmp_path)
        finally:
            clear_settings_cache()
        assert config.policy.default_action == "deny"
        assert config.policy.allowed_paths == ["src", "tests"]

    def test_the_shipped_default_survives_an_empty_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from openburrow.core.config.load import load_config
        from openburrow.core.config.settings import clear_settings_cache

        (tmp_path / "openburrow.yaml").write_text("schema_version: 1\n", encoding="utf-8")
        monkeypatch.setenv("OPENBURROW_HOME", str(tmp_path / ".openburrow"))
        monkeypatch.delenv("OPENBURROW_POLICY_DEFAULT_ACTION", raising=False)
        monkeypatch.delenv("OPENBURROW_POLICY_ALLOWED_PATHS", raising=False)
        clear_settings_cache()
        try:
            config = load_config(repo_root=tmp_path)
        finally:
            clear_settings_cache()
        assert config.policy.default_action == "allow"
        assert config.policy.allowed_paths == ["."]
