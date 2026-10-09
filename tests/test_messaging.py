# tests/test_messaging.py
from agent.config import (
    AuthTuning,
    MessageTuning,
    StoreTuning,
)
from agent.messaging import (
    fingerprint_of,
    Messenger,
    REMOVED,
)
from agent.errors import InvalidRequest
from agent.accounts import Accounts
from agent.db import Database
from agent.vault import Vault

import pytest
import json

PUBLIC = {"kty": "EC", "crv": "P-256", "x": "AAAA", "y": "BBBB"}


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def tick(self, seconds=60):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def world(tmp_path, clock):
    database = Database(StoreTuning(path=tmp_path / "data" / "agent.db"), clock)
    auth = AuthTuning()
    accounts = Accounts(database, auth, clock)
    accounts.sync_roles(auth.roles)
    vault = Vault(tmp_path / "data" / "secret.key")

    def build(**changes):
        return Messenger(database, vault, MessageTuning(**changes), clock)

    def person(name, role="recruiter"):
        return accounts.create_user(name, accounts.role_named(role).id).id

    yield {"accounts": accounts, "build": build, "person": person, "database": database}
    database.close()


@pytest.fixture
def chat(world):
    return world["build"]()


def keys_for(chat, user_id):
    return chat.save_keys(user_id, json.dumps(PUBLIC), "wrapped-private", "salt", 600_000)


def wraps_for(*ids, key=PUBLIC):
    return {user: {"wrapped": f"w-{user}", "sender_key": json.dumps(key)} for user in ids}


def test_a_direct_conversation_is_created_once_and_only_its_two_people_see_it(world, chat):
    ann, bob, cy = world["person"]("ann"), world["person"]("bob"), world["person"]("cy")
    first = chat.direct(ann, bob)
    again = chat.direct(bob, ann)
    assert first.id == again.id and first.kind == "direct" and not first.encrypted
    assert first.title == "bob" and again.title == "ann"
    chat.send(ann, first.id, "Hello Bob")
    assert chat.conversation(cy, first.id) is None
    assert chat.feed(cy, first.id) is None
    with pytest.raises(InvalidRequest):
        chat.send(cy, first.id, "sneaky")
    assert [item.text for item in chat.feed(bob, first.id)] == ["Hello Bob"]
    with pytest.raises(InvalidRequest, match="someone else"):
        chat.direct(ann, ann)
    assert [item.id for item in chat.conversations(cy)] == [
        chat.role_channel(world["accounts"].user(cy).role_id)
    ]


def test_text_is_stored_encrypted_on_the_server_and_never_in_the_clear(world, chat):
    ann, bob = world["person"]("ann"), world["person"]("bob")
    conversation = chat.direct(ann, bob)
    chat.send(ann, conversation.id, "the secret plan")
    stored = world["database"].query("SELECT body FROM chat_messages")[0]["body"]
    assert "secret plan" not in stored and stored.startswith("gAAAA")
    world["database"].run("UPDATE chat_messages SET body = 'garbage'")
    assert chat.feed(bob, conversation.id)[0].text == "[this message cannot be read]"


def test_message_text_is_validated_and_limited(world):
    chat = world["build"](max_chars=100)
    ann, bob = world["person"]("ann"), world["person"]("bob")
    conversation = chat.direct(ann, bob)
    with pytest.raises(InvalidRequest, match="something"):
        chat.send(ann, conversation.id, "   ")
    sent = chat.send(ann, conversation.id, "x" * 500)
    assert len(sent.text) == 100 and sent.mine is True


def test_unread_counts_follow_the_read_marker(world, chat):
    ann, bob = world["person"]("ann"), world["person"]("bob")
    conversation = chat.direct(ann, bob)
    first = chat.send(ann, conversation.id, "one")
    chat.send(ann, conversation.id, "two")
    assert chat.conversation(bob, conversation.id).unread == 2
    assert chat.conversation(ann, conversation.id).unread == 0
    assert chat.unread(bob)["messages"] == 2
    assert chat.mark_read(bob, conversation.id, first.id) is True
    assert chat.conversation(bob, conversation.id).unread == 1
    chat.mark_read(bob, conversation.id, 10_000)
    assert chat.unread(bob)["messages"] == 0
    assert chat.mark_read(bob, "nope", 1) is False
    assert [item.text for item in chat.feed(bob, conversation.id, after=first.id)] == ["two"]


def test_groups_have_owners_members_and_limits(world):
    chat = world["build"](max_members=3, max_groups_per_user=1)
    ann, bob, cy, dee = (world["person"](name) for name in ("ann", "bob", "cy", "dee"))
    group = chat.create_group(ann, " Hiring team ", [bob, cy])
    assert group.title == "Hiring team" and group.owner is True
    assert {member.name for member in chat.members(bob, group.id)} == {"ann", "bob", "cy"}
    with pytest.raises(InvalidRequest, match=r"too many|too large"):
        chat.create_group(ann, "Another", [bob])
    with pytest.raises(InvalidRequest, match="too large"):
        chat.add_members(ann, group.id, [dee])
    with pytest.raises(InvalidRequest, match="cannot change"):
        chat.add_members(bob, group.id, [dee])
    with pytest.raises(InvalidRequest, match="name"):
        world["build"]().create_group(bob, "x", [ann])
    with pytest.raises(InvalidRequest, match="at least one other"):
        world["build"]().create_group(bob, "Solo group", [])
    assert chat.send(cy, group.id, "hi all").text == "hi all"
    assert chat.remove_member(ann, group.id, cy) is True
    assert chat.conversation(cy, group.id) is None
    with pytest.raises(InvalidRequest):
        chat.send(cy, group.id, "still here?")


def test_members_can_leave_and_a_new_owner_takes_over(world, chat):
    ann, bob, cy = world["person"]("ann"), world["person"]("bob"), world["person"]("cy")
    group = chat.create_group(ann, "Team", [bob, cy])
    assert chat.remove_member(ann, group.id, ann) is True
    assert chat.conversation(ann, group.id) is None
    owners = [item for item in chat.members(bob, group.id) if item.owner]
    assert len(owners) == 1
    assert owners[0].user_id in (bob, cy)
    chat.rename_group(owners[0].user_id, group.id, "Renamed")
    assert chat.conversation(owners[0].user_id, group.id).title == "Renamed"


def test_moderators_may_manage_groups_they_do_not_own(world, chat):
    ann, bob = world["person"]("ann"), world["person"]("bob")
    boss = world["person"]("boss", "manager")
    group = chat.create_group(ann, "Team", [bob, boss])
    chat.rename_group(boss, group.id, "Moved", moderator=True)
    assert chat.conversation(ann, group.id).title == "Moved"
    with pytest.raises(InvalidRequest, match="cannot change"):
        chat.rename_group(bob, group.id, "Mine")
    with pytest.raises(InvalidRequest, match="cannot change"):
        chat.delete_group(bob, group.id)
    chat.delete_group(boss, group.id, moderator=True)
    assert chat.conversation(ann, group.id) is None


def test_the_role_channel_belongs_to_everyone_with_that_role_and_follows_role_changes(world, chat):
    accounts = world["accounts"]
    world["person"]("root", "admin")
    ann, bob = world["person"]("ann"), world["person"]("bob")
    viewer = world["person"]("vic", "viewer")
    channel = chat.role_channel(accounts.user(ann).role_id)
    assert chat.role_channel(accounts.user(bob).role_id) == channel
    assert chat.role_channel("none") is None
    listed = [item.id for item in chat.conversations(ann)]
    assert channel in listed
    chat.send(ann, channel, "recruiters only")
    assert [item.text for item in chat.feed(bob, channel)] == ["recruiters only"]
    assert chat.feed(viewer, channel) is None
    accounts.update_user(bob, role_id=accounts.role_named("viewer").id)
    assert chat.feed(bob, channel) is None
    assert chat.conversation(bob, channel) is None


def test_people_without_the_permission_cannot_take_part(world, chat):
    accounts = world["accounts"]
    accounts.save_role("Silent", "", ["chat.use"])
    mute = accounts.create_user("mute", accounts.role_named("Silent").id).id
    ann = world["person"]("ann")
    with pytest.raises(InvalidRequest, match="cannot be messaged"):
        chat.direct(ann, mute)
    with pytest.raises(InvalidRequest, match="cannot be messaged"):
        chat.direct(mute, ann)
    assert chat.conversations(mute) == [] and chat.announcements(mute) == []
    assert [item.name for item in chat.people(ann)] == []
    off = accounts.create_user("off", accounts.role_named("recruiter").id, status="disabled").id
    with pytest.raises(InvalidRequest):
        chat.direct(ann, off)
    assert chat.unread(mute) == {"messages": 0, "announcements": 0}


def test_the_directory_lists_only_active_people_who_may_message(world, chat):
    ann, bob = world["person"]("ann"), world["person"]("bob")
    world["person"]("vic", "viewer")
    names = [item.name for item in chat.people(ann)]
    assert names == ["bob", "vic"]
    keys_for(chat, bob)
    assert {item.name: item.has_keys for item in chat.people(ann)} == {"bob": True, "vic": False}


def test_a_sender_can_remove_their_own_message_and_moderators_any(world, chat):
    ann, bob = world["person"]("ann"), world["person"]("bob")
    conversation = chat.direct(ann, bob)
    mine = chat.send(ann, conversation.id, "oops")
    theirs = chat.send(bob, conversation.id, "ok")
    assert chat.remove_message(ann, theirs.id) is False
    assert chat.remove_message(ann, mine.id) is True
    assert chat.remove_message(ann, theirs.id, moderator=True) is True
    texts = [item.text for item in chat.feed(bob, conversation.id)]
    assert texts == [REMOVED, REMOVED]
    stored = world["database"].query("SELECT body FROM chat_messages")
    assert all(row["body"] == "" for row in stored)
    assert chat.remove_message(world["person"]("cy"), mine.id) is False


def test_keys_are_validated_and_the_fingerprint_is_stable(world, chat):
    ann = world["person"]("ann")
    saved = keys_for(chat, ann)
    assert saved.fingerprint == fingerprint_of(PUBLIC) and len(saved.fingerprint.split()) == 8
    with pytest.raises(InvalidRequest, match="already exist"):
        keys_for(chat, ann)
    assert (
        chat.save_keys(
            ann, json.dumps(PUBLIC), "new", "salt", 600_000, replace=True
        ).wrapped_private
        == "new"
    )
    for bad in (
        "not json",
        "{}",
        json.dumps({**PUBLIC, "crv": "P-384"}),
        json.dumps({**PUBLIC, "x": "A" * 500}),
    ):
        with pytest.raises(InvalidRequest, match="public key"):
            chat.save_keys(world["person"](f"u{len(bad)}"), bad, "w", "salt", 600_000)
    other = world["person"]("bob")
    with pytest.raises(InvalidRequest, match="weak"):
        chat.save_keys(other, json.dumps(PUBLIC), "w", "salt", 10)
    with pytest.raises(InvalidRequest, match="too large"):
        chat.save_keys(other, json.dumps(PUBLIC), "w" * 100_000, "salt", 600_000)
    assert chat.public_key_of(ann)["fingerprint"] == saved.fingerprint
    assert chat.public_key_of(other) is None


def test_end_to_end_conversations_store_only_ciphertext_and_need_keys_from_everyone(world, chat):
    ann, bob = world["person"]("ann"), world["person"]("bob")
    with pytest.raises(InvalidRequest, match="private keys"):
        chat.direct(ann, bob, True, wraps_for(ann, bob))
    keys_for(chat, ann)
    keys_for(chat, bob)
    with pytest.raises(InvalidRequest, match="Every member"):
        chat.direct(ann, bob, True, wraps_for(ann))
    with pytest.raises(InvalidRequest, match="Every member"):
        chat.direct(ann, bob, True, None)
    conversation = chat.direct(ann, bob, True, wraps_for(ann, bob))
    assert conversation.encrypted and conversation.key_version == 1
    assert chat.direct(ann, bob, False).id != conversation.id
    cipher = json.dumps({"iv": "aXY=", "ct": "Y2lwaGVy"})
    with pytest.raises(InvalidRequest, match="encrypted message"):
        chat.send(ann, conversation.id, "plain text must not be accepted here")
    with pytest.raises(InvalidRequest, match="not valid"):
        chat.send(ann, conversation.id, cipher="[]", version=1)
    with pytest.raises(InvalidRequest, match="key changed"):
        chat.send(ann, conversation.id, cipher=cipher, version=2)
    sent = chat.send(ann, conversation.id, cipher=cipher, version=1)
    assert sent.cipher == cipher and sent.text == "" and sent.encrypted
    assert chat.feed(bob, conversation.id)[0].cipher == cipher
    assert chat.wrapped_keys(bob, conversation.id) == [
        {"version": 1, "wrapped": f"w-{bob}", "sender_key": json.dumps(PUBLIC)}
    ]
    assert chat.wrapped_keys(world["person"]("cy"), conversation.id) is None
    assert world["database"].query("SELECT body FROM chat_messages")[0]["body"] == cipher


def test_changing_who_is_in_an_end_to_end_group_always_makes_a_new_key(world, chat):
    ann, bob, cy, dee = (world["person"](name) for name in ("ann", "bob", "cy", "dee"))
    for person in (ann, bob, cy, dee):
        keys_for(chat, person)
    group = chat.create_group(ann, "Private", [bob, cy], True, wraps_for(ann, bob, cy))
    with pytest.raises(InvalidRequest, match="Every member"):
        chat.add_members(ann, group.id, [dee], wraps=wraps_for(dee))
    assert chat.add_members(ann, group.id, [dee], wraps=wraps_for(ann, bob, cy, dee)) == 1
    assert [item["version"] for item in chat.wrapped_keys(dee, group.id)] == [2]
    with pytest.raises(InvalidRequest, match="Every member"):
        chat.remove_member(ann, group.id, cy, wraps=wraps_for(ann, bob))
    assert chat.remove_member(ann, group.id, cy, wraps=wraps_for(ann, bob, dee)) is True
    assert chat.conversation(ann, group.id).key_version == 3
    assert [item["version"] for item in chat.wrapped_keys(bob, group.id)] == [1, 2, 3]
    assert chat.wrapped_keys(cy, group.id) is None
    cipher = json.dumps({"iv": "aXY=", "ct": "Y2lwaGVy"})
    with pytest.raises(InvalidRequest, match="key changed"):
        chat.send(ann, group.id, cipher=cipher, version=2)
    assert chat.send(ann, group.id, cipher=cipher, version=3).version == 3


def test_key_wraps_must_be_made_with_the_senders_own_registered_key(world, chat):
    ann, bob = world["person"]("ann"), world["person"]("bob")
    keys_for(chat, ann)
    keys_for(chat, bob)
    stranger = {**PUBLIC, "x": "CCCC"}
    with pytest.raises(InvalidRequest, match="your own key"):
        chat.direct(ann, bob, True, wraps_for(ann, bob, key=stranger))


def test_leaving_a_private_group_locks_it_until_someone_rotates_the_key(world, chat):
    ann, bob, cy = (world["person"](name) for name in ("ann", "bob", "cy"))
    for person in (ann, bob, cy):
        keys_for(chat, person)
    group = chat.create_group(ann, "Private", [bob, cy], True, wraps_for(ann, bob, cy))
    assert chat.remove_member(cy, group.id, cy) is True
    assert chat.conversation(ann, group.id).key_version == 2
    assert [item["version"] for item in chat.wrapped_keys(ann, group.id)] == [1]
    assert chat.rotate_key(bob, group.id, wraps_for(ann, bob)) == 3
    assert [item["version"] for item in chat.wrapped_keys(ann, group.id)] == [1, 3]
    with pytest.raises(InvalidRequest, match="Every member"):
        chat.rotate_key(bob, group.id, wraps_for(bob))
    with pytest.raises(InvalidRequest, match="cannot change its key"):
        chat.rotate_key(bob, chat.role_channel(world["accounts"].user(bob).role_id), {})


def test_rotating_drops_people_who_can_no_longer_take_part(world, chat):
    accounts = world["accounts"]
    world["person"]("root", "admin")
    ann, bob, cy = (world["person"](name) for name in ("ann", "bob", "cy"))
    for person in (ann, bob, cy):
        keys_for(chat, person)
    group = chat.create_group(ann, "Private", [bob, cy], True, wraps_for(ann, bob, cy))
    accounts.update_user(cy, status="disabled")
    assert {item.user_id: item.eligible for item in chat.members(ann, group.id)}[cy] is False
    assert chat.rotate_key(ann, group.id, wraps_for(ann, bob)) == 2
    assert {item.user_id for item in chat.members(ann, group.id)} == {ann, bob}
    assert chat.wrapped_keys(cy, group.id) is None
    pair = chat.direct(ann, bob, True, wraps_for(ann, bob))
    accounts.update_user(bob, status="disabled")
    with pytest.raises(InvalidRequest, match="still be able"):
        chat.rotate_key(ann, pair.id, wraps_for(ann, bob))


def test_only_members_may_change_a_private_group_but_moderators_may_delete_it(world, chat):
    ann, bob, cy, dee = (world["person"](name) for name in ("ann", "bob", "cy", "dee"))
    boss = world["person"]("boss", "admin")
    for person in (ann, bob, cy, dee, boss):
        keys_for(chat, person)
    group = chat.create_group(ann, "Private", [bob, cy], True, wraps_for(ann, bob, cy))
    with pytest.raises(InvalidRequest, match="Only a member"):
        chat.add_members(
            boss, group.id, [dee], True, wraps_for(ann, bob, cy, dee, boss, key=PUBLIC)
        )
    with pytest.raises(InvalidRequest, match="Only a member"):
        chat.remove_member(boss, group.id, cy, True, wraps_for(ann, bob))
    chat.delete_group(boss, group.id, moderator=True)
    assert chat.conversation(ann, group.id) is None


def test_peer_keys_include_people_who_were_disabled_so_old_messages_stay_checkable(world, chat):
    accounts = world["accounts"]
    world["person"]("root", "admin")
    ann, bob = world["person"]("ann"), world["person"]("bob")
    keys_for(chat, ann)
    keys_for(chat, bob)
    pair = chat.direct(ann, bob, True, wraps_for(ann, bob))
    accounts.update_user(bob, status="disabled")
    found = chat.peer_keys(ann, pair.id)
    assert found[bob]["eligible"] is False and found[ann]["eligible"] is True
    assert found[bob]["fingerprint"] == fingerprint_of(PUBLIC)
    assert chat.peer_keys(world["person"]("cy"), pair.id) is None


def test_a_changed_role_no_longer_counts_the_old_role_channel(world, chat):
    accounts = world["accounts"]
    world["person"]("root", "admin")
    ann, bob = world["person"]("ann"), world["person"]("bob")
    channel = chat.role_channel(accounts.user(ann).role_id)
    chat.send(ann, channel, "one")
    chat.feed(bob, channel)
    chat.mark_read(bob, channel, 0)
    accounts.update_user(bob, role_id=accounts.role_named("viewer").id)
    chat.send(ann, channel, "two")
    assert channel not in [item.id for item in chat.conversations(bob)]
    assert chat.unread(bob)["messages"] == 0


def test_the_read_marker_is_clamped_to_real_messages(world, chat):
    ann, bob = world["person"]("ann"), world["person"]("bob")
    conversation = chat.direct(ann, bob)
    chat.send(ann, conversation.id, "hello")
    assert chat.mark_read(bob, conversation.id, 10**30) is True
    chat.send(ann, conversation.id, "again")
    assert chat.unread(bob)["messages"] == 1


def test_resetting_keys_drops_the_old_wrapped_keys(world, chat):
    ann, bob = world["person"]("ann"), world["person"]("bob")
    keys_for(chat, ann)
    keys_for(chat, bob)
    conversation = chat.direct(ann, bob, True, wraps_for(ann, bob))
    chat.save_keys(ann, json.dumps(PUBLIC), "fresh", "salt", 600_000, replace=True)
    assert chat.wrapped_keys(ann, conversation.id) == []
    assert len(chat.wrapped_keys(bob, conversation.id)) == 1


def test_announcements_reach_exactly_their_audience(world, chat, clock):
    accounts = world["accounts"]
    boss = world["person"]("boss", "manager")
    ann, bob = world["person"]("ann"), world["person"]("bob")
    vic = world["person"]("vic", "viewer")
    recruiters = accounts.user(ann).role_id
    group = chat.create_group(ann, "Squad", [bob])
    everyone = chat.announce(boss, " All hands ", "Friday at ten", pinned=True)
    only_recruiters = chat.announce(boss, "Recruiting", "New process", "role", recruiters, days=7)
    squad = chat.announce(ann, "Squad news", "Standup moved", "group", group.id)
    assert (
        everyone.pinned and everyone.audience_label == "Everyone" and everyone.title == "All hands"
    )
    assert {item.title for item in chat.announcements(vic)} == {"All hands"}
    assert {item.title for item in chat.announcements(bob)} == {
        "All hands",
        "Recruiting",
        "Squad news",
    }
    assert {item.title for item in chat.announcements(ann)} == {
        "All hands",
        "Recruiting",
        "Squad news",
    }
    assert chat.announcements(bob)[0].title == "All hands"
    assert chat.unread(bob)["announcements"] == 3
    assert chat.unread(boss)["announcements"] == 0
    assert chat.read_announcement(bob, squad.id) and chat.unread(bob)["announcements"] == 2
    assert chat.read_announcement(vic, squad.id) is False
    assert {item.title for item in chat.announcements(bob, unread_only=True)} == {
        "All hands",
        "Recruiting",
    }
    clock.tick(8 * 86_400)
    assert {item.title for item in chat.announcements(bob)} == {"All hands", "Squad news"}
    assert only_recruiters.expires is not None
    stored = world["database"].query("SELECT body FROM announcements")
    assert all("Friday" not in row["body"] for row in stored)


def test_announcements_are_validated_and_controlled(world, chat):
    boss = world["person"]("boss", "manager")
    ann, bob = world["person"]("ann"), world["person"]("bob")
    group = chat.create_group(ann, "Squad", [bob])
    for bad in (
        ("", "text", "all", "", 0),
        ("title", "", "all", "", 0),
        ("title", "text", "everybody", "", 0),
        ("title", "text", "role", "none", 0),
        ("title", "text", "group", "none", 0),
        ("title", "text", "all", "", 3),
    ):
        with pytest.raises(InvalidRequest):
            chat.announce(boss, bad[0], bad[1], bad[2], bad[3], bad[4])
    with pytest.raises(InvalidRequest, match="group"):
        chat.announce(boss, "title", "text", "group", group.id)
    assert (
        chat.announce(boss, "title", "text", "group", group.id, moderator=True).audience == "group"
    )
    mine = chat.announce(boss, "Mine", "text")
    assert chat.delete_announcement(ann, mine.id) is False
    assert chat.delete_announcement(boss, mine.id) is True
    other = chat.announce(boss, "Other", "text")
    assert chat.delete_announcement(ann, other.id, moderator=True) is True
    world["database"].run("DELETE FROM announcements")
    small = world["build"](announcement_max_active=1)
    small.announce(boss, "One", "text")
    with pytest.raises(InvalidRequest, match="too many"):
        small.announce(boss, "Two", "text")


def test_deleting_a_group_removes_its_messages_and_announcements(world, chat):
    ann, bob = world["person"]("ann"), world["person"]("bob")
    group = chat.create_group(ann, "Squad", [bob])
    chat.send(ann, group.id, "hello")
    chat.announce(ann, "News", "text", "group", group.id)
    chat.delete_group(ann, group.id)
    assert world["database"].query("SELECT * FROM chat_messages") == []
    assert world["database"].query("SELECT * FROM announcements") == []


def test_retention_purges_old_messages_and_stale_announcements(world, clock):
    chat = world["build"](retention_days=10)
    boss = world["person"]("boss", "manager")
    ann, bob = world["person"]("ann"), world["person"]("bob")
    conversation = chat.direct(ann, bob)
    chat.send(ann, conversation.id, "old")
    chat.announce(boss, "Old", "text", days=1)
    clock.tick(45 * 86_400)
    chat.send(ann, conversation.id, "new")
    assert chat.purge() == {"messages": 1, "announcements": 1}
    assert [item.text for item in chat.feed(bob, conversation.id)] == ["new"]
    assert world["build"]().purge() == {"messages": 0, "announcements": 0}


def test_one_author_cannot_fill_the_announcement_board(world):
    chat = world["build"](announcement_max_per_author=2)
    boss, other = world["person"]("boss", "manager"), world["person"]("other", "manager")
    chat.announce(boss, "One", "text")
    chat.announce(boss, "Two", "text")
    with pytest.raises(InvalidRequest, match="too many"):
        chat.announce(boss, "Three", "text")
    assert chat.announce(other, "Four", "text").title == "Four"


def test_leaving_the_last_seat_and_purging_remove_announcements_that_nobody_can_see(world, clock):
    chat = world["build"]()
    boss = world["person"]("boss", "manager")
    ann, bob = world["person"]("ann"), world["person"]("bob")
    group = chat.create_group(ann, "Squad", [bob])
    chat.announce(ann, "Squad news", "text", "group", group.id)
    chat.remove_member(bob, group.id, bob)
    chat.remove_member(ann, group.id, ann)
    assert world["database"].query("SELECT * FROM announcements") == []
    world["accounts"].save_role("Temp", "", ["messages.use"])
    other = world["accounts"].role_named("Temp")
    chat.announce(boss, "Temp news", "text", "role", other.id)
    world["database"].run("DELETE FROM roles WHERE id = ?", (other.id,))
    assert chat.purge()["announcements"] == 1


def test_the_unread_banner_is_not_crowded_out_by_already_read_notices(world, clock):
    chat = world["build"]()
    boss = world["person"]("boss", "manager")
    ann = world["person"]("ann")
    for number in range(4):
        chat.announce(boss, f"Old {number}", "text")
        clock.tick(1)
    for item in chat.announcements(ann):
        chat.read_announcement(ann, item.id)
    clock.tick(1)
    chat.announce(boss, "Fresh", "text")
    shown = chat.announcements(ann, True, 2)
    assert [item.title for item in shown] == ["Fresh"]
    assert chat.unread(ann)["announcements"] == 1
