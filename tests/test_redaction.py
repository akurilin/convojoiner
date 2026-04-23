"""Tests for the secret redaction pipeline.

Detection fixtures are assembled at runtime from split string literals — see
tests/fixtures.py and the trailing guard test here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from redaction import (
    CUSTOM_DETECTORS,
    MULTILINE_PATTERNS,
    REDACTION_COUNTS,
    redact_secrets,
    reset_counts,
)
from tests.fixtures import (
    ALL_FAKES,
    fake_anthropic_key,
    fake_aws_access_key,
    fake_basic_auth_url,
    fake_pem_private_key_block,
)


@pytest.fixture(autouse=True)
def _clean_counts():
    reset_counts()
    yield
    reset_counts()


@pytest.mark.parametrize("label, builder, expected_slug", ALL_FAKES)
def test_detector_redacts_secret(label, builder, expected_slug):
    secret = builder()
    input_text = f"before the secret: {secret} :after the secret"
    redacted = redact_secrets(input_text)

    assert f"[REDACTED:{expected_slug}]" in redacted, (
        f"{label}: expected slug {expected_slug!r} in output {redacted!r}"
    )
    assert secret not in redacted, f"{label}: secret not redacted (still present in output)"


def test_clean_text_is_unchanged():
    text = "Just some ordinary text about Python variables and deployment."
    assert redact_secrets(text) == text
    assert REDACTION_COUNTS == {}


def test_empty_string_is_empty():
    assert redact_secrets("") == ""
    assert redact_secrets(None) is None  # type: ignore[arg-type]


def test_multiple_secrets_all_redacted():
    a = fake_anthropic_key()
    b = fake_aws_access_key()
    text = f"first {a}, then {b}, and {a} again"
    out = redact_secrets(text)

    assert "[REDACTED:anthropic-key]" in out
    assert "[REDACTED:aws-access-key]" in out
    assert a not in out
    assert b not in out
    # anthropic-key appeared twice, aws once
    assert REDACTION_COUNTS.get("anthropic-key") == 2
    assert REDACTION_COUNTS.get("aws-access-key") == 1


def test_pem_block_is_redacted_as_a_whole():
    pem = fake_pem_private_key_block()
    assert "BEGIN" in pem and "END" in pem  # sanity
    out = redact_secrets(f"prefix\n{pem}\nsuffix")
    assert "[REDACTED:private-key]" in out
    assert "BEGIN" not in out
    assert "END" not in out


def test_basic_auth_redacts_only_the_password():
    url = fake_basic_auth_url()
    out = redact_secrets(url)
    # Host and scheme preserved; password replaced
    assert "postgres://" in out
    assert "@db.example.com/app" in out
    assert "mySecret" not in out
    assert "[REDACTED:basic-auth-credentials]" in out


def test_redaction_counts_accumulate_across_calls():
    redact_secrets(f"one {fake_anthropic_key()}")
    redact_secrets(f"two {fake_anthropic_key()}")
    assert REDACTION_COUNTS.get("anthropic-key") == 2


def test_all_custom_detectors_have_fixture_coverage():
    """If someone adds a detector to CUSTOM_DETECTORS without a corresponding
    ALL_FAKES entry, the parametrized test would silently skip that detector.
    This guard makes that mistake loud."""
    covered = {slug for _, _, slug in ALL_FAKES}
    missing = [
        detector.__name__ for detector in CUSTOM_DETECTORS if detector.secret_type not in covered
    ]
    assert not missing, (
        f"Custom detectors without fixtures in ALL_FAKES: {missing}. "
        "Add a fake_* builder in tests/fixtures.py and register it in ALL_FAKES."
    )


def test_source_has_no_literal_secrets():
    """Guard: if someone simplifies a fixture into a single literal, this fires.

    We build the scanner regex set directly from the redaction module so any
    detector added later is automatically checked.
    """
    fixtures_source = (Path(__file__).parent / "fixtures.py").read_text(encoding="utf-8")

    all_patterns: list[tuple[str, re.Pattern[str]]] = []
    for label, pattern in MULTILINE_PATTERNS:
        all_patterns.append((label, pattern))
    for detector in CUSTOM_DETECTORS:
        # detect-secrets' RegexBasedDetector declares `denylist` as an
        # abstractproperty, which confuses mypy about iteration at the
        # subclass level; we know each subclass overrides it with a concrete
        # tuple of compiled patterns.
        for pat in detector.denylist:  # type: ignore[attr-defined]
            all_patterns.append((str(detector.secret_type), pat))

    # Also add a few high-signal built-in patterns whose fakes we assemble by
    # hand (so the guard covers them too).
    all_patterns.extend(
        [
            ("aws-access-key", re.compile(r"(?:A3T[A-Z0-9]|ABIA|ACCA|AKIA|ASIA)[0-9A-Z]{16}")),
            ("github-token", re.compile(r"(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36}")),
            (
                "openai-token",
                re.compile(r"sk-[A-Za-z0-9\-_]*[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}"),
            ),
            ("slack-token", re.compile(r"xox(?:a|b|p|o|s|r)-(?:\d+-)+[a-z0-9]+")),
            ("stripe-access-key", re.compile(r"(?:r|s)k_live_[0-9a-zA-Z]{24}")),
        ]
    )

    violations = []
    for label, pattern in all_patterns:
        for match in pattern.finditer(fixtures_source):
            violations.append((label, match.group(0)[:60]))

    assert not violations, (
        "fixtures.py contains literal secret-looking substrings. "
        "Split the literals so scanners don't match them: " + repr(violations)
    )
