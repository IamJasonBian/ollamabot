#!/bin/sh
# Install the ollamabot client SDK + CLI.
#   curl -sSL https://raw.githubusercontent.com/IamJasonBian/ollamabot/main/install.sh | sh
set -e

REPO="git+https://github.com/IamJasonBian/ollamabot.git#subdirectory=sdk"

# --user is invalid inside a virtualenv
USERFLAG="--user"
[ -n "$VIRTUAL_ENV" ] && USERFLAG=""

if [ -z "$VIRTUAL_ENV" ] && command -v pipx >/dev/null 2>&1; then
    pipx install "$REPO"
elif command -v pip3 >/dev/null 2>&1; then
    pip3 install $USERFLAG "$REPO"
elif command -v pip >/dev/null 2>&1; then
    pip install $USERFLAG "$REPO"
else
    echo "error: need pipx or pip installed" >&2
    exit 1
fi

echo
echo "Installed. Configure and test:"
echo "  export OLLAMABOT_URL=http://<server-host>:8080"
echo "  export OLLAMABOT_TOKEN=<your token>"
echo "  ollamabot chat \"hello\""
