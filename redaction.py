"""Redact secrets from text using detect-secrets plus a few custom detectors
for keys the upstream library doesn't cover (newer cloud/AI tokens).

Public API:
    redact_secrets(text) -> str
    REDACTION_COUNTS: dict[str, int]  (read-only in practice; reset via reset_counts)
"""

from __future__ import annotations

import re
from typing import Any, cast

from detect_secrets.core.plugins.util import get_mapping_from_secret_type_to_class
from detect_secrets.core.scan import scan_line
from detect_secrets.plugins.base import RegexBasedDetector
from detect_secrets.settings import transient_settings

# ---------- Custom detectors for gaps in detect-secrets ----------


class AnthropicKeyDetector(RegexBasedDetector):
    secret_type = "anthropic-key"
    denylist = (re.compile(r"sk-ant-[A-Za-z0-9\-_]{90,}"),)


class OpenAIProjectKeyDetector(RegexBasedDetector):
    secret_type = "openai-project-key"
    denylist = (re.compile(r"sk-proj-[A-Za-z0-9\-_]{40,}"),)


class GitHubPatDetector(RegexBasedDetector):
    """Fine-grained PAT format introduced in 2022; upstream GitHubTokenDetector
    only covers the short (ghp|gho|ghu|ghs|ghr)_ forms."""

    secret_type = "github-pat"
    denylist = (re.compile(r"github_pat_[A-Za-z0-9_]{60,}"),)


class GoogleApiKeyDetector(RegexBasedDetector):
    secret_type = "google-api-key"
    denylist = (re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),)


class GoogleOAuthClientSecretDetector(RegexBasedDetector):
    secret_type = "google-oauth-client-secret"
    denylist = (re.compile(r"GOCSPX-[A-Za-z0-9_\-]{20,}"),)


class SupabaseSecretKeyDetector(RegexBasedDetector):
    secret_type = "supabase-secret"
    denylist = (re.compile(r"\bsb_secret_[A-Za-z0-9_\-]{20,}"),)


class SupabasePublishableKeyDetector(RegexBasedDetector):
    secret_type = "supabase-publishable"
    denylist = (re.compile(r"\bsb_publishable_[A-Za-z0-9_\-]{20,}"),)


class SupabaseAccessTokenDetector(RegexBasedDetector):
    secret_type = "supabase-access-token"
    denylist = (re.compile(r"\bsbp_[A-Za-z0-9]{40,}"),)


CUSTOM_DETECTORS: tuple[type[RegexBasedDetector], ...] = (
    AnthropicKeyDetector,
    OpenAIProjectKeyDetector,
    GitHubPatDetector,
    GoogleApiKeyDetector,
    GoogleOAuthClientSecretDetector,
    SupabaseSecretKeyDetector,
    SupabasePublishableKeyDetector,
    SupabaseAccessTokenDetector,
)


# Upstream plugins we enable. Excludes entropy/keyword detectors (too noisy on
# transcript content) and the public-IP detector (IPs aren't secrets).
BUILTIN_PLUGINS: tuple[str, ...] = (
    "AWSKeyDetector",
    "ArtifactoryDetector",
    "AzureStorageKeyDetector",
    "BasicAuthDetector",
    "CloudantDetector",
    "DiscordBotTokenDetector",
    "GitHubTokenDetector",
    "GitLabTokenDetector",
    "IbmCloudIamDetector",
    "IbmCosHmacDetector",
    "JwtTokenDetector",
    "MailchimpDetector",
    "NpmDetector",
    "OpenAIDetector",
    "PrivateKeyDetector",
    "PypiTokenDetector",
    "SendGridDetector",
    "SlackDetector",
    "SoftlayerDetector",
    "SquareOAuthDetector",
    "StripeDetector",
    "TelegramBotTokenDetector",
    "TwilioKeyDetector",
)


# ---------- Multi-line patterns (PEM blocks) ----------
# detect-secrets' PrivateKeyDetector only matches the BEGIN line; to redact
# the entire block we preprocess with a multi-line regex first.

MULTILINE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private-key",
        re.compile(
            r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"
            r"[\s\S]*?"
            r"-----END (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"
        ),
    ),
)


# ---------- Public state ----------

REDACTION_COUNTS: dict[str, int] = {}


def reset_counts() -> None:
    REDACTION_COUNTS.clear()


# ---------- Setup ----------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(label: str) -> str:
    return _SLUG_RE.sub("-", label.lower()).strip("-")


def _register_custom_detectors() -> None:
    # detect-secrets' abstract-property annotations confuse mypy about the
    # mapping's value type; cast to Any for the writes.
    mapping = cast(dict[str, Any], get_mapping_from_secret_type_to_class())
    for detector in CUSTOM_DETECTORS:
        mapping[str(detector.secret_type)] = detector


def _plugins_config() -> list[dict[str, str]]:
    names = list(BUILTIN_PLUGINS) + [d.__name__ for d in CUSTOM_DETECTORS]
    return [{"name": name} for name in names]


_register_custom_detectors()
_PLUGINS_USED = _plugins_config()


# ---------- Redaction ----------


def redact_secrets(text: str) -> str:
    if not text:
        return text

    for label, pattern in MULTILINE_PATTERNS:
        text, count = pattern.subn(f"[REDACTED:{label}]", text)
        if count:
            REDACTION_COUNTS[label] = REDACTION_COUNTS.get(label, 0) + count

    with transient_settings({"plugins_used": _PLUGINS_USED}):
        findings: dict[str, str] = {}
        for line in text.splitlines():
            for secret in scan_line(line):
                if secret.secret_value and secret.secret_value not in findings:
                    findings[secret.secret_value] = _slugify(secret.type)

    for value, slug in findings.items():
        occurrences = text.count(value)
        if not occurrences:
            continue
        text = text.replace(value, f"[REDACTED:{slug}]")
        REDACTION_COUNTS[slug] = REDACTION_COUNTS.get(slug, 0) + occurrences

    return text
