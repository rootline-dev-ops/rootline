import pytest

from rootline.redact import Redactor, redact

SECRETS = [
    ("token: ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8", "github_token"),
    ("github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyzABCDEF", "github_pat"),
    ("ANTHROPIC=sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "anthropic_key"),
    ("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE", "aws_access_key_id"),
    ("curl -H 'Authorization: Bearer abcdefghijklmnop1234567890'", "auth_header"),
    ("git clone https://deploy:hunter2hunter2@github.com/acme/x.git", "url_credentials"),
    ("xoxb-EXAMPLE-EXAMPLE-EXAMPLE", "slack_token"),
    (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "jwt",
    ),
    ("DB_PASSWORD=Sup3rS3cretValue!", "secret_assignment"),
    ('"client_secret": "9f8e7d6c5b4a39281706"', "secret_assignment"),
]


@pytest.mark.parametrize("text,kind", SECRETS, ids=[k for _, k in SECRETS])
def test_known_secret_formats(text, kind):
    out = redact(text)
    assert f"[REDACTED:{kind}]" in out, out


def test_private_key_block():
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ\nAAAA\n-----END OPENSSH PRIVATE KEY-----"
    out = redact(f"before\n{pem}\nafter")
    assert "b3BlbnNzaC1rZXkt" not in out
    assert out.startswith("before\n[REDACTED:private_key]\nafter")


@pytest.mark.parametrize(
    "text",
    [
        "token: ***",
        "api_key: ${{ secrets.API_KEY }}",
        "password: $DB_PASSWORD",
        "Tests: 1 failed, 23 passed, 24 total",
        "error TS2345: Argument of type 'string' is not assignable",
        "tokenizer = Tokenizer()",
    ],
)
def test_non_secrets_untouched(text):
    assert redact(text) == text


def test_env_literals_and_custom_patterns():
    r = Redactor.from_env(
        {
            "MY_SERVICE_TOKEN": "zz-plain-looking-value-123",
            "HOME": "/home/runner",
            "ROOTLINE_REDACT_PATTERNS": r"ACME-\d{6}",
        }
    )
    out = r.redact("using zz-plain-looking-value-123 for ACME-123456 in /home/runner")
    assert "zz-plain-looking-value-123" not in out
    assert "ACME-123456" not in out
    assert "/home/runner" in out
    assert r.count == 2
