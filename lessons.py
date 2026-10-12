# 워크북 기본 틀(목표·체크리스트·평가 항목). 날짜별로 달라지는 것은 음원과 정답 스크립트(관리자 페이지에서 올림)입니다.

BASE_LESSON = {
    "lesson1": {
        "title": "오늘의 연습",
        "subtitle": "문장 강세와 정확한 모음 발음으로, 첫인사를 또렷하게 전달하는 연습이에요.",
        "preview": {
            "goals": [
                {"title": "문장 끝을 흐리지 않기", "desc": "문장의 마지막 단어까지 힘을 유지해요."},
                {"title": "모음을 크고 정확하게", "desc": "입모양을 또렷이 만들어 단어가 뭉개지지 않게 해요."},
                {"title": "숨 쉬는 지점 정하기", "desc": "문장 중간에 어디서 숨을 쉴지 미리 표시해두면 안정감이 생겨요."},
            ],
            "checklist": [
                "조용한 공간에 있어요",
                "마이크(휴대폰/이어폰)를 사용할 수 있어요",
                "목을 가볍게 풀었어요 (하품, 어깨 돌리기 등)",
            ],
        },
        "review_caution": [
            "조용한 공간인가요?",
            "이어폰(또는 마이크)을 착용하셨나요?",
            "목을 가볍게 풀어주셨나요?",
        ],
        "rate_items": [
            {"key": "clarity", "label": "발음이 또렷했나요?"},
            {"key": "ending", "label": "문장 끝까지 힘있게 말했나요?"},
            {"key": "pace", "label": "속도가 적당했나요?"},
            {"key": "confidence", "label": "자신있게 들렸나요?"},
        ],
    }
}

BASE_LESSON = BASE_LESSON["lesson1"]

MAX_DAYS = 60  # 관리자 페이지에서 준비할 수 있는 최대 Day 수


def day_lesson_id(n: int) -> str:
    return f"day{n}"


def parse_day(lesson_id):
    """'day3' -> 3. 형식이 맞지 않으면 None."""
    if isinstance(lesson_id, str) and lesson_id.startswith("day") and lesson_id[3:].isdigit():
        n = int(lesson_id[3:])
        if 1 <= n <= MAX_DAYS:
            return n
    return None
