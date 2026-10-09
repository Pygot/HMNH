# tests/test_route_guards.py
from agent.web.api_core import ENDPOINTS
from tests.web_helpers import build_app
from agent.config import PolicyTuning
from agent.permissions import KNOWN

LOGIN_ONLY = {
    ("/account", "GET"),
    ("/account/profile", "POST"),
    ("/account/password", "POST"),
    ("/account/email/code", "POST"),
    ("/account/email/confirm", "POST"),
    ("/account/email-login", "POST"),
    ("/account/sessions/revoke", "POST"),
    ("/account/2fa", "GET"),
    ("/account/2fa/enable", "POST"),
    ("/account/2fa/recovery", "POST"),
    ("/account/2fa/disable", "POST"),
    ("/admin", "GET"),
    ("/settings", "GET"),
    ("/settings", "POST"),
    ("/settings/reset", "POST"),
    ("/live-view", "POST"),
    ("/logout", "POST"),
}


def methods_of(route):
    return {method for method in route.methods if method != "HEAD"}


def test_every_route_declares_who_may_use_it(tmp_path):
    app, _ = build_app(tmp_path)
    undeclared = []
    for route in app.routes:
        endpoint = route.endpoint
        if route.path.startswith("/api/v1/"):
            registered = {(item["method"], item["path"]) for item in ENDPOINTS}
            if not all((method, route.path) in registered for method in methods_of(route)):
                undeclared.append(route.path)
        elif not (hasattr(endpoint, "permission") or getattr(endpoint, "public", False)):
            undeclared.append(route.path)
    assert undeclared == []


def test_the_pages_open_to_every_signed_in_person_are_a_short_known_list(tmp_path):
    app, _ = build_app(tmp_path)
    open_to_all = set()
    for route in app.routes:
        endpoint = route.endpoint
        if getattr(endpoint, "public", False) or not hasattr(endpoint, "permission"):
            continue
        if endpoint.permission is None:
            open_to_all.update((route.path, method) for method in methods_of(route))
    assert open_to_all == LOGIN_ONLY


def test_every_api_endpoint_names_a_real_permission_and_module():
    for item in ENDPOINTS:
        assert item["permission"] in KNOWN, item
        assert item["module"] is None or item["module"] in PolicyTuning.model_fields, item
    assert len({(item["method"], item["path"]) for item in ENDPOINTS}) == len(ENDPOINTS)
