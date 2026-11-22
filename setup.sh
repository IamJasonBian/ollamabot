#!/usr/bin/env bash
# Set up, start and test the Linux judge on this Mac.
#
#   ./setup.sh               install or update everything, then run the tests
#   ./setup.sh start         start the VM and service (use after a reboot)
#   ./setup.sh test          containment tests in the VM + a request from the Mac
#   ./setup.sh token         print the judge token (to paste into the backend's env)
#   ./setup.sh token rotate  replace the token, then print the new one
#   ./setup.sh status        VM, service and port state
#
# What it builds: Lima in ~/.local/lima (no sudo), a small Ubuntu VM with no
# folders from this Mac mounted, Python 3.8 in it, and server.py + judge.py
# running as a systemd service. Lima forwards the port to 127.0.0.1 on this Mac.
#
# Environment: JUDGE_VM (default judge), JUDGE_PORT (2358),
# JUDGE_TOKEN_NAME (judge-leetcards), JUDGE_CPUS (4), JUDGE_MEMORY_GB (4).
set -euo pipefail

VM=${JUDGE_VM:-judge}
PORT=${JUDGE_PORT:-2358}
TOKEN_NAME=${JUDGE_TOKEN_NAME:-judge-leetcards}
CPUS=${JUDGE_CPUS:-4}
MEMORY_GB=${JUDGE_MEMORY_GB:-4}
LIMA_VERSION=2.3.0
HERE=$(cd "$(dirname "$0")" && pwd)
export PATH="$HOME/.local/bin:$PATH"

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
# Every limactl call gets /dev/null on stdin unless it is deliberately fed.
vm() { limactl shell "$VM" "$@" < /dev/null; }

install_lima() {
  if command -v limactl > /dev/null; then return; fi
  [ "$(uname -sm)" = "Darwin arm64" ] || die "this script installs Lima for Apple Silicon macOS only"
  say "installing Lima $LIMA_VERSION into ~/.local/lima"
  local file="lima-$LIMA_VERSION-Darwin-arm64.tar.gz" work
  work=$(mktemp -d)
  curl -fsSL -o "$work/$file" "https://github.com/lima-vm/lima/releases/download/v$LIMA_VERSION/$file"
  curl -fsSL -o "$work/SHA256SUMS" "https://github.com/lima-vm/lima/releases/download/v$LIMA_VERSION/SHA256SUMS"
  (cd "$work" && grep " $file\$" SHA256SUMS | shasum -a 256 -c - > /dev/null) || die "Lima checksum mismatch"
  mkdir -p "$HOME/.local/lima" "$HOME/.local/bin"
  tar -xzf "$work/$file" -C "$HOME/.local/lima"
  ln -sf "$HOME/.local/lima/bin/limactl" "$HOME/.local/bin/limactl"
  rm -rf "$work"
}

vm_status() { limactl list --format '{{.Name}} {{.Status}}' 2> /dev/null | awk -v n="$VM" '$1 == n {print $2}'; }

start_vm() {
  case "$(vm_status)" in
    Running) ;;
    "")
      say "creating VM $VM ($CPUS CPUs, ${MEMORY_GB} GB, no host mounts)"
      limactl start --name="$VM" --tty=false --cpus="$CPUS" --memory="$MEMORY_GB" --disk=20 \
        --vm-type=vz --mount-none --containerd=none template://ubuntu-lts < /dev/null > /dev/null 2>&1 \
        || die "could not create the VM; run: limactl start --name=$VM template://ubuntu-lts"
      ;;
    *)
      say "starting VM $VM"
      limactl start "$VM" < /dev/null > /dev/null 2>&1 || die "could not start the VM"
      ;;
  esac
}

wait_healthy() {
  local i
  for i in $(seq 1 30); do
    if curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" 2> /dev/null | grep -q true; then return 0; fi
    sleep 1
  done
  die "judge did not answer on http://127.0.0.1:$PORT/healthz (check: ./setup.sh status)"
}

provision() {
  say "installing Python 3.8 in the VM (deadsnakes archive)"
  vm bash -c '
    set -e
    export DEBIAN_FRONTEND=noninteractive
    if ! command -v python3.8 > /dev/null; then
      sudo add-apt-repository -y ppa:deadsnakes/ppa > /dev/null 2>&1
      sudo apt-get install -y -qq python3.8 > /dev/null 2>&1
    fi
    python3.8 --version > /dev/null
    sudo mkdir -p /opt/judge
    rm -rf /tmp/judge-in && mkdir -p /tmp/judge-in
  ' || die "Python 3.8 is not installable in the VM; set JUDGE_PYTHON in the unit to another interpreter"

  say "copying server.py, judge.py and test_judge.py to /opt/judge"
  local f
  for f in server.py judge.py test_judge.py; do
    limactl copy "$HERE/$f" "$VM:/tmp/judge-in/$f" < /dev/null
  done
  vm bash -c 'sudo install -m 0644 -o root -g root /tmp/judge-in/*.py /opt/judge/ && rm -rf /tmp/judge-in'

  say "installing judge.service on port $PORT"
  limactl shell "$VM" sudo tee /etc/systemd/system/judge.service > /dev/null << UNIT
[Unit]
Description=Judge0-compatible Python runner
After=network.target

[Service]
WorkingDirectory=/opt/judge
ExecStart=/usr/bin/python3 /opt/judge/server.py serve --port $PORT
Environment=JUDGE_PYTHON=/usr/bin/python3.8
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT
  vm bash -c "
    set -e
    cd /opt/judge
    if ! sudo python3 server.py token list | cut -f1 | grep -qx '$TOKEN_NAME'; then
      sudo python3 server.py token new '$TOKEN_NAME' > /dev/null
      echo '    minted token $TOKEN_NAME'
    fi
    sudo chmod 600 /opt/judge/tokens.json
    sudo systemctl daemon-reload
    sudo systemctl enable -q judge.service
    sudo systemctl restart judge.service
  "
  wait_healthy
}

read_token() {
  vm sudo python3 -c "import json; print(json.load(open('/opt/judge/tokens.json'))['$TOKEN_NAME'])" 2> /dev/null \
    || die "no token named $TOKEN_NAME; run ./setup.sh"
}

run_tests() {
  say "containment tests inside the VM"
  vm bash -c 'cd /opt/judge && sudo python3 test_judge.py' | awk '
    /^ok/ {ok++} /^FAIL/ {bad++; print} /^ {7}/ {print} /^platform/ {print "    " $0}
    END {printf "    %d passed, %d failed\n", ok, bad; exit bad > 0}' || die "containment tests failed"

  say "authenticated request from this Mac through 127.0.0.1:$PORT"
  local token body
  token=$(read_token)
  body=$(curl -fsS -m 20 -X POST "http://127.0.0.1:$PORT/submissions?base64_encoded=false&wait=true" \
    -H "X-Auth-Token: $token" \
    -d '{"language_id": 71, "source_code": "import sys\nprint(sys.version.split()[0], 6 * 7)"}') \
    || die "request failed"
  echo "$body" | grep -q '"Accepted"' && echo "$body" | grep -q ' 42' || die "unexpected answer: $body"
  echo "    $(echo "$body" | sed -E 's/.*"stdout": "([^"]*)\\n".*/\1/') (Accepted)"

  local code
  code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/submissions?wait=true" -d '{}')
  [ "$code" = 401 ] || die "a request without a token got HTTP $code, expected 401"
  echo "    no token -> 401"
  say "all tests passed"
}

status() {
  echo "vm:      $VM $(vm_status || true)"
  if [ "$(vm_status)" = Running ]; then
    echo "service: $(vm systemctl is-active judge.service 2> /dev/null || true)"
    echo "python:  $(vm python3.8 --version 2> /dev/null || echo missing)"
  fi
  echo "port:    http://127.0.0.1:$PORT/healthz -> $(curl -s -m 3 "http://127.0.0.1:$PORT/healthz" || echo unreachable)"
}

case "${1:-install}" in
  install)
    install_lima
    start_vm
    provision
    run_tests
    say "done. Backend settings: JUDGE0_URL=http://127.0.0.1:$PORT  JUDGE0_AUTH_TOKEN=\$(./setup.sh token)"
    ;;
  start)
    install_lima
    start_vm
    wait_healthy
    say "judge is up on http://127.0.0.1:$PORT"
    ;;
  test) run_tests ;;
  token)
    if [ "${2:-}" = rotate ]; then
      vm bash -c "cd /opt/judge && (sudo python3 server.py token revoke '$TOKEN_NAME' > /dev/null 2>&1 || true) && sudo python3 server.py token new '$TOKEN_NAME' > /dev/null && sudo chmod 600 tokens.json"
    fi
    read_token
    ;;
  status) status ;;
  *) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
