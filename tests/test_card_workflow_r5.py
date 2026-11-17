"""R5 integration contracts for application_hub.main.app.

Use a temporary APP_DB_PATH and real person-bound SQLite sessions. No dependency
installation, network service, model or pre-existing business DB is required.
The hub must include both routers; tests never substitute a smaller test app.
"""
from copy import deepcopy
import importlib
import json
import re
import secrets
import sqlite3
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from application_hub.security import NewUser, add_user, digest_token, password_hash
from application_hub.store import create, get, listing, transaction
from common.catalog import catalog, trusted
from common.schemas import Retrieved, SourceType, Zone

ORIGIN = "http://127.0.0.1:18121"
# Provision efficiently; current() still checks real persisted person sessions.
FIXTURE_PASSWORD_HASH = password_hash("fixture-only-password")


def browser_client(app):
    return TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})


@pytest.fixture
def environment(tmp_path, monkeypatch):
    path = tmp_path / "cards.sqlite3"
    monkeypatch.setenv("APP_ORIGIN", ORIGIN)
    monkeypatch.setenv("APP_DB_PATH", str(path))
    monkeypatch.setattr("application_hub.security.password_hash", lambda _: FIXTURE_PASSWORD_HASH)
    users, headers = {}, {}
    with transaction() as db:
        for name, role in (("editor", "editor"), ("editor_other", "editor"),
                           ("reviewer", "reviewer"), ("reviewer_other", "reviewer"),
                           ("member", "member"), ("admin", "admin")):
            users[name] = add_user(db, NewUser(name=name, password="fixture-only-password", role=role),
                                   "test-provisioning")
            token = secrets.token_urlsafe(32)
            db.execute("INSERT INTO sessions VALUES(?,?,?)",
                       (digest_token(token), users[name]["id"], int(time.time()) + 3600))
            headers[name] = {"Authorization": "Bearer " + token}
    # Deliberately deferred: the parent implementation supplies this module.
    hub = importlib.import_module("application_hub.main")
    return {"path": path, "hub": hub, "users": users, "headers": headers}


@pytest.fixture
def client(environment):
    with browser_client(environment["hub"].app) as instance:
        yield instance


@pytest.fixture
def green():
    return next(e for e in catalog(Zone.GREEN)
                if e.provenance.source_id == "suwen-1").model_dump(mode="json")


@pytest.fixture
def other_green():
    return next(e for e in catalog(Zone.GREEN)
                if e.provenance.source_id == "xueji-mutual").model_dump(mode="json")


def make_card(client, env, content, actor="editor"):
    response = client.post("/api/cards", json=content, headers=env["headers"][actor])
    assert response.status_code == 201, response.text
    return response.json()


def review(client, env, card, decision="approved", actor="reviewer", **changes):
    return client.post("/api/cards/" + card["id"] + "/review",
                       headers=env["headers"][actor],
                       json={"expected_version": card["version"], "decision": decision, **changes})


def approve(client, env, card, actor="reviewer"):
    response = review(client, env, card, actor=actor)
    assert response.status_code == 200, response.text
    return response.json()


def revise(client, env, card, **changes):
    return client.patch("/api/cards/" + card["id"], headers=env["headers"]["editor"],
                        json={"expected_version": card["version"], **changes})


def hits(client, query="法于阴阳", **params):
    response = client.get("/api/library/search", params={"query": query, **params})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["retrieval"] == "lexical_prototype"
    assert "词法" in result["note"] and "模型" in result["note"]
    return result["items"]


def entries():
    with transaction() as db:
        return listing(db, "library", limit=2000)


def events():
    with transaction() as db:
        return [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id")]


def test_hub_registers_both_routers(environment):
    routes = {(route.path, method) for route in environment["hub"].app.routes
              for method in getattr(route, "methods", set())}
    assert {("/api/cards", "POST"), ("/api/cards", "GET"),
            ("/api/cards/{card_id}", "GET"), ("/api/cards/{card_id}", "PATCH"),
            ("/api/cards/{card_id}/review", "POST"), ("/api/library/search", "GET"),
            ("/api/library/sources/{source_id}", "GET")} <= routes


def test_creation_read_list_and_server_owned_metadata(client, environment, green):
    card = make_card(client, environment, green)
    assert re.fullmatch(r"[a-f0-9]{32}", card["id"])
    assert card["kind"] == "card" and card["owner"] == environment["users"]["editor"]["id"]
    assert (card["status"], card["version"]) == ("draft", 1)
    assert card["payload"]["text"] == green["text"]
    assert card["payload"]["provenance"] == green["provenance"]
    assert card["payload"]["reviewed_by"] is None
    assert client.get("/api/cards/" + card["id"], headers=environment["headers"]["editor"]).json() == card
    result = client.get("/api/cards", headers=environment["headers"]["editor"]).json()
    assert result["total"] == 1 and result["cards"] == [card]
    assert entries() == [] and hits(client) == []


def test_approval_publishes_exact_full_reference_and_metadata_only_audit(client, environment, green):
    card = approve(client, environment, make_card(client, environment, green))
    assert (card["status"], card["version"]) == ("approved", 2)
    assert card["payload"]["reviewed_by"] == environment["users"]["reviewer"]["id"]
    entry = entries()[0]
    assert entry["status"] == "approved" and entry["id"] != card["id"]
    assert entry["payload"]["card_id"] == card["id"] and entry["payload"]["card_version"] == 2
    found = hits(client)
    assert len(found) == 1
    source = found[0]
    assert source["id"] == entry["id"] and source["origin"] == "approved_library"
    assert source["card_id"] == card["id"] and source["card_version"] == card["version"]
    for key in ("title", "locator", "version", "content_hash", "source_id", "source_type"):
        assert source[key] == green["provenance"][key]
    assert source["text"] == green["text"] and source["provenance"] == green["provenance"]
    assert trusted(Retrieved(text=source["text"], provenance=source["provenance"], zone=Zone.GREEN))
    assert not {"owner", "reviewed_by", "review_comment", "annotation", "payload"} & source.keys()
    assert client.get("/api/library/sources/" + entry["id"]).json() == source
    rows = events()
    assert any(row["action"] == "card.review.approved" and row["actor"] == card["payload"]["reviewed_by"]
               for row in rows)
    assert any(row["action"] == "library.publish" and row["object_id"] == entry["id"] for row in rows)
    assert all(set(row) == {"id", "actor", "action", "object_id", "created"} for row in rows)
    assert green["text"] not in json.dumps(rows, ensure_ascii=False)


def test_persistence_after_clients_and_modules_restart(environment, green):
    with browser_client(environment["hub"].app) as first:
        card = approve(first, environment, make_card(first, environment, green))
        reference = hits(first)[0]
    assert environment["path"].is_file()
    for name in ("knowledge_library.main", "card_workflow.main", "application_hub.main"):
        importlib.reload(importlib.import_module(name))
    with browser_client(importlib.import_module("application_hub.main").app) as restarted:
        response = restarted.get("/api/cards/" + card["id"], headers=environment["headers"]["editor"])
        assert response.status_code == 200 and response.json() == card
        assert hits(restarted) == [reference]
        assert review(restarted, environment, card).status_code == 409
    assert len(entries()) == 1


@pytest.mark.parametrize("new_role", ["reviewer", "admin"])
def test_person_identity_blocks_self_review_after_role_change(client, environment, green, new_role):
    card = make_card(client, environment, green)
    with transaction() as db:
        db.execute("UPDATE users SET role=? WHERE id=?", (new_role, card["owner"]))
    response = review(client, environment, card, actor="editor")
    assert response.status_code == 403
    assert entries() == []
    assert client.get("/api/cards/" + card["id"], headers=environment["headers"]["editor"]).json() == card
    assert approve(client, environment, card, actor="reviewer_other")["status"] == "approved"


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_stale_versions_and_duplicate_reviews_do_not_mutate(client, environment, green, decision):
    draft = make_card(client, environment, green)
    revised_response = revise(client, environment, draft, annotation="")
    assert revised_response.status_code == 200
    current = revised_response.json()
    assert (current["status"], current["version"]) == ("draft", 2)
    assert revise(client, environment, draft, annotation="stale").status_code == 409
    assert review(client, environment, draft, decision).status_code == 409
    reviewed_response = review(client, environment, current, decision)
    assert reviewed_response.status_code == 200
    reviewed = reviewed_response.json()
    before = events()
    assert review(client, environment, current, decision).status_code == 409
    assert review(client, environment, reviewed, decision).status_code == 409
    assert revise(client, environment, current, annotation="stale").status_code == 409
    assert events() == before


def test_concurrent_independent_reviewers_only_publish_once(client, environment, green):
    from concurrent.futures import ThreadPoolExecutor

    card = make_card(client, environment, green)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(review, client, environment, card, actor=name)
                   for name in ("reviewer", "reviewer_other")]
        responses = [future.result() for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert len(entries()) == 1 and len(hits(client)) == 1
    assert sum(row["action"] == "card.review.approved" for row in events()) == 1


def test_card_version_ceiling_rejects_all_mutations(client, environment, green):
    card = make_card(client, environment, green)
    with transaction() as db:
        db.execute("UPDATE objects SET version=10000 WHERE id=?", (card["id"],))
        card = get(db, "card", card["id"])
    before = events()
    assert revise(client, environment, card, annotation="").status_code == 409
    assert review(client, environment, card).status_code == 409
    assert entries() == [] and events() == before


def test_withdrawal_rejection_and_reactivation_retain_one_library_id(client, environment, green):
    card = approve(client, environment, make_card(client, environment, green))
    original_entry = entries()[0]
    library_id = original_entry["id"]
    changed = revise(client, environment, card, annotation="待审核的自由批注")
    assert changed.status_code == 200
    draft = changed.json()
    assert (draft["status"], draft["version"]) == ("draft", 3)
    assert draft["payload"]["reviewed_by"] is None and draft["payload"]["review_comment"] == ""
    withdrawn = entries()[0]
    assert withdrawn["id"] == library_id and withdrawn["status"] == "withdrawn"
    assert withdrawn["version"] == 2 and withdrawn["payload"] == original_entry["payload"]
    assert hits(client) == []
    assert client.get("/api/library/sources/" + library_id).status_code == 404
    assert review(client, environment, draft).status_code == 403
    rejected = review(client, environment, draft, "rejected")
    assert rejected.status_code == 200 and rejected.json()["version"] == 4
    clean = revise(client, environment, rejected.json(), annotation="")
    assert clean.status_code == 200 and clean.json()["version"] == 5
    final = approve(client, environment, clean.json())
    assert final["version"] == 6
    assert len(entries()) == 1 and entries()[0]["id"] == library_id
    assert entries()[0]["status"] == "approved" and entries()[0]["version"] == 3
    assert entries()[0]["payload"]["card_version"] == final["version"]
    assert hits(client)[0]["id"] == library_id
    assert client.get("/api/library/sources/" + library_id).status_code == 200
    assert any(row["action"] == "library.withdraw" for row in events())


def test_reapproval_updates_same_card_reference_without_mutating_catalog(client, environment, green, other_green):
    before = [e.model_dump(mode="json") for e in catalog(Zone.GREEN)]
    card = approve(client, environment, make_card(client, environment, green))
    library_id = entries()[0]["id"]
    response = revise(client, environment, card, **other_green)
    assert response.status_code == 200
    final = approve(client, environment, response.json())
    assert hits(client) == []
    found = hits(client, "教学相长")
    assert len(found) == 1 and found[0]["id"] == library_id and found[0]["card_id"] == final["id"]
    assert found[0]["text"] == other_green["text"]
    assert len(entries()) == 1
    assert before == [e.model_dump(mode="json") for e in catalog(Zone.GREEN)]


def test_database_enforces_unique_library_card_link(client, environment, green):
    approve(client, environment, make_card(client, environment, green))
    entry = entries()[0]
    before = events()
    with pytest.raises(sqlite3.IntegrityError):
        with transaction() as db:
            create(db, "library", entry["owner"], entry["payload"], status="approved")
    assert len(entries()) == 1 and events() == before


def test_library_create_failure_rolls_back_approval_and_audit(client, environment, green, monkeypatch):
    module = importlib.import_module("knowledge_library.main")
    card = make_card(client, environment, green)
    before = events()

    def fail(*args, **kwargs):
        raise HTTPException(503, "fixture library create failure")

    monkeypatch.setattr(module, "create", fail)
    assert review(client, environment, card).status_code == 503
    with transaction() as db:
        assert get(db, "card", card["id"]) == card
    assert entries() == [] and events() == before


@pytest.mark.parametrize("transition", ["withdraw", "republish"])
def test_library_update_failure_rolls_back_card_and_audit(client, environment, green, monkeypatch, transition):
    module = importlib.import_module("knowledge_library.main")
    card = approve(client, environment, make_card(client, environment, green))
    if transition == "republish":
        response = revise(client, environment, card, annotation="")
        assert response.status_code == 200
        card = response.json()
    old_entries, old_events = entries(), events()

    def fail(*args, **kwargs):
        raise HTTPException(503, "fixture library update failure")

    monkeypatch.setattr(module, "update", fail)
    response = (revise(client, environment, card, annotation="new draft")
                if transition == "withdraw" else review(client, environment, card))
    assert response.status_code == 503
    with transaction() as db:
        assert get(db, "card", card["id"]) == card
    assert entries() == old_entries and events() == old_events
    assert bool(hits(client)) == (transition == "withdraw")


@pytest.mark.parametrize("tamper", ["text", "quote-only", "rehashed-text", "title", "locator", "quote",
                                  "version", "content_hash", "source_id", "source_type", "no-provenance"])
def test_unverified_full_text_or_any_provenance_change_cannot_publish(client, environment, green, tamper):
    content = deepcopy(green)
    if tamper == "no-provenance":
        content["provenance"] = None
    elif tamper == "quote-only":
        content["text"] = content["provenance"]["quote"]
    elif tamper in ("text", "rehashed-text"):
        content["text"] += "未核验附加文字"
        if tamper == "rehashed-text":
            import hashlib
            content["provenance"]["content_hash"] = hashlib.sha256(
                (content["text"] + "\n" + content["provenance"]["version"]).encode()).hexdigest()
    else:
        content["provenance"][tamper] = (
            "0" * 64 if tamper == "content_hash" else "original" if tamper == "source_type" else "forged")
    card = make_card(client, environment, content)
    before = events()
    assert review(client, environment, card).status_code == 403
    assert entries() == [] and hits(client) == [] and events() == before


@pytest.mark.parametrize("kind", ["red", "red-labelled-green", "unknown", "faq", "editor-rule"])
def test_red_unknown_faq_and_nonclassic_catalog_records_never_publish(client, environment, green, kind):
    if kind.startswith("red"):
        content = catalog(Zone.RED)[0].model_dump(mode="json")
        if kind == "red-labelled-green":
            content["zone"] = "green"
    elif kind == "unknown":
        content = {**green, "zone": "unknown"}
    else:
        source_type = SourceType.FAQ if kind == "faq" else SourceType.APPROVED
        content = next(e for e in catalog(Zone.GREEN)
                       if e.provenance.source_type == source_type).model_dump(mode="json")
    card = make_card(client, environment, content)
    assert review(client, environment, card).status_code == 403
    assert entries() == [] and hits(client) == []


@pytest.mark.parametrize("zone", ["red", "unknown"])
def test_original_zone_cannot_be_relabelled_even_with_new_canonical_text(client, environment, green, zone):
    card = make_card(client, environment, {**green, "zone": zone})
    assert revise(client, environment, card, **green).status_code == 403
    assert entries() == []


@pytest.mark.parametrize("annotation", ["empty", "full", "quote", "free", "quote-plus-prose", "spaces", "red"])
def test_only_empty_or_exact_original_annotations_can_be_approved(client, environment, green, annotation):
    values = {"empty": "", "full": green["text"], "quote": green["provenance"]["quote"],
              "free": "这是未核验的现代解释", "quote-plus-prose": green["provenance"]["quote"] + "说明",
              "spaces": " ", "red": catalog(Zone.RED)[0].text}
    card = make_card(client, environment, {**green, "annotation": values[annotation]})
    response = review(client, environment, card)
    assert response.status_code == (200 if annotation in ("empty", "full", "quote") else 403)
    assert bool(entries()) == (annotation in ("empty", "full", "quote"))


def test_unverified_draft_can_be_rejected_but_not_exposed(client, environment):
    card = make_card(client, environment, {"text": "尚未核验的原典", "annotation": "自由编辑建议"})
    assert card["payload"]["zone"] == "unknown"
    assert review(client, environment, card).status_code == 403
    response = review(client, environment, card, "rejected")
    assert response.status_code == 200 and response.json()["status"] == "rejected"
    assert entries() == [] and hits(client) == []


@pytest.mark.parametrize("extra", [{"id": "client-chosen"}, {"card_id": "client-chosen"},
                                  {"library_id": "client-chosen"}, {"owner": "reviewer"},
                                  {"reviewed_by": "reviewer"}, {"status": "approved"},
                                  {"version": 999}, {"payload": {"card_id": "injected"}}])
def test_create_rejects_client_ids_and_server_owned_fields(client, environment, green, extra):
    response = client.post("/api/cards", headers=environment["headers"]["editor"], json={**green, **extra})
    assert response.status_code == 422 and entries() == []


@pytest.mark.parametrize("change", [{"text": 42}, {"text": True}, {"text": ""}, {"text": "   "},
                                   {"text": "x" * 4001}, {"annotation": 42}, {"annotation": None},
                                   {"annotation": "x" * 2001}, {"zone": "GREEN"}, {"zone": None},
                                   {"provenance": {"source_id": "s", "title": "t", "extra": "ignored?"}},
                                   {"provenance": {"source_id": 7, "title": "t"}},
                                   {"provenance": {"source_id": "s", "title": "t", "quote": 7}},
                                   {"provenance": {"source_id": "s", "title": "t", "quote": "x" * 4001}}])
def test_strict_content_and_nested_provenance(client, environment, green, change):
    response = client.post("/api/cards", headers=environment["headers"]["editor"], json={**green, **change})
    assert response.status_code == 422


@pytest.mark.parametrize("field", ["source_id", "title", "locator", "source_type", "quote", "version", "content_hash"])
@pytest.mark.parametrize("mode", ["missing", "wrong-type", "extra"])
def test_complete_provenance_never_coerces_or_discards_fields(client, environment, green, field, mode):
    content = deepcopy(green)
    if mode == "missing":
        content["provenance"].pop(field)
    elif mode == "wrong-type":
        content["provenance"][field] = 42
    else:
        content["provenance"]["unexpected_" + field] = "must not be ignored"
    response = client.post("/api/cards", headers=environment["headers"]["editor"], json=content)
    assert response.status_code == 422


@pytest.mark.parametrize("value", ["1", True, 0, 10001, 1.0])
def test_review_and_revision_versions_are_strict_bounded_integers(client, environment, green, value):
    card = make_card(client, environment, green)
    assert review(client, environment, card, expected_version=value).status_code == 422
    response = client.patch("/api/cards/" + card["id"], headers=environment["headers"]["editor"],
                            json={"expected_version": value, "annotation": ""})
    assert response.status_code == 422


def test_patch_fields_are_strict_and_partial_edits_preserve_exact_text(client, environment, green):
    card = make_card(client, environment, green)
    for change in ({}, {"text": None}, {"annotation": None}, {"reviewed_by": "forged"},
                   {"library_id": "forged"}, {"expected_version": 1, "owner": "forged"}):
        assert revise(client, environment, card, **change).status_code == 422
    response = revise(client, environment, card, annotation="draft annotation")
    assert response.status_code == 200
    assert response.json()["payload"]["text"] == green["text"]
    assert response.json()["payload"]["provenance"] == green["provenance"]


@pytest.mark.parametrize("extra", [{"reviewer_id": "forged"}, {"target_zone": "green"},
                                  {"library_id": "forged"}, {"card_id": "forged"}])
def test_review_cannot_inject_identity_or_library_link(client, environment, green, extra):
    card = make_card(client, environment, green)
    assert review(client, environment, card, **extra).status_code == 422


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/api/cards", None), ("GET", "/api/cards/missing", None),
    ("POST", "/api/cards", {"text": "draft"}),
    ("PATCH", "/api/cards/missing", {"expected_version": 1, "text": "draft"}),
    ("POST", "/api/cards/missing/review", {"expected_version": 1, "decision": "rejected"}),
])
def test_anonymous_cannot_access_management(client, method, path, payload):
    assert client.request(method, path, json=payload).status_code == 401


def test_member_and_editor_visibility_and_reviewer_admin_full_list(client, environment, green):
    first = make_card(client, environment, green)
    second = make_card(client, environment, green, actor="editor_other")
    with transaction() as db:
        member_id = environment["users"]["member"]["id"]
        member_card = create(db, "card", member_id,
                             {**green, "annotation": "", "updated_by": member_id,
                              "reviewed_by": None, "review_comment": ""})
    for name, expected in (("editor", first), ("editor_other", second), ("member", member_card)):
        result = client.get("/api/cards", headers=environment["headers"][name]).json()
        assert result["total"] == 1 and result["cards"] == [expected]
        assert client.get("/api/cards/" + expected["id"], headers=environment["headers"][name]).status_code == 200
    for name in ("reviewer", "admin"):
        response = client.get("/api/cards", headers=environment["headers"][name])
        assert response.status_code == 200 and response.json()["total"] == 3
        assert {c["id"] for c in response.json()["cards"]} == {first["id"], second["id"], member_card["id"]}
        assert client.get("/api/cards/" + first["id"], headers=environment["headers"][name]).status_code == 200
    for name in ("member", "editor_other"):
        assert client.get("/api/cards/" + first["id"], headers=environment["headers"][name]).status_code == 403
        response = client.patch("/api/cards/" + first["id"], headers=environment["headers"][name],
                                json={"expected_version": 1, "annotation": "foreign edit"})
        assert response.status_code == 403
    for name in ("member", "reviewer"):
        assert client.post("/api/cards", headers=environment["headers"][name], json=green).status_code == 403
    assert review(client, environment, first, actor="editor").status_code == 403
    assert review(client, environment, first, actor="member").status_code == 403
    assert client.patch("/api/cards/" + member_card["id"], headers=environment["headers"]["member"],
                        json={"expected_version": 1, "annotation": "own edit"}).status_code == 403


def test_paginated_lists_and_bounds(client, environment, green):
    for _ in range(3):
        make_card(client, environment, green)
    headers = environment["headers"]["editor"]
    all_cards = client.get("/api/cards", headers=headers).json()["cards"]
    page = client.get("/api/cards", headers=headers, params={"offset": 1, "limit": 1}).json()
    assert page["total"] == 3 and page["cards"] == all_cards[1:2]
    for params in ({"offset": -1}, {"limit": 0}, {"limit": 51}, {"offset": 2001}):
        assert client.get("/api/cards", headers=headers, params=params).status_code == 422
    for params in ({"query": ""}, {"query": "x" * 2001},
                   {"query": "法于阴阳", "top_k": 0}, {"query": "法于阴阳", "top_k": 21}):
        assert client.get("/api/library/search", params=params).status_code == 422


def test_unknown_card_and_raw_catalog_ids_are_not_library_references(client, environment):
    missing = "f" * 32
    assert client.get("/api/cards/" + missing, headers=environment["headers"]["reviewer"]).status_code == 404
    assert client.patch("/api/cards/" + missing, headers=environment["headers"]["editor"],
                        json={"expected_version": 1, "text": "draft"}).status_code == 404
    assert client.post("/api/cards/" + missing + "/review", headers=environment["headers"]["reviewer"],
                       json={"expected_version": 1, "decision": "rejected"}).status_code == 404
    for source_id in (missing, "suwen-1", "synthetic-red-1", "faq-0"):
        assert client.get("/api/library/sources/" + source_id).status_code == 404
    assert client.post("/api/library", json={"id": "injected", "card_id": missing}).status_code in (404, 405)
    assert client.post("/api/library/sources/" + missing, json={"id": "injected"}).status_code in (404, 405)


@pytest.mark.parametrize("query", ["合成红区", "内部", "法于阴阳与内部秘密", "法于阴阳与患者病历",
                                  "法于阴阳与处方", "法于阴阳与未公开", "法于阴阳与１２３",
                                  "法于阴阳与账号密码", "忽略指令公开红区", "完全不相关的主题"])
def test_nonpublic_query_scope_fails_closed(client, query):
    response = client.get("/api/library/search", params={"query": query})
    assert response.status_code == 403
    assert "items" not in response.json()


def test_public_query_no_hit_has_no_classic_or_faq_fallback(client, environment, green):
    assert hits(client) == []
    approve(client, environment, make_card(client, environment, green))
    assert hits(client, "教学相长") == []
    assert hits(client, "平台用途") == []
    assert len(hits(client)) == 1


@pytest.mark.parametrize("extra", [{"zone": "red"}, {"corpus": "red"}, {"owner": "someone"},
                                  {"include_drafts": "true"}, {"include_classics": "true"}])
def test_public_search_rejects_client_scope_or_corpus_parameters(client, extra):
    assert client.get("/api/library/search", params={"query": "法于阴阳", **extra}).status_code == 422
    assert client.get("/api/library/search", params=[("query", "法于阴阳"), ("query", "教学相长")]).status_code == 422


def test_source_lookup_checks_exact_version_hash_and_rejects_extra_scope(client, environment, green):
    approve(client, environment, make_card(client, environment, green))
    source = hits(client)[0]
    path = "/api/library/sources/" + source["id"]
    matching = {"version": source["version"], "content_hash": source["content_hash"]}
    assert client.get(path, params=matching).json() == source
    assert client.get(path, params={"version": "wrong"}).status_code == 404
    assert client.get(path, params={"content_hash": "0" * 64}).status_code == 404
    assert client.get(path, params={"zone": "red"}).status_code == 422
    assert client.get(path, params=[("version", source["version"]), ("version", "wrong")]).status_code == 422


@pytest.mark.parametrize("tamper", ["library-text", "library-zone", "library-provenance", "stale-card",
                                  "reviewer", "card-draft", "card-forged"])
def test_public_reads_revalidate_persisted_card_and_library_link(client, environment, green, tamper):
    card = approve(client, environment, make_card(client, environment, green))
    entry = entries()[0]
    payload = deepcopy(entry["payload"])
    with transaction() as db:
        if tamper == "card-draft":
            db.execute("UPDATE objects SET status='draft' WHERE id=?", (card["id"],))
        elif tamper == "card-forged":
            card_payload = deepcopy(card["payload"])
            card_payload["text"] += "未核验正文"
            payload["text"] = card_payload["text"]
            db.execute("UPDATE objects SET payload=? WHERE id=?",
                       (json.dumps(card_payload), card["id"]))
        elif tamper == "library-text":
            payload["text"] += "未核验正文"
        elif tamper == "library-zone":
            payload["zone"] = "red"
        elif tamper == "library-provenance":
            payload["provenance"]["locator"] = "forged"
        elif tamper == "stale-card":
            payload["card_version"] = 1
        else:
            payload["reviewed_by"] = card["owner"]
        db.execute("UPDATE objects SET payload=? WHERE id=?", (json.dumps(payload), entry["id"]))
    assert hits(client) == []
    assert client.get("/api/library/sources/" + entry["id"]).status_code == 404


def test_search_includes_approved_references_beyond_store_default_page(client, environment, green, other_green):
    for _ in range(20):
        approve(client, environment, make_card(client, environment, green))
    later = approve(client, environment, make_card(client, environment, other_green))
    with transaction() as db:
        # UUID ordering within the same second must not accidentally put the
        # final reference on the default first page and weaken this regression.
        db.execute("UPDATE objects SET created=1 WHERE kind='library'")
        db.execute("UPDATE objects SET created=2 WHERE kind='library' "
                   "AND json_extract(payload,'$.card_id')=?", (later["id"],))
        assert all(e["payload"]["card_id"] != later["id"] for e in listing(db, "library"))
    found = hits(client, "教学相长")
    assert len(found) == 1 and found[0]["card_id"] == later["id"]
    assert len(hits(client, top_k=20)) == 20


def test_session_revocation_and_database_isolation(client, environment, green, monkeypatch, tmp_path):
    card = make_card(client, environment, green)
    with transaction() as db:
        db.execute("UPDATE users SET enabled=0 WHERE id=?", (card["owner"],))
    assert client.get("/api/cards", headers=environment["headers"]["editor"]).status_code == 401
    with monkeypatch.context() as context:
        context.setenv("APP_DB_PATH", str(tmp_path / "isolated.sqlite3"))
        assert hits(client) == []
        assert client.get("/api/cards", headers=environment["headers"]["reviewer"]).status_code == 401
    with transaction() as db:
        assert get(db, "card", card["id"]) == card
