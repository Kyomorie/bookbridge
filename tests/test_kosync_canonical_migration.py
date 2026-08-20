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


if __name__ == '__main__':
    unittest.main()
