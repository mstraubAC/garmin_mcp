"""Unit tests for the legacy stdio-mode module (_stdio.py)."""
import os

import pytest

from garmin_mcp._stdio import (
    _load_credentials,
    get_mfa,
    is_interactive_terminal,
)


class TestIsInteractiveTerminal:
    def test_tty_returns_true(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
        assert is_interactive_terminal() is True

    def test_non_tty_returns_false(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
        assert is_interactive_terminal() is False


class TestGetMfa:
    def test_raises_in_non_interactive(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        with pytest.raises(RuntimeError, match="MFA required"):
            get_mfa()

    def test_prompts_in_interactive(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "123456")
        result = get_mfa()
        assert result == "123456"


class TestLoadCredentials:
    def test_defaults_when_no_env_vars(self, monkeypatch):
        monkeypatch.delenv("GARMIN_EMAIL", raising=False)
        monkeypatch.delenv("GARMIN_PASSWORD", raising=False)
        monkeypatch.delenv("GARMINTOKENS", raising=False)
        monkeypatch.delenv("GARMINTOKENS_BASE64", raising=False)
        monkeypatch.delenv("GARMIN_IS_CN", raising=False)
        email, password, tokenstore, tokenstore_base64, is_cn = _load_credentials()
        assert email is None
        assert password is None
        assert tokenstore == "~/.garminconnect"
        assert tokenstore_base64 == "~/.garminconnect_base64"
        assert is_cn is False

    def test_loads_email_and_password(self, monkeypatch):
        monkeypatch.setenv("GARMIN_EMAIL", "test@example.com")
        monkeypatch.setenv("GARMIN_PASSWORD", "secret")
        monkeypatch.delenv("GARMINTOKENS", raising=False)
        monkeypatch.delenv("GARMINTOKENS_BASE64", raising=False)
        monkeypatch.delenv("GARMIN_IS_CN", raising=False)
        email, password, tokenstore, tokenstore_base64, is_cn = _load_credentials()
        assert email == "test@example.com"
        assert password == "secret"

    def test_loads_email_from_file(self, monkeypatch, tmp_path):
        email_file = tmp_path / "email.txt"
        email_file.write_text("file@example.com")
        monkeypatch.setenv("GARMIN_EMAIL_FILE", str(email_file))
        monkeypatch.delenv("GARMIN_EMAIL", raising=False)
        monkeypatch.delenv("GARMIN_PASSWORD", raising=False)
        monkeypatch.delenv("GARMINTOKENS", raising=False)
        monkeypatch.delenv("GARMINTOKENS_BASE64", raising=False)
        monkeypatch.delenv("GARMIN_IS_CN", raising=False)
        email, password, tokenstore, tokenstore_base64, is_cn = _load_credentials()
        assert email == "file@example.com"

    def test_both_email_and_file_raises(self, monkeypatch):
        monkeypatch.setenv("GARMIN_EMAIL", "test@example.com")
        monkeypatch.setenv("GARMIN_EMAIL_FILE", "/tmp/nonexistent.txt")
        with pytest.raises(ValueError, match="Must only provide one"):
            _load_credentials()

    def test_both_password_and_file_raises(self, monkeypatch):
        monkeypatch.delenv("GARMIN_EMAIL", raising=False)
        monkeypatch.setenv("GARMIN_PASSWORD", "secret")
        monkeypatch.setenv("GARMIN_PASSWORD_FILE", "/tmp/nonexistent.txt")
        with pytest.raises(ValueError, match="Must only provide one"):
            _load_credentials()

    def test_cn_detection(self, monkeypatch):
        monkeypatch.delenv("GARMIN_EMAIL", raising=False)
        monkeypatch.delenv("GARMIN_PASSWORD", raising=False)
        monkeypatch.delenv("GARMINTOKENS", raising=False)
        monkeypatch.delenv("GARMINTOKENS_BASE64", raising=False)
        monkeypatch.setenv("GARMIN_IS_CN", "true")
        email, password, tokenstore, tokenstore_base64, is_cn = _load_credentials()
        assert is_cn is True


class TestMain:
    def test_main_does_not_crash(self, monkeypatch):
        from unittest.mock import MagicMock, patch
        mock_mcp = MagicMock()
        mock_run = MagicMock()
        mock_mcp.run = mock_run
        with patch("garmin_mcp._stdio.FastMCP", return_value=mock_mcp):
            with patch("garmin_mcp._stdio.init_api", return_value=MagicMock()):
                monkeypatch.setattr("garmin_mcp._stdio._email", "test@x.com")
                monkeypatch.setattr("garmin_mcp._stdio._password", "secret")
                try:
                    from garmin_mcp._stdio import main
                    main()
                except SystemExit:
                    pass
        mock_run.assert_called_once()
