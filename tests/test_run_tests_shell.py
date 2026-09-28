"""Exercise the canonical shell runner's interpreter selection and clean env."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest


@pytest.fixture
def runner_repo(tmp_path):
    root = tmp_path / "checkout"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "scripts" / "run_tests.sh",
        scripts / "run_tests.sh",
    )
    # Execute the real shell entrypoint without recursively running pytest.
    (scripts / "run_tests_parallel.py").write_text(
        "import json, os, sys\n"
        "print('RUNNER_PROBE=' + json.dumps(dict(os.environ)))\n"
        "sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)\n",
        encoding="utf-8",
    )
    return root


def _python(root, relative, *, pytest_available=True):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    bash = shutil.which("bash")
    assert bash is not None
    path.write_text(
        f"#!{bash}\n"
        'if [[ "$1" == -c && "$2" == "import pytest" ]]; then\n'
        f"  exit {0 if pytest_available else 1}\n"
        "fi\n"
        f"export RUNNER_SELECTED={shlex.quote(str(path))}\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    (path.parent / "activate").touch()
    return path


def _run(root, explicit=None, *, exit_code=0):
    home = root / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "HERMES_HOME": str(home / ".hermes"),
        "HERMES_PYTHON_SRC_ROOT": str(root),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "TZ": "UTC", "LANG": "C.UTF-8", "PYTHONHASHSEED": "0",
    }
    if explicit is not None:
        env["HERMES_PYTHON"] = str(explicit)
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run(
        [bash, str(root / "scripts/run_tests.sh"), str(exit_code)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    return result


def _selected(result):
    assert result.returncode == 0, result.stdout + result.stderr
    line = next(line for line in result.stdout.splitlines() if line.startswith("RUNNER_PROBE="))
    return json.loads(line.removeprefix("RUNNER_PROBE="))


def test_running_interpreter_imports_checkout():
    import importlib

    root = Path(__file__).resolve().parents[1]
    for name in ("hermes_state", "hermes_constants", "agent.shell_hooks",
                 "hermes_cli._subprocess_compat", "tools.environments.base"):
        module = importlib.import_module(name)
        source = Path(module.__file__).resolve()
        assert source.is_relative_to(root), (name, source)


@pytest.mark.requires_wal
def test_running_interpreter_uses_real_wal(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    try:
        assert db._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        db.create_session(session_id="runner-wal", source="cli")
        assert db.get_session("runner-wal")["id"] == "runner-wal"
    finally:
        db.close()


def test_explicit_dev_python_precedes_stale_local_venv(runner_repo):
    _python(runner_repo, ".venv/bin/python")
    dev = _python(runner_repo, "nix-dev/bin/python3")
    result = _run(runner_repo, dev)
    assert _selected(result)["RUNNER_SELECTED"] == str(dev)
    assert f"interpreter: {dev}" in result.stdout
    assert "Python " in result.stdout
    assert "SQLite " in result.stdout


@pytest.mark.parametrize("explicit_kind", ["absent", "missing", "release-without-pytest"])
def test_local_venv_fallback(runner_repo, explicit_kind):
    local = _python(runner_repo, ".venv/bin/python")
    explicit = None
    if explicit_kind == "missing":
        explicit = runner_repo / "missing-python"
    elif explicit_kind == "release-without-pytest":
        explicit = _python(runner_repo, "release/bin/python3", pytest_available=False)
    assert _selected(_run(runner_repo, explicit))["RUNNER_SELECTED"] == str(local)


@pytest.mark.parametrize("relative", ["venv/bin/python", ".venv/Scripts/python.exe", "home/.hermes/hermes-agent/venv/bin/python"])
def test_other_venv_layouts_and_pytest_probe(runner_repo, relative):
    _python(runner_repo, ".venv/bin/python", pytest_available=False)
    fallback = _python(runner_repo, relative)
    assert _selected(_run(runner_repo))["RUNNER_SELECTED"] == str(fallback)


def test_no_pytest_interpreter_fails(runner_repo):
    release = _python(runner_repo, "release/bin/python3", pytest_available=False)
    result = _run(runner_repo, release)
    assert result.returncode == 1
    assert "RUNNER_PROBE=" not in result.stdout


def test_runner_exit_status_is_not_hidden(runner_repo):
    dev = _python(runner_repo, "nix-dev/bin/python3")
    assert _run(runner_repo, dev, exit_code=7).returncode == 7


def test_source_root_and_disposable_locations_survive_scrub(runner_repo):
    dev = _python(runner_repo, "nix-dev/bin/python3")
    probe = _selected(_run(runner_repo, dev))
    assert probe["HERMES_PYTHON_SRC_ROOT"] == str(runner_repo)
    home = runner_repo / "home"
    assert probe["HERMES_HOME"] == str(home / ".hermes")
    for key, suffix in (("XDG_CONFIG_HOME", ".config"), ("XDG_CACHE_HOME", ".cache"),
                        ("XDG_DATA_HOME", ".local/share"), ("XDG_STATE_HOME", ".local/state")):
        assert probe[key] == str(home / suffix)
