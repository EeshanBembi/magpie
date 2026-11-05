from __future__ import annotations

import pytest

from magpie.redact import luhn_valid, redact


def test_luhn_validation():
    assert luhn_valid("4111111111111111") is True
    assert luhn_valid("4111 1111 1111 1111") is True
    assert luhn_valid("4111111111111112") is False
    assert luhn_valid("1234") is False


def test_credit_card_valid_is_redacted():
    text, counts = redact("Card on file: 4111 1111 1111 1111, thanks.")
    assert "4111" not in text
    assert "[REDACTED:credit_card]" in text
    assert counts == {"credit_card": 1}


def test_credit_card_with_dashes_is_redacted():
    text, counts = redact("4111-1111-1111-1111")
    assert text == "[REDACTED:credit_card]"
    assert counts["credit_card"] == 1


def test_invalid_luhn_is_left_alone():
    original = "Order 4111 1111 1111 1112 shipped"
    text, counts = redact(original)
    assert text == original
    assert counts == {}


@pytest.mark.parametrize(
    ("secret", "kind"),
    [
        ("AKIAIOSFODNN7EXAMPLE", "aws_key"),
        ("sk-ant-api03-" + "A" * 40, "anthropic_key"),
        ("sk-proj-" + "b" * 40, "openai_key"),
        ("ghp_" + "c" * 36, "github_token"),
        ("xoxb-1234567890-abcdefghij", "slack_token"),
        (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
            "jwt",
        ),
        ("123-45-6789", "ssn"),
    ],
)
def test_each_token_kind_is_redacted(secret, kind):
    text, counts = redact(f"value: {secret} end")
    assert secret not in text
    assert f"[REDACTED:{kind}]" in text
    assert counts.get(kind) == 1


def test_private_key_block_is_redacted():
    block = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA1234567890abcdef\n"
        "abcdefghijklmnopqrstuvwxyz\n"
        "-----END RSA PRIVATE KEY-----"
    )
    text, counts = redact(f"before\n{block}\nafter")
    assert "MIIEow" not in text
    assert text.startswith("before\n[REDACTED:private_key]\nafter")
    assert counts == {"private_key": 1}


def test_password_lines_are_redacted():
    text, counts = redact(
        "user: sam\npassword: hunter2\nPassword = s3cret!\nunrelated: value"
    )
    assert "hunter2" not in text
    assert "s3cret!" not in text
    assert "password: [REDACTED:password]" in text
    assert "Password = [REDACTED:password]" in text
    assert "unrelated: value" in text
    assert counts == {"password": 2}


def test_multiple_occurrences_counted():
    _, counts = redact(
        "AKIAIOSFODNN7EXAMPLE and AKIAIOSFODNN7EXAMPLE and 123-45-6789"
    )
    assert counts["aws_key"] == 2
    assert counts["ssn"] == 1


def test_empty_and_clean_text():
    assert redact("") == ("", {})
    text, counts = redact("Nothing secret here at all.")
    assert text == "Nothing secret here at all."
    assert counts == {}


@pytest.mark.parametrize(
    "secret",
    [
        "sk_live_" + "a" * 24,
        "rk_live_" + "b" * 24,
        "sk_test_" + "c" * 24,
    ],
)
def test_stripe_keys_are_redacted(secret):
    text, counts = redact(f"stripe: {secret} end")
    assert secret not in text
    assert "[REDACTED:stripe_key]" in text
    assert counts == {"stripe_key": 1}


def test_oc_sk_tokens_are_redacted():
    secret = "oc_sk_test_" + "d" * 24
    text, counts = redact(f"token={secret}")
    assert secret not in text
    assert text == "token=[REDACTED:oc_token]"
    assert counts == {"oc_token": 1}


@pytest.mark.parametrize(
    "header",
    [
        "Authorization: Bearer abc123DEF456ghi789",
        "authorization: bearer token-with.dots_and/slashes==",
        "AUTHORIZATION:   Bearer   0123456789abcdef",
    ],
)
def test_bearer_tokens_are_redacted(header):
    text, counts = redact(f"{header}\nnext line")
    assert "abc123DEF456ghi789" not in text
    assert "token-with.dots_and/slashes==" not in text
    assert "0123456789abcdef" not in text
    assert "[REDACTED:bearer_token]" in text
    assert "next line" in text
    assert counts == {"bearer_token": 1}


def test_bearer_keeps_header_and_is_not_double_redacted():
    text, counts = redact("Authorization: Bearer [REDACTED:bearer_token]")
    assert text == "Authorization: Bearer [REDACTED:bearer_token]"
    assert counts == {}


def test_api_key_inside_password_line_is_not_double_redacted():
    secret = "sk-proj-" + "d" * 40
    text, counts = redact(f"password: {secret}")
    assert secret not in text
    assert counts.get("openai_key") == 1
    assert counts.get("password") is None
    assert "[REDACTED:openai_key]" in text


def test_ocr_split_stripe_key_is_redacted():
    text, counts = redact("My key is sk_live_51 Habcdefghijk/mnopqrstuv")
    assert text == "My key is [REDACTED:stripe_key]"
    assert counts == {"stripe_key": 1}


def test_ocr_split_tokens_are_redacted_up_to_two_following_tokens():
    text, counts = redact("AKIA Ab1c23d4 Ef56gh78 Xy90zw12")
    assert text == "[REDACTED:aws_key] Xy90zw12"
    assert counts == {"aws_key": 1}


def test_ocr_tolerant_redaction_does_not_eat_common_words():
    text, counts = redact("sk- please update the value")
    assert text == "[REDACTED:openai_key] please update the value"
    assert counts == {"openai_key": 1}


@pytest.mark.parametrize(
    ("prefix", "kind"),
    [
        ("sk-ant-", "anthropic_key"),
        ("sk_live_", "stripe_key"),
        ("sk_test_", "stripe_key"),
        ("rk_live_", "stripe_key"),
        ("oc_sk_", "oc_token"),
        ("ghp_", "github_token"),
        ("gho_", "github_token"),
        ("ghu_", "github_token"),
        ("ghs_", "github_token"),
        ("ghr_", "github_token"),
        ("xoxb-", "slack_token"),
        ("AKIA", "aws_key"),
        ("sk-", "openai_key"),
    ],
)
def test_ocr_tolerant_each_prefix_is_redacted(prefix, kind):
    text, counts = redact(f"token {prefix}Ab1c23d4 rest")
    assert f"{prefix}Ab1c23d4" not in text
    assert f"[REDACTED:{kind}]" in text
    assert counts.get(kind) == 1


def test_ocr_tolerant_leaves_random_tokens_alone_without_a_prefix():
    original = "Branch names: feature/ABC-123 and release_2025_01"
    text, counts = redact(original)
    assert text == original
    assert counts == {}
