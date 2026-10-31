"""Anonymous judge sessions with isolated simulations and bounded model spending."""

import hashlib
import hmac
import time
from uuid import uuid4

from app.contracts import ResourceState, ScenarioSnapshot, ServiceError, utcnow
from app.store import Store

COOKIE_NAME = "plab_judge"
SESSION_SECONDS = 24 * 60 * 60


class JudgeAccess:
    def __init__(self, store: Store, secret: str, daily_run_limit: int = 100) -> None:
        if len(secret) < 32:
            raise ValueError("Public judge mode requires a server session secret of 32+ characters")
        if not 1 <= daily_run_limit <= 1000:
            raise ValueError("Daily judge run limit must be between 1 and 1000")
        self.store = store
        self.secret = secret.encode()
        self.daily_run_limit = daily_run_limit
        with store.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS judge_sessions "
                "(session_id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, ip_hash TEXT NOT NULL)"
            )

    def _signature(self, message: str) -> str:
        return hmac.new(self.secret, message.encode(), hashlib.sha256).hexdigest()

    def owner(self, cookie: str | None) -> str:
        if not cookie or len(cookie) > 150:
            raise ServiceError("JUDGE_SESSION_REQUIRED", 403)
        parts = cookie.split(".")
        if len(parts) != 3:
            raise ServiceError("JUDGE_SESSION_REQUIRED", 403)
        identity, issued, signature = parts
        message = f"{identity}.{issued}"
        if not hmac.compare_digest(signature, self._signature(message)):
            raise ServiceError("JUDGE_SESSION_REQUIRED", 403)
        try:
            age = time.time() - int(issued)
        except ValueError as error:
            raise ServiceError("JUDGE_SESSION_REQUIRED", 403) from error
        if not 0 <= age < SESSION_SECONDS:
            raise ServiceError("JUDGE_SESSION_EXPIRED", 403)
        with self.store.transaction() as db:
            found = db.execute(
                "SELECT 1 FROM judge_sessions WHERE session_id=?", (identity,)
            ).fetchone()
        if not found:
            raise ServiceError("JUDGE_SESSION_EXPIRED", 403)
        return identity

    def open(
        self, cookie: str | None, client_ip: str, templates: list[ScenarioSnapshot]
    ) -> tuple[str, str]:
        try:
            identity = self.owner(cookie)
            assert cookie is not None
            return identity, cookie
        except ServiceError:
            pass
        now = int(time.time())
        ip_hash = self._signature(client_ip)
        identity = "judge-" + uuid4().hex
        with self.store.transaction() as db:
            recent = db.execute(
                "SELECT COUNT(*) FROM judge_sessions WHERE created_at>?", (now - SESSION_SECONDS,)
            ).fetchone()[0]
            same_ip = db.execute(
                "SELECT COUNT(*) FROM judge_sessions WHERE created_at>? AND ip_hash=?",
                (now - SESSION_SECONDS, ip_hash),
            ).fetchone()[0]
            if recent >= 200 or same_ip >= 40:
                raise ServiceError("JUDGE_SESSION_LIMIT", 429)
            db.execute("INSERT INTO judge_sessions VALUES (?, ?, ?)", (identity, now, ip_hash))
        for template in templates:
            snapshot = template.model_copy(
                update={
                    "scenario_id": f"{identity}:{template.scenario_id}",
                    "resource_id": f"{identity}:{template.resource_id}",
                }
            )
            self.store.register_scenario(
                snapshot,
                ResourceState(
                    resource_id=snapshot.resource_id, revision=snapshot.resource_revision
                ),
            )
        message = f"{identity}.{now}"
        return identity, f"{message}.{self._signature(message)}"

    @staticmethod
    def require_scope(identity: str, resource: str) -> None:
        if not resource.startswith(identity + ":"):
            raise ServiceError("NOT_FOUND", 404)

    def check_run_budget(self, identity: str) -> None:
        """Called immediately before create_run, with no await, on the single API worker."""
        today = utcnow().date().isoformat()
        with self.store.transaction() as db:
            daily = db.execute(
                "SELECT COUNT(*) FROM runs WHERE json_extract(data, '$.created_at')>=?", (today,)
            ).fetchone()[0]
            personal = db.execute(
                "SELECT COUNT(*) FROM runs WHERE json_extract(data, '$.created_at')>=? "
                "AND json_extract(data, '$.snapshot.scenario_id') LIKE ?",
                (today, identity + ":%"),
            ).fetchone()[0]
            active = db.execute("SELECT COUNT(*) FROM runs WHERE state!='TERMINAL'").fetchone()[0]
        if daily >= self.daily_run_limit or personal >= 10:
            raise ServiceError("JUDGE_RUN_LIMIT", 429)
        if active >= 2:
            raise ServiceError("JUDGE_DESK_BUSY", 429)
