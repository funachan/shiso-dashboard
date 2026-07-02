"""
宍粟市 追加統計データ自動更新スクリプト

【e-Stat API の正しい使い方】
  getStatsData のレスポンス構造:
    CLASS_INF.CLASS_OBJ[] → カテゴリコード→ラベル のマッピング
    DATA_INF.VALUE[]       → 実際の数値
      VALUE要素の属性: @area(地域コード) @time(時点) @cat01/@cat02(カテゴリコード) $（数値）

  ※ VALUE要素にはテキストラベルは含まれない。コードのみ。
  ※ ラベルはCLASS_INFを参照してコード→ラベル変換が必要。

アプローチ:
  1. searchWord でテーブル一覧を検索（短いキーワードでAND検索）
  2. 最初に cdArea=28221 付きで検索（市区町村レベルのテーブルを優先）
  3. 見つからない場合は cdArea なしで検索
  4. getStatsData で CLASS_INF をパースし、宍粟市(28221)の値を正確に抽出
  5. 宍粟市の値がない場合は兵庫県(28)でフォールバック
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
HYOGO_PREF = "28"      # 兵庫県（フォールバック）
BASE_URL   = "https://api.e-stat.go.jp/rest/3.0/app/json"

SURVEY_SEARCHES = {
    # 学校基本調査: statsCode + searchWord="市町村別集計" で市区町村レベルテーブルを直接検索
    # title_kw でタイトルに学校種別が含まれるテーブルを優先（幼稚園テーブルを除外）
    "school_el": {
        "searchWord": "市町村別集計",
        "statsCode":  "00400001",
        "city_kw":    "市町村",
        "title_kw":   ["小学校", "在学者"],   # リスト: 両方含むテーブルを選ぶ
        "label_kw":   ["在学者", "合計"],
    },
    "school_jh": {
        "searchWord": "市町村別集計",
        "statsCode":  "00400001",
        "city_kw":    "市町村",
        "title_kw":   ["中学校", "在学者"],   # リスト: 両方含むテーブルを選ぶ
        "label_kw":   ["在学者", "合計"],
    },
    # 介護保険: searchWord 1語のみ（複数語のAND検索が意図しない結果を返すため）
    "care": {
        "searchWord": "介護保険",
        "city_kw":    "市区町村",
        "label_kw":   ["合計", "総数", "認定者", "要介護"],
    },
    # 医療施設: 宍粟市に病院がない可能性があるため第２表（一般診療所）を優先
    "medical": {
        "searchWord": "医療施設 市区町村",
        "city_kw":    "市区町村",
        "title_kw":   "一般診療所",
        "label_kw":   ["一般診療所", "合計", "総数"],
    },
    # 農林業センサス: 地方別テーブルが返るため近畿地方テーブルを狙う
    "agri": {
        "searchWord": "農業経営体 近畿",
        "statsCode":  "00500209",
        "city_kw":    "近畿",
        "label_kw":   ["合計", "総数", "農業経営体"],
    },
}


# ────────────────────────────────────────────────
# e-Stat API ヘルパー
# ────────────────────────────────────────────────
def search_tables(search_word: str, limit: int = 30, cd_area: str = None,
                  stats_code: str = None) -> list[dict]:
    """キーワードで統計表一覧を検索。stats_code で調査を絞込み、cd_area で地域フィルタ。"""
    params = {
        "appId":      APP_ID,
        "limit":      limit,
    }
    if search_word:
        params["searchWord"] = search_word
    if stats_code:
        params["statsCode"] = stats_code
    if cd_area:
        params["cdArea"] = cd_area

    try:
        resp = requests.get(f"{BASE_URL}/getStatsList", params=params, timeout=45)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [ERROR] getStatsList 失敗: {e}")
        return []

    result = data.get("GET_STATS_LIST", {})
    status = result.get("RESULT", {}).get("STATUS", -1)
    if status != 0:
        return []  # データなし

    tables = result.get("DATALIST_INF", {}).get("TABLE_INF", [])
    if isinstance(tables, dict):
        tables = [tables]
    return tables


def get_stats_data(stats_data_id: str, area: str = None) -> dict | None:
    """statsDataId でデータを取得。areaで地域フィルタ。"""
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
        print(f"  [ERROR] getStatsData 失敗: {e}")
        return None

    result = data.get("GET_STATS_DATA", {})
    status = result.get("RESULT", {}).get("STATUS", -1)
    if status != 0:
        return None
    return result


def get_title_str(t: dict) -> str:
    title = t.get("TITLE", "")
    if isinstance(title, dict):
        return title.get("$", "")
    return str(title)


def sort_key(t: dict) -> str:
    return str(t.get("SURVEY_DATE", "0"))


def parse_class_inf(raw: dict) -> dict:
    """
    CLASS_INF から (dim_id, code) → label のマッピングを返す。
    例: {("cat01", "001"): "病院", ("area", "28221"): "宍粟市", ...}
    """
    stat_data = raw.get("STATISTICAL_DATA", {})
    class_objs = stat_data.get("CLASS_INF", {}).get("CLASS_OBJ", [])
    if isinstance(class_objs, dict):
        class_objs = [class_objs]

    mapping = {}
    for obj in class_objs:
        dim_id = obj.get("@id", "")
        classes = obj.get("CLASS", [])
        if isinstance(classes, dict):
            classes = [classes]
        for c in classes:
            code  = c.get("@code", "")
            label = c.get("@name", "")
            mapping[(dim_id, code)] = label

    return mapping


def extract_value(raw: dict, target_area: str, label_keywords: list[str]) -> tuple[int | None, str | None]:
    """
    e-Stat API レスポンスから値を正しく抽出する。

    1. CLASS_INF をパースしてコード→ラベル変換テーブルを作成
    2. target_area (例: "28221") の VALUE に絞り込む
    3. label_keywords にマッチするカテゴリの値を返す
    4. マッチしなければ最初の有効な数値を返す

    Returns: (value_int, time_str) または (None, None)
    """
    stat_data = raw.get("STATISTICAL_DATA", {})

    # CLASS_INF パース
    class_map = parse_class_inf(raw)

    # area次元のコードリスト
    area_codes = {k[1] for k, v in class_map.items() if k[0] == "area"}
    has_target = target_area in area_codes
    if area_codes:
        print(f"    エリア次元: {len(area_codes)}件 | {target_area}({('宍粟市' if target_area==SHISO_AREA else '兵庫県')})含む: {has_target}")

    # VALUES 取得
    values = stat_data.get("DATA_INF", {}).get("VALUE", [])
    if isinstance(values, dict):
        values = [values]

    # target_area でフィルタ
    area_filtered = [v for v in values if v.get("@area") == target_area]
    if area_filtered:
        values = area_filtered
        print(f"    → {target_area} の値: {len(values)}件")
    else:
        if area_codes:
            print(f"    → {target_area} の値なし（エリア次元あるが該当コードなし）")
        else:
            print(f"    → エリア次元なし（地域別集計表でない可能性）")
        return None, None

    # カテゴリラベルでマッチング
    for v in values:
        # このVALUEの全カテゴリのラベルを結合
        cat_labels = ""
        for dim in ["cat01", "cat02", "cat03", "cat04"]:
            code = v.get(f"@{dim}", "")
            if code:
                cat_labels += class_map.get((dim, code), "")

        for kw in label_keywords:
            if kw in cat_labels or not cat_labels:  # カテゴリなしテーブルは全て対象
                try:
                    val = str(v.get("$", "")).strip()
                    if val and val not in ("-", "***", "X", "…", ""):
                        time_code = v.get("@time", "")
                        return int(val.replace(",", "")), time_code
                except (ValueError, AttributeError):
                    pass

    # キーワードマッチなし → 最初の有効値
    for v in values:
        try:
            val = str(v.get("$", "")).strip()
            if val and val not in ("-", "***", "X", "…", ""):
                return int(val.replace(",", "")), v.get("@time", "")
        except (ValueError, AttributeError):
            pass

    return None, None


def find_best_table(tables: list[dict], city_kw: str, title_kw: str = None) -> dict | None:
    """市区町村レベルのテーブルを優先して選択。title_kw でタイトルをさらに絞り込む。"""
    city_tables = [
        t for t in tables
        if city_kw in t.get("STATISTICS_NAME", "") or city_kw in get_title_str(t)
    ]
    # title_kw でタイトルフィルタ（str または list）
    if title_kw and city_tables:
        kws = [title_kw] if isinstance(title_kw, str) else list(title_kw)
        # 全キーワードが含まれるテーブルを優先
        all_match = [t for t in city_tables if all(kw in get_title_str(t) for kw in kws)]
        if all_match:
            city_tables = all_match
        else:
            # 一部でもマッチするものにフォールバック
            any_match = [t for t in city_tables if any(kw in get_title_str(t) for kw in kws)]
            if any_match:
                city_tables = any_match
                print(f"  （title_kw={kws} 部分一致で{len(any_match)}件）")
            else:
                print(f"  （title_kw={kws} に一致なし、全市区町村テーブルを対象）")

    if city_tables:
        best = max(city_tables, key=sort_key)
        print(f"  ★市区町村テーブル: {get_title_str(best)} [{best.get('@id')}]")
        return best

    if tables:
        best = max(tables, key=sort_key)
        print(f"  → フォールバック: {get_title_str(best)} [{best.get('@id')}]")
        return best

    return None


def print_found_tables(tables: list[dict], n: int = 5) -> None:
    print(f"  発見テーブル数: {len(tables)}")
    city_tables = [
        t for t in tables
        if "市区町村" in t.get("STATISTICS_NAME", "") or "市区町村" in get_title_str(t)
        or "市町村" in t.get("STATISTICS_NAME", "") or "市町村" in get_title_str(t)
    ]
    if city_tables:
        print(f"  ★市区町村候補: {len(city_tables)}件")
        for t in city_tables[:n]:
            sc = t.get("STAT_CODE", t.get("GOV_ORG", {}).get("@code", "—"))
            print(f"    ID={t.get('@id','—')} | {t.get('SURVEY_DATE','')} | {sc} | {get_title_str(t)[:60]}")
    else:
        print(f"  （市区町村テーブルなし）先頭{n}件:")
        for t in tables[:n]:
            sc = t.get("STAT_CODE", t.get("GOV_ORG", {}).get("@code", "—"))
            print(f"    ID={t.get('@id','—')} | {t.get('SURVEY_DATE','')} | {sc} | {get_title_str(t)[:60]}")


def try_fetch(table: dict, target_area: str, label_kw: list[str]) -> tuple[int | None, str | None]:
    """テーブルからデータ取得を試みる"""
    tid = table["@id"]
    raw = get_stats_data(tid, area=target_area)
    time.sleep(1)
    if not raw:
        return None, None
    return extract_value(raw, target_area, label_kw)


def fetch_with_fallback(cfg: dict, search_kw: str) -> tuple[int | None, str | None, str]:
    """
    2段階でテーブルを探してデータ取得:
    1. cdArea=28221 付きでテーブル検索（市区町村レベル優先）
    2. cdArea なしでテーブル検索
    3. 宍粟市レベルで取得、失敗したら兵庫県レベルで取得
    Returns: (value, time_period, data_level)
    """
    sc = cfg.get("statsCode")

    # Phase 1: 宍粟市コード付きでテーブル検索
    tables = search_tables(search_kw, cd_area=SHISO_AREA, stats_code=sc)
    time.sleep(1)
    source_level = "city"

    title_kw = cfg.get("title_kw")

    if tables:
        print(f"  [Phase1] cdArea=28221 付き → {len(tables)}件")
        print_found_tables(tables)
        table = find_best_table(tables, cfg["city_kw"], title_kw)
        if table:
            val, t = try_fetch(table, SHISO_AREA, cfg["label_kw"])
            if val is not None:
                return val, t, source_level

    # Phase 2: cdArea なしでテーブル検索
    tables = search_tables(search_kw, stats_code=sc)
    time.sleep(1)
    if not tables:
        print("  テーブルなし")
        return None, None, ""

    print(f"  [Phase2] cdArea なし → {len(tables)}件")
    print_found_tables(tables)
    table = find_best_table(tables, cfg["city_kw"], title_kw)
    if not table:
        return None, None, ""

    # まず宍粟市で試みる
    val, t = try_fetch(table, SHISO_AREA, cfg["label_kw"])
    if val is not None:
        return val, t, "city"

    # 兵庫県でフォールバック
    print(f"  → 宍粟市データなし。兵庫県(28)でフォールバック")
    val, t = try_fetch(table, HYOGO_PREF, cfg["label_kw"])
    if val is not None:
        return val, t, "pref"

    return None, None, ""


# ────────────────────────────────────────────────
# 各調査のデータ取得
# ────────────────────────────────────────────────
def fetch_school_data() -> dict:
    result = {"year": None, "elementary": None, "junior_high": None,
              "source_name": "学校基本調査", "data_level": ""}

    for label, key in [("小学校", "school_el"), ("中学校", "school_jh")]:
        cfg = SURVEY_SEARCHES[key]
        print(f"\n[学校基本調査 {label}] 検索: {cfg['searchWord']}")
        val, t, level = fetch_with_fallback(cfg, cfg["searchWord"])

        year = str(t)[:4] if t else None
        if result["year"] is None and year:
            result["year"] = year
        if result["data_level"] == "" and level:
            result["data_level"] = level

        if label == "小学校":
            result["elementary"] = val
        else:
            result["junior_high"] = val
        print(f"  → {label} {year}年: {val}人 [レベル:{level}]")

    return result


def fetch_care_data() -> dict:
    result = {"year": None, "certified_total": None,
              "source_name": "介護保険事業状況報告", "data_level": ""}

    cfg = SURVEY_SEARCHES["care"]
    print(f"\n[介護保険] 検索: {cfg['searchWord']}")
    val, t, level = fetch_with_fallback(cfg, cfg["searchWord"])

    result["year"]            = str(t)[:4] if t else None
    result["certified_total"] = val
    result["data_level"]      = level
    print(f"  → 介護認定者 {result['year']}年: {val}人 [レベル:{level}]")
    return result


def fetch_medical_data() -> dict:
    result = {"year": None, "hospitals": None, "clinics": None, "beds": None,
              "source_name": "医療施設調査", "data_level": ""}

    cfg = SURVEY_SEARCHES["medical"]
    sc  = cfg.get("statsCode")
    print(f"\n[医療施設] 検索: {cfg['searchWord']}")

    # 施設数と病床数は別テーブルの可能性があるため、最初に見つかったテーブルから取得
    tables = search_tables(cfg["searchWord"], cd_area=SHISO_AREA, stats_code=sc)
    time.sleep(1)
    if not tables:
        tables = search_tables(cfg["searchWord"], stats_code=sc)
        time.sleep(1)

    if not tables:
        return result

    print_found_tables(tables)
    table = find_best_table(tables, cfg["city_kw"], cfg.get("title_kw"))
    if not table:
        return result

    tid = table["@id"]
    # まず宍粟市で試みる
    for area, level in [(SHISO_AREA, "city"), (HYOGO_PREF, "pref")]:
        raw = get_stats_data(tid, area=area)
        time.sleep(1)
        if not raw:
            continue

        stat_data = raw.get("STATISTICAL_DATA", {})
        class_map = parse_class_inf(raw)
        values = stat_data.get("DATA_INF", {}).get("VALUE", [])
        if isinstance(values, dict):
            values = [values]

        area_filtered = [v for v in values if v.get("@area") == area]
        if not area_filtered:
            print(f"  → {area} の値なし")
            continue

        print(f"  → {area} の値: {len(area_filtered)}件")
        result["data_level"] = level
        result["year"] = str(area_filtered[0].get("@time", ""))[:4] if area_filtered else None

        # 病院数・診療所数・病床数を探す
        for v in area_filtered:
            cat_labels = "".join(
                class_map.get((dim, v.get(f"@{dim}", "")), "")
                for dim in ["cat01", "cat02", "cat03"]
            )
            try:
                val_str = str(v.get("$", "")).strip()
                val_int = int(val_str.replace(",", "")) if val_str and val_str not in ("-","***","X","…") else None
            except ValueError:
                val_int = None

            if val_int is None:
                continue
            if "病院" in cat_labels and result["hospitals"] is None:
                result["hospitals"] = val_int
            elif "一般診療所" in cat_labels and result["clinics"] is None:
                result["clinics"] = val_int
            elif "病床" in cat_labels and result["beds"] is None:
                result["beds"] = val_int

        print(f"  → 病院:{result['hospitals']} 診療所:{result['clinics']} 病床:{result['beds']} [レベル:{level}]")
        break

    return result


def fetch_agri_data() -> dict:
    result = {"year": None, "farm_households": None, "farmers": None,
              "source_name": "農林業センサス", "data_level": ""}

    cfg = SURVEY_SEARCHES["agri"]
    print(f"\n[農林業センサス] 検索: {cfg['searchWord']}")
    val, t, level = fetch_with_fallback(cfg, cfg["searchWord"])

    result["year"]            = str(t)[:4] if t else None
    result["farm_households"] = val
    result["data_level"]      = level
    print(f"  → 農業経営体 {result['year']}年: {val} [レベル:{level}]")
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
  .stat-note { font-size:.7rem; color:#e67e22; margin-top:4px; }
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
function levelNote(d) {
  if (d.data_level === "pref") return '<div class="stat-note">※ 兵庫県全体の値</div>';
  return "";
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
  { label:"農林業センサス", color:"var(--purple)", title:"農業経営体数",
    data: AGRI_DATA,
    rows: d => [["農業経営体数", val(d.farm_households,"経営体")]] },
];

document.getElementById("cards").innerHTML = CARDS.map(c => `
  <div class="card">
    <div class="card-label" style="color:${c.color}">${c.label}</div>
    <div class="card-title">${c.title}</div>
    ${c.rows(c.data).map(([n,v]) => `<div class="stat-row"><span>${n}</span><span>${v}</span></div>`).join("")}
    ${c.data.year ? `<div class="stat-year">調査年：${c.data.year}年</div>` : ""}
    ${levelNote(c.data)}
  </div>`).join("");
</script>
</body>
</html>
"""


def main():
    print("=" * 55)
    print("宍粟市 統計データ自動更新")
    print(f"実行日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 55)

    school  = fetch_school_data()
    care    = fetch_care_data()
    medical = fetch_medical_data()
    agri    = fetch_agri_data()

    print("\n" + "=" * 55)
    print("【取得結果サマリー】")
    print(f"  学校 小学校: {school.get('elementary')}人 / 中学校: {school.get('junior_high')}人 ({school.get('year')}年) [{school.get('data_level')}]")
    print(f"  介護 認定者: {care.get('certified_total')}人 ({care.get('year')}年) [{care.get('data_level')}]")
    print(f"  医療 病院: {medical.get('hospitals')} 診療所: {medical.get('clinics')} ({medical.get('year')}年) [{medical.get('data_level')}]")
    print(f"  農業 経営体: {agri.get('farm_households')} ({agri.get('year')}年) [{agri.get('data_level')}]")

    html = load_html()
    html = inject_data(html, "SCHOOL_DATA",  school)
    html = inject_data(html, "CARE_DATA",    care)
    html = inject_data(html, "MEDICAL_DATA", medical)
    html = inject_data(html, "AGRI_DATA",    agri)
    html = update_timestamp(html)

    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✅ {HTML_FILE} を更新しました")

    # GitHub Actions Step Summary
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write("## 統計データ更新完了\n\n")
            f.write("| データ | 調査年 | 主要指標 | データ範囲 |\n|---|---|---|---|\n")
            lvl = lambda d: "宍粟市" if d.get("data_level")=="city" else ("兵庫県" if d.get("data_level")=="pref" else "—")
            f.write(f"| 学校基本調査 | {school.get('year','—')} | 小{school.get('elementary','—')}人 / 中{school.get('junior_high','—')}人 | {lvl(school)} |\n")
            f.write(f"| 介護保険 | {care.get('year','—')} | 認定者{care.get('certified_total','—')}人 | {lvl(care)} |\n")
            f.write(f"| 医療施設 | {medical.get('year','—')} | 病院{medical.get('hospitals','—')}・診療所{medical.get('clinics','—')} | {lvl(medical)} |\n")
            f.write(f"| 農林業 | {agri.get('year','—')} | 農業経営体{agri.get('farm_households','—')} | {lvl(agri)} |\n")


if __name__ == "__main__":
    main()
