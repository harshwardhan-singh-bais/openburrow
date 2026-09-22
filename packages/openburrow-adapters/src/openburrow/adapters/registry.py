"""Adapter registry.

Two sources of adapters:

* **Built-in** — the harnesses OpenBurrow ships support for. Imported eagerly
  because there are few of them and the failure mode of a lazy import (a typo'd
  harness name silently yielding "no adapter") is worse than the import cost.
* **Custom** — adapters discovered from ``OPENBURROW_CUSTOM_ADAPTER_DIR``. These
  are third-party code, so they load through an explicit trust decision
  (``prompt`` | ``allow`` | ``deny``) rather than silently. Loading arbitrary
  code from a repo checkout without asking is how you get a supply-chain
  incident inside an agent orchestration tool.

The three trust modes mean what they say:

``allow``
    Load every custom adapter without asking. Correct for CI images you control.
``deny``
    Never load, never ask.
``prompt``
    Ask on the terminal, once per module, and load only what is approved. When
    there is no terminal to ask — a daemon, a CI job, a pipe — the answer is
    **no**. A trust gate that defaults to *yes* when nobody is watching is not a
    gate; it is a delay.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openburrow.adapters.base import HarnessAdapter
from openburrow.core.config.settings import Settings
from openburrow.core.errors import AdapterError, ConfigError
from openburrow.core.logging import get_logger

log = get_logger(__name__)

#: Built-in adapter classes, keyed by registry name.
_BUILTIN_MODULES: dict[str, str] = {
    "opencode": "openburrow.adapters.harnesses.opencode:OpenCodeAdapter",
    "claude-code": "openburrow.adapters.harnesses.claude_code:ClaudeCodeAdapter",
    "codex": "openburrow.adapters.harnesses.codex:CodexAdapter",
    "crush": "openburrow.adapters.harnesses.crush:CrushAdapter",
    "gemini": "openburrow.adapters.harnesses.gemini:GeminiAdapter",
    "vscode": "openburrow.adapters.harnesses.vscode:VSCodeAdapter",
    "aider": "openburrow.adapters.harnesses.aider:AiderAdapter",
    "goose": "openburrow.adapters.harnesses.goose:GooseAdapter",
    "mock": "openburrow.adapters.harnesses.mock:MockAdapter",
    "custom": "openburrow.adapters.harnesses.custom:CustomScriptAdapter",
}

#: Aliases so users can type the name they know rather than ours.
_ALIASES: dict[str, str] = {
    "claude": "claude-code",
    "claudecode": "claude-code",
    "open-code": "opencode",
    "gemini-cli": "gemini",
    "antigravity": "gemini",
    "codex-cli": "codex",
    "vscode": "vscode",
    "vs-code": "vscode",
    "script": "custom",
}


#: Callable that decides whether one custom adapter module may be imported.
#: Returns ``True`` to load it. The default asks the terminal.
ConfirmFn = Callable[[Path], bool]


class AdapterRegistry:
    """Resolves harness names to adapter classes and instances."""

    def __init__(self, settings: Settings, *, confirm: ConfirmFn | None = None) -> None:
        self.settings = settings
        self._classes: dict[str, type[HarnessAdapter]] = {}
        self._custom_loaded = False
        # Injectable so the trust prompt is testable without a pty, and so a
        # host that already has its own approval UI (the daemon, the TUI) can
        # supply it instead of having a second, divergent prompt.
        self._confirm: ConfirmFn = confirm if confirm is not None else _ask_terminal

    # --- registration ------------------------------------------------------
    def register(self, name: str, adapter_cls: type[HarnessAdapter]) -> None:
        self._classes[name] = adapter_cls

    def unregister(self, name: str) -> None:
        self._classes.pop(name, None)

    def names(self) -> list[str]:
        self._load_builtins()
        self._load_custom()
        return sorted(self._classes)

    # --- resolution --------------------------------------------------------
    def resolve(self, name: str) -> type[HarnessAdapter]:
        """Look up an adapter class, raising a helpful error when it is unknown."""
        self._load_builtins()
        self._load_custom()

        key = _ALIASES.get(name.lower().strip(), name.lower().strip())
        adapter_cls = self._classes.get(key)
        if adapter_cls is None:
            known = ", ".join(self.names())
            raise AdapterError(
                f"unknown harness {name!r}",
                hint=f"Known harnesses: {known}. Run `burrow adapters list` for details.",
                context={"requested": name, "known": self.names()},
            )
        return adapter_cls

    def create(self, name: str, *, lane: Any = None) -> HarnessAdapter:
        """Instantiate an adapter for a lane."""
        adapter_cls = self.resolve(name)
        return adapter_cls(self.settings, lane=lane)

    def available(self) -> dict[str, bool]:
        """Which harnesses are actually installed on this machine.

        Used by ``burrow doctor`` and by ``burrow init``, which will not write a
        lane template for a harness that is not present.
        """
        self._load_builtins()
        self._load_custom()
        result: dict[str, bool] = {}
        for name, adapter_cls in self._classes.items():
            try:
                probe = adapter_cls(self.settings)
                result[name] = HarnessAdapter._binary_exists(probe.resolve_binary())
            except Exception:
                result[name] = False
        return result

    def describe_all(self) -> list[dict[str, Any]]:
        """Structured listing for `burrow adapters list --json`."""
        self._load_builtins()
        self._load_custom()
        rows: list[dict[str, Any]] = []
        availability = self.available()
        for name in sorted(self._classes):
            adapter_cls = self._classes[name]
            try:
                probe = adapter_cls(self.settings)
                caps = probe.capabilities.to_dict()
                skills = [s.id for s in probe.skills()]
            except Exception:
                caps, skills = {}, []
            rows.append(
                {
                    "name": name,
                    "description": adapter_cls.description,
                    "binary": adapter_cls.binary,
                    "installed": availability.get(name, False),
                    "capabilities": caps,
                    "extra_skills": skills,
                    "docs": adapter_cls.docs_url,
                    "builtin": name in _BUILTIN_MODULES,
                }
            )
        return rows

    # --- loading -----------------------------------------------------------
    def _load_builtins(self) -> None:
        for name, target in _BUILTIN_MODULES.items():
            if name in self._classes:
                continue
            try:
                self._classes[name] = _import_target(target)
            except ImportError as exc:
                # A missing optional dependency should make one adapter
                # unavailable, never break the whole registry.
                log.debug("adapter.builtin_unavailable", adapter=name, error=str(exc))

    def _load_custom(self) -> None:
        if self._custom_loaded:
            return
        self._custom_loaded = True

        trust = self.settings.custom_adapter_trust
        if trust == "deny":
            log.debug("adapter.custom_denied_by_policy")
            return

        directory = Path(self.settings.custom_adapter_dir)
        if not directory.is_absolute():
            directory = Path.cwd() / directory
        if not directory.is_dir():
            return

        for module_path in sorted(directory.glob("*.py")):
            if module_path.name.startswith("_"):
                continue
            if trust == "prompt" and not self._confirm_custom(module_path):
                continue
            try:
                self._load_custom_module(module_path)
            except Exception as exc:
                log.error("adapter.custom_load_failed", path=str(module_path), error=str(exc))

    def _confirm_custom(self, module_path: Path) -> bool:
        """Ask the trust gate about one module. Any failure to ask means *no*.

        A prompter that raises, returns a non-boolean, or cannot reach a
        terminal must not be interpreted as consent — that is the whole point of
        putting a gate here rather than an unconditional import.
        """
        try:
            approved = bool(self._confirm(module_path))
        except Exception as exc:
            log.warning(
                "adapter.custom_trust_prompt_failed",
                path=str(module_path),
                error=str(exc),
            )
            return False

        if not approved:
            log.warning(
                "adapter.custom_trust_declined",
                path=str(module_path),
                hint=(
                    "Custom adapters are third-party code and were not loaded. "
                    "Set OPENBURROW_CUSTOM_ADAPTER_TRUST=allow to load them "
                    "without asking, or =deny to stop asking."
                ),
            )
        return approved

    def _load_custom_module(self, module_path: Path) -> None:
        module_name = f"openburrow_custom_adapter_{module_path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise ConfigError(f"could not load custom adapter from {module_path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        registered = 0
        for attr in dir(module):
            candidate = getattr(module, attr)
            if (
                isinstance(candidate, type)
                and issubclass(candidate, HarnessAdapter)
                and candidate is not HarnessAdapter
                and getattr(candidate, "name", "base") != "base"
            ):
                self.register(candidate.name, candidate)
                registered += 1

        if registered:
            log.info(
                "adapter.custom_loaded",
                path=str(module_path),
                adapters=registered,
            )


def _ask_terminal(module_path: Path) -> bool:
    """Ask the operator whether one custom adapter module may be imported.

    The question goes to **stderr**, not stdout: ``burrow adapters list --json``
    writes machine-readable output there, and a prompt spliced into a JSON
    document is a bug that only shows up in a pipeline.

    Returns ``False`` when there is no terminal to ask. An unattended process
    gets the safe answer rather than a guess.
    """
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        log.debug("adapter.custom_trust_unattended", path=str(module_path))
        return False

    # ``sys.stderr.write`` rather than ``print``: T20 reserves print for scripts,
    # and the constraint here is the same one the prompt itself documents — this
    # must go to stderr so a JSON pipeline on stdout stays machine-readable.
    sys.stderr.write(
        f"OpenBurrow found a custom adapter at {module_path}\n"
        "  Custom adapters are third-party code; loading one imports it into\n"
        "  this process with your credentials and your filesystem access.\n"
        "Load it? [y/N] "
    )
    sys.stderr.flush()
    try:
        answer = input()
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n")
        return False
    return answer.strip().lower() in {"y", "yes"}


def _import_target(target: str) -> type[HarnessAdapter]:
    """Import ``module:Class`` or ``module.Class`` and verify it is an adapter."""
    if ":" in target:
        module_name, _, class_name = target.partition(":")
    else:
        module_name, _, class_name = target.rpartition(".")

    module = importlib.import_module(module_name)
    candidate = getattr(module, class_name, None)
    if candidate is None:
        raise ImportError(f"{module_name} has no attribute {class_name!r}")
    if not (isinstance(candidate, type) and issubclass(candidate, HarnessAdapter)):
        raise ImportError(f"{target} is not a HarnessAdapter subclass")
    return candidate


def build_registry(settings: Settings, *, confirm: ConfirmFn | None = None) -> AdapterRegistry:
    registry = AdapterRegistry(settings, confirm=confirm)
    registry._load_builtins()
    return registry


__all__ = ["AdapterRegistry", "ConfirmFn", "build_registry"]
