import os
from datetime import datetime, timedelta
import requests

BASE_URL = "https:" + "//open.neis.go.kr/hub"
TIMEOUT = 5

ALLERGY_CODES = {
    1: "난류", 2: "우유", 3: "메밀", 4: "땅콩", 5: "대두", 6: "밀",
    7: "고등어", 8: "게", 9: "새우", 10: "돼지고기", 11: "복숭아", 12: "토마토",
    13: "아황산류", 14: "호두", 15: "닭고기", 16: "쇠고기", 17: "오징어",
    18: "조개류(굴, 전복, 홍합 등)", 19: "잣",
}

def _get(endpoint: str, **params):
    query = {"Type": "json", "pIndex": 1, "pSize": 5, **params}
    api_key = os.environ.get("NEIS_API_KEY")
    if api_key:
        query["KEY"] = api_key
    try:
        return requests.get(f"{BASE_URL}/{endpoint}", params=query, timeout=TIMEOUT).json()
    except (requests.RequestException, ValueError):
        return {}

def get_school_code(school_name: str):
    """학교 이름 검색 후 (교육청코드, 학교코드, 학교명) 반환"""
    res = _get("schoolInfo", SCHUL_NM=school_name)
    if "schoolInfo" not in res:
        return None
    row = res["schoolInfo"][1]["row"][0]
    return row["ATPT_OFCDC_SC_CODE"], row["SD_SCHUL_CODE"], row["SCHUL_NM"]

def get_today_meal(office_code: str, school_code: str):
    """오늘 급식 데이터를 딕셔너리로 반환"""
    today = datetime.now().strftime("%Y%m%d")
    res = _get(
        "mealServiceDietInfo",
        ATPT_OFCDC_SC_CODE=office_code,
        SD_SCHUL_CODE=school_code,
        MLSV_YMD=today,
    )
    if "mealServiceDietInfo" not in res:
        return None

    rows = res["mealServiceDietInfo"][1]["row"]
    row = next((r for r in rows if r.get("MMEAL_SC_CODE") == "2"), rows[0])
    return {
        "date": today,
        "meal_name": row.get("MMEAL_SC_NM", ""),
        "menu": row["DDISH_NM"].replace("<br/>", "\n"),
        "calorie": row.get("CAL_INFO", ""),
        "nutrition": row.get("NTR_INFO", "").replace("<br/>", "\n"),
    }
TIMETABLE_ENDPOINTS = {"초등학교": "elsTimetable", "중학교": "misTimetable", "고등학교": "hisTimetable"}
WEEKDAY_NAMES = ["월", "화", "수", "목", "금"]


def get_school(school_name: str):
    """학교 이름 검색 후 코드·학교급을 딕셔너리로 반환"""
    res = _get("schoolInfo", SCHUL_NM=school_name)
    if "schoolInfo" not in res:
        return None
    row = res["schoolInfo"][1]["row"][0]
    return {
        "office_code": row["ATPT_OFCDC_SC_CODE"],
        "school_code": row["SD_SCHUL_CODE"],
        "name": row["SCHUL_NM"],
        "kind": row.get("SCHUL_KND_SC_NM", ""),
    }


def get_week_timetable(school: dict, grade: int, class_nm: str):
    """이번 주(월~금) 시간표를 [{날짜, 요일, 교시별 과목}] 형태로 반환. 조회 실패 시 None"""
    endpoint = TIMETABLE_ENDPOINTS.get(school["kind"])
    if endpoint is None:
        return None

    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    days = [monday + timedelta(days=i) for i in range(5)]
    res = _get(
        endpoint,
        pSize=100,
        ATPT_OFCDC_SC_CODE=school["office_code"],
        SD_SCHUL_CODE=school["school_code"],
        GRADE=grade,
        CLASS_NM=class_nm,
        TI_FROM_YMD=days[0].strftime("%Y%m%d"),
        TI_TO_YMD=days[-1].strftime("%Y%m%d"),
    )
    if endpoint not in res:
        return None

    by_date = {d.strftime("%Y%m%d"): {} for d in days}
    for row in res[endpoint][1]["row"]:
        ymd = row.get("ALL_TI_YMD")
        if ymd in by_date:
            by_date[ymd][int(row.get("PERIO", 0))] = row.get("ITRT_CNTNT", "").lstrip("-").strip()

    max_period = max([0] + [p for periods in by_date.values() for p in periods])
    return {
        "periods": list(range(1, max_period + 1)),
        "days": [
            {
                "date": d.strftime("%m/%d"),
                "weekday": WEEKDAY_NAMES[i],
                "is_today": d == today,
                "subjects": by_date[d.strftime("%Y%m%d")],
            }
            for i, d in enumerate(days)
        ],
    }


def get_school_schedule(school: dict, days_ahead: int = 60):
    """오늘부터 days_ahead 일 동안의 학사일정 목록. 토요휴업일 같은 반복 일정은 뺀다."""
    today = datetime.now().date()
    res = _get(
        "SchoolSchedule",
        pSize=100,
        ATPT_OFCDC_SC_CODE=school["office_code"],
        SD_SCHUL_CODE=school["school_code"],
        AA_FROM_YMD=today.strftime("%Y%m%d"),
        AA_TO_YMD=(today + timedelta(days=days_ahead)).strftime("%Y%m%d"),
    )
    if "SchoolSchedule" not in res:
        return []

    events = []
    for row in res["SchoolSchedule"][1]["row"]:
        name = row.get("EVENT_NM", "").strip()
        if not name or name == "토요휴업일":
            continue
        event_date = datetime.strptime(row["AA_YMD"], "%Y%m%d").date()
        events.append({
            "date": event_date.strftime("%m/%d"),
            "weekday": "월화수목금토일"[event_date.weekday()],
            "name": name,
            "d_day": (event_date - today).days,
        })
    return events
