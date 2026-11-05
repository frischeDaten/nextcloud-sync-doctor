"""Command line entry point: scan, report, serve."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__, config, creds, detect, journal as journal_mod, names
from .dav import Dav
from .model import KINDS
from .state import Store


def build_session(account_index: int = 0, folder_index: int = 0, password: str | None = None):
    cfg = config.load_config()
    account = cfg.account(account_index)
    if not account.folders:
        raise SystemExit("The selected account has no sync folders configured.")
    folder = next((f for f in account.folders if f.index == folder_index), account.folders[0])
    credential = creds.find_account_credential(account, explicit_password=password)
    dav = Dav(account.base_url, account.user, credential.token)
    return cfg, account, folder, dav


def collect_issues(
    account,
    folder,
    dav,
    store: Store,
    include_junk: bool = False,
    deep: bool = False,
    scope: str = "",
    journal_obj=None,
    deep_results=None,
):
    data_dir = os.path.join(store.data_dir, "snapshots")
    if journal_obj is None:
        try:
            journal_obj = journal_mod.load(folder.journal_path, data_dir)
            journal_mod.cleanup_snapshots(data_dir)
        except journal_mod.JournalError:
            journal_obj = None

    options = detect.ScanOptions(include_junk=include_junk, deep=deep, scope=scope)
    issues = detect.collect(account, folder, journal_obj, dav, options, deep_results=deep_results)

    dismissed = store.dismissals()
    now = time.time()
    for issue in issues:
        entry = dismissed.get(issue.key)
        if entry and (not entry.get("until") or entry["until"] > now):
            issue.dismissed = True
    return issues, journal_obj


def _fmt_ts(ts: float) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _fmt_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"


def cmd_scan(args) -> int:
    if args.demo:
        from . import demo

        cfg, account, folder = demo.build_context()
        journal_obj = demo.DemoJournal()
        issues = demo.build_issues()
        if not args.junk:
            issues = [i for i in issues if i.kind != "junk"]
    else:
        store = Store()
        cfg, account, folder, dav = build_session(args.account, args.folder, args.password)
        issues, journal_obj = collect_issues(
            account, folder, dav, store,
            include_junk=args.junk, deep=args.deep, scope=args.scope,
        )

    if args.json:
        payload = {
            "account": {"url": account.url, "user": account.user, "server": account.server_version},
            "folder": {"local": folder.local_root, "remote": folder.target_path},
            "issues": [i.to_dict() for i in issues if args.all or not i.dismissed],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    real = [i for i in issues if i.severity != "noise" and not i.resolved]
    noise = [i for i in issues if i.severity == "noise" and not i.resolved]
    resolved = [i for i in issues if i.resolved]

    print(f"Nextcloud Sync Doctor {__version__}")
    if args.demo:
        print("Mode      : DEMO - synthetic data, nothing is read from your machine or the network")
    print(f"Account   : {account.label}")
    print(f"Server    : {account.server_version or 'unknown'}   client config: {cfg.client_version}")
    print(f"Sync folder: {folder.local_root}  ->  {account.url}{folder.target_path}")
    print(f"Journal   : {os.path.basename(folder.journal_path)}"
          + (f"  ({len(journal_obj.rows)} indexed entries)" if journal_obj else "  (unavailable)"))
    print()

    if not real and not noise and not resolved:
        print("No sync problems found.")
        return 0

    print(f"{len(real)} real problem(s), {len(noise)} noise item(s) hidden by default, "
          f"{len(resolved)} already resolved")
    if resolved:
        for issue in resolved:
            print(f"  [resolved] {issue.path}")
            print(f"      {issue.resolved_note}")
        print()
    for issue in real:
        meta = KINDS.get(issue.kind, {})
        print(f"  [{meta.get('label', issue.kind)}] {issue.path}")
        if issue.reason:
            print(f"      reason   : {issue.reason}")
        print(f"      status   : {issue.status or '-'}   sources: {', '.join(issue.sources)}"
              f"   seen: {_fmt_ts(issue.last_seen)}" + (f" x{issue.occurrences}" if issue.occurrences else ""))
        local = "missing" if not issue.local.exists else f"{_fmt_size(issue.local.size)}  {_fmt_ts(issue.local.mtime)}"
        print(f"      local    : {local}")
        if issue.remote is not None:
            remote = "missing" if not issue.remote.exists else (
                f"{_fmt_size(issue.remote.size)}  {_fmt_ts(issue.remote.mtime)}  id={issue.remote.fileid}")
        else:
            remote = "not checked"
        print(f"      server   : {remote}")
        if issue.suggested_name and issue.suggested_name != names.basename(issue.path):
            print(f"      suggested: {issue.suggested_name}")
        print(f"      actions  : {', '.join(issue.actions)}")
        print()

    if noise and not args.junk:
        print(f"({len(noise)} ignored/junk items such as ._* and .DS_Store are counted as noise."
              " Re-run with --junk to see them.)")
    return 0


def cmd_serve(args) -> int:
    from . import webui

    return webui.serve(account_index=args.account, folder_index=args.folder, port=args.port,
                       open_browser=not args.no_browser, password=args.password, demo=args.demo)


def cmd_report(args) -> int:
    store = Store()
    entries = store.recent_audit(args.limit)
    if not entries:
        print("No actions recorded yet.")
        return 0
    print(f"{'when':17} {'action':14} {'ok':3} path")
    for entry in entries:
        print(f"{_fmt_ts(entry.get('at', 0)):17} {entry.get('action', '?'):14} "
              f"{'yes' if entry.get('ok') else 'no ':3} {entry.get('path', '')}")
        if entry.get("message"):
            print(f"{'':17} {'':14}     {entry['message']}")
    count, total = store.backups_size()
    print(f"\nBackups: {count} file(s), {_fmt_size(total)} in {os.path.join(store.data_dir, 'backups')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ncsd",
        description="Find and fix Nextcloud Desktop sync problems file by file.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--account", type=int, default=0, help="account index (default 0)")
    parser.add_argument("--folder", type=int, default=0, help="folder index within the account")
    parser.add_argument("--password", help="app password instead of the stored client token")
    parser.add_argument(
        "--demo", action="store_true",
        help="use synthetic demo data (no client, no server, nothing is changed)",
    )
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", help="list files that fail to sync")
    scan.add_argument("--json", action="store_true", help="machine readable output")
    scan.add_argument("--junk", action="store_true", help="include .DS_Store / ._* noise")
    scan.add_argument("--all", action="store_true", help="include dismissed entries")
    scan.add_argument("--deep", action="store_true", help="also crawl the server for Windows-invalid names")
    scan.add_argument("--scope", default="", help="limit a deep scan to this sub-folder")
    # default=SUPPRESS keeps the top-level --demo value when the flag is not repeated
    scan.add_argument("--demo", action="store_true", default=argparse.SUPPRESS,
                      help="use synthetic demo data")
    scan.set_defaults(func=cmd_scan)

    serve = sub.add_parser("serve", help="start the local web UI")
    serve.add_argument("--port", type=int, default=0, help="port (0 = pick a free one)")
    serve.add_argument("--no-browser", action="store_true")
    serve.add_argument("--demo", action="store_true", default=argparse.SUPPRESS,
                       help="use synthetic demo data")
    serve.set_defaults(func=cmd_serve)

    report = sub.add_parser("report", help="show the action audit trail")
    report.add_argument("--limit", type=int, default=50)
    report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except (config.ConfigError, creds.AuthError, journal_mod.JournalError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
