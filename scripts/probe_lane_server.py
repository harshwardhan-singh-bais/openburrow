"""Probe a lane's A2A server route by route.

This exists because "the peer returned 404" is not a diagnosis. A 404 from a
Starlette app can mean the path did not match, a Mount swallowed it, or the
handler itself chose to answer 404 — and those have completely different fixes.
This script starts a real server on an ephemeral port and prints the status and
body of every route so the answer is on screen rather than in your head.

Run with::

    uv run python scripts/probe_lane_server.py

It is a debugging tool, not a test. `scripts/smoke_test.py` is the test.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages"))

import httpx

from openburrow.a2a import HarnessCapabilities, LaneA2AServer
from openburrow.a2a.transport import make_request
from openburrow.core.models import Lane, LaneRole

BODY_LIMIT = 300


def show(label: str, method: str, url: str, response: httpx.Response) -> None:
    text = response.text.replace("\n", " ")[:BODY_LIMIT]
    print(f"[{response.status_code}] {label}")
    print(f"      {method} {url}")
    print(f"      body: {text}")
    print()


async def main() -> int:
    lane = Lane(
        name="probe",
        harness="claude-code",
        session_id="sess_probe",
        owner="probe",
        role=LaneRole.IMPLEMENTER,
    )
    server = LaneA2AServer(lane, capabilities=HarnessCapabilities(structured_output=True), port=0)

    await server.start()
    base = server.base_url
    print(f"base_url: {base}")
    print(f"card_url: {server.card_url}")
    print(f"bound port: {server.port}")
    print("-" * 70)
    print("route table as built:")
    for route in server._app.routes:
        print(f"  {route.path!r:<45} methods={sorted(route.methods or [])}")
    print("-" * 70)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Each request gets its own client-level connection, but we reuse one
            # AsyncClient — which is exactly the shape that failed before.
            r = await client.get(f"{base}/health")
            show("health", "GET", f"{base}/health", r)

            r = await client.get(server.card_url)
            show("agent card", "GET", server.card_url, r)

            payload = make_request("message/send", {"message": {"role": "user", "parts": []}})
            r = await client.post(f"{base}/", json=payload)
            show("jsonrpc (explicit trailing slash)", "POST", f"{base}/", r)

            r = await client.post(base, json=payload)
            show("jsonrpc (bare base url, as PeerClient does it)", "POST", base, r)

            r = await client.post(base, json={"not": "jsonrpc"})
            show("jsonrpc (malformed body)", "POST", base, r)

            # Second and third call on the same connection, to check whether
            # matching decays across requests on one keep-alive connection.
            for n in (2, 3):
                r = await client.post(base, json=payload)
                show(f"jsonrpc (repeat #{n}, same connection)", "POST", base, r)
    finally:
        await server.stop()

    # --- same app object, in-process transport -----------------------------
    # If this section passes while the uvicorn section above fails, the bug is
    # in the ASGI server or the HTTP client, not in the routing table.
    print("-" * 70)
    print("same app, in-process TestClient (no uvicorn, no sockets):")
    from starlette.testclient import TestClient

    with TestClient(server._app) as tc:
        # /stream is deliberately absent: it is an SSE endpoint that never ends,
        # and TestClient waits for the complete body.
        for label, method, path in (
            ("health", "GET", "/health"),
            ("agent card", "GET", "/.well-known/agent-card.json"),
            ("jsonrpc", "POST", "/"),
        ):
            kwargs = {"json": make_request("message/send", {})} if method == "POST" else {}
            resp = tc.request(method, path, **kwargs)
            print(f"  [{resp.status_code}] {label:<12} {method} {path}")

    # --- what does uvicorn actually deliver? -------------------------------
    # An echo app with one catch-all route. Whatever it prints is the path the
    # server handed the ASGI application, which is the only thing routing sees.
    print("-" * 70)
    print("echo app under uvicorn (shows the scope the server delivers):")
    import uvicorn
    from starlette.applications import Starlette as EchoStarlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route as EchoRoute

    async def echo(request):
        return PlainTextResponse(
            f"path={request.scope['path']!r} "
            f"raw_path={request.scope.get('raw_path')!r} "
            f"root_path={request.scope.get('root_path')!r} "
            f"method={request.scope['method']!r}"
        )

    echo_app = EchoStarlette(routes=[EchoRoute("/{rest:path}", echo, methods=["GET", "POST"])])
    echo_server = uvicorn.Server(
        uvicorn.Config(echo_app, host="127.0.0.1", port=0, log_level="warning", access_log=False)
    )
    echo_task = asyncio.create_task(echo_server.serve())
    for _ in range(200):
        if getattr(echo_server, "started", False):
            break
        await asyncio.sleep(0.02)
    echo_port = echo_server.servers[0].sockets[0].getsockname()[1]
    async with httpx.AsyncClient(timeout=5.0) as client:
        for method, path in (
            ("GET", "/health"),
            ("GET", "/x"),
            ("GET", "/x.y"),
            ("GET", "/a/b"),
            ("GET", "/a/b/c.json"),
            ("GET", "/.well-known"),
            ("GET", "/.well-known/agent-card.json"),
            ("GET", "/stream"),
            ("POST", "/"),
            ("POST", "/x"),
            ("POST", "/a/b"),
        ):
            r = await client.request(method, f"http://127.0.0.1:{echo_port}{path}")
            body = r.text[:120].replace("\n", " ")
            print(f"  [{r.status_code}] {method:<5} {path:<32} {body}")

    print("  -- same paths, but a fresh connection per request --")
    for method, path in (
        ("GET", "/health"),
        ("GET", "/x"),
        ("GET", "/.well-known/agent-card.json"),
        ("POST", "/"),
    ):
        async with httpx.AsyncClient(timeout=5.0) as fresh:
            r = await fresh.request(method, f"http://127.0.0.1:{echo_port}{path}")
        body = r.text[:120].replace("\n", " ")
        print(f"  [{r.status_code}] {method:<5} {path:<32} {body}")
    echo_server.should_exit = True
    await asyncio.sleep(0.1)
    echo_task.cancel()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
