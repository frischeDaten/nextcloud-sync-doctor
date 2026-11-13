"""Command line entry point: scan, report, serve."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__, config, creds, detect, journal as journal_mod, names, platform
from .dav import Dav
from .model import KINDS
from .state import Store


def build_session(account_index: int = 0, folder_index: int = 0, password: str | None = None,
                  config_path: str | None = None):
    cfg = config.load_config(config_path)
    account = cfg.account(account_index)
    if not account.folders:
        if platform.is_macos() and account.file_provider_domain:
            raise config.fileprovider_folder_missing_error(account)
        raise SystemExit(
            f"The selected account has no sync folders configured.\n"
            f"  Config read: {cfg.path}\n"
            f"  Accounts found: {len(cfg.accounts)}; folder keys: {', '.join(cfg.folder_keys) or 'none'}\n"
            "If that is not the file the client uses, pass --config <nextcloud.cfg>."
        )
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
    journal_error = ""
    if journal_obj is None:
        try:
            journal_obj = journal_mod.load(folder.journal_path, data_dir)
            journal_mod.cleanup_snapshots(data_dir)
        except journal_mod.JournalError as exc:
            journal_obj = None
            journal_error = str(exc)

    options = detect.ScanOptions(include_junk=include_junk, deep=deep, scope=scope)
    issues = detect.collect(account, folder, journal_obj, dav, options, deep_results=deep_results)

    dismissed = store.dismissals()
    now = time.time()
    for issue in issues:
        entry = dismissed.get(issue.key)
        if entry and (not entry.get("until") or entry["until"] > now):
            issue.dismissed = True
    return issues, journal_obj, journal_error


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
        journal_error = ""
        issues = demo.build_issues()
        if not args.junk:
            issues = [i for i in issues if i.kind != "junk"]
    else:
        store = Store()
        cfg, account, folder, dav = build_session(args.account, args.folder, args.password,
                                                  getattr(args, "config", None))
        issues, journal_obj, journal_error = collect_issues(
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
    print(f"Platform  : {platform.platform_name()}")
    print(f"Account   : {account.label}")
    print(f"Server    : {account.server_version or 'unknown'}   client config: {cfg.client_version}")
    print(f"Sync folder: {folder.local_root}  ->  {account.url}{folder.target_path}")
    if folder.note:
        print(f"             ({folder.note})")
    print(f"Journal   : {os.path.basename(folder.journal_path) or '(none)'}"
          + (f"  ({len(journal_obj.rows)} indexed entries)" if journal_obj
             else f"  (unavailable: {journal_error})" if journal_error else "  (unavailable)"))
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
                       open_browser=not args.no_browser, password=args.password, demo=args.demo,
                       config_path=args.config)


# Folders checked only with --all; the default check covers what is likely to be in use.
CHECK_DEFAULT_SKIP = platform.CHECK_DEFAULT_SKIP


def cmd_check(args) -> int:
    """Explain what the doctor can and cannot see, without needing the server."""
    cfg = config.load_config(args.config)
    account = cfg.account(args.account)
    print(f"Nextcloud Sync Doctor {__version__} - check")
    print(f"Platform  : {platform.platform_name()}")
    print(f"Config    : {cfg.path}")
    print(f"Account   : {account.label}   ({len(cfg.accounts)} account(s) in the config)")
    if not account.folders:
        print("Folder    : NOT FOUND")
        if platform.is_macos() and account.file_provider_domain:
            print(f"            {config.fileprovider_folder_missing_error(account)}")
        else:
            print(f"            folder keys in the config: {', '.join(cfg.folder_keys) or 'none'}")
        return 2
    folder = next((f for f in account.folders if f.index == args.folder), account.folders[0])
    print(f"Folder    : {folder.local_root}   (source: {folder.source})")
    if folder.note:
        print(f"            {folder.note}")

    # What the doctor can read on this machine.
    logs = config.recent_logs()
    print(f"Logs      : {len(logs)} file(s)" + (f", newest {_fmt_ts(os.path.getmtime(logs[-1]))}" if logs else ""))
    try:
        j = journal_mod.load(folder.journal_path, os.path.join(Store().data_dir, "snapshots"))
        print(f"Journal   : {len(j.rows)} entries, {len(j.blacklist)} blacklisted")
    except journal_mod.JournalError as exc:
        print(f"Journal   : unavailable - {exc}")
    print("Credentials: " + ("stored client credential found" if not args.password else "given on the command line"))

    # What is on disk right now.
    # On a FileProvider folder every directory costs a provider round-trip (~1 s), so
    # a full walk of a big folder takes hours. Check what is likely to be in use and
    # list the rest, instead of appearing to hang.
    skipped: list[str] = []
    roots: list[str]
    if args.all:
        roots = [folder.local_root]
    else:
        roots = []
        try:
            entries = sorted(os.listdir(folder.local_root))
        except OSError as exc:
            raise SystemExit(f"Cannot list the sync folder: {exc.strerror or exc}") from None
        for name in entries:
            path = os.path.join(folder.local_root, name)
            if not os.path.isdir(path) or name.lower() in CHECK_DEFAULT_SKIP:
                if os.path.isdir(path):
                    skipped.append(name)
                continue
            roots.append(path)

    print()
    print("Checking the local names (no server, no journal needed)...")
    if skipped:
        print(f"  skipping {len(skipped)} large folder(s) by default: {', '.join(skipped)}")
        print("  (use --all to include them; on a cloud folder each one costs a few minutes)")

    reported = [0]

    def progress(state):
        # On a cloud folder a walk can take a minute or more; report so it does not
        # look like a hang.
        if state["files"] - reported[0] >= 2000:
            reported[0] = state["files"]
            print(f"  ... {state['dirs']} folder(s), {state['files']} file(s)", flush=True)

    started = time.time()
    state = detect.local_name_problems(roots, progress=progress, max_seconds=args.timeout,
                                      base=folder.local_root)
    took = time.time() - started
    print(f"  {state['dirs']} folder(s), {state['files']} file(s) in {took:.1f}s"
          + ("" if state["complete"] else
             f" - stopped after the {args.timeout:.0f}s time limit, so this is not the whole "
             "folder; re-run with a larger --timeout to finish"))
    if not state["problems"]:
        print("  no local name problems")
        return 0
    counts: dict[str, int] = {}
    for problem in state["problems"]:
        counts[problem["kind"]] = counts.get(problem["kind"], 0) + 1
    print(f"  {len(state['problems'])} problem(s): " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    for problem in state["problems"][: args.limit]:
        label = KINDS.get(problem["kind"], {}).get("label", problem["kind"])
        print(f"    [{label}] {problem['path']}")
        if problem["reason"]:
            print(f"        {problem['reason']}")
    if any(p["kind"] in ("filename_invalid", "mac_trash") for p in state["problems"]):
        print()
        print("  Fix them in the UI (Rescan -> Check the folder itself, then Rename local / "
              "Delete local / Ignore), or by hand in Finder.")
    if len(state["problems"]) > args.limit:
        print(f"    ... and {len(state['problems']) - args.limit} more (raise --limit)")
    return 0


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
        "--config", metavar="PATH",
        help="read the client configuration from this nextcloud.cfg instead of the "
             "default location (also: NCSD_CONFIG); useful when macOS hides the "
             "client's own copy behind Full Disk Access",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="use synthetic demo data (no client, no server, nothing is changed)",
    )
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", help="list files that fail to sync")
    scan.add_argument("--json", action="store_true", help="machine readable output")
    scan.add_argument("--junk", action="store_true", help="include .DS_Store / ._* noise")
    scan.add_argument("--all", action="store_true", help="include dismissed entries")
    scan.add_argument("--deep", action="store_true",
                      help="also crawl the server for Windows-invalid names, case clashes and long paths")
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

    check = sub.add_parser("check", help="explain what the doctor can see, without the server")
    check.add_argument("--verbose", action="store_true", help="print progress while walking the folder")
    check.add_argument("--all", action="store_true",
                       help="check the whole folder, including large archive trees (slow on a "
                            "FileProvider folder: about a second per folder)")
    check.add_argument("--limit", type=int, default=40, help="how many problems to print (default 40)")
    check.add_argument("--timeout", type=float, default=180.0,
                       help="seconds to spend walking the folder (default 180)")
    check.set_defaults(func=cmd_check)

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
