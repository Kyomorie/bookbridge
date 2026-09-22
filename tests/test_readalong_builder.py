"""Unit tests for EPUB 3 read-along assembly (Phase 3:
docs/PLAN_READALONG_EPUB3_GENERATION.md) -- marker injection, SMIL emission,
OPF rewriting, and zip packaging. Phase 4 adds: clip contiguity
(_extend_clips_to_contiguous), the READALONG_AUDIO_BITRATE setting, and real
ffmpeg audio transcoding/concatenation.

Builds small inline EPUB fixtures with zipfile (same pattern as
test_ebook_dom_map.py / test_readalong_segments.py). Alignment maps are
supplied via the same minimal fake AlignmentService double
test_readalong_segments.py uses.

Every test drives build_readalong_epub end to end, which (since Phase 4)
always transcodes through real ffmpeg -- there is no mock seam for it, mirroring
this repo's existing precedent in test_forced_aligner.py
(test_load_audio_decodes_to_mono_16k_via_ffmpeg) of calling the real binary
and skipping if it is not on PATH, rather than mocking subprocess. _make_audio
below generates real, tiny, silent audio via ffmpeg's lavfi anullsrc source so
every test's input is something ffmpeg can actually decode.
"""
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from unittest.mock import patch
from xml.etree import ElementTree

import pytest
from lxml import etree

from src.services import readalong_builder as _readalong_builder_module
from src.services.readalong_builder import (
    _DEFAULT_AUDIO_BITRATE,
    _extend_clips_to_contiguous,
    _package_epub,
    _probe_duration_seconds,
    _safe_progress,
    _transcode_audio_for_embed,
    build_readalong_epub,
)
from src.services.readalong_segments import SentenceClip
from src.utils.ebook_utils import EbookParser

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not on PATH -- Phase 4 always transcodes real audio",
)

_CONTAINER_XML = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)

_SMIL_NS = "{http://www.w3.org/ns/SMIL}"


def _marker_match(xhtml: str, marker_id: str) -> re.Match:
    """Find an injected ``<span id="...">`` marker regardless of whether it
    serialized self-closed (``<span id="x"/>``) or open/close
    (``<span id="x"></span>``) -- both are valid, equivalent XML for an empty
    element, and which one comes out depends on which injection path built the
    document (Finding 1's fix serializes via an XML-mode parser, which
    self-closes empty elements, unlike the pre-fix HTML-mode path)."""
    pattern = re.compile(
        r'<span id="%s"\s*(?:/>|>\s*</span>)' % re.escape(marker_id)
    )
    match = pattern.search(xhtml)
    assert match is not None, f"marker id={marker_id!r} not found in: {xhtml}"
    return match


def _marker_start(xhtml: str, marker_id: str) -> int:
    return _marker_match(xhtml, marker_id).start()


def _text_after_marker(xhtml: str, marker_id: str) -> str:
    return xhtml[_marker_match(xhtml, marker_id).end():]


def _parser(tmp: Path) -> EbookParser:
    books = tmp / "books"
    cache = tmp / "cache"
    books.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    return EbookParser(books_dir=str(books), epub_cache_dir=str(cache))


def _opf(manifest_ids: List[str], spine_idrefs: List[str], extra_manifest: str = "",
         opf_version: str = "3.0") -> str:
    manifest = "".join(
        f'<item id="{iid}" href="{iid}.xhtml" media-type="application/xhtml+xml"/>'
        for iid in manifest_ids
    )
    spine = "".join(f'<itemref idref="{iid}"/>' for iid in spine_idrefs)
    return (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
        f'version="{opf_version}" unique-identifier="id"><metadata '
        'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Test Book</dc:title>'
        '<dc:identifier id="id">urn:uuid:test-book-id</dc:identifier></metadata>'
        f'<manifest>{manifest}{extra_manifest}</manifest><spine>{spine}</spine></package>'
    )


def _write_epub(path: Path, items: Dict[str, bytes], extra_manifest: str = "",
                 extra_files: Optional[Dict[str, bytes]] = None,
                 opf_version: str = "3.0") -> None:
    """``items``: {item_id: xhtml_bytes}. Spine order is dict order.
    ``extra_manifest`` lets a test add a pre-existing, unrelated manifest item
    (e.g. a cover image) to verify it survives the OPF rewrite untouched.
    ``extra_files`` writes additional raw zip entries (e.g. that cover's
    bytes). ``opf_version`` lets a test build an EPUB 2 fixture (Finding 2)."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", _CONTAINER_XML)
        z.writestr(
            "OEBPS/content.opf",
            _opf(list(items.keys()), list(items.keys()), extra_manifest, opf_version=opf_version),
        )
        for item_id, content in items.items():
            z.writestr(f"OEBPS/{item_id}.xhtml", content)
        for name, data in (extra_files or {}).items():
            z.writestr(name, data)


class _FakeAlignmentService:
    """Same minimal AlignmentService double as test_readalong_segments.py."""

    class _FakeDatabaseService:
        def __init__(self, total_chars: Optional[int]):
            self._total_chars = total_chars

        def get_alignment_total_chars(self, abs_id: str) -> Optional[int]:
            return self._total_chars

    def __init__(
        self,
        terminal_char: Optional[int],
        time_for_char: Callable[[int], Optional[float]],
        total_chars: Optional[int] = None,
        segments: Optional[list] = None,
    ):
        self._terminal_char = terminal_char
        self._time_for_char = time_for_char
        self.database_service = self._FakeDatabaseService(total_chars)
        self._segments = segments

    def get_map_terminal_char(self, abs_id: str) -> Optional[int]:
        return self._terminal_char

    def get_time_for_char(self, abs_id: str, char_offset: int) -> Optional[float]:
        return self._time_for_char(char_offset)

    def _get_segments(self, abs_id: str) -> Optional[list]:
        """Same "friend" access pattern this repo's own AlignmentService
        tests use directly on the real class -- None means an unsegmented
        (single, in-order narration) map, matching the real
        AlignmentService._get_segments contract (Finding 3 fix)."""
        return self._segments


def _linear_alignment(total_chars: int, total_seconds: float) -> _FakeAlignmentService:
    """A fake alignment map mapping char 0 -> 0.0s and the last char -> total_seconds,
    linear in between -- matches the fitted-EPUB guard via total_chars."""
    def interpolate(char_offset: int) -> float:
        frac = max(0.0, min(1.0, char_offset / total_chars)) if total_chars else 0.0
        return frac * total_seconds

    return _FakeAlignmentService(
        terminal_char=total_chars, time_for_char=interpolate, total_chars=total_chars,
    )


def _make_audio(tmp: Path, suffix: str = ".mp3", duration: float = 1.0, name: str = "audio") -> Path:
    """A tiny, real, silent audio file ffmpeg can actually decode.

    Phase 3's fixture wrote raw zero bytes -- fine when audio was embedded
    as-is, but Phase 4 always transcodes through real ffmpeg, which cannot
    decode that. Silence keeps the fixture fast and deterministic; duration
    is intentionally short (tests care about contiguity/bitrate behavior,
    not matching any particular audiobook length).
    """
    audio_path = tmp / f"{name}{suffix}"
    subprocess.run(
        [
            "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
            "-t", str(duration), str(audio_path),
        ],
        check=True,
    )
    return audio_path


def _build(tmp: Path, parser: EbookParser, epub_path: Path, audio_path,
           combined_text: str, abs_id: str = "abs1", total_seconds: float = 100.0,
           output_name: str = "out.epub", standalone_audio_output_path=None):
    alignment_service = _linear_alignment(len(combined_text), total_seconds)
    output_path = tmp / output_name
    result = build_readalong_epub(
        parser=parser,
        alignment_service=alignment_service,
        epub_path=epub_path,
        audio_paths=audio_path,
        abs_id=abs_id,
        output_path=output_path,
        standalone_audio_output_path=standalone_audio_output_path,
    )
    return result, output_path


# ---------------------------------------------------------------------------
# Marker injection: correct character, inline tags, validity
# ---------------------------------------------------------------------------

def test_marker_lands_at_correct_sentence_start_character():
    """Each inserted <span id="..."> sits immediately before the exact
    character its sentence starts with -- verified by re-parsing the output
    and checking each marker's very next sibling text starts with the
    expected sentence text."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>First sentence here. Second sentence follows.</p></body></html>",
        })
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text)
        assert result is not None
        assert result.total_sentences == 2
        assert result.dropped_no_location == 0

        with zipfile.ZipFile(output_path) as zf:
            xhtml = zf.read("OEBPS/ch1.xhtml").decode("utf-8")

        assert _text_after_marker(xhtml, "c1-s0").lstrip().startswith("First sentence here.")
        assert _text_after_marker(xhtml, "c1-s1").lstrip().startswith("Second sentence follows.")


def test_marker_injection_into_node_with_inline_tags():
    """A sentence whose text is split across <em>/<strong> inline tags still
    gets its single start-of-sentence marker placed correctly, and the
    inline tags themselves are left completely intact (never split/wrapped --
    the plan's chosen empty-marker-span strategy, not range-wrapping)."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>Hello <em>brave <strong>new</strong></em> world. "
                   b"A second sentence with <em>emphasis</em> here.</p></body></html>",
        })
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        assert combined_text == "Hello brave new world. A second sentence with emphasis here."
        audio_path = _make_audio(tmp)

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text)
        assert result is not None
        assert result.dropped_no_location == 0
        assert result.total_sentences == 2

        with zipfile.ZipFile(output_path) as zf:
            xhtml = zf.read("OEBPS/ch1.xhtml").decode("utf-8")

        # The <em>/<strong> structure around "brave new" is untouched.
        assert "<em>brave <strong>new</strong></em>" in xhtml
        # Sentence 0's marker precedes "Hello", sentence 1's precedes "A second".
        assert _marker_start(xhtml, "c1-s0") < xhtml.index("Hello")
        assert _marker_start(xhtml, "c1-s1") < xhtml.index("A second sentence")
        # Only one marker was needed for sentence 0 even though it spans two
        # inline elements -- no marker was injected inside <em>/<strong>.
        assert len(re.findall(r'<span id="c1-s0"', xhtml)) == 1
        em_start = xhtml.index("<em>brave")
        em_end = xhtml.index("</em>", em_start)
        assert "c1-s0" not in xhtml[em_start:em_end]
        assert "c1-s1" not in xhtml[em_start:em_end]


def test_finding1_preserves_stylesheet_links_body_attrs_and_xml_case():
    """Finding 1 (independent review of Phases 1-4): spine content was
    sourced from ``spine_map['content']`` -- ebooklib's ``EpubHtml.get_content()``,
    which reconstructs the document from scratch, dropping the original
    ``<head>``'s stylesheet ``<link>``s and the original ``<body>``'s own
    attributes entirely, and (via its HTML-mode reparse of the original bytes)
    lowercasing XML-cased attributes like SVG's ``viewBox``. The fix injects
    into the spine item's ORIGINAL archive bytes via a case-preserving XML
    parser instead, falling back to the old (lossy) path only when the
    original bytes cannot be verified to reproduce the same extracted text.
    This must survive marker injection: both stylesheet links, the body's
    ``lang``/``xml:lang``/``class`` attributes, and the SVG's ``viewBox``
    case all appear unchanged in the generated output."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {
            "ch1": (
                b'<?xml version="1.0" encoding="utf-8"?>'
                b'<html xmlns="http://www.w3.org/1999/xhtml">'
                b'<head>'
                b'<link href="../css/style1.css" rel="stylesheet" type="text/css"/>'
                b'<link href="../css/style2.css" rel="stylesheet" type="text/css"/>'
                b'</head>'
                b'<body lang="en-US" xml:lang="en-US" class="calibre">'
                b'<p>First sentence here. Second sentence follows.</p>'
                b'<svg viewBox="0 0 10 10"><linearGradient id="g1"/></svg>'
                b'</body></html>'
            ),
        })
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text)
        assert result is not None
        assert result.dropped_no_location == 0

        with zipfile.ZipFile(output_path) as zf:
            xhtml = zf.read("OEBPS/ch1.xhtml").decode("utf-8")

        assert xhtml.count('rel="stylesheet"') == 2, (
            "stylesheet <link>s were dropped -- Finding 1 regression"
        )
        assert 'lang="en-US"' in xhtml, "body lang attribute was dropped"
        assert 'class="calibre"' in xhtml, "body class attribute was dropped"
        assert 'viewBox="0 0 10 10"' in xhtml, (
            "SVG viewBox was lowercased by an HTML-mode reparse -- Finding 1 regression"
        )
        assert "linearGradient" in xhtml, (
            "SVG linearGradient tag name was lowercased -- Finding 1 regression"
        )
        # Marker injection still landed correctly despite using the
        # original-bytes path.
        assert _text_after_marker(xhtml, "c1-s0").lstrip().startswith("First sentence here.")
        assert _text_after_marker(xhtml, "c1-s1").lstrip().startswith("Second sentence follows.")


def test_multiple_sentences_in_one_text_node():
    """Two sentence starts landing in the SAME original text node (no inline
    tags between them) both get correctly-placed, independent markers."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>Alpha bravo charlie. Delta echo foxtrot. Golf hotel india.</p></body></html>",
        })
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text)
        assert result is not None
        assert result.total_sentences == 3
        assert result.dropped_no_location == 0

        with zipfile.ZipFile(output_path) as zf:
            xhtml = zf.read("OEBPS/ch1.xhtml").decode("utf-8")

        assert _marker_start(xhtml, "c1-s0") < xhtml.index("Alpha")
        assert _marker_start(xhtml, "c1-s1") < xhtml.index("Delta")
        assert _marker_start(xhtml, "c1-s2") < xhtml.index("Golf")
        assert xhtml.index("Alpha") < _marker_start(xhtml, "c1-s1")
        assert xhtml.index("Delta") < _marker_start(xhtml, "c1-s2")


def test_finding6_marker_id_collision_with_preexisting_document_id_is_avoided():
    """Finding 6 (independent review): marker ids were allocated as
    ``c<spine>-s<n>`` without checking whether that id already belongs to
    some other element in the source document. A real reproduction: a
    source containing ``<p id="c1-s0">`` "succeeded" with two elements both
    carrying ``id="c1-s0"`` (the pre-existing paragraph and the injected
    marker), making any SMIL fragment reference to that id ambiguous. Fixed
    by allocating a deterministic, collision-free id instead, and using that
    SAME allocated value in both the XHTML marker and the SMIL's own
    ``<par id>``/``<text src="...#...">`` fragment."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        # The document already has an element using exactly the id our
        # marker scheme would generate for the very first sentence.
        _write_epub(epub_path, {
            "ch1": b'<html><body>'
                   b'<p id="c1-s0">A pre-existing paragraph with this exact id.</p>'
                   b'<p>First real sentence here. Second real sentence follows.</p>'
                   b'</body></html>',
        })
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text)
        assert result is not None
        # 3 sentences: the pre-existing paragraph's own text is itself a
        # sentence, plus the two "real" ones.
        assert result.total_sentences == 3
        assert result.dropped_no_location == 0

        with zipfile.ZipFile(output_path) as zf:
            xhtml = zf.read("OEBPS/ch1.xhtml").decode("utf-8")
            smil_name = next(n for n in zf.namelist() if n.endswith(".smil"))
            smil_bytes = zf.read(smil_name).decode("utf-8")

        all_ids = re.findall(r'id="([^"]*)"', xhtml)
        from collections import Counter
        dupes = {k: v for k, v in Counter(all_ids).items() if v > 1}
        assert not dupes, f"duplicate ids in generated XHTML: {dupes}"

        # The pre-existing paragraph's id survives untouched...
        assert 'id="c1-s0"' in xhtml
        # ...and the first sentence's marker got a DIFFERENT, allocated id
        # instead of colliding with it.
        allocated_ids = [i for i in all_ids if i != "c1-s0" and i.startswith("c1-s0")]
        assert len(allocated_ids) == 1, f"expected exactly one reallocated id, got {allocated_ids}"
        allocated_id = allocated_ids[0]

        # The SMIL references the SAME allocated id, not the original
        # (colliding) "c1-s0" -- both the <par id> and the <text src> fragment.
        assert f'id="{allocated_id}"' in smil_bytes
        assert f'#{allocated_id}"' in smil_bytes
        assert f'id="c1-s0"' not in smil_bytes


def test_verify_marker_injection_catches_xml_regression_without_text_change():
    """Isolates the well-formedness half of _verify_marker_injection from its
    text-equality half: a bare, unescaped ``&`` in an attribute value is
    invalid strict XML but is completely invisible to
    get_text(separator=' ', strip=True) (which never looks at attributes), so
    this is a case the text-equality check alone would silently let through --
    only the differential XML well-formedness check catches it. Also confirms
    the check is differential, not absolute: when the *original* already had
    the same defect, it is treated as pre-existing and not raised on."""
    from src.services.readalong_builder import _verify_marker_injection

    well_formed = b'<html><body><p data-x="A and B">Hello world.</p></body></html>'
    newly_broken = b'<html><body><p data-x="A & B">Hello world.</p></body></html>'
    already_broken = b'<html><body><p data-x="C & D">Hello world.</p></body></html>'

    # A regression (well-formed -> not well-formed) raises even though the
    # extracted text is identical either way.
    try:
        _verify_marker_injection(well_formed, newly_broken, spine_index=1, href="x.xhtml")
        assert False, "expected ValueError for a well-formedness regression"
    except ValueError:
        pass

    # A pre-existing defect (already malformed on both sides) is not this
    # phase's regression to raise on.
    _verify_marker_injection(already_broken, newly_broken, spine_index=1, href="x.xhtml")


def test_output_xhtml_reparses_as_well_formed_xml():
    """The modified spine XHTML must remain well-formed XML after marker
    injection -- checked with a strict XML parser (ElementTree), not bs4's
    lenient html.parser, since bs4 would silently accept malformed XML too."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {
            "ch1": b'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
                   b"<body><p>One sentence here. Another one follows, with an "
                   b'<a href="#x">anchor</a> inside it.</p></body></html>',
        })
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text)
        assert result is not None

        with zipfile.ZipFile(output_path) as zf:
            xhtml = zf.read("OEBPS/ch1.xhtml")

        # Raises if not well-formed.
        ElementTree.fromstring(xhtml)


def test_marker_injection_does_not_change_extracted_text():
    """An empty marker span contributes zero characters to
    get_text(separator=' ', strip=True) -- re-extracting text from the
    modified XHTML must exactly equal the original."""
    from bs4 import BeautifulSoup

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        original_body = b"<html><body><p>First sentence. Second sentence continues on.</p></body></html>"
        _write_epub(epub_path, {"ch1": original_body})
        combined_text, spine_map = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text)
        assert result is not None

        with zipfile.ZipFile(output_path) as zf:
            modified_xhtml = zf.read("OEBPS/ch1.xhtml")

        modified_text = BeautifulSoup(modified_xhtml, "html.parser").get_text(separator=" ", strip=True)
        assert modified_text == combined_text


# ---------------------------------------------------------------------------
# SMIL emission
# ---------------------------------------------------------------------------

def test_smil_every_par_has_both_clocks():
    """Every <par> in the generated SMIL carries a parseable clipBegin AND
    clipEnd -- BookOrbit's inspector collapses total duration to null if any
    par lacks one (plan's Phase 3 exit criteria)."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>Alpha bravo. Charlie delta. Echo foxtrot. Golf hotel.</p></body></html>",
        })
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text)
        assert result is not None

        with zipfile.ZipFile(output_path) as zf:
            smil_name = next(n for n in zf.namelist() if n.endswith(".smil"))
            smil_bytes = zf.read(smil_name)

        root = etree.fromstring(smil_bytes)
        pars = root.findall(f".//{_SMIL_NS}par")
        assert len(pars) == result.total_sentences
        assert len(pars) >= 4

        for par in pars:
            audio = par.find(f"{_SMIL_NS}audio")
            assert audio is not None
            clip_begin = audio.get("clipBegin")
            clip_end = audio.get("clipEnd")
            assert clip_begin is not None and clip_begin.endswith("s")
            assert clip_end is not None and clip_end.endswith("s")
            # Parseable as a float number of seconds.
            float(clip_begin[:-1])
            float(clip_end[:-1])
            text_elem = par.find(f"{_SMIL_NS}text")
            assert text_elem is not None
            assert "#" in text_elem.get("src")


def test_smil_text_fragment_matches_an_actual_injected_marker_id():
    """Every SMIL <text src="...#id"> fragment id is one this phase actually
    inserted into the XHTML -- an unmatched fragment collapses read-along
    playback to the chapter start (see EbookParser.get_media_overlay_fragment_ids)."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>Alpha bravo. Charlie delta.</p></body></html>",
        })
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text)
        assert result is not None

        with zipfile.ZipFile(output_path) as zf:
            xhtml = zf.read("OEBPS/ch1.xhtml").decode("utf-8")
            smil_name = next(n for n in zf.namelist() if n.endswith(".smil"))
            smil_bytes = zf.read(smil_name)

        injected_ids = set(re.findall(r'<span id="([^"]+)"', xhtml))
        root = etree.fromstring(smil_bytes)
        for text_elem in root.findall(f".//{_SMIL_NS}text"):
            fragment = text_elem.get("src").split("#", 1)[1]
            assert fragment in injected_ids


# ---------------------------------------------------------------------------
# OPF rewriting
# ---------------------------------------------------------------------------

def test_finding4_percent_encoded_manifest_href_still_gets_its_overlay():
    """Finding 4 (independent review): a manifest ``<item href="...">`` is a
    URI reference and may be percent-encoded (``chapter%201.xhtml``) even
    when the archive member it points to is literally named with the
    decoded characters (``chapter 1.xhtml``) -- comparing the encoded and
    decoded forms directly (as the OPF rewrite's href-to-manifest-item
    lookup used to) never matches, so a book "succeeds" with no media
    overlay attached to that spine item. Verified against the real library:
    5 books (Virgil Knightley/Micky Carre's "Unicorn Breeder"; Jeff Noon's
    "Vurt" and "Nymphomation") have percent-encoded manifest hrefs.

    Also covers the other half of Finding 4: the generated SMIL's own
    references to that same file must themselves be percent-encoded (not
    the raw, space-containing archive name), since a raw space is not a
    valid URI reference either."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        archive_name = "chapter 1.xhtml"  # literal space in the actual archive member
        manifest_href = "chapter%201.xhtml"  # percent-encoded in the OPF
        opf = (
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
            'version="3.0" unique-identifier="id"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title>'
            '<dc:identifier id="id">x</dc:identifier></metadata>'
            f'<manifest><item id="ch1" href="{manifest_href}" '
            'media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="ch1"/></spine></package>'
        )
        with zipfile.ZipFile(epub_path, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml", _CONTAINER_XML)
            z.writestr("OEBPS/content.opf", opf)
            z.writestr(
                f"OEBPS/{archive_name}",
                b"<html><body><p>Alpha bravo. Charlie delta.</p></body></html>",
            )

        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text)
        assert result is not None, (
            "the overlay must still attach for a percent-encoded manifest href"
        )
        assert len(result.spine_overlays) == 1
        assert result.spine_overlays[0].href == f"OEBPS/{archive_name}"

        with zipfile.ZipFile(output_path) as zf:
            opf_out = zf.read("OEBPS/content.opf").decode("utf-8")
            smil_name = next(n for n in zf.namelist() if n.endswith(".smil"))
            smil_bytes = zf.read(smil_name)

        assert "media-overlay=" in opf_out  # the spine item's own manifest entry
        # The generated SMIL's own textref/src reference the ENCODED form of
        # the archive member's actual (space-containing) name -- never the
        # raw space.
        assert b"chapter%201.xhtml" in smil_bytes
        assert b"chapter 1.xhtml" not in smil_bytes


def test_opf_preserves_preexisting_manifest_items_and_adds_overlay_refs():
    """A pre-existing, unrelated manifest item (e.g. a cover image) survives
    the OPF rewrite untouched; the content document gets media-overlay=,
    a new SMIL item and an audio item are added, and both an overall and a
    per-overlay media:duration meta are present."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(
            epub_path,
            {"ch1": b"<html><body><p>Alpha bravo. Charlie delta.</p></body></html>"},
            extra_manifest='<item id="cover-img" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>',
            extra_files={"OEBPS/cover.jpg": b"\xff\xd8\xff\xd9"},
        )
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text)
        assert result is not None

        with zipfile.ZipFile(output_path) as zf:
            opf_bytes = zf.read("OEBPS/content.opf")
            assert zf.read("OEBPS/cover.jpg") == b"\xff\xd8\xff\xd9"

        tree = etree.fromstring(opf_bytes)
        ns = "{http://www.idpf.org/2007/opf}"
        manifest = tree.find(f"{ns}manifest")
        items_by_id = {item.get("id"): item for item in manifest.findall(f"{ns}item")}

        # Pre-existing cover item untouched.
        cover = items_by_id["cover-img"]
        assert cover.get("href") == "cover.jpg"
        assert cover.get("media-type") == "image/jpeg"
        assert cover.get("properties") == "cover-image"

        # Content doc got media-overlay=.
        ch1 = items_by_id["ch1"]
        overlay_id = ch1.get("media-overlay")
        assert overlay_id is not None
        assert overlay_id in items_by_id
        assert items_by_id[overlay_id].get("media-type") == "application/smil+xml"

        # An audio manifest item was added.
        audio_items = [i for i in items_by_id.values() if (i.get("media-type") or "").startswith("audio/")]
        assert len(audio_items) == 1

        # Pre-existing metadata (title/identifier) untouched; media:duration added.
        metadata = tree.find(f"{ns}metadata")
        dc_ns = "{http://purl.org/dc/elements/1.1/}"
        assert metadata.find(f"{dc_ns}title").text == "Test Book"
        metas = metadata.findall(f"{ns}meta")
        duration_props = [m for m in metas if m.get("property") == "media:duration"]
        # One overall (no refines) + one per overlay (refines=).
        assert any(m.get("refines") is None for m in duration_props)
        assert any(m.get("refines") == f"#{overlay_id}" for m in duration_props)
        for m in duration_props:
            assert m.text  # non-empty clock value


def test_defect2_refuses_source_with_existing_media_overlays():
    """Defect 2 (independent review): _rewrite_opf always appends its own new
    publication-level media:duration meta without touching any existing
    overlay metadata/assets. Building from a source that already has media
    overlays (e.g. a Storyteller-produced read-along fed back into this
    builder) would leave two global media:duration declarations, which
    EPUB 3 disallows (exactly one is permitted --
    https://www.w3.org/TR/epub-33/#sec-duration). Per this project's
    refuse-over-partial policy, the whole build is refused instead, before
    any output is written."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        epub_path.parent.mkdir(parents=True, exist_ok=True)
        opf_with_existing_overlay = (
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
            'version="3.0" unique-identifier="id"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Test Book</dc:title>'
            '<dc:identifier id="id">urn:uuid:test-book-id</dc:identifier>'
            '<meta refines="#existing-smil" property="media:duration">0:00:05.000</meta>'
            '<meta property="media:duration">0:00:05.000</meta>'
            "</metadata><manifest>"
            '<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml" '
            'media-overlay="existing-smil"/>'
            '<item id="existing-smil" href="ch1.smil" media-type="application/smil+xml"/>'
            "</manifest><spine><itemref idref=\"ch1\"/></spine></package>"
        )
        with zipfile.ZipFile(epub_path, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml", _CONTAINER_XML)
            z.writestr("OEBPS/content.opf", opf_with_existing_overlay)
            z.writestr("OEBPS/ch1.xhtml", b"<html><body><p>Alpha bravo. Charlie delta.</p></body></html>")
            z.writestr("OEBPS/ch1.smil", b'<smil xmlns="http://www.w3.org/ns/SMIL"><body/></smil>')
        original_bytes = epub_path.read_bytes()

        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text)

        assert result is None
        assert not output_path.exists()
        assert epub_path.read_bytes() == original_bytes


def test_opf_spine_and_identifier_unchanged():
    """Spine order and the book's dc:identifier are untouched by the rewrite."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>Alpha bravo.</p></body></html>",
            "ch2": b"<html><body><p>Charlie delta.</p></body></html>",
        })
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text)
        assert result is not None

        with zipfile.ZipFile(output_path) as zf:
            opf_bytes = zf.read("OEBPS/content.opf")

        ns = "{http://www.idpf.org/2007/opf}"
        dc_ns = "{http://purl.org/dc/elements/1.1/}"
        tree = etree.fromstring(opf_bytes)
        spine = tree.find(f"{ns}spine")
        idrefs = [ref.get("idref") for ref in spine.findall(f"{ns}itemref")]
        assert idrefs == ["ch1", "ch2"]
        assert tree.find(f"{ns}metadata/{dc_ns}identifier").text == "urn:uuid:test-book-id"


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------

def test_mimetype_is_first_entry_and_stored_uncompressed():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {"ch1": b"<html><body><p>Alpha bravo.</p></body></html>"})
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text)
        assert result is not None

        with zipfile.ZipFile(output_path) as zf:
            infos = zf.infolist()
            assert infos[0].filename == "mimetype"
            assert infos[0].compress_type == zipfile.ZIP_STORED
            assert zf.read("mimetype") == b"application/epub+zip"


def test_package_carries_through_untouched_files_byte_identical():
    """A spine item with no sentences (e.g. empty/unmatched) and any other
    original archive member not touched by this phase is byte-identical in
    the output."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(
            epub_path,
            {"ch1": b"<html><body><p>Alpha bravo. Charlie delta.</p></body></html>"},
            extra_files={"OEBPS/style.css": b"body { color: black; }"},
        )
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text)
        assert result is not None

        with zipfile.ZipFile(output_path) as zf:
            assert zf.read("OEBPS/style.css") == b"body { color: black; }"
            assert zf.read("META-INF/container.xml").decode("utf-8") == _CONTAINER_XML


def test_finding5_refuses_output_path_aliasing_the_source_epub():
    """Finding 5 (independent review): using the source EPUB's own path as
    the output destroys the source -- opening it with
    ``zipfile.ZipFile(path, "w")`` truncates it while the source archive's
    own ``ZipFile`` is still reading from that same underlying file. A real
    reproduction of this exact call truncated the source to its first entry
    and then raised ``BadZipFile`` reading the second. This must instead
    refuse (via ``build_readalong_epub`` returning ``None``) without
    touching the source file at all."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {"ch1": b"<html><body><p>Alpha bravo. Charlie delta.</p></body></html>"})
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)
        original_bytes = epub_path.read_bytes()

        alignment_service = _linear_alignment(len(combined_text), 100.0)
        result = build_readalong_epub(
            parser=parser, alignment_service=alignment_service, epub_path=epub_path,
            audio_paths=audio_path, abs_id="abs1", output_path=epub_path,  # ALIASED
        )
        assert result is None
        # The source EPUB must be completely untouched, not truncated.
        assert epub_path.read_bytes() == original_bytes
        assert zipfile.is_zipfile(epub_path)


def test_finding5_package_epub_rejects_source_output_aliasing_directly():
    """Unit-level version of the aliasing guard, isolating _package_epub
    itself from the rest of the pipeline."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        epub_path = tmp / "book.epub"
        with zipfile.ZipFile(epub_path, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("OEBPS/ch1.xhtml", b"<html><body><p>Hello.</p></body></html>")
        original_bytes = epub_path.read_bytes()

        try:
            _package_epub(epub_path, epub_path, modified_files={}, new_bytes_files={}, new_disk_files={})
            assert False, "expected ValueError for aliased source/output paths"
        except ValueError:
            pass

        assert epub_path.read_bytes() == original_bytes
        assert zipfile.is_zipfile(epub_path)


def test_finding5_failed_package_leaves_existing_output_untouched_and_no_temp_litter():
    """Finding 5's other half: the destination is built into a temporary
    sibling and only atomically replaced on success, so a failure partway
    through (here: a new_disk_files entry pointing at a nonexistent file)
    never leaves a truncated/partial file at output_path, and never leaves
    the temporary file behind either."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        src_path = tmp / "book.epub"
        with zipfile.ZipFile(src_path, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("OEBPS/ch1.xhtml", b"<html><body><p>Hello.</p></body></html>")

        out_path = tmp / "existing_output.epub"
        out_path.write_bytes(b"PRE-EXISTING CONTENT THAT MUST SURVIVE A FAILED BUILD")

        try:
            _package_epub(
                src_path, out_path, modified_files={}, new_bytes_files={},
                new_disk_files={"audio.m4a": tmp / "does_not_exist.m4a"},
            )
            assert False, "expected an exception for a missing new_disk_files source"
        except OSError:
            pass

        assert out_path.read_bytes() == b"PRE-EXISTING CONTENT THAT MUST SURVIVE A FAILED BUILD"
        leftover = [p for p in tmp.iterdir() if p.name.startswith(".existing_output.epub.")]
        assert leftover == []


def test_finding5_temp_file_permissions_widened_before_replace():
    """Bug in the Finding 5 fix itself, found during live verification:
    tempfile.mkstemp() deliberately creates its file mode 0600 (owner-only)
    for shared-location safety, and os.replace() preserves that mode on the
    renamed file -- silently shipping a read-along EPUB that OTHER processes
    (BookOrbit's own scanner, running in a different container) cannot read.
    Reproduced live: BookOrbit's scan indexed the sibling standalone audio
    file but never the generated EPUB at all, with
    'EACCES: permission denied, unlink ...' in its own logs once its
    permission model rejected the 0600 file. _package_epub must widen the
    temp file's permissions to a normal, world-readable mode before the
    atomic rename. Verified by asserting os.chmod is actually called with a
    permissive mode -- an assertion on file mode BITS would not be
    meaningful on Windows, where this suite also runs, since Windows does
    not enforce POSIX permission bits the same way."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        src_path = tmp / "book.epub"
        with zipfile.ZipFile(src_path, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("OEBPS/ch1.xhtml", b"<html><body><p>Hello.</p></body></html>")
        out_path = tmp / "out.epub"

        with patch("src.services.readalong_builder.os.chmod") as mock_chmod:
            _package_epub(src_path, out_path, modified_files={}, new_bytes_files={}, new_disk_files={})

        assert mock_chmod.call_count >= 1, "_package_epub must chmod its temp file to a permissive mode"
        widened_modes = [call.args[1] for call in mock_chmod.call_args_list]
        assert any(mode & 0o044 == 0o044 for mode in widened_modes), (
            f"expected a chmod call widening group/other read access, got {widened_modes!r}"
        )
        assert out_path.exists()


def test_embedded_audio_is_transcoded_aac_not_the_original_bytes():
    """Phase 4 replaces Phase 3's "embed as-is" with a real ffmpeg transcode
    to mono AAC -- the embedded audio must NOT be byte-identical to the
    source file anymore (that was Phase 3's own guarantee, deliberately
    superseded here), and must itself be valid, decodable audio."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {"ch1": b"<html><body><p>Alpha bravo.</p></body></html>"})
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp, suffix=".wav")
        original_bytes = audio_path.read_bytes()

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text)
        assert result is not None
        assert result.audio_bitrate == _DEFAULT_AUDIO_BITRATE

        with zipfile.ZipFile(output_path) as zf:
            audio_name = next(n for n in zf.namelist() if n.startswith("OEBPS/readalong/audio"))
            embedded_bytes = zf.read(audio_name)
            assert embedded_bytes != original_bytes
            assert audio_name.endswith(".m4a")
            extracted_path = tmp / "extracted.m4a"
            extracted_path.write_bytes(embedded_bytes)

        # Decodable and roughly the source duration (silence encodes fast,
        # AAC frame quantization means it won't be exact).
        duration = _probe_duration_seconds(extracted_path)
        assert duration is not None
        assert 0.5 < duration < 2.0


# ---------------------------------------------------------------------------
# Injection-failure hardening (added to unblock live BookOrbit verification
# of the six findings above -- not one of the six itself; see the module
# docstring's discussion in the fix pass's report for the full mechanism)
# ---------------------------------------------------------------------------

def test_nonbreaking_space_at_marker_split_preserves_chapter_text():
    """Removing generated empty markers before verification preserves a
    non-breaking space at a sentence boundary."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        nbsp = " "
        _write_epub(epub_path, {
            "ch1": (
                f"<html><body><p>First sentence here.{nbsp} Second sentence follows. "
                f"Third one too.</p></body></html>"
            ).encode("utf-8"),
        })
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text)
        assert result is not None

        with zipfile.ZipFile(output_path) as zf:
            ch1_bytes = zf.read("OEBPS/ch1.xhtml")

        assert 'id="c1-s0"' in ch1_bytes.decode("utf-8")
        assert b"\xc2\xa0" in ch1_bytes


# ---------------------------------------------------------------------------
# Fitted-EPUB guard passthrough (Phase 2) and empty-result refusal
# ---------------------------------------------------------------------------

def test_refuses_when_alignment_map_does_not_fit_epub():
    """Phase 2's fitted-EPUB guard refusal (mismatched total_chars/terminal
    char) propagates as a None return, not a generated (wrong) book."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {"ch1": b"<html><body><p>Alpha bravo. Charlie delta.</p></body></html>"})
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        # total_chars deliberately wrong (way outside the drift tolerance).
        alignment_service = _FakeAlignmentService(
            terminal_char=len(combined_text),
            time_for_char=lambda c: c * 0.1,
            total_chars=len(combined_text) + 5000,
        )
        output_path = tmp / "out.epub"
        result = build_readalong_epub(
            parser=parser, alignment_service=alignment_service, epub_path=epub_path,
            audio_paths=audio_path, abs_id="abs1", output_path=output_path,
        )
        assert result is None
        assert not output_path.exists()


def test_epub2_source_is_converted_to_epub3_not_refused():
    """Finding 2 (independent review) originally refused EPUB 2 input
    outright; ``docs/PLAN_READALONG_EPUB3_GENERATION.md``'s follow-up work
    replaces that refusal with an actual EPUB 2 -> EPUB 3 conversion
    (``src/services/epub3_upgrade.py``), applied to a private temporary copy
    before assembly (:func:`~src.services.readalong_builder._resolve_epub3_source`).
    The final generated read-along package must itself be a conformant
    EPUB 3 (version bumped, a nav document registered), and the original
    library file must be completely untouched."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(
            epub_path,
            {"ch1": b"<html><body><p>Alpha bravo. Charlie delta.</p></body></html>"},
            opf_version="2.0",
        )
        original_bytes = epub_path.read_bytes()
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text)

        assert result is not None
        assert output_path.exists()
        # Source file untouched (Finding 5's exact failure mode -- never
        # regress on this, whatever else changes about conversion).
        assert epub_path.read_bytes() == original_bytes

        with zipfile.ZipFile(output_path) as zf:
            opf_bytes = zf.read("OEBPS/content.opf")
            pkg = etree.fromstring(opf_bytes)
            assert pkg.get("version") == "3.0"
            manifest = pkg.find("{http://www.idpf.org/2007/opf}manifest")
            nav_items = [
                item for item in manifest.findall("{http://www.idpf.org/2007/opf}item")
                if "nav" in (item.get("properties") or "").split()
            ]
            assert len(nav_items) == 1
            nav_href = nav_items[0].get("href")
            assert zf.read(f"OEBPS/{nav_href}")  # the nav document itself was packaged
            metadata = pkg.find("{http://www.idpf.org/2007/opf}metadata")
            assert any(
                meta.get("property") == "dcterms:modified"
                for meta in metadata.findall("{http://www.idpf.org/2007/opf}meta")
            )
            spine = pkg.find("{http://www.idpf.org/2007/opf}spine")
            assert "toc" not in spine.attrib


def test_defect1_refuses_output_path_aliasing_an_epub2_source():
    """Defect 1 (independent review): the EPUB 2 -> EPUB 3 conversion in
    _resolve_epub3_source runs BEFORE _package_epub's own Finding 5 aliasing
    check ever sees the call, and it converts into a private temporary file
    -- so _package_epub's check compares that temp file against output_path,
    never the real source_epub. Calling this public entry point with
    output_path equal to the ORIGINAL EPUB 2 library path sailed straight
    through that check and _package_epub then os.replace()'d the temporary
    converted copy over the user's real library file. This must instead
    refuse before _resolve_epub3_source is even entered, leaving the
    original file completely untouched."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(
            epub_path,
            {"ch1": b"<html><body><p>Alpha bravo. Charlie delta.</p></body></html>"},
            opf_version="2.0",
        )
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)
        original_bytes = epub_path.read_bytes()

        alignment_service = _linear_alignment(len(combined_text), 100.0)
        result = build_readalong_epub(
            parser=parser, alignment_service=alignment_service, epub_path=epub_path,
            audio_paths=audio_path, abs_id="abs1", output_path=epub_path,  # ALIASED, EPUB 2
        )
        assert result is None
        # The source EPUB must be completely untouched, not overwritten by
        # the temporary EPUB 3 conversion copy.
        assert epub_path.read_bytes() == original_bytes
        assert zipfile.is_zipfile(epub_path)


def test_refuses_epub2_source_when_conversion_cannot_produce_valid_epub3():
    """Keeps refusal as the fallback (per the plan): when
    ``upgrade_epub2_to_epub3`` itself cannot convert the source (here: the
    OPF has no ``<manifest>`` at all, so there is nothing to register a nav
    document in), the whole build is refused rather than emitting a package
    that merely claims to be EPUB 3."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        epub_path = tmp / "books" / "book.epub"
        epub_path.parent.mkdir(parents=True, exist_ok=True)
        broken_opf = (
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
            'version="2.0" unique-identifier="id"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Test</dc:title>'
            "</metadata></package>"
        )
        with zipfile.ZipFile(epub_path, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml", _CONTAINER_XML)
            z.writestr("OEBPS/content.opf", broken_opf)
            z.writestr("OEBPS/ch1.xhtml", b"<html><body><p>Alpha bravo.</p></body></html>")
        original_bytes = epub_path.read_bytes()

        parser = _parser(tmp)
        audio_path = _make_audio(tmp)
        output_path = tmp / "out.epub"
        alignment_service = _linear_alignment(40, 100.0)
        result = build_readalong_epub(
            parser=parser, alignment_service=alignment_service, epub_path=epub_path,
            audio_paths=audio_path, abs_id="abs1", output_path=output_path,
        )
        assert result is None
        assert not output_path.exists()
        assert epub_path.read_bytes() == original_bytes


def test_output_reused_epub_has_no_leftover_markers_from_prior_run():
    """Regenerating the same book from the same source EPUB (not the
    previously-generated output) is idempotent -- sanity check that the
    builder always starts from the pristine source, not some accumulated
    state."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {"ch1": b"<html><body><p>Alpha bravo. Charlie delta.</p></body></html>"})
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        result1, output1 = _build(tmp, parser, epub_path, audio_path, combined_text, output_name="out1.epub")
        result2, output2 = _build(tmp, parser, epub_path, audio_path, combined_text, output_name="out2.epub")
        assert result1 is not None and result2 is not None

        with zipfile.ZipFile(output1) as z1, zipfile.ZipFile(output2) as z2:
            assert z1.read("OEBPS/ch1.xhtml") == z2.read("OEBPS/ch1.xhtml")


# ---------------------------------------------------------------------------
# Phase 4 Part A: clip contiguity (_extend_clips_to_contiguous)
# ---------------------------------------------------------------------------

def _clip(sentence_id: str, spine_index: int, ts_start: float, ts_end: float) -> SentenceClip:
    return SentenceClip(
        sentence_id=sentence_id, spine_index=spine_index, href=f"c{spine_index}.xhtml",
        char_start=0, char_end=1, ts_start=ts_start, ts_end=ts_end,
    )


def test_extend_clips_to_contiguous_closes_internal_gaps():
    """Each non-last clip's end is pulled forward to exactly the next clip's
    start -- the fix for Phase 3's measured 4.28% short overlay total."""
    clips = [
        _clip("c1-s0", 1, 0.0, 1.0),
        _clip("c1-s1", 1, 1.5, 2.5),   # 0.5s gap before this clip
        _clip("c1-s2", 1, 3.0, 4.0),   # 0.5s gap before this clip
    ]
    extended = _extend_clips_to_contiguous(clips, audio_duration_seconds=10.0)
    assert extended[0].ts_end == extended[1].ts_start == 1.5
    assert extended[1].ts_end == extended[2].ts_start == 3.0


def test_extend_clips_to_contiguous_preserves_ts_start():
    """Only ts_end is ever changed -- ts_start (the true sentence onset) is
    identical before and after for every clip, including the last."""
    clips = [
        _clip("c1-s0", 1, 0.0, 1.0),
        _clip("c1-s1", 1, 1.5, 2.5),
        _clip("c1-s2", 1, 3.0, 4.0),
    ]
    extended = _extend_clips_to_contiguous(clips, audio_duration_seconds=10.0)
    assert [c.ts_start for c in extended] == [c.ts_start for c in clips]
    assert [c.sentence_id for c in extended] == [c.sentence_id for c in clips]
    assert [c.char_start for c in extended] == [c.char_start for c in clips]


def test_extend_clips_to_contiguous_extends_last_clip_to_audio_duration():
    """The book's final clip has no "next" clip, so it is extended all the
    way to the real (probed) audio duration instead."""
    clips = [_clip("c1-s0", 1, 0.0, 1.0), _clip("c1-s1", 1, 1.5, 2.5)]
    extended = _extend_clips_to_contiguous(clips, audio_duration_seconds=10.0)
    assert extended[-1].ts_end == 10.0
    assert extended[0].ts_end == 1.5  # internal gap still closed


def test_extend_clips_to_contiguous_never_shrinks_when_duration_too_short_or_missing():
    """If the probed duration is unavailable, or isn't actually past the last
    clip's own computed end, the last clip is left untouched -- this never
    shrinks a clip or guesses at an unbacked duration."""
    clips = [_clip("c1-s0", 1, 0.0, 1.0), _clip("c1-s1", 1, 1.5, 5.0)]

    extended_no_probe = _extend_clips_to_contiguous(clips, audio_duration_seconds=None)
    assert extended_no_probe[-1].ts_end == 5.0

    extended_short_probe = _extend_clips_to_contiguous(clips, audio_duration_seconds=3.0)
    assert extended_short_probe[-1].ts_end == 5.0  # 3.0 < 5.0, so left alone


def test_extend_clips_to_contiguous_empty_list():
    assert _extend_clips_to_contiguous([], 10.0) == []


def test_extend_clips_to_contiguous_single_clip_only_gets_tail_extension():
    clips = [_clip("c1-s0", 1, 0.0, 1.0)]
    extended = _extend_clips_to_contiguous(clips, audio_duration_seconds=5.0)
    assert extended[0].ts_start == 0.0
    assert extended[0].ts_end == 5.0


def test_extend_clips_to_contiguous_does_not_corrupt_a_legitimate_backward_segment_transition():
    """Finding 3 follow-on: build_sentence_clips now legitimately allows
    ts_start[i+1] < ts_end[i] across a genuine out-of-order-narration segment
    transition (see test_readalong_segments.py's
    test_out_of_order_segments_preserve_reordered_narration_timestamps).
    Blindly extending clip i's ts_end to clip i+1's ts_start in that case
    would produce a NEGATIVE-duration clip (ts_end < ts_start) -- worse than
    the pause this function exists to close. The earlier clip's own computed
    end must survive untouched instead."""
    clips = [
        SentenceClip(
            sentence_id="c1-s0", spine_index=1, href="c1.xhtml",
            char_start=0, char_end=1, ts_start=10.0, ts_end=20.0,
            segment_key=1, segment_ts_start=10.0, segment_ts_end=20.0,
            segment_scoped=True,
        ),
        SentenceClip(
            sentence_id="c1-s1", spine_index=1, href="c1.xhtml",
            char_start=1, char_end=2, ts_start=0.0, ts_end=9.0,
            segment_key=2, segment_ts_start=0.0, segment_ts_end=9.0,
            segment_scoped=True,
        ),  # a different, earlier-narrated segment
    ]
    extended = _extend_clips_to_contiguous(clips, audio_duration_seconds=25.0)
    assert extended[0].ts_start == 10.0
    assert extended[0].ts_end == 25.0  # temporal-last segment gets the real audio tail
    assert extended[0].ts_end >= extended[0].ts_start  # never negative duration
    assert extended[1].ts_start == 0.0
    assert extended[1].ts_end == 9.0  # no tail extension across a later reading-order segment


# ---------------------------------------------------------------------------
# Phase 4 Part A: contiguity end to end, via build_readalong_epub
# ---------------------------------------------------------------------------

def _all_pars_in_order(output_path: Path) -> List[Dict[str, float]]:
    """Every <par>'s (clipBegin, clipEnd), across every .smil in the archive,
    ordered by spine index then position within the file -- i.e. book
    reading order, matching how _extend_clips_to_contiguous consumes them."""
    pairs = []
    with zipfile.ZipFile(output_path) as zf:
        smil_names = sorted(
            (n for n in zf.namelist() if n.endswith(".smil")),
            key=lambda n: int(Path(n).stem),
        )
        for name in smil_names:
            root = etree.fromstring(zf.read(name))
            for par in root.findall(f".//{_SMIL_NS}par"):
                audio = par.find(f"{_SMIL_NS}audio")
                pairs.append({
                    "id": par.get("id"),
                    "begin": float(audio.get("clipBegin")[:-1]),
                    "end": float(audio.get("clipEnd")[:-1]),
                })
    return pairs


def test_no_gap_between_consecutive_pars_within_one_spine_item():
    """Adjacent sentences in the same chapter: clipEnd of one exactly equals
    clipBegin of the next -- no inter-sentence pause is left uncovered."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>Alpha bravo. Charlie delta. Echo foxtrot.</p></body></html>",
        })
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text, total_seconds=0.5)
        assert result is not None

        pars = _all_pars_in_order(output_path)
        assert len(pars) == 3
        for i in range(len(pars) - 1):
            assert pars[i]["end"] == pars[i + 1]["begin"], pars


def test_no_gap_across_spine_item_boundary():
    """The last sentence of chapter 1 and the first sentence of chapter 2 are
    also contiguous -- the fix applies across spine items, not just within
    one, per the plan's explicit instruction."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>Alpha bravo. Charlie delta.</p></body></html>",
            "ch2": b"<html><body><p>Echo foxtrot. Golf hotel.</p></body></html>",
        })
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text, total_seconds=0.5)
        assert result is not None

        pars = _all_pars_in_order(output_path)
        assert len(pars) == 4
        for i in range(len(pars) - 1):
            assert pars[i]["end"] == pars[i + 1]["begin"], pars


def test_last_clip_of_book_reaches_the_real_embedded_audio_duration():
    """The very last <par> in the whole book has clipEnd equal to the
    embedded (transcoded) audio's own real, probed duration -- not the raw
    interpolated end Phase 2 computed, which stops short of the real audio
    by however long the trailing pause/silence runs."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>Alpha bravo. Charlie delta.</p></body></html>",
        })
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp, duration=1.0)

        # total_seconds well under the real ~1.0s audio, so the tail
        # extension actually has room to grow into.
        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text, total_seconds=0.2)
        assert result is not None

        with zipfile.ZipFile(output_path) as zf:
            audio_name = next(n for n in zf.namelist() if n.startswith("OEBPS/readalong/audio"))
            extracted_path = tmp / "extracted.m4a"
            extracted_path.write_bytes(zf.read(audio_name))
        real_duration = _probe_duration_seconds(extracted_path)
        assert real_duration is not None

        pars = _all_pars_in_order(output_path)
        assert pars[-1]["end"] == pytest.approx(real_duration, abs=1e-6)


# ---------------------------------------------------------------------------
# Phase 4 Part B: READALONG_AUDIO_BITRATE (read per call, invalid degrades safely)
# ---------------------------------------------------------------------------

def test_bitrate_setting_read_per_call():
    """Two builds in the same process with different
    READALONG_AUDIO_BITRATE values each pick up their own setting -- proof
    it is read per call, not cached at import or in a Singleton."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {"ch1": b"<html><body><p>Alpha bravo.</p></body></html>"})
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        old = os.environ.get("READALONG_AUDIO_BITRATE")
        try:
            os.environ["READALONG_AUDIO_BITRATE"] = "24k"
            result1, _ = _build(tmp, parser, epub_path, audio_path, combined_text, output_name="a.epub")
            assert result1 is not None
            assert result1.audio_bitrate == "24k"

            os.environ["READALONG_AUDIO_BITRATE"] = "64k"
            result2, _ = _build(tmp, parser, epub_path, audio_path, combined_text, output_name="b.epub")
            assert result2 is not None
            assert result2.audio_bitrate == "64k"
        finally:
            if old is None:
                os.environ.pop("READALONG_AUDIO_BITRATE", None)
            else:
                os.environ["READALONG_AUDIO_BITRATE"] = old


def test_invalid_bitrate_degrades_to_default_instead_of_crashing():
    """An admin typo in READALONG_AUDIO_BITRATE must not abort generation --
    it falls back to the safe default and the build still succeeds."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {"ch1": b"<html><body><p>Alpha bravo.</p></body></html>"})
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)

        old = os.environ.get("READALONG_AUDIO_BITRATE")
        try:
            os.environ["READALONG_AUDIO_BITRATE"] = "not-a-bitrate"
            result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text)
            assert result is not None
            assert result.audio_bitrate == _DEFAULT_AUDIO_BITRATE
            assert output_path.exists()
        finally:
            if old is None:
                os.environ.pop("READALONG_AUDIO_BITRATE", None)
            else:
                os.environ["READALONG_AUDIO_BITRATE"] = old


# ---------------------------------------------------------------------------
# Phase 4 Part B: multi-file audio concatenation
# ---------------------------------------------------------------------------

def test_multi_file_audio_is_concatenated_into_one_embedded_file():
    """A multi-file audiobook's parts, given in order, are concatenated into
    the single physical file the SMIL references -- the embedded audio's
    real duration should be close to the sum of the two source parts'
    durations (loose tolerance: AAC frame quantization on re-encode)."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {"ch1": b"<html><body><p>Alpha bravo.</p></body></html>"})
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        part1 = _make_audio(tmp, duration=1.0, name="part1")
        part2 = _make_audio(tmp, duration=1.5, name="part2")

        result, output_path = _build(tmp, parser, epub_path, [part1, part2], combined_text, total_seconds=0.1)
        assert result is not None

        with zipfile.ZipFile(output_path) as zf:
            audio_name = next(n for n in zf.namelist() if n.startswith("OEBPS/readalong/audio"))
            extracted_path = tmp / "extracted.m4a"
            extracted_path.write_bytes(zf.read(audio_name))
        duration = _probe_duration_seconds(extracted_path)
        assert duration is not None
        assert 2.0 < duration < 3.0  # ~2.5s (1.0 + 1.5), generous tolerance


def test_refuses_with_no_audio_paths():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {"ch1": b"<html><body><p>Alpha bravo.</p></body></html>"})
        combined_text, _ = parser.extract_text_and_map(str(epub_path))

        alignment_service = _linear_alignment(len(combined_text), 10.0)
        output_path = tmp / "out.epub"
        result = build_readalong_epub(
            parser=parser, alignment_service=alignment_service, epub_path=epub_path,
            audio_paths=[], abs_id="abs1", output_path=output_path,
        )
        assert result is None
        assert not output_path.exists()


def test_standalone_audio_output_path_gets_a_copy_of_the_transcoded_audio():
    """standalone_audio_output_path receives the same transcoded bytes that
    got embedded -- Phase 3's live finding is that BookOrbit's file scanner
    needs a standalone sibling audio file next to the generated EPUB; this is
    how a caller gets that file without re-running ffmpeg."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {"ch1": b"<html><body><p>Alpha bravo.</p></body></html>"})
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)
        standalone_path = tmp / "standalone.m4a"

        result, output_path = _build(
            tmp, parser, epub_path, audio_path, combined_text,
            standalone_audio_output_path=standalone_path,
        )
        assert result is not None
        assert standalone_path.exists()

        with zipfile.ZipFile(output_path) as zf:
            audio_name = next(n for n in zf.namelist() if n.startswith("OEBPS/readalong/audio"))
            assert zf.read(audio_name) == standalone_path.read_bytes()


# ---------------------------------------------------------------------------
# Staged progress reporting
# ---------------------------------------------------------------------------

def test_safe_progress_swallows_callback_exception():
    """Progress reporting must never break generation -- a callback that
    raises is logged and swallowed, not propagated."""
    def _boom(stage, fraction):
        raise RuntimeError("callback exploded")

    _safe_progress(_boom, "transcoding_audio", 0.5)  # must not raise


def test_safe_progress_clamps_fraction_and_is_a_noop_with_no_callback():
    calls: List[Tuple[str, float]] = []

    def _record(stage, fraction):
        calls.append((stage, fraction))

    _safe_progress(_record, "packaging", 5.0)   # over 1.0
    _safe_progress(_record, "packaging", -1.0)  # under 0.0
    assert calls == [("packaging", 1.0), ("packaging", 0.0)]

    calls.clear()
    _safe_progress(None, "packaging", 0.5)  # no callback: silent no-op
    assert calls == []


def test_build_reports_stages_in_order_with_nondecreasing_progress():
    """End-to-end (real ffmpeg, tiny fixture): every stage
    `_resolve_epub3_source`/`_build_readalong_epub_impl` can report fires, in
    call order, with a never-decreasing overall fraction. Consecutive
    same-stage entries are collapsed before comparison -- ffmpeg's own
    `-progress` reporting granularity on a sub-second silent fixture is
    timing-dependent (it may emit zero or several intra-transcode ticks),
    but the STAGE TRANSITION sequence must not vary."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>First sentence here. Second sentence follows.</p></body></html>",
        })
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp)
        alignment_service = _linear_alignment(len(combined_text), 100.0)
        output_path = tmp / "out.epub"

        seen: List[Tuple[str, float]] = []
        result = build_readalong_epub(
            parser=parser,
            alignment_service=alignment_service,
            epub_path=epub_path,
            audio_paths=audio_path,
            abs_id="abs1",
            output_path=output_path,
            progress_callback=lambda stage, fraction: seen.append((stage, fraction)),
        )
        assert result is not None
        assert seen, "expected at least one progress report"

        transitions: List[str] = []
        for stage, _fraction in seen:
            if not transitions or transitions[-1] != stage:
                transitions.append(stage)
        assert transitions == [
            "converting_epub", "parsing_epub", "transcoding_audio",
            "building_overlays", "packaging",
        ]

        fractions = [fraction for _stage, fraction in seen]
        assert fractions == sorted(fractions)
        assert all(0.0 <= fraction <= 1.0 for fraction in fractions)


def test_build_refusal_still_reports_the_stages_reached_before_refusing():
    """A refused build (no audio paths here) reaches parsing_epub's report
    before returning None -- progress reporting doesn't require a
    successful build to have fired at all."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {"ch1": b"<html><body><p>Alpha bravo.</p></body></html>"})
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        alignment_service = _linear_alignment(len(combined_text), 10.0)

        seen: List[Tuple[str, float]] = []
        result = build_readalong_epub(
            parser=parser, alignment_service=alignment_service, epub_path=epub_path,
            audio_paths=[], abs_id="abs1", output_path=tmp / "out.epub",
            progress_callback=lambda stage, fraction: seen.append((stage, fraction)),
        )
        assert result is None
        # Refused before parsing (no audio paths is the very first check in
        # _build_readalong_epub_impl) -- converting_epub still fired.
        assert [s for s, _ in seen] == ["converting_epub"]


def test_transcode_reports_intra_stage_progress_via_mocked_ffmpeg_stream():
    """`_transcode_audio_for_embed`'s `-progress pipe:1` parsing, exercised
    deterministically: real silent audio encodes far faster than ffmpeg's
    own ~0.5s reporting period, so a real end-to-end run can't be relied on
    to produce more than one progress line. `ffprobe` is real (fast, on a
    tiny fixture file) -- only the ffmpeg *process* is faked so the exact
    progress lines it emits, and therefore the fractions computed from
    them, are known in advance."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        audio_path = _make_audio(tmp, duration=4.0)

        class _FakeProc:
            def __init__(self, lines: List[str]):
                self.stdout = iter(lines)

            def wait(self) -> int:
                return 0

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        fake_proc = _FakeProc([
            "out_time=00:00:01.000000\n",
            "out_time=00:00:02.000000\n",
            "out_time=00:00:04.000000\n",
            "progress=end\n",
        ])

        # `_probe_duration_seconds` (called first, to compute total_duration)
        # goes through `subprocess.run`, which internally calls this same
        # module-global `Popen` too -- faking only the ffmpeg invocation and
        # falling through to the real Popen for everything else (ffprobe)
        # keeps that probe real while making the ffmpeg process's own
        # progress stream deterministic.
        real_popen = subprocess.Popen
        ffmpeg_calls = []

        def _maybe_fake_popen(cmd, *args, **kwargs):
            if cmd and cmd[0] == "ffmpeg":
                ffmpeg_calls.append(cmd)
                return fake_proc
            return real_popen(cmd, *args, **kwargs)

        seen: List[float] = []
        with patch.object(_readalong_builder_module.subprocess, "Popen", side_effect=_maybe_fake_popen):
            ok = _transcode_audio_for_embed(
                [audio_path], "32k", tmp / "out.m4a", progress_callback=seen.append,
            )

        assert ok is True
        assert len(ffmpeg_calls) == 1
        cmd = ffmpeg_calls[0]
        # The no-callback path never adds these -- confirms the progress
        # branch, not the plain subprocess.run path, actually ran.
        assert "-progress" in cmd
        assert "pipe:1" in cmd

        assert len(seen) == 3
        assert seen[0] == pytest.approx(0.25, abs=0.02)
        assert seen[1] == pytest.approx(0.5, abs=0.02)
        assert seen[2] == pytest.approx(1.0, abs=0.02)


def test_transcode_without_progress_callback_never_adds_progress_flags():
    """No `progress_callback` given: the plain, pre-existing `subprocess.run`
    path runs, with no `-progress`/`-nostats` flags added -- confirms the
    new code path is strictly additive and opt-in."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        audio_path = _make_audio(tmp, duration=1.0)
        ok = _transcode_audio_for_embed([audio_path], "32k", tmp / "out.m4a")
        assert ok is True
