# Release Notes - 7.3.2

The headline change is that **BookBridge leaves your disks alone when nothing is
happening**. A background task that prepared the list for the optional KOReader
managed-folder sync was re-reading — hashing — every book in your library once a
minute, forever. It did this on installs that had never enabled that feature and on
days you never opened a book, which meant hard drives could never spin down. That
list is now built only when a KOReader device actually asks for it, and each book's
hash is remembered until the file itself changes.

This release also makes shelving and book matching reliable and private per reader,
restores Grimmory shelf creation, reconnects Audiobookshelf instant sync behind
HTTPS reverse proxies, and adds a dashboard indicator showing which app last moved
each book.

This release does **not** change the BridgeSync KOReader plugin (still 0.5.4), so no
plugin re-download is required. Highlight and note sync continues to require the
**BridgeSync plugin from 7.1.0 or newer**; standard KOReader/KOSync progress sync
works without it.

## What's New

- **See at a glance which app last moved a book's position.** On the dashboard's In
  Progress cards, a small green dot marks the service that most recently updated
  where you are — Audiobookshelf if you last listened in the ABS app, KoSync if you
  last read on your e-reader. It stays out of the way while making it obvious which
  side drove the latest progress on books you're reading and listening to across
  services. (#333)

## Fixed

- **Your book files are no longer read constantly when nothing is happening.** The
  KOReader managed-folder sync manifest was rebuilt on a fixed one-minute schedule,
  hashing every book in the library each time regardless of whether any device ever
  requested it. BookBridge now builds that list on demand and caches each book's
  hash until the underlying file changes, so unchanged files are never re-read and
  idle installs let drives spin down. (#342)

- **Shelving and matching queues are consistent and private per reader.** Add Book
  and Suggestions now share one atomic background processor covering BookOrbit
  hashes, ownership claims, suggestion dismissal, and shelf-watch completion for
  direct, Forge & Match, Forge only, audio-only, and ebook-only approvals. Queue
  items are stamped to the reader who created them, so another user can no longer
  view, remove, clear, or process them; pre-upgrade unowned items remain available
  to the primary admin only, deleting a user removes their queued work, and
  malformed queue owners are discarded on the next rewrite. **A Grimmory shelf move
  can no longer lose a book** — the old shelf used to be cleared first, so a failed
  write to the new shelf left the book on neither. BookOrbit now recognizes a
  configured shelf name regardless of capitalization in every shelving path, so a
  book can't be added to and then removed from the same collection.

- **BookBridge can create Grimmory shelves again.** Shelving a book to a shelf that
  didn't exist yet silently did nothing: the create request carried icon fields that
  newer Grimmory builds reject outright, so the shelf was never created and the book
  was never filed. BookBridge now retries without those fields, and your configured
  Kobo and Up Next shelves are created on first use as intended. Installs whose
  shelves already existed were unaffected.

- **Audiobookshelf instant sync now connects through HTTPS reverse proxies.** Secure
  Audiobookshelf URLs are handed to the WebSocket transport as `wss://` rather than
  `https://`, which the socket client had been rejecting. Affected installs kept
  working on scheduled polling; instant sync resumes automatically once you restart
  on this version.

- **Two devices opening the same new book at once no longer fails.** KoSync document
  progress is written with an atomic conflict-safe upsert, so both devices update
  one shared row instead of racing into a database error.

- **KOReader managed-folder sync can recover Audiobookshelf ebooks with ordinary
  filenames**, instead of requiring the legacy `{item_id}_abs.epub` naming
  convention, and identifiers belonging to other ebook providers are no longer sent
  to Audiobookshelf during that fallback.

- **Positions in ebooks containing HTML comments now resolve.** Files produced by
  conversion tools often carry HTML comments; when a CFI-based position landed on
  one, resolution failed and that book was skipped for the cycle. Comment and
  processing-instruction nodes are now skipped, as the EPUB CFI specification
  requires. (#341)

- **An expired StoryGraph login is reported once, clearly.** An expired session
  cookie still looked configured, so every write attempt was redirected to the
  sign-in page and logged as a failure — for every book, on every cycle. BookBridge
  now logs one actionable warning and stops writing until the credentials change or
  a write succeeds, so saving a fresh cookie resumes syncing on its own.

- **Book editions with apostrophes can be selected from multi-result matching
  searches.** The Add / Update Book picker reads each edition's card metadata rather
  than embedding the filename and title in inline JavaScript. (#339)

- **A round of log-noise fixes.** Cleaning up a book whose Audiobookshelf collection
  no longer exists, and looking for a transcript on a book that has never been
  transcribed, are both normal outcomes and no longer surface as warnings or errors.
  When BookOrbit does refuse to create a collection, the log now reports the status
  and response instead of a bare failure line.

## Maintenance

The standalone match, batch match, and forge screens had been fully superseded by the
current Add / Update Book flow but were still shipping in the image. They have been
removed and those entry points now redirect to Add / Update Book, so the only visible
difference is which page you land on. The Shelfmark link now opens the configured
tool directly rather than embedding it in a BookBridge page. A batch of unused Python
code and four unused dependencies were dropped as well. No feature was lost.

## Operational Notes

No database migration is required for this release, and the BridgeSync KOReader
plugin is unchanged (0.5.4), so no plugin re-download is needed. Pull the new image
and restart BookBridge; the Audiobookshelf instant-sync and idle-hashing fixes take
effect on that restart.

If you build from source rather than pulling the published image, `requirements.txt`
now lists four fewer packages. Nothing needs to be installed — a plain restart is
enough — and a rebuild simply produces a slightly smaller image.
