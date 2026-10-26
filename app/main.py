import asyncio
import errno
import hashlib
import os
import secrets
import shutil
import sqlite3
import time
import unicodedata
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .admin_security import password_matches

DATA = Path(os.environ.get("DATA_DIR", "/data")).absolute()
STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "1024")) * 1024 * 1024
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
SESSION_SECONDS = 365 * 24 * 3600
mutation_lock = asyncio.Lock()
ADMIN_NAME = "admin"
ADMIN_SESSION_SECONDS = 8 * 3600
STORAGE_LIMIT = 20 * 1024 ** 3
storage_reservations = {}


@contextmanager
def database():
    conn = sqlite3.connect(DATA / "index.sqlite3", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@contextmanager
def file_transaction():
    undo = []
    try:
        with database() as db:
            yield db, undo
    except Exception:
        for operation in reversed(undo):
            operation()
        raise


def initialize():
    for name in ("files", "trash", "temporary"):
        (DATA / name).mkdir(parents=True, exist_ok=True)
    with database() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, default_id TEXT, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS directories (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_id TEXT REFERENCES users(id), created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS pins (
          user_id TEXT NOT NULL REFERENCES users(id), directory_id TEXT NOT NULL REFERENCES directories(id),
          PRIMARY KEY(user_id, directory_id));
        CREATE TABLE IF NOT EXISTS trash (
          id TEXT PRIMARY KEY, directory_id TEXT NOT NULL REFERENCES directories(id), path TEXT NOT NULL,
          actor_id TEXT NOT NULL REFERENCES users(id), reason TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS activity (
          id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT NOT NULL REFERENCES users(id),
          directory_id TEXT NOT NULL REFERENCES directories(id), action TEXT NOT NULL,
          path TEXT NOT NULL, created REAL NOT NULL);
        """)
        for table, column in (("users", "disabled_at"), ("directories", "deleted_at")):
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} REAL")
        if "name_key" not in {row[1] for row in db.execute("PRAGMA table_info(users)")}:
            db.execute("ALTER TABLE users ADD COLUMN name_key TEXT")
        for row in db.execute("SELECT id,name FROM users WHERE name_key IS NULL").fetchall():
            db.execute("UPDATE users SET name_key=? WHERE id=?", (identity_key(row["name"]), row["id"]))
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS unique_active_user_name ON users(name_key) WHERE disabled_at IS NULL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS administrators (
          id INTEGER PRIMARY KEY CHECK(id=1), password_hash TEXT,
          failures INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0);
        INSERT OR IGNORE INTO administrators(id) VALUES (1);
        CREATE TABLE IF NOT EXISTS admin_sessions (
          token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS admin_activity (
          id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
          target_id TEXT NOT NULL, label TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS storage_settings (
          id INTEGER PRIMARY KEY CHECK(id=1), limit_bytes INTEGER NOT NULL CHECK(limit_bytes>0));
        """)
        db.execute("INSERT OR IGNORE INTO directories(id,name,owner_id,created) VALUES (?, ?, NULL, ?)", ("public", "公共目录", time.time()))
    (DATA / "files" / "public").mkdir(exist_ok=True)
    # Unfinished uploads are never published into shared directories.
    for item in (DATA / "temporary").iterdir():
        if item.is_file():
            item.unlink()


@asynccontextmanager
async def lifespan(app):
    initialize()
    yield


app = FastAPI(title="邻间 · 局域网共享文件站", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(OSError)
async def storage_error(request, exc):
    if exc.errno == errno.ENOSPC:
        return JSONResponse({"detail": "服务器磁盘空间不足，请联系管理员整理空间后重试"}, status_code=507)
    return JSONResponse({"detail": "无法读写服务器文件，请检查存储状态后重试"}, status_code=500)


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
        # Cross-origin pages cannot send this custom header without an allowed CORS preflight.
        if request.headers.get("x-lan-share") != "1":
            return JSONResponse({"detail": "请从文件站页面执行此操作"}, status_code=403)
        origin = request.headers.get("origin")
        expected = str(request.base_url).rstrip("/")
        if origin and origin != expected:
            return JSONResponse({"detail": "不允许跨站操作"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "不允许跨站操作"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def current_user(request):
    token = request.cookies.get("lan_session", "")
    digest = hashlib.sha256(token.encode()).hexdigest()
    with database() as db:
        row = db.execute("SELECT u.* FROM users u JOIN sessions s ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>? AND u.disabled_at IS NULL", (digest, time.time())).fetchone()
    if not row:
        raise HTTPException(401, "先填写名字，就能开始使用")
    return dict(row)


def clean_name(value):
    value = value.strip()
    if not value or len(value) > 100 or any(ord(c) < 32 for c in value):
        raise HTTPException(400, "请输入 1–100 个字符的名称")
    return value


def identity_key(value):
    return unicodedata.normalize("NFKC", value).strip().casefold()


def relative(value, allow_empty=True):
    if not value and allow_empty:
        return ""
    if not value or len(value) > 2000 or any(ord(c) < 32 for c in value) or "\\" in value:
        raise HTTPException(400, "路径不合法")
    parts = value.split("/")
    if any(p in ("", ".", "..") or len(p.encode()) > 255 for p in parts):
        raise HTTPException(400, "路径不合法")
    return "/".join(parts)


def locate(directory_id, value="", must_exist=True):
    with database() as db:
        found = db.execute("SELECT id FROM directories WHERE id=? AND deleted_at IS NULL", (directory_id,)).fetchone()
    if not found:
        raise HTTPException(404, "目录不存在")
    value = relative(value)
    root = DATA / "files" / directory_id
    target = root
    if root.is_symlink():
        raise HTTPException(400, "不支持符号链接")
    for part in value.split("/") if value else []:
        target /= part
        if target.is_symlink():
            raise HTTPException(400, "不支持符号链接")
    if not target.resolve().is_relative_to(root.resolve()):
        raise HTTPException(400, "路径越界")
    if must_exist and not target.exists():
        raise HTTPException(404, "文件或目录已不存在，请刷新")
    return target


def version(path):
    st = path.stat()
    return f"{st.st_ino:x}-{st.st_mtime_ns:x}-{st.st_size:x}"


def check_version(path, expected):
    if not expected or version(path) != expected:
        raise HTTPException(409, "文件已发生变化，请刷新后再操作")


def log(db, user, directory_id, action, path):
    db.execute("INSERT INTO activity(actor_id, directory_id, action, path, created) VALUES (?,?,?,?,?)", (user["id"], directory_id, action, path, time.time()))


def stash(db, undo, user, directory_id, value, target, reason):
    trash_id = uuid.uuid4().hex
    os.replace(target, DATA / "trash" / trash_id)
    undo.append(lambda: os.replace(DATA / "trash" / trash_id, target))
    db.execute("INSERT INTO trash VALUES (?,?,?,?,?,?)", (trash_id, directory_id, value, user["id"], reason, time.time()))
    return trash_id


def profile(user):
    with database() as db:
        pins = [r[0] for r in db.execute("SELECT directory_id FROM pins WHERE user_id=?", (user["id"],))]
    return {"user": user, "pins": pins, "max_upload_bytes": MAX_UPLOAD, "share_base_url": PUBLIC_URL, "storage": storage_status(user["id"])}


def tree_bytes(root):
    if root.is_symlink() or not root.exists():
        return 0
    if root.is_file():
        try:
            return root.stat().st_size
        except FileNotFoundError:
            return 0
    total = 0
    for parent, folders, files in os.walk(root, followlinks=False):
        folders[:] = [name for name in folders if not (Path(parent) / name).is_symlink()]
        for name in files:
            item = Path(parent) / name
            if not item.is_symlink():
                try:
                    total += item.stat().st_size
                except FileNotFoundError:
                    pass
    return total


def storage_limit():
    with database() as db:
        row = db.execute("SELECT limit_bytes FROM storage_settings WHERE id=1").fetchone()
    return row[0] if row else STORAGE_LIMIT


def storage_status(owner_id):
    with database() as db:
        directories = [row[0] for row in db.execute("SELECT id FROM directories WHERE owner_id IS ?", (owner_id,))]
        history = [row[0] for row in db.execute("SELECT t.id FROM trash t JOIN directories d ON d.id=t.directory_id WHERE d.owner_id IS ?", (owner_id,))]
        owner = db.execute("SELECT name FROM users WHERE id=?", (owner_id,)).fetchone()
    used = sum(tree_bytes(DATA / "files" / did) for did in directories)
    used += sum(tree_bytes(DATA / "trash" / tid) for tid in history)
    reserved = sum(size for scope, size in list(storage_reservations.values()) if scope == owner_id)
    limit = storage_limit()
    return {"owner_id": owner_id, "owner_name": owner[0] if owner else "公共目录", "used_bytes": used,
            "reserved_bytes": reserved, "limit_bytes": limit,
            "available_bytes": max(0, limit - used - reserved)}


def storage_owner(did):
    with database() as db:
        row = db.execute("SELECT owner_id FROM directories WHERE id=? AND deleted_at IS NULL", (did,)).fetchone()
    if not row:
        raise HTTPException(404, "目录不存在")
    return row[0]


def reserve_storage(key, owner_id, size):
    current = storage_reservations.get(key, (owner_id, 0))[1]
    status = storage_status(owner_id)
    if size > 0 and status["used_bytes"] + status["reserved_bytes"] - current + size > status["limit_bytes"]:
        raise HTTPException(413, "空间已满，请联系管理员调整空间。")
    storage_reservations[key] = (owner_id, size)


def create_parents(target, root, undo):
    missing = []
    parent = target.parent
    while parent != root and not parent.exists():
        missing.append(parent)
        parent = parent.parent
    if not parent.is_dir():
        raise HTTPException(409, "上级路径与已有文件冲突")
    for parent in reversed(missing):
        parent.mkdir()
        undo.append(parent.rmdir)


class NameBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class JoinBody(NameBody):
    existing_user_id: str | None = Field(default=None, min_length=1, max_length=64)
    create_new: bool = False


class Preferences(BaseModel):
    pins: list[str] = Field(max_length=500)
    default_id: str


class PathBody(BaseModel):
    path: str = Field(max_length=2000)


class FolderBody(PathBody):
    parents: bool = False
    exist_ok: bool = False


class ChangeBody(PathBody):
    version: str = Field(max_length=200)
    destination: str | None = Field(default=None, max_length=2000)


class CopyBody(PathBody):
    version: str = Field(max_length=200)


@app.get("/healthz")
def health():
    with database() as db:
        db.execute("SELECT 1").fetchone()
    return {"status": "ok"}


@app.get("/api/me")
def me(request: Request):
    return profile(current_user(request))


@app.post("/api/join")
async def join(body: JoinBody, request: Request):
    if body.existing_user_id and body.create_new:
        raise HTTPException(400, "请选择继续使用已有空间，或新建空间")
    try:
        return profile(current_user(request))
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
    name = clean_name(body.name)
    key = identity_key(name)
    if key == ADMIN_NAME:
        return {"status": "admin_password_required"}
    uid, did, token = uuid.uuid4().hex, uuid.uuid4().hex, secrets.token_urlsafe(32)
    now = time.time()
    async with mutation_lock:
        with file_transaction() as (db, undo):
            if body.existing_user_id:
                found = db.execute("SELECT * FROM users WHERE id=? AND name_key=? AND disabled_at IS NULL", (body.existing_user_id, key)).fetchone()
                if not found:
                    raise HTTPException(409, "这个身份已发生变化，请返回重新填写名字")
                user = dict(found)
                uid = user["id"]
            else:
                candidates = db.execute("SELECT u.id, u.name, u.created, d.name AS directory_name FROM users u LEFT JOIN directories d ON d.id=u.default_id WHERE u.name_key=? AND u.disabled_at IS NULL ORDER BY u.created, u.id", (key,)).fetchall()
                if candidates:
                    return {"status": "choose_identity", "candidates": [dict(row) for row in candidates]}
                if db.execute("SELECT 1 FROM users WHERE name_key=?", (key,)).fetchone():
                    raise HTTPException(409, "这个名字的用户已停用，请联系管理员恢复，或使用其他名字")
                root = DATA / "files" / did
                root.mkdir()
                undo.append(root.rmdir)
                db.execute("INSERT INTO users(id,name,name_key,default_id,created) VALUES (?,?,?,?,?)", (uid, name, key, did, now))
                db.execute("INSERT INTO directories(id,name,owner_id,created) VALUES (?,?,?,?)", (did, f"{name}的目录", uid, now))
                db.execute("INSERT INTO pins VALUES (?,?)", (uid, did))
                user = {"id": uid, "name": name, "default_id": did, "created": now}
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), uid, now + SESSION_SECONDS))
    response = JSONResponse(profile(user))
    response.set_cookie("lan_session", token, max_age=SESSION_SECONDS, httponly=True, secure=COOKIE_SECURE, samesite="strict", path="/")
    return response


@app.post("/api/logout")
async def logout(request: Request):
    digest = hashlib.sha256(request.cookies.get("lan_session", "").encode()).hexdigest()
    async with mutation_lock:
        with database() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest,))
    response = JSONResponse({"ok": True})
    response.delete_cookie("lan_session", path="/", httponly=True, secure=COOKIE_SECURE, samesite="strict")
    return response


@app.patch("/api/profile")
async def rename_profile(body: NameBody, request: Request):
    user = current_user(request)
    name = clean_name(body.name)
    key = identity_key(name)
    if key == ADMIN_NAME:
        raise HTTPException(400, "这个名字已保留，请使用其他名字")
    async with mutation_lock:
        user = current_user(request)
        with database() as db:
            if db.execute("SELECT 1 FROM users WHERE name_key=? AND id<>?", (key, user["id"])).fetchone():
                if key != user["name_key"]:
                    raise HTTPException(409, "这个名字已被使用，请使用其他名字")
            db.execute("UPDATE users SET name=?,name_key=? WHERE id=?", (name, key, user["id"]))
    return {"ok": True}


@app.put("/api/preferences")
async def preferences(body: Preferences, request: Request):
    user = current_user(request)
    async with mutation_lock:
        user = current_user(request)
        with database() as db:
            valid = {r[0] for r in db.execute("SELECT id FROM directories WHERE deleted_at IS NULL")}
            if body.default_id not in valid or not set(body.pins) <= valid:
                raise HTTPException(400, "所选目录不存在")
            db.execute("DELETE FROM pins WHERE user_id=?", (user["id"],))
            db.executemany("INSERT INTO pins VALUES (?,?)", [(user["id"], did) for did in set(body.pins) | {body.default_id}])
            db.execute("UPDATE users SET default_id=? WHERE id=?", (body.default_id, user["id"]))
    return {"ok": True}


@app.get("/api/directories")
def directories(request: Request):
    current_user(request)
    with database() as db:
        return [dict(r) for r in db.execute("SELECT d.*, u.name AS owner FROM directories d LEFT JOIN users u ON u.id=d.owner_id WHERE d.deleted_at IS NULL ORDER BY d.created")]


@app.post("/api/directories")
async def create_directory(body: NameBody, request: Request):
    user = current_user(request)
    did, name = uuid.uuid4().hex, clean_name(body.name)
    async with mutation_lock:
        user = current_user(request)
        with file_transaction() as (db, undo):
            root = DATA / "files" / did
            root.mkdir()
            undo.append(root.rmdir)
            db.execute("INSERT INTO directories(id,name,owner_id,created) VALUES (?,?,?,?)", (did, name, user["id"], time.time()))
            db.execute("INSERT INTO pins VALUES (?,?)", (user["id"], did))
            log(db, user, did, "create_directory", "")
    return {"id": did}


@app.get("/api/directories/{did}/files")
async def files(did: str, request: Request, path: str = ""):
    current_user(request)
    target = locate(did, path)
    if not target.is_dir():
        raise HTTPException(400, "请选择文件夹")
    items = []
    for entry in target.iterdir():
        if entry.is_symlink() or not (entry.is_file() or entry.is_dir()):
            continue
        try:
            st = entry.stat()
            items.append({"name": entry.name, "path": f"{path}/{entry.name}".lstrip("/"), "is_dir": entry.is_dir(), "size": st.st_size, "modified": st.st_mtime, "version": version(entry)})
        except FileNotFoundError:
            continue
    async with mutation_lock:
        storage = storage_status(storage_owner(did))
    return {"path": path, "items": sorted(items, key=lambda x: (not x["is_dir"], x["name"].casefold())), "storage": storage}


@app.post("/api/directories/{did}/folders")
async def folder(did: str, body: FolderBody, request: Request):
    user = current_user(request)
    value = relative(body.path, False)
    async with mutation_lock:
        user = current_user(request)
        target = locate(did, value, False)
        if not body.parents and not target.parent.is_dir():
            raise HTTPException(404, "上级目录不存在")
        if target.exists():
            if body.exist_ok and target.is_dir():
                return {"ok": True, "created": False}
            raise HTTPException(409, "已有同名文件或文件夹")
        with file_transaction() as (db, undo):
            if body.parents:
                create_parents(target, DATA / "files" / did, undo)
            target.mkdir()
            undo.append(target.rmdir)
            log(db, user, did, "mkdir", value)
    return {"ok": True, "created": True}


@app.get("/api/directories/{did}/download")
def download(did: str, request: Request, path: str):
    current_user(request)
    target = locate(did, relative(path, False))
    if not target.is_file():
        raise HTTPException(400, "请选择文件下载")
    return FileResponse(target, filename=target.name, media_type="application/octet-stream")


@app.put("/api/directories/{did}/upload")
async def upload(did: str, request: Request, path: str, expected_version: str = "", create_directories: bool = False):
    user = current_user(request)
    value = relative(path, False)
    locate(did, value, False)
    temp = DATA / "temporary" / uuid.uuid4().hex
    size = 0
    try:
        async with mutation_lock:
            current_user(request)
            owner_id = storage_owner(did)
            length = request.headers.get("content-length", "0")
            if not length.isdigit():
                raise HTTPException(400, "文件大小无效")
            expected_size = int(length)
            if expected_size > MAX_UPLOAD:
                raise HTTPException(413, "文件超过单文件上传上限")
            reserve_storage(temp.name, owner_id, expected_size)
        with temp.open("xb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, f"单个文件不能超过 {MAX_UPLOAD // 1024 // 1024} MB")
                if size > storage_reservations[temp.name][1]:
                    async with mutation_lock:
                        reserve_storage(temp.name, owner_id, size)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        async with mutation_lock:
            user = current_user(request)
            target = locate(did, value, False)
            if not create_directories and not target.parent.is_dir():
                raise HTTPException(404, "上传目录已不存在")
            old = None
            if target.exists():
                if not target.is_file():
                    raise HTTPException(409, "已有同名文件夹")
                check_version(target, expected_version)
            elif expected_version:
                raise HTTPException(409, "原文件已被移动或删除，请刷新")
            reserve_storage(temp.name, owner_id, size)
            with file_transaction() as (db, undo):
                if create_directories:
                    create_parents(target, DATA / "files" / did, undo)
                if target.exists():
                    old = stash(db, undo, user, did, value, target, "overwrite")
                os.replace(temp, target)
                undo.append(target.unlink)
                log(db, user, did, "overwrite" if old else "upload", value)
        return {"ok": True, "size": size}
    finally:
        temp.unlink(missing_ok=True)
        storage_reservations.pop(temp.name, None)


@app.get("/api/directories/{did}/text")
async def read_text(did: str, request: Request, path: str):
    current_user(request)
    async with mutation_lock:
        target = locate(did, relative(path, False))
        if not target.is_file() or target.stat().st_size > 2 * 1024 * 1024:
            raise HTTPException(400, "在线编辑支持不超过 2 MB 的 UTF-8 文本文件")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise HTTPException(400, "此文件不是 UTF-8 文本，请下载后编辑")
        if "\x00" in content:
            raise HTTPException(400, "此文件不是可编辑的文本")
        return {"content": content, "version": version(target)}


def copy_to_temporary(source, temporary):
    with temporary.open("xb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)
        output.flush()
        os.fsync(output.fileno())


def available_copy_name(root, original):
    if not (root / original).exists() and not (root / original).is_symlink():
        return original
    suffix = Path(original).suffix
    stem = original[:-len(suffix)] if suffix else original
    for index in range(1, 10001):
        tail = f" (副本{index if index > 1 else ''}){suffix}"
        available = 255 - len(tail.encode())
        if available < 1:
            raise HTTPException(400, "文件扩展名过长，无法生成副本名称")
        name = stem.encode()[:available].decode("utf-8", errors="ignore") + tail
        if not (root / name).exists() and not (root / name).is_symlink():
            return name
    raise HTTPException(409, "同名副本过多，请先整理目标目录")


@app.post("/api/directories/{did}/copy")
async def copy_file(did: str, body: CopyBody, request: Request):
    user = current_user(request)
    value = relative(body.path, False)
    temporary = DATA / "temporary" / uuid.uuid4().hex
    try:
        async with mutation_lock:
            user = current_user(request)
            source = locate(did, value)
            if not source.is_file():
                raise HTTPException(400, "快速复制目前支持单个文件")
            check_version(source, body.version)
            with database() as db:
                destination = db.execute("SELECT * FROM directories WHERE owner_id=? AND deleted_at IS NULL ORDER BY (id=?) DESC, created, id LIMIT 1", (user["id"], user["default_id"])).fetchone()
            if not destination:
                raise HTTPException(409, "你还没有自己的可用目录，请先新建共享目录")
            reserve_storage(temporary.name, user["id"], source.stat().st_size)
            source_stream = source.open("rb")
        with source_stream:
            copying = asyncio.create_task(asyncio.to_thread(copy_to_temporary, source_stream, temporary))
            try:
                await asyncio.shield(copying)
            except asyncio.CancelledError:
                await copying
                raise
        async with mutation_lock:
            user = current_user(request)
            check_version(locate(did, value), body.version)
            root = locate(destination["id"])
            name = available_copy_name(root, source.name)
            target = locate(destination["id"], name, False)
            reserve_storage(temporary.name, user["id"], temporary.stat().st_size)
            with file_transaction() as (db, undo):
                os.replace(temporary, target)
                undo.append(target.unlink)
                log(db, user, destination["id"], "copy", name)
        return {"ok": True, "directory_id": destination["id"], "directory_name": destination["name"], "path": name}
    finally:
        temporary.unlink(missing_ok=True)
        storage_reservations.pop(temporary.name, None)


@app.post("/api/directories/{did}/rename")
async def rename(did: str, body: ChangeBody, request: Request):
    user = current_user(request)
    value, dest = relative(body.path, False), relative(body.destination or "", False)
    async with mutation_lock:
        user = current_user(request)
        target, destination = locate(did, value), locate(did, dest, False)
        check_version(target, body.version)
        if destination.exists() or destination.is_relative_to(target):
            raise HTTPException(409, "目标名称已存在或位置无效")
        if not destination.parent.is_dir():
            raise HTTPException(404, "目标目录不存在")
        with file_transaction() as (db, undo):
            os.replace(target, destination)
            undo.append(lambda: os.replace(destination, target))
            log(db, user, did, "rename", f"{value} → {dest}")
    return {"ok": True}


@app.post("/api/directories/{did}/delete")
async def delete(did: str, body: ChangeBody, request: Request):
    user = current_user(request)
    value = relative(body.path, False)
    async with mutation_lock:
        user = current_user(request)
        target = locate(did, value)
        check_version(target, body.version)
        with file_transaction() as (db, undo):
            stash(db, undo, user, did, value, target, "delete")
            log(db, user, did, "delete", value)
    return {"ok": True}


@app.get("/api/trash")
def trash(request: Request):
    current_user(request)
    with database() as db:
        return [dict(r) for r in db.execute("SELECT t.*, u.name AS actor, d.name AS directory FROM trash t JOIN users u ON u.id=t.actor_id JOIN directories d ON d.id=t.directory_id WHERE d.deleted_at IS NULL ORDER BY t.created DESC")]


@app.post("/api/trash/{tid}/restore")
async def restore(tid: str, body: PathBody, request: Request):
    user = current_user(request)
    async with mutation_lock:
        user = current_user(request)
        with file_transaction() as (db, undo):
            item = db.execute("SELECT * FROM trash WHERE id=?", (tid,)).fetchone()
            if not item:
                raise HTTPException(404, "此记录已恢复")
            value = relative(body.path or item["path"], False)
            target = locate(item["directory_id"], value, False)
            if target.exists():
                raise HTTPException(409, "已有同名文件，请指定新的恢复名称")
            if not target.parent.is_dir():
                raise HTTPException(409, "原上级目录已不存在，请先重建，或选择新的恢复路径")
            source = DATA / "trash" / item["id"]
            if not source.exists():
                raise HTTPException(404, "回收文件不存在")
            os.replace(source, target)
            undo.append(lambda: os.replace(target, source))
            db.execute("DELETE FROM trash WHERE id=?", (tid,))
            log(db, user, item["directory_id"], "restore", value)
    return {"ok": True}


@app.get("/api/activity")
def activity(request: Request):
    current_user(request)
    with database() as db:
        return [dict(r) for r in db.execute("SELECT a.*, u.name AS actor, d.name AS directory FROM activity a JOIN users u ON u.id=a.actor_id JOIN directories d ON d.id=a.directory_id WHERE d.deleted_at IS NULL ORDER BY a.id DESC LIMIT 200")]


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


from .admin_routes import router as admin_router

app.include_router(admin_router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
