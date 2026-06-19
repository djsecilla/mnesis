"""The egress control plane — the reusable, **default-deny** gate every future
``risk_class=external`` channel MUST pass through before sending (E1).

No external channel exists yet; this is the machinery one will have to clear.
The posture is **default-deny throughout**: with no configuration, *nothing* may
egress. Every decision is cheap, deterministic, fail-closed, and logged (decision
+ reason, never the raw recipient/secret).

The plane composes five checks, in fail-closed order:

  1. **kill-switch** (`MNESIS_EGRESS_KILL`) and **enabled** (`MNESIS_EGRESS_ENABLED`,
     default false) — either denies *everything*, overriding all else;
  2. **risk** — only `external` sends are governed here (an inert channel must not
     call this; if it does, deny);
  3. **recipient** — accepted ONLY when supplied as structured **policy/user**
     input AND on the **recipient allowlist**. A recipient whose source is
     content/model/artifact is **rejected outright, regardless of the allowlist**
     (anti-exfiltration); an unknown source fails closed;
  4. **endpoint** — the send target must be on the **endpoint allowlist**;
  5. **quota/rate** — per-recipient and global rate limits + daily quotas.

A channel calls :meth:`EgressPolicy.check_send_allowed` *immediately before*
sending and, only if allowed, performs the send and calls
:meth:`EgressPolicy.record_send`. Any error anywhere → **deny**.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .channels import RISK_EXTERNAL
from .triggers.connector import path_lock

log = logging.getLogger("mnesis_agents.egress")

#: A recipient is trustworthy ONLY when it came from these sources. Anything else
#: (content / model / artifact / unknown) is rejected — content can never address.
TRUSTED_SOURCES: frozenset[str] = frozenset({"policy", "user"})

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mask(address: str | None) -> str:
    """Mask a recipient for logs/decisions (never log the full address — PII)."""
    if not address:
        return "(none)"
    if "@" in address:
        local, _, domain = address.partition("@")
        return f"{local[:1]}***@{domain}"
    return f"{address[:2]}***"


# ── Recipient value object ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Recipient:
    """A send target plus the **source** it came from — the source is load-bearing:
    only ``policy``/``user`` recipients are ever trusted."""

    address: str
    source: str  # "policy" | "user" | "content" | "model" | "artifact" | …


def _resolve(recipient: "Recipient | str", source: str | None) -> tuple[str, str]:
    """(address, source) from a Recipient or a bare string. A bare string with no
    explicit source resolves to an **unknown** source — which fails closed."""
    if isinstance(recipient, Recipient):
        return recipient.address.strip(), (recipient.source or "").strip().lower()
    return str(recipient or "").strip(), (source or "unknown").strip().lower()


# ── Decision ────────────────────────────────────────────────────────────────


@dataclass
class EgressDecision:
    allowed: bool
    reason: str
    recipient: str | None = None   # MASKED
    endpoint: str | None = None
    risk_class: str | None = None

    @property
    def denied(self) -> bool:
        return not self.allowed


# ── Quota / rate ledger ─────────────────────────────────────────────────────


class EgressQuotaStore:
    """A tiny durable ledger of send timestamps per key (a recipient address, or
    ``__global__``), for rate-limit + daily-quota checks. JSON, lock-guarded."""

    GLOBAL_KEY = "__global__"

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = path_lock(self.path)

    def _load(self) -> dict[str, list[float]]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            return {k: [float(t) for t in v] for k, v in data.items()} if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001 — a corrupt ledger must not crash a check
            return {}

    def _save(self, data: dict[str, list[float]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.path)

    def record(self, key: str, ts: float, *, keep_seconds: float) -> None:
        with self._lock:
            data = self._load()
            stamps = data.get(key, [])
            stamps.append(ts)
            cutoff = ts - keep_seconds
            data[key] = [t for t in stamps if t >= cutoff]
            self._save(data)

    def counts(self, key: str, now: datetime, window_seconds: float) -> tuple[int, int]:
        """(sends within the rate window, sends within the current UTC day)."""
        with self._lock:
            stamps = self._load().get(key, [])
        now_ts = now.timestamp()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        rate_count = sum(1 for t in stamps if t > now_ts - window_seconds)
        day_count = sum(1 for t in stamps if t >= day_start)
        return rate_count, day_count


# ── Policy ──────────────────────────────────────────────────────────────────


def _parse_list(raw: str) -> frozenset[str]:
    return frozenset(e.strip().lower() for e in (raw or "").split(",") if e.strip())


@dataclass
class EgressPolicy:
    """The default-deny egress policy. Construct from config via :meth:`from_config`
    (or directly, for tests)."""

    enabled: bool = False
    kill: bool = False
    recipient_allowlist: frozenset[str] = field(default_factory=frozenset)
    endpoint_allowlist: frozenset[str] = field(default_factory=frozenset)
    rate_limit: int = 10
    rate_window_seconds: float = 3600.0
    daily_quota: int = 50
    global_rate_limit: int = 30
    global_daily_quota: int = 200
    quota_store: EgressQuotaStore | None = None

    @classmethod
    def from_config(cls, *, quota_store: EgressQuotaStore | None = None) -> "EgressPolicy":
        return cls(
            enabled=config.MNESIS_EGRESS_ENABLED,
            kill=config.MNESIS_EGRESS_KILL,
            recipient_allowlist=_parse_list(config.MNESIS_EGRESS_RECIPIENT_ALLOWLIST),
            endpoint_allowlist=_parse_list(config.MNESIS_EGRESS_ENDPOINT_ALLOWLIST),
            rate_limit=config.MNESIS_EGRESS_RATE_LIMIT,
            rate_window_seconds=config.MNESIS_EGRESS_RATE_WINDOW_SECONDS,
            daily_quota=config.MNESIS_EGRESS_DAILY_QUOTA,
            global_rate_limit=config.MNESIS_EGRESS_GLOBAL_RATE_LIMIT,
            global_daily_quota=config.MNESIS_EGRESS_GLOBAL_DAILY_QUOTA,
            quota_store=quota_store or EgressQuotaStore(config.MNESIS_EGRESS_STATE_DIR / "egress.json"),
        )

    def _store(self) -> EgressQuotaStore:
        if self.quota_store is None:
            self.quota_store = EgressQuotaStore(config.MNESIS_EGRESS_STATE_DIR / "egress.json")
        return self.quota_store

    # -- decision helper (logs masked; never the raw recipient) ----------------

    def _decision(self, allowed: bool, reason: str, *, recipient=None, endpoint=None, risk=None) -> EgressDecision:
        masked = _mask(recipient)
        (log.info if allowed else log.warning)(
            "egress %s: %s [recipient=%s endpoint=%s risk=%s]",
            "ALLOW" if allowed else "DENY", reason, masked, endpoint, risk,
        )
        return EgressDecision(allowed, reason, recipient=masked, endpoint=endpoint, risk_class=risk)

    # -- allowlist matching ----------------------------------------------------

    def _recipient_allowlisted(self, address: str) -> bool:
        addr = address.strip().lower()
        if not addr:
            return False
        domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
        for entry in self.recipient_allowlist:
            if "@" in entry and not entry.startswith("@"):
                if entry == addr:           # exact address
                    return True
            else:                            # a domain (with or without leading @)
                dom = entry[1:] if entry.startswith("@") else entry
                if domain and dom == domain:
                    return True
        return False

    def _endpoint_allowlisted(self, endpoint: str | None) -> bool:
        ep = (endpoint or "").strip().lower()
        if not ep or not self.endpoint_allowlist:
            return False
        host = ep.split(":", 1)[0]
        return ep in self.endpoint_allowlist or host in self.endpoint_allowlist

    # -- recipient validation (source + allowlist) -----------------------------

    def validate_recipient(self, recipient: "Recipient | str", source: str | None = None) -> EgressDecision:
        """Accept a recipient ONLY when it is **policy/user**-sourced AND on the
        allowlist. A content/model/artifact (or unknown) source is rejected
        regardless of the allowlist."""
        address, src = _resolve(recipient, source)
        if src not in TRUSTED_SOURCES:
            return self._decision(
                False,
                f"recipient source {src!r} is not policy/user — content/model/artifact "
                "recipients are rejected", recipient=address,
            )
        if not address or not _EMAIL_RE.match(address):
            return self._decision(False, "recipient is empty or malformed", recipient=address)
        if not self._recipient_allowlisted(address):
            return self._decision(False, "recipient not on the allowlist", recipient=address)
        return self._decision(True, "recipient ok (policy/user + allowlisted)", recipient=address)

    # -- quota / rate ----------------------------------------------------------

    def _check_quota(self, address: str, now: datetime, *, endpoint=None) -> EgressDecision:
        store = self._store()
        window = self.rate_window_seconds
        g_rate, g_day = store.counts(EgressQuotaStore.GLOBAL_KEY, now, window)
        r_rate, r_day = store.counts(address, now, window)

        checks = [
            (self.global_rate_limit, g_rate, "global rate limit"),
            (self.global_daily_quota, g_day, "global daily quota"),
            (self.rate_limit, r_rate, "per-recipient rate limit"),
            (self.daily_quota, r_day, "per-recipient daily quota"),
        ]
        for limit, count, label in checks:
            if limit == 0:
                return self._decision(False, f"{label} is zero (no sends permitted)",
                                      recipient=address, endpoint=endpoint, risk=RISK_EXTERNAL)
            if limit > 0 and count >= limit:
                return self._decision(False, f"{label} exceeded ({count}/{limit})",
                                      recipient=address, endpoint=endpoint, risk=RISK_EXTERNAL)
        return self._decision(True, "within quota/rate", recipient=address,
                              endpoint=endpoint, risk=RISK_EXTERNAL)

    # -- the composed gate -----------------------------------------------------

    def check_send_allowed(
        self,
        channel_risk: str,
        recipient: "Recipient | str",
        endpoint: str | None,
        *,
        source: str | None = None,
        now: datetime | None = None,
    ) -> EgressDecision:
        """The single fail-closed gate a channel calls immediately before sending.

        Composes enabled + kill-switch + risk + recipient (source + allowlist) +
        endpoint + quota/rate. Any error → **deny**."""
        try:
            now = now or _now()
            address, _src = _resolve(recipient, source)

            # 1) Kill-switch and "disabled" override everything.
            if self.kill:
                return self._decision(False, "egress kill-switch engaged (MNESIS_EGRESS_KILL)",
                                      recipient=address, endpoint=endpoint, risk=channel_risk)
            if not self.enabled:
                return self._decision(False, "egress disabled (default-deny; MNESIS_EGRESS_ENABLED unset)",
                                      recipient=address, endpoint=endpoint, risk=channel_risk)

            # 2) Only external sends are governed here.
            if channel_risk != RISK_EXTERNAL:
                return self._decision(False, f"egress plane governs external sends only (risk={channel_risk!r})",
                                      recipient=address, endpoint=endpoint, risk=channel_risk)

            # 3) Recipient: policy/user-sourced AND allowlisted.
            rv = self.validate_recipient(recipient, source)
            if rv.denied:
                return self._decision(False, rv.reason, recipient=address,
                                      endpoint=endpoint, risk=channel_risk)

            # 4) Endpoint allowlist.
            if not self._endpoint_allowlisted(endpoint):
                return self._decision(False, "endpoint not on the allowlist", recipient=address,
                                      endpoint=endpoint, risk=channel_risk)

            # 5) Quota / rate.
            q = self._check_quota(address, now, endpoint=endpoint)
            if q.denied:
                return q

            return self._decision(True, "send permitted", recipient=address,
                                  endpoint=endpoint, risk=channel_risk)
        except Exception as exc:  # noqa: BLE001 — fail closed on ANY error
            return self._decision(False, f"egress check error (fail-closed): {exc}")

    def record_send(self, recipient: "Recipient | str", *, source: str | None = None, now: datetime | None = None) -> None:
        """Record a completed send (call only after an allowed, successful send)."""
        address, _ = _resolve(recipient, source)
        if not address:
            return
        ts = (now or _now()).timestamp()
        keep = max(self.rate_window_seconds, 86400.0)
        store = self._store()
        store.record(EgressQuotaStore.GLOBAL_KEY, ts, keep_seconds=keep)
        store.record(address, ts, keep_seconds=keep)


# ── Module-level convenience (a default policy from config) ──────────────────


def default_policy() -> EgressPolicy:
    return EgressPolicy.from_config()


def validate_recipient(recipient: "Recipient | str", source: str | None = None,
                       *, policy: EgressPolicy | None = None) -> EgressDecision:
    return (policy or default_policy()).validate_recipient(recipient, source)


def check_send_allowed(channel_risk: str, recipient: "Recipient | str", endpoint: str | None,
                       *, source: str | None = None, policy: EgressPolicy | None = None,
                       now: datetime | None = None) -> EgressDecision:
    return (policy or default_policy()).check_send_allowed(
        channel_risk, recipient, endpoint, source=source, now=now)
