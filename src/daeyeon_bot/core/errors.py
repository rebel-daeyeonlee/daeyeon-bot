"""Error taxonomy. See `CONTRACTS.md` §4.

Unclassified exceptions are treated by the dispatcher as `PermanentError`.
"""

from __future__ import annotations


class BotError(Exception):
    """Base class for all daeyeon-bot domain errors."""


class TransientError(BotError):
    """Retry-eligible failure (network blip, 5xx, timeout)."""


class RateLimitError(TransientError):
    """Upstream rate limited us. Dispatcher uses dedicated backoff."""


class PermanentError(BotError):
    """Retrying will not help. Goes to dead_letter."""


class ValidationError(PermanentError):
    """Payload failed boundary validation."""


class ConfigError(PermanentError):
    """Misconfiguration. Daemon-level when raised at boot, dead_letter at handler scope."""


class RunLogUnavailableError(PermanentError):
    """A GitHub Actions run log is gone for good (not found / deleted / expired by
    retention / overwritten by a re-run). Distinct from a transient `gh` failure:
    re-queueing will never recover the log, so the ci_triage handler maps this to
    `Ack` + audit(`skipped_log_unavailable`) instead of `Retry`. See
    specs/003-ci-monitor-bot/plan.md §gh_cli.py extension."""


class AuthError(BotError):
    """Token expired/revoked. Daemon halts (exit 78). Operator must rotate."""


class QuotaError(BotError):
    """Local rate limiter rejected the call (kill-switch / token bucket empty)."""
