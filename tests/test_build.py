"""Contracts of the two standalone deliveries and their fixed build script."""

import ast
import base64
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts.build import artifacts

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["openwebui_chatgpt_connect.py", "chatgpt_responses_history.py"])
def test_deliveries_are_current(built_artifacts, name):
    assert (ROOT / "dist" / name).read_bytes() == (built_artifacts / name).read_bytes()


def test_generation_is_deterministic_from_another_working_directory(
    tmp_path, monkeypatch, built_artifacts
):
    monkeypatch.chdir(tmp_path)
    for name, source in artifacts().items():
        assert source == (built_artifacts / name).read_text(encoding="utf-8")


def test_script_generates_both_deliveries_in_its_project(tmp_path, built_artifacts):
    project = tmp_path / "project"
    shutil.copytree(ROOT / "src", project / "src")
    shutil.copytree(
        ROOT / "scripts", project / "scripts", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copy2(ROOT / "pyproject.toml", project / "pyproject.toml")
    subprocess.run([sys.executable, str(project / "scripts/build.py")], cwd=tmp_path, check=True)
    output = project / "dist"
    expected = list(built_artifacts.glob("*.py"))
    assert {path.name for path in output.iterdir()} == {path.name for path in expected}
    for path in expected:
        assert (output / path.name).read_bytes() == path.read_bytes()


def test_delivery_metadata_comes_from_project(artifact):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    metadata = dict(
        line.split(": ", 1)
        for line in ast.get_docstring(ast.parse(artifact.read_text())).splitlines()
    )
    assert metadata["author"] == project["project"]["authors"][0]["name"]
    assert metadata["version"] == project["project"]["version"]
    assert metadata["requirements"] == ",".join(project["project"]["dependencies"])
    assert (
        metadata["required_open_webui_version"]
        == project["tool"]["openwebui-chatgpt-connect"]["required-open-webui-version"]
    )


def test_history_filter_is_an_independent_delivery(filter_artifact, history_filter):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    tree = ast.parse(filter_artifact.read_text())
    metadata = dict(line.split(": ", 1) for line in ast.get_docstring(tree).splitlines())
    assert metadata["title"] == "ChatGPT Responses History"
    assert metadata["version"] == project["project"]["version"]
    assert metadata["author"] == project["project"]["authors"][0]["name"]
    assert "requirements" not in metadata
    assert {node.name for node in tree.body if isinstance(node, ast.ClassDef)} == {"Filter"}
    assert hasattr(history_filter, "stream") and hasattr(history_filter, "request")


async def test_copied_artifact_registers_without_project_imports(harness):
    response = await harness.page()
    assert response.status_code == 200
    assert harness.event.Valves.model_validate(harness.host.valves).INTERNAL_API_KEY


async def test_admin_assets_are_embedded_in_the_delivery(harness):
    source = (ROOT / "src/openwebui_chatgpt_connect/assets/admin.html").read_text()
    avatar = (ROOT / "src/openwebui_chatgpt_connect/assets/chatgpt.png").read_bytes()
    assert source.startswith("<!doctype html>")
    assert '<html lang="en">' in source
    assert "Static preview" in source
    assert harness.module.ADMIN_HTML == source
    assert base64.b64decode(harness.module.MODEL_AVATAR) == avatar
