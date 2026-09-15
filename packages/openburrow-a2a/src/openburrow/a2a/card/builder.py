"""Agent Card generation.

The central claim of OpenBurrow is that *any* harness can look like a standard
A2A peer from the outside, regardless of what it natively speaks. This module is
where that claim is made true.

An adapter does not write an Agent Card by hand. It declares what it can do in
:class:`HarnessCapabilities`, and :func:`build_agent_card` produces a spec-shaped
card from that declaration. The card is served at
``/.well-known/agent-card.json`` on the lane's local port, so a third-party A2A
client can discover the lane exactly as it would discover any other agent.

Two OpenBurrow-specific extensions ride along in ``metadata``:

* ``openburrow:capabilityFlags`` — what the harness can actually do (structured
  output, resumability, MCP tools). Adapters and the Radar read this.
* ``openburrow:authorityScope`` — the bounded authority the lane operates under.
  The governance layer's capability-card verification (item 186) compares this
  against observed behaviour.
"""

from __future__ import annotations

from typing import Any

from openburrow.core.models import Lane, LaneRole
from openburrow.core.version import A2A_PROTOCOL_VERSION, __version__

#: Input/output modes the A2A spec recognises that we actually use.
DEFAULT_INPUT_MODES: tuple[str, ...] = ("text/plain", "application/json")
DEFAULT_OUTPUT_MODES: tuple[str, ...] = ("text/plain", "application/json")


class HarnessCapabilities:
    """What an adapter can truthfully claim about its harness.

    Every field is a *statement about behaviour*, and the governance layer
    spot-checks them. Claiming a capability the harness does not have is the
    exact failure mode :class:`~openburrow.core.models.GovernanceFlag` catches,
    so adapters are expected to be conservative here.
    """

    __slots__ = (
        "input_modes",
        "mcp_tools",
        "native_a2a",
        "output_modes",
        "resumable",
        "streaming",
        "structured_output",
        "supports_file_context",
        "supports_interrupt",
    )

    def __init__(
        self,
        *,
        structured_output: bool = False,
        streaming: bool = True,
        resumable: bool = False,
        mcp_tools: bool = False,
        supports_interrupt: bool = False,
        supports_file_context: bool = True,
        native_a2a: bool = False,
        output_modes: tuple[str, ...] = DEFAULT_OUTPUT_MODES,
        input_modes: tuple[str, ...] = DEFAULT_INPUT_MODES,
    ) -> None:
        self.structured_output = structured_output
        self.streaming = streaming
        self.resumable = resumable
        self.mcp_tools = mcp_tools
        self.supports_interrupt = supports_interrupt
        self.supports_file_context = supports_file_context
        self.native_a2a = native_a2a
        self.output_modes = output_modes
        self.input_modes = input_modes

    def to_dict(self) -> dict[str, Any]:
        return {
            "structuredOutput": self.structured_output,
            "streaming": self.streaming,
            "resumable": self.resumable,
            "mcpTools": self.mcp_tools,
            "supportsInterrupt": self.supports_interrupt,
            "supportsFileContext": self.supports_file_context,
            "nativeA2A": self.native_a2a,
        }

    def to_skill_tags(self) -> list[str]:
        """Capabilities rendered as skill tags, for coarse discovery."""
        tags: list[str] = []
        if self.structured_output:
            tags.append("structured-output")
        if self.resumable:
            tags.append("resumable")
        if self.mcp_tools:
            tags.append("mcp-native")
        if self.supports_interrupt:
            tags.append("interruptible")
        return tags


class SkillSpec:
    """One declared skill, in A2A's shape."""

    __slots__ = ("description", "examples", "id", "input_modes", "name", "output_modes", "tags")

    def __init__(
        self,
        *,
        skill_id: str,
        name: str,
        description: str = "",
        tags: list[str] | None = None,
        examples: list[str] | None = None,
        input_modes: tuple[str, ...] = DEFAULT_INPUT_MODES,
        output_modes: tuple[str, ...] = DEFAULT_OUTPUT_MODES,
    ) -> None:
        self.id = skill_id
        self.name = name
        self.description = description
        self.tags = tags or []
        self.examples = examples or []
        self.input_modes = input_modes
        self.output_modes = output_modes

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "tags": self.tags,
            "inputModes": list(self.input_modes),
            "outputModes": list(self.output_modes),
        }
        if self.description:
            payload["description"] = self.description
        if self.examples:
            payload["examples"] = self.examples
        return payload


#: Skills every lane gets, because every lane can be asked to do these.
BASE_SKILLS: tuple[SkillSpec, ...] = (
    SkillSpec(
        skill_id="implement",
        name="Implement a change",
        description="Make a code change in the lane's worktree and report the diff.",
        tags=["code", "write"],
        examples=["Add pagination to GET /users", "Refactor the auth middleware"],
    ),
    SkillSpec(
        skill_id="review",
        name="Review a proposed change",
        description=(
            "Evaluate another lane's diff and respond with an ACP performative: "
            "accept, reject, or counter with a specific requested change."
        ),
        tags=["review", "negotiate"],
        examples=["Review this diff for the signature change", "Would this break callers?"],
    ),
    SkillSpec(
        skill_id="explain",
        name="Explain code or intent",
        description="Describe what a piece of code does or what a planned change will touch.",
        tags=["read", "inform"],
        examples=["What does this module own?", "Which callers hit this function?"],
    ),
    SkillSpec(
        skill_id="plan",
        name="Produce a plan",
        description="Break a task description into ordered steps with dependencies.",
        tags=["plan"],
        examples=["Plan the migration to the new client", "Break down the auth rewrite"],
    ),
)

#: Extra skills a lane gets based on its role.
ROLE_SKILLS: dict[LaneRole, tuple[SkillSpec, ...]] = {
    LaneRole.REVIEWER: (
        SkillSpec(
            skill_id="approve",
            name="Approve a change",
            description="Formally accept a proposal, allowing the proposing lane to proceed.",
            tags=["review", "governance"],
        ),
    ),
    LaneRole.COORDINATOR: (
        SkillSpec(
            skill_id="delegate",
            name="Delegate work",
            description="Assign a task to another lane, with an explicit bounded authority scope.",
            tags=["coordinate", "governance"],
        ),
        SkillSpec(
            skill_id="claim",
            name="Adjudicate claims",
            description="Resolve two lanes' competing claims over the same resource.",
            tags=["coordinate"],
        ),
    ),
    LaneRole.OBSERVER: (),
    LaneRole.IMPLEMENTER: (),
    LaneRole.CUSTOM: (),
}


def build_agent_card(
    lane: Lane,
    *,
    capabilities: HarnessCapabilities | None = None,
    skills: list[SkillSpec] | None = None,
    base_url: str = "",
    provider_name: str = "",
    provider_url: str = "",
    documentation_url: str = "",
) -> dict[str, Any]:
    """Produce an A2A-spec Agent Card for one lane.

    ``base_url`` is the lane's own local endpoint (``http://127.0.0.1:7401``).
    The card is what a peer fetches to discover what this lane can do; keeping it
    generated rather than hand-written is what stops an adapter's claims from
    drifting away from its behaviour.
    """
    caps = capabilities or HarnessCapabilities()

    declared: list[SkillSpec] = list(BASE_SKILLS)
    declared.extend(ROLE_SKILLS.get(LaneRole(str(lane.role)), ()))
    if skills:
        declared.extend(skills)

    # Deduplicate by skill id, last definition wins (a caller-supplied skill
    # overriding a base one is the useful direction).
    by_id: dict[str, SkillSpec] = {}
    for skill in declared:
        by_id[skill.id] = skill

    endpoint = base_url or lane.a2a_endpoint or lane.agent_card_url.rsplit("/.well-known", 1)[0]

    card: dict[str, Any] = {
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "name": lane.name,
        "description": _describe(lane),
        "url": endpoint,
        "preferredTransport": "JSONRPC",
        "additionalInterfaces": [
            {"url": endpoint, "transport": "JSONRPC"},
            {"url": f"{endpoint}/stream" if endpoint else "", "transport": "SSE"},
        ],
        "version": __version__,
        "capabilities": {
            "streaming": caps.streaming,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": list(caps.input_modes),
        "defaultOutputModes": list(caps.output_modes),
        "skills": [skill.to_dict() for skill in by_id.values()],
        "supportsAuthenticatedExtendedCard": False,
    }

    if provider_name:
        card["provider"] = {"organization": provider_name, "url": provider_url}

    if documentation_url:
        card["documentationUrl"] = documentation_url

    # --- OpenBurrow extensions -------------------------------------------
    card["metadata"] = {
        "openburrow:laneId": lane.id,
        "openburrow:sessionId": lane.session_id,
        "openburrow:harness": lane.harness,
        "openburrow:role": str(lane.role),
        "openburrow:owner": lane.owner,
        "openburrow:trustBoundary": lane.trust_boundary,
        "openburrow:capabilityFlags": caps.to_dict(),
        "openburrow:authorityScope": list(lane.authority_scope),
        "openburrow:transferable": lane.transferable,
        "openburrow:canDelegate": lane.can_delegate,
        "openburrow:worktree": lane.worktree_path,
        "openburrow:branch": lane.branch,
    }

    return card


def _describe(lane: Lane) -> str:
    role = str(lane.role)
    harness = lane.harness or "unknown harness"
    owner = f", operated by {lane.owner}" if lane.owner else ""
    return (
        f"OpenBurrow lane '{lane.name}' running {harness} as a {role}{owner}. "
        f"Participates in session {lane.session_id} as an A2A peer."
    )


def capability_flags_from_card(card: dict[str, Any]) -> dict[str, Any]:
    """Read the OpenBurrow capability flags back out of a received Agent Card."""
    metadata = card.get("metadata") or {}
    flags = metadata.get("openburrow:capabilityFlags")
    return dict(flags) if isinstance(flags, dict) else {}


def authority_scope_from_card(card: dict[str, Any]) -> list[str]:
    """Read a peer's declared authority scope. Used by the delegation validator."""
    metadata = card.get("metadata") or {}
    scope = metadata.get("openburrow:authorityScope")
    return list(scope) if isinstance(scope, list) else []


def declared_skills(card: dict[str, Any]) -> list[str]:
    """Skill ids from a card — the input to capability-card verification (item 186)."""
    skills = card.get("skills") or []
    return [str(skill.get("id", "")) for skill in skills if isinstance(skill, dict)]


__all__ = [
    "BASE_SKILLS",
    "DEFAULT_INPUT_MODES",
    "DEFAULT_OUTPUT_MODES",
    "ROLE_SKILLS",
    "HarnessCapabilities",
    "SkillSpec",
    "authority_scope_from_card",
    "build_agent_card",
    "capability_flags_from_card",
    "declared_skills",
]
