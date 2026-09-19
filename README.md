# 발음/스피치 훈련 워크북 서버

FastAPI + SQLite로 만든 자기주도형 학습 워크북입니다. 학생마다 개별 링크를 발급하고,
예습/복습/훈련/훈련피드백 진행상황과 녹음 파일이 서버(선생님 쪽)에 저장됩니다.

## 폴더 구조

```
main.py           FastAPI 앱, 라우트
db.py             SQLite 접근 함수
lessons.py        차시별 콘텐츠 (새 차시는 여기에 추가)
templates/
  workbook.html   학생용 화면
  admin.html      선생님 관리자 화면
data/             실행 중 자동 생성 (DB 파일 + 녹음 파일 저장 위치)
```

## 로컬에서 실행해보기

1. 파이썬 3.10 이상이 필요해요.
2. 터미널에서:
   ```
   cd workbook-server
   pip install -r requirements.txt
   python -m uvicorn main:app --reload
   ```
3. 브라우저에서 `http://localhost:8000/admin` 접속 → 기본 키는 `changeme` 예요.
4. 학생을 등록하면 `/w/토큰` 형태의 링크가 만들어져요. 로컬에서는 `http://localhost:8000/w/토큰` 으로 접속해서 테스트할 수 있어요.

## 관리자 키(비밀번호) 바꾸기

기본값 `changeme` 를 꼭 바꿔주세요. 환경변수 `ADMIN_KEY` 로 지정합니다.

로컬 테스트: `ADMIN_KEY=원하는비밀번호 python -m uvicorn main:app --reload`

## Render에 무료로 배포하기

1. https://render.com 에 가입 (GitHub 계정으로 가입하면 편해요)
2. 이 프로젝트 폴더를 본인 GitHub 저장소에 올려요 (새 레포 만들고 push)
3. Render 대시보드 → "New +" → "Web Service" → 방금 만든 GitHub 저장소 선택
4. 아래처럼 입력:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: Free
5. "Environment" 탭에서 환경변수 추가: `ADMIN_KEY` = 원하는 비밀번호
6. Deploy! 몇 분 뒤 `https://your-app-이름.onrender.com` 주소가 생겨요.
7. `https://your-app-이름.onrender.com/admin` 으로 들어가서 학생을 등록하면
   `https://your-app-이름.onrender.com/w/토큰` 링크가 만들어져요. 이 링크를 학생에게 나눠주세요.

### 무료 요금제 참고사항

- 15분간 접속이 없으면 서버가 잠들고, 다음 학생이 접속할 때 몇 초~십몇 초 정도 깨어나는 시간이 걸려요. 수업용으로는 크게 문제되지 않는 수준이에요.
- 무료 요금제는 재배포(코드를 새로 push)할 때 디스크가 초기화될 수 있어요. 즉 코드를 수정해서 다시 올리면 그동안 쌓인 학생 기록이 사라질 수 있습니다. 학생 기록이 중요해지는 시점부터는 Render의 유료 "Starter" 플랜(월 $7 수준, 디스크가 유지됨)으로 올리는 걸 권장해요.
- 운영 중 데이터를 안전하게 보관하려면 `data/workbook.db` 파일을 주기적으로 다운로드해 백업해두는 것도 방법이에요.

## 모범 음원 올리기 (코드 수정 없이)

배포 후 `/admin` 페이지에 들어가면 "차시별 모범 음원" 섹션이 있어요. 여기서 mp3/wav 파일을 올리면
바로 학생 화면의 "모범 음원 듣기" 자리에 반영돼요. 코드 수정이나 재배포가 필요 없어요.

- 이미 음원이 있는 차시는 "교체 업로드" 버튼으로 다시 올리면 기존 파일을 덮어써요.
- `seed_audio/` 폴더에는 처음 배포할 때 자동으로 채워지는 기본 음원이 들어있어요 (현재 `lesson1.mp3`).
  서버가 처음 실행될 때 이 폴더의 파일들을 `data/lesson_audio/` 로 복사해둡니다.
- 주의: Render 무료 요금제에서 재배포(코드를 git에 다시 push)하면 `data/` 폴더가 초기화될 수 있어요.
  그때는 `seed_audio/` 에 있는 파일은 자동으로 복원되지만, 관리자 페이지로 새로 올린 음원은
  다시 업로드해줘야 할 수 있어요. 자주 바뀌지 않는 기본 음원은 `seed_audio/` 에 넣어서 깃에 커밋해두는 걸 추천해요.

## 차시 추가하기

`lessons.py` 의 `LESSONS` 딕셔너리에 `"lesson2": {...}` 형태로 같은 구조를 추가하면 됩니다.
학생 링크는 `/w/토큰?lesson=lesson2` 처럼 쿼리로 차시를 지정할 수 있어요.
(추후 차시 탭 UI를 워크북 상단에 추가하는 것도 요청하시면 만들어드릴게요.)
