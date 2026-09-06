import os
import requests
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

GRADE_LABELS = {
    1: "좋음",
    2: "보통",
    3: "나쁨",
    4: "매우 나쁨",
}
SAMPLE_PATH = Path(__file__).resolve().parent / "data" / "sample_air_quality.json"
API_URL = "http://apis.data.go.kr/B552584/ArpltnInforInqireSvc/getCtprvnRltmMesureDnsty"

def fetch_raw(sido="서울"):
    api_key = os.environ.get("PUBLIC_DATA_API_KEY")
    params = {
        "serviceKey": api_key,
        "returnType": "json",
        "numOfRows": 100,
        "pageNo": 1,
        "sidoName": sido,
        "ver": "1.3",
    }
    response = requests.get(API_URL, params=params, timeout=5)
    return response.json()

def parse_items(raw): 
    items = raw["response"]["body"]["items"]
    results = []
    for item in items:
        results.append({
            "station": item.get("stationName", "알 수 없음"),
            "pm10": int(item.get("pm10Value")) if str(item.get("pm10Value") or "").isdigit() else 0,
            "pm25": int(item.get("pm25Value")) if str(item.get("pm25Value") or "").isdigit() else 0,
            "grade": GRADE_LABELS.get(item.get("khaiGrade"), "정보없음"),
        })
    results.sort(key=lambda row: row["pm10"], reverse=True)
    return results

def load_sample():
    with open(SAMPLE_PATH, encoding="utf-8") as f:
        return parse_items(json.load(f))

def fetch_air_quality(sido="서울"):
    try:
        raw = fetch_raw(sido)
        return parse_items(raw), "live"
    except Exception:
        return load_sample(), "sample"

if __name__ == "__main__" :
    data = fetch_raw()
    print(data)