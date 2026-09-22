"""Workspace commands: init, doctor, config, adapters, version.

These are the commands that must work *before* anything else does — in a fresh
clone, with no daemon running, possibly with no harness installed. They are
therefore all daemon-free and all tolerant of a missing ``openburrow.yaml``.

``burrow doctor`` is the most important of them. Its job is to answer "why
isn't this working?" with a specific cause rather than a stack trace, so it
checks each layer in dependency order and reports the first thing that is
actually wrong.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Annotated, Protocol

import typer

from openburrow.adapters import build_registry
from openburrow.cli.context import CliContext
from openburrow.cli.output import (
    banner,
    console,
    emit,
    failure,
    info,
    print_kv,
    render_error,
    section,
    success,
    table,
    warn,
)
from openburrow.core.config.load import ResolvedConfig, load_config
from openburrow.core.config.migrate import apply_migration, plan_migration
from openburrow.core.config.repo_config import (
    LaneTemplate,
    ProjectConfig,
    RepoConfig,
    dump_repo_config,
)
from openburrow.core.config.settings import Settings
from openburrow.core.errors import OpenBurrowError
from openburrow.core.paths import (
    BurrowPaths,
    find_config_file,
    find_repo_root,
    find_repo_root_or_none,
)
from openburrow.core.version import __version__, version_info
from openburrow.daemon.sandbox import plan as sandbox_plan

app = typer.Typer(help="Workspace setup, diagnostics, and configuration.", no_args_is_help=True)


# ---------------------------------------------------------------------------
# burrow init
# ---------------------------------------------------------------------------
def _declared_credentials(registry: object, harness: str) -> list[str]:
    """Credential variables a harness declares, for its template to grant.

    The other half of credential isolation. A lane receives a credential only if
    its template names it, which is what makes the property checkable — but it
    also means a template written without the list produces a harness that
    starts, cannot authenticate, and fails with the provider's own error. Writing
    the grant here keeps the *generated* config working without loosening the
    mechanism: a hand-written template that omits a key still does not get it,
    and the spawn log names the variable and the YAML to add.

    Every declared credential is written, not only the ones currently set in the
    environment. Granting a variable that is unset is a no-op at spawn time,
    whereas filtering by presence would bake today's shell into the repo's config
    and silently stop granting a key the moment someone added it later.
    """
    try:
        adapter_cls = registry.resolve(harness)  # type: ignore[attr-defined]
    except Exception:
        # An unknown or unloadable harness is already reported by the caller;
        # a template with no grant is the correct fallback.
        return []
    return list(getattr(adapter_cls, "credential_env", ()))


@app.command("init")
def init(
    ctx: typer.Context,
    path: Annotated[Path | None, typer.Argument(help="Repository root. Defaults to cwd.")] = None,
    name: Annotated[str, typer.Option("--name", "-n", help="Project name.")] = "",
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing config.")] = False,
    lanes: Annotated[
        str,
        typer.Option(
            "--lanes",
            help="Comma-separated harness names to declare as lane templates.",
        ),
    ] = "",
    default_branch: Annotated[str, typer.Option("--branch", help="Default branch.")] = "main",
    no_worktrees: Annotated[
        bool, typer.Option("--no-worktrees", help="Skip creating the worktree pool.")
    ] = False,
) -> None:
    """Initialise OpenBurrow in a repository.

    Writes ``openburrow.yaml``, creates the runtime directory, initialises the
    local database, and declares a lane template for each requested harness —
    but only for harnesses actually installed on this machine, because a
    template for a harness that is not present is a trap for the next person.
    """
    context: CliContext = ctx.obj

    try:
        repo_root = Path(path).expanduser().resolve() if path else find_repo_root()
    except OpenBurrowError:
        repo_root = Path(path).expanduser().resolve() if path else Path.cwd()
        if not (repo_root / ".git").exists():
            warn(f"{repo_root} is not a git repository — initialising anyway")

    existing = find_config_file(repo_root)
    if existing is not None and not force:
        failure(f"{existing.name} already exists")
        info("Use --force to overwrite it, or edit it directly.")
        raise typer.Exit(code=1)

    paths = BurrowPaths.for_repo(repo_root)

    # --- which harnesses are actually present -----------------------------
    config_probe = load_config(repo_root, require_repo=False)
    registry = build_registry(config_probe.settings)
    availability = registry.available()

    requested = [item.strip() for item in lanes.split(",") if item.strip()]
    if not requested:
        requested = [name_ for name_, ok in availability.items() if ok and name_ != "mock"]
        if not requested:
            requested = ["mock"]

    unknown = [name_ for name_ in requested if name_ not in availability]
    if unknown:
        warn(f"unknown harness(es): {', '.join(unknown)}")
        info(f"Known: {', '.join(registry.names())}")
        requested = [name_ for name_ in requested if name_ in availability]

    missing = [name_ for name_ in requested if not availability.get(name_, False)]
    if missing:
        warn(f"declared but not installed on this machine: {', '.join(missing)}")
        info("Lane templates will be written; `burrow doctor` will report them as unavailable.")

    # --- build the config --------------------------------------------------
    roles = ["implementer", "reviewer", "coordinator", "observer"]
    lane_templates = [
        LaneTemplate(
            name=f"{harness}-{index + 1}",
            harness=harness,
            role=roles[index % len(roles)],
            claims=[],
            env_passthrough=_declared_credentials(registry, harness),
        )
        for index, harness in enumerate(requested)
    ]

    repo_config = RepoConfig(
        project=ProjectConfig(
            name=name or repo_root.name,
            default_branch=default_branch,
            description="",
        ),
        lanes=lane_templates,
        adapters=config_probe.repo.adapters.model_copy(
            update={
                "enabled": requested or ["mock"],
                "default": requested[0] if requested else "mock",
            }
        ),
    )

    config_path = repo_root / "openburrow.yaml"
    config_path.write_text(dump_repo_config(repo_config), encoding="utf-8")

    # --- runtime scaffolding ----------------------------------------------
    paths.ensure(worktrees=not no_worktrees)
    (paths.runtime_dir / ".gitignore").write_text(
        "# OpenBurrow runtime state — never commit\n*\n", encoding="utf-8"
    )

    agents_md = paths.agents_md
    created_agents_md = False
    if not agents_md.exists() and repo_config.brain.ingest_agents_md:
        agents_md.write_text(_agents_md_skeleton(repo_root.name), encoding="utf-8")
        created_agents_md = True

    # --- database ----------------------------------------------------------
    db_info: dict[str, object] = {}
    if not context.no_daemon:
        import asyncio

        from openburrow.core.db.engine import init_database

        async def _prepare_database(url: str) -> dict[str, object]:
            """Create the schema, read the version, close — all on one event loop.

            These three steps belong to a single loop. The previous version used
            three: ``asyncio.run(init_database(...))``, then
            ``asyncio.get_event_loop().run_until_complete(...)`` for the version,
            then ``asyncio.run(database.close())``.

            The middle call raised on Python 3.12+ — ``RuntimeError: There is no
            current event loop in thread 'MainThread'`` — because ``asyncio.run``
            had already closed the loop it created. Even had it worked, the
            engine's connections were bound to the first loop, so closing the
            database on a third was never going to be correct. ``asyncio.run``
            per operation is a pattern that looks tidy and is wrong for anything
            that holds a connection pool.
            """
            database = await init_database(url)
            try:
                return {"url": url, "schema": await database.schema_version()}
            finally:
                await database.close()

        try:
            db_info = asyncio.run(_prepare_database(config_probe.db_url))
        except Exception as exc:
            warn(f"could not initialise the local database: {exc}")

    payload = {
        "repo_root": str(repo_root),
        "config": str(config_path),
        "runtime_dir": str(paths.runtime_dir),
        "worktree_root": str(paths.worktree_root),
        "lanes": [template.name for template in lane_templates],
        "harnesses": requested,
        "agents_md": str(agents_md) if created_agents_md else None,
        "database": db_info,
    }

    def render(result: dict) -> None:
        banner("OpenBurrow initialised", subtitle=str(repo_root), style="green")
        print_kv(
            {
                "config": result["config"],
                "runtime": result["runtime_dir"],
                "worktrees": result["worktree_root"],
                "harnesses": result["harnesses"],
                "lanes": result["lanes"],
            }
        )
        if result.get("agents_md"):
            info(f"seeded {result['agents_md']}")
        console.print(
            "\n[dim]Next:[/dim] burrow doctor, then burrow session start --name my-first-session"
        )

    emit(context, payload, human_renderer=render)


def _agents_md_skeleton(project_name: str) -> str:
    """Seed ``AGENTS.md``.

    OpenBurrow honors the emerging ``AGENTS.md`` convention rather than
    inventing a parallel format, and the Brain both reads and writes this file.
    Seeding it here means the very first session has somewhere to put what it
    learns.
    """
    return f"""# AGENTS.md

Repo-level context for coding agents working in `{project_name}`.

This file is read natively by Crush, Codex, and other tools. OpenBurrow's
Shared Brain also reads and writes it, so knowledge captured during a session
lands somewhere every tool benefits from — not in a format only one tool reads.

## Conventions

<!-- Decisions, gotchas, and conventions accumulate here. -->

## Gotchas

## Decisions
"""


# ---------------------------------------------------------------------------
# burrow doctor
# ---------------------------------------------------------------------------
def _sandbox_check(settings: Settings) -> tuple[bool, str]:
    """The sandbox line for ``burrow doctor``.

    A helper rather than inline, because ``doctor`` is a linear list of checks
    and inlining this pushed it past the branch and statement limits the repo
    deliberately keeps low. Extracting beats suppressing: the rule is what stops
    a check list quietly becoming a control flow graph.

    Reported as a **failure** when the backend cannot be used, not as a note.
    ``sandbox.wrap`` refuses the spawn in that state, so a green tick here would
    be the last thing a user saw before a confusing spawn error — and doctor
    exists to be the place where the real cause is visible.

    The detail names what is *enforced*, not what was configured. An operator who
    set ``sandbox_network=allowlist`` on a backend that can only turn the network
    off needs to learn that here rather than from a post-incident read of the
    source.

    Defined *above* the ``@app.command("doctor")`` decorator, and that placement
    is load-bearing. When this was extracted it landed between the decorator and
    the function it decorates, so Typer registered *this* helper as the ``doctor``
    command and tried to build a Click parameter type for ``Settings`` — every
    ``burrow`` invocation then died at parse time with ``RuntimeError: Type not
    yet supported``. A decorator binds to the next definition, not the next
    name that reads well.
    """
    described = sandbox_plan(settings)
    if not described.enabled:
        return True, "disabled — lanes spawn unsandboxed"
    if not described.available:
        return False, described.reason

    parts = [described.backend]
    if described.enforced:
        parts.append(f"enforces {', '.join(described.enforced)}")
    if described.unenforced:
        parts.append(f"NOT enforced: {', '.join(described.unenforced)}")
    return True, " · ".join(parts)


def _config_row(config: ResolvedConfig) -> dict[str, object]:
    """The ``openburrow.yaml`` row for ``burrow doctor``, which has three outcomes.

    A helper rather than inline, for the reason ``_sandbox_check`` gives: the
    branch and statement limits are what stop this command's linear list of
    checks quietly becoming a control-flow graph.

    The third outcome is the point. ``load_config`` falls back to built-in
    defaults when the file is absent, so the call succeeding does not mean the
    file is there. Reporting ``True`` unconditionally printed
    ``✓ openburrow.yaml  /path/to/openburrow.yaml`` for a file that did not
    exist — on the first command the README tells anyone to run, and the one
    whose entire job is to say what is actually wrong.

    Absent is not a failure: the defaults are valid, and CI relies on
    ``doctor`` exiting 0 on a fresh checkout. So it gets the informational
    marker (``ok: None``, rendered as ``·`` and left out of the pass count)
    rather than a tick or a cross.
    """
    path = config.paths.config_file
    if path.is_file():
        return {"check": "openburrow.yaml", "ok": True, "detail": str(path), "fixable": False}
    return {
        "check": "openburrow.yaml",
        "ok": None,
        "detail": f"absent ({path}) — running on built-in defaults; `burrow init` writes it",
        "fixable": False,
    }


class _Check(Protocol):
    """The ``check`` closure ``doctor`` passes down to its helpers.

    A protocol rather than ``Callable[[str, bool, str, bool], bool]``
    because the closure has defaults and a ``Callable`` type cannot express
    them: it describes four *required positional* parameters, so the three-
    argument call for ``runtime writable`` is a type error against it.
    """

    def __call__(self, name: str, ok: bool, detail: str = "", fixable: bool = False) -> bool: ...


def _path_checks(
    check: _Check,
    config: ResolvedConfig,
    *,
    fix: bool,
) -> None:
    """The four directory rows, plus whether the runtime dir is writable.

    ``check`` is passed in rather than reimplemented here. A helper that built
    the rows itself would report the same information and change what the
    command does: ``check`` is also what records a failure in ``problems``, and
    ``problems`` is what decides the exit code.
    """
    paths = config.paths
    for label, directory in (
        ("runtime dir", paths.runtime_dir),
        ("global dir", paths.global_dir),
        ("worktree root", paths.worktree_root),
        ("cache dir", paths.cache_dir),
    ):
        exists = directory.exists()
        if not exists and fix:
            directory.mkdir(parents=True, exist_ok=True)
            exists = True
        check(label, exists, str(directory), fixable=True)

    check("runtime writable", _is_writable(paths.runtime_dir), str(paths.runtime_dir))


@app.command("doctor")
def doctor(
    ctx: typer.Context,
    fix: Annotated[bool, typer.Option("--fix", help="Create missing directories.")] = False,
    network: Annotated[
        bool, typer.Option("--network", help="Also probe LLM provider endpoints.")
    ] = False,
) -> None:
    """Check the environment and report what is wrong, in dependency order.

    Checks run from the outside in — Python, then repo, then config, then paths,
    then database, then adapters, then protocols — because a failure at an outer
    layer makes inner results meaningless and reporting them all as "failed"
    would bury the actual cause.
    """
    context: CliContext = ctx.obj
    results: list[dict[str, object]] = []
    problems: list[str] = []

    def check(name: str, ok: bool, detail: str = "", fixable: bool = False) -> bool:
        results.append({"check": name, "ok": ok, "detail": detail, "fixable": fixable})
        if not ok:
            problems.append(name)
        return ok

    # --- 1. runtime --------------------------------------------------------
    check(
        "python",
        sys.version_info >= (3, 12),
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )
    check("version", True, f"openburrow {__version__}")

    git = shutil.which("git")
    check("git", git is not None, git or "not found on PATH")

    # --- 2. repository -----------------------------------------------------
    repo_root = find_repo_root_or_none()
    check("repository", repo_root is not None, str(repo_root) if repo_root else "no repo found")

    config = None
    if repo_root is not None:
        try:
            config = load_config(repo_root)
        except OpenBurrowError as exc:
            check("openburrow.yaml", False, str(getattr(exc, "message", exc)))
        else:
            results.append(_config_row(config))
    else:
        check("openburrow.yaml", False, "skipped — no repository")

    # --- 3. paths ----------------------------------------------------------
    if config is not None:
        _path_checks(check, config, fix=fix)

    # --- 4. database -------------------------------------------------------
    if config is not None:
        import asyncio

        from openburrow.core.db.engine import init_database

        try:
            database = asyncio.run(init_database(config.db_url))
            health = asyncio.run(database.healthcheck())
            asyncio.run(database.close())
            check(
                "database",
                bool(health.get("ok")),
                f"{health.get('journal_mode', '?')} mode, schema v{health.get('schema_version', '?')}",
            )
        except Exception as exc:
            check("database", False, str(exc))

    # --- 5. adapters -------------------------------------------------------
    if config is not None:
        registry = build_registry(config.settings)
        availability = registry.available()
        installed = [name for name, ok in availability.items() if ok and name != "mock"]
        check(
            "harnesses",
            bool(installed),
            ", ".join(installed) if installed else "none installed — mock adapter only",
        )
        for row in registry.describe_all():
            if row["builtin"] and not row["installed"]:
                results.append(
                    {
                        "check": f"  harness:{row['name']}",
                        "ok": None,
                        "detail": f"not installed ({row['binary']})",
                        "fixable": False,
                    }
                )

    # --- 6. protocols ------------------------------------------------------
    if config is not None:
        check(
            "a2a bus",
            config.bus.enabled,
            f"{config.bus.transport}, protocol {config.bus.protocol_version}",
        )
        check(
            "governance",
            config.governance.enabled,
            f"bounded inheritance, max depth {config.governance.max_delegation_depth}",
        )
        providers = config.settings.approved_provider_keys
        configured = [name for name, ok in providers.items() if ok and name != "ollama"]
        check(
            "llm providers",
            bool(configured),
            ", ".join(configured) if configured else "none configured (Radar/classifier disabled)",
        )

        # --- sandbox -------------------------------------------------------
        sandbox_ok, sandbox_detail = _sandbox_check(config.settings)
        check("sandbox", sandbox_ok, sandbox_detail)

    # --- 7. optional network ----------------------------------------------
    if network and config is not None:
        import asyncio

        check("network", *_probe_providers(config))

    # --- render ------------------------------------------------------------
    def render(_: object) -> None:
        ok_count = sum(1 for r in results if r["ok"] is True)
        total = sum(1 for r in results if r["ok"] is not None)
        banner(
            "burrow doctor",
            subtitle=f"{ok_count}/{total} checks passed"
            + (f" · {len(problems)} problem(s)" if problems else " · all good"),
            style="green" if not problems else "yellow",
        )
        for result in results:
            state_ = result["ok"]
            if state_ is True:
                marker = "[green]✓[/green]"
            elif state_ is False:
                marker = "[red]✗[/red]"
            else:
                marker = "[dim]·[/dim]"
            console.print(f"  {marker} {result['check']:<22} [dim]{result['detail']}[/dim]")
        if problems:
            section("Problems")
            for problem in problems:
                console.print(f"  [yellow]![/yellow] {problem}")
            if fix:
                info("Re-run without --fix if directories were created.")

    emit(context, {"results": results, "problems": problems}, human_renderer=render)
    if problems and not context.is_json:
        raise typer.Exit(code=1)


def _is_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _probe_providers(config: object) -> tuple[bool, str]:
    """Optionally probe provider endpoints. Never sends credentials anywhere new."""
    import httpx

    settings = config.settings  # type: ignore[attr-defined]
    targets: list[tuple[str, str]] = []
    if settings.openai_api_key:
        targets.append(("openai", settings.openai_api_base))
    if settings.anthropic_api_key:
        targets.append(("anthropic", settings.anthropic_api_base))
    if settings.ollama_api_base:
        targets.append(("ollama", settings.ollama_api_base))

    reachable: list[str] = []
    for name, url in targets:
        try:
            httpx.get(url, timeout=3.0)
            reachable.append(name)
        except httpx.HTTPError:
            continue
    return bool(reachable) or not targets, ", ".join(
        reachable
    ) if reachable else "no endpoints probed"


# ---------------------------------------------------------------------------
# burrow config
# ---------------------------------------------------------------------------
config_app = typer.Typer(help="Inspect and validate openburrow.yaml.", no_args_is_help=True)
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show(
    ctx: typer.Context,
    resolved: Annotated[
        bool,
        typer.Option("--resolved", help="Show the merged view including environment overrides."),
    ] = False,
) -> None:
    """Print the effective configuration."""
    context: CliContext = ctx.obj
    config = context.config()

    payload = config.as_public_dict() if resolved else config.repo.model_dump(mode="json")

    def render(data: dict) -> None:
        section("openburrow.yaml" if not resolved else "resolved configuration")
        print_kv({k: v for k, v in data.items() if not isinstance(v, dict)})
        for key, value in data.items():
            if isinstance(value, dict):
                section(key)
                print_kv(value)

    emit(context, payload, human_renderer=render)


@config_app.command("validate")
def config_validate(ctx: typer.Context) -> None:
    """Validate the committed configuration without running anything."""
    context: CliContext = ctx.obj
    try:
        config = context.config()
    except OpenBurrowError as exc:
        emit(context, {"valid": False, "error": str(getattr(exc, "message", exc))})
        if not context.is_json:
            from openburrow.cli.output import render_error

            render_error(exc)
        raise typer.Exit(code=1) from exc

    problems: list[str] = []
    for lane in config.repo.lanes:
        if lane.harness not in config.adapters.enabled:
            problems.append(
                f"lane '{lane.name}' uses harness '{lane.harness}', which is not in adapters.enabled"
            )

    payload = {
        "valid": not problems,
        "config_file": str(config.paths.config_file),
        "lanes": len(config.repo.lanes),
        "problems": problems,
    }

    def render(data: dict) -> None:
        if data["valid"]:
            success(f"configuration is valid ({data['lanes']} lane template(s))")
        else:
            for problem in data["problems"]:
                failure(problem)

    emit(context, payload, human_renderer=render)
    if problems:
        raise typer.Exit(code=1)


@config_app.command("migrate")
def config_migrate(
    ctx: typer.Context,
    path: Annotated[
        Path | None,
        typer.Option("--path", help="Configuration file; defaults to this repo's."),
    ] = None,
    write: Annotated[
        bool,
        typer.Option("--write", help="Rewrite the file, keeping the previous one as .bak."),
    ] = False,
    check: Annotated[
        bool,
        typer.Option("--check", help="Exit non-zero if migration is needed. Writes nothing."),
    ] = False,
) -> None:
    """Upgrade openburrow.yaml to the schema version this build understands.

    Reports rather than rewrites unless --write is given, because the write is a
    full re-serialisation and YAML comments do not survive it. --check is the same
    plan with the exit code flipped, so CI can assert that the committed
    configuration is current without owning a copy of it.
    """
    context: CliContext = ctx.obj
    if write and check:
        failure("--write and --check are opposites; pass one of them")
        raise typer.Exit(code=2)

    # The target path is derived without loading the configuration, and that is
    # the whole point of this command: `burrow config migrate` exists to repair a
    # file this build cannot parse, so routing the lookup through
    # `context.config()` would make it fail for exactly the input it is for. It
    # did — a repo whose `openburrow.yaml` declared a future schema version raised
    # `ConfigSchemaError` while resolving the path, so the command named by that
    # error's own hint could never run. `--path` stays as the override for a file
    # that is somewhere other than the repo root.
    target = (
        path if path is not None else BurrowPaths.for_repo(context.repo_root_path()).config_file
    )

    try:
        plan = plan_migration(target)
    except OpenBurrowError as exc:
        # One funnel call rather than `emit` plus a side-channel print, so
        # `--json` gets the payload and the human path gets the formatted error
        # with its hint, decided in the same place. `config validate` above does
        # the second half separately, which is why its failure prints the error
        # twice — worth aligning if either is touched again.
        #
        # Bound to a local because `except ... as exc` deletes the name when the
        # handler ends, so a closure that captured it directly is a reference to
        # a name the interpreter is about to unbind.
        error = exc

        def render_failure(_: object) -> None:
            render_error(error)

        emit(
            context,
            {
                "path": str(target),
                "needed": None,
                "written": False,
                "error": str(getattr(exc, "message", exc)),
            },
            human_renderer=render_failure,
        )
        raise typer.Exit(code=1) from exc

    written = write and plan.needed
    if written:
        apply_migration(plan)

    payload = {**plan.as_dict(), "written": written}

    def render(data: dict) -> None:
        section(str(data["path"]))
        declared = data["declared"]
        print_kv(
            {
                "declared": "absent" if declared is None else declared,
                "supported": data["supported"],
                "steps": ", ".join(str(step) for step in data["steps"]) or "—",
            }
        )
        if data["unknown_keys"]:
            warn("preserved, not interpreted by this build: " + ", ".join(data["unknown_keys"]))
        if data["written"]:
            success(f"migrated · previous file kept as {Path(str(data['path'])).name}.bak")
        elif not data["needed"]:
            success("already current")
        else:
            info("needs migrating; re-run with --write to apply")

    emit(context, payload, human_renderer=render)

    if check and plan.needed:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# burrow adapters
# ---------------------------------------------------------------------------
adapters_app = typer.Typer(help="List and inspect harness adapters.", no_args_is_help=True)
app.add_typer(adapters_app, name="adapters")


@adapters_app.command("list")
def adapters_list(ctx: typer.Context) -> None:
    """List every known adapter and whether it is installed."""
    context: CliContext = ctx.obj
    config = context.config_or_none()
    settings = (
        config.settings
        if config
        else __import__("openburrow.core.config.settings", fromlist=["get_settings"]).get_settings()
    )
    registry = build_registry(settings)
    rows = registry.describe_all()

    def render(data: list[dict]) -> None:
        table(
            ["harness", "installed", "structured", "mcp", "resumable", "binary"],
            [
                [
                    row["name"],
                    "[green]yes[/green]" if row["installed"] else "[dim]no[/dim]",
                    row["capabilities"].get("structuredOutput", False),
                    row["capabilities"].get("mcpTools", False),
                    row["capabilities"].get("resumable", False),
                    row["binary"] or "-",
                ]
                for row in data
            ],
            caption="Capability flags are declarations the governance layer spot-checks.",
        )

    emit(context, rows, human_renderer=render)


@adapters_app.command("health")
def adapters_health(ctx: typer.Context) -> None:
    """Run each adapter's pre-flight check."""
    import asyncio

    context: CliContext = ctx.obj
    config = context.config_or_none()
    if config is None:
        failure("no repository found")
        raise typer.Exit(code=1)

    registry = build_registry(config.settings)
    results: list[dict[str, object]] = []

    async def probe_all() -> None:
        for name in registry.names():
            try:
                adapter = registry.create(name)
                ok, detail = await adapter.healthcheck()
            except Exception as exc:
                ok, detail = False, str(exc)
            results.append({"harness": name, "ok": ok, "detail": detail})

    asyncio.run(probe_all())

    def render(data: list[dict]) -> None:
        for row in data:
            marker = "[green]✓[/green]" if row["ok"] else "[dim]·[/dim]"
            console.print(f"  {marker} {row['harness']:<14} [dim]{row['detail']}[/dim]")

    emit(context, results, human_renderer=render)


# ---------------------------------------------------------------------------
# burrow completion
# ---------------------------------------------------------------------------
@app.command("completion")
def completion(
    ctx: typer.Context,
    shell: Annotated[
        str,
        typer.Argument(help="Target shell: bash, zsh, fish, or pwsh."),
    ],
) -> None:
    """Print the shell completion script for the given shell.

    Deliberately delegates to Click/Typer's own generator instead of shipping a
    hand-written script: the generated code has to stay in lockstep with the
    option names the framework parses, and two sources of that truth is how a
    completion script starts suggesting flags that no longer exist.

    Usage is printed rather than the script itself when the shell is unknown —
    a completion script for the wrong shell silently does nothing once sourced,
    which is worse than an error at request time.
    """
    from click.shell_completion import get_completion_class

    # Click ships generators for exactly these three; `pwsh` is provided by
    # Typer only when its optional `pwsh` extra is installed, so it is not
    # offered here rather than offered and failing on a stock install.
    supported = ("bash", "zsh", "fish")
    if shell not in supported:
        allowed = ", ".join(supported)
        typer.echo(f"unknown shell {shell!r} — supported: {allowed}", err=True)
        raise typer.Exit(code=2)

    complete_cls = get_completion_class(shell)
    if complete_cls is None:  # pragma: no cover - click returns None only for unknown shells
        typer.echo(f"no completion support for {shell!r}", err=True)
        raise typer.Exit(code=2)
    # The instance is bound to the root `burrow` group (the command this one is
    # running inside), so the generated script always describes the app that
    # printed it rather than a snapshot that can drift from the real flags.
    # `ctx.parent` is typed Optional but is never None inside a running
    # subcommand — narrowing it here records that invariant for the reader too.
    assert ctx.parent is not None
    root_command = ctx.parent.command
    complete = complete_cls(
        # typer re-exports click's Command under its own module path, which
        # mypy treats as a nominal mismatch even though it is the same class
        # at runtime. Suppressing here rather than re-wrapping the object.
        root_command,  # type: ignore[arg-type]
        {},
        "burrow",
        "_BURROW_COMPLETE",
    )
    typer.echo(complete.source())


# ---------------------------------------------------------------------------
# burrow version
# ---------------------------------------------------------------------------
@app.command("version")
def version(
    ctx: typer.Context,
    check: Annotated[bool, typer.Option("--check", help="Check for a newer release.")] = False,
) -> None:
    """Print version and protocol information."""
    context: CliContext = ctx.obj
    payload = {
        "version": __version__,
        "schema_version": version_info.schema_version,
        "a2a_protocol_version": version_info.a2a_protocol_version,
        "control_plane_api_version": version_info.control_plane_api_version,
        "repo_config_schema_version": version_info.repo_config_schema_version,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "update_available": None,
    }

    if check:
        payload["update_available"] = _check_for_update(context)

    def render(data: dict) -> None:
        banner("OpenBurrow", subtitle=f"v{data['version']}", style="cyan")
        print_kv(
            {
                "schema": data["schema_version"],
                "a2a protocol": data["a2a_protocol_version"],
                "control plane": data["control_plane_api_version"],
                "python": data["python"],
            }
        )
        if data.get("update_available"):
            warn("a newer release is available")

    emit(context, payload, human_renderer=render)


def _check_for_update(context: CliContext) -> bool | None:
    """Best-effort update check. Silent on any failure — it is a nicety, not a feature."""
    config = context.config_or_none()
    if config is None:
        # Stated rather than fallen into. The previous version reached
        # `config.settings` on a `None` from inside a bare `except Exception`,
        # so the AttributeError was swallowed and this returned None anyway —
        # the behaviour was right by accident and the code did not say so.
        # Skipping without a repo config is a deliberate choice, not a
        # consequence: this check reads its setting from the repo config, so
        # with no config there is nothing to read.
        return None
    if not config.settings.update_check:
        return None
    try:
        import httpx

        # A real attribute, not `settings.__dict__.get(...)`. That workaround
        # existed because `update_manifest_url` was never declared on Settings;
        # `.get` on a missing key returns None, so the documented
        # OPENBURROW_UPDATE_MANIFEST_URL was read, discarded, and replaced with
        # the literal below on every single call.
        response = httpx.get(config.settings.update_manifest_url, timeout=3.0)
        if response.status_code != 200:
            return None
        latest = str(response.json().get("latest", ""))
        return bool(latest and latest != __version__)
    except Exception:
        return None


__all__ = ["adapters_app", "app", "config_app"]
