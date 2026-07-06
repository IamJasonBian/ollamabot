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
  POST /v1/chat                 {"model": "hermes", "messages": [...]}
                                or shorthand {"model": "hermes", "prompt": "hi"}
"""
import argparse
import json
import logging
import pathlib
import secrets
import sys
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
    req = urllib.request.Request(
        f"{OLLAMA}/api/chat",
        data=json.dumps({"model": model, "messages": messages, "stream": False}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["message"]["content"].strip()


def ollama_tags():
    with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=10) as r:
        return [m["name"] for m in json.load(r)["models"]]


class Handler(BaseHTTPRequestHandler):
    server_version = "ollama-api/1.0"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self):
        client = self.auth()
        if not client:
            self.reply(401, {"error": "missing or invalid bearer token"})
            return
        if self.path != "/v1/chat":
            self.reply(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self.reply(400, {"error": "invalid JSON body"})
            return

        model_key = payload.get("model", DEFAULT_MODEL)
        model = MODELS.get(model_key, model_key)  # allow alias or raw ollama tag

        messages = payload.get("messages")
        if not messages and payload.get("prompt"):
            messages = [{"role": "user", "content": payload["prompt"]}]
        if not isinstance(messages, list) or not messages:
            self.reply(400, {"error": "provide 'messages' (list) or 'prompt' (string)"})
            return

        log.info("chat client=%s model=%s messages=%d", client, model, len(messages))
        try:
            content = ollama_chat(model, messages)
        except urllib.error.HTTPError as e:
            self.reply(502, {"error": f"ollama error: {e.read().decode(errors='replace')[:500]}"})
            return
        except Exception as e:
            self.reply(502, {"error": f"ollama unreachable: {e}"})
            return
        self.reply(200, {"model": model, "content": content})


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
