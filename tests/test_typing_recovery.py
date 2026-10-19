"""Presence HTTP contracts, generation recovery and waiting leases."""

import base64
import contextlib
import json
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from network_fixtures import FakeSocket, FakeTimer
from wechat_dot_bridge import net_transport as net
from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.store import Store, _opaque_id
from wechat_dot_bridge.transport import IlinkClient, MAX_RESPONSE_BYTES, WeixinWorker, _normalize_message
from wechat_dot_bridge.typing import TypingIndicator, WAITING_SECONDS


class _TypingFixture:
    def setUp(self):
        net.reset_cancellation()
        self.directory = tempfile.TemporaryDirectory()
        self.now = 0.0
        self.store = Store(Path(self.directory.name) / 'state', clock=lambda: 1800000000 + self.now)
        self.binding = {'id': 'fixture-account', 'owner': 'fixture-owner', 'token': 'fixture-token',
                        'base_url': 'https://ilinkai.weixin.qq.com'}
        self.store.import_binding(self.binding)
        self.store.set_enabled(True)
        self.store.ingest('fixture-account', [{'source_id': str(index), 'peer': 'fixture-owner',
                          'text': 'synthetic inbound', 'created_at': self.store.clock(),
                          'context_token': 'fixture-context'} for index in range(2)], 'fixture-cursor')
        self.mids = [row['message_id'] for row in self.store.list_pending()]
        self.client = IlinkClient(clock=self.store.clock)
        self.typing = TypingIndicator(self.store, self.client, clock=lambda: self.now)
        # Every request must be covered by an explicit synthetic exchange.
        self.block_dns = patch('wechat_dot_bridge.net_transport.socket.getaddrinfo',
                               side_effect=AssertionError('real DNS forbidden'))
        self.block_socket = patch('wechat_dot_bridge.net_transport.socket.socket',
                                  side_effect=AssertionError('real network forbidden'))
        self.block_dns.start()
        self.block_socket.start()

    def tearDown(self):
        try:
            self.typing.close()
            self.store.close()
            net.reset_cancellation()
        finally:
            self.block_socket.stop()
            self.block_dns.stop()
            self.directory.cleanup()

    @contextlib.contextmanager
    def network(self, responses):
        wires = []
        remaining = iter(responses)
        context = Mock()

        def wrap(raw, **kwargs):
            status, body = next(remaining)
            tls = FakeSocket(b'HTTP/1.1 ' + str(status).encode() + b' fixture\r\nContent-Length: ' +
                             str(len(body)).encode() + b'\r\n\r\n' + body)
            wires.append(tls)
            return tls

        context.wrap_socket.side_effect = wrap
        entries = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('8.8.8.8', 443))]
        with patch('wechat_dot_bridge.net_transport.socket.socket', side_effect=lambda *args: FakeSocket()) as factory, \
                patch('wechat_dot_bridge.net_transport.socket.getaddrinfo', return_value=entries), \
                patch('wechat_dot_bridge.net_transport.ssl.create_default_context', return_value=context), \
                patch('wechat_dot_bridge.net_transport.threading.Timer', FakeTimer):
            yield wires, factory

    def read(self, mid):
        value = self.store.read_message(mid)
        self.typing.sync(mid)
        return value['processing']

    @staticmethod
    def request(tls):
        wire = b''.join(tls.sent)
        headers, body = wire.split(b'\r\n\r\n', 1)
        return headers.split(b'\r\n', 1)[0], json.loads(body)

    @staticmethod
    def ticket():
        return 200, b'{"ret":0,"typing_ticket":"fixture-ticket"}'


class TypingRecoveryTests(_TypingFixture, unittest.TestCase):
    def test_opaque_typing_ack_preserves_start_refresh_and_stop(self):
        with self.network([self.ticket(), (200, b'OK'), (200, b'{"unexpected":"opaque"}'),
                           (200, b'non-json stop ack')]) as (wires, factory):
            self.typing.prewarm()
            self.assertEqual(len(wires), 1)
            self.assertIsNone(self.typing.active)
            self.read(self.mids[0])
            self.typing.tick()
            self.now = 5
            self.typing.tick()
            self.typing.progress(self.mids[0], False)
            self.typing.tick()
        self.assertEqual(self.typing.status()['outcomes'], {'ticket_ok': 1, 'start_ok': 2, 'stop_ok': 1})
        self.assertIsNone(self.typing.active)
        self.assertEqual([self.request(tls)[1]['status'] for tls in wires[1:]], [1, 1, 2])
        self.assertEqual(factory.call_count, 4)

    def test_shutdown_stop_uses_same_opaque_ack_without_resuming_business(self):
        with self.network([self.ticket(), (200, b'OK'), (200, b'opaque shutdown ack')]):
            self.read(self.mids[0])
            self.typing.tick()
            stopped = net.cancellation_token()
            net.cancel_all()
            self.typing.close()
        self.assertEqual(self.typing.status()['outcomes'], {'ticket_ok': 1, 'start_ok': 1, 'stop_ok': 1})
        self.assertIsNone(self.typing.active)
        self.assertIs(net.cancellation_token(), stopped)
        self.assertTrue(stopped.is_set())

    def test_refresh_failure_keeps_valid_ticket_for_bounded_retry(self):
        with self.network([self.ticket(), (200, b'OK'), (500, b'fixture error'),
                           (200, b'stop ack'), (200, b'recovered ack'), (200, b'final stop')]) as (wires, _):
            self.typing.prewarm()
            self.read(self.mids[0])
            self.typing.tick()
            valid_until = self.typing.ticket_until
            self.now = 5
            self.typing.tick()
            self.assertEqual(self.typing.ticket_until, valid_until)
            self.assertEqual(self.typing.next_send, 10)
            self.now = 9
            self.typing.tick()
            self.assertEqual(len(wires), 4)
            self.now = 10
            self.typing.tick()
            self.typing.close()
        self.assertEqual(self.typing.status()['outcomes'],
                         {'ticket_ok': 1, 'start_ok': 2, 'start_failed': 1, 'stop_ok': 2})
        self.assertEqual(sum(b'getconfig' in self.request(tls)[0] for tls in wires), 1)

    def check_generation_recovery(self, same_member):
        responses = [self.ticket(), (500, b'fixture'), (200, b'stop'),
                     (500, b'fixture'), (200, b'stop'), (500, b'fixture'), (200, b'stop'),
                     (200, b'recovered'), (200, b'next refresh'), (200, b'final stop')]
        with self.network(responses) as (wires, _):
            self.read(self.mids[0])
            for at in (0, 5, 15):
                self.now = at
                self.typing.tick()
            self.assertEqual(self.typing.next_send, 35)
            self.assertEqual(self.typing.status()['outcomes']['ticket_ok'], 1)
            self.now = 16
            self.store.report_progress(self.mids[0], False, completed=not same_member)
            self.typing.sync(self.mids[0])
            mid = self.mids[0] if same_member else self.mids[1]
            work = self.read(mid)
            # No idle tick occurs between the old and new generations.
            self.typing.tick()
            self.assertEqual(self.typing.status()['outcomes']['start_ok'], 1)
            self.assertEqual(self.typing.next_send, 21)
            self.assertEqual(self.typing.failures, 0)
            count = len(wires)
            self.read(mid)
            self.typing.tick()
            self.now = 17
            self.store.report_progress(mid, True, processing_id=work['processing_id'], revision=work['revision'])
            self.typing.sync(mid)
            self.typing.tick()
            self.store.queue_reply(mid, 'synthetic progress', reply_key='fixture-progress', final=False)
            self.typing.sync(mid)
            self.typing.tick()
            self.store.queue_reply(mid, 'synthetic progress', reply_key='fixture-progress', final=False)
            self.typing.sync(mid)
            self.typing.tick()
            self.assertEqual(len(wires), count)
            self.assertEqual(self.typing.next_send, 21)
            self.now = 21
            self.typing.tick()
            self.typing.close()
        self.assertEqual(sum(b'getconfig' in self.request(tls)[0] for tls in wires), 1)

    def test_new_member_turn_does_not_inherit_previous_failure_backoff(self):
        self.check_generation_recovery(False)

    def test_resumed_same_member_generation_does_not_inherit_previous_backoff(self):
        self.check_generation_recovery(True)

    def test_added_active_member_recovers_backoff_without_amplifying_successful_presence(self):
        with self.network([self.ticket(), (500, b'fixture'), (200, b'stop'),
                           (200, b'recovered'), (200, b'final stop')]) as (wires, _):
            self.read(self.mids[0])
            self.typing.tick()
            self.assertEqual(self.typing.next_send, 5)
            self.now = 1
            self.read(self.mids[1])
            self.typing.tick()
            self.assertEqual(self.typing.status()['outcomes']['start_ok'], 1)
            self.assertEqual(self.typing.next_send, 6)
            count = len(wires)
            # A real regrouping changes both generations but owner presence
            # already succeeded. Keep its cadence instead of another start.
            self.store.report_progress(self.mids[0], True, message_ids=self.mids)
            self.typing.sync(self.mids[0])
            self.typing.tick()
            self.assertEqual(len(wires), count)
            self.assertEqual(self.typing.next_send, 6)
            self.typing.close()
        self.assertEqual(sum(b'getconfig' in self.request(tls)[0] for tls in wires), 1)

    def test_void_typing_ack_preserves_http_status_size_tls_and_cancellation(self):
        for status, body, expected in ((401, b'', 'auth_required'), (429, b'', 'rate_limited'),
                                       (500, b'non-json failure', 'http_error')):
            with self.subTest(status=status), self.network([(status, body)]), \
                    self.assertRaisesRegex(BridgeError, '^' + expected + '$'):
                self.client.send_typing(self.binding, 'fixture-ticket', 1)
        with self.network([(200, b'x' * (MAX_RESPONSE_BYTES + 1))]), \
                self.assertRaisesRegex(BridgeError, '^response_too_large$'):
            self.client.send_typing(self.binding, 'fixture-ticket', 1)
        with self.network([(200, b'OK')]) as (wires, _):
            # Force a TLS failure before application bytes.
            def failed_handshake():
                raise ssl.SSLError('fixture TLS failure')
            context = Mock()
            tls = FakeSocket()
            tls.on_handshake = failed_handshake
            context.wrap_socket.return_value = tls
            with patch('wechat_dot_bridge.net_transport.ssl.create_default_context', return_value=context), \
                    self.assertRaisesRegex(BridgeError, '^network_error$'):
                self.client.send_typing(self.binding, 'fixture-ticket', 1)
            self.assertEqual(tls.sent, [])
        net.cancel_all()
        with self.network([(200, b'OK')]) as (_, factory), \
                self.assertRaisesRegex(BridgeError, '^network_cancelled$'):
            self.client.send_typing(self.binding, 'fixture-ticket', 1)
        factory.assert_not_called()


class WaitingTypingTests(_TypingFixture, unittest.TestCase):
    def inbound(self, index=100, **changes):
        return {'source_id': str(index), 'peer': 'fixture-owner', 'text': 'fixture-private-body',
                'created_at': self.store.clock(), 'context_token': 'fixture-private-context', **changes}

    def ingest(self, index=100, **changes):
        value = self.inbound(index, **changes)
        self.assertEqual(self.store.ingest('fixture-account', [value], 'fixture-next-cursor',
                                          after_commit=self.typing.received), 1)
        return _opaque_id('fixture-account', 'fixture-owner', str(index))

    def wire(self, index=100, **changes):
        return {'message_id': str(index), 'from_user_id': 'fixture-owner', 'message_type': 1,
                'message_state': 2, 'context_token': 'fixture-private-context',
                'create_time_ms': self.store.clock() * 1000,
                'item_list': [{'type': 1, 'text_item': {'text': 'fixture-private-body'}}], **changes}

    @staticmethod
    def poll_response(message, cursor='fixture-poll-cursor'):
        return 200, json.dumps({'ret': 0, 'msgs': [message], 'get_updates_buf': cursor}).encode()

    def test_default_poll_commits_media_and_cursor_before_waiting_without_reading(self):
        key = base64.b64encode(bytes(range(16))).decode()
        item = {'type': 2, 'image_item': {'media': {
            'encrypt_query_param': 'fixture-private-cdn', 'aes_key': key}}}
        wire = self.wire(item_list=[item])
        observed = []

        def committed(ids):
            observed.append((ids, self.store.get_binding()['cursor'], self.store.db.in_transaction))
            self.typing.received(ids)

        worker = WeixinWorker(self.store, self.client, on_received=committed)
        with self.network([self.poll_response(wire), self.ticket(), (200, b'OK'), (200, b'stop')]) as (wires, _):
            self.assertTrue(worker.poll_once())
            mid = observed[0][0][0]
            self.assertEqual(observed[0][1:], ('fixture-poll-cursor', False))
            self.assertEqual(worker.poll_added, 1)
            self.assertEqual(self.store.work_status(mid)['state'], 'waiting')
            self.assertIsNone(self.store.db.execute('SELECT read_at FROM message_work WHERE message_id=?', (mid,)).fetchone()[0])
            self.assertEqual(self.store.attachment_metadata(mid)[0]['status'], 'deferred')
            self.typing.tick()
            self.assertEqual(self.typing.status()['waiting_leases'], 1)
            self.assertEqual(self.typing.status()['live_leases'], 0)
            self.typing.close()
        bodies = [self.request(tls)[1] for tls in wires[1:]]
        for body in bodies:
            for private in ('fixture-private-body', 'fixture-private-cdn', key, mid):
                self.assertNotIn(private, json.dumps(body))
        self.assertEqual([body['status'] for body in bodies[1:]], [1, 2])
        status = json.dumps(self.typing.status())
        for private in ('fixture-private-body', 'fixture-private-context', 'fixture-private-cdn', mid):
            self.assertNotIn(private, status)

    def test_reader_before_commit_hook_keeps_only_actual_processing(self):
        def read_first(ids):
            self.read(ids[0])
            self.typing.received(ids)

        value = self.inbound()
        self.store.ingest('fixture-account', [value], 'fixture-read-first', after_commit=read_first)
        mid = _opaque_id('fixture-account', 'fixture-owner', '100')
        self.assertEqual(self.typing.waiting, {})
        self.assertEqual(self.store.work_status(mid)['state'], 'active')
        with self.network([self.ticket(), (200, b'OK'), (200, b'stop')]):
            self.typing.tick()
            self.typing.close()
        self.assertEqual(self.typing.status()['outcomes']['start_ok'], 1)

    def test_read_after_waiting_switches_to_read_lease_without_extra_start(self):
        mid = self.ingest()
        with self.network([self.ticket(), (200, b'OK'), (200, b'refresh'), (200, b'stop')]) as (wires, _):
            self.typing.tick()
            self.now = 299
            self.read(mid)
            self.assertNotIn(mid, self.typing.waiting)
            self.assertEqual(self.typing.leases[mid], 599)
            self.typing.tick()
            count = len(wires)
            self.now = 300
            self.read(mid)
            self.typing.tick()
            self.assertEqual(len(wires), count)
            self.store.queue_reply(mid, 'fixture-complete')
            self.typing.sync(mid)
            self.typing.tick()
        self.assertIsNone(self.typing.active)
        self.assertEqual(self.typing.waiting, {})
        self.assertEqual(self.store.work_status(mid)['state'], 'completed')

    def test_duplicate_poll_does_not_extend_waiting_and_restart_does_not_restore_it(self):
        hook = Mock(side_effect=self.typing.received)
        worker = WeixinWorker(self.store, self.client, on_received=hook)
        wire = self.wire()
        with self.network([self.poll_response(wire), self.ticket(), (200, b'OK'),
                           self.poll_response(wire, 'fixture-duplicate-cursor'), (200, b'stop')]):
            self.assertTrue(worker.poll_once())
            mid = hook.call_args.args[0][0]
            self.typing.tick()
            self.now = 299
            self.assertTrue(worker.poll_once())
            self.assertEqual(worker.poll_added, 0)
            self.assertEqual(self.typing.waiting[mid], WAITING_SECONDS)
            self.now = 300
            self.typing.tick()
        hook.assert_called_once()
        self.assertIsNone(self.typing.active)
        self.assertEqual(self.store.work_status(mid)['state'], 'waiting')
        self.typing.close()
        self.store.close()
        self.store = Store(Path(self.directory.name) / 'state', clock=lambda: 1800000000 + self.now)
        self.typing = TypingIndicator(self.store, self.client, clock=lambda: self.now)
        with self.network([]) as (_, factory):
            self.typing.tick()
        factory.assert_not_called()
        self.assertEqual(self.typing.status()['waiting_leases'], 0)

    def test_failed_batch_never_registers_and_hook_failure_cannot_undo_commit(self):
        hook = Mock(side_effect=self.typing.received)
        with self.assertRaisesRegex(BridgeError, '^owner_identity_mismatch$'):
            self.store.ingest('fixture-account', [self.inbound(), self.inbound(101, peer='fixture-other-owner')],
                              'fixture-rollback', after_commit=hook)
        hook.assert_not_called()
        self.assertEqual(self.typing.waiting, {})
        self.assertEqual(self.store.get_binding()['cursor'], 'fixture-cursor')
        mid = _opaque_id('fixture-account', 'fixture-owner', '100')
        self.assertFalse(self.store.db.execute('SELECT 1 FROM messages WHERE id=?', (mid,)).fetchone())
        failed_hook = Mock(side_effect=RuntimeError('fixture-private-hook-error'))
        self.assertEqual(self.store.ingest('fixture-account', [self.inbound()], 'fixture-committed',
                                          after_commit=failed_hook), 1)
        self.assertEqual(self.store.get_binding()['cursor'], 'fixture-committed')
        self.assertEqual(self.store.work_status(mid)['state'], 'waiting')
        self.assertIsNone(self.store.get_binding()['last_error'])

    def test_completed_stopped_and_disabled_candidates_do_not_restart_waiting(self):
        mid = self.ingest()
        with self.network([self.ticket(), (200, b'OK'), (200, b'stop')]):
            self.typing.tick()
            self.store.report_progress(mid, False)
            self.typing.tick()
        self.typing.received((mid,))
        self.assertEqual(self.typing.waiting, {})
        other = self.ingest(101)
        with self.network([(200, b'OK'), (200, b'stop')]):
            self.typing.tick()
            self.store.set_enabled(False)
            self.typing.tick()
        self.assertEqual(self.typing.waiting, {})
        self.store.set_enabled(True)
        with self.network([]) as (_, factory):
            self.typing.tick()
        factory.assert_not_called()
        self.store.queue_reply(other, 'fixture-final')
        self.typing.received((other,))
        self.assertEqual(self.typing.waiting, {})

    def test_active_priority_then_other_fresh_waiting_continues_after_completion(self):
        first = self.ingest()
        with self.network([self.ticket(), (200, b'OK'), (200, b'refresh'), (200, b'stop')]) as (wires, _):
            self.typing.tick()
            self.read(first)
            self.now = 1
            second = self.ingest(101)
            self.assertEqual(self.typing._target()[2], {first})
            self.store.queue_reply(first, 'fixture-first-complete')
            self.typing.sync(first)
            self.assertEqual(self.typing._target()[2], {second})
            self.typing.tick()
            self.assertEqual(len(wires), 2)
            self.now = 5
            self.typing.tick()
            self.now = 301
            self.typing.tick()
        self.assertIsNone(self.typing.active)
        self.assertEqual(self.store.work_status(second)['state'], 'waiting')

    def test_new_waiting_ids_reset_failed_backoff_without_duplicate_amplification(self):
        first = self.ingest()
        with self.network([self.ticket(), (500, b'fixture'), (200, b'stop'), (500, b'fixture'),
                           (200, b'stop'), (200, b'recovered'), (200, b'stop')]) as (wires, _):
            self.typing.tick()
            self.now = 5
            self.typing.tick()
            self.assertEqual(self.typing.next_send, 15)
            self.now = 6
            second = self.ingest(101)
            self.typing.tick()
            self.assertEqual(self.typing.status()['outcomes']['start_ok'], 1)
            self.assertEqual(self.typing.next_send, 11)
            count = len(wires)
            self.typing.received((first, second))
            self.typing.tick()
            self.assertEqual(len(wires), count)
            self.typing.close()

    def test_old_history_tombstones_and_private_scope_never_register(self):
        old = self.ingest(100, created_at=self.typing.started_at - 3)
        self.assertEqual(self.typing.waiting, {})
        value = self.inbound(101)
        retired = _opaque_id('fixture-account', 'fixture-owner', '101')
        self.store.db.execute('INSERT INTO retired_messages VALUES(?,?)', (retired, self.store.clock()))
        hook = Mock(side_effect=self.typing.received)
        self.assertEqual(self.store.ingest('fixture-account', [value], 'fixture-tombstone', after_commit=hook), 0)
        hook.assert_not_called()
        self.typing.received((retired, 'fixture-unknown', old))
        self.assertEqual(self.typing.waiting, {})
        self.assertIsNone(self.store.typing_target((), waiting_ids=(retired,)))
        self.assertIsNone(self.store.typing_target((), waiting_ids=('fixture-unknown',) * 129))

    def test_protocol_creation_time_uses_milliseconds_and_store_time_uses_seconds(self):
        for index, value in enumerate((None, 0, float('nan'), 'fixture-invalid-time'), 200):
            with self.subTest(value=value):
                wire = self.wire(index, create_time_ms=value)
                normalized = _normalize_message(wire, self.binding, self.store.clock())
                self.assertEqual(normalized['created_at'], self.store.clock())
                self.store.ingest('fixture-account', [normalized], 'fixture-no-time', after_commit=self.typing.received)
        missing = self.wire(204)
        missing.pop('create_time_ms')
        normalized = _normalize_message(missing, self.binding, self.store.clock())
        self.assertEqual(normalized['created_at'], self.store.clock())
        self.store.ingest('fixture-account', [normalized], 'fixture-missing-time', after_commit=self.typing.received)
        known = _normalize_message(self.wire(205), self.binding, self.store.clock())
        self.assertEqual(known['created_at'], self.store.clock())
        self.store.ingest('fixture-account', [known], 'fixture-ms-time', after_commit=self.typing.received)
        # Seconds in a field explicitly named create_time_ms are not guessed.
        wrong_unit = _normalize_message(self.wire(206, create_time_ms=self.store.clock()), self.binding, self.store.clock())
        self.assertEqual(wrong_unit['created_at'], self.store.clock() / 1000)
        self.store.ingest('fixture-account', [wrong_unit], 'fixture-wrong-unit', after_commit=self.typing.received)
        self.assertNotIn(_opaque_id('fixture-account', 'fixture-owner', 'message:206'), self.typing.waiting)
        self.assertEqual(len(self.typing.waiting), 6)
        self.assertEqual(self.typing.status()['outcomes']['waiting_history_skipped'], 1)

    def test_late_new_arrival_gets_full_waiting_lease_and_restart_filters_history(self):
        self.now = 600
        mid = self.ingest(200, created_at=self.typing.started_at + 10)
        self.assertEqual(self.typing.waiting[mid], 900)
        restarted = TypingIndicator(self.store, self.client, clock=lambda: self.now)
        try:
            self.store.ingest('fixture-account', [self.inbound(201, created_at=self.store.clock() - 3)],
                              'fixture-restart-history', after_commit=restarted.received)
            self.assertEqual(restarted.waiting, {})
            self.assertEqual(restarted.status()['outcomes']['waiting_history_skipped'], 1)
            self.store.ingest('fixture-account', [self.inbound(202)], 'fixture-new-after-restart',
                              after_commit=restarted.received)
            self.assertEqual(len(restarted.waiting), 1)
        finally:
            restarted.close()

    def test_large_commit_hooks_select_valid_candidates_in_bounded_chunks(self):
        values = [self.inbound(index) for index in range(200, 326)]
        self.store.ingest('fixture-account', values, 'fixture-large-batch')
        mids = tuple(_opaque_id('fixture-account', 'fixture-owner', str(index)) for index in range(200, 326))
        with patch.object(self.store, '_waiting_typing_ids', wraps=self.store._waiting_typing_ids) as query:
            self.typing.received((mids[0], *('fixture-unknown-' + str(index) for index in range(128))))
            self.assertEqual(len(self.typing.waiting), 1)
            self.typing.received((*mids, *('fixture-unknown-' + str(index) for index in range(874))))
        self.assertEqual(len(self.typing.waiting), 126)
        self.assertLessEqual(len(self.typing.waiting_seen), 128)
        self.assertTrue(all(len(call.args[0]) <= 128 for call in query.call_args_list))

    def test_old_target_snapshot_cannot_discard_concurrent_committed_waiting(self):
        inspected = threading.Event()
        release = threading.Event()
        original = self.store._waiting_typing_ids

        def delayed(ids, **kwargs):
            value = original(ids, **kwargs)
            if threading.current_thread().name == 'fixture-presence' and not ids:
                inspected.set()
                self.assertTrue(release.wait(timeout=2))
            return value

        targets = []
        with patch.object(self.store, '_waiting_typing_ids', side_effect=delayed):
            thread = threading.Thread(target=lambda: targets.append(self.typing._target()), name='fixture-presence')
            thread.start()
            try:
                self.assertTrue(inspected.wait(timeout=2))
                mid = self.ingest()
                self.assertIn(mid, self.typing.waiting)
            finally:
                release.set()
                thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(targets[0][2], {mid})
        self.assertIn(mid, self.typing.waiting)

    def test_old_target_snapshot_observes_concurrent_read_sync(self):
        mid = self.ingest()
        inspected = threading.Event()
        release = threading.Event()
        original = self.store._waiting_typing_ids

        def delayed(ids, **kwargs):
            value = original(ids, **kwargs)
            if threading.current_thread().name == 'fixture-presence':
                inspected.set()
                self.assertTrue(release.wait(timeout=2))
            return value

        targets = []
        with patch.object(self.store, '_waiting_typing_ids', side_effect=delayed):
            thread = threading.Thread(target=lambda: targets.append(self.typing._target()), name='fixture-presence')
            thread.start()
            try:
                self.assertTrue(inspected.wait(timeout=2))
                self.read(mid)
            finally:
                release.set()
                thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(targets[0][2], {mid})
        self.assertEqual(self.typing.waiting, {})


if __name__ == "__main__":
    unittest.main()
