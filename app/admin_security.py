import hashlib
import hmac
import secrets


def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1, maxmem=128 * 1024 * 1024)
    return f"scrypt${salt.hex()}${digest.hex()}"


def password_matches(password, encoded):
    if not encoded:
        return False
    try:
        method, salt, expected = encoded.split("$")
        if method != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=1, maxmem=128 * 1024 * 1024)
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False
