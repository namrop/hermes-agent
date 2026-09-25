# Python test runs and Nix production parity

Use `scripts/run_tests.sh`, not direct pytest. Each file runs in its own
subprocess; `-j 2` bounds concurrent file workers without xdist. The runner
prints the selected interpreter, Python version and SQLite version before
collection. A green run with WAL tests skipped is not equivalent to exercising
production's WAL path.

## Interpreter selection

1. A supplied, executable `HERMES_PYTHON` that can import pytest wins, even
   when the checkout contains `.venv`.
2. Otherwise probe `.venv`, `venv`, then `$HOME/.hermes/hermes-agent/venv`, in
   that order. POSIX `bin/python` and Windows `Scripts/python.exe` layouts
   remain supported. Candidates without pytest are skipped, including an
   inherited release `HERMES_PYTHON`.
3. No candidate means an error, never a zero-test success.

Non-Nix users can continue using their local venv without setting anything.
Do not delete or replace a checkout's `.venv` to select the Nix interpreter.
The runner pins `HERMES_PYTHON_SRC_ROOT` to its own checkout before probing and
again after its environment scrub, so a Nix editable environment imports the
candidate source. Explicit `HERMES_HOME` and XDG location variables also survive
the scrub; credential variables do not. Test conftest still applies its own
per-test disposable-home and live-database guards.

## Python-only Nix environment (no npm setup hook)

If an existing dev interpreter is provided for an audit, reuse it read-only.
Otherwise resolve/build just the **existing dev dependency**, not the full
application and not the default dev shell. From the checkout, in Bash:

```bash
set -euo pipefail
system=$(nix eval --impure --raw --expr builtins.currentSystem)
dev_drv=$(nix eval --raw --no-write-lock-file \
  ".#packages.${system}.default.devDeps" \
  --apply 'deps: (builtins.head (builtins.filter
    (p: (p.name or "") == "hermes-agent-editable-env") deps)).drvPath')
dev_env=$(nix build --no-link --print-out-paths "${dev_drv}^out")
python="$dev_env/bin/python3"
```

This uses the same editable Python dependency as `nix/devShell.nix`, including
`dev` extras. It does not execute the npm shell hook, regenerate package locks,
install JS dependencies, activate a service, or mutate a shared venv. Builds
may need normal Nix cache/network access. For an offline audit with a supplied
store path, do not rebuild merely because current source evaluates to a different
editable-environment derivation.

## Credential-free focused run with real exit status

Choose a disposable scratch directory appropriate to your environment. Run the
following in a subshell (the final `exit` deliberately returns the test status):

```bash
(
  set -u
  root=$(pwd -P)
  scratch=/absolute/path/to/disposable-test-state
  python=/nix/store/EXISTING-hermes-agent-editable-env/bin/python3
  bash_bin=$(command -v bash)
  mkdir -p "$scratch/home/.hermes" "$scratch/home/.config" \
    "$scratch/home/.cache" "$scratch/home/.local/share" \
    "$scratch/home/.local/state" "$scratch/logs"
  if env -i PATH="$PATH" HOME="$scratch/home" \
    HERMES_HOME="$scratch/home/.hermes" \
    XDG_CONFIG_HOME="$scratch/home/.config" \
    XDG_CACHE_HOME="$scratch/home/.cache" \
    XDG_DATA_HOME="$scratch/home/.local/share" \
    XDG_STATE_HOME="$scratch/home/.local/state" \
    HERMES_PYTHON="$python" HERMES_PYTHON_SRC_ROOT="$root" \
    TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 \
    "$bash_bin" scripts/run_tests.sh -j 2 --file-retries 0 \
      tests/test_run_tests_shell.py \
      tests/test_session_db_read_conn_pool.py \
      tests/test_session_db_read_path_split.py \
      tests/gateway/test_matrix_voice.py \
      > "$scratch/logs/focused.log" 2>&1; then
    rc=0
  else
    rc=$?
  fi
  printf '\nRUNNER_EXIT_STATUS=%s\n' "$rc" >> "$scratch/logs/focused.log"
  exit "$rc"
)
```

Do not pipe test commands through `tail`, or end a failed invocation with a
successful `echo`. The runner preserves its child runner's status via `exec`;
its per-file runner reports failing files and returns nonzero. With retries
explicitly disabled here, a first-attempt failure cannot disappear on retry.

If supplying a shared `--basetemp` to put pytest fixtures inside scratch, use
`-j 1`: parallel files must not clear one another's base directory. Standard
library temporary files and conftest's temporary homes are independently managed
by the existing test harness, rather than by `--basetemp`.

Python 3.11 remains supported: run the Matrix and shell fixture cohort with a
pytest-capable 3.11 interpreter as a separate compatibility check. Its SQLite
may intentionally skip `requires_wal` tests. Under the production-matched Nix
interpreter these tests must execute, including the real `SessionDB` WAL-mode
assertion in `test_run_tests_shell.py`. Matrix regressions force MIME lookup to
return `None` or `application/ogg`, and verify that a failed ffmpeg conversion
sends the original MP3 bytes as `audio/mpeg`. No real provider, Matrix server or
browser is required.
