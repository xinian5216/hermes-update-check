"""hermes-update-check - safety-first update checker and risk assessor for Hermes Agent.

Design rule number one: this tool never updates anything on its own.
It checks, it assesses, it explains, it asks, and only then it acts.
"""

from __future__ import annotations

__all__ = ["__version__", "TOOL_NAME"]

TOOL_NAME = "hermes-update-check"
__version__ = "1.0.0"
