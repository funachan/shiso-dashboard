"""
宍粟市 追加統計データ自動更新スクリプト
e-Stat API から以下のデータを取得し、shiso_stats_dashboard.html を更新する。

対象データ:
  - 学校基本調査（小中学校児童生徒数）
  - 介護保険事業状況報告（要介護認定者数）
  - 医療施設調査（医療施設数・病床数）
  - 農林業センサス（農家数・農業就業者数）
"""

import json
import os
import re
import sys
import time
from datetime import datetime

import requests

# ────────────────────────────────────────────────
# 設定
# ────────────────────────────────────────────────
APP_ID      = os.environ["ESTAT_APP_ID"]   # GitHub Secrets に設定済み
SHISO_AREA  = "28221"                       # 宍粟市
BASE_URL    = "https://api.e-stat.go.jp/rest/3.0/app/json"

# 統計調査コード（e-Stat の調査識別子）
SURVEY_CODES = {
    "school":   "00400001",   # 学校基本調査
    "care":     "00450012",   # 介護保険事業状況報告
    "medical":  "00450011",   # 医療施設調査
    "agri":     "00500209",   # 農林業センサス
}

# ────────────────────────────────────────────────
# e-Stat API ヘルパー
# ────────────────────────────────────────────────
def get_stats_list(stats_code: str, limit: int = 20) -> list[dict]:
    """調査コードで統計表一覧を取得（新しい順）
    ※テーブル一覧はエリア絞り込みなしで検索する。
      cdArea 絞り込みをすると市区町村データが indexing されていない
      調査でテーブルが見つからないため。
    """
    params = {
        "appId":     APP_ID,
        "statsCode": stats_code,
        "limit":     limit,
        "updatedDate": "2010",   # 2010年以降
    }
    resp = requests.get(f"{BASE_URL}/getStatsList", params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    result = data.get("GET_STATS_LIST", {})
    status = result.get("RESULT", {}).get("STATUS", -1)
    if status != 0:
        print(f"  [WARN] getStatsList status={status}: {result.get('RESULT',{}).get('ERROR_MSG','')}")
        return []

    tables = result.get("DATALIST_INF", {}).get("TABLE_INF", [])
    if isinstance(tables, dict):
        tables = [tables]
    return tables


def get_stats_data(stats_data_id: str) -> dict | None:
    """statsDataId でデータを取得"""
    params = {
        "appId":       APP_ID,
        "statsDataId": stats_data_id,
        "cdArea":      SHISO_AREA,
        "metaGetFlg":  "Y",
        "cntGetFlg":   "N",
        "sectionHeaderFlg": "1",
    }
    resp = requests.get(f"{BASE_URL}/getStatsData", params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    result = data.get("GET_STATS_DATA", {})
    status = result.get("RESULT", {}).get("STATUS", -1)
    if status != 0:
        print(f"  [WARN] getStatsData status={status}")
        return None

    return result


def get_title_str(t: dict) -> str:
    """TITLE フィールドを安全に文字列で返す（str or dict）"""
    title = t.get("TITLE", "")
    if isinstance(title, dict):
        return title.get("$", "")
    return str(title)


def find_latest_table(tables: list[dict], keyword: str = "") -> dict | None:
    """キーワードで絞り込んで最新のテーブルを返す"""
    if keyword:
        filtered = [t for t in tables if keyword in t.get("STATISTICS_NAME", "")
                    or keyword in get_title_str(t)]
        if filtered:
            tables = filtered

    # SURVEY_DATE（調査年月）が最大のものを選択
    def sort_key(t):
        return t.get("SURVEY_DATE", "0")

    return max(tables, key=sort_key) if tables else None


# ────────────────────────────────────────────────
# 各調査のデータ取得
# ────────────────────────────────────────────────
def fetch_school_data() -> dict:
    """学校基本調査：小中学校の児童生徒数"""
    print("[学校基本調査] テーブル検索中...")
    tables = get_stats_list(SURVEY_CODES["school"])
    time.sleep(1)

    # 小学校・中学校のデータを探す
    el_table = find_latest_table(tables, "小学校")
    jh_table = find_latest_table(tables, "中学校")

    result = {"year": None, "elementary": None, "junior_high": None, "source_name": "学校基本調査"}

    for label, table in [("小学校", el_table), ("中学校", jh_table)]:
        if not table:
            print(f"  {label}のテーブルが見つかりません")
            continue

        tid = table["@id"]
        title = get_title_str(table) or tid
        survey_date = table.get("SURVEY_DATE", "")
        print(f"  {label}: {title} ({survey_date})")

        raw = get_stats_data(tid)
        time.sleep(1)
        if not raw:
            continue

        values = raw.get("STATISTICAL_DATA", {}).get("DATA_INF", {}).get("VALUE", [])
        if isinstance(values, dict):
            values = [values]

        # 合計（在学者数）を探す
        total = None
        for v in values:
            cat = str(v.get("@cat01", "")) + str(v.get("@cat02", ""))
            text = str(v.get("@name", "")) + cat
            if "計" in text or "合計" in text or "総数" in text:
                try:
                    total = int(v.get("$", "").replace(",", ""))
                    break
                except (ValueError, AttributeError):
                    pass

        # 合計が見つからなければ最初の数値
        if total is None and values:
            try:
                total = int(values[0].get("$", "").replace(",", ""))
            except (ValueError, AttributeError):
                pass

        year = str(survey_date)[:4] if survey_date else None
        if result["year"] is None:
            result["year"] = year

        if label == "小学校":
            result["elementary"] = total
        else:
            result["junior_high"] = total

    return result


def fetch_care_data() -> dict:
    """介護保険事業状況報告：要介護認定者数"""
    print("[介護保険状況報告] テーブル検索中...")
    tables = get_stats_list(SURVEY_CODES["care"])
    time.sleep(1)

    result = {"year": None, "certified_total": None, "source_name": "介護保険事業状況報告"}

    table = find_latest_table(tables, "認定")
    if not table:
        table = find_latest_table(tables)
    if not table:
        print("  テーブルが見つかりません")
        return result

    tid = table["@id"]
    title = get_title_str(table) or tid
    survey_date = table.get("SURVEY_DATE", "")
    print(f"  使用テーブル: {title} ({survey_date})")

    raw = get_stats_data(tid)
    time.sleep(1)
    if not raw:
        return result

    values = raw.get("STATISTICAL_DATA", {}).get("DATA_INF", {}).get("VALUE", [])
    if isinstance(values, dict):
        values = [values]

    total = None
    for v in values:
        name = str(v.get("@name", "")) + str(v.get("@cat01", ""))
        if "合計" in name or "計" in name or "総数" in name or "認定者数" in name:
            try:
                total = int(v.get("$", "").replace(",", ""))
                break
            except (ValueError, AttributeError):
                pass

    if total is None and values:
        try:
            total = int(values[0].get("$", "").replace(",", ""))
        except (ValueError, AttributeError):
            pass

    result["year"] = str(survey_date)[:4] if survey_date else None
    result["certified_total"] = total
    return result


def fetch_medical_data() -> dict:
    """医療施設調査：施設数・病床数"""
    print("[医療施設調査] テーブル検索中...")
    tables = get_stats_list(SURVEY_CODES["medical"])
    time.sleep(1)

    result = {
        "year": None,
        "hospitals": None,
        "clinics": None,
        "beds": None,
        "source_name": "医療施設調査"
    }

    table = find_latest_table(tables)
    if not table:
        print("  テーブルが見つかりません")
        return result

    tid = table["@id"]
    title = get_title_str(table) or tid
    survey_date = table.get("SURVEY_DATE", "")
    print(f"  使用テーブル: {title} ({survey_date})")

    raw = get_stats_data(tid)
    time.sleep(1)
    if not raw:
        return result

    values = raw.get("STATISTICAL_DATA", {}).get("DATA_INF", {}).get("VALUE", [])
    if isinstance(values, dict):
        values = [values]

    for v in values:
        name = str(v.get("@name", "")) + str(v.get("@cat01", "")) + str(v.get("@cat02", ""))
        try:
            val = int(v.get("$", "").replace(",", ""))
        except (ValueError, AttributeError):
            continue

        if "病院" in name and "施設数" in name and result["hospitals"] is None:
            result["hospitals"] = val
        elif "診療所" in name and "施設数" in name and result["clinics"] is None:
            result["clinics"] = val
        elif "病床数" in name and result["beds"] is None:
            result["beds"] = val

    result["year"] = str(survey_date)[:4] if survey_date else None
    return result


def fetch_agri_data() -> dict:
    """農林業センサス：農家数・農業就業人口"""
    print("[農林業センサス] テーブル検索中...")
    tables = get_stats_list(SURVEY_CODES["agri"])
    time.sleep(1)

    result = {
        "year": None,
        "farm_households": None,
        "farmers": None,
        "source_name": "農林業センサス"
    }

    table = find_latest_table(tables)
    if not table:
        print("  テーブルが見つかりません")
        return result

    tid = table["@id"]
    title = get_title_str(table) or tid
    survey_date = table.get("SURVEY_DATE", "")
    print(f"  使用テーブル: {title} ({survey_date})")

    raw = get_stats_data(tid)
    time.sleep(1)
    if not raw:
        return result

    values = raw.get("STATISTICAL_DATA", {}).get("DATA_INF", {}).get("VALUE", [])
    if isinstance(values, dict):
        values = [values]

    for v in values:
        name = str(v.get("@name", "")) + str(v.get("@cat01", ""))
        try:
            val = int(v.get("$", "").replace(",", ""))
        except (ValueError, AttributeError):
            continue

        if "農家数" in name or "農業経営体" in name:
            if result["farm_households"] is None:
                result["farm_households"] = val
        elif "就業者" in name or "従事者" in name:
            if result["farmers"] is None:
                result["farmers"] = val

    result["year"] = str(survey_date)[:4] if survey_date else None
    return result


# ────────────────────────────────────────────────
# HTML 更新
# ────────────────────────────────────────────────
HTML_FILE = "shiso_stats_dashboard.html"


def load_html() -> str:
    """既存の HTML を読み込む（なければテンプレートを返す）"""
    if os.path.exists(HTML_FILE):
        with open(HTML_FILE, encoding="utf-8") as f:
            return f.read()
    return generate_template()


def inject_data(html: str, var_name: str, data: dict) -> str:
    """HTML 内の JS 変数を上書き"""
    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    pattern  = rf"(const\s+{re.escape(var_name)}\s*=\s*)(\{{[\s\S]*?\}})(\s*;)"
    replacement = rf"\g<1>{json_str}\g<3>"
    new_html = re.sub(pattern, replacement, html)
    if new_html == html:
        # 変数が見つからない場合は末尾の </script> の直前に挿入
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
    """HTML テンプレートを生成"""
    return """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>宍粟市 統計ダッシュボード</title>
<style>
  :root { --navy:#1a3a6b; --sky:#2e86c1; --green:#27ae60; --orange:#e67e22; --red:#c0392b; --bg:#f5f7fa; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:'Hiragino Sans','Meiryo',sans-serif; background:var(--bg); color:#333; }
  header { background:var(--navy); color:#fff; padding:24px 32px; }
  header h1 { font-size:1.5rem; }
  header p  { font-size:.85rem; opacity:.75; margin-top:4px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:20px; padding:28px 32px; }
  .card { background:#fff; border-radius:10px; padding:20px 24px; box-shadow:0 2px 8px rgba(0,0,0,.08); }
  .card-label { font-size:.75rem; font-weight:700; color:var(--sky); letter-spacing:.05em; text-transform:uppercase; }
  .card-title { font-size:1rem; font-weight:600; margin:4px 0 12px; }
  .stat-row { display:flex; justify-content:space-between; align-items:baseline; margin:6px 0; font-size:.9rem; }
  .stat-val  { font-size:1.3rem; font-weight:700; color:var(--navy); }
  .stat-unit { font-size:.75rem; color:#888; margin-left:2px; }
  .stat-year { font-size:.72rem; color:#aaa; }
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
const SCHOOL_DATA = {};
const CARE_DATA   = {};
const MEDICAL_DATA = {};
const AGRI_DATA   = {};

function val(v, unit) {
  if (v === null || v === undefined) return '<span class="na">データなし</span>';
  return `<span class="stat-val">${v.toLocaleString()}</span><span class="stat-unit">${unit}</span>`;
}

function renderCards() {
  const cards = [
    {
      label: "学校基本調査",
      color: "#2e86c1",
      title: "小中学校 児童生徒数",
      year: SCHOOL_DATA.year,
      rows: [
        { name: "小学校", value: val(SCHOOL_DATA.elementary, "人") },
        { name: "中学校", value: val(SCHOOL_DATA.junior_high, "人") },
      ]
    },
    {
      label: "介護保険事業状況報告",
      color: "#27ae60",
      title: "要介護・要支援認定者数",
      year: CARE_DATA.year,
      rows: [
        { name: "認定者合計", value: val(CARE_DATA.certified_total, "人") },
      ]
    },
    {
      label: "医療施設調査",
      color: "#e67e22",
      title: "市内医療施設",
      year: MEDICAL_DATA.year,
      rows: [
        { name: "病院数",   value: val(MEDICAL_DATA.hospitals, "施設") },
        { name: "診療所数", value: val(MEDICAL_DATA.clinics, "施設") },
        { name: "病床数",   value: val(MEDICAL_DATA.beds, "床") },
      ]
    },
    {
      label: "農林業センサス",
      color: "#8e44ad",
      title: "農業経営体・就業者",
      year: AGRI_DATA.year,
      rows: [
        { name: "農業経営体数", value: val(AGRI_DATA.farm_households, "経営体") },
        { name: "農業就業者数", value: val(AGRI_DATA.farmers, "人") },
      ]
    },
  ];

  const container = document.getElementById("cards");
  container.innerHTML = cards.map(c => `
    <div class="card">
      <div class="card-label" style="color:${c.color}">${c.label}</div>
      <div class="card-title">${c.title}</div>
      ${c.rows.map(r => `
        <div class="stat-row">
          <span>${r.name}</span>
          <span>${r.value}</span>
        </div>
      `).join("")}
      ${c.year ? `<div class="stat-year" style="margin-top:8px">調査年：${c.year}年</div>` : ""}
    </div>
  `).join("");
}

renderCards();
</script>
</body>
</html>
"""


def main():
    print("=" * 50)
    print("宍粟市 統計データ自動更新")
    print(f"実行日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    # 各調査のデータ取得
    school  = fetch_school_data()
    care    = fetch_care_data()
    medical = fetch_medical_data()
    agri    = fetch_agri_data()

    print("\n【取得結果】")
    print(f"  学校: {school}")
    print(f"  介護: {care}")
    print(f"  医療: {medical}")
    print(f"  農業: {agri}")

    # HTML 更新
    html = load_html()
    html = inject_data(html, "SCHOOL_DATA",  school)
    html = inject_data(html, "CARE_DATA",    care)
    html = inject_data(html, "MEDICAL_DATA", medical)
    html = inject_data(html, "AGRI_DATA",    agri)
    html = update_timestamp(html)

    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n✅ {HTML_FILE} を更新しました")

    # GitHub Actions のサマリー出力
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write("## 統計データ更新完了\n\n")
            f.write(f"| データ | 調査年 | 主要指標 |\n|---|---|---|\n")
            f.write(f"| 学校基本調査 | {school.get('year','—')} | 小学校{school.get('elementary','—')}人 / 中学校{school.get('junior_high','—')}人 |\n")
            f.write(f"| 介護保険 | {care.get('year','—')} | 認定者{care.get('certified_total','—')}人 |\n")
            f.write(f"| 医療施設 | {medical.get('year','—')} | 病院{medical.get('hospitals','—')}・診療所{medical.get('clinics','—')} |\n")
            f.write(f"| 農林業 | {agri.get('year','—')} | 農業経営体{agri.get('farm_households','—')} |\n")


if __name__ == "__main__":
    main()
