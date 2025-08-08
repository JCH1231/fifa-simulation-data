from playwright.sync_api import sync_playwright
import json
import statistics
import random

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
    if cho > 0: parts.append(f"{cho}조")
    if eo > 0: parts.append(f"{eo}억")
    if man > 0: parts.append(f"{man}만")
    return " ".join(parts) if parts else "0"

def parse_price(s):
    if not s: return None
    s = "".join(ch for ch in str(s) if ch.isdigit() or ch == ",")
    if not s: return None
    try: return int(s.replace(",", ""))
    except: return None

def filter_prices(prices, k=1, low=None, high=None):
    if not prices or len(prices) < 3: return prices
    mean = statistics.mean(prices); stdev = statistics.stdev(prices)
    if stdev == 0: return prices
    if low is not None and high is not None:
        out = [x for x in prices if abs(x-mean) <= k*stdev and low <= x <= high]
    else:
        out = [x for x in prices if abs(x-mean) <= k*stdev]
    return out or prices

def filter_by_ovr(ovr, arr):
    if not arr: return []
    n = len(arr)
    if n < 5: return arr
    arr = sorted(arr)
    if 111 <= ovr <= 119: return filter_prices(arr[:min(150,n)], k=1)
    if 120 <= ovr <= 127: return arr[:min(20,n)]
    if 128 <= ovr <= 129: return arr[:min(15,n)]
    if 130 <= ovr <= 134: return arr[:min(10,n)]
    if ovr == 135:      return arr[:min(5,n)]
    if 136 <= ovr <= 140:return arr[:min(15,n)]
    return filter_prices(arr, k=1)

data = {}

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    )
    context.set_default_timeout(10_000)  # 기본 10초로 다이어트

    # 리소스 차단(이미지/폰트/미디어)
    def _router(route):
        rt = route.request.resource_type
        if rt in {"image", "font", "media"}:
            return route.abort()
        return route.continue_()
    context.route("**/*", _router)

    page = context.new_page()
    page.set_default_navigation_timeout(12_000)

    for ovr in range(90, 137):
        season_enc = season_some if ovr <= 129 else season_high_enc
        all_prices = []

        # 등급 범위
        if 90  <= ovr <= 112: min_g, max_g = 1, 1
        elif 113 <= ovr <= 114: min_g, max_g = 2, 7
        elif 115 <= ovr <= 119: min_g, max_g = 4, 7
        elif 120 <= ovr <= 125: min_g, max_g = 6, 8
        elif 126 <= ovr <= 134: min_g, max_g = 7, 8
        else:                   min_g, max_g = 9, 9

        for grade in range(min_g, max_g + 1):
            url = url_tpl.format(season_enc=season_enc, ovr=ovr, grade=grade)

            try:
                print(f"[START] OVR {ovr} / G{grade}")
                # 빠른 네비 + 필요 요소만 셀렉터로 기다림
                page.goto(url, wait_until="domcontentloaded", timeout=12_000)
                page.wait_for_selector("#divPlayerList", state="visible", timeout=6_000)

                # URL 파라미터 적용 실패 시 한 번만 클릭 폴백
                use_click = False
                try:
                    page.wait_for_selector(f"#divPlayerList .td_ar_bp .span_bp{grade}", timeout=4_000)
                except:
                    use_click = True
                if use_click:
                    page.click('div.en_selector_wrap .ability')
                    page.wait_for_selector(f".selector_list a.en_level{grade}", state="visible", timeout=5_000)
                    page.click(f'div.en_selector_wrap .selector_list a.en_level{grade}')
                    page.wait_for_selector(f"#divPlayerList .td_ar_bp .span_bp{grade}", timeout=6_000)

                # 공백일 때만 1회 재시도
                rows = page.query_selector_all("#divPlayerList > .tr[onclick]")
                if not rows:
                    page.reload(wait_until="domcontentloaded")
                    page.wait_for_selector("#divPlayerList", state="visible", timeout=5_000)
                    rows = page.query_selector_all("#divPlayerList > .tr[onclick]")

                for row in rows or []:
                    cell = row.query_selector(f'.td_ar_bp .span_bp{grade}')
                    if not cell: continue
                    price = parse_price(cell.get_attribute("alt")) \
                            or parse_price(cell.get_attribute("title")) \
                            or parse_price((cell.inner_text() or "").strip())
                    if price: all_prices.append(price)

                print(f"[DONE ] OVR {ovr} / G{grade} rows={len(rows)} prices={len(all_prices)}")

            except Exception:
                print(f"[SKIP ] OVR {ovr} / G{grade}")
                continue

        print(f"OVR {ovr} raw (n={len(all_prices)}):", all_prices[:10], "..." if len(all_prices) > 10 else "")
        filtered = filter_by_ovr(ovr, all_prices)
        print(f"OVR {ovr} filtered (n={len(filtered)}):", filtered[:10], "..." if len(filtered) > 10 else "")

        data[ovr] = (sum(filtered)//len(filtered)) if filtered else None
        if filtered:
            print(f"{ovr} OVR 평균: {format_price(data[ovr])}")
        else:
            print(f"{ovr} OVR 평균: 데이터 없음")

        # 변화 있을 때만 저장
        try:
            with open("average.json", "r", encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            old = {}
        if old.get(str(ovr)) != data.get(ovr):
            with open("average.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    context.close(); browser.close()

with open("average.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print("average.json 저장 완료!")
