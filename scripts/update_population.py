#!/usr/bin/env python3
"""
宍粟市 人口ダッシュボード 自動更新スクリプト
================================================
機能:
  1. 宍粟市HPから月次人口データを取得（住民基本台帳）
  2. e-Stat APIから人口動態データを取得（出生・死亡・婚姻・離婚）
  3. shiso_population_dashboard.html を自動更新

実行方法:
  pip install requests beautifulsoup4
  ESTAT_APP_ID=xxxxxxxx python scripts/update_population.py

環境変数:
  ESTAT_APP_ID  e-Stat APIのアプリケーションID（必須）
"""

import re
import os
import sys
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime

# ================================================================
#  設定
# ================================================================
BASE_URL   = "https://www.city.shiso.lg.jp"
INDEX_URL  = f"{BASE_URL}/soshiki/shiminseikatsu/shimin/tantojoho/jinkoutokei/index.html"
ESTAT_APP_ID   = os.environ.get("ESTAT_APP_ID", "")
ESTAT_STATS_ID = "0003411564"
AREA_CODE      = "28221"  # 宍粟市

# このスクリプトから見たHTMLファイルのパス
HTML_PATH = os.path.join(os.path.dirname(__file__), "..", "shiso_population_dashboard.html")


# ================================================================
#  元号 → 西暦 変換
# ================================================================
def era_to_year(era: str, num: int) -> int:
    return {"令和": 2018, "平成": 1988, "昭和": 1925}.get(era, 0) + num


def parse_date_label(text: str):
    """'令和8年5月31日現在' → '2026-05'、解析できなければ None"""
    m = re.search(r"(令和|平成|昭和)(\d+)年(\d+)月", text)
    if not m:
        return None
    year  = era_to_year(m.group(1), int(m.group(2)))
    month = int(m.group(3))
    return f"{year:04d}-{month:02d}"


# ================================================================
#  宍粟市HP スクレイピング
# ================================================================
def parse_population_table(soup) -> list[dict]:
    """ページ内のテーブルから月次人口データを抽出"""
    results = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue

            date_str = parse_date_label(cells[0].get_text(strip=True))
            if not date_str:
                continue

            # 数値列を抽出
            nums = []
            for cell in cells[1:]:
                raw = cell.get_text(strip=True).replace(",", "").replace("，", "")
                try:
                    nums.append(int(raw))
                except ValueError:
                    nums.append(None)

            # 列構成: 宍粟市(男,女), 山崎(男,女), 一宮(男,女), 波賀(男,女), 千種(男,女) = 10列
            # または: 宍粟市計, 山崎計, 一宮計, 波賀計, 千種計 = 5列
            def safe_sum(a, b):
                return (a or 0) + (b or 0)

            try:
                if len(nums) >= 10:
                    total    = safe_sum(nums[0], nums[1])
                    yamazaki = safe_sum(nums[2], nums[3])
                    ichimiya = safe_sum(nums[4], nums[5])
                    haga     = safe_sum(nums[6], nums[7])
                    chigusa  = safe_sum(nums[8], nums[9])
                elif len(nums) >= 5:
                    total, yamazaki, ichimiya, haga, chigusa = [n or 0 for n in nums[:5]]
                else:
                    continue

                if total > 0:
                    results.append({
                        "date":     date_str,
                        "total":    total,
                        "yamazaki": yamazaki,
                        "ichimiya": ichimiya,
                        "haga":     haga,
                        "chigusa":  chigusa,
                    })
            except Exception as e:
                print(f"  行スキップ ({date_str}): {e}")

    return results


def fetch_year_page(url: str) -> list[dict]:
    """年次ページから月次データを取得"""
    r = requests.get(url, timeout=20)
    r.encoding = "utf-8"
    return parse_population_table(BeautifulSoup(r.text, "html.parser"))


def fetch_city_population(existing_dates: set) -> list[dict]:
    """市HPから既存データにない月のデータをすべて取得"""
    print("【市HP】インデックスページ取得中...")
    r = requests.get(INDEX_URL, timeout=20)
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")

    # リンクから年次ページURLを収集
    year_page_urls = [INDEX_URL]  # インデックス自体も含む
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "jinkoutokei" in href and href.endswith(".html") and "index" not in href:
            full_url = href if href.startswith("http") else BASE_URL + href
            if full_url not in year_page_urls:
                year_page_urls.append(full_url)

    print(f"  発見ページ数: {len(year_page_urls)}")

    all_new = []
    for url in year_page_urls:
        print(f"  取得: {url}")
        try:
            rows = fetch_year_page(url)
            new_rows = [row for row in rows if row["date"] not in existing_dates]
            if new_rows:
                print(f"  → 新規 {len(new_rows)}件: {[r['date'] for r in new_rows]}")
                all_new.extend(new_rows)
            else:
                print(f"  → 新規なし（{len(rows)}件取得済）")
        except Exception as e:
            print(f"  エラー ({url}): {e}")

    return all_new


# ================================================================
#  e-Stat API
# ================================================================
def fetch_estat_vitals(existing_years: set) -> list[dict]:
    """e-Stat APIから宍粟市の人口動態データを取得"""
    if not ESTAT_APP_ID:
        print("【e-Stat】ESTAT_APP_ID が未設定のためスキップ")
        return []

    print("【e-Stat】人口動態データ取得中...")
    url = (
        f"https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData"
        f"?appId={ESTAT_APP_ID}&lang=J&statsDataId={ESTAT_STATS_ID}"
        f"&metaGetFlg=N&cntGetFlg=N&cdArea={AREA_CODE}&replaceSpChars=0"
    )
    r = requests.get(url, timeout=20)
    data = r.json()

    values = data["GET_STATS_DATA"]["STATISTICAL_DATA"]["DATA_INF"]["VALUE"]
    cat_map = {"100": "births", "120": "deaths", "200": "marriages", "210": "divorces"}

    year_data: dict[int, dict] = {}
    for v in values:
        cat = v["@cat01"]
        if cat not in cat_map:
            continue
        year = int(str(v["@time"])[:4])
        raw  = v.get("$", "-")
        try:
            val = int(raw) if raw not in ("-", "***", "…", "・") else 0
        except ValueError:
            val = 0

        if year not in year_data:
            year_data[year] = {"year": year, "births": 0, "deaths": 0,
                               "marriages": 0, "divorces": 0}
        year_data[year][cat_map[cat]] = val

    new_rows = [v for k, v in sorted(year_data.items()) if k not in existing_years]
    if new_rows:
        print(f"  新規年: {[r['year'] for r in new_rows]}")
    else:
        print("  新規データなし")
    return new_rows


# ================================================================
#  HTML の読み込み・更新
# ================================================================
def parse_monthly_from_html(content: str) -> list[dict]:
    pattern = (
        r'\{ date:"(\d{4}-\d{2})", total:(\d+), '
        r'yamazaki:(\d+), ichimiya:(\d+), haga:(\d+), chigusa:(\d+) \}'
    )
    entries = []
    for m in re.finditer(pattern, content):
        entries.append({
            "date":     m.group(1),
            "total":    int(m.group(2)),
            "yamazaki": int(m.group(3)),
            "ichimiya": int(m.group(4)),
            "haga":     int(m.group(5)),
            "chigusa":  int(m.group(6)),
        })
    return entries


def parse_vitals_from_html(content: str) -> list[dict]:
    pattern = (
        r'\{ year:(\d{4}), births:(\d+), deaths:(\d+), '
        r'marriages:(\d+),\s+divorces:(\d+) \}'
    )
    entries = []
    for m in re.finditer(pattern, content):
        entries.append({
            "year":      int(m.group(1)),
            "births":    int(m.group(2)),
            "deaths":    int(m.group(3)),
            "marriages": int(m.group(4)),
            "divorces":  int(m.group(5)),
        })
    return entries


def build_monthly_js(entries: list[dict], updated: str) -> str:
    entries = sorted(entries, key=lambda x: x["date"])
    lines = [f"// 最終自動更新: {updated}"]
    lines.append("const MONTHLY_DATA = [")
    for e in entries:
        lines.append(
            f'  {{ date:"{e["date"]}", total:{e["total"]}, '
            f'yamazaki:{e["yamazaki"]}, ichimiya:{e["ichimiya"]}, '
            f'haga:{e["haga"]}, chigusa:{e["chigusa"]} }},'
        )
    lines.append("];")
    return "\n".join(lines)


def build_vitals_js(entries: list[dict]) -> str:
    entries = sorted(entries, key=lambda x: x["year"])
    lines = ["const VITAL_DATA = ["]
    for e in entries:
        lines.append(
            f'  {{ year:{e["year"]}, births:{e["births"]}, deaths:{e["deaths"]}, '
            f'marriages:{e["marriages"]},  divorces:{e["divorces"]} }},'
        )
    lines.append("];")
    return "\n".join(lines)


def replace_js_block(content: str, start_marker: str, end_marker: str,
                     new_block: str) -> str:
    """start_marker から end_marker までを new_block に置換"""
    pattern = re.escape(start_marker) + r".*?" + re.escape(end_marker)
    replacement = new_block
    result, n = re.subn(pattern, replacement, content, count=1, flags=re.DOTALL)
    if n == 0:
        raise ValueError(f"マーカーが見つかりません: '{start_marker}'")
    return result


def update_html(monthly_all: list[dict], vital_all: list[dict]) -> None:
    with open(HTML_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    now = datetime.now().strftime("%Y-%m-%d")

    # MONTHLY_DATA を置換
    new_monthly = build_monthly_js(monthly_all, now)
    # "// 最終自動更新:" または "const MONTHLY_DATA" から始まり "];" で終わるブロックを置換
    if "// 最終自動更新:" in content:
        content = replace_js_block(content,
            start_marker="// 最終自動更新:",
            end_marker="];",
            new_block=new_monthly)
    else:
        content = replace_js_block(content,
            start_marker="const MONTHLY_DATA = [",
            end_marker="];",
            new_block=new_monthly)

    # VITAL_DATA を置換
    new_vitals = build_vitals_js(vital_all)
    content = replace_js_block(content,
        start_marker="const VITAL_DATA = [",
        end_marker="];",
        new_block=new_vitals)

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"\nHTML更新完了: {HTML_PATH}")


# ================================================================
#  メイン
# ================================================================
def main():
    print("=" * 50)
    print("宍粟市 人口ダッシュボード 自動更新")
    print(f"実行日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    with open(HTML_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    # 既存データを解析
    existing_monthly = parse_monthly_from_html(content)
    existing_monthly_dates = {e["date"] for e in existing_monthly}
    existing_vitals = parse_vitals_from_html(content)
    existing_vital_years = {e["year"] for e in existing_vitals}

    print(f"\n既存 月次データ: {len(existing_monthly)}件 "
          f"({min(existing_monthly_dates)} 〜 {max(existing_monthly_dates)})")
    print(f"既存 人口動態データ: {sorted(existing_vital_years)}\n")

    # 新規データを取得
    monthly_new = fetch_city_population(existing_monthly_dates)
    vital_new   = fetch_estat_vitals(existing_vital_years)

    if not monthly_new and not vital_new:
        print("\n更新データなし。終了します。")
        return

    # マージして更新
    monthly_all = existing_monthly + monthly_new
    vital_all   = existing_vitals + vital_new

    update_html(monthly_all, vital_all)

    print(f"\n  月次データ追加: {len(monthly_new)}件")
    print(f"  人口動態追加: {len(vital_new)}件")


if __name__ == "__main__":
    main()
