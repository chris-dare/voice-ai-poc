from __future__ import annotations

import argparse
import asyncio
import json
from uuid import uuid4

from voice_ai.agent.models import resolve_model
from voice_ai.agent.protocol import AgentTurnRequest
from voice_ai.agent.runtime import AgentRuntime
from voice_ai.shared.config import Settings


async def run(prompts: list[str]) -> int:
    settings = Settings()
    selection = resolve_model(settings)
    print(
        json.dumps(
            {
                "model": selection.primary_id,
                "provider": selection.provider,
                "fallbacks": selection.fallback_ids,
                "configuration_ready": not selection.configuration_errors,
            }
        )
    )
    runtime = AgentRuntime(settings)
    failed = False
    session_id = uuid4()
    try:
        for index, prompt in enumerate(prompts, start=1):
            print(json.dumps({"turn": index, "prompt": prompt}))
            async for event in runtime.stream_turn(
                AgentTurnRequest(session_id=session_id, text=prompt)
            ):
                print(event.model_dump_json())
                failed = failed or event.type == "error"
    finally:
        await runtime.shutdown()
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one real configured-model agent turn.")
    parser.add_argument("prompts", nargs="+")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.prompts)))


if __name__ == "__main__":
    main()
