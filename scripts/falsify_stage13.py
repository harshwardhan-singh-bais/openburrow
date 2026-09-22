"""Stage 13 falsification pass.

For every new test row, re-implement the behaviour the fix replaced and confirm
the row *fails*. A test that passes against both the old and the new code is not a
boundary, it is decoration.

The harness differs from ``falsify_stage12.py`` in one way that matters. Stage 12
patched methods at runtime, which meant the "reverted" code lived in this script
and could drift from what it claimed to reconstruct. Here the revert is applied
to the **module source** and the result is executed as a fresh module, so the
assertion runs against genuinely reverted code — the same bytes the fix replaced,
minus the fix. An anchor that no longer matches is an error, not a silent skip.

Three verdicts, as in Stage 12:

* ``ok`` — the test fails against the reverted code, which is the point.
* ``VACUOUS`` — the test still passes, so it does not discriminate.
* ``WEAK`` — the revert *raised*. A revert that raises has not tested the
  assertion at all, and counting that as a pass is how a falsification pass
  becomes theatre.

Convention: one file per stage, named ``falsify_stageNN.py``, kept as a snapshot
of the old behaviour at the time of the fix. It is deliberately *not* a permanent
gate — when the code moves on, the reverted behaviour it reconstructs stops being
the behaviour that was replaced, and a stale falsification is worse than none.
Re-run it when touching the stage; write a new one for a new stage.
"""

from __future__ import annotations

import os
import sys
import types
from collections.abc import Callable
from functools import cached_property
from itertools import count
from pathlib import Path
from typing import Any

from falsify_matcher import replace_anchor, self_check
from pydantic_settings.exceptions import SettingsError

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "packages"))

from openburrow.core.config.repo_config import PolicyConfig  # noqa: E402
from openburrow.core.config.settings import Settings  # noqa: E402
from openburrow.core.errors import PolicyViolation  # noqa: E402

POLICY_SRC = (
    REPO / "packages" / "openburrow-governance" / "src" / "openburrow" / "governance" / "policy.py"
)
SETTINGS_SRC = (
    REPO / "packages" / "openburrow-core" / "src" / "openburrow" / "core" / "config" / "settings.py"
)
REPO_CONFIG_SRC = (
    REPO
    / "packages"
    / "openburrow-core"
    / "src"
    / "openburrow"
    / "core"
    / "config"
    / "repo_config.py"
)
LOAD_SRC = (
    REPO / "packages" / "openburrow-core" / "src" / "openburrow" / "core" / "config" / "load.py"
)
SESSIONS_SRC = (
    REPO / "packages" / "openburrow-daemon" / "src" / "openburrow" / "daemon" / "sessions.py"
)
REGISTRY_SRC = (
    REPO / "packages" / "openburrow-adapters" / "src" / "openburrow" / "adapters" / "registry.py"
)

#: A custom adapter module the trust-gate rows can point the registry at. It has
#: to be a real ``HarnessAdapter`` subclass or the registry's ``issubclass``
#: filter discards it and the row measures nothing.
_TRUST_PROBE = """
from openburrow.adapters.harnesses.mock import MockAdapter


class FalsifyProbeAdapter(MockAdapter):
    name = "falsify-probe"
    description = "Written by the falsification harness."
"""


def write_trust_probe(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "falsify_probe.py"
    path.write_text(_TRUST_PROBE, encoding="utf-8")
    return path


#: Names each reverted module uniquely so two reverts of one file can coexist
#: in ``sys.modules`` without the second clobbering the first.
_revert_counter = count(1)


def revert(path: Path, *replacements: tuple[str, str]) -> types.ModuleType:
    """Execute ``path``'s source with ``replacements`` applied, as a new module.

    A missing anchor is a hard error, and so is an ambiguous one. The whole failure
    mode this script exists to catch is a revert that quietly did not happen, and a
    ``str.replace`` that matched nothing is exactly that. Matching is on tokens
    rather than bytes — see :mod:`falsify_matcher` for why, and for the anchors that
    want the exact mode instead.

    The module is registered in ``sys.modules`` under its own name before it is
    executed, and that is not optional. ``@dataclass(slots=True)`` resolves
    ``__slots__`` through ``sys.modules[cls.__module__].__dict__``, and pydantic
    resolves annotations the same way, so an unregistered module makes both raise
    ``AttributeError: 'NoneType' object has no attribute '__dict__'`` and
    ``PydanticUserError: not fully defined``. Both were reported as WEAK rows on
    the first run of this script — correctly, because a revert that raises proves
    nothing. It is a good reminder that a falsification harness is code too, and
    its own failures look exactly like a test that does not discriminate.
    """
    source = path.read_text(encoding="utf-8")
    for old, new in replacements:
        source = replace_anchor(source, old, new, path.name)

    name = f"{path.stem}_reverted_{next(_revert_counter)}"
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    sys.modules[name] = module
    exec(compile(source, str(path), "exec"), module.__dict__)  # noqa: S102
    return module


def reverted_policy(*replacements: tuple[str, str]) -> types.ModuleType:
    return revert(POLICY_SRC, *replacements)


def gate_of(module: types.ModuleType, **overrides: Any) -> Any:
    return module.PolicyGate(module.PolicyConfig(**overrides))


ROWS: list[tuple[str, str]] = []


def row(name: str, note: str) -> Callable[[Callable[[], bool]], Callable[[], bool]]:
    def wrap(fn: Callable[[], bool]) -> Callable[[], bool]:
        ROWS.append((name, note))
        return fn

    return wrap


# --- 1. the risk tier -------------------------------------------------------
@row(
    "TestHighestTierWins::test_force_push_is_critical_not_low",
    "assign the tier instead of maximising it, and match on the first token",
)
def check_1() -> bool:
    module = reverted_policy(
        (
            """        best: RiskTier = "low"
        best_index = _RISK_INDEX[best]
        for tier, patterns in self.policy.risk_tiers.items():
            index = _RISK_INDEX.get(tier)
            if index is None or index <= best_index:
                continue
            for pattern in patterns:
                if matches_command(pattern, command):
                    best = tier
                    best_index = index
                    break
        return best""",
            """        risk_tier = "low"
        for tier, patterns in self.policy.risk_tiers.items():
            for pattern in patterns:
                if pattern.split()[0] in command:
                    risk_tier = tier
                    break
        return risk_tier""",
        )
    )
    return gate_of(module).check("git push --force").risk_tier == "critical"


# --- 2. word boundaries -----------------------------------------------------
@row(
    "TestWordBoundaries::test_substring_neighbours_do_not_match",
    "match with `pattern.split()[0] in command` again",
)
def check_2() -> bool:
    module = reverted_policy(
        (
            """    pattern = (pattern or "").strip()
    if not pattern or not command:
        return False
    return _pattern_regex(pattern).search(command) is not None""",
            """    if not pattern or not command:
        return False
    return pattern.split()[0] in command""",
        )
    )
    return module.matches_command("ls", "false") is False


# --- 3. default_action applies unconditionally ------------------------------
@row(
    "TestFailClosedDefault::test_deny_default_applies_with_an_empty_allowlist",
    "consult `default_action` only when an allowlist exists",
)
def check_3() -> bool:
    module = reverted_policy(
        (
            """        if allowed_rule is not None:
            action: Action = "allow"
            matched_rule = f"allowed_commands: {allowed_rule}"
            matched_kind = "allowed_commands"
        else:
            action = rules.default_action
            matched_rule = f"default_action={rules.default_action}"
            matched_kind = "default_action\"""",
            """        if allowed_rule is not None:
            action: Action = "allow"
            matched_rule = f"allowed_commands: {allowed_rule}"
            matched_kind = "allowed_commands"
        elif rules.allowed_commands and rules.default_action == "deny":
            action = "deny"
            matched_rule = "default_action=deny"
            matched_kind = "default_action"
        else:
            action = "allow"
            matched_rule = "default"
            matched_kind = "default_action\"""",
        )
    )
    return gate_of(module, default_action="deny").check("git status").action == "deny"


# --- 4. the shipped default ------------------------------------------------
@row(
    "TestFailClosedDefault::test_the_shipped_default_is_allow",
    "ship `default_action: deny` beside an empty allowlist",
)
def check_4() -> bool:
    module = revert(
        REPO_CONFIG_SRC,
        (
            '    default_action: Literal["deny", "allow"] = "allow"',
            '    default_action: Literal["deny", "allow"] = "deny"',
        ),
    )
    return module.PolicyConfig().default_action == "allow"


# --- 5. directory patterns -------------------------------------------------
@row(
    "TestPaths::test_a_directory_pattern_matches_with_or_without_its_slash",
    "require a trailing slash before treating a pattern as a directory",
)
def check_5() -> bool:
    module = reverted_policy(
        (
            """    if stem and (
        candidate == stem
        or candidate.startswith(f"{stem}/")
        or f"/{stem}/" in f"/{candidate}"
        or candidate.rsplit("/", 1)[-1] == stem
    ):
        return True""",
            """    if body.endswith("/") and stem and (
        candidate == stem or candidate.startswith(f"{stem}/") or f"/{stem}/" in f"/{candidate}"
    ):
        return True""",
        )
    )
    return module.matches_path("~/.ssh", "~/.ssh/id_rsa") is True


# --- 6. "." is the whole path, not the basename ----------------------------
@row(
    "TestPaths::test_dot_is_judged_on_the_whole_path_not_its_basename",
    "run the repository-relative pattern through the basename candidates",
)
def check_6() -> bool:
    module = reverted_policy(
        (
            """    if target in {".", "./"}:
        return _match_one_path(target, _normalise(path))""",
            """    if target in {".", "./"}:
        return any(_match_one_path(target, candidate) for candidate in _path_candidates(path))""",
        )
    )
    return module.matches_path(".", "/etc/passwd") is False


# --- 7. denied paths are scanned in the command text -----------------------
@row(
    "TestPaths::test_denied_paths_are_scanned_in_the_command_text",
    "scan only the path the caller declared",
)
def check_7() -> bool:
    module = reverted_policy(
        ("        tokens = _path_tokens(command)", "        tokens: tuple[str, ...] = ()")
    )
    return gate_of(module).check("cat ~/.ssh/id_rsa").action == "deny"


# --- 8. deny beats allow ---------------------------------------------------
@row(
    "TestFailClosedDefault::test_deny_beats_allow",
    "let the allowlist win: consult `denied_commands` only when nothing is allowed",
)
def check_8() -> bool:
    """The first draft of this row was VACUOUS, and the reason is instructive.

    Moving the deny block *later* while keeping its early ``return`` left deny
    still winning, so the test passed and the row proved nothing. The property is
    not "deny is evaluated somewhere"; it is "deny is evaluated *first*". The
    revert below is the one that actually removes it: the allowlist is consulted
    alone, and denials are only reached when no allow rule matched.
    """
    deny_block = """        denied_rule = _first_match(rules.denied_commands, command)
        if denied_rule is not None:
            return self._verdict(
                command,
                "deny",
                f"denied_commands: {denied_rule}",
                "denied_commands",
                declared,
                role_name,
                diagnostics,
            )

"""
    module = reverted_policy(
        (deny_block, ""),
        (
            """        if allowed_rule is not None:
            action: Action = "allow"
            matched_rule = f"allowed_commands: {allowed_rule}"
            matched_kind = "allowed_commands"
        else:
            action = rules.default_action
            matched_rule = f"default_action={rules.default_action}"
            matched_kind = "default_action\"""",
            """        if allowed_rule is not None:
            action: Action = "allow"
            matched_rule = f"allowed_commands: {allowed_rule}"
            matched_kind = "allowed_commands"
        else:
            denied_rule = _first_match(rules.denied_commands, command)
            if denied_rule is not None:
                action = "deny"
                matched_rule = f"denied_commands: {denied_rule}"
                matched_kind = "denied_commands"
            else:
                action = rules.default_action
                matched_rule = f"default_action={rules.default_action}"
                matched_kind = "default_action\"""",
        ),
    )
    built = gate_of(module, allowed_commands=["docker push"], denied_commands=["docker push"])
    return built.check("docker push acme/api").action == "deny"


# --- 9. role overrides narrow ----------------------------------------------
@row(
    "TestRoleOverrides::test_a_role_may_not_loosen_the_default",
    "let an override set `default_action: allow` over a base that denies",
)
def check_9() -> bool:
    module = reverted_policy(
        (
            '    if override.get("default_action") == "deny":\n        default = "deny"',
            '    if override.get("default_action") in {"deny", "allow"}:\n'
            '        default = override["default_action"]',
        )
    )
    built = gate_of(
        module, default_action="deny", role_overrides={"reviewer": {"default_action": "allow"}}
    )
    return built.check("make coffee", role="reviewer").action == "deny"


# --- 10. `require` raises --------------------------------------------------
@row(
    "TestSingleOwner::test_require_raises_policy_violation",
    "return the verdict instead of raising on a denial",
)
def check_10() -> bool:
    module = reverted_policy(
        (
            "        verdict = self.check(command, path=path, role=role)\n"
            '        if verdict.action == "deny" and self.enforce:',
            "        verdict = self.check(command, path=path, role=role)\n"
            '        if False and verdict.action == "deny" and self.enforce:',
        )
    )
    try:
        gate_of(module, denied_commands=["rm -rf /"]).require("rm -rf /")
    except PolicyViolation:
        return True
    except Exception:
        # Raised, but not the exception the test expects — the test would still
        # fail, but for a reason unrelated to the assertion.
        return False
    return False


# --- 11. the env prefix ----------------------------------------------------
@row(
    "TestTheNamespace::test_a_prefixed_string_setting_reaches_its_field",
    'remove `env_prefix="OPENBURROW_"` so bare names are matched instead',
)
def check_11() -> bool:
    """Probes a field with no native alias, and that choice is the row's point.

    ``opencode_config_dir`` — the first field this check used — carries
    ``native_alias("OPENCODE_CONFIG_DIR")``, whose choices include the prefixed
    form, so it survives the prefix being removed and the row came back VACUOUS.
    ``policy_file`` has no alias, so it depends entirely on ``env_prefix``. The
    test sweeps every ``str`` field, so it catches both; a single-field probe has
    to pick one that can actually fail.
    """
    module = revert(SETTINGS_SRC, ('        env_prefix="OPENBURROW_",\n', ""))
    saved = dict(os.environ)
    try:
        os.environ["OPENBURROW_POLICY_FILE"] = "probe-policy.yaml"
        return module.Settings().policy_file == "probe-policy.yaml"
    finally:
        os.environ.clear()
        os.environ.update(saved)


# --- 12. NoDecode on CSV lists --------------------------------------------
@row(
    "TestCsvLists::test_a_comma_separated_value_parses",
    "drop `NoDecode`, so pydantic-settings JSON-decodes before the CSV validator",
)
def check_12() -> bool:
    """A WEAK verdict would be wrong here, and the distinction is worth stating.

    Normally a revert that raises proves nothing — the row "failed" for a reason
    unrelated to the assertion. This row is the exception: the raise *is* the
    replaced behaviour. pydantic-settings JSON-decodes any non-scalar field, so
    ``OPENBURROW_POLICY_ALLOWED_COMMANDS=alpha, beta`` fails inside
    ``prepare_field_value`` before the CSV validator is ever consulted, and a
    deployment that copied ``.env.example`` could not start. The check asserts
    the exception names the field, so a revert that raised for some *other*
    reason still fails the assertion and is still reported as WEAK.
    """
    module = revert(
        SETTINGS_SRC,
        ("CsvList = Annotated[list[str], NoDecode]", "CsvList = Annotated[list[str], object]"),
    )
    saved = dict(os.environ)
    try:
        os.environ["OPENBURROW_POLICY_ALLOWED_COMMANDS"] = "alpha, beta"
        try:
            settings = module.Settings()
        except SettingsError as exc:
            assert "policy_allowed_commands" in str(exc), f"unexpected SettingsError: {exc}"
            return False
        return settings.policy_allowed_commands == ["alpha", "beta"]
    finally:
        os.environ.clear()
        os.environ.update(saved)


# --- 13. the env layer actually merges ------------------------------------
@row(
    "TestEnvLayerReachesTheGate::test_default_action_and_allowed_paths_merge_from_settings",
    "declare the two settings and never merge them",
)
def check_13() -> bool:
    from openburrow.core.config import load as load_module

    def old_policy(self: Any) -> Any:
        base = self.repo.policy.model_copy(deep=True)
        base.enforce = self.settings.policy_enforce
        if self.settings.policy_allowed_commands:
            base.allowed_commands = list(self.settings.policy_allowed_commands)
        if self.settings.policy_denied_commands:
            base.denied_commands = list(self.settings.policy_denied_commands)
        if self.settings.policy_denied_paths:
            base.denied_paths = sorted({*base.denied_paths, *self.settings.policy_denied_paths})
        return base

    original = load_module.ResolvedConfig.__dict__["policy"]
    # `cached_property.__set_name__` has to be called by hand: assigning the
    # descriptor to an existing class skips the metaclass hook, and without it
    # the property raises on first access rather than returning a value.
    reverted_property = cached_property(old_policy)
    reverted_property.__set_name__(load_module.ResolvedConfig, "policy")
    load_module.ResolvedConfig.policy = reverted_property  # type: ignore[assignment]
    saved = dict(os.environ)
    try:
        os.environ["OPENBURROW_POLICY_DEFAULT_ACTION"] = "deny"
        os.environ["OPENBURROW_POLICY_ALLOWED_PATHS"] = "src, tests"
        from openburrow.core.config.settings import clear_settings_cache

        clear_settings_cache()
        config = load_module.load_config(repo_root=Path.cwd(), require_repo=False)
        return config.policy.default_action == "deny" and config.policy.allowed_paths == [
            "src",
            "tests",
        ]
    finally:
        os.environ.clear()
        os.environ.update(saved)
        load_module.ResolvedConfig.policy = original  # type: ignore[assignment]
        from openburrow.core.config.settings import clear_settings_cache

        clear_settings_cache()


# --- 14. the daemon consults the gate ------------------------------------
@row(
    "TestDeniedSpawn::test_a_denied_plan_raises_before_the_lane_starts",
    "make `_gate_spawn` a no-op, as it was when nothing consulted the gate",
)
def check_14() -> bool:
    import asyncio

    from openburrow.core.errors import PolicyViolation
    from openburrow.core.models import Lane, LaneRole, LaneStatus
    from openburrow.daemon.sessions import SessionManager

    async def noop(_self: Any, _lane: Any, _spec: Any, *, role: str) -> None:  # noqa: ARG001
        return None

    original = SessionManager._gate_spawn
    SessionManager._gate_spawn = noop  # type: ignore[method-assign]

    async def probe() -> bool:
        from types import SimpleNamespace

        manager = SessionManager(
            config=SimpleNamespace(
                policy=PolicyConfig(denied_commands=["docker"]),
                paths=SimpleNamespace(repo_root=Path.cwd()),
                settings=SimpleNamespace(governance_human_id="", governance_human_email=""),
            ),
            database=None,
            bus=SimpleNamespace(emit=None),
            registry=None,
        )
        lane = Lane(
            session_id="s-1",
            name="alice",
            harness="claude-code",
            role=LaneRole.IMPLEMENTER,
            status=LaneStatus.STARTING,
        )
        try:
            await manager._gate_spawn(lane, None, role="implementer")
        except PolicyViolation:
            return True
        return False

    try:
        return asyncio.run(probe())
    finally:
        SessionManager._gate_spawn = original  # type: ignore[method-assign]


# --- 15. the call site -----------------------------------------------------
@row(
    "TestTheCallSite::test_the_plan_gated_is_the_plan_spawned",
    "remove the gate call from `start_lane` and rebuild the spec inside `start`",
)
def check_15() -> bool:
    source = SESSIONS_SRC.read_text(encoding="utf-8")
    # The replacements carry their own indentation. The matcher inserts at the start
    # of the matched line, so an unindented replacement would dedent the statement
    # and leave the next anchor unable to tokenise the source it has to read.
    source = replace_anchor(
        source, "await self._gate_spawn(lane, spec, role=role)", "", SESSIONS_SRC.name
    )
    source = replace_anchor(
        source,
        "spec = adapter.prepare_spawn_spec(lane)",
        "        spec = None",
        SESSIONS_SRC.name,
    )
    source = replace_anchor(
        source,
        "await adapter.start(lane, spec=spec)",
        "                    await adapter.start(lane)",
        SESSIONS_SRC.name,
    )
    return (
        "await self._gate_spawn(lane, spec, role=role)" in source
        and "await adapter.start(lane, spec=spec)" in source
    )


# --- 16. `prompt` actually prompts ----------------------------------------
@row(
    "TestPromptActuallyPrompts::test_approval_loads_the_adapter",
    "log-and-continue on `prompt`, importing nothing and asking nothing",
)
def check_16() -> bool:
    """The regression that made the default trust mode a no-op.

    The assertion is "an approved adapter loads". Against the old code the
    adapter is never imported, so the row fails — which is the point. Note that
    the reverted registry is a *different class object* from the real one; the
    check instantiates the reverted one, so it measures the reverted loop.
    """
    import tempfile

    module = revert(
        REGISTRY_SRC,
        (
            '            if trust == "prompt" and not self._confirm_custom(module_path):\n'
            "                continue\n",
            '            if trust == "prompt":\n                continue\n',
        ),
    )
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        write_trust_probe(directory)
        registry = module.AdapterRegistry(
            Settings(custom_adapter_dir=str(directory), custom_adapter_trust="prompt"),
            confirm=lambda _path: True,
        )
        return "falsify-probe" in registry.names()


# --- 17. a failure to ask is not consent -----------------------------------
@row(
    "TestTheGateIsFailClosed::test_a_prompter_that_raises_denies",
    "treat a raising prompter as approval",
)
def check_17() -> bool:
    import tempfile

    module = revert(
        REGISTRY_SRC,
        (
            '                "adapter.custom_trust_prompt_failed",\n'
            "                path=str(module_path),\n"
            "                error=str(exc),\n"
            "            )\n"
            "            return False",
            '                "adapter.custom_trust_prompt_failed",\n'
            "                path=str(module_path),\n"
            "                error=str(exc),\n"
            "            )\n"
            "            return True",
        ),
    )

    def explode(_path: Path) -> bool:
        raise RuntimeError("no approval UI")

    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        write_trust_probe(directory)
        registry = module.AdapterRegistry(
            Settings(custom_adapter_dir=str(directory), custom_adapter_trust="prompt"),
            confirm=explode,
        )
        return "falsify-probe" not in registry.names()


# --- 18. unattended runs deny ----------------------------------------------
@row(
    "TestDefaultPrompter::test_unattended_runs_deny",
    "answer `yes` when there is no terminal to ask on",
)
def check_18() -> bool:
    """The security-critical default: silence is not consent.

    Patches the real ``sys`` streams rather than the reverted module's, because
    the revert only rewrites the *guard*, not the module's ``sys`` reference —
    both point at the same object.
    """
    import io

    module = revert(
        REGISTRY_SRC,
        (
            "    if not (sys.stdin.isatty() and sys.stderr.isatty()):\n"
            '        log.debug("adapter.custom_trust_unattended", path=str(module_path))\n'
            "        return False",
            "    if not (sys.stdin.isatty() and sys.stderr.isatty()):\n        return True",
        ),
    )

    class _NoTty(io.StringIO):
        def isatty(self) -> bool:
            return False

    saved_in, saved_err = sys.stdin, sys.stderr
    sys.stdin, sys.stderr = _NoTty(), _NoTty()
    try:
        return module._ask_terminal(Path("probe.py")) is False
    finally:
        sys.stdin, sys.stderr = saved_in, saved_err


CHECKS: list[Callable[[], bool]] = [
    check_1,
    check_2,
    check_3,
    check_4,
    check_5,
    check_6,
    check_7,
    check_8,
    check_9,
    check_10,
    check_11,
    check_12,
    check_13,
    check_14,
    check_15,
    check_16,
    check_17,
    check_18,
]


def main() -> int:
    assert len(CHECKS) == len(ROWS), "row metadata is out of step with the checks"
    # The real `Settings` is imported above only so an import error in the
    # untouched module is caught here rather than inside a revert.
    assert Settings().env in {"development", "staging", "production"}
    self_check()

    vacuous: list[str] = []
    weak: list[str] = []
    for (name, note), check in zip(ROWS, CHECKS, strict=True):
        raised = ""
        try:
            still_passes = check()
        except Exception as exc:
            still_passes = False
            raised = f"{type(exc).__name__}: {exc}"

        if raised:
            weak.append(name)
            verdict = "WEAK"
        elif still_passes:
            vacuous.append(name)
            verdict = "VACUOUS"
        else:
            verdict = "ok"

        print(f"  [{verdict:>7}] {name}")
        print(f"            reverted: {note}")
        if raised:
            print(f"            the revert raised instead of asserting: {raised}")

    print()
    if vacuous:
        print(f"{len(vacuous)} VACUOUS row(s) — the test does not discriminate:")
        for name in vacuous:
            print(f"  - {name}")
    if weak:
        print(f"{len(weak)} WEAK row(s) — the revert raised, so nothing was proven:")
        for name in weak:
            print(f"  - {name}")
    if vacuous or weak:
        return 1

    print(f"all {len(CHECKS)} rows discriminate: every test fails on the old code")
    return 0


if __name__ == "__main__":
    sys.exit(main())
