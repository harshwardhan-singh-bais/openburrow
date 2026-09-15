"""Aider adapter.

Aider is the strongest "pure diff" harness: its output is a diff and little
else, which makes it a natural fit for the reviewer role and for the
negotiation path where the *content* of a proposal is a concrete patch.

Because Aider commits as it goes, the adapter records the commit SHA from its
output so the checkpoint layer can anchor to real git state rather than
guessing.

The commit anchor
-----------------

Aider prints its commit notice from one place, verbatim::

    self.io.tool_output(f"Commit {commit_hash} {commit_message}", bold=True)

with ``commit_hash`` a **short 7-character** SHA from
``get_head_commit_sha(short=True)``. So the notice is a line that *begins* with
``Commit``, and that is what the detector keys on.

It used to key on ``\\b([0-9a-f]{7,40})\\b`` — any run of hex digits with word
boundaries around it, anywhere in the chunk. Aider's output is mostly diffs, and
a diff body is arbitrary source code, so the pattern matched the first
hex-looking token in whatever the lane had just written. Measured cases that
would have been recorded as the lane's git anchor: a base64 fragment in a test
fixture, ``deadbeef`` in a comment, a 10-character hex id in a JSON blob. Each
one published a ``commit`` artifact naming a revision that does not exist, to a
checkpoint layer whose entire purpose is to *not* guess.

Anchoring on the line-leading literal is not merely narrower, it is
structurally safe: inside a unified diff every line carries a ``+``, ``-``, or
space prefix, so no diff line can begin with ``Commit``. The anchor cannot match
diff content even in principle.

The ``--no-auto-commits`` contradiction
---------------------------------------

The docstring above says Aider commits as it goes, and the adapter is written to
record the resulting SHA. The spawn args used to pass ``--no-auto-commits``,
which turns off exactly that. Verified against Aider's own ``args.py``:
``--auto-commits`` is an ``argparse.BooleanOptionalAction`` that **defaults to
True**, so the flag was not opting into anything — it was switching off the
behaviour the rest of the module was built around.

The consequence was a feature that could not fire. Nothing else in OpenBurrow
produces a commit SHA for a lane (checked: no ``git commit`` call site outside
this adapter), so with auto-commits disabled the extraction had no input, the
``commit`` artifact was never emitted, and the checkpoint anchor described above
did not exist.

The flag is therefore gone. It is safe to remove: a lane works in its own
disposable git worktree, which is the isolation the whole design rests on, so a
commit lands on the lane's own branch and is torn down with it. Restoring it is
a one-word change if an installation wants uncommitted lanes instead — but then
this module's commit handling is dead code and should go with it, because an
adapter that suppresses commits while extracting commit SHAs is describing a
workflow it has prevented.
"""

from __future__ import annotations

import os
import re

from openburrow.a2a.card import SkillSpec
from openburrow.adapters.base import HarnessOutput, _strip_ansi
from openburrow.adapters.harnesses.generic import GenericCliAdapter
from openburrow.core.models import Lane, TaskArtifact

#: Aider's commit notice, anchored to the line-leading literal it actually
#: prints. See the module docstring for why the anchor matters and why the
#: leading ``^`` is what makes it safe against diff content.
#:
#: ``finditer`` rather than ``search`` because a chunk can carry more than one
#: notice when several files were committed in a burst; the caller takes the
#: last, which is the newest revision.
_COMMIT_LINE = re.compile(
    r"^\s*Commit\s+([0-9a-f]{7,40})\b[ \t]*(.*)$",
    re.MULTILINE,
)


class AiderAdapter(GenericCliAdapter):
    name = "aider"
    description = "Aider — diff-first terminal coding assistant that commits as it works."
    binary = "aider"
    binary_env = "AIDER_BIN"
    docs_url = "https://aider.chat/docs"

    #: ``--yes-always`` answers Aider's interactive confirmations, including the
    #: offer to initialise a git repository when the lane's worktree is not one.
    #: ``--no-auto-commits`` is deliberately absent; see the module docstring.
    base_args: tuple[str, ...] = ("--yes-always",)
    structured_args: tuple[str, ...] = ()
    has_structured_mode: bool = False
    #: Credentials this harness understands. A lane gets one only by granting it
    #: in its template's ``env_passthrough``; see
    #: :meth:`~openburrow.adapters.base.HarnessAdapter.granted_env`.
    credential_env: tuple[str, ...] = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    is_resumable: bool = False
    mcp_native: bool = False

    def skills(self) -> list[SkillSpec]:
        return [
            SkillSpec(
                skill_id="aider-patch",
                name="Produce a patch",
                description="Emit a minimal, reviewable diff for a described change.",
                tags=["code", "diff"],
            )
        ]

    def extra_env(self, lane: Lane) -> dict[str, str]:
        """Aider's non-credential configuration.

        Model and config-path overrides are not secrets, so they are read from
        the ambient environment rather than gated by a per-lane grant. They still
        go through ``super()`` first, because the granted credentials come from
        there — an override that returned only its own dict would silently drop
        the keys the lane did grant.
        """
        env = super().extra_env(lane)
        for key in ("AIDER_MODEL", "AIDER_CONFIG"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    def _interpret(self, text: str) -> list[HarnessOutput]:
        """Interpret the chunk, then attach any commit notice it carries.

        The notice usually arrives in its own read, one turn after the diff that
        caused it, and a lone ``Commit <sha> <message>`` line classifies as
        ``text`` — a kind :meth:`~openburrow.adapters.base.HarnessAdapter.translate_output`
        does not broadcast. So attaching the commit only to ``diff`` outputs,
        which is what this method used to do, meant the common case recorded
        nothing: the anchor was dropped at the last step, after being detected
        correctly.

        A lone notice is therefore reclassified as ``status``, which *is*
        broadcast, and carries the SHA in its payload. A notice sharing a chunk
        with a diff still attaches to the diff, because that is the output whose
        provenance it describes.
        """
        outputs = super()._interpret(text)

        matches = list(_COMMIT_LINE.finditer(_strip_ansi(text)))
        if not matches:
            return outputs

        # Newest revision wins when a chunk carries several notices.
        sha, message = matches[-1].group(1), matches[-1].group(2).strip()
        artifact = TaskArtifact(
            kind="json",
            name="commit",
            content=sha,
            mime_type="application/json",
        )
        provenance = {"commit": sha, "commit_message": message}

        diff_outputs = [output for output in outputs if output.kind == "diff"]
        if diff_outputs:
            for output in diff_outputs:
                output.data = {**output.data, **provenance}
                output.artifacts.append(artifact)
            return outputs

        for output in outputs:
            if output.kind == "text":
                output.kind = "status"
                output.data = {**output.data, **provenance}
                output.artifacts.append(artifact)

        return outputs


__all__ = ["AiderAdapter"]
