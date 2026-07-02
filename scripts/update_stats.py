"""
宍粟市 追加統計データ自動更新スクリプト
e-Stat API から以下のデータを取得し、shiso_stats_dashboard.html を更新する。

アプローチ:
  statsCode（調査コード）ではなく searchWord（キーワード）で検索する。
  市区町村レベルのテーブルを優先して探し、見つかったIDをログに出力する。

対象データ:
  - 学校基本調査（小中学校児童生徒数）
  - 介護保険事業状況報告（要介護認定者数）
  - 医療施設調査（医療施設数・病床数）
  - 農林業センサス（農家数・農業就業者数）
"""

import json
import os
import re
import time
from datetime import datetime

import requests

# ────────────────────────────────────────────────
# 設定
# ────────────────────────────────────────────────
APP_ID     = os.environ["ESTAT_APP_ID"]
SHISO_AREA = "28221"   # 宍粟市
HYOGO_PREF = "28"      # 兵庫県（フォールバック用）
BASE_URL   = "https://api.e-stat.go.jp/rest/3.0/app/json"

# 各調査のキーワード検索設定
# searchWord: e-Stat getStatsList の AND 検索（単語数を絞ること！）
# city_kw:    ローカルで市区町村レベルを絞り込むキーワード
SURVEY_SEARCHES = {
    "school_el": {
        "searchWord": "学校基本調査 小学校",
        "city_kw":    "市区町村",
        "fallback_kw":"小学校",
    },
    "school_jh": {
        "searchWord": "学校基本調査 中学校",
        "city_kw":    "市区町村",
        "fallback_kw":"中学校",
    },
    "care": {
        "searchWord": "介護保険",
        "city_kw":    "市区町村",
        "fallback_kw":"認定",
    },
    "medical": {
        "searchWord": "医療施設調査",
        "city_kw":    "市区町村",
        "fallback_kw":"診療所",
    },
    "agri": {
        "searchWord": "農林業センサス",
        "city_kw":    "市区町村",
        "fallback_kw":"農業経営体",
    },
}


# ────────────────────────────────────────────────
# e-Stat API ヘルパー
# ────────────────────────────────────────────────
def search_tables(search_word: str, limit: int = 50) -> list[dict]:
    """キーワードで統計表一覧を検索"""
    params = {
        "appId":      APP_ID,
        "searchWord": search_word,
        "limit":      limit,
    }
    try:
        resp = requests.get(f"{BASE_URL}/getStatsList", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [ERROR] API呼び出し失敗: {e}")
        return []

    result = data.get("GET_STATS_LIST", {})
    status = result.get("RESULT", {}).get("STATUS", -1)
    if status != 0:
        msg = result.get("RESULT", {}).get("ERROR_MSG", "")
        print(f"  [WARN] getStatsList status={status}: {msg}")
        return []

    tables = result.get("DATALIST_INF", {}).get("TABLE_INF", [])
    if isinstance(tables, dict):
        tables = [tables]
    return tables


def get_stats_data(stats_data_id: str, area: str = None) -> dict | None:
    """statsDataId でデータを取得"""
    params = {
        "appId":       APP_ID,
        "statsDataId": stats_data_id,
        "metaGetFlg":  "Y",
        "cntGetFlg":   "N",
    }
    if area:
        params["cdArea"] = area

    try:
        resp = requests.get(f"{BASE_URL}/getStatsData", params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [ERROR] getStatsData失敗: {e}")
        return None

    result = data.get("GET_STATS_DATA", {})
    status = result.get("RESULT", {}).get("STATUS", -1)
    if status != 0:
        print(f"  [WARN] getStatsData status={status}")
        return None
    return result


def get_title_str(t: dict) -> str:
    title = t.get("TITLE", "")
    if isinstance(title, dict):
        return title.get("$", "")
    return str(title)


def find_best_table(tables: list[dict], city_kw: str, fallback_kw: str) -> dict | None:
    """市区町村レベルのテーブルを優先して選択。なければキーワードで絞込み"""
    def sort_key(t):
        return t.get("SURVEY_DATE", "0")

    # まず市区町村レベルを探す
    city_tables = [
        t for t in tables
        if city_kw in t.get("STATISTICS_NAME", "") or city_kw in get_title_str(t)
    ]
    if city_tables:
        best = max(city_tables, key=sort_key)
        print(f"  → 市区町村レベルのテーブル発見: {get_title_str(best)} [{best.get('@id')}]")
        return best

    # フォールバック: キーワードで絞込み
    kw_tables = [
        t for t in tables
        if fallback_kw in t.get("STATISTICS_NAME", "") or fallback_kw in get_title_str(t)
    ]
    if kw_tables:
        best = max(kw_tables, key=sort_key)
        print(f"  → フォールバックテーブル: {get_title_str(best)} [{best.get('@id')}]")
        return best

    if tables:
        best = max(tables, key=sort_key)
        print(f"  → 先頭テーブル使用: {get_title_str(best)} [{best.get('@id')}]")
        return best

    return None


def extract_first_numeric(values: list) -> int | None:
    """VALUES リストから最初の数値を取得"""
    for v in values:
        try:
            val = v.get("$", "")
            if val and val != "-":
                return int(str(val).replace(",", ""))
        except (ValueError, AttributeError):
            pass
    return None


def extract_value_by_keyword(values: list, keywords: list[str]) -> int | None:
    """キーワードにマッチする値を探す"""
    for v in values:
        name = (
            str(v.get("@name", ""))
            + str(v.get("@cat01", ""))
            + str(v.get("@cat02", ""))
        )
        for kw in keywords:
            if kw in name:
                try:
                    val = v.get("$", "")
                    if val and val != "-":
                        return int(str(val).replace(",", ""))
                except (ValueError, AttributeError):
                    pass
    return None


def print_table_ids(tables: list[dict]) -> None:
    """発見したテーブルIDをログ出力（デバッグ用）"""
    print(f"  発見テーブル数: {len(tables)}")
    city_tables = [t for t in tables if "市区町村" in t.get("STATISTICS_NAME", "") or "市区町村" in get_title_str(t)]
    if city_tables:
        print(f"  ★市区町村レベル候補: {len(city_tables)}件")
        for t in city_tables[:5]:
            print(f"    ID={t.get('@id','—')} | {t.get('SURVEY_DATE','')} | {get_title_str(t)[:70]}")
    else:
        print("  （市区町村レベルなし）先頭5件:")
        for t in tables[:5]:
            tid   = t.get("@id", "—")
            title = get_title_str(t)
            sdate = t.get("SURVEY_DATE", "")
            print(f"    ID={tid} | {sdate} | {title[:70]}")


# ────────────────────────────────────────────────
# 各調査のデータ取得
# ────────────────────────────────────────────────
def fetch_school_data() -> dict:
    """学校基本調査：小中学校の児童生徒数"""
    result = {"year": None, "elementary": None, "junior_high": None, "source_name": "学校基本調査"}

    for label, key in [("小学校", "school_el"), ("中学校", "school_jh")]:
        cfg = SURVEY_SEARCHES[key]
        print(f"\n[学校基本調査 {label}] 検索: {cfg['searchWord']}")
        tables = search_tables(cfg["searchWord"])
        print_table_ids(tables)
        time.sleep(1)

        table = find_best_table(tables, cfg["city_kw"], cfg["fallback_kw"])
        if not table:
            print(f"  → テーブルなし")
            continue

        tid = table["@id"]
        survey_date = table.get("SURVEY_DATE", "")

        # まず宍粟市レベルで試みる
        raw = get_stats_data(tid, area=SHISO_AREA)
        time.sleep(1)
        if not raw:
            print(f"  → 宍粟市レベルのデータなし、スキップ")
            continue

        values = raw.get("STATISTICAL_DATA", {}).get("DATA_INF", {}).get("VALUE", [])
        if isinstance(values, dict):
            values = [values]

        total = extract_value_by_keyword(values, ["合計", "計", "総数", "在学者数"])
        if total is None:
            total = extract_first_numeric(values)

        year = str(survey_date)[:4] if survey_date else None
        if result["year"] is None:
            result["year"] = year
        if label == "小学校":
            result["elementary"] = total
        else:
            result["junior_high"] = total

        print(f"  → {label} {year}年: {total}人")

    return result


def fetch_care_data() -> dict:
    """介護保険事業状況報告：要介護認定者数"""
    result = {"year": None, "certified_total": None, "source_name": "介護保険事業状況報告"}

    cfg = SURVEY_SEARCHES["care"]
    print(f"\n[介護保険] 検索: {cfg['searchWord']}")
    tables = search_tables(cfg["searchWord"])
    print_table_ids(tables)
    time.sleep(1)

    table = find_best_table(tables, cfg["city_kw"], cfg["fallback_kw"])
    if not table:
        print("  → テーブルなし")
        return result

    tid = table["@id"]
    survey_date = table.get("SURVEY_DATE", "")

    raw = get_stats_data(tid, area=SHISO_AREA)
    time.sleep(1)
    if not raw:
        print("  → 宍粟市レベルのデータなし")
        return result

    values = raw.get("STATISTICAL_DATA", {}).get("DATA_INF", {}).get("VALUE", [])
    if isinstance(values, dict):
        values = [values]

    total = extract_value_by_keyword(values, ["合計", "計", "総数", "認定者"])
    if total is None:
        total = extract_first_numeric(values)

    result["year"] = str(survey_date)[:4] if survey_date else None
    result["certified_total"] = total
    print(f"  → 介護認定者 {result['year']}年: {total}人")
    return result


def fetch_medical_data() -> dict:
    """医療施設調査：施設数・病床数"""
    result = {"year": None, "hospitals": None, "clinics": None, "beds": None, "source_name": "医療施設調査"}

    cfg = SURVEY_SEARCHES["medical"]
    print(f"\n[医療施設] 検索: {cfg['searchWord']}")
    tables = search_tables(cfg["searchWord"])
    print_table_ids(tables)
    time.sleep(1)

    table = find_best_table(tables, cfg["city_kw"], cfg["fallback_kw"])
    if not table:
        print("  → テーブルなし")
        return result

    tid = table["@id"]
    survey_date = table.get("SURVEY_DATE", "")

    raw = get_stats_data(tid, area=SHISO_AREA)
    time.sleep(1)
    if not raw:
        print("  → 宍粟市レベルのデータなし")
        return result

    values = raw.get("STATISTICAL_DATA", {}).get("DATA_INF", {}).get("VALUE", [])
    if isinstance(values, dict):
        values = [values]

    result["hospitals"] = extract_value_by_keyword(values, ["病院"])
    result["clinics"]   = extract_value_by_keyword(values, ["診療所", "一般診療所"])
    result["beds"]      = extract_value_by_keyword(values, ["病床"])
    result["year"]      = str(survey_date)[:4] if survey_date else None
    print(f"  → 病院{result['hospitals']} 診療所{result['clinics']} 病床{result['beds']}")
    return result


def fetch_agri_data() -> dict:
    """農林業センサス：農業経営体数"""
    result = {"year": None, "farm_households": None, "farmers": None, "source_name": "農林業センサス"}

    cfg = SURVEY_SEARCHES["agri"]
    print(f"\n[農林業センサス] 検索: {cfg['searchWord']}")
    tables = search_tables(cfg["searchWord"])
    print_table_ids(tables)
    time.sleep(1)

    table = find_best_table(tables, cfg["city_kw"], cfg["fallback_kw"])
    if not table:
        print("  → テーブルなし")
        return result

    tid = table["@id"]
    survey_date = table.get("SURVEY_DATE", "")

    raw = get_stats_data(tid, area=SHISO_AREA)
    time.sleep(1)
    if not raw:
        print("  → 宍粟市レベルのデータなし")
        return result

    values = raw.get("STATISTICAL_DATA", {}).get("DATA_INF", {}).get("VALUE", [])
    if isinstance(values, dict):
        values = [values]

    result["farm_households"] = extract_value_by_keyword(values, ["農業経営体", "農家"])
    result["farmers"]         = extract_value_by_keyword(values, ["就業者", "従事者"])
    result["year"]            = str(survey_date)[:4] if survey_date else None
    print(f"  → 農業経営体{result['farm_households']} 就業者{result['farmers']}")
    return result


# ────────────────────────────────────────────────
# HTML 更新
# ────────────────────────────────────────────────
HTML_FILE = "shiso_stats_dashboard.html"


def load_html() -> str:
    if os.path.exists(HTML_FILE):
        with open(HTML_FILE, encoding="utf-8") as f:
            return f.read()
    return generate_template()


def inject_data(html: str, var_name: str, data: dict) -> str:
    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    pattern  = rf"(const\s+{re.escape(var_name)}\s*=\s*)(\{{[\s\S]*?\}})(\s*;)"
    new_html = re.sub(pattern, rf"\g<1>{json_str}\g<3>", html)
    if new_html == html:
        new_html = html.replace(
            "</script>",
            f"\nconst {var_name} = {json_str};\n</script>",
            1,
        )
    return new_html


def update_timestamp(html: str) -> str:
    now = datetime.now().strftime("%Y年%m月%d日")
    return re.sub(
        r'(<span id="lastUpdated">)([^<]*)(<\/span>)',
        rf'\g<1>{now}\g<3>',
        html,
    )


def generate_template() -> str:
    return """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>宍粟市 統計ダッシュボード</title>
<style>
  :root { --navy:#1a3a6b; --sky:#2e86c1; --green:#27ae60; --orange:#e67e22; --purple:#8e44ad; --bg:#f5f7fa; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:'Hiragino Sans','Meiryo',sans-serif; background:var(--bg); color:#333; }
  header { background:var(--navy); color:#fff; padding:24px 32px; }
  header h1 { font-size:1.5rem; }
  header p  { font-size:.85rem; opacity:.75; margin-top:4px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:20px; padding:28px 32px; }
  .card { background:#fff; border-radius:10px; padding:20px 24px; box-shadow:0 2px 8px rgba(0,0,0,.08); }
  .card-label { font-size:.75rem; font-weight:700; letter-spacing:.05em; }
  .card-title { font-size:1rem; font-weight:600; margin:4px 0 12px; }
  .stat-row { display:flex; justify-content:space-between; align-items:baseline; margin:6px 0; font-size:.9rem; }
  .stat-val  { font-size:1.3rem; font-weight:700; color:var(--navy); }
  .stat-unit { font-size:.75rem; color:#888; margin-left:2px; }
  .stat-year { font-size:.72rem; color:#aaa; margin-top:8px; }
  .na { color:#ccc; font-size:.85rem; }
  footer { text-align:center; color:#aaa; font-size:.75rem; padding:24px; }
  footer a { color:var(--sky); }
</style>
</head>
<body>
<header>
  <h1>宍粟市 統計ダッシュボード</h1>
  <p>最終更新：<span id="lastUpdated">—</span>　　データ出典：政府統計の総合窓口 e-Stat</p>
</header>
<div class="grid" id="cards"></div>
<footer>
  データ出典：<a href="https://www.e-stat.go.jp/" target="_blank">e-Stat（政府統計の総合窓口）</a>
  &nbsp;／&nbsp; 宍粟市（市区町村コード：28221）
</footer>
<script>
const SCHOOL_DATA  = {};
const CARE_DATA    = {};
const MEDICAL_DATA = {};
const AGRI_DATA    = {};

function val(v, unit) {
  if (v === null || v === undefined) return '<span class="na">データなし</span>';
  return `<span class="stat-val">${v.toLocaleString()}</span><span class="stat-unit">${unit}</span>`;
}

const CARDS = [
  { label:"学校基本調査", color:"var(--sky)", title:"小中学校 児童生徒数",
    data: SCHOOL_DATA,
    rows: d => [["小学校", val(d.elementary,"人")],["中学校", val(d.junior_high,"人")]] },
  { label:"介護保険事業状況報告", color:"var(--green)", title:"要介護・要支援認定者数",
    data: CARE_DATA,
    rows: d => [["認定者合計", val(d.certified_total,"人")]] },
  { label:"医療施設調査", color:"var(--orange)", title:"市内医療施設",
    data: MEDICAL_DATA,
    rows: d => [["病院数", val(d.hospitals,"施設")],["診療所数", val(d.clinics,"施設")],["病床数", val(d.beds,"床")]] },
  { label:"農林業センサス", color:"var(--purple)", title:"農業経営体・就業者",
    data: AGRI_DATA,
    rows: d => [["農業経営体数", val(d.farm_households,"経営体")],["農業就業者数", val(d.farmers,"人")]] },
];

document.getElementById("cards").innerHTML = CARDS.map(c => `
  <div class="card">
    <div class="card-label" style="color:${c.color}">${c.label}</div>
    <div class="card-title">${c.title}</div>
    ${c.rows(c.data).map(([n,v]) => `<div class="stat-row"><span>${n}</span><span>${v}</span></div>`).join("")}
    ${c.data.year ? `<div class="stat-year">調査年：${c.data.year}年</div>` : ""}
  </div>`).join("");
</script>
</body>
</html>
"""


def main():
    print("=" * 50)
    print("宍粟市 統計データ自動更新")
    print(f"実行日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    school  = fetch_school_data()
    care    = fetch_care_data()
    medical = fetch_medical_data()
    agri    = fetch_agri_data()

    print("\n" + "=" * 50)
    print("【取得結果サマリー】")
    print(f"  学校 小学校: {school.get('elementary')}人 / 中学校: {school.get('junior_high')}人 ({school.get('year')}年)")
    print(f"  介護 認定者: {care.get('certified_total')}人 ({care.get('year')}年)")
    print(f"  医療 病院: {medical.get('hospitals')} 診療所: {medical.get('clinics')} ({medical.get('year')}年)")
    print(f"  農業 経営体: {agri.get('farm_households')} ({agri.get('year')}年)")

    html = load_html()
    html = inject_data(html, "SCHOOL_DATA",  school)
    html = inject_data(html, "CARE_DATA",    care)
    html = inject_data(html, "MEDICAL_DATA", medical)
    html = inject_data(html, "AGRI_DATA",    agri)
    html = update_timestamp(html)

    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✅ {HTML_FILE} を更新しました")

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write("## 統計データ更新完了\n\n")
            f.write("| データ | 調査年 | 主要指標 |\n|---|---|---|\n")
            f.write(f"| 学校基本調査 | {school.get('year','—')} | 小学校{school.get('elementary','—')}人 / 中学校{school.get('junior_high','—')}人 |\n")
            f.write(f"| 介護保険 | {care.get('year','—')} | 認定者{care.get('certified_total','—')}人 |\n")
            f.write(f"| 医療施設 | {medical.get('year','—')} | 病院{medical.get('hospitals','—')}・診療所{medical.get('clinics','—')} |\n")
            f.write(f"| 農林業 | {agri.get('year','—')} | 農業経営体{agri.get('farm_households','—')} |\n")


if __name__ == "__main__":
    main()
