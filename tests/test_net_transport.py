"""Shared HTTP, DNS transport and webhook signature contracts."""

import base64
import contextlib
import hashlib
import hmac
import io
import json
import logging
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from network_fixtures import FakeSocket, FakeTimer
from wechat_dot_bridge import media, net_transport as net
from wechat_dot_bridge.artifacts import download
from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.events import secure_post
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.transport import IlinkClient, WeixinWorker
from wechat_dot_bridge.webhook_signing import signed_headers


class CommunityNetworkTests(unittest.TestCase):
    HOST = "ilinkai.weixin.qq.com"
    ADDRESSES = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, ("8.8.8.8", 443))]

    def setUp(self):
        net.reset_cancellation()
        FakeTimer.instances = []
        self.raw = FakeSocket()
        self.tls = FakeSocket(b'HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\n{"ret":0}')
        self.context = Mock()
        self.context.wrap_socket.return_value = self.tls

    def tearDown(self):
        net.reset_cancellation()

    @contextlib.contextmanager
    def network(self):
        with patch("wechat_dot_bridge.net_transport.socket.socket", return_value=self.raw) as factory, \
                patch("wechat_dot_bridge.net_transport.socket.getaddrinfo", side_effect=AssertionError("real DNS forbidden")), \
                patch("wechat_dot_bridge.net_transport.ssl.create_default_context", return_value=self.context), \
                patch("wechat_dot_bridge.net_transport.threading.Timer", FakeTimer):
            yield factory

    def request(self, limit=1024, proxy=None, addresses=None):
        return net.request(self.HOST, addresses or self.ADDRESSES, "POST", "/private-route?secret=private-query",
                           b"private-body", {"Authorization": "Bearer " + "fixture-token"},
                           timeout=10, max_response=limit, proxy=proxy)

    def test_chunked_response_has_a_streamed_size_limit(self):
        self.tls.response = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nabcd\r\n4\r\nefgh\r\n0\r\n\r\n"
        with self.network() as factory, self.assertRaises(net.NetworkFailure) as caught:
            self.request(limit=7)
        self.assertEqual(caught.exception.category, "response_too_large")
        factory.assert_called_once()
        self.assertTrue(self.tls.closed)

    def test_oversized_dns_answer_still_rejects_before_opening_a_socket(self):
        entries = self.ADDRESSES * (net.MAX_RESOLVED_ADDRESSES + 1)
        with self.network() as factory, self.assertRaises(net.NetworkFailure) as caught:
            net.request(self.HOST, entries, "GET", "/", None, {}, timeout=10, max_response=1024)
        self.assertEqual(caught.exception.category, "dns_failed")
        factory.assert_not_called()

    def test_declared_length_rejects_before_waiting_for_large_body(self):
        self.tls.response = b"HTTP/1.1 200 OK\r\nContent-Length: 99999999\r\n\r\n"
        with self.network(), self.assertRaises(net.NetworkFailure) as caught:
            self.request(limit=10)
        self.assertEqual(caught.exception.category, "response_too_large")
        self.assertTrue(self.tls.closed)

    def test_total_deadline_interrupts_trickled_body(self):
        parts = iter([b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n", b"a"])

        def receive(count):
            result = next(parts)
            if result == b"a":
                FakeTimer.instances[-1].fire()
            return result

        self.tls.recv = receive
        with self.network(), self.assertRaises(net.NetworkFailure) as caught:
            self.request()
        self.assertEqual(caught.exception.category, "timeout")
        self.assertTrue(self.tls.closed)
        self.assertEqual(self.tls.shutdowns, [socket.SHUT_RDWR])

    def test_ipv6_connect_is_numeric_and_bracketed_with_original_tls(self):
        addresses = [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                      ("2606:4700:4700::1111", 443, 0, 0))]
        with self.network():
            result = self.request(proxy="http://127.0.0.1:9000", addresses=addresses)
        self.assertEqual(result.body, b'{"ret":0}')
        self.assertIn(b"CONNECT [2606:4700:4700::1111]:443 HTTP/1.1", b"".join(self.raw.sent))
        self.context.wrap_socket.assert_called_once_with(self.raw, server_hostname=self.HOST,
                                                        do_handshake_on_connect=False)
        self.assertIn(b"Host: ilinkai.weixin.qq.com\r\n", b"".join(self.tls.sent))

    def test_private_httpcore_debug_trace_is_suppressed(self):
        self.tls.response = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nx-private-header: private-secret\r\n\r\n"
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("httpcore")
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            with self.network():
                self.request()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        self.assertEqual(stream.getvalue(), "")

    def test_cancel_during_dns_window_prevents_new_network_exchange(self):
        net.cancel_all()
        with self.network() as factory, self.assertRaises(net.NetworkAbort):
            self.request()
        factory.assert_not_called()

    def test_shutdown_interrupts_dns_wait_without_joining_stuck_libc(self):
        entered, release = threading.Event(), threading.Event()
        failed = []

        def lookup(*args, **kwargs):
            entered.set()
            release.wait(1)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]

        def resolve():
            try:
                net.resolve_public_addresses(self.HOST, 10, threading.BoundedSemaphore(1))
            except net.NetworkFailure as error:
                failed.append(error.category)

        with patch("wechat_dot_bridge.net_transport.socket.getaddrinfo", side_effect=lookup):
            worker = threading.Thread(target=resolve)
            worker.start()
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            net.cancel_all()
            worker.join(0.5)
            release.set()
            self.assertFalse(worker.is_alive())
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(failed, ["cancelled"])

    def test_cancel_during_body_closes_socket_and_interrupts_retry_wait(self):
        stop_signal = net.cancellation_token()
        parts = iter([b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n", b"abcd"])

        def receive(count):
            result = next(parts)
            if result == b"abcd":
                net.cancel_all()
            return result

        self.tls.recv = receive
        with self.network(), self.assertRaises(net.NetworkAbort):
            self.request()
        self.assertTrue(self.tls.closed)
        started = time.monotonic()
        with self.assertRaises(net.NetworkAbort):
            net.wait_for_retry(30, stop_signal)
        self.assertLess(time.monotonic() - started, 0.1)

    def test_failure_after_actual_send_bytes_is_uncertain_and_never_retried(self):
        def fail_after_write(value):
            self.tls.sent.append(bytes(value))
            if b'"client_id"' in value:
                raise TimeoutError("private-route private-token")

        self.tls.sendall = fail_after_write
        binding = {"id": "fixture-account", "owner": "fixture-owner", "token": "fixture-token-NOT-REAL",
                   "base_url": "https://ilinkai.weixin.qq.com", "cursor": ""}
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            store = Store(state)
            store.import_binding(binding)
            store.set_enabled(True)
            store.ingest(binding["id"], [{"source_id": "1", "peer": binding["owner"], "text": "fixture",
                                         "created_at": 100, "context_token": "private-context"}], "")
            mid = store.list_pending()[0]["message_id"]
            store.queue_reply(mid, "fixture reply")
            client = IlinkClient()
            try:
                with self.network() as factory, patch("wechat_dot_bridge.transport._resolve_public", return_value="8.8.8.8"):
                    self.assertTrue(WeixinWorker(store, client).send_once())
                self.assertEqual(store.send_status(mid)["status"], "uncertain")
                self.assertEqual(store.send_status(mid)["error"], "network_timeout")
                factory.assert_called_once()
                self.assertEqual(b"".join(self.tls.sent).count(b'"client_id"'), 1)
            finally:
                store.close()
            reopened = Store(state)
            try:
                reopened.recover_inflight()
                self.assertIsNone(reopened.claim_send())
                self.assertEqual(reopened.send_status(mid)["status"], "uncertain")
            finally:
                reopened.close()


class StandardWebhookInteropTests(unittest.TestCase):
    def test_exact_unicode_bytes_and_both_rotation_keys_match_reference_protocol(self):
        body = '{"text":"中文 😀","spacing":"  "}'.encode("utf-8")
        keys = (b"a" * 32, b"b" * 32)
        secrets = ["whsec_" + base64.b64encode(key).decode("ascii") for key in keys]
        result = signed_headers("evt_fixture", body, secrets[0], 1800000000.999,
                                "sub_fixture", old_secret=secrets[1])
        signed = b"evt_fixture.1800000000." + body
        expected = ["v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")
                    for key in keys]
        self.assertEqual(result["webhook-signature"], " ".join(expected))
        self.assertEqual(result["webhook-timestamp"], "1800000000")
        self.assertEqual(result["X-MCP-Subscription-Id"], "sub_fixture")

    def test_non_utf8_payload_is_rejected_with_fixed_category(self):
        secret = "whsec_" + base64.b64encode(b"a" * 32).decode("ascii")
        with self.assertRaisesRegex(BridgeError, "^invalid_signing_payload$"):
            signed_headers("evt_fixture", b"\xffprivate-payload", secret, 1800000000, "sub_fixture")


class SharedDNSTransportTests(unittest.TestCase):
    PROXY = 'http://127.0.0.1:9000'
    IP = '93.184.216.34'

    def setUp(self):
        net.reset_cancellation()
        self.previous = media.NETWORK_PROXY, media.NETWORK_DNS
        media.configure_network(self.PROXY, 'google')
        self.block_dns = patch('wechat_dot_bridge.net_transport.socket.getaddrinfo',
                               side_effect=AssertionError('real DNS forbidden'))
        self.block_socket = patch('wechat_dot_bridge.net_transport.socket.socket',
                                  side_effect=AssertionError('real network forbidden'))
        self.block_dns.start()
        self.block_socket.start()

    def tearDown(self):
        self.block_socket.stop()
        self.block_dns.stop()
        media.configure_network(*self.previous)
        net.reset_cancellation()

    @staticmethod
    def response(status, body, *, declared=None):
        size = len(body) if declared is None else declared
        return b'HTTP/1.1 ' + str(status).encode() + b' fixture\r\nContent-Length: ' + str(size).encode() + b'\r\n\r\n' + body

    def answer(self, records=None):
        return json.dumps({'Status': 0, 'Answer': records or [
            {'type': 5, 'data': 'alias.example.net.'}, {'type': 1, 'data': self.IP}]}).encode()

    @contextlib.contextmanager
    def network(self, responses, *, interrupt=False, tls_failure=False):
        raws, tls_sockets, names = [], [], []
        remaining = iter(responses)
        context = Mock()

        def raw_socket(*args):
            raw = FakeSocket()
            raws.append(raw)
            return raw

        def wrap(raw, **kwargs):
            names.append(kwargs['server_hostname'])
            tls = FakeSocket(next(remaining))
            tls_sockets.append(tls)
            if tls_failure:
                def fail():
                    raise ssl.SSLCertVerificationError('fixture-private-provider-detail')
                tls.on_handshake = fail
            if interrupt:
                receive = tls.recv

                def cancelled_read(count):
                    data = receive(count)
                    net.cancel_all()
                    return data

                tls.recv = cancelled_read
            return tls

        context.wrap_socket.side_effect = wrap
        with patch('wechat_dot_bridge.net_transport.socket.socket', side_effect=raw_socket) as factory, \
                patch('wechat_dot_bridge.net_transport.socket.getaddrinfo',
                      side_effect=AssertionError('unexpected DNS fallback')) as dns, \
                patch('wechat_dot_bridge.net_transport.ssl.create_default_context', return_value=context), \
                patch('wechat_dot_bridge.net_transport.threading.Timer', FakeTimer):
            yield raws, tls_sockets, names, factory, dns

    def invoke(self, kind):
        if kind == 'callback':
            return secure_post('https://callback.example.com/fixture-private-route?key=fixture-private-query',
                               b'{"body":"fixture-private-body"}', {'webhook-signature': 'fixture-private-signature'},
                               {'callback.example.com'}, proxy=self.PROXY, dns='google')
        if kind == 'media':
            return media.cdn_request(media.CDN_BASE + '/download?encrypted_query_param=fixture-private-media')
        return download('https://artifact.example.com/fixture-private-file?key=fixture-private-query')

    def test_google_provider_and_application_use_distinct_pinned_hosts_without_credentials(self):
        hosts = {'callback': 'callback.example.com', 'media': media.CDN_HOST, 'artifact': 'artifact.example.com'}
        ambient = {key: 'http://192.0.2.100:8080' for key in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY')}
        for kind, host in hosts.items():
            status, body = (204, b'') if kind == 'callback' else (200, b'fixture-data')
            with self.subTest(kind=kind), patch.dict(os.environ, ambient), \
                    self.network([self.response(200, self.answer()), self.response(status, body)]) as (raws, sockets, names, factory, dns):
                self.invoke(kind)
            self.assertEqual(names, ['dns.google', host])
            self.assertEqual([raw.connected for raw in raws], [[('127.0.0.1', 9000)]] * 2)
            self.assertIn(b'CONNECT 8.8.8.8:443 HTTP/', b''.join(raws[0].sent))
            self.assertIn(b'CONNECT 93.184.216.34:443 HTTP/', b''.join(raws[1].sent))
            provider = b''.join(sockets[0].sent)
            self.assertIn(('GET /resolve?name=' + host + '&type=A HTTP/').encode(), provider)
            self.assertIn(b'Host: dns.google\r\n', provider)
            self.assertIn(('Host: ' + host + '\r\n').encode(), b''.join(sockets[1].sent))
            self.assertNotIn(b'Authorization', provider)
            self.assertNotIn(b'fixture-private', provider)
            self.assertNotIn(b'webhook-signature', provider)
            self.assertNotIn(b'alias.example.net', provider)
            self.assertTrue(all(tls.closed for tls in sockets))
            self.assertEqual(factory.call_count, 2)
            dns.assert_not_called()

    def test_mixed_private_doh_answers_reject_before_application_and_never_fall_back(self):
        records = [{'type': 1, 'data': self.IP}, {'type': 1, 'data': '127.0.0.1'}]
        for kind, expected in (('callback', 'callback_dns_blocked'), ('media', 'media_network_failed'),
                               ('artifact', 'artifact_endpoint_rejected')):
            with self.subTest(kind=kind), self.network([self.response(200, self.answer(records))]) as (_, _, names, factory, dns), \
                    self.assertRaisesRegex(BridgeError, '^' + expected + '$'):
                self.invoke(kind)
            self.assertEqual(names, ['dns.google'])
            factory.assert_called_once()
            dns.assert_not_called()

    def test_provider_cancellation_keeps_each_callers_fixed_category_and_never_retries(self):
        for kind, expected in (('callback', 'callback_cancelled'), ('media', 'media_cancelled'),
                               ('artifact', 'artifact_cancelled')):
            net.reset_cancellation()
            with self.subTest(kind=kind), self.network([self.response(200, self.answer())], interrupt=True) as (_, sockets, names, factory, dns), \
                    self.assertRaisesRegex(BridgeError, '^' + expected + '$'):
                self.invoke(kind)
            self.assertTrue(net.cancellation_token().is_set())
            self.assertTrue(sockets[0].closed)
            self.assertEqual(names, ['dns.google'])
            factory.assert_called_once()
            dns.assert_not_called()

    def test_provider_tls_failure_and_redirect_never_reach_application(self):
        for kind, tls_code, redirect_code in (('callback', 'callback_tls_failed', 'callback_redirect'),
                                               ('media', 'media_network_failed', 'media_network_failed'),
                                               ('artifact', 'artifact_download_failed', 'artifact_download_failed')):
            for failure, expected in ((True, tls_code), (False, redirect_code)):
                with self.subTest(kind=kind, tls_failure=failure), \
                        self.network([self.response(302, b'')], tls_failure=failure) as (_, sockets, names, factory, dns), \
                        self.assertRaisesRegex(BridgeError, '^' + expected + '$'):
                    self.invoke(kind)
                self.assertEqual(names, ['dns.google'])
                self.assertNotIn(b'fixture-private', b''.join(sockets[0].sent))
                factory.assert_called_once()
                dns.assert_not_called()

    def test_provider_size_limit_does_not_claim_the_media_itself_is_oversized(self):
        for kind, expected in (('callback', 'callback_response_too_large'), ('media', 'media_network_failed'),
                               ('artifact', 'artifact_download_failed')):
            with self.subTest(kind=kind), \
                    self.network([self.response(200, b'', declared=net.MAX_DNS_RESPONSE + 1)]) as (_, _, names, factory, dns), \
                    self.assertRaisesRegex(BridgeError, '^' + expected + '$'):
                self.invoke(kind)
            self.assertEqual(names, ['dns.google'])
            factory.assert_called_once()
            dns.assert_not_called()

    def test_malformed_provider_response_is_a_fixed_dns_failure_without_fallback(self):
        bodies = [b'not-json', b'[' * 2000 + b']' * 2000,
                  b'{"Status":0,"TC":true,"Answer":[{"type":1,"data":"93.184.216.34"}]}']
        for body in bodies:
            with self.subTest(body_size=len(body)), self.network([self.response(200, body)]) as (_, _, names, factory, dns), \
                    self.assertRaises(net.NetworkFailure) as raised:
                net.resolve_addresses('artifact.example.com', dns='google', proxy=self.PROXY)
            self.assertEqual(str(raised.exception), 'dns_failed')
            self.assertEqual(names, ['dns.google'])
            factory.assert_called_once()
            dns.assert_not_called()

    def test_proxy_dns_and_hostname_validation_precede_all_provider_io(self):
        cases = ({'proxy': 'http://127.0.0.1:0'}, {'dns': 'fixture-invalid-dns'},
                 {'host': 'artifact.example.com/fixture-private-route'})
        for case in cases:
            options = {'dns': 'google', 'proxy': self.PROXY, **case}
            host = options.pop('host', 'artifact.example.com')
            with self.subTest(case=case), self.network([]) as (_, _, _, factory, dns), \
                    self.assertRaises(net.NetworkFailure):
                net.resolve_addresses(host, **options)
            factory.assert_not_called()
            dns.assert_not_called()


if __name__ == "__main__":
    unittest.main()
