#!/usr/bin/env python3
"""HTTP API server exposing local ollama models with bearer-token auth.

Runs alongside bot.py; both talk to the same ollama instance.

Usage:
  python3 server.py serve [--port 8080]      run the API server
  python3 server.py token new <name>         mint a token for a client
  python3 server.py token list               list issued tokens
  python3 server.py token revoke <name>      revoke a token

API (all /v1/* require "Authorization: Bearer <token>"):
  GET  /healthz                 no auth, liveness check
  GET  /v1/models               model aliases + underlying ollama tags
  POST /v1/chat                 synchronous; {"model": "hermes", "messages": [...]}
                                or shorthand {"model": "hermes", "prompt": "hi"}.
                                Short prompts only — proxies (Cloudflare) kill
                                requests at ~100s.
  POST /v1/jobs                 same body; returns {"job_id", "status"} at once
  GET  /v1/jobs/<id>            poll until "status" is "done" (or "error")

Responses separate model reasoning from the answer: "content" is the clean
reply, "thinking" holds any <think>...</think> block (qwen3 emits these).
"""
import argparse
import json
import logging
import pathlib
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OLLAMA = "http://localhost:11434"

MODELS = {
    "hermes": "hermes3:8b",
    "qwen": "qwen3:8b",
}
DEFAULT_MODEL = "hermes"

TOKENS_FILE = pathlib.Path(__file__).parent / "tokens.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ollama-api")


def load_tokens():
    if TOKENS_FILE.exists():
        return json.loads(TOKENS_FILE.read_text())
    return {}


def save_tokens(tokens):
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2) + "\n")


def ollama_chat(model, messages):
    """Returns (content, thinking). Ollama reports reasoning either as a separate
    message.thinking field (current versions) or inline <think> tags (older)."""
    req = urllib.request.Request(
        f"{OLLAMA}/api/chat",
        data=json.dumps({"model": model, "messages": messages, "stream": False}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        msg = json.load(r)["message"]
    content, inline_thinking = split_thinking(msg["content"].strip())
    return content, msg.get("thinking", "").strip() or inline_thinking


def ollama_tags():
    with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=10) as r:
        return [m["name"] for m in json.load(r)["models"]]


THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def split_thinking(text):
    """Return (content, thinking) with <think> blocks pulled out of the reply."""
    thinking = "\n".join(m.strip() for m in THINK_RE.findall(text)) or None
    return THINK_RE.sub("", text).strip(), thinking


JOB_TTL = 3600  # seconds a finished job stays fetchable
jobs = {}       # job_id -> dict
jobs_lock = threading.Lock()


def prune_jobs():
    cutoff = time.time() - JOB_TTL
    with jobs_lock:
        for jid in [j for j, v in jobs.items() if v["created"] < cutoff]:
            del jobs[jid]


def run_job(job_id, model, messages):
    with jobs_lock:
        jobs[job_id]["status"] = "running"
    try:
        content, thinking = ollama_chat(model, messages)
        update = {"status": "done", "content": content, "thinking": thinking}
    except Exception as e:
        update = {"status": "error", "error": str(e)}
    with jobs_lock:
        jobs[job_id].update(update, elapsed=round(time.time() - jobs[job_id]["created"], 1))


class Handler(BaseHTTPRequestHandler):
    server_version = "ollama-api/1.0"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # CORS preflight for browser frontends; auth still enforced on the real request
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def auth(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        token = header[len("Bearer "):].strip()
        for name, t in load_tokens().items():
            if secrets.compare_digest(t, token):
                return name
        return None

    def do_GET(self):
        if self.path == "/healthz":
            self.reply(200, {"ok": True})
            return
        client = self.auth()
        if not client:
            self.reply(401, {"error": "missing or invalid bearer token"})
            return
        if self.path == "/v1/models":
            try:
                tags = ollama_tags()
            except Exception as e:
                self.reply(502, {"error": f"ollama unreachable: {e}"})
                return
            self.reply(200, {"aliases": MODELS, "default": DEFAULT_MODEL, "ollama_tags": tags})
        elif self.path.startswith("/v1/jobs/"):
            job_id = self.path[len("/v1/jobs/"):]
            with jobs_lock:
                job = jobs.get(job_id)
                snapshot = dict(job) if job else None
            if not snapshot:
                self.reply(404, {"error": "no such job (expired or never existed)"})
                return
            self.reply(200, snapshot)
        else:
            self.reply(404, {"error": "not found"})

    def parse_chat_body(self):
        """Returns (model, messages) or None after sending an error reply."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self.reply(400, {"error": "invalid JSON body"})
            return None
        model_key = payload.get("model", DEFAULT_MODEL)
        model = MODELS.get(model_key, model_key)  # allow alias or raw ollama tag
        messages = payload.get("messages")
        if not messages and payload.get("prompt"):
            messages = [{"role": "user", "content": payload["prompt"]}]
        if not isinstance(messages, list) or not messages:
            self.reply(400, {"error": "provide 'messages' (list) or 'prompt' (string)"})
            return None
        return model, messages

    def do_POST(self):
        client = self.auth()
        if not client:
            self.reply(401, {"error": "missing or invalid bearer token"})
            return

        if self.path == "/v1/chat":
            parsed = self.parse_chat_body()
            if not parsed:
                return
            model, messages = parsed
            log.info("chat client=%s model=%s messages=%d", client, model, len(messages))
            try:
                content, thinking = ollama_chat(model, messages)
            except urllib.error.HTTPError as e:
                self.reply(502, {"error": f"ollama error: {e.read().decode(errors='replace')[:500]}"})
                return
            except Exception as e:
                self.reply(502, {"error": f"ollama unreachable: {e}"})
                return
            self.reply(200, {"model": model, "content": content, "thinking": thinking})
        elif self.path == "/v1/jobs":
            parsed = self.parse_chat_body()
            if not parsed:
                return
            model, messages = parsed
            prune_jobs()
            job_id = secrets.token_urlsafe(16)
            with jobs_lock:
                jobs[job_id] = {"job_id": job_id, "status": "queued", "model": model,
                                "created": time.time()}
            threading.Thread(target=run_job, args=(job_id, model, messages), daemon=True).start()
            log.info("job=%s client=%s model=%s messages=%d", job_id, client, model, len(messages))
            self.reply(202, {"job_id": job_id, "status": "queued"})
        else:
            self.reply(404, {"error": "not found"})


def cmd_token(args):
    tokens = load_tokens()
    if args.action == "new":
        if args.name in tokens:
            sys.exit(f"token '{args.name}' already exists; revoke it first")
        tokens[args.name] = secrets.token_urlsafe(32)
        save_tokens(tokens)
        print(tokens[args.name])
    elif args.action == "list":
        for name, t in tokens.items():
            print(f"{name}\t{t[:8]}...")
    elif args.action == "revoke":
        if tokens.pop(args.name, None) is None:
            sys.exit(f"no token named '{args.name}'")
        save_tokens(tokens)
        print(f"revoked {args.name}")


def cmd_serve(args):
    if not load_tokens():
        log.warning("no tokens issued yet — mint one with: python3 server.py token new <name>")
    log.info("serving on 0.0.0.0:%d (models: %s)", args.port, MODELS)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8080)
    serve.set_defaults(func=cmd_serve)

    token = sub.add_parser("token")
    token.add_argument("action", choices=["new", "list", "revoke"])
    token.add_argument("name", nargs="?")
    token.set_defaults(func=cmd_token)

    args = p.parse_args()
    if args.cmd == "token" and args.action in ("new", "revoke") and not args.name:
        p.error("token new/revoke requires a name")
    args.func(args)


if __name__ == "__main__":
    main()
