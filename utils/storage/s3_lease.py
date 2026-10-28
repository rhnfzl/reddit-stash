"""
S3 lease lock so only one runner (homelab, GitHub Actions, ...) syncs at a time.

One small JSON object is both the lock and the heartbeat:

    <prefix>/.reddit-stash/control/lock.json
    {"schema": 1, "state": "running" | "idle", "lease_id": "<uuid>",
     "runner": "...", "acquired_at": "...", "expires_at": "...",
     "last_success": "..." | null, "last_success_runner": "..." | null}

Every write is conditional (S3 conditional writes): create with If-None-Match: *,
replace with If-Match: <etag>. A lost race shows up as 412/409/404, and because
botocore retries requests, a conflict can also be our own earlier attempt that
landed. So every conflict is resolved by reading the object back and comparing
lease_id, never by assuming who won.

Correctness relies on runners being killed before the lease expires
(LEASE_TTL is longer than any runner's hard deadline) and on NTP-synced clocks
(SKEW_MARGIN covers small drift).
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

CONTROL_DIR = ".reddit-stash"
LEASE_KEY = f"{CONTROL_DIR}/control/lock.json"
SCHEMA = 1
LEASE_TTL = timedelta(minutes=210)  # GitHub job timeout is 180 min; homelab kills at 170
SKEW_MARGIN = timedelta(minutes=5)

# HTTP statuses S3 uses when a conditional write loses: precondition failed,
# concurrent conditional write, and If-Match on an object deleted meanwhile.
_CONFLICT_STATUSES = {404, 409, 412}


def lease_key(prefix: str) -> str:
    prefix = prefix.strip("/")
    return f"{prefix}/{LEASE_KEY}" if prefix else LEASE_KEY


def is_control_key(key: str) -> bool:
    """True for keys under the control directory, which must never be synced."""
    return key.startswith(f"{CONTROL_DIR}/") or f"/{CONTROL_DIR}/" in key


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value) -> Optional[datetime]:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _status(exc) -> Optional[int]:
    return getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")


class S3Lease:
    """Acquire, check and release the run lease for one dataset prefix."""

    def __init__(self, s3_client, bucket: str, prefix: str, runner: str):
        self._s3 = s3_client
        self._bucket = bucket
        self._key = lease_key(prefix)
        self.runner = runner
        self.lease_id = str(uuid.uuid4())
        self._etag: Optional[str] = None  # ETag of the last write this lease made
        self._body: Optional[dict] = None

    @property
    def body(self) -> Optional[dict]:
        """The lease object as last written by this runner."""
        return self._body

    # -- reading -------------------------------------------------------

    def read(self) -> Tuple[Optional[dict], Optional[str]]:
        """Return (body, etag); (None, None) when the object does not exist.

        An unparseable object comes back as {} so it is treated as expired."""
        try:
            resp = self._s3.get_object(Bucket=self._bucket, Key=self._key)
        except Exception as exc:
            if _status(exc) == 404:
                return None, None
            raise
        try:
            body = json.loads(resp["Body"].read())
            if not isinstance(body, dict):
                body = {}
        except ValueError:
            body = {}
        return body, resp["ETag"]

    @staticmethod
    def holder_is_active(body: Optional[dict], now: Optional[datetime] = None) -> bool:
        """True when body is a valid, unexpired running lease."""
        if not body or body.get("schema") != SCHEMA or body.get("state") != "running":
            return False
        now = now or _now()
        expires = _parse(body.get("expires_at"))
        if expires is None or expires > now + LEASE_TTL + SKEW_MARGIN:
            return False  # corrupt or implausibly long lease: never let it block forever
        return expires + SKEW_MARGIN > now

    @staticmethod
    def last_success(body: Optional[dict]) -> Optional[datetime]:
        """Last successful run, or None when never succeeded or the value is invalid."""
        when = _parse((body or {}).get("last_success"))
        if when is None or when > _now() + SKEW_MARGIN:
            return None
        return when

    # -- writing -------------------------------------------------------

    def _put(self, body: dict, etag: Optional[str]) -> str:
        condition = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
        resp = self._s3.put_object(
            Bucket=self._bucket, Key=self._key,
            Body=json.dumps(body, indent=2).encode(),
            ContentType="application/json",
            **condition,
        )
        return resp["ETag"]

    def _resolve_conflict(self) -> bool:
        """After a conflict, check whether our own write is the one that landed."""
        body, etag = self.read()
        if body and body.get("lease_id") == self.lease_id:
            self._body, self._etag = body, etag
            return True
        return False

    def acquire(self) -> bool:
        """Take the lease. False means another runner holds it or won the race."""
        current, etag = self.read()
        now = _now()
        if self.holder_is_active(current, now):
            holder = current or {}
            print(f"Lease held by {holder.get('runner')} until {holder.get('expires_at')}; skipping.")
            return False
        if current and current.get("state") == "running":
            print(f"Taking over expired or invalid lease from {current.get('runner')}.")

        body = {
            "schema": SCHEMA,
            "state": "running",
            "lease_id": self.lease_id,
            "runner": self.runner,
            "acquired_at": _iso(now),
            "expires_at": _iso(now + LEASE_TTL),
            "last_success": (current or {}).get("last_success"),
            "last_success_runner": (current or {}).get("last_success_runner"),
        }
        try:
            self._etag = self._put(body, etag)
            self._body = body
            return True
        except Exception as exc:
            if _status(exc) not in _CONFLICT_STATUSES:
                raise
        if self._resolve_conflict():
            return True
        print("Another runner acquired the lease first; skipping.")
        return False

    def still_held(self) -> bool:
        """True when the stored lease is still ours, untouched and unexpired."""
        body, etag = self.read()
        return (
            body is not None
            and etag == self._etag
            and body.get("lease_id") == self.lease_id
            and self.holder_is_active(body)
        )

    def release(self, success: bool) -> bool:
        """Mark the lease idle; record last_success only for a successful run."""
        if self._etag is None:
            return False
        body = dict(self._body or {}, state="idle")
        if success:
            body["last_success"] = _iso(_now())
            body["last_success_runner"] = self.runner
        try:
            self._etag = self._put(body, self._etag)
            self._body = body
            return True
        except Exception as exc:
            if _status(exc) not in _CONFLICT_STATUSES:
                raise
        current, etag = self.read()
        if current and current.get("lease_id") == self.lease_id and current.get("state") == "idle":
            self._body, self._etag = current, etag  # our release landed; the response was lost
            return True
        print("WARNING: lease was taken over before release; leaving the new holder's lease untouched.")
        return False
