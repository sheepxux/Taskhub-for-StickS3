"""
Tests for first-run device pairing: PairingManager state machine, the /pair
HTTP endpoints (LAN vs loopback rules), and the discovery responder's
behaviour for unpaired devices.

Run:  python3 -m unittest discover -s host/tests
"""
import json
import os
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import task_hub as th  # noqa: E402


class PairingManagerTests(unittest.TestCase):
    def setUp(self):
        self.pm = th.PairingManager("secret-token")

    def test_pending_until_approved_then_token_once(self):
        first = self.pm.request("sticks3-ab12", "StickS3-AB12", "4821", "192.168.1.50", "2.3.0")
        self.assertTrue(first["ok"])
        self.assertEqual(first["status"], "pending")
        self.assertNotIn("token", first)

        pending = self.pm.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["device_id"], "sticks3-ab12")
        # The code is never listed; the human reads it from the Stick.
        self.assertNotIn("code", pending[0])
        self.assertEqual(pending[0]["code_length"], 4)

        ok, msg, info = self.pm.approve("4821")
        self.assertTrue(ok, msg)
        self.assertEqual(info["device_id"], "sticks3-ab12")

        second = self.pm.request("sticks3-ab12", "StickS3-AB12", "4821", "192.168.1.50")
        self.assertEqual(second["status"], "approved")
        self.assertEqual(second["token"], "secret-token")
        self.assertEqual(second["port"], th.DEFAULT_PORT)
        self.assertEqual(self.pm.recent_paired()[0]["device_id"], "sticks3-ab12")

    def test_wrong_code_is_rejected(self):
        self.pm.request("dev", "StickS3", "1111", "10.0.0.2")
        ok, msg, _ = self.pm.approve("2222")
        self.assertFalse(ok)
        self.assertIn("no StickS3", msg)
        self.assertEqual(self.pm.request("dev", "StickS3", "1111", "10.0.0.2")["status"], "pending")

    def test_code_must_be_digits(self):
        self.assertFalse(self.pm.request("dev", "StickS3", "abcd", "10.0.0.2")["ok"])
        self.assertFalse(self.pm.request("dev", "StickS3", "12", "10.0.0.2")["ok"])
        self.assertFalse(self.pm.approve("12a4")[0])

    def test_new_code_after_approval_invalidates_it(self):
        self.pm.request("dev", "StickS3", "1234", "10.0.0.2")
        self.assertTrue(self.pm.approve("1234")[0])
        # Device rebooted and rolled a new code before collecting the token.
        rolled = self.pm.request("dev", "StickS3", "9999", "10.0.0.2")
        self.assertEqual(rolled["status"], "pending")
        self.assertNotIn("token", rolled)

    def test_ambiguous_code_needs_retry(self):
        self.pm.request("a", "A", "5555", "10.0.0.2")
        self.pm.request("b", "B", "5555", "10.0.0.3")
        ok, msg, _ = self.pm.approve("5555")
        self.assertFalse(ok)
        self.assertIn("more than one", msg)
        ok, _, info = self.pm.approve("5555", device_id="b")
        self.assertTrue(ok)
        self.assertEqual(info["device_id"], "b")

    def test_pending_cap(self):
        for i in range(th.PAIR_MAX_PENDING):
            self.assertTrue(self.pm.request(f"dev{i}", "S", "1234", "10.0.0.2")["ok"])
        overflow = self.pm.request("extra", "S", "1234", "10.0.0.2")
        self.assertFalse(overflow["ok"])

    def test_host_without_token_refuses(self):
        pm = th.PairingManager("")
        self.assertFalse(pm.request("dev", "S", "1234", "10.0.0.2")["ok"])


class _HubStub:
    def list_tasks(self, *args, **kwargs):
        return []


class PairHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class TestHandler(th.Handler):
            pass

        TestHandler.hub = _HubStub()
        TestHandler.token = "tok"
        TestHandler.pairing = th.PairingManager("tok")
        cls.handler_cls = TestHandler
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    # Bypass any system proxy so requests really hit the loopback test server.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _post(self, path, payload):
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with self.opener.open(req, timeout=3) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _get(self, path):
        try:
            with self.opener.open(f"http://127.0.0.1:{self.port}{path}", timeout=3) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_pair_flow_over_http(self):
        code, body = self._post("/pair", {"device_id": "sticks3-http", "name": "StickS3", "code": "3141"})
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "pending")

        code, body = self._get("/pair/pending")
        self.assertEqual(code, 200)
        self.assertEqual(body["pending"][0]["device_id"], "sticks3-http")

        code, body = self._post("/pair/approve", {"code": "0000"})
        self.assertEqual(code, 404)

        code, body = self._post("/pair/approve", {"code": "3141"})
        self.assertEqual(code, 200, body)

        code, body = self._post("/pair", {"device_id": "sticks3-http", "name": "StickS3", "code": "3141"})
        self.assertEqual(body["status"], "approved")
        self.assertEqual(body["token"], "tok")

    def test_pair_rejects_garbage(self):
        code, body = self._post("/pair", {"device_id": "x", "code": "nope"})
        self.assertEqual(code, 400)
        self.assertFalse(body["ok"])

    def test_pending_and_approve_are_loopback_only(self):
        # Simulate a LAN client by patching the loopback check.
        original = self.handler_cls.is_loopback
        self.handler_cls.is_loopback = lambda self: False
        try:
            self.assertEqual(self._get("/pair/pending")[0], 403)
            self.assertEqual(self._post("/pair/approve", {"code": "1234"})[0], 403)
            # /pair itself must still work from the LAN: that is the device.
            code, body = self._post("/pair", {"device_id": "lan", "code": "1234"})
            self.assertEqual(code, 200)
        finally:
            self.handler_cls.is_loopback = original


class DiscoveryPairingTests(unittest.TestCase):
    def _run_discovery(self, payload, token="tok"):
        server = th.DiscoveryServer(("127.0.0.1", 0), th.DiscoveryHandler)
        server.token = token
        server.http_port = 5577
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1.0)
            sock.sendto(json.dumps(payload).encode(), server.server_address)
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                return None
            finally:
                sock.close()
            return json.loads(data)
        finally:
            server.shutdown()
            server.server_close()

    def test_unpaired_device_gets_hub_location(self):
        resp = self._run_discovery({"type": "sticks3.discover", "device": "new", "pair": True})
        self.assertIsNotNone(resp)
        self.assertEqual(resp["type"], "sticks3.hub")
        self.assertTrue(resp["pairing"])
        self.assertEqual(resp["port"], 5577)

    def test_wrong_token_without_pair_flag_is_ignored(self):
        resp = self._run_discovery({"type": "sticks3.discover", "device": "x", "token": "wrong"})
        self.assertIsNone(resp)

    def test_paired_device_still_works(self):
        resp = self._run_discovery({"type": "sticks3.discover", "device": "x", "token": "tok"})
        self.assertIsNotNone(resp)
        self.assertFalse(resp["pairing"])


if __name__ == "__main__":
    unittest.main()
