"""Redact secrets from captured screen text before it is stored.

``redact(text)`` returns ``(clean_text, counts_by_kind)``.  Every match is
replaced with ``[REDACTED:<kind>]`` so the UI can show that something was
removed without keeping the original value.
"""

from __future__ import annotations

import re
from typing import Callable

KINDS = (
    "private_key",
    "bearer_token",
    "jwt",
    "aws_key",
    "stripe_key",
    "anthropic_key",
    "openai_key",
    "oc_token",
    "github_token",
    "slack_token",
    "credit_card",
    "ssn",
    "password",
)

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----"
)
_BEARER_RE = re.compile(
    r"(?im)(\bauthorization[ \t]*:[ \t]*bearer[ \t]+)"
    r"(?!\[REDACTED:)([^\s]+)"
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")
_AWS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_STRIPE_KEY_RE = re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{8,}\b")
_ANTHROPIC_KEY_RE = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")
_OC_TOKEN_RE = re.compile(r"\boc_sk_[A-Za-z0-9._-]{8,}\b")
_GITHUB_TOKEN_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")
_SLACK_TOKEN_RE = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")
_CREDIT_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_SSN_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_PASSWORD_RE = re.compile(
    r"(?im)^([^\n:=\r]*\bpassword\b[^\n:=\r]*[:=][ \t]*)"
    r"(?!\[REDACTED:)(\S[^\n\r]*)$"
)

_OCR_KEY_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<prefix>"
    r"sk-ant-"
    r"|(?:sk|rk)_(?:live|test)_"
    r"|oc_sk_"
    r"|gh[pousr]_"
    r"|xox[baprs]-"
    r"|AKIA"
    r"|sk-"
    r")"
)

_OCR_KEY_KINDS = (
    ("sk-ant-", "anthropic_key"),
    ("sk_live_", "stripe_key"),
    ("sk_test_", "stripe_key"),
    ("rk_live_", "stripe_key"),
    ("rk_test_", "stripe_key"),
    ("oc_sk_", "oc_token"),
    ("ghp_", "github_token"),
    ("gho_", "github_token"),
    ("ghu_", "github_token"),
    ("ghs_", "github_token"),
    ("ghr_", "github_token"),
    ("xoxb-", "slack_token"),
    ("xoxa-", "slack_token"),
    ("xoxp-", "slack_token"),
    ("xoxr-", "slack_token"),
    ("xoxs-", "slack_token"),
    ("AKIA", "aws_key"),
    ("sk-", "openai_key"),
)

_COMMON_WORDS = frozenset(
    """
    about above after again against almost always another answer around because
    before being below between cannot could during every follow found further
    given great group having hello however important large later little making
    might more most much must never number often other place please point
    problem public question really right same school second should since small
    something state still such system their them then there these they thing
    think this those though through time today together under until very want
    water well were what when where which while will with within without work
    world would write year your yours known looking using update review design
    thanks email notes reply message meeting followup tomorrow morning
    afternoon evening window screen document project customer support issue
    error value string return import export class function today yesterday
    """.split()
)


def _kind_for_prefix(prefix: str) -> str:
    for candidate, kind in _OCR_KEY_KINDS:
        if prefix.startswith(candidate):
            return kind
    return "openai_key"


def _looks_random(token: str) -> bool:
    token = (token or "").strip(".,;:!?()[]{}\"'")
    if len(token) < 6:
        return False
    lowered = token.lower()
    if lowered in _COMMON_WORDS:
        return False
    has_letter = any(char.isalpha() for char in token)
    has_digit = any(char.isdigit() for char in token)
    has_separator = any(char in "/_-" for char in token)
    if not (has_letter and has_digit) and not has_separator:
        return False
    parts = [part for part in re.split(r"[/_-]+", token) if part]
    if parts and all(part.lower() in _COMMON_WORDS for part in parts):
        return False
    return True


def _redact_ocr_keys(text: str, counts: dict[str, int]) -> str:
    pieces: list[str] = []
    position = 0
    for match in _OCR_KEY_PREFIX_RE.finditer(text):
        if match.start() < position:
            continue
        kind = _kind_for_prefix(match.group("prefix"))
        end = match.end()
        while end < len(text) and not text[end].isspace():
            end += 1
        consumed = end
        extra = 0
        while extra < 2:
            scan = consumed
            while scan < len(text) and text[scan] in " \t":
                scan += 1
            if scan == consumed:
                break
            token_start = scan
            while scan < len(text) and not text[scan].isspace():
                scan += 1
            if not _looks_random(text[token_start:scan]):
                break
            consumed = scan
            extra += 1
        pieces.append(text[position : match.start()])
        pieces.append(f"[REDACTED:{kind}]")
        counts[kind] = counts.get(kind, 0) + 1
        position = consumed
    if not pieces:
        return text
    pieces.append(text[position:])
    return "".join(pieces)


def luhn_valid(number: str) -> bool:
    """Return True when ``number`` is a Luhn-valid 13-19 digit card number."""

    digits = [int(char) for char in number if char.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def _apply(
    text: str,
    kind: str,
    pattern: re.Pattern[str],
    counts: dict[str, int],
    validator: Callable[[str], bool] | None = None,
) -> str:
    def replace(match: re.Match[str]) -> str:
        original = match.group(0)
        if validator is not None and not validator(original):
            return original
        counts[kind] = counts.get(kind, 0) + 1
        return f"[REDACTED:{kind}]"

    return pattern.sub(replace, text)


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Remove known secret formats from ``text``.

    Returns the cleaned text and a mapping of kind to replacement count.  Kinds
    with no matches are omitted.  The function is safe to call on arbitrary
    text (empty, huge, or containing malformed tokens).
    """

    if not text:
        return text or "", {}

    counts: dict[str, int] = {}
    cleaned = text

    cleaned = _apply(cleaned, "private_key", _PRIVATE_KEY_RE, counts)

    def replace_bearer(match: re.Match[str]) -> str:
        counts["bearer_token"] = counts.get("bearer_token", 0) + 1
        return f"{match.group(1)}[REDACTED:bearer_token]"

    cleaned = _BEARER_RE.sub(replace_bearer, cleaned)
    cleaned = _apply(cleaned, "jwt", _JWT_RE, counts)
    cleaned = _apply(cleaned, "aws_key", _AWS_KEY_RE, counts)
    cleaned = _apply(cleaned, "stripe_key", _STRIPE_KEY_RE, counts)
    cleaned = _apply(cleaned, "anthropic_key", _ANTHROPIC_KEY_RE, counts)
    cleaned = _apply(cleaned, "openai_key", _OPENAI_KEY_RE, counts)
    cleaned = _apply(cleaned, "oc_token", _OC_TOKEN_RE, counts)
    cleaned = _apply(cleaned, "github_token", _GITHUB_TOKEN_RE, counts)
    cleaned = _apply(cleaned, "slack_token", _SLACK_TOKEN_RE, counts)
    cleaned = _redact_ocr_keys(cleaned, counts)
    cleaned = _apply(
        cleaned,
        "credit_card",
        _CREDIT_CARD_RE,
        counts,
        validator=luhn_valid,
    )
    cleaned = _apply(cleaned, "ssn", _SSN_RE, counts)

    def replace_password(match: re.Match[str]) -> str:
        counts["password"] = counts.get("password", 0) + 1
        return f"{match.group(1)}[REDACTED:password]"

    cleaned = _PASSWORD_RE.sub(replace_password, cleaned)
    return cleaned, counts
