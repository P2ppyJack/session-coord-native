"""The old force-copy path cannot bypass the canonical installer lifecycle."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("legacy_flag", ["--enable", "--force", "--json"])
def test_legacy_copy_flags_refuse_without_touching_the_existing_install(tmp_path, legacy_flag):
    board = os.environ.get("SESSION_COORD_TEST_BOARD_INSTALLER")
    if not board:
        pytest.skip("set canonical board installer path for the entrypoint test")
    root = tmp_path / "home/.hermes"
    managed = root / "scripts/session_coord.py"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"locally modified board\n")
    result = subprocess.run(
        [sys.executable, str(PACKAGE / "install.py"), "--board-installer", board,
         "--hermes-home", str(root), legacy_flag],
        env={"HOME": str(tmp_path / "home"), "PATH": os.environ["PATH"]},
        text=True, capture_output=True, timeout=10, check=False)
    assert result.returncode == 2
    assert "legacy copy installer is retired" in result.stderr
    assert managed.read_bytes() == b"locally modified board\n"
    assert not (root / "plugins").exists()
