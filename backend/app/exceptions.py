from __future__ import annotations


class AutomaticRevision(Exception):
    """Ask the workflow manager to rerun one role without failing the project."""

    def __init__(self, target: str, unit_key: str, feedback: str):
        super().__init__(feedback)
        self.target = target
        self.unit_key = unit_key
        self.feedback = feedback
