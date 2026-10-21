"""Shared fixtures: every test gets an isolated project root (copied config + prompts)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from research_agent.settings import load_settings
from research_agent.storage.database import open_db

REPO = Path(__file__).resolve().parents[1]
RECORDED = Path(__file__).parent / "fixtures" / "recorded"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    shutil.copytree(REPO / "config", tmp_path / "config")
    shutil.copytree(REPO / "prompts", tmp_path / "prompts")
    (tmp_path / "reports").mkdir()
    return tmp_path


@pytest.fixture
def settings(project: Path):
    return load_settings(project)


@pytest.fixture
def db(settings):
    d = open_db(settings.db_path)
    yield d
    d.close()


def recorded(name: str) -> str:
    return (RECORDED / name).read_text(encoding="utf-8")
