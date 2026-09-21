"""EPUB 3 read-along assembly: marker injection, SMIL emission, OPF rewriting,
and repackaging (Phase 3 of ``docs/PLAN_READALONG_EPUB3_GENERATION.md``).

Builds on Phase 1 (``src/utils/ebook_dom_map.py`` -- DOM anchor map) and Phase 2
(``src/services/readalong_segments.py`` -- sentence/clip table) to produce a new
EPUB whose spine documents carry empty ``<span id="c<spine>-s<n>">`` markers at
each sentence's start, with one SMIL media-overlay document per spine item that
has sentences, an OPF rewritten in place to reference them, and the source
audio embedded as-is (no transcode -- that is Phase 4).

**Anchor strategy is empty marker spans, not range-wrapping** (plan Sec. "Phase
3 -- EPUB 3 assembly", carried over from Phase 2's docstring). A sentence
frequently crosses inline elements (``<em>``, ``<a>``), and wrapping its full
range in a new element would require splitting those inline elements too --
easy to get subtly wrong and easy to emit invalid markup from. An empty marker
at the sentence's *start* is enough: SMIL ``<par>`` seeking only needs to know
where playback should jump to, and the ``<audio>`` clip's own ``clipEnd``
already governs how long to stay there. This is a deliberate, previously-made
decision -- see the Phase 2 module's docstring and the plan doc -- not
something to silently revisit here.

This module reuses two Phase 1 internals directly rather than re-implementing
them: ``ebook_dom_map.locate_offset`` (turns a sentence's global char offset
into ``(spine_index, node_index, node_offset)``) and
``ebook_dom_map.content_string_nodes`` (the exact node list bs4's
``get_text()`` would enumerate, in the same order ``locate_offset``'s
``node_index`` indexes into). Re-deriving that filtering here risked drifting
from Phase 1's own carefully-pinned bs4 behaviour notes; importing it keeps
the two modules' idea of "node N" identical by construction. Neither Phase 1
nor Phase 2 is modified by this module.

**SMIL/OPF conventions were read from Storyteller's real writer**
(``storyteller-platform/storyteller``, MIT), specifically
``applications/web/src/assets/library/scanner`` and
``applications/web/src/app/api/v2/books/[bookId]/debug/media-overlay/route.ts``
by way of ``libraries/align/src/align/ctc/mediaOverlay.ts``'s
``createMediaOverlay``: the ``<seq epub:textref="...">``/``<par><text
src="...#id"/><audio src="..." clipBegin="Ns" clipEnd="Ns"/></par>`` shape, and
the plain-seconds-plus-``s`` clock format Storyteller writes
(``${value.toFixed(3)}s``). That format is also already what this repo's own
``src/utils/smil_extractor.py`` (``_parse_timestamp``) expects to read, so it
was independently corroborated, not taken on faith. None of that TypeScript
was copied, ported, or transliterated -- the shape is the public IDPF EPUB 3
Media Overlays specification's own worked examples, and the Python below
(marker grouping/splitting, relative-href computation via ``posixpath.relpath``
against whatever directory layout a given EPUB actually uses, OPF surgery via
lxml that preserves everything not explicitly changed, zip repackaging) is
original, written against this codebase's own data shapes. Storyteller's own
directory layout convention (fixed sibling ``Text/``/``Audio/`` folders, hrefs
hard-coded as ``../Audio/<file>``) is *not* reused, since an arbitrary library
EPUB cannot be assumed to share it; hrefs here are computed with
``posixpath.relpath`` from whatever directories the source EPUB and the
generated ``readalong/`` folder actually land in. Per the same judgment Phase
2 made for its own Storyteller-confirmed-but-not-copied ``-sN`` id shape, no
MIT notice is added to this file.

**Phase 4 Part A -- contiguous clips.** A live run measured the embedded
overlay's summed duration at 4.28% short of the real audio (against
BookOrbit's own ``min(300s, 5%)`` tolerance) -- inter-sentence pauses belong
to no clip, so they are never counted. Storyteller's own SMIL has zero gaps
across all 10,312 ``<par>``s of a real 10.1h book: every clip's ``clipEnd``
equals the next one's ``clipBegin``. This module now reproduces that,
extending each clip's end to the next one's start (:func:`_extend_clips_to_contiguous`)
rather than doing it in ``readalong_segments.py``: contiguity is a property
of the *emitted SMIL sequence* (which sentences actually got a ``<par>``,
after this module's own no-DOM-location drops -- Phase 2 knows nothing about
those), and it needs the real, final embedded audio's probed duration to
extend the book's last clip, which only this module (the one doing the
transcode below) has. Leaving ``SentenceClip.ts_end`` itself untouched in
Phase 2 also keeps it meaning "this sentence's own measured end" for the
per-sentence highlight-range upgrade this module's docstring already floats
as a later step -- extending it there would quietly repurpose it into
"how long to keep highlighting", a different value.

**Phase 4 Part B -- audio packaging.** Source audio is transcoded to mono AAC
via ``ffmpeg`` (budget: Storyteller ships 141MB for a 10.1h book, ~31kbps) at
a configurable ``READALONG_AUDIO_BITRATE``, and, for a multi-file audiobook,
concatenated into the single physical file the embedded SMIL references.
Concatenation uses ffmpeg's ``concat`` *filter* (full decode of every part,
then concatenate the decoded samples) rather than the ``concat`` demuxer,
because that is exactly what ``ForcedAligner._load_audio`` already does to
build the single timeline the stored alignment map's timestamps are
absolute against -- reproducing that decode order means the timestamps need
no adjustment for whatever this module embeds.
"""
import logging
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union, TYPE_CHECKING

from bs4 import BeautifulSoup, NavigableString
from lxml import etree

from src.utils.ebook_dom_map import (
    SpineDomMap,
    content_string_nodes,
    build_dom_anchor_map,
    locate_offset,
)
from src.services.readalong_segments import SentenceClip, build_sentence_clips

if TYPE_CHECKING:
    from src.services.alignment_service import AlignmentService
    from src.utils.ebook_utils import EbookParser

logger = logging.getLogger(__name__)

_OPF_NS = "http://www.idpf.org/2007/opf"
_SMIL_NS = "http://www.w3.org/ns/SMIL"
_OPS_NS = "http://www.idpf.org/2007/ops"

_AUDIO_MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".wav": "audio/x-wav",
}

# Base name for the folder new read-along files (SMIL + embedded audio) are
# written into, sibling to the OPF. Suffixed with a counter on the vanishingly
# rare chance a library EPUB already has an entry with this name.
_READALONG_DIR_BASE = "readalong"


@dataclass(frozen=True)
class SpineOverlayResult:
    """One spine item's generated media overlay.

    ``href`` is the spine item's full archive path (matches
    ``extract_text_and_map``'s ``spine_map`` entry exactly). ``smil_href`` is
    the generated SMIL document's own full archive path. ``duration_seconds``
    is this overlay's own temporal span (last par's ``clipEnd`` minus the
    first par's ``clipBegin``) -- the per-overlay ``media:duration`` the OPF
    records for it.
    """
    spine_index: int
    href: str
    smil_href: str
    par_count: int
    duration_seconds: float


@dataclass(frozen=True)
class ReadalongBuildResult:
    """The full output of :func:`build_readalong_epub`.

    ``dropped_no_timestamp`` is carried over from Phase 2's
    ``SentenceClipResult`` (a sentence whose alignment map lookup returned no
    timestamp for a boundary). ``dropped_no_location`` counts sentences Phase 2
    did produce a clip for, but whose start offset this phase could not place
    in the DOM (``ebook_dom_map.locate_offset`` returned ``None``, or
    disagreed about which spine item it belongs to) -- both are excluded from
    the generated SMIL, since a ``<text>`` reference to a fragment id that was
    never inserted collapses playback to the chapter start.

    ``total_duration_seconds`` (Phase 4 Part A) is the summed *contiguous*
    overlay duration -- every clip's end already reaches the next one's start
    (or, for the book's very last clip, the real embedded audio's own probed
    length), so this should land close to the full audio duration rather than
    running short by the sum of every inter-sentence pause. ``audio_bitrate``
    (Phase 4 Part B) is the ``READALONG_AUDIO_BITRATE`` value actually used
    for this build (the configured value, or the safe default if the
    configured value did not parse as an ffmpeg bitrate) -- recorded here so
    a caller/test can confirm which one took effect without re-reading the
    setting itself.
    """
    abs_id: str
    output_path: str
    spine_overlays: List[SpineOverlayResult]
    total_sentences: int
    dropped_no_timestamp: int
    dropped_no_location: int
    total_duration_seconds: float
    audio_href: str
    audio_bitrate: str


def _find_opf_path(zf: zipfile.ZipFile) -> Optional[str]:
    """The OPF's full archive path, read from ``META-INF/container.xml``.

    Mirrors ``EbookParser._build_href_resolver``'s own container.xml read
    (regex, not a full XML parse -- consistent with that existing precedent).
    """
    try:
        container = zf.read("META-INF/container.xml").decode("utf-8", "replace")
    except KeyError:
        return None
    match = re.search(r'full-path="([^"]+)"', container)
    return match.group(1) if match else None


def _unique_archive_dir(zip_names: set, opf_dir: str, base: str) -> str:
    """A directory (relative to ``opf_dir``) that collides with no existing entry."""
    candidate_base = posixpath.join(opf_dir, base) if opf_dir else base
    candidate = candidate_base
    n = 2
    prefix = candidate + "/"
    while any(name.startswith(prefix) or name == candidate for name in zip_names):
        candidate = f"{candidate_base}-{n}"
        prefix = candidate + "/"
        n += 1
    return candidate


def _unique_manifest_id(existing_ids: set, base: str) -> str:
    """A manifest ``id`` that collides with no id already in ``existing_ids``."""
    if base not in existing_ids:
        return base
    n = 2
    while f"{base}-{n}" in existing_ids:
        n += 1
    return f"{base}-{n}"


def _audio_media_type(path: Union[str, Path]) -> str:
    """Best-effort ``media-type`` for the embedded audio file's extension."""
    ext = Path(path).suffix.lower()
    guessed = _AUDIO_MEDIA_TYPES.get(ext)
    if guessed:
        return guessed
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


# ffmpeg accepts a bare bit-rate number or one suffixed with k/K (kilobits) or
# m/M (megabits) for -b:a (e.g. "32k", "128000", "1.5M"). Anything else is
# rejected by _resolve_audio_bitrate rather than handed to the subprocess.
_BITRATE_RE = re.compile(r'^\d+(\.\d+)?[kKmM]?$')

# Default READALONG_AUDIO_BITRATE (see src/utils/config_loader.py's
# DEFAULT_CONFIG, which must match this value). 32kbps mono AAC is the
# reference point from real data: Storyteller ships 141MB for a 10.1h
# read-along book, ~31kbps -- almost exactly the ~14.4MB/hour this bitrate
# works out to (32,000 bits/s / 8 / 3600s). Spoken-word audio (the only
# content this file ever carries -- it exists to drive playback position for
# a highlight, not to be listened to on its own merits) stays intelligible
# well below music bitrates, and every device that downloads a generated
# read-along book pays this size across the whole library.
_DEFAULT_AUDIO_BITRATE = "32k"


def _resolve_audio_bitrate() -> str:
    """Read ``READALONG_AUDIO_BITRATE`` per call -- never cached at import or
    in a Singleton's ``__init__`` (CLAUDE.md's settings-system rule: the
    Settings UI writes ``os.environ`` immediately and every consumer must see
    it without a restart).

    Falls back to :data:`_DEFAULT_AUDIO_BITRATE` -- logging a warning, never
    raising -- for anything that is not a value ffmpeg's ``-b:a`` accepts.
    An admin typo in this setting must degrade generation to a safe default,
    not abort it.
    """
    raw = os.environ.get("READALONG_AUDIO_BITRATE", _DEFAULT_AUDIO_BITRATE).strip()
    if not _BITRATE_RE.match(raw):
        logger.warning(
            "⚠️ READALONG_AUDIO_BITRATE=%s is not a valid ffmpeg bitrate "
            "(expected e.g. '32k'); using default %s",
            raw, _DEFAULT_AUDIO_BITRATE,
        )
        return _DEFAULT_AUDIO_BITRATE
    return raw


def _normalize_audio_paths(
    audio_paths: Union[str, Path, Sequence[Union[str, Path]]],
) -> List[Path]:
    """Normalize the caller's audio input to an ordered list of ``Path``s.

    A bare ``str``/``Path`` (the common single-file case) becomes a
    one-element list rather than being iterated character-by-character.
    Order is preserved exactly as given and never re-sorted: for a
    multi-file audiobook this must already be the same order the book was
    force-aligned against (``ForcedAligner._load_audio`` decodes parts in
    the order it is given them, back to back, with nothing trimmed or added
    between them -- see this module's docstring), since that order is what
    the stored alignment map's timestamps are absolute against.
    """
    if isinstance(audio_paths, (str, Path)):
        return [Path(audio_paths)]
    return [Path(p) for p in audio_paths]


def _transcode_audio_for_embed(audio_paths: List[Path], bitrate: str, output_path: Path) -> bool:
    """Transcode (and, for more than one part, concatenate) source audio into
    a single mono AAC file at ``output_path``.

    Multi-file audiobooks (Grimmory/BookOrbit both stage tracks to disk as
    ``track_000.<ext>``, ``track_001.<ext>``, ... -- see
    ``forge_service.py``'s ``_copy_*_audio_files``) are joined with ffmpeg's
    ``concat`` *filter*, not the ``concat`` *demuxer*: the filter fully
    decodes every input and concatenates the decoded samples, which is
    exactly what ``ForcedAligner._load_audio`` already does (each part
    streamed through its own ffmpeg decode into one continuous buffer, in
    list order) to build the single timeline the stored alignment map's
    timestamps are absolute against. Reproducing that same decode-then-concat
    semantics here means those timestamps need no adjustment for whatever
    actually ends up embedded, whether it is one file or many.

    Returns ``False`` -- never raises -- on any ffmpeg failure, including
    ffmpeg not being installed, so the caller can refuse the build the same
    way every other guard in this module does.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-nostdin", "-loglevel", "error"]
    for path in audio_paths:
        cmd += ["-i", str(path)]
    if len(audio_paths) > 1:
        graph = "".join(f"[{i}:a:0]" for i in range(len(audio_paths)))
        cmd += [
            "-filter_complex", f"{graph}concat=n={len(audio_paths)}:v=0:a=1[aout]",
            "-map", "[aout]",
        ]
    else:
        cmd += ["-map", "0:a:0"]
    cmd += ["-vn", "-sn", "-ac", "1", "-c:a", "aac", "-b:a", bitrate, str(output_path)]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error(
            "Read-along audio transcode failed for %d part(s) at bitrate %s: %s",
            len(audio_paths), bitrate, e, exc_info=True,
        )
        return False


def _probe_duration_seconds(path: Union[str, Path]) -> Optional[float]:
    """The real duration, in seconds, of an audio file via ``ffprobe``.

    Mirrors ``Transcriber.get_audio_duration``'s own ffprobe invocation
    rather than importing it: that class's module pulls in the
    transcription stack's heavier dependencies for what is, here, a single
    stdlib subprocess call. Returns ``None`` -- never raises -- on any
    failure, so the caller can degrade to leaving the book's final clip
    un-extended instead of crashing the whole build.
    """
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as e:
        logger.warning("Could not probe audio duration for '%s': %s", path, e, exc_info=True)
        return None


def _format_smil_clock(seconds: float) -> str:
    """Format seconds as an EPUB 3 ``media:duration`` clock value (``H:MM:SS.mmm``).

    Matches the IDPF Media Overlays 3.0 spec's own worked examples
    (``0:32:29.000``, ``1:57:35.000``) -- hours unpadded, minutes/seconds
    zero-padded to 2 digits, milliseconds to 3.
    """
    total_ms = round(max(0.0, seconds) * 1000)
    hours, rem_ms = divmod(total_ms, 3_600_000)
    minutes, rem_ms = divmod(rem_ms, 60_000)
    secs, ms = divmod(rem_ms, 1000)
    return f"{hours}:{minutes:02d}:{secs:02d}.{ms:03d}"


def _markers_for_spine_item(
    dom_map: List[SpineDomMap],
    clips: List[SentenceClip],
    spine_index: int,
) -> Tuple[List[Tuple[int, int, str]], int]:
    """Resolve each clip's sentence-start char offset to a DOM insertion point.

    Returns ``(markers, dropped)``: ``markers`` are ``(node_index,
    node_offset, sentence_id)`` triples ready for :func:`_inject_markers`.
    ``dropped`` counts sentences excluded because ``locate_offset`` returned
    ``None`` (the offset landed in a synthetic separator gap -- should not
    happen for a real sentence start, see this module's docstring, but is not
    assumed) or resolved to a different spine item than expected.
    """
    markers: List[Tuple[int, int, str]] = []
    dropped = 0
    for clip in clips:
        located = locate_offset(dom_map, clip.char_start)
        if located is None or located[0] != spine_index:
            logger.warning(
                "Read-along marker: could not place sentence %s (char %d) in "
                "spine item %d; dropping its SMIL par",
                clip.sentence_id, clip.char_start, spine_index,
            )
            dropped += 1
            continue
        _, node_index, node_offset = located
        markers.append((node_index, node_offset, clip.sentence_id))
    return markers, dropped


def _extend_clips_to_contiguous(
    clips: List[SentenceClip], audio_duration_seconds: Optional[float],
) -> List[SentenceClip]:
    """Extend each clip's ``ts_end`` to the next clip's ``ts_start`` (Phase 4
    Part A), so playback highlighting never goes dark during an
    inter-sentence pause and the summed overlay duration matches the real
    audio instead of running short by the sum of every pause -- measured
    4.28% short on a real book before this fix, against BookOrbit's own
    ``min(300s, 5%)`` duration-mismatch tolerance.

    ``clips`` must already be in book reading order (spine order, then each
    spine item's own sentence order) *and* already be the sentences that will
    actually be emitted as SMIL ``<par>``s -- i.e. after
    :func:`_markers_for_spine_item`'s no-DOM-location drops, not the raw
    Phase 2 output. Extending against a sentence that never gets its own
    ``<par>`` would silently absorb its pause into the wrong neighbour.

    ``ts_start`` is never touched; only ``ts_end`` grows. Phase 2's
    :func:`~src.services.readalong_segments.build_sentence_clips` already
    guarantees ``ts_start[i+1] >= ts_end[i]`` for consecutive clips, so
    setting ``ts_end[i] = ts_start[i+1]`` can only grow ``ts_end[i]`` (or
    leave it unchanged) -- it can never shrink it, push it past
    ``ts_start[i+1]``, or otherwise violate the existing monotonic /
    non-overlapping guarantee. Extended clips touch exactly at the boundary
    (``ts_end[i] == ts_start[i+1]``); they never cross it.

    The book's *last* clip has no "next" clip to extend to, so it is instead
    extended to ``audio_duration_seconds`` -- the real embedded audio's own
    probed length -- provided that is actually past the clip's own computed
    end. When ``audio_duration_seconds`` is unavailable (the probe failed)
    or is not itself past the last clip's end, the last clip is left as
    Phase 2 computed it and a warning is logged: this never shrinks a clip,
    and never guesses at a duration that isn't backed by a real probe.
    """
    if not clips:
        return clips
    extended = list(clips)
    for i in range(len(extended) - 1):
        extended[i] = replace(extended[i], ts_end=extended[i + 1].ts_start)
    last = extended[-1]
    if audio_duration_seconds is not None and audio_duration_seconds > last.ts_end:
        extended[-1] = replace(last, ts_end=audio_duration_seconds)
    else:
        logger.warning(
            "Read-along: could not extend final clip %s to the real audio "
            "duration (probed=%s, clip end=%.3f); overlay total will run short",
            last.sentence_id, audio_duration_seconds, last.ts_end,
        )
    return extended


def _inject_markers(content: Union[str, bytes], markers: List[Tuple[int, int, str]]) -> bytes:
    """Insert empty ``<span id="...">`` markers into one spine item's XHTML.

    ``markers`` are ``(node_index, node_offset, marker_id)`` triples as
    :func:`_markers_for_spine_item` returns them. Multiple markers can
    legitimately share one ``node_index`` -- a single text node (e.g. a
    ``<p>`` with no inline tags) commonly holds more than one sentence
    boundary -- so they are grouped per node and applied in one pass, in
    ascending ``node_offset`` order, splicing the node's original string into
    text/marker/text/marker/.../text pieces. This never touches any other
    node, so processing order between different ``node_index`` groups does
    not matter.

    Re-parses ``content`` independently of ``ebook_dom_map`` (which discards
    its soup after extracting text) with the identical parser
    (``'html.parser'``) and node filter (``content_string_nodes``), so
    ``node_index`` values line up exactly with what :func:`build_dom_anchor_map`
    computed them against.
    """
    soup = BeautifulSoup(content, "html.parser")
    nodes = content_string_nodes(soup)

    by_node: Dict[int, List[Tuple[int, str]]] = {}
    for node_index, node_offset, marker_id in markers:
        by_node.setdefault(node_index, []).append((node_offset, marker_id))

    for node_index, offsets in by_node.items():
        if node_index < 0 or node_index >= len(nodes):
            logger.error(
                "Marker injection: node_index %d out of range (%d nodes); "
                "skipping markers %s",
                node_index, len(nodes), [m for _, m in offsets],
            )
            continue
        node = nodes[node_index]
        raw = str(node)
        offsets.sort(key=lambda pair: pair[0])

        pieces: List = []
        prev = 0
        for node_offset, marker_id in offsets:
            if node_offset < prev or node_offset > len(raw):
                logger.error(
                    "Marker injection: offset %d out of order/range for node "
                    "%d (prev=%d, len=%d); skipping marker %s",
                    node_offset, node_index, prev, len(raw), marker_id,
                )
                continue
            if node_offset > prev:
                pieces.append(NavigableString(raw[prev:node_offset]))
            marker = soup.new_tag("span")
            marker["id"] = marker_id
            pieces.append(marker)
            prev = node_offset
        if prev < len(raw):
            pieces.append(NavigableString(raw[prev:]))

        if pieces:
            node.replace_with(*pieces)

    return str(soup).encode("utf-8")


def _xml_wellformed(data: bytes) -> bool:
    """Whether ``data`` parses as well-formed XML."""
    try:
        etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))
        return True
    except etree.XMLSyntaxError:
        return False


def _verify_marker_injection(original: bytes, modified: bytes, spine_index: int, href: str) -> None:
    """Confirm marker injection did not alter this spine item's extracted text
    and did not turn well-formed XML into malformed XML.

    An empty marker span contributes nothing to
    ``BeautifulSoup.get_text(separator=' ', strip=True)``, so re-running the
    same extraction ``extract_text_and_map``/Phase 1 use over the modified
    content must reproduce the original text exactly -- this is the concrete
    form of "the injected markup must stay valid XHTML" the plan calls for.
    Raises rather than silently shipping a book whose markers landed in the
    wrong place or corrupted surrounding text.

    The well-formedness check is differential: if the *original* content was
    not well-formed XML to begin with (a real-world EPUB using HTML-only
    markup bs4's lenient parser already tolerates), that is a pre-existing
    condition outside this phase's scope, not a regression -- only a
    previously-well-formed document turning invalid here raises.
    """
    original_text = BeautifulSoup(original, "html.parser").get_text(separator=" ", strip=True)
    modified_text = BeautifulSoup(modified, "html.parser").get_text(separator=" ", strip=True)
    if original_text != modified_text:
        first_diff = next(
            (i for i, (a, b) in enumerate(zip(original_text, modified_text)) if a != b),
            min(len(original_text), len(modified_text)),
        )
        logger.error(
            "Marker injection changed spine item %s (href=%s) text at offset %d",
            spine_index, href, first_diff,
        )
        raise ValueError(
            f"Marker injection altered extracted text for spine_index={spine_index} "
            f"href={href!r} at offset {first_diff}"
        )

    if _xml_wellformed(original) and not _xml_wellformed(modified):
        logger.error(
            "Marker injection produced invalid XML for spine item %s (href=%s)",
            spine_index, href,
        )
        raise ValueError(
            f"Marker injection produced invalid XML for spine_index={spine_index} "
            f"href={href!r}"
        )


def _build_smil(
    chapter_id: str,
    xhtml_href_from_smil: str,
    audio_href_from_smil: str,
    clips: List[SentenceClip],
) -> bytes:
    """Build one spine item's SMIL media-overlay document.

    ``xhtml_href_from_smil``/``audio_href_from_smil`` are paths relative to
    the SMIL document's own location (computed by the caller via
    ``posixpath.relpath``). Every ``<par>`` carries both ``clipBegin`` and
    ``clipEnd`` -- required so BookOrbit's own duration inspector does not
    collapse the whole overlay to a null total (see this module's docstring
    and the plan's Phase 3 exit criteria).
    """
    nsmap = {None: _SMIL_NS, "epub": _OPS_NS}
    smil = etree.Element(f"{{{_SMIL_NS}}}smil", nsmap=nsmap, attrib={"version": "3.0"})
    body = etree.SubElement(smil, f"{{{_SMIL_NS}}}body")
    seq = etree.SubElement(
        body,
        f"{{{_SMIL_NS}}}seq",
        attrib={
            "id": f"{chapter_id}_overlay",
            f"{{{_OPS_NS}}}textref": xhtml_href_from_smil,
            f"{{{_OPS_NS}}}type": "bodymatter chapter",
        },
    )
    for clip in clips:
        ts_start = float(clip.ts_start)
        ts_end = float(clip.ts_end)
        if ts_end < ts_start:
            logger.warning(
                "SMIL par %s: clipEnd %.3f < clipBegin %.3f, clamping",
                clip.sentence_id, ts_end, ts_start,
            )
            ts_end = ts_start
        par = etree.SubElement(seq, f"{{{_SMIL_NS}}}par", attrib={"id": clip.sentence_id})
        etree.SubElement(
            par,
            f"{{{_SMIL_NS}}}text",
            attrib={"src": f"{xhtml_href_from_smil}#{clip.sentence_id}"},
        )
        etree.SubElement(
            par,
            f"{{{_SMIL_NS}}}audio",
            attrib={
                "src": audio_href_from_smil,
                "clipBegin": f"{ts_start:.3f}s",
                "clipEnd": f"{ts_end:.3f}s",
            },
        )
    return etree.tostring(smil, xml_declaration=True, encoding="utf-8", standalone=False)


def _rewrite_opf(
    opf_bytes: bytes,
    opf_dir: str,
    overlays: List[SpineOverlayResult],
    audio_manifest_href: str,
    audio_media_type: str,
    audio_manifest_id: str,
    total_duration: float,
) -> bytes:
    """Add media-overlay manifest items and ``media:duration`` metadata to an OPF.

    Everything else in the OPF is preserved exactly as parsed -- this edits
    the existing tree in place with lxml (which keeps comments, processing
    instructions, attribute order and untouched elements' formatting intact)
    rather than rebuilding the document, per the plan's explicit instruction.

    Raises ``ValueError`` if the OPF has no ``<manifest>``/``<metadata>`` to
    attach overlays to -- the caller treats this as a refusal, not a crash.
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    tree = etree.fromstring(opf_bytes, parser=parser)
    manifest = tree.find(f"{{{_OPF_NS}}}manifest")
    metadata = tree.find(f"{{{_OPF_NS}}}metadata")
    if manifest is None or metadata is None:
        raise ValueError("OPF is missing <manifest> or <metadata>; cannot attach read-along overlays")

    existing_ids = {item.get("id") for item in manifest.findall(f"{{{_OPF_NS}}}item") if item.get("id")}

    href_to_item = {}
    for item in manifest.findall(f"{{{_OPF_NS}}}item"):
        href_attr = item.get("href")
        if not href_attr:
            continue
        archive_href = posixpath.normpath(posixpath.join(opf_dir, href_attr)) if opf_dir else posixpath.normpath(href_attr)
        href_to_item[archive_href] = item

    def _append_with_tail(parent: etree._Element, child: etree._Element) -> None:
        """Append ``child`` and copy a sibling's tail whitespace onto it, so
        the new element does not land squished onto the closing tag's line."""
        if len(parent) > 0:
            child.tail = parent[-1].tail
        parent.append(child)

    for overlay in overlays:
        target = href_to_item.get(overlay.href)
        if target is None:
            logger.error(
                "Read-along OPF rewrite: no manifest <item> found for spine href "
                "'%s'; media-overlay not recorded for this spine item",
                overlay.href,
            )
            continue

        smil_item_id = _unique_manifest_id(existing_ids, f"c{overlay.spine_index}-overlay")
        existing_ids.add(smil_item_id)
        target.set("media-overlay", smil_item_id)

        smil_manifest_href = posixpath.relpath(overlay.smil_href, opf_dir or ".")
        smil_item = etree.Element(
            f"{{{_OPF_NS}}}item",
            attrib={"id": smil_item_id, "href": smil_manifest_href, "media-type": "application/smil+xml"},
        )
        _append_with_tail(manifest, smil_item)

        duration_meta = etree.Element(
            f"{{{_OPF_NS}}}meta",
            attrib={"refines": f"#{smil_item_id}", "property": "media:duration"},
        )
        duration_meta.text = _format_smil_clock(overlay.duration_seconds)
        _append_with_tail(metadata, duration_meta)

    audio_item = etree.Element(
        f"{{{_OPF_NS}}}item",
        attrib={"id": audio_manifest_id, "href": audio_manifest_href, "media-type": audio_media_type},
    )
    _append_with_tail(manifest, audio_item)

    total_meta = etree.Element(f"{{{_OPF_NS}}}meta", attrib={"property": "media:duration"})
    total_meta.text = _format_smil_clock(total_duration)
    _append_with_tail(metadata, total_meta)

    return etree.tostring(tree, xml_declaration=True, encoding="utf-8", standalone=False)


def _package_epub(
    source_epub: Union[str, Path],
    output_path: Union[str, Path],
    modified_files: Dict[str, bytes],
    new_bytes_files: Dict[str, bytes],
    new_disk_files: Dict[str, Path],
) -> None:
    """Repackage an EPUB with modified/added files, everything else untouched.

    Per the EPUB OCF spec: ``mimetype`` is written first, stored uncompressed
    (``ZIP_STORED``), containing exactly ``application/epub+zip``. Every other
    original entry is copied byte-for-byte with its original compression
    method, unless overridden by ``modified_files``. ``new_bytes_files``
    (generated SMIL) are appended deflated; ``new_disk_files`` (the embedded
    audio) are streamed from disk with ``ZipFile.write`` rather than loaded
    into memory, stored uncompressed since audio is already compressed.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(source_epub) as src, zipfile.ZipFile(output_path, "w") as dst:
        dst.writestr(
            zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0)),
            b"application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )

        for name in src.namelist():
            if name == "mimetype":
                continue
            info = src.getinfo(name)
            data = modified_files.get(name, src.read(name))
            new_info = zipfile.ZipInfo(name, date_time=info.date_time)
            new_info.compress_type = info.compress_type
            new_info.external_attr = info.external_attr
            dst.writestr(new_info, data)

        for name, data in new_bytes_files.items():
            dst.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)

        for name, disk_path in new_disk_files.items():
            dst.write(str(disk_path), arcname=name, compress_type=zipfile.ZIP_STORED)


def build_readalong_epub(
    parser: "EbookParser",
    alignment_service: "AlignmentService",
    epub_path: Union[str, Path],
    audio_paths: Union[str, Path, Sequence[Union[str, Path]]],
    abs_id: str,
    output_path: Union[str, Path],
    standalone_audio_output_path: Optional[Union[str, Path]] = None,
) -> Optional[ReadalongBuildResult]:
    """Assemble a read-along EPUB 3 (SMIL media overlays) for one book.

    Combines Phase 1's DOM anchor map with Phase 2's sentence/clip table to:
    inject an empty ``<span id="...">`` marker at each sentence's start
    position in its spine item's XHTML, emit one ``.smil`` per spine item that
    has sentences, add the SMIL/audio manifest items and ``media:duration``
    metadata to the OPF, and repackage as a new EPUB.

    The source EPUB is otherwise untouched: every existing manifest item,
    spine entry, and file is carried through byte-for-byte except the handful
    of spine XHTML documents that receive markers and the OPF itself.

    **Phase 4 Part A:** every emitted clip's end is extended to the next
    emitted clip's start (:func:`_extend_clips_to_contiguous`), across
    spine-item boundaries, so playback never goes dark between sentences and
    the summed overlay duration tracks the real audio instead of running
    short by every inter-sentence pause. The book's last clip is extended to
    the real, probed duration of whatever audio actually gets embedded.

    **Phase 4 Part B:** ``audio_paths`` (one file, or several in book reading
    order for a multi-file audiobook) is transcoded -- and, for more than one
    file, concatenated -- to mono AAC via ``ffmpeg`` at ``READALONG_AUDIO_BITRATE``
    (see :func:`_resolve_audio_bitrate`), and that transcoded file, not the
    original(s), is what gets embedded. See :func:`_transcode_audio_for_embed`
    for why concatenation is safe against the alignment map's absolute
    timestamps.

    Returns ``None`` (refuses, does not raise) rather than emit a broken or
    empty book when: ``audio_paths`` is empty; Phase 2's fitted-EPUB guard
    refuses the stored alignment map (see
    ``readalong_segments.build_sentence_clips``); no spine item's sentences
    could be anchored in the DOM at all; the audio transcode fails; or the
    EPUB's OPF has no ``<manifest>``/``<metadata>`` to attach overlays to.

    :param parser: source of the book's spine text/DOM (shared with Phases 1/2).
    :param alignment_service: source of the book's stored alignment map.
    :param epub_path: path to the source EPUB.
    :param audio_paths: path to the source audio, or an ordered list of parts
        for a multi-file audiobook -- must be in the same order the book was
        force-aligned against.
    :param abs_id: the book's ABS id (the alignment map's primary key).
    :param output_path: where to write the generated EPUB.
    :param standalone_audio_output_path: when given, the transcoded embed
        audio is also copied here -- BookOrbit's file scanner only recognizes
        a media-overlay EPUB's audio when a standalone copy sits beside it in
        the same library entry (Phase 3's live finding); this lets a caller
        (or Phase 5's delivery step) get that sibling file from the exact
        same transcode this build already paid for, instead of running
        ffmpeg a second time.
    :return: the build result, or ``None`` if refused.
    """
    epub_path = Path(epub_path)
    output_path = Path(output_path)
    source_audio_paths = _normalize_audio_paths(audio_paths)
    if not source_audio_paths:
        logger.warning(
            "🚫 Refusing to build read-along EPUB for '%s': no audio paths given",
            abs_id,
        )
        return None

    clip_result = build_sentence_clips(parser, epub_path, alignment_service, abs_id)
    if clip_result is None or not clip_result.clips:
        logger.warning(
            "🚫 Refusing to build read-along EPUB for '%s': no sentence clips available",
            abs_id,
        )
        return None

    dom_map = build_dom_anchor_map(parser, epub_path)
    _combined_text, spine_map = parser.extract_text_and_map(epub_path)
    content_by_spine = {entry["spine_index"]: entry["content"] for entry in spine_map}
    href_by_spine = {entry["spine_index"]: entry["href"] for entry in spine_map}

    clips_by_spine: Dict[int, List[SentenceClip]] = {}
    for clip in clip_result.clips:
        clips_by_spine.setdefault(clip.spine_index, []).append(clip)

    with zipfile.ZipFile(epub_path) as zf:
        zip_names = set(zf.namelist())
        opf_path = _find_opf_path(zf)
        if not opf_path or opf_path not in zip_names:
            logger.error(
                "Read-along build: could not locate OPF in '%s' (abs_id=%s)",
                epub_path, abs_id,
            )
            return None
        opf_bytes = zf.read(opf_path)

    opf_dir = posixpath.dirname(opf_path)
    readalong_dir = _unique_archive_dir(zip_names, opf_dir, _READALONG_DIR_BASE)

    # Pass 1: DOM-locate every clip's marker per spine item. This determines
    # the actual, final sequence of sentences that will become SMIL <par>s (a
    # sentence Phase 2 timestamped but this phase can't place in the DOM is
    # dropped here) -- cheap, and worth resolving before paying for a
    # (potentially multi-minute, for a long audiobook) transcode below.
    located_by_spine: Dict[int, List[SentenceClip]] = {}
    markers_by_spine: Dict[int, List[Tuple[int, int, str]]] = {}
    dropped_no_location = 0
    for spine_index, clips in sorted(clips_by_spine.items()):
        href = href_by_spine.get(spine_index)
        content = content_by_spine.get(spine_index)
        if href is None or content is None:
            logger.error(
                "Read-along build: spine index %d has clips but no spine_map "
                "entry (abs_id=%s); skipping",
                spine_index, abs_id,
            )
            continue
        if href not in zip_names:
            logger.error(
                "Read-along build: spine href '%s' is not an archive entry "
                "(abs_id=%s); skipping spine item %d",
                href, abs_id, spine_index,
            )
            continue

        markers, item_dropped = _markers_for_spine_item(dom_map, clips, spine_index)
        dropped_no_location += item_dropped
        located_ids = {marker_id for _, _, marker_id in markers}
        located_clips = [c for c in clips if c.sentence_id in located_ids]
        if not located_clips:
            continue
        located_by_spine[spine_index] = located_clips
        markers_by_spine[spine_index] = markers

    if not located_by_spine:
        logger.warning(
            "🚫 Refusing to build read-along EPUB for '%s': no spine item could "
            "be anchored in the DOM",
            abs_id,
        )
        return None

    audio_bitrate = _resolve_audio_bitrate()
    with tempfile.TemporaryDirectory(prefix="readalong-audio-") as tmp_dir:
        transcoded_audio_path = Path(tmp_dir) / "audio.m4a"
        if not _transcode_audio_for_embed(source_audio_paths, audio_bitrate, transcoded_audio_path):
            logger.warning(
                "🚫 Refusing to build read-along EPUB for '%s': audio transcode failed",
                abs_id,
            )
            return None
        audio_duration = _probe_duration_seconds(transcoded_audio_path)

        if standalone_audio_output_path is not None:
            standalone_audio_output_path = Path(standalone_audio_output_path)
            standalone_audio_output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(transcoded_audio_path, standalone_audio_output_path)

        audio_archive_path = posixpath.join(readalong_dir, f"audio{transcoded_audio_path.suffix.lower()}")

        # Pass 2 (Phase 4 Part A): extend every already-located clip's end to
        # the next one's start, in book reading order across spine-item
        # boundaries, using the real probed audio duration for the very last
        # one -- see _extend_clips_to_contiguous. Then emit SMIL/inject
        # markers per spine item from the extended clips.
        flat_clips = [c for clips in located_by_spine.values() for c in clips]
        flat_clips = _extend_clips_to_contiguous(flat_clips, audio_duration)
        extended_by_spine: Dict[int, List[SentenceClip]] = {}
        cursor = 0
        for spine_index, clips in located_by_spine.items():
            extended_by_spine[spine_index] = flat_clips[cursor:cursor + len(clips)]
            cursor += len(clips)

        modified_files: Dict[str, bytes] = {}
        new_bytes_files: Dict[str, bytes] = {}
        overlays: List[SpineOverlayResult] = []

        for spine_index, located_clips in extended_by_spine.items():
            href = href_by_spine[spine_index]
            content = content_by_spine[spine_index]
            markers = markers_by_spine[spine_index]

            modified_content = _inject_markers(content, markers)
            _verify_marker_injection(content, modified_content, spine_index, href)
            modified_files[href] = modified_content

            chapter_id = f"c{spine_index}"
            smil_archive_path = posixpath.join(readalong_dir, f"{spine_index}.smil")
            xhtml_href_from_smil = posixpath.relpath(href, readalong_dir)
            audio_href_from_smil = posixpath.relpath(audio_archive_path, readalong_dir)
            smil_bytes = _build_smil(chapter_id, xhtml_href_from_smil, audio_href_from_smil, located_clips)
            new_bytes_files[smil_archive_path] = smil_bytes

            first_begin = min(c.ts_start for c in located_clips)
            last_end = max(c.ts_end for c in located_clips)
            overlays.append(SpineOverlayResult(
                spine_index=spine_index,
                href=href,
                smil_href=smil_archive_path,
                par_count=len(located_clips),
                duration_seconds=max(0.0, last_end - first_begin),
            ))

        audio_manifest_href = posixpath.relpath(audio_archive_path, opf_dir or ".")
        audio_media_type = _audio_media_type(transcoded_audio_path)
        total_duration = sum(o.duration_seconds for o in overlays)

        existing_manifest_ids = _manifest_item_ids(opf_bytes)
        audio_manifest_id = _unique_manifest_id(existing_manifest_ids, "readalong-audio")

        try:
            modified_opf = _rewrite_opf(
                opf_bytes, opf_dir, overlays, audio_manifest_href, audio_media_type,
                audio_manifest_id, total_duration,
            )
        except ValueError as e:
            logger.warning(
                "🚫 Refusing to build read-along EPUB for '%s': %s", abs_id, e,
            )
            return None
        modified_files[opf_path] = modified_opf

        new_disk_files = {audio_archive_path: transcoded_audio_path}
        _package_epub(epub_path, output_path, modified_files, new_bytes_files, new_disk_files)

    total_sentences = sum(len(clips) for clips in clips_by_spine.values())
    logger.info(
        "📖 Built read-along EPUB for '%s': %d spine overlays, %d sentences "
        "(%d dropped: no timestamp, %d dropped: no DOM location), "
        "%.1fs total overlay duration (bitrate=%s) -> '%s'",
        abs_id, len(overlays), total_sentences, clip_result.dropped_no_timestamp,
        dropped_no_location, total_duration, audio_bitrate, output_path,
    )
    return ReadalongBuildResult(
        abs_id=abs_id,
        output_path=str(output_path),
        spine_overlays=overlays,
        total_sentences=total_sentences,
        dropped_no_timestamp=clip_result.dropped_no_timestamp,
        dropped_no_location=dropped_no_location,
        total_duration_seconds=total_duration,
        audio_href=audio_manifest_href,
        audio_bitrate=audio_bitrate,
    )


def _manifest_item_ids(opf_bytes: bytes) -> set:
    """The set of existing manifest item ids, read without mutating the OPF.

    Used before :func:`_rewrite_opf` to pick a collision-free id for the
    embedded audio manifest item (the per-spine SMIL ids are allocated inside
    ``_rewrite_opf`` itself, where the live, growing id set is available).
    """
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        tree = etree.fromstring(opf_bytes, parser=parser)
    except etree.XMLSyntaxError as e:
        logger.warning("Could not parse OPF to collect manifest ids: %s", e, exc_info=True)
        return set()
    manifest = tree.find(f"{{{_OPF_NS}}}manifest")
    if manifest is None:
        return set()
    return {item.get("id") for item in manifest.findall(f"{{{_OPF_NS}}}item") if item.get("id")}
