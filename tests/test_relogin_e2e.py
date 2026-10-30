"""자동 재로그인 끝-대-끝: 상시 브라우저(CDP) + 흉내 사이트. node·playwright-core·Chrome 이 있을 때만."""
import json
import os
import shutil
import socket
import subprocess
import sys
import time

import pytest

from conftest import ROOT

CHROME = os.environ.get("CHROME") or shutil.which("google-chrome") or shutil.which("chromium")
PW_CORE = os.environ.get("AI_PW_CORE")
pytestmark = pytest.mark.skipif(not (CHROME and PW_CORE and shutil.which("node")),
                                reason="CHROME·AI_PW_CORE·node 필요 (CI 에서 실행)")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.5).close()
            return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(port)


@pytest.mark.parametrize("auto", [False, True], ids=["continue-button", "auto-close-popup"])
def test_relogin(tmp_path, auto):
    site, cdp = free_port(), free_port()
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "tests", "fixtures", "mock_social_site.py"), str(site)]
                           + (["--auto"] if auto else []))
    chrome = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={cdp}", "--no-first-run",
                               "--no-sandbox", f"--user-data-dir={tmp_path / 'prof'}", "--keep-alive-for-test"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_port(site)
        wait_port(cdp)
        home = tmp_path / "home"
        bot = home / "bots" / "concierge"
        (bot / "notes" / "browser").mkdir(parents=True)
        (bot / "playwright.json").write_text(json.dumps(
            {"browser": {"cdpEndpoint": f"http://127.0.0.1:{cdp}"}, "outputDir": str(bot / "notes" / "browser")}))
        sites = tmp_path / "sites.json"
        sites.write_text(json.dumps({"mock": {
            "domain": "127.0.0.1", "check_url": f"http://127.0.0.1:{site}/home", "logged_in": "님을 위한",
            "login_url": f"http://127.0.0.1:{site}/auth", "button_text": "카카오", "social_host": "kakaoauth"}}))
        env = dict(os.environ, HOME=str(home), AI_RELOGIN_SITES=str(sites))
        run = lambda *a: subprocess.run(["node", os.path.join(ROOT, "src", "ai-browser-relogin.js"), *a],  # noqa: E731
                                        env=env, capture_output=True, text=True, timeout=120).stdout
        assert run("mock", "--check").startswith("FAIL")
        assert run("mock").startswith("OK"), run("mock")
        assert run("mock").startswith("LOGGED_IN")
        assert run("nope").startswith("FAIL 모르는 사이트")
    finally:
        chrome.kill()
        srv.kill()
