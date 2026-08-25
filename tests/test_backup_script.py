import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "backup_db.sh"
_BASH = shutil.which("bash")


def _run_backup(data_dir: Path, **extra_env):
    env = os.environ.copy()
    for key in ("BACKUP_DIR", "BOOKBRIDGE_SECRET_KEY", "BOOKBRIDGE_SECRET_KEY_FILE"):
        env.pop(key, None)
    env.update({
        "DATA_DIR": str(data_dir),
        "PYTHON_BIN": sys.executable,
    })
    env.update(extra_env)
    return subprocess.run(
        [_BASH, str(_SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def _create_minimal_database(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(data_dir / "database.db") as db:
        db.execute("CREATE TABLE marker (value TEXT)")
        db.execute("INSERT INTO marker (value) VALUES ('ready')")


@pytest.mark.skipif(_BASH is None, reason="bash is required for the backup helper")
def test_backup_captures_committed_wal_data(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "database.db"
    source = sqlite3.connect(db_path)
    try:
        assert source.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        source.execute("PRAGMA wal_autocheckpoint=0")
        source.execute("CREATE TABLE marker (value TEXT)")
        source.commit()
        source.execute("INSERT INTO marker (value) VALUES ('committed-in-wal')")
        source.commit()

        wal_path = Path(f"{db_path}-wal")
        assert wal_path.exists()
        assert wal_path.stat().st_size > 0

        result = _run_backup(data_dir)
        assert result.returncode == 0, result.stdout + result.stderr

        snapshots = list((data_dir / "backups").glob("abs_kosync_*.db"))
        assert len(snapshots) == 1
        with sqlite3.connect(snapshots[0]) as snapshot:
            rows = snapshot.execute("SELECT value FROM marker").fetchall()
            assert rows == [("committed-in-wal",)]
            assert snapshot.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        source.close()


@pytest.mark.skipif(_BASH is None, reason="bash is required for the backup helper")
def test_backup_copies_default_secret_key(tmp_path):
    data_dir = tmp_path / "data"
    _create_minimal_database(data_dir)
    key_path = data_dir / "secret.key"
    key_path.write_text("test-fernet-key\n", encoding="utf-8")
    key_path.chmod(0o600)

    result = _run_backup(data_dir)
    assert result.returncode == 0, result.stdout + result.stderr

    key_snapshots = list((data_dir / "backups").glob("abs_kosync_*.secret.key"))
    assert len(key_snapshots) == 1
    assert key_snapshots[0].read_text(encoding="utf-8") == "test-fernet-key\n"
    assert key_snapshots[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(_BASH is None, reason="bash is required for the backup helper")
@pytest.mark.parametrize(
    "override",
    [
        {"BOOKBRIDGE_SECRET_KEY": "managed-outside-data"},
        {"BOOKBRIDGE_SECRET_KEY_FILE": "/run/secrets/bookbridge.key"},
    ],
)
def test_backup_does_not_bundle_external_credential_key(tmp_path, override):
    data_dir = tmp_path / "data"
    _create_minimal_database(data_dir)
    (data_dir / "secret.key").write_text("stale-default-key\n", encoding="utf-8")

    result = _run_backup(data_dir, **override)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list((data_dir / "backups").glob("abs_kosync_*.secret.key"))
    assert next(iter(override)) in result.stdout


@pytest.mark.skipif(_BASH is None, reason="bash is required for the backup helper")
def test_backup_fails_without_database(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    result = _run_backup(data_dir)

    assert result.returncode != 0
    assert "Database file not found" in result.stdout
    assert not (data_dir / "backups").exists()


def test_backup_helper_is_packaged_in_container_image():
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY scripts/backup_db.sh /app/scripts/backup_db.sh" in dockerfile
    assert "chmod +x /app/start.sh /app/scripts/backup_db.sh" in dockerfile
