import os
import tempfile
from pathlib import Path


def test_home_and_xdg_roots_point_into_the_test_temp_dir():
    tmp = Path(tempfile.gettempdir()).resolve()
    assert tmp in Path.home().resolve().parents
    assert tmp in Path("~/.config").expanduser().resolve().parents
    for var in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        assert tmp in Path(os.environ[var]).resolve().parents


def test_curator_config_never_reads_the_real_home():
    from doclayer.curator.config import load_config

    path = Path(os.environ["BRAIN_CURATOR_CONFIG"])
    assert not path.exists()
    assert load_config() == load_config(path)
