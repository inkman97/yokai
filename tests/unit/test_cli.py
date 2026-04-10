"""Tests for the command-line interface."""

import os
from pathlib import Path

import pytest

from yokai.cli import STARTER_CONFIG, build_parser, cmd_init, main


class TestBuildParser:
    def test_run_subcommand_parses(self):
        parser = build_parser()
        args = parser.parse_args(["run", "--config", "x.yaml"])
        assert args.command == "run"
        assert args.config == "x.yaml"

    def test_status_subcommand_parses(self):
        parser = build_parser()
        args = parser.parse_args(["status", "--config", "x.yaml", "--limit", "5"])
        assert args.command == "status"
        assert args.limit == 5

    def test_init_subcommand_parses(self):
        parser = build_parser()
        args = parser.parse_args(["init", "--output", "out.yaml"])
        assert args.command == "init"
        assert args.output == "out.yaml"

    def test_log_level_default_is_info(self):
        parser = build_parser()
        args = parser.parse_args(["run", "--config", "x.yaml"])
        assert args.log_level == "INFO"

    def test_log_level_can_be_overridden(self):
        parser = build_parser()
        args = parser.parse_args(
            ["--log-level", "DEBUG", "run", "--config", "x.yaml"]
        )
        assert args.log_level == "DEBUG"

    def test_missing_subcommand_errors(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])


class TestCmdInit:
    def test_writes_to_stdout_when_no_output(self, capsys):
        parser = build_parser()
        args = parser.parse_args(["init"])
        rc = cmd_init(args)
        assert rc == 0
        captured = capsys.readouterr()
        assert "issue_tracker:" in captured.out
        assert "repo_hosting:" in captured.out

    def test_writes_to_file_when_output_given(self, tmp_path: Path):
        out = tmp_path / "config.yaml"
        parser = build_parser()
        args = parser.parse_args(["init", "--output", str(out)])
        rc = cmd_init(args)
        assert rc == 0
        assert out.exists()
        content = out.read_text()
        assert "issue_tracker:" in content
        assert "repo_hosting:" in content

    def test_refuses_to_overwrite_without_force(self, tmp_path: Path, capsys):
        out = tmp_path / "config.yaml"
        out.write_text("existing")
        parser = build_parser()
        args = parser.parse_args(["init", "--output", str(out)])
        rc = cmd_init(args)
        assert rc == 1
        assert out.read_text() == "existing"

    def test_force_overwrites_existing(self, tmp_path: Path):
        out = tmp_path / "config.yaml"
        out.write_text("existing")
        parser = build_parser()
        args = parser.parse_args(
            ["init", "--output", str(out), "--force"]
        )
        rc = cmd_init(args)
        assert rc == 0
        assert "existing" not in out.read_text()
        assert "issue_tracker:" in out.read_text()


class TestStarterConfig:
    def test_starter_config_is_valid_yaml(self):
        import yaml
        parsed = yaml.safe_load(STARTER_CONFIG)
        assert "issue_tracker" in parsed
        assert "repo_hosting" in parsed
        assert "agent" in parsed
        assert "routing" in parsed


class TestMainEntryPoint:
    def test_main_init_to_stdout(self, capsys):
        rc = main(["init"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "issue_tracker:" in out

    def test_main_run_with_missing_config_returns_error(self, tmp_path, capsys):
        rc = main(["run", "--config", str(tmp_path / "missing.yaml")])
        assert rc == 2
        err = capsys.readouterr().err
        assert "Configuration error" in err

    def test_main_status_with_missing_config_returns_error(self, tmp_path, capsys):
        rc = main(["status", "--config", str(tmp_path / "missing.yaml")])
        assert rc == 2
