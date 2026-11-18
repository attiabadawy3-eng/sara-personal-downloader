"""FastAPI app. Run via `python bootstrap.py`."""
from __future__ import annotations

import hmac
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .core import ALLOWED_UPLOADS, MAX_BYTES, MAX_MB, JobManager, ROOT, safe_name, validate_public_url

APP_PASSWORD = os.environ.get("SARA_PASSWORD", "")
APP_SECRET = os.environ.get("SARA_SECRET", "")
if not APP_PASSWORD or len(APP_SECRET) < 32:
    raise RuntimeError("الإعدادات غير موجودة: شغّل python bootstrap.py أولاً")

manager = JobManager()
app = FastAPI(title="SARA Downloader", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=APP_SECRET, session_cookie="sara_session",
                   max_age=7*24*3600, same_site="lax",
                   https_only=os.environ.get("SARA_HTTPS", "0") == "1")
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")
login_lock = threading.Lock()
login_failures: dict[str, tuple[int, float]] = {}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update({
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY", "Cache-Control": "no-store",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    })
    return response


def require_auth(request: Request):
    if request.session.get("auth") is not True:
        raise HTTPException(status_code=401, detail="يرجى تسجيل الدخول")


def require_write(request: Request, x_sara_request: str | None):
    require_auth(request)
    if x_sara_request != "1":
        raise HTTPException(status_code=403, detail="طلب غير مسموح")
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        # A custom header blocks browser-origin CSRF; also compare origin and Host.
        if parsed.netloc.lower() != request.headers.get("host", "").lower() or parsed.scheme not in {"http", "https"}:
            raise HTTPException(status_code=403, detail="مصدر الطلب غير مسموح")


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not request.session.get("auth"):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse((ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8"))


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("auth"):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse((ROOT / "app" / "static" / "login.html").read_text(encoding="utf-8"))


@app.post("/login")
async def login(request: Request, password: str = Form(default="")):
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with login_lock:
        tries, blocked_until = login_failures.get(ip, (0, 0.0))
        if now < blocked_until:
            return RedirectResponse("/login?error=locked", status_code=303)
    if hmac.compare_digest(password.encode(), APP_PASSWORD.encode()):
        request.session.clear()
        request.session["auth"] = True
        with login_lock:
            login_failures.pop(ip, None)
        return RedirectResponse("/", status_code=303)
    with login_lock:
        tries += 1
        login_failures[ip] = (tries, now + 15*60 if tries >= 6 else 0)
    return RedirectResponse("/login?error=bad", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


class LinkRequest(BaseModel):
    url: str = Field(min_length=7, max_length=2048)
    mode: str = "best"


@app.post("/api/jobs")
def create_link_job(data: LinkRequest, request: Request, x_sara_request: str | None = Header(default=None)):
    require_write(request, x_sara_request)
    if data.mode not in {"best", "1080", "720", "480", "audio"}:
        raise HTTPException(400, "صيغة تحميل غير صحيحة")
    try:
        url = validate_public_url(data.url)
        job = manager.create("link", url, data.mode)
        return job.json()
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err


@app.post("/api/upload")
async def upload_file(request: Request, file: UploadFile = File(...),
                      mode: str = Form(default="original"),
                      x_sara_request: str | None = Header(default=None)):
    require_write(request, x_sara_request)
    if mode not in {"original", "audio"}:
        raise HTTPException(400, "صيغة غير مسموحة")
    name = safe_name(file.filename or "media")
    extension = Path(name).suffix.lower()
    if extension not in ALLOWED_UPLOADS:
        raise HTTPException(400, "امتداد الملف غير مدعوم؛ اختر ملف صورة أو صوت أو فيديو")
    if mode == "audio" and extension in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}:
        raise HTTPException(400, "لا يمكن استخراج صوت من صورة")
    try:
        job = manager.create("upload", name, mode, submit=False)
    except ValueError as err:
        raise HTTPException(429, str(err)) from err
    target = manager.data_dir / job.id / f"input{extension}"
    count = 0
    try:
        with target.open("wb") as stream:
            while chunk := await file.read(1024 * 1024):
                count += len(chunk)
                if count > MAX_BYTES:
                    raise ValueError(f"الملف أكبر من حد {MAX_MB} MB")
                stream.write(chunk)
        if count == 0:
            raise ValueError("الملف فارغ")
        manager.pool.submit(manager.process, job.id)
        return job.json()
    except Exception as exc:
        manager.change(job.id, state="failed", error="تعذّر رفع الملف", message="تعذر رفع الملف")
        target.unlink(missing_ok=True)
        if isinstance(exc, ValueError):
            raise HTTPException(413, str(exc)) from exc
        raise HTTPException(500, "حدث خطأ أثناء رفع الملف") from exc
    finally:
        await file.close()


@app.get("/api/jobs")
def list_jobs(request: Request):
    require_auth(request)
    manager.cleanup()
    return {"jobs": manager.list(), "max_mb": MAX_MB}


@app.get("/api/jobs/{jid}")
def job_status(jid: str, request: Request):
    require_auth(request)
    job = manager.get(jid)
    if job is None:
        raise HTTPException(404, "العملية غير موجودة أو انتهت صلاحيتها")
    return job.json()


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str, request: Request, x_sara_request: str | None = Header(default=None)):
    require_write(request, x_sara_request)
    if not manager.cancel(jid):
        raise HTTPException(409, "العملية انتهت أو غير موجودة")
    return {"ok": True}


@app.get("/api/jobs/{jid}/file")
def fetch_file(jid: str, request: Request):
    require_auth(request)
    job = manager.get(jid)
    if job is None or job.state != "done" or not job.file_path:
        raise HTTPException(404, "الملف غير جاهز أو انتهت صلاحيته")
    path = Path(job.file_path).resolve()
    expected = (manager.data_dir / job.id).resolve()
    if not path.is_relative_to(expected) or not path.is_file():
        raise HTTPException(404, "الملف غير موجود")
    return FileResponse(path, media_type="application/octet-stream", filename=job.filename, content_disposition_type="attachment")


@app.get("/health")
def health():
    # Does not reveal credentials or other internal server data.
    return JSONResponse({"status": "ok"})
