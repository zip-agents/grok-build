"""Fixtures for integration tests. The server lives in ``server_proc``."""

from tests.integration.server_proc import make_server, server

__all__ = ["make_server", "server"]
