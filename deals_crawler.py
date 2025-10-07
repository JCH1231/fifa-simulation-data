import requests
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import re

# --- 설정값 ---
BASE_URL = "https://raw.githubusercontent.com/JCH1231/fifa-simulation-data/main/"
OUTPUT_FILENAME = "deals.json"
SUCCESS_PROBS = [1.00, 0.81, 0.64, 0.50, 0.26, 0.15, 0.07]
ALLOWED_SEASON_CODES = {
    "100", "113", "114", "848", "850", "846", "845", "840", "839", "836", "829",
    "828", "827", "826", "825", "821", "818", "814", "813", "802", "290",
    "801", "291", "289", "283", "284", "274", "835", "811", "834", "831", "268"
}


# --- 헬퍼 함수 ---
def http_get(url, **kwargs):
    kwargs.setdefault("timeout", 10)
    return requests.get(url, **kwargs)


def get_real_gauge_percent(spid, grade, material_ovrs, gauge_table):
    try:
        grade_str, ovr_target, ovr_material = str(grade), str(material_ovrs[0]), str(material_ovrs[1])
        return float(gauge_table[grade_str][ovr_target][ovr_material])
    except (KeyError, IndexError, TypeError):
        return 0.0


def get_cheapest_material_cost(target_ovr, stage, average_prices, gauge_table):
    candidates = []
    for ovr, price in average_prices.items():
        try:
            price_val = int(price)
            if price_val <= 0: continue
            percent = get_real_gauge_percent(0, stage, [target_ovr, int(ovr)], gauge_table)
            if percent > 0:
                candidates.append((int(ovr), price_val, percent))
        except (ValueError, TypeError):
            continue

    if not candidates: return float('inf')

    SCALE, CAP = 100, 100 * 100
    values = {ov: int(round(pe * SCALE)) for ov, pr, pe in candidates}
    costs = {ov: pr for ov, pr, pe in candidates}

    dp = [{} for _ in range(6)]
    dp[0][0] = 0

    for c in range(1, 6):
        current_dp = {}
        for prev_p, prev_cost in dp[c - 1].items():
            for ov in values:
                new_p = min(CAP, prev_p + values[ov])
                new_cost = prev_cost + costs[ov]
                if new_p not in current_dp or new_cost < current_dp[new_p]:
                    current_dp[new_p] = new_cost
        dp[c] = current_dp

    return dp[5].get(CAP, float('inf'))


def estimate_total_cost(base_ovr, target_grade, average_prices, gauge_table, cost_cache):
    total_cost = 0
    grade_bonus_map = {1: 0, 2: 1, 3: 2, 4: 4, 5: 6, 6: 8, 7: 11}
    for i in range(1, target_grade):
        prob = SUCCESS_PROBS[i - 1]
        if prob <= 0: return float('inf')

        attempts = 1 / prob
        current_card_ovr = base_ovr + grade_bonus_map.get(i, 0)

        # [핵심] 캐시 확인 및 사용
        cache_key = (current_card_ovr, i)
        if cache_key in cost_cache:
            material_cost_one_attempt = cost_cache[cache_key]
        else:
            material_cost_one_attempt = get_cheapest_material_cost(current_card_ovr, i, average_prices, gauge_table)
            cost_cache[cache_key] = material_cost_one_attempt # 계산 결과를 캐시에 저장

        if material_cost_one_attempt == float('inf'): return float('inf')
        total_cost += attempts * material_cost_one_attempt
    return total_cost


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
    print("Fetching initial data (players, prices, gauge)...")
    all_players = http_get(BASE_URL + "all_players.json").json()
    average_prices = http_get(BASE_URL + "average.json").json()
    gauge_table = http_get(BASE_URL + "gauge_table_60_140.json").json()
    print("Data fetch complete.")

    player_map = {p['spid']: p for p in all_players}
    candidate_spids = [p['spid'] for p in all_players if str(p.get('spid', ''))[:3] in ALLOWED_SEASON_CODES]
    print(f"Processing {len(candidate_spids)} players from allowed seasons...")

    # 재료비 계산 결과를 저장할 캐시 생성
    material_cost_cache = {}

    deals_to_save = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_fetch_all_grade_prices, spid): spid for spid in candidate_spids}
        total = len(futures)
        for i, future in enumerate(as_completed(futures)):
            spid = futures[future]
            player = player_map.get(spid)
            if not player: continue

            if (i + 1) % 100 == 0:
                print(f"  Processed {i + 1}/{total} players...")

            all_prices = future.result()
            if not all_prices or len(all_prices) < 7: continue
            price_1 = all_prices[0]
            if price_1 == 0: continue

            for grade in range(4, 8):
                price_target = all_prices[grade - 1]
                if price_target <= price_1: continue

                # 캐시를 estimate_total_cost 함수에 전달
                material_cost_only = estimate_total_cost(int(player['overall']), grade, average_prices, gauge_table, material_cost_cache)
                if material_cost_only == float('inf'): continue

                total_investment = material_cost_only + price_1
                profit = price_target - total_investment

                if profit > 0:
                    profit_margin = (profit / total_investment) * 100 if total_investment > 0 else 0
                    deal_data = {
                        'spid': spid,
                        'target_grade': grade,
                        'profit': profit,
                        'profit_margin': profit_margin,
                        'total_investment': total_investment,
                    }
                    deals_to_save.append(deal_data)

    with open(OUTPUT_FILENAME, "w", encoding="utf-8") as f:
        json.dump(deals_to_save, f)

    print(f"Update complete. Saved {len(deals_to_save)} profitable deals to {OUTPUT_FILENAME}.")


if __name__ == "__main__":
    main()

