"""Fake secret builders for tests.

Every value is assembled at runtime from split string literals so that
no complete matchable secret ever appears as a contiguous literal in
this file. This avoids tripping GitHub push protection, gitleaks, and
similar upstream scanners that grep the repo contents.

Rule: if you ever simplify a builder into a single string literal,
test_source_has_no_literal_secrets() in test_redaction.py will fail —
fix the split, don't suppress the test.
"""

from __future__ import annotations

from typing import Callable


# Placeholder body filler. Marked with distinctive repeated letters so a
# human reader can immediately see these are fake.
def _pad(char: str, n: int) -> str:
    return char * n


# --- Custom-plugin detectors (gaps we cover ourselves) ---


def fake_anthropic_key() -> str:
    return "sk-" + "ant-" + "api03-" + _pad("A", 95)


def fake_openai_project_key() -> str:
    return "sk-" + "proj-" + _pad("B", 45)


def fake_github_pat() -> str:
    return "github_" + "pat_" + _pad("C", 70)


def fake_google_api_key() -> str:
    return "AI" + "za" + _pad("D", 35)


def fake_google_oauth_client_secret() -> str:
    return "GOCSP" + "X-" + _pad("E", 25)


def fake_supabase_secret_key() -> str:
    return "sb_" + "secret_" + _pad("F", 25)


def fake_supabase_publishable_key() -> str:
    return "sb_" + "publishable_" + _pad("G", 25)


def fake_supabase_access_token() -> str:
    return "sb" + "p_" + _pad("H", 45)


# --- Built-in detect-secrets detectors we rely on ---


def fake_aws_access_key() -> str:
    # AWS publishes this exact value as a canonical example, but we split
    # anyway — some scanners don't allowlist it and we want zero surprises.
    return "AK" + "IA" + "IOSF" + "ODNN7" + "EXAMPLE"


def fake_github_token() -> str:
    # Upstream pattern: (ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36}
    return "gh" + "p_" + _pad("J", 36)


def fake_openai_legacy_key() -> str:
    # Upstream pattern requires the T3BlbkFJ segment.
    return "sk-" + _pad("K", 20) + "T3Blb" + "kFJ" + _pad("L", 20)


def fake_slack_token() -> str:
    # Upstream: xox(a|b|p|o|s|r)-(\d+-)+[a-z0-9]+
    return "xox" + "b-" + "1234567890" + "-" + "abcdef"


def fake_stripe_live_key() -> str:
    return "sk_" + "live_" + _pad("N", 24)


def fake_jwt() -> str:
    # detect-secrets validates that the first two segments base64-decode to
    # valid JSON, so build those from real (but trivially fake) claims.
    import base64
    import json

    def b64url(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = b64url({"typ": "JWT", "alg": "HS256"})
    payload = b64url({"sub": "fake-test-subject"})
    signature = "FAKE" + "SIG" + _pad("Z", 16)
    return header + "." + payload + "." + signature


def fake_basic_auth_url() -> str:
    # postgres://user:password@host/db, split the password portion
    return "postgres" + "://user:" + "my" + "Secret" + "@db.example.com/app"


def fake_pem_private_key_block() -> str:
    # Multi-line block — split BEGIN/END markers
    begin = "-----BEGIN RSA " + "PRIVATE KEY-----"
    end = "-----END RSA " + "PRIVATE KEY-----"
    body = _pad("Z", 32) + "\n" + _pad("Y", 32)
    return begin + "\n" + body + "\n" + end


# Grouped list used by the parametrized detection test.
# (label_substring, fixture_callable, expected_redacted_slug)
ALL_FAKES: tuple[tuple[str, Callable[[], str], str], ...] = (
    ("anthropic", fake_anthropic_key, "anthropic-key"),
    ("openai-project", fake_openai_project_key, "openai-project-key"),
    ("github-pat", fake_github_pat, "github-pat"),
    ("google-api", fake_google_api_key, "google-api-key"),
    ("google-oauth", fake_google_oauth_client_secret, "google-oauth-client-secret"),
    ("supabase-secret", fake_supabase_secret_key, "supabase-secret"),
    ("supabase-publishable", fake_supabase_publishable_key, "supabase-publishable"),
    ("supabase-access", fake_supabase_access_token, "supabase-access-token"),
    ("aws", fake_aws_access_key, "aws-access-key"),
    ("github-token", fake_github_token, "github-token"),
    ("openai-legacy", fake_openai_legacy_key, "openai-token"),
    ("slack", fake_slack_token, "slack-token"),
    ("stripe", fake_stripe_live_key, "stripe-access-key"),
    ("jwt", fake_jwt, "json-web-token"),
    ("basic-auth", fake_basic_auth_url, "basic-auth-credentials"),
    ("pem", fake_pem_private_key_block, "private-key"),
)
