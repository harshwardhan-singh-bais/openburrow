"""Merge Radar: predicting collisions before they cost anyone a day.

The Radar exists because the expensive failures in multi-agent work are not
loud. A merge conflict is loud and cheap — git tells you immediately. What is
quiet and expensive is two lanes spending an afternoon implementing incompatible
versions of the same interface, discovering it at integration, and then having to
decide whose work to throw away.

Three layers, in order of trustworthiness:

:mod:`openburrow.radar.intent`
    What each lane is working on. Files come from claims and plan steps, which
    are records of decisions. Descriptions may come from a model, but a model can
    never add a file — see the module docstring for why that line is load-bearing.

:mod:`openburrow.radar.judge`
    An optional model asked to compare two intents. Clamps its own confidence,
    distinguishes "no conflict" from "I could not tell", and fails toward silence
    rather than toward interrupting two agents.

:mod:`openburrow.radar.conflicts`
    Scoring and thresholds, with certainty kept visibly separate from prediction.
    A shared file is a fact; a judge's opinion is not; they are never rendered
    with the same weight.

:mod:`openburrow.radar.detector`
    The loop: keep intents current, scan pairs, announce each conflict once, and
    report its own coverage honestly so a dead judge cannot look like a quiet
    week.

A Radar prediction is a *prompt to negotiate*, not a decision. When the Radar
raises a conflict, the coordinator opens an ACP exchange between the two lanes —
they resolve it, or a human does. The Radar never assigns ownership, because the
lanes know more about their own work than a pairwise score does.
"""

from openburrow.radar.conflicts import (
    DEFAULT_SCOPE_THRESHOLD,
    DEFAULT_SEMANTIC_THRESHOLD,
    ConflictPrediction,
    ConflictPredictor,
    Severity,
    Signal,
)
from openburrow.radar.detector import Radar, RadarStats, pairs
from openburrow.radar.intent import CRITICAL, HIGH, LOW, MEDIUM, Intent, IntentExtractor
from openburrow.radar.judge import ConflictJudge, JudgeVerdict

__all__ = [
    "CRITICAL",
    "DEFAULT_SCOPE_THRESHOLD",
    "DEFAULT_SEMANTIC_THRESHOLD",
    "HIGH",
    "LOW",
    "MEDIUM",
    "ConflictJudge",
    "ConflictPrediction",
    "ConflictPredictor",
    "Intent",
    "IntentExtractor",
    "JudgeVerdict",
    "Radar",
    "RadarStats",
    "Severity",
    "Signal",
    "pairs",
]
