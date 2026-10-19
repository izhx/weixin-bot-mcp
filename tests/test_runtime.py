"""Runtime wakeups, queue scheduling and coordinated shutdown."""

import base64
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import anyio

from wechat_dot_bridge.cli import _run_runtime
from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.events import Events
from wechat_dot_bridge.mcp import MCPServer
from wechat_dot_bridge.media import MediaWorker
from wechat_dot_bridge.net_transport import reset_cancellation
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.transport import WeixinWorker


class InterruptedClient:
    def __init__(self):
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.posts = []

    def poll(self, binding):
        return {"msgs": [], "get_updates_buf": "fixture-cursor"}

    def send(self, binding, reply):
        self.posts.append(reply["client_id"])
        self.started.set()
        if not self.stopped.wait(5):
            raise AssertionError("runtime failed to cancel active send")
        raise BridgeError("network_cancelled")

    def cancel(self):
        self.stopped.set()


class RuntimeTests(unittest.TestCase):
    def check_shutdown(self, protocol_failure):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            store = Store(state)
            store.import_binding({"id": "fixture-account", "owner": "fixture-owner", "token": "fixture-token",
                                  "base_url": "https://ilinkai.weixin.qq.com"})
            store.set_enabled(True)
            store.ingest("fixture-account", [{"source_id": "1", "peer": "fixture-owner", "text": "fixture-input",
                                              "created_at": 1800000000, "context_token": "fixture-context"}], "1")
            mid = store.list_pending()[0]["message_id"]
            queued = store.queue_reply(mid, "fixture-output")
            client = InterruptedClient()
            worker = WeixinWorker(store, client=client)
            typing = Mock()
            def close_presence():
                # If cleanup runs before workers, this remains 'sending'. If
                # the store has already closed, reading it raises instead.
                self.assertEqual(store.send_status(mid)["status"], "uncertain")
                self.assertEqual(store.send_status(mid)["error"], "network_cancelled")
            typing.close.side_effect = close_presence

            async def stdio(server):
                self.assertTrue(await anyio.to_thread.run_sync(client.started.wait, 3))
                if protocol_failure:
                    raise RuntimeError("fixture protocol failure")
                # Returning represents stdio EOF; the runtime must cancel the
                # active network exchange and wait for its durable outcome.

            try:
                with patch("wechat_dot_bridge.sdk_adapter.run_stdio", stdio):
                    args = (store, Mock(), worker, Mock(), Mock(), Mock(), typing)
                    if protocol_failure:
                        with self.assertRaises(ExceptionGroup):
                            anyio.run(_run_runtime, *args)
                    else:
                        anyio.run(_run_runtime, *args)
                self.assertTrue(client.stopped.is_set())
                typing.close.assert_called_once()
                self.assertEqual(store.send_status(mid)["status"], "uncertain")
                self.assertEqual(store.send_status(mid)["reply_id"], queued["reply_id"])
                self.assertEqual(len(client.posts), 1)
            finally:
                store.close()
                from wechat_dot_bridge.net_transport import reset_cancellation
                reset_cancellation()
            restored = Store(state)
            try:
                restored.recover_inflight()
                self.assertEqual(restored.send_status(mid)["status"], "uncertain")
                self.assertIsNone(restored.claim_send())
                self.assertFalse(WeixinWorker(restored, client=client).send_once())
                self.assertEqual(len(client.posts), 1)
            finally:
                restored.close()

    def test_eof_waits_for_interrupted_send_before_closing_store(self):
        self.check_shutdown(False)

    def test_protocol_error_also_closes_typing_and_preserves_uncertain(self):
        self.check_shutdown(True)

    def test_eof_recovers_notification_and_download_without_resending_weixin(self):
        from wechat_dot_bridge.net_transport import cancellation_token, reset_cancellation

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            store = Store(state)
            store.import_binding({"id": "fixture-account", "owner": "fixture-owner", "token": "fixture-token",
                                  "base_url": "https://ilinkai.weixin.qq.com"})
            store.set_enabled(True)
            rows = [{"source_id": str(index), "peer": "fixture-owner", "text": "synthetic inbound",
                     "created_at": store.clock(), "context_token": "fixture-context"} for index in range(3)]
            rows[1]["attachments"] = [{"kind": "file", "name": "fixture.json", "url": "fixture-download"}]
            client = InterruptedClient()
            event_started, download_started = threading.Event(), threading.Event()

            def callback(url, body, headers):
                value = json.loads(body)
                if value.get("type") == "verification":
                    return 200, json.dumps({"challenge": value["challenge"]}).encode()
                event_started.set()
                if not cancellation_token().wait(5):
                    raise AssertionError("event did not receive runtime cancellation")
                raise BridgeError("callback_cancelled")

            def interrupted_download(descriptor):
                download_started.set()
                if not cancellation_token().wait(5):
                    raise AssertionError("download did not receive runtime cancellation")
                raise BridgeError("media_cancelled")

            events = Events(store, ["callback.example.com"], callback)
            secret = "whsec_" + base64.b64encode(b"fixture-signing-key-32-byte-data!").decode()
            events.subscribe({"name": "weixin.message", "arguments": {},
                              "delivery": {"mode": "webhook", "url": "https://callback.example.com/fixture", "secret": secret}})
            store.ingest("fixture-account", rows, "fixture-cursor")
            mids = [row["message_id"] for row in store.list_pending()]
            store.queue_reply(mids[0], "synthetic reply")
            aid = store.attachment_metadata(mids[1])[0]["attachment_id"]
            store.request_attachment(aid)
            media = MediaWorker(store)

            async def stdio(server):
                for name, started in (("send", client.started), ("event", event_started), ("download", download_started)):
                    self.assertTrue(await anyio.to_thread.run_sync(started.wait, 3), name)

            try:
                with patch("wechat_dot_bridge.sdk_adapter.run_stdio", stdio), \
                        patch("wechat_dot_bridge.media.fetch", interrupted_download):
                    anyio.run(_run_runtime, store, Mock(), WeixinWorker(store, client), events,
                              media, Mock(), Mock())
                self.assertEqual(store.send_status(mids[0])["status"], "uncertain")
                self.assertEqual(store.send_status(mids[0])["error"], "network_cancelled")
                delivery = store.db.execute("SELECT status,attempts FROM deliveries WHERE message_id=?", (mids[1],)).fetchone()
                self.assertEqual(tuple(delivery), ("retry", 0))
                attachment = store.db.execute("SELECT status,attempts FROM attachments WHERE id=?", (aid,)).fetchone()
                self.assertEqual(tuple(attachment), ("pending", 0))
                self.assertEqual(len(client.posts), 1)
            finally:
                store.close()
                reset_cancellation()

            restored = Store(state)
            try:
                restored.recover_inflight()
                self.assertIsNone(restored.claim_send())
                recovered_events = Events(restored, ["callback.example.com"], lambda *args: (200, b"{}"))
                self.assertTrue(recovered_events.deliver_once())
                self.assertEqual(recovered_events.status()["deliveries"].get("acknowledged"), 1)
                with patch("wechat_dot_bridge.media.fetch", return_value=b"{}"):
                    MediaWorker(restored).tick()
                self.assertEqual(restored.read_attachment(aid)["data"], b"{}")
                self.assertTrue(recovered_events.deliver_once())
                self.assertEqual(recovered_events.status()["deliveries"].get("acknowledged"), 2)
                self.assertFalse(WeixinWorker(restored, client).send_once())
                self.assertEqual(len(client.posts), 1)
            finally:
                restored.close()


class RecordingClient:
    def __init__(self, *, poll_delay=0, poll_error=None):
        self.poll_delay, self.poll_error = poll_delay, poll_error
        self.polls, self.poll_ends, self.sends = [], [], []
        self.sent = threading.Event()

    def poll(self, binding):
        self.polls.append(time.monotonic())
        time.sleep(self.poll_delay)
        self.poll_ends.append(time.monotonic())
        if self.poll_error:
            raise BridgeError(self.poll_error)
        return {'msgs': [], 'get_updates_buf': 'fixture-cursor'}

    def send(self, binding, reply):
        self.sends.append(time.monotonic())
        self.sent.set()
        return {}

    def cancel(self):
        pass


class RecordingTyping:
    def __init__(self):
        self.started, self.refreshed, self.release = (threading.Event() for _ in range(3))
        self.ticks, self.new_ids = [], []
        self.block_refresh = False

    def received(self, message_ids):
        self.new_ids.extend(message_ids)

    def sync(self, message_id):
        pass

    def prewarm(self):
        pass

    def tick(self):
        self.ticks.append(time.monotonic())
        self.started.set()
        if len(self.ticks) >= 2:
            self.refreshed.set()
        if self.block_refresh and len(self.ticks) == 2:
            self.release.wait(2)

    def close(self):
        self.release.set()


class RuntimeSchedulingTests(unittest.TestCase):
    def setUp(self):
        reset_cancellation()
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / 'state')
        self.store.import_binding({'id': 'fixture-account', 'owner': 'fixture-owner',
                                  'token': 'fixture-token', 'base_url': 'https://ilinkai.weixin.qq.com'})
        self.store.set_enabled(True)
        self.store.ingest('fixture-account', [{'source_id': 'fixture-input', 'peer': 'fixture-owner',
                         'text': 'synthetic input', 'created_at': self.store.clock(),
                         'context_token': 'fixture-context'}], 'fixture-cursor')
        self.mid = self.store.list_pending()[0]['message_id']

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()
        reset_cancellation()

    def run_runtime(self, stdio, *, client=None, typing=None, events=None, media=None,
                    configure_worker=None, on_received=None):
        client = client or RecordingClient()
        typing = typing or RecordingTyping()
        events = events or SimpleNamespace(deliver_once=lambda: False)
        media = media or SimpleNamespace(tick=lambda: False)
        server = MCPServer(self.store, events, typing)
        worker = WeixinWorker(self.store, client, on_received=on_received)

        async def run_stdio(server):
            if configure_worker is not None:
                configure_worker(worker)
            await stdio(server)

        with patch('wechat_dot_bridge.sdk_adapter.run_stdio', run_stdio):
            anyio.run(_run_runtime, self.store, server, worker, events, media,
                      SimpleNamespace(tick=lambda: None), typing)
        self.assertIsNone(server.wake_workers)
        self.assertIs(worker.on_received, on_received)

    async def await_event(self, event, timeout=0.35):
        self.assertTrue(await anyio.to_thread.run_sync(event.wait, timeout))

    async def tool(self, server, name, **arguments):
        result = await anyio.to_thread.run_sync(server._call, {'name': name, 'arguments': arguments})
        self.assertFalse(result['isError'], result.get('structuredContent'))
        return result['structuredContent']

    def test_read_wakes_presence_and_reply_wakes_serial_sender_with_pacing(self):
        client, typing = RecordingClient(), RecordingTyping()

        async def stdio(server):
            await self.await_event(typing.started)
            await self.tool(server, 'read_weixin_message', message_id=self.mid)
            await self.await_event(typing.refreshed)
            await self.tool(server, 'reply_weixin_message', message_id=self.mid,
                            text='synthetic first', reply_key='first', final=False)
            await self.await_event(client.sent)
            await self.tool(server, 'reply_weixin_message', message_id=self.mid,
                            text='synthetic second', reply_key='second', final=False)
            await self.tool(server, 'reply_weixin_message', message_id=self.mid,
                            text='synthetic third', reply_key='third', final=False)
            # Repeated notifications cannot shorten the send floor.
            for _ in range(10):
                await anyio.to_thread.run_sync(server._wake, 'send')
            with anyio.fail_after(1):
                while len(client.sends) < 3:
                    await anyio.sleep(0.01)

        self.run_runtime(stdio, client=client, typing=typing)
        self.assertLess(typing.ticks[1] - typing.ticks[0], 0.35)
        self.assertEqual(len(client.sends), 3)
        self.assertTrue(all(b - a >= 0.095 for a, b in zip(client.sends, client.sends[1:])))
        self.assertEqual(self.store.get_status()['reply_status'], {'accepted': 3})

    def test_final_reply_triggers_body_cleanup_in_running_daemon(self):
        client = RecordingClient()

        async def stdio(server):
            await self.tool(server, 'reply_weixin_message', message_id=self.mid,
                            text='synthetic final reply', reply_key='final')
            await self.await_event(client.sent)
            with anyio.fail_after(1.5):
                while not self.store.read_message(self.mid)['text_cleared']:
                    await anyio.sleep(0.01)
            self.assertEqual(self.store.send_status(self.mid)['status'], 'accepted')
            duplicate = await self.tool(server, 'reply_weixin_message', message_id=self.mid,
                                       text='synthetic final reply', reply_key='final')
            self.assertEqual(duplicate['status'], 'accepted')

        self.run_runtime(stdio, client=client)
        self.assertEqual(len(client.sends), 1)

    def test_notification_while_presence_is_busy_is_not_lost(self):
        typing = RecordingTyping()
        typing.block_refresh = True

        async def stdio(server):
            await self.await_event(typing.started)
            await self.tool(server, 'read_weixin_message', message_id=self.mid)
            await self.await_event(typing.refreshed)
            await self.tool(server, 'report_weixin_progress', message_id=self.mid, active=True)
            released = time.monotonic()
            typing.release.set()
            with anyio.fail_after(0.35):
                while len(typing.ticks) < 3:
                    await anyio.sleep(0.01)
            self.assertLess(typing.ticks[2] - released, 0.35)

        self.run_runtime(stdio, typing=typing)

    def test_periodic_sender_finds_a_queue_commit_without_notification(self):
        client, typing = RecordingClient(), RecordingTyping()

        async def stdio(server):
            await self.await_event(typing.started)
            # An embedding or an offline writer need not know about wakeups.
            self.store.queue_reply(self.mid, 'synthetic unnotified reply')
            await self.await_event(client.sent, timeout=0.8)

        self.run_runtime(stdio, client=client, typing=typing)
        self.assertEqual(len(client.sends), 1)

    def test_send_wake_cannot_bypass_channel_rate_limit(self):
        client = RecordingClient()

        async def stdio(server):
            await self.tool(server, 'reply_weixin_message', message_id=self.mid,
                            text='synthetic rate-limited reply', reply_key='limited')
            for _ in range(10):
                await anyio.to_thread.run_sync(server._wake, 'send')
                await anyio.sleep(0.02)
            self.assertEqual(client.sends, [])
            await self.await_event(client.sent, timeout=1)

        def limit(worker):
            worker.send_after = time.monotonic() + 0.6

        self.run_runtime(stdio, client=client, configure_worker=limit)
        self.assertEqual(len(client.sends), 1)

    def test_new_committed_inbound_wakes_presence_without_claiming_and_duplicates_do_not(self):
        client, typing = RecordingClient(), RecordingTyping()
        release = threading.Event()
        previous = []
        inbound = {'message_id': 123456, 'from_user_id': 'fixture-owner', 'message_type': 1,
                   'message_state': 2, 'create_time_ms': int(self.store.clock() * 1000),
                   'context_token': 'fixture-context',
                   'item_list': [{'type': 1, 'text_item': {'text': 'synthetic new input'}}]}

        def poll(binding):
            release.wait(1)
            return {'msgs': [inbound], 'get_updates_buf': 'fixture-cursor'}

        client.poll = poll

        async def stdio(server):
            await self.await_event(typing.started)
            release.set()
            await self.await_event(typing.refreshed)
            self.assertEqual(len(typing.new_ids), 1)
            mid = typing.new_ids[0]
            self.assertEqual(self.store.work_status(mid)['state'], 'waiting')
            row = next(row for row in self.store.get_status()['pending_processing'] if row['message_id'] == mid)
            self.assertFalse(row['read_observed'])
            await anyio.sleep(0.22)
            self.assertEqual(typing.new_ids, [mid])
            self.assertEqual(previous, [(mid,)])

        self.run_runtime(stdio, client=client, typing=typing, on_received=previous.append)

    def test_fast_empty_polls_have_a_floor_but_no_half_second_success_gap(self):
        client = RecordingClient()

        async def stdio(server):
            await anyio.sleep(0.36)

        self.run_runtime(stdio, client=client)
        self.assertGreaterEqual(len(client.polls), 3)
        self.assertLessEqual(len(client.polls), 5)
        self.assertTrue(all(b - a >= 0.085 for a, b in zip(client.polls, client.polls[1:])))

    def test_long_successful_poll_continues_immediately(self):
        client = RecordingClient(poll_delay=0.12)

        async def stdio(server):
            with anyio.fail_after(0.5):
                while len(client.polls) < 2:
                    await anyio.sleep(0.01)

        self.run_runtime(stdio, client=client)
        self.assertLess(client.polls[1] - client.poll_ends[0], 0.15)

    def test_poll_network_failure_retains_backoff(self):
        client = RecordingClient(poll_error='network_error')

        async def stdio(server):
            await anyio.sleep(1.15)

        self.run_runtime(stdio, client=client)
        self.assertEqual(len(client.polls), 2)
        self.assertGreaterEqual(client.polls[1] - client.polls[0], 0.95)

    def test_event_precedes_demand_download_and_read_wakes_media(self):
        download, release, acknowledged = (threading.Event() for _ in range(3))
        completed = []

        def fetch(descriptor):
            download.set()
            release.wait(1)
            completed.append(time.monotonic())
            return b'{}'

        def callback(url, body, headers):
            value = json.loads(body)
            if value.get('type') == 'verification':
                return 200, json.dumps({'challenge': value['challenge']}).encode()
            if value['data']['message_id'] == mid:
                acknowledged.set()
            return 204, b''

        events = Events(self.store, ['callback.example.com'], callback)
        events.subscribe({'name': 'weixin.message', 'arguments': {}, 'delivery': {'mode': 'webhook',
                          'url': 'https://callback.example.com/fixture',
                          'secret': 'whsec_' + base64.b64encode(b'fixture-secret-with-32-byte-data!').decode()}})
        self.store.ingest('fixture-account', [{'source_id': 'fixture-media', 'peer': 'fixture-owner',
                         'text': 'synthetic media', 'created_at': self.store.clock(),
                         'context_token': 'fixture-context', 'attachments': [
                             {'kind': 'file', 'name': 'fixture.json', 'url': 'fixture-download'}]}], 'fixture-cursor')
        mid = self.store.list_pending()[1]['message_id']

        aid = self.store.attachment_metadata(mid)[0]['attachment_id']

        async def stdio(server):
            await self.await_event(acknowledged)
            self.assertFalse(download.is_set())
            requested = time.monotonic()
            result = await anyio.to_thread.run_sync(lambda: server._call({
                'name': 'read_weixin_attachment', 'arguments': {'attachment_id': aid}}))
            self.assertEqual(result['structuredContent']['error'], 'attachment_pending')
            await self.await_event(download)
            self.assertLess(time.monotonic() - requested, 0.35)
            release.set()
            with anyio.fail_after(1):
                while self.store.attachment_metadata(mid)[0]['status'] != 'ready':
                    await anyio.sleep(0.01)

        with patch('wechat_dot_bridge.media.fetch', fetch):
            self.run_runtime(stdio, events=events, media=MediaWorker(self.store))


if __name__ == "__main__":
    unittest.main()
