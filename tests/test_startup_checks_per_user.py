"""Startup connection checks must not report unreachable global credentials.

Going from single-user to multi-user copied every per-user credential into the
first admin's account and left the original in the global settings table. Those
global copies have no Settings UI, so they cannot be corrected — and the moment a
user rotates a token they drift. The global client then fails a connection check
using a credential nothing syncs with.

Observed 2026-08-20: Hardcover replaced long-lived JWTs with `hc_pat_` keys. The
admin saved a new key against their account, syncs picked it up, and every restart
still logged `⚠️ 'Hardcover' connection failed` from the stale global JWT with no
way to fix it.

Mirroring the admin's values back over the globals was rejected: an admin who does
not use a service another user does would clobber that user's config. So the check
targets the admin's own clients instead, and touches no stored credential.
"""

import logging
import unittest
from types import SimpleNamespace

from src.sync_manager import SyncManager


class _Client:
    def __init__(self, name, configured=True, fails=None):
        self.name = name
        self._configured = configured
        self._fails = fails
        self.checked = False

    def is_configured(self):
        return self._configured

    def check_connection(self):
        self.checked = True
        if self._fails:
            raise Exception(self._fails)
        return True


class _Registry:
    def __init__(self, bundles, raises=False):
        self._bundles = bundles
        self._raises = raises
        self.requested = []

    def get_clients(self, user_id):
        self.requested.append(user_id)
        if self._raises:
            raise RuntimeError("registry unavailable")
        return SimpleNamespace(sync_clients=self._bundles.get(user_id, {}))


class _DB:
    def __init__(self, users, raises=False):
        self._users = users
        self._raises = raises

    def list_users(self):
        if self._raises:
            raise RuntimeError("db down")
        return self._users


def _user(uid, role="admin", active=1):
    return SimpleNamespace(id=uid, role=role, active=active, username=f"u{uid}")


def _manager(global_clients, database_service=None, registry=None):
    manager = SyncManager.__new__(SyncManager)
    manager.sync_clients = global_clients
    manager.database_service = database_service
    manager.user_client_registry = registry
    # startup_checks continues into CWA and ABS sections after the client loop;
    # these are the collaborators it touches there.
    manager.library_service = None
    manager.abs_client = None
    manager.migration_service = None
    return manager


class StartupCheckTargetTests(unittest.TestCase):
    def test_admin_client_is_preferred_over_the_global_one(self):
        stale_global = _Client("Hardcover", fails="401 invalid_token")
        admin_client = _Client("Hardcover")
        manager = _manager(
            {"Hardcover": stale_global},
            database_service=_DB([_user(1)]),
            registry=_Registry({1: {"Hardcover": admin_client}}),
        )

        manager.startup_checks()

        self.assertTrue(admin_client.checked, "the admin's own client must be checked")
        self.assertFalse(
            stale_global.checked,
            "the unreachable global credential must not be checked",
        )

    def test_the_stale_global_no_longer_produces_a_warning(self):
        """The reported symptom, verbatim."""
        manager = _manager(
            {"Hardcover": _Client("Hardcover", fails="401 invalid_token")},
            database_service=_DB([_user(1)]),
            registry=_Registry({1: {"Hardcover": _Client("Hardcover")}}),
        )

        with self.assertLogs("src.sync_manager", level="DEBUG") as captured:
            manager.startup_checks()

        warnings = [r.getMessage() for r in captured.records if r.levelno >= logging.WARNING]
        self.assertEqual([], warnings, warnings)

    def test_a_service_the_admin_does_not_use_is_skipped_not_failed(self):
        """An admin without CWA must not generate a CWA failure for everyone."""
        manager = _manager(
            {"CWA": _Client("CWA", fails="not configured")},
            database_service=_DB([_user(1)]),
            registry=_Registry({1: {"CWA": _Client("CWA", configured=False)}}),
        )

        with self.assertLogs("src.sync_manager", level="DEBUG") as captured:
            manager.startup_checks()

        levels = [r.levelno for r in captured.records]
        self.assertNotIn(logging.WARNING, levels)
        self.assertTrue(
            any("not configured" in r.getMessage() for r in captured.records)
        )

    def test_a_real_admin_failure_is_still_reported(self):
        """Suppressing the stale global must not suppress genuine breakage."""
        manager = _manager(
            {"Hardcover": _Client("Hardcover")},
            database_service=_DB([_user(1)]),
            registry=_Registry({1: {"Hardcover": _Client("Hardcover", fails="500 boom")}}),
        )

        with self.assertLogs("src.sync_manager", level="WARNING") as captured:
            manager.startup_checks()

        self.assertTrue(
            any("'Hardcover' connection failed" in line for line in captured.output),
            captured.output,
        )

    def test_a_client_with_no_per_user_counterpart_still_uses_the_global(self):
        """Catalog-wide clients are not per-user and must keep being checked."""
        global_client = _Client("ABS")
        manager = _manager(
            {"ABS": global_client},
            database_service=_DB([_user(1)]),
            registry=_Registry({1: {}}),
        )

        manager.startup_checks()

        self.assertTrue(global_client.checked)


class StartupCheckFallbackTests(unittest.TestCase):
    def test_no_registry_falls_back_to_global_clients(self):
        global_client = _Client("Hardcover")
        manager = _manager({"Hardcover": global_client})

        manager.startup_checks()

        self.assertTrue(global_client.checked)

    def test_no_admin_falls_back_to_global_clients(self):
        global_client = _Client("Hardcover")
        manager = _manager(
            {"Hardcover": global_client},
            database_service=_DB([_user(2, role="user")]),
            registry=_Registry({}),
        )

        manager.startup_checks()

        self.assertTrue(global_client.checked)

    def test_an_inactive_admin_is_not_chosen(self):
        global_client = _Client("Hardcover")
        manager = _manager(
            {"Hardcover": global_client},
            database_service=_DB([_user(1, active=0)]),
            registry=_Registry({1: {"Hardcover": _Client("Hardcover")}}),
        )

        manager.startup_checks()

        self.assertTrue(global_client.checked)

    def test_a_broken_database_falls_back_instead_of_crashing_startup(self):
        global_client = _Client("Hardcover")
        manager = _manager(
            {"Hardcover": global_client},
            database_service=_DB([], raises=True),
            registry=_Registry({}),
        )

        manager.startup_checks()

        self.assertTrue(global_client.checked)

    def test_a_broken_registry_falls_back_instead_of_crashing_startup(self):
        global_client = _Client("Hardcover")
        manager = _manager(
            {"Hardcover": global_client},
            database_service=_DB([_user(1)]),
            registry=_Registry({}, raises=True),
        )

        manager.startup_checks()

        self.assertTrue(global_client.checked)


if __name__ == "__main__":
    unittest.main()
