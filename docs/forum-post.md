# Forum post: Nextcloud Sync Doctor 1.1.0 (Windows + macOS)

**Venue:** https://help.nextcloud.com — category **🏷️ General** (https://help.nextcloud.com/c/general/127).
General is where non-support discussions go and where similar project announcements are
posted ("Introducing EVA AI", "Gameworld", "NetBase"); the Desktop & mobile clients
category is for support questions and explicitly says support questions are deleted there.

**Before posting:** tick the template question "I have no support/technical question and
have seen the support category." Choose the tag that best fits if the form asks for one.

---

## Title

Nextcloud Sync Doctor 1.1.0 — see and fix every file that fails to sync (Windows + macOS)

## Body

I have no support/technical question and have seen the support category.

Hi all,

when the desktop client says *"Some files could not be synced!"* it sends you to the
activity list, which is not filtered, cannot show what the server actually has, and has
no per-file way out of a problem that is not a textbook conflict. On a Mac the same list
can also show errors you cannot clear at all.

I built **Nextcloud Sync Doctor** for exactly that list. It reads the client's own data,
talks to your server with the client's own credentials, and shows the files that are
really broken — with your local version and the server version side by side, and one
decision per file:

- keep **local**, keep **server**, or keep **both**
- **rename on the server** so a name Windows can't store (a Mac-created `pricing_grid_*.pdf`) syncs everywhere
- **roll back** to an earlier server version, or pull one out as a copy
- **delete** on either side (server deletes go to the trash), **ignore** forever, or hide

Every overwrite is backed up locally first, every action is logged, and the UI runs on
127.0.0.1 with a per-run token. Standard library only — no dependencies.

**What it finds:** names Windows can't store, upper/lower-case clashes, conflicts, sync
errors, over-long paths, and ignored files that the client still reports as errors. On
macOS it also finds Finder deletions that pile up in the folder's local `.Trash`, cloud
placeholders that were never downloaded, and the FileProvider folder the client does not
record in `nextcloud.cfg`.

**New in 1.1.0:** macOS support (sandbox-aware config discovery with a Full Disk Access
explanation, keychain credential reuse, FileProvider folder discovery) and `ncsd check` /
**Check the folder itself** — a local name check that needs neither the server nor the
client's journal, for exactly the situation where the log and journal are unreadable.

- Project: https://github.com/frischeDaten/nextcloud-sync-doctor
- Release (ZIPs for Windows and macOS): https://github.com/frischeDaten/nextcloud-sync-doctor/releases/latest
- GPL-2.0-or-later, Python 3.10+

It is a companion to the client, not a replacement: the missing resolution UI rather than
a second sync engine. I would especially like to hear from people who sync the same folder
between macOS and Windows — that is where most of the problems come from — and from macOS
users, since the FileProvider behaviour is the part I know least about.

---

## Notes for you (not part of the post)

- No Nextcloud employee asked for this; it is an independent tool. Keep the post factual
  and do not claim anything about the client's internals beyond what is observable.
- Do not ask for donations or stars in the post; that reads as promotion rather than a
  contribution.
- If a moderator moves it or asks questions, answer in the thread — the forum is the
  place where the Nextcloud team reads feedback about the desktop client.
- Optional follow-up: the desktop repo's issue tracker is for bugs, not announcements.
  If the thread produces a concrete client bug, a separate issue there (with a link back)
  is the right place.
