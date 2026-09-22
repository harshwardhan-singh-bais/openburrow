"""The pre-execution policy gate.

:class:`PolicyGate` answers one question, deterministically and without
executing anything: *given this command, this path, and this lane role, does
policy allow it, and how risky is it?*

Why this module exists rather than a function in the CLI
--------------------------------------------------------
The logic was previously inlined in ``burrow governance policy test``. It had
four defects, and the fourth is the reason the other three survived: **the gate
lived in a command nobody could call.** A rule evaluator reachable only from a
``--dry-run`` subcommand is not a gate, it is a report about a gate. No amount of
unit-testing the report would have caught that ``config.policy.enforce`` was read
by nothing outside the CLI, or that :class:`openburrow.core.errors.PolicyViolation`
— whose docstring reads "The pre-execution policy gate blocked an action" — was
exported and never raised.

The other three, each of which the old code got backwards:

1. **Risk tier resolution kept the *last* matching tier, not the highest.** The
   loop was::

       for tier, patterns in policy.risk_tiers.items():
           for pattern in patterns:
               if pattern.split()[0] in command:
                   risk_tier = tier
                   break

   The inner ``break`` leaves the *outer* loop running, so the surviving value
   is whichever tier happens to be last in the dict — for the shipped defaults,
   ``low``. ``git push --force`` therefore reported ``low`` risk and
   ``would_require_approval: false``, the exact opposite of the documented
   "Highest matching tier wins".

2. **Matching was a substring test on the pattern's first token.** ``"git" in
   command`` is true of every git command, so every git command matched all four
   tiers; and ``"ls" in "false"`` is true, so ``false`` was classified as a
   low-risk read-only command. Both directions are wrong, and both are invisible
   in review because the expression reads as "does the command start with git".

3. **``default_action`` was conditional on an allowlist existing.** It was
   consulted only inside ``if action == "allow" and policy.allowed_commands:``.
   With the shipped default — an empty ``allowed_commands`` — nothing was ever
   denied, so the knob that looks like the strictest line in the config file did
   nothing at all.

The corrected semantics
-----------------------
* **Highest matching tier wins.** Tiers are ordered ``low < medium < high <
  critical``; every matching pattern is considered and the most severe result is
  returned. A command matching both ``git push`` (high) and ``git push --force``
  (critical) is critical.
* **A pattern matches a whole word phrase, not a substring.** Patterns are
  compiled with non-word guards on both edges, so ``ls`` matches ``ls -la`` but
  not ``false``, ``lsblk`` or ``als``; ``git push`` matches ``sudo git push
  origin`` but not ``git push-x``. Matching is case-insensitive, which is what
  makes the shipped ``DROP TABLE`` pattern fire against ``drop table``.
* **``default_action`` is the verdict whenever no rule matched.** Unconditional.
  Because the shipped value was ``deny`` beside an empty allowlist, and because
  ``allowed_paths`` defaults to ``["."]`` — allow by default, deny by name — the
  shipped ``default_action`` is now ``allow``: denial is expressed through
  ``denied_commands`` and ``denied_paths``, and risk tiers route the dangerous
  commands to a human rather than blocking them. The dangerous combination is
  never silent: :meth:`PolicyGate.diagnose` reports ``default_action: deny`` with
  an empty allowlist, and ``burrow governance policy show`` prints it.
* **A deny rule always beats an allow rule.** The reverse would make the deny
  list advisory.
* **Role overrides can only narrow.** A ``role_overrides`` entry may replace a
  role's allowlist, add denials, and tighten ``default_action`` from ``allow`` to
  ``deny``. It may never loosen: an override cannot turn ``deny`` back into
  ``allow``. This is the invariant the delegation ledger enforces on authority —
  authority originates with a human and can only ever narrow — applied to policy.
* **Denied paths are scanned in the command text, not only in the declared
  path.** A deny rule that only fires when a caller volunteers a path is bypassed
  by not volunteering one, so ``cat ~/.ssh/id_rsa`` is caught with no ``--path``
  at all. ``allowed_paths`` is deliberately *not* applied to command text: it
  constrains the path a caller declares, and applying it to every path-looking
  token would deny ``uv add foo/bar``.
* **Command patterns are literal phrases, not globs.** ``npm *`` does not mean
  "any npm command"; it means the literal text ``npm *``. Patterns containing
  ``*`` or ``?`` are reported by :meth:`PolicyGate.diagnose` rather than silently
  never matching.

What this gate does not do
--------------------------
It gates **what OpenBurrow launches** — the resolved argv, working directory and
environment of a harness — not what that harness's agent later chooses to run
inside its own process. Intercepting mid-session tool calls needs a tool-call
choke point (MCP passthrough or a shell shim) and there is none yet. The
roadmap's claim that "the policy gate can inspect a command before it runs" is
true of spawn, and only of spawn; ``FEATURE_STATUS.md`` says so in those words.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from openburrow.core.config.repo_config import PolicyConfig, RiskTier

__all__ = [
    "APPROVAL_TIERS",
    "RISK_ORDER",
    "Action",
    "PolicyGate",
    "PolicyVerdict",
    "RolePolicy",
]

Action = Literal["allow", "deny"]

#: Least to most severe. The order is the whole point: the previous
#: implementation had no order at all, so "highest tier wins" was unimplementable
#: even in principle.
RISK_ORDER: tuple[RiskTier, ...] = ("low", "medium", "high", "critical")
_RISK_INDEX: dict[str, int] = {tier: index for index, tier in enumerate(RISK_ORDER)}

#: Tiers that pause for a human instead of running unattended. Named here so the
#: gate and the approvals subsystem cannot disagree about which tiers those are.
APPROVAL_TIERS: frozenset[str] = frozenset({"high", "critical"})

#: Characters that make a pattern's edge "inside a word". A pattern may only
#: match where neither neighbour is one of these. ``/`` is deliberately absent:
#: ``rm -rf /`` must match at end of line, and ``\\b`` cannot express that
#: because both ``/`` and end-of-line are non-word.
_WORD_CHARS = "A-Za-z0-9_.-"
_LEADING_GUARD = f"(?<![{_WORD_CHARS}])"
_TRAILING_GUARD = f"(?![{_WORD_CHARS}])"

#: Keys a ``role_overrides`` entry may use. Anything else is a typo, and a typo
#: in a security config that is silently ignored is worse than a crash.
_OVERRIDE_KEYS = frozenset({"allowed_commands", "denied_commands", "default_action"})

#: Runs of characters that could be a path in a command line. Quotes are excluded
#: so ``"secrets/x"`` still yields ``secrets/x``; ``|``, ``;``, ``&`` and the
#: redirection operators are excluded so shell syntax does not glue onto a path.
_PATH_TOKEN = re.compile(r"""[^\s"'`|;&()<>]+""")


@lru_cache(maxsize=2048)
def _pattern_regex(pattern: str) -> re.Pattern[str]:
    """Compile a command pattern to a whole-word, case-insensitive matcher.

    Cached because :meth:`PolicyGate.check` is on the spawn path and a lane
    start should not recompile the same policy on every call.
    """
    return re.compile(f"{_LEADING_GUARD}{re.escape(pattern)}{_TRAILING_GUARD}", re.IGNORECASE)


def matches_command(pattern: str, command: str) -> bool:
    """Whether ``pattern`` occurs in ``command`` as a whole word phrase.

    Public because it is the one rule the whole gate rests on, and it is worth
    being able to test and to show a user directly.
    """
    pattern = (pattern or "").strip()
    if not pattern or not command:
        return False
    return _pattern_regex(pattern).search(command) is not None


def _first_match(patterns: Sequence[str], command: str) -> str | None:
    for pattern in patterns:
        if matches_command(pattern, command):
            return pattern
    return None


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def _normalise(path: str) -> str:
    """Forward slashes, no surrounding space. Policy paths are POSIX-shaped."""
    return str(path).replace("\\", "/").strip()


def _expanded(path: str) -> str:
    """``~`` resolved, normalised. Used to match ``~/.ssh``-style patterns."""
    return _normalise(str(Path(path).expanduser()))


def _absolute(path: str) -> str:
    """An absolute, symlink-resolved form.

    ``resolve`` rather than a lexical join on purpose: a lane worktree reached
    through a symlink is still inside the repository, and a lexical comparison
    would call it outside and deny the spawn.
    """
    return _normalise(str(Path(path).expanduser().resolve()))


def _is_absolute(path: str) -> bool:
    return path.startswith("/") or (len(path) > 1 and path[1] == ":")


def _escapes_repo(path: str) -> bool:
    """Whether the path climbs out of the repository with ``..``."""
    return any(part == ".." for part in path.split("/"))


def _path_candidates(path: str) -> tuple[str, ...]:
    """The forms of one path a pattern is allowed to match against.

    Both the raw and the home-expanded form, and the basename of each. The
    basename is what makes ``.env`` deny ``src/config/.env``: the shipped default
    is a list of *file names*, and a deny list of names that only matched at the
    repository root would be trivially defeated by a subdirectory.
    """
    raw = _normalise(path)
    if not raw:
        return ()
    expanded = _expanded(raw)
    stripped = raw[2:] if raw.startswith("./") else raw

    ordered: dict[str, None] = {}
    for value in (raw, stripped, expanded):
        if not value:
            continue
        ordered.setdefault(value, None)
        base = value.rsplit("/", 1)[-1]
        if base:
            ordered.setdefault(base, None)
    return tuple(ordered)


def _match_one_path(pattern: str, candidate: str) -> bool:
    if not pattern or not candidate:
        return False

    # "." is the shipped ``allowed_paths`` default and means "this repository".
    # It has to be special-cased: as a literal it matches nothing, and a
    # glob-based reading of "." would match nothing either.
    if pattern in {".", "./"}:
        return not _is_absolute(candidate) and not _escapes_repo(candidate)

    body = pattern[2:] if pattern.startswith("./") else pattern
    stem = body.rstrip("/")

    # A pattern naming a directory denies what is inside it, with or without a
    # trailing slash, and denies the directory itself. ``secrets/`` and
    # ``secrets`` must behave the same way, ``~/.ssh`` has to catch
    # ``~/.ssh/id_rsa``, and ``secrets`` has to catch an absolute
    # ``/home/u/repo/secrets`` used as a working directory. An earlier draft only
    # did the trailing-slash form, which left every directory pattern without one
    # matching only a bare relative name.
    if stem and (
        candidate == stem
        or candidate.startswith(f"{stem}/")
        or f"/{stem}/" in f"/{candidate}"
        or candidate.rsplit("/", 1)[-1] == stem
    ):
        return True

    variants = [body]
    if body.startswith("**/"):
        variants.append(body[3:])
    if "/" not in body:
        variants.append(f"**/{body}")

    # ``fnmatch``'s ``*`` matches ``/``, so ``*.pem`` also covers
    # ``certs/a.pem``; the extra variants are for the two shapes it does not:
    # a leading ``**/`` (whose literal slash would require a directory) and a
    # bare name that should match at any depth.
    return any(fnmatch.fnmatchcase(candidate, variant) for variant in variants)


def matches_path(pattern: str, path: str) -> bool:
    """Whether ``path`` falls under a policy path pattern.

    Patterns are matched against the raw path, the home-expanded path, and each
    one's basename — see :func:`_path_candidates` for why all three are needed.
    """
    target = _normalise(pattern)
    if not target or not path:
        return False

    # "." means "inside this repository" and is judged against the whole declared
    # path, never its basename. The candidate expansion below exists for
    # name-based deny rules like ".env", and a basename is by construction
    # relative and never escapes — so running "." through it answered "inside the
    # repo" for both "/etc/passwd" and "../outside/x.py".
    if target in {".", "./"}:
        return _match_one_path(target, _normalise(path))

    expanded = _expanded(target)
    for candidate in _path_candidates(path):
        if _match_one_path(target, candidate) or _match_one_path(expanded, candidate):
            return True
    return False


def _path_tokens(command: str) -> tuple[str, ...]:
    """Path-looking tokens in a command line.

    Only tokens containing a separator or a leading ``~``, because scanning every
    token would make ``denied_paths`` fire on words that merely resemble a name.
    ``--file=secrets/x`` also yields ``secrets/x``: the value of an ``=`` flag is
    where a path usually hides.
    """
    if not command:
        return ()
    found: dict[str, None] = {}
    for match in _PATH_TOKEN.finditer(command):
        token = match.group(0).strip(",;:")
        if not token:
            continue
        if "/" in token or "\\" in token or token.startswith("~"):
            found.setdefault(token, None)
            _, _, tail = token.partition("=")
            if tail and ("/" in tail or tail.startswith("~")):
                found.setdefault(tail, None)
    return tuple(found)


# ---------------------------------------------------------------------------
# role overrides
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RolePolicy:
    """A role's effective rules, after the base policy has been narrowed."""

    allowed_commands: tuple[str, ...]
    denied_commands: tuple[str, ...]
    default_action: Action


def _as_patterns(value: Any) -> tuple[str, ...] | None:
    """Coerce a rule list, or return ``None`` if the value is not a list."""
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return None


def resolve_role(policy: PolicyConfig, role: str | None) -> RolePolicy:
    """Apply a role's override to the base policy, narrowing only.

    An override may *replace* the allowlist (so a reviewer lane can be restricted
    to reads), *add* denials, and *tighten* ``default_action`` to ``deny``. It may
    not loosen: ``default_action: allow`` on a base that denies is dropped,
    because a role that could widen its own authority would make the base policy
    a suggestion. Malformed entries are ignored here and reported by
    :meth:`PolicyGate.diagnose`, so a typo is loud rather than silent.
    """
    allowed = tuple(policy.allowed_commands)
    denied = tuple(policy.denied_commands)
    default: Action = policy.default_action

    if not role:
        return RolePolicy(allowed, denied, default)
    # ``PolicyConfig`` validates that every value here is a mapping, so there is
    # no shape guard: an entry that is not a mapping cannot be constructed.
    override = policy.role_overrides.get(role)
    if not override:
        return RolePolicy(allowed, denied, default)

    replaced = (
        _as_patterns(override.get("allowed_commands")) if "allowed_commands" in override else None
    )
    if replaced is not None:
        allowed = replaced

    if "denied_commands" in override:
        added = _as_patterns(override["denied_commands"]) or ()
        denied = tuple(dict.fromkeys([*denied, *added]))

    if override.get("default_action") == "deny":
        default = "deny"

    return RolePolicy(allowed, denied, default)


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PolicyVerdict:
    """What the gate decided, and the rule that decided it."""

    command: str
    action: Action
    risk_tier: RiskTier
    matched_rule: str
    matched_kind: str
    would_require_approval: bool
    path: str | None = None
    role: str | None = None
    denied_paths: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.action == "allow"

    @property
    def enforced(self) -> bool:
        """Whether this verdict is a block or a note.

        A verdict is only enforced when ``enforce`` is true *and* the action is
        a denial. Reporting that distinction is the difference between "policy
        says no" and "policy says no and nothing acts on it".
        """
        return self.action == "deny"

    def reason(self) -> str:
        if self.action == "allow":
            return f"allowed by {self.matched_rule}"
        if self.denied_paths:
            return f"denied by {self.matched_rule} (paths: {', '.join(self.denied_paths)})"
        return f"denied by {self.matched_rule}"

    def as_dict(self) -> dict[str, Any]:
        """The wire shape. Supersets the keys the CLI emitted before the gate
        moved out of the CLI, so existing JSON consumers keep working."""
        return {
            "command": self.command,
            "path": self.path,
            "role": self.role,
            "action": self.action,
            "risk_tier": self.risk_tier,
            "matched_rule": self.matched_rule,
            "matched_kind": self.matched_kind,
            "would_require_approval": self.would_require_approval,
            "denied_paths": list(self.denied_paths),
            "diagnostics": list(self.diagnostics),
        }


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------
class PolicyGate:
    """Evaluate a command against :class:`PolicyConfig`. Pure; runs nothing.

    Construct from an already-merged policy — ``config.policy`` applies the env
    layer, so passing a raw ``PolicyConfig`` from YAML would quietly drop
    ``OPENBURROW_POLICY_*`` overrides.

    ``repo_root`` is optional and only affects the ``allowed_paths`` check. Policy
    paths are repository-relative (``allowed_paths`` defaults to ``["."]``), while
    a spawn plan's working directory is absolute, so without the root every lane
    start would be denied by a rule that means "keep it in the repo". Given the
    root, an absolute path under it is converted before the check; an absolute
    path outside it stays absolute and is denied, which is the correct answer.
    """

    def __init__(
        self,
        policy: PolicyConfig,
        *,
        repo_root: str | Path | None = None,
    ) -> None:
        self.policy = policy
        self.enforce = bool(policy.enforce)
        self.repo_root = _absolute(str(repo_root)) if repo_root else None

    def _repo_relative(self, path: str) -> str:
        """Express ``path`` relative to the repository, when it is inside it."""
        if not path or self.repo_root is None:
            return path
        normalised = _normalise(path)
        absolute = _absolute(path)
        root = _normalise(self.repo_root)
        if absolute == root:
            return "."
        prefix = root if root.endswith("/") else f"{root}/"
        if absolute.startswith(prefix):
            return absolute[len(prefix) :]
        # Outside the repository, or a relative path already. Returned unchanged
        # so an absolute path outside the repo fails the allowed_paths check
        # rather than being silently reinterpreted.
        return normalised if not _is_absolute(normalised) else absolute

    # --- entry points ------------------------------------------------------
    def check(self, command: str, *, path: str = "", role: str | None = None) -> PolicyVerdict:
        """Evaluate one command, optionally against a declared path and role."""
        command = (command or "").strip()
        role_name = (role or "").strip() or None
        rules = resolve_role(self.policy, role_name)
        declared = (path or "").strip()
        diagnostics = self.diagnose()

        # 1. A deny rule beats an allow rule. The reverse would make the deny
        #    list advisory, which is the failure mode a deny list exists to
        #    avoid.
        denied_rule = _first_match(rules.denied_commands, command)
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

        # 2. Denied paths, from the declared path and from the command text.
        #    A rule that only fires when a caller volunteers a path is bypassed
        #    by not volunteering one.
        hits = self._denied_path_hits(command, declared)
        if hits:
            return self._verdict(
                command,
                "deny",
                f"denied_paths: {hits[0]}",
                "denied_paths",
                declared,
                role_name,
                diagnostics,
                denied_paths=hits,
            )

        # 3. A declared path must sit inside an allowed path. Only the declared
        #    path: applying this to path-looking tokens in the command would
        #    deny `uv add foo/bar`, which is not a filesystem access at all.
        #    Checked against the repository-relative form, because policy paths
        #    are repo-relative and a spawn plan's cwd is absolute.
        if declared and self.policy.allowed_paths:
            relative = self._repo_relative(declared)
            if not any(matches_path(rule, relative) for rule in self.policy.allowed_paths):
                return self._verdict(
                    command,
                    "deny",
                    f"allowed_paths: no match for {declared!r}",
                    "allowed_paths",
                    declared,
                    role_name,
                    diagnostics,
                )

        # 4. Allowlist, then the default. `default_action` applies whenever no
        #    allow rule matched — unconditionally, which is what makes it a
        #    setting rather than a comment.
        allowed_rule = (
            _first_match(rules.allowed_commands, command) if rules.allowed_commands else None
        )
        if allowed_rule is not None:
            action: Action = "allow"
            matched_rule = f"allowed_commands: {allowed_rule}"
            matched_kind = "allowed_commands"
        else:
            action = rules.default_action
            matched_rule = f"default_action={rules.default_action}"
            matched_kind = "default_action"

        risk = self._risk_tier(command)
        return self._verdict(
            command,
            action,
            matched_rule,
            matched_kind,
            declared,
            role_name,
            diagnostics,
            risk=risk,
        )

    def check_argv(
        self, argv: Sequence[str], *, cwd: str = "", role: str | None = None
    ) -> PolicyVerdict:
        """Evaluate a spawn plan's argv and working directory.

        The argv is joined with single spaces rather than shell-quoted: this is
        text for a matcher to read, not a command for a shell to run, and quoting
        would put ``'`` characters between a flag and its neighbours for no gain.
        """
        command = " ".join(str(part) for part in argv if str(part))
        return self.check(command, path=cwd, role=role)

    def require(self, command: str, *, path: str = "", role: str | None = None) -> PolicyVerdict:
        """Like :meth:`check`, but raise when the verdict denies and enforce is on.

        The single enforcement helper, so that "denied" and "actually stopped"
        cannot drift apart across call sites.
        """
        verdict = self.check(command, path=path, role=role)
        if verdict.action == "deny" and self.enforce:
            from openburrow.core.errors import PolicyViolation

            raise PolicyViolation(
                f"policy denied command: {verdict.reason()}",
                hint=(
                    "Run `burrow governance policy test "
                    f"{command!r}` to see which rule matched, or set "
                    "policy.enforce=false to make the gate advisory."
                ),
                context={
                    "command": command,
                    "path": path or None,
                    "role": role,
                    "matched_rule": verdict.matched_rule,
                    "risk_tier": verdict.risk_tier,
                },
            )
        return verdict

    # --- internals ---------------------------------------------------------
    def _verdict(
        self,
        command: str,
        action: Action,
        matched_rule: str,
        matched_kind: str,
        path: str,
        role: str | None,
        diagnostics: tuple[str, ...],
        *,
        risk: RiskTier | None = None,
        denied_paths: tuple[str, ...] = (),
    ) -> PolicyVerdict:
        risk_tier = risk if risk is not None else self._risk_tier(command)
        return PolicyVerdict(
            command=command,
            action=action,
            risk_tier=risk_tier,
            matched_rule=matched_rule,
            matched_kind=matched_kind,
            # Only meaningful if it would run at all: a denied command does not
            # pause for approval, it does not run.
            would_require_approval=action == "allow" and risk_tier in APPROVAL_TIERS,
            path=path or None,
            role=role,
            denied_paths=denied_paths,
            diagnostics=diagnostics,
        )

    def _risk_tier(self, command: str) -> RiskTier:
        """The most severe tier whose patterns match.

        The inner ``break`` is correct *here* and was the bug before: it stops
        scanning further patterns within a tier once one has matched, and the
        outer loop keeps going so every tier still gets a chance. The previous
        implementation had this same shape but assigned instead of maximising, so
        the last tier in the dict won.
        """
        best: RiskTier = "low"
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
        return best

    def _denied_path_hits(self, command: str, declared: str) -> tuple[str, ...]:
        tokens = _path_tokens(command)
        hits: dict[str, None] = {}
        for rule in self.policy.denied_paths:
            if declared and matches_path(rule, declared):
                hits.setdefault(rule, None)
                continue
            if any(matches_path(rule, token) for token in tokens):
                hits.setdefault(rule, None)
        return tuple(hits)

    # --- configuration health ---------------------------------------------
    def diagnose(self) -> tuple[str, ...]:
        """Misconfigurations that would otherwise be silent.

        A policy knob that is set and does nothing is the exact class of defect
        this stage exists to remove, so the gate reports the ones it can see
        rather than waiting for someone to notice a session behaving oddly.
        """
        notes: list[str] = []
        policy = self.policy

        if policy.default_action == "deny" and not policy.allowed_commands:
            notes.append(
                "default_action is 'deny' and allowed_commands is empty, so every "
                "command that no deny rule names is denied. Intended for a strict "
                "allowlist posture, and only then."
            )
        if not policy.allowed_commands and policy.denied_commands:
            notes.append(
                "allowed_commands is empty, so the only command-level rules are "
                "denied_commands and the risk tiers."
            )
        if not policy.risk_tiers:
            notes.append(
                "risk_tiers is empty, so every command is low risk and nothing will "
                "ever require approval."
            )
        else:
            empty = sorted(tier for tier, patterns in policy.risk_tiers.items() if not patterns)
            if empty:
                notes.append(f"risk_tiers has empty tiers: {', '.join(empty)}.")
        if not policy.allowed_paths:
            notes.append(
                "allowed_paths is empty, so a declared path is unconstrained. Set it "
                "to ['.'] to keep declared paths inside the repository."
            )

        for group, patterns in (
            ("allowed_commands", policy.allowed_commands),
            ("denied_commands", policy.denied_commands),
            ("denied_paths", policy.denied_paths),
        ):
            for pattern in patterns:
                if not str(pattern).strip():
                    notes.append(f"{group} contains an empty pattern, which matches nothing.")
                elif any(char in str(pattern) for char in "*?") and group != "denied_paths":
                    notes.append(
                        f"{group} pattern {pattern!r} contains a glob character, but command "
                        "patterns are literal phrases and globs are not expanded."
                    )

        for role, override in policy.role_overrides.items():
            notes.extend(self._diagnose_override(role, override))
        return tuple(notes)

    def _diagnose_override(self, role: str, override: Any) -> list[str]:
        """Report an override that is shaped wrong.

        There is deliberately no branch for "the override is not a mapping":
        ``role_overrides`` is typed ``dict[str, dict[str, Any]]`` and
        ``PolicyConfig`` is validated by pydantic, so a list value is rejected at
        construction with a message that names the field. A diagnostic for it
        would be a branch no input can reach. The inner values *are* unvalidated,
        which is why the key and type checks below are reachable and belong here.
        """
        notes: list[str] = []
        unknown = sorted(set(override) - _OVERRIDE_KEYS)
        if unknown:
            notes.append(
                f"role_overrides[{role!r}] has unknown key(s) {', '.join(unknown)}; only "
                f"{', '.join(sorted(_OVERRIDE_KEYS))} are read."
            )
        for key in ("allowed_commands", "denied_commands"):
            if key in override and _as_patterns(override[key]) is None:
                notes.append(
                    f"role_overrides[{role!r}].{key} is a "
                    f"{type(override[key]).__name__}, not a list; the entry is ignored."
                )
        action = override.get("default_action")
        if action is not None and action not in {"allow", "deny"}:
            notes.append(
                f"role_overrides[{role!r}].default_action is {action!r}; expected "
                "'allow' or 'deny'."
            )
        if action == "allow" and self.policy.default_action == "deny":
            notes.append(
                f"role_overrides[{role!r}].default_action='allow' would loosen the base "
                "'deny'. Role overrides may only narrow, so it is ignored."
            )
        return notes
