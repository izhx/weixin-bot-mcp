"""Atomic schema/event migrations and offline upgrade recovery."""

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from wechat_dot_bridge import migrations
from wechat_dot_bridge.errors import BridgeError
from wechat_dot_bridge.events import Events
from wechat_dot_bridge.operations import backup, restore
from wechat_dot_bridge.store import Store


LEGACY_SCHEMA = """
CREATE TABLE binding (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, token TEXT NOT NULL,
    base_url TEXT NOT NULL, cursor TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'ready',
    last_error TEXT);
CREATE TABLE messages (
    id TEXT PRIMARY KEY, account TEXT NOT NULL, peer TEXT NOT NULL,
    source_id TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL,
    context_token TEXT, state TEXT NOT NULL DEFAULT 'pending',
    UNIQUE(account,peer,source_id));
CREATE TABLE replies (
    message_id TEXT PRIMARY KEY REFERENCES messages(id), text TEXT NOT NULL,
    client_id TEXT UNIQUE NOT NULL, status TEXT NOT NULL, error TEXT,
    created_at REAL NOT NULL, context_id TEXT, attempts INTEGER NOT NULL DEFAULT 0);
"""


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "bridge.sqlite3"
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        os.chmod(self.path, 0o600)

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def legacy(self):
        self.db.executescript(LEGACY_SCHEMA)
        self.db.execute("INSERT INTO binding(id,owner,token,base_url,enabled) VALUES('fixture-bot','fixture-owner','not-a-token','https://ilinkai.weixin.qq.com',1)")
        for index, status in enumerate(("accepted", "uncertain", "sending", "queued")):
            mid = "fixture-message-" + str(index)
            rid = "fixture-reply-" + str(index)
            self.db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",
                            (mid, "fixture-bot", "fixture-owner", "message:"+str(index),
                             "synthetic inbound", 100, "synthetic-context", "replied"))
            self.db.execute("INSERT INTO replies VALUES(?,?,?,?,?,?,?,?)",
                            (mid, "synthetic reply", rid, status,
                             "network_timeout" if status == "uncertain" else None,
                             101, mid, 2))

    def snapshot(self):
        schema = [tuple(row) for row in self.db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name")]
        tables = [row[0] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        data = {name: [tuple(row) for row in self.db.execute('SELECT * FROM "'+name+'" ORDER BY rowid')]
                for name in tables}
        return schema, data, self.db.execute("PRAGMA user_version").fetchone()[0]

    def test_empty_database_and_restart_do_not_repeat_backfills(self):
        self.assertEqual(migrations.migrate(self.db, clock=lambda: 200), migrations.SCHEMA_VERSION)
        first = self.snapshot()
        clock = Mock(side_effect=AssertionError("completed migrations must not run again"))
        self.assertEqual(migrations.migrate(self.db, clock=clock), migrations.SCHEMA_VERSION)
        self.assertEqual(self.snapshot(), first)
        clock.assert_not_called()

    def test_legacy_reply_identity_keeps_outcomes_ids_and_accepted_parts(self):
        self.legacy()
        before = [tuple(row) for row in self.db.execute("SELECT * FROM replies ORDER BY client_id")]
        self.db.execute("CREATE TABLE reply_parts(reply_id TEXT NOT NULL,part INTEGER NOT NULL,text TEXT NOT NULL,accepted INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(reply_id,part))")
        self.db.executemany("INSERT INTO reply_parts VALUES('fixture-reply-1',?,?,?)",
                            [(0, "accepted segment", 1), (1, "uncertain segment", 0), (2, "future segment", 0)])
        migrations.migrate(self.db, clock=lambda: 200)
        after = [tuple(row) for row in self.db.execute(
            "SELECT message_id,text,client_id,status,error,created_at,context_id,attempts FROM replies ORDER BY client_id")]
        self.assertEqual(after, before)
        self.assertEqual([tuple(row) for row in self.db.execute(
            "SELECT part,text,accepted,kind FROM reply_parts ORDER BY part")],
            [(0, "accepted segment", 1, "text"), (1, "uncertain segment", 0, "text"), (2, "future segment", 0, "text")])
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.db.close()
        store = Store(self.temporary.name, clock=lambda: 200)
        try:
            store.recover_inflight()
            self.assertEqual(store.send_status("fixture-message-1", "fixture-reply-1")["status"], "uncertain")
            self.assertEqual(store.send_status("fixture-message-2", "fixture-reply-2")["status"], "uncertain")
            self.assertEqual(store.claim_send()["reply_id"], "fixture-reply-3")
            self.assertIsNone(store.claim_send())
        finally:
            store.close()

    def partial_unversioned(self):
        self.legacy()
        # Simulate the old nontransactional startup stopping after ADD COLUMN or
        # CREATE TABLE. Their existence must not suppress the missing backfill.
        self.db.execute("ALTER TABLE replies ADD COLUMN attachment_id TEXT")
        self.db.execute("ALTER TABLE replies ADD COLUMN is_notice INTEGER NOT NULL DEFAULT 0")
        self.db.execute("UPDATE replies SET attachment_id='fixture-attachment',is_notice=1 WHERE client_id='fixture-reply-1'")
        self.db.execute("""CREATE TABLE attachments (
            id TEXT PRIMARY KEY,account TEXT NOT NULL,peer TEXT NOT NULL,
            message_id TEXT REFERENCES messages(id),kind TEXT NOT NULL,name TEXT NOT NULL,
            descriptor TEXT NOT NULL,data BLOB,status TEXT NOT NULL,error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL DEFAULT 0)""")
        self.db.execute("INSERT INTO attachments(id,account,peer,kind,name,descriptor,data,status) VALUES('fixture-attachment','fixture-bot','fixture-owner','file','fixture.txt','{}',X'6162','ready')")
        self.db.execute("CREATE TABLE sent_quotes(source_id TEXT PRIMARY KEY,text TEXT NOT NULL,attachment_id TEXT)")
        self.db.execute("INSERT INTO sent_quotes VALUES('fixture-source','synthetic cached text',NULL)")
        self.db.execute("CREATE TABLE sent_quote_cache(account TEXT NOT NULL,peer TEXT NOT NULL,source_id TEXT NOT NULL,text TEXT NOT NULL,attachment_id TEXT,PRIMARY KEY(account,peer,source_id))")
        self.db.execute("CREATE TABLE reply_parts(reply_id TEXT NOT NULL,part INTEGER NOT NULL,text TEXT NOT NULL,accepted INTEGER NOT NULL DEFAULT 0,kind TEXT NOT NULL DEFAULT 'text',PRIMARY KEY(reply_id,part))")
        self.db.executemany("INSERT INTO reply_parts VALUES('fixture-reply-1',?,?,?,'text')",
                            [(0, "accepted segment", 1), (1, "", 0)])

    def test_adopts_old_interrupted_schema_without_resetting_uncertain(self):
        self.partial_unversioned()
        migrations.migrate(self.db, clock=lambda: 200)
        reply = self.db.execute("SELECT status,error,attachment_id,is_notice,attempts FROM replies WHERE client_id='fixture-reply-1'").fetchone()
        self.assertEqual(tuple(reply), ("uncertain", "network_timeout", "fixture-attachment", 1, 2))
        self.assertEqual(self.db.execute("SELECT created_at FROM attachments").fetchone()[0], 200)
        self.assertEqual([tuple(row) for row in self.db.execute("SELECT part,accepted,kind FROM reply_parts ORDER BY part")],
                         [(0, 1, "text"), (1, 0, "media")])
        self.assertEqual(self.db.execute("SELECT text FROM sent_quote_cache").fetchone()[0], "synthetic cached text")
        # The legacy cache must not reappear if intentionally removed afterwards.
        self.db.execute("DELETE FROM sent_quote_cache")
        before = self.snapshot()
        migrations.migrate(self.db, clock=lambda: 999)
        self.assertEqual(self.snapshot(), before)

    def test_ambiguous_legacy_quote_cache_is_never_assigned_to_current_owner(self):
        self.partial_unversioned()
        self.db.execute("INSERT INTO messages VALUES('other','other-bot','other-peer','other-source','synthetic',1,NULL,'replied')")
        migrations.migrate(self.db, clock=lambda: 200)
        self.assertEqual(self.db.execute("SELECT count(*) FROM sent_quote_cache").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM sent_quotes").fetchone()[0], 1)

    def test_each_step_failure_rolls_back_schema_backfills_and_version(self):
        self.legacy()
        before = self.snapshot()
        for position in range(len(migrations.MIGRATIONS)):
            with self.subTest(version=position + 1):
                steps = list(migrations.MIGRATIONS)
                original = steps[position]
                def fail_after_upgrade(db, clock, upgrade=original):
                    upgrade(db, clock)
                    raise sqlite3.OperationalError("synthetic interruption")
                steps[position] = fail_after_upgrade
                with patch.object(migrations, "MIGRATIONS", tuple(steps)):
                    with self.assertRaises(sqlite3.OperationalError):
                        migrations.migrate(self.db, clock=lambda: 200)
                self.assertFalse(self.db.in_transaction)
                self.assertEqual(self.snapshot(), before)
        migrations.migrate(self.db, clock=lambda: 200)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], migrations.SCHEMA_VERSION)

    def test_keyboard_interrupt_rolls_back_and_allows_retry(self):
        self.legacy()
        before = self.snapshot()
        original = migrations.MIGRATIONS[3]
        def interrupt(db, clock):
            original(db, clock)
            raise KeyboardInterrupt
        steps = (*migrations.MIGRATIONS[:3], interrupt, *migrations.MIGRATIONS[4:])
        with patch.object(migrations, "MIGRATIONS", steps):
            with self.assertRaises(KeyboardInterrupt):
                migrations.migrate(self.db, clock=lambda: 200)
        self.assertEqual(self.snapshot(), before)
        migrations.migrate(self.db, clock=lambda: 200)

    def test_interrupted_incremental_upgrade_keeps_committed_old_version(self):
        self.partial_unversioned()
        for upgrade in migrations.MIGRATIONS[:3]:
            upgrade(self.db, lambda: 200)
        self.db.execute("PRAGMA user_version=3")
        before = self.snapshot()
        original = migrations.MIGRATIONS[3]
        def fail_after_backfill(db, clock):
            original(db, clock)
            raise sqlite3.OperationalError("synthetic interruption")
        steps = (*migrations.MIGRATIONS[:3], fail_after_backfill, *migrations.MIGRATIONS[4:])
        with patch.object(migrations, "MIGRATIONS", steps):
            with self.assertRaises(sqlite3.OperationalError):
                migrations.migrate(self.db, clock=lambda: 300)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 3)
        migrations.migrate(self.db, clock=lambda: 300)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], migrations.SCHEMA_VERSION)
        self.assertEqual(self.db.execute("SELECT created_at FROM attachments").fetchone()[0], 200)
        self.assertEqual(self.db.execute("SELECT count(*) FROM sent_quote_cache").fetchone()[0], 1)

    def test_process_exit_after_quote_backfill_recovers_entire_upgrade(self):
        self.partial_unversioned()
        before = self.snapshot()
        self.db.execute("PRAGMA journal_mode=WAL")
        program = """
import os, sqlite3, sys
from wechat_dot_bridge.migrations import migrate
class InterruptedConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        result = super().execute(sql, parameters)
        if sql.lstrip().startswith('INSERT OR IGNORE INTO sent_quote_cache'):
            os._exit(37)
        return result
db = sqlite3.connect(sys.argv[1], isolation_level=None, factory=InterruptedConnection)
db.execute('PRAGMA foreign_keys=ON')
migrate(db, clock=lambda: 200)
"""
        result = subprocess.run([sys.executable, "-c", program, str(self.path)],
                                capture_output=True, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 37)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")
        self.assertEqual(self.snapshot(), before)
        migrations.migrate(self.db, clock=lambda: 200)
        self.assertEqual(self.db.execute("SELECT count(*) FROM sent_quote_cache").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT status FROM replies WHERE client_id='fixture-reply-1'").fetchone()[0], "uncertain")

    def test_version_nine_upgrade_adds_dedup_digests_without_changing_payloads(self):
        import hashlib
        self.legacy()
        with patch.object(migrations, 'MIGRATIONS', migrations.MIGRATIONS[:9]):
            migrations.migrate(self.db, clock=lambda: 200)
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], 9)
        before = [tuple(row) for row in self.db.execute('SELECT client_id,text,status,attempts FROM replies ORDER BY client_id')]
        migrations.migrate(self.db, clock=lambda: 300)
        after = [tuple(row) for row in self.db.execute('SELECT client_id,text,status,attempts FROM replies ORDER BY client_id')]
        self.assertEqual(before, after)
        digest = hashlib.sha256(b'synthetic reply').hexdigest()
        self.assertTrue(all(row[0] == digest and row[1] is None for row in self.db.execute('SELECT text_digest,text_cleared_at FROM replies')))
        self.assertTrue(all(row[0] is None for row in self.db.execute('SELECT text_cleared_at FROM messages')))

    def test_future_schema_rejected_without_changes(self):
        migrations.migrate(self.db)
        self.db.execute("PRAGMA user_version=" + str(migrations.SCHEMA_VERSION + 1))
        before = self.snapshot()
        with self.assertRaises(BridgeError) as caught:
            migrations.migrate(self.db)
        self.assertEqual(caught.exception.code, "database_schema_unsupported")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.db.in_transaction)

    def test_invalid_binding_rolls_back_upgrade(self):
        self.legacy()
        self.db.execute("INSERT INTO binding(id,owner,token,base_url) VALUES('other-bot','other-owner','fixture','https://ilinkai.weixin.qq.com')")
        before = self.snapshot()
        with self.assertRaises(BridgeError) as caught:
            migrations.migrate(self.db)
        self.assertEqual(caught.exception.code, "invalid_binding_state")
        self.assertEqual(self.snapshot(), before)

    def test_caller_transaction_is_preserved_when_refused(self):
        self.db.execute("BEGIN")
        with self.assertRaises(BridgeError) as caught:
            migrations.migrate(self.db)
        self.assertEqual(caught.exception.code, "database_migration_in_transaction")
        self.assertTrue(self.db.in_transaction)
        self.db.execute("ROLLBACK")


LEGACY_EVENT_SCHEMA = {
    "subscriptions": """CREATE TABLE subscriptions (
        id TEXT PRIMARY KEY, principal TEXT NOT NULL, name TEXT NOT NULL,
        arguments TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL,
        old_secret TEXT, rotate_until REAL, expires_at REAL NOT NULL,
        verified_until REAL NOT NULL, status TEXT NOT NULL, start_rowid INTEGER NOT NULL)""",
    "deliveries": """CREATE TABLE deliveries (
        event_id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL,
        message_id TEXT NOT NULL, body BLOB NOT NULL, status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL,
        error TEXT, UNIQUE(subscription_id,message_id))""",
    "event_metadata": "CREATE TABLE event_metadata (key TEXT PRIMARY KEY,value TEXT)",
}


class EventMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def legacy(self, name="state", event_tables=3):
        state = self.root / name
        state.mkdir(mode=0o700)
        path = state / "bridge.sqlite3"
        path.touch(mode=0o600)
        with closing(sqlite3.connect(path, isolation_level=None)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            for upgrade in migrations.MIGRATIONS[:7]:
                upgrade(db, lambda: 100)
            db.execute("PRAGMA user_version=7")
            db.execute("COMMIT")
            for table in tuple(LEGACY_EVENT_SCHEMA)[:event_tables]:
                db.execute(LEGACY_EVENT_SCHEMA[table])
            db.execute("""INSERT INTO binding(id,owner,token,base_url,enabled)
                VALUES('fixture-account','fixture-owner','fixture-token',
                       'https://ilinkai.weixin.qq.com',1)""")
            for index, outcome in enumerate(("acknowledged", "retry", "sending")):
                mid = "fixture-message-" + str(index)
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",
                           (mid, "fixture-account", "fixture-owner", str(index),
                            "synthetic inbound", 101, "synthetic-context", "pending"))
                db.execute("""INSERT INTO message_work(message_id,run_id,state,updated_at)
                    VALUES(?,?,'waiting',101)""", (mid, "fixture-work-" + str(index)))
                if event_tables >= 2:
                    body = json.dumps({"id": "fixture-event-" + str(index),
                                       "data": {"message_id": mid}},
                                      separators=(",", ":")).encode()
                    db.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,?,?,?)",
                               ("fixture-event-" + str(index), "fixture-subscription",
                                mid, body, outcome, 2, 105,
                                "http_error" if outcome == "retry" else None))
            db.execute("""INSERT INTO replies(message_id,text,client_id,status,error,
                created_at,attempts) VALUES('fixture-message-2','synthetic reply',
                'fixture-reply','uncertain','network_timeout',102,1)""")
            db.execute("INSERT INTO reply_parts VALUES('fixture-reply',0,'accepted segment',1,'text')")
            db.execute("INSERT INTO reply_parts VALUES('fixture-reply',1,'uncertain segment',0,'text')")
            if event_tables >= 1:
                principal = json.dumps(["fixture-account", "fixture-owner"], separators=(",", ":"))
                db.execute("INSERT INTO subscriptions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                           ("fixture-subscription", principal, "weixin.message", "{}",
                            "https://callback.example.com/synthetic", "synthetic-secret",
                            "synthetic-old-secret", 150, 0, 130, "active", 0))
            if event_tables == 3:
                db.execute("INSERT INTO event_metadata VALUES('nonexpiring_default:fixture-subscription','1')")
        return state

    def event_data(self, db):
        return {table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                for table in LEGACY_EVENT_SCHEMA}

    def test_store_adopts_every_legacy_initialization_prefix_before_events(self):
        for count in range(4):
            with self.subTest(tables=count):
                state = self.legacy("state-" + str(count), count)
                with closing(sqlite3.connect(state / "bridge.sqlite3")) as db:
                    previous = {table: [tuple(row) for row in db.execute("SELECT * FROM " + table)]
                                for table in tuple(LEGACY_EVENT_SCHEMA)[:count]}
                with closing(Store(state)) as store:
                    self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], migrations.SCHEMA_VERSION)
                    data = self.event_data(store.db)
                    for table, rows in previous.items():
                        self.assertEqual(data[table], rows)
                    self.assertEqual(store.send_status("fixture-message-2")["status"], "uncertain")
                    self.assertEqual(store.send_status("fixture-message-2")["parts_accepted"], 1)
                    self.assertIsNone(store.claim_send())
                    self.assertFalse(store.db.execute("PRAGMA foreign_key_check").fetchall())

    def test_event_upgrade_failure_rolls_back_version_and_new_tables(self):
        state = self.legacy(event_tables=1)
        original = migrations.MIGRATIONS[7]

        def interrupted(db, clock):
            original(db, clock)
            raise sqlite3.OperationalError("synthetic migration interruption")

        with patch.object(migrations, "MIGRATIONS", (*migrations.MIGRATIONS[:7], interrupted)):
            with self.assertRaisesRegex(BridgeError, "database_unavailable"):
                Store(state)
        with closing(sqlite3.connect(state / "bridge.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='deliveries'").fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='event_metadata'").fetchone())
            self.assertEqual(db.execute("SELECT secret FROM subscriptions").fetchone()[0], "synthetic-secret")
        with closing(Store(state)) as store:
            self.assertEqual(set(self.event_data(store.db)), set(LEGACY_EVENT_SCHEMA))

    def test_read_only_events_does_not_commit_or_recover_callers_transaction(self):
        with closing(Store(self.legacy())) as store:
            original = self.event_data(store.db)
            with self.assertRaisesRegex(RuntimeError, "synthetic caller rollback"):
                with store._transaction():
                    store.db.execute("INSERT INTO event_metadata VALUES('uncommitted','1')")
                    Events(store, recover_inflight=False)
                    self.assertTrue(store.db.in_transaction)
                    self.assertEqual(self.event_data(store.db)["deliveries"], original["deliveries"])
                    raise RuntimeError("synthetic caller rollback")
            self.assertEqual(self.event_data(store.db), original)

    def test_recovering_events_refuses_nested_transaction_without_committing_it(self):
        with closing(Store(self.legacy())) as store:
            original = self.event_data(store.db)
            with store._transaction():
                with self.assertRaisesRegex(BridgeError, "database_unavailable"):
                    Events(store)
                self.assertTrue(store.db.in_transaction)
                self.assertEqual(self.event_data(store.db), original)

    def test_migration_and_backup_do_not_recover_claims_until_runtime_requests_it(self):
        state = self.legacy()
        with closing(sqlite3.connect(state / "bridge.sqlite3")) as db:
            original = self.event_data(db)
        with closing(Store(state)) as store:
            self.assertEqual(self.event_data(store.db), original)
            snapshot = self.root / "backup.sqlite3"
            backup(store, snapshot)
        recovered = self.root / "recovered"
        recovered.mkdir(mode=0o700)
        restore(snapshot, recovered)
        with closing(Store(recovered, clock=lambda: 200)) as store:
            self.assertFalse(store.get_binding()["enabled"])
            self.assertEqual(self.event_data(store.db), original)
            Events(store, recover_inflight=False)
            self.assertEqual(self.event_data(store.db), original)
            Events(store, clock=lambda: 200)
            current = self.event_data(store.db)
            self.assertEqual(current["subscriptions"], original["subscriptions"])
            self.assertEqual(current["event_metadata"], original["event_metadata"])
            self.assertEqual(current["deliveries"][:2], original["deliveries"][:2])
            self.assertEqual(current["deliveries"][2][:4], original["deliveries"][2][:4])
            self.assertEqual(current["deliveries"][2][4:], ("retry", 1, 200, "callback_interrupted"))
            Events(store, clock=lambda: 300)
            self.assertEqual(self.event_data(store.db), current)
            store.set_enabled(True)
            self.assertEqual(store.send_status("fixture-message-2")["status"], "uncertain")
            self.assertEqual(store.send_status("fixture-message-2")["parts_accepted"], 1)
            self.assertIsNone(store.claim_send())

    def test_recovery_failure_leaves_claims_and_connection_usable(self):
        with closing(Store(self.legacy())) as store:
            original = self.event_data(store.db)
            store.db.execute("""CREATE TRIGGER interrupt_recovery BEFORE UPDATE ON deliveries
                BEGIN SELECT RAISE(ABORT,'synthetic recovery failure'); END""")
            with self.assertRaisesRegex(BridgeError, "database_unavailable"):
                Events(store)
            self.assertFalse(store.db.in_transaction)
            self.assertEqual(self.event_data(store.db), original)
            store.db.execute("DROP TRIGGER interrupt_recovery")
            Events(store)
            self.assertEqual(store.db.execute("SELECT status FROM deliveries WHERE status='sending'").fetchall(), [])


ROOT = Path(__file__).resolve().parents[1]


class UpgradeScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        shutil.copytree(ROOT / "wechat_dot_bridge", self.root / "wechat_dot_bridge",
                        ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
        (self.root / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts" / "verify-upgrade.py", self.root / "scripts")
        self.env = dict(os.environ)
        self.env.pop("PYTHONPATH", None)
        self.env["PYTHONNOUSERSITE"] = "1"

    def tearDown(self):
        self.temporary.cleanup()

    def run_script(self, *arguments):
        return subprocess.run([sys.executable, "scripts/verify-upgrade.py", *arguments],
                              cwd=self.root, env=self.env, capture_output=True, text=True,
                              timeout=30)

    def test_upgrade_and_uncertain_recovery_without_git_history(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("uncertain-send preservation, backup/restore and part recovery: passed", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_explicit_unavailable_revision_does_not_fall_back_or_echo_input(self):
        revision = "synthetic-private-revision"
        result = self.run_script("--old-revision", revision)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout),
                         {"passed": False, "error": "old_revision_unavailable"})
        self.assertNotIn(revision, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
