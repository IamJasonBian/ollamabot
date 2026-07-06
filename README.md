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

| Method | Path         | Body                                             | Returns                     |
|--------|--------------|--------------------------------------------------|-----------------------------|
| GET    | `/healthz`   | —                                                | `{"ok": true}` (no auth)    |
| GET    | `/v1/models` | —                                                | aliases, default, ollama tags |
| POST   | `/v1/chat`   | `{"model": "hermes", "prompt": "hi"}` or `{"model": ..., "messages": [...]}` | `{"model": ..., "content": ...}` |

`model` accepts an alias (`hermes`, `qwen`) or a raw ollama tag (`hermes3:8b`).

### curl quickstart

```sh
export TOKEN=<your token>
export URL=http://<server-host>:8080

curl -s $URL/healthz
curl -s -H "Authorization: Bearer $TOKEN" $URL/v1/models
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"model": "hermes", "prompt": "why is the sky blue?"}' \
     $URL/v1/chat
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
ollamabot chat "why is the sky blue?"
ollamabot chat -m qwen "write a haiku"
echo "summarize: ..." | ollamabot chat
```

### Python

```python
from ollamabot import Client

c = Client("http://<server-host>:8080", token="<your token>")
print(c.models())
print(c.chat("why is the sky blue?", model="hermes"))
print(c.chat(messages=[{"role": "user", "content": "hi"}]))
```

## Reaching the server remotely

The server binds `0.0.0.0:8080` with token auth but no TLS. To let people outside
your LAN use it, put it behind a tunnel — e.g. `cloudflared tunnel`, Tailscale
(`tailscale funnel 8080`), or ngrok — and hand out that URL as `OLLAMABOT_URL`.
