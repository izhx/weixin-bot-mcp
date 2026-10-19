"""Weixin protocol compatibility, polling deadlines and send outcomes."""

import base64
import contextlib
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpcore

from network_fixtures import FakeSocket, FakeTimer
from wechat_dot_bridge import net_transport as net
from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.media import upload_item
from wechat_dot_bridge.operations import HealthMonitor
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.transport import IlinkClient, WeixinWorker, _normalize_message


class WeixinProtocolCompatTests(unittest.TestCase):
    def setUp(self):
        net.reset_cancellation()
        self.directory = tempfile.TemporaryDirectory()
        self.state = Path(self.directory.name) / 'state'
        self.now = 1800000000.0
        self.store = Store(self.state, clock=lambda: self.now)
        self.binding = {'id': 'fixture-account', 'owner': 'fixture-owner', 'token': 'fixture-token',
                        'base_url': 'https://ilinkai.weixin.qq.com'}
        self.store.import_binding(self.binding)
        self.store.set_enabled(True)
        self.client = IlinkClient(clock=lambda: self.now)
        self.worker = WeixinWorker(self.store, self.client)

    def tearDown(self):
        self.store.close()
        net.reset_cancellation()
        self.directory.cleanup()

    @contextlib.contextmanager
    def network(self, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        raw = FakeSocket()
        tls = FakeSocket(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
        context = Mock()
        context.wrap_socket.return_value = tls
        entries = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('8.8.8.8', 443))]
        with patch('wechat_dot_bridge.net_transport.socket.socket', return_value=raw) as factory, \
                patch('wechat_dot_bridge.net_transport.socket.getaddrinfo', return_value=entries), \
                patch('wechat_dot_bridge.net_transport.ssl.create_default_context', return_value=context), \
                patch('wechat_dot_bridge.net_transport.threading.Timer', FakeTimer):
            yield tls, factory

    def inbound(self, identifier, items):
        return {'message_id': identifier, 'from_user_id': 'fixture-owner', 'to_user_id': 'fixture-account',
                'message_type': 1, 'message_state': 2, 'context_token': 'fixture-context',
                'create_time_ms': int(self.now * 1000), 'item_list': items}

    def queue_pair(self):
        self.store.ingest('fixture-account', [
            {'source_id': f'fixture-{index}', 'peer': 'fixture-owner', 'text': 'synthetic inbound',
             'created_at': self.now, 'context_token': 'fixture-context'} for index in range(2)
        ], 'fixture-cursor')
        mids = [row['message_id'] for row in self.store.list_pending()]
        replies = [self.store.queue_reply(mid, 'synthetic reply') for mid in mids]
        return mids, replies

    def test_empty_placeholder_preserves_asr_media_cursor_and_later_quote(self):
        # CC types.go:text,omitempty + parse.go:32 permit this empty object.
        asr = self.inbound(1, [{'type': 1, 'text_item': {}},
                              {'type': 3, 'voice_item': {'text': 'synthetic ASR'}}])
        media = self.inbound(2, [{'type': 1, 'text_item': {}}, {'type': 4, 'file_item': {
            'file_name': 'fixture.json', 'media': {'encrypt_query_param': 'fixture-download',
                                                'aes_key': base64.b64encode(bytes(range(16))).decode()}}}])
        with self.network({'ret': 0, 'msgs': [asr, media], 'get_updates_buf': 'fixture-cursor-one'}):
            self.assertTrue(self.worker.poll_once())
        pending = self.store.list_pending()
        self.assertEqual(len(pending), 2)
        first, second = [row['message_id'] for row in pending]
        self.assertEqual(self.store.read_message(first)['text'].strip(), 'synthetic ASR')
        aid = self.store.attachment_metadata(second)[0]['attachment_id']
        self.store.request_attachment(aid)
        claimed = self.store.next_attachment()
        self.assertEqual(claimed['id'], aid)
        self.store.finish_attachment(aid, b'{}')
        self.assertEqual(self.store.get_binding()['cursor'], 'fixture-cursor-one')

        quoted = self.inbound(3, [{'type': 1, 'text_item': None, 'ref_msg': {'svr_id': 2}},
                                 {'type': 1, 'text_item': {'text': 'synthetic followup'}}])
        with self.network({'ret': 0, 'msgs': [quoted], 'get_updates_buf': 'fixture-cursor-two'}):
            self.assertTrue(self.worker.poll_once())
        mid = self.store.list_pending()[-1]['message_id']
        quote = self.store.read_message(mid)
        self.assertEqual(quote['quotes'][0]['status'], 'cached_inbound')
        self.assertEqual(quote['quotes'][0]['resolved_message_id'], second)
        self.assertEqual(quote['attachments'][0]['attachment_id'], aid)
        self.assertEqual(quote['attachments'][0]['status'], 'ready')
        self.assertEqual(self.store.get_binding()['cursor'], 'fixture-cursor-two')
        self.assertEqual(self.store.read_attachment(aid)['data'], b'{}')

    def test_missing_and_null_text_item_preserve_following_asr(self):
        # *textItem unmarshals both an absent field and null as nil in CC.
        messages = [self.inbound(1, [{'type': 1}, {'type': 3, 'voice_item': {'text': 'synthetic ASR'}}]),
                    self.inbound(2, [{'type': 1, 'text_item': None},
                                     {'type': 3, 'voice_item': {'text': 'synthetic ASR'}}])]
        with self.network({'ret': 0, 'msgs': messages, 'get_updates_buf': 'fixture-next'}):
            self.assertTrue(self.worker.poll_once())
        pending = self.store.list_pending()
        self.assertEqual(len(pending), 2)
        for row in pending:
            self.assertEqual(self.store.read_message(row['message_id'])['text'], 'synthetic ASR')
        self.assertEqual(self.store.get_binding()['cursor'], 'fixture-next')

    def test_placeholder_ingest_failure_rolls_back_message_context_alias_and_cursor(self):
        value = {'ret': 0, 'msgs': [self.inbound(1, [{'type': 1, 'text_item': {}},
                   {'type': 3, 'voice_item': {'text': 'synthetic ASR'}}])], 'get_updates_buf': 'fixture-next'}
        with self.network(value), patch.object(self.store, '_ingest_attachments', side_effect=BridgeError('inbox_full')):
            self.assertFalse(self.worker.poll_once())
        self.assertEqual(self.store.get_binding()['cursor'], '')
        for table in ('messages', 'message_work', 'message_aliases'):
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
        with self.network(value):
            self.assertTrue(self.worker.poll_once())
        self.assertEqual(len(self.store.list_pending()), 1)
        self.assertEqual(self.store.get_binding()['cursor'], 'fixture-next')

    def test_explicit_wrong_text_types_and_invalid_structures_remain_rejected(self):
        for text_item in ([], 'invalid', {'text': None}, {'text': 1}, {'text': False}, {'text': []}):
            with self.subTest(shape=text_item):
                value = self.inbound(1, [{'type': 1, 'text_item': text_item},
                                        {'type': 3, 'voice_item': {'text': 'synthetic ASR'}}])
                self.assertIsNone(_normalize_message(value, self.binding, self.now))
        self.assertIsNone(_normalize_message(self.inbound(1, [{'type': 1, 'text_item': {}}]), self.binding, self.now))

    def test_sendmessage_ret_minus_two_cools_down_without_errmsg_or_extra_notice(self):
        # CC weixin.go:861 uses ret=-2, independent of errmsg language/content.
        variants = [{}, {'errmsg': ''}, {'errmsg': 'synthetic alternative'},
                    {'errmsg': '限流'}, {'errmsg': 'prepare failed'}, {'errmsg': 'PREPARE FAILED'}]
        mids, replies = self.queue_pair()
        for index, fields in enumerate(variants):
            if index:
                self.store.close()
                self.store = Store(self.state, clock=lambda: self.now)
                # Each variant has an independent synthetic outbox pair.
                self.store.db.execute('DELETE FROM replies')
                replies = [self.store.queue_reply(mid, 'synthetic reply', reply_key=f'fixture-{index}') for mid in mids]
            worker = WeixinWorker(self.store, self.client)
            with self.subTest(fields=fields), \
                    patch('wechat_dot_bridge.transport.time.monotonic', return_value=100.0), \
                    self.network({'ret': -2, **fields}) as (tls, factory):
                self.assertTrue(worker.send_once())
                result = self.store.send_status(mids[0], replies[0]['reply_id'])
                self.assertEqual((result['status'], result['error']), ('failed', 'rate_limited'))
                self.assertEqual(worker.send_after, 130.0)
                self.assertFalse(worker.send_once())
                HealthMonitor(self.store).tick()
                self.assertEqual(self.store.db.execute('SELECT count(*) FROM replies WHERE is_notice=1').fetchone()[0], 0)
                factory.assert_called_once()
                self.assertEqual(b''.join(tls.sent).count(b'"client_id"'), 1)
                self.assertEqual(self.store.db.execute('SELECT status,attempts FROM replies WHERE client_id=?',
                                                      (replies[1]['reply_id'],)).fetchone()[:], ('queued', 0))
            self.store.close()
            self.store = Store(self.state, clock=lambda: self.now)
            self.store.recover_inflight()
            self.assertEqual(self.store.db.execute('SELECT status,attempts FROM replies WHERE client_id=?',
                                                  (replies[0]['reply_id'],)).fetchone()[:], ('failed', 1))

    def test_unconfirmed_ret_minus_two_meaning_is_not_applied_to_other_endpoints(self):
        with self.network({'ret': -2}):
            self.assertFalse(self.worker.poll_once())
        self.assertEqual(self.store.get_binding()['last_error'], 'gateway_error')
        with self.network({'ret': -2}), self.assertRaisesRegex(BridgeError, '^gateway_error$'):
            self.client.typing_ticket(self.binding, 'fixture-context')
        # sendtyping has an opaque HTTP ACK, unlike these parsed endpoints.
        with self.network({'ret': -2}):
            self.client.send_typing(self.binding, 'fixture-ticket', 1)
        attachment = {'kind': 'file', 'name': 'fixture.json', 'data': b'{}'}
        with self.network({'ret': -2}) as (tls, factory), self.assertRaisesRegex(BridgeError, '^gateway_error$'):
            upload_item(self.client, self.binding, attachment)
        factory.assert_called_once()
        self.assertIn(b'POST /ilink/bot/getuploadurl HTTP/1.1', b''.join(tls.sent))
        self.assertNotIn(b'POST /ilink/bot/sendmessage', b''.join(tls.sent))


class PollDeadlineTests(unittest.TestCase):
    def setUp(self):
        net.reset_cancellation()
        FakeTimer.instances = []
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / 'state')
        self.store.import_binding({'id': 'fixture-account', 'owner': 'fixture-owner',
                                  'token': 'fixture-token', 'base_url': 'https://ilinkai.weixin.qq.com'})
        self.store.set_enabled(True)
        self.store.ingest('fixture-account', [], 'fixture-cursor')
        self.raw, self.tls = FakeSocket(), FakeSocket()
        self.context = Mock()
        self.context.wrap_socket.return_value = self.tls
        self.client = IlinkClient()

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()
        net.reset_cancellation()

    @contextlib.contextmanager
    def network(self):
        entries = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('8.8.8.8', 443))]
        with patch('wechat_dot_bridge.net_transport.socket.socket', return_value=self.raw), \
                patch('wechat_dot_bridge.net_transport.socket.getaddrinfo', return_value=entries), \
                patch('wechat_dot_bridge.net_transport.ssl.create_default_context', return_value=self.context), \
                patch('wechat_dot_bridge.net_transport.threading.Timer', FakeTimer):
            yield

    def assert_empty_round(self):
        with self.network():
            worker = WeixinWorker(self.store, self.client)
            self.assertTrue(worker.poll_once())
        self.assertEqual(self.store.get_binding()['cursor'], 'fixture-cursor')
        self.assertEqual(worker.poll_added, 0)
        self.assertEqual(self.store.get_status()['pending'], 0)
        self.assertNotIn('last_error', self.store.get_status())
        self.assertIn(b'POST /ilink/bot/getupdates HTTP/1.1', b''.join(self.tls.sent))

    def test_response_read_timeout_is_empty_round_and_keeps_cursor(self):
        self.tls.recv = Mock(side_effect=TimeoutError('synthetic deadline'))
        self.assert_empty_round()

    def test_timer_closing_socket_during_response_read_is_empty_round(self):
        def read(_):
            FakeTimer.instances[-1].fire()
            raise OSError('synthetic closed socket')
        self.tls.recv = read
        self.assert_empty_round()

    def test_partial_body_write_then_read_timeout_is_a_real_failure(self):
        send = self.tls.sendall
        def write(value):
            if b'"fixture-cursor"' in value:
                raise httpcore.WriteError('synthetic partial request write')
            send(value)
        self.tls.sendall = write
        self.tls.recv = Mock(side_effect=TimeoutError('synthetic response deadline'))
        with self.network():
            self.assertFalse(WeixinWorker(self.store, self.client).poll_once())
        self.assertEqual(self.store.get_status()['last_error'], 'network_timeout')
        self.assertEqual(self.store.get_binding()['cursor'], 'fixture-cursor')

    def test_deadline_expired_just_before_response_read_is_empty_round(self):
        send = self.tls.sendall
        def write(value):
            send(value)
            # HTTPCore writes h11's empty EndOfMessage after the complete
            # Content-Length body, immediately before its response read.
            if not value:
                FakeTimer.instances[-1].fire()
        self.tls.sendall = write
        self.assert_empty_round()

    def test_dns_connect_tls_and_request_write_deadlines_remain_failures(self):
        for stage in ('dns', 'connect', 'tls', 'write'):
            self.raw, self.tls = FakeSocket(), FakeSocket()
            self.context.wrap_socket.return_value = self.tls
            with self.subTest(stage=stage), self.network():
                if stage == 'dns':
                    context = patch('wechat_dot_bridge.transport._resolve_public', side_effect=BridgeError('network_timeout'))
                else:
                    attribute = {'connect': 'connect', 'tls': 'do_handshake', 'write': 'sendall'}[stage]
                    target = self.raw if stage == 'connect' else self.tls
                    context = patch.object(target, attribute, side_effect=TimeoutError('synthetic deadline'))
                with context:
                    self.assertFalse(WeixinWorker(self.store, self.client).poll_once())
                self.assertEqual(self.store.get_status()['last_error'], 'network_timeout')
                self.assertEqual(self.store.get_binding()['cursor'], 'fixture-cursor')

    def test_cancel_published_before_socket_abort_cannot_be_empty_round(self):
        def read(_):
            # The global signal is published before cancel_all aborts its
            # captured sockets; emulate that exact interruption window.
            net.cancellation_token().set()
            raise TimeoutError('synthetic cancelled read')
        self.tls.recv = read
        with self.network():
            self.assertFalse(WeixinWorker(self.store, self.client).poll_once())
        self.assertEqual(self.store.get_status()['last_error'], 'network_cancelled')

    def test_protocol_and_auth_errors_are_not_empty_rounds(self):
        for response, diagnostic in [(b'bad HTTP\r\n\r\n', 'network_error'),
                                     (b'HTTP/1.1 401 Nope\r\nContent-Length: 0\r\n\r\n', 'auth_required')]:
            self.raw, self.tls = FakeSocket(), FakeSocket(response)
            self.context.wrap_socket.return_value = self.tls
            with self.subTest(diagnostic=diagnostic), self.network():
                self.store.set_enabled(True)
                self.assertFalse(WeixinWorker(self.store, self.client).poll_once())
                self.assertEqual(self.store.get_status()['last_error'], diagnostic)

    def test_deadline_cannot_mask_a_known_rejection_or_protocol_error(self):
        for response in (b'bad HTTP\r\n\r\n',
                         b'HTTP/1.1 401 Nope\r\nContent-Length: 0\r\n\r\n'):
            self.raw, self.tls = FakeSocket(), FakeSocket(response)
            self.context.wrap_socket.return_value = self.tls
            receive = self.tls.recv
            def read(count):
                data = receive(count)
                FakeTimer.instances[-1].fire()
                return data
            self.tls.recv = read
            with self.subTest(response=response), self.network():
                self.assertFalse(WeixinWorker(self.store, self.client).poll_once())
                self.assertEqual(self.store.get_binding()['cursor'], 'fixture-cursor')

    def test_send_response_read_timeout_stays_uncertain_and_never_resends(self):
        self.store.ingest('fixture-account', [{'source_id': 'fixture-input', 'peer': 'fixture-owner',
                         'text': 'synthetic input', 'created_at': self.store.clock(),
                         'context_token': 'fixture-context'}], 'fixture-cursor')
        mid = self.store.list_pending()[0]['message_id']
        self.store.queue_reply(mid, 'synthetic reply')
        self.tls.recv = Mock(side_effect=TimeoutError('synthetic deadline'))
        worker = WeixinWorker(self.store, self.client)
        with self.network():
            self.assertTrue(worker.send_once())
            self.assertFalse(worker.send_once())
        self.assertEqual(self.store.send_status(mid)['status'], 'uncertain')
        self.assertEqual(self.store.send_status(mid)['error'], 'network_timeout')
        self.assertEqual(b''.join(self.tls.sent).count(b'POST /ilink/bot/sendmessage HTTP/1.1'), 1)


class SendResponseTests(unittest.TestCase):
    def setUp(self):
        net.reset_cancellation()

    def tearDown(self):
        net.reset_cancellation()

    @contextlib.contextmanager
    def store(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state'
            store = Store(state)
            store.import_binding({'id': 'fixture-account', 'owner': 'fixture-owner', 'token': 'fixture-token',
                                  'base_url': 'https://ilinkai.weixin.qq.com'})
            store.set_enabled(True)
            store.ingest('fixture-account', [{'source_id': 'fixture-source', 'peer': 'fixture-owner',
                                             'text': 'synthetic inbound', 'created_at': store.clock(),
                                             'context_token': 'fixture-context'}], 'fixture-cursor')
            try:
                yield store, state, store.list_pending()[0]['message_id']
            finally:
                store.close()

    @contextlib.contextmanager
    def network(self, status, body):
        raw = FakeSocket()
        tls = FakeSocket(b'HTTP/1.1 ' + str(status).encode() + b' fixture\r\nContent-Length: ' +
                         str(len(body)).encode() + b'\r\n\r\n' + body)
        context = Mock()
        context.wrap_socket.return_value = tls
        entries = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('8.8.8.8', 443))]
        with patch('wechat_dot_bridge.net_transport.socket.socket', return_value=raw) as factory, \
                patch('wechat_dot_bridge.net_transport.socket.getaddrinfo', return_value=entries), \
                patch('wechat_dot_bridge.net_transport.ssl.create_default_context', return_value=context), \
                patch('wechat_dot_bridge.net_transport.threading.Timer', FakeTimer):
            yield tls, factory

    def check_send(self, status, body, expected, error=None, cancel=False):
        net.reset_cancellation()
        with self.store() as (store, state, mid):
            queued = store.queue_reply(mid, 'synthetic reply')
            client = IlinkClient()
            with self.network(status, body) as (tls, factory):
                if cancel:
                    def cancelled_response(count):
                        net.cancel_all()
                        raise OSError('fixture shutdown')
                    tls.recv = cancelled_response
                self.assertTrue(WeixinWorker(store, client).send_once())
            result = store.send_status(mid)
            self.assertEqual(result['status'], expected)
            self.assertEqual(result.get('error'), error)
            self.assertEqual(result['reply_id'], queued['reply_id'])
            factory.assert_called_once()
            wire = b''.join(tls.sent)
            self.assertIn(b'POST /ilink/bot/sendmessage HTTP/1.1', wire)
            self.assertEqual(wire.count(b'"client_id"'), 1)
            store.close()
            net.reset_cancellation()
            restored = Store(state)
            try:
                restored.recover_inflight()
                self.assertEqual(restored.send_status(mid)['status'], expected)
                self.assertIsNone(restored.claim_send())
                with self.network(200, b'') as (_, factory):
                    self.assertFalse(WeixinWorker(restored, client).send_once())
                factory.assert_not_called()
            finally:
                restored.close()

    def test_empty_and_whitespace_http_200_are_accepted_without_resending(self):
        # cc-connect dfad194 client.go:175 accepts TrimSpace(raw)==empty.
        for body in (b'', b' \t\r\n ', b'{"ret":0}'):
            with self.subTest(body=body):
                self.check_send(200, body, 'accepted')

    def test_non_200_responses_keep_existing_fixed_outcomes(self):
        for status, expected, error in (
            (204, 'uncertain', 'http_error'), (500, 'uncertain', 'http_error'),
            (401, 'auth_required', 'auth_required'), (403, 'auth_required', 'auth_required'),
            (429, 'failed', 'rate_limited'), (302, 'failed', 'endpoint_rejected'),
        ):
            with self.subTest(status=status):
                self.check_send(status, b'', expected, error)

    def test_nonempty_invalid_json_and_application_errors_keep_existing_outcomes(self):
        for body, expected, error in (
            (b'not-json', 'uncertain', 'response_invalid'),
            (b'[]', 'uncertain', 'response_invalid'),
            (b'null', 'uncertain', 'response_invalid'),
            (b'{"ret":"0"}', 'uncertain', 'response_invalid'),
            (b'{"ret":-1}', 'failed', 'gateway_error'),
            (b'{"ret":-14}', 'auth_required', 'auth_required'),
            (b'{"ret":-2,"errmsg":"prepare failed"}', 'failed', 'rate_limited'),
            (b'{"ret":1,"errmsg":"context_token rejected"}', 'context_required', 'context_required'),
        ):
            with self.subTest(body=body):
                self.check_send(200, body, expected, error)

    def test_cancelled_after_post_write_stays_uncertain_without_resending(self):
        self.check_send(200, b'', 'uncertain', 'network_cancelled', cancel=True)

    def test_empty_poll_response_is_not_relaxed(self):
        with self.store() as (store, _, _), self.network(200, b''):
            self.assertFalse(WeixinWorker(store, IlinkClient()).poll_once())
            self.assertEqual(store.get_binding()['last_error'], 'response_invalid')
            self.assertEqual(store.get_binding()['cursor'], 'fixture-cursor')

    def test_empty_upload_preparation_response_never_reaches_sendmessage(self):
        with self.store() as (store, _, mid):
            staged = store.stage_attachment(base64.b64encode(b'{}').decode(), 'file', 'fixture.json')
            store.queue_reply(mid, '', attachment_id=staged['attachment_id'])
            with self.network(200, b'') as (tls, factory):
                self.assertTrue(WeixinWorker(store, IlinkClient()).send_once())
            self.assertEqual(store.send_status(mid)['status'], 'uncertain')
            self.assertEqual(store.send_status(mid)['error'], 'response_invalid')
            factory.assert_called_once()
            wire = b''.join(tls.sent)
            self.assertIn(b'POST /ilink/bot/getuploadurl HTTP/1.1', wire)
            self.assertNotIn(b'POST /ilink/bot/sendmessage', wire)


if __name__ == "__main__":
    unittest.main()
