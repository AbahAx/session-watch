# Security

Session Watch is a self-hosted administrator tool. It does not modify 3x-ui
configuration or source logs. The 3x-ui adapter permits only login, optional
CSRF-token retrieval, and the fixed online-list query routes.

- There is no demo mode or fabricated data. Monitoring and Telegram delivery
  require configuration and activation; local inventory continues to sync while paused.
- The optional privileged local collector reads the panel database using SQLite
  mode=ro and query_only, and queries the existing loopback Xray online-statistics API.
  Run it separately with the supplied systemd sandbox. Its small sanitized snapshot
  excludes panel credentials, proxy UUIDs/passwords and subscription IDs. Never give
  the web app the original database or Xray control endpoint. Stale/failed snapshots
  are unknown, never an empty online list. Source files are never changed.
- Bind to loopback by default. For remote access use an SSH tunnel, or an HTTPS
  reverse proxy with an appropriate access policy. Set
  `SESSION_WATCH_SECURE_COOKIE=1` when accessed over HTTPS.
- First-run browser setup is permitted only from loopback. In Docker, initialize
  with `docker compose exec session-watch python -m app.cli init-admin`.
- Panel credentials can confer administrator access even though this application
  only makes queries. A query-only adapter is not a read-only role enforced by
  3x-ui. Restrict filesystem, host and network access appropriately.
- Passwords are scrypt-hashed. Panel passwords and Telegram tokens are encrypted
  with a locally generated key. Protect the entire data volume and its backups;
  access to both the key and database permits decryption. Windows ACLs and Linux
  service/group permissions remain the administrator's responsibility.
- Same-origin requests, HttpOnly cookies and per-IP login throttling are enforced.
  Cookies expire after 12 hours. TLS validation is enabled for external requests.
- HTTP Host validation defaults to loopback names. For a reverse proxy add its
  hostname to `SESSION_WATCH_ALLOWED_HOSTS`; do not use a wildcard. Set
  `SESSION_WATCH_PUBLIC_ORIGIN=https://your-host` when terminating HTTPS at a
  reverse proxy, preserve Host, and strip untrusted forwarded headers.
- The log reader opens files in `rb`. It never creates, clears, rotates or changes
  permissions on source logs. Do not mount panel databases or the Docker socket.
- For root-only logs, the optional privileged bridge in `deploy/log_bridge.py`
  must run in a separate filesystem/network sandbox. It writes only its own spool,
  checkpoint and heartbeat. The web application should stay unprivileged and have
  read-only access to the spool, not the panel directory. The source path must be
  outside the spool. Configure the heartbeat to detect a stopped bridge.
- Session history contains IPs, email identifiers and targets. Raw recent log
  events are kept temporarily for assigning them to a session. Historical session
  aggregates expire according to retention settings; administrators control source
  log retention separately.
- Telegram messages leave the server and go to the configured chat. Protect its
  membership. Preview messages are never automatically promoted to live sends.
- Optional IP geolocation (off by default) sends only public source IP addresses
  to IP9 over certificate-validated HTTPS. No client names, targets, logs or tokens
  are submitted. Private/reserved addresses are never queried. Cached city results
  remain in the application database (seven-day success, ten-minute error TTL,
  capped at 5000 entries). Treat the data as a provider estimate, not physical location.
- No analytics, external fonts, CDN scripts or automatic update calls are included.

When reporting a security issue, do not attach real tokens, credentials, panel
URLs, access logs or database files to a public issue. Use a private GitHub security
advisory once the repository owner enables private vulnerability reporting.
