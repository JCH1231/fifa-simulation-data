from playwright.sync_api import sync_playwright
import urllib.parse
import json
import statistics
import time

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
    if not alt:
        return None
    return int(alt.replace(",", ""))

def filter_prices(prices, k=1, low=None, high=None):
    if not prices or len(prices) < 3:
        return prices
    mean = statistics.mean(prices)
    stdev = statistics.stdev(prices)
    if low is not None and high is not None:
        filtered = [x for x in prices if abs(x - mean) <= k * stdev and low <= x <= high]
    else:
        filtered = [x for x in prices if abs(x - mean) <= k * stdev]
    if not filtered:
        return prices
    return filtered

data = {}
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    for ovr in range(90, 137):  # 원하는 오버롤 범위 설정
        season_enc = season_some if ovr <= 129 else season_high_enc
        all_prices = []

        # 특정 구간 1~5강, 그 외 1~8강
        if 90 <= ovr <= 112:
            min_grade, max_grade = 1, 1
        elif 113 <= ovr <= 114:
            min_grade,max_grade = 2,7
        elif 115 <= ovr <= 119:
            min_grade,max_grade = 4,7
        elif 120 <= ovr <= 125:
            min_grade,max_grade = 6,8
        elif 126 <= ovr <= 134:
            min_grade,max_grade = 7,8
        else:
            min_grade,max_grade = 9,9

        for grade in range(min_grade, max_grade + 1):
            url = url_tpl.format(season_enc=season_enc, ovr=ovr, grade=grade)
            page.goto(url, wait_until="networkidle")
            page.wait_for_timeout(3500)  # 기존보다 대기시간 증가

            # 드롭다운 클릭해서 강화 등급 선택
            page.click('div.en_selector_wrap .ability')
            page.wait_for_timeout(1000)  # 기존보다 대기시간 증가
            page.click(f'div.en_selector_wrap .selector_list a.en_level{grade}')
            page.wait_for_timeout(3500)  # 기존보다 대기시간 증가

            # 데이터가 나올 때까지 최대 10초(20번) 반복 체크
            for _ in range(20):
                rows = page.query_selector_all("#divPlayerList > .tr[onclick]")
                if rows:
                    break
                page.wait_for_timeout(500)
            else:
                rows = []

            for row in rows:
                cell = row.query_selector(f'.td_ar_bp .span_bp{grade}')
                if not cell:
                    continue
                alt = cell.get_attribute('alt')
                price = parse_price(alt)
                if price:
                    all_prices.append(price)

        print(f"OVR {ovr} raw prices:", all_prices)

        # 특정 오버롤 구간에만 가격 범위 필터링 적용
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
            min10 = sorted_prices[:15]
            filtered_prices = min10
        elif 130 <= ovr <= 134:
            sorted_prices = sorted(all_prices)
            min10 = sorted_prices[:10]
            filtered_prices = min10
        elif ovr == 135:
            sorted_prices = sorted(all_prices)
            min10 = sorted_prices[:5]
            filtered_prices = min10
        elif 136 <= ovr <= 140:
            sorted_prices = sorted(all_prices)
            min10 = sorted_prices[:15]
            filtered_prices = min10
        else:
            filtered_prices = filter_prices(all_prices, k=1)

        print(f"OVR {ovr} filtered prices:", filtered_prices)

        if filtered_prices:
            avg_price = sum(filtered_prices) // len(filtered_prices)
            data[ovr] = avg_price
            print(f"{ovr} OVR 전체 평균(이상치 제거): {format_price(avg_price)}")
        else:
            data[ovr] = None
            print(f"{ovr} OVR 전체 평균(이상치 제거): 데이터 없음")

    browser.close()

with open("average.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print("average.json 저장 완료!")
