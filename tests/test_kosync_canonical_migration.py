import importlib.util
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = Path(__file__).resolve().parents[1] / 'alembic/versions/f7c2a9d41b63_add_kosync_xpath_order_cache.py'
spec = importlib.util.spec_from_file_location('canonical_migration', MIGRATION)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine('sqlite:///:memory:')

    def tearDown(self):
        self.engine.dispose()

    def _run(self, fn):
        with self.engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            operations = Operations(ctx)
            old_op = mod.op
            try:
                mod.op = operations
                fn()
            finally:
                mod.op = old_op

    def test_upgrade_creates_persistent_pair_cache(self):
        self._run(mod.upgrade)
        inspector = sa.inspect(self.engine)
        self.assertIn(mod._TABLE, inspector.get_table_names())
        columns = {c['name'] for c in inspector.get_columns(mod._TABLE)}
        self.assertTrue({
            'key_hash', 'document_hash', 'filename', 'device_xpath', 'synced_xpath',
            'device_index', 'synced_index', 'file_key', 'updated_at',
        }.issubset(columns))
        nullable = {c['name']: c['nullable'] for c in inspector.get_columns(mod._TABLE)}
        self.assertFalse(nullable['device_index'])
        self.assertFalse(nullable['synced_index'])
        indexes = {idx['name'] for idx in inspector.get_indexes(mod._TABLE)}
        self.assertIn('ix_kosync_xpath_order_cache_document_hash', indexes)
        self.assertIn('ix_kosync_xpath_order_cache_updated_at', indexes)

    def test_upgrade_is_idempotent(self):
        self._run(mod.upgrade)
        self._run(mod.upgrade)
        self.assertIn(mod._TABLE, sa.inspect(self.engine).get_table_names())

    def test_downgrade_removes_cache_table(self):
        self._run(mod.upgrade)
        self._run(mod.downgrade)
        self.assertNotIn(mod._TABLE, sa.inspect(self.engine).get_table_names())

    def test_orm_model_matches_the_shipped_migration(self):
        """The table shipped migration-only, with no model, so `create_all` built a
        schema without it. A model exists now; if the two ever drift, a migrated
        install and a freshly created one stop agreeing."""
        from src.db.models import Base

        self._run(mod.upgrade)
        migrated = sa.inspect(self.engine)
        mig_cols = [
            (c['name'], str(c['type']), c['nullable'])
            for c in migrated.get_columns(mod._TABLE)
        ]
        mig_indexes = sorted(i['name'] for i in migrated.get_indexes(mod._TABLE))

        model_engine = sa.create_engine('sqlite:///:memory:')
        try:
            Base.metadata.create_all(model_engine)
            built = sa.inspect(model_engine)
            self.assertIn(mod._TABLE, built.get_table_names())
            model_cols = [
                (c['name'], str(c['type']), c['nullable'])
                for c in built.get_columns(mod._TABLE)
            ]
            model_indexes = sorted(i['name'] for i in built.get_indexes(mod._TABLE))
            uniques = {u['name'] for u in built.get_unique_constraints(mod._TABLE)}
        finally:
            model_engine.dispose()

        self.assertEqual(model_cols, mig_cols)
        self.assertEqual(model_indexes, mig_indexes)
        self.assertIn('uq_kosync_xpath_order_cache_key', uniques)


if __name__ == '__main__':
    unittest.main()
