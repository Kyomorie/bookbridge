from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_preview_markup_lives_natively_in_library_card():
    index = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")

    assert 'data-position-preview-toggle' in index
    assert 'class="position-preview-panel"' in index
    assert 'aria-controls="position-preview-' in index
    assert 'role="status"' in index
    assert 'aria-live="polite"' in index
    assert "mapping.sync_mode != 'audiobook_only'" in index
    assert '/static/css/reading-position-preview.css' in index
    assert '/static/js/reading-position-preview.js' in index

    # Keep the feature local to the Library template.  In particular, do not
    # reintroduce the base-template/DOM-relocation pattern hardened away in #393.
    assert 'position-preview' not in base


def test_preview_script_renders_book_text_as_text_only():
    script = (ROOT / "static" / "js" / "reading-position-preview.js").read_text(encoding="utf-8")

    assert '.textContent' in script
    assert '.innerHTML' not in script
    assert "cache: 'no-store'" in script
    assert "encodeURIComponent(absId)" in script
    assert "console.log" not in script
    assert "console.error" not in script


def test_preview_css_uses_existing_bookbridge_tokens_and_has_mobile_layout():
    css = (ROOT / "static" / "css" / "reading-position-preview.css").read_text(encoding="utf-8")

    assert 'var(--panel-soft)' in css
    assert 'var(--border)' in css
    assert 'var(--accent)' in css
    assert ':focus-visible' in css
    assert '@media (max-width: 480px)' in css
