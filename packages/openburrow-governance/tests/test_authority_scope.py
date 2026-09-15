"""The authority model, tested without a database.

These are the tests for the project's central claim:

    **Authority originates with a human and can only ever narrow.**

Everything else in OpenBurrow is a mechanism; this is the invariant the
mechanisms exist to protect. It is therefore the thing that most deserves tests
that cannot be skipped, which is why nothing here touches I/O — a test that
requires a database to check that a wildcard cannot defeat a denial is a test
that stops running the first time the database is unavailable.

Two properties get the most attention, because they are the ones a plausible
implementation gets wrong:

* **Fail-closed.** An empty scope must permit nothing. The tempting alternative
  — "no capabilities listed means unrestricted" — is how a governance layer
  becomes decorative.
* **Denial beats grant.** ``denials`` are consulted *before* ``capabilities``, so
  ``denials=["exec:git push"]`` survives a scope that contains ``exec:*`` or even
  ``*``. If the order were reversed, a wildcard would silently swallow every
  prohibition, and the prohibition is usually the whole point.
"""

from __future__ import annotations

import pytest

from openburrow.core.models import AuthorityScope
from openburrow.governance import default_scope_for_role

#: Two markers on purpose. ``unit`` because these are fast and touch no I/O, so
#: they belong in the quick suite; ``governance`` because CI runs a separate
#: mandatory gate over this marker, and a gate that selects nothing is a gate
#: that always passes. The duplication is the point — the authority model is the
#: one component whose failure is a release blocker.
pytestmark = [pytest.mark.unit, pytest.mark.governance]


# ---------------------------------------------------------------------------
# allows() — the fail-closed check
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_an_empty_scope_permits_nothing(self) -> None:
        scope = AuthorityScope()
        assert scope.allows("read:src/main.py") is False
        assert scope.allows("*") is False
        assert scope.allows("") is False

    def test_a_scope_with_only_denials_permits_nothing(self) -> None:
        scope = AuthorityScope(denials=["exec:git push"])
        assert scope.allows("read:src/main.py") is False

    def test_an_empty_capability_is_not_granted_by_a_wildcard(self) -> None:
        # `*` must not become a universal key that also opens the empty string.
        # It is a small thing, but `allows("")` returning True would make an
        # unset capability indistinguishable from a granted one at every call
        # site that forgets to validate its input.
        scope = AuthorityScope(capabilities=["*"])
        assert scope.allows("") is False


class TestMatching:
    @pytest.mark.parametrize(
        ("pattern", "capability", "expected"),
        [
            # Exact.
            ("read:src/main.py", "read:src/main.py", True),
            ("read:src/main.py", "read:src/other.py", False),
            # Verb wildcard.
            ("read:*", "read:anything/at/all", True),
            ("read:*", "write:anything", False),
            # Path wildcard.
            ("read:src/**", "read:src/api/routes.py", True),
            ("read:src/**", "read:tests/test_api.py", False),
            # Bare star.
            ("*", "exec:git push", True),
            # A prefix that is not a wildcard is not a prefix match.
            ("read:src", "read:src/api.py", False),
        ],
    )
    def test_pattern_matching(self, pattern: str, capability: str, expected: bool) -> None:
        scope = AuthorityScope(capabilities=[pattern])
        assert scope.allows(capability) is expected

    def test_any_matching_grant_is_enough(self) -> None:
        scope = AuthorityScope(capabilities=["read:*", "write:src/**"])
        assert scope.allows("write:src/api/routes.py") is True
        assert scope.allows("read:docs/README.md") is True


class TestDenialBeatsGrant:
    def test_a_denial_defeats_an_exact_grant(self) -> None:
        scope = AuthorityScope(capabilities=["exec:git push"], denials=["exec:git push"])
        assert scope.allows("exec:git push") is False

    def test_a_denial_defeats_a_verb_wildcard(self) -> None:
        scope = AuthorityScope(capabilities=["exec:*"], denials=["exec:git push"])
        assert scope.allows("exec:git push") is False
        # ...and only that one. A denial is not a blanket revocation of the verb.
        assert scope.allows("exec:git pull") is True

    def test_a_denial_defeats_the_bare_star(self) -> None:
        # This is the case the ordering exists for. `*` is the most permissive
        # grant the model can express, and it must still lose to an explicit
        # prohibition — otherwise "deny this one thing" is unexpressible.
        scope = AuthorityScope(capabilities=["*"], denials=["exec:git push"])
        assert scope.allows("exec:git push") is False
        assert scope.allows("write:src/api/routes.py") is True

    def test_a_path_denial_defeats_a_path_wildcard(self) -> None:
        scope = AuthorityScope(capabilities=["write:src/**"], denials=["write:src/secrets/**"])
        assert scope.allows("write:src/secrets/key.pem") is False
        assert scope.allows("write:src/api/routes.py") is True

    def test_a_denial_can_use_a_wildcard_too(self) -> None:
        scope = AuthorityScope(capabilities=["*"], denials=["exec:*"])
        assert scope.allows("exec:git push") is False
        assert scope.allows("read:src/main.py") is True


# ---------------------------------------------------------------------------
# narrow() — the only sanctioned way to hand off authority
# ---------------------------------------------------------------------------


class TestNarrowing:
    def test_narrowing_cannot_widen(self) -> None:
        # The single most important property in this file. A delegation that
        # asks for more than its delegator holds must receive only what the
        # delegator had.
        parent = AuthorityScope(capabilities=["read:src/**"])
        narrowed = parent.narrow(["read:src/**", "write:src/**", "exec:git push"])

        assert narrowed.capabilities == ["read:src/**"]
        assert narrowed.allows("write:src/api.py") is False
        assert narrowed.allows("exec:git push") is False

    def test_narrowing_to_a_subset_keeps_the_subset(self) -> None:
        parent = AuthorityScope(capabilities=["read:*", "write:src/**"])
        narrowed = parent.narrow(["write:src/api/**"])
        assert narrowed.capabilities == ["write:src/api/**"]

    def test_narrowing_to_nothing_is_allowed_and_yields_nothing(self) -> None:
        # A delegation with no capability is a legitimate thing to create — it
        # is how a lane is given accountability without authority. It must not
        # fall back to the parent's scope.
        parent = AuthorityScope(capabilities=["read:*"])
        narrowed = parent.narrow([])
        assert narrowed.capabilities == []
        assert narrowed.allows("read:anything") is False

    def test_a_denied_capability_cannot_be_re_requested(self) -> None:
        parent = AuthorityScope(capabilities=["exec:*"], denials=["exec:git push"])
        narrowed = parent.narrow(["exec:git push"])
        assert narrowed.capabilities == []
        assert narrowed.allows("exec:git push") is False

    def test_parent_denials_are_sticky(self) -> None:
        # A delegation must not be able to shed a prohibition it inherited.
        # Dropping denials on the way down would make a two-hop chain a way to
        # launder authority: grant narrowly at the root, then re-delegate
        # without the denial.
        parent = AuthorityScope(capabilities=["*"], denials=["exec:git push"])
        narrowed = parent.narrow(["read:*", "exec:*"])
        assert "exec:git push" in narrowed.denials
        assert narrowed.allows("exec:git push") is False

    def test_narrowing_can_add_denials(self) -> None:
        parent = AuthorityScope(capabilities=["write:*"])
        narrowed = parent.narrow(["write:src/**"], denials=["write:src/secrets/**"])
        assert narrowed.allows("write:src/api.py") is True
        assert narrowed.allows("write:src/secrets/key.pem") is False

    def test_denials_are_deduplicated_and_ordered(self) -> None:
        # Sorted and de-duplicated so that a chain of five hops does not produce
        # a denial list with the same entry five times, which would make the
        # audit rendering unreadable for no gain.
        parent = AuthorityScope(denials=["exec:git push", "exec:rm"])
        narrowed = parent.narrow([], denials=["exec:git push", "write:/etc"])
        assert narrowed.denials == ["exec:git push", "exec:rm", "write:/etc"]

    def test_depth_decrements_per_hop(self) -> None:
        root = AuthorityScope(capabilities=["*"], max_depth=3)
        hop1 = root.narrow(["*"])
        hop2 = hop1.narrow(["*"])
        hop3 = hop2.narrow(["*"])

        assert root.max_depth == 3
        assert hop1.max_depth == 2
        assert hop2.max_depth == 1
        assert hop3.max_depth == 0

    def test_depth_floors_at_zero(self) -> None:
        # Not `-1`. A negative ceiling would compare as "deeper than allowed" in
        # any arithmetic that used it, so the floor is what keeps an exhausted
        # scope meaning "no further delegation" rather than something undefined.
        scope = AuthorityScope(capabilities=["*"], max_depth=0)
        assert scope.narrow(["*"]).max_depth == 0

    def test_narrowing_records_its_origin(self) -> None:
        # `derived_from` is what makes a chain reconstructable from the scope
        # alone, without walking the delegation table.
        parent = AuthorityScope(capabilities=["*"], max_depth=2)
        narrowed = parent.narrow(["*"])
        assert narrowed.derived_from == parent.id
        assert narrowed.derived_from != ""

    def test_narrowing_a_wildcard_to_a_wildcard_keeps_the_wildcard(self) -> None:
        # `*` allows `*`, so the intersection is `*`. Worth pinning because a
        # naive implementation that special-cased `*` would return `[]` here and
        # break every coordinator lane.
        parent = AuthorityScope(capabilities=["*"], max_depth=2)
        assert parent.narrow(["*"]).capabilities == ["*"]


# ---------------------------------------------------------------------------
# default_scope_for_role
# ---------------------------------------------------------------------------


class TestDefaultScopeForRole:
    def test_each_known_role_gets_its_scope(self) -> None:
        assert default_scope_for_role("reviewer").allows("read:src/main.py")
        assert default_scope_for_role("reviewer").allows("propose:change")
        # A reviewer reviews. It does not write.
        assert default_scope_for_role("reviewer").allows("write:src/main.py") is False

        assert default_scope_for_role("implementer").allows("write:src/main.py")
        assert default_scope_for_role("implementer").allows("exec:test")
        # An implementer does not delegate. That is the coordinator's job.
        assert default_scope_for_role("implementer").allows("delegate:lane") is False

        assert default_scope_for_role("coordinator").allows("delegate:*")
        assert default_scope_for_role("coordinator").allows("write:src/main.py") is False

        assert default_scope_for_role("observer").allows("read:anything")
        assert default_scope_for_role("observer").allows("write:anything") is False

    def test_an_unknown_role_gets_the_most_restrictive_default(self) -> None:
        # Fail closed. An unrecognised role is a typo or a newer template, and
        # neither is a reason to hand out write access. Read-only is the right
        # answer to "I do not know what this is".
        unknown = default_scope_for_role("not-a-role")
        assert unknown.allows("read:anything") is True
        assert unknown.allows("write:anything") is False
        assert unknown.allows("exec:anything") is False

    def test_defaults_carry_a_depth_ceiling(self) -> None:
        # A default scope with an unbounded depth would let a lane bootstrap an
        # arbitrarily long chain, which is the accumulation the depth cap exists
        # to prevent.
        for role in ("reviewer", "implementer", "coordinator", "observer", "unknown"):
            assert default_scope_for_role(role).max_depth == 1

    def test_defaults_are_not_shared_mutable_state(self) -> None:
        # Two calls must not hand out the same list object. A lane that mutated
        # its scope would otherwise silently widen every other lane of that role.
        first = default_scope_for_role("implementer")
        second = default_scope_for_role("implementer")
        assert first.capabilities is not second.capabilities
        first.capabilities.append("exec:rm -rf /")
        assert "exec:rm -rf /" not in second.capabilities
