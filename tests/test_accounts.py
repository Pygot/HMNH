# tests/test_accounts.py
from agent.accounts import (
    Accounts,
    ACTIVE,
    DISABLED,
    INVITE,
    INVITED,
    LastAdminError,
    LOGIN,
    owner_principal,
    RESET,
)
from agent.config import (
    AuthTuning,
    StoreTuning,
)
from agent.errors import InvalidRequest
from agent.permissions import KNOWN
from agent.db import Database

import pytest


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make(tmp_path, clock):
    opened = []

    def build(**changes):
        database = Database(StoreTuning(path=tmp_path / "data" / "agent.db"), clock)
        tuning = AuthTuning(**changes)
        accounts = Accounts(database, tuning, clock)
        accounts.sync_roles(tuning.roles)
        opened.append(database)
        return accounts

    yield build
    for database in opened:
        database.close()


@pytest.fixture
def accounts(make):
    return make()


def role_id(accounts, name):
    return accounts.role_named(name).id


def admin(accounts, name="root"):
    return accounts.create_user(name, role_id(accounts, "admin"))


def test_the_default_roles_are_created_once_and_admin_can_do_everything(accounts):
    names = [role.name for role in accounts.roles()]
    assert {"admin", "manager", "recruiter", "viewer", "finance", "service"} == set(names)
    accounts.sync_roles(AuthTuning().roles)
    assert len(accounts.roles()) == len(names)
    assert accounts.role_named("admin").effective() == KNOWN
    assert accounts.role_named("viewer").effective() < KNOWN
    assert all(role.builtin for role in accounts.roles())


def test_changes_to_a_default_role_survive_a_restart(accounts):
    viewer = accounts.role_named("viewer")
    accounts.save_role("viewer", "Looks only", ["reports.view"], viewer.id)
    accounts.sync_roles(AuthTuning().roles)
    assert accounts.role(viewer.id).permissions == ["reports.view"]


def test_custom_roles_are_validated_renamed_and_removed(accounts):
    custom = accounts.save_role(
        "  Sourcer  ", "Finds people", ["chat.use", "chat.use", "reports.view"]
    )
    assert custom.name == "Sourcer" and custom.permissions == ["chat.use", "reports.view"]
    assert not custom.builtin
    with pytest.raises(InvalidRequest, match="already exists"):
        accounts.save_role("sourcer", "", [])
    with pytest.raises(InvalidRequest, match="do not exist"):
        accounts.save_role("Other", "", ["made.up"])
    with pytest.raises(InvalidRequest, match="2 to 40"):
        accounts.save_role("x", "", [])
    renamed = accounts.save_role("Sourcing", "", ["chat.use"], custom.id)
    assert renamed.id == custom.id and renamed.permissions == ["chat.use"]
    with pytest.raises(InvalidRequest, match="does not exist"):
        accounts.save_role("Ghost", "", [], "missing")
    accounts.delete_role(custom.id)
    assert accounts.role(custom.id) is None
    with pytest.raises(InvalidRequest, match="does not exist"):
        accounts.delete_role(custom.id)


def test_built_in_roles_and_roles_in_use_cannot_be_removed(accounts):
    with pytest.raises(InvalidRequest, match="Built-in"):
        accounts.delete_role(role_id(accounts, "viewer"))
    custom = accounts.save_role("Temp", "", ["chat.use"])
    admin(accounts)
    accounts.create_user("tom", custom.id)
    with pytest.raises(InvalidRequest, match="Move the accounts"):
        accounts.delete_role(custom.id)


def test_the_number_of_roles_is_limited(make):
    accounts = make(max_roles=7)
    accounts.save_role("One", "", [])
    with pytest.raises(InvalidRequest, match="too many roles"):
        accounts.save_role("Two", "", [])


def test_accounts_are_created_with_clean_identity_and_a_unique_name(accounts):
    root = admin(accounts)
    assert accounts.needs_setup() is False and accounts.count_users() == 1
    assert root.role_name == "admin" and root.status == ACTIVE and not root.has_password
    eva = accounts.create_user(
        "Eva.S",
        role_id(accounts, "recruiter"),
        display_name="  Eva   Svobodova ",
        email="eva@example.com",
    )
    assert eva.label == "Eva Svobodova" and eva.email == "eva@example.com"
    assert accounts.user_named("eva.s").id == eva.id
    assert accounts.user_with_email("EVA@example.com").id == eva.id
    with pytest.raises(InvalidRequest, match="already taken"):
        accounts.create_user("EVA.s", role_id(accounts, "viewer"))
    for bad in ("", "a", "-bad", "has space", "x" * 80, "semi;colon"):
        with pytest.raises(InvalidRequest, match="username"):
            accounts.create_user(bad, role_id(accounts, "viewer"))
    with pytest.raises(InvalidRequest, match="email"):
        accounts.create_user("tom", role_id(accounts, "viewer"), email="not-an-email")
    with pytest.raises(InvalidRequest, match="does not exist"):
        accounts.create_user("tom", "missing")
    with pytest.raises(InvalidRequest, match="status"):
        accounts.create_user("tom", role_id(accounts, "viewer"), status="odd")


def test_a_fresh_install_needs_setup_until_the_first_account_exists(accounts):
    assert accounts.needs_setup() is True
    admin(accounts)
    assert accounts.needs_setup() is False


def test_the_number_of_accounts_is_limited(make):
    accounts = make(max_users=1)
    admin(accounts)
    with pytest.raises(InvalidRequest, match="too many accounts"):
        accounts.create_user("second", role_id(accounts, "viewer"))


def test_the_last_administrator_cannot_be_removed_demoted_or_disabled(accounts):
    root = admin(accounts)
    viewer = role_id(accounts, "viewer")
    with pytest.raises(LastAdminError):
        accounts.update_user(root.id, role_id=viewer)
    with pytest.raises(LastAdminError):
        accounts.update_user(root.id, status=DISABLED)
    with pytest.raises(LastAdminError):
        accounts.delete_user(root.id)
    with pytest.raises(LastAdminError):
        accounts.save_role("admin", "", ["chat.use"], role_id(accounts, "admin"))
    assert accounts.user(root.id).status == ACTIVE and accounts.user(root.id).role_name == "admin"
    second = admin(accounts, "second")
    accounts.update_user(root.id, role_id=viewer)
    with pytest.raises(LastAdminError):
        accounts.delete_user(second.id)


def test_changing_a_role_or_status_invalidates_sessions_but_a_note_does_not(accounts):
    root = admin(accounts)
    eva = accounts.create_user("eva", role_id(accounts, "recruiter"))
    before = accounts.principal_for(eva.id)
    assert before.role == "recruiter" and before.can("chat.use") and not before.can("users.manage")
    accounts.update_user(eva.id, display_name="Eva S")
    assert accounts.principal_for(eva.id, before.version) is not None
    accounts.update_user(eva.id, role_id=role_id(accounts, "viewer"))
    assert accounts.principal_for(eva.id, before.version) is None
    after = accounts.principal_for(eva.id)
    assert after.role == "viewer" and not after.can("research.run") and after.name == "Eva S"
    accounts.update_user(eva.id, status=DISABLED)
    assert accounts.principal_for(eva.id) is None
    assert accounts.principal_for("missing") is None
    assert accounts.user(root.id).token_version == 0


def test_editing_a_role_takes_effect_for_its_accounts_at_once(accounts):
    admin(accounts)
    custom = accounts.save_role("Temp", "", ["chat.use", "reports.view"])
    eva = accounts.create_user("eva", custom.id)
    session = accounts.principal_for(eva.id)
    assert session.can("reports.view")
    accounts.save_role("Temp", "", ["chat.use"], custom.id)
    assert accounts.principal_for(eva.id, session.version) is None
    assert not accounts.principal_for(eva.id).can("reports.view")


def test_email_changes_reset_the_verification(accounts):
    admin(accounts)
    eva = accounts.create_user("eva", role_id(accounts, "viewer"), email="eva@example.com")
    accounts.mark_email_verified(eva.id)
    assert accounts.user(eva.id).email_verified
    accounts.update_user(eva.id, email="eva@example.com")
    assert accounts.user(eva.id).email_verified
    accounts.update_user(eva.id, email="new@example.com")
    assert not accounts.user(eva.id).email_verified
    accounts.update_user(eva.id, clear_email=True)
    assert accounts.user(eva.id).email is None
    with pytest.raises(InvalidRequest, match="email"):
        accounts.update_user(eva.id, email="nope")
    with pytest.raises(InvalidRequest, match="does not exist"):
        accounts.update_user("missing", display_name="x")


def test_setting_a_password_activates_an_invited_account_and_ends_old_sessions(accounts):
    admin(accounts)
    eva = accounts.create_user("eva", role_id(accounts, "viewer"), status=INVITED)
    assert not eva.has_password and eva.status == INVITED
    accounts.set_password(eva.id, "hash-1")
    after = accounts.user(eva.id)
    assert after.has_password and after.status == ACTIVE and after.token_version == 1
    assert accounts.secrets_of(eva.id).password_hash == "hash-1"
    accounts.upgrade_hash(eva.id, "hash-2", "hash-1")
    accounts.upgrade_hash(eva.id, "hash-3", "hash-1")
    assert accounts.secrets_of(eva.id).password_hash == "hash-2"
    assert accounts.user(eva.id).token_version == 1
    accounts.set_password(eva.id, "hash-4", must_change=True)
    assert accounts.user(eva.id).must_change_password
    with pytest.raises(InvalidRequest):
        accounts.set_password("missing", "x")


def test_logins_are_noted(accounts, clock):
    root = admin(accounts)
    assert accounts.user(root.id).last_login is None
    accounts.note_login(root.id)
    assert accounts.user(root.id).last_login == clock.now


def test_totp_needs_confirmation_cannot_be_replayed_and_has_one_use_recovery_codes(accounts):
    root = admin(accounts)
    accounts.begin_totp(root.id, "sealed")
    assert accounts.user(root.id).totp_enabled is False
    assert accounts.enable_totp(root.id, ["AAAA-1", "BBBB-2"], 100) is True
    assert accounts.enable_totp(root.id, ["x"], 1) is False
    user = accounts.user(root.id)
    assert user.totp_enabled and accounts.secrets_of(root.id).totp_secret == "sealed"
    assert accounts.use_counter(root.id, 101) is True
    assert accounts.use_counter(root.id, 101) is False
    assert accounts.use_counter(root.id, 100) is False
    assert accounts.recovery_remaining(root.id) == 2
    assert accounts.use_recovery_code(root.id, "aaaa-1") is True
    assert accounts.use_recovery_code(root.id, "AAAA-1") is False
    assert accounts.use_recovery_code(root.id, "wrong") is False
    assert accounts.recovery_remaining(root.id) == 1
    accounts.disable_totp(root.id)
    after = accounts.user(root.id)
    assert not after.totp_enabled and accounts.secrets_of(root.id).totp_secret is None
    assert accounts.recovery_remaining(root.id) == 0
    assert accounts.use_recovery_code(root.id, "BBBB-2") is False


def test_a_second_enrolment_replaces_the_first_secret(accounts):
    root = admin(accounts)
    accounts.begin_totp(root.id, "first")
    accounts.begin_totp(root.id, "second")
    assert accounts.secrets_of(root.id).totp_secret == "second"


def test_links_work_once_expire_and_a_new_link_voids_the_old_one(accounts, clock):
    eva = accounts.create_user("eva", role_id(accounts, "viewer"), status=INVITED)
    first = accounts.create_link(eva.id, INVITE, 3600, "root")
    assert accounts.peek_link(first, INVITE).user_id == eva.id
    assert accounts.peek_link(first, RESET) is None and accounts.peek_link("nope", INVITE) is None
    assert accounts.redeem_link(first, RESET) is None
    assert accounts.redeem_link(first, INVITE).user_id == eva.id
    assert accounts.redeem_link(first, INVITE) is None and accounts.peek_link(first, INVITE) is None
    second = accounts.create_link(eva.id, INVITE, 3600)
    third = accounts.create_link(eva.id, INVITE, 3600)
    assert accounts.redeem_link(second, INVITE) is None
    clock.now += 3601
    assert accounts.redeem_link(third, INVITE) is None
    with pytest.raises(InvalidRequest):
        accounts.create_link(eva.id, "odd", 60)
    with pytest.raises(InvalidRequest):
        accounts.create_link("missing", RESET, 60)


def test_revoking_links_voids_every_open_one_and_the_stored_value_is_not_the_token(accounts):
    eva = accounts.create_user("eva", role_id(accounts, "viewer"))
    reset = accounts.create_link(eva.id, RESET, 600)
    login = accounts.create_link(eva.id, LOGIN, 600)
    accounts.revoke_links(eva.id)
    assert accounts.peek_link(reset, RESET) is None and accounts.peek_link(login, LOGIN) is None
    stored = [row["token_hash"] for row in accounts._db.query("SELECT token_hash FROM links")]
    assert reset not in stored and login not in stored and all(len(item) == 64 for item in stored)


def test_deleting_an_account_removes_its_links_keys_and_codes(accounts):
    admin(accounts)
    eva = accounts.create_user("eva", role_id(accounts, "viewer"))
    accounts.create_link(eva.id, RESET, 600)
    accounts.issue_code(eva.id, "login")
    accounts.create_key(eva.id, "ci", None)
    accounts.delete_user(eva.id)
    for sql in (
        "SELECT 1 FROM links",
        "SELECT 1 FROM email_codes",
        "SELECT 1 FROM api_keys",
    ):
        assert accounts._db.query(sql) == []


def test_email_codes_work_once_expire_and_lock_after_too_many_wrong_tries(accounts, clock):
    eva = accounts.create_user("eva", role_id(accounts, "viewer"))
    code = accounts.issue_code(eva.id, "login")
    assert len(code) == 6 and code.isdigit()
    assert accounts.check_code(eva.id, "login", "000000" if code != "000000" else "111111") is False
    assert accounts.check_code(eva.id, "other", code) is False
    assert accounts.check_code(eva.id, "login", f" {code[:3]} {code[3:]} ") is True
    assert accounts.check_code(eva.id, "login", code) is False
    code = accounts.issue_code(eva.id, "login")
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        assert accounts.check_code(eva.id, "login", wrong) is False
    assert accounts.check_code(eva.id, "login", code) is False
    code = accounts.issue_code(eva.id, "login")
    clock.now += 601
    assert accounts.check_code(eva.id, "login", code) is False
    assert accounts.check_code("missing", "login", "123456") is False


def test_issuing_a_new_code_replaces_the_old_one_and_resets_the_tries(accounts):
    eva = accounts.create_user("eva", role_id(accounts, "viewer"))
    old = accounts.issue_code(eva.id, "login")
    for _ in range(4):
        accounts.check_code(eva.id, "login", "not a code")
    fresh = accounts.issue_code(eva.id, "login")
    assert accounts.check_code(eva.id, "login", fresh) is True
    assert accounts.check_code(eva.id, "login", old) is False or old == fresh


def test_api_keys_authenticate_by_secret_and_never_store_it(accounts, clock):
    root = admin(accounts)
    key, token = accounts.create_key(root.id, "  CI   job ", None)
    assert key.name == "CI job" and token.startswith(f"hra_{key.prefix}_")
    stored = accounts._db.query("SELECT secret_hash FROM api_keys")[0]["secret_hash"]
    assert token.split("_", 2)[2] not in stored and len(stored) == 64
    found, principal = accounts.authenticate_key(token)
    assert found.id == key.id and principal.source == "key" and principal.key_id == key.id
    assert principal.permissions == KNOWN and principal.name == "root"
    assert accounts.keys(root.id)[0].last_used == clock.now
    for bad in ("", "nonsense", "hra_zz_zz", token + "x", token[:-1], f"hra_{key.prefix}_wrong"):
        assert accounts.authenticate_key(bad) is None
    other = token.replace(key.prefix, "00000000")
    assert accounts.authenticate_key(other) is None


def test_a_key_can_be_narrowed_and_never_exceeds_its_owner(accounts):
    admin(accounts)
    eva = accounts.create_user("eva", role_id(accounts, "viewer"))
    _, narrow = accounts.create_key(eva.id, "narrow", ["reports.view", "finance.view"])
    _, principal = accounts.authenticate_key(narrow)
    assert principal.permissions == {"reports.view"}
    _, wide = accounts.create_key(eva.id, "wide", None)
    assert (
        accounts.authenticate_key(wide)[1].permissions == accounts.principal_for(eva.id).permissions
    )
    with pytest.raises(InvalidRequest, match="do not exist"):
        accounts.create_key(eva.id, "bad", ["made.up"])
    with pytest.raises(InvalidRequest, match="name"):
        accounts.create_key(eva.id, "  ", None)


def test_keys_stop_working_when_revoked_expired_or_their_owner_is_disabled(accounts, clock):
    admin(accounts)
    eva = accounts.create_user("eva", role_id(accounts, "recruiter"))
    key, token = accounts.create_key(eva.id, "one", None)
    assert accounts.revoke_key(key.id, "someone-else") is False
    assert accounts.authenticate_key(token) is not None
    assert accounts.revoke_key(key.id, eva.id) is True and accounts.revoke_key(key.id) is False
    assert accounts.authenticate_key(token) is None
    short, expiring = accounts.create_key(eva.id, "short", None, days=1)
    assert accounts.authenticate_key(expiring) is not None
    clock.now += 86_401
    assert accounts.authenticate_key(expiring) is None and short.expires is not None
    _, live = accounts.create_key(eva.id, "live", None)
    accounts.update_user(eva.id, status=DISABLED)
    assert accounts.authenticate_key(live) is None


def test_the_number_of_keys_per_account_is_limited_and_listings_can_be_scoped(make):
    accounts = make(max_api_keys_per_user=2)
    root = admin(accounts)
    eva = accounts.create_user("eva", role_id(accounts, "viewer"))
    accounts.create_key(root.id, "a", None)
    accounts.create_key(eva.id, "b", None)
    accounts.create_key(eva.id, "c", None)
    with pytest.raises(InvalidRequest, match="too many"):
        accounts.create_key(eva.id, "d", None)
    assert [key.name for key in accounts.keys(eva.id)] == ["c", "b"] or len(
        accounts.keys(eva.id)
    ) == 2
    assert len(accounts.keys()) == 3 and accounts.keys()[0].owner


def test_the_audit_log_is_filtered_paged_truncated_and_purged(make, clock):
    accounts = make(audit_page_size=10, audit_retention_days=30, audit_detail_chars=60)
    root = admin(accounts)
    principal = accounts.principal_for(root.id)
    accounts.log("login.ok", actor=principal, ip="10.0.0.1")
    accounts.log("login.fail", actor_name="mallory", outcome="denied", ip="10.0.0.9")
    accounts.log("user.create", actor=principal, target="eva", detail={"role": "viewer"})
    accounts.log("data.export", actor=principal, detail={"note": "x" * 500})
    entries = accounts.audit()
    assert [entry.action for entry in entries] == [
        "data.export",
        "user.create",
        "login.fail",
        "login.ok",
    ]
    assert entries[0].detail == {"truncated": '{"note":"' + "x" * 51}
    assert entries[1].detail == {"role": "viewer"} and entries[1].actor == "root"
    assert [entry.action for entry in accounts.audit(action="login")] == ["login.fail", "login.ok"]
    assert [entry.actor for entry in accounts.audit(actor="mallory")] == ["mallory"]
    assert [entry.action for entry in accounts.audit(outcome="denied")] == ["login.fail"]
    assert [entry.action for entry in accounts.audit(before=entries[1].id, limit=1)] == [
        "login.fail"
    ]
    clock.now += 31 * 86_400
    accounts.log("later", actor=principal)
    assert accounts.purge()["audit"] == 4
    assert [entry.action for entry in accounts.audit()] == ["later"]


def test_purging_removes_old_links_and_codes_only(accounts, clock):
    eva = accounts.create_user("eva", role_id(accounts, "viewer"))
    fresh = accounts.create_link(eva.id, RESET, 10 * 86_400)
    accounts.create_link(eva.id, LOGIN, 60)
    accounts.issue_code(eva.id, "login")
    clock.now += 2 * 86_400
    removed = accounts.purge()
    assert removed["links"] == 1 and removed["codes"] == 1
    assert accounts.peek_link(fresh, RESET) is not None


def test_the_owner_of_a_token_login_may_do_everything():
    owner = owner_principal()
    assert owner.permissions == KNOWN and owner.source == "owner" and owner.can("users.manage")
