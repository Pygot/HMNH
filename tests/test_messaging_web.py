# tests/test_messaging_web.py
from tests.web_helpers import (
    add_user,
    Browser,
    build_app,
    open_client,
    ORIGIN,
    PASSWORD,
)
from agent.web.security import RateLimiter

import json
import re

PUBLIC = json.dumps({"kty": "EC", "crv": "P-256", "x": "AAAA", "y": "BBBB"})
CIPHER = json.dumps({"iv": "aXY=", "ct": "Y2lwaGVy"})


class Person:
    def __init__(self, app, name, role="recruiter"):
        self.app = app
        self.role = role
        self.user = None
        self.client = open_client(app)
        self.browser = Browser(self.client)
        self.token = None
        self.name = name

    def __enter__(self):
        self.client.__enter__()
        self.user = add_user(self.app, self.name, role=self.role)
        return self

    def __exit__(self, *exc):
        self.client.__exit__(*exc)

    def sign_in(self):
        self.token = self.browser.signed_in_token(self.name, path="/messages")
        return self

    def json(self, path, body=None, method="post"):
        headers = {"X-CSRF-Token": self.token, "origin": ORIGIN}
        if method == "get":
            return self.client.get(path, headers=headers)
        return self.client.post(path, json=body or {}, headers=headers)


def keys(person):
    return person.json(
        "/messages/keys",
        {"public_key": PUBLIC, "wrapped_private": "blob", "salt": "salt", "iterations": 600_000},
    )


def wraps(*ids):
    return {user: {"wrapped": f"w-{user}", "sender_key": PUBLIC} for user in ids}


def boot(tmp_path):
    app, _ = build_app(tmp_path)
    return app


def test_messaging_is_hidden_without_the_permission_and_when_switched_off(tmp_path):
    app = boot(tmp_path)
    app.state.ctx.accounts.save_role("Silent", "", ["chat.use"])
    with Person(app, "mute", "Silent") as mute, Person(app, "ann") as ann:
        mute.sign_in()
        ann.sign_in()
        for path in ("/messages", "/announcements", "/messages/unread", "/messages/keys"):
            assert mute.client.get(path).status_code == 404, path
        assert (
            mute.browser.post(
                "/messages/direct", {"other": ann.user.id}, token=mute.token
            ).status_code
            == 404
        )
        assert ann.client.get("/messages").status_code == 200
        assert 'href="/messages"' not in mute.client.get("/account").text
        app.state.ctx.policy.update(messaging_enabled=False)
        assert ann.client.get("/messages").status_code == 404
        assert 'href="/messages"' not in ann.client.get("/account").text


def test_a_direct_conversation_between_two_people_with_unread_badges(tmp_path):
    app = boot(tmp_path)
    with Person(app, "ann") as ann, Person(app, "bob") as bob:
        ann.sign_in()
        bob.sign_in()
        opened = ann.browser.post("/messages/direct", {"other": bob.user.id}, token=ann.token)
        assert opened.status_code == 303 and opened.headers["location"].startswith("/messages?c=")
        conversation = opened.headers["location"].split("=")[1]
        sent = ann.json(f"/messages/c/{conversation}/send", {"text": "Hello <b>Bob</b>"})
        assert sent.status_code == 200 and sent.json()["message"]["mine"] is True
        assert bob.json("/messages/unread", method="get").json() == {
            "messages": 1,
            "announcements": 0,
        }
        assert "Messages (1)" in bob.client.get("/account").text
        page = bob.client.get(f"/messages?c={conversation}").text
        assert "Hello &lt;b&gt;Bob&lt;/b&gt;" in page and "<b>Bob</b>" not in page
        assert bob.json("/messages/unread", method="get").json()["messages"] == 0
        reply = bob.json(f"/messages/c/{conversation}/send", {"text": "Hi Ann"})
        assert reply.status_code == 200
        feed = ann.json(f"/messages/c/{conversation}/feed?after=0", method="get").json()
        assert [item["text"] for item in feed["messages"]] == ["Hello <b>Bob</b>", "Hi Ann"]
        newer = ann.json(
            f"/messages/c/{conversation}/feed?after={feed['messages'][0]['id']}", method="get"
        )
        assert [item["text"] for item in newer.json()["messages"]] == ["Hi Ann"]


def test_outsiders_cannot_read_write_or_manage_a_conversation(tmp_path):
    app = boot(tmp_path)
    with Person(app, "ann") as ann, Person(app, "bob") as bob, Person(app, "cy") as cy:
        for person in (ann, bob, cy):
            person.sign_in()
        conversation = (
            ann.browser.post("/messages/direct", {"other": bob.user.id}, token=ann.token)
            .headers["location"]
            .split("=")[1]
        )
        for method, path, body in (
            ("get", f"/messages/c/{conversation}/feed", None),
            ("post", f"/messages/c/{conversation}/send", {"text": "x"}),
            ("post", f"/messages/c/{conversation}/read", {"upto": 1}),
            ("get", f"/messages/c/{conversation}/keys", None),
        ):
            assert cy.json(path, body, method=method).status_code == 404, path
        assert cy.client.get(f"/messages?c={conversation}").text.count("Nothing here yet") == 0
        sent = ann.json(f"/messages/c/{conversation}/send", {"text": "private"}).json()["message"]
        assert cy.json(f"/messages/m/{sent['id']}/remove").status_code == 404
        assert (
            cy.browser.post(f"/messages/c/{conversation}/delete", token=cy.token).status_code == 404
        )
        assert ann.json(f"/messages/m/{sent['id']}/remove").status_code == 204


def test_json_endpoints_demand_the_csrf_header_and_a_same_origin_request(tmp_path):
    app = boot(tmp_path)
    with Person(app, "ann") as ann, Person(app, "bob") as bob:
        ann.sign_in()
        bob.sign_in()
        conversation = (
            ann.browser.post("/messages/direct", {"other": bob.user.id}, token=ann.token)
            .headers["location"]
            .split("=")[1]
        )
        url = f"/messages/c/{conversation}/send"
        assert (
            ann.client.post(url, json={"text": "x"}, headers={"origin": ORIGIN}).status_code == 403
        )
        assert (
            ann.client.post(
                url, json={"text": "x"}, headers={"X-CSRF-Token": "wrong", "origin": ORIGIN}
            ).status_code
            == 403
        )
        assert (
            ann.client.post(
                url,
                json={"text": "x"},
                headers={"X-CSRF-Token": ann.token, "origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert ann.json(url, {"text": ""}).status_code == 422
        assert (
            ann.client.post(
                url, content="[1]", headers={"X-CSRF-Token": ann.token, "origin": ORIGIN}
            ).status_code
            == 422
        )


def test_sending_is_rate_limited_per_person(tmp_path):
    app = boot(tmp_path)
    with Person(app, "ann") as ann, Person(app, "bob") as bob:
        ann.sign_in()
        bob.sign_in()
        conversation = (
            ann.browser.post("/messages/direct", {"other": bob.user.id}, token=ann.token)
            .headers["location"]
            .split("=")[1]
        )
        app.state.ctx.message_limiter = RateLimiter(2, 60)
        codes = [
            ann.json(f"/messages/c/{conversation}/send", {"text": str(n)}).status_code
            for n in range(3)
        ]
        assert codes == [200, 200, 429]
        assert bob.json(f"/messages/c/{conversation}/send", {"text": "mine"}).status_code == 200


def test_groups_need_their_permission_and_are_managed_by_their_owner(tmp_path):
    app = boot(tmp_path)
    with Person(app, "ann") as ann, Person(app, "bob") as bob, Person(app, "vic", "viewer") as vic:
        for person in (ann, bob, vic):
            person.sign_in()
        denied = vic.browser.post(
            "/messages/groups", {"title": "Mine", "members": [ann.user.id]}, token=vic.token
        )
        assert denied.status_code == 404
        made = ann.browser.post(
            "/messages/groups",
            {"title": "Hiring team", "members": [bob.user.id, vic.user.id]},
            token=ann.token,
        )
        assert made.status_code == 303 and "done=created" in made.headers["location"]
        group = re.search(r"c=([^&]+)", made.headers["location"]).group(1)
        assert "Hiring team" in vic.client.get("/messages").text
        ann.json(f"/messages/c/{group}/send", {"text": "welcome"})
        assert vic.client.get(f"/messages?c={group}").text.count("welcome") >= 1
        sneaky = bob.browser.post(
            f"/messages/c/{group}/members", {"members": [bob.user.id]}, token=bob.token
        )
        assert sneaky.status_code == 400 and "cannot change" in sneaky.text
        assert (
            bob.browser.post(
                f"/messages/c/{group}/rename", {"title": "Mine now"}, token=bob.token
            ).status_code
            == 400
        )
        assert bob.browser.post(f"/messages/c/{group}/delete", token=bob.token).status_code == 400
        assert (
            ann.browser.post(
                f"/messages/c/{group}/rename", {"title": "Renamed"}, token=ann.token
            ).status_code
            == 303
        )
        assert (
            ann.browser.post(
                f"/messages/c/{group}/leave", {"user": vic.user.id}, token=ann.token
            ).status_code
            == 303
        )
        assert vic.client.get(f"/messages/c/{group}/feed").status_code == 404
        assert (
            bob.browser.post(f"/messages/c/{group}/leave", token=bob.token).headers["location"]
            == "/messages"
        )
        assert ann.browser.post(f"/messages/c/{group}/delete", token=ann.token).status_code == 303
        assert ann.client.get(f"/messages/c/{group}/feed").status_code == 404
        invalid = ann.browser.post(
            "/messages/groups", {"title": "x", "members": []}, token=ann.token
        )
        assert invalid.status_code == 400 and "name" in invalid.text


def test_a_role_channel_is_open_to_exactly_that_role(tmp_path):
    app = boot(tmp_path)
    with Person(app, "ann") as ann, Person(app, "bob") as bob, Person(app, "vic", "viewer") as vic:
        for person in (ann, bob, vic):
            person.sign_in()
        opened = ann.browser.post("/messages/role", token=ann.token)
        channel = opened.headers["location"].split("=")[1]
        ann.json(f"/messages/c/{channel}/send", {"text": "recruiters only"})
        assert (
            bob.json(f"/messages/c/{channel}/feed", method="get").json()["messages"][0]["text"]
            == "recruiters only"
        )
        assert vic.json(f"/messages/c/{channel}/feed", method="get").status_code == 404
        assert "recruiters only" not in vic.client.get(f"/messages?c={channel}").text


def test_private_conversations_keep_the_server_blind_to_the_text(tmp_path):
    app = boot(tmp_path)
    with Person(app, "ann") as ann, Person(app, "bob") as bob, Person(app, "cy") as cy:
        for person in (ann, bob, cy):
            person.sign_in()
        assert (
            ann.json(
                "/messages/direct",
                {"other": bob.user.id, "encrypted": True, "wraps": wraps(ann.user.id, bob.user.id)},
            ).status_code
            == 422
        )
        for person in (ann, bob):
            saved = keys(person)
            assert (
                saved.status_code == 200 and len(saved.json()["keys"]["fingerprint"].split()) == 8
            )
        assert keys(ann).status_code == 422
        assert (
            ann.json(
                "/messages/keys",
                {
                    "public_key": "x",
                    "wrapped_private": "b",
                    "salt": "s",
                    "iterations": 1,
                    "replace": True,
                    "password": PASSWORD,
                },
            ).status_code
            == 422
        )
        assert bob.json(f"/messages/keys/{ann.user.id}", method="get").json()["public_key"]
        assert bob.json("/messages/keys/none", method="get").status_code == 404
        assert cy.json(f"/messages/keys/{cy.user.id}", method="get").status_code == 404
        created = ann.json(
            "/messages/direct",
            {"other": bob.user.id, "encrypted": True, "wraps": wraps(ann.user.id, bob.user.id)},
        )
        assert created.status_code == 200
        conversation = created.json()["id"]
        assert ann.json(f"/messages/c/{conversation}/send", {"text": "plain"}).status_code == 422
        sent = ann.json(f"/messages/c/{conversation}/send", {"cipher": CIPHER, "version": 1})
        assert sent.status_code == 200 and sent.json()["message"]["text"] == ""
        feed = bob.json(f"/messages/c/{conversation}/feed", method="get").json()["messages"][0]
        assert feed["cipher"] == CIPHER and feed["encrypted"] is True and feed["text"] == ""
        wrapped = bob.json(f"/messages/c/{conversation}/keys", method="get").json()
        assert wrapped["keys"][0]["wrapped"] == f"w-{bob.user.id}" and wrapped["version"] == 1
        page = bob.client.get(f"/messages?c={conversation}").text
        assert CIPHER.replace('"', "&#34;") in page and "Locked" in page
        assert 'data-encrypted="1"' in page and 'data-peers="/messages/c/' in page
        stored = app.state.ctx.store.query("SELECT body FROM chat_messages")[0]["body"]
        assert stored == CIPHER
        assert cy.json(f"/messages/c/{conversation}/feed", method="get").status_code == 404
        app.state.ctx.policy.update(messaging_e2ee=False)
        refused = ann.json(
            "/messages/direct", {"other": cy.user.id, "encrypted": True, "wraps": {}}
        )
        assert refused.status_code == 422 and "switched off" in refused.json()["error"]
        assert "Private messages" not in ann.client.get("/messages").text


def test_announcements_reach_their_audience_and_show_as_a_banner(tmp_path):
    app = boot(tmp_path)
    with (
        Person(app, "boss", "manager") as boss,
        Person(app, "ann") as ann,
        Person(app, "vic", "viewer") as vic,
    ):
        for person in (boss, ann, vic):
            person.sign_in()
        recruiters = app.state.ctx.accounts.role_named("recruiter").id
        assert (
            ann.browser.post(
                "/announcements", {"title": "x", "body": "y"}, token=ann.token
            ).status_code
            == 404
        )
        assert "Post an announcement" not in ann.client.get("/announcements").text
        posted = boss.browser.post(
            "/announcements",
            {
                "title": "Office <closed>",
                "body": "On Friday",
                "audience": f"role:{recruiters}",
                "days": "7",
                "pinned": "yes",
            },
            token=boss.token,
        )
        assert posted.status_code == 303
        everyone = boss.browser.post(
            "/announcements",
            {"title": "Hello all", "body": "Welcome", "audience": "all", "days": "0"},
            token=boss.token,
        )
        assert everyone.status_code == 303
        page = ann.client.get("/account").text
        assert "Office &lt;closed&gt;" in page and "Hello all" in page and 'class="banner"' in page
        assert "Announcements (2)" in page
        assert (
            "Office" not in vic.client.get("/account").text
            and "Hello all" in vic.client.get("/account").text
        )
        listing = ann.client.get("/announcements").text
        assert "until" in listing and "pinned" in listing and "Mark as read" in listing
        item = app.state.ctx.messenger.announcements(ann.user.id)[0]
        read = ann.browser.post(
            f"/announcements/{item.id}/read", {"next": "https://evil.example"}, token=ann.token
        )
        assert read.status_code == 303 and read.headers["location"] == "/announcements"
        assert "Announcements (1)" in ann.client.get("/account").text
        bad = boss.browser.post(
            "/announcements",
            {"title": "", "body": "x", "audience": "all", "days": "0"},
            token=boss.token,
        )
        assert bad.status_code == 400
        wrong = boss.browser.post(
            "/announcements",
            {"title": "t", "body": "b", "audience": "role:none", "days": "0"},
            token=boss.token,
        )
        assert wrong.status_code == 400 and "role does not exist" in wrong.text
        assert (
            ann.browser.post(f"/announcements/{item.id}/delete", token=ann.token).status_code == 404
        )
        assert (
            boss.browser.post(f"/announcements/{item.id}/delete", token=boss.token).status_code
            == 303
        )
        assert vic.browser.post("/announcements/none/delete", token=vic.token).status_code == 404


def test_groups_can_be_the_audience_of_an_announcement(tmp_path):
    app = boot(tmp_path)
    app.state.ctx.accounts.save_role(
        "Poster", "", ["messages.use", "messages.groups", "messages.announce"]
    )
    with (
        Person(app, "ann", "Poster") as ann,
        Person(app, "bob") as bob,
        Person(app, "cy") as cy,
        Person(app, "boss", "manager") as boss,
    ):
        for person in (ann, bob, cy, boss):
            person.sign_in()
        created = ann.browser.post(
            "/messages/groups", {"title": "Squad", "members": [bob.user.id]}, token=ann.token
        )
        group = re.search(r"c=([^&]+)", created.headers["location"]).group(1)
        assert "Group: Squad" in ann.client.get("/announcements").text
        assert "Group: Squad" not in boss.client.get("/announcements").text
        outsider = boss.browser.post(
            "/announcements",
            {"title": "Squad only", "body": "x", "audience": f"group:{group}", "days": "0"},
            token=boss.token,
        )
        assert outsider.status_code == 400
        ann.browser.post(
            "/announcements",
            {"title": "Squad only", "body": "x", "audience": f"group:{group}", "days": "0"},
            token=ann.token,
        )
        messenger = app.state.ctx.messenger
        assert [item.title for item in messenger.announcements(bob.user.id)] == ["Squad only"]
        assert messenger.announcements(cy.user.id) == []


def reset_body(**changes):
    return {
        "public_key": PUBLIC,
        "wrapped_private": "fresh",
        "salt": "salt",
        "iterations": 600_000,
        "replace": True,
        **changes,
    }


def test_resetting_keys_needs_the_account_password(tmp_path):
    app = boot(tmp_path)
    with Person(app, "ann") as ann:
        ann.sign_in()
        assert keys(ann).status_code == 200
        assert ann.json("/messages/keys", reset_body()).status_code == 422
        assert ann.json("/messages/keys", reset_body(password="wrong password")).status_code == 422
        stored = app.state.ctx.messenger.keys_of(ann.user.id)
        assert stored.wrapped_private == "blob"
        assert ann.json("/messages/keys", reset_body(password=PASSWORD)).status_code == 200
        assert app.state.ctx.messenger.keys_of(ann.user.id).wrapped_private == "fresh"


def test_key_endpoints_are_throttled(tmp_path):
    app = boot(tmp_path)
    with Person(app, "ann") as ann:
        ann.sign_in()
        limit = app.state.ctx.web.account_actions
        codes = [
            ann.json("/messages/keys", reset_body(password="nope")).status_code
            for _ in range(limit + 3)
        ]
        assert 429 in codes


def test_peers_and_rotation_serve_only_members_and_need_their_own_key(tmp_path):
    app = boot(tmp_path)
    with Person(app, "ann") as ann, Person(app, "bob") as bob, Person(app, "cy") as cy:
        for person in (ann, bob, cy):
            person.sign_in()
            assert keys(person).status_code == 200
        created = ann.json(
            "/messages/direct",
            {"other": bob.user.id, "encrypted": True, "wraps": wraps(ann.user.id, bob.user.id)},
        )
        conversation = created.json()["id"]
        found = bob.json(f"/messages/c/{conversation}/peers", method="get").json()["peers"]
        assert set(found) == {ann.user.id, bob.user.id} and found[ann.user.id]["eligible"] is True
        assert cy.json(f"/messages/c/{conversation}/peers", method="get").status_code == 404
        assert cy.json(f"/messages/c/{conversation}/rotate", {"wraps": {}}).status_code == 404
        wrong = {**wraps(ann.user.id, bob.user.id)}
        wrong[bob.user.id] = {"wrapped": "w", "sender_key": PUBLIC.replace("AAAA", "ZZZZ")}
        assert bob.json(f"/messages/c/{conversation}/rotate", {"wraps": wrong}).status_code == 422
        done = bob.json(
            f"/messages/c/{conversation}/rotate", {"wraps": wraps(ann.user.id, bob.user.id)}
        )
        assert done.status_code == 200 and done.json()["version"] == 2
        assert ann.json(f"/messages/c/{conversation}/feed", method="get").json()["key_version"] == 2


def test_creating_conversations_is_rate_limited(tmp_path):
    app = boot(tmp_path)
    with Person(app, "ann") as ann, Person(app, "bob") as bob:
        ann.sign_in()
        bob.sign_in()
        app.state.ctx.message_limiter = RateLimiter(1, 60)
        first = ann.json("/messages/direct", {"other": bob.user.id})
        second = ann.json("/messages/direct", {"other": bob.user.id})
        assert first.status_code == 200 and second.status_code == 422
        assert "fast" in second.json()["error"]


def test_odd_numbers_and_nested_json_never_crash_the_message_endpoints(tmp_path):
    app = boot(tmp_path)
    with Person(app, "ann") as ann, Person(app, "bob") as bob:
        ann.sign_in()
        conversation = ann.json("/messages/direct", {"other": bob.user.id}).json()["id"]
        huge = "9" * 40
        assert (
            ann.json(f"/messages/c/{conversation}/feed?after={huge}", method="get").status_code
            == 200
        )
        assert (
            ann.json(
                f"/messages/c/{conversation}/feed?after=\u0661\u0662", method="get"
            ).status_code
            == 200
        )
        assert ann.json(f"/messages/c/{conversation}/read", {"upto": 10**30}).status_code == 204
        assert ann.json(f"/messages/m/{huge}/remove").status_code == 404
        nested = "[" * 100_000
        raw = ann.client.post(
            f"/messages/c/{conversation}/send",
            content=nested,
            headers={
                "X-CSRF-Token": ann.token,
                "origin": ORIGIN,
                "content-type": "application/json",
            },
        )
        assert raw.status_code == 422
