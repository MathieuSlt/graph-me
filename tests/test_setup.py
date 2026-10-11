"""M8: first run and setup. init --detect, scan --since, doctor, install-mcp, schedule, demo."""

import json
import os
import plistlib
import subprocess
import tomllib
from datetime import UTC, datetime, timedelta

import pytest
from conftest import assert_consistent, docs_config
from typer.testing import CliRunner

from graph_me import config, doctor
from graph_me import demo as demo_mod
from graph_me.cli import app
from graph_me.pipeline import run
from graph_me.query import graph
from graph_me.setup import clients, detect, schedule
from graph_me.store import db

runner = CliRunner()


# -- init --detect ------------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path):
    home = tmp_path / "home"
    for folder in (
        "Documents",
        "Downloads",
        "Projects/notes/.obsidian",
        "Documents/vault/.obsidian",
    ):
        (home / folder).mkdir(parents=True)
    (home / "Documents/contacts.vcf").write_text("BEGIN:VCARD\nFN:A\nEND:VCARD\n")
    (home / ".msgvault").mkdir()
    (home / ".msgvault/msgvault.db").write_bytes(b"")
    return home


def test_detect_finds_usual_places(fake_home):
    found = detect.detect(fake_home, env={"LANG": "en_GB.UTF-8", "TZ": "Europe/London"})
    by_name = {c.name: c.source for c in found.candidates}
    assert by_name["documents"]["paths"] == [str(fake_home / "Documents")]
    assert by_name["downloads"]["trust"] == "untrusted"
    assert "desktop" not in by_name  # no Desktop folder here
    # The vault inside Documents is already covered; the one in Projects is not.
    assert by_name["notes"]["paths"] == [str(fake_home / "Projects/notes")]
    assert by_name["messages"] == {"type": "msgvault", "db": str(fake_home / ".msgvault")}
    assert by_name["contacts"]["paths"] == [str(fake_home / "Documents/contacts.vcf")]
    assert (found.timezone, found.phone_country_code) == ("Europe/London", "44")

    text = detect.render(found, found.candidates[:1])
    cfg = config.Config.model_validate(__import__("yaml").safe_load(text))
    assert list(cfg.sources) == ["documents"]
    assert cfg.people.phone_country_code == "44"


def test_detect_without_locale(tmp_path):
    found = detect.detect(tmp_path, env={"LANG": "C.UTF-8", "TZ": "Nowhere/Land"})
    assert found.candidates == []
    assert found.phone_country_code is None


def test_init_detect_cli(fake_home, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    monkeypatch.setenv(config.ENV_CONFIG, str(tmp_path / "cfg" / "config.yaml"))
    monkeypatch.setenv(config.ENV_OUT, str(tmp_path / "cfg" / "graph-out"))
    # Keep documents, refuse downloads, keep the rest.
    result = runner.invoke(app, ["init", "--detect"], input="y\nn\ny\ny\ny\n")
    assert result.exit_code == 0, result.output
    cfg = config.load_config(tmp_path / "cfg" / "config.yaml")
    assert set(cfg.sources) == {"documents", "notes", "messages", "contacts"}
    assert cfg.people.phone_country_code == "33"
    assert "scan --since 90d" in result.output
    again = runner.invoke(app, ["init", "--detect", "--yes"])
    assert again.exit_code == 1 and "already exists" in again.output


# -- scan --since and the run lock --------------------------------------------------------------


def test_scan_since_reads_recent_files_first(conn, docs):
    cfg = docs_config(docs)
    old = (datetime.now(UTC) - timedelta(days=400)).timestamp()
    for path in docs.rglob("*"):
        if path.is_file():
            os.utime(path, (old, old))
    recent = docs / "Logement/Contrat_bail_2025.pdf"
    os.utime(recent, None)

    report = run.scan(conn, cfg, workers=1, since=datetime.now(UTC) - timedelta(days=90))
    stats = report.sources["docs"]
    assert stats.added == 1 and stats.older > 0
    assert_consistent(conn)
    report = run.scan(conn, cfg, workers=1)  # the full scan reads the rest
    assert report.sources["docs"].added == stats.older


def test_scan_since_cli_values(conn):
    from graph_me.cli import _since

    assert datetime.fromisoformat(_since("2025-01-31")) == datetime(2025, 1, 31, tzinfo=UTC)
    delta = datetime.now(UTC) - datetime.fromisoformat(_since("2w"))
    assert timedelta(days=13) < delta < timedelta(days=15)
    with pytest.raises(Exception, match="90d"):
        _since("soon")


def test_run_lock_refuses_a_second_run(tmp_path, docs):
    first = db.connect(tmp_path / "graph.db")
    second = db.connect(tmp_path / "graph.db")
    with db.run_lock(first), pytest.raises(db.RunLocked):
        run.scan(second, docs_config(docs), workers=1)
    run.scan(second, docs_config(docs), workers=1)  # released
    first.close()
    second.close()


# -- install-mcp ---------------------------------------------------------------------------------


def test_install_mcp_json_merges_and_keeps_a_backup(tmp_path):
    cfg = tmp_path / "config.yaml"
    path = clients.config_path("cursor", tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    p = clients.plan("cursor", "graph-me", cfg, home=tmp_path)
    clients.apply(p)
    data = json.loads(path.read_text())
    assert data["theme"] == "dark" and "other" in data["mcpServers"]
    entry = data["mcpServers"]["graph-me"]
    assert entry["args"] == ["--config", str(cfg.resolve()), "mcp"]
    assert os.path.isabs(entry["command"])
    assert path.with_name("mcp.json.bak").exists()
    assert clients.plan("cursor", "graph-me", cfg, home=tmp_path).already


def test_install_mcp_codex_toml(tmp_path):
    cfg = tmp_path / "config.yaml"
    path = clients.config_path("codex", tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('model = "x"\n')
    clients.apply(clients.plan("codex", "graph-me", cfg, home=tmp_path))
    data = tomllib.loads(path.read_text())
    assert data["model"] == "x"
    assert data["mcp_servers"]["graph-me"]["args"][-1] == "mcp"
    assert clients.plan("codex", "graph-me", cfg, home=tmp_path).already
    with pytest.raises(ValueError, match="letters"):
        clients.plan("codex", "graph me", cfg, home=tmp_path)


def test_install_mcp_claude_code_uses_its_cli(tmp_path):
    p = clients.plan("claude-code", "graph-me", tmp_path / "config.yaml")
    assert p.run[:6] == ["claude", "mcp", "add", "--scope", "user", "graph-me"]
    assert p.run[-1] == "mcp"


def test_install_mcp_print_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(app, ["install-mcp", "--client", "cursor", "--print"])
    assert result.exit_code == 0, result.output
    assert '"mcpServers"' in result.output
    assert not (tmp_path / ".cursor/mcp.json").exists()


# -- schedule -----------------------------------------------------------------------------------


class FakeRunner:
    def __init__(self, returncode=0):
        self.calls, self.returncode = [], returncode

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, self.returncode, "", "")


def test_parse_every():
    assert schedule.parse_every("6h") == 6 * 3600
    assert schedule.parse_every("30m") == 1800
    for bad in ("5m", "soon", "6"):
        with pytest.raises(ValueError):
            schedule.parse_every(bad)


def test_schedule_launchd(tmp_path):
    fake = FakeRunner()
    command = schedule.sync_command("/bin/graph-me", tmp_path / "config.yaml")
    [plist] = schedule.install(command, tmp_path / "sync.log", 3600, tmp_path, "darwin", fake)
    data = plistlib.loads(plist.read_bytes())
    assert data["ProgramArguments"][-2:] == ["sync", "--scheduled"]
    assert data["StartInterval"] == 3600
    assert any(c[:2] == ["launchctl", "bootstrap"] for c in fake.calls)
    st = schedule.status(tmp_path, "darwin", fake)
    assert (st.installed, st.active, st.every) == (True, True, 3600)
    assert schedule.remove(tmp_path, "darwin", fake) == [plist]
    assert not schedule.status(tmp_path, "darwin", fake).installed


def test_schedule_systemd(tmp_path):
    fake = FakeRunner()
    command = schedule.sync_command("/usr/bin/graph-me", tmp_path / "my config.yaml")
    service, timer = schedule.install(command, tmp_path / "sync.log", 7200, tmp_path, "linux", fake)
    assert '"sync" "--scheduled"' in service.read_text()
    assert "my config.yaml" in service.read_text()
    assert "OnUnitActiveSec=7200s" in timer.read_text()
    assert ["systemctl", "--user", "enable", "--now", "graph-me-sync.timer"] in fake.calls
    assert schedule.status(tmp_path, "linux", FakeRunner(returncode=3)).active is False
    with pytest.raises(schedule.ScheduleError, match="failed"):
        schedule.install(command, tmp_path / "sync.log", 7200, tmp_path, "linux", FakeRunner(1))
    with pytest.raises(schedule.ScheduleError, match="macOS and Linux"):
        schedule.install(command, tmp_path / "sync.log", 7200, tmp_path, "win32", fake)


def test_rotate_log(tmp_path):
    log = tmp_path / "sync.log"
    log.write_bytes(b"x" * (schedule.LOG_MAX_BYTES + 1))
    schedule.rotate_log(tmp_path)
    assert not log.exists() and (tmp_path / "sync.log.1").exists()


# -- doctor and demo ---------------------------------------------------------------------------


def test_doctor_on_the_demo(tmp_path):
    cfg_path = demo_mod.build(tmp_path / "demo")
    cfg = config.load_config(cfg_path)
    out = config.ensure_output(config.resolve_output(None, cfg))
    checks = {c.label: c for c in doctor.run(cfg, cfg_path, out, home=tmp_path,
                                             runner=FakeRunner(1))}  # fmt: skip
    assert checks["index"].status == "fail"  # not scanned yet
    assert checks["source documents"].status == "ok"
    assert checks["schedule"].status == "info"

    conn = db.connect(out / config.DB_NAME)
    run.scan(conn, cfg, workers=1)
    assert_consistent(conn)
    [alex] = graph.get_fact(conn, "my sister", "birthday", cfg.people)
    assert alex["entity"]["name"] == "Alex Taylor"
    conn.close()

    cfg.sources["gone"] = config.SourceConfig(type="filesystem", paths=[str(tmp_path / "nope")])
    out.chmod(0o755)
    checks = {c.label: c for c in doctor.run(cfg, cfg_path, out, home=tmp_path,
                                             runner=FakeRunner(1))}  # fmt: skip
    assert checks["index"].status == "ok"
    assert checks["source gone"].status == "fail"
    assert checks["index folder"].status == "fail" and "chmod 700" in checks["index folder"].hint


def test_demo_cli(tmp_path):
    result = runner.invoke(app, ["demo", "--dir", str(tmp_path / "d")])
    assert result.exit_code == 0, result.output
    assert 'fact "my sister" birthday' in result.output
    result = runner.invoke(app, ["--config", str(tmp_path / "d/config.yaml"), "query", "renews"])
    assert "vehicle_policy_renewal.docx" in result.output
    assert runner.invoke(app, ["demo", "--dir", str(tmp_path / "d")]).exit_code == 1  # not empty
