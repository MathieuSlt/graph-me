import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from graph_me import config


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv(config.ENV_OUT, raising=False)
    monkeypatch.delenv(config.ENV_CONFIG, raising=False)


@pytest.fixture
def outside(tmp_path, monkeypatch):
    """A working directory that is not inside a graph-me clone, with a fake HOME."""
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    return home, work


@pytest.fixture
def clone(tmp_path, monkeypatch):
    root = tmp_path / "clone"
    (root / "src" / "deep").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "graph-me"\n')
    monkeypatch.chdir(root / "src" / "deep")
    return root


def test_default_outside_clone_is_home(clean_env, outside):
    home, _ = outside
    assert config.resolve_output() == (home / "graph-me" / "graph-out").resolve()
    assert config.config_path() == home / "graph-me" / "config.yaml"


def test_default_inside_clone_is_clone_root(clean_env, clone):
    assert config.find_clone_root() == clone.resolve()
    assert config.resolve_output() == (clone / "graph-out").resolve()


def test_other_project_is_not_a_clone(clean_env, outside, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    (other / "pyproject.toml").write_text('[project]\nname = "something-else"\n')
    monkeypatch.chdir(other)
    assert config.find_clone_root() is None


def test_resolution_order(clean_env, outside, tmp_path, monkeypatch):
    cfg = config.Config(output=tmp_path / "from-config")
    assert config.resolve_output(config=cfg) == (tmp_path / "from-config").resolve()

    monkeypatch.setenv(config.ENV_OUT, str(tmp_path / "from-env"))
    assert config.resolve_output(config=cfg) == (tmp_path / "from-env").resolve()

    flag = tmp_path / "from-flag"
    assert config.resolve_output(cli_out=flag, config=cfg) == flag.resolve()


def test_ensure_output_is_private_and_git_ignored(tmp_path):
    out = config.ensure_output(tmp_path / "a" / "graph-out")
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert (out / ".gitignore").read_text().splitlines()[-1] == "*"


def test_load_config_example_is_valid():
    from importlib import resources

    text = resources.files("graph_me").joinpath("config.example.yaml").read_text()
    path = Path(__file__).parent / "_example.yaml"
    try:
        path.write_text(text)
        cfg = config.load_config(path)
    finally:
        path.unlink()
    assert cfg.extraction.default_tier == "none"
    assert cfg.sources["docs"].type == "filesystem"
    assert cfg.sources["docs"].model_extra["paths"] == ["~/Documents", "~/Desktop"]


def test_missing_config_means_defaults(tmp_path):
    cfg = config.load_config(tmp_path / "nope.yaml")
    assert cfg.sources == {}
    assert cfg.extraction.default_tier == "none"


def test_unknown_top_level_key_is_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("sourcez: {}\n")
    with pytest.raises(ValidationError):
        config.load_config(path)
