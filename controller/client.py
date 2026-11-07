"""HTTP client for the simulator / platform API.

Retry rules (see SUBMISSION.md §9):
  * GETs are retried on transport errors and 5xx with exponential backoff.
  * Mutating calls are retried only where the server makes them idempotent
    (proposal_id on /approvals and PUT config) or after we have checked that the
    first attempt did not land (/sim/advance: compare sim_minute first).
  * 4xx are never retried; they are raised as `ApiError` with the server's code so
    callers can branch on `stale_base_version`, `duplicate_proposal_id`, ...
"""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger("controller.client")


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail=None):
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


class TransportError(Exception):
    pass


class SimClient:
    def __init__(self, base_url: str, timeout: float = 600.0, retries: int = 8):
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base, timeout=timeout)
        self.retries = retries
        self.calls = 0
        self.errors = 0

    # ------------------------------------------------------------------ plumbing
    def _send(self, method: str, path: str, *, retry: bool, **kw) -> httpx.Response:
        delay = 0.5
        for attempt in range(self.retries + 1):
            self.calls += 1
            try:
                r = self.http.request(method, path, **kw)
            except httpx.TransportError as e:
                self.errors += 1
                if not retry or attempt == self.retries:
                    raise TransportError(f"{method} {path}: {e}") from e
                log.warning("transport error on %s %s (%s); retry in %.1fs", method, path, e, delay)
            else:
                if r.status_code < 500:
                    return r
                self.errors += 1
                if not retry or attempt == self.retries:
                    return r
                log.warning("%s on %s %s; retry in %.1fs", r.status_code, method, path, delay)
            time.sleep(delay)
            delay = min(delay * 2, 30)
        raise AssertionError("unreachable")

    @staticmethod
    def _json(r: httpx.Response):
        if r.status_code >= 400:
            try:
                body = r.json()
                code, detail = body.get("error") or body.get("detail") or str(r.status_code), body.get("detail")
            except ValueError:
                code, detail = str(r.status_code), r.text[:500]
            raise ApiError(r.status_code, str(code), detail)
        return r.json()

    def get(self, path: str, **params):
        return self._json(self._send("GET", path, retry=True, params=params or None))

    # ------------------------------------------------------------------ reads
    def health(self) -> bool:
        try:
            return bool(self.get("/health").get("ok"))
        except (TransportError, ApiError):
            return False

    def state(self) -> dict:
        return self.get("/sim/state")

    def workflows(self) -> list[dict]:
        return self.get("/workflows")

    def workflow(self, wid: str) -> dict:
        return self.get(f"/workflows/{wid}")

    def config(self, wid: str) -> dict:
        return self.get(f"/workflows/{wid}/config")

    def config_history(self, wid: str) -> list[dict]:
        return self.get(f"/workflows/{wid}/config/history")

    def approvals(self, workflow_id: str | None = None, status: str | None = None) -> list[dict]:
        p = {k: v for k, v in (("workflow_id", workflow_id), ("status", status)) if v}
        return self.get("/approvals", **p)

    def approval(self, aid: str) -> dict:
        return self.get(f"/approvals/{aid}")

    def chunks(self, kind: str, after: int = -1) -> list[dict]:
        return self.get("/events/chunks", kind=kind, after=after)

    def chunk_bytes(self, n: int) -> bytes:
        r = self._send("GET", f"/events/chunks/{n}", retry=True)
        if r.status_code >= 400:
            raise ApiError(r.status_code, "chunk_fetch_failed", r.text[:200])
        return r.content

    # ------------------------------------------------------------------ writes
    def advance(self, minutes: int, expect_from: int) -> dict:
        """Advance the clock exactly once. If the response is lost we check the
        clock before re-sending, so a retry can never advance twice."""
        try:
            r = self._send("POST", "/sim/advance", retry=False, json={"minutes": minutes})
            if r.status_code >= 500:
                raise TransportError(f"advance returned {r.status_code}")
            return self._json(r)
        except TransportError:
            for _ in range(self.retries):
                time.sleep(1.0)
                try:
                    st = self.state()
                except TransportError:
                    continue
                if st["sim_minute"] != expect_from:
                    log.warning("advance response lost but clock moved %s→%s; not re-sending",
                                expect_from, st["sim_minute"])
                    return {"sim_minute": st["sim_minute"], "finished": st["finished"], "new_chunks": [],
                            "recovered": True}
                r = self._send("POST", "/sim/advance", retry=False, json={"minutes": minutes})
                if r.status_code < 500:
                    return self._json(r)
            raise

    def request_approval(self, body: dict) -> dict:
        return self._json(self._send("POST", "/approvals", retry=True, json=body))

    def auto_apply(self, wid: str, body: dict) -> dict:
        return self._json(self._send("PUT", f"/workflows/{wid}/config", retry=True, json=body))

    def apply(self, wid: str, approval_id: str) -> dict:
        return self._json(self._send("POST", f"/workflows/{wid}/apply", retry=True,
                                     json={"approval_id": approval_id}))

    def suggest_fixes(self, wid: str, problem: str) -> dict:
        # Costs credits and has no idempotency key: never auto-retried.
        return self._json(self._send("POST", "/llm/suggest-fixes", retry=False,
                                     json={"workflow_id": wid, "problem": problem}))

    def validate(self, wid: str, prompt_version: str, n: int) -> dict:
        return self._json(self._send("POST", "/validation/runs", retry=False,
                                     json={"workflow_id": wid, "prompt_version": prompt_version, "n": n}))
