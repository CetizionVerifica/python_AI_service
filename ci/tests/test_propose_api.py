"""``POST /v1/excel/propose`` and the proposal path of ``/preview`` with a
fake ESG-lite (httpx MockTransport) and a signed-in caller: the caller's own
token is forwarded, the site must be the caller's, ESG-lite's refusals pass
through, and the rate limit holds. No database: the document lookup and
download are patched."""
import json

import httpx
import pytest

from app.core.auth import Principal, authenticate
from app.main import app
from app.services import excel_plan_client as plans
from app.services.mapping import api as mapping_api
from ci.tests.conftest import FIXTURES
from ci.tests.mapping_fixtures import ACTIVITY_NAMES, CONTEXT, OTHER_ACTIVITY_NAMES, OTHER_COMPANY_CONTEXT

XI1 = FIXTURES / "xi1"
DOC = 921
MANAGER = Principal(kind="user", user_id=2, role="Manager", site_ids=frozenset({2}))
SUPERADMIN = Principal(kind="user", user_id=53, role="Superadmin", site_ids=None)
OUTSIDER = Principal(kind="user", user_id=77, role="Manager", site_ids=frozenset({7}))


class FakeEsgLite:
    """Context, activities, profiles and plans the way ESG-lite answers, by token."""

    def __init__(self):
        self.requests: list[dict] = []
        self.profiles: list[dict] = []
        self.plans: dict[str, dict] = {}
        self.disabled = False

    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        path = request.url.path
        self.requests.append({"method": request.method, "path": path, "auth": auth, "params": dict(request.url.params), "body": json.loads(request.content) if request.content else None})
        if auth == "Bearer outsider":
            context, names = OTHER_COMPANY_CONTEXT, OTHER_ACTIVITY_NAMES
        else:
            context, names = CONTEXT, ACTIVITY_NAMES
        if self.disabled:
            return httpx.Response(403, json={"code": "AI_IMPORT_DISABLED_FOR_CLIENT", "message": "Smart import is switched off for this client."})
        if path == "/imports/context":
            return httpx.Response(200, json=context)
        if path == "/imports/context/activities":
            cid = int(request.url.params["category_id"])
            q = request.url.params.get("q", "").lower()
            rows = [{"name": n, "kind": "factor", "units": [], "latest_year": 2025} for n in names.get(cid, []) if not q or q in n.lower()]
            return httpx.Response(200, json=rows)
        if path == "/imports/mapping-profiles":
            fp = request.url.params.get("fingerprint")
            return httpx.Response(200, json=[p for p in self.profiles if p["fingerprint"] == fp])
        if path == "/imports/plans" and request.method == "POST":
            body = json.loads(request.content)
            plan_id = f"plan-{len(self.plans) + 1}"
            self.plans[plan_id] = {"rows": body["rows"], "mapping": body.get("mapping")}
            return httpx.Response(201, json=self._plan(plan_id))
        if path.startswith("/imports/plans/") and request.method == "GET":
            plan_id = path.split("/")[3]
            rows = [{"row_ref": r["row_ref"], "state": "ready", "input": r, "result": {"total_tco2e": 1.0}, "issues": []} for r in self.plans[plan_id]["rows"]]
            return httpx.Response(200, json={**self._plan(plan_id), "rows": rows[:100], "page": 1, "page_size": 100, "total_rows": len(rows)})
        return httpx.Response(404, json={"code": "NOT_FOUND", "message": "?"})

    def _plan(self, plan_id):
        n = len(self.plans[plan_id]["rows"])
        return {"plan_id": plan_id, "version": 1, "status": "draft", "counts": {"rows": n, "ready": n, "issue": 0, "skipped": 0, "warnings": 0}, "totals": {"tco2e": float(n), "by_category": [], "by_month": []}, "issues_summary": [], "expires_at": "2026-01-01T00:00:00.000Z"}


@pytest.fixture
def api(monkeypatch):
    from fastapi.testclient import TestClient

    esg = FakeEsgLite()

    class WiredClient(plans.PlanClient):
        def __init__(self, token, **kw):
            super().__init__(token, base_url="http://esg-lite.test", transport=esg.transport())

    monkeypatch.setattr(plans, "PlanClient", WiredClient)
    state = {"principal": MANAGER, "file": "01-single-sheet-monthly.xlsx"}
    app.dependency_overrides[authenticate] = lambda: state["principal"]

    import app.core.database as db
    import app.services.excel_parser as ep
    from app.api import excel as excel_api

    monkeypatch.setattr(db, "get_uploaded_document_by_id", lambda doc_id: {"id": doc_id, "uploaded_by": 2, "document_name": state["file"]} if doc_id == DOC else None)
    monkeypatch.setattr(excel_api, "get_uploaded_document_by_id", db.get_uploaded_document_by_id)
    monkeypatch.setattr(ep, "download_document_bytes", lambda doc_id: ((XI1 / state["file"]).read_bytes(), state["file"].rsplit(".", 1)[-1]))
    monkeypatch.setattr(excel_api, "company_id_for_site", lambda site_id: 1)
    mapping_api.reset_propose_rate()
    try:
        yield TestClient(app), esg, state
    finally:
        app.dependency_overrides.pop(authenticate, None)
        mapping_api.reset_propose_rate()


def auth(token="manager-token"):
    return {"Authorization": f"Bearer {token}"}


def test_propose_reads_context_and_names_with_the_callers_token(api):
    client, esg, _ = api
    res = client.post("/v1/excel/propose", json={"document_id": DOC, "site_id": 2}, headers=auth())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["document_id"] == DOC and body["site_id"] == 2 and len(body["sheets"]) == 1
    sheet = body["sheets"][0]
    assert sheet["category"]["id"] == 3 and sheet["period"] == {"kind": "column", "column": "Month"}
    assert body["model"] == {"name": "none", "calls": 0, "cached_profile_hits": 0}
    assert {r["path"] for r in esg.requests} >= {"/imports/context", "/imports/context/activities", "/imports/mapping-profiles"}
    assert all(r["auth"] == "Bearer manager-token" for r in esg.requests)
    assert all("company_id" not in r["params"] for r in esg.requests)


def test_superadmin_names_the_client_on_every_call(api):
    client, esg, state = api
    state["principal"] = SUPERADMIN
    res = client.post("/v1/excel/propose", json={"document_id": DOC, "site_id": 2}, headers=auth("root"))
    assert res.status_code == 200, res.text
    assert all(r["params"].get("company_id") == "1" for r in esg.requests)


def test_a_site_the_caller_does_not_hold_is_forbidden_before_any_call(api):
    client, esg, _ = api
    res = client.post("/v1/excel/propose", json={"document_id": DOC, "site_id": 9}, headers=auth())
    assert res.status_code == 403  # enforce_site_scope, before the route runs
    assert esg.requests == []


def test_a_site_outside_the_context_is_forbidden_even_when_the_token_says_otherwise(api):
    client, esg, state = api
    state["principal"] = Principal(kind="user", user_id=2, role="Manager", site_ids=frozenset({2, 9}))
    res = client.post("/v1/excel/propose", json={"document_id": DOC, "site_id": 9}, headers=auth())
    # Site 9 is in the context's site list but holds no categories; site 7 is not there at all.
    assert res.status_code == 422 and "no categories" in res.json()["detail"]
    state["principal"] = Principal(kind="user", user_id=2, role="Manager", site_ids=frozenset({7}))
    res = client.post("/v1/excel/propose", json={"document_id": DOC, "site_id": 7}, headers=auth())
    assert res.status_code == 403 and res.json()["detail"]["code"] == "IMPORT_FORBIDDEN"


def test_another_companys_manager_gets_their_own_context_and_never_company_ones_names(api):
    client, esg, state = api
    state["principal"] = OUTSIDER
    res = client.post("/v1/excel/propose", json={"document_id": DOC, "site_id": 7}, headers=auth("outsider"))
    # The document belongs to user 2, so the outsider cannot even read it.
    assert res.status_code == 404
    assert esg.requests == []


def test_esg_lite_refusals_pass_through(api):
    client, esg, _ = api
    esg.disabled = True
    res = client.post("/v1/excel/propose", json={"document_id": DOC, "site_id": 2}, headers=auth())
    assert res.status_code == 403 and res.json()["detail"]["code"] == "AI_IMPORT_DISABLED_FOR_CLIENT"


def test_a_saved_profile_is_used_and_counted(api):
    client, esg, _ = api
    first = client.post("/v1/excel/propose", json={"document_id": DOC, "site_id": 2}, headers=auth()).json()["sheets"][0]
    esg.profiles = [{"id": "0f6a6e3c-1111-4aaa-8bbb-ccccccccdddd", "company_id": 1, "site_id": 2, "category_id": None, "fingerprint": first["fingerprint"], "sheet_proposal": first, "uses": 1, "created_at": "x", "updated_at": "x"}]
    body = client.post("/v1/excel/propose", json={"document_id": DOC, "site_id": 2}, headers=auth()).json()
    assert body["model"]["cached_profile_hits"] == 1 and body["sheets"][0]["category"]["source"] == "profile"


def test_rate_limit_after_ten_proposals(api):
    client, _, _ = api
    for _ in range(10):
        assert client.post("/v1/excel/propose", json={"document_id": DOC, "site_id": 2}, headers=auth()).status_code == 200
    res = client.post("/v1/excel/propose", json={"document_id": DOC, "site_id": 2}, headers=auth())
    assert res.status_code == 429 and res.json()["detail"]["code"] == "RATE_LIMITED"


def test_preview_with_a_confirmed_proposal_plans_the_rows_and_keeps_the_mapping(api):
    client, esg, _ = api
    proposal = client.post("/v1/excel/propose", json={"document_id": DOC, "site_id": 2}, headers=auth()).json()
    res = client.post("/v1/excel/preview", json={"document_id": DOC, "proposal": proposal}, headers=auth())
    assert res.status_code == 200, res.text
    create = next(r for r in esg.requests if r["method"] == "POST" and r["path"] == "/imports/plans")
    assert create["body"]["channel"] == "bulk_upload" and len(create["body"]["rows"]) == 8
    assert create["body"]["mapping"]["site_id"] == 2 and create["body"]["rows"][0]["meta"]["source"] == "rules"
    assert res.json()["plan"]["counts"]["rows"] == 8 and res.json()["sheet_rows"] == 8


def test_preview_with_a_proposal_for_another_document_or_site_is_refused(api):
    client, esg, _ = api
    proposal = client.post("/v1/excel/propose", json={"document_id": DOC, "site_id": 2}, headers=auth()).json()
    res = client.post("/v1/excel/preview", json={"document_id": DOC, "proposal": {**proposal, "document_id": 5}}, headers=auth())
    assert res.status_code == 422
    bad = json.loads(json.dumps(proposal))
    bad["sheets"][0]["site_id"] = 9
    res = client.post("/v1/excel/preview", json={"document_id": DOC, "proposal": bad}, headers=auth())
    assert res.status_code == 422
    assert not any(r["path"] == "/imports/plans" for r in esg.requests)
