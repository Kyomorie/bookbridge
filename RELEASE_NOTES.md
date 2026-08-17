# Release Notes - 7.4.0

The headline is **positions you can trust again**. Two separate faults could put a
book's synced position in the wrong place and keep it there: an audiobook
transcribed from a partial download aligned happily against the part it got, and a
position read from the Audiobookshelf mobile app couldn't be understood at all.
Both are fixed, along with the cases where a book got stuck at 100% and every reset
came straight back.

This release also makes KOReader device sync usable straight after a restart — the
first sync on a 400-book library went from about ten minutes of waiting to
effectively instant — and stops a book's link to your reader breaking every time you
edit its metadata. On the features side: an opt-in **Shared Library** for multi-user
installs, a **Public URL** for each integration and **reverse proxy auto-login**
(both contributed by [@benjitobz](https://github.com/benjitobz)), and BridgeSync
plugin 0.6.3.

## What's New

- **Shared Library.** One opt-in setting makes every book anyone matches visible to
  every user, and gives a new account the whole library at once — so nobody re-matches
  a book someone else already processed. Progress, KOSync documents and stats stay
  strictly per-user. (#361)
- **Book links survive editing a book.** Editing metadata rewrites the file and
  changes the fingerprint KOReader syncs by, which used to break the link until you
  repaired it by hand. BookBridge now re-checks and re-links your books on a schedule
  (on by default, every 6 hours). Copies already on a device keep working. Re-encoded
  audiobooks and re-extracted covers are picked up the same way.
- **A book your reader opens can be identified against a BookOrbit library**, not
  just local files and Grimmory, with a search limit so one lookup can't turn into a
  mass download.
- **Public URL per integration.** Audiobookshelf, Grimmory, BookOrbit and CWA each
  get an optional public address, so the server URL can stay on an internal Docker
  hostname while every link and library button sends your browser somewhere it can
  actually reach. Contributed by [@benjitobz](https://github.com/benjitobz).
  (#366, #349)
- **Reverse proxy auto-login.** If Authelia, Authentik, Cloudflare Access, PocketID
  or similar already authenticated you, BookBridge can accept that instead of showing
  a second login form. Off by default, existing accounts only, and restricted to
  proxy addresses you list. Contributed by
  [@benjitobz](https://github.com/benjitobz). (#366)
- **Choose the position format written to Audiobookshelf.** Audiobookshelf keeps one
  ebook-position field that every reader shares, and readers disagree about its
  format. Pick **CFI** (the default — readable everywhere, exact in the official app
  and web reader), **Readium locator** (exact in third-party readers such as
  Audiobooth), or **Auto** to match whatever your reader last wrote.
- **BridgeSync plugin 0.6.3.** Faster highlight sync that sends only what changed,
  network work off KOReader's UI thread, resumable library sweeps, verified downloads
  and a verified self-updater, and a new **Max Downloads per Sync** cap so a few
  hundred newly matched books trickle onto the device instead of arriving in one
  marathon sync.

## Fixed

- **Audiobook positions no longer drift when only part of the audio arrived (#362).**
  A partial download was transcribed and aligned without complaint — one reported
  case turned a 28-hour audiobook into 21 hours and put the ebook about four hours
  ahead of the listener. Downloads are now size-verified, coverage is checked before
  transcription starts (new **Minimum Transcript Coverage**, default 85%), and a
  cached bad transcript is re-checked rather than replayed forever. **Books already
  aligned from short audio should be re-aligned.**
- **Progress percentages for audio sources read too high (#362)**, because the
  conversion divided by where the transcript stopped matching rather than the length
  of the book. Books correct themselves as they sync.
- **Audiobookshelf mobile reading positions work in both directions (#359).** The
  mobile apps and the web reader store positions in two different formats and only
  the web reader's was understood, so a mobile position logged an error every cycle
  and fell back to percentage matching, and a position pushed back couldn't be
  restored. Both are read correctly now and resolve to the exact spot in the book.
- **A book can no longer be wrongly marked finished everywhere (#358).** A bad
  alignment could resolve a mid-audiobook position to the end of the ebook and push
  100% to KOReader, Grimmory, Hardcover and StoryGraph, where it fought back against
  every reset. Bogus 100% is now refused the same way bogus 0% already was.
- **Deleting a mapping clears its KOReader progress (#358)**, so a book stuck at 100%
  no longer comes back at 100% when you delete and re-match it — previously the one
  obvious remedy was the one guaranteed to fail.
- **Re-adding a book someone else already matched finishes immediately (#360)**
  instead of re-running transcription and alignment for a claim that reuses the same
  pairing.
- **BridgeSync connects to numeric IPv4 server addresses (#367)**, which reported
  `DNS lookup failed` on Android KOReader, and a failed book download is retried on
  the next sync instead of being recorded as up to date.
- **Books that live only behind a library API no longer go stale**, and a failed
  refresh can't damage the copy you already had.
- **Multi-user installs are properly isolated** — one user's stale Audiobookshelf
  mapping no longer stops a shared book syncing for everyone, and background re-checks
  use each user's own credentials.
- **A missing Audiobookshelf item is flagged on the dashboard** instead of retrying
  silently forever, plus a batch of integration edge cases, log-noise fixes, and
  diagnostics that are more private and carry the stack detail needed to act on them.

## Operational Notes

**Deploy: pull the new image, run the database migration, restart, then re-download
the BridgeSync plugin on each KOReader device.** The migration is additive (one new
column recording the ebook length an alignment was built against) and runs
automatically on container start.

A few things to know after updating:

- **Books aligned from incomplete audio need a re-align.** The new coverage check
  catches them, but it does not repair a map that was already built. Re-forge any book
  whose ebook position has been running ahead of the audio.
- **Books already stuck at 100% need one manual pass.** The delete-and-re-match fix
  applies to future deletions; leftover rows from before the fix don't self-heal.
  Unlink and delete the document under *Settings → KOSync Documents*, then re-match.
- **Re-download the BridgeSync plugin** on every device — 0.6.3 carries the IPv4 fix,
  the download retry, and the download-cap setting.
- **The first device sync after this restart is still slow once.** The saved book list
  starts empty, so one rebuild is paid on the first sync and every restart after that
  is instant.
- **New settings default to current behavior**, so an install that changes nothing
  behaves as it did. The exceptions worth a look are *Minimum Transcript Coverage*
  (set it to 0 if you deliberately sync abridged audio) and *ABS Ebook Position
  Format* (leave it on **CFI** unless you read in a third-party Readium reader such
  as Audiobooth).
