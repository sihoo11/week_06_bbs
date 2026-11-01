from mcp.server import MCPServer
import neis
mcp = MCPServer("school")
@mcp.tool()
def school_meal(school_name: str, date: str) -> str:
    school = neis.get_school_code(school_name)
    if school is None:
        return "학교를 찾지 못했습니다. 정확한 학교 이름(예: 서울과학고등학교)이 필요합니다."
    meal = neis.get_meal(school[0], school[1], date)
    if meal is None:
        return f"{school[2]}의 {date} 급식 정보가 없습니다. (주말, 방학, 휴업일일 수 있음)"
    return (f"[{school[2]} {date} {meal['meal_name']}]"
            f"{meal['menu']}\n칼로리: {meal['calorie']}")


NOT_FOUND = "학교를 찾지 못했습니다. 정확한 학교 이름(예: 서울과학고등학교)이 필요합니다."


@mcp.tool()
def school_timetable(school_name: str, grade: int, class_nm: str) -> str:
    """이번 주(월~금) 시간표를 조회한다."""
    school = neis.get_school(school_name)
    if school is None:
        return NOT_FOUND
    tt = neis.get_week_timetable(school, grade, class_nm)
    if tt is None or not tt["periods"]:
        return f"{school['name']} {grade}학년 {class_nm}반의 이번 주 시간표를 찾지 못했습니다."
    lines = [f"[{school['name']} {grade}학년 {class_nm}반 이번 주 시간표]"]
    for day in tt["days"]:
        subjects = [f"{p}교시 {day['subjects'].get(p, '-')}" for p in tt["periods"]]
        lines.append(f"{day['date']}({day['weekday']}): " + ", ".join(subjects))
    return "\n".join(lines)


@mcp.tool()
def school_schedule(school_name: str, days_ahead: int = 60) -> str:
    """오늘부터 days_ahead일 동안의 학사일정을 조회한다."""
    school = neis.get_school(school_name)
    if school is None:
        return NOT_FOUND
    events = neis.get_school_schedule(school, days_ahead)
    if not events:
        return f"{school['name']}의 향후 {days_ahead}일 학사일정이 없습니다."
    lines = [f"[{school['name']} 학사일정 (향후 {days_ahead}일)]"]
    for e in events:
        d = "오늘" if e["d_day"] == 0 else f"D-{e['d_day']}"
        lines.append(f"{e['date']}({e['weekday']}) {e['name']} [{d}]")
    return "\n".join(lines)


@mcp.tool()
def school_info(school_name: str) -> str:
    """학교 기본 정보(이름, 학교급, 교육청/학교 코드)를 조회한다."""
    school = neis.get_school(school_name)
    if school is None:
        return NOT_FOUND
    return (f"{school['name']} ({school['kind']})\n"
            f"교육청 코드: {school['office_code']}, 학교 코드: {school['school_code']}")


if __name__ == "__main__":
    mcp.run()
