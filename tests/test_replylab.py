import http.client
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from replylab.models import Cancellation, Invalid, Result, openai, read_openai_stream
from replylab.server import Server
from replylab.service import Conflict, Lab, Missing


HUMAN = {"kind": "human"}


def model(name="echo", **options):
    return {"kind": "ai", "adapter": "fake", "model": name, "instructions": f"Instructions for {name}",
            "options": {"delay_ms": 0, **options}}


def wait_until(check, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := check():
            return result
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for state")


class LabTests(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(":memory:")
        self.addCleanup(self.lab.close)

    def create(self, a=HUMAN, b=HUMAN, starter="A", prompt=None):
        result = self.lab.create({"seats": {"A": a, "B": b}, "starter": starter, "opening_prompt": prompt})
        return result["session_id"], result["branch_id"]

    def idle(self, session_id):
        wait_until(lambda: self.lab.session(session_id)["run"] is None)

    def send(self, ids, content):
        view = self.lab.branch(*ids)
        return self.lab.send(*ids, {"revision": view["branch"]["revision"], "seat": view["next_seat"],
                                    "text": content})

    def test_boundaries_unicode_and_edits_to_both_seats(self):
        ids = self.create()
        original = "A🦜\r\nCafe\u0301  "
        a = self.send(ids, original)["turns"][0]
        b = self.send(ids, "Original second")["turns"][1]
        after_a = self.lab.fork(*ids, {"kind": "cursor", "turn_id": a["id"], "offset": len(original)})
        self.assertEqual(after_a["turns"], [a])
        self.assertEqual(after_a["next_seat"], "B")
        before_b = self.lab.fork(*ids, {"kind": "cursor", "turn_id": b["id"], "offset": 0})
        self.assertEqual(before_b["turns"], [a])
        start = self.lab.fork(*ids, {"kind": "start"})
        self.assertEqual((start["turns"], start["next_seat"]), ([], "A"))
        inside = self.lab.fork(*ids, {"kind": "cursor", "turn_id": a["id"], "offset": 4})
        self.assertEqual(inside["turns"][0]["text"], "A🦜\r\n")
        self.assertEqual(inside["next_seat"], "B")
        for selected, next_seat in [(a, "B"), (b, "A")]:
            fork = self.lab.fork(*ids, {"kind": "edit", "turn_id": selected["id"], "text": "Replaced"})
            self.assertEqual(fork["turns"][-1]["text"], "Replaced")
            self.assertEqual(fork["turns"][-1]["seat"], selected["seat"])
            self.assertEqual(fork["next_seat"], next_seat)
        self.assertEqual(self.lab.branch(*ids)["turns"], [a, b])
        with self.assertRaises(Missing):
            self.lab.fork(ids[0], start["branch"]["id"], {"kind": "edit", "turn_id": a["id"], "text": "Other"})
        with self.assertRaises(Invalid):
            self.lab.fork(*ids, {"kind": "cursor", "turn_id": a["id"], "offset": len(original) + 1})
        with self.assertRaises(Invalid):
            self.lab.fork(*ids, {"kind": "edit", "turn_id": a["id"], "text": "\ud800"})

    def test_model_projection_and_bounded_autoplay_with_b_opening(self):
        ids = self.create(model("Alpha"), model("Beta"), starter="B", prompt="Opening")
        self.lab.start(*ids, {"revision": 0, "limit": 3}, "play")
        self.idle(ids[0])
        view = self.lab.branch(*ids)
        self.assertEqual(view["last_run"]["reason"], "turn_limit")
        turns = view["turns"]
        self.assertEqual([turn["seat"] for turn in turns], ["B", "A", "B"])
        requests = [self.lab.attempt(ids[0], turn["attempt_id"])["request"] for turn in turns]
        self.assertEqual(requests[0]["messages"], [{"role": "user", "content": "Opening"}])
        self.assertEqual(requests[1]["messages"], [{"role": "user", "content": turns[0]["text"]}])
        self.assertEqual(requests[2]["messages"], [
            {"role": "user", "content": "Opening"},
            {"role": "assistant", "content": turns[0]["text"]},
            {"role": "user", "content": turns[1]["text"]},
        ])
        self.assertEqual([r["instructions"] for r in requests],
                         ["Instructions for Beta", "Instructions for Alpha", "Instructions for Beta"])

    def test_mixed_pairings_stop_at_human_turn(self):
        for a, b in [(model(), HUMAN), (HUMAN, model())]:
            with self.subTest(opening=a["kind"]):
                ids = self.create(a, b, prompt="Hello")
                if a == HUMAN:
                    result = self.lab.start(*ids, {"revision": 0}, "next")
                    self.assertEqual(result, {"status": "human_input", "seat": "A"})
                    self.send(ids, "Opening reply")
                revision = self.lab.branch(*ids)["branch"]["revision"]
                self.lab.start(*ids, {"revision": revision}, "play")
                self.idle(ids[0])
                view = self.lab.branch(*ids)
                self.assertEqual(view["last_run"]["reason"], "human_turn")
                if b == HUMAN:
                    self.send(ids, "Answer")
                self.assertEqual(len(self.lab.branch(*ids)["turns"]), 2)

    def test_stop_retry_late_delta_and_snapshot(self):
        first_delta = threading.Event()
        release = threading.Event()
        late = queue.Queue()
        calls = 0

        def slow(request, delta, cancellation):
            nonlocal calls
            calls += 1
            if calls == 1:
                delta("Part 🦜 and more")
                first_delta.set()
                release.wait(3)
                late.put(delta("Late data"))
            else:
                delta("Retry completed")
            return Result("complete", "stop")

        self.lab.adapters = {"fake": slow}
        ids = self.create(model(), model(), prompt="Seed")
        run = self.lab.start(*ids, {"revision": 0}, "next")
        self.assertTrue(first_delta.wait(2))
        snapshot, mailbox = self.lab.subscribe(ids[0])
        self.assertEqual(snapshot["draft"]["text"], "Part 🦜 and more")
        stopped = self.lab.stop(ids[0], run["id"])
        self.assertEqual(stopped["status"], "stopped")
        event = mailbox.get(timeout=2)
        self.assertGreater(event["sequence"], snapshot["sequence"])
        view = self.lab.branch(*ids)
        self.assertEqual((view["turns"], view["next_seat"]), ([], "A"))
        attempt = self.lab.attempt(ids[0], snapshot["draft"]["id"])
        fork = self.lab.fork(*ids, {"kind": "cursor", "attempt_id": attempt["id"], "offset": 6})
        self.assertEqual((fork["turns"][0]["text"], fork["next_seat"]), ("Part 🦜", "B"))
        new_run = self.lab.start(*ids, {"revision": view["branch"]["revision"]}, "next")
        self.assertEqual(self.lab.stop(ids[0], run["id"])["id"], run["id"])
        release.set()
        self.assertFalse(late.get(timeout=2))
        self.idle(ids[0])
        self.assertEqual(self.lab.branch(*ids)["turns"][0]["text"], "Retry completed")
        self.assertNotEqual(new_run["id"], run["id"])
        self.assertEqual(self.lab.attempt(ids[0], attempt["id"])["text"], "Part 🦜 and more")

    def test_late_delta_cannot_enter_the_next_autoplay_attempt(self):
        earlier_callbacks = []
        accepted = []

        def adapter(request, delta, cancellation):
            if not earlier_callbacks:
                earlier_callbacks.append(delta)
                delta("First")
            else:
                accepted.append(earlier_callbacks[0]("Late"))
                delta("Second")
            return Result("complete", "stop")

        self.lab.adapters = {"fake": adapter}
        ids = self.create(model(), model(), prompt="Seed")
        self.lab.start(*ids, {"revision": 0, "limit": 2}, "play")
        self.idle(ids[0])
        self.assertEqual(accepted, [False])
        self.assertEqual([turn["text"] for turn in self.lab.branch(*ids)["turns"]], ["First", "Second"])

    def test_concurrent_starts_and_provider_failure(self):
        release = threading.Event()

        def fail(request, delta, cancellation):
            delta("Before error")
            release.wait(3)
            return Result("incomplete", "length")

        self.lab.adapters = {"fake": fail}
        ids = self.create(model(), HUMAN, prompt="Seed")
        gate = threading.Barrier(3)
        results = queue.Queue()

        def start():
            gate.wait()
            try:
                results.put(self.lab.start(*ids, {"revision": 0}, "next")["status"])
            except Conflict:
                results.put("conflict")

        for _ in range(2):
            threading.Thread(target=start, daemon=True).start()
        gate.wait()
        self.assertCountEqual([results.get(timeout=2), results.get(timeout=2)], ["active", "conflict"])
        release.set()
        self.idle(ids[0])
        view = self.lab.branch(*ids)
        self.assertEqual(view["turns"], [])
        self.assertEqual(view["last_run"]["reason"], "length")
        self.assertTrue(view["attempts"][0]["has_text"])
        with self.assertRaises(Conflict):
            self.lab.start(*ids, {"revision": 0}, "next")


class PersistenceTests(unittest.TestCase):
    def test_recovery_closes_abandoned_run_and_preserves_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lab.sqlite"
            lab = Lab(path)
            result = lab.create({"seats": {"A": model(), "B": HUMAN}, "opening_prompt": "Seed"})
            session, branch = result["session_id"], result["branch_id"]
            with lab.store.db:
                lab.store.write("""
                    INSERT INTO runs (id, session_id, branch_id, mode, turn_limit, status, created_at)
                    VALUES ('run', ?, ?, 'next', 1, 'active', 'test')
                """, session, branch)
                lab.store.write("""
                    INSERT INTO attempts (id, run_id, branch_id, seat, request, text, status, created_at)
                    VALUES ('attempt', 'run', ?, 'A', '{}', 'checkpoint', 'active', 'test')
                """, branch)
            with self.assertRaisesRegex(OSError, "already open"):
                Lab(path)
            lab.store.close()
            with closing(Lab(path)) as reopened:
                self.assertIsNone(reopened.session(session)["run"])
                self.assertEqual(reopened.branch(session, branch)["last_run"]["status"], "interrupted")
                self.assertEqual(reopened.attempt(session, "attempt")["text"], "checkpoint")


def sse(*chunks):
    return [b"data: " + json.dumps(item).encode() + b"\n" if isinstance(item, dict) else item
            for item in chunks]


class OpenAIAdapterTests(unittest.TestCase):
    def test_streaming_text_completion_and_usage(self):
        parts = []
        response = sse({"id": "id", "model": "resolved", "choices": [{"delta": {"content": "Hi"}}]},
                       {"choices": [{"delta": {"content": " there"}, "finish_reason": "stop"}]},
                       {"choices": [], "usage": {"completion_tokens": 2}}, b"data: [DONE]\n")
        result = read_openai_stream(response, lambda part: not parts.append(part), Cancellation())
        self.assertEqual(parts, ["Hi", " there"])
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.metadata, {"id": "id", "model": "resolved", "usage": {"completion_tokens": 2}})

    def test_incomplete_response_never_succeeds(self):
        cutoff = {"choices": [{"delta": {}, "finish_reason": "length"}]}
        for response, reason in [(sse(cutoff, b"data: [DONE]"), "length"),
                                 (sse(cutoff), "stream_ended_early")]:
            self.assertEqual(read_openai_stream(response, bool, Cancellation()).reason, reason)

    def test_wire_request_and_api_key_separation(self):
        from replylab.models import make_request, normalize_seat

        session = {"starter": "A", "opening_prompt": "Seed", "seats": {"A": normalize_seat({
            "kind": "ai", "adapter": "openai", "model": "test-model", "instructions": "Private setup",
        })}}
        request = make_request(session, "A", [])
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-credential"}), patch(
            "replylab.models.http.client.HTTPSConnection"
        ) as transport:
            response = transport.return_value.getresponse.return_value.__enter__.return_value
            response.status = 200
            response.__iter__.return_value = iter(sse(
                {"choices": [{"delta": {"content": "Response"}, "finish_reason": "stop"}]}, b"data: [DONE]"
            ))
            self.assertEqual(openai(request, bool, Cancellation()).status, "complete")
            method, path, body, headers = transport.return_value.request.call_args.args
        self.assertEqual((method, path), ("POST", "/v1/chat/completions"))
        self.assertEqual(headers["Authorization"], "Bearer test-credential")
        self.assertEqual(json.loads(body), request["body"])
        self.assertEqual(request["body"]["messages"], [
            {"role": "developer", "content": "Private setup"}, {"role": "user", "content": "Seed"}
        ])
        self.assertNotIn("test-credential", json.dumps(request))


class HTTPTests(unittest.TestCase):
    def test_cli_and_sse_share_playback(self):
        lab = Lab(":memory:")
        server = Server(("127.0.0.1", 0), lab)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lab.close)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def cli(*args):
            return json.loads(subprocess.check_output([
                sys.executable, "-m", "replylab", "--url", f"http://127.0.0.1:{server.server_port}", *args,
            ], timeout=5))

        result = cli("create", "--a", "fake", "--b", "fake", "--prompt", "A seed",
                     "--a-options", '{"delay_ms":500}')
        session, branch = result["session_id"], result["branch_id"]
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        self.addCleanup(connection.close)
        connection.request("GET", f"/api/sessions/{session}/events")
        with connection.getresponse() as stream:
            snapshot = json.loads(stream.readline()[6:])
            self.assertEqual(snapshot["type"], "snapshot")
            cli("play", session, branch, "--limit", "2")
            for line in stream:
                if line.startswith(b"data: ") and json.loads(line[6:])["type"] == "text_delta":
                    break
            stopped = cli("stop", session)
        self.assertEqual(stopped["status"], "stopped")
        transcript = cli("show", session, branch, "--json")
        self.assertEqual(transcript["turns"], [])
        self.assertTrue(transcript["attempts"][0]["has_text"])
        fork = cli("fork", session, branch, "--partial", transcript["attempts"][0]["id"] + ":3")
        self.assertEqual(fork["turns"][0]["text"], "[ec")
        self.assertEqual(fork["next_seat"], "B")
        invalid = http.client.HTTPConnection("127.0.0.1", server.server_port)
        self.addCleanup(invalid.close)
        invalid.request("GET", "/", headers={"Origin": "https://foreign.example"})
        with invalid.getresponse() as response:
            self.assertEqual(response.status, 403)
