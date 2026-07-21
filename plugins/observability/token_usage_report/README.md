# Token Usage Report

Local observability plugin for Hermes token usage. When enabled, it listens to the per-request `post_api_request` hook and writes:

- `~/.hermes/reports/token_usage/events.jsonl` — one row per model request
- `~/.hermes/reports/token_usage/latest.md` — rolling Markdown summary

The report includes per-model reasoning-token statistics and exact-boundary counts for `516`, `1034`, and `1552` by default, useful for investigating Codex reasoning-token clustering.

Enable:

```bash
hermes plugins enable observability/token_usage_report
# restart the gateway or start a new CLI session
```

Environment knobs:

- `HERMES_TOKEN_USAGE_REPORT_DIR` — output directory override
- `HERMES_TOKEN_USAGE_REPORT_MAX_EVENTS` — number of recent JSONL rows scanned for `latest.md` (default `20000`)
- `HERMES_TOKEN_USAGE_REPORT_RECENT_ROWS` — recent-events rows in the report (default `25`)
- `HERMES_TOKEN_USAGE_REPORT_TARGETS` — comma-separated exact reasoning-token targets (default `516,1034,1552`)
