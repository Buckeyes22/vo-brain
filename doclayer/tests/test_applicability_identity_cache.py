"""The known-identity cache follows a brain repo switch immediately."""

import os

from doclayer import applicability


def _count_loads(monkeypatch):
    loads = []

    def load():
        loads.append(1)
        return applicability.Identities(frozenset({f"host{len(loads)}"}))

    monkeypatch.setattr(applicability, "_load_identities", load)
    return loads


def test_pointer_file_switch_invalidates_cache_before_ttl(monkeypatch, tmp_path):
    from doclayer.settings import settings

    pointer = tmp_path / "brain-repo"
    monkeypatch.setattr(applicability, "_brain_pointer", lambda: pointer)
    monkeypatch.setattr(settings, "_brain_repo_override", None)
    monkeypatch.delenv("DOC_LAYER_BRAIN_REPO", raising=False)
    loads = _count_loads(monkeypatch)

    pointer.write_text("/repo/one\n")
    first = applicability._cached_identities()
    assert applicability._cached_identities() is first and len(loads) == 1  # TTL cache still works

    pointer.write_text("/repo/two\n")
    os.utime(pointer, ns=(1, 2_000_000_000))  # guard against equal-mtime filesystems
    second = applicability._cached_identities()
    assert second is not first and len(loads) == 2


def test_pointer_removal_and_symlink_retarget_invalidate(monkeypatch, tmp_path):
    from doclayer.settings import settings

    pointer = tmp_path / "brain-repo"
    monkeypatch.setattr(applicability, "_brain_pointer", lambda: pointer)
    monkeypatch.setattr(settings, "_brain_repo_override", None)
    monkeypatch.delenv("DOC_LAYER_BRAIN_REPO", raising=False)
    loads = _count_loads(monkeypatch)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    pointer.symlink_to(tmp_path / "a")
    applicability._cached_identities()
    pointer.unlink()
    pointer.symlink_to(tmp_path / "b")
    applicability._cached_identities()
    assert len(loads) == 2
    pointer.unlink()
    applicability._cached_identities()
    assert len(loads) == 3
