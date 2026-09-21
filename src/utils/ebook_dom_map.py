"""DOM-anchored text extraction for read-along EPUB 3 generation (Phase 1).

``EbookParser.extract_text_and_map`` (``src/utils/ebook_utils.py``) builds a book's
plain-text representation with ``BeautifulSoup(item.get_content(), 'html.parser')``
then ``soup.get_text(separator=' ', strip=True)``. Alignment maps (the bridge's
transcript-to-audio-timestamp maps) live in that exact character space. Generating
SMIL media overlays requires the reverse: given a character offset in that space,
find the source text node and offset inside the *original* spine-item XHTML so a
marker can be inserted there.

``strip=True`` is not invertible from the joined string alone, so this module does
not try to reverse-engineer it. Instead it walks the same DOM tree
``extract_text_and_map`` walked and replicates bs4's own per-node algorithm
directly, recording provenance as it goes:

    bs4's ``Tag._all_strings(strip=True)`` (what ``get_text`` calls) walks
    ``soup.descendants``, keeps only nodes whose *exact* type is
    ``NavigableString`` or ``CData`` (this is what silently drops ``<script>``/
    ``<style>`` content -- those are the distinct ``Script``/``Stylesheet``
    subclasses -- and comments/doctype/processing-instructions), strips each
    surviving string individually, and drops it entirely if stripping empties it.
    ``get_text(separator=' ')`` then joins the surviving *already-stripped*
    strings with a single space. It does NOT join first and strip the result --
    verified against the exact bs4 build pinned in this repo
    (``.venv/Lib/site-packages/bs4/element.py``, ``Tag._all_strings``).

This module re-parses each spine item's *already-captured* ``content`` bytes from
``extract_text_and_map``'s own ``spine_map`` (rather than re-reading the EPUB), so
it can never drift from the exact bytes the reference text was built from, and it
never touches ``EbookParser.cache`` -- it is an ordinary reader of
``extract_text_and_map``'s cached-or-fresh result, exactly like the interface's
other 28 callers.

Node identity is a 0-based index into the *content-string* list for that spine
item (i.e. the same nodes ``get_text`` would enumerate, in document order, before
the empty-after-strip ones are dropped). Re-parsing the same ``content`` bytes
with the same parser is deterministic, so that index is a stable, reproducible
way to relocate the node later (e.g. to insert a SMIL marker span in Phase 3) --
no fragile CSS-selector or XPath scheme required.
"""
import html.entities
import logging
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple, Union, TYPE_CHECKING

from bs4 import BeautifulSoup, CData, NavigableString, Tag

if TYPE_CHECKING:
    from src.utils.ebook_utils import EbookParser

logger = logging.getLogger(__name__)

# The exact set bs4's Tag._all_strings() filters to when called (indirectly, via
# get_text()) on a top-level BeautifulSoup document, whose own
# `interesting_string_types` is unset and therefore resolves to this constant.
# Kept as our own copy (rather than reaching into `soup.interesting_string_types`
# per call) so behaviour is pinned regardless of a given document's tag names.
_CONTENT_STRING_TYPES = (NavigableString, CData)

# Tags whose text content bs4's HTML-mode builders never surface as ordinary
# content strings (they get the special ``Script``/``Stylesheet`` subclasses
# instead, which fail the exact-type check above -- see this module's
# docstring). ``TreeBuilder.string_containers`` only carries that mapping for
# HTML-flavoured builders; the XML builder :func:`parse_original_spine_xml`
# uses for read-along fidelity (Finding 1) inherits the base class's *empty*
# mapping, so without this explicit parent-tag check, ``<script>``/``<style>``
# text would leak into the node list when parsing a spine item's ORIGINAL
# archive bytes as XML, even though it never did when parsing the (lossy)
# ebooklib-reconstructed content ``extract_text_and_map`` builds ``combined_text``
# from. Filtering by parent tag name is redundant (but harmless) for the HTML
# path, since those nodes are already excluded there by type; it is load-bearing
# for the XML path.
_EXCLUDED_STRING_PARENTS = frozenset({"script", "style", "template", "head"})

# XML's five predefined entities -- the only named entity references a strict
# XML parser understands without an external/internal DTD. Left untouched by
# :func:`escape_named_html_entities` (rewriting ``&amp;`` to ``&#38;`` would be
# harmless in isolation, but the point of that function is to touch only the
# entities a bare XML parser cannot already resolve).
_XML_BUILTIN_ENTITY_NAMES = frozenset({"amp", "lt", "gt", "apos", "quot"})

# A named entity reference, e.g. ``&nbsp;`` or ``&mdash;``.
_NAMED_ENTITY_RE = re.compile(r'&([a-zA-Z][a-zA-Z0-9]*);')


@dataclass(frozen=True)
class DomRun:
    """One emitted, non-empty-after-strip text run, with source-node provenance.

    ``start``/``end`` are global character offsets in the same character space as
    ``EbookParser.extract_text_and_map``'s combined text (half-open ``[start,
    end)``). ``node_index`` is this run's source node's position (0-based) in the
    spine item's content-string node list, in document order. ``node_offset_start``/
    ``node_offset_end`` are the half-open offset range within that node's *original*
    (unstripped) string value that produced ``text``.
    """
    node_index: int
    node_offset_start: int
    node_offset_end: int
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class SpineDomMap:
    """DOM-anchored runs for one spine item, aligned with an ``extract_text_and_map``
    ``spine_map`` entry.

    ``spine_index``, ``href``, ``start`` and ``end`` mirror the corresponding
    ``spine_map`` dict fields exactly (``start``/``end`` are the same half-open
    global range). ``runs`` is empty for a spine item with no content-string nodes,
    or one whose nodes all strip to empty. ``node_count`` is the total number of
    content-string nodes considered for this item (including ones that stripped to
    empty and so produced no run) -- an upper bound for validating a ``node_index``.
    """
    spine_index: int
    href: str
    start: int
    end: int
    runs: List[DomRun] = field(default_factory=list)
    node_count: int = 0


def content_string_nodes(soup: BeautifulSoup) -> List[NavigableString]:
    """All descendants of ``soup`` that ``soup.get_text()`` would enumerate.

    Public because the read-along builder re-parses the same ``content`` bytes to
    splice in markers and must enumerate nodes in exactly the order
    :func:`locate_offset` indexed them. Re-deriving this filter there would risk a
    silent divergence that misplaces every marker in the item.

    Mirrors ``Tag._all_strings`` filtering to the exact types ``get_text()``
    considers for a top-level document (excludes ``Comment``, ``Doctype``,
    ``ProcessingInstruction``, and the ``Script``/``Stylesheet``/``TemplateString``
    subclasses used for ``<script>``/``<style>``/``<template>`` content -- all are
    ``NavigableString`` subclasses but not of the exact filtered types), PLUS an
    explicit parent-tag-name check for the same three tags (see
    :data:`_EXCLUDED_STRING_PARENTS`) -- a no-op for an HTML-flavoured parser
    (those nodes are already excluded by type there) but load-bearing when
    ``soup`` was built by the XML builder :func:`parse_original_spine_xml` uses,
    which has no HTML-specific string-container knowledge and would otherwise
    surface ``<script>``/``<style>`` text as ordinary content.
    """
    nodes = []
    for node in soup.descendants:
        if type(node) not in _CONTENT_STRING_TYPES:
            continue
        if _has_excluded_ancestor(node):
            continue
        nodes.append(node)
    return nodes


def _has_excluded_ancestor(node: NavigableString) -> bool:
    """Whether ``node`` sits under a tag whose text never reaches the canonical
    character space.

    ``<script>``/``<style>``/``<template>`` are excluded for the reason given in
    :func:`content_string_nodes`. ``<head>`` is excluded for a different and
    less obvious one: ebooklib's reconstruction, which is what
    ``extract_text_and_map`` builds ``combined_text`` from, empties the head
    outright -- a real book's

        <head><link rel="stylesheet" .../><title>c2T</title></head>

    comes back as ``<head/>``. So the canonical text contains no ``<title>``
    text, while parsing the ORIGINAL archive bytes does surface it. Left
    unfiltered that put four extra characters (``"c2T "``) at offset 0 of the
    affected spine item, shifting every marker in it and tripping the builder's
    own injection self-check. That discarded head is also where the stylesheet
    links live, which is the root of the styling loss this XML path exists to
    repair.
    """
    parent = node.parent
    while parent is not None:
        name = getattr(parent, "name", None)
        if name and name.lower() in _EXCLUDED_STRING_PARENTS:
            return True
        parent = parent.parent
    return False


def runs_from_nodes(nodes: List[NavigableString]) -> List[DomRun]:
    """Build local-offset :class:`DomRun`\\ s from an already-enumerated content-
    string node list, in the same order/offset scheme :func:`_spine_item_runs`
    uses.

    Public and separated from parsing so the read-along builder can run this
    exact algorithm a second time over a spine item's ORIGINAL archive bytes
    (parsed with :func:`parse_original_spine_xml` rather than ebooklib's lossy
    reconstruction) and get directly comparable ``node_offset_start``/
    ``node_offset_end`` values -- those are computed fresh from each node's own
    (unstripped) string, so they are only ever valid relative to the exact
    node list they were built from. Reusing offsets computed against one
    document's nodes against a *different* document's (even structurally
    equivalent) nodes would silently misplace a marker if the two documents'
    whitespace serialization differs, which is why this is a function on a node
    list rather than something baked into the offsets stored anywhere.
    """
    runs: List[DomRun] = []
    local_idx = 0
    for node_index, node in enumerate(nodes):
        raw = str(node)
        stripped = raw.strip()
        if not stripped:
            continue
        leading = len(raw) - len(raw.lstrip())
        node_offset_start = leading
        node_offset_end = leading + len(stripped)

        if runs:
            local_idx += 1  # the single-space separator emitted before this run
        start = local_idx
        end = start + len(stripped)
        runs.append(DomRun(
            node_index=node_index,
            node_offset_start=node_offset_start,
            node_offset_end=node_offset_end,
            text=stripped,
            start=start,
            end=end,
        ))
        local_idx = end
    return runs


def _spine_item_runs(content: Union[str, bytes]) -> Tuple[List[DomRun], int, str]:
    """Build provenance-carrying runs for one spine item's raw XHTML ``content``.

    Returns ``(runs, node_count, item_text)`` where ``item_text`` is
    ``" ".join(run.text for run in runs)`` -- the reconstruction of what
    ``soup.get_text(separator=' ', strip=True)`` would have returned for the same
    ``content``. Offsets in the returned runs are *local* to this item (0-based);
    the caller shifts them into the book's global char space.
    """
    soup = BeautifulSoup(content, 'html.parser')
    nodes = content_string_nodes(soup)
    runs = runs_from_nodes(nodes)
    item_text = " ".join(run.text for run in runs)
    return runs, len(nodes), item_text


def _resolve_named_entity(match: "re.Match") -> str:
    """Replace one named HTML entity reference with numeric character
    reference(s) an XML parser can resolve without a DTD.

    A bare XML parser only understands the five entities in
    :data:`_XML_BUILTIN_ENTITY_NAMES`; everything else (``&nbsp;``, ``&mdash;``,
    ``&hellip;`` ...) requires either network/DTD resolution (which
    :func:`parse_original_spine_xml` deliberately never does) or pre-conversion
    to the character it names. Real-world EPUB content documents -- especially
    ones produced by tools like ``pdftohtml`` -- routinely use these bare,
    un-substituted, without declaring them in an internal DTD subset. Left
    unresolvable names untouched (returns the original match) rather than
    guessing; the caller's own text-equality verification against
    ``extract_text_and_map``'s combined text will catch the resulting parse
    failure or mismatch and fall back to the lossy reconstructed-content path
    for that one spine item.
    """
    name = match.group(1)
    if name in _XML_BUILTIN_ENTITY_NAMES:
        return match.group(0)
    resolved = html.entities.html5.get(name + ";")
    if resolved is None:
        return match.group(0)
    return "".join(f"&#{ord(ch)};" for ch in resolved)


def escape_named_html_entities(content: bytes) -> bytes:
    """Rewrite named HTML entity references in ``content`` to numeric character
    references, leaving XML's five predefined entities alone.

    Read-along fidelity (Finding 1 of the independent review) requires parsing
    a spine item's ORIGINAL archive bytes with a case- and attribute-preserving
    **XML** parser rather than the HTML-mode parser ``extract_text_and_map``
    uses -- HTML mode is what silently lowercases XML-cased attributes like
    ``viewBox``. A bare XML parser, though, only knows the five entities in
    :data:`_XML_BUILTIN_ENTITY_NAMES` unless it resolves an external/internal
    DTD; real EPUB content documents routinely use other named entities
    (``&nbsp;``, ``&mdash;``, ...) without declaring them. This pre-pass makes
    those parseable without ever contacting a network for a DTD.

    ``content`` is decoded as UTF-8 (EPUB content documents are required to be
    UTF-8 or UTF-16, and this repo already treats spine content as UTF-8
    elsewhere) with ``errors='replace'`` so a decoding hiccup degrades rather
    than raises here -- the caller's downstream text-equality check is what
    actually gates whether the result is trustworthy.
    """
    text = content.decode("utf-8", "replace")
    text = _NAMED_ENTITY_RE.sub(_resolve_named_entity, text)
    return text.encode("utf-8")


def parse_original_spine_xml(content: bytes) -> Optional[BeautifulSoup]:
    """Parse a spine item's ORIGINAL (un-reconstructed) archive bytes with a
    case- and attribute-preserving XML parser.

    ``extract_text_and_map`` (and this module's own :func:`_spine_item_runs`)
    parse ``item.get_content()`` -- ebooklib's own from-scratch reconstruction
    of the document, built by re-parsing the original bytes in **HTML** mode
    (which lowercases every tag/attribute name, since HTML is case-insensitive)
    and discarding the original ``<head>``'s stylesheet links and the original
    ``<body>``'s own attributes (see ``EpubHtml.get_content``). None of that
    reconstruction is fit to ship to a reader -- Finding 1 of the independent
    review of this feature. This function instead parses the ORIGINAL bytes
    with bs4's ``lxml-xml`` builder, which is namespace-aware and preserves
    case exactly, so ``viewBox``/``linearGradient``/etc. survive untouched.

    Returns ``None`` -- never raises -- on any parse failure (including a
    document that is not well-formed XML at all, which some real-world "XHTML"
    content documents are not): the caller falls back to the lossy
    reconstructed-content path for that one spine item rather than risk
    injecting a marker at a node index computed against a document structure
    that may not correspond to this one.
    """
    try:
        return BeautifulSoup(escape_named_html_entities(content), "lxml-xml")
    except Exception as e:
        logger.warning(
            "Could not parse original spine XML for read-along fidelity: %s",
            e, exc_info=True,
        )
        return None


def original_body_scope(soup: BeautifulSoup) -> Union[BeautifulSoup, Tag]:
    """The ``<body>`` element of ``soup``, or ``soup`` itself if none is found.

    Content-string node enumeration for the ORIGINAL-bytes path is scoped to
    ``<body>`` (rather than the whole document, as ``extract_text_and_map``'s
    HTML-mode parse effectively is) because a spine item read via
    ``ebooklib.epub.read_epub`` always has ``item.title == ""`` (nothing in the
    reader ever sets it per-item), so ``EpubHtml.get_content()``'s reconstructed
    ``<head>`` never contains a ``<title>`` -- its only contents are self-closing
    ``<meta>``/``<link>`` tags, which contribute zero content-string nodes.
    The reconstructed document's node list is therefore already exactly its
    ``<body>``'s node list; scoping the original parse the same way keeps the
    two directly comparable by node index, and additionally sidesteps the
    original document's own (possibly real) ``<title>`` text, which would
    otherwise shift every subsequent node index.
    """
    body = soup.find("body")
    return body if body is not None else soup


def build_dom_anchor_map(parser: "EbookParser", filepath: Union[str, Path]) -> List[SpineDomMap]:
    """Build the DOM-anchored run map for an EPUB, spine item by spine item.

    Calls ``parser.extract_text_and_map(filepath)`` to get the reference combined
    text and ``spine_map`` (an ordinary cached call, identical to any of that
    method's other callers -- this never bypasses or invalidates its cache), then
    re-parses each spine item's already-captured ``content`` bytes to recover
    per-run node provenance. Raises ``ValueError`` if a rebuilt item's text does
    not match the corresponding slice of the reference combined text exactly --
    that would mean this module's replication of bs4's algorithm has drifted from
    the reference implementation, and silently returning wrong provenance would
    misplace every downstream SMIL marker for that item.

    :param parser: the ``EbookParser`` instance to source book text/spine data from.
    :param filepath: path to the EPUB, exactly as accepted by
        ``EbookParser.extract_text_and_map``.
    :return: one ``SpineDomMap`` per spine item present in ``extract_text_and_map``'s
        ``spine_map`` (spine entries it skips -- missing manifest item, non-document
        type -- are absent here too, identically).
    """
    combined_text, spine_map = parser.extract_text_and_map(filepath)

    dom_maps: List[SpineDomMap] = []
    for entry in spine_map:
        runs, node_count, item_text = _spine_item_runs(entry["content"])
        start = entry["start"]
        end = entry["end"]

        expected = combined_text[start:end]
        if item_text != expected:
            first_diff = next(
                (i for i, (a, b) in enumerate(zip(item_text, expected)) if a != b),
                min(len(item_text), len(expected)),
            )
            logger.error(
                "DOM anchor mismatch for spine item %s (href=%s) in '%s': "
                "rebuilt text diverges from extract_text_and_map at local offset %d",
                entry.get("spine_index"), entry.get("href"), filepath, first_diff,
            )
            raise ValueError(
                f"DOM-anchored reconstruction mismatch for spine_index="
                f"{entry.get('spine_index')} href={entry.get('href')!r} in {filepath!r} "
                f"at local offset {first_diff}"
            )

        global_runs = [
            DomRun(
                node_index=r.node_index,
                node_offset_start=r.node_offset_start,
                node_offset_end=r.node_offset_end,
                text=r.text,
                start=start + r.start,
                end=start + r.end,
            )
            for r in runs
        ]
        dom_maps.append(SpineDomMap(
            spine_index=entry["spine_index"],
            href=entry["href"],
            start=start,
            end=end,
            runs=global_runs,
            node_count=node_count,
        ))

    return dom_maps


def reconstruct_text(dom_map: List[SpineDomMap]) -> str:
    """Rebuild the full combined text from a DOM anchor map.

    Mirrors ``" ".join(full_text_parts)`` in ``extract_text_and_map`` exactly,
    including the inter-item single-space gap even when an item's own text is
    empty. Intended for tests/verification -- the result should be byte-identical
    to ``extract_text_and_map(filepath)[0]`` for the same book.
    """
    parts = []
    for item in dom_map:
        parts.append(" ".join(run.text for run in item.runs))
    return " ".join(parts)


def locate_offset(dom_map: List[SpineDomMap], offset: int) -> Optional[Tuple[int, int, int]]:
    """Map a global char offset to ``(spine_index, node_index, node_offset)``.

    ``offset`` is in the same character space as ``extract_text_and_map``'s
    combined text. Returns ``None`` when ``offset`` falls outside every spine
    item's range, or lands in a synthetic separator gap -- the single joining
    space between spine items, or between two runs within an item -- which has no
    corresponding position in the original XHTML.

    :param dom_map: the result of :func:`build_dom_anchor_map`.
    :param offset: a 0-based character offset into the combined text.
    :return: ``(spine_index, node_index, node_offset)`` or ``None``.
    """
    if not dom_map or offset < 0:
        return None

    starts = [item.start for item in dom_map]
    i = bisect_right(starts, offset) - 1
    if i < 0:
        return None
    item = dom_map[i]
    if offset >= item.end:
        return None  # inter-item separator gap (or past the end of the book)

    run_starts = [run.start for run in item.runs]
    j = bisect_right(run_starts, offset) - 1
    if j < 0:
        return None
    run = item.runs[j]
    if offset >= run.end:
        return None  # inter-run separator gap within this item

    node_offset = run.node_offset_start + (offset - run.start)
    return item.spine_index, run.node_index, node_offset
