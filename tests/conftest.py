import json
import os

import pytest

from tierwright import backends, config, init
from tierwright.project import Project


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TIERWRIGHT_FAKE", raising=False)
    init.run(str(tmp_path), backend="subscription", per_day=5, per_goal=20,
             goal="make hello.sh print hi", done=["bash hello.sh | grep -q hi"],
             probe=False, interactive=False)
    return Project(tmp_path).ensure()


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """fake({"role": answer | [answers...]}) -> path; routes every call to the fake backend."""
    def make(answers: dict):
        f = tmp_path / f"fake-{len(os.listdir(tmp_path))}.json"
        f.write_text(json.dumps(answers))
        monkeypatch.setenv("TIERWRIGHT_FAKE", str(f))
        backends._FAKE = None
        return f
    return make


@pytest.fixture
def cfg(proj):
    return config.load(proj)


def job(p: Project, name: str, body: str) -> str:
    f = p.root / f"{name}.job"
    f.write_text(body)
    return str(f)
