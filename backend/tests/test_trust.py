"""Trust Score Engine — pure unit coverage with the network probe disabled.

Every test passes ``skip_proxy_probe=True`` so the trust evaluator never
attempts a real HTTP HEAD; the proxy weight is granted IFF the proxy is
attached, regardless of the network.

Threshold + weights (from app/services/trust.py):
    PROXY_WEIGHT       = 60
    USER_AGENT_WEIGHT  = 25
    HYGIENE_WEIGHT     = 15
    DEFAULT_MIN_TRUST_SCORE = 50
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.trust import (
    DEFAULT_MIN_TRUST_SCORE,
    HYGIENE_WEIGHT,
    PROXY_WEIGHT,
    USER_AGENT_WEIGHT,
    TrustReport,
    evaluate_trust,
)


_GOOD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def _make_account(*, tags=(), status=None, with_proxy=True):
    """Build an InstagramAccount-shaped SimpleNamespace for trust evaluation.

    The trust evaluator only reads ``account.proxy``, ``account.tags``, and
    ``account.status`` — nothing else needs to be set.
    """
    proxy = None
    if with_proxy:
        proxy = SimpleNamespace(
            host="proxy.example.com",
            port=8080,
            username="puser",
            password="ppass",
        )
    return SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        proxy=proxy,
        tags=list(tags),
        status=status,
    )


# ── Negative-path tests ─────────────────────────────────────────────────
class TestTrustFailures:
    def test_missing_proxy_drops_score_below_threshold(self):
        account = _make_account(with_proxy=False)
        report = evaluate_trust(account, user_agent=_GOOD_UA, skip_proxy_probe=True)

        assert report.proxy_ok is False
        assert report.score == USER_AGENT_WEIGHT + HYGIENE_WEIGHT  # 25 + 15 = 40
        assert report.score < DEFAULT_MIN_TRUST_SCORE
        assert report.passed is False
        assert any("no proxy" in reason.lower() for reason in report.reasons)

    def test_bad_user_agent_drops_ua_weight(self):
        account = _make_account()
        report = evaluate_trust(account, user_agent="curl/7.81", skip_proxy_probe=True)

        assert report.user_agent_ok is False
        assert report.score == PROXY_WEIGHT + HYGIENE_WEIGHT  # 60 + 15 = 75
        # Still passes threshold — proxy alone exceeds 50.
        assert report.passed is True
        assert any("user_agent" in reason for reason in report.reasons)

    def test_empty_user_agent_is_rejected(self):
        account = _make_account()
        report = evaluate_trust(account, user_agent="", skip_proxy_probe=True)

        assert report.user_agent_ok is False
        assert any("missing" in reason.lower() for reason in report.reasons)

    def test_oversized_user_agent_is_rejected(self):
        account = _make_account()
        report = evaluate_trust(
            account, user_agent="A" * 1024, skip_proxy_probe=True
        )
        assert report.user_agent_ok is False

    def test_risk_tag_drops_hygiene_weight(self):
        account = _make_account(tags=["possible_shadowban"])
        report = evaluate_trust(account, user_agent=_GOOD_UA, skip_proxy_probe=True)

        assert report.hygiene_ok is False
        assert report.score == PROXY_WEIGHT + USER_AGENT_WEIGHT  # 60 + 25 = 85
        assert report.passed is True
        assert any("possible_shadowban" in reason for reason in report.reasons)

    def test_checkpoint_status_drops_hygiene_weight(self):
        account = _make_account(status="checkpoint_required")
        report = evaluate_trust(account, user_agent=_GOOD_UA, skip_proxy_probe=True)

        assert report.hygiene_ok is False
        assert "checkpoint_required" in str(report.reasons)

    def test_dead_proxy_alone_is_a_hard_fail(self):
        """Even with perfect UA + hygiene, no proxy = below threshold."""
        account = _make_account(with_proxy=False)
        report = evaluate_trust(account, user_agent=_GOOD_UA, skip_proxy_probe=True)
        assert report.passed is False, "proxy must be necessary, not optional"

    def test_to_skip_reason_includes_low_trust_score_token(self):
        """Fan-out depends on the leading 'low_trust_score' token for the UI."""
        account = _make_account(with_proxy=False)
        report = evaluate_trust(account, user_agent=_GOOD_UA, skip_proxy_probe=True)
        skip_reason = report.to_skip_reason()
        assert skip_reason.startswith("low_trust_score")
        assert f"score={report.score}/100" in skip_reason


# ── Positive-path tests ─────────────────────────────────────────────────
class TestTrustHappyPath:
    def test_clean_account_with_good_proxy_and_ua_scores_full(self):
        account = _make_account(tags=[], status=None)
        report = evaluate_trust(account, user_agent=_GOOD_UA, skip_proxy_probe=True)

        assert report.proxy_ok is True
        assert report.user_agent_ok is True
        assert report.hygiene_ok is True
        assert report.score == 100
        assert report.passed is True
        assert report.reasons == []

    def test_neutral_tags_do_not_penalize(self):
        """Only the curated risk-tag set should drop hygiene."""
        account = _make_account(tags=["crypto", "tier1", "fitness"])
        report = evaluate_trust(account, user_agent=_GOOD_UA, skip_proxy_probe=True)
        assert report.hygiene_ok is True
        assert report.score == 100


# ── Combinatorial / edge cases ──────────────────────────────────────────
class TestTrustEdges:
    def test_evaluator_crash_is_handled_in_report_form(self):
        """When the proxy probe raises, score=0 and the reason reports it."""
        # Build an account whose proxy is well-formed enough to invoke the
        # probe path, but with a bogus host that will fail DNS resolution.
        account = SimpleNamespace(
            id="x",
            proxy=SimpleNamespace(
                host="this-host-cannot-possibly-exist.invalid",
                port=8080,
                username="u",
                password="p",
            ),
            tags=[],
            status=None,
        )
        # Don't skip the probe — exercise the real getaddrinfo path. It will
        # fail fast (NXDOMAIN). UA + hygiene still pass.
        report = evaluate_trust(account, user_agent=_GOOD_UA)
        assert report.proxy_ok is False
        assert report.user_agent_ok is True
        assert report.hygiene_ok is True
        assert report.score == USER_AGENT_WEIGHT + HYGIENE_WEIGHT
        assert any("proxy" in reason.lower() for reason in report.reasons)
