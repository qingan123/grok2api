# Vercel deployment

This fork exposes the main chat models:

- `grok-4.3`: routes to console `grok-4.3` with `reasoning.effort=high`
- `grok-4.3-high`: same as `grok-4.3`
- `grok-4.20-multi-agent-high`: routes to console `grok-4.20-multi-agent-0309` with high effort

These models select accounts from the `basic` pool and do not require super or heavy accounts.

Recommended Vercel environment variables:

- `GROK_APP_API_KEY`: API key for `/v1/*`
- `GROK_APP_APP_KEY`: admin password
- `ACCOUNT_STORAGE`: use `redis`, `mysql`, or `postgresql` for persistent storage
- `ACCOUNT_REDIS_URL`, `ACCOUNT_MYSQL_URL`, or `ACCOUNT_POSTGRESQL_URL`: set the matching DSN
- `LOG_FILE_ENABLED=false`: avoid file logging on serverless runtime
- `GROK_ACCOUNT_REFRESH_ENABLED=false`: use random account selection and avoid background quota probing

Local SQLite storage is not recommended on Vercel because serverless file storage is ephemeral.
