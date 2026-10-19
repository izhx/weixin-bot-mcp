"""Weixin channel features, media handling and recovery."""

import base64
import hashlib
import io
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

from wechat_dot_bridge import audio
from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.media import upload_item
from wechat_dot_bridge.message_format import partial_quote, split_text
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.transport import IlinkClient, _normalize_message


class ChannelFeatures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.binding = dict(id='bot', owner='owner', token='fixture', base_url='https://ilinkai.weixin.qq.com')
        self.store.import_binding(self.binding)
        self.store.set_enabled(True)
        self.store.ingest('bot', [dict(source_id='message:1', peer='owner', text='fixture', created_at=1, context_token='fixture')], 'c')
        self.mid = self.store.list_pending()[0]['message_id']

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def restart(self):
        self.store.close()
        self.store = Store(self.tmp.name)
        self.store.recover_inflight()

    def test_long_unicode_reply_survives_restart_without_resending_accepted_parts(self):
        text = '中文🙂abc\n' * 3000
        receipt = self.store.queue_reply(self.mid, text, 'long')
        self.assertGreater(receipt['parts_total'], 3)
        chunks, ids = [], []
        for _ in range(receipt['parts_total']):
            reply = self.store.claim_send()
            chunks.append(reply['text']); ids.append(reply['client_id'])
            self.assertLessEqual(len(reply['text'].encode()), 8192)
            self.assertLessEqual(len(reply['text']), 3800)
            self.store.finish_send(self.mid, 'accepted', reply_id=reply['reply_id'])
            self.restart()
        self.assertEqual(''.join(chunks), text)
        self.assertEqual(len(set(ids)), len(ids))
        self.assertIsNone(self.store.claim_send())
        status = self.store.queue_reply(self.mid, text, 'long')
        self.assertEqual(status['status'], 'accepted')
        self.assertEqual(status['parts_accepted'], status['parts_total'])

    def test_uncertain_part_stops_remaining_parts_but_allows_completion(self):
        self.store.queue_reply(self.mid, 'x' * 12000)
        first = self.store.claim_send()
        self.store.finish_send(self.mid, 'accepted', reply_id=first['reply_id'])
        second = self.store.claim_send()
        self.restart()
        status = self.store.send_status(self.mid, second['reply_id'])
        self.assertEqual(status['status'], 'uncertain')
        self.assertEqual(status['parts_accepted'], 1)
        self.assertIsNone(self.store.claim_send())
        self.store.queue_reply(self.mid, 'completion')
        self.assertEqual(self.store.claim_send()['text'], 'completion')

    def test_chunk_upload_retries_and_restart_and_last_chunk_idempotency(self):
        a, b = b'a' * (1024 * 1024), b'b' * 123
        stage = self.store.stage_attachment
        args = (base64.b64encode(a).decode(), 'file', 'test.bin', 'upload', 0, False)
        self.assertEqual(stage(*args)['next_offset'], len(a))
        self.assertEqual(stage(*args)['next_offset'], len(a))
        self.restart()
        args = (base64.b64encode(b).decode(), 'file', 'test.bin', 'upload', len(a), True)
        result = self.store.stage_attachment(*args)
        self.assertEqual(self.store.read_attachment(result['attachment_id'])['data'], a+b)
        self.restart()
        self.assertEqual(self.store.stage_attachment(*args), result)
        with self.assertRaises(BridgeError):
            self.store.stage_attachment(base64.b64encode(b'changed').decode(), 'file', 'test.bin', 'upload', len(a), True)

    def test_inline_quote_and_partial_quote_and_voice_asr(self):
        ref = dict(message_item=dict(type=1, text_item=dict(text='before selected after')), partial_text=dict(start='selected', end='selected', startindex=0, endindex=0, quotemd5=hashlib.md5(b'selected').hexdigest()))
        msg = dict(message_id=2, from_user_id='owner', message_type=1, message_state=2, item_list=[dict(type=1, text_item=dict(text='explain'), ref_msg=ref)])
        result = _normalize_message(msg, self.binding, 1)
        self.assertIn('[引用]\nselected\n[/引用]', result['text'])
        self.assertIn('explain', result['text'])
        msg['item_list'] = [dict(type=3, voice_item=dict(text='语音转写'))]
        self.assertEqual(_normalize_message(msg, self.binding, 1)['text'], '语音转写')
        self.assertEqual(partial_quote('hello', dict(start='x')), 'hello')

    def test_native_amr_and_silk_and_unknown_audio_file_fallback(self):
        client = Mock()
        client._post.return_value = {'upload_param': 'fixture'}
        for data, expected, encoding in [(b'#!AMR\nfixture', 3, 0), (b'\x02#!SILK_V3fixture', 3, 1), (b'RIFFfixture', 4, None)]:
            with patch('wechat_dot_bridge.audio.available', return_value=False), patch('wechat_dot_bridge.media.cdn_request', return_value=(b'', 'fixture')):
                item = upload_item(client, self.binding, dict(data=data, kind='voice', name='voice.bin'))
            self.assertEqual(item['type'], expected)
            if expected == 3:
                self.assertEqual(item['voice_item']['encode_type'], encoding)

    def test_quote_id_cache_restored_after_restart(self):
        self.store.remember_sent_quote('123', {'text': 'previous bot reply'})
        self.restart()
        for sid, expected in [('1', 'fixture'), ('123', 'previous bot reply')]:
            wire = dict(message_id=int(sid)+1000, from_user_id='owner', message_type=1, message_state=2,
                        item_list=[dict(type=1, text_item=dict(text='explain'), ref_msg=dict(svr_id=sid))])
            msg = _normalize_message(wire, self.binding, 2)
            self.store.ingest('bot', [msg], 'next')
            row = self.store.db.execute('SELECT text FROM messages WHERE source_id=?', (msg['source_id'],)).fetchone()
            self.assertIn('[引用缓存]\n' + expected, row['text'])

    def test_markdown_preserves_code_and_urls(self):
        from wechat_dot_bridge.message_format import _plain_markdown as markdown_to_text
        rendered = markdown_to_text('# Title\n**bold** [link](https://example.com)\n```python\nvalue = "**keep**"\n```\n')
        self.assertEqual(rendered, 'Title\nbold link (https://example.com)\nvalue = "**keep**"\n')

    def test_zero_message_ids_do_not_collapse_different_messages(self):
        def wire(seq):
            return dict(from_user_id='owner',message_type=1,message_state=2,message_id=0,seq=seq,client_id=str(seq),item_list=[dict(type=1,text_item=dict(text='fixture'))])
        a,b=[_normalize_message(wire(n),self.binding,1) for n in (1,2)]
        self.assertNotEqual(a['source_id'],b['source_id'])

    def test_quoted_attachment_shares_content_and_download_state(self):
        self.store.ingest('bot',[dict(source_id='message:2',peer='owner',text='file',created_at=2,context_token='fixture',attachments=[dict(kind='file',name='test.txt',url='fixture')])],'2')
        aid=self.store.db.execute('SELECT id FROM attachments').fetchone()[0]
        self.store.db.execute("UPDATE attachments SET status='downloading',attempts=3")
        self.store.ingest('bot',[dict(source_id='message:3',peer='owner',text='quote',created_at=3,context_token='fixture',quotes=[dict(source_id='2')])],'3')
        self.store.finish_attachment(aid,b'fixture')
        mid=self.store.db.execute("SELECT id FROM messages WHERE source_id='message:3'").fetchone()[0]
        meta=self.store.attachment_metadata(mid)
        self.assertEqual(meta[0]['attachment_id'],aid)
        self.assertEqual(meta[0]['status'],'ready')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM attachments').fetchone()[0],1)

    def test_retry_rejected_reply_preserves_accepted_parts(self):
        receipt=self.store.queue_reply(self.mid,'x'*8000)
        first=self.store.claim_send()
        self.store.finish_send(self.mid,'accepted',reply_id=first['reply_id'])
        second=self.store.claim_send()
        self.store.finish_send(self.mid,'failed','gateway_error',second['reply_id'])
        self.store.retry_reply(receipt['reply_id'])
        self.assertEqual(self.store.claim_send()['client_id'],second['client_id'])
        self.store.finish_send(self.mid,'uncertain',reply_id=second['reply_id'])
        with self.assertRaisesRegex(BridgeError,'delivery_uncertain_review_required'):
            self.store.retry_reply(receipt['reply_id'])

    def test_nested_quote_id_and_item_alias_restore_image_reference(self):
        self.store.ingest('bot',[dict(source_id='message:20',aliases=['20','image-item:fixture'],peer='owner',text='[图片]',created_at=2,context_token='fixture',attachments=[dict(kind='image',name='picture.jpg',url='fixture')])],'20')
        aid=self.store.db.execute('SELECT id FROM attachments').fetchone()[0]
        from test_media import picture
        self.store.finish_attachment(aid,picture())
        self.restart()
        wire=dict(message_id=21,from_user_id='owner',message_type=1,message_state=2,item_list=[dict(type=1,text_item=dict(text='question'),ref_msg=dict(message_item=dict(type=2,msg_id=' image-item:fixture ')))])
        msg=_normalize_message(wire,self.binding,3)
        self.assertEqual(msg['quotes'][0]['source_id'],'image-item:fixture')
        self.assertEqual(msg['attachments'],[])
        self.store.ingest('bot',[msg],'21')
        mid=self.store.db.execute("SELECT id FROM messages WHERE source_id='message:21'").fetchone()[0]
        result=self.store.read_message(mid)
        self.assertEqual(result['quotes'][0]['status'],'cached_inbound')
        self.assertEqual(result['attachments'][0]['attachment_id'],aid)
        self.assertNotIn('引用内容暂不可用',result['text'])

    def test_quote_diagnostics_distinguish_missing_id_from_missing_cache(self):
        for n,ref,status in [(30,dict(title=''),'reference_not_provided'),(31,dict(svr_id='unknown'),'not_cached')]:
            ref['fixture_nonsecret_field']=True
            wire=dict(message_id=n,from_user_id='owner',message_type=1,message_state=2,item_list=[dict(type=1,text_item=dict(text='question'),ref_msg=ref)])
            self.store.ingest('bot',[_normalize_message(wire,self.binding,3)],str(n))
            mid=self.store.db.execute('SELECT id FROM messages WHERE source_id=?',('message:'+str(n),)).fetchone()[0]
            result=self.store.read_message(mid)
            self.assertEqual(result['quotes'][0]['status'],status)
            self.assertEqual(result['quotes'][0]['has_reference_id'],n==31)

    def test_normalizer_retains_top_and_item_ids_for_later_quotes(self):
        wire=dict(message_id=50,from_user_id='owner',message_type=1,message_state=2,item_list=[dict(type=1,msg_id='item-50',text_item=dict(text='fixture'))])
        result=_normalize_message(wire,self.binding,3)
        self.assertEqual(result['aliases'],['50','item-50'])

    def test_unknown_items_preserve_text_and_explain_unsupported_content(self):
        wire=dict(message_id=80,from_user_id='owner',message_type=1,message_state=2,
                  item_list=[dict(type=99,secret_payload='never expose'),dict(type=1,text_item=dict(text='question'))])
        result=_normalize_message(wire,self.binding,3)
        self.assertIn('question',result['text'])
        self.assertIn('暂不支持',result['text'])
        self.assertNotIn('secret_payload',str(result))
        self.assertNotIn('never expose',str(result))
        wire['item_list']=wire['item_list'][:1]
        self.assertIn('暂不支持',_normalize_message(wire,self.binding,3)['text'])

    def test_inline_media_and_cached_quote_do_not_download_twice(self):
        key=base64.b64encode(b'x'*16).decode()
        image=dict(type=2,image_item=dict(media=dict(encrypt_query_param='fixture',aes_key=key)))
        wire=dict(message_id=81,from_user_id='owner',message_type=1,message_state=2,item_list=[image,image])
        self.store.ingest('bot',[_normalize_message(wire,self.binding,3)],'a')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM attachments').fetchone()[0],1)
        quoted=dict(type=1,text_item=dict(text='question'),ref_msg=dict(svr_id='81',message_item=image))
        wire.update(message_id=82,item_list=[quoted])
        self.store.ingest('bot',[_normalize_message(wire,self.binding,4)],'b')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM attachments').fetchone()[0],1)
        mid=self.store.db.execute("SELECT id FROM messages WHERE source_id='message:82'").fetchone()[0]
        self.assertEqual(len(self.store.attachment_metadata(mid)),1)

    def test_outbound_quotes_are_scoped_and_keep_same_id_in_other_scopes(self):
        self.store.remember_sent_quote('500',dict(text='first scope'))
        self.store.db.execute("INSERT INTO sent_quote_cache VALUES('other','owner','500','other scope',NULL)")
        self.restart()
        self.store.ingest('bot',[dict(source_id='message:90',peer='owner',text='question',created_at=4,quotes=[dict(source_id='500')])],'a')
        mid=self.store.db.execute("SELECT id FROM messages WHERE source_id='message:90'").fetchone()[0]
        text=self.store.read_message(mid)['text']
        self.assertIn('first scope',text)
        self.assertNotIn('other scope',text)

    def test_legacy_quote_migration_preserves_unambiguous_cache(self):
        self.store.db.execute("INSERT INTO sent_quotes VALUES('600','legacy reply',NULL)")
        self.store.db.execute("PRAGMA user_version=0")
        self.store.db.execute("DROP TABLE sent_quote_cache")
        self.restart()
        self.assertEqual(self.store.db.execute("SELECT text FROM sent_quote_cache WHERE source_id='600'").fetchone()[0],'legacy reply')
        self.store.db.execute("DELETE FROM sent_quote_cache")
        self.restart()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM sent_quote_cache").fetchone()[0],0)

    def test_image_bad_hex_key_falls_back_to_valid_media_key(self):
        from wechat_dot_bridge.media import descriptor
        key=b'x'*16
        item=dict(type=2,image_item=dict(aeskey='bad',media=dict(encrypt_query_param='fixture',aes_key=base64.b64encode(key).decode())))
        self.assertEqual(descriptor(item)['key'],key.hex())
        item['image_item']['media']['aes_key']='bad'
        with self.assertRaises(BridgeError):descriptor(item)

    def test_expired_quote_cache_uses_fresh_inline_media(self):
        image=dict(type=2,image_item=dict(media=dict(encrypt_query_param='fixture',aes_key=base64.b64encode(b'x'*16).decode())))
        wire=dict(message_id=101,from_user_id='owner',message_type=1,message_state=2,item_list=[image]*9)
        normalized=_normalize_message(wire,self.binding,3)
        self.assertEqual(len(normalized['attachments']),1)
        self.assertNotIn('error',normalized['attachments'][0])
        self.store.ingest('bot',[normalized],'a')
        self.store.db.execute("UPDATE attachments SET status='expired'")
        wire.update(message_id=102,item_list=[dict(type=1,text_item=dict(text='question'),ref_msg=dict(svr_id='101',message_item=image))])
        self.store.ingest('bot',[_normalize_message(wire,self.binding,4)],'b')
        mid=self.store.db.execute("SELECT id FROM messages WHERE source_id='message:102'").fetchone()[0]
        meta=self.store.attachment_metadata(mid)
        self.assertEqual(len(meta),1)
        self.assertEqual(meta[0]['status'],'deferred')


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000000
        self.store = Store(self.tmp.name, clock=lambda: self.now)
        self.store.import_binding(dict(id='bot',owner='owner',token='fixture',base_url='https://ilinkai.weixin.qq.com'))
        self.store.set_enabled(True)
        self.store.ingest('bot',[dict(source_id='message:1',peer='owner',text='fixture',created_at=self.now,context_token='fixture')],'1')
        self.mid = self.store.list_pending()[0]['message_id']

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_retention_preserves_queued_and_uncertain_but_releases_completed(self):
        aid = self.store.stage_attachment(base64.b64encode(b'fixture').decode(),'file','test.txt')['attachment_id']
        self.store.queue_reply(self.mid,'caption',attachment_id=aid)
        self.now += 8*86400
        self.assertEqual(self.store.cleanup_media(),0)
        reply = self.store.claim_send()
        self.store.finish_send(self.mid,'accepted',reply_id=reply['reply_id'])
        reply = self.store.claim_send()
        self.store.finish_send(self.mid,'uncertain',reply_id=reply['reply_id'])
        self.assertEqual(self.store.cleanup_media(),0)
        # Explicit rejection is safe to retry, unlike uncertain delivery.
        with self.assertRaisesRegex(BridgeError,'delivery_uncertain'):
            self.store.retry_reply(reply['reply_id'])
        self.store.db.execute("UPDATE replies SET status='failed' WHERE client_id=?",(reply['reply_id'],))
        self.assertEqual(self.store.cleanup_media(),1)
        self.store.stage_attachment(base64.b64encode(b'fixture').decode(),'file','test.txt')
        self.assertEqual(self.store.read_attachment(aid)['data'],b'fixture')

    def test_progress_survives_interim_reply_and_final_stops_typing(self):
        self.store.queue_reply(self.mid,'working',final=False)
        self.assertIsNotNone(self.store.typing_target([self.mid]))
        self.store.queue_reply(self.mid,'complete')
        self.assertIsNone(self.store.typing_target([self.mid]))
        with self.assertRaisesRegex(BridgeError, 'processing_already_completed'):
            self.store.report_progress(self.mid)
        self.assertEqual(self.store.work_status(self.mid)['state'], 'completed')
        self.assertIsNone(self.store.typing_target([self.mid]))
        self.now += 301
        self.assertIsNone(self.store.typing_target([self.mid]))

    def test_server_longpoll_timeout_and_cancel(self):
        client = IlinkClient()
        client._post = Mock(return_value={'longpolling_timeout_ms':35000})
        client.poll(self.store.get_binding())
        client.poll(self.store.get_binding())
        self.assertEqual(client._post.call_args.args[-1],40)
        connection=Mock();client.connections.add(connection);client.cancel()
        connection.cancel.assert_called_once()

    def test_tool_diagnostics_omit_message_and_credentials(self):
        self.store.record_tool_call('read_weixin_message',self.mid,'ok')
        result=self.store.get_status()
        self.assertTrue(result['pending_processing'][0]['read_observed'])
        self.assertNotIn('fixture',str(result))

    @unittest.skipUnless(audio.available(),'optional local codecs not installed')
    def test_real_wav_silk_wav_conversion(self):
        out=io.BytesIO()
        with wave.open(out,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000)
            wav.writeframes(b'\0\0'*24000)
        silk=audio.to_silk(out.getvalue())
        self.assertTrue(audio.is_silk(silk))
        decoded=audio.to_wav(silk)
        with wave.open(io.BytesIO(decoded)) as wav:
            self.assertEqual(wav.getframerate(),24000)
            self.assertGreater(wav.getnframes(),23000)
        with self.assertRaisesRegex(BridgeError,'media_audio_conversion_failed'):
            audio.to_silk(b'not audio')

    @unittest.skipUnless(__import__("shutil").which("node"),"optional Node not installed")
    def test_upstream_markdown_keeps_supported_constructs(self):
        from wechat_dot_bridge.message_format import markdown_to_text
        self.assertEqual(markdown_to_text('##### 标题\n*中文* **bold**\n```\n*原样*\n```'), '标题\n中文 **bold**\n```\n*原样*\n```')

    def test_text_file_staging_and_diagnostic(self):
        from wechat_dot_bridge.mcp import MCPServer
        from wechat_dot_bridge.events import Events
        server=MCPServer(self.store,Events(self.store))
        response=server._call({'name':'stage_weixin_attachment','arguments':{'text_content':'中文文件','kind':'file','name':'example.txt'}})
        aid=response['structuredContent']['attachment_id']
        self.assertEqual(self.store.read_attachment(aid)['data'],'中文文件'.encode())
        self.assertNotIn('中文文件',str(self.store.get_status()))

    def test_stale_session_cooldown_survives_restart_and_manual_disable(self):
        self.store.pause_session()
        self.store.resume_session()
        self.assertFalse(self.store.get_binding()['enabled'])
        self.now += 3601
        self.store.resume_session()
        self.assertTrue(self.store.get_binding()['enabled'])
        self.store.pause_session()
        self.store.set_enabled(False)
        self.now += 3601
        self.store.resume_session()
        self.assertFalse(self.store.get_binding()['enabled'])


if __name__ == "__main__":
    unittest.main()
