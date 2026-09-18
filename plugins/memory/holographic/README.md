# Holographic Memory Provider

Local SQLite fact store with FTS5 search, trust scoring, entity resolution, and HRR-based compositional retrieval.

## Requirements

None — uses SQLite (always available). NumPy optional for HRR algebra.

## Setup

```bash
hermes memory setup    # select "holographic"
```

Or manually:
```bash
hermes config set memory.provider holographic
```

## Config

Config in `config.yaml` under `plugins.hermes-memory-store`:

| Key | Default | Description |
|-----|---------|-------------|
| `db_path` | `$HERMES_HOME/memory_store.db` | SQLite database path |
| `auto_extract` | `false` | Auto-extract facts at session end |
| `default_trust` | `0.5` | Default trust score for new facts |
| `hrr_dim` | `1024` | HRR vector dimensions |
| `retrieval_timeout_seconds` | `6` | SQL deadline per automatic recall (cancellable) |
| `tool_timeout_seconds` | `20` | Deadline per explicit `fact_store` read action |
| `max_query_chars` | `4000` | Query prefix used for retrieval (bounded before tokenising) |
| `max_query_terms` | `64` | Distinct FTS `OR` terms per query |
| `lock_wait_seconds` | `2` | Reader busy-wait on the database file lock |

Reads run on per-request reader connections with progress-handler
cancellation; writes stay on the shared serialized writer. See
`docs/memory-recall-isolation.md` for the runtime `memory_query` API,
diagnostics and the incident this guards against.

## Tools

| Tool | Description |
|------|-------------|
| `fact_store` | 9 actions: add, search, probe, related, reason, contradict, update, remove, list |
| `fact_feedback` | Rate facts as helpful/unhelpful (trains trust scores) |
