"""Unit tests for EPUB 3 read-along assembly (Phase 3:
docs/PLAN_READALONG_EPUB3_GENERATION.md) -- marker injection, SMIL emission,
OPF rewriting, and zip packaging.

Builds small inline EPUB fixtures with zipfile (same pattern as
test_ebook_dom_map.py / test_readalong_segments.py). Alignment maps are
supplied via the same minimal fake AlignmentService double
test_readalong_segments.py uses.
"""
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional
from xml.etree import ElementTree

from lxml import etree

from src.services.readalong_builder import build_readalong_epub
from src.utils.ebook_utils import EbookParser

_CONTAINER_XML = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)

_SMIL_NS = "{http://www.w3.org/ns/SMIL}"


def _parser(tmp: Path) -> EbookParser:
    books = tmp / "books"
    cache = tmp / "cache"
    books.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    return EbookParser(books_dir=str(books), epub_cache_dir=str(cache))


def _opf(manifest_ids: List[str], spine_idrefs: List[str], extra_manifest: str = "") -> str:
    manifest = "".join(
        f'<item id="{iid}" href="{iid}.xhtml" media-type="application/xhtml+xml"/>'
        for iid in manifest_ids
    )
    spine = "".join(f'<itemref idref="{iid}"/>' for iid in spine_idrefs)
    return (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
        'version="3.0" unique-identifier="id"><metadata '
        'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Test Book</dc:title>'
        '<dc:identifier id="id">urn:uuid:test-book-id</dc:identifier></metadata>'
        f'<manifest>{manifest}{extra_manifest}</manifest><spine>{spine}</spine></package>'
    )


def _write_epub(path: Path, items: Dict[str, bytes], extra_manifest: str = "",
                 extra_files: Optional[Dict[str, bytes]] = None) -> None:
    """``items``: {item_id: xhtml_bytes}. Spine order is dict order.
    ``extra_manifest`` lets a test add a pre-existing, unrelated manifest item
    (e.g. a cover image) to verify it survives the OPF rewrite untouched.
    ``extra_files`` writes additional raw zip entries (e.g. that cover's
    bytes)."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", _CONTAINER_XML)
        z.writestr("OEBPS/content.opf", _opf(list(items.keys()), list(items.keys()), extra_manifest))
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
    ):
        self._terminal_char = terminal_char
        self._time_for_char = time_for_char
        self.database_service = self._FakeDatabaseService(total_chars)

    def get_map_terminal_char(self, abs_id: str) -> Optional[int]:
        return self._terminal_char

    def get_time_for_char(self, abs_id: str, char_offset: int) -> Optional[float]:
        return self._time_for_char(char_offset)


def _linear_alignment(total_chars: int, total_seconds: float) -> _FakeAlignmentService:
    """A fake alignment map mapping char 0 -> 0.0s and the last char -> total_seconds,
    linear in between -- matches the fitted-EPUB guard via total_chars."""
    def interpolate(char_offset: int) -> float:
        frac = max(0.0, min(1.0, char_offset / total_chars)) if total_chars else 0.0
        return frac * total_seconds

    return _FakeAlignmentService(
        terminal_char=total_chars, time_for_char=interpolate, total_chars=total_chars,
    )


def _make_audio(tmp: Path, suffix: str = ".mp3", size: int = 256) -> Path:
    audio_path = tmp / f"audio{suffix}"
    audio_path.write_bytes(b"\x00" * size)
    return audio_path


def _build(tmp: Path, parser: EbookParser, epub_path: Path, audio_path: Path,
           combined_text: str, abs_id: str = "abs1", total_seconds: float = 100.0,
           output_name: str = "out.epub"):
    alignment_service = _linear_alignment(len(combined_text), total_seconds)
    output_path = tmp / output_name
    result = build_readalong_epub(
        parser=parser,
        alignment_service=alignment_service,
        epub_path=epub_path,
        audio_path=audio_path,
        abs_id=abs_id,
        output_path=output_path,
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

        soup_text_after_s0 = xhtml.split('<span id="c1-s0">', 1)[1]
        assert soup_text_after_s0.split("</span>", 1)[1].lstrip().startswith("First sentence here.")
        soup_text_after_s1 = xhtml.split('<span id="c1-s1">', 1)[1]
        assert soup_text_after_s1.split("</span>", 1)[1].lstrip().startswith("Second sentence follows.")


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
        assert xhtml.index('<span id="c1-s0">') < xhtml.index("Hello")
        assert xhtml.index('<span id="c1-s1">') < xhtml.index("A second sentence")
        # Only one marker was needed for sentence 0 even though it spans two
        # inline elements -- no marker was injected inside <em>/<strong>.
        assert xhtml.count('<span id="c1-s0">') == 1
        em_start = xhtml.index("<em>brave")
        em_end = xhtml.index("</em>", em_start)
        assert "c1-s0" not in xhtml[em_start:em_end]
        assert "c1-s1" not in xhtml[em_start:em_end]


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

        assert xhtml.index('<span id="c1-s0">') < xhtml.index("Alpha")
        assert xhtml.index('<span id="c1-s1">') < xhtml.index("Delta")
        assert xhtml.index('<span id="c1-s2">') < xhtml.index("Golf")
        assert xhtml.index("Alpha") < xhtml.index('<span id="c1-s1">')
        assert xhtml.index("Delta") < xhtml.index('<span id="c1-s2">')


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


def test_embedded_audio_is_present_and_byte_identical():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "book.epub"
        _write_epub(epub_path, {"ch1": b"<html><body><p>Alpha bravo.</p></body></html>"})
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        audio_path = _make_audio(tmp, size=4096)
        audio_bytes = audio_path.read_bytes()

        result, output_path = _build(tmp, parser, epub_path, audio_path, combined_text)
        assert result is not None

        with zipfile.ZipFile(output_path) as zf:
            audio_name = next(n for n in zf.namelist() if n.startswith("OEBPS/readalong/audio"))
            assert zf.read(audio_name) == audio_bytes


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
            audio_path=audio_path, abs_id="abs1", output_path=output_path,
        )
        assert result is None
        assert not output_path.exists()


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
