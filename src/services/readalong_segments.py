"""Sentence segmentation and clip-time interpolation for read-along EPUB 3
generation (Phase 2 of ``docs/PLAN_READALONG_EPUB3_GENERATION.md``).

Builds on Phase 1 (``src/utils/ebook_dom_map.py``): that module recovers DOM
provenance for every character offset in ``EbookParser.extract_text_and_map``'s
combined text; this module splits that same text into sentences -- scoped one
spine item at a time, since a SMIL ``<par>`` can never reference two XHTML
documents -- and interpolates each sentence's ``(char_start, char_end)``
through the book's alignment map to get an audio clip ``(ts_start, ts_end)``.

Sentence ids follow Storyteller's ``<chapter>-s<N>`` convention, confirmed
against the MIT-licensed ``storyteller-platform/storyteller`` source
(``libraries/align/src/align/ctc/mediaOverlay.ts``:
``id: index === 0 ? `${chapter.id}-s${sentenceRange.id}` : ...``, where the
integer resets to 0 for each chapter). BookBridge already depends on that
``-sN`` shape -- ``EbookParser.get_media_overlay_fragment_ids`` collects ids
out of existing SMIL, and a fragment it doesn't recognise collapses read-along
playback to the start of the chapter. Storyteller's ``chapter.id`` is the
EPUB manifest item's own ``id`` attribute; ``extract_text_and_map``'s
``spine_map`` does not carry that (only ``spine_index`` and ``href``), so this
module uses ``c<spine_index>`` in its place -- still deterministic (fixed by
EPUB spine order) and directly reversible to the ``spine_map``/
``SpineDomMap`` entry a later phase anchors into.

Sentence segmentation and the interpolation below are original code, not
ported from Storyteller. Storyteller's ``getSentenceRanges.ts`` solves a
harder problem this module doesn't have: error-aligning a noisy ASR
transcript against reference text via edit-distance search. BookBridge's own
alignment maps make that unnecessary -- they are already 5-10x finer than
what Storyteller itself writes into SMIL (measured in the plan's Sec. 0), so a
plain interpolation over an existing char/timestamp map is enough. Sentence
splitting here is a dependency-free regex scan (see ``requirements.txt`` --
Storyteller instead pulls in ``@echogarden/text-segmentation``, an npm
package with no Python equivalent already vendored in this repo; adding a new
Python dependency would move this change from a bind-mount restart to an
image rebuild for a problem stdlib regex already solves adequately).
Storyteller's ``enforceMonotonicAudioRanges`` clamp-rather-than-interpolate
philosophy for audio that runs backwards informed, but was not copied into,
the monotonic clamp in :func:`build_sentence_clips`.
"""
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from src.services.alignment_service import AlignmentService
    from src.utils.ebook_utils import EbookParser

logger = logging.getLogger(__name__)

# Lowercase, dot-stripped abbreviations after which a "." must not be read as
# a sentence end. Common English titles, Latin abbreviations, units and
# calendar short forms -- not exhaustive, and it doesn't need to be: a missed
# abbreviation only produces one extra (still valid, just smaller) sentence,
# never a wrong one.
_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "mx", "dr", "prof", "sr", "jr", "st", "sgt", "capt",
    "col", "gen", "lt", "cmdr", "rev", "hon", "esq", "rep", "sen", "gov",
    "vs", "etc", "eg", "ie", "cf", "al", "no", "nos", "vol", "vols",
    "fig", "figs", "pp", "approx", "inc", "ltd", "co", "corp", "dept",
    "univ", "assn", "bros", "ave", "blvd", "rd", "mt", "ft", "sq",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
    "oct", "nov", "dec",
    "mon", "tue", "tues", "wed", "thu", "thurs", "fri", "sat", "sun",
    "us", "uk", "un",
})

# A sentence-boundary candidate: one or more terminal punctuation marks,
# optionally followed by closing quotes/brackets (a quotation's closing mark
# lands *after* the period that ends the quoted sentence), then whitespace.
_BOUNDARY_RE = re.compile(
    r'(?P<punct>[.!?]+)(?P<quotes>[\'"‘’“”)\]]*)(?P<ws>\s+)'
)

# Characters that plausibly open a new sentence, checked on the character
# immediately following a candidate boundary's whitespace. Anything else
# (most commonly a lowercase letter -- a trailing clause, an ellipsis
# mid-thought) means the candidate is not a real boundary.
_SENTENCE_START_CHARS = frozenset('"\'‘’“”([{—–')

# How far a recorded `total_chars` may sit from the EPUB's current extracted
# length and still be treated as the same book. Deliberately smaller than a
# short sentence: extraction drift moves a handful of characters, a different
# edition moves thousands. See `_map_fits_epub` for the measurement behind it.
_TOTAL_CHARS_DRIFT_TOLERANCE = 16


def _preceding_token(text: str, pos: int) -> str:
    """The run of alnum/period characters immediately before ``pos``.

    Includes internal periods so a multi-part abbreviation like ``e.g`` (when
    checking its *second* period) or an initial-heavy acronym like ``U.S`` is
    captured whole rather than just its last letter.
    """
    j = pos
    while j > 0 and (text[j - 1].isalnum() or text[j - 1] == '.'):
        j -= 1
    return text[j:pos]


def _is_abbreviation(token: str) -> bool:
    """Whether a trailing "." right after ``token`` should not end a sentence."""
    if not token:
        return False
    if len(token) == 1 and token.isalpha():
        return True  # a bare initial, e.g. the "J" in "J. K. Rowling"
    return token.lower().replace('.', '') in _ABBREVIATIONS


def _is_real_boundary(text: str, punct_start: int, punct: str, after: str) -> bool:
    """Whether a regex-matched candidate is an actual sentence boundary."""
    if punct == '.':
        if _is_abbreviation(_preceding_token(text, punct_start)):
            return False
    if not after:
        return True  # end of this spine item's text -- always a real boundary
    ch = after[0]
    return ch.isupper() or ch.isdigit() or ch in _SENTENCE_START_CHARS


def split_sentences(text: str) -> List[Tuple[int, int]]:
    """Split ``text`` into sentence spans, half-open ``[start, end)``.

    Deterministic and dependency-free: a regex scan for terminal punctuation
    with a hand-maintained abbreviation exception list, not a statistical
    tokenizer -- the same text must always produce the same spans, because a
    stored reading position is a sentence id and regenerating a book must not
    renumber its sentences.

    ``text`` is assumed already trimmed of leading/trailing whitespace, true
    of every ``extract_text_and_map`` spine-item slice (bs4's
    ``get_text(strip=True)`` strips each contributing string before joining,
    per ``src/utils/ebook_dom_map.py``'s own module docstring).

    A boundary candidate -- terminal punctuation, optional closing quote or
    bracket, then whitespace -- is accepted unless the punctuation is a lone
    "." immediately after an abbreviation or initial (:func:`_is_abbreviation`),
    or the next sentence would start with a lowercase letter (a trailing
    clause continuing past an ellipsis, or a rare stray terminator). The
    final span always runs to ``len(text)`` even when the text has no
    trailing punctuation, so content is never silently dropped, and a
    sentence never crosses the end of ``text`` -- callers scope this to one
    spine item's slice so a sentence never spans two XHTML documents.

    :param text: one spine item's text, in ``extract_text_and_map``'s
        combined-text character space (a local, 0-based slice of it).
    :return: half-open ``(start, end)`` spans covering ``text``, in order.
    """
    if not text:
        return []

    spans: List[Tuple[int, int]] = []
    start = 0
    for match in _BOUNDARY_RE.finditer(text):
        punct = match.group('punct')
        content_end = match.start() + len(punct) + len(match.group('quotes'))
        after = text[match.end():]

        if not _is_real_boundary(text, match.start(), punct, after):
            continue
        if content_end > start:
            spans.append((start, content_end))
        start = match.end()

    if start < len(text):
        spans.append((start, len(text)))
    return spans


def sentence_id_for(spine_index: int, local_index: int) -> str:
    """The stable id for the ``local_index``-th sentence of spine item
    ``spine_index`` (both as :func:`build_sentence_clips` uses them: 1-based
    ``spine_index`` matching ``extract_text_and_map``'s ``spine_map``,
    0-based ``local_index`` resetting per spine item).

    Follows Storyteller's ``-sN`` convention -- see this module's docstring
    for the confirmed source and why the chapter prefix here is
    ``c<spine_index>`` rather than Storyteller's manifest item id.
    """
    return f"c{spine_index}-s{local_index}"


@dataclass(frozen=True)
class SentenceClip:
    """One sentence's text span and interpolated audio clip.

    ``char_start``/``char_end`` are half-open, in the same character space as
    ``EbookParser.extract_text_and_map``'s combined text (and
    ``ebook_dom_map.SpineDomMap``). ``ts_start``/``ts_end`` are seconds into
    the book's audio; monotonically non-decreasing and non-overlapping across
    the whole book by construction -- see :func:`build_sentence_clips`.
    """
    sentence_id: str
    spine_index: int
    href: str
    char_start: int
    char_end: int
    ts_start: float
    ts_end: float


@dataclass(frozen=True)
class SentenceClipResult:
    """The full per-book output of :func:`build_sentence_clips`.

    ``dropped_no_timestamp`` counts sentences excluded from ``clips`` because
    the alignment map returned no timestamp for one of their boundaries
    (``AlignmentService.get_time_for_char`` returning ``None`` -- only
    possible for a degenerate empty map). ``clamped_count`` counts sentences
    whose interpolated start and/or end had to be pulled forward to keep the
    book monotonic and non-overlapping -- a diagnostic, not an error; see
    :func:`build_sentence_clips`.
    """
    abs_id: str
    clips: List[SentenceClip]
    dropped_no_timestamp: int
    clamped_count: int


def _map_fits_epub(alignment_service: "AlignmentService", abs_id: str, combined_text_len: int) -> bool:
    """True only when ``abs_id``'s stored alignment map was fitted against
    text of exactly this length.

    Checks two independent fingerprints, preferring the more direct one:

    1. ``total_chars`` (``BookAlignment.total_chars``, read via
       ``AlignmentService.database_service.get_alignment_total_chars`` --
       both public attributes): every ``_publish_map`` call site in
       ``AlignmentService`` writes this as ``len(ebook_text)`` *at forge
       time*, deliberately and unconditionally -- it is a direct record of
       "this map was built against text of this length," not derived from
       where the anchors happened to land.
    2. ``AlignmentService.get_map_terminal_char`` (the map's own last
       anchor's char) -- the fallback ``SyncManager._get_alignment_epub_filename``
       uses, and the only signal available for a map predating the
       ``total_chars`` column (NULL) or stored with it as 0 (334 of 378 maps
       on the reference install, per the plan doc's Phase 2 section).

    These two disagree more often than the plan anticipated: measured live
    against this install, several CTC maps have a correctly-recorded
    ``total_chars`` exactly matching their current EPUB's length while their
    *last anchor* falls short of it by anywhere from a few hundred to
    hundreds of thousands of characters -- forced alignment (and lexical
    anchoring) does not always confidently anchor all the way to the final
    character of extracted text (back matter, acknowledgments, an unnarrated
    tail). Trusting the terminal char alone there would refuse a large
    fraction of genuinely-matching CTC maps for a reason that has nothing to
    do with a wrong EPUB. ``total_chars`` is authoritative when present
    because it is written directly, not inferred; the terminal-char check
    remains the fallback for the maps that predate it.

    ``total_chars`` is compared with a small absolute tolerance rather than for
    exact equality. Measured across every book on the reference install carrying
    a recorded ``total_chars`` (79 of them): 74 matched to the character, 4 were
    off by 1-15, one by 236, and none by more than that. The small deltas are
    extraction drift -- a re-stamped metadata field, a changed copyright line --
    on a book that is otherwise the same file. The condition this guard exists to
    catch is a *different edition*, which cannot differ by less than a sentence;
    the same install's real mismatch was 11,117 characters. So the tolerance is
    set below the length of a short sentence, which separates drift from a
    different book on principle rather than by fitting the sample.

    Returns False (refuse, never guess) when neither fingerprint is
    available or neither matches ``combined_text_len``.
    """
    try:
        total_chars = alignment_service.database_service.get_alignment_total_chars(abs_id)
    except Exception as e:
        logger.warning(
            "Could not read alignment total_chars for '%s': %s", abs_id, e, exc_info=True
        )
        total_chars = None
    if total_chars:
        return abs(int(total_chars) - int(combined_text_len)) <= _TOTAL_CHARS_DRIFT_TOLERANCE

    try:
        terminal = alignment_service.get_map_terminal_char(abs_id)
    except Exception as e:
        logger.warning(
            "Could not read alignment map terminal char for '%s': %s", abs_id, e, exc_info=True
        )
        return False
    if not terminal:
        return False
    return int(terminal) == int(combined_text_len)


def build_sentence_clips(
    parser: "EbookParser",
    filepath: Union[str, Path],
    alignment_service: "AlignmentService",
    abs_id: str,
) -> Optional[SentenceClipResult]:
    """Build the sentence + clip-time table for one book.

    Splits each spine item's text into sentences (:func:`split_sentences`,
    scoped so a sentence never crosses a spine item), then interpolates each
    sentence's ``(char_start, char_end)`` through ``abs_id``'s alignment map
    via ``AlignmentService.get_time_for_char`` -- already segment-aware for
    out-of-order narration (issue #426), so a reordered block is clamped to
    its own segment's edges rather than blended with its neighbour's.

    Refuses -- returns ``None`` -- rather than guess when the stored
    alignment map was not fitted against this exact EPUB (see
    :func:`_map_fits_epub`). A wrong map produces a read-along that drifts
    further the longer it plays, so this never emits output for one.

    Interpolated timestamps are additionally clamped to be monotonically
    non-decreasing and non-overlapping across the whole book (a running
    floor at the previous clip's end): a sentence whose two boundaries fall
    in two different out-of-order segments can otherwise resolve to an end
    timestamp before its own start (each edge clamps independently to its
    *own* nearest segment edge). This never silently emits a sentence with no
    timestamp at all -- one is only ever dropped, and counted, when the
    alignment map itself returns ``None`` for a boundary.

    :param parser: the ``EbookParser`` to source the book's spine text from.
    :param filepath: the EPUB path, exactly as ``extract_text_and_map`` accepts
        (an existing path, or a bare filename ``resolve_book_path`` can find).
    :param alignment_service: source of the book's stored alignment map.
    :param abs_id: the book's ABS id (the alignment map's primary key).
    :return: the per-book result, or ``None`` if the fitted-EPUB guard refused.
    """
    combined_text, spine_map = parser.extract_text_and_map(filepath)

    if not _map_fits_epub(alignment_service, abs_id, len(combined_text)):
        logger.warning(
            "🚫 Refusing to generate read-along sentences for '%s': stored "
            "alignment map was not fitted against '%s' (fitted-EPUB guard)",
            abs_id, filepath,
        )
        return None

    clips: List[SentenceClip] = []
    dropped = 0
    clamped = 0
    floor_ts = 0.0

    for entry in spine_map:
        item_text = combined_text[entry["start"]:entry["end"]]
        for local_index, (local_start, local_end) in enumerate(split_sentences(item_text)):
            char_start = entry["start"] + local_start
            char_end = entry["start"] + local_end
            sentence_id = sentence_id_for(entry["spine_index"], local_index)

            raw_start = alignment_service.get_time_for_char(abs_id, char_start)
            raw_end = alignment_service.get_time_for_char(abs_id, char_end)
            if raw_start is None or raw_end is None:
                dropped += 1
                logger.warning(
                    "'%s' sentence %s (chars %d-%d): alignment map returned no "
                    "timestamp for a boundary, dropping",
                    abs_id, sentence_id, char_start, char_end,
                )
                continue

            ts_start = max(float(raw_start), floor_ts)
            ts_end = max(float(raw_end), ts_start)
            if ts_start > float(raw_start) or ts_end > float(raw_end):
                clamped += 1

            clips.append(SentenceClip(
                sentence_id=sentence_id,
                spine_index=entry["spine_index"],
                href=entry["href"],
                char_start=char_start,
                char_end=char_end,
                ts_start=ts_start,
                ts_end=ts_end,
            ))
            floor_ts = ts_end

    return SentenceClipResult(
        abs_id=abs_id,
        clips=clips,
        dropped_no_timestamp=dropped,
        clamped_count=clamped,
    )
