"""ollamabot CLI.

Config via flags or env: OLLAMABOT_URL, OLLAMABOT_TOKEN.

Examples:
    ollamabot models
    ollamabot chat "why is the sky blue"
    ollamabot chat -m qwen "write a haiku"
    echo "summarize this" | ollamabot chat
"""
import argparse
import sys

from .client import APIError, Client


def main():
    p = argparse.ArgumentParser(prog="ollamabot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", help="server URL (default: $OLLAMABOT_URL or http://localhost:8080)")
    p.add_argument("--token", help="bearer token (default: $OLLAMABOT_TOKEN)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health")
    sub.add_parser("models")
    chat = sub.add_parser("chat")
    chat.add_argument("-m", "--model", help="model alias or ollama tag (default: server default)")
    chat.add_argument("prompt", nargs="*", help="prompt text (reads stdin if omitted)")

    args = p.parse_args()
    client = Client(args.url, args.token)

    try:
        if args.cmd == "health":
            print(client.health())
        elif args.cmd == "models":
            info = client.models()
            for alias, tag in info["aliases"].items():
                marker = "*" if alias == info["default"] else " "
                print(f"{marker} {alias} -> {tag}")
            print("ollama tags:", ", ".join(info["ollama_tags"]))
        elif args.cmd == "chat":
            prompt = " ".join(args.prompt) if args.prompt else sys.stdin.read().strip()
            if not prompt:
                sys.exit("no prompt given")
            print(client.chat(prompt, model=args.model))
    except APIError as e:
        sys.exit(f"error: {e}")
    except OSError as e:
        sys.exit(f"connection error: {e}")


if __name__ == "__main__":
    main()
