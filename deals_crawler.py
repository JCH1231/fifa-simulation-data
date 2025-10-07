import requests
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import re

# --- 설정값 ---
BASE_URL = "https://raw.githubusercontent.com/JCH1231/fifa-simulation-data/main/"
OUTPUT_FILENAME = "deals.json"
ALLOWED_SEASON_CODES = {
    "100", "113", "114", "848", "850", "846", "845", "840", "839", "836", "829",
    "828", "827", "826", "825", "821", "818", "814", "813", "802", "290",
    "801", "291", "289", "283", "284", "274", "835", "811", "834", "831", "268"
}


# --- 헬퍼 함수 ---
def http_get(url, **kwargs):
    kwargs.setdefault("timeout", 10)
    return requests.get(url, **kwargs)


def _fetch_all_grade_prices(spid):
    try:
        url = f"https://m.fconline.nexon.com/datacenter/playerinfo?spid={spid}"
        resp = http_get(url, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200: return None
        price_texts = re.findall(r"([0-9,]{2,})\s*BP", resp.text)
        if len(price_texts) < 13: return None
        return [int(p.replace(",", "")) for p in price_texts][-13:]
    except Exception:
        return None


def main():
    """메인 실행 함수"""
    print("Fetching initial data (players)...")
    all_players = http_get(BASE_URL + "all_players.json").json()
    print("Data fetch complete.")

    candidate_players = [p for p in all_players if str(p.get('spid', ''))[:3] in ALLOWED_SEASON_CODES]
    print(f"Processing {len(candidate_players)} players from allowed seasons...")

    deals_to_save = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_fetch_all_grade_prices, p['spid']): p for p in candidate_players}
        total = len(futures)
        for i, future in enumerate(as_completed(futures)):
            player = futures[future]
            if (i + 1) % 100 == 0:
                print(f"  Processed {i + 1}/{total} players...")

            all_prices = future.result()
            if not all_prices or len(all_prices) < 7: continue
            price_1 = all_prices[0]
            if price_1 == 0: continue

            for grade in range(4, 8):
                price_target = all_prices[grade - 1]
                if price_target <= price_1: continue

                # [수정] 재료비 계산을 제거하고, 시세 정보만 저장
                deal_data = {
                    'spid': player['spid'],
                    'target_grade': grade,
                    'price_1': price_1,
                    'price_target': price_target,
                }
                deals_to_save.append(deal_data)

    with open(OUTPUT_FILENAME, "w", encoding="utf-8") as f:
        json.dump(deals_to_save, f)

    print(f"Update complete. Saved {len(deals_to_save)} potential deals to {OUTPUT_FILENAME}.")


if __name__ == "__main__":
    main()