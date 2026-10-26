import asyncio
import hashlib
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import main as core

router = APIRouter(prefix="/api/admin")


def require_admin(request):
    digest = hashlib.sha256(request.cookies.get("lan_admin", "").encode()).hexdigest()
    with core.database() as db:
        row = db.execute("SELECT expires FROM admin_sessions WHERE token_hash=? AND expires>?", (digest, time.time())).fetchone()
    if not row:
        raise HTTPException(401, "请重新验证管理员密码")


def record(db, action, target_id, label):
    db.execute("INSERT INTO admin_activity(action,target_id,label,created) VALUES (?,?,?,?)", (action, target_id, label, time.time()))


class LoginBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class StorageBody(BaseModel):
    model_config = {"extra": "forbid"}
    limit_gib: int = Field(ge=1, le=10240, strict=True)


@router.put("/storage")
async def update_storage(body: StorageBody, request: Request):
    async with core.mutation_lock:
        require_admin(request)
        limit = body.limit_gib * 1024 ** 3
        with core.database() as db:
            db.execute("INSERT INTO storage_settings(id,limit_bytes) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET limit_bytes=excluded.limit_bytes", (limit,))
            record(db, "update_storage", "global", f"统一空间上限设为 {body.limit_gib} GiB")
    return {"ok": True, "limit_bytes": limit}


@router.post("/login")
async def login(body: LoginBody):
    if core.identity_key(body.name) != core.ADMIN_NAME:
        raise HTTPException(401, "账户或密码不正确")
    async with core.mutation_lock:
        with core.database() as db:
            admin = dict(db.execute("SELECT * FROM administrators WHERE id=1").fetchone())
        if not admin["password_hash"]:
            raise HTTPException(409, "管理员密码尚未设置，请先在服务器终端运行 set-admin-password.sh")
        if admin["locked_until"] > time.time():
            raise HTTPException(429, "尝试次数较多，请一分钟后重试")
        valid = await asyncio.to_thread(core.password_matches, body.password, admin["password_hash"])
        with core.database() as db:
            latest = db.execute("SELECT password_hash FROM administrators WHERE id=1").fetchone()[0]
            if latest != admin["password_hash"]:
                raise HTTPException(409, "管理员密码刚刚更新，请重试")
            if not valid:
                failures = (0 if admin["locked_until"] else admin["failures"]) + 1
                db.execute("UPDATE administrators SET failures=?, locked_until=? WHERE id=1", (failures, time.time() + 60 if failures >= 5 else 0))
            else:
                token = secrets.token_urlsafe(32)
                db.execute("UPDATE administrators SET failures=0,locked_until=0 WHERE id=1")
                db.execute("DELETE FROM admin_sessions WHERE expires<=?", (time.time(),))
                db.execute("INSERT INTO admin_sessions VALUES (?,?)", (hashlib.sha256(token.encode()).hexdigest(), time.time() + core.ADMIN_SESSION_SECONDS))
        if not valid:
            raise HTTPException(401, "账户或密码不正确")
    response = JSONResponse({"ok": True})
    response.set_cookie("lan_admin", token, max_age=core.ADMIN_SESSION_SECONDS, httponly=True, secure=core.COOKIE_SECURE, samesite="strict", path="/")
    response.delete_cookie("lan_session", path="/")
    return response


@router.get("/me")
def me(request: Request):
    require_admin(request)
    return {"name": core.ADMIN_NAME}


@router.post("/logout")
async def logout(request: Request):
    digest = hashlib.sha256(request.cookies.get("lan_admin", "").encode()).hexdigest()
    async with core.mutation_lock:
        with core.database() as db:
            db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (digest,))
    response = JSONResponse({"ok": True})
    response.delete_cookie("lan_admin", path="/", httponly=True, secure=core.COOKIE_SECURE, samesite="strict")
    return response


@router.get("/state")
def state(request: Request):
    require_admin(request)
    with core.database() as db:
        users = [dict(row) for row in db.execute("SELECT u.*, (SELECT COUNT(*) FROM directories d WHERE d.owner_id=u.id AND d.deleted_at IS NULL) AS directory_count FROM users u ORDER BY u.created")]
        directories = [dict(row) for row in db.execute("SELECT d.*, u.name AS owner FROM directories d LEFT JOIN users u ON u.id=d.owner_id ORDER BY d.created")]
        activity = [dict(row) for row in db.execute("SELECT * FROM admin_activity ORDER BY id DESC LIMIT 100")]
    for directory in directories:
        root = core.DATA / "files" / directory["id"]
        directory["entries"] = sum(1 for _ in root.iterdir()) if root.is_dir() and not root.is_symlink() else None
    for user in users:
        user["storage"] = core.storage_status(user["id"])
    return {"users": users, "directories": directories, "activity": activity, "storage_limit_bytes": core.storage_limit()}


@router.post("/users/{uid}/disable")
async def disable_user(uid: str, request: Request):
    async with core.mutation_lock:
        require_admin(request)
        with core.database() as db:
            user = db.execute("SELECT * FROM users WHERE id=? AND disabled_at IS NULL", (uid,)).fetchone()
            if not user:
                raise HTTPException(404, "用户不存在或已停用")
            db.execute("UPDATE users SET disabled_at=? WHERE id=?", (time.time(), uid))
            db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            record(db, "disable_user", uid, user["name"])
    return {"ok": True}


@router.post("/users/{uid}/restore")
async def restore_user(uid: str, request: Request):
    async with core.mutation_lock:
        require_admin(request)
        with core.database() as db:
            user = db.execute("SELECT * FROM users WHERE id=? AND disabled_at IS NOT NULL", (uid,)).fetchone()
            if not user:
                raise HTTPException(404, "用户不存在或未停用")
            if db.execute("SELECT 1 FROM users WHERE name_key=? AND disabled_at IS NULL AND id<>?", (user["name_key"], uid)).fetchone():
                raise HTTPException(409, "已有启用中的同名用户，不能恢复重复身份")
            db.execute("UPDATE users SET disabled_at=NULL WHERE id=?", (uid,))
            record(db, "restore_user", uid, user["name"])
    return {"ok": True}


@router.post("/directories/{did}/archive")
async def archive_directory(did: str, request: Request):
    async with core.mutation_lock:
        require_admin(request)
        if did == "public":
            raise HTTPException(400, "公共目录是默认回退位置，不能移入管理回收站")
        with core.database() as db:
            directory = db.execute("SELECT * FROM directories WHERE id=? AND deleted_at IS NULL", (did,)).fetchone()
            if not directory:
                raise HTTPException(404, "目录不存在或已移入管理回收站")
            db.execute("UPDATE directories SET deleted_at=? WHERE id=?", (time.time(), did))
            db.execute("UPDATE users SET default_id='public' WHERE default_id=?", (did,))
            db.execute("DELETE FROM pins WHERE directory_id=?", (did,))
            db.execute("INSERT OR IGNORE INTO pins(user_id,directory_id) SELECT id,'public' FROM users WHERE default_id='public'")
            record(db, "archive_directory", did, directory["name"])
    return {"ok": True}


@router.post("/directories/{did}/restore")
async def restore_directory(did: str, request: Request):
    async with core.mutation_lock:
        require_admin(request)
        with core.database() as db:
            directory = db.execute("SELECT * FROM directories WHERE id=? AND deleted_at IS NOT NULL", (did,)).fetchone()
            if not directory:
                raise HTTPException(404, "目录不存在或未被移入管理回收站")
            db.execute("UPDATE directories SET deleted_at=NULL WHERE id=?", (did,))
            record(db, "restore_directory", did, directory["name"])
    return {"ok": True}
