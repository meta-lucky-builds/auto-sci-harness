"""The UI's local server (GOAL 18.7): the same view through HTTP as through the class, loopback only, a closed list of
routes, and no way to take a human step."""

from __future__ import annotations

import ast
import http.client
import inspect
import json
import threading
from pathlib import Path

import pytest

from _experience_world import World
from custom_harness.court import minted as C
from custom_harness.experience import Experience
from custom_harness.experience import schemas as M
from custom_harness.experience import server as V
from custom_harness.experience.scripted import NOTICE, ScriptedExperience
from custom_harness.experience.words import jargon_problems
from custom_harness.kerneld.releases import released

SRC = Path(__file__).resolve().parents[1] / "src" / "custom_harness"


@pytest.fixture
def scripted(tmp_path):
    (tmp_path / "index.html").write_text('<meta content="__CHH_TOKEN__">', encoding="utf-8")
    (tmp_path / "a.js").write_text("1", encoding="utf-8")
    (tmp_path.parent / "secret.txt").write_text("secret", encoding="utf-8")
    srv = V.make_server(ScriptedExperience, 0, dist=tmp_path)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None, *, host=None, origin=None, token=True):
    port = srv.server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    head = {"Host": host or f"127.0.0.1:{port}"}
    if origin:
        head["Origin"] = origin
    if token:
        head["X-CHH-Token"] = srv.RequestHandlerClass.app.token
    c.request(method, path, json.dumps(body) if body is not None else None, head)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def test_the_server_binds_to_the_loopback_interface_and_has_no_address_parameter(scripted):
    assert scripted.server_address[0] == "127.0.0.1"
    assert "host" not in inspect.signature(V.make_server).parameters
    assert "address" not in " ".join(inspect.signature(V.serve).parameters)


def test_another_site_is_refused_by_host_origin_and_token(scripted):
    assert call(scripted, "GET", "/api/whoami", host="evil.example:80")[0] == 403  # DNS rebinding
    assert call(scripted, "GET", "/api/whoami", origin="https://evil.example")[0] == 403
    assert call(scripted, "POST", "/api/case", {"case_id": "c-cosine"}, token=False)[0] == 403
    assert call(scripted, "POST", "/api/case", {"case_id": "c-cosine"}, origin="https://evil.example")[0] == 403
    assert scripted.RequestHandlerClass.app.x.opened == set()  # nothing opened


def test_only_listed_routes_answer_and_no_route_takes_a_human_step(scripted):
    assert call(scripted, "GET", "/api/store")[0] == 404
    assert call(scripted, "POST", "/api/approve_role", {})[0] == 404
    assert V.ROUTES.isdisjoint({"store", "seal", "start", "freeze", "mint", "run_case", "retire", "approve_role",
                                "approve_adapter", "sign_off", "register_human_check"})
    st, data = call(scripted, "GET", "/api/approve?item_id=role-1")
    route = M.ApprovalRoute.model_validate_json(data)
    assert st == 200 and route.performed is False and route.human_only and route.command.startswith("chh approve-role")
    assert "approve" not in V.ACTS  # a read: it writes nothing


def test_the_server_imports_nothing_that_reaches_the_store_or_the_kernel():
    tree = ast.parse((SRC / "experience" / "server.py").read_text(encoding="utf-8"))
    names = {f"{n.level}:{n.module}" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert {x for x in names if x.startswith("1:")} == {"1:None", "1:api"}


def test_a_request_with_the_wrong_shape_is_a_plain_refusal(scripted):
    st, data = call(scripted, "POST", "/api/rehearse", {"rows": -1})
    assert st == 400 and json.loads(data)["code"] == "BadRequest"
    st, data = call(scripted, "POST", "/api/case", {"case_id": "nope"})
    assert st == 409 and json.loads(data)["code"] == "NoCase"


def test_a_crash_gives_a_plain_message_and_no_traceback(scripted, monkeypatch):
    monkeypatch.setattr(ScriptedExperience, "waiting", lambda self: 1 / 0)
    st, data = call(scripted, "GET", "/api/waiting")
    assert st == 500 and b"Traceback" not in data and b"ZeroDivision" not in data


def test_static_files_stay_in_the_build_folder(scripted):
    assert call(scripted, "GET", "/a.js") == (200, b"1")
    st, data = call(scripted, "GET", "/../secret.txt")
    assert b"secret" not in data and st == 200  # the page, not the file
    st, data = call(scripted, "GET", "/%2e%2e/secret.txt")
    assert b"secret" not in data


def test_the_page_carries_the_token(scripted):
    assert scripted.RequestHandlerClass.app.token.encode() in call(scripted, "GET", "/")[1]


def test_the_scripted_stand_in_says_so_and_has_every_route():
    x = ScriptedExperience()
    assert x.meta().scripted and x.meta().notice == NOTICE
    assert all(callable(getattr(x, r, None)) for r in V.ROUTES)


def test_every_scripted_view_passes_the_rules_of_a_main_view():
    x = ScriptedExperience()
    views = [x.meta(), x.waiting(), x.cases(), x.sessions(), x.frozen(), x.trust(), x.record()]
    views += [x.case(c.case_id) for c in x.cases().cases]
    views += [x.lab("s-label-smoothing"), x.lab_log("s-label-smoothing")]
    for v in views:
        assert jargon_problems(v.model_dump(mode="json")) == [], type(v)


def test_the_route_table_matches_the_real_doorway():
    for r in V.ROUTES:
        assert callable(getattr(Experience, r)), r


# ---------------------------------------------------------------- a real store, real Court, through HTTP
@pytest.fixture
def world(tmp_path):
    w = World(tmp_path / "w")
    yield w
    w.close()


def test_a_real_case_through_the_server_is_the_one_display_path(world, tmp_path):
    world.seal()
    world.session()
    pid = world.freeze()
    case_id = world.x.promote(M.PromoteIn(promotion_id=pid, route="sealed", project="cohort-1")).case_id
    world.approve_role(C.case(world.store, case_id)["target"])
    world.x.run_case(case_id)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("x", encoding="utf-8")
    srv = V.make_server(lambda: world.x, 0, dist=tmp_path / "dist")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        target = C.case(world.store, case_id)["target"]
        before = len(released(world.store, target))
        assert json.loads(call(srv, "GET", "/api/meta")[1])["scripted"] is False
        lines = json.loads(call(srv, "GET", "/api/cases")[1])["cases"]
        assert [c["case_id"] for c in lines] == [case_id]
        for name in ("waiting", "sessions", "frozen", "record"):
            assert call(srv, "GET", f"/api/{name}")[0] == 200, name
        log = M.LabLog.model_validate_json(call(srv, "GET", "/api/lab_log?session_id=s-exp")[1])
        assert any("Radius" in x.text for x in log.lines)
        assert len(released(world.store, target)) == before  # listing spends nothing
        st, data = call(srv, "POST", "/api/case", {"case_id": case_id})
        view = M.Case.model_validate_json(data)
        assert st == 200 and view.verdict is not None
        assert len(released(world.store, target)) > before  # opening is a recorded release
        world.clean(world.x.sessions())
        world.clean(world.x.frozen())
        world.clean(log)
    finally:
        srv.shutdown()
        srv.server_close()
