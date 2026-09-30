"""M0 gate: `init` creates config + graph-out, `where` reports store and environment."""

import stat

import pytest
from typer.testing import CliRunner

from graph_me import config
from graph_me.cli import app

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(config.ENV_OUT, raising=False)
    monkeypatch.delenv(config.ENV_CONFIG, raising=False)
    monkeypatch.chdir(work)
    return home


def test_init_then_where(home):
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    base = home / "graph-me"
    assert (base / "config.yaml").is_file()
    out = base / "graph-out"
    assert (out / "graph.db").is_file()
    assert stat.S_IMODE(out.stat().st_mode) == 0o700

    result = runner.invoke(app, ["where"])
    assert result.exit_code == 0, result.output
    assert str(out) in result.output
    for label in ("schema", "python", "sqlite", "fts5", "ext", "uv"):
        assert label in result.output


def test_init_keeps_existing_config(home):
    runner.invoke(app, ["init"])
    cfg = home / "graph-me" / "config.yaml"
    cfg.write_text("sources: {}\n")
    result = runner.invoke(app, ["init"])
    assert "kept" in result.output
    assert cfg.read_text() == "sources: {}\n"


def test_out_flag_wins(home, tmp_path):
    custom = tmp_path / "elsewhere"
    result = runner.invoke(app, ["--out", str(custom), "init"])
    assert result.exit_code == 0, result.output
    assert (custom / "graph.db").is_file()


def test_invalid_config_is_reported(home, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("extraction: {default_tier: turbo}\n")
    result = runner.invoke(app, ["--config", str(bad), "where"])
    assert result.exit_code == 1
    assert "Invalid config" in result.output


def test_install_skill(home, tmp_path):
    dest = tmp_path / "skills"
    result = runner.invoke(app, ["install-skill", "--dest", str(dest)])
    assert result.exit_code == 0, result.output
    skill = dest / "graph-me" / "SKILL.md"
    assert skill.read_text().startswith("---\nname: graph-me")

    skill.write_text("edited by the user")
    assert runner.invoke(app, ["install-skill", "--dest", str(dest)]).exit_code == 1
    assert runner.invoke(app, ["install-skill", "--dest", str(dest), "--force"]).exit_code == 0


@pytest.mark.parametrize("command", ["scan", "sync", "enrich", "ingest", "query", "ui", "mcp"])
def test_future_commands_say_not_implemented(home, command):
    result = runner.invoke(app, [command])
    assert result.exit_code == 2
    assert "Not implemented yet" in result.output


def test_uv_managed_detection(tmp_path, monkeypatch):
    from graph_me import env

    monkeypatch.setenv("HOME", str(tmp_path))  # detection must not depend on HOME
    uv_py = tmp_path / "anywhere" / "uv" / "python" / "cpython-3.12.13-linux-x86_64-gnu"
    uv_py.mkdir(parents=True)
    assert env.is_uv_managed(str(uv_py))
    assert not env.is_uv_managed("/usr")

    custom = tmp_path / "custom-pythons"
    (custom / "cpython-3.13").mkdir(parents=True)
    monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", str(custom))
    assert env.is_uv_managed(str(custom / "cpython-3.13"))
