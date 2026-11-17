"""Shared fixtures: isolated state dir and a stdlib stub of the Power BI REST API."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

WS_ID = "11111111-1111-1111-1111-111111111111"
DS_ID = "22222222-2222-2222-2222-222222222222"
TOKEN = "test-token"


class Stub:
    def __init__(self):
        self.requests = []
        self.unknown_polls = 0     # refresh polls that report "in progress"
        self.refresh_status = "Completed"


def _handler(stub: Stub):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body=None, headers=None):
            data = json.dumps(body).encode() if body is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _route(self, method):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            path = self.path.split("?")[0]
            stub.requests.append({"method": method, "path": self.path, "body": body,
                                  "auth": self.headers.get("Authorization")})
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self._send(401, {"error": {"code": "TokenExpired"}})
            grp = rf"(?:/groups/{WS_ID})?"
            if method == "GET" and path == "/groups":
                return self._send(200, {"value": [
                    {"id": WS_ID, "name": "Sales Analytics", "type": "Workspace", "isOnDedicatedCapacity": False}]})
            if method == "GET" and re.fullmatch(grp + "/datasets", path):
                return self._send(200, {"value": [
                    {"id": DS_ID, "name": "Demo", "configuredBy": "a@b.c", "isRefreshable": True}]})
            if method == "GET" and re.fullmatch(grp + f"/datasets/{DS_ID}", path):
                return self._send(200, {"id": DS_ID, "name": "Demo", "isRefreshable": True})
            if method == "GET" and re.fullmatch(grp + "/reports", path):
                return self._send(200, {"value": [
                    {"id": "r1", "name": "Demo", "datasetId": DS_ID, "webUrl": "https://x"}]})
            if method == "POST" and re.fullmatch(grp + f"/datasets/{DS_ID}/refreshes", path):
                return self._send(202, None, {"RequestId": "req-123"})
            if method == "GET" and re.fullmatch(grp + f"/datasets/{DS_ID}/refreshes", path):
                if stub.unknown_polls > 0:
                    stub.unknown_polls -= 1
                    status = "Unknown"
                else:
                    status = stub.refresh_status
                return self._send(200, {"value": [{"requestId": "req-123", "refreshType": "ViaApi",
                                                   "status": status, "startTime": "2026-11-17T10:00:00Z"}]})
            if method == "POST" and re.fullmatch(grp + f"/datasets/{DS_ID}/executeQueries", path):
                q = body["queries"][0]["query"]
                if "BAD" in q:
                    return self._send(400, {"error": {"code": "DatasetExecuteQueriesError",
                                                      "pbi.error": {"details": [{"detail": "syntax"}]}}})
                return self._send(200, {"results": [{"tables": [{"rows": [
                    {"Sales[Product]": "Widget", "[Total]": 165.5},
                    {"Sales[Product]": "Gadget", "[Total]": 80.0}]}]}]})
            return self._send(404, {"error": {"code": "NotFound", "path": path}})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")
    return H


@pytest.fixture
def stub_api(monkeypatch):
    stub = Stub()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(stub))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    stub.base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv("POWERBI_API_BASE", stub.base)
    monkeypatch.setenv("POWERBI_ACCESS_TOKEN", TOKEN)
    yield stub
    srv.shutdown()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "pbi-home"
    monkeypatch.setenv("CLI_ANYTHING_POWERBI_HOME", str(home))
    for k in ("POWERBI_PROJECT", "POWERBI_WORKSPACE", "POWERBI_TENANT_ID",
              "POWERBI_CLIENT_ID", "POWERBI_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    return home
