"""Structured acceptance state for planned agent work."""
from __future__ import annotations

import threading
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

MAX_ACTIVE_CHECKS = 10
MAX_SOURCE_CHARS = 300
CHECK_RESULT_STATUSES = {"passed", "failed", "not_run"}


class AcceptanceError(ValueError):
    pass


@dataclass
class AcceptanceState:
    revision: int = 0
    next_id: int = 1
    checks: list[dict[str, str]] = field(default_factory=list)
    removed_checks: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    check_results: list[dict[str, str]] = field(default_factory=list)
    review_status: str = "not_started"
    review_warning: str = ""
    review_truncated: bool = False
    stale_final_submission: bool = False
    pending_notification: str = ""
    pending_review_events: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _review_done: threading.Event = field(default_factory=threading.Event, repr=False)

    def initialize(self, raw_checks: list[dict] | None, *, actor: str = "main_agent") -> dict:
        with self._lock:
            if self.revision or self.checks or self.removed_checks:
                raise AcceptanceError("acceptance checks are already initialized")
            raw_checks = list(raw_checks or [])
            if not raw_checks:
                raise AcceptanceError("start requires at least one acceptance check")
            if len(raw_checks) > MAX_ACTIVE_CHECKS:
                raise AcceptanceError(f"start allows at most {MAX_ACTIVE_CHECKS} acceptance checks")
            checks = [self._new_check(item, origin=actor) for item in raw_checks]
            self.checks = checks
            self.revision = 1
            self.history.append(
                {
                    "revision": self.revision,
                    "actor": actor,
                    "operations": [
                        {"operation": "add", "check": deepcopy(check), "reason": "initial acceptance check"}
                        for check in checks
                    ],
                }
            )
            return self.snapshot()

    def apply_operations(
        self,
        operations: list[dict] | None,
        *,
        expected_revision: int | None,
        actor: str = "main_agent",
    ) -> dict:
        operations = list(operations or [])
        if not operations:
            return self.snapshot()
        with self._lock:
            if expected_revision is None:
                raise AcceptanceError("acceptance_revision is required when modifying acceptance checks")
            if int(expected_revision) != self.revision:
                raise AcceptanceError(
                    f"stale acceptance_revision {expected_revision}; current revision is {self.revision}"
                )

            candidate_checks = deepcopy(self.checks)
            candidate_removed = deepcopy(self.removed_checks)
            candidate_next_id = self.next_id
            recorded: list[dict[str, Any]] = []

            for raw_operation in operations:
                operation = self._normalize_operation(raw_operation)
                kind = operation["operation"]
                reason = operation["reason"]
                if kind == "add":
                    check, candidate_next_id = self._new_check_with_id(
                        {
                            "text": operation.get("text"),
                            "source": operation.get("source"),
                            "verification_command": operation.get("verification_command"),
                        },
                        origin=actor,
                        next_id=candidate_next_id,
                    )
                    candidate_checks.append(check)
                    recorded.append(
                        {"operation": "add", "check": deepcopy(check), "reason": reason}
                    )
                    continue

                check_id = str(operation.get("id") or "").strip()
                index = _check_index(candidate_checks, check_id)
                if index is None:
                    raise AcceptanceError(f"unknown active acceptance check id: {check_id}")
                before = deepcopy(candidate_checks[index])
                if kind == "remove":
                    removed = candidate_checks.pop(index)
                    candidate_removed.append(
                        {
                            **removed,
                            "removed_revision": self.revision + 1,
                            "reason": reason,
                            "actor": actor,
                        }
                    )
                    recorded.append(
                        {"operation": "remove", "before": before, "reason": reason}
                    )
                    continue

                updated = deepcopy(before)
                changed = False
                for field_name in ("text", "source", "verification_command"):
                    if field_name not in operation:
                        continue
                    updated[field_name] = _required_text(operation[field_name], field_name)
                    changed = True
                if not changed:
                    raise AcceptanceError("update operation must change text, source, or verification_command")
                _validate_check(updated)
                candidate_checks[index] = updated
                recorded.append(
                    {
                        "operation": "update",
                        "id": check_id,
                        "before": before,
                        "after": deepcopy(updated),
                        "reason": reason,
                    }
                )

            if len(candidate_checks) > MAX_ACTIVE_CHECKS:
                raise AcceptanceError(f"at most {MAX_ACTIVE_CHECKS} active acceptance checks are allowed")

            self.checks = candidate_checks
            self.removed_checks = candidate_removed
            self.next_id = candidate_next_id
            self.revision += 1
            self.check_results = []
            self.history.append(
                {
                    "revision": self.revision,
                    "actor": actor,
                    "operations": recorded,
                }
            )
            return self.snapshot()

    def begin_review(self) -> bool:
        with self._lock:
            if self.review_status != "not_started":
                return False
            self.review_status = "running"
            self.review_warning = ""
            self._review_done.clear()
            return True

    def finish_review(self, *, status: str, warning: str = "", notification: str = "") -> None:
        with self._lock:
            self.review_status = status
            self.review_warning = str(warning or "").strip()
            self.pending_notification = str(notification or "").strip()
            self._review_done.set()

    def wait_for_review(self, timeout: float | None = None) -> bool:
        return self._review_done.wait(timeout)

    def take_notification(self) -> str:
        with self._lock:
            notification = self.pending_notification
            self.pending_notification = ""
            return notification

    def queue_review_event(self, status: str, payload: dict) -> None:
        self.queue_runtime_event(
            "acceptance_review",
            {"status": status, **deepcopy(payload)},
        )

    def queue_runtime_event(self, event_type: str, payload: dict) -> None:
        with self._lock:
            self.pending_review_events.append(
                {"event_type": str(event_type), "payload": deepcopy(payload)}
            )

    def take_review_events(self) -> list[dict[str, Any]]:
        with self._lock:
            events = self.pending_review_events
            self.pending_review_events = []
            return events

    def checkpoint(self) -> dict[str, Any]:
        with self._lock:
            return {
                "revision": self.revision,
                "next_id": self.next_id,
                "checks": deepcopy(self.checks),
                "removed_checks": deepcopy(self.removed_checks),
                "history": deepcopy(self.history),
                "check_results": deepcopy(self.check_results),
                "stale_final_submission": self.stale_final_submission,
            }

    def rollback(self, checkpoint: dict[str, Any], *, if_revision: int) -> bool:
        with self._lock:
            if self.revision != if_revision:
                return False
            self.revision = checkpoint["revision"]
            self.next_id = checkpoint["next_id"]
            self.checks = deepcopy(checkpoint["checks"])
            self.removed_checks = deepcopy(checkpoint["removed_checks"])
            self.history = deepcopy(checkpoint["history"])
            self.check_results = deepcopy(checkpoint["check_results"])
            self.stale_final_submission = bool(checkpoint["stale_final_submission"])
            return True

    def validate_final(
        self,
        *,
        result_status: str,
        expected_revision: int | None,
        raw_results: list[dict] | None,
    ) -> list[dict[str, str]]:
        with self._lock:
            status = str(result_status or "").strip().lower()
            if status == "success" and expected_revision != self.revision:
                raise AcceptanceError(
                    f"stale acceptance_revision {expected_revision}; current revision is {self.revision}"
                )

            submitted: dict[str, dict[str, str]] = {}
            for raw in raw_results or []:
                if not isinstance(raw, dict):
                    raise AcceptanceError("each check_result must be an object")
                check_id = str(raw.get("id") or "").strip()
                if not check_id or check_id in submitted:
                    raise AcceptanceError("check_results contain a missing or duplicate id")
                check_status = str(raw.get("status") or "").strip().lower()
                if check_status not in CHECK_RESULT_STATUSES:
                    raise AcceptanceError("check result status must be passed, failed, or not_run")
                summary = _required_text(raw.get("summary"), "check result summary")
                submitted[check_id] = {
                    "id": check_id,
                    "status": check_status,
                    "summary": summary,
                }

            active_ids = [check["id"] for check in self.checks]
            unknown = sorted(set(submitted) - set(active_ids))
            if status == "success" and unknown:
                raise AcceptanceError(f"check_results contain unknown or removed ids: {', '.join(unknown)}")

            if status == "success":
                self.stale_final_submission = False
                if not active_ids:
                    raise AcceptanceError("success requires at least one active acceptance check")
                if set(submitted) != set(active_ids):
                    raise AcceptanceError("success requires exactly one result for every active acceptance check")
                if any(submitted[check_id]["status"] != "passed" for check_id in active_ids):
                    raise AcceptanceError("success requires every active acceptance check to be passed")
                normalized = [submitted[check_id] for check_id in active_ids]
            else:
                self.stale_final_submission = (
                    expected_revision is not None and expected_revision != self.revision
                ) or bool(unknown)
                normalized = [
                    submitted.get(
                        check_id,
                        {
                            "id": check_id,
                            "status": "not_run",
                            "summary": "No result submitted before non-success exit.",
                        },
                    )
                    for check_id in active_ids
                ]

            self.check_results = deepcopy(normalized)
            return normalized

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "revision": self.revision,
                "checks": deepcopy(self.checks),
                "removed_checks": deepcopy(self.removed_checks),
                "check_results": deepcopy(self.check_results),
                "review_status": self.review_status,
                "review_warning": self.review_warning,
                "review_truncated": self.review_truncated,
                "stale_final_submission": self.stale_final_submission,
                "report": self._report_unlocked(),
            }

    def _report_unlocked(self) -> dict[str, list[dict[str, Any]]]:
        summaries: dict[str, dict[str, Any]] = {}
        for entry in self.history:
            for operation in entry.get("operations", []):
                kind = operation.get("operation")
                check = operation.get("check") or operation.get("after") or operation.get("before") or {}
                check_id = check.get("id") or operation.get("id")
                if not check_id:
                    continue
                summary = summaries.setdefault(
                    check_id,
                    {
                        "id": check_id,
                        "initial_verification_command": check.get("verification_command", ""),
                        "final_verification_command": check.get("verification_command", ""),
                        "command_modification_count": 0,
                        "last_modification_reason": "",
                    },
                )
                if kind == "update":
                    before = operation.get("before") or {}
                    after = operation.get("after") or {}
                    if before.get("verification_command") != after.get("verification_command"):
                        summary["command_modification_count"] += 1
                        summary["final_verification_command"] = after.get("verification_command", "")
                        summary["last_modification_reason"] = operation.get("reason", "")
        active_ids = {check["id"] for check in self.checks}
        return {
            "active": [summaries[check["id"]] for check in self.checks if check["id"] in summaries],
            "removed": [
                {**summaries.get(check["id"], {"id": check["id"]}), "reason": check.get("reason", "")}
                for check in self.removed_checks
                if check["id"] not in active_ids
            ],
        }

    def _new_check(self, raw: dict, *, origin: str) -> dict[str, str]:
        check, next_id = self._new_check_with_id(raw, origin=origin, next_id=self.next_id)
        self.next_id = next_id
        return check

    @staticmethod
    def _new_check_with_id(raw: dict, *, origin: str, next_id: int) -> tuple[dict[str, str], int]:
        if not isinstance(raw, dict):
            raise AcceptanceError("each acceptance check must be an object")
        unknown = set(raw) - {"text", "source", "verification_command"}
        if unknown:
            raise AcceptanceError(
                "acceptance check contains unsupported fields: " + ", ".join(sorted(unknown))
            )
        check = {
            "id": f"check_{next_id}",
            "text": _required_text(raw.get("text"), "acceptance check text"),
            "source": _required_text(raw.get("source"), "acceptance check source"),
            "verification_command": _required_text(
                raw.get("verification_command"),
                "acceptance check verification_command",
            ),
            "origin": origin,
        }
        _validate_check(check)
        return check, next_id + 1

    @staticmethod
    def _normalize_operation(raw: dict) -> dict:
        if not isinstance(raw, dict):
            raise AcceptanceError("each acceptance operation must be an object")
        operation = dict(raw)
        kind = str(operation.get("operation") or "").strip().lower()
        if kind not in {"add", "update", "remove"}:
            raise AcceptanceError("acceptance operation must be add, update, or remove")
        allowed = {
            "add": {"operation", "text", "source", "verification_command", "reason"},
            "update": {"operation", "id", "text", "source", "verification_command", "reason"},
            "remove": {"operation", "id", "reason"},
        }[kind]
        unknown = set(operation) - allowed
        if unknown:
            raise AcceptanceError(
                f"{kind} operation contains unsupported fields: " + ", ".join(sorted(unknown))
            )
        operation["operation"] = kind
        operation["reason"] = _required_text(operation.get("reason"), "acceptance operation reason")
        return operation


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise AcceptanceError(f"{field_name} must be non-empty")
    return text


def _validate_check(check: dict[str, str]) -> None:
    if len(check["source"]) > MAX_SOURCE_CHARS:
        raise AcceptanceError(f"acceptance check source must be at most {MAX_SOURCE_CHARS} characters")


def _check_index(checks: list[dict[str, str]], check_id: str) -> int | None:
    for index, check in enumerate(checks):
        if check["id"] == check_id:
            return index
    return None
