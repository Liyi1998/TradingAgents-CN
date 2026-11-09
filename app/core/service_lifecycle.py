"""Process-local lifecycle state; no network I/O in the liveness probe."""

from dataclasses import dataclass


@dataclass
class ServiceLifecycle:
    phase: str = "starting"

    def mark_ready(self) -> None:
        self.phase = "ready"

    def mark_stopping(self) -> None:
        self.phase = "stopping"

    def mark_stopped(self) -> None:
        self.phase = "stopped"

    def mark_failed(self) -> None:
        self.phase = "failed"
