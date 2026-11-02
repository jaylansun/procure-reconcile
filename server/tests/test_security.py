import pytest

from app.errors import AppError
from app.security.passwords import hash_password, new_session_token, token_digest, verify_password
from app.security.rate_limit import LoginRateLimiter


def test_password_hash_is_argon2_and_rejects_incorrect_password():
    password = new_session_token()
    hashed = hash_password(password)
    assert hashed.startswith("$argon2id$")
    assert password not in hashed
    assert verify_password(password, hashed)
    assert not verify_password(password + "incorrect", hashed)
    assert not verify_password(password, "corrupt-hash")


def test_six_digit_administrator_password_is_hashed_and_verified():
    # An explicit test fixture, never a default account password.
    password = "042619"
    hashed = hash_password(password)
    assert hashed.startswith("$argon2id$")
    assert verify_password(password, hashed)
    assert not verify_password("042618", hashed)


@pytest.mark.parametrize("password", ["", "70421", "x" * 129])
def test_administrator_password_requires_bounded_length(password):
    with pytest.raises(ValueError):
        hash_password(password)


def test_failed_login_window_expires_and_is_per_peer(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("app.security.rate_limit.time.monotonic", lambda: clock[0])
    limiter = LoginRateLimiter(attempts=2, window_seconds=30)
    limiter.failed("peer-a")
    limiter.failed("peer-a")
    with pytest.raises(AppError) as error:
        limiter.check("peer-a")
    assert error.value.status == 429
    limiter.check("peer-b")
    clock[0] += 31
    limiter.check("peer-a")


def test_tokens_are_random_and_only_have_stable_one_way_digests():
    first, second = new_session_token(), new_session_token()
    assert first != second
    assert len(first) >= 43
    assert len(token_digest(first)) == 64
    assert token_digest(first) == token_digest(first)
    assert first != token_digest(first)
