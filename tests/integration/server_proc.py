"""Real server processes. Each server gets its own port and a private cwd."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

_CONFIG_KEYS = (
    "PORT",
    "LOG_LEVEL",
    "ZIP_COMPRESSION_LEVEL",
    "SHUTDOWN_GRACE_SECONDS",
    "MAX_FILES",
    "MAX_CONCURRENT_REQUESTS",
    "IDLE_TIMEOUT_SECONDS",
    "MAX_REQUEST_DURATION_SECONDS",
    "OUTPUT_BUFFER_MAX_BYTES",
    "ZIP_SERVICE_TEST_HOOKS",
)


class RunningServer:
    def __init__(self, tmp_path: Path, env: dict[str, str]) -> None:
        self.port = _free_port()
        self.cwd = tmp_path
        self._stdout: list[bytes] = []
        self._stderr: list[bytes] = []
        child_env = os.environ.copy()
        for key in _CONFIG_KEYS:
            child_env.pop(key, None)
        child_env.update(env)
        child_env["PORT"] = str(self.port)
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        child_env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "zip_service"],
            cwd=tmp_path,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert self.proc.stdout is not None
        assert self.proc.stderr is not None
        self._threads = [
            threading.Thread(target=_drain, args=(self.proc.stdout, self._stdout), daemon=True),
            threading.Thread(target=_drain, args=(self.proc.stderr, self._stderr), daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        self._wait_until_live()

    def _wait_until_live(self) -> None:
        deadline = time.monotonic() + 10
        url = f"http://127.0.0.1:{self.port}/health/live"
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(self.output())
            try:
                with urllib.request.urlopen(url, timeout=0.3) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(0.05)
        raise RuntimeError(self.output())

    def output(self) -> str:
        stdout = b"".join(self._stdout).decode(errors="replace")
        stderr = b"".join(self._stderr).decode(errors="replace")
        return f"stdout:\n{stdout}\nstderr:\n{stderr}"

    def logs(self) -> list[dict[str, object]]:
        text = b"".join(self._stdout).decode(errors="replace")
        parsed: list[dict[str, object]] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            parsed.append(json.loads(line))
        return parsed

    def zip_logs(self) -> list[dict[str, object]]:
        return [line for line in self.logs() if line.get("message") == "zip request finished"]

    def wait_zip_logs(self, count: int = 1, timeout: float = 2) -> list[dict[str, object]]:
        """Poll until a completion line is visible.

        The success line is written after the terminating chunk, so a client
        can observe the body before the log reaches the captured stdout.
        """

        deadline = time.monotonic() + timeout
        found = self.zip_logs()
        while len(found) < count and time.monotonic() < deadline:
            time.sleep(0.02)
            found = self.zip_logs()
        return found

    def stop(self) -> int:
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            code = self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = self.proc.wait(timeout=5)
        for thread in self._threads:
            thread.join(timeout=2)
        return code


def _drain(pipe: object, sink: list[bytes]) -> None:
    assert hasattr(pipe, "readline")
    while True:
        line = pipe.readline()
        if not line:
            return
        sink.append(line)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def server(tmp_path: Path) -> Iterator[RunningServer]:
    running = RunningServer(tmp_path, {})
    try:
        yield running
    finally:
        running.stop()


@pytest.fixture
def make_server(tmp_path: Path) -> Iterator[object]:
    started: list[RunningServer] = []

    def start(**env: str) -> RunningServer:
        running = RunningServer(tmp_path, {key: str(value) for key, value in env.items()})
        started.append(running)
        return running

    yield start
    for running in started:
        if running.proc.poll() is None:
            running.stop()
