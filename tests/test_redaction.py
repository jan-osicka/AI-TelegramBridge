import pytest

from telegram_bridge import fmt, redaction
from telegram_bridge.redaction import MASK, redact
from telegram_bridge.textutil import split_message

TG = "123456789:" + "A" * 35
SECRETS = [
    f"token {TG} end",
    "key sk-ant-api03-abcdefGHIJ123456",
    "gh ghp_" + "a1" * 18,
    "pat github_pat_11ABCDEFG0abcdefgh_xyz123",
    "slack xoxb-1234-5678-abcdefgh",
    "google AIzaSyA1234567890abcdefghij",
    "aws AKIAABCDEFGHIJKLMNOP",
    "Authorization: Bearer abc.def",
    "password=hunter22",
    '{"api_key": "zzz"}',
    "run --token abc123xyz now",
    "see https://x.com/a?key=1&sig=2 ok",
]


@pytest.mark.parametrize("s", SECRETS)
def test_masks(s):
    out, n = redact(s)
    assert MASK in out and n >= 1
    for bad in (TG, "abcdefGHIJ123456", "hunter22", "zzz", "correcthorse", "hunter2", "mysecret", "abc123xyz", "sig=2", "abc.def"):
        assert bad not in out


def test_url_query_masked_path_kept():
    out, n = redact("https://x.com/a?key=1&sig=2")
    assert out == f"https://x.com/a?{MASK}" and n == 1


@pytest.mark.parametrize("s", [
    "commit " + "a1b2c3d4e5" * 4,
    "id 123e4567-e89b-12d3-a456-426614174000",
    "Heslo je v trezoru.",
    "token_count = 1500",
    r"open C:\work\tokens.md",
    "https://x.com/a?page=2&q=hello",
    "time 12:34:56",
])
def test_not_masked(s):
    assert redact(s) == (s, 0)


def test_known_value_and_count():
    redaction.set_known(["sekret-hodnota-123", "short"])
    try:
        out, n = redact("a sekret-hodnota-123 b sekret-hodnota-123 short")
        assert out == f"a {MASK} b {MASK} short" and n == 2
    finally:
        redaction.set_known([])
    out, n = redact("x zzzzzzzzzz y", known=["zzzzzzzzzz"])
    assert out == f"x {MASK} y" and n == 1


def test_count_multiple():
    assert redact(f"password=abc12345 {TG} Bearer xyz")[1] == 3


def test_idempotent():
    once, _ = redact("password=hunter22")
    assert redact(once) == (once, 0)


def test_fence_survives_fmt():
    out, n = redact("```\npassword=hunter22\nx = 1\n```")
    assert n == 1
    html, _ = fmt.render_chunks(out)[0]
    assert "<pre>" in html and "hunter22" not in html and MASK in html and html.count("</pre>") == 1


def test_mask_not_split():
    text = "x" * 4090 + " " + TG + " " + "y" * 900
    out, n = redact(text)
    assert n == 1
    for chunk in split_message(out):
        assert TG[:12] not in chunk and "AAAAAAAA" not in chunk
    assert "".join(split_message(out)).count(MASK) == 1


@pytest.mark.parametrize("s", [
    "https://github.com/a/b#readme",
    "https://x.com/a#section",
    "https://x.com/search?q=python&page=2",
    "sorted(x, key=len)",
    "{key: value}",
    "pass: ok",
    "password: str",
    "def f(token: str, secret=None): pass",
])
def test_more_not_masked(s):
    assert redact(s) == (s, 0)


@pytest.mark.parametrize("s", [
    "https://x.com/cb?access_token=abc123def456",
    "https://x.com/cb#access_token=abc123def456",
    "https://x.com/cb?code=xyz789",
])
def test_url_sensitive_masked(s):
    out, n = redact(s)
    assert n == 1 and MASK in out and "abc123def456" not in out and "xyz789" not in out
    assert out.startswith("https://x.com/cb")


SEED = ("abandon ability able about above absent absorb abstract absurd abuse access accident "
        "account accuse achieve acid acoustic acquire across act action actor actress actual")


@pytest.mark.parametrize("s", [
    "my seed: " + " ".join(SEED.split()[:12]) + " keep it safe",
    "Seed: " + SEED,
    "\n".join(f"{i}. {w}" for i, w in enumerate(SEED.split()[:12], 1)),
    '["' + '", "'.join(SEED.upper().split()[:12]) + '"]',
])
def test_masks_seed_phrase(s):
    out, n = redact(s)
    assert n == 1 and MASK in out
    assert not any(w in out.lower() for w in ("abandon", "absurd", "accident"))


@pytest.mark.parametrize("s", [
    " ".join(SEED.split()[:11]),   # 11 words: below the limit
    "I want to build a simple bridge so my phone can reach the local agent from outside the house.",
    "Abandon ability able about above absent, then the rest: absorb abstract absurd abuse access accident.",
])
def test_seed_no_false_positive(s):
    assert redact(s) == (s, 0)


@pytest.mark.parametrize("s", [
    "[" + ",".join(["false"] * 20) + "]",
    "where: top-left, top, top-right, bottom-left, bottom, bottom-right, left, right, top, bottom, left, right",
    "use_index_order=use_index_order, use_field_number=use_field_number, descriptor_pool=descriptor_pool",
    "Please remove this job Please update this job Please promote this job Wrong location Wrong category",
])
def test_seed_no_false_positive_code(s):
    assert redact(s) == (s, 0)


def test_seed_gap_no_backtracking():
    # A long digit run between two wordlist words must not make the gap regex backtrack.
    s = "abandon " + "1" * 60 + "! ability"
    assert redact(s) == (s, 0)
