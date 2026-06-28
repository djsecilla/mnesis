"""The immutable send-audit log (E4) — one tamper-evident record per send attempt.

Every attempt to transmit through an external channel writes **exactly one**
append-only record: the proposal id, approval id, channel, recipient, endpoint,
content hash, the egress **decision**, the resulting **status**, and a timestamp —
**never the message body, never a secret**.

Tamper-evidence is a **hash chain**: each record carries the previous record's
hash and its own ``hash = sha256(prev_hash | canonical(record-without-hash))``, so
any edit/insertion/deletion breaks the chain and :meth:`verify` reports it. The
log is append-only (records are only ever appended; nothing is rewritten).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import config
from .config import now_iso as _now
from .triggers.connector import path_lock

_GENESIS = "GENESIS"

#: The fields covered by the hash (everything except the hash itself), in order.
_HASHED_FIELDS = (
    "seq", "ts", "proposal_id", "approval_id", "channel", "recipient",
    "endpoint", "content_hash", "decision", "status", "prev_hash",
)


def _chain_hash(prev_hash: str, record: dict) -> str:
    payload = "|".join(f"{k}={record.get(k)!r}" for k in _HASHED_FIELDS)
    return "sha256:" + hashlib.sha256((prev_hash + "|" + payload).encode("utf-8")).hexdigest()


class SendAuditLog:
    """Append-only, hash-chained JSONL of external send attempts."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or config.MNESIS_SEND_AUDIT_FILE)
        self._lock = path_lock(self.path)

    # -- read ------------------------------------------------------------------

    def all(self) -> list[dict]:
        if not self.path.is_file():
            return []
        out = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def _last(self) -> dict | None:
        records = self.all()
        return records[-1] if records else None

    # -- append (the only mutation) -------------------------------------------

    def record(
        self, *, proposal_id, approval_id, channel, recipient, endpoint,
        content_hash, decision, status,
    ) -> dict:
        """Append one immutable record (chained to the previous). Returns it.

        ``decision`` is a short egress decision string and ``status`` the send
        outcome — neither carries the body or a secret."""
        with self._lock:
            last = self._last()
            prev_hash = last["hash"] if last else _GENESIS
            seq = (last["seq"] + 1) if last else 0
            rec = {
                "seq": seq, "ts": _now(),
                "proposal_id": proposal_id, "approval_id": approval_id,
                "channel": channel, "recipient": recipient, "endpoint": endpoint,
                "content_hash": content_hash, "decision": decision, "status": status,
                "prev_hash": prev_hash,
            }
            rec["hash"] = _chain_hash(prev_hash, rec)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            return rec

    # -- integrity -------------------------------------------------------------

    def verify(self) -> tuple[bool, int | None]:
        """Recompute the hash chain. Returns ``(ok, broken_at_seq)`` — ``ok`` is
        True when every record's hash matches and links to its predecessor; a
        tampered/edited/reordered record is reported by its ``seq``."""
        prev_hash = _GENESIS
        for i, rec in enumerate(self.all()):
            if rec.get("seq") != i or rec.get("prev_hash") != prev_hash:
                return False, rec.get("seq", i)
            expected = _chain_hash(prev_hash, rec)
            if rec.get("hash") != expected:
                return False, rec.get("seq", i)
            prev_hash = rec["hash"]
        return True, None
