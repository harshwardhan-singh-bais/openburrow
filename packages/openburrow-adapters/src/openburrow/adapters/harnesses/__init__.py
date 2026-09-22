"""Built-in harness adapters.

Import order matters for nothing here — the registry imports them lazily and
tolerates a missing optional dependency by marking that one adapter unavailable.
That is why this module is a re-export surface rather than an eager importer.
"""

from openburrow.adapters.harnesses.aider import AiderAdapter
from openburrow.adapters.harnesses.claude_code import ClaudeCodeAdapter
from openburrow.adapters.harnesses.codex import CodexAdapter
from openburrow.adapters.harnesses.crush import CrushAdapter
from openburrow.adapters.harnesses.custom import CustomScriptAdapter
from openburrow.adapters.harnesses.gemini import GeminiAdapter
from openburrow.adapters.harnesses.generic import GenericCliAdapter
from openburrow.adapters.harnesses.goose import GooseAdapter
from openburrow.adapters.harnesses.mock import MockAdapter
from openburrow.adapters.harnesses.opencode import OpenCodeAdapter
from openburrow.adapters.harnesses.vscode import VSCodeAdapter

__all__ = [
    "AiderAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "CrushAdapter",
    "CustomScriptAdapter",
    "GeminiAdapter",
    "GenericCliAdapter",
    "GooseAdapter",
    "MockAdapter",
    "OpenCodeAdapter",
    "VSCodeAdapter",
]
