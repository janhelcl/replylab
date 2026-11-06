import argparse
import json
import os
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .server import serve


class Client:
    def __init__(self, url):
        self.url = url.rstrip("/") + "/api"

    def open(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        headers = {} if data is None else {"Content-Type": "application/json"}
        try:
            return urlopen(Request(self.url + path, data=data, headers=headers), timeout=30)
        except HTTPError as error:
            with error:
                try:
                    message = json.load(error)["error"]
                except (ValueError, KeyError):
                    message = f"HTTP {error.code}"
            raise ValueError(message) from None

    def call(self, path, body=None):
        with self.open(path, body) as response:
            return json.load(response)

    def watch(self, session_id):
        while True:
            try:
                with self.open(f"/sessions/{session_id}/events") as response:
                    for line in response:
                        if line.startswith(b"data: "):
                            print(line[6:].decode().rstrip(), flush=True)
            except OSError as error:
                print(f"Stream disconnected ({error}); reconnecting", file=sys.stderr)
            time.sleep(0.5)


def parser():
    root = argparse.ArgumentParser(prog="replylab", description="Run and branch a two-seat chat.")
    root.add_argument("--url", default=os.environ.get("REPLYLAB_URL", "http://127.0.0.1:8765"))
    commands = root.add_subparsers(dest="command", required=True)

    server = commands.add_parser("serve", help="start the local service and browser UI")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--db", default=os.environ.get("REPLYLAB_DB", ".replylab/replylab.sqlite3"))

    create = commands.add_parser("create", help="create a session")
    create.add_argument("--starter", choices=("A", "B"), default="A")
    create.add_argument("--prompt", help="opening prompt for the starting seat")
    for side in ("a", "b"):
        create.add_argument(f"--{side}", choices=("human", "fake", "openai"), default="human")
        create.add_argument(f"--{side}-model")
        create.add_argument(f"--{side}-instructions", default="")
        create.add_argument(f"--{side}-options", type=json.loads, default={})

    commands.add_parser("sessions", help="list sessions")
    for name in ("branches", "show", "send", "next", "play", "stop", "fork", "attempt", "watch"):
        command = commands.add_parser(name)
        command.add_argument("session")
        if name in ("show", "send", "next", "play", "fork"):
            command.add_argument("branch")
        if name == "show":
            command.add_argument("--json", action="store_true")
        if name == "send":
            command.add_argument("text", help="message text or - to read stdin")
            command.add_argument("--seat", choices=("A", "B"), help="defaults to the next seat")
        if name == "play":
            command.add_argument("--limit", type=int, default=10)
        if name == "stop":
            command.add_argument("run", nargs="?", help="defaults to the session's active run")
        if name == "attempt":
            command.add_argument("attempt_id")
        if name == "fork":
            anchor = command.add_mutually_exclusive_group(required=True)
            anchor.add_argument("--start", action="store_true")
            anchor.add_argument("--before", type=int, metavar="TURN")
            anchor.add_argument("--after", type=int, metavar="TURN")
            anchor.add_argument("--at", metavar="TURN:OFFSET")
            anchor.add_argument("--partial", metavar="ATTEMPT:OFFSET")
            anchor.add_argument("--edit", type=int, metavar="TURN")
            command.add_argument("--text", help="replacement text for --edit or - to read stdin")
    return root


def seat_config(args, side):
    adapter = getattr(args, side)
    if adapter == "human":
        return {"kind": "human"}
    config = {"kind": "ai", "adapter": adapter,
              "instructions": getattr(args, f"{side}_instructions"),
              "options": getattr(args, f"{side}_options")}
    model = getattr(args, f"{side}_model")
    if model is not None:
        config["model"] = model
    return config


def input_text(text):
    return sys.stdin.read() if text == "-" else text


def numbered_turn(view, number):
    if not 1 <= number <= len(view["turns"]):
        raise ValueError(f"Turn {number} is not on the branch")
    return view["turns"][number - 1]


def fork_body(args, view):
    if args.edit is not None:
        if args.text is None:
            raise ValueError("--edit requires --text")
        turn = numbered_turn(view, args.edit)
        return {"kind": "edit", "turn_id": turn["id"], "text": input_text(args.text)}
    if args.text is not None:
        raise ValueError("--text applies to --edit")
    if args.start:
        return {"kind": "start"}
    if args.partial is not None:
        attempt, offset = args.partial.rsplit(":", 1)
        return {"kind": "cursor", "attempt_id": attempt, "offset": int(offset)}
    if args.at is not None:
        number, offset = args.at.rsplit(":", 1)
        turn = numbered_turn(view, int(number))
        return {"kind": "cursor", "turn_id": turn["id"], "offset": int(offset)}
    number = args.before if args.before is not None else args.after
    turn = numbered_turn(view, number)
    offset = 0 if args.before is not None else len(turn["text"])
    return {"kind": "cursor", "turn_id": turn["id"], "offset": offset}


def show(view, session):
    branch = view["branch"]
    print(f"Branch {branch['id']} ({branch['kind']}, revision {branch['revision']})")
    if branch["source_id"]:
        print(f"From {branch['source_id']} at {json.dumps(branch['anchor'])}")
    prompt = session["session"]["opening_prompt"]
    if prompt is not None:
        print(f"Opening prompt for {session['session']['starter']}: {prompt}")
    for number, turn in enumerate(view["turns"], 1):
        print(f"{number}. {turn['seat']}: {turn['text']}")
        if turn["attempt_id"]:
            print(f"   Attempt {turn['attempt_id']}")
    for attempt in view["attempts"]:
        if attempt["status"] != "complete":
            print(f"Attempt {attempt['id']}: {attempt['status']} ({attempt['reason'] or 'streaming'})")
    run = session["run"]
    if run is not None:
        print(f"Active run {run['id']} on {run['branch_id']}")
        if session["draft"] and run["branch_id"] == branch["id"]:
            print(f"Draft {session['draft']['seat']}: {session['draft']['text']}")
    elif view["last_run"]:
        print(f"Last run: {view['last_run']['status']} ({view['last_run']['reason']})")
    print(f"Next seat: {view['next_seat']}")


def execute(args, client):
    command = args.command
    if command == "create":
        return client.call("/sessions", {"seats": {seat.upper(): seat_config(args, seat) for seat in ("a", "b")},
                                         "starter": args.starter, "opening_prompt": args.prompt})
    if command == "sessions":
        return client.call("/sessions")
    if command == "watch":
        client.watch(args.session)
        return None
    session = f"/sessions/{args.session}"
    if command == "branches":
        return client.call(session)
    if command == "attempt":
        return client.call(f"{session}/attempts/{args.attempt_id}")
    if command == "stop":
        run = args.run
        if run is None:
            active = client.call(session)["run"]
            if active is None:
                return {"status": "idle"}
            run = active["id"]
        return client.call(f"{session}/runs/{run}/stop", {})
    branch = f"{session}/branches/{args.branch}"
    view = client.call(branch)
    revision = view["branch"]["revision"]
    if command == "show":
        if args.json:
            return view
        show(view, client.call(session))
        return None
    if command == "send":
        return client.call(branch + "/send", {"revision": revision,
                           "seat": args.seat or view["next_seat"], "text": input_text(args.text)})
    if command == "fork":
        return client.call(branch + "/fork", fork_body(args, view))
    body = {"revision": revision}
    if command == "play":
        body["limit"] = args.limit
    result = client.call(f"{branch}/{command}", body)
    if result["status"] == "human_input" and sys.stdin.isatty():
        setup = client.call(session)["session"]
        if not view["turns"] and setup["opening_prompt"] is not None:
            print(f"Opening prompt: {setup['opening_prompt']}")
        try:
            message = input(f"{result['seat']} > ")
        except EOFError:
            return result
        if message.strip():
            return client.call(branch + "/send", {"revision": revision, "seat": result["seat"], "text": message})
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "serve":
            serve(args.port, args.db)
        else:
            result = execute(args, Client(args.url))
            if result is not None:
                print(json.dumps(result, indent=2, ensure_ascii=False))
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as error:
        print(f"replylab: {error}", file=sys.stderr)
        return 1
    return 0
