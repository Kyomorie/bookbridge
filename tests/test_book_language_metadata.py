from types import SimpleNamespace

from src.api.cwa_client import CWAClient
from src.services.audio_source_adapters import ABSAudioSourceAdapter
from src.services.suggestions_service import SuggestionsService


def test_cwa_opds_parser_preserves_dcterms_language():
    client = object.__new__(CWAClient)
    client.base_url = "http://cwa.test"
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom" xmlns:dcterms="http://purl.org/dc/terms/">
      <entry>
        <id>http://cwa.test/opds/book/42</id>
        <title>Example Book</title>
        <author><name>Example Author</name></author>
        <dcterms:language>deu</dcterms:language>
        <link rel="http://opds-spec.org/acquisition" type="application/epub+zip" href="/opds/download/42/epub/" />
      </entry>
    </feed>"""

    result = client._parse_opds(xml)

    assert len(result) == 1
    assert result[0]["language"] == "deu"


def test_cwa_opds_parser_uses_empty_language_when_missing():
    client = object.__new__(CWAClient)
    client.base_url = "http://cwa.test"
    xml = """<feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>http://cwa.test/opds/book/7</id>
        <title>Unknown Language</title>
        <author><name>Example Author</name></author>
        <link rel="http://opds-spec.org/acquisition" type="application/epub+zip" href="/opds/download/7/epub/" />
      </entry>
    </feed>"""

    result = client._parse_opds(xml)

    assert result[0]["language"] == ""


class _FakeABSClient:
    def search_audiobooks(self, query, library_id=None):
        return [{
            "id": "abs-1",
            "media": {
                "metadata": {
                    "title": "Example Book",
                    "authorName": "Example Author",
                    "language": "eng",
                },
                "duration": 123.0,
            },
        }]

    def is_configured(self):
        return True


def test_abs_audio_adapter_preserves_existing_language(monkeypatch):
    monkeypatch.setattr(ABSAudioSourceAdapter, "_parse_library_scope", staticmethod(lambda: None))
    result = ABSAudioSourceAdapter(_FakeABSClient()).search("Example")

    assert len(result) == 1
    assert result[0].language == "eng"


def _suggestions_service():
    return SuggestionsService(
        database_service=SimpleNamespace(),
        container=SimpleNamespace(),
        manager=SimpleNamespace(),
        get_audiobooks_conditionally=lambda: [],
        get_searchable_ebooks=lambda _query: [],
        audiobook_matches_search=lambda _ab, _text: False,
        get_abs_author=lambda _ab: "",
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None, debug=lambda *args, **kwargs: None),
    )


def test_suggestion_candidate_language_is_not_part_of_search_text():
    service = _suggestions_service()
    candidate = SimpleNamespace(
        title="Example Book",
        authors="Example Author",
        source="CWA",
        source_id="42",
        name="cwa_42.epub",
        display_name="Example Book - Example Author",
        language="deu",
        path=None,
        abs_identifier=None,
        booklore_id=None,
    )

    prepared = service._prepare_candidate_pool([candidate])[0]
    match = service._match_from_pool(prepared, 91.25)

    assert prepared["language"] == "deu"
    assert prepared["search_text"] == "Example Book Example Author"
    assert match["language"] == "deu"


def test_suggestion_audio_language_supports_normalized_and_raw_abs_records():
    service = _suggestions_service()

    assert service._audio_language({"audio_language": "deu"}) == "deu"
    assert service._audio_language({"media": {"metadata": {"language": "fra"}}}) == "fra"


def test_suggestion_shell_keeps_audio_language_without_affecting_title_author():
    service = _suggestions_service()
    suggestion = service._suggestion_shell({
        "audio_source": "ABS",
        "audio_source_id": "abs-1",
        "audio_title": "Example Book",
        "audio_author": "Example Author",
        "audio_language": "eng",
    }, [])

    assert suggestion["audio_language"] == "eng"
    assert suggestion["audio_title"] == "Example Book"
    assert suggestion["audio_author"] == "Example Author"
