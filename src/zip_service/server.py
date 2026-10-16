"""Process entrypoint. Listens on ``0.0.0.0`` and ``PORT``."""

from __future__ import annotations

import asyncio
import signal
import sys

import uvicorn

from zip_service.app import create_app
from zip_service.config import ConfigError, load_settings
from zip_service.gate import Gate
from zip_service.jsonlog import configure_logging, emit


def main() -> int:
    """Load configuration, serve until SIGTERM or SIGINT, then exit."""

    try:
        settings = load_settings()
    except ConfigError as exc:
        sys.stderr.write(f"configuration error: {exc}\n")
        return 2

    configure_logging(settings.log_level)
    app = create_app(settings)
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=settings.port,
        log_config=None,
        access_log=False,
        log_level=settings.log_level.lower(),
        use_colors=False,
        server_header=False,
        date_header=False,
        proxy_headers=False,
        lifespan="on",
        workers=1,
        # Backstop after our own grace period has already cancelled work.
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)

    try:
        asyncio.run(_serve(server, app.state.gate, float(settings.shutdown_grace_seconds)))
    except ConfigError as exc:
        sys.stderr.write(f"configuration error: {exc}\n")
        return 2
    return 0


async def _serve(server: uvicorn.Server, gate: Gate, grace_seconds: float) -> None:
    loop = asyncio.get_running_loop()
    signals = {"count": 0}
    shutdown_task: asyncio.Task[None] | None = None

    async def drain_and_stop() -> None:
        await gate.wait_for_drain(grace_seconds)
        server.should_exit = True

    def on_signal() -> None:
        nonlocal shutdown_task
        signals["count"] += 1
        if signals["count"] == 1:
            gate.begin_shutdown()
            emit("INFO", "shutdown requested")
            shutdown_task = asyncio.create_task(drain_and_stop())
            return
        emit("WARNING", "shutdown forced")
        gate.force_stop()
        server.force_exit = True
        server.should_exit = True

    loop.add_signal_handler(signal.SIGTERM, on_signal)
    loop.add_signal_handler(signal.SIGINT, on_signal)
    # ``serve`` installs its own handlers. ``_serve`` leaves ours in place.
    await server._serve()


if __name__ == "__main__":
    sys.exit(main())
