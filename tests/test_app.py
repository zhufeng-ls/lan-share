import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import main
from app.admin_security import password_hash


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path)
    monkeypatch.setattr(main, "mutation_lock", asyncio.Lock())
    monkeypatch.setattr(main, "storage_reservations", {})
    with TestClient(main.app, headers={"X-Lan-Share": "1"}) as c:
        yield c


def join(client, name="小王"):
    response = client.post("/api/join", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["user"]


def path(did, action):
    return f"/api/directories/{did}/{action}"


def upload(client, did, name="测试.txt", content=b"first", version=""):
    return client.put(path(did, "upload"), params={"path": name, "expected_version": version}, content=content)


def entries(client, did, folder=""):
    result = client.get(path(did, "files"), params={"path": folder})
    assert result.status_code == 200, result.text
    return result.json()["items"]


def test_join_remembers_identity_and_rejects_duplicate_creation(client):
    first = join(client)
    cookie = client.cookies.get("lan_session")
    assert cookie and len(cookie) >= 40
    assert client.get("/api/me").json()["user"]["id"] == first["id"]
    assert join(client, "另一个名字")["id"] == first["id"]
    client.cookies.clear()
    response = client.post("/api/join", json={"name": "小王", "create_new": True})
    assert response.status_code == 200
    assert response.json()["candidates"][0]["id"] == first["id"]
    assert client.get("/api/me").status_code == 401
    with main.database() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM directories").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM sessions WHERE token_hash=?", (cookie,)).fetchone()[0] == 0


def test_anonymous_cannot_read_or_change_files(client):
    for url in ("/api/me", "/api/directories", "/api/trash", "/api/activity", path("public", "files")):
        assert client.get(url).status_code == 401
    assert upload(client, "public").status_code == 401


def test_same_name_prompts_without_creating_identity_or_session(client):
    first = join(client)
    client.cookies.clear()
    response = client.post("/api/join", json={"name": " 小王 "})
    assert response.status_code == 200
    assert response.json()["status"] == "choose_identity"
    assert response.json()["candidates"][0]["id"] == first["id"]
    assert "set-cookie" not in response.headers
    assert client.get("/api/me").status_code == 401
    with main.database() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM directories").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_reuse_identity_restores_files_preferences_and_retains_other_session(client):
    user = join(client)
    original_cookie = client.cookies.get("lan_session")
    upload(client, user["default_id"])
    client.put("/api/preferences", json={"pins": [user["default_id"]], "default_id": "public"})
    client.cookies.clear()
    response = client.post("/api/join", json={"name": "小王", "existing_user_id": user["id"]})
    assert response.status_code == 200
    data = response.json()
    assert data["user"]["id"] == user["id"]
    assert data["user"]["default_id"] == "public"
    assert set(data["pins"]) == {user["default_id"], "public"}
    assert entries(client, user["default_id"])[0]["name"] == "测试.txt"
    assert client.cookies.get("lan_session") != original_cookie
    current_cookie = client.cookies.get("lan_session")
    assert client.post("/api/logout").status_code == 200
    assert not client.cookies.get("lan_session")
    client.cookies.set("lan_session", current_cookie)
    assert client.get("/api/me").status_code == 401
    client.cookies.clear()
    client.cookies.set("lan_session", original_cookie)
    assert client.get("/api/me").json()["user"]["id"] == user["id"]


def test_name_normalization_and_rename_conflicts(client):
    first = join(client, "Henry")
    client.cookies.clear()
    response = client.post("/api/join", json={"name": " ＨＥＮＲＹ ", "create_new": True})
    assert response.json()["candidates"][0]["id"] == first["id"]
    second = join(client, "小李")
    assert client.patch("/api/profile", json={"name": " HENRY "}).status_code == 409
    assert client.get("/api/me").json()["user"]["name"] == "小李"
    assert client.patch("/api/profile", json={"name": "小李"}).status_code == 200
    client.cookies.clear()
    result = client.post("/api/join", json={"name": "henry", "existing_user_id": first["id"]})
    assert result.json()["user"]["id"] == first["id"]
    assert first["id"] != second["id"]


def test_identity_choice_revalidates_name_and_rejects_invalid_selection(client):
    user = join(client)
    client.patch("/api/profile", json={"name": "新名字"})
    client.cookies.clear()
    for choice in ({"name": "小王", "existing_user_id": user["id"]}, {"name": "新名字", "existing_user_id": "missing"}):
        assert client.post("/api/join", json=choice).status_code == 409
    assert client.post("/api/join", json={"name": "新名字", "existing_user_id": user["id"], "create_new": True}).status_code == 400
    assert client.get("/api/me").status_code == 401


def test_simultaneous_first_join_offers_choice_instead_of_duplicate_directory(client):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post("/api/join", headers={"Cookie": ""}, json={"name": "同时进入"}).json(), range(2)))
    assert sum("user" in result for result in results) == 1
    assert sum(result.get("status") == "choose_identity" for result in results) == 1
    with main.database() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_database_unique_name_constraint_and_restart(client):
    first = join(client, "Henry")
    client.cookies.clear()
    second = join(client, "小李")
    main.initialize()
    with pytest.raises(sqlite3.IntegrityError), main.database() as db:
        db.execute("UPDATE users SET name_key=? WHERE id=?", ("henry", second["id"]))
    assert client.get("/api/me").json()["user"]["name"] == "小李"


def test_disabled_name_cannot_be_recreated_or_restored_over_active_user(client):
    user = join(client, "Henry")
    admin_login(client)
    assert client.post(f"/api/admin/users/{user['id']}/disable").status_code == 200
    assert client.post("/api/join", json={"name": "HENRY", "create_new": True}).status_code == 409
    replacement = join(client, "小李")
    assert client.patch("/api/profile", json={"name": "henry"}).status_code == 409
    with main.database() as db:
        db.execute("UPDATE users SET name=?,name_key=? WHERE id=?", ("Henry", "henry", replacement["id"]))
    assert client.post(f"/api/admin/users/{user['id']}/restore").status_code == 409
    with main.database() as db:
        assert db.execute("SELECT disabled_at FROM users WHERE id=?", (user["id"],)).fetchone()[0]


def test_cross_user_read_write_and_associate_multiple_directories(client):
    first = join(client)
    did = first["default_id"]
    assert upload(client, did).status_code == 200
    client.cookies.clear()
    second = join(client, "小李")
    assert client.get(path(did, "download"), params={"path": "测试.txt"}).content == b"first"
    ver = entries(client, did)[0]["version"]
    assert upload(client, did, content=b"second user", version=ver).status_code == 200
    assert client.put("/api/preferences", json={"pins": [did, second["default_id"], "public"], "default_id": did}).status_code == 200
    me = client.get("/api/me").json()
    assert me["user"]["default_id"] == did
    assert len(me["pins"]) == 3
    assert client.get("/api/activity").json()[0]["actor_id"] == second["id"]


def test_unpin_does_not_delete_and_default_is_always_pinned(client):
    user = join(client)
    did = user["default_id"]
    upload(client, did)
    assert client.put("/api/preferences", json={"pins": [], "default_id": "public"}).status_code == 200
    assert client.get("/api/me").json()["pins"] == ["public"]
    assert len(entries(client, did)) == 1
    assert client.put("/api/preferences", json={"pins": ["not-found"], "default_id": "public"}).status_code == 400


def test_overwrite_requires_current_version_and_preserves_old_content(client):
    did = join(client)["default_id"]
    upload(client, did)
    ver = entries(client, did)[0]["version"]
    assert upload(client, did, content=b"second").status_code == 409
    assert upload(client, did, content=b"second", version=ver).status_code == 200
    assert upload(client, did, content=b"stale", version=ver).status_code == 409
    old = client.get("/api/trash").json()[0]
    assert old["reason"] == "overwrite"
    assert client.post(f"/api/trash/{old['id']}/restore", json={"path": ""}).status_code == 409
    assert client.post(f"/api/trash/{old['id']}/restore", json={"path": "旧版本.txt"}).status_code == 200
    assert client.get(path(did, "download"), params={"path": "旧版本.txt"}).content == b"first"
    assert client.get(path(did, "download"), params={"path": "测试.txt"}).content == b"second"


def test_deleted_file_and_folder_restore(client):
    did = join(client)["default_id"]
    assert client.post(path(did, "folders"), json={"path": "资料"}).status_code == 200
    assert upload(client, did, "资料/hello.txt").status_code == 200
    folder = entries(client, did)[0]
    assert client.post(path(did, "delete"), json={"path": folder["path"], "version": folder["version"]}).status_code == 200
    assert entries(client, did) == []
    old = client.get("/api/trash").json()[0]
    assert client.post(f"/api/trash/{old['id']}/restore", json={"path": "资料"}).status_code == 200
    assert client.get(path(did, "download"), params={"path": "资料/hello.txt"}).content == b"first"
    assert client.get("/api/trash").json() == []


@pytest.mark.parametrize("bad_path", ["../index.sqlite3", "/etc/passwd", "x/../../a", "a//b", "a/./b", "a\\b", "a\x00b", ".", ".."])
def test_path_traversal_rejected(client, bad_path):
    did = join(client)["default_id"]
    assert upload(client, did, bad_path).status_code == 400
    assert client.get(path(did, "download"), params={"path": bad_path}).status_code == 400
    assert client.post(path(did, "folders"), json={"path": bad_path}).status_code == 400


def test_symlinks_cannot_escape_storage(client, tmp_path):
    did = join(client)["default_id"]
    outside = tmp_path / "private.txt"
    outside.write_text("secret")
    root = main.DATA / "files" / did
    (root / "leak").symlink_to(outside)
    (root / "parent").symlink_to(tmp_path, target_is_directory=True)
    assert client.get(path(did, "download"), params={"path": "leak"}).status_code == 400
    assert upload(client, did, "parent/private.txt").status_code == 400
    assert entries(client, did) == []
    assert outside.read_text() == "secret"


def test_csrf_and_cookie_flags(client):
    response = client.post("/api/join", json={"name": "王"})
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert client.post("/api/directories", json={"name": "bad"}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/directories", json={"name": "bad"}, headers={"X-Lan-Share": ""}).status_code == 403
    assert client.post("/api/directories", json={"name": "bad"}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_oversized_upload_leaves_no_partial_file(client, monkeypatch):
    did = join(client)["default_id"]
    monkeypatch.setattr(main, "MAX_UPLOAD", 3)
    assert upload(client, did, content=b"too large").status_code == 413
    assert entries(client, did) == []
    assert list((main.DATA / "temporary").iterdir()) == []
    assert upload(client, did, content=b"").status_code == 200


def test_rename_conflicts_and_text_editor(client):
    did = join(client)["default_id"]
    upload(client, did, content="你好\n世界".encode())
    upload(client, did, "other.txt")
    item = next(f for f in entries(client, did) if f["name"] == "测试.txt")
    assert client.post(path(did, "rename"), json={"path": "测试.txt", "destination": "other.txt", "version": item["version"]}).status_code == 409
    assert client.post(path(did, "rename"), json={"path": "测试.txt", "destination": "新名字.txt", "version": item["version"]}).status_code == 200
    result = client.get(path(did, "text"), params={"path": "新名字.txt"}).json()
    assert result["content"] == "你好\n世界"
    assert upload(client, did, "新名字.txt", b"changed", result["version"]).status_code == 200
    upload(client, did, "binary.txt", b"\xff\x00")
    assert client.get(path(did, "text"), params={"path": "binary.txt"}).status_code == 400
    result = client.get(path(did, "download"), params={"path": "新名字.txt"})
    assert result.headers["content-type"] == "application/octet-stream"
    assert result.headers["content-disposition"].startswith("attachment;")


def test_parallel_overwrites_only_one_wins(client):
    did = join(client)["default_id"]
    upload(client, did)
    ver = entries(client, did)[0]["version"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda content: upload(client, did, content=content, version=ver).status_code, [b"A", b"B"]))
    assert sorted(results) == [200, 409]
    assert len(client.get("/api/trash").json()) == 1


def test_restart_preserves_identity_files_and_preferences(client):
    user = join(client)
    upload(client, user["default_id"])
    client.put("/api/preferences", json={"pins": [user["default_id"]], "default_id": "public"})
    main.initialize()
    assert client.get("/api/me").json()["user"]["id"] == user["id"]
    assert client.get("/api/me").json()["user"]["default_id"] == "public"
    assert entries(client, user["default_id"])[0]["name"] == "测试.txt"


def test_unknown_directory_and_invalid_names(client):
    join(client)
    assert client.post("/api/directories", json={"name": "   "}).status_code == 400
    assert client.get(path("missing", "files")).status_code == 404
    assert client.post(path("public", "delete"), json={"path": "", "version": "x"}).status_code == 400
    assert client.get("/").status_code == 200
    assert client.get("/healthz").json()["status"] == "ok"


@pytest.mark.parametrize("operation", ["delete", "overwrite", "rename", "restore", "mkdir"])
def test_database_failure_rolls_back_file_mutations(client, monkeypatch, operation):
    did = join(client)["default_id"]
    upload(client, did)
    item = entries(client, did)[0]
    if operation == "restore":
        client.post(path(did, "delete"), json={"path": item["path"], "version": item["version"]})
        old = client.get("/api/trash").json()[0]

    def fail_log(*args):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(main, "log", fail_log)
    with pytest.raises(RuntimeError, match="simulated database failure"):
        if operation == "delete":
            client.post(path(did, "delete"), json={"path": item["path"], "version": item["version"]})
        elif operation == "overwrite":
            upload(client, did, content=b"new", version=item["version"])
        elif operation == "rename":
            client.post(path(did, "rename"), json={"path": item["path"], "version": item["version"], "destination": "changed.txt"})
        elif operation == "mkdir":
            client.post(path(did, "folders"), json={"path": "new"})
        else:
            client.post(f"/api/trash/{old['id']}/restore", json={"path": ""})
    if operation == "restore":
        assert entries(client, did) == []
        assert len(client.get("/api/trash").json()) == 1
        assert (main.DATA / "trash" / old["id"]).read_bytes() == b"first"
    else:
        assert len(entries(client, did)) == 1
        assert client.get(path(did, "download"), params={"path": "测试.txt"}).content == b"first"
        assert client.get("/api/trash").json() == []


def admin_login(client):
    with main.database() as db:
        db.execute("UPDATE administrators SET password_hash=? WHERE id=1", (password_hash("fixture-password-123"),))
    response = client.post("/api/admin/login", json={"name": "admin", "password": "fixture-password-123"})
    assert response.status_code == 200, response.text
    return client.cookies.get("lan_admin")


def normal_headers(token):
    return {"Cookie": f"lan_session={token}"}


def test_admin_name_is_reserved_and_password_never_bypassed(client):
    for body in ({"name": "admin"}, {"name": " ADMIN ", "create_new": True}, {"name": "admin", "existing_user_id": "forged"}):
        response = client.post("/api/join", json=body)
        assert response.json() == {"status": "admin_password_required"}
        assert "set-cookie" not in response.headers
    assert client.get("/api/admin/state").status_code == 401
    with main.database() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    join(client)
    assert client.patch("/api/profile", json={"name": "Admin"}).status_code == 400
    assert all(item["owner"] != "admin" for item in client.get("/api/directories").json())


def test_regular_identity_has_no_management_permission(client):
    user = join(client)
    assert client.get("/api/admin/state").status_code == 401
    for endpoint in (f"users/{user['id']}/disable", f"users/{user['id']}/restore", f"directories/{user['default_id']}/archive", f"directories/{user['default_id']}/restore"):
        assert client.post("/api/admin/" + endpoint).status_code == 401
    assert client.get("/api/me").status_code == 200
    assert len(entries(client, user["default_id"])) == 0


def test_admin_password_session_expiry_and_logout(client):
    assert client.post("/api/admin/login", json={"name": "admin", "password": "unset"}).status_code == 409
    admin_login(client)
    assert client.get("/api/admin/me").status_code == 200
    assert client.get("/api/me").status_code == 401
    token = client.cookies.get("lan_admin")
    assert client.post("/api/admin/logout").status_code == 200
    client.cookies.set("lan_admin", token)
    assert client.get("/api/admin/state").status_code == 401
    client.cookies.clear()
    admin_login(client)
    with main.database() as db:
        db.execute("UPDATE admin_sessions SET expires=0")
    assert client.get("/api/admin/state").status_code == 401


def test_admin_login_throttling_persists_and_password_hash_is_not_exposed(client):
    admin_login(client)
    client.post("/api/admin/logout")
    for _ in range(5):
        assert client.post("/api/admin/login", json={"name": "admin", "password": "wrong"}).status_code == 401
    main.initialize()
    assert client.post("/api/admin/login", json={"name": "admin", "password": "fixture-password-123"}).status_code == 429
    with main.database() as db:
        db.execute("UPDATE administrators SET locked_until=1")
        encoded = db.execute("SELECT password_hash FROM administrators").fetchone()[0]
        assert "fixture-password-123" not in encoded
    assert client.post("/api/admin/login", json={"name": "admin", "password": "fixture-password-123"}).status_code == 200
    assert "password" not in client.get("/api/admin/state").text


def test_disable_user_revokes_sessions_but_keeps_files_and_can_restore(client):
    user = join(client)
    token = client.cookies.get("lan_session")
    upload(client, user["default_id"])
    admin_login(client)
    assert client.post(f"/api/admin/users/{user['id']}/disable").status_code == 200
    assert client.get("/api/me", headers=normal_headers(token)).status_code == 401
    assert (main.DATA / "files" / user["default_id"] / "测试.txt").read_bytes() == b"first"
    assert client.post("/api/join", json={"name": "小王", "existing_user_id": user["id"]}).status_code == 409
    assert client.post(f"/api/admin/users/{user['id']}/restore").status_code == 200
    assert client.get("/api/me", headers=normal_headers(token)).status_code == 401
    assert client.post("/api/join", json={"name": "小王", "existing_user_id": user["id"]}).json()["user"]["id"] == user["id"]


def test_archive_directory_hides_links_and_updates_defaults_and_restore_preserves_files(client):
    user = join(client)
    did = user["default_id"]
    token = client.cookies.get("lan_session")
    upload(client, did)
    admin_login(client)
    assert client.post("/api/admin/directories/public/archive").status_code == 400
    assert client.post(f"/api/admin/directories/{did}/archive").status_code == 200
    headers = normal_headers(token)
    assert did not in [item["id"] for item in client.get("/api/directories", headers=headers).json()]
    assert client.get(path(did, "download"), params={"path": "测试.txt"}, headers=headers).status_code == 404
    me = client.get("/api/me", headers=headers).json()
    assert me["user"]["default_id"] == "public"
    assert "public" in me["pins"] and did not in me["pins"]
    assert client.put("/api/preferences", json={"pins": [did], "default_id": did}, headers=headers).status_code == 400
    state = client.get("/api/admin/state").json()
    assert next(d for d in state["directories"] if d["id"] == did)["deleted_at"]
    assert client.post(f"/api/admin/directories/{did}/restore").status_code == 200
    assert client.get(path(did, "download"), params={"path": "测试.txt"}, headers=headers).content == b"first"
    assert len(client.get("/api/admin/state").json()["activity"]) == 2


def test_user_disabled_during_upload_cannot_commit(client):
    user = join(client)
    def chunks():
        yield b"first"
        with main.database() as db:
            db.execute("UPDATE users SET disabled_at=1 WHERE id=?", (user["id"],))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        yield b"second"
    response = client.put(path(user["default_id"], "upload"), params={"path": "late.txt"}, content=chunks())
    assert response.status_code == 401
    assert not (main.DATA / "files" / user["default_id"] / "late.txt").exists()
    assert list((main.DATA / "temporary").iterdir()) == []


def prepare_copy(client):
    owner = join(client, "文件主人")
    upload(client, owner["default_id"], "资料.txt", "文件内容".encode())
    item = entries(client, owner["default_id"])[0]
    client.cookies.clear()
    copier = join(client, "复制用户")
    return owner, copier, item


def test_copy_uses_own_directory_and_never_overwrites(client):
    owner, copier, item = prepare_copy(client)
    client.put("/api/preferences", json={"pins": [owner["default_id"]], "default_id": owner["default_id"]})
    for expected in ("资料.txt", "资料 (副本).txt", "资料 (副本2).txt"):
        response = client.post(path(owner["default_id"], "copy"), json={"path": item["path"], "version": item["version"]})
        assert response.status_code == 200, response.text
        assert response.json()["directory_id"] == copier["default_id"]
        assert response.json()["path"] == expected
        assert client.get(path(copier["default_id"], "download"), params={"path": expected}).content == "文件内容".encode()
    assert entries(client, owner["default_id"])[0]["version"] == item["version"]


def test_copy_rejects_missing_identity_stale_version_and_traversal(client):
    owner, copier, item = prepare_copy(client)
    assert client.post(path(owner["default_id"], "copy"), json={"path": "../index.sqlite3", "version": item["version"]}).status_code == 400
    assert client.post(path(owner["default_id"], "copy"), json={"path": item["path"], "version": "outdated"}).status_code == 409
    client.cookies.clear()
    assert client.post(path(owner["default_id"], "copy"), json={"path": item["path"], "version": item["version"]}).status_code == 401
    assert not list((main.DATA / "files" / copier["default_id"]).iterdir())


def test_concurrent_copy_creates_distinct_names(client):
    owner, copier, item = prepare_copy(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post(path(owner["default_id"], "copy"), json={"path": item["path"], "version": item["version"]}), range(2)))
    assert all(result.status_code == 200 for result in results)
    assert {result.json()["path"] for result in results} == {"资料.txt", "资料 (副本).txt"}


@pytest.mark.parametrize("change", ["disable_user", "archive_destination"])
def test_copy_revalidates_after_background_io(client, monkeypatch, change):
    owner, copier, item = prepare_copy(client)
    original = main.copy_to_temporary
    def copying(source, temp):
        original(source, temp)
        with main.database() as db:
            if change == "disable_user":
                db.execute("UPDATE users SET disabled_at=1 WHERE id=?", (copier["id"],))
            else:
                db.execute("UPDATE directories SET deleted_at=1 WHERE id=?", (copier["default_id"],))
    monkeypatch.setattr(main, "copy_to_temporary", copying)
    response = client.post(path(owner["default_id"], "copy"), json={"path": item["path"], "version": item["version"]})
    assert response.status_code == (401 if change == "disable_user" else 404)
    assert not list((main.DATA / "files" / copier["default_id"]).iterdir())
    assert list((main.DATA / "temporary").iterdir()) == []


def test_folder_upload_preserves_paths_and_overwrite_versions(client):
    did = join(client)['default_id']
    target = '项目资料/子目录/说明.txt'
    params = {'path': target, 'create_directories': True}
    assert client.put(path(did, 'upload'), params=params, content=b'original').status_code == 200
    assert client.get(path(did, 'download'), params={'path': target}).content == b'original'
    assert client.put(path(did, 'upload'), params=params, content=b'changed').status_code == 409
    item = entries(client, did, '项目资料/子目录')[0]
    params['expected_version'] = item['version']
    assert client.put(path(did, 'upload'), params=params, content=b'changed').status_code == 200
    assert len(client.get('/api/trash').json()) == 1
    assert client.get('/api/me').json()['storage']['used_bytes'] == len(b'originalchanged')


@pytest.mark.parametrize('target', ['../outside/file.txt', '/outside/file.txt', 'safe/../../file.txt', 'safe//file.txt'])
def test_folder_upload_rejects_path_traversal(client, target):
    did = join(client)['default_id']
    response = client.put(path(did, 'upload'), params={'path': target, 'create_directories': True}, content=b'bad')
    assert response.status_code == 400
    assert entries(client, did) == []
    assert not main.storage_reservations


def test_folder_upload_rejects_file_parent_and_symlinks(client, tmp_path):
    did = join(client)['default_id']
    upload(client, did, 'block', b'file')
    root = main.DATA / 'files' / did
    (root / 'link').symlink_to(tmp_path, target_is_directory=True)
    for target in ('block/sub/file.txt', 'link/new/file.txt'):
        response = client.put(path(did, 'upload'), params={'path': target, 'create_directories': True}, content=b'bad')
        assert response.status_code in (400, 409)
    assert not (tmp_path / 'new').exists()
    assert not main.storage_reservations


def test_folder_creation_rolls_back_if_file_commit_fails(client, monkeypatch):
    did = join(client)['default_id']
    def fail_log(*args):
        raise RuntimeError('folder commit failed')
    monkeypatch.setattr(main, 'log', fail_log)
    with pytest.raises(RuntimeError, match='folder commit failed'):
        client.put(path(did, 'upload'), params={'path': 'root/sub/file.txt', 'create_directories': True}, content=b'data')
    assert entries(client, did) == []
    assert not list((main.DATA / 'temporary').iterdir())
    assert not main.storage_reservations


def test_quota_spans_owned_directories_and_charges_directory_owner(client, monkeypatch):
    monkeypatch.setattr(main, 'STORAGE_LIMIT', 10)
    owner = join(client, '所有者')
    extra = client.post('/api/directories', json={'name': '第二个目录'}).json()['id']
    assert upload(client, owner['default_id'], content=b'123456').status_code == 200
    client.cookies.clear()
    writer = join(client, '协作者')
    assert upload(client, extra, content=b'1234').status_code == 200
    assert upload(client, extra, 'full.txt', b'x').status_code == 413
    storage = client.get(path(extra, 'files')).json()['storage']
    assert storage['owner_id'] == owner['id'] and storage['used_bytes'] == 10
    assert client.get('/api/me').json()['storage']['used_bytes'] == 0
    assert upload(client, writer['default_id'], content=b'x').status_code == 200
    assert not main.storage_reservations


def test_quota_counts_overwrite_trash_archive_and_survives_restart(client, monkeypatch):
    monkeypatch.setattr(main, 'STORAGE_LIMIT', 10)
    user = join(client)
    did = user['default_id']
    assert upload(client, did, content=b'123456').status_code == 200
    item = entries(client, did)[0]
    assert upload(client, did, content=b'12345', version=item['version']).status_code == 413
    assert upload(client, did, content=b'1234', version=item['version']).status_code == 200
    item = entries(client, did)[0]
    assert client.post(path(did, 'delete'), json={'path': item['path'], 'version': item['version']}).status_code == 200
    assert client.get('/api/me').json()['storage']['used_bytes'] == 10
    history = client.get('/api/trash').json()[0]
    assert client.post(f"/api/trash/{history['id']}/restore", json={'path': 'restored.txt'}).status_code == 200
    assert client.get('/api/me').json()['storage']['used_bytes'] == 10
    admin_login(client)
    assert client.post(f'/api/admin/directories/{did}/archive').status_code == 200
    main.initialize()
    state = client.get('/api/admin/state').json()
    assert next(u for u in state['users'] if u['id'] == user['id'])['storage']['used_bytes'] == 10


def test_public_quota_is_one_shared_pool(client, monkeypatch):
    monkeypatch.setattr(main, 'STORAGE_LIMIT', 10)
    join(client, '甲')
    assert upload(client, 'public', 'a.txt', b'123456').status_code == 200
    client.cookies.clear()
    join(client, '乙')
    assert upload(client, 'public', 'b.txt', b'12345').status_code == 413
    assert upload(client, 'public', 'b.txt', b'1234').status_code == 200
    assert client.get('/api/me').json()['storage']['used_bytes'] == 0
    assert client.get(path('public', 'files')).json()['storage']['used_bytes'] == 10


def test_chunked_upload_cannot_exceed_quota_and_cleans_temporary(client, monkeypatch):
    monkeypatch.setattr(main, 'STORAGE_LIMIT', 5)
    did = join(client)['default_id']
    result = client.put(path(did, 'upload'), params={'path': 'root/file.txt', 'create_directories': True}, content=iter([b'123', b'456']))
    assert result.status_code == 413
    assert entries(client, did) == []
    assert not list((main.DATA / 'temporary').iterdir())
    assert not main.storage_reservations
    assert upload(client, did, content=b'12345').status_code == 200


def test_copy_reserves_space_against_parallel_upload(client, monkeypatch):
    owner, copier, item = prepare_copy(client)
    monkeypatch.setattr(main, 'STORAGE_LIMIT', item['size'])
    started, release = threading.Event(), threading.Event()
    original = main.copy_to_temporary
    def copying(source, temporary):
        started.set()
        assert release.wait(5)
        original(source, temporary)
    monkeypatch.setattr(main, 'copy_to_temporary', copying)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, path(owner['default_id'], 'copy'), json={'path': item['path'], 'version': item['version']})
        try:
            assert started.wait(5)
            assert upload(client, copier['default_id'], 'race.txt', b'x').status_code == 413
        finally:
            release.set()
        assert future.result().status_code == 200
    assert client.post(path(owner['default_id'], 'copy'), json={'path': item['path'], 'version': item['version']}).status_code == 413
    assert not main.storage_reservations
    assert len(entries(client, copier['default_id'])) == 1


def test_parallel_uploads_cannot_exceed_user_quota(client, monkeypatch):
    monkeypatch.setattr(main, 'STORAGE_LIMIT', 10)
    did = join(client)['default_id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda n: upload(client, did, f'{n}.txt', b'123456'), range(2)))
    assert sorted(r.status_code for r in results) == [200, 413]
    assert client.get('/api/me').json()['storage']['used_bytes'] == 6
    assert not main.storage_reservations


def test_global_storage_settings_require_admin_and_persist_for_every_scope(client):
    assert client.put('/api/admin/storage', json={'limit_gib': 30}).status_code == 401
    user = join(client)
    assert client.put('/api/admin/storage', json={'limit_gib': 30}).status_code == 401
    admin_login(client)
    assert client.get('/api/admin/state').json()['storage_limit_bytes'] == 20 * 1024**3
    assert client.put('/api/admin/storage', json={'limit_gib': 30}).status_code == 200
    main.initialize()
    assert main.storage_status(user['id'])['limit_bytes'] == 30 * 1024**3
    assert main.storage_status(None)['limit_bytes'] == 30 * 1024**3
    client.cookies.clear()
    new = join(client, '新用户')
    assert main.storage_status(new['id'])['limit_bytes'] == 30 * 1024**3
    admin_login(client)
    assert client.get('/api/admin/state').json()['activity'][0]['action'] == 'update_storage'


@pytest.mark.parametrize('body', [{'limit_gib': 0}, {'limit_gib': -1}, {'limit_gib': 10241}, {'limit_gib': 1.5}, {'limit_gib': True}, {'limit_gib': 20, 'user_id': 'specific-user'}])
def test_storage_settings_reject_invalid_limits_and_individual_overrides(client, body):
    admin_login(client)
    assert client.put('/api/admin/storage', json=body).status_code == 422
    assert main.storage_limit() == 20 * 1024**3


def test_reducing_global_limit_keeps_files_and_blocks_growth_without_exposing_sizes(client):
    user = join(client)
    did = user['default_id']
    target = main.DATA / 'files' / did / 'large.bin'
    with target.open('wb') as stream:
        stream.truncate(2 * 1024**3)
    token = client.cookies.get('lan_session')
    admin_login(client)
    assert client.put('/api/admin/storage', json={'limit_gib': 1}).status_code == 200
    assert target.stat().st_size == 2 * 1024**3
    response = client.put(path(did, 'upload'), headers=normal_headers(token), params={'path': 'extra.txt'}, content=b'x')
    assert response.status_code == 413
    assert response.json()['detail'] == '空间已满，请联系管理员调整空间。'
    assert client.put('/api/admin/storage', json={'limit_gib': 3}).status_code == 200
    assert client.put(path(did, 'upload'), headers=normal_headers(token), params={'path': 'extra.txt'}, content=b'x').status_code == 200


def test_limit_reduction_revalidates_inflight_copy(client, monkeypatch):
    owner, copier, item = prepare_copy(client)
    original = main.copy_to_temporary
    def copying(source, temporary):
        original(source, temporary)
        with main.database() as db:
            db.execute('INSERT INTO storage_settings VALUES (1,?)', (item['size']-1,))
    monkeypatch.setattr(main, 'copy_to_temporary', copying)
    response = client.post(path(owner['default_id'], 'copy'), json={'path': item['path'], 'version': item['version']})
    assert response.status_code == 413
    assert not entries(client, copier['default_id'])
    assert not main.storage_reservations
    assert not list((main.DATA / 'temporary').iterdir())


def test_hidden_files_and_directories_are_uploaded_listed_and_downloaded(client):
    did = join(client)['default_id']
    for name in ('.env', '.config/settings.json', 'project/.git/config'):
        r = client.put(path(did, 'upload'), params={'path':name,'create_directories':True}, content=b'demo only')
        assert r.status_code == 200
        assert client.get(path(did,'download'),params={'path':name}).content == b'demo only'
    assert {i['name'] for i in entries(client,did)} == {'.env','.config','project'}
    assert entries(client,did,'project')[0]['name'] == '.git'


def test_dragged_empty_directories_merge_without_replacing_files(client):
    did = join(client)['default_id']
    body = {'path':'root/.hidden/empty','parents':True,'exist_ok':True}
    r=client.post(path(did,'folders'),json=body)
    assert r.status_code == 200 and r.json()['created'] is True
    assert client.post(path(did,'folders'),json=body).json()['created'] is False
    assert entries(client,did,'root/.hidden')[0]['name'] == 'empty'
    assert upload(client,did,'block',b'keep').status_code == 200
    assert client.post(path(did,'folders'),json={'path':'block/new','parents':True,'exist_ok':True}).status_code == 409
    assert client.post(path(did,'folders'),json={'path':'block','parents':True,'exist_ok':True}).status_code == 409
    assert client.get(path(did,'download'),params={'path':'block'}).content==b'keep'


def test_recursive_folder_creation_rejects_traversal_and_rolls_back(client,monkeypatch):
    did=join(client)['default_id']
    assert client.post(path(did,'folders'),json={'path':'../escape','parents':True,'exist_ok':True}).status_code==400
    root=main.DATA/'files'/did
    (root/'link').symlink_to(main.DATA,target_is_directory=True)
    assert client.post(path(did,'folders'),json={'path':'link/sub','parents':True,'exist_ok':True}).status_code==400
    def fail(*args):raise RuntimeError('failure')
    monkeypatch.setattr(main,'log',fail)
    with pytest.raises(RuntimeError,match='failure'):
        client.post(path(did,'folders'),json={'path':'new/sub/empty','parents':True,'exist_ok':True})
    assert not (root/'new').exists()


def test_move_file_into_nested_folder_and_back_keeps_owner_quota(client):
    user=join(client)
    did=user['default_id']
    upload(client,did,'notes.txt',b'content')
    client.post(path(did,'folders'),json={'path':'a/b','parents':True})
    original_usage=client.get('/api/me').json()['storage']['used_bytes']
    item=next(i for i in entries(client,did) if i['name']=='notes.txt')
    response=client.post(path(did,'rename'),json={'path':item['path'],'version':item['version'],'destination':'a/b/notes.txt'})
    assert response.status_code==200
    assert client.get(path(did,'download'),params={'path':'notes.txt'}).status_code==404
    assert client.get(path(did,'download'),params={'path':'a/b/notes.txt'}).content==b'content'
    assert client.get('/api/activity').json()[0]['action']=='move'
    moved=entries(client,did,'a/b')[0]
    assert client.post(path(did,'rename'),json={'path':moved['path'],'version':moved['version'],'destination':'notes.txt'}).status_code==200
    assert client.get('/api/me').json()['storage']['used_bytes']==original_usage


def test_move_directory_keeps_descendants_and_empty_hidden_folders(client,monkeypatch):
    did=join(client)['default_id']
    client.put(path(did,'upload'),params={'path':'source/.hidden/note.txt','create_directories':True},content=b'keep')
    client.post(path(did,'folders'),json={'path':'source/empty','parents':True})
    client.post(path(did,'folders'),json={'path':'target'})
    monkeypatch.setattr(main,'STORAGE_LIMIT',1)
    item=next(i for i in entries(client,did) if i['name']=='source')
    assert client.post(path(did,'rename'),json={'path':item['path'],'version':item['version'],'destination':'target/source'}).status_code==200
    assert client.get(path(did,'download'),params={'path':'target/source/.hidden/note.txt'}).content==b'keep'
    assert entries(client,did,'target/source/empty')==[]
    assert client.get(path(did,'files'),params={'path':'source'}).status_code==404


def test_move_rejects_cycles_conflicts_stale_versions_and_cross_root_paths(client):
    did=join(client)['default_id']
    upload(client,did,'source.txt',b'source')
    client.post(path(did,'folders'),json={'path':'target'})
    upload(client,did,'target/source.txt',b'original')
    item=next(i for i in entries(client,did) if i['name']=='source.txt')
    for destination in ('target/source.txt','missing/source.txt','../public/source.txt','/public/source.txt'):
        r=client.post(path(did,'rename'),json={'path':item['path'],'version':item['version'],'destination':destination})
        assert r.status_code in (400,404,409)
    assert client.post(path(did,'rename'),json={'path':item['path'],'version':'stale','destination':'target/new.txt'}).status_code==409
    assert client.get(path(did,'download'),params={'path':'target/source.txt'}).content==b'original'
    folder=next(i for i in entries(client,did) if i['name']=='target')
    assert client.post(path(did,'rename'),json={'path':folder['path'],'version':folder['version'],'destination':'target/inside'}).status_code==409


def test_move_rollback_restores_original_tree(client,monkeypatch):
    did=join(client)['default_id']
    client.put(path(did,'upload'),params={'path':'source/sub/note.txt','create_directories':True},content=b'keep')
    client.post(path(did,'folders'),json={'path':'target'})
    item=next(i for i in entries(client,did) if i['name']=='source')
    def fail(*args):raise RuntimeError('move commit failed')
    monkeypatch.setattr(main,'log',fail)
    with pytest.raises(RuntimeError,match='move commit failed'):
        client.post(path(did,'rename'),json={'path':item['path'],'version':item['version'],'destination':'target/source'})
    assert client.get(path(did,'download'),params={'path':'source/sub/note.txt'}).content==b'keep'
    assert entries(client,did,'target')==[]


def test_folder_download_zip_includes_hidden_and_empty_content(client):
    import io, zipfile
    did=join(client)['default_id']
    client.put(path(did,'upload'),params={'path':'资料/.hidden/data.txt','create_directories':True},content=b'content')
    client.post(path(did,'folders'),json={'path':'资料/empty','parents':True})
    result=client.get(path(did,'download'),params={'path':'资料'})
    assert result.status_code==200
    assert result.headers['content-type']=='application/zip'
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        assert archive.read('资料/.hidden/data.txt')==b'content'
        assert '资料/empty/' in archive.namelist()
    assert not list((main.DATA/'temporary').iterdir())


def test_folder_download_rejects_nested_symlink(client,tmp_path):
    did=join(client)['default_id']
    client.post(path(did,'folders'),json={'path':'folder'})
    (main.DATA/'files'/did/'folder'/'escape').symlink_to(tmp_path,target_is_directory=True)
    assert client.get(path(did,'download'),params={'path':'folder'}).status_code==400
    assert not list((main.DATA/'temporary').iterdir())
