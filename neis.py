import os
from datetime import datetime
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