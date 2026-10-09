# tests/test_mailer.py
from agent.mailer import (
    BLOCKED,
    deliver,
    FAILED,
    Mailer,
    OUTREACH,
    SENT,
    SYSTEM,
)
from agent.errors import (
    InvalidRequest,
    RateLimitedError,
    UpstreamError,
)
from agent.config import (
    Settings,
    StoreTuning,
)
from agent.accounts import Principal
from agent.db import Database
from typing import ClassVar

import smtplib
import pytest

PRINCIPAL = Principal("u1", "Eva", "recruiter", frozenset(), 0, "session")


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class Outbox:
    def __init__(self):
        self.sent = []
        self.error = None

    def __call__(self, settings, message):
        if self.error is not None:
            raise self.error
        self.sent.append(message)


def settings(**changes):
    values = {
        "smtp_host": "smtp.example.com",
        "smtp_from": "hiring@example.com",
        "smtp_from_name": "Acme Hiring",
        **changes,
    }
    return Settings(_env_file=None, **values)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def outbox():
    return Outbox()


@pytest.fixture
def make(tmp_path, clock, outbox):
    opened = []

    def build(**changes):
        database = Database(StoreTuning(path=tmp_path / "agent.db"), clock)
        opened.append(database)
        return Mailer(settings(**changes), database, clock, outbox)

    yield build
    for database in opened:
        database.close()


@pytest.fixture
def mailer(make):
    return make()


async def send(mailer, **changes):
    values = {"kind": OUTREACH, "purpose": "outreach", "actor": PRINCIPAL}
    values.update(changes)
    return await mailer.send(
        values.pop("to", "jan@example.com"),
        values.pop("subject", "Hello"),
        values.pop("body", "Hi Jan"),
        **values,
    )


async def test_a_message_is_built_sent_and_logged(mailer, outbox, clock):
    entry = await send(mailer, template="outreach-single", job_id="j1", candidate_id="c1")
    (message,) = outbox.sent
    assert message["From"] == "Acme Hiring <hiring@example.com>"
    assert message["To"] == "jan@example.com" and message["Subject"] == "Hello"
    assert message["Message-ID"].endswith("@example.com>") and "+0000" in message["Date"]
    assert message.get_content_type() == "text/plain" and message.get_content().strip() == "Hi Jan"
    assert entry.status == SENT and entry.actor == "Eva" and entry.template == "outreach-single"
    assert entry.job_id == "j1" and entry.candidate_id == "c1" and entry.message_id
    assert mailer.entries()[0].id == entry.id


async def test_nothing_is_sent_until_the_server_is_configured(make, outbox):
    plain = make(smtp_host=None)
    assert plain.configured() is False
    with pytest.raises(InvalidRequest, match="not set up"):
        await send(plain)
    assert outbox.sent == [] and plain.entries() == []


@pytest.mark.parametrize(
    "address",
    [
        "",
        "plain",
        "a@b",
        "jan@example.com\nBcc: boss@example.com",
        "jan@example.com, eve@example.com",
        "Jan <jan@example.com>",
        "jan@example.com;eve@example.com",
        "<jan@example.com>",
        "ja n@example.com",
        "x" * 400 + "@example.com",
    ],
)
async def test_only_one_plain_address_is_accepted(mailer, outbox, address):
    with pytest.raises(InvalidRequest, match="email address"):
        await send(mailer, to=address)
    assert outbox.sent == []


async def test_subjects_cannot_inject_headers_and_text_is_limited(mailer, outbox):
    entry = await send(mailer, subject="Hello\r\nBcc: boss@example.com")
    assert entry.subject == "Hello Bcc: boss@example.com"
    assert outbox.sent[0]["Bcc"] is None
    for kwargs in (
        {"subject": "  "},
        {"subject": "x" * 500},
        {"body": " "},
        {"body": "x" * 20_000},
    ):
        with pytest.raises(InvalidRequest):
            await send(mailer, **kwargs)
    with pytest.raises(InvalidRequest, match="does not exist"):
        await send(mailer, kind="spam")


async def test_a_reply_to_address_is_checked_too(mailer, outbox):
    await send(mailer, reply_to="boss@example.com")
    assert outbox.sent[0]["Reply-To"] == "boss@example.com"
    with pytest.raises(InvalidRequest, match="email address"):
        await send(mailer, reply_to="nope\nBcc: x@example.com")


async def test_suppressed_addresses_are_never_contacted_but_system_mail_still_works(mailer, outbox):
    mailer.suppress("Jan@Example.com", "asked to stop", "Eva")
    assert (
        mailer.suppressed("jan@example.com") and mailer.suppressions()[0].reason == "asked to stop"
    )
    with pytest.raises(InvalidRequest, match="asked not to be contacted"):
        await send(mailer)
    assert outbox.sent == [] and mailer.entries()[0].status == BLOCKED
    system = await send(mailer, kind=SYSTEM, purpose="login_code")
    assert system.status == SENT
    mailer.suppress("jan@example.com")
    assert len(mailer.suppressions()) == 1
    assert (
        mailer.unsuppress("jan@example.com") is True
        and mailer.unsuppress("jan@example.com") is False
    )
    assert (await send(mailer)).status == SENT


async def test_outreach_is_limited_per_hour_but_system_mail_is_not(make, outbox, clock):
    mailer = make()
    limited = Mailer(settings(), mailer._db, clock, outbox)
    limited._settings = settings(mail={"send_per_hour": 2})
    await send(limited)
    await send(limited)
    with pytest.raises(RateLimitedError, match="hourly"):
        await send(limited)
    assert (await send(limited, kind=SYSTEM, purpose="invite")).status == SENT
    clock.now += 3601
    assert (await send(limited)).status == SENT
    assert len(outbox.sent) == 4


@pytest.mark.parametrize(
    "address", ["(x)jan@example.com", "grp:jan@example.com", "jan@example.com (note)"]
)
async def test_comments_and_groups_cannot_hide_the_real_recipient(mailer, outbox, address):
    with pytest.raises(InvalidRequest, match="email address"):
        await send(mailer, to=address)
    assert outbox.sent == []


async def test_plus_tags_and_case_do_not_get_around_the_do_not_contact_list(mailer, outbox):
    mailer.suppress("jan@example.com", "asked to stop")
    for variant in ("jan+news@example.com", "JAN+a+b@Example.com", "Jan@EXAMPLE.com"):
        with pytest.raises(InvalidRequest, match="asked not to be contacted"):
            await send(mailer, to=variant)
    assert mailer.suppressed("jan+x@example.com") and outbox.sent == []
    assert mailer.unsuppress("jan+x@example.com") is True
    assert (await send(mailer)).status == SENT


async def test_routine_notices_respect_the_do_not_contact_list(mailer, outbox):
    mailer.suppress("jan@example.com")
    with pytest.raises(InvalidRequest, match="asked not to be contacted"):
        await send(mailer, kind=SYSTEM, purpose="routine")
    assert (await send(mailer, kind=SYSTEM, purpose="reset")).status == SENT


async def test_one_address_cannot_be_flooded_with_system_mail(make, outbox, clock):
    mailer = make()
    mailer._settings = settings(mail={"system_per_recipient": 2})
    for _ in range(2):
        await send(mailer, kind=SYSTEM, purpose="reset")
    with pytest.raises(RateLimitedError, match="Too many messages"):
        await send(mailer, kind=SYSTEM, purpose="reset", to="JAN@example.com")
    assert (await send(mailer, kind=SYSTEM, purpose="reset", to="eve@example.com")).status == SENT
    clock.now += 3601
    assert (await send(mailer, kind=SYSTEM, purpose="reset")).status == SENT


async def test_a_failed_delivery_is_logged_without_leaking_details(mailer, outbox):
    outbox.error = UpstreamError("The mail server could not be used (OSError).")
    with pytest.raises(UpstreamError, match="could not be used"):
        await send(mailer)
    entry = mailer.entries()[0]
    assert entry.status == FAILED and "OSError" in entry.error
    outbox.error = None
    assert (await send(mailer)).status == SENT


async def test_failed_attempts_do_not_count_against_the_hour(make, outbox, clock):
    base = make()
    limited = Mailer(settings(mail={"send_per_hour": 1}), base._db, clock, outbox)
    outbox.error = UpstreamError("down")
    with pytest.raises(UpstreamError):
        await send(limited)
    outbox.error = None
    assert (await send(limited)).status == SENT


def test_the_log_is_paged_newest_first_and_old_entries_are_purged(mailer, clock):
    for number in range(5):
        mailer._db.run(
            "INSERT INTO mail_log (ts, to_address, subject, kind, purpose, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                clock.now - number * 86_400 * 100,
                f"a{number}@example.com",
                "s",
                SYSTEM,
                "test",
                SENT,
            ),
        )
    page = mailer.entries(limit=3)
    assert [entry.to_address for entry in page] == [
        "a4@example.com",
        "a3@example.com",
        "a2@example.com",
    ]
    assert [entry.to_address for entry in mailer.entries(before=page[-1].id)] == [
        "a1@example.com",
        "a0@example.com",
    ]
    assert mailer.purge() == 1 and len(mailer.entries()) == 4
    assert mailer.entry(99_999) is None


class FakeSMTP:
    instances: ClassVar[list] = []
    fail: ClassVar[str | None] = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user))
        if self.fail == "auth":
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials hunter2")

    def send_message(self, message):
        self.calls.append("send")
        if self.fail == "refused":
            raise smtplib.SMTPRecipientsRefused({"x": (550, b"no")})
        if self.fail == "down":
            raise smtplib.SMTPServerDisconnected("hunter2 gone")


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def message_for(mailer):
    return mailer._message("jan@example.com", "Hello", "Hi", None)


def test_starttls_is_used_before_the_login_with_a_verifying_context(smtp, mailer):
    config = settings(smtp_username="hiring", smtp_password="hunter2")
    deliver(config, message_for(mailer))
    (client,) = smtp.instances
    assert client.calls == ["starttls", ("login", "hiring"), "send"]
    assert (client.host, client.port, client.timeout) == ("smtp.example.com", 587, 20.0)


def test_ssl_mode_connects_encrypted_with_the_right_port_and_no_login_without_a_password(
    smtp, mailer
):
    deliver(settings(smtp_security="ssl"), message_for(mailer))
    (client,) = smtp.instances
    assert client.port == 465 and client.context is not None and client.calls == ["send"]
    deliver(settings(smtp_security="none", smtp_port=2525), message_for(mailer))
    assert smtp.instances[1].port == 2525 and smtp.instances[1].calls == ["send"]


@pytest.mark.parametrize(
    ("failure", "text"),
    [
        ("auth", "did not accept the user name and password"),
        ("refused", "refused the recipient"),
        ("down", "could not be used .SMTPServerDisconnected."),
    ],
)
def test_server_errors_become_short_safe_messages(smtp, mailer, monkeypatch, failure, text):
    monkeypatch.setattr(FakeSMTP, "fail", failure)
    config = settings(smtp_username="hiring", smtp_password="hunter2")
    with pytest.raises(UpstreamError, match=text) as caught:
        deliver(config, message_for(mailer))
    assert "hunter2" not in str(caught.value)


def test_network_failures_are_reported_without_the_address_or_password(monkeypatch, mailer):
    def refuse(*args, **kwargs):
        raise ConnectionRefusedError("hunter2 at smtp.example.com")

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    with pytest.raises(UpstreamError) as caught:
        deliver(settings(smtp_username="x", smtp_password="hunter2"), message_for(mailer))
    assert "ConnectionRefusedError" in str(caught.value) and "hunter2" not in str(caught.value)
