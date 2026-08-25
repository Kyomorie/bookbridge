# Release Notes - 7.5.0

**Kavita** joins BookBridge as a full ebook source and reading client, library
cards can now show you the text at your current position, and the dashboard
learned to sort by when you added a book.

This release also contains a **security fix that matters for multi-user
installs**. If other people have accounts on your BookBridge, read the Security
section below and upgrade promptly. If you are the only user, it is an ordinary
upgrade.

## What's New

- **Kavita is a first-class ebook source and reading client.** BookBridge can
  search and import Kavita EPUBs, download them to managed KOReader devices,
  match them by their KOReader hash, sync progress in both directions, manage a
  collection for shelf-watch and Storyteller workflows, proxy covers, and use
  Kavita books for Hardcover and StoryGraph metadata. Progress rides Kavita's
  native KOReader endpoint, so the same position shows up in its web reader.
  Credentials and library/collection choices are per-reader, and polling and
  shelf-watch work exactly as they do for Grimmory and BookOrbit.

- **See the text at your current position, without opening a reader.** On any
  book with an ebook, **Show position** beside the progress bar opens a short
  excerpt with a marker where you are synced to — enough to recognise the spot.
  BookBridge uses the exact XPath or CFI when your reader saved one, maps an
  audiobook position through the stored alignment when it did not, and clearly
  labels a percentage-only estimate as approximate instead of implying precision.
  Contributed by [@Kyomorie](https://github.com/Kyomorie) in #397 (#394).

- **Sort your library by when you added a book.** The sort menu gains a **Date
  Added** option, and the arrow beside it flips newest/oldest. A series sorts by
  its most recently added book, so adding one title to an old series brings the
  whole group forward.

- **Searching for a book you have not added yet now leads somewhere.** The
  library search box only ever filtered books you already sync, so typing the
  name of an unmatched book emptied the page and explained nothing. It now offers
  to look for that title in your libraries, carrying your text straight into Add
  Book.

- **The Add Book tab shows how many books are waiting in your queue**, so work in
  progress is visible from anywhere.

- **Add Book and Suggestions show each edition's language** as a compact badge,
  when the provider supplies it. Display only — it does not affect search or
  ranking. Contributed by [@Kyomorie](https://github.com/Kyomorie) in #405.

- **Suggestions names the service each audiobook came from**, so you can tell
  whether a proposed pair uses Audiobookshelf, Grimmory, or BookOrbit audio
  before approving it. Contributed by
  [@Marcelwalter](https://github.com/Marcelwalter) in #407.

## Security

**Forge and Match now confine a local ebook source to your configured library.**

BookBridge did not fully verify that a selected *Local File* ebook stayed inside
your configured ebook directories, so a signed-in account could cause the server
to read a file from outside them.

Local sources are now restricted to `BOOKS_DIR`, any `EXTRA_EBOOK_DIRS`, and the
EPUB cache. Anything outside those roots is refused and logged.

**Who should upgrade promptly:** any install with accounts you would not trust
with the server's files. That is the multi-user case. On a single-user install,
or one where every account is already trusted, this granted nothing an
administrator could not already reach.

Affects **7.4.2 and earlier**. Found by external security review and reported
privately. No exploitation in the wild is known.

## Fixed

- **BridgeSync's *Test Connection* tells you when you have pointed it at the
  wrong server.** It only checked that the address accepted your login — and any
  KoSync-compatible server does, including other reading apps. It now asks the
  server to identify itself and says plainly when the answer is not BookBridge.
  Plugin updated to **0.6.6**. (#403)

- **The source badge on the Add Book page is visible, and it leads the card.**
  7.4.1's fix did not go far enough; the card was still locked to a square and
  went on slicing the badge off its bottom edge. It now sizes to its content, and
  the badge naming the source sits at the top of every candidate. (#381)

- **A KoSync timing setting you change now takes effect without a restart.** The
  instant-sync debounce window was read once at startup, so editing it appeared
  to save and then changed nothing. Contributed by
  [@Kyomorie](https://github.com/Kyomorie) in #404.

## Changed

- **The Settings page opens immediately again.** It was reading every stored
  alignment map just to count them — well over a gigabyte on a large library
  before the page would render. It asks the database for the counts now.

- **The dashboard no longer re-checks every cover on each visit.** Covers were
  served with no cache lifetime, so browsers revalidated all of them every time —
  hundreds of round trips, all answered "unchanged".

- **Storyteller edition creation is clearly separated from ordinary matching.**
  Add Book and Suggestions show only **Match All** when the current reader has no
  Storyteller account. When Storyteller is available, the former Forge actions say
  what they do: **Create Storyteller Edition & Match All** and **Create
  Storyteller Edition Only**.

## Upgrading

Pull the new image and restart:

```bash
docker compose pull && docker compose up -d
```

One database migration is included (a covering index that fixes the slow Settings
page). It applies automatically on boot — no manual step.

**KOReader plugin:** 7.5.0 ships BridgeSync **0.6.6**. Unlike 7.4.2, the plugin
can update itself — use **Check for Plugin Update** in BridgeSync, or download
the zip from your BookBridge account page. If you are still on 0.6.4 or earlier,
you must re-download by hand; see the 7.4.2 notes.

## Operational Notes

- No settings changes are required. Kavita is off until you configure it.
- Kavita needs a non-expiring auth key per reader, created in Kavita under **User
  Settings -> 3rd Party Clients** and saved under **Account -> My Integrations ->
  Kavita**. See [Configuration](docs/configuration.md#kavita).
- If your ebooks live outside `BOOKS_DIR`, make sure those folders are listed in
  `EXTRA_EBOOK_DIRS`. After this release a local file outside your configured
  roots is refused rather than silently read.
