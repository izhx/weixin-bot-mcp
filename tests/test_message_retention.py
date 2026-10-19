"""Regression coverage for lossless intake, demand fetching and body retirement."""
import json
import tempfile
import unittest
from unittest.mock import Mock

from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.store import Store, MAX_INBOUND_BYTES
from wechat_dot_bridge.transport import WeixinWorker
from test_transport import FIXTURE, message


class BridgeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name, clock=lambda: 1700000100)
        self.store.import_binding(FIXTURE)
        self.store.set_enabled(True)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def poll(self, payload):
        client = Mock()
        client.clock.return_value = 1700000100
        client.poll.return_value = {"msgs": [payload], "get_updates_buf": "next-cursor"}
        return WeixinWorker(self.store, client).poll_once()

    def read_all(self, mid):
        chunks, offset = [], 0
        while True:
            page = self.store.read_message(mid, offset)
            chunks.append(page['text'])
            if page['text_complete']:
                self.assertIsNone(page['next_offset'])
                return ''.join(chunks)
            self.assertGreater(page['next_offset'], offset)
            offset = page['next_offset']


class LongInboundTests(BridgeCase):
    def test_chinese_long_text_and_inline_quote_survive_cursor_commit(self):
        for index, text in enumerate(('汉' * 3000, '🙂中文' * 24000)):
            with self.subTest(size=len(text.encode())):
                payload = message(message_id=100 + index, item_list=[{'type': 1, 'text_item': {'text': text}}])
                self.assertTrue(self.poll(payload))
                mid = self.store.db.execute('SELECT id FROM messages WHERE source_id=?', ('message:' + str(100 + index),)).fetchone()[0]
                self.assertEqual(self.read_all(mid), text)
                self.assertEqual(self.store.get_binding()['cursor'], 'next-cursor')
        quote = '引用长正文' * 10000
        payload = message(message_id=102, item_list=[{'type': 1, 'text_item': {'text': '请解释'}, 'ref_msg': {'message_item': {'type': 1, 'text_item': {'text': quote}}}}])
        self.assertTrue(self.poll(payload))
        mid = self.store.db.execute("SELECT id FROM messages WHERE source_id='message:102'").fetchone()[0]
        self.assertEqual(self.read_all(mid), '[引用]\n' + quote + '\n[/引用]\n请解释')

    def test_oversize_failure_does_not_advance_cursor_or_drop_other_messages(self):
        previous = self.store.get_binding()['cursor']
        self.assertFalse(self.poll(message(item_list=[{'type': 1, 'text_item': {'text': 'x' * (MAX_INBOUND_BYTES + 1)}}])))
        self.assertEqual(self.store.get_binding()['cursor'], previous)
        self.assertEqual(self.store.get_status()['last_error'], 'inbound_too_large')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)

    def test_invalid_unicode_byte_cursor_is_rejected(self):
        self.assertTrue(self.poll(message()))
        mid = self.store.list_pending()[0]['message_id']
        with self.assertRaises(BridgeError) as error:
            self.store.read_message(mid, 1)
        self.assertEqual(error.exception.code, 'invalid_text_offset')


class DemandAttachmentTests(BridgeCase):
    def test_receive_and_event_do_not_fetch_images_voice_or_files(self):
        import base64
        from unittest.mock import patch
        from wechat_dot_bridge.events import Events
        from wechat_dot_bridge.mcp import MCPServer
        from wechat_dot_bridge.media import MediaWorker
        from test_events import Callback
        callback = Callback()
        events = Events(self.store, ['callback.example.com'], callback, self.store.clock)
        events.subscribe({'name': 'weixin.message', 'arguments': {}, 'delivery': {
            'mode': 'webhook', 'url': 'https://callback.example.com/events',
            'secret': 'whsec_' + base64.b64encode(b'fixture-signing-key-32-byte-data!').decode()}})
        server = MCPServer(self.store, events)
        worker = MediaWorker(self.store)
        for index, kind in enumerate(('image', 'voice', 'file')):
            ref = {'kind': kind, 'name': 'fixture', 'url': 'fixture', 'key': 'fixture'}
            self.store.ingest(FIXTURE['id'], [{'source_id': 'lazy-' + str(index), 'peer': FIXTURE['owner'], 'text': '[微信附件]', 'created_at': self.store.clock(), 'context_token': 'fixture-context', 'attachments': [ref]}], 'cursor')
            mid = self.store.list_pending()[-1]['message_id']
            aid = self.store.attachment_metadata(mid)[0]['attachment_id']
            with patch('wechat_dot_bridge.media.fetch', return_value=b'fixture data') as fetch:
                self.assertFalse(worker.tick())
                self.assertTrue(events.deliver_once())
                self.store.read_message(mid)
                self.assertFalse(worker.tick())
                fetch.assert_not_called()
                self.assertEqual(json.loads(callback.calls[-1][1])['data']['attachments'][0]['status'], 'deferred')
                args = {'attachment_id': aid}
                result = server._call({'name': 'read_weixin_attachment', 'arguments': args})
                self.assertEqual(result['structuredContent']['error'], 'attachment_pending')
                self.assertEqual(self.store.attachment_metadata(mid)[0]['status'], 'pending')
                server._call({'name': 'read_weixin_attachment', 'arguments': args})
                self.assertTrue(worker.tick())
                self.assertFalse(worker.tick())
                fetch.assert_called_once()
                self.assertEqual(self.store.read_attachment(aid)['data'], b'fixture data')

    def test_demand_survives_restart_and_forward_pending_does_not_lose_request(self):
        from wechat_dot_bridge.mcp import MCPServer
        from wechat_dot_bridge.events import Events
        self.store.ingest(FIXTURE['id'], [{'source_id': 'lazy', 'peer': FIXTURE['owner'], 'text': 'file', 'created_at': self.store.clock(), 'context_token': 'fixture-context', 'attachments': [{'kind': 'file', 'name': 'fixture.txt', 'url': 'fixture'}]}], 'cursor')
        mid = self.store.list_pending()[0]['message_id']
        aid = self.store.attachment_metadata(mid)[0]['attachment_id']
        server = MCPServer(self.store, Events(self.store))
        result = server._call({'name': 'reply_weixin_attachment', 'arguments': {'message_id': mid, 'attachment_id': aid}})
        self.assertEqual(result['structuredContent']['error'], 'attachment_pending')
        self.assertEqual(self.store.get_status()['reply_status'], {})
        self.store.close()
        self.store = Store(self.temp.name)
        self.assertEqual(self.store.next_attachment()['id'], aid)
        with self.assertRaisesRegex(BridgeError, 'unknown_owner_attachment'):
            self.store.request_attachment('another-owners-attachment')


class CompletedBodyTests(BridgeCase):
    def ingest(self, source='body', text='private inbound'):
        self.store.ingest(FIXTURE['id'], [{'source_id': source, 'peer': FIXTURE['owner'], 'text': text, 'created_at': self.store.clock(), 'context_token': 'fixture-context'}], 'cursor')
        return self.store.list_pending()[-1]['message_id']

    def clean(self):
        from wechat_dot_bridge.maintenance import clear_completed_bodies
        return clear_completed_bodies(self.store)

    def accept_all(self):
        while (reply := self.store.claim_send()) is not None:
            self.store.finish_send(reply['message_id'], 'accepted', reply_id=reply['reply_id'])

    def test_final_send_clears_bodies_and_keeps_idempotency_context_and_receipts(self):
        mid = self.ingest()
        reply = self.store.queue_reply(mid, 'private reply', reply_key='stable')
        self.assertEqual(self.clean()['messages_cleared'], 0)
        claimed = self.store.claim_send()
        self.assertEqual(self.clean()['messages_cleared'], 0)
        self.store.finish_send(mid, 'accepted', reply_id=claimed['reply_id'])
        self.store.remember_sent_quote('123', claimed)
        self.assertEqual(self.clean()['messages_cleared'], 1)
        result = self.store.read_message(mid)
        self.assertEqual(result['text'], '')
        self.assertTrue(result['text_cleared'])
        self.assertEqual(self.store.db.execute('SELECT text FROM replies').fetchone()[0], '')
        self.assertEqual(self.store.db.execute('SELECT text FROM reply_parts').fetchone()[0], '')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM sent_quote_cache').fetchone()[0], 0)
        self.assertEqual(self.store.queue_reply(mid, 'private reply', reply_key='stable')['reply_id'], reply['reply_id'])
        self.assertEqual(self.store.queue_reply(mid, 'private reply')['reply_id'], reply['reply_id'])
        with self.assertRaisesRegex(BridgeError, 'reply_key_conflict'):
            self.store.queue_reply(mid, 'different', reply_key='stable')
        # A sender's delayed quote hook cannot resurrect an erased body.
        self.store.remember_sent_quote('124', claimed)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM sent_quote_cache').fetchone()[0], 0)
        self.store.close()
        self.store = Store(self.temp.name)
        self.assertEqual(self.store.queue_reply(mid, 'private reply', reply_key='stable')['status'], 'accepted')
        self.assertEqual(self.store.list_pending(), [])
        self.store.queue_notification('context-after-cleanup', 'new notification')
        self.assertEqual(self.store.claim_send()['context_token'], 'fixture-context')

    def test_partial_and_uncertain_sends_keep_content_until_explicit_resolution(self):
        mid = self.ingest()
        self.store.queue_reply(mid, 'x' * 9000)
        first = self.store.claim_send()
        self.store.finish_send(mid, 'accepted', reply_id=first['reply_id'])
        self.assertEqual(self.clean()['messages_cleared'], 0)
        second = self.store.claim_send()
        self.store.finish_send(mid, 'uncertain', 'network_timeout', reply_id=second['reply_id'])
        self.assertEqual(self.clean()['messages_cleared'], 0)
        self.assertFalse(self.store.read_message(mid)['text_cleared'])
        self.assertIsNone(self.store.claim_send())
        self.store.close()
        self.store = Store(self.temp.name)
        self.store.recover_inflight()
        self.assertIsNone(self.store.claim_send())
        self.assertEqual(self.clean()['messages_cleared'], 0)
        self.store.resolve_uncertain(second['reply_id'], True)
        self.accept_all()
        self.assertEqual(self.clean()['messages_cleared'], 1)

    def test_interim_reply_and_unfinished_group_member_are_preserved(self):
        first, second = self.ingest('one'), self.ingest('two')
        self.store.read_message(first)
        self.store.read_message(second)
        self.store.queue_reply(first, 'interim', final=False)
        self.accept_all()
        self.assertEqual(self.clean()['messages_cleared'], 0)
        self.store.queue_reply(first, 'final', final=True)
        self.accept_all()
        self.assertEqual(self.clean()['messages_cleared'], 1)
        self.assertEqual(self.store.read_message(second)['text'], 'private inbound')

    def test_callback_inflight_protects_body_then_cancels_retry_without_reply(self):
        mid = self.ingest()
        self.store.db.execute("INSERT INTO deliveries(event_id,subscription_id,message_id,body,status,next_attempt) VALUES('event','sub',?,?,'sending',0)", (mid, b'private event body'))
        self.store.read_message(mid)
        self.store.report_progress(mid, False, completed=True)
        self.assertEqual(self.clean()['messages_cleared'], 0)
        self.store.db.execute("UPDATE deliveries SET status='retry'")
        self.assertEqual(self.clean()['messages_cleared'], 1)
        row = self.store.db.execute('SELECT body,status FROM deliveries').fetchone()
        self.assertEqual(tuple(row[:2]), (b'', 'cancelled'))

    def test_additional_reply_and_notification_keep_dedup_after_cleanup(self):
        mid = self.ingest()
        self.store.queue_reply(mid, 'first')
        self.accept_all()
        self.clean()
        self.store.queue_reply(mid, 'second')
        self.assertEqual(self.clean()['messages_cleared'], 0)
        self.accept_all()
        self.assertEqual(self.clean()['messages_cleared'], 1)
        self.store.queue_notification('notice', 'private notice')
        self.accept_all()
        self.assertEqual(self.clean()['notifications_cleared'], 1)
        self.assertEqual(self.store.queue_notification('notice', 'private notice')['status'], 'accepted')
        with self.assertRaisesRegex(BridgeError, 'notification_id_conflict'):
            self.store.queue_notification('notice', 'different')
        self.assertEqual(self.store.db.execute('SELECT text FROM notification_parts').fetchone()[0], '')

    def test_repoll_does_not_resurrect_and_pending_cached_quote_keeps_own_text(self):
        mid = self.ingest('original', 'original private text')
        self.store.db.execute('INSERT INTO message_aliases VALUES(?,?,?,?)', (FIXTURE['id'], FIXTURE['owner'], '123', mid))
        self.store.ingest(FIXTURE['id'], [{'source_id': 'quote', 'peer': FIXTURE['owner'], 'text': 'new question', 'created_at': self.store.clock(), 'quotes': [{'source_id': '123'}]}], 'cursor')
        quote_mid = self.store.list_pending()[-1]['message_id']
        self.store.queue_reply(mid, 'done')
        self.accept_all()
        self.clean()
        self.assertIn('original private text', self.store.read_message(quote_mid)['text'])
        self.store.ingest(FIXTURE['id'], [{'source_id': 'original', 'peer': FIXTURE['owner'], 'text': 'original private text', 'created_at': self.store.clock()}], 'cursor-2')
        self.assertTrue(self.store.read_message(mid)['text_cleared'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM messages').fetchone()[0], 2)

    def test_group_waits_for_primary_send_and_clears_segment_quote_cache(self):
        first, second = self.ingest('group-one'), self.ingest('group-two')
        self.store.read_message(first)
        self.store.read_message(second)
        self.store.queue_reply(first, 'long reply' * 1500, message_ids=[first, second])
        self.assertEqual(self.clean()['messages_cleared'], 0)
        index = 0
        while (reply := self.store.claim_send()) is not None:
            self.store.finish_send(first, 'accepted', reply_id=reply['reply_id'])
            self.store.remember_sent_quote(str(1000 + index), reply)
            index += 1
        self.assertGreater(index, 1)
        self.assertEqual(self.clean()['messages_cleared'], 2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM sent_quote_cache').fetchone()[0], 0)

    def test_rejected_sends_and_unfinished_work_are_not_cleaned(self):
        for status in ('failed', 'context_required', 'auth_required', 'uncertain'):
            with self.subTest(status=status):
                mid = self.ingest('failed-' + status)
                reply = self.store.queue_reply(mid, 'must retain for retry ' + status)
                self.store.db.execute('UPDATE replies SET status=? WHERE client_id=?', (status, reply['reply_id']))
                self.assertEqual(self.clean()['messages_cleared'], 0)
                self.assertFalse(self.store.read_message(mid)['text_cleared'])

    def test_accepted_media_reply_deduplicates_after_attachment_expiry(self):
        import base64
        mid = self.ingest()
        aid = self.store.stage_attachment(base64.b64encode(b'file bytes').decode(), 'file', 'fixture.txt')['attachment_id']
        reply = self.store.queue_reply(mid, 'file', reply_key='media', attachment_id=aid)
        self.accept_all()
        self.clean()
        self.store.db.execute("UPDATE attachments SET status='expired',data=NULL WHERE id=?", (aid,))
        duplicate = self.store.queue_reply(mid, 'file', reply_key='media', attachment_id=aid)
        self.assertEqual(duplicate['reply_id'], reply['reply_id'])
        self.assertEqual(duplicate['status'], 'accepted')
        self.assertIsNone(self.store.claim_send())
