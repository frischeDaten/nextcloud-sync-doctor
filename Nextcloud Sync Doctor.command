#!/bin/bash
# Nextcloud Sync Doctor - macOS launcher.
#
# Double-click this file in Finder (or run it from Terminal) to start the doctor.
# The first time macOS asks whether it may run a downloaded script, choose "Open".
#
# Options: any option the doctor understands, e.g.  ./"Nextcloud Sync Doctor.command" --demo
set -u
cd "$(dirname "$0")" || exit 1

# --- locate a Python 3.10+ interpreter --------------------------------------
PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      PY="$candidate"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  # The system Python (/usr/bin/python3) only appears once the Command Line Tools
  # are installed, which is exactly the version this check wants.
  cat <<'EOF'

  Python 3.10 or newer was not found.
  Install it from https://www.python.org/downloads/ (or "xcode-select --install"
  for the Apple build), then run this file again.

EOF
  [ -t 0 ] && read -r -p "Press Return to close this window. " _
  exit 1
fi

# --- the client's own data is protected on macOS ----------------------------
# The Nextcloud client is a sandboxed app, so its nextcloud.cfg and sync journal
# live in directories that only a process with Full Disk Access may read. Warn
# up front instead of failing later with a confusing permission error.
CFG="$HOME/Library/Containers/com.nextcloud.desktopclient/Data/Library/Preferences/Nextcloud/nextcloud.cfg"
if [ -e "$CFG" ] && ! head -c 1 "$CFG" >/dev/null 2>&1; then
  cat <<'EOF'
  macOS does not let this Terminal read the Nextcloud client's configuration yet.

  Grant Full Disk Access to the app you start this from:
    System Settings > Privacy & Security > Full Disk Access
    -> enable Terminal (or iTerm, or the app running this script)
    -> quit and reopen that app, then run this file again.

  Alternatively, pass a readable copy of the configuration:
    ./"Nextcloud Sync Doctor.command" --config /path/to/nextcloud.cfg

EOF
fi

# Default to the web UI, but pass a subcommand through ("... --demo scan").
COMMAND="serve"
for arg in "$@"; do
  case "$arg" in
    scan|serve|report) COMMAND="" ;;
  esac
done
if [ -n "$COMMAND" ]; then
  exec "$PY" -m ncsd serve "$@"
else
  exec "$PY" -m ncsd "$@"
fi
