from pathlib import Path
import tempfile

from backend.storage_paths import default_data_dir


def test_vercel_defaults_writable_state_to_tmp(monkeypatch):
    monkeypatch.delenv("ATLAS_DATA_DIR", raising=False)
    monkeypatch.setenv("VERCEL", "1")

    assert default_data_dir() == Path(tempfile.gettempdir()) / "atlas"


def test_explicit_data_dir_overrides_vercel_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path / "atlas-data"))

    assert default_data_dir() == tmp_path / "atlas-data"
