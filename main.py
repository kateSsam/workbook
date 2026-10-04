import asyncio
import glob
import hashlib
import hmac
import io
import json
import mimetypes
import os
import shutil
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

import db
import lessons

APP_DIR = os.path.dirname(__file__)
UPLOADS_DIR = os.path.join(APP_DIR, "data", "uploads")
AUDIO_DIR = os.path.join(APP_DIR, "data", "lesson_audio")
SEED_AUDIO_DIR = os.path.join(APP_DIR, "seed_audio")
os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)

# 선생님 관리자 페이지 비밀번호. 배포할 때 반드시 환경변수로 바꿔주세요.
ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme")

app = FastAPI(title="발음/스피치 훈련 워크북")
templates = Jinja2Templates(directory=os.path.join(APP_DIR, "templates"))

db.init_db()


def seed_lesson_audio():
    """처음 실행될 때, seed_audio/ 안의 파일을 data/lesson_audio/ 로 복사해둔다.
    (data/ 는 배포 환경에서 초기화될 수 있는 저장소라, 깃에 포함되는 seed_audio/ 에서 복원한다.)"""
    if not os.path.isdir(SEED_AUDIO_DIR):
        return
    for path in glob.glob(os.path.join(SEED_AUDIO_DIR, "*")):
        fname = os.path.basename(path)
        dest = os.path.join(AUDIO_DIR, fname)
        if not os.path.exists(dest):
            shutil.copyfile(path, dest)


seed_lesson_audio()


def find_lesson_audio(lesson_id: str, stage: str = "training") -> Optional[str]:
    matches = glob.glob(os.path.join(AUDIO_DIR, f"{lesson_id}_{stage}.*"))
    if matches:
        return matches[0]
    if stage == "training":
        # 이전 버전과의 호환: stage 구분 없이 올렸던 훈련 음원 (예: lesson1.mp3)
        legacy = glob.glob(os.path.join(AUDIO_DIR, f"{lesson_id}.*"))
        if legacy:
            return legacy[0]
    return None


STAGES_WITH_AUDIO = ["preview", "review"]


# ---- 관리자 로그인 (주소에 열쇠가 남지 않도록 브라우저 쿠키로 로그인 유지) ----
SESSION_COOKIE = "kate_admin"
SESSION_DAYS = 14


def _sign(ts: str) -> str:
    return hmac.new(ADMIN_KEY.encode(), f"admin:{ts}".encode(), hashlib.sha256).hexdigest()


def make_session() -> str:
    ts = str(int(time.time()))
    return f"{ts}.{_sign(ts)}"


def is_admin(request: Request) -> bool:
    raw = request.cookies.get(SESSION_COOKIE, "")
    if "." not in raw:
        return False
    ts, sig = raw.split(".", 1)
    if not ts.isdigit() or not hmac.compare_digest(sig, _sign(ts)):
        return False
    return time.time() - int(ts) < SESSION_DAYS * 86400


def check_admin(request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="관리자 로그인이 필요해요. /admin 에서 다시 로그인해 주세요.")


def back_to_admin(path: str = "/admin"):
    return RedirectResponse(url=path, status_code=303)


LOGIN_PAGE = """<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex, nofollow">
<title>관리자 로그인</title></head>
<body style="font-family:sans-serif;padding:40px;background:#FBF8F3;color:#24211D">
<h3>Kate English 관리자 센터</h3>
{msg}
<form method="post" action="/admin/login" style="display:flex;gap:8px;flex-wrap:wrap">
<input name="key" type="password" placeholder="관리자 키" autocomplete="current-password" required
 style="padding:10px;border:1px solid #ccc;border-radius:8px;min-width:220px"/>
<button type="submit" style="padding:10px 16px;border-radius:8px;border:0;background:#1C5B57;color:#fff;font-weight:700">입장</button>
</form></body></html>"""


# ---------------------------------------------------------------- student ---

@app.api_route("/ping", methods=["GET", "HEAD"])
def ping():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home():
    return (
        "<body style='font-family:sans-serif;padding:40px;line-height:1.7'>"
        "<h2>발음/스피치 훈련 워크북 서버</h2>"
        "<p>학생용 링크는 선생님이 관리자 페이지에서 발급합니다.</p>"
        "<p><a href='/admin'>관리자 페이지로 이동</a></p>"
        "</body>"
    )


@app.get("/w/{token}", response_class=HTMLResponse)
def workbook(request: Request, token: str, lesson: str = lessons.DEFAULT_LESSON_ID):
    student = db.get_student_by_token(token)
    if not student:
        raise HTTPException(status_code=404, detail="유효하지 않은 링크입니다. 선생님께 링크를 다시 받아주세요.")
    base_lesson = lessons.LESSONS.get(lesson)
    if not base_lesson:
        raise HTTPException(status_code=404, detail="존재하지 않는 차시입니다.")

    lesson_data = dict(base_lesson)
    lesson_data["stage_audio"] = {
        stage: (f"/lesson-audio/{lesson}?stage={stage}" if find_lesson_audio(lesson, stage) else None)
        for stage in STAGES_WITH_AUDIO
    }
    lesson_data["preview_answer"] = db.get_script(lesson, "preview")
    lesson_data["review_answer"] = db.get_script(lesson, "review")

    state = db.get_progress(student["id"], lesson)
    feedback_text = db.get_feedback(student["id"], lesson)

    return templates.TemplateResponse(
        "workbook.html",
        {
            "request": request,
            "token": token,
            "student_name": student["name"],
            "lesson_id": lesson,
            "lesson": lesson_data,
            "initial_state": state,
            "teacher_feedback": feedback_text,
        },
    )


@app.get("/api/{token}/state")
def api_get_state(token: str, lesson: str = lessons.DEFAULT_LESSON_ID):
    student = db.get_student_by_token(token)
    if not student:
        raise HTTPException(status_code=404, detail="not found")
    return db.get_progress(student["id"], lesson)


@app.post("/api/{token}/state")
async def api_save_state(request: Request, token: str, lesson: str = lessons.DEFAULT_LESSON_ID):
    student = db.get_student_by_token(token)
    if not student:
        raise HTTPException(status_code=404, detail="not found")
    patch = await request.json()
    new_state = db.save_progress(student["id"], lesson, patch)
    return new_state


@app.get("/lesson-audio/{lesson_id}")
def get_lesson_audio(lesson_id: str, stage: str = "training"):
    path = find_lesson_audio(lesson_id, stage)
    if not path:
        raise HTTPException(status_code=404, detail="아직 등록된 음원이 없어요.")
    media_type, _ = mimetypes.guess_type(path)
    return FileResponse(path, media_type=media_type or "audio/mpeg")


@app.post("/api/{token}/upload")
async def api_upload_recording(
    token: str, lesson: str = lessons.DEFAULT_LESSON_ID, file: UploadFile = File(...)
):
    student = db.get_student_by_token(token)
    if not student:
        raise HTTPException(status_code=404, detail="not found")

    ext = ".webm"
    if file.filename and "." in file.filename:
        ext = "." + file.filename.rsplit(".", 1)[-1]

    safe_name = f"{token}_{lesson}_{uuid.uuid4().hex[:8]}{ext}"
    dest_path = os.path.join(UPLOADS_DIR, safe_name)
    content = await file.read()
    with open(dest_path, "wb") as f:
        f.write(content)

    db.add_recording(student["id"], lesson, safe_name)
    return {"ok": True, "filename": safe_name}


# ------------------------------------------------------------------ admin ---

@app.post("/admin/login")
async def admin_login(key: str = Form(...)):
    if not hmac.compare_digest(key.encode(), ADMIN_KEY.encode()):
        await asyncio.sleep(1.5)  # 마구잡이로 비밀번호를 넣어 보는 걸 느리게 만들어요
        return HTMLResponse(LOGIN_PAGE.format(msg="<p style='color:#D9613F'>관리자 키가 맞지 않아요. 다시 입력해 주세요.</p>"), status_code=401)
    resp = back_to_admin()
    resp.set_cookie(SESSION_COOKIE, make_session(), max_age=SESSION_DAYS * 86400,
                    httponly=True, secure=True, samesite="lax", path="/")
    return resp


@app.get("/admin/logout")
def admin_logout():
    resp = back_to_admin()
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, key: Optional[str] = None):
    # 예전 방식(주소에 ?key=...)으로 들어와도, 쿠키로 바꿔 주고 깨끗한 주소로 다시 보내요
    if key is not None:
        if hmac.compare_digest(key.encode(), ADMIN_KEY.encode()):
            resp = back_to_admin()
            resp.set_cookie(SESSION_COOKIE, make_session(), max_age=SESSION_DAYS * 86400,
                            httponly=True, secure=True, samesite="lax", path="/")
            return resp
        return back_to_admin()
    if not is_admin(request):
        return HTMLResponse(LOGIN_PAGE.format(msg=""))

    students = db.list_students()
    students_view = []
    for s in students:
        progress = db.all_progress_for_student(s["id"])
        recordings = db.list_recordings(s["id"])
        feedback = {
            lid: db.get_feedback(s["id"], lid) for lid in lessons.LESSONS.keys()
        }
        students_view.append(
            {
                "row": s,
                "progress": progress,
                "recordings": recordings,
                "feedback": feedback,
                "link": f"/w/{s['token']}",
            }
        )

    def display_audio_name(lid, stage):
        path = find_lesson_audio(lid, stage)
        if not path:
            return None
        original = db.get_audio_original_name(lid, stage)
        return original if original else os.path.basename(path)

    lesson_audio_info = {
        lid: {stage: display_audio_name(lid, stage) for stage in STAGES_WITH_AUDIO}
        for lid in lessons.LESSONS.keys()
    }
    lesson_answers = {
        lid: {
            "preview": db.get_script(lid, "preview"),
            "review": db.get_script(lid, "review"),
        }
        for lid in lessons.LESSONS.keys()
    }

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "active_tab": "students",
            "students": students_view,
            "lessons": lessons.LESSONS,
            "lesson_audio_info": lesson_audio_info,
            "lesson_answers": lesson_answers,
            "stages_with_audio": STAGES_WITH_AUDIO,
        },
    )


@app.post("/admin/lesson-audio")
async def admin_upload_lesson_audio(
    request: Request,
    lesson_id: str = Form(...),
    stage: str = Form(...),
    file: UploadFile = File(...),
):
    check_admin(request)
    if lesson_id not in lessons.LESSONS:
        raise HTTPException(status_code=404, detail="존재하지 않는 차시입니다.")
    if stage not in STAGES_WITH_AUDIO:
        raise HTTPException(status_code=400, detail="알 수 없는 단계입니다.")

    # 기존 파일(확장자 다를 수 있음, 이전 버전 파일명 포함) 제거 후 새로 저장
    for old in glob.glob(os.path.join(AUDIO_DIR, f"{lesson_id}_{stage}.*")):
        os.remove(old)
    if stage == "training":
        for old in glob.glob(os.path.join(AUDIO_DIR, f"{lesson_id}.*")):
            os.remove(old)

    ext = ".mp3"
    if file.filename and "." in file.filename:
        ext = "." + file.filename.rsplit(".", 1)[-1]
    dest_path = os.path.join(AUDIO_DIR, f"{lesson_id}_{stage}{ext}")
    content = await file.read()
    with open(dest_path, "wb") as f:
        f.write(content)

    db.set_audio_original_name(lesson_id, stage, file.filename or f"{lesson_id}_{stage}{ext}")

    return back_to_admin()


@app.post("/admin/answer-script")
def admin_save_answer_script(
    request: Request,
    lesson_id: str = Form(...),
    stage: str = Form(...),
    text: str = Form(""),
):
    check_admin(request)
    if lesson_id not in lessons.LESSONS:
        raise HTTPException(status_code=404, detail="존재하지 않는 차시입니다.")
    if stage not in ("preview", "review"):
        raise HTTPException(status_code=400, detail="알 수 없는 단계입니다.")
    db.set_script(lesson_id, stage, text)
    return back_to_admin()


@app.post("/admin/students")
def admin_add_student(request: Request, name: str = Form(...)):
    check_admin(request)
    if name.strip():
        db.create_student(name.strip())
    return back_to_admin()


@app.post("/admin/students/{student_id}/delete")
def admin_delete_student(request: Request, student_id: int):
    check_admin(request)
    db.delete_student(student_id)
    return back_to_admin()


@app.post("/admin/feedback")
def admin_save_feedback(
    request: Request,
    student_id: int = Form(...),
    lesson_id: str = Form(...),
    text: str = Form(""),
):
    check_admin(request)
    db.set_feedback(student_id, lesson_id, text)
    return back_to_admin()


@app.get("/admin/recording/{filename}")
def admin_get_recording(request: Request, filename: str):
    check_admin(request)
    path = os.path.join(UPLOADS_DIR, os.path.basename(filename))
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.")
    return FileResponse(path)


@app.get("/admin/backup")
def admin_backup(request: Request):
    check_admin(request)
    data = db.export_all()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("backup.json", json.dumps(data, ensure_ascii=False, indent=2))
        for rec in data["recordings"]:
            src = os.path.join(UPLOADS_DIR, rec["filename"])
            if os.path.isfile(src):
                zf.write(src, arcname=f"recordings/{rec['filename']}")
        for path in glob.glob(os.path.join(AUDIO_DIR, "*")):
            zf.write(path, arcname=f"audio/{os.path.basename(path)}")
    buf.seek(0)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    headers = {"Content-Disposition": f'attachment; filename="workbook_backup_{stamp}.zip"'}
    return StreamingResponse(buf, media_type="application/zip", headers=headers)


@app.post("/admin/restore")
async def admin_restore(request: Request, file: UploadFile = File(...)):
    check_admin(request)
    content = await file.read()
    buf = io.BytesIO(content)

    try:
        with zipfile.ZipFile(buf) as zf:
            with zf.open("backup.json") as f:
                data = json.load(f)

            for s in data.get("students", []):
                db.restore_student(s["token"], s["name"], s["created_at"])
            for p in data.get("progress", []):
                db.restore_progress_row(p["token"], p["lesson_id"], p["state_json"], p["updated_at"])
            for fb in data.get("feedback", []):
                db.restore_feedback_row(fb["token"], fb["lesson_id"], fb["text"], fb["updated_at"])
            for ls in data.get("lesson_scripts", []):
                db.set_script(ls["lesson_id"], ls["stage"], ls["script_text"])
            for am in data.get("lesson_audio_meta", []):
                db.set_audio_original_name(am["lesson_id"], am["stage"], am["original_filename"])

            for name in zf.namelist():
                if name.startswith("recordings/") and not name.endswith("/"):
                    dest = os.path.join(UPLOADS_DIR, os.path.basename(name))
                    with zf.open(name) as src, open(dest, "wb") as out:
                        out.write(src.read())
                elif name.startswith("audio/") and not name.endswith("/"):
                    dest = os.path.join(AUDIO_DIR, os.path.basename(name))
                    with zf.open(name) as src, open(dest, "wb") as out:
                        out.write(src.read())

            for rec in data.get("recordings", []):
                db.restore_recording_row(rec["token"], rec["lesson_id"], rec["filename"], rec["uploaded_at"])
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"백업 파일을 읽을 수 없어요: {e}")

    return back_to_admin()


# -------------------------------------------------------------- inquiries ---
# 홈페이지 상담 신청서(구글 설문지 → 구글 시트)의 응답을 관리자 센터에서 보여줘요.
# 렌더 Environment 에 INQUIRY_URL(구글 앱스 스크립트 웹 앱 주소)과 INQUIRY_TOKEN(비밀 토큰)을 넣어야 작동해요.
INQUIRY_URL = os.environ.get("INQUIRY_URL", "").strip()
INQUIRY_TOKEN = os.environ.get("INQUIRY_TOKEN", "").strip()
_inquiry_cache = {"at": 0.0, "data": None}


def fetch_inquiries(force: bool = False):
    if not INQUIRY_URL or not INQUIRY_TOKEN:
        return {"error": "setup"}
    if not force and _inquiry_cache["data"] is not None and time.time() - _inquiry_cache["at"] < 60:
        return _inquiry_cache["data"]
    sep = "&" if "?" in INQUIRY_URL else "?"
    url = f"{INQUIRY_URL}{sep}token={urllib.parse.quote(INQUIRY_TOKEN)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # 네트워크 문제 등
        return {"error": f"불러오지 못했어요: {e}"}
    if payload.get("error"):
        return {"error": "토큰이 맞지 않아요. 구글 스크립트의 TOKEN 과 렌더의 INQUIRY_TOKEN 이 같은지 확인해 주세요."}
    headers = payload.get("headers") or []
    rows = [r for r in (payload.get("rows") or []) if any(str(c).strip() for c in r)]
    items = []
    for r in reversed(rows):  # 최신 신청이 위로 (첫 칸은 구글이 넣는 신청 시각)
        fields = []
        for h, v in list(zip(headers, r))[1:]:
            v = str(v).strip()
            if not v or "개인정보" in h:
                continue
            fields.append({"label": h, "value": v})
        items.append({"time": str(r[0]) if r else "", "fields": fields})
    data = {"items": items, "count": len(items)}
    _inquiry_cache.update(at=time.time(), data=data)
    return data


@app.get("/admin/inquiries", response_class=HTMLResponse)
def admin_inquiries(request: Request, refresh: int = 0):
    if not is_admin(request):
        return back_to_admin()
    data = fetch_inquiries(force=bool(refresh))
    return templates.TemplateResponse(
        "inquiries.html",
        {"request": request, "active_tab": "inquiries", "data": data},
    )
