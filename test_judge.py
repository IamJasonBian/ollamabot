#!/usr/bin/env python3
"""Checks that the judge runs normal programs and contains hostile ones.

Run: python3 test_judge.py
"""
import os
import sys

import judge

HOME = os.path.expanduser("~")
CASES = [
    # name, source, stdin, expected status id, check(result) -> bool
    ("hello", "print('hi')", "", 3, lambda r: r["stdout"] == "hi\n"),
    ("stdin", "import sys; print(sys.stdin.read().upper())", "abc", 3, lambda r: r["stdout"] == "ABC\n"),
    ("stdlib", "import json, collections, heapq, bisect, itertools, functools, math, re, random, time\n"
               "print(json.dumps(sorted(collections.Counter('banana').items())))", "", 3,
     lambda r: "banana"[0] in r["stdout"]),
    ("future_annotations", "from __future__ import annotations\n"
                           "class S:\n    def f(self, a: list[int]) -> dict[str, int]:\n        return {}\nprint(S().f([]))", "", 3,
     lambda r: r["stdout"] == "{}\n"),
    ("exception", "raise ValueError('boom')", "", 11, lambda r: "ValueError: boom" in r["stderr"] and r["exit_code"] == 1),
    ("syntax_error", "def (:", "", 11, lambda r: "SyntaxError" in r["stderr"]),
    ("infinite_loop", "while True: pass", "", 5, lambda r: True),
    ("sleep_forever", "import time; time.sleep(60)", "", 5, lambda r: True),
    ("read_home", "import os\nprint(os.listdir(%r))" % HOME, "", 11, lambda r: "PermissionError" in r["stderr"] or "Operation not permitted" in r["stderr"]),
    ("read_ssh_key", "print(open(%r).read())" % os.path.join(HOME, ".ssh", "config"), "", 11,
     lambda r: "Operation not permitted" in r["stderr"] and not r["stdout"]),
    ("read_tokens", "print(open(%r).read())" % os.path.join(os.path.dirname(os.path.abspath(judge.__file__)), "judge.py"), "", 11,
     lambda r: "Operation not permitted" in r["stderr"]),
    ("write_outside", "open('/tmp/judge-escape-test', 'w').write('x')", "", 11, lambda r: not os.path.exists("/tmp/judge-escape-test")),
    ("write_inside", "open('scratch.txt', 'w').write('ok'); print(open('scratch.txt').read())", "", 3, lambda r: r["stdout"] == "ok\n"),
    ("network", "import socket\ns = socket.create_connection(('1.1.1.1', 80), timeout=3)\nprint('connected')", "", 11,
     lambda r: "connected" not in (r["stdout"] or "")),
    ("localhost_network", "import urllib.request\nprint(urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=3).read()[:20])", "", 11,
     lambda r: not r["stdout"]),
    ("subprocess_shell", "import subprocess\nprint(subprocess.run(['/bin/cat', %r], capture_output=True, text=True))" % os.path.join(HOME, ".ssh", "config"), "", None,
     lambda r: "Host " not in (r["stdout"] or "")),
    ("output_flood", "import sys\nwhile True: sys.stdout.write('x' * 65536)", "", None,
     lambda r: r["status"]["id"] in (5, 8, 11, 12) and len(r["stdout"] or "") <= judge.MAX_OUTPUT_BYTES),
    ("memory_hog", "x = bytearray(900 * 1024 * 1024)\nfor i in range(0, len(x), 4096): x[i] = 1\nprint('allocated')", "", 12,
     lambda r: "Memory limit" in r["message"] and "allocated" not in (r["stdout"] or "")),
    ("fork_bomb", "import os\nwhile True: os.fork()", "", 11, lambda r: "Operation not permitted" in r["stderr"] or "PermissionError" in r["stderr"]),
    ("memory_reported", "x = bytearray(60 * 1024 * 1024)\nfor i in range(0, len(x), 4096): x[i] = 1\nimport time; time.sleep(0.3)", "", 3,
     lambda r: (r["memory"] or 0) > 50 * 1024),
    ("kill_parent", "import os, signal\nos.kill(os.getppid(), signal.SIGKILL)\nprint('survived')", "", None, lambda r: True),
]


def main():
    """Run every case and exit non-zero if any fails."""
    failed = 0
    for name, source, stdin, want, check in CASES:
        result = judge.run_submission({"language_id": 71, "source_code": source, "stdin": stdin,
                                       "cpu_time_limit": 2, "wall_time_limit": 3})
        ok = (want is None or result["status"]["id"] == want) and check(result)
        failed += not ok
        print("%-4s %-20s status=%-2d time=%s%s" % (
            "ok" if ok else "FAIL", name, result["status"]["id"], result["time"],
            "" if ok else "\n       stdout=%r\n       stderr=%r" % ((result["stdout"] or "")[:300], (result["stderr"] or "")[-400:])))
    for bad in ({"language_id": 62, "source_code": "x"}, {"language_id": 71, "source_code": ""},
                {"language_id": 71, "source_code": "x" * (judge.MAX_SOURCE_BYTES + 1)}):
        try:
            judge.run_submission(bad)
            print("FAIL rejected-request: accepted %r" % {k: str(v)[:20] for k, v in bad.items()})
            failed += 1
        except judge.JudgeError as e:
            print("ok   rejected %d: %s" % (e.code, e))
    print("interpreter:", judge.interpreter())
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
