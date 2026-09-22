"""Stage 17 notifications: family mapping, signature verification, hooks, standup."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openburrow.daemon.notify import (
    Notifier,
    build_standup,
    event_family,
    load_hooks,
    render_standup_text,
    sign_webhook_body,
    verify_webhook_signature,
)

pytestmark = [pytest.mark.unit]


def make_settings(**overrides: Any) -> SimpleNamespace:
    base = {
        "notify_enabled": True,
        "notify_desktop": False,
        "notify_on": ["approval", "session-done"],
        "slack_webhook_url": "",
        "discord_webhook_url": "",
        "teams_webhook_url": "",
        "webhook_secret": "",
        "hooks_file": "",
        "is_production": False,
        **overrides,
    }
    return SimpleNamespace(**base)


def test_event_family_mapping() -> None:
    assert event_family("governance.approval_required") == "approval"
    assert event_family("session.closed") == "session-done"
    assert event_family("governance.flag") == "governance-flag"
    assert event_family("lane.output.diff") == ""  # noise


def test_signature_round_trip() -> None:
    body = b'{"text": "hello"}'
    signature = sign_webhook_body("s3cret", body)
    assert signature.startswith("sha256=")
    assert verify_webhook_signature("s3cret", body, signature)
    assert verify_webhook_signature("s3cret", body, signature.removeprefix("sha256="))


def test_signature_rejects_tampering_and_missing_secret() -> None:
    body = b'{"text": "hello"}'
    signature = sign_webhook_body("s3cret", body)
    assert not verify_webhook_signature("s3cret", body + b" ", signature)
    assert not verify_webhook_signature("wrong", body, signature)
    assert not verify_webhook_signature("", body, signature)  # no secret -> no trust


def test_load_hooks_parses_and_skips_bad_entries(tmp_path: Path) -> None:
    hooks_file = tmp_path / "hooks.yaml"
    hooks_file.write_text(
        "- name: ping\n  command: python scripts/ping.py\n  on: [session-done]\n"
        "- command-without-name\n"
        "- name: broken\n",
        encoding="utf-8",
    )
    hooks = load_hooks(hooks_file)
    assert len(hooks) == 1
    assert hooks[0].name == "ping"
    assert hooks[0].matches("session.closed", "session-done")
    assert not hooks[0].matches("lane.crashed", "")


def test_load_hooks_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_hooks(tmp_path / "nope.yaml") == []


async def test_notifier_filters_noise_and_dispatches_family() -> None:
    ran: list[str] = []
    notifier = Notifier(make_settings(hooks_file=""))  # type: ignore[arg-type]

    class FakeHook:
        name = "x"
        command = "true"

        def __init__(self) -> None:
            self.on: list[str] = []

        def matches(self, _event_type: str, _family: str) -> bool:
            return True

    notifier.hooks = [FakeHook()]  # type: ignore[list-item]

    async def fake_run(hook: Any, event: dict) -> None:
        ran.append(hook.name)

    notifier._run_hook = fake_run  # type: ignore[method-assign]

    # A noise event reaches hooks (all-event hook) but no channels fire.
    await notifier.handle_event({"event_type": "lane.output.diff", "summary": "x"})
    assert ran == ["x"]

    # A family event outside notify_on is filtered entirely.
    ran.clear()
    await notifier.handle_event(
        {"event_type": "governance.flag", "summary": "flagged", "payload": {}}
    )
    assert ran == []  # governance-flag not in notify_on


async def test_notifier_runs_hook_with_event_env(monkeypatch: Any, tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def fake_exec(*args: Any, **kwargs: Any) -> FakeProcess:
        captured.update(kwargs.get("env") or {})
        return FakeProcess()

    import openburrow.daemon.notify as notify_module

    monkeypatch.setattr(notify_module.asyncio, "create_subprocess_exec", fake_exec)

    notifier = Notifier(make_settings())  # type: ignore[arg-type]
    hook = SimpleNamespace(name="echo", command="python -c pass", on=["approval"])
    event = {
        "event_type": "governance.approval_required",
        "session_id": "s1",
        "lane_id": "lane-9",
        "summary": "risky action",
        "payload": {"risk": "high"},
    }
    await notifier._run_hook(hook, event)  # type: ignore[arg-type]

    assert captured["OPENBURROW_EVENT_TYPE"] == "governance.approval_required"
    assert captured["OPENBURROW_EVENT_SESSION"] == "s1"
    assert json.loads(captured["OPENBURROW_EVENT_PAYLOAD"]) == {"risk": "high"}


def test_standup_builder_orders_the_three_sections() -> None:
    events: list[dict[str, Any]] = [
        {
            "event_type": "session.created",
            "summary": "session started",
            "at": "2026-09-16T01:00:00",
        },
        {
            "event_type": "lane.started",
            "lane_id": "lane-a",
            "summary": "lane a up",
            "at": "2026-09-16T01:00:05",
        },
        {
            "event_type": "task.completed",
            "lane_id": "lane-a",
            "summary": "step 1 done",
            "at": "2026-09-16T01:05:00",
        },
        {
            "event_type": "bus.message.sent",
            "lane_id": "lane-a",
            "summary": "lane-a: claim src/x.py",
            "at": "2026-09-16T01:06:00",
            "payload": {"sender_lane": "lane-a"},
        },
        {
            "event_type": "negotiation.finished",
            "summary": "agreed",
            "at": "2026-09-16T01:07:00",
            "payload": {"outcome": "agreed", "collision_avoided": True},
        },
        {
            "event_type": "governance.flag",
            "summary": "poisoned lesson refused",
            "at": "2026-09-16T01:08:00",
        },
    ]
    report = build_standup(events, session_name="night-shift")
    assert report["session_name"] == "night-shift"
    assert report["counts"]["negotiations"] == 1
    assert report["counts"]["collisions_avoided"] == 1
    assert report["counts"]["governance_events"] == 1
    assert any("step 1 done" in h for lane in report["lanes"] for h in lane["highlights"])

    text = render_standup_text(report)
    assert "What the agents did" in text
    assert "What they said to each other" in text
    assert "Governance" in text
    assert "poisoned lesson refused" in text
