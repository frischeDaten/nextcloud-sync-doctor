# Contributing

Thanks for looking! Bug reports and pull requests are welcome.

## Fastest way in (no Nextcloud account needed)

```bash
git clone <your-fork>
cd nextcloud-sync-doctor
pip install -e .

python -m ncsd --demo scan          # the CLI against synthetic data
python -m ncsd --demo serve         # the web UI against synthetic data
```

`--demo` replaces the client configuration, the credential store and the network with the
fixtures in `ncsd/demo.py`. It is the intended way to work on the UI or on detection
logic, and it is what the screenshots in the README are made with.

## With a real account

Requirements: Windows with the Nextcloud desktop client configured, Python 3.10+.

```bash
python -m ncsd scan --json          # what does detection see?
python -m ncsd scan --deep          # also crawl the server
python -m ncsd serve                # the UI
```

Please **plan before you apply**: every action in the UI is shown as a plan first. When
testing destructive paths, point the tool at a throwaway account or an ignored (`. _*`)
folder rather than at real data.

## Layout

| File | Responsibility |
|---|---|
| `ncsd/config.py` | read `nextcloud.cfg`, locate journal/logs, data dir |
| `ncsd/creds.py` | read the client's app password from Windows Credential Manager |
| `ncsd/journal.py` | snapshot and query the sync journal (read-only) |
| `ncsd/dav.py` | minimal WebDAV client (PROPFIND, GET, PUT, MOVE, DELETE, versions) |
| `ncsd/names.py` | Windows filename rules — mirrors `csync_exclude.cpp` |
| `ncsd/detect.py` | issue discovery: logs, journal, deep server crawl |
| `ncsd/actions.py` | plans + execution for every resolution action |
| `ncsd/state.py` | dismissals, audit trail, backups |
| `ncsd/webui.py` + `ncsd/static/` | local HTTP server and the browser UI |
| `ncsd/demo.py` | synthetic fixtures for `--demo` |

## Ground rules

* **Standard library only.** A new runtime dependency needs a very good reason; the whole
  point is that this installs and runs anywhere Python does.
* **Never write the sync journal.** Snapshot it, open it read-only, throw the snapshot
  away. The client caches records and validates the schema version.
* **Plan first, then apply.** Every new action needs a `Plan` (summary + steps + whether
  it is destructive) and must be recorded in the audit trail. Destructive local steps back
  the file up first; server deletes go to the trash.
* **Reality over history.** Detection output must be validated against the current state
  of both sides before it is reported as a problem — log lines are history, not facts.
* **Fictional data in fixtures and screenshots.** No real hostnames, user names, folder
  names or file contents. This repository is public.

## Style

* Python: 4 spaces, ~110 columns, type hints where they help, no formatter enforced.
* JS/CSS: vanilla, no build step, no CDN.
* Commit messages: imperative mood, one logical change per commit. Signing off
  (`git commit -s`) is appreciated.

## Reporting a bug

Include the output of `python -m ncsd scan --demo` (to prove it runs at all) plus the
relevant part of `python -m ncsd scan` from your machine — **after removing personal
paths and hostnames**. Never paste your app password or the contents of
`nextcloud.cfg`.
