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

## Judge (Judge0-compatible Python runner)

`POST /submissions` accepts a Judge0 submission and answers in Judge0's response
shape, so a Judge0 client can use this server as its instance:

```bash
python3 server.py token new judge-leetcards     # the token name must start with "judge"
curl -s -X POST "http://localhost:8080/submissions?base64_encoded=false&wait=true" \
  -H "X-Auth-Token: $TOKEN" \
  -d '{"language_id": 71, "source_code": "print(6 * 7)"}'
# -> {"stdout": "42\n", "status": {"id": 3, "description": "Accepted"}, "time": "0.06", ...}
```

- Python only (`language_id` 71), always synchronous, at most two programs at
  once. CPU, wall time, memory (512 MB cap) and output (1 MB) are limited. Set
  `JUDGE_PYTHON` to choose the interpreter.
- On macOS each program runs under `sandbox-exec` in its own temporary
  directory with the system Python (3.9): no network, no fork or subprocesses,
  no reads outside the system and the interpreter, no writes outside its
  directory. This is a macOS sandbox, not Judge0's `isolate`.
- On Linux each program runs as a transient systemd unit (`systemd-run`) with a
  throwaway user, no network, a read-only system, a private memory-backed
  `/tmp`, and cgroup limits. The server must run as root, so it gets a small VM
  of its own; `setup.sh` builds that on a Mac.
- `python3 test_judge.py` (as root on Linux) checks the containment on the
  machine it runs on; run it after an OS update.
- Chat tokens are refused (403), since they can end up in browser frontends.
  The token may be sent as `Authorization: Bearer` or `X-Auth-Token`.

### Linux judge on a Mac: `setup.sh`

```bash
./setup.sh               # install or update everything, then run the tests
./setup.sh start         # start the VM and service (after a reboot)
./setup.sh test          # containment tests in the VM + a request from the Mac
./setup.sh token         # print the judge token
./setup.sh token rotate  # replace the token and print the new one
./setup.sh status
```

It installs Lima into `~/.local/lima` (no sudo), creates an Ubuntu VM named
`judge` with nothing from the Mac mounted, installs Python 3.8, and runs
`server.py` as `judge.service`. Lima forwards the port to `127.0.0.1:2358` on
the Mac only. Remove it all with `limactl delete -f judge`.

**Giving the token to a backend.** The token never leaves the VM unless you
print it. Put it in the backend's server-side environment, never in a frontend
build variable (a `VITE_*` value is shipped to every browser):

```bash
JUDGE0_URL=http://127.0.0.1:2358          # or wherever the port is reachable from
JUDGE0_AUTH_TOKEN=$(./setup.sh token)
```

A backend on another machine reaches it through a tunnel, for example
`ssh -N -L 2358:127.0.0.1:2358 <mac>`; a hosted backend needs the port
published (for example Tailscale Funnel, which adds TLS).

**Ways this breaks, and what to do**

| What happens | Why | Fix |
|---|---|---|
| Backend reports "Judge unavailable: fetch failed" after the Mac restarts | the VM does not start by itself | `./setup.sh start` |
| Same, on a hosted backend | the port is loopback-only on the Mac | publish it or tunnel to it |
| "auth rejected" | token rotated or mistyped | `./setup.sh token` and update the backend |
| "judge is busy" under load, then the backend's circuit breaker opens | two programs run at once; a third waits up to 20 s, longer than the backend's 9 s timeout | raise `JUDGE_MAX_CONCURRENT` in `judge.service` (the VM has 4 CPUs) |
| A solution that works on LeetCode fails here | Python 3.8 lacks newer features (`match`, `functools.cache`, `int.bit_count`) | set `JUDGE_PYTHON` to a newer interpreter |
| `./setup.sh` fails at Python 3.8 | the deadsnakes archive dropped 3.8 for this Ubuntu release | install another Python and set `JUDGE_PYTHON` |
| Containment tests fail after an update | a systemd or kernel change | do not expose the judge until `./setup.sh test` passes |

## Reaching the server remotely

The server binds `0.0.0.0:8080` with token auth but no TLS. To let people outside
your LAN use it, put it behind a tunnel — e.g. `cloudflared tunnel`, Tailscale
(`tailscale funnel 8080`), or ngrok — and hand out that URL as `OLLAMABOT_URL`.
