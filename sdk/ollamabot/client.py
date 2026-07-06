"""Zero-dependency client for the ollamabot API server.

Usage:
    from ollamabot import Client
    c = Client("http://myhost:8080", token="...")
    print(c.chat("hello", model="hermes"))
"""
import json
import os
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

    def chat(self, prompt=None, *, messages=None, model=None):
        """Send one prompt string, or a full messages list. Returns the reply text."""
        body = {}
        if model:
            body["model"] = model
        if messages:
            body["messages"] = messages
        elif prompt:
            body["prompt"] = prompt
        else:
            raise ValueError("provide prompt or messages")
        return self._request("POST", "/v1/chat", body)["content"]
