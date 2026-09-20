import glob
import mimetypes
import os
import shutil
import uuid
from typing import Optional

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
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


STAGES_WITH_AUDIO = ["preview", "review", "training"]


def check_admin(key: Optional[str]):
    if not key or key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="관리자 키가 올바르지 않습니다.")


# ---------------------------------------------------------------- student ---

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
    lesson_data["model_audio_url"] = lesson_data["stage_audio"]["training"]
    lesson_data["preview_script"] = db.get_script(lesson, "preview")
    lesson_data["review_script"] = db.get_script(lesson, "review")

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

@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, key: Optional[str] = None):
    if key != ADMIN_KEY:
        return HTMLResponse(
            "<body style='font-family:sans-serif;padding:40px'>"
            "<h3>관리자 페이지</h3>"
            "<form method='get' action='/admin'>"
            "<input name='key' type='password' placeholder='관리자 키' "
            "style='padding:8px;border:1px solid #ccc;border-radius:6px'/> "
            "<button type='submit' style='padding:8px 14px'>입장</button>"
            "</form></body>"
        )

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

    lesson_audio_info = {
        lid: {
            stage: (os.path.basename(find_lesson_audio(lid, stage)) if find_lesson_audio(lid, stage) else None)
            for stage in STAGES_WITH_AUDIO
        }
        for lid in lessons.LESSONS.keys()
    }
    lesson_scripts = {
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
            "key": key,
            "students": students_view,
            "lessons": lessons.LESSONS,
            "lesson_audio_info": lesson_audio_info,
            "lesson_scripts": lesson_scripts,
            "stages_with_audio": STAGES_WITH_AUDIO,
        },
    )


@app.post("/admin/lesson-audio")
async def admin_upload_lesson_audio(
    key: str = Form(...),
    lesson_id: str = Form(...),
    stage: str = Form(...),
    file: UploadFile = File(...),
):
    check_admin(key)
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

    return RedirectResponse(url=f"/admin?key={key}", status_code=303)


@app.post("/admin/script")
def admin_save_script(
    key: str = Form(...),
    lesson_id: str = Form(...),
    stage: str = Form(...),
    text: str = Form(""),
):
    check_admin(key)
    if lesson_id not in lessons.LESSONS:
        raise HTTPException(status_code=404, detail="존재하지 않는 차시입니다.")
    if stage not in ("preview", "review"):
        raise HTTPException(status_code=400, detail="알 수 없는 단계입니다.")
    db.set_script(lesson_id, stage, text)
    return RedirectResponse(url=f"/admin?key={key}", status_code=303)


@app.post("/admin/students")
def admin_add_student(key: str = Form(...), name: str = Form(...)):
    check_admin(key)
    if name.strip():
        db.create_student(name.strip())
    return RedirectResponse(url=f"/admin?key={key}", status_code=303)


@app.post("/admin/students/{student_id}/delete")
def admin_delete_student(student_id: int, key: str = Form(...)):
    check_admin(key)
    db.delete_student(student_id)
    return RedirectResponse(url=f"/admin?key={key}", status_code=303)


@app.post("/admin/feedback")
def admin_save_feedback(
    key: str = Form(...),
    student_id: int = Form(...),
    lesson_id: str = Form(...),
    text: str = Form(""),
):
    check_admin(key)
    db.set_feedback(student_id, lesson_id, text)
    return RedirectResponse(url=f"/admin?key={key}", status_code=303)


@app.get("/admin/recording/{filename}")
def admin_get_recording(filename: str, key: Optional[str] = None):
    check_admin(key)
    path = os.path.join(UPLOADS_DIR, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.")
    return FileResponse(path)
