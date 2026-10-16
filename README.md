# zip-service

Streaming multipart-to-ZIP service. Archive names, entry names, multipart parsing, and the ZIP writer are covered by unit tests.

```bash
uv sync --frozen --group dev
uv run pytest tests/unit
```
