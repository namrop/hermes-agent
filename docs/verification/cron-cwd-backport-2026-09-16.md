# Cron execution-cwd backport — validation, 2026-09-16

## Scope and deployment boundary

Prepared for `namrop/hermes-agent`, branch `luis/sol-primary`, from base
`246180427ca5a24b966deacd11f99a03dac28ca7`. This record describes source validation,
not production activation. The fleet pin, running gateway, cron definitions,
model pins, schedules and missed-day records were not changed.

Adapted upstream contributions:
- Andrew Bagrin: `b7c59bda54ae6ba6aa020c774a1c0d10a51ae148` — per-execution cwd isolation.
- Yong Chang Yi: `62b0235d4524ddcf36894837045616ecd909a3a5` — agent pre-run script workdir.

See [ADR](../ADR.md#2026-09-16-cron-working-directories-are-per-execution-not-per-process--backport)
for design and fork-specific preservation. The fork adaptation additionally
migrates project-skill roots, subdirectory hints, checkpoint preflight and
parent-to-child workspace hints to scoped cwd, and closes a demonstrated
LocalEnvironment race left in the upstream result-normalization path.

## Evidence

- Initial untouched cron/cwd baseline: **999 passed, 0 failed, 1 skipped**.
- Initial behavioral RED before production edits: **10 failing tests**, including
  concurrent-run exclusion, wrong script cwd and cross-session cwd attribution.
- Legacy-consumer RED: **5 failed, 1 passed**; related GREEN: **64 passed**.
- Local-backend forced interleaving RED: **3 failed**; related GREEN: **31 passed**.
- Final focused matrix: **1,132 passed, 0 failed, 1 skipped**, 90 files.
- Completed broad Linux sweep: **37,068 passed, 29 failed, 247 skipped**,
  3,197 files. **Not a globally green suite.** Every failed test identity was
  independently reproduced on the untouched base; the new-failure set is empty.
- The broad baseline survey was stopped before completion; baseline attribution
  above comes from completed reruns of the exact failing files, not a claim of
  a completed full baseline run.
- The LocalEnvironment regression was added after the broad sweep's discovery;
  it is covered by the separate final focused matrix, not counted in the broad
  sweep total.
- Independent review initially rejected the LocalEnvironment shared-cwd readback.
  After the regression-backed fix, independent re-review passed with no remaining
  security or logic blockers. Seven of eight behavioral source files matched the
  first review byte-for-byte; only the reviewed LocalEnvironment fix changed.
- Ruff on modified behavioral source and new tests: passed. Git whitespace checks:
  passed. The cron tool's workdir help text was also updated to remove the obsolete
  promise of sequential execution.
- Nix `packages.x86_64-linux.default` built successfully. A no-network smoke using
  the built Python **3.12.13** package exercised the real scheduler plus terminal
  and file tools for three overlapping jobs (two explicit workdirs and one
  ambient/default job). Each read its own sentinel, retained correct prompt-context
  semantics, left the ambient environment unchanged, and cleared task cwd records.
  Provider/model/prompt setup was stubbed; no live inference or delivery occurred.
- The built package also passed all three deterministic LocalEnvironment
  interleaving cases (valid, missing and stale markers). Reviewed behavioral-module
  hashes matched the built package and source.

## Reproduction

Tests used the repository's `scripts/run_tests.sh` in disposable HOME directories;
its credential-stripped per-file subprocess runner and conftest sandbox isolate
Hermes state. Python test dependencies came from the existing dev venv, unmodified.
Use the local development interpreter through `HERMES_PYTHON` if the worktree has
no `.venv`. The focused command is:

```bash
scripts/run_tests.sh tests/cron \
  tests/tools/test_terminal_task_cwd.py tests/tools/test_session_cwd_store.py \
  tests/tools/test_file_tools_cwd_resolution.py tests/tools/test_file_ops_cwd_tracking.py \
  tests/tools/test_gateway_cwd_contract.py tests/tools/test_interrupted_command_cwd.py \
  tests/tools/test_terminal_cwd_echo.py tests/tools/test_local_env_cwd_recovery.py \
  tests/tools/test_local_env_relative_cwd.py tests/tools/test_container_cwd_sanitize.py \
  tests/tools/test_code_execution_modes.py tests/agent/test_runtime_cwd.py \
  tests/agent/test_scoped_cwd_consumers.py tests/agent/test_project_skills.py \
  tests/agent/test_tool_executor_checkpoint_paths.py tests/agent/test_subdirectory_hints.py \
  tests/agent/test_meta_agent_init.py tests/tools/test_local_cwd_result_isolation.py \
  -j 4 --file-retries 0 --tb=short
```

Broad command: `scripts/run_tests.sh -j 6 --file-timeout 300`.
Build command: `nix build .#packages.x86_64-linux.default --no-link --print-out-paths --no-write-lock-file`.
External-service, Docker and E2E suites remain excluded by the canonical runner;
Windows/macOS-only behavior is not represented as exercised on Linux.

## Pre-existing failed-test accounting

These are observed failures of the unchanged fork/test environment, not all
"environmental" defects. They include hardcoded FHS paths, snapshot expectations
that lag fork additions, existing quota-bench/config issues, and a scratch-HOME
assertion. No unrelated production fix was bundled into this backport.

| Test file | Failed identities reproduced on base |
|---|---:|
| `tests/agent/test_shell_hooks_tree_kill.py` | 4 |
| `tests/gateway/test_matrix_voice.py` | 1 |
| `tests/gateway/test_send_image_file.py` | 2 |
| `tests/hermes_cli/test_config_read_guard.py` | 1 |
| `tests/hermes_cli/test_doctor.py` | 1 |
| `tests/hermes_cli/test_git_probe_tree_kill.py` | 2 |
| `tests/plugins/memory/test_holographic_retrieval.py` | 1 |
| `tests/plugins/web/test_web_search_provider_plugins.py` | 1 |
| `tests/scripts/test_windows_footguns_full_repo_scan.py` | 1 |
| `tests/test_install_macos_launcher.py` | 1 |
| `tests/test_log_isolation.py` | 1 |
| `tests/test_toolsets.py` | 3 |
| `tests/tools/test_base_environment.py` | 3 |
| `tests/tools/test_browser_use_cli.py` | 1 |
| `tests/tools/test_checkpoint_manager.py` | 3 |
| `tests/tools/test_send_message_plugin_extensibility.py` | 2 |
| `tests/tools/test_termux_api_detection.py` | 1 |

Raw logs, exact failure-identity comparison, RED/GREEN receipts, source-hash
reviews and packaged smoke script/output are retained in the local working
packet `/tmp/hermes-cron-cwd-port-20260916/`. That scratch path is not required by
runtime or tests and is not a durable substitute for the summary recorded here.

## Next operation

After an explicit deployment decision: advance the fleet's `hermes-fork` pin to
the pushed source, build/activate through the governed fleet workflow, verify the
running package, then recover the missing dated Lantern output and downstream
inputs with duplicate-effect checks. Preparing this patch did none of those
production actions.
