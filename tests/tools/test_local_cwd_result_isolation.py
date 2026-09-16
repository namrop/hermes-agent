"""Local cwd normalization must not overwrite command-owned observations."""

import pytest

from tools.environments.base import BaseEnvironment
from tools.environments.local import LocalEnvironment


@pytest.mark.parametrize("marker_state", ["valid", "missing", "stale"])
def test_local_result_keeps_its_own_cwd_after_another_command_finishes(
    tmp_path, monkeypatch, marker_state
):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    env = LocalEnvironment(cwd=str(tmp_path))
    marker = env._cwd_marker
    observed_first = first_dir if marker_state == "valid" else tmp_path / "deleted"
    first = {
        "output": f"first\n{marker}{observed_first}{marker}\n"
        if marker_state != "missing" else "interrupted"
    }
    second = {"output": f"second\n{marker}{second_dir}{marker}\n"}
    original = BaseEnvironment._extract_cwd_from_output

    def interleave_commands(self, result):
        original(self, result)
        # Force the real second command parser into the window after the
        # first observation was captured but before Local normalizes it.
        if result is first:
            original(self, second)

    monkeypatch.setattr(BaseEnvironment, "_extract_cwd_from_output", interleave_commands)
    try:
        env._extract_cwd_from_output(first)
        assert second["cwd"] == str(second_dir)
        if marker_state == "valid":
            assert first["cwd"] == str(first_dir)
            assert first["cwd_observed"] is True
        else:
            assert "cwd" not in first
            assert "cwd_observed" not in first
            assert env.cwd == str(second_dir)
    finally:
        env.cleanup()
