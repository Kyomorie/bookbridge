from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_preview_assets_are_scoped_to_library_page():
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    index = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    assert "{% if active_page == 'library' %}" in base
    assert '/static/css/reading-position-preview.css' in base
    assert '/static/js/reading-position-preview.js' in base
    assert "{% set active_page = 'library' %}" in index


def test_preview_script_builds_local_accessible_ui_without_dom_relocation():
    script = (ROOT / "static" / "js" / "reading-position-preview.js").read_text(encoding="utf-8")

    assert "card.querySelector('.book-info')" in script
    assert "card.dataset.syncMode === 'audiobook_only'" in script
    assert "panel.setAttribute('role', 'status')" in script
    assert "panel.setAttribute('aria-live', 'polite')" in script
    assert "data-position-preview-toggle" in script
    assert "position-preview-panel" in script
    assert "append(button, panel)" in script
    assert "appendChild" not in script


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
    assert 'white-space: pre-line' in css
    assert '@media (max-width: 480px)' in css
