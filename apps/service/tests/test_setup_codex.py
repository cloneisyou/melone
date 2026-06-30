import sys
from pathlib import Path

import pytest
import tomlkit

from melone_service.setup import codex
from melone_service.setup.common import BACKUP_INFIX, ConfigParseError


EXPECTED_COMMAND = str(Path(sys.executable))

# Realistic fixture mixing comments, another [mcp_servers.x] entry, and settings.
EXISTING_CONFIG = """\
# Codex 글로벌 설정
model = "gpt-5"  # 모델은 손대지 말 것

[mcp_servers.docs]
command = "docs-server"
args = ["--stdio"]

# 마지막 주석
"""


@pytest.fixture
def config_path(tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    path = codex_home / "config.toml"
    path.write_text(EXISTING_CONFIG, encoding="utf-8")
    return path


def list_backups(config_path):
    return sorted(config_path.parent.glob(f"{config_path.name}{BACKUP_INFIX}*"))


def test_detect_checks_codex_directory(tmp_path):
    config_path = tmp_path / ".codex" / "config.toml"

    assert codex.detect(config_path) is False

    # The directory alone marks a Codex user, even without config.toml.
    config_path.parent.mkdir()
    assert codex.detect(config_path) is True


def test_default_config_path_points_to_codex_home():
    assert codex.default_config_path() == Path.home() / ".codex" / "config.toml"


def test_enable_adds_table_and_preserves_comments_and_other_servers(config_path):
    result = codex.enable(config_path)

    assert result.changed is True
    assert result.enabled is True
    assert result.backup_path is not None and result.backup_path.is_file()

    content = config_path.read_text(encoding="utf-8")
    assert "# Codex 글로벌 설정" in content
    assert "# 모델은 손대지 말 것" in content
    assert "# 마지막 주석" in content

    document = tomlkit.parse(content)
    assert document["model"] == "gpt-5"
    assert document["mcp_servers"]["docs"].unwrap() == {
        "command": "docs-server",
        "args": ["--stdio"],
    }
    assert document["mcp_servers"]["melone"].unwrap() == {
        "command": EXPECTED_COMMAND,
        "args": ["-m", "melone_service.mcp"],
    }


def test_enable_creates_config_file_when_missing(tmp_path):
    config_path = tmp_path / ".codex" / "config.toml"

    result = codex.enable(config_path)

    assert result.changed is True
    assert result.backup_path is None
    assert codex.is_enabled(config_path) is True

    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    assert document["mcp_servers"]["melone"]["command"] == EXPECTED_COMMAND


def test_enable_includes_melone_home_env_when_given(config_path, tmp_path):
    melone_home = tmp_path / "melone-home"

    codex.enable(config_path, melone_home=melone_home)

    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    assert document["mcp_servers"]["melone"]["env"].unwrap() == {
        "MELONE_HOME": str(melone_home)
    }


def test_enable_is_idempotent(config_path):
    first = codex.enable(config_path)
    content_after_first = config_path.read_text(encoding="utf-8")
    second = codex.enable(config_path)

    assert first.changed is True
    assert second.changed is False
    assert second.enabled is True
    assert second.backup_path is None
    assert config_path.read_text(encoding="utf-8") == content_after_first


def test_enable_updates_entry_when_content_differs(config_path):
    codex.enable(config_path, melone_home="C:/old-home")

    result = codex.enable(config_path)

    assert result.changed is True
    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    assert document["mcp_servers"]["melone"].unwrap() == {
        "command": EXPECTED_COMMAND,
        "args": ["-m", "melone_service.mcp"],
    }


def test_disable_removes_only_melone_table(config_path):
    codex.enable(config_path)

    result = codex.disable(config_path)

    assert result.changed is True
    assert result.enabled is False

    content = config_path.read_text(encoding="utf-8")
    assert "melone" not in content
    assert "# Codex 글로벌 설정" in content
    assert "# 마지막 주석" in content

    document = tomlkit.parse(content)
    assert document["mcp_servers"]["docs"]["command"] == "docs-server"


def test_disable_is_idempotent_without_entry_or_file(tmp_path, config_path):
    missing_file = codex.disable(tmp_path / ".codex" / "missing.toml")
    assert missing_file.changed is False
    assert missing_file.enabled is False

    no_entry = codex.disable(config_path)
    assert no_entry.changed is False

    codex.enable(config_path)
    first = codex.disable(config_path)
    second = codex.disable(config_path)

    assert first.changed is True
    assert second.changed is False
    assert second.backup_path is None


def test_parse_error_never_touches_original_file(config_path):
    broken = "[mcp_servers.docs\ncommand ="
    config_path.write_text(broken, encoding="utf-8")

    with pytest.raises(ConfigParseError):
        codex.enable(config_path)
    with pytest.raises(ConfigParseError):
        codex.disable(config_path)
    with pytest.raises(ConfigParseError):
        codex.is_enabled(config_path)

    assert config_path.read_text(encoding="utf-8") == broken
    assert list_backups(config_path) == []


def test_non_table_mcp_servers_raises_parse_error(config_path):
    config_path.write_text('mcp_servers = "oops"\n', encoding="utf-8")

    with pytest.raises(ConfigParseError):
        codex.enable(config_path)


def test_backups_keep_only_latest_three(config_path):
    # Vary melone_home to force a changed=True write (and backup) every time.
    for index in range(5):
        codex.enable(config_path, melone_home=f"C:/home-{index}")

    backups = list_backups(config_path)
    assert len(backups) == 3
    # The newest backup must hold the state just before the fifth write (home-3).
    latest_backup = tomlkit.parse(backups[-1].read_text(encoding="utf-8"))
    env = latest_backup["mcp_servers"]["melone"]["env"].unwrap()
    assert env == {"MELONE_HOME": "C:/home-3"}


def test_is_enabled_reflects_entry_presence(config_path):
    assert codex.is_enabled(config_path) is False

    codex.enable(config_path)
    assert codex.is_enabled(config_path) is True

    codex.disable(config_path)
    assert codex.is_enabled(config_path) is False
