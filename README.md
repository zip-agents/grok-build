# zip-service

Streaming multipart-to-ZIP HTTP service.

```bash
uv sync --frozen --group dev
uv run python -m zip_service
uv run pytest -m "not slow"
uv run pytest -m slow
```

`unzip` and `7z` must be on `PATH`. Integration tests start a real server process.
