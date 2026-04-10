"""Unit tests for logging configuration and secret redaction.

We use a direct StreamHandler with StringIO instead of pytest's caplog
because the framework logger has propagate=False to keep its output
isolated from the root logger.
"""

import io
import logging

import pytest

from yokai.core.logging_setup import (
    _RedactionFilter,
    clear_secrets,
    configure_logging,
    get_logger,
    register_secret,
)


@pytest.fixture(autouse=True)
def reset_secrets():
    clear_secrets()
    yield
    clear_secrets()


@pytest.fixture
def captured_stream():
    configure_logging("DEBUG")
    framework_logger = logging.getLogger("yokai")

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(_RedactionFilter())
    framework_logger.addHandler(handler)

    yield stream

    framework_logger.removeHandler(handler)


class TestRedaction:
    def test_registered_secret_is_redacted(self, captured_stream):
        register_secret("super-secret-token-123")
        log = get_logger("test")
        log.info("Calling API with token super-secret-token-123 in URL")
        output = captured_stream.getvalue()
        assert "super-secret-token-123" not in output
        assert "***REDACTED***" in output

    def test_multiple_secrets_all_redacted(self, captured_stream):
        register_secret("token-A")
        register_secret("token-B")
        log = get_logger("test")
        log.info("first token-A then token-B together")
        output = captured_stream.getvalue()
        assert "token-A" not in output
        assert "token-B" not in output
        assert output.count("***REDACTED***") == 2

    def test_empty_secret_is_ignored(self, captured_stream):
        register_secret("")
        log = get_logger("test")
        log.info("normal message with no secrets")
        output = captured_stream.getvalue()
        assert "normal message with no secrets" in output

    def test_unrelated_messages_pass_through(self, captured_stream):
        register_secret("hidden")
        log = get_logger("test")
        log.info("a benign message")
        output = captured_stream.getvalue()
        assert "a benign message" in output

    def test_secret_appearing_multiple_times_all_redacted(self, captured_stream):
        register_secret("xyz")
        log = get_logger("test")
        log.info("xyz xyz and xyz again")
        output = captured_stream.getvalue()
        assert "xyz" not in output
        assert output.count("***REDACTED***") == 3

    def test_logger_namespace(self):
        log = get_logger("subsystem")
        assert log.name == "yokai.subsystem"

    def test_configure_is_idempotent(self):
        configure_logging("INFO")
        framework_logger = logging.getLogger("yokai")
        handler_count = len(framework_logger.handlers)
        configure_logging("DEBUG")
        assert len(framework_logger.handlers) == handler_count
