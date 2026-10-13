"""Owner retirement preserves bytes/budgets and leaves only the client outbox writable."""

import os
import stat

import pytest

from doclayer.dispatcher import GapDispatcher
from doclayer.gap_budget import ResourceBudget, today
from doclayer.gap_migration import install_export, inventory, retire_and_export
from doclayer.gaps import GapError, GapLedger


def seed(root):
    ledger = GapLedger(root)
    for i in range(3):
        ledger.submit_event(
            need_class="missing_coverage",
            subject="unit",
            representative_question=f"Question {i}",
            event_key=f"event-{i}",
        )
    budget = ResourceBudget(root)
    budget.consume_model(kind="discovery")
    (root / "outbox").mkdir()
    (root / "outbox" / "unflushed.json").write_text('{"event":"preserve"}')
    return ledger


def restore_modes(root, journal):
    os.chmod(root, journal["original"]["root_mode"])
    for rel, row in journal["original"]["entries"].items():
        os.chmod(root / rel, row["mode"])


def test_retired_owner_readable_all_mutations_fail_closed(tmp_path):
    root = tmp_path / "source"
    ledger = seed(root)
    before = inventory(root)
    journal = retire_and_export(
        root,
        tmp_path / "journal",
        owner="worker1:owner",
        legacy_clients_reconnected=True,
    )
    try:
        assert len(ledger.list_records()) == 3
        assert ledger.summary()["total"] == 3
        assert ResourceBudget(root).load(today())["model_calls"] == 1
        record = ledger.list_records()[0]
        for operation in [
            lambda: ledger.save(record),
            lambda: ledger.update(record.gap_id, lambda r: None),
            lambda: ledger.submit_event(need_class="missing_coverage", representative_question="new"),
            lambda: ResourceBudget(root).consume_model(kind="discovery"),
            lambda: GapDispatcher(ledger).set_paused(True),
        ]:
            with pytest.raises(GapError, match="owner moved"):
                operation()
        (root / "outbox" / "new.json").write_text("{}")
        assert stat.S_IMODE(root.stat().st_mode) & 0o222 == 0
        assert stat.S_IMODE((root / "gaps").stat().st_mode) & 0o222 == 0
        for rel, row in before["entries"].items():
            if "sha256" in row:
                assert inventory(root)["entries"][rel]["sha256"] == row["sha256"]
        destination = tmp_path / "destination"
        installed = install_export(
            tmp_path / "journal" / "owner-state.tar",
            tmp_path / "journal" / "journal.json",
            destination,
        )
        assert installed["phase"] == "installed"
        assert inventory(destination)["entries"] == journal["original"]["entries"]
        assert len(GapLedger(destination).list_records()) == 3
        assert not (destination / "owner-migrated.json").exists()
        assert not (destination / "outbox").exists()
        assert ResourceBudget(destination).load(today())["model_calls"] == 1
        with pytest.raises(GapError, match="already exists"):
            install_export(
                tmp_path / "journal" / "owner-state.tar",
                tmp_path / "journal" / "journal.json",
                destination,
            )
    finally:
        restore_modes(root, journal)


def test_obsolete_clients_require_actual_reconnect_before_freeze(tmp_path):
    root = tmp_path / "source"
    seed(root)
    before = inventory(root)
    with pytest.raises(GapError, match="reconnect"):
        retire_and_export(root, tmp_path / "journal", owner="remote")
    assert inventory(root) == before
    assert not (tmp_path / "journal").exists()


@pytest.mark.parametrize("value", ["not json", "{}"])
def test_corrupt_moved_marker_never_restores_write_authority(tmp_path, value):
    ledger = seed(tmp_path / "source")
    (ledger.root / "owner-migrated.json").write_text(value)
    assert len(ledger.list_records()) == 3
    with pytest.raises(GapError, match="owner moved"):
        ledger.save(ledger.list_records()[0])


def test_filesystem_guard_blocks_legacy_writes_but_real_client_outbox_survives(
    tmp_path,
):
    from doclayer.gap_client import GapClient
    from doclayer.settings import Settings

    root = tmp_path / "source"
    ledger = seed(root)
    journal = retire_and_export(
        root,
        tmp_path / "journal",
        owner="worker1:owner",
        legacy_clients_reconnected=True,
    )
    try:
        if os.geteuid() != 0:
            # Obsolete code knows no marker, but atomic writes/new records fail
            # at the filesystem boundary instead of silently forking ownership.
            with pytest.raises(PermissionError):
                (root / "gaps" / "new-legacy-record").mkdir()
            with pytest.raises(PermissionError):
                ledger.record_path(ledger.list_ids()[0]).write_text("{}")
        config = Settings()
        config.gap_state_dir = root
        config.gap_owner = "ssh"
        client = GapClient(config)
        client.submit_event(
            need_class="missing_coverage",
            representative_question="Fresh observation after retirement",
            event_key="new-owner-event",
            defer=True,
        )
        files = list((root / "outbox").glob("*.json"))
        assert len(files) == 2
        assert any("Fresh observation" in f.read_text() for f in files)
    finally:
        restore_modes(root, journal)


def test_tampered_export_never_creates_destination(tmp_path):
    root = tmp_path / "source"
    seed(root)
    journal = retire_and_export(
        root,
        tmp_path / "journal",
        owner="worker1:owner",
        legacy_clients_reconnected=True,
    )
    try:
        archive = tmp_path / "journal" / "owner-state.tar"
        archive.write_bytes(archive.read_bytes() + b"tampered")
        with pytest.raises(GapError, match="hash mismatch"):
            install_export(
                archive,
                tmp_path / "journal" / "journal.json",
                tmp_path / "remote" / "owner",
            )
        assert not (tmp_path / "remote").exists()
    finally:
        restore_modes(root, journal)


def test_symlink_source_refused_before_any_guard_or_permission_change(tmp_path):
    source = tmp_path / "source"
    seed(source)
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    before = inventory(source)
    with pytest.raises(GapError, match="symbolic link"):
        retire_and_export(alias, tmp_path / "journal", owner="remote", legacy_clients_reconnected=True)
    assert inventory(source) == before
    assert not (tmp_path / "journal").exists()


def test_interrupted_export_resumes_original_snapshot_without_reset(tmp_path, monkeypatch):
    import doclayer.gap_migration as migration

    root = tmp_path / "source"
    ledger = seed(root)
    original_open = migration.tarfile.open
    with monkeypatch.context() as patch:
        patch.setattr(
            migration.tarfile,
            "open",
            lambda *a, **k: (_ for _ in ()).throw(OSError("crash before archive")),
        )
        with pytest.raises(OSError, match="crash"):
            migration.retire_and_export(
                root,
                tmp_path / "journal",
                owner="remote",
                legacy_clients_reconnected=True,
            )
    with pytest.raises(GapError, match="owner moved"):
        ledger.save(ledger.list_records()[0])
    try:
        journal = migration.resume_retirement(tmp_path / "journal")
        assert journal["phase"] == "retired_exported"
        assert ResourceBudget(root).load(today())["model_calls"] == 1
        assert len(ledger.list_records()) == 3
        assert migration.tarfile.open is original_open
        migration.install_export(
            tmp_path / "journal" / "owner-state.tar",
            tmp_path / "journal" / "journal.json",
            tmp_path / "target",
        )
        assert ResourceBudget(tmp_path / "target").load(today())["model_calls"] == 1
    finally:
        restore_modes(root, journal)


def test_pre_activation_rollback_requires_absent_destination_and_retains_outbox(
    tmp_path,
):
    from doclayer.gap_migration import rollback_retirement

    root = tmp_path / "source"
    ledger = seed(root)
    journal = retire_and_export(root, tmp_path / "journal", owner="remote", legacy_clients_reconnected=True)
    with pytest.raises(GapError, match="proven absent"):
        rollback_retirement(tmp_path / "journal")
    rolled = rollback_retirement(tmp_path / "journal", destination_absent=True)
    assert rolled["phase"] == "rolled_back_before_destination"
    assert inventory(root)["entries"] == journal["original"]["entries"]
    assert (root / "outbox" / "unflushed.json").read_text() == '{"event":"preserve"}'
    ledger.save(ledger.list_records()[0])
    assert ResourceBudget(root).load(today())["model_calls"] == 1
