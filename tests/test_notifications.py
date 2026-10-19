"""Exercise notification persistence and ambiguous-send safety without accounts."""
import json
import tempfile
import unittest
from pathlib import Path

from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.events import Events
from wechat_dot_bridge.mcp import MCPServer
from wechat_dot_bridge.store import Store, MAX_QUEUED_REPLIES
from wechat_dot_bridge.transport import WeixinWorker


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state"
        self.store = Store(self.path, clock=lambda: 200)
        self.store.import_binding({"id": "fixture-bot", "owner": "fixture-owner", "token": "fixture-token", "base_url": "https://ilinkai.weixin.qq.com"})
        self.store.set_enabled(True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def inbound(self, source="1", created_at=100):
        self.store.ingest("fixture-bot", [{"source_id": source, "peer": "fixture-owner", "text": "hello", "created_at": created_at, "context_token": "context-" + source}], source)
        return self.store.db.execute("SELECT id FROM messages WHERE source_id=?", (source,)).fetchone()[0]

    def reopen(self):
        self.store.close()
        self.store = Store(self.path, clock=lambda: 200)
        self.store.recover_inflight()

    def test_without_inbound_waits_then_uses_owner_context(self):
        self.store.queue_notification("task-1", "finished")
        self.assertIsNone(self.store.claim_send())
        self.assertEqual(self.store.notification_status("task-1")["status"], "context_required")
        self.inbound()
        outbound = self.store.claim_send()
        self.assertIsNone(outbound["message_id"])
        self.assertEqual(outbound["peer"], "fixture-owner")
        self.assertEqual(outbound["context_token"], "context-1")
        self.store.finish_send(None, "accepted", reply_id=outbound["reply_id"])
        self.assertEqual(self.store.notification_status("task-1")["status"], "accepted")

    def test_idempotency_survives_restart_and_text_conflict_rejected(self):
        first = self.store.queue_notification("task-1", "finished")
        self.reopen()
        self.assertEqual(first, self.store.queue_notification("task-1", "finished"))
        with self.assertRaises(BridgeError) as caught:
            self.store.queue_notification("task-1", "different")
        self.assertEqual(caught.exception.code, "notification_id_conflict")

    def test_inflight_restart_never_resends_uncertain_notification(self):
        self.inbound()
        self.store.queue_notification("task-1", "finished")
        self.store.claim_send()
        self.reopen()
        self.assertEqual(self.store.notification_status("task-1")["status"], "uncertain")
        self.inbound("2", 101)
        self.assertIsNone(self.store.claim_send())

    def test_rejected_context_requires_genuinely_new_context(self):
        self.inbound()
        self.store.queue_notification("task-1", "finished")
        item = self.store.claim_send()
        self.store.finish_send(None, "context_required", "context_required", item["reply_id"])
        self.assertIsNone(self.store.claim_send())
        self.inbound("2", 101)
        self.assertEqual(self.store.claim_send()["context_token"], "context-2")

    def test_fresh_context_during_send_resumes_explicit_rejection(self):
        self.inbound()
        self.store.queue_notification("task-1", "finished")
        item = self.store.claim_send()
        self.inbound("2", 101)
        self.store.finish_send(None, "context_required", "context_required", item["reply_id"])
        self.assertEqual(self.store.notification_status("task-1")["status"], "queued")

    def test_long_notification_parts_and_restart_preserve_accepted_parts(self):
        self.inbound()
        self.store.queue_notification("long", "测试" * 10000)
        first = self.store.claim_send()
        self.store.finish_send(None, "accepted", reply_id=first["reply_id"])
        self.reopen()
        second = self.store.claim_send()
        self.assertNotEqual(first["client_id"], second["client_id"])
        self.assertEqual(self.store.notification_status("long")["parts_accepted"], 1)

    def test_notifications_do_not_create_inbound_events_or_finish_work(self):
        mid = self.inbound()
        self.store.read_message(mid)
        before = self.store.work_status(mid)
        self.store.queue_notification("task-1", "finished")
        self.assertEqual(self.store.work_status(mid), before)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)

    def test_shared_queue_limit_rejects_notifications_and_replies(self):
        mid = self.inbound()
        for i in range(MAX_QUEUED_REPLIES):
            self.store.queue_notification("n" + str(i), "done")
        for operation in (lambda: self.store.queue_notification("overflow", "done"), lambda: self.store.queue_reply(mid, "reply")):
            with self.assertRaises(BridgeError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "send_queue_full")

    def test_regular_replies_are_sent_before_notifications(self):
        mid = self.inbound()
        self.store.queue_notification("n", "notification")
        self.store.queue_reply(mid, "reply")
        self.assertEqual(self.store.claim_send()["message_id"], mid)

    def test_upgrade_from_upstream_preserves_existing_reply_and_adds_notifications(self):
        mid = self.inbound()
        reply = self.store.queue_reply(mid, "existing reply")
        self.store.db.execute("DROP TABLE notification_parts")
        self.store.db.execute("DROP TABLE notifications")
        self.store.db.execute("PRAGMA user_version=8")
        self.reopen()
        self.assertEqual(self.store.send_status(mid)["reply_id"], reply["reply_id"])
        self.assertEqual(self.store.queue_notification("new", "notification")["status"], "queued")
        self.assertEqual(self.store.claim_send()["reply_id"], reply["reply_id"])

    def test_send_worker_routes_and_records_notification_outcomes(self):
        self.inbound()
        self.store.queue_notification("n", "notification")
        class Client:
            def send(self, binding, reply):
                assert reply["peer"] == "fixture-owner"
                return {"ret": 0}
        self.assertTrue(WeixinWorker(self.store, client=Client()).send_once())
        self.assertEqual(self.store.notification_status("n")["status"], "accepted")

    def test_mcp_exposes_notification_tools_without_recipient_override(self):
        server = MCPServer(self.store, Events(self.store, []))
        def call(name, arguments):
            return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        args = {"notification_id": "n", "text": "finished"}
        result = call("send_weixin_notification", args)["result"]
        self.assertFalse(result["isError"])
        status = call("get_weixin_notification_status", {"notification_id": "n"})["result"]["structuredContent"]
        self.assertEqual(status["status"], "queued")
        self.assertNotIn("fixture-token", json.dumps(status))
        self.assertEqual(call("send_weixin_notification", dict(args, to="someone-else"))["error"]["code"], -32602)

    def test_paused_account_and_invalid_ids_are_rejected(self):
        self.store.set_enabled(False)
        for identifier in ("", "../bad", "x" * 129):
            with self.assertRaises(BridgeError):
                self.store.queue_notification(identifier, "done")
        with self.assertRaises(BridgeError):
            self.store.queue_notification("valid", "done")
