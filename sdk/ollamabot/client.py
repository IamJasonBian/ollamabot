"""Zero-dependency client for the ollamabot API server.

Usage:
    from ollamabot import Client
    c = Client("http://myhost:8080", token="...")
    print(c.chat("hello", model="hermes"))
"""
import json
import os
import time
import urllib.error
import urllib.request


class APIError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class Client:
    def __init__(self, base_url=None, token=None, timeout=600):
        self.base_url = (base_url or os.environ.get("OLLAMABOT_URL", "http://localhost:8080")).rstrip("/")
        self.token = token or os.environ.get("OLLAMABOT_TOKEN", "")
        self.timeout = timeout

    def _request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                message = json.load(e).get("error", "")
            except Exception:
                message = e.reason
            raise APIError(e.code, message) from None

    def health(self):
        return self._request("GET", "/healthz")

    def models(self):
        return self._request("GET", "/v1/models")

    @staticmethod
    def _chat_body(prompt, messages, model):
        body = {}
        if model:
            body["model"] = model
        if messages:
            body["messages"] = messages
        elif prompt:
            body["prompt"] = prompt
        else:
            raise ValueError("provide prompt or messages")
        return body

    def chat(self, prompt=None, *, messages=None, model=None):
        """Synchronous chat. Returns the reply text. Short prompts only when the
        server sits behind a proxy (Cloudflare kills requests at ~100s) — prefer
        ask() for anything that might think for a while."""
        return self._request("POST", "/v1/chat", self._chat_body(prompt, messages, model))["content"]

    def submit(self, prompt=None, *, messages=None, model=None):
        """Start an async job. Returns the job_id."""
        return self._request("POST", "/v1/jobs", self._chat_body(prompt, messages, model))["job_id"]

    def job(self, job_id):
        """Fetch job status: {"status": queued|running|done|error, "content", "thinking", ...}."""
        return self._request("GET", f"/v1/jobs/{job_id}")

    def ask(self, prompt=None, *, messages=None, model=None, poll_interval=2, timeout=None):
        """Submit a job and poll until done. Returns the full result dict
        ({"content", "thinking", "model", "elapsed", ...}). Timeout-proof through
        proxies — the recommended way to call the API."""
        job_id = self.submit(prompt, messages=messages, model=model)
        deadline = time.time() + (timeout or self.timeout)
        while True:
            result = self.job(job_id)
            if result["status"] == "done":
                return result
            if result["status"] == "error":
                raise APIError(502, result.get("error", "job failed"))
            if time.time() > deadline:
                raise TimeoutError(f"job {job_id} still {result['status']} after {timeout or self.timeout}s")
            time.sleep(poll_interval)
