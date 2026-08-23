# crawler.py (수정 완료된 전체 코드)
import requests
import json
import re
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from datetime import datetime, timedelta

# --- 설정 ---
EXCLUDED_SEASON_CODES = {
    "812", "317", "318", "319", "320", "321", "322", "514", "512", "510", "507", "504", "805", "277", "257",
    "239", "515", "513", "511", "508", "506", "503", "502", "501", "500", "300", "323", "516", "297", "298",
    "295", "221", "242", "260", "280", "215", "230", "250", "267", "287", "212", "211", "222", "220", "241",
    "240", "259", "258", "279", "278", "201", "202", "206", "207", "210", "213", "214", "216", "217", "218",
    "219", "238", "262", "276", "804", "236", "233", "231", "237", "246", "249", "253", "251", "256", "252",
    "254", "261", "281", "264", "265", "270", "273", "272", "110", "849", "815"
}
REQUEST_TIMEOUT = 30
session = requests.Session()
from requests.adapters import HTTPAdapter

session.mount("https://", HTTPAdapter(pool_connections=20, pool_maxsize=40))


def http_get(url, **kw):
    kw.setdefault("timeout", REQUEST_TIMEOUT)
    return session.get(url, **kw)


def fetch_player_graph_data(spid, grade=8):
    """선수 한 명의 전체 가격 내역(HTML)을 가져옵니다."""
    url = "https://m.fconline.nexon.com/datacenter/PlayerPriceGraph"
    payload = {'spid': str(spid), 'n1strong': str(grade)}
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/5.37.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/5.37.36',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': f'https://m.fconline.nexon.com/datacenter/playerinfo?spid={spid}'
    }
    try:
        response = session.post(url, headers=headers, data=payload, timeout=5)
        if response.status_code == 200:
            return response.text
    except requests.exceptions.RequestException as e:
        print(f"SPID {spid} 요청 실패: {e}")
        return None
    return None


# 배율빔으로 볼 강화 등급. 예전에는 8강 하나만 받아서 배율빔 매물도 8강만 나왔다.
# 실측: 등급마다 시세가 확연히 다르고(같은 선수 8강 1998만 / 11강 6520만),
# 표본 120명 전원이 8~11강 모두 데이터를 갖고 있었다.
TARGET_GRADES = (8, 9, 10, 11)


def parse_graph_text(graph_text):
    """그래프 응답 HTML에서 [{'time': ms, 'value': int}, ...] 를 뽑는다."""
    if not graph_text:
        return None
    try:
        match = re.search(r"var chartData = (\{.*?\});", graph_text, re.DOTALL)
        if not match:
            return None

        data_string = match.group(1).replace('time:', '"time":').replace('value:', '"value":')
        data_string = re.sub(r",\s*([\]}])", r"\1", data_string)
        chart_data = json.loads(data_string)

        price_points = []
        # 1. 최신 데이터 형식 처리 (datasets)
        if 'datasets' in chart_data and chart_data['datasets'] and 'data' in chart_data['datasets'][0]:
            price_points = [{'time': p['x'], 'value': p['y']} for p in chart_data['datasets'][0]['data']]
        # 2. 구형 데이터 형식 처리 (time, value)
        elif 'time' in chart_data and 'value' in chart_data:
            temp_points = []
            now = datetime.now()
            current_year = now.year
            last_date_parsed = now

            # 날짜 형식이 섞여있으므로 뒤에서부터 파싱하며 연도를 추정
            for time_str, value in reversed(list(zip(chart_data['time'], chart_data['value']))):
                point_date = None
                # "new Date(1728639600000)" 형식
                if 'Date' in time_str:
                    ts_match = re.search(r"\((\d+)\)", time_str)
                    if ts_match:
                        # 타임스탬프(밀리초)를 직접 사용
                        temp_points.append({'time': int(ts_match.group(1)), 'value': int(value)})
                # "10.12" 형식
                elif '.' in time_str:
                    try:
                        point_date = datetime.strptime(f"{current_year}-{time_str.replace('.', '-')}", "%Y-%m-%d")
                        if point_date > last_date_parsed:
                            current_year -= 1
                            point_date = datetime(year=current_year, month=point_date.month, day=point_date.day)
                        last_date_parsed = point_date
                        # 날짜를 타임스탬프(밀리초)로 변환하여 저장
                        timestamp = int(point_date.timestamp() * 1000)
                        temp_points.append({'time': timestamp, 'value': int(value)})
                    except (ValueError, TypeError):
                        continue
            # 시간순으로 다시 뒤집기
            price_points = temp_points[::-1]

        return price_points or None
    except Exception as e:
        print(f"그래프 파싱 실패 (예외 발생): {e}")
        return None


def parse_and_process_player(player):
    """선수 한 명의 8~11강 가격 내역을 모아서 돌려준다.

    저장 형식은 등급별로 통째로 중첩하지 않고 time 배열을 등급끼리 공유한다:
        {"name": ..., "times": [ms, ...], "values": {"8": [v, ...], "9": [...]}}
    실측상 한 선수의 time 배열은 등급이 달라도 완전히 동일했고(표본 120명 전원),
    그냥 중첩하면 파일이 68.5MB 가 되는데 time 을 공유하면 23.8MB 로 줄어든다.
    이 파일은 앱 사용자가 매번 내려받으므로 크기가 그대로 체감 속도가 된다.
    """
    spid = player.get('spid')
    if not spid:
        return None

    times = None
    values = {}
    for grade in TARGET_GRADES:
        points = parse_graph_text(fetch_player_graph_data(spid, grade=grade))
        if not points:
            continue
        t = [p['time'] for p in points]
        if times is None:
            times = t
            values[str(grade)] = [p['value'] for p in points]
        elif t == times:
            values[str(grade)] = [p['value'] for p in points]
        else:
            # 등급끼리 time 이 어긋나면(실측상 없었지만) 공유 배열에 못 넣는다.
            # 값을 엉뚱한 날짜에 붙이느니 그 등급만 버린다.
            print(f"SPID {spid} {grade}강: time 배열 불일치 — 이 등급은 건너뜀")

    if not values:
        return None

    return {"spid": spid, "name": player.get('name'), "times": times, "values": values}


def main():
    print("선수 목록(all_players.json)을 불러옵니다...")
    try:
        # 저장소에 체크아웃된 파일을 먼저 본다. 예전에는 json/ 하위만 보고 없으면 무조건
        # 41MB 를 내려받았는데, CI 에는 json/ 이 없으니 매 실행(하루 12번)마다 헛으로 받았다.
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [os.path.join(here, "all_players.json"),
                      os.path.join(here, "json", "all_players.json")]
        local_json_path = next((p for p in candidates if os.path.exists(p)), None)

        if local_json_path is None:
            local_json_path = candidates[1]
            os.makedirs(os.path.dirname(local_json_path), exist_ok=True)
            print("로컬 all_players.json 파일이 없어 다운로드합니다...")
            remote_url = "https://raw.githubusercontent.com/JCH1231/fifa-simulation-data/main/all_players.json"
            resp = http_get(remote_url, timeout=120)
            resp.raise_for_status()
            with open(local_json_path, 'w', encoding='utf-8') as f:
                json.dump(resp.json(), f, ensure_ascii=False)
            print("다운로드 완료.")
        else:
            print(f"로컬 파일 사용: {local_json_path}")

        with open(local_json_path, "r", encoding="utf-8") as f:
            all_players = json.load(f)

    except Exception as e:
        print(f"오류: all_players.json 파일을 준비하는 중 문제가 발생했습니다: {e}")
        return

    target_players = [p for p in all_players if str(p.get('spid', '000'))[:3] not in EXCLUDED_SEASON_CODES]
    total_count = len(target_players)
    print(f"총 {total_count}명의 선수에 대한 가격 정보 수집을 시작합니다.")

    all_results = {}
    processed_count = 0
    start_time = time.time()

    # 실측: 20 -> 32 스레드에서 463건/s -> 605건/s. 64 는 158건/s 로 급락한다(서버 제한).
    with ThreadPoolExecutor(max_workers=32) as executor:
        future_to_player = {executor.submit(parse_and_process_player, p): p for p in target_players}
        for future in as_completed(future_to_player):
            processed_count += 1
            try:
                result = future.result()
                if result:
                    all_results[result['spid']] = {
                        "name": result['name'],
                        "times": result['times'],
                        "values": result['values'],
                    }
            except Exception as exc:
                print(f'선수 처리 중 예외 발생: {exc}')

            if processed_count % 100 == 0 or processed_count == total_count:
                elapsed_time = time.time() - start_time
                print(f"진행 상황: {processed_count}/{total_count} ({processed_count / total_count:.1%}) | 소요 시간: {elapsed_time:.1f}초")

    # 30일치만 보존 — API 는 365일을 주는데 배율빔은 최대 30일 창만 쓴다.
    print(f"\n데이터 최적화 시작: 최근 30일치 데이터만 보존합니다.")
    processed_results = {}
    limit_ts = int((datetime.now() - timedelta(days=30)).timestamp() * 1000)

    for spid, data in all_results.items():
        times = data['times']
        keep = [i for i, t in enumerate(times) if t >= limit_ts]
        if len(keep) < 2:      # 배율빔은 포인트 2개 이상이어야 계산된다
            continue
        processed_results[spid] = {
            "name": data['name'],
            "times": [times[i] for i in keep],
            "values": {g: [vals[i] for i in keep] for g, vals in data['values'].items()},
        }

    output_filename = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "price_history.json")
    tmp = output_filename + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(processed_results, f, ensure_ascii=False)
    os.replace(tmp, output_filename)

    end_time = time.time()
    size_mb = os.path.getsize(output_filename) / 1024 / 1024
    grade_counts = {}
    for d in processed_results.values():
        for g in d['values']:
            grade_counts[g] = grade_counts.get(g, 0) + 1
    print(f"\n완료! 총 {len(processed_results)}명의 선수 정보를 저장했습니다 ({size_mb:.1f}MB).")
    print(f"등급별 보유 선수 수: {dict(sorted(grade_counts.items(), key=lambda x: int(x[0])))}")
    print(f"총 소요 시간: {end_time - start_time:.1f}초")


if __name__ == "__main__":
    main()
