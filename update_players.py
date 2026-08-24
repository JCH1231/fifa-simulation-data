# -*- coding: utf-8 -*-
"""all_players.json 을 갱신한다: 신규 선수 추가 + 기존 선수 OVR 재확인. (하루 1회 실행)

[예전에 수동으로 하던 것]
  DATA_CRAWLER.py 로 spid.json 을 받고 → player_crawler.py 를 돌려 all_players.json 을 만들고
  → 깃허브에 직접 업로드. 귀찮아서 자주 안 하게 되고, 그 사이 데이터가 낡는다.
이 스크립트가 그 세 단계를 한 번에 하고, 워크플로가 커밋까지 한다.

[예전 player_crawler.py 의 문제]
  · 단일 스레드 for 루프 — 신규 선수가 수백 명이면 그만큼 순차로 기다린다.
  · `if not is_new_player and not is_target_season: continue` 로 **기존 선수를 통째로 건너뛴다**.
    target_seasons_for_ovr_check 가 빈 리스트라 사실상 OVR 이 영영 갱신되지 않았다.
    피파는 라이브 부스트를 주기적으로 조정하므로 OVR 이 계속 흘러간다. 실측 결과 1,277명이
    어긋나 있었고(-7 ~ +10), 이게 게이지 시뮬레이션의 조회 키를 직접 망가뜨린다
    (앱은 `대상OVR = 기본OVR + 보너스[단계]` 로 키를 만든다).
  · indent=2 로 저장 — 같은 내용이 41MB -> 56MB 로 불어난다. 앱 사용자가 매번 그만큼 더 받는다.
세 가지 모두 여기서 고쳤다.

[사용법]
    python update_players.py                  # 신규 추가 + 전체 OVR 재확인
    python update_players.py --skip-ovr       # 신규만
    python update_players.py --dry-run        # 저장 안 하고 결과만
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

BASE = "https://fconline.nexon.com"
OPENAPI = "https://open.api.nexon.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYERS = os.path.join(HERE, "all_players.json")
SEASONS = os.path.join(HERE, "seasonid.json")

# 넥슨 오픈 API 토큰. CI 에서는 시크릿으로 넣고, 없으면 예전 DATA_CRAWLER.py 의 값을 쓴다.
API_TOKEN = os.environ.get("NEXON_API_TOKEN", "").strip() or (
    "test_127700297f9f53eed98c214b28c615fa07c715700f7866928ca6f615b774f824"
    "efe8d04e6d233bd35cf2fabdeb93fb0d")

# 9만 건을 BeautifulSoup 으로 전부 파싱하면 그게 곧 병목이다. OVR 재확인은 한 조각만 필요하므로
# 정규식으로 집고, 신규 선수(수백 명)만 BeautifulSoup 으로 꼼꼼히 파싱한다.
_RE_OVR = re.compile(r'class="[^"]*\bovr\b[^"]*\bvalue\b[^"]*"[^>]*>\s*(\d+)')
# <img src=".../traits/trait_icon_59.png" alt="커맨더" />
_RE_TRAIT = re.compile(r'trait_icon_(\d+)\.png"')
# 급여: <div class="pay"> ... <span>32</span>. 라이브 부스트로 바뀌므로 매번 다시 읽는다.
_RE_PAY = re.compile(r'class="[^"]*\bpay\b[^"]*"[^>]*>(?:(?!</div>).)*?<span[^>]*>\s*(\d+)\s*</span>', re.S)

_tls = threading.local()


def _new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": f"{BASE}/datacenter/index"})
    s.mount("https://", requests.adapters.HTTPAdapter(
        pool_connections=4, pool_maxsize=4, max_retries=0))
    s.get(f"{BASE}/", timeout=20)
    return s


def _session():
    if not hasattr(_tls, "s"):
        _tls.s = _new_session()
    return _tls.s


def _ability_html(spid, retries=3):
    for attempt in range(retries):
        try:
            r = _session().get(
                f"{BASE}/DataCenter/PlayerAbility?spid={spid}&n1Strong=1", timeout=25)
            if r.status_code != 200:
                raise RuntimeError(f"status {r.status_code}")
            return r.text
        except Exception:
            if attempt == retries - 1:
                return None
            _tls.s = _new_session()
            time.sleep(0.3 * (attempt + 1))
    return None


def fetch_ovr_and_traits(spid):
    """현재 OVR·급여·보유 특성 ID 를 한 번의 조회로 가져온다 → (ovr, [trait_id, ...], pay).

    특성을 이름 문자열이 아니라 ID로 저장하는 이유:
      · 예전에는 `skills` 에 "특성 파워 헤더 중거리 슛 선호" 같은 한 덩어리 문자열만 넣고,
        앱이 그 안에 특성 이름이 들어있는지 부분 문자열로 찾았다. 이름을 한 글자라도 다르게
        적어두면 영영 안 잡힌다(실제로 "GK 능숙한 펀치" vs 실제 "GK 능숙한 펀칭" 등 3개가
        그렇게 죽어 있었다).
      · 게다가 이 skills 는 신규 선수를 처음 긁을 때만 채워지고 그 뒤로 갱신되지 않아,
        신규 특성이 추가돼도 반영이 안 됐다(실측 표본의 72%가 신규 특성 누락).
    급여도 같이 돌려준다. 라이브 부스트를 받으면 OVR 과 함께 급여도 바뀌는데 예전에는
    기존 선수의 급여를 한 번도 갱신하지 않아, 처음 긁힌 값 그대로 낡아 있었다.
    OVR 갱신을 위해 어차피 받아오는 바로 그 페이지에서 같이 뽑으므로 추가 요청이 0이다.
    """
    html = _ability_html(spid)
    if not html:
        return None, None, None
    m = _RE_OVR.search(html)
    ovr = int(m.group(1)) if m else None
    pm = _RE_PAY.search(html)
    pay = pm.group(1) if pm else None
    # 등장 순서를 유지하면서 중복만 제거(같은 아이콘이 여러 번 나올 수 있다)
    seen, trait_ids = set(), []
    for t in _RE_TRAIT.findall(html):
        tid = int(t)
        if tid not in seen:
            seen.add(tid)
            trait_ids.append(tid)
    return ovr, trait_ids, pay


# ───────── 신규 선수 상세 파싱 (player_crawler.py 의 추출 로직을 그대로 옮김) ─────────
def _text(soup, sel):
    el = soup.select_one(sel)
    return el.get_text(" ", strip=True) if el else None


def _img_id(soup, sel):
    img = soup.select_one(sel + " img")
    if img and "src" in img.attrs:
        m = re.search(r"/([^/]+)\.png", img["src"])
        if m:
            return m.group(1)
    return None


def _foot(soup):
    box = soup.select_one("span.etc.foot") or soup.select_one(".etc.foot")
    if not box:
        return None, None
    text = box.get_text(" ", strip=True)
    m = re.search(r"[Ll]\s*([0-5])\s*[–-]?\s*[Rr]\s*([0-5])", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    left = right = None
    strong = box.find("strong")
    if strong:
        mL = re.search(r"[Ll]\s*([0-5])", strong.get_text(strip=True))
        if mL:
            left = int(mL.group(1))
    mR = re.search(r"[Rr]\s*([0-5])", text)
    if mR:
        right = int(mR.group(1))
    return left, right


def _skills(soup):
    wrap = soup.select_one(".skill_wrap")
    if not wrap:
        return []
    items = [li.get_text(" ", strip=True) for li in wrap.select("li")]
    items = [x for x in items if x]
    if not items:
        blob = wrap.get_text(" ", strip=True)
        items = [p.strip() for p in re.split(r"\s{2,}|[•·,]|/", blob) if p.strip()]
    seen, out = set(), []
    for x in items:
        if len(x) >= 2 and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _teamcolors(soup):
    wrap = soup.select_one(".teamcolor_selector_wrap")
    if not wrap:
        return [], []

    def collect(root):
        nodes = root.select(".on, .active, .selected, input:checked + label, .is-active")
        texts = [n.get_text(" ", strip=True) for n in nodes]
        if not any(texts):
            texts = [n.get_text(" ", strip=True)
                     for n in root.select("li, label, a, button, .item")]
        texts = [re.sub(r"\s+", " ", x).strip() for x in texts if x and x.strip()]
        seen, out = set(), []
        for x in texts:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    texts = collect(wrap)
    REL = ("관계", "관계팀", "관계 팀컬러", "Relation", "Club Link")
    if any(k in " ".join(texts) for k in REL):
        lists = wrap.select("ul")
        if len(lists) >= 2:
            tc = [re.sub(r"\s+", " ", n.get_text(" ", strip=True)).strip()
                  for n in lists[0].select("li") if n.get_text(strip=True)]
            rel = [re.sub(r"\s+", " ", n.get_text(" ", strip=True)).strip()
                   for n in lists[1].select("li") if n.get_text(strip=True)]
        else:
            head, tail, hit = [], [], False
            for x in texts:
                if any(k in x for k in REL):
                    hit = True
                    continue
                (tail if hit else head).append(x)
            tc, rel = head, tail
    else:
        tc, rel = texts, []

    def dd(lst):
        seen, out = set(), []
        for x in lst:
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return out

    return dd(tc), dd(rel)


def _body(soup):
    def digit(t):
        if not t:
            return None
        m = re.search(r"(\d+)", t.replace(",", ""))
        return int(m.group(1)) if m else None

    return (digit(_text(soup, ".etc.height")), digit(_text(soup, ".etc.weight")),
            _text(soup, ".etc.physical") or None)


def _nation_name(soup):
    txt = _text(soup, ".etc.nation")
    if txt:
        return re.sub(r"\s+", " ", txt).strip()
    box = soup.select_one(".nation")
    if box:
        t = box.get_text(" ", strip=True)
        if t:
            return re.sub(r"\s+", " ", t).strip()
    return None


def fetch_new_player(spid, name, _retry=True):
    """신규 선수의 전체 상세. 실패하면 None.

    상세 페이지가 가끔 .league/.team 의 img 를 비운 채로 온다(실측: 같은 선수도 어떤 응답에선
    src 가 아예 없다). 신규 선수는 이 한 번의 조회로 값이 영구히 굳으므로, 빈 값이면 한 번 더
    받아본다. 기존 선수는 애초에 OVR 만 갱신하니 이 경로를 타지 않는다.
    """
    html = _ability_html(spid)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    if _retry and not (_img_id(soup, ".league") and _img_id(soup, ".team")):
        time.sleep(0.2)
        again = fetch_new_player(spid, name, _retry=False)
        if again and again.get("league") and again.get("team"):
            return again
    overall = _text(soup, "div.ovr.value")
    position = (_text(soup, ".playerCardInfoSide .position")
                or _text(soup, ".info_line.info_ab .position .txt"))
    lf, rf = _foot(soup)
    h, w, body = _body(soup)
    tc, rel = _teamcolors(soup)
    seen, trait_ids = set(), []
    for t in _RE_TRAIT.findall(html):
        tid = int(t)
        if tid not in seen:
            seen.add(tid)
            trait_ids.append(tid)
    return {
        "spid": spid, "name": name,
        "pay": _text(soup, ".pay span"),
        "overall": overall, "position": position,
        "nation": _img_id(soup, ".nation"), "league": _img_id(soup, ".league"),
        "team": _img_id(soup, ".team"), "nation_name": _nation_name(soup),
        "foot": f"L{lf} R{rf}" if (lf is not None and rf is not None) else None,
        "left_foot": lf, "right_foot": rf,
        "height_cm": h, "weight_kg": w, "body_type": body,
        # skills(사람이 읽는 문자열)는 기존 화면 호환용으로 계속 넣고,
        # trait_ids(정확한 ID)를 함께 저장해 앱이 이쪽을 쓰게 한다.
        "skills": _skills(soup), "trait_ids": trait_ids,
        "teamcolors": tc, "relation_teamcolors": rel,
    }


# ───────── 넥슨 메타 ─────────
def fetch_meta(path):
    r = requests.get(OPENAPI + path, timeout=30,
                     headers={"Authorization": f"Bearer {API_TOKEN}",
                              "accept": "application/json", "User-Agent": UA})
    r.raise_for_status()
    return r.json()


def run_pool(fn, items, workers, label, report=1000):
    """items 를 workers 개로 병렬 처리하며 진행률을 찍는다."""
    out, done, fails = {}, 0, 0
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, it): it for it in items}
        for fut in as_completed(futs):
            key = futs[fut]
            try:
                val = fut.result()
            except Exception:
                val = None
            if val is None:
                fails += 1
            else:
                out[key if not isinstance(key, tuple) else key[0]] = val
            done += 1
            if done % report == 0 or done == len(items):
                el = time.perf_counter() - t0
                rate = done / max(el, .001)
                print(f"  [{label}] {done}/{len(items)}  {el:6.1f}s "
                      f"({rate:5.1f}/s, 남은시간 {(len(items)-done)/max(rate,.001)/60:4.1f}분, "
                      f"실패 {fails})", flush=True)
    return out, fails


def main():
    ap = argparse.ArgumentParser(description="all_players.json 갱신 (신규 선수 + OVR)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--skip-ovr", action="store_true", help="기존 선수 OVR 재확인 건너뛰기")
    ap.add_argument("--skip-new", action="store_true", help="신규 선수 추가 건너뛰기")
    ap.add_argument("--dry-run", action="store_true", help="저장하지 않음")
    args = ap.parse_args()

    t_all = time.perf_counter()

    players = []
    if os.path.exists(PLAYERS):
        with open(PLAYERS, encoding="utf-8") as f:
            players = json.load(f)
    print(f"기존 선수 {len(players)}명")
    by_spid = {p["spid"]: p for p in players if "spid" in p}

    print("넥슨 메타(spid/seasonid) 가져오는 중...")
    spid_meta = fetch_meta("/static/fconline/meta/spid.json")
    season_meta = fetch_meta("/static/fconline/meta/seasonid.json")
    print(f"  넥슨 선수 {len(spid_meta)}명 / 시즌 {len(season_meta)}개")

    # 이름 변경 반영 (네트워크 불필요)
    renamed = 0
    for it in spid_meta:
        p = by_spid.get(it["id"])
        if p and p.get("name") != it["name"]:
            p["name"] = it["name"]
            renamed += 1
    if renamed:
        print(f"  이름 변경 {renamed}건 반영")

    # ── 신규 선수 ──
    new_items = [(it["id"], it["name"]) for it in spid_meta if it["id"] not in by_spid]
    added = 0
    if new_items and not args.skip_new:
        print(f"\n신규 선수 {len(new_items)}명 상세 수집")
        got, fails = run_pool(lambda t: fetch_new_player(*t), new_items,
                              args.workers, "신규", report=100)
        for spid, rec in got.items():
            by_spid[spid] = rec
            added += 1
        print(f"  추가 {added}명 (실패 {fails})")
    elif not new_items:
        print("\n신규 선수 없음")

    # ── 기존 선수 OVR + 특성 재확인 (라이브 부스트 / 신규 특성 반영) ──
    changes = []
    trait_changes = 0
    if not args.skip_ovr:
        live_ids = [it["id"] for it in spid_meta if it["id"] in by_spid]
        print(f"\n현역 선수 {len(live_ids)}명 OVR·급여·특성 재확인")
        got, fails = run_pool(fetch_ovr_and_traits, live_ids, args.workers, "OVR")
        pay_changes = []
        for spid, (ovr, trait_ids, pay) in got.items():
            p = by_spid[spid]
            if ovr is not None:
                try:
                    old = int(p.get("overall"))
                except (TypeError, ValueError):
                    old = None
                if old != ovr:
                    changes.append((spid, p.get("name"), old, ovr))
                    p["overall"] = str(ovr)
            # 급여도 라이브 부스트로 같이 바뀐다. 예전엔 처음 값 그대로 굳어 있었다.
            if pay and str(p.get("pay")) != str(pay):
                pay_changes.append((spid, p.get("name"), p.get("pay"), pay))
                p["pay"] = str(pay)
            # 특성은 게임 패치로 새로 붙거나 빠질 수 있어 항상 최신으로 덮어쓴다.
            # 조회는 됐는데 특성이 하나도 없는 건 정상(특성 없는 선수도 있다).
            if trait_ids is not None and p.get("trait_ids") != trait_ids:
                p["trait_ids"] = trait_ids
                trait_changes += 1
        print(f"  OVR 변경 {len(changes)}명 / 특성 변경 {trait_changes}명 (조회 실패 {fails})")
        if changes:
            delta = {}
            for _s, _n, o, n in changes:
                if o is not None:
                    delta[n - o] = delta.get(n - o, 0) + 1
            print(f"  변화량 분포: {dict(sorted(delta.items()))}")
            for s, n, o, nv in changes[:8]:
                print(f"    {n}({s}): {o} -> {nv}")

    if args.dry_run:
        print("\n[--dry-run] 저장하지 않았습니다.")
        return 0
    if not (added or changes or renamed or trait_changes):
        print("\n바뀐 게 없어 저장을 건너뜁니다.")
        return 0

    # 넥슨 메타 순서를 따르되, 메타에서 빠진(게임에서 삭제된) 선수도 뒤에 남겨둔다 —
    # 예전 데이터로 만든 스쿼드/매물이 갑자기 이름을 잃지 않도록.
    ordered = [by_spid[it["id"]] for it in spid_meta if it["id"] in by_spid]
    live = {it["id"] for it in spid_meta}
    ordered += [p for p in by_spid.values() if p.get("spid") not in live]

    tmp = PLAYERS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        # 앱이 받아 저장할 때와 같은 형식(들여쓰기 없음). indent=2 면 41MB -> 56MB 가 된다.
        json.dump(ordered, f, ensure_ascii=False)
    os.replace(tmp, PLAYERS)

    with open(SEASONS, "w", encoding="utf-8") as f:
        json.dump(season_meta, f, ensure_ascii=False, indent=2)

    if changes:
        with open(os.path.join(HERE, "ovr_change_log.txt"), "w", encoding="utf-8") as f:
            for s, n, o, nv in changes:
                f.write(f"{n}({s}): OVR {o} -> {nv}\n")

    size = os.path.getsize(PLAYERS) / 1024 / 1024
    print(f"\n저장 완료 — {len(ordered)}명, {size:.1f}MB, "
          f"총 {(time.perf_counter() - t_all) / 60:.1f}분")
    return 0


if __name__ == "__main__":
    sys.exit(main())
