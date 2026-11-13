#!/bin/bash
# Read-only macOS diagnostics for the Nextcloud Sync Doctor.
# Prints structure only: paths, URLs and secrets are redacted.
# Usage:  bash tools/macos-diagnostics.sh
set -u
domain="4eee1cb4-baeb-432a-a840-be0d54cc98ea"
fp="$HOME/Library/Application Support/FileProvider"
cfg="$HOME/Library/Containers/com.nextcloud.desktopclient/Data/Library/Preferences/Nextcloud/nextcloud.cfg"

echo "== 1. can this terminal read the client's files? =="
if head -c 1 "$cfg" >/dev/null 2>&1; then echo "nextcloud.cfg: readable"; else echo "nextcloud.cfg: NOT readable (Full Disk Access missing?)"; fi

echo
echo "== 2. folder keys in nextcloud.cfg =="
grep -E '^(Folders|[0-9]+\\Folders)' "$cfg" 2>&1 |
  sed -E 's#(localPath=).*#\1<path>#; s#(targetPath=).*#\1<path>#' | head -10

echo
echo "== 3. FileProvider domain directories =="
ls "$fp" 2>&1 | head -10
for d in "$fp/NKUJUXUJ3B.com.nextcloud.desktopclient" "$fp/com.nextcloud.desktopclient" "$fp/$domain"; do
  if [ -d "$d" ]; then
    echo "-- $d"
    find "$d" -maxdepth 2 2>&1 | head -10
  fi
done

echo
echo "== 4. domain plists (keys, paths redacted) =="
for p in "$fp/NKUJUXUJ3B.com.nextcloud.desktopclient"/*.plist "$fp/com.nextcloud.desktopclient"/*.plist; do
  [ -f "$p" ] || continue
  echo "-- $p"
  plutil -p "$p" 2>&1 | sed -E 's#(/Users/[^"]*)#<path>#g; s#(https?://[^"]*)#<url>#g' | head -40
done

echo
echo "== 5. other nextcloud.cfg files (targeted locations) =="
for p in "$HOME/Library/Preferences/Nextcloud/nextcloud.cfg" \
         "$HOME/Library/Containers/com.nextcloud.desktopclient/Data/Library/Preferences/Nextcloud/nextcloud.cfg" \
         "$HOME/Library/Group Containers/NKUJUXUJ3B.com.nextcloud.desktopclient/Library/Preferences/Nextcloud/nextcloud.cfg" \
         "$HOME/Library/Application Support/Nextcloud/nextcloud.cfg"; do
  [ -f "$p" ] && echo "found: $p"
done

echo
echo "== 6. sync folder root =="
ls -d "$HOME/Library/CloudStorage/"* 2>/dev/null
