import json
import sys
from pathlib import Path

import pytest

from melone_service.setup import claude_code
from melone_service.setup.common import (
    BACKUP_INFIX,
    ConfigParseError,
    build_server_entry,
)


EXPECTED_COMMAND = str(Path(sys.executable))


def read_config(config_path):
    return json.loads(config_path.read_text(encoding="utf-8"))


def write_config(config_path, config):
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")


def list_backups(config_path):
    return sorted(config_path.parent.glob(f"{config_path.name}{BACKUP_INFIX}*"))


def test_build_server_entry_uses_interpreter_and_optional_env(tmp_path):
    entry = build_server_entry()

    assert entry == {
        "command": EXPECTED_COMMAND,
        "args": ["-m", "melone_service.mcp"],
    }

    entry_with_home = build_server_entry(melone_home=tmp_path / "home")
    assert entry_with_home["env"] == {"MELONE_HOME": str(tmp_path / "home")}


def test_build_server_entry_uses_mcp_subcommand_when_frozen(monkeypatch):
    # The packaged melone-daemon binary cannot honor `-m module`, so it must be
    # registered with the `mcp` argv subcommand it dispatches on instead. The
    # frozen binary is a real file, so the command is resolved.
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    entry = build_server_entry()

    assert entry == {
        "command": str(Path(sys.executable).resolve()),
        "args": ["mcp"],
    }


def test_build_server_entry_does_not_resolve_venv_symlink(monkeypatch):
    # A venv python is a symlink to the base interpreter; resolving it escapes
    # the venv and drops melone_service, so the dev entry keeps the venv path.
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    entry = build_server_entry()

    assert entry["command"] == str(Path(sys.executable))


def test_detect_checks_config_file_existence(tmp_path):
    config_path = tmp_path / ".claude.json"

    assert claude_code.detect(config_path) is False

    config_path.write_text("{}", encoding="utf-8")
    assert claude_code.detect(config_path) is True


def test_default_config_path_points_to_home():
    assert claude_code.default_config_path() == Path.home() / ".claude.json"


def test_enable_adds_melone_entry_and_preserves_other_keys(tmp_path):
    config_path = tmp_path / ".claude.json"
    write_config(
        config_path,
        {
            "numStartups": 5,
            "mcpServers": {
                "docs": {"type": "stdio", "command": "docs-server", "args": []}
            },
            "projects": {"c:/work": {"allowedTools": ["Read"]}},
        },
    )

    result = claude_code.enable(config_path)

    assert result.changed is True
    assert result.enabled is True
    assert result.config_path == config_path
    assert result.backup_path is not None and result.backup_path.is_file()

    config = read_config(config_path)
    assert config["numStartups"] == 5
    assert config["projects"] == {"c:/work": {"allowedTools": ["Read"]}}
    assert config["mcpServers"]["docs"] == {
        "type": "stdio",
        "command": "docs-server",
        "args": [],
    }
    assert config["mcpServers"]["melone"] == {
        "type": "stdio",
        "command": EXPECTED_COMMAND,
        "args": ["-m", "melone_service.mcp"],
    }


def test_enable_creates_config_file_when_missing(tmp_path):
    config_path = tmp_path / ".claude.json"

    result = claude_code.enable(config_path)

    assert result.changed is True
    assert result.backup_path is None
    assert claude_code.is_enabled(config_path) is True


def test_enable_includes_melone_home_env_when_given(tmp_path):
    config_path = tmp_path / ".claude.json"
    melone_home = tmp_path / "melone-home"

    claude_code.enable(config_path, melone_home=melone_home)

    entry = read_config(config_path)["mcpServers"]["melone"]
    assert entry["env"] == {"MELONE_HOME": str(melone_home)}


def test_enable_is_idempotent(tmp_path):
    config_path = tmp_path / ".claude.json"

    first = claude_code.enable(config_path)
    content_after_first = config_path.read_text(encoding="utf-8")
    second = claude_code.enable(config_path)

    assert first.changed is True
    assert second.changed is False
    assert second.enabled is True
    assert second.backup_path is None
    assert config_path.read_text(encoding="utf-8") == content_after_first
    assert list(read_config(config_path)["mcpServers"]) == ["melone"]


def test_enable_updates_entry_when_content_differs(tmp_path):
    config_path = tmp_path / ".claude.json"
    write_config(
        config_path,
        {"mcpServers": {"melone": {"type": "stdio", "command": "old", "args": []}}},
    )

    result = claude_code.enable(config_path)

    assert result.changed is True
    assert read_config(config_path)["mcpServers"]["melone"]["command"] == (
        EXPECTED_COMMAND
    )


def test_disable_removes_only_melone_entry(tmp_path):
    config_path = tmp_path / ".claude.json"
    write_config(
        config_path,
        {
            "mcpServers": {
                "docs": {"type": "stdio", "command": "docs-server", "args": []},
                "melone": {"type": "stdio", "command": "python", "args": []},
            },
            "numStartups": 1,
        },
    )

    result = claude_code.disable(config_path)

    assert result.changed is True
    assert result.enabled is False
    config = read_config(config_path)
    assert "melone" not in config["mcpServers"]
    assert config["mcpServers"]["docs"]["command"] == "docs-server"
    assert config["numStartups"] == 1


def test_disable_is_idempotent_without_entry_or_file(tmp_path):
    config_path = tmp_path / ".claude.json"

    missing_file = claude_code.disable(config_path)
    assert missing_file.changed is False
    assert missing_file.enabled is False

    claude_code.enable(config_path)
    first = claude_code.disable(config_path)
    second = claude_code.disable(config_path)

    assert first.changed is True
    assert second.changed is False
    assert second.backup_path is None


def test_parse_error_never_touches_original_file(tmp_path):
    config_path = tmp_path / ".claude.json"
    broken = "{not valid json"
    config_path.write_text(broken, encoding="utf-8")

    with pytest.raises(ConfigParseError):
        claude_code.enable(config_path)
    with pytest.raises(ConfigParseError):
        claude_code.disable(config_path)
    with pytest.raises(ConfigParseError):
        claude_code.is_enabled(config_path)

    assert config_path.read_text(encoding="utf-8") == broken
    assert list_backups(config_path) == []


def test_non_object_top_level_raises_parse_error(tmp_path):
    config_path = tmp_path / ".claude.json"
    config_path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(ConfigParseError):
        claude_code.enable(config_path)


def test_backups_keep_only_latest_three(tmp_path):
    config_path = tmp_path / ".claude.json"
    config_path.write_text("{}", encoding="utf-8")

    # Vary melone_home to force a changed=True write (and backup) every time.
    for index in range(5):
        claude_code.enable(config_path, melone_home=tmp_path / f"home-{index}")

    backups = list_backups(config_path)
    assert len(backups) == 3
    # The newest backup must hold the state just before the fifth write (home-3).
    latest_backup = json.loads(backups[-1].read_text(encoding="utf-8"))
    melone_home = latest_backup["mcpServers"]["melone"]["env"]["MELONE_HOME"]
    assert melone_home == str(tmp_path / "home-3")


def test_is_enabled_reflects_entry_presence(tmp_path):
    config_path = tmp_path / ".claude.json"

    assert claude_code.is_enabled(config_path) is False

    claude_code.enable(config_path)
    assert claude_code.is_enabled(config_path) is True

    claude_code.disable(config_path)
    assert claude_code.is_enabled(config_path) is False
