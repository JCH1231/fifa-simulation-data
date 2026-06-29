import requests
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import re

# --- 설정값 ---
BASE_URL = "https://raw.githubusercontent.com/JCH1231/fifa-simulation-data/main/"
OUTPUT_FILENAME = "deals.json"

# [수정] 조건 설정
MIN_PROFIT_BP = 50_000_000_000_000  # 최소 순수익 50조
MIN_OVR = 100                       # 최소 오버롤 (이 숫자 미만은 검색 안 함)

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

    near = [it for it in candidates if abs(it[0] - target_ovr) <= 10]
    by_ratio_full = sorted([(ov, pr, pe, (pr / pe)) for ov, pr, pe in candidates if pe > 0], key=lambda x: x[3])
    by_ratio = [t[:3] for t in by_ratio_full[:20]]

    pool = {ov: (pr, pe) for ov, pr, pe in near + by_ratio}
    pool_items = [(ov, pr, pe) for ov, (pr, pe) in pool.items()]

    if not pool_items:
        pool_items = [t[:3] for t in by_ratio_full]

    if not pool_items: return float('inf')

    SCALE, CAP = 100, 100 * 100
    values = {ov: int(round(pe * SCALE)) for ov, pr, pe in pool_items}
    costs = {ov: pr for ov, pr, pe in pool_items}

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

    best_cost = float('inf')
    for c in range(1, 6):
        cost = dp[c].get(CAP, float('inf'))
        if cost < best_cost:
            best_cost = cost

    return best_cost

def estimate_total_cost(base_ovr, target_grade, average_prices, gauge_table, cost_cache):
    SUCCESS_PROBS = [1.00, 0.81, 0.64, 0.50, 0.26, 0.15, 0.07]
    RECOVERY_PROBS = {
        1: {1: 1.00}, 2: {1: 1.00}, 3: {1: 0.65, 2: 0.35},
        4: {1: 0.55, 2: 0.45}, 5: {1: 0.35, 2: 0.40, 3: 0.25},
        6: {1: 0.10, 2: 0.32, 3: 0.36, 4: 0.22},
        7: {1: 0.04, 2: 0.10, 3: 0.30, 4: 0.35, 5: 0.21},
    }
    GRADE_BONUS_MAP = {1: 0, 2: 1, 3: 2, 4: 4, 5: 6, 6: 8, 7: 11}

    memo = {}

    def get_cost_to_reach(current_grade, final_grade):
        if current_grade >= final_grade:
            return 0

        if (current_grade, final_grade) in memo:
            return memo[(current_grade, final_grade)]

        stage = current_grade
        current_card_ovr = base_ovr + GRADE_BONUS_MAP.get(stage, 0)

        cache_key = (current_card_ovr, stage)
        if cache_key in cost_cache:
            material_cost = cost_cache[cache_key]
        else:
            material_cost = get_cheapest_material_cost(current_card_ovr, stage, average_prices, gauge_table)
            cost_cache[cache_key] = material_cost

        if material_cost == float('inf'):
            return float('inf')

        success_prob = SUCCESS_PROBS[stage - 1]
        fail_prob = 1 - success_prob

        recovery_cost = 0
        if fail_prob > 0:
            recovery_options = RECOVERY_PROBS.get(stage, {max(1, stage - 1): 1.0})
            for recovery_grade, prob in recovery_options.items():
                cost_to_recover = get_cost_to_reach(recovery_grade, current_grade)
                if cost_to_recover == float('inf'):
                    return float('inf')
                recovery_cost += prob * cost_to_recover

        cost_after_success = get_cost_to_reach(current_grade + 1, final_grade)
        if cost_after_success == float('inf'):
            return float('inf')

        expected_cost = (material_cost + fail_prob * recovery_cost) / success_prob + cost_after_success

        memo[(current_grade, final_grade)] = expected_cost
        return expected_cost

    return get_cost_to_reach(1, target_grade)


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
    
    # [수정] 오버롤(OVR)이 MIN_OVR(100) 이상인 선수만 걸러냄
    candidate_spids = []
    for p in all_players:
        if 'spid' in p:
            try:
                # overall 값이 없는 경우를 대비해 0으로 처리
                ovr = int(p.get('overall', 0)) 
                if ovr >= MIN_OVR:
                    candidate_spids.append(p['spid'])
            except (ValueError, TypeError):
                pass

    print(f"Processing {len(candidate_spids)} players (OVR {MIN_OVR} 이상)...")

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

                material_cost_only = estimate_total_cost(int(player['overall']), grade, average_prices, gauge_table,
                                                         material_cost_cache)
                if material_cost_only == float('inf'): continue

                total_investment = material_cost_only + price_1
                profit = price_target - total_investment

                # [수지 타산 확인] 50조 이상만 저장
                if profit >= MIN_PROFIT_BP:
                    profit_margin = (profit / total_investment) * 100 if total_investment > 0 else 0
                    deal_data = {
                        'spid': spid,
                        'target_grade': grade,
                        'profit': profit,
                        'profit_margin': profit_margin,
                        'total_investment': total_investment,
                        'price_1': price_1,
                        'all_prices': all_prices,
                    }
                    deals_to_save.append(deal_data)

    with open(OUTPUT_FILENAME, "w", encoding="utf-8") as f:
        json.dump(deals_to_save, f)

    print(f"Update complete. Saved {len(deals_to_save)} profitable deals (OVR >={MIN_OVR}, Profit >={MIN_PROFIT_BP}) to {OUTPUT_FILENAME}.")


if __name__ == "__main__":
    main()
