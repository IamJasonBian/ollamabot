# ollamabot

HTTP API + client SDK for local [ollama](https://ollama.com) models behind bearer-token auth.
The API server (`server.py`) runs next to ollama; clients hit it with plain HTTP from anywhere
that can reach the host.

## Server (on the machine running ollama)

```sh
# mint a token for each client/person
python3 server.py token new alice
# -> prints the token; give it to alice

# run the API (default port 8080)
python3 server.py serve
```

Token management: `token new <name>`, `token list`, `token revoke <name>`.
Tokens live in `tokens.json` next to the script (never commit it).

## API

All `/v1/*` endpoints require `Authorization: Bearer <token>`.

| Method | Path            | Body                                             | Returns                     |
|--------|-----------------|--------------------------------------------------|-----------------------------|
| GET    | `/healthz`      | —                                                | `{"ok": true}` (no auth)    |
| GET    | `/v1/models`    | —                                                | aliases, default, ollama tags |
| POST   | `/v1/chat`      | `{"model": "hermes", "prompt": "hi"}` or `{"model": ..., "messages": [...]}` | `{"model", "content", "thinking"}` (synchronous) |
| POST   | `/v1/jobs`      | same body as `/v1/chat`                          | `202 {"job_id", "status": "queued"}` |
| GET    | `/v1/jobs/<id>` | —                                                | `{"status": queued\|running\|done\|error, "content", "thinking", "elapsed", ...}` |

`model` accepts an alias (`hermes`, `qwen`) or a raw ollama tag (`hermes3:8b`).

**Response model.** Reasoning models (qwen3) think before answering; the API always
splits that out: `content` is the clean reply, `thinking` holds the reasoning (or
`null`). **Use jobs + polling for anything nontrivial** — proxies like Cloudflare
kill HTTP requests at ~100s, and local generation can take longer than that.
`/v1/chat` is fine for short prompts. Finished jobs stay fetchable for 1 hour.

### curl quickstart

```sh
export TOKEN=<your token>
export URL=http://<server-host>:8080

curl -s $URL/healthz
curl -s -H "Authorization: Bearer $TOKEN" $URL/v1/models

# short prompt, synchronous
curl -s -H "Authorization: Bearer $TOKEN" \
     -d '{"model": "hermes", "prompt": "why is the sky blue?"}' \
     $URL/v1/chat

# anything bigger: submit a job, then poll
curl -s -H "Authorization: Bearer $TOKEN" \
     -d '{"model": "qwen", "prompt": "plan a 3-day trip to Vancouver"}' \
     $URL/v1/jobs
# -> {"job_id": "abc...", "status": "queued"}
curl -s -H "Authorization: Bearer $TOKEN" $URL/v1/jobs/abc...
# poll until -> {"status": "done", "content": "...", "thinking": "...", "elapsed": 42.0}
```

## Client SDK + CLI

One-line install (needs pipx or pip):

```sh
curl -sSL https://raw.githubusercontent.com/IamJasonBian/ollamabot/main/install.sh | sh
```

or directly:

```sh
pip install "git+https://github.com/IamJasonBian/ollamabot.git#subdirectory=sdk"
```

### CLI

```sh
export OLLAMABOT_URL=http://<server-host>:8080
export OLLAMABOT_TOKEN=<your token>

ollamabot models
ollamabot chat "why is the sky blue?"          # job + polling (default, timeout-proof)
ollamabot chat -m qwen --thinking "write a haiku"
ollamabot chat --sync "quick one-liner"        # single blocking request
echo "summarize: ..." | ollamabot chat
```

### Python

```python
from ollamabot import Client

c = Client("https://<public-url>", token="<your token>")
print(c.models())

# recommended: job + polling, returns the full result dict
r = c.ask("why is the sky blue?", model="qwen")
print(r["content"], r["thinking"], r["elapsed"])

# or manage the job yourself
job_id = c.submit("long question...")
status = c.job(job_id)          # poll until status["status"] == "done"

# short prompts only: synchronous, returns just the text
print(c.chat("hi", model="hermes"))
```

## Reaching the server remotely

The server binds `0.0.0.0:8080` with token auth but no TLS. To let people outside
your LAN use it, put it behind a tunnel — e.g. `cloudflared tunnel`, Tailscale
(`tailscale funnel 8080`), or ngrok — and hand out that URL as `OLLAMABOT_URL`.
