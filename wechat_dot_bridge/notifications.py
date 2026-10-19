"""Owner-only proactive notifications using the existing serial send worker.

Notifications never become fake inbound messages or trigger MCP Events. Their
immutable IDs, text parts and send outcomes survive restarts. A missing/rejected
Weixin context waits for a fresh owner message; an ambiguous POST is never
automatically repeated.
"""

import hashlib
import re

from .errors import BridgeError
from .message_format import split_text


class NotificationStore:
    def _notification(self, notification_id, binding):
        if not isinstance(notification_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", notification_id):
            raise BridgeError("invalid_notification_id")
        return self.db.execute(
            "SELECT * FROM notifications WHERE id=? AND account=? AND peer=?",
            (notification_id, binding["id"], binding["owner"])).fetchone()

    def queue_notification(self, notification_id, text):
        from .store import MAX_REPLY_BYTES, MAX_QUEUED_REPLIES, _text_bytes

        size = _text_bytes(text)
        if size is None or not text.strip() or size > MAX_REPLY_BYTES:
            raise BridgeError("invalid_reply_text")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._transaction():
            binding = self._binding()
            existing = self._notification(notification_id, binding)
            if existing:
                if existing["text_digest"] != digest:
                    raise BridgeError("notification_id_conflict")
            else:
                if not binding["enabled"] or binding["status"] != "ready":
                    raise BridgeError("account_disabled")
                replies = self.db.execute("SELECT count(*) FROM replies WHERE status IN ('queued','sending','context_required','auth_required')").fetchone()[0]
                if replies + self._pending_notifications() >= MAX_QUEUED_REPLIES:
                    raise BridgeError("send_queue_full")
                identity = "\0".join((binding["id"], binding["owner"], notification_id))
                client_id = "dot_notice_" + hashlib.sha256(identity.encode()).hexdigest()
                self.db.execute(
                    "INSERT INTO notifications(id,account,peer,text,client_id,status,created_at,text_digest) VALUES(?,?,?,?,?,'queued',?,?)",
                    (notification_id, binding["id"], binding["owner"], text, client_id, self.clock(), digest))
                for index, chunk in enumerate(split_text(text)):
                    self.db.execute("INSERT INTO notification_parts(notification_id,part,text) VALUES(?,?,?)",
                                    (notification_id, index, chunk))
        return self.notification_status(notification_id)

    def _pending_notifications(self):
        return self.db.execute("SELECT count(*) FROM notifications WHERE status IN ('queued','sending','context_required','auth_required')").fetchone()[0]

    def notification_status(self, notification_id):
        with self.lock:
            row = self._notification(notification_id, self._binding())
            if not row:
                raise BridgeError("unknown_notification")
            parts = self.db.execute("SELECT count(*),COALESCE(sum(accepted),0) FROM notification_parts WHERE notification_id=?", (notification_id,)).fetchone()
            result = {"notification_id": notification_id, "status": row["status"],
                      "delivered": None, "parts_total": parts[0], "parts_accepted": parts[1]}
            if row["error"]:
                from .store import _safe_error
                result["error"] = _safe_error(row["error"])
            return result

    def _claim_notification(self, binding):
        context = self._latest_context(binding)
        if context:
            self.db.execute("UPDATE notifications SET status='queued',error=NULL WHERE status='context_required' AND (context_id IS NULL OR context_id!=?)", (context["id"],))
        row = self.db.execute("SELECT * FROM notifications WHERE status='queued' AND account=? AND peer=? ORDER BY created_at,rowid LIMIT 1", (binding["id"], binding["owner"])).fetchone()
        if row is None:
            return None
        if row["attempts"] >= 3:
            self.db.execute("UPDATE notifications SET status='failed',error='retry_limit' WHERE id=?", (row["id"],))
            return None
        if context is None:
            self.db.execute("UPDATE notifications SET status='context_required',error='fresh_context_required' WHERE id=?", (row["id"],))
            return None
        part = self.db.execute("SELECT * FROM notification_parts WHERE notification_id=? AND accepted=0 ORDER BY part LIMIT 1", (row["id"],)).fetchone()
        self.db.execute("UPDATE notifications SET status='sending',error=NULL,context_id=?,attempts=attempts+1 WHERE id=?", (context["id"], row["id"]))
        return {"message_id": None, "notification_id": row["id"], "reply_id": row["client_id"],
                "client_id": row["client_id"] + ("_part_" + str(part["part"]) if part["part"] else ""),
                "peer": binding["owner"], "text": part["text"], "context_token": context["context_token"]}

    def _finish_notification(self, client_id, status, error):
        from .store import SEND_OUTCOMES, _safe_error

        if status not in SEND_OUTCOMES:
            raise BridgeError("invalid_send_outcome")
        error = None if error is None else _safe_error(error)
        with self._transaction():
            binding = self._binding()
            row = self.db.execute("SELECT * FROM notifications WHERE client_id=? AND account=? AND peer=?", (client_id, binding["id"], binding["owner"])).fetchone()
            if not row or row["status"] != "sending":
                if row and row["status"] == status and row["error"] == error:
                    return
                raise BridgeError("notification_not_sending")
            if status == "context_required":
                context = self._latest_context(binding)
                if row["attempts"] >= 3:
                    status, error = "failed", "retry_limit"
                elif context and context["id"] != row["context_id"]:
                    status, error = "queued", None
            if status == "accepted":
                part = self.db.execute("SELECT part FROM notification_parts WHERE notification_id=? AND accepted=0 ORDER BY part LIMIT 1", (row["id"],)).fetchone()
                self.db.execute("UPDATE notification_parts SET accepted=1 WHERE notification_id=? AND part=?", (row["id"], part[0]))
                if self.db.execute("SELECT 1 FROM notification_parts WHERE notification_id=? AND accepted=0", (row["id"],)).fetchone():
                    status = "queued"
                    self.db.execute("UPDATE notifications SET attempts=0 WHERE id=?", (row["id"],))
            self.db.execute("UPDATE notifications SET status=?,error=? WHERE id=?", (status, error, row["id"]))
            if status == "auth_required":
                self.db.execute("UPDATE binding SET status='auth_required',last_error='auth_required' WHERE id=?", (binding["id"],))
