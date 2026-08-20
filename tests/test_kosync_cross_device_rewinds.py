import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
from jinja2 import Environment, FileSystemLoader

from src.api import kosync_server
from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG
from src.utils.time_utils import utcnow


_DOC_HASH = "a" * 32
_ROOT = Path(__file__).resolve().parents[1]


class _FakeDatabase:
    def __init__(self, *, percentage=0.50, device_id="reader-a"):
        self.doc = SimpleNamespace(
            document_hash=_DOC_HASH,
            linked_abs_id=None,
            progress="/body/original",
            percentage=percentage,
            device="KOReader",
            device_id=device_id,
            timestamp=utcnow(),
            user_id=None,
            filename=None,
            source=None,
            booklore_id=None,
            mtime=None,
        )
        self.saved = []
        self.user_progress_updates = []

    def get_kosync_document(self, _document_hash):
        return self.doc

    def get_user_kosync_progress(self, _document_hash, _user_id):
        return self.doc

    def save_kosync_document(self, document):
        self.saved.append((float(document.percentage), document.device_id))
        self.doc = document
        return document

    def upsert_user_kosync_progress(
        self, document_hash, percentage, *, progress, device, device_id,
        timestamp, user_id,
    ):
        self.user_progress_updates.append(
            (document_hash, float(percentage), progress, device, device_id, user_id)
        )

    def get_book_by_kosync_id(self, _document_hash):
        return None


class TestKoSyncCrossDeviceRewinds:
    @staticmethod
    def _put(db, *, percentage, device_id, furthest_wins):
        app = Flask(__name__)
        payload = {
            "document": _DOC_HASH,
            "progress": f"/body/{device_id}/{percentage}",
            "percentage": percentage,
            "device": "KOReader",
            "device_id": device_id,
        }

        with app.test_request_context("/syncs/progress", method="PUT", json=payload):
            with patch.dict(
                os.environ,
                {"KOSYNC_FURTHEST_WINS": furthest_wins},
                clear=False,
            ), patch.object(
                kosync_server, "_database_service", db
            ), patch.object(
                kosync_server, "_flush_stale_kosync_sessions"
            ), patch.object(
                kosync_server, "_record_recent_external_kosync_put"
            ), patch.object(
                kosync_server, "_schedule_auto_discovery"
            ):
                return kosync_server.kosync_put_progress.__wrapped__()

    @staticmethod
    def _render_setting(value):
        env = Environment(loader=FileSystemLoader(_ROOT / "templates"))
        template = env.get_template("_kosync_cross_device_rewinds.html")

        def get_val(key, default=""):
            return value if key == "KOSYNC_FURTHEST_WINS" else default

        return template.render(get_val=get_val)

    def test_setting_is_managed_and_defaults_to_safe_behavior(self):
        assert "KOSYNC_FURTHEST_WINS" in ALL_SETTINGS
        assert DEFAULT_CONFIG["KOSYNC_FURTHEST_WINS"] == "true"

    def test_safe_default_rejects_backward_put_from_different_device(self):
        db = _FakeDatabase(percentage=0.50, device_id="reader-a")

        _response, status = self._put(
            db,
            percentage=0.30,
            device_id="reader-b",
            furthest_wins="true",
        )

        assert status == 200
        assert float(db.doc.percentage) == 0.50
        assert db.saved == []
        assert db.user_progress_updates == []

    def test_safe_default_still_accepts_intentional_rewind_on_same_device(self):
        db = _FakeDatabase(percentage=0.50, device_id="reader-a")

        _response, status = self._put(
            db,
            percentage=0.30,
            device_id="reader-a",
            furthest_wins="true",
        )

        assert status == 200
        assert float(db.doc.percentage) == 0.30
        assert db.saved == [(0.30, "reader-a")]
        assert db.user_progress_updates[0][1] == 0.30

    def test_opt_in_accepts_backward_put_from_different_device(self):
        db = _FakeDatabase(percentage=0.50, device_id="reader-a")

        _response, status = self._put(
            db,
            percentage=0.30,
            device_id="reader-b",
            furthest_wins="false",
        )

        assert status == 200
        assert float(db.doc.percentage) == 0.30
        assert db.saved == [(0.30, "reader-b")]
        assert db.user_progress_updates[0][1] == 0.30

    def test_ui_renders_safe_default_and_explicit_opt_in(self):
        safe = self._render_setting("true")
        opt_in = self._render_setting("false")

        assert 'name="KOSYNC_FURTHEST_WINS"' in safe
        assert 'value="true" selected' in safe
        assert 'value="false" selected' not in safe
        assert 'value="false" selected' in opt_in
        assert 'value="true" selected' not in opt_in
        assert "out-of-date second" in opt_in

    def test_setting_partial_is_mounted_before_page_scripts(self):
        base = (_ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        include = '{% include "_kosync_cross_device_rewinds.html" %}'
        scripts = "{% block scripts %}{% endblock %}"

        assert "active_page == 'settings'" in base
        assert include in base
        assert base.index(include) < base.index(scripts)
