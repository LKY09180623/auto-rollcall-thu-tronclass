import http.client
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import render_start
from tests.test_environment_config import ACCOUNTS


class WebDashboardApiTest(unittest.TestCase):
    def setUp(self):
        self.supervisor = MagicMock()
        self.supervisor.health.return_value = {"status": "ok", "expected_processes": 2, "running_processes": 2}
        self.handler = render_start.make_handler(self.supervisor)
        self.server = render_start.ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)

    def tearDown(self):
        self.conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_get_root_serves_html(self):
        self.conn.request("GET", "/", headers={"Accept": "text/html"})
        resp = self.conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.getheader("Content-Type", ""))
        body = resp.read().decode("utf-8")
        self.assertIn("quickPayloadInput", body)

    def test_dashboard_uses_configured_mqtt_channel(self):
        with patch.dict("os.environ", {"TRON_MQTT_CHANNEL": 'private-"channel'}):
            self.conn.request("GET", "/", headers={"Accept": "text/html"})
            resp = self.conn.getresponse()
            body = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn('const FIXED_MQTT_CHANNEL = "private-\\"channel";', body)
        self.assertNotIn('const FIXED_MQTT_CHANNEL = "A113280040_secret";', body)

    def test_get_health_endpoints(self):
        self.conn.request("GET", "/health")
        resp = self.conn.getresponse()
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read().decode())
        self.assertEqual(data["status"], "ok")

    def test_get_api_status(self):
        self.conn.request("GET", "/api/status")
        resp = self.conn.getresponse()
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read().decode())
        self.assertEqual(data["status"], "ok")
        self.assertIn("accounts", data)

    def test_post_api_submit_empty(self):
        self.conn.request("POST", "/api/submit", body=json.dumps({"payload": ""}), headers={"Content-Type": "application/json"})
        resp = self.conn.getresponse()
        self.assertEqual(resp.status, 400)
        data = json.loads(resp.read().decode())
        self.assertFalse(data["ok"])

    def test_guided_submit_requires_specific_account_and_rollcall(self):
        config = {"accounts": {"profiles": {"primary": {}}}}
        with (
            patch("troTHU.runtime_context.load_config", return_value=config),
            patch("troTHU.runtime_context.CONFIG", {}),
            patch("troTHU.bot_handlers.BotHandlerBridge.qr_submit", new_callable=AsyncMock) as submit,
        ):
            body = json.dumps({"profile": "all", "payload": "0001", "expectedType": "number", "expectedRollcallId": "88"})
            self.conn.request("POST", "/api/submit", body=body, headers={"Content-Type": "application/json"})
            response = self.conn.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            submit.assert_not_awaited()

    def test_guided_submit_passes_selected_activity(self):
        config = {"accounts": {"profiles": {"primary": {}}}}
        with (
            patch("troTHU.runtime_context.load_config", return_value=config),
            patch("troTHU.runtime_context.CONFIG", {}),
            patch("troTHU.bot_handlers.BotHandlerBridge.qr_submit", new_callable=AsyncMock) as submit,
        ):
            submit.return_value = {"ok": True, "status": "ok", "reply": "confirmed"}
            body = json.dumps({"profile": "primary", "payload": "0001", "expectedType": "number", "expectedRollcallId": "88"})
            self.conn.request("POST", "/api/submit", body=body, headers={"Content-Type": "application/json"})
            response = self.conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(response.read().decode())["ok"])
            command = submit.await_args.kwargs["command"]
            self.assertEqual(command.payload, {"expected_type": "number", "expected_rollcall_id": "88"})


if __name__ == "__main__":
    unittest.main()
