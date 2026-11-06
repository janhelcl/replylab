import json
import logging
import queue
import threading
import time
from dataclasses import dataclass

from .models import ADAPTERS, Cancellation, Invalid, Result, integer, make_request, normalize_seat, require_text
from .store import Store, new_id, timestamp


class Missing(ValueError):
    pass


class Conflict(ValueError):
    def __init__(self, message, state):
        super().__init__(message)
        self.state = state


def other(seat):
    return "B" if seat == "A" else "A"


@dataclass
class Draft:
    attempt_id: str
    text: str = ""
    checkpoint: float = 0


class Lab:
    def __init__(self, path, adapters=None):
        self.store = Store(path)
        self.lock = threading.RLock()
        self.adapters = ADAPTERS if adapters is None else adapters
        self.running = {}
        self.drafts = {}
        self.subscribers = {}
        self.sequence = 0

    def _session(self, session_id):
        session = self.store.one("SELECT * FROM sessions WHERE id = ?", session_id)
        if session is None:
            raise Missing("Session not found")
        return session

    def _branch(self, session_id, branch_id):
        branch = self.store.one(
            "SELECT * FROM branches WHERE session_id = ? AND id = ?", session_id, branch_id
        )
        if branch is None:
            raise Missing("Branch not found")
        return branch

    def _run(self, session_id, run_id):
        run = self.store.one("SELECT * FROM runs WHERE session_id = ? AND id = ?", session_id, run_id)
        if run is None:
            raise Missing("Run not found")
        return run

    def _active(self, session_id):
        return self.store.one("SELECT * FROM runs WHERE session_id = ? AND status = 'active'", session_id)

    def _attempt(self, session_id, attempt_id):
        attempt = self.store.one("""
            SELECT a.* FROM attempts a JOIN branches b ON b.id = a.branch_id
            WHERE b.session_id = ? AND a.id = ?
        """, session_id, attempt_id)
        if attempt is None:
            raise Missing("Attempt not found")
        if attempt["status"] == "active":
            draft = self.drafts.get(attempt["run_id"])
            if draft is not None:
                attempt["text"] = draft.text
        return attempt

    def _seat(self, session, branch):
        if branch["head_id"] is None:
            return session["starter"]
        turn = self.store.one("SELECT seat FROM turns WHERE id = ?", branch["head_id"])
        return other(turn["seat"])

    def _write_turn(self, session_id, parent_id, seat, text, origin, attempt_id=None):
        turn_id = new_id()
        self.store.write("""
            INSERT INTO turns (id, session_id, parent_id, seat, text, origin, attempt_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, turn_id, session_id, parent_id, seat, text, origin, attempt_id, timestamp())
        return turn_id

    def _move_head(self, branch_id, turn_id):
        self.store.write("UPDATE branches SET head_id = ?, revision = revision + 1 WHERE id = ?", turn_id, branch_id)

    def create(self, body):
        starter = body.get("starter", "A")
        if starter not in ("A", "B"):
            raise Invalid("starting seat must be A or B")
        raw_seats = body.get("seats")
        if not isinstance(raw_seats, dict) or set(raw_seats) != {"A", "B"}:
            raise Invalid("configure both seats A and B")
        seats = {seat: normalize_seat(config) for seat, config in raw_seats.items()}
        prompt = body.get("opening_prompt")
        if prompt is not None or seats[starter]["kind"] == "ai":
            prompt = require_text(prompt, "opening prompt", nonblank=True)
        session_id, branch_id = new_id(), new_id()
        with self.lock:
            with self.store.db:
                self.store.write("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                                 session_id, timestamp(), starter, prompt, json.dumps(seats))
                self.store.write("""
                    INSERT INTO branches (id, session_id, kind, created_at) VALUES (?, ?, 'root', ?)
                """, branch_id, session_id, timestamp())
            return {"session_id": session_id, "branch_id": branch_id}

    def sessions(self):
        with self.lock:
            return self.store.all("SELECT * FROM sessions ORDER BY rowid DESC")

    def session(self, session_id):
        with self.lock:
            session = self._session(session_id)
            branches = self.store.all("SELECT * FROM branches WHERE session_id = ? ORDER BY rowid", session_id)
            active = self._active(session_id)
            draft = self.drafts.get(active["id"]) if active else None
            live = None
            if draft is not None:
                attempt = self._attempt(session_id, draft.attempt_id)
                live = {key: attempt[key] for key in ("id", "run_id", "branch_id", "seat", "text")}
            return {"session": session, "branches": branches, "run": active, "draft": live}

    def branch(self, session_id, branch_id):
        with self.lock:
            session = self._session(session_id)
            branch = self._branch(session_id, branch_id)
            attempts = self.store.all("""
                SELECT id, run_id, branch_id, seat, status, reason, created_at,
                       text <> '' AS has_text
                FROM attempts WHERE branch_id = ? ORDER BY rowid
            """, branch_id)
            last_run = self.store.one("SELECT * FROM runs WHERE branch_id = ? ORDER BY rowid DESC LIMIT 1", branch_id)
            return {"branch": branch, "turns": self.store.history(branch["head_id"]),
                    "next_seat": self._seat(session, branch), "attempts": attempts, "last_run": last_run}

    def attempt(self, session_id, attempt_id):
        with self.lock:
            return self._attempt(session_id, attempt_id)

    def _guard(self, session_id, branch, body):
        revision = integer(body.get("revision"), "revision", 0, 2**63 - 1)
        run = self._active(session_id)
        if run is not None or revision != branch["revision"]:
            message = "A run is already active" if run else "Branch revision changed"
            raise Conflict(message, {"branch": branch, "run": run})

    def send(self, session_id, branch_id, body):
        content = require_text(body.get("text"), "message", nonblank=True)
        with self.lock:
            session, branch = self._session(session_id), self._branch(session_id, branch_id)
            self._guard(session_id, branch, body)
            seat = self._seat(session, branch)
            if body.get("seat") != seat or session["seats"][seat]["kind"] != "human":
                raise Conflict("The requested human seat is not up", {"branch": branch, "next_seat": seat})
            with self.store.db:
                turn_id = self._write_turn(session_id, branch["head_id"], seat, content, "human")
                self._move_head(branch_id, turn_id)
            self._emit(session_id, "turn_committed", branch_id=branch_id, turn_id=turn_id)
            return self.branch(session_id, branch_id)

    def fork(self, session_id, branch_id, body):
        kind = body.get("kind")
        if kind not in ("start", "cursor", "edit"):
            raise Invalid("fork kind must be start, cursor, or edit")
        with self.lock:
            branch = self._branch(session_id, branch_id)
            anchor = None
            head = None
            replacement = None
            if kind != "start":
                if "attempt_id" in body:
                    if kind != "cursor":
                        raise Invalid("Only a cursor can anchor to a saved partial")
                    attempt_id = require_text(body["attempt_id"], "attempt ID", nonblank=True)
                    attempt = self._attempt(session_id, attempt_id)
                    if attempt["branch_id"] != branch_id or attempt["status"] not in {
                        "failed", "incomplete", "stopped", "interrupted"
                    } or not attempt["text"]:
                        raise Invalid("Select a saved partial from this branch")
                    content, seat, parent = attempt["text"], attempt["seat"], attempt["starting_head_id"]
                    anchor = {"attempt_id": attempt_id}
                    original_id = None
                else:
                    turn_id = require_text(body.get("turn_id"), "turn ID", nonblank=True)
                    turn = next((item for item in self.store.history(branch["head_id"]) if item["id"] == turn_id), None)
                    if turn is None:
                        raise Missing("Turn not found on this branch")
                    content, seat, parent = turn["text"], turn["seat"], turn["parent_id"]
                    anchor = {"turn_id": turn_id}
                    original_id = turn_id
                if kind == "edit":
                    replacement = require_text(body.get("text"), "edited message", nonblank=True)
                else:
                    offset = integer(body.get("offset"), "offset", 0, len(content))
                    anchor["offset"] = offset
                    if offset == 0:
                        head = parent
                    elif original_id and offset == len(content):
                        head = original_id
                    else:
                        replacement = require_text(content[:offset], "prefix", nonblank=True)
            new_branch = new_id()
            with self.store.db:
                if replacement is not None:
                    head = self._write_turn(session_id, parent, seat, replacement, kind)
                self.store.write("""
                    INSERT INTO branches (id, session_id, head_id, source_id, kind, anchor, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, new_branch, session_id, head, branch_id, kind, json.dumps(anchor), timestamp())
            self._emit(session_id, "branch_created", branch_id=new_branch, source_id=branch_id)
            return self.branch(session_id, new_branch)

    def start(self, session_id, branch_id, body, mode):
        limit = integer(body.get("limit", 10), "turn limit", 1, 2**31 - 1) if mode == "play" else 1
        with self.lock:
            session, branch = self._session(session_id), self._branch(session_id, branch_id)
            self._guard(session_id, branch, body)
            seat = self._seat(session, branch)
            if session["seats"][seat]["kind"] == "human":
                return {"status": "human_input", "seat": seat}
            run_id = new_id()
            with self.store.db:
                self.store.write("""
                    INSERT INTO runs (id, session_id, branch_id, mode, turn_limit, status, created_at)
                    VALUES (?, ?, ?, ?, ?, 'active', ?)
                """, run_id, session_id, branch_id, mode, limit, timestamp())
                self.store.write("UPDATE branches SET revision = revision + 1 WHERE id = ?", branch_id)
            cancellation = Cancellation()
            self.running[run_id] = cancellation
            self._emit(session_id, "run_started", branch_id=branch_id, run_id=run_id, seat=seat)
            thread = threading.Thread(target=self._work, args=(session, branch_id, run_id, cancellation), daemon=True)
            thread.start()
            return self._run(session_id, run_id)

    def _end_run(self, run_id, status, reason):
        self.store.write("UPDATE runs SET status = ?, reason = ?, ended_at = ? WHERE id = ?",
                         status, reason, timestamp(), run_id)

    def _end_attempt(self, attempt_id, status, reason, text, metadata=None):
        self.store.write("""
            UPDATE attempts SET status = ?, reason = ?, text = ?, metadata = ?, ended_at = ? WHERE id = ?
        """, status, reason, text, json.dumps(metadata) if metadata is not None else None, timestamp(), attempt_id)

    def stop(self, session_id, run_id):
        with self.lock:
            run = self._run(session_id, run_id)
            if run["status"] != "active":
                return run
            self._stop_run(run, "stopped", "operator_stop")
            return self._run(session_id, run_id)

    def _stop_run(self, run, status, reason):
        run_id = run["id"]
        draft = self.drafts.get(run_id)
        with self.store.db:
            if draft is not None:
                self._end_attempt(draft.attempt_id, status, reason, draft.text)
            self._end_run(run_id, status, reason)
        self.drafts.pop(run_id, None)
        self.running.pop(run_id).cancel()
        if draft is not None:
            self._emit(run["session_id"], "attempt_ended", branch_id=run["branch_id"], run_id=run_id,
                       attempt_id=draft.attempt_id, status=status, reason=reason)
        self._emit(run["session_id"], "run_ended", branch_id=run["branch_id"], run_id=run_id,
                   status=status, reason=reason)

    def _work(self, session, branch_id, run_id, cancellation):
        session_id = session["id"]
        while True:
            with self.lock:
                if run_id not in self.running:
                    return
                branch = self._branch(session_id, branch_id)
                seat = self._seat(session, branch)
                request = make_request(session, seat, self.store.history(branch["head_id"]))
                draft = Draft(new_id())
                with self.store.db:
                    self.store.write("""
                        INSERT INTO attempts
                        (id, run_id, branch_id, seat, starting_head_id, request, status, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
                    """, draft.attempt_id, run_id, branch_id, seat, branch["head_id"], json.dumps(request), timestamp())
                self.drafts[run_id] = draft
                self._emit(session_id, "attempt_started", branch_id=branch_id, run_id=run_id,
                           attempt_id=draft.attempt_id, seat=seat)

            def delta(text):
                return self._delta(session_id, branch_id, run_id, draft, text)

            try:
                result = self.adapters[request["adapter"]](request, delta, cancellation)
            except Exception:
                logging.getLogger(__name__).warning("Model adapter raised an unexpected error")
                result = Result("failed", "adapter_error")
            if not self._finish(session, branch, run_id, draft, seat, result):
                return

    def _delta(self, session_id, branch_id, run_id, draft, text):
        with self.lock:
            if self.drafts.get(run_id) is not draft:
                return False
            if not text:
                return True
            draft.text += require_text(text, "model output")
            now = time.monotonic()
            if now - draft.checkpoint >= 0.25:
                with self.store.db:
                    self.store.write("UPDATE attempts SET text = ? WHERE id = ?", draft.text, draft.attempt_id)
                draft.checkpoint = now
            self._emit(session_id, "text_delta", branch_id=branch_id, run_id=run_id,
                       attempt_id=draft.attempt_id, text=text)
            return True

    def _finish(self, session, starting_branch, run_id, draft, seat, result):
        session_id, branch_id = session["id"], starting_branch["id"]
        with self.lock:
            if self.drafts.get(run_id) is not draft:
                return False
            branch = self._branch(session_id, branch_id)
            run = self._run(session_id, run_id)
            if branch["head_id"] != starting_branch["head_id"]:
                result = Result("failed", "branch_head_changed")
            elif result.status == "complete" and not draft.text.strip():
                result = Result("failed", "blank_output", result.metadata)
            committed = result.status == "complete"
            turn_id = None
            reason = result.reason
            if committed:
                if run["mode"] == "next":
                    reason = "single_turn"
                elif run["committed_count"] + 1 >= run["turn_limit"]:
                    reason = "turn_limit"
                elif session["seats"][other(seat)]["kind"] == "human":
                    reason = "human_turn"
                else:
                    reason = None
            with self.store.db:
                self._end_attempt(draft.attempt_id, result.status, result.reason, draft.text, result.metadata)
                if committed:
                    turn_id = self._write_turn(session_id, branch["head_id"], seat, draft.text, "model",
                                               draft.attempt_id)
                    self._move_head(branch_id, turn_id)
                    self.store.write("UPDATE runs SET committed_count = committed_count + 1 WHERE id = ?", run_id)
                if reason is not None:
                    status = "complete" if committed else "failed"
                    self._end_run(run_id, status, reason)
            self.drafts.pop(run_id)
            self._emit(session_id, "attempt_ended", branch_id=branch_id, run_id=run_id,
                       attempt_id=draft.attempt_id, status=result.status, reason=result.reason)
            if turn_id is not None:
                self._emit(session_id, "turn_committed", branch_id=branch_id, run_id=run_id, turn_id=turn_id)
            if reason is not None:
                self.running.pop(run_id)
                self._emit(session_id, "run_ended", branch_id=branch_id, run_id=run_id, status=status, reason=reason)
                return False
            return True

    def subscribe(self, session_id):
        with self.lock:
            snapshot = {"type": "snapshot", "sequence": self.sequence, **self.session(session_id)}
            mailbox = queue.Queue(maxsize=256)
            self.subscribers.setdefault(session_id, set()).add(mailbox)
            return snapshot, mailbox

    def unsubscribe(self, session_id, mailbox):
        with self.lock:
            subscribers = self.subscribers.get(session_id)
            if subscribers is not None:
                subscribers.discard(mailbox)
                if not subscribers:
                    del self.subscribers[session_id]

    def _emit(self, session_id, kind, **payload):
        self.sequence += 1
        event = {"type": kind, "sequence": self.sequence, "session_id": session_id, **payload}
        for mailbox in tuple(self.subscribers.get(session_id, ())):
            try:
                mailbox.put_nowait(event)
            except queue.Full:
                self._disconnect(mailbox)
                self.unsubscribe(session_id, mailbox)

    @staticmethod
    def _disconnect(mailbox):
        while True:
            try:
                mailbox.get_nowait()
            except queue.Empty:
                mailbox.put_nowait(None)
                return

    def close(self):
        with self.lock:
            for run in self.store.all("SELECT * FROM runs WHERE status = 'active'"):
                self._stop_run(run, "interrupted", "service_shutdown")
            for mailboxes in self.subscribers.values():
                for mailbox in mailboxes:
                    self._disconnect(mailbox)
            self.subscribers.clear()
            self.store.close()
