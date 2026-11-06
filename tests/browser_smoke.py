import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


class BrowserSmokeTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="replylab-smoke-")
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        self.cli_path = shutil.which("replylab")
        self.assertIsNotNone(self.cli_path, "Install the ReplyLab wheel before running this test")
        self.log_path = self.workspace / "server.log"
        env = {**os.environ, "PYTHONUTF8": "1"}
        env.pop("PYTHONPATH", None)
        self.env = env
        with self.log_path.open("wb") as log:
            self.server = subprocess.Popen(
                [self.cli_path, "serve", "--port", "0", "--db", "test.sqlite3"],
                cwd=self.workspace, env=env, stdout=log, stderr=subprocess.STDOUT,
            )
        self.addCleanup(self.stop_server)
        deadline = time.monotonic() + 10
        while self.server.poll() is None and time.monotonic() < deadline:
            match = re.search(r"http://127\.0\.0\.1:\d+", self.log_path.read_text(encoding="utf-8"))
            if match:
                self.url = match.group()
                return
            time.sleep(0.05)
        self.fail("ReplyLab service did not start")

    def stop_server(self):
        if self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait(timeout=5)
        print(self.log_path.read_text(encoding="utf-8", errors="replace"), file=sys.stderr, end="")

    def cli(self, *args):
        result = subprocess.run(
            [self.cli_path, "--url", self.url, *args], cwd=self.workspace, env=self.env,
            check=False, capture_output=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def branch_after(self, page, previous):
        expect(page).not_to_have_url(re.compile(r"/" + previous + "$"))
        expect(page.get_by_role("heading", name=re.compile(r"^Fork "))).to_be_visible()
        return urlsplit(page.url).fragment.split("/")[1]

    def test_browser_and_cli_share_playback_and_forks(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
            )
            try:
                self.exercise(browser.new_page())
            finally:
                browser.close()

    def exercise(self, page):
        page.set_default_timeout(10_000)
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(self.url)
        page.get_by_role("button", name="Create a session").click()
        page.locator('[data-seat="B"] [name="adapter"]').select_option("fake")
        page.locator('[data-seat="B"] [name="options"]').fill('{"delay_ms": 1200}')
        page.get_by_role("button", name="Create session", exact=True).click()
        expect(page.locator("#message")).to_be_visible()
        session, original = urlsplit(page.url).fragment.split("/")

        text = "A🦜\r\nCafe"
        self.cli("send", session, original, text)
        expect(page.locator(".turn")).to_have_count(1)
        page.get_by_role("button", name="Play", exact=True).click()
        expect(page.locator("#draft .message-text")).to_contain_text("[echo]", timeout=10_000)
        self.assertEqual(self.cli("stop", session)["status"], "stopped")
        self.assertEqual(len(self.cli("show", session, original, "--json")["turns"]), 1)

        page.locator("#attempt-list").get_by_role("button", name=re.compile("stopped")).click()
        expect(page.locator("#attempt-request")).to_contain_text("Cafe")
        page.get_by_role("button", name="↳ Replay from this partial").click()
        cursor = page.locator("#fork-text")
        cursor.evaluate("field => field.setSelectionRange(7, 7)")
        page.get_by_role("button", name="Reply from here").click()
        partial_branch = self.branch_after(page, original)
        partial = self.cli("show", session, partial_branch, "--json")
        self.assertEqual([turn["text"] for turn in partial["turns"]], [text, "[echo] "])

        page.get_by_role("navigation", name="Branches").get_by_role("button", name="Original").click()
        expect(page.get_by_role("heading", name="Original")).to_be_visible()
        page.get_by_role("button", name="Replay", exact=True).click()
        # A, the emoji, and the normalized newline occupy four UTF-16 units.
        cursor.evaluate("field => field.setSelectionRange(4, 4)")
        page.get_by_role("button", name="Reply from here").click()
        replay_branch = self.branch_after(page, original)
        replay = self.cli("show", session, replay_branch, "--json")
        self.assertEqual(replay["turns"][0]["text"], "A🦜\r\n")
        self.assertEqual(replay["next_seat"], "B")

        page.get_by_role("button", name="Edit and replay").click()
        cursor.fill("Edited through browser")
        page.get_by_role("button", name="Create edited branch").click()
        edit_branch = self.branch_after(page, replay_branch)
        page.reload()
        expect(page.get_by_text("Edited through browser", exact=True)).to_be_visible()

        self.cli("next", session, edit_branch)
        expect(page.get_by_text("Seat A is next · Last run: single turn", exact=True)).to_be_visible(timeout=10_000)
        page.locator("#message").fill("Sent from browser")
        page.get_by_role("button", name="Send", exact=True).click()
        expect(page.get_by_text("Sent from browser", exact=True)).to_be_visible()
        edit = self.cli("show", session, edit_branch, "--json")
        self.assertEqual(edit["turns"][-1]["text"], "Sent from browser")
        self.assertEqual(self.cli("show", session, original, "--json")["turns"][0]["text"], text)
        self.assertEqual(page_errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
