from types import SimpleNamespace

from src.services.reading_position_preview import build_reading_position_preview


class FakeParser:
    def __init__(self, text="abcdefghijklmnopqrstuvwxyz " * 30):
        self.text = text
        self.xpath_result = None
        self.cfi_result = None
        self.xpath_calls = []
        self.cfi_calls = []

    def resolve_book_path(self, filename):
        if filename == "missing.epub":
            raise FileNotFoundError(filename)
        return filename

    def extract_text_and_map(self, _path):
        return self.text, [{"start": 0, "end": len(self.text)}]

    def resolve_xpath_to_index(self, filename, xpath):
        self.xpath_calls.append((filename, xpath))
        return self.xpath_result

    def resolve_cfi_to_index(self, filename, cfi):
        self.cfi_calls.append((filename, cfi))
        return self.cfi_result


class FakeAlignment:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def get_char_for_time(self, abs_id, timestamp):
        self.calls.append((abs_id, timestamp))
        return self.result


def _book(filename="book.epub", **kwargs):
    values = {
        "abs_id": "book-1",
        "original_ebook_filename": filename,
        "ebook_filename": filename,
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def _state(client="kosync", percentage=0.25, **kwargs):
    values = {
        "client_name": client,
        "percentage": percentage,
        "timestamp": None,
        "xpath": None,
        "cfi": None,
        "last_updated": 100.0,
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def test_xpath_is_preferred_and_marker_context_is_bounded():
    parser = FakeParser()
    parser.xpath_result = 260
    state = _state(xpath="/body/DocFragment[2]/body/p[3]/text().0")

    result = build_reading_position_preview(
        book=_book(), states=[state], last_leader="KoSync:kindle", ebook_parser=parser,
        context_chars=120,
    )

    assert result["status"] == "exact"
    assert result["confidence"] == "Exact · XPath"
    assert result["source"] == "KoSync"
    assert result["percentage"] == 25.0
    assert parser.xpath_calls
    assert parser.cfi_calls == []
    assert len(result["before"]) <= 120
    assert len(result["after"]) <= 120


def test_kavita_leader_uses_exact_xpath_and_display_label():
    parser = FakeParser()
    parser.xpath_result = 260
    state = _state(
        client="kavita",
        percentage=0.42,
        xpath="/body/DocFragment[2]/body/p[3]/text().0",
    )

    result = build_reading_position_preview(
        book=_book(), states=[state], last_leader="Kavita", ebook_parser=parser,
    )

    assert result["status"] == "exact"
    assert result["source"] == "Kavita"
    assert result["percentage"] == 42.0
    assert parser.xpath_calls


def test_cfi_is_used_when_xpath_is_absent():
    parser = FakeParser()
    parser.cfi_result = 150
    state = _state(cfi="epubcfi(/6/4!/4/2:3)")

    result = build_reading_position_preview(
        book=_book(), states=[state], last_leader="kosync", ebook_parser=parser,
    )

    assert result["status"] == "exact"
    assert result["confidence"] == "Exact · CFI"
    assert parser.cfi_calls == [("book.epub", "epubcfi(/6/4!/4/2:3)")]


def test_audio_leader_uses_stored_alignment_instead_of_linear_percentage():
    parser = FakeParser()
    alignment = FakeAlignment(result=420)
    audio = _state(client="abs", percentage=0.10, timestamp=987.5, last_updated=10)
    newer_but_not_leader = _state(client="kosync", percentage=0.80, last_updated=99)

    result = build_reading_position_preview(
        book=_book(),
        states=[newer_but_not_leader, audio],
        last_leader="ABS",
        ebook_parser=parser,
        alignment_service=alignment,
    )

    assert result["status"] == "mapped"
    assert result["confidence"] == "Mapped · audio alignment"
    assert result["source"] == "Audiobookshelf"
    assert alignment.calls == [("book-1", 987.5)]


def test_failed_precise_locator_degrades_visibly_to_percentage_estimate():
    parser = FakeParser()
    state = _state(
        percentage=0.50,
        xpath="/body/DocFragment[99]/body/p[1].0",
        cfi="epubcfi(/6/999!/4/2:0)",
    )

    result = build_reading_position_preview(
        book=_book(), states=[state], last_leader="KoSync", ebook_parser=parser,
    )

    assert result["status"] == "approximate"
    assert result["confidence"] == "Approximate · percentage"
    assert "XPath and CFI could not be resolved" in result["message"]
    assert result["before"] or result["after"]


def test_non_cfi_locator_is_not_misrepresented_as_exact_cfi():
    parser = FakeParser()
    state = _state(cfi='{"href":"chapter.xhtml","locations":{"progression":0.3}}')

    result = build_reading_position_preview(
        book=_book(), states=[state], last_leader="kosync", ebook_parser=parser,
    )

    assert result["status"] == "approximate"
    assert result["confidence"] == "Approximate · percentage"
    assert parser.cfi_calls == []


def test_missing_ebook_is_unavailable_without_trying_to_guess_text():
    parser = FakeParser()
    state = _state(percentage=0.75)

    result = build_reading_position_preview(
        book=_book("missing.epub"), states=[state], last_leader="kosync", ebook_parser=parser,
    )

    assert result["status"] == "unavailable"
    assert result["confidence"] == "Unavailable"
    assert result["before"] == ""
    assert result["after"] == ""
    assert result["percentage"] == 75.0


def test_without_session_leader_newest_state_is_used_as_display_fallback():
    parser = FakeParser()
    older = _state(client="kosync", percentage=0.1, last_updated=10)
    newer = _state(client="bookorbit", percentage=0.7, last_updated=20)

    result = build_reading_position_preview(
        book=_book(), states=[older, newer], last_leader=None, ebook_parser=parser,
    )

    assert result["source"] == "BookOrbit"
    assert result["percentage"] == 70.0
    assert result["status"] == "approximate"
