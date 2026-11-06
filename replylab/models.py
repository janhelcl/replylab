import http.client
import json
import os
import socket
import threading
import time
from dataclasses import dataclass


class Invalid(ValueError):
    pass


def require_text(value, field, *, nonblank=False):
    if not isinstance(value, str) or (nonblank and not value.strip()):
        raise Invalid(f"{field} must be {'nonblank ' if nonblank else ''}text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise Invalid(f"{field} contains invalid Unicode") from None
    return value


def integer(value, field, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise Invalid(f"{field} must be an integer from {minimum} to {maximum}")
    return value


def normalize_seat(value):
    if not isinstance(value, dict):
        raise Invalid("each seat must be a human or AI configuration")
    kind = value.get("kind")
    if kind == "human":
        return {"kind": "human"}
    if kind != "ai":
        raise Invalid("seat kind must be human or ai")
    adapter = value.get("adapter", "fake")
    if adapter not in ("fake", "openai"):
        raise Invalid("adapter must be fake or openai")
    default_model = "echo" if adapter == "fake" else None
    model = require_text(value.get("model", default_model), "model", nonblank=True).strip()
    instructions = require_text(value.get("instructions", ""), "instructions")
    options = value.get("options", {})
    if not isinstance(options, dict):
        raise Invalid("generation options must be an object")
    if adapter == "fake":
        if options.keys() - {"delay_ms"}:
            raise Invalid("the fake adapter accepts only delay_ms")
        options = {"delay_ms": integer(options.get("delay_ms", 90), "delay_ms", 0, 5000)}
    else:
        if options.keys() - {"max_completion_tokens", "temperature", "top_p"}:
            raise Invalid("OpenAI options are max_completion_tokens, temperature, and top_p")
        options = dict(options)
        options["max_completion_tokens"] = integer(
            options.get("max_completion_tokens", 1024), "max_completion_tokens", 1, 1000000
        )
        for key, maximum in (("temperature", 2), ("top_p", 1)):
            if key in options:
                number = options[key]
                if type(number) not in (int, float) or not 0 <= number <= maximum:
                    raise Invalid(f"{key} must be between 0 and {maximum}")
    return {"kind": "ai", "adapter": adapter, "model": model, "instructions": instructions,
            "options": options}


def make_request(session, seat, turns):
    config = session["seats"][seat]
    messages = []
    if seat == session["starter"] and session["opening_prompt"] is not None:
        messages.append({"role": "user", "content": session["opening_prompt"]})
    messages.extend(
        {"role": "assistant" if turn["seat"] == seat else "user", "content": turn["text"]}
        for turn in turns
    )
    body = {"model": config["model"], "messages": messages, **config["options"]}
    if config["adapter"] == "openai":
        body["messages"] = list(messages)
        if config["instructions"]:
            body["messages"].insert(0, {"role": "developer", "content": config["instructions"]})
        body.update(stream=True, stream_options={"include_usage": True})
    return {
        "adapter": config["adapter"], "model": config["model"], "options": config["options"],
        "instructions": config["instructions"], "messages": messages, "body": body,
    }


@dataclass
class Result:
    status: str
    reason: str
    metadata: dict | None = None


class Cancellation:
    def __init__(self):
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.abort = None

    def cancel(self):
        with self.lock:
            self.event.set()
            if self.abort is not None:
                self.abort()

    def set_abort(self, abort):
        with self.lock:
            self.abort = abort
            if self.event.is_set():
                abort()


def fake(request, delta, cancellation):
    last = request["messages"][-1]["content"]
    quoted = last[:100]
    parts = (f'[{request["model"]}] ', "I received: ", quoted, "\nWhat follows from that?")
    for part in parts:
        if cancellation.event.wait(request["options"]["delay_ms"] / 1000) or not delta(part):
            return Result("incomplete", "cancelled")
    return Result("complete", "stop")


def openai(request, delta, cancellation):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return Result("failed", "missing_openai_api_key")
    if cancellation.event.is_set():
        return Result("incomplete", "cancelled")

    connection = http.client.HTTPSConnection("api.openai.com", timeout=45)
    try:
        connection.request(
            "POST", "/v1/chat/completions", json.dumps(request["body"]).encode(),
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
             "Accept": "text/event-stream"},
        )
        upstream_socket = connection.sock

        def abort():
            if upstream_socket is not None:
                try:
                    upstream_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        cancellation.set_abort(abort)
        with connection.getresponse() as response:
            if response.status != 200:
                payload = response.read(65536)
                try:
                    code = json.loads(payload).get("error", {}).get("code")
                except (ValueError, AttributeError):
                    code = None
                reason = "context_limit" if code == "context_length_exceeded" else f"openai_http_{response.status}"
                return Result("failed", reason)
            return read_openai_stream(response, delta, cancellation)
    except TimeoutError:
        return Result("failed", "provider_timeout")
    except (OSError, http.client.HTTPException, ValueError, AttributeError, TypeError):
        return Result("failed", "provider_connection_or_response_error")
    finally:
        connection.close()


def read_openai_stream(response, delta, cancellation):
    metadata = {}
    finish = None
    done = False
    deadline = time.monotonic() + 180
    for line in response:
        if cancellation.event.is_set():
            return Result("incomplete", "cancelled")
        if time.monotonic() >= deadline:
            return Result("failed", "provider_timeout", metadata)
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if data == b"[DONE]":
            done = True
            break
        chunk = json.loads(data)
        if chunk.get("error"):
            return Result("failed", "openai_stream_error", metadata)
        for field in ("id", "model", "usage", "system_fingerprint"):
            if chunk.get(field) is not None:
                metadata[field] = chunk[field]
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            payload = choice.get("delta") or {}
            for field in ("content", "refusal"):
                part = payload.get(field)
                if part and not delta(part):
                    return Result("incomplete", "cancelled", metadata)
            finish = choice.get("finish_reason") or finish
    if not done:
        return Result("incomplete", "stream_ended_early", metadata)
    if finish == "stop":
        return Result("complete", "stop", metadata)
    if finish in ("length", "content_filter", "tool_calls", "function_call"):
        return Result("incomplete", finish, metadata)
    return Result("incomplete", "missing_finish_reason", metadata)


ADAPTERS = {"fake": fake, "openai": openai}
