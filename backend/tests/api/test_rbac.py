"""RBAC matrix generated from the app's own route table: every route is probed with every role."""
import asyncio
import re

import httpx
import pytest

from app.security import ROLE_RANK

from .helpers import ApiClient, build_app, make_client

SKIP_PATHS = {"/api/auth/login"}

# Independent oracle (from the product spec): which operations need the admin role.
ADMIN_RULES = [
    ("*", r"^/api/users(/.*)?$"), ("GET", r"^/api/audit$"),
    ("PUT", r"^/api/settings/\w+$"), ("POST", r"^/api/settings/retention/run$"),
    ("POST", r"^/api/system/(pause-all|resume-processing|restart-failed|smtp-egress-check)$"), ("PUT", r"^/api/system/maintenance$"),
    ("POST", r"^/api/categories/(\d+/(disable|enable|merge)|import)$"), ("DELETE", r"^/api/categories/\d+$"),
    ("POST", r"^/api/businesses/merge$"), ("DELETE", r"^/api/contacts/\d+$"), ("DELETE", r"^/api/suppressions/\d+$"),
    ("POST", r"^/api/sources/[\w-]+/(enable|disable)$"), ("PUT", r"^/api/sources/[\w-]+/(priority|config)$"),
    ("PUT", r"^/api/verification/domain-permissions/[\w.-]+$"), ("DELETE", r"^/api/verification/domain-permissions/[\w.-]+$"),
]


def expected_min_role(method: str, path: str) -> int:
    if path.startswith("/api/auth/"):
        return 1
    for m, rx in ADMIN_RULES:
        if m in ("*", method) and re.match(rx, path):
            return 3
    return 1 if method == "GET" else 2


def fill(path: str) -> str:
    def sub(m):
        name = m.group(1)
        return {"domain": "example.com", "section": "validation", "adapter_id": "company_websites", "code": "GB"}.get(name, "999999")

    return re.sub(r"\{(\w+)\}", sub, path)


def routes(app):
    out = []
    for path, ops in app.openapi()["paths"].items():
        if not path.startswith("/api") or path.startswith("/api/auth/") or path.endswith("/events"):  # auth endpoints are covered in test_auth
            continue
        for method in ops:
            out.append((method.upper(), path, expected_min_role(method.upper(), fill(path))))
    return out


async def test_openapi_operation_ids_are_unique_function_names():
    app = build_app()
    ids = [op["operationId"] for ops in app.openapi()["paths"].values() for op in ops.values()]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"[a-z_0-9]+", i) for i in ids)


async def test_rbac_matrix(monkeypatch):
    async def refuse(*a, **k):
        raise ConnectionRefusedError

    monkeypatch.setattr(asyncio, "open_connection", refuse)  # smtp-egress-check must not touch the network
    app = build_app()
    table = routes(app)
    assert len(table) > 80
    clients = {}
    async with make_client("viewer", app) as v, make_client("operator", app) as o, make_client("admin", app) as a:
        clients = {"viewer": v, "operator": o, "admin": a}
        failures = []
        for method, path, need in table:
            url = fill(path)
            for role, c in clients.items():
                kw = {"json": {}} if method in ("POST", "PUT", "PATCH") else {}
                r = await c.request(method, url, **kw)
                allowed = ROLE_RANK[role] >= need
                if allowed and r.status_code in (401, 403):
                    failures.append(f"{role} should be allowed {method} {path} -> {r.status_code} {r.text[:120]}")
                if not allowed and r.status_code != 403:
                    failures.append(f"{role} should be forbidden {method} {path} -> {r.status_code}")
        assert failures == [], "\n".join(failures)


@pytest.mark.parametrize("path", ["/api/users", "/api/audit", "/api/system/maintenance"])
async def test_operator_cannot_reach_admin_areas(operator_client, path):
    if path == "/api/system/maintenance":
        r = await operator_client.put(path, json={"enabled": True})
    else:
        r = await operator_client.get(path)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


async def test_operator_cannot_change_settings_but_can_read_basic(operator_client):
    assert (await operator_client.put("/api/settings/exports", json={})).status_code == 403
    s = (await operator_client.get("/api/settings")).json()
    assert s["exports"] is not None and s["worker"] is None  # advanced sections hidden


async def test_viewer_is_read_only_but_can_manage_own_session(viewer_client):
    c = viewer_client
    assert (await c.get("/api/runs")).status_code == 200
    r = await c.post("/api/presets", json={"kind": "run", "name": "x", "config": {}})
    assert r.status_code == 403
    assert (await c.post("/api/auth/logout")).status_code == 200


async def test_forged_role_in_request_is_ignored(viewer_client):
    r = await viewer_client.post("/api/users", json={"email": "x@example.com", "password": "Another-Strong-Pass-55", "role": "admin"}, headers={"X-Role": "admin"})
    assert r.status_code == 403
    _ = (ApiClient, httpx)
