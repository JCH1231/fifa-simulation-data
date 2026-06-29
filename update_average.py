from playwright.sync_api import sync_playwright
import urllib.parse
import json
import statistics
import time
import random

season_some = ",100,101,113,114,111,596,864,863,862,861,852,851,848,850,846,845,840,839,836,284,283,289,291,290,801,802,813,814,818,821,825,826,827,828,829,268,858,835,811,854,855,831,832,807,808,866,844,820,856,834,518,516,514,853,830,512,265,270,252,256,251,246,237,234,"
season_high_enc = ",100,101,113,114,111,596,864,863,862,861,852,851,848,850,846,845,840,839,836,284,283,289,291,290,801,802,813,814,818,821,825,826,827,828,829,268,858,835,811,854,855,831,832,807,808,866,844,820,856,834,518,516,514,853,830,512,265,270,252,256,251,246,237,234,"

url_tpl = (
    "https://fconline.nexon.com/datacenter/index?"
    "n8PlayerGrade1Min=0&n8PlayerGrade1Max=100000000"
    "&n1Confederation=0&n4LeagueId=0"
    "&strSeason={season_enc}"
    "&strPosition=&strPhysical=&preferredfoot=0"
    "&n1FootAblity=0&n1SkillMove=0&n1InterationalRep=0"
    "&n4BirthMonth=0&n4BirthDay=0&n4TeamId=0&n4NationId=0"
    "&strAbility1=&strAbility2=&strAbility3="
    "&strTrait1=&strTrait2=&strTrait3="
    "&strTraitNon1=&strTraitNon2=&strTraitNon3="
    "&n1Strong={grade}"
    "&n1Grow=0&n1TeamColor=0"
    "&strSkill1=sprintspeed&strSkill2=acceleration"
    "&strSkill3=strength&strSkill4=stamina"
    "&strSearchStatus=off"
    "&strOrderby="
    "&teamcolorid=0"
    "&strTeamColorCategory="
    "&n1History=0"
    "&n4PlayYear=0"
    "&IsSummaryPlayer=0"
    "&strPlayerName=&strTeamName=&strNationName=&strTeamColorName="
    "&n4OvrMin={ovr}&n4OvrMax={ovr}"
    "&n4SalaryMin=4&n4SalaryMax=99"
    "&n1Ability1Min=40&n1Ability1Max=200"
    "&n1Ability2Min=40&n1Ability2Max=200"
    "&n1Ability3Min=40&n1Ability3Max=200"
    "&n4BirthYearMin=1900&n4BirthYearMax=2010"
    "&n4HeightMin=140&n4HeightMax=208"
    "&n4WeightMin=40&n4WeightMax=110"
    "&n4AvgPointMin=0&n4AvgPointMax=10"
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
    if not alt:
        return None
    return int(alt.replace(",", ""))

def filter_prices(prices, k=1, low=None, high=None):
    if not prices or len(prices) < 2: # 1개 이하면 필터링 불가
        return prices
    try:
        mean = statistics.mean(prices)
        stdev = statistics.stdev(prices)
    except statistics.StatisticsError: # 데이터가 모두 같을 경우 stdev=0 오류 방지
        return prices

    if stdev == 0: # 모든 가격이 같으면 필터링 불필요
        return prices

    if low is not None and high is not None:
        filtered = [x for x in prices if abs(x - mean) <= k * stdev and low <= x <= high]
    else:
        filtered = [x for x in prices if abs(x - mean) <= k * stdev]
    if not filtered: # 필터링 결과 아무것도 안 남으면 원본 반환
        return prices
    return filtered

def safe_goto(page, url, attempts=3):
    for i in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded")
            return True
        except Exception:
            time.sleep(2 * (i + 1))
    return False

def wait_rows_or_reload(page, grade, reload_attempts=2):
    sel_rows = "div.tr .td_ar_bp"
    for r in range(reload_attempts + 1):
        try:
            page.wait_for_selector(sel_rows, timeout=10000)
            return True
        except Exception:
            if r < reload_attempts:
                try:
                    page.reload(wait_until="domcontentloaded")
                    time.sleep(1.5 * (r + 1))
                except Exception:
                    break
            else:
                return False
    return False

def sleep_jitter(base_ms=1200, spread_ms=800):
    time.sleep((base_ms + random.randint(0, spread_ms)) / 1000.0)

data = {}
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ),
        locale="ko-KR",
        timezone_id="Asia/Seoul",
    )
    page = context.new_page()
    page.set_default_timeout(20000)
    page.set_default_navigation_timeout(30000)

    for ovr in range(100, 151):
        season_enc = season_some if ovr <= 129 else season_high_enc
        all_prices = []

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
            min_grade, max_grade = 9, 9

        for grade in range(min_grade, max_grade + 1):
            url = url_tpl.format(season_enc=season_enc, ovr=ovr, grade=grade)

            if not safe_goto(page, url, attempts=3):
                sleep_jitter()
                continue
            sleep_jitter(1400, 900)

            ok = wait_rows_or_reload(page, grade, reload_attempts=2)
            if not ok:
                # wait_rows_or_reload 실패 시 경고 출력 (유지)
                print(f"[warn] OVR {ovr} grade {grade}: wait_rows_or_reload failed.")
                continue

            sleep_jitter(800, 500)
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass

            sleep_jitter(1000, 600) # 스크롤 후 잠시 대기

            rows_selector = "div.tr"
            rows = page.query_selector_all(rows_selector)

            if not rows:
                continue # 다음 등급으로

            for row in rows:
                cell_selector = f'.td_ar_bp .span_bp{grade}'
                cell = row.query_selector(cell_selector)
                if not cell:
                    continue
                alt = cell.get_attribute('alt')
                price = parse_price(alt)
                if price:
                    all_prices.append(price)

        # 원래 있던 결과 출력 (유지)
        print(f"OVR {ovr} raw prices ({len(all_prices)} items):", all_prices)

        # 가격 필터링
        if 111 <= ovr <= 119:
            sorted_prices = sorted(all_prices)
            min80 = sorted_prices[:150]
            filtered_prices = filter_prices(min80, k=1)
        elif 120 <= ovr <= 127:
            sorted_prices = sorted(all_prices)
            min10 = sorted_prices[:20]
            filtered_prices = min10
        elif 128 <= ovr <= 129:
            sorted_prices = sorted(all_prices)
            min10 = sorted_prices[:20]
            filtered_prices = min10
        elif 130 <= ovr <= 134:
            sorted_prices = sorted(all_prices)
            min10 = sorted_prices[:20]
            filtered_prices = min10
        elif ovr == 135:
            sorted_prices = sorted(all_prices)
            min10 = sorted_prices[:20]
            filtered_prices = min10
        elif 136 <= ovr <= 150:
            sorted_prices = sorted(all_prices)
            min10 = sorted_prices[:20]
            filtered_prices = min10
        else:
            filtered_prices = filter_prices(all_prices, k=1)

        # 원래 있던 결과 출력 (유지)
        print(f"OVR {ovr} filtered prices ({len(filtered_prices)} items):", filtered_prices)

        if filtered_prices:
            avg_price = sum(filtered_prices) // len(filtered_prices)
            data[ovr] = avg_price
            # 원래 있던 결과 출력 (유지)
            print(f"==> {ovr} OVR 전체 평균(이상치 제거): {format_price(avg_price)}")
        else:
            data[ovr] = None
            # 원래 있던 결과 출력 (유지)
            print(f"==> {ovr} OVR 전체 평균(이상치 제거): 데이터 없음")

        sleep_jitter(1600, 1200)

    context.close()
    browser.close()

with open("average.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

# 원래 있던 완료 메시지 (유지)
print("average.json 저장 완료!")