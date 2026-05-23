"""
tests for the trust score engine.
proxy probing is disabled here to run faster.
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
    """creates a fake instagram account for testing trust."""
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


# negative tests
class TestTrustFailures:
    def test_missing_proxy_drops_score_below_threshold(self):
        account = _make_account(with_proxy=False)
        report = evaluate_trust(account, user_agent=_GOOD_UA, skip_proxy_probe=True)

        assert report.proxy_ok is False
        assert report.score == USER_AGENT_WEIGHT + HYGIENE_WEIGHT  # 25 + 15 = 40
        assert report.score < 50  # below production threshold (independent of .env)
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
        """fails if there is no proxy."""
        account = _make_account(with_proxy=False)
        report = evaluate_trust(account, user_agent=_GOOD_UA, skip_proxy_probe=True)
        assert report.passed is False, "proxy must be necessary, not optional"

    def test_to_skip_reason_includes_low_trust_score_token(self):
        """skip reason includes low trust score token."""
        account = _make_account(with_proxy=False)
        report = evaluate_trust(account, user_agent=_GOOD_UA, skip_proxy_probe=True)
        skip_reason = report.to_skip_reason()
        assert skip_reason.startswith("low_trust_score")
        assert f"score={report.score}/100" in skip_reason


# positive tests
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
        """normal tags do not lower trust."""
        account = _make_account(tags=["crypto", "tier1", "fitness"])
        report = evaluate_trust(account, user_agent=_GOOD_UA, skip_proxy_probe=True)
        assert report.hygiene_ok is True
        assert report.score == 100


# edge cases
class TestTrustEdges:
    def test_evaluator_crash_is_handled_in_report_form(self):
        """catches errors in the proxy probe."""
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
