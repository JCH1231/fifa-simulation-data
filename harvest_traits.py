# -*- coding: utf-8 -*-
"""특성(trait) ID ↔ 이름 매핑을 넥슨 데이터센터에서 자동 수집해 trait_map.json 으로 저장한다.

[왜 만들었나]
예전에는 squad_maker.py 안에 TRAIT_MAP 딕셔너리를 손으로 적어두고, 신규 특성이 나올 때마다
사람이 직접 이름과 번호를 찾아 추가했다. 그래서 실제로 이런 일이 벌어져 있었다:
  · 코드에 아예 없는 특성 15개 (59 커맨더, 7 슬라이딩 태클 선호, 1 장거리 스로잉 …)
  · 이름을 잘못 적어 영영 매칭이 안 되던 것 3개
      20 "GK 능숙한 펀치"  -> 실제 "GK 능숙한 펀칭"
      23 "GK 침착한 수비 1대1" -> 실제 "GK 침착한 1:1 수비"
      25 "아웃사이드 슈팅"/"아웃사이드 크로스" -> 실제 "아웃사이드 슈팅/크로스"

넥슨 오픈 API 에는 특성 메타 엔드포인트가 없다(spskill/trait 류 경로는 전부 403).
대신 선수 상세 페이지에 아이콘과 이름이 나란히 박혀 있다:
    <img src=".../traits/trait_icon_59.png" alt="커맨더" />
선수 몇백 명만 훑으면 현재 게임에 존재하는 특성이 사실상 전부 모인다. 이제 사람이
목록을 관리할 필요가 없다.

[사용법]
    python harvest_traits.py                # 기본 표본으로 수집
    python harvest_traits.py --sample 800   # 더 넓게
    python harvest_traits.py --dry-run      # 저장 안 하고 결과만
"""

import argparse
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "https://fconline.nexon.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYERS = os.path.join(HERE, "all_players.json")
OUT = os.path.join(HERE, "trait_map.json")

# <img src="https://.../traits/trait_icon_59.png" alt="커맨더" />
_RE_TRAIT = re.compile(r'trait_icon_(\d+)\.png"\s+alt="([^"]+)"')

_tls = threading.local()


def _session():
    if not hasattr(_tls, "s"):
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Referer": f"{BASE}/datacenter/index"})
        s.mount("https://", requests.adapters.HTTPAdapter(
            pool_connections=4, pool_maxsize=4, max_retries=0))
        _tls.s = s
    return _tls.s


def fetch_traits(spid, retries=2):
    """선수 한 명의 페이지에서 (특성ID, 이름) 목록을 뽑는다."""
    for attempt in range(retries):
        try:
            r = _session().get(
                f"{BASE}/DataCenter/PlayerAbility?spid={spid}&n1Strong=1", timeout=25)
            if r.status_code != 200:
                raise RuntimeError(f"status {r.status_code}")
            return [(int(t), n) for t, n in _RE_TRAIT.findall(r.text)]
        except Exception:
            if attempt == retries - 1:
                return []
            _tls.s = None
            time.sleep(0.3)
    return []


def main():
    ap = argparse.ArgumentParser(description="특성 ID↔이름 매핑 자동 수집")
    ap.add_argument("--sample", type=int, default=600, help="훑을 선수 수 (기본 600)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(PLAYERS):
        print(f"[중단] {PLAYERS} 이 없습니다.")
        return 1
    with open(PLAYERS, encoding="utf-8") as f:
        players = json.load(f)

    # 신규 특성은 고OVR 카드에 먼저 붙는 경향이 있어 절반은 고OVR, 절반은 무작위로 섞는다.
    # (무작위만 뽑으면 저OVR 카드가 대부분이라 신규 특성을 놓친다)
    def _ovr(p):
        try:
            return int(p.get("overall", 0))
        except (TypeError, ValueError):
            return 0

    high = sorted(players, key=_ovr, reverse=True)[:args.sample // 2]
    random.seed(0)
    rand = random.sample(players, min(args.sample // 2, len(players)))
    picked = list({p["spid"]: p for p in high + rand}.values())
    print(f"선수 {len(picked)}명 훑는 중 ({args.workers}스레드)...")

    harvested = {}
    t0 = time.perf_counter()
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for pairs in ex.map(lambda p: fetch_traits(p["spid"]), picked):
            for tid, name in pairs:
                harvested[tid] = name
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(picked)}  누적 특성 {len(harvested)}종 "
                      f"({time.perf_counter() - t0:.0f}s)", flush=True)

    if not harvested:
        print("[중단] 특성을 하나도 못 찾았습니다. 저장을 건너뜁니다.")
        return 1

    old = {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                old = {int(k): v for k, v in json.load(f).items()}
        except Exception:
            old = {}

    added = {t: n for t, n in harvested.items() if t not in old}
    renamed = {t: (old[t], n) for t, n in harvested.items()
               if t in old and old[t] != n}
    # 이번 표본에 안 걸린 특성은 사라진 게 아니라 그냥 표본에 없는 것이다 — 지우지 않는다.
    merged = dict(old)
    merged.update(harvested)

    print(f"\n수집 {len(harvested)}종 / 기존 {len(old)}종 -> 병합 {len(merged)}종 "
          f"({time.perf_counter() - t0:.0f}s)")
    if added:
        print(f"  신규 {len(added)}개: " +
              ", ".join(f"{t}={n}" for t, n in sorted(added.items())))
    if renamed:
        print(f"  이름 변경 {len(renamed)}개: " +
              ", ".join(f"{t} {a!r}->{b!r}" for t, (a, b) in sorted(renamed.items())))
    if not added and not renamed:
        print("  변경 없음")

    if args.dry_run:
        print("\n[--dry-run] 저장하지 않았습니다.")
        return 0
    if not added and not renamed:
        return 0

    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({str(k): merged[k] for k in sorted(merged)}, f,
                  ensure_ascii=False, indent=2)
    os.replace(tmp, OUT)
    print(f"\n저장 완료 -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
