"""A stand-in harness process for the PTY tests.

This is not a fixture of harness *output* — that is ``fixtures/``. This is a
program that behaves like a harness: it writes ``stream-json`` frames to stdout,
reads a prompt from stdin, and exits.

It exists because the PTY path cannot be exercised without a real child process
on a real pseudo-terminal, and the real harnesses need credentials and network
access that CI does not have. Spawning the actual ``claude`` binary in a test
would make the test depend on an API key, which is how a test becomes a test
nobody runs.

Usage: ``python -u pty_harness_stub.py [long]``

``long`` keeps the process alive after the frames so that shutdown behaviour —
the SIGTERM/SIGKILL escalation and the handle release — has something to act on.
"""

from __future__ import annotations

import json
import sys
import time

PROMPT_TIMEOUT_S = 10.0


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "frames"

    # Frame 1: the init notice. Carries the session id the adapter captures for
    # resume, and no prose at all — which is what makes it the empty-broadcast
    # case.
    emit({"type": "system", "subtype": "init", "session_id": "sess_pty_stub"})

    # Frame 2: prose inside the nested Anthropic envelope, with nested usage.
    emit(
        {
            "type": "assistant",
            "message": {
                "id": "msg_stub_1",
                "role": "assistant",
                "content": [{"type": "text", "text": "ready"}],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        }
    )

    if mode == "long":
        # Nothing else to say; stay alive so stop() has a live process to end.
        while True:
            time.sleep(0.1)

    # Wait for the prompt. A PTY is in canonical mode, so this returns on the
    # newline the adapter appends.
    deadline = time.time() + PROMPT_TIMEOUT_S
    line = ""
    while time.time() < deadline:
        line = sys.stdin.readline()
        if line.strip():
            break
        if not line:
            break

    if line.strip():
        emit(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "echo: " + line.strip()}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
        )

    # Frame: the terminal result. Carries a cumulative total_cost_usd, which the
    # adapter must not feed into an accumulating counter.
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "done",
            "session_id": "sess_pty_stub",
            "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 5, "output_tokens": 4},
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
