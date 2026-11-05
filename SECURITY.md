# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private
vulnerability reporting: *Security* tab → *Report a vulnerability*. You can expect an
initial answer within a few days.

## What this tool does with your credentials

The doctor needs to talk to your Nextcloud server to show you the server side of a sync
problem. It obtains a token in one of two ways:

1. **Reuse (default).** It reads the app password that the Nextcloud desktop client
   already stored in the Windows Credential Manager
   (`Nextcloud_<user>_app-password:<url>:<id>`). The entry is opened read-only; it is never
   modified, rotated or deleted. Only the same Windows user can read it.
2. **Explicit.** `--password <app-password>` (create one in Nextcloud → *Settings* →
   *Security* → *App passwords*). Nothing is read from the credential store.

Consequences worth knowing:

* The token is used for HTTP Basic authentication against the server configured in
  `%APPDATA%\Nextcloud\nextcloud.cfg`. It is sent nowhere else.
* The token is held in memory for the lifetime of the process. It is **not** written to
  disk, to logs, or to the audit trail.
* If you do not want a tool to read the client's stored credential, use `--password`, or
  set `NCSD_ALLOW_CREDENTIAL_REUSE=0` to disable the reuse path entirely.

## Local attack surface

* The UI is an HTTP server bound to `127.0.0.1` only, on a random free port.
* Every API call requires a random per-run token that is printed in the console and put
  in the browser URL. Static files are served without it; they contain no data.
* There is no telemetry, no analytics and no outbound connection other than to your
  Nextcloud server.

## What the tool can change

All changes to your files or server are user-initiated from the UI or CLI. To make them
recoverable:

* the sync journal is copied and opened **read-only**; it is never written,
* local files are copied to `%LOCALAPPDATA%\Nextcloud\sync-doctor\backups\` before being
  overwritten or removed,
* the previous server version is downloaded to that backup folder before a server
  overwrite,
* server deletions go through Nextcloud, i.e. into the server's trash bin,
* every applied action is appended to `audit.jsonl`.

## Dependencies

The package has **no third-party runtime dependencies** — Python standard library only.
That is deliberate: there is no dependency supply chain to compromise, and nothing to keep
patched besides Python itself.
