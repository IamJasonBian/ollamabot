"""Judge0-compatible Python runner for the API server.

Runs one submitted program in a macOS sandbox (sandbox-exec) with no network,
no access to the home directory, a CPU limit, a wall-clock limit and capped
output, and reports the result in Judge0's response shape so a Judge0 client
can point at this server unchanged.
"""
import ctypes
import ctypes.util
import os
import pathlib
import resource
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import time

PYTHON_LANGUAGE_ID = 71          # Judge0 CE's id for Python 3
MAX_SOURCE_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_CPU_SECONDS = 10
MAX_WALL_SECONDS = 15
MAX_MEMORY_KB = 512 * 1024
POLL_SECONDS = 0.05
MAX_CONCURRENT = 2
QUEUE_WAIT_SECONDS = 20

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Deny everything, then allow only what a pure-Python program needs: read the
# system and the interpreter, read and write its own run directory. There is no
# network rule, no home-directory rule and no fork rule, so all three stay
# denied (no fork also means no subprocesses and no fork bombs).
PROFILE = """
(version 1)
(deny default)
(allow process-exec)
(allow signal (target self))
(allow sysctl-read)
(allow file-read-metadata)
(allow file-read*
    (literal "/")
    (subpath "/usr")
    (subpath "/bin")
    (subpath "/System")
    (subpath "/Library/Developer/CommandLineTools")
    (subpath "/Library/Frameworks/Python.framework")
    (subpath "/Applications/Xcode.app/Contents/Developer")
    (subpath "/opt/homebrew/Cellar")
    (subpath "/opt/homebrew/opt")
    (subpath "/private/var/db/timezone")
    (subpath "/private/var/select")
    (literal "/dev/null")
    (literal "/dev/urandom")
    (literal "/dev/random")
    (subpath (param "INTERPRETER_HOME"))
    (subpath (param "RUN_DIR")))
(allow file-write*
    (literal "/dev/null")
    (subpath (param "RUN_DIR")))
"""

STATUS = {
    3: "Accepted",
    5: "Time Limit Exceeded",
    7: "Runtime Error (SIGSEGV)",
    8: "Runtime Error (SIGXFSZ)",
    11: "Runtime Error (NZEC)",
    12: "Runtime Error (Other)",
    13: "Internal Error",
}

slots = threading.BoundedSemaphore(MAX_CONCURRENT)
_interpreter = None
_libproc = ctypes.CDLL(ctypes.util.find_library("proc"), use_errno=True)


class JudgeError(Exception):
    """A request the judge refuses, with the HTTP status to answer with."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def interpreter():
    """Resolve the real Python binary once.

    /usr/bin/python3 is a shim that locates the interpreter through xcrun,
    which the sandbox does not allow; the resolved binary runs directly.

    Returns:
        Absolute path of the interpreter to run submissions with.
    """
    global _interpreter
    if _interpreter is None:
        wanted = os.environ.get("JUDGE_PYTHON", "/usr/bin/python3")
        out = subprocess.run(
            [wanted, "-I", "-c", "import sys; print(getattr(sys, '_base_executable', '') or sys.executable)"],
            capture_output=True, text=True, timeout=20, check=True,
        ).stdout.strip()
        _interpreter = os.path.realpath(out)
    return _interpreter


def footprint_kb(pid):
    """Read a process's physical memory footprint.

    macOS does not enforce memory rlimits, so the judge polls this instead.

    Args:
        pid: Process to inspect.

    Returns:
        Footprint in KB, or 0 if the process is gone.
    """
    buf = (ctypes.c_uint64 * 64)()
    if _libproc.proc_pid_rusage(pid, 2, ctypes.byref(buf)) != 0:  # RUSAGE_INFO_V2
        return 0
    return buf[9] // 1024  # ri_phys_footprint: 16-byte uuid, then the 8th uint64


def limits(cpu_seconds):
    """Build the pre-exec hook that applies resource limits to the child.

    Args:
        cpu_seconds: CPU time allowed before the kernel signals the process.

    Returns:
        Callable for subprocess.Popen's preexec_fn.
    """
    def apply():
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return apply


def read_capped(path):
    """Read a captured output file, decoding leniently.

    Args:
        path: File written by the child.

    Returns:
        The text, or None when the file is empty.
    """
    data = path.read_bytes()[:MAX_OUTPUT_BYTES]
    return data.decode("utf-8", errors="replace") or None


def run_submission(payload):
    """Run one Judge0-style submission and return a Judge0-style result.

    Args:
        payload: Decoded request body with language_id, source_code and
            optional stdin, cpu_time_limit, wall_time_limit and memory_limit (KB).

    Returns:
        Dict with stdout, stderr, compile_output, message, status, time,
        memory, exit_code and token.

    Raises:
        JudgeError: The request is malformed, unsupported, or the judge is busy.
    """
    if payload.get("language_id") != PYTHON_LANGUAGE_ID:
        raise JudgeError(422, "only language_id %d (Python 3) is supported" % PYTHON_LANGUAGE_ID)
    source = payload.get("source_code")
    if not isinstance(source, str) or not source.strip():
        raise JudgeError(422, "source_code is required")
    if len(source.encode()) > MAX_SOURCE_BYTES:
        raise JudgeError(413, "source_code is larger than %d bytes" % MAX_SOURCE_BYTES)
    stdin = payload.get("stdin") or ""
    if not isinstance(stdin, str) or len(stdin.encode()) > MAX_SOURCE_BYTES:
        raise JudgeError(422, "stdin must be a string of at most %d bytes" % MAX_SOURCE_BYTES)
    try:
        cpu = min(max(1, int(float(payload.get("cpu_time_limit") or 5))), MAX_CPU_SECONDS)
        wall = min(max(1.0, float(payload.get("wall_time_limit") or 8)), MAX_WALL_SECONDS)
        memory_kb = min(max(16 * 1024, int(float(payload.get("memory_limit") or 256000))), MAX_MEMORY_KB)
    except (TypeError, ValueError):
        raise JudgeError(422, "cpu_time_limit, wall_time_limit and memory_limit must be numbers")

    if not slots.acquire(timeout=QUEUE_WAIT_SECONDS):
        raise JudgeError(503, "judge is busy, try again")
    run_dir = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix="judge-")))
    try:
        (run_dir / "main.py").write_text(source)
        (run_dir / "stdin").write_text(stdin)
        python = interpreter()
        command = [
            SANDBOX_EXEC,
            "-D", "RUN_DIR=%s" % run_dir,
            "-D", "INTERPRETER_HOME=%s" % os.path.dirname(os.path.dirname(python)),
            "-p", PROFILE,
            python, "-I", "-B", "main.py",
        ]
        started = time.monotonic()
        timed_out = over_memory = False
        peak_kb = 0
        with open(run_dir / "stdin") as fin, open(run_dir / "stdout", "wb") as fout, open(run_dir / "stderr", "wb") as ferr:
            child = subprocess.Popen(
                command, cwd=str(run_dir), stdin=fin, stdout=fout, stderr=ferr,
                env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "PYTHONIOENCODING": "utf-8", "TMPDIR": str(run_dir)},
                start_new_session=True, preexec_fn=limits(cpu),
            )
            try:
                while child.poll() is None:
                    peak_kb = max(peak_kb, footprint_kb(child.pid))
                    if peak_kb > memory_kb:
                        over_memory = True
                        break
                    if time.monotonic() - started > wall:
                        timed_out = True
                        break
                    time.sleep(POLL_SECONDS)
            finally:
                # Kill the whole session so nothing the program started survives.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
        elapsed = time.monotonic() - started
        code = child.returncode
        message = None
        if over_memory:
            status, exit_code, message = 12, None, "Memory limit exceeded (%d KB)" % memory_kb
        elif timed_out or code == -signal.SIGXCPU:
            status, exit_code = 5, None
        elif code == 0:
            status, exit_code = 3, 0
        elif code == -signal.SIGSEGV:
            status, exit_code = 7, None
        elif code == -signal.SIGXFSZ:
            status, exit_code = 8, None
        elif code < 0:
            status, exit_code = 12, None
        else:
            status, exit_code = 11, code
        return {
            "stdout": read_capped(run_dir / "stdout"),
            "stderr": read_capped(run_dir / "stderr"),
            "compile_output": None,
            "message": message or (None if status == 3 else STATUS[status]),
            "status": {"id": status, "description": STATUS[status]},
            "time": "%.3f" % elapsed,
            "memory": peak_kb or None,
            "exit_code": exit_code,
            "token": secrets.token_hex(16),
        }
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
        slots.release()
