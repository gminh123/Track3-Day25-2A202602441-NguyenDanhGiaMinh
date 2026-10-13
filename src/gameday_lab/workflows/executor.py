"""Part D — Exactly-once *effects* for an agent workflow.

Two ingredients, and you need both:

1. **Idempotency keys** — a retry (or a re-run after a crash) of a side effect
   must reuse the *same* key, so the payment provider de-duplicates it.
   The key must be derived from *what* you are doing (session + step), never
   random per attempt.
2. **Checkpoints** — after each step succeeds, persist its result in the
   DurableStore. A fresh worker resumes from the last checkpoint instead of
   redoing (and re-paying for) everything.

Think hard about the ORDER of "execute step" and "save checkpoint".
Never catch ``WorkerCrash`` — it means this process is dead.

Tests: tests/test_executor.py, then `make workflows`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any, TypeVar

from gameday_lab.workflows.runner import STEPS, RefundRequest
from gameday_lab.workflows.services import DurableStore, ServiceError, Services, ServiceTimeout

T = TypeVar("T")


def idempotency_key(req: RefundRequest, step: str) -> str:
    """Return a stable, step-specific key scoped to the workflow session."""
    raw = json.dumps([req.session_id, step], separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def checkpoint_key(req: RefundRequest, step: str) -> str:
    """Return the durable storage key for a workflow step result."""
    return f"workflow:{req.session_id}:{step}"


class WorkflowExecutor:
    def __init__(self, services: Services, store: DurableStore, max_attempts: int = 5) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.s = services
        self.store = store
        self.max_attempts = max_attempts

    def _retry(self, fn: Callable[[], T]) -> T:
        """Retry failed service calls, allowing ambiguous timeouts to reuse their idempotency key."""
        for attempt in range(self.max_attempts):
            try:
                return fn()
            except (ServiceError, ServiceTimeout):
                if attempt + 1 == self.max_attempts:
                    raise
        raise RuntimeError("unreachable retry state")

    def _step(self, req: RefundRequest, state: dict[str, Any], step: str) -> Any:
        """PROVIDED: execute one step. ``state`` holds results of earlier steps."""
        if step == "classify_request":
            return self._retry(lambda: self.s.llm.classify(req.order_id, req.amount_cents))
        if step == "lookup_order":
            return self._retry(lambda: self.s.orders.get(state["classify_request"]["order_id"]))
        if step == "issue_refund":
            key = idempotency_key(req, step)
            return self._retry(lambda: self.s.payments.refund(req.order_id, req.amount_cents, idempotency_key=key))
        if step == "send_email":
            key = idempotency_key(req, step)
            data = {"refund_id": state["issue_refund"]["refund_id"]}
            return self._retry(lambda: self.s.email.send(req.customer_email, "refund_done", data, idempotency_key=key))
        if step == "write_summary":
            return self._retry(lambda: self.s.llm.summarize(state["issue_refund"]["refund_id"],
                                                            state["send_email"]["message_id"]))
        raise ValueError(step)

    def run(self, req: RefundRequest) -> dict[str, Any]:
        """Resume from durable step results and checkpoint each completed step.

        Persist only after a step succeeds so a crash cannot make an unfinished
        side effect look complete.
        """
        state: dict[str, Any] = {}
        for step in STEPS:
            key = checkpoint_key(req, step)
            checkpoint = self.store.get(key)
            if checkpoint is not None:
                state[step] = checkpoint
                continue

            result = self._step(req, state, step)
            self.store.put(key, result)
            state[step] = result

        return {"status": "done", **state}
