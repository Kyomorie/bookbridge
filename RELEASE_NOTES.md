# Release Notes - 7.3.3

The headline change is that **audiobook covers load again when Audiobookshelf is only
reachable from the server — and your Audiobookshelf API token is no longer sent to the
browser.** If Audiobookshelf runs as a Docker service alongside BookBridge, its address
is an internal name like `https://audiobookshelf` that your browser cannot reach, yet
that was exactly the address the dashboard put in each cover image, together with your
API token. Every audiobook cover came up blank, and anyone who viewed the page received
the token. Covers now always go through BookBridge's own cover routes, so the browser
only ever talks to BookBridge and no library address or credential leaves the server.

This release also adds **external GPU transcription against any OpenAI-compatible
server**, stops an Audiobookshelf ebook library from burying real audiobook
suggestions, and repairs book matching for libraries BookBridge reaches only over the
network.

This release does **not** change the BridgeSync KOReader plugin (still 0.5.4), so no
plugin re-download is required. Highlight and note sync continues to require the
**BridgeSync plugin from 7.1.0 or newer**; standard KOReader/KOSync progress sync
works without it.

## What's New

- **Transcription can now run on an external GPU server, including ones that are not
  whisper.cpp.** The Whisper.cpp provider works against any OpenAI-compatible
  transcription endpoint — speaches, NVIDIA parakeet, or a proxy such as llama-swap —
  so a spare GPU elsewhere on your network can do the work without giving the
  BookBridge container a GPU of its own. A 🔗 **Test** button next to the server URL
  confirms the endpoint is reachable before you save.

  Two new options cover servers that behave differently from whisper.cpp. **Split
  Uploads** breaks each upload into short sub-requests and re-times the results, which
  restores sync accuracy on servers that return one merged segment per request — set it
  to 2 or 3 minutes on those, and leave it off for servers that already return
  fine-grained segments. **Send Original Audio** hands the original mp3/m4b straight to
  servers that decode and chunk it themselves, skipping minutes of local conversion per
  book; leave it off for whisper.cpp, which requires 16kHz WAV input. Contributed by
  [@chelming](https://github.com/chelming). (#330)

- **Audio Split Length is now adjustable from Settings.** The size of the chunks audio
  is cut into before transcription was previously fixed at 45 minutes; lowering it helps
  a smaller GPU get through long books without running out of memory. Contributed by
  [@chelming](https://github.com/chelming).

- **Books that share a title are no longer impossible to tell apart when you add them.**
  Picking the right book out of a series used to be guesswork: three separate books all
  called "Warlock" showed up as three identical cards, with nothing to say which was
  book one, two, or three. Both sides of the Add / Update Book picker now show a small
  edition line under each result — the book's subtitle when your library has one
  ("Book 2"), and otherwise its series position ("Warlock #2"). This surfaces detail
  BookBridge was already fetching and quietly discarding, so libraries that track
  subtitles or series see the difference immediately, and standalone books look exactly
  as they did before. The label only helps you choose; the title BookBridge stores and
  shows on your dashboard is unchanged.

## Fixed

- **Audiobook covers load when Audiobookshelf is only reachable from the server, and
  your Audiobookshelf API token is no longer sent to the browser.** Books with a local
  ebook cover hid the problem, because BookBridge shows that first and only falls back
  to the Audiobookshelf address when it is missing. BookBridge already had its own cover
  routes for Audiobookshelf, Grimmory and BookOrbit; covers now always go through them.
  This applies everywhere covers appear — the dashboard, series stacks, Suggestions and
  the match queue — and covers already saved the old way are corrected as the page is
  drawn, so your existing books fix themselves on the next load with nothing for you to
  do. Reported by [@mahood73](https://github.com/mahood73). (#353)

- **Ebooks in an Audiobookshelf library are no longer offered as audiobooks to match.**
  If your Audiobookshelf holds ebooks alongside audiobooks, every one of those ebooks was
  treated as an audiobook by the Suggestions scan, so it appeared as its own 100% match
  against its own file. On a large ebook collection that buried the real audiobook
  suggestions under thousands of bogus ones. BookBridge asked Audiobookshelf for
  audiobooks only, but Audiobookshelf has no such filter and returned everything; the
  results are now checked for actual audio before use. (#351)

- **Books matched from a library BookBridge reaches only over the network now actually
  download.** If your ebooks live in BookOrbit or Grimmory and you have not mounted that
  library's folder into BookBridge, matching a book could still end in `EPUB not found
  in BookOrbit` and a job stuck on "failed, retry later" — even though the match had
  recorded exactly which book you picked. BookBridge was throwing that away and
  searching the library again by filename, which only worked when the filename happened
  to read like the book's title; anything with a series number or a year in it
  (`07. Agent in Place (2018).epub`) failed. It now fetches the exact book you matched,
  by id. Three further improvements come with it: a book you match is downloaded once
  and kept instead of being fetched again later; the filename search still used for
  older matches now copes with series numbers and years; and an explicitly matched book
  is never quietly swapped for a different edition found by searching. Affected books
  recover on their own — they are retried automatically. (#352)

- **"Add all exact" on the Suggestions page no longer silently does nothing.** After a
  long library scan, the results were held only in memory — so if BookBridge restarted,
  or you came back to a tab that had been sitting open, clicking **Add all exact** or
  **Add selected** queued nothing at all. The counter still dropped to zero and every
  card still greyed out, so it looked like it had worked, and the only way to get a book
  onto the dashboard was to add it by hand from Add Book. Scan results are now restored
  from the cache BookBridge already writes to disk, so a restart no longer throws away a
  scan that took minutes to run. If a suggestion genuinely can't be queued, the page now
  says so instead of quietly pretending otherwise, and the affected cards stay
  selectable. (#351)

- **Manual bug reports now include the recent logs needed to investigate them.** A
  written report could previously arrive with no technical evidence whenever no warning
  was buffered at that moment. Manual reports now attach up to 200 recent, scrubbed
  INFO-and-higher log lines even when their warning list is empty. Those lines are shown
  only on the private report detail page and do not create anomaly findings.

- **Raising the log level now actually produces the extra detail.** Choosing DEBUG in
  Settings updated the logger but left the existing log handler at its previous level,
  so the messages were generated and then discarded before reaching the log. Contributed
  by [@chelming](https://github.com/chelming).

- **A failed transcription against an external server now reports the server's error.**
  With Send Original Audio enabled, the failure path crashed while composing its own
  error message and buried the real cause from the transcription server. Contributed by
  [@chelming](https://github.com/chelming).

- **Concurrent KOReader manifest builds no longer collide while linking the same ebook
  hash.** Manifest hash linking now uses the same conflict-safe SQLite upsert strategy
  as KoSync progress writes, preserving existing progress and metadata while ensuring
  concurrent builders produce one shared document row.

- **Diagnostics no longer exhaust their warning-template limit on short book IDs,
  filenames, or XPath fragments.** Short values inside quotes now share a stable
  diagnostic template while the original scrubbed warning remains available for
  troubleshooting. The scrubber also no longer mistakes the closing quote of one short
  value for the opening quote of another.

## Operational Notes

No database migration is required for this release, the BridgeSync KOReader plugin is
unchanged (0.5.4) so no plugin re-download is needed, and `requirements.txt` and the
Dockerfile are untouched. Pull the new image and restart BookBridge.

The cover fix needs nothing from you: existing dashboard entries are rewritten to the
proxied routes as each page is drawn.

The new external-transcription options are off by default, so nothing changes for
installs using local Whisper or a plain whisper.cpp server. See
[Configuration](docs/configuration.md#transcription-settings) for the full option list, including
when to use Split Uploads and Send Original Audio.
