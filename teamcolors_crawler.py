# -*- coding: utf-8 -*-
"""팀컬러 목록/레벨별 효과/소속 선수를 수집해 team_colors.json 으로 저장한다.

[예전 방식의 문제 — 전부 실측으로 확인]
 1) 목록을 사람이 손으로 저장한 로컬 HTML(`!DOCTYPE html.txt`)에서 읽었다.
    그래서 팀컬러가 추가돼도 그 파일을 다시 저장하기 전엔 반영이 안 됐다.
    실제로 저장된 데이터는 539개인데 지금 사이트에는 799개가 있다(260개 누락).
 2) 최고 레벨만 긁고 1~3단계는 하드코딩된 규칙으로 **추측**했다.
    120개를 실제 상세 페이지와 대조해보니 3개가 틀렸다
    (2단계 효과를 "전체 능력치 +2" 로 넣지만 실제로는 "+3" 인 팀컬러가 있다).
    지금은 98% 맞지만, 넥슨이 1년에 두 번 정도 단계 규칙을 손대므로 언제든 통째로
    어긋날 수 있는 방식이다. 상세 페이지에 실제 값이 다 있으니 추측할 이유가 없다.
 3) 특성 팀컬러의 선수 목록을 포지션 ID 25개로 나눠 25번 요청했다. 포지션 파라미터를
    아예 빼면 한 번에 전부 온다(5개 팀컬러로 대조 — 결과 집합이 완전히 동일).
 4) 전 과정이 순차 실행이었다.

[지금 방식]
 목록 2회 + 팀컬러당 상세 1회 + 특성 팀컬러당 선수 1회, 전부 병렬.
 요청 수가 12,000회대에서 1,300회대로 줄고 사람 손이 들어갈 곳이 없다.

[사용법]
    python teamcolors_crawler.py
    python teamcolors_crawler.py --dry-run     # 저장 안 하고 비교만
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup

BASE = "https://fconline.nexon.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
XHR = {"X-Requested-With": "XMLHttpRequest"}

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "team_colors.json")

LIST_URL = f"{BASE}/datacenter/teamcolor"
# strTeamColorType=,relation, 으로 거르면 '특성 팀컬러'만 나온다.
RELATION_LIST_URL = f"{BASE}/datacenter/teamcolor?strTeamColorType=%2Crelation%2C"

_RE_ID = re.compile(r"GetTeamColorDetail\((\d+)\)")
_tls = threading.local()


def _session():
    if not hasattr(_tls, "s"):
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Referer": LIST_URL})
        s.mount("https://", requests.adapters.HTTPAdapter(
            pool_connections=4, pool_maxsize=4, max_retries=0))
        _tls.s = s
    return _tls.s


def _get(url, retries=3, **kw):
    for attempt in range(retries):
        try:
            r = _session().get(url, timeout=30, **kw)
            if r.status_code != 200:
                raise RuntimeError(f"status {r.status_code}")
            return r
        except Exception:
            if attempt == retries - 1:
                return None
            _tls.s = None
            time.sleep(0.4 * (attempt + 1))
    return None


def fetch_list(url):
    """목록 페이지에서 {id: {"name":…, "crest":…}} 를 뽑는다.

    crest 는 팀 엠블럼 이미지 URL이다. 앱 스쿼드메이커가 활성 팀컬러를 글자 대신
    엠블럼으로 보여주는 데 쓴다(공식 홈페이지 스쿼드메이커와 같은 방식).
    예전처럼 손으로 저장한 HTML 파일이 필요 없다.
    """
    r = _get(url)
    if r is None:
        return {}
    soup = BeautifulSoup(r.text, "html.parser")
    out = {}
    for item in soup.select(".teamcolor_item"):
        a = item.select_one("a.btn_detail_link")
        name_el = item.select_one(".name")
        if not (a and name_el):
            continue
        m = _RE_ID.search(a.get("onclick") or "")
        if not m:
            continue
        img = item.select_one(".crests img")
        out[m.group(1)] = {
            "name": name_el.get_text(strip=True),
            "crest": (img.get("src") or "") if img else "",
        }
    return out


def fetch_levels(tid):
    """상세 페이지에서 단계별 (필요 인원, 효과들)을 있는 그대로 읽는다.

    마크업: <div class="level lvN"><div class="tit">N 단계</div>
              <div class="desc">M명</div>
              <div class="ap_list"><ul><li>효과</li><li>-</li>…</ul></div>
    비어 있는 칸은 "-" 로 오므로 걸러낸다.
    """
    r = _get(f"{BASE}/DataCenter/TeamColorDetail?teamcolorid={tid}", headers=XHR)
    if r is None:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    levels = []
    for el in soup.select("div.level"):
        tit, desc = el.select_one(".tit"), el.select_one(".desc")
        if not (tit and desc):
            continue
        lv = re.search(r"(\d+)", tit.get_text())
        need = re.search(r"(\d+)", desc.get_text())
        if not (lv and need):
            continue
        effects = [li.get_text(strip=True) for li in el.select(".ap_list li")]
        effects = [e for e in effects if e and e != "-"]
        levels.append({"level": int(lv.group(1)),
                       "required_players": int(need.group(1)),
                       "effects": effects})
    return sorted(levels, key=lambda x: x["level"]) or None


# TeamColorPlayerList 는 한 번에 최대 100명만 돌려준다(실측: 표본 120개 중 103개가 정확히
# 100명, 101명 이상은 하나도 없었다). 페이징 파라미터도 없다(n4PageNo/page/offset/limit …
# 전부 무시되고 같은 100명이 온다). 포지션으로 나눠도 한 포지션이 100을 넘으면 또 잘린다.
# 유일하게 먹히는 추가 필터가 OVR 범위라서, 포지션별로 받되 100에 닿으면 OVR 구간을
# 반씩 쪼개 재귀적으로 내려간다.
_PLAYER_PAGE_CAP = 100
POSITION_IDS = (24, 25, 26, 20, 21, 22, 27, 23, 13, 14, 15, 17, 18, 19, 16, 12,
                9, 10, 11, 1, 4, 5, 6, 3, 7, 2, 8, 0)


def _player_bucket(tid, pos=None, lo=None, hi=None):
    """한 조각을 받아 {(pid, spid)} 로 돌려준다. 조회 실패는 None (빈 결과와 구분)."""
    url = f"{BASE}/DataCenter/TeamColorPlayerList?teamcolorid={tid}"
    if pos is not None:
        url += f"&strPosition=%2C{pos}%2C"
    if lo is not None:
        url += f"&n4OvrMin={lo}&n4OvrMax={hi}"
    r = _get(url, headers=XHR)
    if r is None:
        return None
    try:
        return {(p.get("pid"), p.get("spid")) for p in r.json().get("players", [])
                if p.get("pid") is not None}
    except Exception:
        return None


def _collect_position(tid, pos, lo=1, hi=200, depth=0):
    """한 포지션을 OVR 이분 분할로 훑는다(100에 닿는 동안 계속 쪼갬)."""
    r = _player_bucket(tid, pos, lo, hi)
    if r is None:
        return set()
    if len(r) < _PLAYER_PAGE_CAP or lo >= hi or depth >= 8:
        return r
    mid = (lo + hi) // 2
    return (_collect_position(tid, pos, lo, mid, depth + 1)
            | _collect_position(tid, pos, mid + 1, hi, depth + 1))


def fetch_players(tid):
    """특성 팀컬러의 소속 선수 전체. 실패하면 None(빈 목록과 구분).

    대부분의 팀컬러는 100명 미만이라 요청 1번으로 끝난다. 100에 닿은 것만 포지션 x OVR
    로 쪼개서 다시 훑는다(실측: 19-20 FC 바르셀로나는 1회 요청이면 100명으로 잘리지만
    이 방식으로는 507명을 다 받는다 — 포지션 25분할만 하던 예전 방식의 497명보다도 많다).
    """
    first = _player_bucket(tid)
    if first is None:
        return None
    if len(first) < _PLAYER_PAGE_CAP:
        return [{"pid": pid, "spid": spid} for pid, spid in first]

    out = set()
    for pos in POSITION_IDS:
        out |= _collect_position(tid, pos)
    if not out:
        # 쪼개서 하나도 못 받았다면 조회가 통째로 실패한 것으로 본다
        return None
    return [{"pid": pid, "spid": spid} for pid, spid in out]


def main():
    ap = argparse.ArgumentParser(description="팀컬러 수집")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    t0 = time.perf_counter()

    print("팀컬러 목록을 받는 중...")
    all_items = fetch_list(LIST_URL)
    relation_ids = set(fetch_list(RELATION_LIST_URL))
    if not all_items:
        print("[중단] 목록을 못 받았습니다.")
        return 1
    print(f"  전체 {len(all_items)}개 (그중 특성 팀컬러 {len(relation_ids)}개)")

    ids = list(all_items)
    print(f"\n단계별 효과 수집 ({args.workers}스레드)...")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        levels_list = list(ex.map(fetch_levels, ids))
    levels_map = dict(zip(ids, levels_list))
    got_levels = sum(1 for v in levels_list if v)
    print(f"  {got_levels}/{len(ids)}개 성공")

    rel = [t for t in ids if t in relation_ids]
    print(f"\n특성 팀컬러 선수 목록 수집 ({len(rel)}개)...")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        players_list = list(ex.map(fetch_players, rel))
    players_map = dict(zip(rel, players_list))
    failed = [t for t in rel if players_map.get(t) is None]
    if failed:
        print(f"  조회 실패 {len(failed)}개 — 한 번 더 시도")
        with ThreadPoolExecutor(max_workers=max(4, args.workers // 2)) as ex:
            for t, r in zip(failed, ex.map(fetch_players, failed)):
                players_map[t] = r
        failed = [t for t in rel if players_map.get(t) is None]
        print(f"  재시도 후 실패 {len(failed)}개")

    data = []
    for tid in ids:
        lv = levels_map.get(tid)
        if not lv:
            continue    # 단계 정보를 못 받은 건 넣지 않는다(반쪽 데이터 방지)
        data.append({
            "id": tid,
            "name": all_items[tid]["name"],
            "crest": all_items[tid]["crest"],
            "type": "특성 팀컬러" if tid in relation_ids else "소속 팀컬러",
            "levels": lv,
            "players": players_map.get(tid) or [],
        })

    el = time.perf_counter() - t0
    rel_data = [d for d in data if d["type"] == "특성 팀컬러"]
    with_players = sum(1 for d in rel_data if d["players"])
    print(f"\n수집 완료 {el:.0f}초 — 팀컬러 {len(data)}개")
    print(f"  특성 팀컬러 {len(rel_data)}개 중 선수 목록 보유 {with_players}개 "
          f"(총 매핑 {sum(len(d['players']) for d in data)}건)")

    old = []
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            old = []
    if old:
        old_ids = {d.get("id") for d in old}
        new_ids = {d["id"] for d in data}
        print(f"  기존 {len(old)}개 -> 신규 {len(new_ids - old_ids)}개 추가 / "
              f"사라짐 {len(old_ids - new_ids)}개")
        old_map = {d.get("id"): d for d in old}
        changed = [d for d in data
                   if d["id"] in old_map and old_map[d["id"]].get("levels") != d["levels"]]
        print(f"  단계 효과가 달라진 팀컬러: {len(changed)}개")
        for d in changed[:5]:
            print(f"    {d['name']}: {old_map[d['id']].get('levels')} -> {d['levels']}")

    if args.dry_run:
        print("\n[--dry-run] 저장하지 않았습니다.")
        return 0
    if len(data) < 100:
        print(f"\n[중단] 수집 결과가 {len(data)}개뿐이라 기존 파일을 덮어쓰지 않습니다.")
        return 1

    # 선수 목록이 통째로 비면 그 특성 팀컬러는 앱에서 조용히 발동하지 않는다. 실제로
    # 한 번은 500개 중 190개가 빈 채로 커밋돼서 팀컬러가 사라져 보였다(조회 실패를
    # 빈 목록으로 처리했던 탓). 실패가 많으면 저장하지 않고 기존 파일을 지킨다.
    empty_ratio = 1 - (with_players / max(len(rel_data), 1))
    if empty_ratio > 0.1:
        print(f"\n[중단] 특성 팀컬러 {len(rel_data)}개 중 {len(rel_data) - with_players}개가 "
              f"선수 목록 없음({empty_ratio:.0%}). 조회가 불안정한 것으로 보고 "
              f"기존 파일을 덮어쓰지 않습니다.")
        return 1
    if old:
        old_total = sum(len(d.get("players") or []) for d in old)
        new_total = sum(len(d["players"]) for d in data)
        if old_total and new_total < old_total * 0.7:
            print(f"\n[중단] 선수 매핑이 {old_total} -> {new_total} 로 크게 줄었습니다. "
                  f"기존 파일을 덮어쓰지 않습니다.")
            return 1

    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, OUT)
    print(f"\n저장 완료 -> {OUT} ({os.path.getsize(OUT)/1024/1024:.2f}MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
