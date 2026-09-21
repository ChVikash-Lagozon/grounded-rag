from __future__ import annotations

import io
import json
from pathlib import Path

from carquery.config import LoggingConfig
from carquery.logging import bind_context, bound_context, configure_logging, get_logger


def _json_lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_json_logging_emits_valid_json_with_bound_context() -> None:
    stream = io.StringIO()
    configure_logging(LoggingConfig(format="json"), stream=stream)

    bind_context(request_id="req-1")
    with bound_context(dataset_version="v42"):
        get_logger("test", phase="sql").info("query_done", rows=3)
    get_logger("test").info("after_block")

    first, second = _json_lines(stream)
    assert first["event"] == "query_done"
    assert first["level"] == "info"
    assert first["request_id"] == "req-1"
    assert first["dataset_version"] == "v42"
    assert first["phase"] == "sql"
    assert first["rows"] == 3
    assert "timestamp" in first
    assert second["request_id"] == "req-1"
    assert "dataset_version" not in second


def test_level_filters_records() -> None:
    stream = io.StringIO()
    configure_logging(LoggingConfig(level="WARNING", format="json"), stream=stream)

    log = get_logger("test")
    log.info("hidden")
    log.warning("shown")

    assert [record["event"] for record in _json_lines(stream)] == ["shown"]


def test_stdlib_loggers_are_rendered_too() -> None:
    import logging

    stream = io.StringIO()
    configure_logging(LoggingConfig(format="json"), stream=stream)
    logging.getLogger("thirdparty").warning("from stdlib %s", "logger")

    (record,) = _json_lines(stream)
    assert record["event"] == "from stdlib logger"
    assert record["logger"] == "thirdparty"


def test_console_format_is_human_readable() -> None:
    stream = io.StringIO()
    configure_logging(LoggingConfig(format="console"), stream=stream)
    get_logger("test").info("hello", user="x")

    output = stream.getvalue()
    assert "hello" in output
    assert "user" in output
    assert not output.lstrip().startswith("{")


def test_log_file_receives_json(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "carq.log"
    configure_logging(LoggingConfig(format="console", file=log_file), stream=io.StringIO())
    get_logger("test").info("to_file")

    records = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["event"] == "to_file"


def test_reconfiguring_does_not_duplicate_handlers() -> None:
    stream = io.StringIO()
    configure_logging(LoggingConfig(format="json"), stream=io.StringIO())
    configure_logging(LoggingConfig(format="json"), stream=stream)
    get_logger("test").info("once")

    assert len(_json_lines(stream)) == 1
