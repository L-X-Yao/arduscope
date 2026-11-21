"""fleet.db carries a schema version and refuses a newer one."""

from __future__ import annotations

import sqlite3

import pytest

from arduscope.lib import fleet_history


def _v0_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(fleet_history._SCHEMA)
    conn.execute("PRAGMA user_version=0")
    conn.execute(
        "INSERT INTO airframes(serial, board) VALUES('s', 'board')")
    conn.execute(
        "INSERT INTO flights(serial, log_sha1, health_score) "
        "VALUES('s', 'sha', 88)")
    conn.commit()
    conn.close()


@pytest.mark.fast
def test_a_version_zero_database_upgrades_in_place(tmp_path):
    db = tmp_path / "fleet.db"
    _v0_db(db)

    conn = fleet_history._connect(str(db))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT health_score FROM flights").fetchone()[0] == 88
    finally:
        conn.close()


@pytest.mark.parametrize("open_db", [
    fleet_history._connect,
    fleet_history._read_conn,
])
@pytest.mark.fast
def test_a_newer_schema_is_refused(tmp_path, open_db):
    db = tmp_path / "fleet.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version=2")
    conn.close()

    with pytest.raises(RuntimeError, match="newer than this ArduScope"):
        open_db(str(db))
