from pathlib import Path
import shutil
import subprocess

import pytest


def test_browser_profile_image_is_pinned_and_runs_nonroot():
    root = Path(__file__).parents[2]
    dockerfile = root / "containers/delegation-browser/Dockerfile"

    assert dockerfile.is_file()
    text = dockerfile.read_text()
    assert "mcr.microsoft.com/playwright:v1.55.0-noble@sha256:" in text
    assert "playwright@1.55.0" in text
    assert "USER pwuser" in text
    assert "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" in text


def test_base_profile_image_has_private_nonroot_workspace():
    root = Path(__file__).parents[2]
    dockerfile = root / "containers/delegation/Dockerfile"

    assert dockerfile.is_file()
    text = dockerfile.read_text()
    assert "USER hermes" in text
    assert "WORKDIR /workspace" in text


@pytest.mark.integration
@pytest.mark.docker
@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI unavailable")
def test_browser_profile_launches_chromium_and_creates_private_screenshot():
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("Docker daemon unavailable")
    root = Path(__file__).parents[2]
    image = "hermes-delegation-browser:test"
    subprocess.run(
        ["docker", "build", "-t", image,
         str(root / "containers/delegation-browser")],
        check=True,
        timeout=600,
    )
    script = """
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage();
  await page.setContent('<h1>scoped browser</h1>');
  await page.screenshot({path: '/workspace/shot.png'});
  await browser.close();
  const fs = require('fs');
  if (fs.statSync('/workspace/shot.png').size < 100) process.exit(2);
})();
"""
    result = subprocess.run(
        ["docker", "run", "--rm", "--network=none", "--pids-limit", "128",
         "--tmpfs", "/workspace:rw,noexec,nosuid,nodev,mode=1777,size=64m",
         image, "node", "-e", script],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
