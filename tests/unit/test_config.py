from pathlib import Path

import pytest

from config import ConfigError, load_settings, parse_dotenv


def test_defaults_when_nothing_is_set() -> None:
    settings = load_settings(environ={}, dotenv_path=Path("/no/such.env"))
    assert settings.port == 8000
    assert settings.log_level == "INFO"
    assert settings.zip_compression_level == 6
    assert settings.shutdown_grace_seconds == 25
    assert settings.max_files == 500
    assert settings.max_concurrent_requests == 32
    assert settings.idle_timeout_seconds == 30
    assert settings.max_request_duration_seconds == 900
    assert settings.output_buffer_max_bytes == 16 * 1024 * 1024


def test_environment_overrides_dotenv(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PORT=1111\nLOG_LEVEL=debug\nMAX_FILES=3\n", encoding="utf-8")
    settings = load_settings(environ={"PORT": "2222"}, dotenv_path=env_file)
    assert settings.port == 2222
    assert settings.log_level == "DEBUG"
    assert settings.max_files == 3


def test_empty_string_is_invalid_not_a_default() -> None:
    with pytest.raises(ConfigError, match="PORT"):
        load_settings(environ={"PORT": ""}, dotenv_path=Path("/no/such.env"))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PORT", "0"),
        ("PORT", "65536"),
        ("PORT", "nope"),
        ("LOG_LEVEL", "verbose"),
        ("ZIP_COMPRESSION_LEVEL", "-1"),
        ("ZIP_COMPRESSION_LEVEL", "10"),
        ("SHUTDOWN_GRACE_SECONDS", "-1"),
        ("MAX_FILES", "0"),
        ("IDLE_TIMEOUT_SECONDS", "0"),
        ("OUTPUT_BUFFER_MAX_BYTES", "0"),
    ],
)
def test_rejects_out_of_range_values(name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load_settings(environ={name: value}, dotenv_path=Path("/no/such.env"))


def test_shutdown_grace_may_be_zero() -> None:
    settings = load_settings(
        environ={"SHUTDOWN_GRACE_SECONDS": "0"},
        dotenv_path=Path("/no/such.env"),
    )
    assert settings.shutdown_grace_seconds == 0


def test_dotenv_syntax(tmp_path: Path) -> None:
    text = """
# comment
export PORT=9000
LOG_LEVEL="warning"
MAX_FILES='12'
"""
    values = parse_dotenv(text)
    assert values["PORT"] == "9000"
    assert values["LOG_LEVEL"] == "warning"
    assert values["MAX_FILES"] == "12"
    env_file = tmp_path / ".env"
    env_file.write_text("NOT A LINE\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="line 1"):
        load_settings(environ={}, dotenv_path=env_file)
