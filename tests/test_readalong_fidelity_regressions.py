from bs4 import BeautifulSoup

from src.services.readalong_builder import (
    _inject_markers_into_original,
    _resolve_spine_injection_target,
    _verify_marker_injection,
)
from src.utils.ebook_dom_map import (
    SpineDomMap,
    content_string_nodes,
    original_body_scope,
    parse_original_spine_xml,
    runs_from_nodes,
)


def _reference(content: bytes) -> tuple[SpineDomMap, str]:
    soup = BeautifulSoup(content, "html.parser")
    runs = runs_from_nodes(content_string_nodes(soup))
    text = " ".join(run.text for run in runs)
    return SpineDomMap(1, "OEBPS/ch1.xhtml", 0, len(text), runs, len(content_string_nodes(soup))), text


def test_original_mapping_preserves_body_text_and_uses_child_structure():
    canonical = b"<html><body><p>Repeat once.</p><p>Repeat twice.</p></body></html>"
    original = (
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head><link rel="stylesheet" href="book.css"/></head>'
        b'<body data-book="keep">Leading text. <p>Repeat once.</p><p>Repeat twice.</p></body></html>'
    )
    ref, expected = _reference(canonical)

    resolved = _resolve_spine_injection_target(original, ref, expected, canonical)

    assert resolved is not None
    dom_entry, soup, nodes = resolved
    assert [run.node_index for run in dom_entry.runs] == [1, 2]
    modified = _inject_markers_into_original(
        soup, nodes, [(dom_entry.runs[0].node_index, 0, "c1-s0")]
    )
    assert b'Leading text. ' in modified
    assert b'data-book="keep"' in modified
    assert b'href="book.css"' in modified


def test_original_mapping_refuses_same_text_with_different_structure():
    canonical = b"<html><body><p>Repeat.</p><p>Repeat.</p></body></html>"
    original = b"<html><body><p>Repeat.</p><div><p>Repeat.</p></div></body></html>"
    ref, expected = _reference(canonical)

    assert _resolve_spine_injection_target(original, ref, expected, canonical) is None


def test_original_mapping_resolves_duplicate_text_runs_under_same_parent():
    """A real-book shape found on 'Buy a Bullet' (Gregg Hurwitz): a single
    ``<p>`` with two lone em-dash text nodes flanking an inline ``<span>``
    (a dialogue interruption, e.g. ``<p>&mdash;<span>...</span>&mdash;</p>``).

    Both dashes strip to the identical text under the identical immediate
    parent ``<p>``, so the structural-path lookup key (parent-tag ancestry +
    text) collides for both. Requiring exactly one candidate for that key
    refused the whole spine item -- and therefore the whole build -- even
    though the paragraph is completely, unambiguously matchable by document
    order. This must resolve, and the two identical runs must land on their
    own distinct, correctly-ordered original nodes rather than colliding on
    one or refusing outright.
    """
    canonical = (
        b"<html><body><p>\xe2\x80\x94<span>thank God thank God thank</span>"
        b"\xe2\x80\x94</p></body></html>"
    )
    original = (
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p class="tx">'
        b'\xe2\x80\x94<span class="epub-i">thank God thank God thank</span>'
        b'\xe2\x80\x94</p></body></html>'
    )
    ref, expected = _reference(canonical)

    resolved = _resolve_spine_injection_target(original, ref, expected, canonical)

    assert resolved is not None
    dom_entry, soup, nodes = resolved
    assert [run.text for run in dom_entry.runs] == [
        "—", "thank God thank God thank", "—",
    ]
    # The two identical em-dash runs must map to their own distinct,
    # document-order-increasing original nodes (the one before the <span>,
    # then the one after it) -- never the same node twice.
    node_indices = [run.node_index for run in dom_entry.runs]
    assert node_indices == sorted(node_indices)
    assert len(set(node_indices)) == len(node_indices)


def test_marker_verification_merges_empty_marker_split_before_text_compare():
    original = b"<html><body><p>First sentence.\xc2\xa0 Second sentence.</p></body></html>"
    soup = parse_original_spine_xml(original)
    assert soup is not None
    nodes = content_string_nodes(original_body_scope(soup))
    modified = _inject_markers_into_original(soup, nodes, [(0, len("First sentence."), "c1-s0")])

    _verify_marker_injection(original, modified, spine_index=1, href="ch1.xhtml")


def test_prefixed_xhtml_gets_marker_in_the_source_namespace():
    original = (
        b'<h:html xmlns:h="http://www.w3.org/1999/xhtml"><h:body><h:p>First sentence.</h:p>'
        b'</h:body></h:html>'
    )
    soup = parse_original_spine_xml(original)
    assert soup is not None
    nodes = content_string_nodes(original_body_scope(soup))
    modified = _inject_markers_into_original(soup, nodes, [(0, 0, "c1-s0")])

    assert b"<h:span id=\"c1-s0\"" in modified
