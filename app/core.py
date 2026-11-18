"""Configuration, task state, URL validation and media processing."""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

LOG = logging.getLogger("sara.downloader")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("SARA_DATA_DIR", str(ROOT / "downloads"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
MAX_MB = max(10, min(4096, int(os.environ.get("SARA_MAX_MB", "750"))))
MAX_BYTES = MAX_MB * 1024 * 1024
TTL_SECONDS = 2 * 3600
MAX_PENDING = 3
MAX_CONCURRENT = 1
ALLOWED_UPLOADS = {
    ".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".3gp",
    ".mp3", ".m4a", ".wav", ".opus", ".ogg", ".aac", ".flac",
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic",
}


def safe_name(original: str) -> str:
    s = unicodedata.normalize("NFKC", Path(original.replace("\\", "/")).name)
    s = re.sub(r"[^\w\-. ()\u0600-\u06FF]", "_", s, flags=re.UNICODE)
    s = s.strip(" .")[:110]
    return s if s and s not in {".", ".."} else "media"


def validate_public_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("الرابط غير صالح أو أطول من اللازم")
    url = url.strip()
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("صيغة الرابط غير صحيحة") from exc
    if parsed.scheme not in {"https", "http"} or not host or parsed.username or parsed.password:
        raise ValueError("استخدم رابط فيديو عامًا يبدأ بـ https:// أو http://")
    if port not in {None, 80, 443} or "\\" in url or any(ord(c) < 32 for c in url):
        raise ValueError("عنوان الرابط أو منفذه غير مسموح")
    hostname = host.lower().rstrip(".")
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith((".local", ".internal", ".localhost", ".test", ".lan")):
        raise ValueError("الروابط الداخلية غير مسموحة")
    allowed_domains = (
        "youtube.com", "youtu.be", "youtube-nocookie.com", "tiktok.com",
        "instagram.com", "facebook.com", "fb.watch", "x.com", "twitter.com",
        "vimeo.com", "dailymotion.com", "pinterest.com", "pin.it",
        "threads.net", "reddit.com", "redd.it", "twitch.tv", "soundcloud.com",
        "streamable.com", "bilibili.com", "snapchat.com", "likee.video"
    )
    if not any(hostname == d or hostname.endswith("." + d) for d in allowed_domains):
        raise ValueError("في نسخة الإنترنت نقبل الروابط من منصات الفيديو المعروفة فقط لحماية الخادم")
    try:
        rawip = ipaddress.ip_address(hostname.strip("[]"))
        if not rawip.is_global:
            raise ValueError("عناوين الشبكات الداخلية غير مسموحة")
    except ValueError as exc:
        if "غير مسموحة" in str(exc):
            raise
        # Resolve DNS before invoking yt-dlp: this validates the initial host.
        # Server operators must still avoid exposing this service to untrusted users.
        try:
            infos = socket.getaddrinfo(hostname, port or 443, type=socket.SOCK_STREAM)
        except OSError as err:
            raise ValueError("لا يمكن الوصول إلى اسم الموقع؛ تحقق من الرابط والإنترنت") from err
        if not infos:
            raise ValueError("تعذر تحديد عنوان الموقع")
        if any(not ipaddress.ip_address(item[4][0]).is_global for item in infos):
            raise ValueError("هذا الرابط يشير إلى عنوان شبكة داخلية")
    return url


@dataclass
class Job:
    id: str
    kind: str
    source: str
    mode: str
    created: float = field(default_factory=time.time)
    state: str = "queued"
    progress: int = 0
    message: str = "في الانتظار"
    filename: str | None = None
    file_path: str | None = None
    error: str | None = None
    cancel_requested: bool = False
    process: subprocess.Popen | None = field(default=None, repr=False)

    def json(self) -> dict:
        return {"id": self.id, "kind": self.kind, "source": self.source,
                "mode": self.mode, "state": self.state, "progress": self.progress,
                "message": self.message, "filename": self.filename,
                "error": self.error, "created": self.created}


class JobManager:
    def __init__(self, data_dir: Path = DATA_DIR):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs: dict[str, Job] = {}
        self.pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT, thread_name_prefix="sara-media")
        self.last_cleanup = 0.0

    def cleanup(self, force: bool = False):
        now = time.time()
        with self.lock:
            if not force and now - self.last_cleanup < 60:
                return
            self.last_cleanup = now
            remove = [jid for jid, job in self.jobs.items()
                      if job.state in {"done", "failed", "cancelled"}
                      and now - job.created > TTL_SECONDS]
            for jid in remove:
                self.jobs.pop(jid, None)
        for jid in remove:
            shutil.rmtree(self.data_dir / jid, ignore_errors=True)
        # Clean directories left behind by previous server sessions.
        for d in self.data_dir.iterdir():
            if not d.is_dir() or not re.fullmatch(r"[0-9a-f]{32}", d.name):
                continue
            with self.lock:
                active = d.name in self.jobs
            if not active and now - d.stat().st_mtime > TTL_SECONDS:
                shutil.rmtree(d, ignore_errors=True)

    def create(self, kind: str, source: str, mode: str, *, submit: bool = True) -> Job:
        self.cleanup()
        with self.lock:
            active_count = sum(j.state in {"queued", "running"} for j in self.jobs.values())
            if active_count >= MAX_PENDING:
                raise ValueError("هناك تحميلات كثيرة قيد التنفيذ؛ جرّب بعد انتهاء أحدها")
            jid = uuid.uuid4().hex
            job = Job(id=jid, kind=kind, source=source, mode=mode)
            self.jobs[jid] = job
            (self.data_dir / jid).mkdir(parents=True, exist_ok=False)
        if submit:
            self.pool.submit(self.process, jid)
        return job

    def get(self, jid: str) -> Job | None:
        with self.lock:
            return self.jobs.get(jid)

    def list(self) -> list[dict]:
        with self.lock:
            return [job.json() for job in sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)[:30]]

    def change(self, jid: str, **updates):
        with self.lock:
            job = self.jobs[jid]
            for k, v in updates.items():
                setattr(job, k, v)

    def cancel(self, jid: str) -> bool:
        with self.lock:
            job = self.jobs.get(jid)
            if not job or job.state not in {"queued", "running"}:
                return False
            job.cancel_requested = True
            job.message = "جاري الإلغاء"
            proc = job.process
        if proc is not None and proc.poll() is None:
            proc.terminate()
        return True

    def process(self, jid: str):
        with self.lock:
            job = self.jobs[jid]
            if job.cancel_requested:
                job.state = "cancelled"
                job.message = "تم إلغاء التحميل"
                return
            job.state = "running"
            job.message = "جاري التحضير"
        try:
            if job.kind == "link":
                self._download_link(job)
            elif job.kind == "upload":
                self._process_upload(job)
            else:
                raise ValueError("نوع عملية غير معروف")
            with self.lock:
                if job.cancel_requested:
                    job.state = "cancelled"
                    job.message = "تم الإلغاء"
                else:
                    job.state = "done"
                    job.progress = 100
                    job.message = "الملف جاهز للتنزيل"
        except Exception as exc:
            with self.lock:
                job.error = self._safe_error(str(exc))
                job.state = "cancelled" if job.cancel_requested else "failed"
                job.message = "تم الإلغاء" if job.cancel_requested else "تعذر تنزيل الملف"
        finally:
            with self.lock:
                job.process = None
                failed = job.state in {"failed", "cancelled"}
            if failed:
                # Avoid keeping failed or cancelled partial files.
                shutil.rmtree(self.data_dir / jid, ignore_errors=True)

    @staticmethod
    def _safe_error(message: str) -> str:
        """User-safe diagnostics; never return an unredacted external URL or subprocess logs."""
        msg = message.lower()
        if any(p in msg for p in ("not a bot", "captcha", "robot check", "automated queries")):
            return "الموقع طلب التحقق من أن الطلب ليس روبوتًا. قد يكون عنوان خادم Render محظورًا؛ جرّب التحميل من جهازك المحلي."
        if any(p in msg for p in ("http error 403", "403 forbidden", "access denied", "forbidden (403)")):
            return "الموقع رفض الاتصال من خادم الاستضافة (HTTP 403). لا يمكن ضمان تحميل هذا الرابط من Render."
        if any(p in msg for p in ("http error 429", "429 too many requests", "too many requests", "rate limit exceeded")):
            return "الموقع أوقف الطلبات مؤقتًا (HTTP 429). لا تعيد المحاولة باستمرار؛ انتظر ثم حاول لاحقًا."
        if "no supported javascript runtime" in msg or "js challenge" in msg or "challenge solving failed" in msg:
            return "تعذر حل متطلبات JavaScript الخاصة بالموقع؛ يحتاج محرك التنزيل إلى تحديث أو Runtime مناسب."
        if "ffmpeg" in msg:
            return "تعذر تحويل الملف بواسطة FFmpeg؛ قد تكون الصيغة غير مدعومة أو لا تحتوي على صوت."
        if "unsupported url" in msg or "no suitable" in msg:
            return "الرابط أو الموقع غير مدعوم حاليًا."
        if "sign in" in msg or "login" in msg or "private video" in msg or "authentication" in msg:
            return "المحتوى غير عام أو يحتاج تسجيل دخول؛ التطبيق لا يستطيع تنزيله بدون تصريح من الموقع."
        if "requested format" in msg:
            return "الجودة المطلوبة غير متاحة؛ اختر أفضل جودة تلقائيًا."
        if "video unavailable" in msg or "not available" in msg or "removed" in msg:
            return "الفيديو غير متاح أو تمت إزالته."
        if "larger than max-filesize" in msg or "limit exceeded" in msg or "file is larger" in msg:
            return f"الفيديو أكبر من الحد المسموح ({MAX_MB} MB) على الاستضافة المجانية."
        if any(p in msg for p in ("timed out", "name resolution", "temporary failure", "unable to download webpage")):
            return "تعذر الاتصال بموقع الفيديو من الخادم؛ قد يكون عطل شبكة أو حظرًا مؤقتًا."
        if "not found" in msg and "yt_dlp" in msg:
            return "محرك yt-dlp غير مثبت بشكل صحيح على الخادم."
        return "فشل محرك التحميل. الرابط قد يكون محميًا أو الموقع يمنع الاتصال من الاستضافة؛ جرّب رابطًا عامًا آخر."

    def _run(self, job: Job, cmd: list[str], *, progress: bool = False):
        popen_args = dict(cwd=str(self.data_dir / job.id), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                          bufsize=1, stdin=subprocess.DEVNULL)
        proc = subprocess.Popen(cmd, **popen_args)
        with self.lock:
            job.process = proc
            cancelled = job.cancel_requested
        if cancelled:
            proc.terminate()
        lines = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if line:
                    lines.append(line)
                    if len(lines) > 20:
                        lines.pop(0)
                if progress:
                    percent_match = re.search(r"(?:\[download\]\s*)?(\d{1,3}(?:\.\d+)?)\s*%", line)
                    if percent_match:
                        p = max(0, min(99, int(float(percent_match.group(1)))))
                        self.change(job.id, progress=p, message=f"تحميل الملف: {p}%")
                    elif "[Merger]" in line or "[ExtractAudio]" in line or "[VideoConvertor]" in line:
                        self.change(job.id, message="جاري تجهيز الصيغة النهائية")
            proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
            raise RuntimeError("تعذّر تشغيل أداة المعالجة")
        if job.cancel_requested:
            raise RuntimeError("cancelled")
        if proc.returncode != 0:
            detail = " ".join(lines[-12:])[-3500:]
            # Log only classified errors; never expose user-provided URLs, cookies or tokens.
            LOG.warning("media_job_failed id=%s kind=%s returncode=%d category=%s",
                        job.id[:12], job.kind, proc.returncode, self._safe_error(detail))
            raise RuntimeError(detail)

    def _choose_output(self, job: Job) -> Path:
        dest = self.data_dir / job.id
        files = [p for p in dest.iterdir() if p.is_file() and p.stat().st_size > 0
                 and not p.name.endswith((".part", ".ytdl", ".temp")) and not p.name.startswith("input.")]
        if not files:
            raise RuntimeError("لا يوجد ملف جاهز")
        selected = max(files, key=lambda p: p.stat().st_mtime)
        if selected.stat().st_size > MAX_BYTES:
            raise RuntimeError("limit exceeded")
        return selected

    def _download_link(self, job: Job):
        dest = self.data_dir / job.id
        cmd = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist",
               "--newline", "--no-mtime", "--retries", "2", "--fragment-retries", "2",
               "--socket-timeout", "20", "--force-ipv4", "--js-runtimes", "node", "--max-filesize", f"{MAX_MB}M",
               "--restrict-filenames", "--trim-filenames", "100", "--no-overwrites",
               "--output", "%(title).80B-%(id)s.%(ext)s"]
        if job.mode == "audio":
            cmd += ["--format", "bestaudio/best", "--extract-audio", "--audio-format", "mp3", "--audio-quality", "2"]
        else:
            quality = job.mode if job.mode in {"1080", "720", "480"} else None
            q_filter = f"[height<={quality}]" if quality else ""
            cmd += ["--format", f"bv*{q_filter}+ba/b{q_filter}/best", "--merge-output-format", "mp4",
                    "--format-sort", "res,ext:mp4:m4a,vcodec:h264"]
        cmd.extend(["--", job.source])
        self.change(job.id, message="جاري الاتصال بالموقع")
        self._run(job, cmd, progress=True)
        chosen = self._choose_output(job)
        self.change(job.id, filename=safe_name(chosen.name), file_path=str(chosen))

    def _process_upload(self, job: Job):
        dest = self.data_dir / job.id
        srcs = list(dest.glob("input.*"))
        if len(srcs) != 1:
            raise RuntimeError("الملف غير موجود")
        src = srcs[0]
        if job.mode == "audio":
            output = dest / (safe_name(Path(job.source).stem) + ".mp3")
            self.change(job.id, message="جاري استخراج الصوت")
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(src),
                   "-vn", "-codec:a", "libmp3lame", "-qscale:a", "3", str(output)]
            self._run(job, cmd)
            if not output.exists() or output.stat().st_size == 0:
                raise RuntimeError("ffmpeg missing audio")
            src.unlink(missing_ok=True)
            selected = output
        else:
            selected = dest / safe_name(job.source)
            if selected == src:
                selected = dest / ("media" + src.suffix)
            src.rename(selected)
        if selected.stat().st_size > MAX_BYTES:
            raise RuntimeError("limit exceeded")
        self.change(job.id, file_path=str(selected), filename=safe_name(selected.name), progress=99)
