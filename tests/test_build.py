"""Contracts of the standalone delivery asset and the build command."""

import ast
import base64
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "scripts/build.py"


def test_artifact_is_current():
    subprocess.run([sys.executable, str(BUILD), "--check"], check=True)


def test_build_is_deterministic_and_independent_of_working_directory(tmp_path, artifact):
    output = tmp_path / "another.py"
    subprocess.run([sys.executable, str(BUILD), "--output", str(output)], cwd=tmp_path, check=True)
    assert output.read_bytes() == artifact.read_bytes()
    output.write_text("# stale\n")
    check = subprocess.run(
        [sys.executable, str(BUILD), "--output", str(output), "--check"], capture_output=True
    )
    assert check.returncode == 1
    assert output.read_text() == "# stale\n"


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
