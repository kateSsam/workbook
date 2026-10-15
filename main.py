import asyncio
import glob
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import shutil
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone, date, timedelta
from typing import List, Optional

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
    """서버가 켜질 때, GitHub의 seed_audio/ 안의 음원(하위 폴더 포함)을 data/lesson_audio/ 로 복사해둔다.
    하위 폴더에 있는 파일은 '폴더이름 - 파일이름' 으로 보관함에 올라간다.
    (data/ 는 배포 환경에서 초기화될 수 있는 저장소라, 깃에 포함되는 seed_audio/ 에서 복원한다.)"""
    if not os.path.isdir(SEED_AUDIO_DIR):
        return
    for root, _dirs, files in os.walk(SEED_AUDIO_DIR):
        for fname in files:
            if os.path.splitext(fname)[1].lower() not in {".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac", ".webm", ".mp4", ".oga"}:
                continue
            rel = os.path.relpath(os.path.join(root, fname), SEED_AUDIO_DIR)
            name = " - ".join(rel.split(os.sep))
            dest = os.path.join(AUDIO_DIR, name)
            if not os.path.exists(dest):
                shutil.copyfile(os.path.join(root, fname), dest)


seed_lesson_audio()


def norm_lid(lid: str) -> str:
    return "day1" if lid == "lesson1" else lid


def kst_today() -> date:
    return db.kst_today()


WEEKDAYS_KO = "월화수목금토일"


def student_start(student) -> date:
    try:
        return date.fromisoformat(student["start_date"])
    except (TypeError, ValueError):
        return kst_today()


def slot_for_date(start: date, d: date) -> int:
    """start부터 d까지(포함) 평일(월~금)이 며칠인지 = 수업일 번호(Day N). 시작 전이면 0."""
    if d < start:
        return 0
    total = (d - start).days + 1
    weeks, rem = divmod(total, 7)
    count = weeks * 5
    for i in range(rem):
        if (start + timedelta(days=weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


def date_for_slot(start: date, n: int) -> date:
    d, c = start, 0
    while True:
        if d.weekday() < 5:
            c += 1
            if c == n:
                return d
        d += timedelta(days=1)


def day_label(start: date, n: int) -> str:
    d = date_for_slot(start, n)
    return f"{d.month}/{d.day}({WEEKDAYS_KO[d.weekday()]})"


def student_day(student) -> int:
    """오늘이 이 학생의 몇 번째 수업일(Day)인지. 주말엔 직전 금요일의 번호, 시작 전이면 0."""
    return slot_for_date(student_start(student), kst_today())


# ---- 음원 보관함: data/lesson_audio 안의 음원 파일 전부 (파일명은 자유) ----
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac", ".webm", ".mp4", ".oga"}


def safe_audio_name(name: str) -> str:
    base = os.path.basename((name or "").replace("\\", "/")).strip().lstrip(".")
    return base


def audio_path(name: str) -> Optional[str]:
    base = safe_audio_name(name)
    if not base or os.path.splitext(base)[1].lower() not in AUDIO_EXTS:
        return None
    p = os.path.join(AUDIO_DIR, base)
    return p if os.path.isfile(p) else None


def list_library() -> List[str]:
    names = [f for f in os.listdir(AUDIO_DIR)
             if os.path.isfile(os.path.join(AUDIO_DIR, f)) and os.path.splitext(f)[1].lower() in AUDIO_EXTS]
    def natural(s):
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]
    return sorted(names, key=natural)


def group_of(name: str) -> str:
    """'교재A - 1과.mp3' -> '교재A'. 묶음 표시가 없으면 빈 문자열."""
    return name.split(" - ", 1)[0].strip() if " - " in name else ""


def plan_files(plan: dict, slot: int) -> List[str]:
    """그날 연습할 음원 (보통 하나, 필요하면 최대 둘)."""
    return [f for f in plan.get(slot, []) if audio_path(f)][:2]


def script_for(filename: str) -> str:
    """음원의 정답 스크립트 (예전에 예습/복습으로 나눠 저장한 것도 읽어줌)."""
    return db.get_script("audio:" + filename, "train") or db.get_script("audio:" + filename, "review") or db.get_script("audio:" + filename, "preview")


def student_allowed_audio(student, filename: str) -> bool:
    today_n = min(student_day(student), lessons.MAX_DAYS)
    plan = db.get_plan(student["id"])
    return any(filename in files for slot, files in plan.items() if slot <= today_n)


# ---- 관리자 로그인 (주소에 열쇠가 남지 않도록 브라우저 쿠키로 로그인 유지) ----
SESSION_COOKIE = "kate_admin"
SESSION_DAYS = 14


def _sign(ts: str) -> str:
    return hmac.new(ADMIN_KEY.encode(), f"admin:{ts}".encode(), hashlib.sha256).hexdigest()


def make_session() -> str:
    ts = str(int(time.time()))
    return f"{ts}.{_sign(ts)}"


def key_ok(key: Optional[str]) -> bool:
    return bool(key) and hmac.compare_digest(key.encode(), ADMIN_KEY.encode())


def is_admin(request: Request) -> bool:
    raw = request.cookies.get(SESSION_COOKIE, "")
    if "." not in raw:
        return False
    ts, sig = raw.split(".", 1)
    if not ts.isdigit() or not hmac.compare_digest(sig, _sign(ts)):
        return False
    return time.time() - int(ts) < SESSION_DAYS * 86400


def check_admin(request: Request, key: Optional[str] = None):
    if is_admin(request) or key_ok(key):
        return
    raise HTTPException(status_code=403, detail="관리자 로그인이 필요해요. /admin 에서 다시 로그인해 주세요.")


def login_cookie(resp):
    resp.set_cookie(SESSION_COOKIE, make_session(), max_age=SESSION_DAYS * 86400,
                    httponly=True, secure=True, samesite="lax", path="/")
    return resp


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


def _allowed_lesson(student, lesson: str) -> bool:
    n = lessons.parse_day(lesson)
    return n is not None and n <= student_day(student)


@app.get("/w/{token}", response_class=HTMLResponse)
def workbook(request: Request, token: str, day: Optional[int] = None):
    student = db.get_student_by_token(token)
    if not student:
        raise HTTPException(status_code=404, detail="유효하지 않은 링크입니다. 선생님께 링크를 다시 받아주세요.")

    start = student_start(student)
    today_n = student_day(student)
    if today_n < 1:
        first = date_for_slot(start, 1)
        return HTMLResponse(
            "<body style='font-family:sans-serif;padding:40px;line-height:1.8;text-align:center'>"
            f"<h2>{student['name']}님, 아직 시작 전이에요</h2>"
            f"<p>워크북은 <strong>{first.month}월 {first.day}일({WEEKDAYS_KO[first.weekday()]})</strong> 에 시작돼요.<br>그날 다시 이 링크로 들어와주세요!</p></body>"
        )
    today_n = min(today_n, lessons.MAX_DAYS)
    rest_day = kst_today().weekday() >= 5
    day_n = day if day and 1 <= day <= today_n else today_n
    lesson = lessons.day_lesson_id(day_n)

    plan = db.get_plan(student["id"])
    audios = []
    for f in plan_files(plan, day_n):
        audios.append({
            "url": f"/audio/{urllib.parse.quote(f)}?t={token}",
            "name": f,
            "answer": script_for(f),
        })

    lesson_data = dict(lessons.BASE_LESSON)
    lesson_data["audios"] = audios

    state = db.get_progress(student["id"], lesson)
    feedback_text = db.get_feedback(student["id"], lesson)
    days = [{"n": n, "label": day_label(start, n)} for n in range(today_n, 0, -1)]

    return templates.TemplateResponse(
        "workbook.html",
        {
            "request": request,
            "token": token,
            "student_name": student["name"],
            "lesson_id": lesson,
            "day_n": day_n,
            "today_n": today_n,
            "day_label": day_label(start, day_n),
            "rest_day": rest_day,
            "days": days,
            "lesson": lesson_data,
            "initial_state": state,
            "teacher_feedback": feedback_text,
        },
    )


@app.get("/api/{token}/state")
def api_get_state(token: str, lesson: str = "day1"):
    student = db.get_student_by_token(token)
    if not student or not _allowed_lesson(student, lesson):
        raise HTTPException(status_code=404, detail="not found")
    return db.get_progress(student["id"], lesson)


@app.post("/api/{token}/state")
async def api_save_state(request: Request, token: str, lesson: str = "day1"):
    student = db.get_student_by_token(token)
    if not student or not _allowed_lesson(student, lesson):
        raise HTTPException(status_code=404, detail="not found")
    patch = await request.json()
    return db.save_progress(student["id"], lesson, patch)


@app.get("/audio/{filename}")
def get_audio(request: Request, filename: str, t: Optional[str] = None, key: Optional[str] = None):
    # 관리자 또는 이 음원이 이미 열린 날짜에 배정된 학생(t=링크 토큰)만 들을 수 있어요.
    if is_admin(request) or key:
        check_admin(request, key)
    else:
        student = db.get_student_by_token(t or "")
        if not student or not student_allowed_audio(student, safe_audio_name(filename)):
            raise HTTPException(status_code=404, detail="아직 열리지 않았어요.")
    path = audio_path(filename)
    if not path:
        raise HTTPException(status_code=404, detail="음원을 찾을 수 없어요.")
    media_type, _ = mimetypes.guess_type(path)
    return FileResponse(path, media_type=media_type or "audio/mpeg")


@app.post("/api/{token}/upload")
async def api_upload_recording(token: str, lesson: str = "day1", file: UploadFile = File(...)):
    student = db.get_student_by_token(token)
    if not student or not _allowed_lesson(student, lesson):
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

def _kst_str(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        k = dt.astimezone(timezone(timedelta(hours=9)))
        return f"{k.month}/{k.day} {k.hour:02d}:{k.minute:02d}"
    except (TypeError, ValueError):
        return ""


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, key: Optional[str] = None, day: Optional[int] = None, sel: Optional[str] = None):
    # 예전 방식(주소에 ?key=...)으로 들어와도, 쿠키로 바꿔 주고 깨끗한 주소로 다시 보내요
    if key:
        if key_ok(key):
            return login_cookie(_admin_redirect(lesson_id=lessons.day_lesson_id(day) if day else None))
        return HTMLResponse(LOGIN_PAGE.format(msg="<p style='color:#D9613F'>관리자 키가 맞지 않아요. 다시 입력해 주세요.</p>"), status_code=401)
    if not is_admin(request):
        return HTMLResponse(LOGIN_PAGE.format(msg=""))

    today = kst_today()
    students_view = []
    for s in db.list_students():
        progress = db.all_progress_for_student(s["id"])
        recordings = db.list_recordings(s["id"])
        feedbacks = db.all_feedback_for_student(s["id"])
        start = student_start(s)
        sd = student_day(s)
        today_slot = min(sd, lessons.MAX_DAYS) if sd >= 1 else 0
        plan = db.get_plan(s["id"])

        recs_by_lid = {}
        for r in recordings:
            recs_by_lid.setdefault(r["lesson_id"], []).append({
                "filename": r["filename"], "time": _kst_str(r["uploaded_at"]),
            })

        days = []
        for n in range(today_slot, 0, -1):
            lid = lessons.day_lesson_id(n)
            st = progress.get(lid, {}).get("state", {})
            recs = recs_by_lid.get(lid, [])
            fb = feedbacks.get(lid, "")
            days.append({
                "n": n, "lid": lid, "label": day_label(start, n),
                "audios": plan_files(plan, n),
                "state": st, "recordings": recs, "feedback": fb,
                "new": bool(recs) and not fb.strip(),
            })

        recent = []
        for r in recordings[:3]:
            n = lessons.parse_day(r["lesson_id"])
            recent.append({"filename": r["filename"], "time": _kst_str(r["uploaded_at"]), "n": n})

        selected = today_slot
        if sel and ":" in sel:
            sid, _, dn = sel.partition(":")
            if sid == str(s["id"]) and dn.isdigit() and 1 <= int(dn) <= today_slot:
                selected = int(dn)

        students_view.append(
            {
                "row": s,
                "today_day": sd,
                "rest": today.weekday() >= 5,
                "today_audios": plan_files(plan, today_slot) if today_slot else [],
                "plan_count": len(plan),
                "days": days,
                "selected": selected,
                "recent": recent,
                "new_count": sum(1 for x in days if x["new"]),
                "rec_days": sorted({x["n"] for x in days if x["recordings"]}),
                "link": f"/w/{s['token']}",
            }
        )

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "key": "",
            "active_tab": "students",
            "students": students_view,
            "today": today.isoformat(),
            "library_count": len(list_library()),
            "rollover_hour": db.rollover_hour(),
        },
    )


def _admin_redirect(key: str = None, lesson_id: str = None):
    n = lessons.parse_day(lesson_id) if lesson_id else None
    suffix = f"?day={n}" if n else ""
    return RedirectResponse(url=f"/admin{suffix}", status_code=303)


# ------------------------------------------------------------ 음원 보관함 ---

@app.get("/admin/library", response_class=HTMLResponse)
def admin_library(request: Request):
    if not is_admin(request):
        return RedirectResponse(url="/admin", status_code=303)
    groups = {}
    for f in list_library():
        g = group_of(f)
        groups.setdefault(g, []).append({
            "name": f,
            "short": f.split(" - ", 1)[1] if g else f,
            "url": "/audio/" + urllib.parse.quote(f),
            "size_mb": round(os.path.getsize(os.path.join(AUDIO_DIR, f)) / 1048576, 1),
            "answer": script_for(f),
        })
    ordered = [{"name": g or "묶음 없음", "files": groups[g]} for g in sorted(groups, key=lambda x: (x == "", x.lower()))]
    existing_groups = [g for g in groups if g]
    return templates.TemplateResponse(
        "library.html",
        {"request": request, "active_tab": "library", "groups": ordered, "total": sum(len(v) for v in groups.values()), "existing_groups": existing_groups},
    )


@app.post("/admin/library/upload")
async def admin_library_upload(request: Request, files: List[UploadFile] = File(...), group: str = Form("")):
    check_admin(request, None)
    group = safe_audio_name(group).replace(" - ", "-")
    for up in files:
        name = safe_audio_name(up.filename or "")
        if not name or os.path.splitext(name)[1].lower() not in AUDIO_EXTS:
            continue
        if group and not name.startswith(group + " - "):
            name = f"{group} - {name}"
        content = await up.read()
        with open(os.path.join(AUDIO_DIR, name), "wb") as out:
            out.write(content)
    return RedirectResponse(url="/admin/library", status_code=303)


@app.post("/admin/library/rename")
def admin_library_rename(request: Request, filename: str = Form(...), new_name: str = Form(...)):
    check_admin(request, None)
    old = audio_path(filename)
    base_old = safe_audio_name(filename)
    new = safe_audio_name(new_name)
    ext = os.path.splitext(base_old)[1]
    if new and os.path.splitext(new)[1].lower() not in AUDIO_EXTS:
        new += ext
    if not old or not new or os.path.exists(os.path.join(AUDIO_DIR, new)):
        raise HTTPException(status_code=400, detail="이름을 바꿀 수 없어요. 같은 이름의 음원이 이미 있는지 확인해주세요.")
    os.rename(old, os.path.join(AUDIO_DIR, new))
    db.rename_audio_refs(base_old, new)
    return RedirectResponse(url="/admin/library", status_code=303)


@app.post("/admin/library/delete")
def admin_library_delete(request: Request, filename: str = Form(...)):
    check_admin(request, None)
    p = audio_path(filename)
    if p:
        os.remove(p)
    return RedirectResponse(url="/admin/library", status_code=303)


@app.post("/admin/library/script")
def admin_library_script(request: Request, filename: str = Form(...), text: str = Form("")):
    check_admin(request, None)
    if not audio_path(filename):
        raise HTTPException(status_code=404, detail="음원을 찾을 수 없어요.")
    n = safe_audio_name(filename)
    db.set_script("audio:" + n, "train", text)
    db.set_script("audio:" + n, "review", "")   # 예전 예습/복습 정답이 남아 있으면 정리
    db.set_script("audio:" + n, "preview", "")
    return RedirectResponse(url="/admin/library", status_code=303)


# ------------------------------------------------------------ 학생별 진도표 ---

def _library_groups():
    groups = {}
    for f in list_library():
        groups.setdefault(group_of(f), []).append(f)
    return [{"name": g or "묶음 없음", "files": groups[g]} for g in sorted(groups, key=lambda x: (x == "", x.lower()))]


def _plan_page_data(student):
    start = student_start(student)
    plan = db.get_plan(student["id"])
    weeks = []
    for w in range(lessons.MAX_DAYS // 5):
        slots = []
        for i in range(5):
            n = w * 5 + i + 1
            files = plan.get(n, [])
            slots.append({
                "n": n,
                "label": day_label(start, n),
                "a": files[0] if len(files) > 0 else "",
                "b": files[1] if len(files) > 1 else "",
            })
        weeks.append({"no": w + 1, "slots": slots, "filled": any(s["a"] for s in slots)})
    return weeks


@app.get("/admin/students/{student_id}/plan", response_class=HTMLResponse)
def admin_plan(request: Request, student_id: int, saved: int = 0):
    if not is_admin(request):
        return RedirectResponse(url="/admin", status_code=303)
    student = db.get_student_by_id(student_id)
    if not student:
        raise HTTPException(status_code=404, detail="학생을 찾을 수 없어요.")
    others = [s for s in db.list_students() if s["id"] != student_id]
    return templates.TemplateResponse(
        "plan.html",
        {
            "request": request,
            "active_tab": "students",
            "student": student,
            "weeks": _plan_page_data(student),
            "library": list_library(),
            "library_groups": _library_groups(),
            "others": others,
            "today_day": student_day(student),
            "saved": bool(saved),
        },
    )


@app.post("/admin/students/{student_id}/plan")
async def admin_plan_save(request: Request, student_id: int):
    check_admin(request, None)
    if not db.get_student_by_id(student_id):
        raise HTTPException(status_code=404, detail="학생을 찾을 수 없어요.")
    form = await request.form()
    plan = {}
    for n in range(1, lessons.MAX_DAYS + 1):
        files = []
        for part in ("a", "b"):
            v = safe_audio_name(str(form.get(f"s{n}_{part}", "")))
            if v and v not in files:
                files.append(v)
        if files:
            plan[n] = files
    db.set_plan(student_id, plan)
    return RedirectResponse(url=f"/admin/students/{student_id}/plan?saved=1", status_code=303)


@app.post("/admin/students/{student_id}/plan/copy")
def admin_plan_copy(request: Request, student_id: int, source_id: int = Form(...)):
    check_admin(request, None)
    if not db.get_student_by_id(student_id) or not db.get_student_by_id(source_id):
        raise HTTPException(status_code=404, detail="학생을 찾을 수 없어요.")
    db.set_plan(student_id, db.get_plan(source_id))
    return RedirectResponse(url=f"/admin/students/{student_id}/plan?saved=1", status_code=303)


@app.post("/admin/students")
def admin_add_student(request: Request, key: Optional[str] = Form(None), name: str = Form(...), start_date: str = Form("")):
    check_admin(request, key)
    sd = _valid_date(start_date)
    if name.strip():
        db.create_student(name.strip(), sd)
    return RedirectResponse(url="/admin", status_code=303)


def _valid_date(s: str):
    try:
        return date.fromisoformat(s.strip()).isoformat()
    except (ValueError, AttributeError):
        return None


@app.post("/admin/students/{student_id}/start-date")
def admin_set_start_date(student_id: int, request: Request, key: Optional[str] = Form(None), start_date: str = Form(...), day: int = Form(1)):
    check_admin(request, key)
    sd = _valid_date(start_date)
    if not sd:
        raise HTTPException(status_code=400, detail="날짜 형식이 올바르지 않아요.")
    db.set_student_start_date(student_id, sd)
    return RedirectResponse(url=f"/admin#card-{student_id}", status_code=303)


@app.post("/admin/rollover")
def admin_set_rollover(request: Request, key: Optional[str] = Form(None), hour: int = Form(...)):
    check_admin(request, key)
    if not 0 <= hour <= 12:
        raise HTTPException(status_code=400, detail="0~12 사이 시각을 골라 주세요.")
    db.set_setting("rollover_hour", str(hour))
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/students/{student_id}/delete")
def admin_delete_student(student_id: int, request: Request, key: Optional[str] = Form(None)):
    check_admin(request, key)
    db.delete_student(student_id)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/feedback")
def admin_save_feedback(
    request: Request, key: Optional[str] = Form(None),
    student_id: int = Form(...),
    lesson_id: str = Form(...),
    text: str = Form(""),
):
    check_admin(request, key)
    if lessons.parse_day(lesson_id) is None:
        raise HTTPException(status_code=404, detail="존재하지 않는 Day입니다.")
    db.set_feedback(student_id, lesson_id, text)
    n = lessons.parse_day(lesson_id)
    return RedirectResponse(url=f"/admin?sel={student_id}:{n}#card-{student_id}", status_code=303)


@app.get("/admin/recording/{filename}")
def admin_get_recording(request: Request, filename: str, key: Optional[str] = None):
    check_admin(request, key)
    path = os.path.join(UPLOADS_DIR, os.path.basename(filename))
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.")
    return FileResponse(path)


@app.get("/admin/backup")
def admin_backup(request: Request, key: Optional[str] = None):
    check_admin(request, key)
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
async def admin_restore(request: Request, key: Optional[str] = Form(None), file: UploadFile = File(...)):
    check_admin(request, key)
    content = await file.read()
    buf = io.BytesIO(content)

    try:
        with zipfile.ZipFile(buf) as zf:
            with zf.open("backup.json") as f:
                data = json.load(f)

            for s in data.get("students", []):
                db.restore_student(s["token"], s["name"], s["created_at"], s.get("start_date"))
            for p in data.get("progress", []):
                db.restore_progress_row(p["token"], norm_lid(p["lesson_id"]), p["state_json"], p["updated_at"])
            for fb in data.get("feedback", []):
                db.restore_feedback_row(fb["token"], norm_lid(fb["lesson_id"]), fb["text"], fb["updated_at"])
            for ls in data.get("lesson_scripts", []):
                db.set_script(norm_lid(ls["lesson_id"]), ls["stage"], ls["script_text"])
            for am in data.get("lesson_audio_meta", []):
                db.set_audio_original_name(norm_lid(am["lesson_id"]), am["stage"], am["original_filename"])

            for name in zf.namelist():
                if name.startswith("recordings/") and not name.endswith("/"):
                    dest = os.path.join(UPLOADS_DIR, os.path.basename(name))
                    with zf.open(name) as src, open(dest, "wb") as out:
                        out.write(src.read())
                elif name.startswith("audio/") and not name.endswith("/"):
                    dest = os.path.join(AUDIO_DIR, os.path.basename(name))
                    with zf.open(name) as src, open(dest, "wb") as out:
                        out.write(src.read())

            for st in data.get("settings", []):
                db.set_setting(st["key"], st["value"])

            for pl in data.get("plans", []):
                db.restore_plan_row(pl["token"], pl["slot"], pl["audios_json"])

            for rec in data.get("recordings", []):
                db.restore_recording_row(rec["token"], norm_lid(rec["lesson_id"]), rec["filename"], rec["uploaded_at"])
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"백업 파일을 읽을 수 없어요: {e}")

    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/login")
async def admin_login(key: str = Form(...)):
    if not key_ok(key):
        await asyncio.sleep(1.5)  # 마구잡이로 비밀번호를 넣어 보는 걸 느리게 만들어요
        return HTMLResponse(LOGIN_PAGE.format(msg="<p style='color:#D9613F'>관리자 키가 맞지 않아요. 다시 입력해 주세요.</p>"), status_code=401)
    return login_cookie(RedirectResponse(url="/admin", status_code=303))


@app.get("/admin/logout")
def admin_logout():
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


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
        return RedirectResponse(url="/admin", status_code=303)
    data = fetch_inquiries(force=bool(refresh))
    return templates.TemplateResponse(
        "inquiries.html",
        {"request": request, "active_tab": "inquiries", "data": data},
    )
