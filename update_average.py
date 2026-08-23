# -*- coding: utf-8 -*-
"""OVR 별 평균 재료 시세를 뽑아 average.json 으로 저장한다. (2시간마다 실행)

[Playwright 를 걷어낸 이유]
예전 버전은 datacenter/index 페이지를 헤드리스 크롬으로 열고, 로딩을 기다리고, 스크롤하고,
그 사이사이 sleep_jitter() 로 3.7~5.2초씩 쉬면서 (OVR, 강화등급) 조합마다 페이지를 새로 띄웠다.
176개 조합 × 6초 ≈ 20분. CI 에서는 여기에 크롬 설치 시간까지 붙었다.

실측해보니 목록은 그냥 POST 로 나온다:
    POST /DataCenter/PlayerList  (n1Strong, n4OvrMin, n4OvrMax, ...)
한 응답에 200행이 ~950ms 만에 오고, 각 행에는 span_bp1~span_bp13 (강화 단계별 시세)가 전부
들어있다. 브라우저가 할 일이 애초에 없었다. 같은 176개 조합을 병렬로 돌면 20초면 끝난다.

[주의] 토큰 쿠키는 부트스트랩 GET 에 X-Requested-With 를 붙이면 서버가 안 내려준다.
그 헤더는 POST 에만 붙인다.

평균을 내는 구간/필터링 규칙은 예전과 100% 동일하게 유지했다 — average.json 값이 달라지면
이 파일을 쓰는 deals_crawler.py 와 앱의 시뮬레이션 결과가 같이 흔들리기 때문이다.
"""

import json
import re
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "https://fconline.nexon.com"
LIST_URL = f"{BASE}/DataCenter/PlayerList"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
XHR = {"X-Requested-With": "XMLHttpRequest"}
WORKERS = 12

SEASON_SOME = (",100,101,113,114,111,596,864,863,862,861,852,851,848,850,846,845,840,839,836,"
               "284,283,289,291,290,801,802,813,814,818,821,825,826,827,828,829,268,858,835,"
               "811,854,855,831,832,807,808,866,844,820,856,834,518,516,514,853,830,512,265,"
               "270,252,256,251,246,237,234,")
SEASON_HIGH = SEASON_SOME

# 원본 url_tpl 에 있던 고정 파라미터를 그대로 옮겼다(검색 조건이 달라지면 표본이 달라진다).
FIXED = {
    "n8PlayerGrade1Min": 0, "n8PlayerGrade1Max": 10000000000,
    "n1Confederation": 0, "n4LeagueId": 0,
    "strPosition": "", "strPhysical": "", "preferredfoot": 0,
    "n1FootAblity": 0, "n1SkillMove": 0, "n1InterationalRep": 0,
    "n4BirthMonth": 0, "n4BirthDay": 0, "n4TeamId": 0, "n4NationId": 0,
    "strAbility1": "", "strAbility2": "", "strAbility3": "",
    "strTrait1": "", "strTrait2": "", "strTrait3": "",
    "strTraitNon1": "", "strTraitNon2": "", "strTraitNon3": "",
    "n1Grow": 0, "n1TeamColor": 0,
    "strSkill1": "sprintspeed", "strSkill2": "acceleration",
    "strSkill3": "strength", "strSkill4": "stamina",
    "strSearchStatus": "off", "strOrderby": "",
    "teamcolorid": 0, "strTeamColorCategory": "",
    "n1History": 0, "n4PlayYear": 0, "IsSummaryPlayer": 0,
    "strPlayerName": "", "strTeamName": "", "strNationName": "", "strTeamColorName": "",
    "n4SalaryMin": 4, "n4SalaryMax": 99,
    "n1Ability1Min": 40, "n1Ability1Max": 200,
    "n1Ability2Min": 40, "n1Ability2Max": 200,
    "n1Ability3Min": 40, "n1Ability3Max": 200,
    "n4BirthYearMin": 1900, "n4BirthYearMax": 2010,
    "n4HeightMin": 140, "n4HeightMax": 208,
    "n4WeightMin": 40, "n4WeightMax": 110,
    "n4AvgPointMin": 0, "n4AvgPointMax": 10,
}

_tls = threading.local()
# BeautifulSoup 으로 200행 × 176응답을 파싱하면 그게 곧 병목이라, 필요한 조각만 정규식으로 집는다.
# <span class="span_bp5" style="display:none" alt="24,600" title="24,600">
_RX = {g: re.compile(r'class="[^"]*\bspan_bp%d\b[^"]*"[^>]*\balt="([0-9,]+)"' % g)
       for g in range(1, 14)}


def _new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": f"{BASE}/datacenter/index"})
    s.mount("https://", requests.adapters.HTTPAdapter(
        pool_connections=4, pool_maxsize=4, max_retries=0))
    s.get(f"{BASE}/", timeout=20)          # ← XHR 헤더 없이! 있으면 토큰 쿠키를 안 준다
    tok = s.cookies.get("__RequestVerificationToken")
    if not tok:
        raise RuntimeError("__RequestVerificationToken 을 못 받았습니다")
    return s, tok


def _session():
    if not hasattr(_tls, "s"):
        _tls.s, _tls.tok = _new_session()
    return _tls.s, _tls.tok


def fetch_prices(ovr, grade, retries=3):
    """(OVR, 강화등급) 조합의 매물 시세 목록. 원본의 '.td_ar_bp .span_bp{grade}' alt 와 동일."""
    season = SEASON_SOME if ovr <= 129 else SEASON_HIGH
    for attempt in range(retries):
        try:
            s, tok = _session()
            data = dict(FIXED)
            data.update({
                "__RequestVerificationToken": tok,
                "strSeason": season,
                "n1Strong": grade,
                "n4OvrMin": ovr, "n4OvrMax": ovr,
            })
            r = s.post(LIST_URL, headers=XHR, data=data, timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f"status {r.status_code}")
            return [int(x.replace(",", "")) for x in _RX[grade].findall(r.text)]
        except Exception:
            if attempt == retries - 1:
                return []
            _tls.s, _tls.tok = _new_session()
            time.sleep(0.4 * (attempt + 1))
    return []


# ── 아래 두 함수와 등급/필터 구간은 원본 그대로 ────────────────────────────
def format_price(won):
    if won is None:
        return "0"
    cho, eo = won // 10**12, (won % 10**12) // 10**8
    man, gyeong = (won % 10**8) // 10**4, (won % 10**4) // 1
    parts = []
    if cho > 0:
        parts.append(f"{cho}조")
    if eo > 0:
        parts.append(f"{eo}억")
    if man > 0:
        parts.append(f"{man}만")
    if gyeong > 0:
        parts.append(f"{gyeong}경")
    return " ".join(parts) if parts else "0"


def filter_prices(prices, k=1, low=None, high=None):
    if not prices or len(prices) < 2:
        return prices
    try:
        mean, stdev = statistics.mean(prices), statistics.stdev(prices)
    except statistics.StatisticsError:
        return prices
    if stdev == 0:
        return prices
    if low is not None and high is not None:
        filtered = [x for x in prices if abs(x - mean) <= k * stdev and low <= x <= high]
    else:
        filtered = [x for x in prices if abs(x - mean) <= k * stdev]
    return filtered or prices


def grade_range(ovr):
    if 90 <= ovr <= 112:
        return 1, 1
    if 113 <= ovr <= 119:
        return 4, 7
    if 120 <= ovr <= 124:
        return 2, 8
    if 125 <= ovr <= 130:
        return 4, 8
    if 131 <= ovr <= 145:
        return 6, 10
    return 9, 9


def pick(ovr, all_prices):
    if 111 <= ovr <= 119:
        return filter_prices(sorted(all_prices)[:150], k=1)
    if 120 <= ovr <= 127:
        return sorted(all_prices)[:100]
    if 128 <= ovr <= 129:
        return sorted(all_prices)[:50]
    if 130 <= ovr <= 134:
        return sorted(all_prices)[:30]
    if 135 <= ovr <= 150:
        return sorted(all_prices)[:20]
    return filter_prices(all_prices, k=1)


def main():
    ovrs = list(range(100, 145))
    jobs = []
    for o in ovrs:
        lo, hi = grade_range(o)
        jobs += [(o, g) for g in range(lo, hi + 1)]
    print(f"조합 {len(jobs)}개 ({WORKERS}스레드) — 예전 방식은 여기서 페이지를 그만큼 띄웠다")

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(lambda j: fetch_prices(*j), jobs))
    el = time.perf_counter() - t0
    print(f"수집 완료 {el:.1f}초")

    by_ovr = {o: [] for o in ovrs}
    for (o, _g), prices in zip(jobs, results):
        by_ovr[o].extend(prices)

    data, empty = {}, []
    for o in ovrs:
        raw = by_ovr[o]
        filtered = pick(o, raw) if raw else []
        if filtered:
            data[o] = sum(filtered) // len(filtered)
            print(f"  OVR {o}: 매물 {len(raw):4d} -> 표본 {len(filtered):3d} "
                  f"-> 평균 {format_price(data[o])}")
        else:
            data[o] = None
            empty.append(o)
            print(f"  OVR {o}: 데이터 없음")

    if empty:
        print(f"[경고] 값이 비어있는 OVR: {empty}")
    # 전부 실패했다면 덮어쓰지 않는다 — CI 가 average.json 을 0으로 밀어버리면
    # deals_crawler 와 앱 시뮬레이션이 통째로 망가진다.
    if len(empty) == len(list(ovrs)):
        print("[중단] 수집이 전부 실패해서 average.json 을 건드리지 않습니다.")
        return 1

    with open("average.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"average.json 저장 완료! (총 {time.perf_counter() - t0:.1f}초)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
