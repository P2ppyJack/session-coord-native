"""The plugin checkout delegates to the one optional-component installer."""
import os
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]


def test_plugin_entrypoint_uses_standalone_default(tmp_path):
    board = os.environ.get("SESSION_COORD_TEST_BOARD_INSTALLER")
    if not board:
        import pytest
        pytest.skip("set canonical board installer path for the wrapper test")
    home = tmp_path / "home"
    root = home / ".hermes"
    result = subprocess.run(
        [sys.executable, str(PACKAGE / "install.py"), "--board-installer", board,
         "--hermes-home", str(root), "--plugin", "keep", "--no-verify"],
        env={"HOME": str(home), "PATH": os.environ["PATH"], "PYTHONUTF8": "1"},
        text=True, capture_output=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "works without the plugin" in result.stdout
    assert (root / "scripts/session_coord.py").is_file()
    assert not (root / "plugins/session-coord-native").exists()


def test_missing_canonical_installer_is_actionable_and_nonmutating(tmp_path):
    root = tmp_path / "home/.hermes"
    result = subprocess.run(
        [sys.executable, str(PACKAGE / "install.py"), "--board-installer", str(tmp_path / "absent.py"),
         "--hermes-home", str(root)],
        env={"HOME": str(tmp_path / "home"), "PATH": os.environ["PATH"]},
        text=True, capture_output=True, timeout=10, check=False)
    assert result.returncode != 0
    assert "canonical" in result.stderr.lower()
    assert not root.exists()
