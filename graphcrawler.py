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


def parse_and_process_player(player):
    """[수정] 선수 정보와 spid를 받아 여러 데이터 형식을 안정적으로 파싱합니다."""
    spid = player.get('spid')
    if not spid:
        return None

    graph_text = fetch_player_graph_data(spid, grade=8)
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

        if not price_points:
            return None

        return {
            "spid": spid,
            "name": player.get('name'),
            "prices": price_points
        }
    except Exception as e:
        print(f"SPID {spid} 파싱 실패 (예외 발생): {e}")
        return None


def main():
    print("선수 목록(all_players.json)을 불러옵니다...")
    try:
        json_dir = "json"
        if not os.path.exists(json_dir):
            os.makedirs(json_dir)

        local_json_path = os.path.join(json_dir, "all_players.json")

        if not os.path.exists(local_json_path):
            print("로컬 all_players.json 파일이 없어 다운로드합니다...")
            remote_url = "https://raw.githubusercontent.com/JCH1231/fifa-simulation-data/main/all_players.json"
            resp = http_get(remote_url)
            resp.raise_for_status()
            with open(local_json_path, 'w', encoding='utf-8') as f:
                json.dump(resp.json(), f, ensure_ascii=False)
            print("다운로드 완료.")

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

    with ThreadPoolExecutor(max_workers=20) as executor:
        future_to_player = {executor.submit(parse_and_process_player, p): p for p in target_players}
        for future in as_completed(future_to_player):
            processed_count += 1
            try:
                result = future.result()
                if result:
                    all_results[result['spid']] = {"name": result['name'], "prices": result['prices']}
            except Exception as exc:
                print(f'선수 처리 중 예외 발생: {exc}')

            if processed_count % 100 == 0 or processed_count == total_count:
                elapsed_time = time.time() - start_time
                print(f"진행 상황: {processed_count}/{total_count} ({processed_count / total_count:.1%}) | 소요 시간: {elapsed_time:.1f}초")

    # [수정 완료] 30일치 데이터 필터링 및 저장 (중복 저장 코드 삭제)
    print(f"\n데이터 최적화 시작: 최근 30일치 데이터만 보존합니다.")
    processed_results = {}
    
    # 30일 전 타임스탬프 계산 (밀리초 단위)
    # ※ 코드 맨 위 import에 from datetime import datetime, timedelta 가 있는지 확인하세요!
    limit_date = datetime.now() - timedelta(days=30)
    limit_ts = int(limit_date.timestamp() * 1000)

    for spid, data in all_results.items():
        recent_prices = [p for p in data['prices'] if p['time'] >= limit_ts]
        if recent_prices:
            processed_results[spid] = {"name": data['name'], "prices": recent_prices}
    
    output_filename = "price_history.json"
    with open(output_filename, "w", encoding="utf-8") as f:
        json.dump(processed_results, f, ensure_ascii=False)

    end_time = time.time()
    print(f"\n완료! 총 {len(processed_results)}명의 선수 정보를 {output_filename} 파일에 저장했습니다.")
    print(f"총 소요 시간: {end_time - start_time:.1f}초")


if __name__ == "__main__":
    main()
