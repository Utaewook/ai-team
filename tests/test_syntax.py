"""모든 프로그램이 최소한 문법상 실행 가능한지."""
import os
import shutil
import subprocess

import pytest

from conftest import ROOT

FILES = [os.path.join(ROOT, d, f) for d in ("src", "deploy") for f in sorted(os.listdir(os.path.join(ROOT, d)))
         if os.path.isfile(os.path.join(ROOT, d, f))]
SHELL = [os.path.join(ROOT, f) for f in ("install.sh", "harden.sh", "install-browser.sh")]


def kind(path):
    if path.endswith(".js"):
        return "js"
    if path.endswith((".json", ".tsv")):
        return None
    head = open(path, "rb").readline()
    return "py" if (path.endswith(".py") or b"python" in head) else "sh" if b"sh" in head else None


@pytest.mark.parametrize("path", FILES + SHELL, ids=lambda p: os.path.relpath(p, ROOT))
def test_syntax(path):
    k = kind(path)
    if k == "py":
        subprocess.run(["python3", "-m", "py_compile", path], check=True)
    elif k == "sh":
        subprocess.run(["bash", "-n", path], check=True)
    elif k == "js":
        if not shutil.which("node"):
            pytest.skip("node 없음")
        subprocess.run(["node", "--check", path], check=True)


def test_manifest_sources_exist():
    for line in open(os.path.join(ROOT, "deploy", "manifest.tsv"), encoding="utf-8"):
        if line.strip() and not line.startswith("#"):
            group, src, dst, mode = line.rstrip("\n").split("\t")
            assert group in ("core", "browser")
            assert os.path.isfile(os.path.join(ROOT, src)), src
            assert dst.startswith("/") and mode in ("755", "644")
