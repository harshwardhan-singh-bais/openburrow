"""OpenBurrow harness adapters.

An adapter is the only harness-specific code in the project. Its job is to make
a heterogeneous set of tools — Claude Code, Codex, Crush, OpenCode, Aider, a
shell script — look identical from above, so that the bus, the lifecycle, and
the governance ledger never need to know which vendor is on the other end.

The protocol an adapter implements:

=========================  ==================================================
``build_spawn_spec``       compute the command, cwd, and env (pure, inspectable)
``start`` / ``stop``       spawn and terminate, PTY-aware
``send_prompt``            deliver text in whatever form the harness accepts
``read_output``            yield interpreted :class:`HarnessOutput` chunks
``status``                 liveness plus the harness's own notion of state
``translate_output``       native output -> A2A message
``inject_message``         A2A message -> native input
=========================  ==================================================

``build_spawn_spec`` being pure is load-bearing: the policy gate calls it to
inspect a command *before* anything executes, which is what makes the gate a
gate rather than a log.
"""

from openburrow.adapters.base import HarnessAdapter, HarnessOutput, SpawnSpec
from openburrow.adapters.harnesses import (
    AiderAdapter,
    ClaudeCodeAdapter,
    CodexAdapter,
    CrushAdapter,
    CustomScriptAdapter,
    GeminiAdapter,
    GooseAdapter,
    MockAdapter,
    OpenCodeAdapter,
)
from openburrow.adapters.registry import AdapterRegistry, build_registry

__all__ = [
    "AdapterRegistry",
    "AiderAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "CrushAdapter",
    "CustomScriptAdapter",
    "GeminiAdapter",
    "GooseAdapter",
    "HarnessAdapter",
    "HarnessOutput",
    "MockAdapter",
    "OpenCodeAdapter",
    "SpawnSpec",
    "build_registry",
]
