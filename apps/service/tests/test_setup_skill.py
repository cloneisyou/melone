from pathlib import Path

import pytest

from melone_service.setup import skill
from melone_service.setup.common import BACKUP_INFIX


def list_backups(path):
    return sorted(path.parent.glob(f"{path.name}{BACKUP_INFIX}*"))


def test_default_skill_path_maps_targets():
    assert (
        skill.default_skill_path("claude-code")
        == Path.home() / ".claude" / "skills" / "melone" / "SKILL.md"
    )
    assert (
        skill.default_skill_path("codex")
        == Path.home() / ".codex" / "skills" / "melone" / "SKILL.md"
    )


def test_default_skill_path_rejects_unknown_target():
    with pytest.raises(KeyError):
        skill.default_skill_path("vscode")


def test_install_creates_skill_file(tmp_path):
    path = tmp_path / ".claude" / "skills" / "melone" / "SKILL.md"

    result = skill.install_skill(path)

    assert result.changed is True
    assert result.enabled is True
    assert result.backup_path is None  # new file, nothing to back up
    content = path.read_text(encoding="utf-8")
    assert content == skill.SKILL_CONTENT
    assert "name: melone" in content
    assert skill.is_skill_installed(path) is True


def test_install_is_idempotent(tmp_path):
    path = tmp_path / "SKILL.md"
    skill.install_skill(path)

    second = skill.install_skill(path)

    assert second.changed is False
    assert list_backups(path) == []


def test_install_refreshes_a_stale_skill_with_backup(tmp_path):
    path = tmp_path / "melone" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("old skill body\n", encoding="utf-8")

    result = skill.install_skill(path)

    assert result.changed is True
    assert result.backup_path is not None and result.backup_path.is_file()
    assert path.read_text(encoding="utf-8") == skill.SKILL_CONTENT


def test_uninstall_removes_file_and_empty_dir(tmp_path):
    path = tmp_path / "skills" / "melone" / "SKILL.md"
    skill.install_skill(path)

    result = skill.uninstall_skill(path)

    assert result.changed is True
    assert result.enabled is False
    assert path.is_file() is False
    # The melone/ dir we created is cleaned up; the shared skills/ parent remains.
    assert path.parent.exists() is False
    assert path.parent.parent.exists() is True


def test_uninstall_is_idempotent_on_missing_file(tmp_path):
    path = tmp_path / "melone" / "SKILL.md"
    result = skill.uninstall_skill(path)
    assert result.changed is False
    assert result.enabled is False
