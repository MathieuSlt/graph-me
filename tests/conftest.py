from pathlib import Path

import pytest
from factory import make_docs

from graph_me.config import BlacklistConfig, Config, SourceConfig
from graph_me.store import db


@pytest.fixture(scope="session")
def docs_template(tmp_path_factory) -> Path:
    return make_docs(tmp_path_factory.mktemp("fixtures"))


@pytest.fixture
def docs(tmp_path, docs_template) -> Path:
    """A private copy of the documents fixture that a test may modify."""
    import shutil

    target = tmp_path / "docs"
    shutil.copytree(docs_template, target, symlinks=True)
    return target


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "graph.db")
    yield c
    c.close()


def docs_config(docs: Path, **source_extra) -> Config:
    return Config(
        sources={"docs": SourceConfig(type="filesystem", paths=[str(docs)], **source_extra)},
        blacklist=BlacklistConfig(paths=[str(docs / "Medical")]),
    )
