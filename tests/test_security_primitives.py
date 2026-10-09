# tests/test_security_primitives.py
from agent.permissions import (
    ALL,
    expand,
    GROUPS,
    is_known,
    KNOWN,
    LABELS,
    Permission,
)
from agent.totp import (
    check,
    code_at,
    counter_for,
    new_secret,
    provisioning_uri,
    qr_svg,
)
from agent.config import (
    AuthTuning,
    RoleSpec,
)
from agent.passwords import PasswordHasher
from pydantic import ValidationError
from agent.vault import Vault

import base64
import pytest
import os
import re

TUNING = AuthTuning(scrypt_n=2**10, password_min_length=8)
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")


def test_every_permission_has_a_label_and_a_place_in_the_matrix():
    assert {permission.value for permission in Permission} == set(KNOWN) == set(LABELS)
    listed = [value for entries in GROUPS.values() for value, _ in entries]
    assert len(listed) == len(set(listed)) == len(KNOWN)
    assert all(is_known(name) for name in listed) and not is_known("anything.else")


def test_the_wildcard_grants_everything_and_unknown_names_grant_nothing():
    assert expand([ALL]) == KNOWN
    assert expand(["chat.use", "made.up"]) == {"chat.use"}
    assert expand("chat.use") == frozenset() and expand(None) == frozenset()


def test_the_default_roles_only_name_real_permissions():
    for spec in AuthTuning().roles.values():
        assert spec.description and expand(list(spec.permissions))
    with pytest.raises(ValidationError, match="unknown permissions"):
        RoleSpec(description="x", permissions=("chat.use", "nope"))
    with pytest.raises(ValidationError, match="is not defined"):
        AuthTuning(default_role="ghost")


def test_a_password_hash_verifies_only_the_right_password():
    hasher = PasswordHasher(TUNING)
    encoded = hasher.hash("correct horse battery")
    assert encoded.startswith("scrypt$1024$8$1$")
    assert hasher.hash("correct horse battery") != encoded
    assert hasher.verify("correct horse battery", encoded)
    assert not hasher.verify("correct horse batterz", encoded)
    assert not hasher.verify("", encoded)


def test_unicode_passwords_are_normalised():
    hasher = PasswordHasher(TUNING)
    encoded = hasher.hash("café au lait 99")
    assert hasher.verify("café au lait 99", encoded)


@pytest.mark.parametrize(
    "encoded",
    [
        None,
        "",
        "plain",
        "scrypt$1024$8$1$salt",
        "bcrypt$1024$8$1$c2FsdA==$aGFzaA==",
        "scrypt$x$8$1$c2FsdA==$aGFzaA==",
        "scrypt$1000$8$1$c2FsdA==$aGFzaA==",
        "scrypt$4294967296$8$1$c2FsdA==$aGFzaA==",
        "scrypt$1024$8$1$!!!$aGFzaA==",
    ],
)
def test_malformed_or_hostile_hashes_never_verify_and_never_crash(encoded):
    assert PasswordHasher(TUNING).verify("anything", encoded) is False


def test_overlong_passwords_are_refused_before_any_work():
    hasher = PasswordHasher(TUNING)
    encoded = hasher.hash("fine password")
    assert hasher.verify("x" * (TUNING.password_max_length + 1), encoded) is False


def test_hashes_made_with_older_costs_are_marked_for_an_upgrade():
    old = PasswordHasher(AuthTuning(scrypt_n=2**10))
    new = PasswordHasher(AuthTuning(scrypt_n=2**11))
    encoded = old.hash("some password")
    assert new.needs_rehash(encoded) and not old.needs_rehash(encoded)
    assert new.needs_rehash("garbage") and new.verify("some password", encoded)


def test_the_password_policy_explains_what_is_wrong():
    hasher = PasswordHasher(TUNING)
    assert hasher.problems("a long enough one 77") == []
    assert "at least 8" in hasher.problems("short")[0]
    assert any("too easy" in item for item in hasher.problems("password"))
    assert any("too easy" in item for item in hasher.problems("aaaaaaaaaa"))
    assert any("name or username" in item for item in hasher.problems("eva-secret-77", "Eva"))
    assert any("at most" in item for item in hasher.problems("x" * 300))


def test_unknown_accounts_cost_as_much_work_as_known_ones():
    hasher = PasswordHasher(TUNING)
    hasher.burn("whatever")
    assert hasher._dummy is not None and hasher.verify("whatever", hasher._dummy) is False
    first = hasher._dummy
    hasher.burn("again")
    assert hasher._dummy == first


def test_the_rfc_6238_test_vector_is_reproduced():
    assert code_at(RFC_SECRET, 59 // 30, 8) == "94287082"
    assert code_at(RFC_SECRET, 1111111109 // 30, 8) == "07081804"
    assert code_at(RFC_SECRET, 59 // 30, 6) == "287082"


def test_codes_are_accepted_within_the_window_and_only_once():
    tuning = AuthTuning(totp_window=1)
    now = 1_000_000.0
    counter = counter_for(now, tuning)
    code = code_at(RFC_SECRET, counter, tuning.totp_digits)
    assert check(RFC_SECRET, code, now, tuning, None) == counter
    assert check(RFC_SECRET, code, now + 30, tuning, None) == counter
    assert check(RFC_SECRET, code, now + 90, tuning, None) is None
    assert check(RFC_SECRET, code, now, tuning, counter) is None
    assert check(RFC_SECRET, f" {code[:3]} {code[3:]} ", now, tuning, None) == counter
    assert check(RFC_SECRET, "000000", now, tuning, None) is None
    assert check(RFC_SECRET, "12345", now, tuning, None) is None
    assert check(RFC_SECRET, "abcdef", now, tuning, None) is None


def test_new_secrets_are_random_base32_and_provisioning_is_standard():
    first, second = new_secret(TUNING), new_secret(TUNING)
    assert (
        first != second
        and len(first) == 32
        and set(first) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    )
    uri = provisioning_uri(first, "eva@example.com", TUNING)
    assert uri.startswith("otpauth://totp/HMNH:eva%40example.com?secret=")
    assert "&issuer=HMNH&digits=6&period=30" in uri


def test_the_qr_code_is_one_inline_svg_with_no_script_or_link():
    svg = qr_svg(provisioning_uri(new_secret(TUNING), "eva", TUNING), 4, 2)
    assert svg.startswith("<svg") and svg.endswith("</svg>") and 'class="qr"' in svg
    assert "<script" not in svg and "href" not in svg and "\n" not in svg
    assert re.search(r'viewBox="0 0 \d+ \d+"', svg)


def test_the_vault_encrypts_creates_its_key_once_and_survives_garbage(tmp_path):
    path = tmp_path / "keys" / "secret.key"
    vault = Vault(path)
    sealed = vault.encrypt("JBSWY3DPEHPK3PXP")
    assert sealed != "JBSWY3DPEHPK3PXP" and vault.decrypt(sealed) == "JBSWY3DPEHPK3PXP"
    assert Vault(path).decrypt(sealed) == "JBSWY3DPEHPK3PXP"
    assert vault.decrypt("garbage") is None and vault.decrypt("") is None
    assert vault.decrypt(None) is None
    assert Vault(tmp_path / "other.key").decrypt(sealed) is None
    assert [item.name for item in path.parent.iterdir()] == ["secret.key"]


def test_the_key_file_is_private_on_systems_that_support_it(tmp_path):
    path = tmp_path / "secret.key"
    Vault(path)
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0
    assert path.read_bytes().strip()
