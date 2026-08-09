from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

import typer
import uvicorn
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from voice_ai.shared.config import get_agent_settings, get_settings, get_voice_settings
from voice_ai.shared.startup import PROCESS_STARTED_AT  # noqa: F401

app = typer.Typer(
    name="voice-ai",
    help="Operate the local voice AI assistant.",
    no_args_is_help=True,
)
console = Console()


@app.command()
def serve(
    host: str | None = typer.Option(None, help="Bind host; defaults to HOST."),
    port: int | None = typer.Option(None, help="Bind port; defaults to PORT."),
    reload: bool = typer.Option(False, help="Reload on Python source changes."),
) -> None:
    """Serve the public voice gateway and bundled browser client."""
    settings = get_voice_settings()
    uvicorn.run(
        "voice_ai.voice.app:create_app",
        factory=True,
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
        log_level=settings.log_level.lower(),
    )


@app.command()
def agent(
    host: str | None = typer.Option(None, help="Private bind host; defaults to AGENT_HOST."),
    port: int | None = typer.Option(None, help="Private port; defaults to AGENT_PORT."),
    reload: bool = typer.Option(False, help="Reload on Python source changes."),
) -> None:
    """Serve the private Pydantic AI reasoning and tool service."""
    settings = get_agent_settings()
    uvicorn.run(
        "voice_ai.agent.app:create_agent_app",
        factory=True,
        host=host or settings.agent_host,
        port=port or settings.agent_port,
        reload=reload,
        log_level=settings.log_level.lower(),
    )


@app.command()
def worker() -> None:
    """Run the durable agent-response execution worker."""
    asyncio.run(_run_worker())


async def _run_worker() -> None:
    from voice_ai.agent.api.services import AgentApiService
    from voice_ai.agent.capacity import DistributedExecutionCapacity
    from voice_ai.agent.persistence.database import Database
    from voice_ai.agent.runtime import AgentRuntime
    from voice_ai.shared.observability import configure_observability, instrument_sqlalchemy

    settings = get_agent_settings()
    configure_observability(settings, service_name="voice-ai-agent-worker")
    database = Database(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
    )
    instrument_sqlalchemy(database.engine)
    runtime = AgentRuntime(
        settings,
        execution_capacity=DistributedExecutionCapacity(database, settings),
    )
    service = AgentApiService(settings, database, runtime)
    try:
        await runtime.startup()
        await service.startup(start_worker=False)
        await _run_until_termination(service.run_worker_forever())
    finally:
        await service.shutdown()
        await runtime.shutdown()
        await database.close()


async def _run_until_termination(worker: Awaitable[None]) -> None:
    """Cancel a long-running worker cleanly when the process receives a stop signal."""
    loop = asyncio.get_running_loop()
    stop_requested = asyncio.Event()
    installed_signals: list[signal.Signals] = []
    for process_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(process_signal, stop_requested.set)
        except (NotImplementedError, RuntimeError):
            continue
        installed_signals.append(process_signal)

    worker_task = asyncio.create_task(worker, name="agent-worker-main")
    stop_task = asyncio.create_task(stop_requested.wait(), name="agent-worker-stop-signal")
    try:
        done, _pending = await asyncio.wait(
            {worker_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if worker_task in done:
            await worker_task
            return
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
    finally:
        stop_task.cancel()
        worker_task.cancel()
        await asyncio.gather(stop_task, worker_task, return_exceptions=True)
        for process_signal in installed_signals:
            loop.remove_signal_handler(process_signal)


@app.command()
def seed(
    reset_demo: bool = typer.Option(
        False,
        "--reset-demo",
        hidden=True,
    ),
) -> None:
    """Apply database migrations (the legacy option is accepted but ignored)."""
    asyncio.run(_seed(reset_demo))


async def _seed(reset_demo: bool) -> None:
    from voice_ai.agent.persistence.database import Database
    from voice_ai.migrations import upgrade_database

    settings = get_agent_settings()
    database = Database(settings.database_url)
    try:
        await upgrade_database()
        console.print("[green]Database migrations applied.[/green]")
    finally:
        await database.close()


@app.command("evals")
def evals_command(
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help="Call the configured model and tools; omitted means deterministic contract mode.",
        ),
    ] = False,
    suite: Annotated[
        Path | None,
        typer.Option(help="Path to a versioned evaluation suite JSON file."),
    ] = None,
    modality: Annotated[
        Literal["text", "voice"],
        typer.Option(help="Evaluation contract to run."),
    ] = "text",
    judge_model: Annotated[
        str | None,
        typer.Option(
            help=(
                "Optional Pydantic AI model ID for case-specific answer-quality judging. "
                "Used only with --live text evaluations."
            )
        ),
    ] = None,
) -> None:
    """Run the agent-service evaluation release gate."""
    from voice_ai.agent.eval_persistence import persist_eval_run
    from voice_ai.agent.evals import (
        DEFAULT_SUITE_PATH,
        EvalPreflightError,
        gate_summary,
        run_eval_gate,
    )
    from voice_ai.agent.voice_evals import (
        DEFAULT_VOICE_SUITE_PATH,
        run_voice_eval_gate,
    )
    from voice_ai.shared.observability import configure_observability, record_eval_run

    settings = get_agent_settings()
    configure_observability(settings, service_name="voice-ai-evals")
    if modality == "voice":
        if live:
            raise typer.BadParameter(
                "Live voice evaluation requires recorded, consented audio fixtures and a "
                "hardware-profile runner; use contract mode until those fixtures are configured."
            )
        suite_path = suite or DEFAULT_VOICE_SUITE_PATH
        result = asyncio.run(run_voice_eval_gate(suite_path=suite_path))
    else:
        suite_path = suite or DEFAULT_SUITE_PATH
        try:
            result = asyncio.run(
                run_eval_gate(
                    settings,
                    live=live,
                    suite_path=suite_path,
                    judge_model=judge_model,
                )
            )
        except EvalPreflightError as exc:
            console.print(
                Panel.fit(
                    f"[bold red]Live evaluation did not start.[/bold red]\n"
                    f"Model: {exc.model}\nReason: {exc.detail}\n"
                    "No evaluation cases or judge calls were made.",
                    title="Model preflight",
                    border_style="red",
                )
            )
            raise typer.Exit(2) from exc
    run_id = asyncio.run(persist_eval_run(settings, result, live=live, suite_path=suite_path))
    result.report.print(include_output=not result.passed, include_reasons=True)
    console.print(gate_summary(result, live=live))
    record_eval_run(
        suite_version=result.suite_version,
        mode="live" if live else "contract",
        model=settings.agent_model if live else "deterministic-fixture",
        assertion_pass_rate=result.assertion_pass_rate,
        failed_cases=result.failed_cases,
        passed=result.passed,
        run_id=run_id,
    )
    console.print(f"[dim]Persisted eval run: {run_id}[/dim]")
    if not result.passed:
        raise typer.Exit(1)


@app.command()
def doctor(
    fix: bool = typer.Option(False, help="Provision missing local dependencies and demo data."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask before applying fixes."),
    offline: bool = typer.Option(
        False,
        help="Perform a demo-day check without downloading or repairing anything.",
    ),
    smoke: bool = typer.Option(
        False,
        help="After checks pass, run STT, tool-selection, and TTS inference.",
    ),
) -> None:
    """Check whether this checkout can survive a live demonstration."""
    if fix and offline:
        raise typer.BadParameter("--fix and --offline cannot be used together")
    if fix and not yes and not typer.confirm("Provision models, database data and browser assets?"):
        raise typer.Abort()
    code = asyncio.run(_doctor(fix=fix, offline=offline, smoke=smoke))
    if code:
        raise typer.Exit(code)


async def _doctor(*, fix: bool, offline: bool, smoke: bool) -> int:
    from voice_ai.agent.persistence.database import Database
    from voice_ai.voice.health import overall_status, run_checks

    settings = get_settings()
    database = Database(settings.database_url)
    try:
        if fix:
            await _apply_fixes(settings, database)
        checks, _capabilities = await run_checks(settings, database)
        if smoke and overall_status(checks) != "not_ready":
            checks.append(await _smoke_check(settings))
        _render_checks(checks, offline=offline)
        return 1 if any(check.status == "fail" for check in checks) else 0
    finally:
        await database.close()


async def _apply_fixes(settings: Any, database: Any) -> None:
    actions: list[tuple[str, Callable[[], Awaitable[None]]]] = [
        ("Building the local browser bundle", _build_frontend),
        ("Downloading the Whisper model", lambda: _download_whisper(settings.whisper_model)),
        ("Downloading the sentence tokenizer", lambda: _download_nltk(settings)),
        ("Downloading the Kokoro voice model", lambda: _download_kokoro(settings)),
        (
            "Applying database migrations and restoring demo data",
            lambda: _repair_database(database),
        ),
    ]
    if settings.agent_model.startswith("ollama:"):
        actions.append(("Checking the configured Ollama model", lambda: _repair_ollama(settings)))
    for label, action in actions:
        with console.status(f"[bold green]{label}…[/bold green]"):
            try:
                await action()
            except Exception as exc:
                console.print(f"[yellow]Warning:[/yellow] {label} failed: {exc}")


async def _build_frontend() -> None:
    root = Path(__file__).parent.parent
    install = await asyncio.create_subprocess_exec(
        "npm",
        "--prefix",
        str(root / "frontend"),
        "ci",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await install.communicate()
    if install.returncode:
        raise RuntimeError(stderr.decode("utf-8", "replace"))
    build = await asyncio.create_subprocess_exec(
        "npm",
        "--prefix",
        str(root / "frontend"),
        "run",
        "build",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await build.communicate()
    if build.returncode:
        raise RuntimeError(stderr.decode("utf-8", "replace"))


async def _download_whisper(model: str) -> None:
    from faster_whisper.utils import download_model

    await asyncio.to_thread(download_model, model)


async def _download_nltk(settings) -> None:
    from voice_ai.voice.speech.assets import provision_punkt_tab

    destination = settings.model_cache_dir / "nltk"
    await asyncio.to_thread(provision_punkt_tab, destination)


async def _download_kokoro(settings) -> None:
    from voice_ai.voice.speech.assets import provision_kokoro

    await asyncio.to_thread(provision_kokoro, settings.kokoro_download_dir)


async def _repair_database(database: Any) -> None:
    from voice_ai.migrations import upgrade_database

    await upgrade_database()


async def _repair_ollama(settings) -> None:
    from voice_ai.agent.capabilities import probe_ollama

    model = settings.agent_model.split(":", 1)[1]
    capabilities = await probe_ollama(settings.ollama_base_url, model)
    if not capabilities.reachable:
        raise RuntimeError("Ollama is not running. Start it with: ollama serve")
    if capabilities.installed:
        return
    process = await asyncio.create_subprocess_exec("ollama", "pull", model)
    if await process.wait():
        raise RuntimeError(f"ollama pull {model} failed")


async def _smoke_check(settings: Any) -> Any:
    """Run actual local STT, tool selection, and TTS inference."""
    try:
        import numpy as np
        from faster_whisper import WhisperModel
        from kokoro_onnx import Kokoro

        from voice_ai.agent.protocol import AgentTurnRequest, TextDelta, ToolStarted
        from voice_ai.agent.runtime import AgentRuntime
        from voice_ai.voice.health import CheckResult

        whisper = await asyncio.to_thread(
            WhisperModel,
            settings.whisper_model,
            device="cpu",
            compute_type="int8",
        )

        def transcribe_silence() -> None:
            segments, _ = whisper.transcribe(np.zeros(16_000, dtype=np.float32), language="en")
            list(segments)

        await asyncio.to_thread(transcribe_silence)

        runtime = AgentRuntime(settings)
        try:
            events = [
                event
                async for event in runtime.stream_turn(
                    AgentTurnRequest(
                        session_id=uuid4(),
                        text=(
                            "Use the Python sandbox to calculate (19 * 23) - 46. Return the result."
                        ),
                    )
                )
            ]
        finally:
            await runtime.shutdown()
        if not any(isinstance(event, ToolStarted) and event.tool == "run_code" for event in events):
            raise RuntimeError("The configured model did not use sandboxed code")
        answer = "".join(event.text for event in events if isinstance(event, TextDelta))
        if "391" not in answer:
            raise RuntimeError("The configured model returned the wrong calculation result")

        model_path = settings.kokoro_download_dir / "kokoro-v1.0.onnx"
        voices_path = settings.kokoro_download_dir / "voices-v1.0.bin"
        voice = await asyncio.to_thread(Kokoro, str(model_path), str(voices_path))
        audio_bytes = 0
        async for samples, _sample_rate in voice.create_stream(
            "System ready.",
            voice=settings.kokoro_voice,
            lang="en-us",
            speed=1.0,
        ):
            audio_bytes += samples.nbytes
        if audio_bytes == 0:
            raise RuntimeError("Kokoro produced no audio")

        return CheckResult(
            "End-to-end smoke",
            "pass",
            (
                f"Whisper inference, {settings.agent_model} tool execution, and "
                "Kokoro synthesis succeeded"
            ),
        )
    except Exception as exc:
        from voice_ai.voice.health import CheckResult

        return CheckResult(
            "End-to-end smoke",
            "fail",
            f"Local inference failed: {type(exc).__name__}: {exc}",
        )


def _render_checks(checks: list[Any], *, offline: bool) -> None:
    from voice_ai.voice.health import overall_status

    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("Status", width=8)
    table.add_column("Check", width=18)
    table.add_column("Detail")
    table.add_column("Exact fix")
    styles = {"pass": ("✓ PASS", "green"), "warn": ("! WARN", "yellow"), "fail": ("✗ FAIL", "red")}
    for check in checks:
        label, style = styles[check.status]
        table.add_row(
            f"[{style}]{label}[/{style}]",
            check.name,
            check.detail,
            check.fix_command or "—",
        )
    console.print(table)
    status = overall_status(checks)
    title = "OFFLINE PREFLIGHT" if offline else "VOICE AI DOCTOR"
    message = {
        "ready": "[bold green]READY[/bold green] — the demo prerequisites are present.",
        "degraded": "[bold yellow]DEGRADED[/bold yellow] — usable, but review warnings before the demo.",
        "not_ready": "[bold red]NOT READY[/bold red] — run the exact fixes above.",
    }[status]
    console.print(Panel(message, title=title))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
