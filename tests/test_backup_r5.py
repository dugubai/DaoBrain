"""Real SQLite copy/restore regressions; all generated data lives in tmp_path."""
from contextlib import closing
from pathlib import Path
import sqlite3
import pytest
from application_hub.store import transaction
from scripts import backup_business_db as backup_script
from scripts.backup_business_db import transfer


def test_consistent_copy_restore_no_overwrite(tmp_path, monkeypatch):
    source = tmp_path / "data.sqlite3"
    monkeypatch.setenv("APP_DB_PATH", str(source))
    with transaction() as db:
        db.execute("INSERT INTO users VALUES('u','username','hash','member',1)")
        db.execute("INSERT INTO sessions VALUES('digest','u',9999999999)")
    backup = tmp_path / "backup.sqlite3"
    restored = tmp_path / "restored.sqlite3"
    before = source.read_bytes()
    assert transfer(str(source), str(backup))["verified"]
    assert source.read_bytes() == before
    backup_before = backup.read_bytes()
    assert transfer(str(backup), str(restored), True)["sessions_invalidated"]
    with closing(sqlite3.connect(str(restored))) as db:
        assert db.execute("SELECT expires FROM sessions").fetchone()[0] == 0
        assert db.execute("SELECT name FROM users").fetchone()[0] == "username"
    assert source.read_bytes() == before
    assert backup.read_bytes() == backup_before
    with pytest.raises(ValueError):
        transfer(str(source), str(backup))
    assert backup.read_bytes() == backup_before
    assert source.read_bytes() == before


@pytest.mark.parametrize("restore", [False, True])
def test_corrupt_source_refuses(tmp_path, monkeypatch, restore):
    source = tmp_path / "bad.sqlite3"
    destination = tmp_path / "copy.sqlite3"
    source.write_bytes(b"not a database")

    def forbidden_publish(*_args, **_kwargs):
        pytest.fail("a corrupt source must never be published")

    monkeypatch.setattr(backup_script.os, "link", forbidden_publish)
    with pytest.raises(sqlite3.DatabaseError):
        transfer(str(source), str(destination), restore)
    assert not destination.exists()
    assert source.read_bytes() == b"not a database"


def database_snapshot(path):
    """Read-only byte-independent oracle for real schema, integrity and all rows."""
    path = Path(path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        schema = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        tables = {}
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            quoted = '"' + name.replace('"', '""') + '"'
            tables[name] = db.execute("SELECT * FROM " + quoted + " ORDER BY rowid").fetchall()
        return {"schema": schema, "tables": tables,
                "integrity": db.execute("PRAGMA integrity_check").fetchall()}


@pytest.fixture
def populated_source(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    monkeypatch.setenv("APP_DB_PATH", str(source))
    with transaction() as db:
        db.execute("INSERT INTO users VALUES('u','synthetic-user','synthetic-hash','member',1)")
        db.executemany("INSERT INTO sessions VALUES(?,?,?)", [
            ("active-one", "u", 9999999999), ("active-two", "u", 9999999998),
            ("expired", "u", 1), ("invalidated", "u", 0),
        ])
        db.execute("INSERT INTO objects VALUES('obj','synthetic','u',3,'draft',?,123)",
                   ('{"note":"synthetic-only"}',))
        db.execute("INSERT INTO events VALUES('event','u','synthetic.create','obj',123)")
        db.execute("INSERT INTO payment_events VALUES('payment','synthetic-digest')")
        # A real CHECK violation in the staged copy makes integrity_check fail.
        db.execute("CREATE TABLE integrity_probe(value INTEGER NOT NULL CHECK(value>0))")
        db.execute("INSERT INTO integrity_probe VALUES(1)")
    return source


def expected_copy(snapshot, restore):
    tables = dict(snapshot["tables"])
    if restore:
        tables["sessions"] = [(digest, user_id, 0) for digest, user_id, _ in tables["sessions"]]
    return {**snapshot, "tables": tables}


@pytest.mark.parametrize("restore", [False, True])
def test_copy_is_verified_and_sessions_committed_before_atomic_publish(
        populated_source, tmp_path, monkeypatch, restore):
    source = populated_source
    destination = tmp_path / "published.sqlite3"
    before_bytes, before_database = source.read_bytes(), database_snapshot(source)
    assert before_database["integrity"] == [("ok",)]
    expected = expected_copy(before_database, restore)
    real_link = backup_script.os.link
    published = []

    def observe_publish(staging, target):
        staging, target = Path(staging), Path(target)
        assert target == destination and not destination.exists()
        assert staging != source and staging != destination and staging.parent == destination.parent
        assert staging.name.startswith(".business-staging-")
        assert published == []
        # This new connection proves expires=0 is committed, not merely pending.
        assert database_snapshot(staging) == expected
        assert source.read_bytes() == before_bytes
        assert database_snapshot(source) == before_database
        published.append(staging)
        real_link(str(staging), str(target))

    real_connect = sqlite3.connect

    def staging_only_connect(path, *args, **kwargs):
        assert str(path) != str(destination)
        assert not str(path).startswith(destination.as_uri())
        return real_connect(path, *args, **kwargs)

    with monkeypatch.context() as publishing:
        publishing.setattr(backup_script.os, "link", observe_publish)
        publishing.setattr(backup_script.sqlite3, "connect", staging_only_connect)
        result = transfer(str(source), str(destination), restore)
    assert len(published) == 1
    assert result["verified"] is True and result["source_preserved"] is True
    assert result["sessions_invalidated"] is restore and result["restored_copy"] is restore
    assert database_snapshot(destination) == expected
    assert source.read_bytes() == before_bytes
    assert database_snapshot(source) == before_database
    # A retained staging name is allowed; never require cleanup of staged files.
    if published[0].exists():
        assert published[0].samefile(destination)


@pytest.mark.parametrize("restore", [False, True])
def test_version_two_source_refuses_without_publish_or_mutation(
        populated_source, tmp_path, monkeypatch, restore):
    source = populated_source
    with closing(sqlite3.connect(str(source))) as db:
        db.execute("UPDATE schema_version SET version=2")
        db.commit()
    before_bytes, before_database = source.read_bytes(), database_snapshot(source)
    destination = tmp_path / "unsupported-copy.sqlite3"

    def forbidden_publish(*_args, **_kwargs):
        pytest.fail("an unsupported schema must never be published")

    monkeypatch.setattr(backup_script.os, "link", forbidden_publish)
    with pytest.raises(ValueError, match="Unsupported schema"):
        transfer(str(source), str(destination), restore)
    assert not destination.exists()
    assert database_snapshot(source) == before_database
    assert source.read_bytes() == before_bytes


@pytest.mark.parametrize("failure,restore,error,match", [
    pytest.param("backup", False, sqlite3.OperationalError, "injected copy failure", id="backup-copy-error"),
    pytest.param("backup", True, sqlite3.OperationalError, "injected copy failure", id="restore-copy-error"),
    pytest.param("integrity", False, ValueError, "Destination integrity check failed", id="backup-corrupt-copy"),
    pytest.param("integrity", True, ValueError, "Destination integrity check failed", id="restore-corrupt-copy"),
    pytest.param("sessions", True, sqlite3.OperationalError, "injected session failure", id="restore-session-error"),
])
def test_staging_failures_never_create_formal_target_or_change_source(
        populated_source, tmp_path, monkeypatch, failure, restore, error, match):
    source = populated_source
    destination = tmp_path / "failed-copy.sqlite3"
    before_bytes, before_database = source.read_bytes(), database_snapshot(source)
    real_connect = sqlite3.connect
    injected, stages = [], []

    class Incoming(sqlite3.Connection):
        def backup(self, target, *args, **kwargs):
            assert not destination.exists()
            super().backup(target, *args, **kwargs)
            assert not destination.exists()
            if failure == "backup":
                injected.append("backup")
                raise sqlite3.OperationalError("injected copy failure")
            if failure == "integrity":
                injected.append("integrity")
                target.execute("PRAGMA ignore_check_constraints=ON")
                target.execute("INSERT INTO integrity_probe VALUES(-1)")
                target.commit()
                target.execute("PRAGMA ignore_check_constraints=OFF")
                # No fabricated PRAGMA result: SQLite diagnoses the damaged data.
                assert target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]

    class Outgoing(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if failure == "sessions" and statement == "UPDATE sessions SET expires=0":
                injected.append("sessions")
                assert not destination.exists()
                raise sqlite3.OperationalError("injected session failure")
            return super().execute(statement, *args, **kwargs)

    def connect(path, *args, **kwargs):
        incoming = kwargs.get("uri", False)
        kwargs["factory"] = Incoming if incoming else Outgoing
        if not incoming:
            stages.append(Path(path))
            assert Path(path) != destination and Path(path) != source
        return real_connect(path, *args, **kwargs)

    def forbidden_publish(*_args, **_kwargs):
        pytest.fail("a failed or damaged staging copy must never be published")

    monkeypatch.setattr(backup_script.sqlite3, "connect", connect)
    monkeypatch.setattr(backup_script.os, "link", forbidden_publish)
    with pytest.raises(error, match=match):
        transfer(str(source), str(destination), restore)
    assert injected == [failure] and len(stages) == 1
    assert stages[0].parent == destination.parent and stages[0].name.startswith(".business-staging-")
    assert not destination.exists()
    assert database_snapshot(source) == before_database
    assert source.read_bytes() == before_bytes


@pytest.mark.parametrize("restore", [False, True])
def test_atomic_publish_failure_leaves_only_staging(
        populated_source, tmp_path, monkeypatch, restore):
    source = populated_source
    destination = tmp_path / "unpublished.sqlite3"
    before_bytes, before_database = source.read_bytes(), database_snapshot(source)
    attempted = []

    def fail_publish(staging, target):
        assert Path(target) == destination and not destination.exists()
        assert database_snapshot(staging) == expected_copy(before_database, restore)
        assert source.read_bytes() == before_bytes
        attempted.append(Path(staging))
        raise OSError("injected link failure")

    monkeypatch.setattr(backup_script.os, "link", fail_publish)
    with pytest.raises(OSError, match="injected link failure"):
        transfer(str(source), str(destination), restore)
    assert len(attempted) == 1
    assert not destination.exists()
    assert database_snapshot(source) == before_database
    assert source.read_bytes() == before_bytes


@pytest.mark.parametrize("restore", [False, True])
def test_publish_race_preserves_existing_target_and_source(
        populated_source, tmp_path, monkeypatch, restore):
    source = populated_source
    destination = tmp_path / "race-target.sqlite3"
    before_bytes, before_database = source.read_bytes(), database_snapshot(source)
    real_link = backup_script.os.link
    competitor = []

    def race_publish(staging, target):
        assert Path(target) == destination and not destination.exists()
        assert database_snapshot(staging) == expected_copy(before_database, restore)
        assert source.read_bytes() == before_bytes
        # A real competing SQLite file appears after the initial exists() check.
        with closing(sqlite3.connect(str(destination))) as db:
            db.executescript("CREATE TABLE competitor(note TEXT); INSERT INTO competitor VALUES('synthetic-race-owner');")
        competitor.append((destination.read_bytes(), database_snapshot(destination)))
        real_link(str(staging), str(target))

    monkeypatch.setattr(backup_script.os, "link", race_publish)
    with pytest.raises(FileExistsError):
        transfer(str(source), str(destination), restore)
    assert len(competitor) == 1
    assert destination.read_bytes() == competitor[0][0]
    assert database_snapshot(destination) == competitor[0][1]
    assert database_snapshot(source) == before_database
    assert source.read_bytes() == before_bytes


@pytest.mark.parametrize("restore", [False, True])
def test_real_constraint_violation_refuses_before_restore_or_publish(
        populated_source, tmp_path, monkeypatch, restore):
    source = populated_source
    with closing(sqlite3.connect(str(source))) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("INSERT INTO integrity_probe VALUES(-1)")
        db.commit()
        db.execute("PRAGMA ignore_check_constraints=OFF")
        assert db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
    before_bytes, before_database = source.read_bytes(), database_snapshot(source)
    # SQLite 3.46 may report ok when reopened with mode=ro. Do not fabricate a
    # source PRAGMA failure: the writable copy must independently detect it.
    assert (-1,) in before_database["tables"]["integrity_probe"]
    destination = tmp_path / "bad-integrity-copy.sqlite3"
    real_connect = sqlite3.connect
    updates = []

    class Outgoing(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if statement == "UPDATE sessions SET expires=0":
                updates.append(statement)
                pytest.fail("restore must not change a constraint-invalid copy")
            return super().execute(statement, *args, **kwargs)

    def connect(path, *args, **kwargs):
        if not kwargs.get("uri", False):
            kwargs["factory"] = Outgoing
        return real_connect(path, *args, **kwargs)

    def forbidden_publish(*_args, **_kwargs):
        pytest.fail("a source failing integrity_check must never be published")

    monkeypatch.setattr(backup_script.sqlite3, "connect", connect)
    monkeypatch.setattr(backup_script.os, "link", forbidden_publish)
    with pytest.raises(ValueError, match="(?:Source|Destination) integrity check failed"):
        transfer(str(source), str(destination), restore)
    assert not destination.exists() and not updates
    assert database_snapshot(source) == before_database
    assert source.read_bytes() == before_bytes


def test_outgoing_connect_failure_closes_real_incoming(populated_source, tmp_path, monkeypatch):
    source = populated_source
    destination = tmp_path / "failed-connect.sqlite3"
    before = source.read_bytes()
    real_connect = sqlite3.connect
    incoming = []

    def connect(path, *args, **kwargs):
        if kwargs.get("uri", False):
            value = real_connect(path, *args, **kwargs)
            incoming.append(value)
            return value
        raise sqlite3.OperationalError("injected outgoing connection failure")

    monkeypatch.setattr(backup_script.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="injected outgoing connection failure"):
        transfer(str(source), str(destination), True)
    assert len(incoming) == 1 and not destination.exists()
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        incoming[0].execute("SELECT 1")
    assert source.read_bytes() == before
