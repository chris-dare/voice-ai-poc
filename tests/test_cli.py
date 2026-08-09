from __future__ import annotations

import asyncio
import signal

import pytest

from voice_ai.cli import _run_until_termination


@pytest.mark.asyncio
async def test_worker_stop_signal_cancels_long_running_work(monkeypatch) -> None:
    loop = asyncio.get_running_loop()
    callbacks: dict[signal.Signals, object] = {}
    removed: list[signal.Signals] = []
    started = asyncio.Event()
    cleaned_up = asyncio.Event()

    def capture_handler(process_signal, callback) -> None:
        callbacks[process_signal] = callback

    monkeypatch.setattr(loop, "add_signal_handler", capture_handler)
    monkeypatch.setattr(loop, "remove_signal_handler", lambda value: removed.append(value))

    async def worker() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    task = asyncio.create_task(_run_until_termination(worker()))
    await started.wait()
    callback = callbacks[signal.SIGTERM]
    assert callable(callback)
    callback()
    await asyncio.wait_for(task, timeout=1)

    assert cleaned_up.is_set()
    assert set(removed) == {signal.SIGINT, signal.SIGTERM}
