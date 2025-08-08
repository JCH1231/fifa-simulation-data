from playwright.sync_api import sync_playwright
import urllib.parse
import json
import statistics
import time
import random
import os

season_some = "289,283,284,274,270,839,836,840,829,268,265,828,827,264,835,826,825,811,821,281,256,818,814,252,251,813,802,253,801,290,246,237,291,216,233,231,254,249,100,832,831,844,830,234,834"
season_high_enc = "835,811,826,825,844,831,818,827,828,829,836,840,834,283,832"

url_tpl = (
    "https://fconline.nexon.com/datacenter/index?"
    "strSeason={season_enc}"
    "&n1Confederation=0&n4LeagueId=0"
    "&strPosition=&strPhysical=&preferredfoot=0"
    "&n1FootAblity=0&n1SkillMove=0&n1InterationalRep=0"
    "&n4BirthMonth=0&n4BirthDay=0&n4TeamId=0&n4NationId=0"
    "&strAbility1=&strAbility2=&strAbility3="
    "&strTrait1=&strTrait2=&strTrait3="
    "&strTraitNon1=&strTraitNon2=&strTraitNon3="
    "&n1Grow=0&n1TeamColor=0"
    "&strSkill1=sprintspeed&strSkill2=acceleration"
    "&strSkill3=strength&strSkill4=stamina"
    "&n4OvrMin={ovr}&n4OvrMax={ovr}"
    "&n4SalaryMin=4&n4SalaryMax=99"
    "&n1Ability1Min=40&n1Ability1Max=200"
    "&n1Ability2Min=40&n1Ability2Max=200"
    "&n1Ability3Min=40&n1Ability3Max=200"
    "&n4BirthYearMin=1900&n4BirthYearMax=2010"
    "&n4HeightMin=140&n4HeightMax=208"
    "&n4WeightMin=40&n4WeightMax=110"
    "&n4AvgPointMin=0&n4AvgPointMax=10"
    "&n1Strong={grade}"
)

def format_price(won):
    if won is None:
        return "0"
    cho = won // 10**12
    eo = (won % 10**12) // 10**8
    man = (won % 10**8) // 10**4
    parts = []
    if cho > 0:
        parts.append(f"{cho}조")
    if eo > 0:
        parts.append(f"{eo}억")
    if man > 0:
        parts.append(f"{man}만")
    return " ".join(parts) if parts else "0"

def parse_price(alt):
    """문자열에서 숫자/콤마만 남겨 정수로 변환"""
    if not alt:
        return None
    s = "".join(ch for ch in str(alt) if ch.isdigit() or ch == ",")
    if not s:
        return None
    try:
        return int(s.replace(",", ""))
    except Exception:
        return None

def filter_prices(prices, k=1, low=None, high=None):
    """이상치 필터(표본 >=3 일 때만). 평균±k*표준편차, (옵션) 구간필터 동시 적용"""
    if not prices or len(prices) < 3:
        return prices
    mean = statistics.mean(prices)
    stdev = statistics.stdev(prices)
    if stdev == 0:
        return prices
    if low is not None and high is not None:
        filtered = [x for x in prices if abs(x - mean) <= k * stdev and low <= x <= high]
    else:
        filtered = [x for x in prices if abs(x - mean) <= k * stdev]
    return filtered or prices

def filter_by_ovr(ovr, all_prices):
    """OVR 구간별 필터 + 표본 적으면 스킵"""
    if not all_prices:
        return []
    n = len(all_prices)
    if n < 5:
        return all_prices  # 표본이 너무 적으면 필터 스킵

    sorted_prices = sorted(all_prices)
    if 111 <= ovr <= 119:
        take = min(150, n)
        return filter_prices(sorted_prices[:take], k=1)
    elif 120 <= ovr <= 127:
        take = min(20, n)
        return sorted_prices[:take]
    elif 128 <= ovr <= 129:
        take = min(15, n)
        return sorted_prices[:take]
    elif 130 <= ovr <= 134:
        take = min(10, n)
        return sorted_prices[:take]
    elif ovr == 135:
        take = min(5, n)
        return sorted_prices[:take]
    elif 136 <= ovr <= 140:
        take = min(15, n)
        return sorted_prices[:take]
    else:
        return filter_prices(all_prices, k=1)

data = {}

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    # 컨텍스트/페이지 (타임아웃/UA 설정)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    )
    context.set_default_timeout(15_000)  # 15초 기본 타임아웃

    # [추가] 리소스 차단(이미지/폰트/미디어)
    def _router(route):
        r = route.request
        rt = r.resource_type
        if rt in {"image", "font", "media"}:
            return route.abort()
        return route.continue_()
    context.route("**/*", _router)

    page = context.new_page()

    for ovr in range(90, 137):  # 원하는 오버롤 범위 설정
        season_enc = season_some if ovr <= 129 else season_high_enc
        all_prices = []

        # 등급 범위 결정(네 로직 유지)
        if 90 <= ovr <= 112:
            min_grade, max_grade = 1, 1
        elif 113 <= ovr <= 114:
            min_grade, max_grade = 2, 7
        elif 115 <= ovr <= 119:
            min_grade, max_grade = 4, 7
        elif 120 <= ovr <= 125:
            min_grade, max_grade = 6, 8
        elif 126 <= ovr <= 134:
            min_grade, max_grade = 7, 8
        else:
            min_grade, max_grade = 9, 9  # 가드

        for grade in range(min_grade, max_grade + 1):
            url = url_tpl.format(season_enc=season_enc, ovr=ovr, grade=grade)

            try:
                page.goto(url, wait_until="networkidle")
                # divPlayerList 나타날 때까지 대기
                page.wait_for_selector("#divPlayerList", state="visible", timeout=10_000)

                # 등급 드롭다운 클릭은 기본 비활성화(파라미터로 적용되는 경우가 많음)
                need_click = False

                # [추가] 파라미터 적용 실패 시 클릭 폴백
                use_click = False
                try:
                    page.wait_for_selector(f"#divPlayerList .td_ar_bp .span_bp{grade}", timeout=5_000)
                except Exception:
                    use_click = True

                if need_click or use_click:
                    page.click('div.en_selector_wrap .ability')
                    page.wait_for_selector(f".selector_list a.en_level{grade}", state="visible", timeout=8_000)
                    page.click(f'div.en_selector_wrap .selector_list a.en_level{grade}')
                    page.wait_for_selector(f"#divPlayerList .td_ar_bp .span_bp{grade}", timeout=10_000)
                else:
                    try:
                        page.wait_for_selector(f"#divPlayerList .td_ar_bp .span_bp{grade}", timeout=10_000)
                    except Exception:
                        pass

                # 데이터 로딩 재시도(빈 응답 방지)
                rows = []
                for attempt in range(3):  # 최대 3회 재시도
                    for _ in range(20):
                        rows = page.query_selector_all("#divPlayerList > .tr[onclick]")
                        if rows:
                            break
                        page.wait_for_timeout(500)
                    if rows:
                        break
                    # 완전 새로고침 + 네트워크 안정화
                    page.reload(wait_until="networkidle")
                    page.wait_for_timeout(random.randint(200, 600))
                    try:
                        page.wait_for_selector(f"#divPlayerList .td_ar_bp .span_bp{grade}", timeout=10_000)
                    except Exception:
                        pass

                # 행 파싱
                for row in rows or []:
                    cell = row.query_selector(f'.td_ar_bp .span_bp{grade}')
                    if not cell:
                        continue
                    alt = cell.get_attribute("alt")
                    price = parse_price(alt)
                    if not price:
                        title = cell.get_attribute("title")
                        price = parse_price(title)
                    if not price:
                        txt = (cell.inner_text() or "").strip()
                        price = parse_price(txt)
                    if price:
                        all_prices.append(price)

            except Exception:
                # 이 grade는 스킵
                continue

        print(f"OVR {ovr} raw prices (n={len(all_prices)}):", all_prices[:10], "..." if len(all_prices) > 10 else "")

        # OVR 구간별 필터
        filtered_prices = filter_by_ovr(ovr, all_prices)
        print(f"OVR {ovr} filtered (n={len(filtered_prices)}):", filtered_prices[:10], "..." if len(filtered_prices) > 10 else "")

        if filtered_prices:
            avg_price = sum(filtered_prices) // len(filtered_prices)
            data[ovr] = avg_price
            print(f"{ovr} OVR 전체 평균(이상치 제거): {format_price(avg_price)}")
        else:
            data[ovr] = None
            print(f"{ovr} OVR 전체 평균(이상치 제거): 데이터 없음")

        # 중간 저장(변화 있을 때만)
        try:
            with open("average.json", "r", encoding="utf-8") as _f:
                _old = json.load(_f)
        except Exception:
            _old = {}
        if _old.get(str(ovr)) != data.get(ovr):
            with open("average.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    context.close()
    browser.close()

# 최종 저장(포맷 동일)
with open("average.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print("average.json 저장 완료!")
