# -*- coding: utf-8 -*-
"""게이지 테이블 크롤러 — Playwright 제거 + 병렬화 버전.

기존 2단계 방식(ove_test_crawler.py → fifa_ovr_test.py)을 한 스크립트로 합치고 다시 짰다.

[기존 방식의 병목 — 실측]
  1단계 ove_test_crawler.py
    (등급, OVR) 조합마다 Playwright로 페이지를 열고 → networkidle 대기 → 드롭다운 클릭 →
    행 로딩 폴링 → **전체화면 스크린샷 저장**까지 했다. 1,000 조합 × 2초 이상.
  2단계 fifa_ovr_test.py
    게이지 API를 순차로 81,000번 호출하면서 매번 time.sleep(0.07).
    sleep만 합쳐서 94분, 왕복 지연까지 하면 6시간 이상.

[측정으로 확인한 사실]
  - 데이터센터 목록은 POST /DataCenter/PlayerList 로 그냥 나온다.
    n1Strong / n4OvrMin / n4OvrMax 필터가 정상 동작하고 200행이 1초에 온다. 브라우저 불필요.
  - 게이지 API(PlayerGrowCalApi)는 원래 건당 32ms로 빨랐다. 느렸던 건 sleep 때문.
    병렬 8스레드에서 건당 9ms, 16스레드에서 6ms, 실패 0건.
  - 게이지 값은 spid 자체가 아니라 "그 spid가 해당 등급에서 갖는 표시 OVR"에만 의존한다.
    (같은 OVR의 다른 선수 4명이 전부 같은 값을 반환하는 것을 확인)
    → 1단계는 (등급, OVR)마다 대표 spid 하나만 구하면 충분하다.

[사용법]
    python gauge_crawler.py                 # 1단계 + 2단계 전부
    python gauge_crawler.py --only 1        # spid 테이블만
    python gauge_crawler.py --only 2        # 기존 spid 테이블로 게이지만
    python gauge_crawler.py --workers 16    # 더 빠르게(서버 부하 주의)
    python gauge_crawler.py --resume        # 중단된 지점부터 이어서

중간 결과를 계속 체크포인트로 저장하므로 중단해도 --resume 으로 이어받을 수 있다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

BASE = "https://fconline.nexon.com"
LIST_URL = f"{BASE}/DataCenter/PlayerList"
GAUGE_URL = f"{BASE}/DataCenter/PlayerGrowCalApi"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

HERE = os.path.dirname(os.path.abspath(__file__))
SPID_OUT = os.path.join(HERE, "ovr_spid_table.json")
GAUGE_OUT = os.path.join(HERE, "gauge_table_60_140.json")
GAUGE_CKPT = os.path.join(HERE, ".gauge_checkpoint.json")

# 행 컨테이너 id 에서 spid 를 뽑는다: <div id="area_playerunit_101000240">
_RE_UNIT = re.compile(r'id="area_playerunit_(\d+)"')

_tls = threading.local()


# ── 세션 (스레드마다 하나. 토큰이 세션 쿠키라 공유하면 안 된다) ──────────────
# POST 에만 붙인다. 부트스트랩 GET 에 X-Requested-With 를 달면 서버가 AJAX 요청으로 보고
# __RequestVerificationToken 쿠키를 아예 내려주지 않는다(실측 확인).
XHR = {"X-Requested-With": "XMLHttpRequest"}


def _new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": f"{BASE}/datacenter/index"})
    s.mount("https://", requests.adapters.HTTPAdapter(
        pool_connections=4, pool_maxsize=4, max_retries=0))
    token = None
    for url in (f"{BASE}/", f"{BASE}/datacenter/index"):
        try:
            s.get(url, timeout=20)
        except Exception:
            continue
        token = s.cookies.get("__RequestVerificationToken")
        if token:
            break
    if not token:
        raise RuntimeError("__RequestVerificationToken 을 못 받았습니다")
    return s, token


def _session():
    """스레드 전용 (세션, 토큰). 토큰이 만료되면 _refresh() 로 새로 받는다."""
    if not hasattr(_tls, "sess"):
        _tls.sess, _tls.token = _new_session()
    return _tls.sess, _tls.token


def _refresh():
    _tls.sess, _tls.token = _new_session()
    return _tls.sess, _tls.token


# ── 1단계: (등급, OVR) → 대표 spid ──────────────────────────────────────
def fetch_spid(grade: int, ovr: int, retries: int = 3):
    """해당 등급에서 표시 OVR이 정확히 `ovr` 인 선수 하나의 spid.

    기존 크롤러는 이 한 건을 위해 브라우저 페이지를 통째로 띄웠지만, 목록은 평범한
    POST 응답이라 HTML에서 행 컨테이너 id 만 긁으면 된다.
    """
    for attempt in range(retries):
        try:
            s, tok = _session()
            r = s.post(LIST_URL, timeout=30, headers=XHR, data={
                "__RequestVerificationToken": tok,
                "n1Strong": grade,
                "n4OvrMin": ovr,
                "n4OvrMax": ovr,
            })
            if r.status_code != 200:
                raise RuntimeError(f"status {r.status_code}")
            ids = _RE_UNIT.findall(r.text)
            return int(ids[0]) if ids else None
        except Exception:
            if attempt == retries - 1:
                return None
            _refresh()
            time.sleep(0.3 * (attempt + 1))
    return None


def stage1(grades, ovrs, workers):
    print(f"[1단계] spid 테이블 — 등급 {grades[0]}~{grades[-1]}, "
          f"OVR {ovrs[0]}~{ovrs[-1]} ({len(grades) * len(ovrs)}건, {workers}스레드)")
    table = {str(g): {} for g in grades}
    jobs = [(g, o) for g in grades for o in ovrs]
    done = 0
    t0 = time.perf_counter()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_spid, g, o): (g, o) for g, o in jobs}
        for fut in as_completed(futs):
            g, o = futs[fut]
            spid = fut.result()
            if spid:
                table[str(g)][str(o)] = spid
            done += 1
            if done % 50 == 0 or done == len(jobs):
                el = time.perf_counter() - t0
                print(f"  {done}/{len(jobs)}  {el:5.1f}s  "
                      f"({done / max(el, .001):.0f}건/s)", flush=True)

    with open(SPID_OUT, "w", encoding="utf-8") as f:
        json.dump(table, f, ensure_ascii=False, indent=2)

    total = sum(len(v) for v in table.values())
    print(f"[1단계] 완료 {time.perf_counter() - t0:.1f}s — {total}개 spid → {SPID_OUT}")
    for g in grades:
        got = table[str(g)]
        missing = [o for o in ovrs if str(o) not in got]
        tag = "" if not missing else f"  없음 {len(missing)}개: {missing[:6]}{'...' if len(missing) > 6 else ''}"
        print(f"    등급{g:>3}: {len(got):3d}/{len(ovrs)}{tag}")
    return table


# ── 2단계: (등급, 대상OVR, 재료OVR) → 게이지 % ──────────────────────────
def fetch_gauge(spid: int, grade: int, material_ovr: int, retries: int = 3):
    for attempt in range(retries):
        try:
            s, tok = _session()
            payload = {
                "n8SpId": spid,
                "n1Strong": grade,
                "strCalinfo": f"{material_ovr},",
                "n4BoostPer": 0,
                "__RequestVerificationToken": tok,
            }
            for i in range(5):
                payload[f"strPrice{i + 1}"] = 0
            r = s.post(GAUGE_URL, data=payload, timeout=30,
                       headers={**XHR, "Content-Type": "application/x-www-form-urlencoded"})
            if "<!doctype html>" in r.text.lower():
                raise RuntimeError("HTML 에러 페이지")
            return float(r.json().get("strBoostPer", "0"))
        except Exception:
            if attempt == retries - 1:
                return None
            _refresh()
            time.sleep(0.3 * (attempt + 1))
    return None


def crawl_row(spid, grade, materials, known):
    """한 행(등급 g, 대상OVR t)의 재료별 게이지를 채운다.

    실측한 두 가지 성질을 이용해 요청 수를 절반 이하로 줄인다:
      · 한 행 안에서 재료 OVR 이 커질수록 게이지가 단조 증가한다(850개 행 전수 검사, 위반 0).
      · 전체 칸의 68%가 포화값이다(0.0 이 17%, 100.0 이 51%).
    그래서 "처음으로 0을 벗어나는 지점"과 "처음으로 100에 닿는 지점"만 이분탐색으로 찾고,
    그 바깥은 요청 없이 0.0 / 100.0 으로 채운다. 가운데 구간만 실제로 조회한다.

    반환: (값 dict, 실제 요청 횟수). 조회에 실패하면 (None, 요청횟수).
    """
    n = len(materials)
    got = dict(known)          # --resume 으로 이미 받아둔 값은 그대로 재사용
    calls = 0

    def val(i):
        nonlocal calls
        k = str(materials[i])
        if k not in got:
            v = fetch_gauge(spid, grade, materials[i])
            if v is None:
                return None
            got[k] = v
            calls += 1
        return got[k]

    def first_true(pred):
        """단조 조건에서 pred 가 처음 참이 되는 인덱스(없으면 n). 실패 시 None."""
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) // 2
            v = val(mid)
            if v is None:
                return None
            if pred(v):
                hi = mid
            else:
                lo = mid + 1
        return lo

    start = first_true(lambda v: v > 0.0)        # 이 앞은 전부 0.0
    end = first_true(lambda v: v >= 100.0)       # 이 지점부터는 전부 100.0
    if start is None or end is None:
        return None, calls

    for i in range(n):
        k = str(materials[i])
        if i < start:
            got[k] = 0.0
        elif i >= end:
            got[k] = 100.0
        elif k not in got:
            v = val(i)
            if v is None:
                return None, calls
    return got, calls


def stage2(spid_table, grades, targets, materials, workers, resume):
    result = {}
    if resume and os.path.exists(GAUGE_CKPT):
        with open(GAUGE_CKPT, encoding="utf-8") as f:
            result = json.load(f)
        have = sum(len(v2) for v in result.values() for v2 in v.values())
        print(f"[2단계] 체크포인트에서 이어받기 — 이미 {have}칸")

    # 이제 작업 단위가 '칸'이 아니라 '행'이다. 이분탐색이 행 안에서 순차적이어야 하므로
    # 병렬화는 행 단위로 한다(동시 요청 수 = 스레드 수라 서버 부하는 그대로).
    jobs = []
    for g in grades:
        gs = str(g)
        result.setdefault(gs, {})
        for t in targets:
            ts = str(t)
            spid = (spid_table.get(gs) or {}).get(ts)
            if not spid:
                continue
            row = result[gs].setdefault(ts, {})
            if len(row) < len(materials):
                jobs.append((gs, ts, int(spid), g))

    if not jobs:
        print("[2단계] 할 일이 없습니다(이미 전부 채워짐)")
        return result

    full = len(jobs) * len(materials)
    print(f"[2단계] {len(jobs)}행 × 재료 {len(materials)}종 = {full}칸, {workers}스레드")
    print("        (포화 구간은 이분탐색으로 건너뜁니다)")
    t0 = time.perf_counter()
    done = fails = calls_total = 0
    lock = threading.Lock()

    def work(job):
        gs, ts, spid, g = job
        got, calls = crawl_row(spid, g, materials, result[gs][ts])
        return job, got, calls

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(work, j) for j in jobs]):
            (gs, ts, _spid, _g), got, calls = fut.result()
            with lock:
                calls_total += calls
                if got is None:
                    fails += 1
                else:
                    result[gs][ts] = got
                done += 1
                if done % 50 == 0 or done == len(jobs):
                    el = time.perf_counter() - t0
                    eta = (len(jobs) - done) / max(done / max(el, .001), .001)
                    print(f"  {done}/{len(jobs)}행  {el:5.1f}s  "
                          f"(요청 {calls_total}회, 남은시간 {eta / 60:4.1f}분, "
                          f"실패행 {fails})", flush=True)
                    with open(GAUGE_CKPT, "w", encoding="utf-8") as f:
                        json.dump(result, f, ensure_ascii=False)
    print(f"  실제 요청 {calls_total}회 / 전체 {full}칸 "
          f"= {100 * calls_total / max(full, 1):.0f}%")

    with open(GAUGE_OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    if os.path.exists(GAUGE_CKPT):
        os.remove(GAUGE_CKPT)

    el = time.perf_counter() - t0
    cells = sum(len(v2) for v in result.values() for v2 in v.values())
    print(f"[2단계] 완료 {el / 60:.1f}분 — {cells}칸 (실패 {fails}) → {GAUGE_OUT}")
    return result


# ── 검증: 앱이 실제로 읽는 형태로 구멍이 없는지 확인 ─────────────────────
def verify(table, grades, targets, materials):
    print("\n[검증] 앱 조회 형식 gauge_table[등급][대상OVR][재료OVR]")
    holes = 0
    for g in grades:
        gs = str(g)
        rows = table.get(gs, {})
        full = sum(1 for t in targets if len(rows.get(str(t), {})) == len(materials))
        empty_rows = [t for t in targets if not rows.get(str(t))]
        holes += len(empty_rows)
        tag = "" if not empty_rows else f"  빈 대상OVR {len(empty_rows)}개"
        print(f"    등급{g:>3}: 완전한 행 {full:3d}/{len(targets)}{tag}")
    print(f"[검증] 대상OVR 구멍 총 {holes}개"
          + ("  (해당 등급/OVR 선수가 실제로 없는 경우 정상)" if holes else ""))


def main():
    ap = argparse.ArgumentParser(description="FIFA 게이지 테이블 크롤러")
    ap.add_argument("--only", choices=["1", "2"], help="한 단계만 실행")
    ap.add_argument("--grades", default="1-11", help="강화 등급 범위 (기본 1-11)")
    ap.add_argument("--ovr", default="60-149", help="OVR 범위 (기본 60-149)")
    # 실측: 게이지 API 는 24 동시요청 부근이 정점이고 그 위로는 오히려 느려진다.
    ap.add_argument("--workers", type=int, default=20, help="동시 요청 수 (기본 20)")
    ap.add_argument("--resume", action="store_true", help="체크포인트에서 이어서")
    args = ap.parse_args()

    def rng(spec):
        a, _, b = spec.partition("-")
        return list(range(int(a), int(b or a) + 1))

    grades, ovrs = rng(args.grades), rng(args.ovr)
    materials = ovrs

    print(f"등급 {grades[0]}~{grades[-1]} / OVR {ovrs[0]}~{ovrs[-1]} / {args.workers}스레드")
    try:
        _new_session()
    except Exception as e:
        print(f"[중단] 넥슨 세션을 열 수 없습니다: {e}")
        return 1

    spid_table = None
    if args.only != "2":
        spid_table = stage1(grades, ovrs, args.workers)

    if args.only == "1":
        return 0

    if spid_table is None:
        if not os.path.exists(SPID_OUT):
            print(f"[중단] {SPID_OUT} 이 없습니다. 먼저 1단계를 실행하세요.")
            return 1
        with open(SPID_OUT, encoding="utf-8") as f:
            spid_table = json.load(f)

    table = stage2(spid_table, grades, ovrs, materials, args.workers, args.resume)
    verify(table, grades, ovrs, materials)
    return 0


if __name__ == "__main__":
    sys.exit(main())
