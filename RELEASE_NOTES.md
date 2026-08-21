# Release Notes - 7.4.1

A maintenance release for 7.4.0. The headline is **the BridgeSync plugin starts on a
fresh install again** — 0.6.3 crashed before it ever ran on any device that did not
already have a BridgeSync log file, which meant every new KOReader setup. It also
stops Hardcover losing the read you already finished, keeps a stock Kobo from
dragging CWA progress back, and adds five opt-in settings, four of them from
contributors.

## What's New

- **Share an existing library with the people who already have accounts.** *Shared
  Library* only ever applied going forward. Settings → Users now has a **Share
  library with all users** button that hands the whole catalog to every active user
  at once. Visibility only — progress, KoSync documents and stats stay per-user.
  (#384)
- **Change an existing account between user and admin.** Settings → Users gains a
  *Make admin* / *Make user* button, so widening or restricting someone's access no
  longer means deleting and recreating them and losing their progress and saved
  logins. A promoted admin keeps using their own service accounts. (#385)
- **Propagate Completion.** Percentages never agree at the end of a book, so a title
  you finished in one app can sit at 92–97% in another. Turn it on under
  Settings → Sync and finishing anywhere marks the book finished everywhere. Off by
  default. Contributed by [@benjitobz](https://github.com/benjitobz). (#374)
- **Auto-match suggestions.** High-confidence candidates link themselves as a scan
  finds them instead of waiting on the Suggestions page. Off by default; loose title
  matches and same-folder candidates are never linked automatically. Contributed by
  [@benjitobz](https://github.com/benjitobz). (#375)
- **Cross-device rewind policy.** The protection that ignores a *lower* percentage
  from a second device is now a setting under Settings → KOSync, still on by
  default. Contributed by [@Kyomorie](https://github.com/Kyomorie). (#391)
- **Compare text positions when percentages disagree.** Your reader and BookBridge
  can measure the same EPUB on slightly different scales, letting a stale spot win
  on the number alone. Off by default, because comparing means opening the book.
  Contributed by [@Kyomorie](https://github.com/Kyomorie). (#380)

## Fixed

- **BridgeSync 0.6.4: the KOReader plugin starts on a fresh install.** 0.6.3 crashed
  during startup on any device without an existing log file, so the plugin appeared
  in KOReader's list but never actually ran. Managed-folder detection and error
  reporting are improved in the same version. Reported in #370, fixed by
  [@theryanmc](https://github.com/theryanmc). (#373, #377)
- **Re-reading a book no longer overwrites the read you already finished**, and a
  stale reader no longer invents one — a re-read is recorded only once the position
  really moves forward. Contributed by [@Kyomorie](https://github.com/Kyomorie).
  (#398, #390)
- **A broken Hardcover connection reports itself once, clearly, with the next step**
  instead of repeating its whole HTML error page every attempt; transient errors are
  retried and recovery is announced.
- **Startup stops reporting a connection failure you cannot fix.** It now checks the
  admin's own account credentials rather than the abandoned global copies left
  behind by the multi-user upgrade.
- **Positions at the very start of a chapter no longer drift forward** — in one
  reported case by about 7,900 characters. Contributed by
  [@Kyomorie](https://github.com/Kyomorie). (#382, #276)
- **CWA progress no longer snaps back when a stock Kobo opens the book** (#364), and
  **Audiobookshelf item lookups ask for the expanded record** so audio files and
  chapters are present. The latter contributed by
  [@TheSingularis](https://github.com/TheSingularis). (#371)
- **Mark Complete works on titles containing an apostrophe**, the source badge stays
  visible on long Add Book titles (#381), and StoryGraph/Hardcover cooldowns fire
  when their timer expires instead of waiting for the next full sync cycle.
- **KOReader position comparisons survive a restart**, now that the XPath ordering is
  persisted and prewarmed. Contributed by
  [@Kyomorie](https://github.com/Kyomorie). (#389)

## Operational Notes

**Deploy: pull the new image, restart (the database migration runs automatically on
start), then re-download the BridgeSync plugin on each KOReader device.** The
migration is additive — one table caching KOReader XPath ordering.

- **Re-download BridgeSync on every device.** 0.6.3 cannot update itself: the crash
  happens before the updater runs. Get 0.6.4 from *Settings → KOSync* and copy it
  over the existing `bridgesync.koplugin` folder.
- **Every new setting defaults to current behavior**, so an install that changes
  nothing behaves as it did.
- **Admins no longer inherit the global service credentials.** Those settings are
  the primary admin's own logins mirrored outward, so a *second* admin with blank
  integration fields used to sync against the primary admin's Audiobookshelf,
  Grimmory, BookOrbit and CWA accounts. Only the primary admin inherits now. If you
  created a second admin account and left its Integrations blank, fill them in under
  *Settings → Users → Integrations* — that account's services are skipped until you
  do. Single-admin installs are unaffected.
