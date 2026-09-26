#!/usr/bin/env python3
"""Minimal end-to-end check for server.py — stdlib only. Run: python3 test_relay.py"""
import json
import os
import threading
import unittest
import urllib.error
import urllib.request

os.environ.setdefault("RELAY_TOKEN", "test-token")

import server  # noqa: E402

TOKEN = os.environ["RELAY_TOKEN"]


class RelayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, method, path, body=None, token=TOKEN):
        req = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-Relay-Token": token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, json.loads(raw) if raw else {}

    def test_reaper(self):
        # a job nobody picks up must end in "error" — and reap() must not crash
        # (regression: it used deque.discard(), which does not exist)
        code, job = self.call("POST", "/api/commands", {"device": "*", "command": "sleep"})
        self.assertEqual(code, 200)
        j = server.STATE.jobs[job["job_id"]]
        j.created -= 400                      # pretend it has been queued > 5 min
        server.STATE.reap()
        self.assertEqual(j.status, "error")

        # a job taken but never answered must time out instead of hanging forever
        code, job2 = self.call("POST", "/api/commands", {"device": "*", "command": "x", "timeout": 1})
        code, got = self.call("GET", "/api/device/poll?device=dev2")
        self.assertEqual(got["job_id"], job2["job_id"])
        j2 = server.STATE.jobs[job2["job_id"]]
        j2.delivered -= 60
        server.STATE.reap()
        self.assertEqual(j2.status, "timeout")

    def test_roundtrip(self):
        code, body = self.call("GET", "/api/health")
        self.assertEqual((code, body["ok"]), (200, True))
        # auth is enforced
        code, _ = self.call("GET", "/api/devices", token="wrong")
        self.assertEqual(code, 401)
        # garbage numeric params must fall back, not crash the handler
        code, _ = self.call("GET", "/api/jobs?limit=abc")
        self.assertEqual(code, 200)
        # queue → device poll → result → waiter sees output
        code, job = self.call("POST", "/api/commands", {"device": "*", "command": "echo hi", "timeout": 5})
        self.assertEqual(code, 200)
        code, got = self.call("GET", "/api/device/poll?device=dev1&name=Pixel&sdk=34")
        self.assertEqual((code, got["job_id"]), (200, job["job_id"]))
        code, _ = self.call("POST", "/api/device/result",
                            {"job_id": job["job_id"], "device": "dev1", "exit_code": 0, "stdout": "hi\n"})
        self.assertEqual(code, 200)
        code, res = self.call("GET", "/api/commands/%s/wait?t=1" % job["job_id"])
        self.assertEqual((code, res["status"], res["stdout"]), (200, "done", "hi\n"))


if __name__ == "__main__":
    unittest.main()
