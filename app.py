from __future__ import annotations
from datetime import datetime, date
from typing import List, Optional, Dict, Any
import re
import pandas as pd
from flask import Flask, jsonify, Response, request

app = Flask(__name__)

# =========================
# الإعدادات
# =========================
EXCEL_PATH = r"C:\Users\itsno\Desktop\YazAn\تقرير تفاصيل الاعتراضات_تفصيلي_تقرير تفاصيل الاعتراضات_20251217_1405.xlsx"

CUTOFF_ISO = "2025-12-13"
YEAR_OVERRIDE: Optional[int] = None

COL_DATE   = "تاريخ تقديم الاعتراض"
COL_TYPE   = "نوع الرقابة"
COL_DEPT   = "اسم الادارة"
COL_STATUS = "حالة الاعتراض"
COL_MUNI   = "اسم البلدية"

APPROVED_STATUS_VALUE = "مكتمل - مقبول"

# ❌ استبعاد إجادة نهائيًا (بكل أشكالها)
AJADA_KEYWORDS = [
    "إجادة",
    "اجادة",
    "إجاده",
    "اجاده",
]

TOP_TYPES_LIMIT = 50  # لو تبين كل الأنواع خليها 999


# =========================
# Helpers
# =========================
def _norm(s) -> str:
    if pd.isna(s):
        return ""
    return (
        str(s).strip()
        .replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ى", "ي")
        .replace("ة", "ه")
        .lower()
    )

def quarter_of(ts: pd.Timestamp) -> int:
    return (ts.month - 1) // 3 + 1

def quarter_labels_up_to(cutoff: date, year: int) -> List[str]:
    q = (cutoff.month - 1) // 3 + 1
    return [f"{year}-Q{i}" for i in range(1, q + 1)]

def safe_slug(s: str) -> str:
    return re.sub(r"[^a-z0-9\u0600-\u06FF]+", "-", _norm(s)).strip("-") or "x"


# =========================
# قراءة البيانات + حذف إجادة
# =========================
def load_excel_full() -> pd.DataFrame:
    df = pd.read_excel(EXCEL_PATH, engine="openpyxl")
    df.columns = df.columns.astype(str).str.strip()
    return df

def exclude_ajada_everywhere(df: pd.DataFrame) -> pd.DataFrame:
    """
    حذف أي صف يحتوي كلمة إجادة (بكل أشكالها) في أي عمود
    """
    keys = [_norm(k) for k in AJADA_KEYWORDS]
    sn = df.fillna("").astype(str).applymap(_norm)

    mask = False
    for k in keys:
        mask = mask | sn.apply(lambda col: col.str.contains(k, na=False), axis=0).any(axis=1)

    removed = int(mask.sum())
    out = df.loc[~mask].copy()
    out.attrs["ajada_removed_rows"] = removed
    return out

def prepare_df() -> pd.DataFrame:
    cutoff_dt = datetime.strptime(CUTOFF_ISO, "%Y-%m-%d").date()
    year = YEAR_OVERRIDE or cutoff_dt.year

    df_full = load_excel_full()
    df_full = exclude_ajada_everywhere(df_full)  # ✅ حذف إجادة قبل أي شيء

    needed = [COL_DATE, COL_TYPE, COL_DEPT, COL_STATUS, COL_MUNI]
    missing = [c for c in needed if c not in df_full.columns]
    if missing:
        raise ValueError(f"أعمدة ناقصة: {missing}")

    df = df_full[needed].copy()

    df["_dt"] = pd.to_datetime(df[COL_DATE], errors="coerce")
    df = df.dropna(subset=["_dt"])

    start = pd.Timestamp(year=year, month=1, day=1)
    end = pd.Timestamp(cutoff_dt)
    df = df[(df["_dt"] >= start) & (df["_dt"] <= end)]

    df["_yq"] = df["_dt"].apply(lambda x: f"{year}-Q{quarter_of(x)}")

    df["_muni_norm"] = df[COL_MUNI].map(_norm)
    df["_dept_norm"] = df[COL_DEPT].map(_norm)
    df["_type_norm"] = df[COL_TYPE].map(_norm)

    df["_approved"] = df[COL_STATUS].astype(str).str.strip().eq(APPROVED_STATUS_VALUE)

    df.attrs["ajada_removed_rows"] = int(df_full.attrs.get("ajada_removed_rows", 0))
    return df


# =========================
# API
# =========================
def build_options(df: pd.DataFrame) -> Dict[str, List[str]]:
    munis = sorted({str(x).strip() for x in df[COL_MUNI].dropna().unique() if str(x).strip()})
    depts = sorted({str(x).strip() for x in df[COL_DEPT].dropna().unique() if str(x).strip()})
    types = sorted({str(x).strip() for x in df[COL_TYPE].dropna().unique() if str(x).strip()})
    return {"municipalities": munis, "departments": depts, "types": types}

def build_series(g: pd.DataFrame, labels: List[str]) -> Dict[str, List[int]]:
    base = {l: 0 for l in labels}
    total_map = g.groupby("_yq").size().to_dict()
    approved_map = g[g["_approved"]].groupby("_yq").size().to_dict()

    total = base.copy()
    approved = base.copy()

    for k, v in total_map.items():
        if k in total:
            total[k] = int(v)
    for k, v in approved_map.items():
        if k in approved:
            approved[k] = int(v)

    return {"total": [total[l] for l in labels], "approved": [approved[l] for l in labels]}

def build_data(df: pd.DataFrame, muni: str, dept: str, type_: str) -> Dict[str, Any]:
    cutoff_dt = datetime.strptime(CUTOFF_ISO, "%Y-%m-%d").date()
    year = YEAR_OVERRIDE or cutoff_dt.year
    labels = quarter_labels_up_to(cutoff_dt, year)

    sub = df
    if muni != "ALL":
        sub = sub[sub["_muni_norm"] == _norm(muni)]
    if dept != "ALL":
        sub = sub[sub["_dept_norm"] == _norm(dept)]
    if type_ != "ALL":
        sub = sub[sub["_type_norm"] == _norm(type_)]

    if sub.empty:
        return {
            "config": {"labels": labels, "year": year, "cutoff": CUTOFF_ISO, "muni": muni, "dept": dept, "type": type_},
            "cards": [],
            "ajada_removed_rows": int(df.attrs.get("ajada_removed_rows", 0))
        }

    cards: List[Dict[str, Any]] = []

    if type_ == "ALL":
        counts = sub.groupby(COL_TYPE).size().sort_values(ascending=False)
        top = list(counts.head(TOP_TYPES_LIMIT).index.astype(str))

        sub2 = sub.copy()
        sub2["_bucket"] = sub2[COL_TYPE].astype(str).apply(lambda x: x if x in top else "نوع رقابه غير محدد")

        for name, g in sub2.groupby("_bucket"):
            cards.append({"title": str(name), "slug": safe_slug(str(name)), "series": build_series(g, labels)})

        cards.sort(key=lambda c: sum(c["series"]["total"]), reverse=True)
    else:
        cards.append({"title": type_, "slug": safe_slug(type_), "series": build_series(sub, labels)})

    return {
        "config": {"labels": labels, "year": year, "cutoff": CUTOFF_ISO, "muni": muni, "dept": dept, "type": type_},
        "cards": cards,
        "ajada_removed_rows": int(df.attrs.get("ajada_removed_rows", 0))
    }


# =========================
# HTML UI
# =========================
HTML = r"""
<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>لوحة الاعتراضات</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@400;500;600;700&display=swap" rel="stylesheet">

  <style>
    :root{
      --bg-primary:#1a1a1a;
      --bg-card:#242424;
      --text-primary:#f1f5f9;
      --text-secondary:#94a3b8;
      --text-muted:#64748b;
      --border:rgba(148,163,184,.12);
      --shadow:0 20px 40px rgba(0,0,0,.4);
      --approved:rgba(0,123,105,.9);
      --remaining:rgba(71,85,105,.3);

      --select-bg: #1f2937;
      --select-border: rgba(148,163,184,.25);
      --option-bg: #111827;
      --option-fg: #f1f5f9;
    }

    *{box-sizing:border-box;margin:0;padding:0}
    body{
      background:linear-gradient(135deg,var(--bg-primary) 0%, #2a2a2a 100%);
      color:var(--text-primary);
      font-family:'IBM Plex Sans Arabic',system-ui,-apple-system,sans-serif;
      min-height:100vh;
    }

    header{
      border-bottom:1px solid var(--border);
      background:rgba(36,36,36,.8);
      backdrop-filter: blur(20px) saturate(180%);
      position:sticky; top:0; z-index:100;
    }

    .container{max-width:1100px;margin:0 auto;padding:24px}
    .topbarInner{
      display:flex; justify-content:space-between; align-items:flex-end; gap:16px;
      padding-top:6px;
    }
    .titleMain{font-size:20px;font-weight:800}
    .subTitle{color:var(--text-secondary);font-size:13px;margin-top:6px}
    .filters{display:flex; gap:12px; flex-wrap:wrap; margin-top:14px; align-items:center;}

    .select{
      appearance:none; -webkit-appearance:none; -moz-appearance:none;
      background: var(--select-bg);
      border: 1px solid var(--select-border);
      color: var(--text-primary);
      padding: 10px 40px 10px 12px;
      border-radius: 12px;
      min-width: 220px;
      outline: none;
      font-weight: 700;

      background-image:
        linear-gradient(45deg, transparent 50%, var(--text-secondary) 50%),
        linear-gradient(135deg, var(--text-secondary) 50%, transparent 50%);
      background-position:
        calc(18px) calc(50% - 2px),
        calc(12px) calc(50% - 2px);
      background-size: 6px 6px, 6px 6px;
      background-repeat: no-repeat;
    }
    .select option, .select optgroup{ background: var(--option-bg); color: var(--option-fg); }

    .btn{
      background:rgba(0,123,105,.22);
      border:1px solid rgba(0,123,105,.45);
      color:var(--text-primary);
      padding:10px 14px;
      border-radius:12px;
      cursor:pointer;
      font-weight:800;
    }

    .rows{display:flex;flex-direction:column;gap:18px;padding-top:18px}
    .row{
      display:grid; grid-template-columns: 1fr 300px;
      gap:16px; align-items:stretch;
      direction:ltr;
    }
    .row *{direction:rtl}
    @media (max-width:980px){ .row{grid-template-columns:1fr} .container{padding:16px} }

    .card{
      background:var(--bg-card);
      border:1px solid var(--border);
      border-radius:20px;
      box-shadow:var(--shadow);
      padding:18px;
      overflow:hidden;
    }
    .chartHead{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;gap:10px}
    .title{font-size:16px;font-weight:900}
    .canvasWrap{height:240px;margin-top:8px}

    .kpi{display:flex;flex-direction:column;justify-content:center;height:100%;gap:10px;text-align:right;padding:10px}
    .kpiTitle{font-size:13px;color:var(--text-secondary);font-weight:800}
    .kpiValue{font-size:44px;font-weight:900;letter-spacing:-1px}
    .kpiStats{display:flex;gap:10px;margin-top:6px}
    .kpiStat{flex:1;padding:10px;background:rgba(71,85,105,.2);border-radius:12px;border:1px solid var(--border)}
    .kpiStatLabel{font-size:11px;color:var(--text-muted);margin-bottom:4px}
    .kpiStatValue{font-size:16px;font-weight:900}
    .muted{color:var(--text-secondary);font-size:12px}
  </style>
</head>

<body>
<header>
  <div class="container">
    <div class="topbarInner">
      <div>
        <div class="titleMain">لوحة الاعتراضات</div>
        <div class="subTitle" id="subtitle"></div>
        <div class="filters">
          <select id="selMuni" class="select"></select>
          <select id="selDept" class="select"></select>
          <select id="selType" class="select"></select>
          <button class="btn" id="btnApply">تطبيق</button>
        </div>
      </div>
      <div class="muted" id="ajadaInfo"></div>
    </div>
  </div>
</header>

<main class="container">
  <div class="rows" id="rows"></div>
</main>

<script>
let charts = [];

function destroyCharts(){
  charts.forEach(c => c && c.destroy());
  charts = [];
}
function sum(arr){ return arr.reduce((a,b)=>a+b,0); }

function buildSingleBarWithApprovedPart(canvas, labels, total, approved){
  const remaining = total.map((t,i)=> Math.max(0, t - approved[i]));
  const qLabels = labels.map(x => (x.split("-")[1] || x));

  const approvedColor = getComputedStyle(document.documentElement).getPropertyValue('--approved').trim();
  const rest = getComputedStyle(document.documentElement).getPropertyValue('--remaining').trim();

  return new Chart(canvas, {
    type: "bar",
    data: {
      labels: qLabels,
      datasets: [
        { label: "المقبولة", data: approved, stack:"s", backgroundColor: approvedColor, borderRadius: 8, borderSkipped:false },
        { label: "_remaining", data: remaining, stack:"s", backgroundColor: rest, borderRadius: 8, borderSkipped:false }
      ]
    },
    options: {
      responsive:true,
      maintainAspectRatio:false,
      plugins:{
        legend:{ labels:{ filter:(item)=> item.text === "المقبولة" } },
        tooltip:{
          callbacks:{
            label:(ctx)=> ctx.dataset.label === "المقبولة" ? `المقبولة: ${ctx.parsed.y ?? 0}` : null,
            footer:(items)=>{
              const idx = items?.[0]?.dataIndex ?? 0;
              const t = total[idx] ?? 0;
              const a = approved[idx] ?? 0;
              const rate = t ? ((a/t)*100).toFixed(1) : "0.0";
              return `الإجمالي: ${t} | نسبة القبول: ${rate}%`;
            }
          }
        }
      },
      scales:{
        x:{ stacked:true, grid:{display:false}, title:{display:true, text:"الربع"} },
        y:{ stacked:true, beginAtZero:true, ticks:{precision:0}, title:{display:true, text:"عدد الاعتراضات"} }
      }
    }
  });
}

function makeOption(sel, value, text){
  const opt = document.createElement("option");
  opt.value = value;
  opt.textContent = text;
  sel.appendChild(opt);
}

async function loadOptions(){
  const r = await fetch("/options", {cache:"no-store"});
  let j = null;
  try { j = await r.json(); }
  catch(e){
    throw new Error("فشل قراءة JSON من /options (غالباً فيه خطأ 500 في السيرفر).");
  }
  if(!r.ok){
    throw new Error(j?.error || "خطأ غير معروف في /options");
  }

  const selMuni = document.getElementById("selMuni");
  const selDept = document.getElementById("selDept");
  const selType = document.getElementById("selType");

  selMuni.innerHTML = ""; selDept.innerHTML = ""; selType.innerHTML = "";

  makeOption(selMuni, "ALL", "كل البلديات");
  (j.municipalities || []).forEach(x => makeOption(selMuni, x, x));

  makeOption(selDept, "ALL", "كل الإدارات");
  (j.departments || []).forEach(x => makeOption(selDept, x, x));

  makeOption(selType, "ALL", "كل أنواع الرقابة");
  (j.types || []).forEach(x => makeOption(selType, x, x));
}

function renderCards(payload){
  const rows = document.getElementById("rows");
  rows.innerHTML = "";
  destroyCharts();

  // ✅ حذف السطرين اللي طلبتي
  document.getElementById("subtitle").textContent = "";
  document.getElementById("ajadaInfo").textContent = "";

  if(!payload.cards || payload.cards.length === 0){
    const empty = document.createElement("div");
    empty.className = "card";
    empty.textContent = "لا توجد بيانات حسب التصفية الحالية.";
    rows.appendChild(empty);
    return;
  }

  const cfg = payload.config || {};
  const labels = cfg.labels || [];

  payload.cards.forEach(card => {
    const total = card.series.total;
    const approved = card.series.approved;

    const a = sum(approved);
    const t = sum(total);
    const rate = t ? ((a/t)*100).toFixed(1) : "0.0";

    const section = document.createElement("section");
    section.className = "row";

    section.innerHTML = `
      <div class="card">
        <div class="chartHead"><div class="title">${card.title}</div></div>
        <div class="canvasWrap"><canvas id="cv-${card.slug}"></canvas></div>
      </div>

      <aside class="card">
        <div class="kpi">
          <div class="kpiTitle">عدد الاعتراضات المقبولة</div>
          <div class="kpiValue">${a.toLocaleString("en-US")}</div>
          <div class="kpiStats">
            <div class="kpiStat">
              <div class="kpiStatLabel">الإجمالي</div>
              <div class="kpiStatValue">${t.toLocaleString("en-US")}</div>
            </div>
            <div class="kpiStat">
              <div class="kpiStatLabel">نسبة القبول</div>
              <div class="kpiStatValue">${rate}%</div>
            </div>
          </div>
        </div>
      </aside>
    `;

    rows.appendChild(section);

    const canvas = document.getElementById(`cv-${card.slug}`);
    charts.push(buildSingleBarWithApprovedPart(canvas, labels, total, approved));
  });
}

async function loadData(){
  const muni = document.getElementById("selMuni").value;
  const dept = document.getElementById("selDept").value;
  const type = document.getElementById("selType").value;

  const qs = new URLSearchParams({muni, dept, type}).toString();
  const r = await fetch(`/data?${qs}`, {cache:"no-store"});
  let j = null;
  try { j = await r.json(); }
  catch(e){
    throw new Error("فشل قراءة JSON من /data (غالباً فيه خطأ 500 في السيرفر).");
  }
  if(!r.ok){
    throw new Error(j?.error || "خطأ غير معروف في /data");
  }
  renderCards(j);
}

document.getElementById("btnApply").addEventListener("click", async ()=> {
  try { await loadData(); }
  catch(e){
    const rows = document.getElementById("rows");
    rows.innerHTML = `<div class="card">خطأ: ${e.message}</div>`;
  }
});

(async function init(){
  try{
    await loadOptions();
    await loadData();
  }catch(e){
    console.error(e);
    const rows = document.getElementById("rows");
    rows.innerHTML = `<div class="card">خطأ: ${e.message}</div>`;
  }
})();
</script>

</body>
</html>
"""


# =========================
# Routes
# =========================
_df_cache = None

@app.route("/")
def index():
    return Response(HTML, mimetype="text/html; charset=utf-8")


@app.route("/options")
def options():
    try:
        global _df_cache
        if _df_cache is None:
            _df_cache = prepare_df()
        return jsonify(build_options(_df_cache))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/data")
def data():
    try:
        global _df_cache
        if _df_cache is None:
            _df_cache = prepare_df()

        muni = request.args.get("muni", "ALL")
        dept = request.args.get("dept", "ALL")
        type_ = request.args.get("type", "ALL")

        return jsonify(build_data(_df_cache, muni, dept, type_))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("RUNNING → http://127.0.0.1:5000")
    app.run(debug=True)
from __future__ import annotations

import os
from datetime import datetime, date
from typing import List, Optional, Dict, Any
import re

import pandas as pd
from flask import Flask, jsonify, Response, request

app = Flask(__name__)

# =========================
# الإعدادات
# =========================
# ✅ لازم يكون ملف الإكسل داخل نفس فولدر المشروع (نفس فولدر app.py)
# وسمّيه: تقرير_الاعتراضات.xlsx
EXCEL_PATH = os.path.join(os.path.dirname(__file__), "تقرير_الاعتراضات.xlsx")

CUTOFF_ISO = "2025-12-13"
YEAR_OVERRIDE: Optional[int] = None

COL_DATE   = "تاريخ تقديم الاعتراض"
COL_TYPE   = "نوع الرقابة"
COL_DEPT   = "اسم الادارة"
COL_STATUS = "حالة الاعتراض"
COL_MUNI   = "اسم البلدية"

APPROVED_STATUS_VALUE = "مكتمل - مقبول"

# ❌ استبعاد إجادة نهائيًا (بكل أشكالها)
AJADA_KEYWORDS = [
    "إجادة",
    "اجادة",
    "إجاده",
    "اجاده",
]

TOP_TYPES_LIMIT = 50  # لو تبين كل الأنواع خليها 999


# =========================
# Helpers
# =========================
def _norm(s) -> str:
    if pd.isna(s):
        return ""
    return (
        str(s).strip()
        .replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ى", "ي")
        .replace("ة", "ه")
        .lower()
    )

def quarter_of(ts: pd.Timestamp) -> int:
    return (ts.month - 1) // 3 + 1

def quarter_labels_up_to(cutoff: date, year: int) -> List[str]:
    q = (cutoff.month - 1) // 3 + 1
    return [f"{year}-Q{i}" for i in range(1, q + 1)]

def safe_slug(s: str) -> str:
    return re.sub(r"[^a-z0-9\u0600-\u06FF]+", "-", _norm(s)).strip("-") or "x"


# =========================
# قراءة البيانات + حذف إجادة
# =========================
def load_excel_full() -> pd.DataFrame:
    if not os.path.exists(EXCEL_PATH):
        raise FileNotFoundError(
            f"ملف الإكسل غير موجود: {EXCEL_PATH}\n"
            "تأكدي أنك وضعتي ملف (تقرير_الاعتراضات.xlsx) داخل نفس مجلد app.py ورفعتيه مع المشروع."
        )

    df = pd.read_excel(EXCEL_PATH, engine="openpyxl")
    df.columns = df.columns.astype(str).str.strip()
    return df

def exclude_ajada_everywhere(df: pd.DataFrame) -> pd.DataFrame:
    """
    حذف أي صف يحتوي كلمة إجادة (بكل أشكالها) في أي عمود
    """
    keys = [_norm(k) for k in AJADA_KEYWORDS]
    sn = df.fillna("").astype(str).applymap(_norm)

    mask = False
    for k in keys:
        mask = mask | sn.apply(lambda col: col.str.contains(k, na=False), axis=0).any(axis=1)

    removed = int(mask.sum())
    out = df.loc[~mask].copy()
    out.attrs["ajada_removed_rows"] = removed
    return out

def prepare_df() -> pd.DataFrame:
    cutoff_dt = datetime.strptime(CUTOFF_ISO, "%Y-%m-%d").date()
    year = YEAR_OVERRIDE or cutoff_dt.year

    df_full = load_excel_full()
    df_full = exclude_ajada_everywhere(df_full)  # ✅ حذف إجادة قبل أي شيء

    needed = [COL_DATE, COL_TYPE, COL_DEPT, COL_STATUS, COL_MUNI]
    missing = [c for c in needed if c not in df_full.columns]
    if missing:
        raise ValueError(f"أعمدة ناقصة: {missing}")

    df = df_full[needed].copy()

    df["_dt"] = pd.to_datetime(df[COL_DATE], errors="coerce")
    df = df.dropna(subset=["_dt"])

    start = pd.Timestamp(year=year, month=1, day=1)
    end = pd.Timestamp(cutoff_dt)
    df = df[(df["_dt"] >= start) & (df["_dt"] <= end)]

    df["_yq"] = df["_dt"].apply(lambda x: f"{year}-Q{quarter_of(x)}")

    df["_muni_norm"] = df[COL_MUNI].map(_norm)
    df["_dept_norm"] = df[COL_DEPT].map(_norm)
    df["_type_norm"] = df[COL_TYPE].map(_norm)

    df["_approved"] = df[COL_STATUS].astype(str).str.strip().eq(APPROVED_STATUS_VALUE)

    df.attrs["ajada_removed_rows"] = int(df_full.attrs.get("ajada_removed_rows", 0))
    return df


# =========================
# API
# =========================
def build_options(df: pd.DataFrame) -> Dict[str, List[str]]:
    munis = sorted({str(x).strip() for x in df[COL_MUNI].dropna().unique() if str(x).strip()})
    depts = sorted({str(x).strip() for x in df[COL_DEPT].dropna().unique() if str(x).strip()})
    types = sorted({str(x).strip() for x in df[COL_TYPE].dropna().unique() if str(x).strip()})
    return {"municipalities": munis, "departments": depts, "types": types}

def build_series(g: pd.DataFrame, labels: List[str]) -> Dict[str, List[int]]:
    base = {l: 0 for l in labels}
    total_map = g.groupby("_yq").size().to_dict()
    approved_map = g[g["_approved"]].groupby("_yq").size().to_dict()

    total = base.copy()
    approved = base.copy()

    for k, v in total_map.items():
        if k in total:
            total[k] = int(v)
    for k, v in approved_map.items():
        if k in approved:
            approved[k] = int(v)

    return {"total": [total[l] for l in labels], "approved": [approved[l] for l in labels]}

def build_data(df: pd.DataFrame, muni: str, dept: str, type_: str) -> Dict[str, Any]:
    cutoff_dt = datetime.strptime(CUTOFF_ISO, "%Y-%m-%d").date()
    year = YEAR_OVERRIDE or cutoff_dt.year
    labels = quarter_labels_up_to(cutoff_dt, year)

    sub = df
    if muni != "ALL":
        sub = sub[sub["_muni_norm"] == _norm(muni)]
    if dept != "ALL":
        sub = sub[sub["_dept_norm"] == _norm(dept)]
    if type_ != "ALL":
        sub = sub[sub["_type_norm"] == _norm(type_)]

    if sub.empty:
        return {
            "config": {"labels": labels, "year": year, "cutoff": CUTOFF_ISO, "muni": muni, "dept": dept, "type": type_},
            "cards": [],
            "ajada_removed_rows": int(df.attrs.get("ajada_removed_rows", 0))
        }

    cards: List[Dict[str, Any]] = []

    if type_ == "ALL":
        counts = sub.groupby(COL_TYPE).size().sort_values(ascending=False)
        top = list(counts.head(TOP_TYPES_LIMIT).index.astype(str))

        sub2 = sub.copy()
        sub2["_bucket"] = sub2[COL_TYPE].astype(str).apply(lambda x: x if x in top else "نوع رقابه غير محدد")

        for name, g in sub2.groupby("_bucket"):
            cards.append({"title": str(name), "slug": safe_slug(str(name)), "series": build_series(g, labels)})

        cards.sort(key=lambda c: sum(c["series"]["total"]), reverse=True)
    else:
        cards.append({"title": type_, "slug": safe_slug(type_), "series": build_series(sub, labels)})

    return {
        "config": {"labels": labels, "year": year, "cutoff": CUTOFF_ISO, "muni": muni, "dept": dept, "type": type_},
        "cards": cards,
        "ajada_removed_rows": int(df.attrs.get("ajada_removed_rows", 0))
    }


# =========================
# HTML UI
# =========================
HTML = r"""
<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>لوحة الاعتراضات</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@400;500;600;700&display=swap" rel="stylesheet">

  <style>
    :root{
      --bg-primary:#1a1a1a;
      --bg-card:#242424;
      --text-primary:#f1f5f9;
      --text-secondary:#94a3b8;
      --text-muted:#64748b;
      --border:rgba(148,163,184,.12);
      --shadow:0 20px 40px rgba(0,0,0,.4);
      --approved:rgba(0,123,105,.9);
      --remaining:rgba(71,85,105,.3);

      --select-bg: #1f2937;
      --select-border: rgba(148,163,184,.25);
      --option-bg: #111827;
      --option-fg: #f1f5f9;
    }

    *{box-sizing:border-box;margin:0;padding:0}
    body{
      background:linear-gradient(135deg,var(--bg-primary) 0%, #2a2a2a 100%);
      color:var(--text-primary);
      font-family:'IBM Plex Sans Arabic',system-ui,-apple-system,sans-serif;
      min-height:100vh;
    }

    header{
      border-bottom:1px solid var(--border);
      background:rgba(36,36,36,.8);
      backdrop-filter: blur(20px) saturate(180%);
      position:sticky; top:0; z-index:100;
    }

    .container{max-width:1100px;margin:0 auto;padding:24px}
    .topbarInner{
      display:flex; justify-content:space-between; align-items:flex-end; gap:16px;
      padding-top:6px;
    }
    .titleMain{font-size:20px;font-weight:800}
    .subTitle{color:var(--text-secondary);font-size:13px;margin-top:6px}
    .filters{display:flex; gap:12px; flex-wrap:wrap; margin-top:14px; align-items:center;}

    .select{
      appearance:none; -webkit-appearance:none; -moz-appearance:none;
      background: var(--select-bg);
      border: 1px solid var(--select-border);
      color: var(--text-primary);
      padding: 10px 40px 10px 12px;
      border-radius: 12px;
      min-width: 220px;
      outline: none;
      font-weight: 700;

      background-image:
        linear-gradient(45deg, transparent 50%, var(--text-secondary) 50%),
        linear-gradient(135deg, var(--text-secondary) 50%, transparent 50%);
      background-position:
        calc(18px) calc(50% - 2px),
        calc(12px) calc(50% - 2px);
      background-size: 6px 6px, 6px 6px;
      background-repeat: no-repeat;
    }
    .select option, .select optgroup{ background: var(--option-bg); color: var(--option-fg); }

    .btn{
      background:rgba(0,123,105,.22);
      border:1px solid rgba(0,123,105,.45);
      color:var(--text-primary);
      padding:10px 14px;
      border-radius:12px;
      cursor:pointer;
      font-weight:800;
    }

    .rows{display:flex;flex-direction:column;gap:18px;padding-top:18px}
    .row{
      display:grid; grid-template-columns: 1fr 300px;
      gap:16px; align-items:stretch;
      direction:ltr;
    }
    .row *{direction:rtl}
    @media (max-width:980px){ .row{grid-template-columns:1fr} .container{padding:16px} }

    .card{
      background:var(--bg-card);
      border:1px solid var(--border);
      border-radius:20px;
      box-shadow:var(--shadow);
      padding:18px;
      overflow:hidden;
    }
    .chartHead{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;gap:10px}
    .title{font-size:16px;font-weight:900}
    .canvasWrap{height:240px;margin-top:8px}

    .kpi{display:flex;flex-direction:column;justify-content:center;height:100%;gap:10px;text-align:right;padding:10px}
    .kpiTitle{font-size:13px;color:var(--text-secondary);font-weight:800}
    .kpiValue{font-size:44px;font-weight:900;letter-spacing:-1px}
    .kpiStats{display:flex;gap:10px;margin-top:6px}
    .kpiStat{flex:1;padding:10px;background:rgba(71,85,105,.2);border-radius:12px;border:1px solid var(--border)}
    .kpiStatLabel{font-size:11px;color:var(--text-muted);margin-bottom:4px}
    .kpiStatValue{font-size:16px;font-weight:900}
    .muted{color:var(--text-secondary);font-size:12px}
  </style>
</head>

<body>
<header>
  <div class="container">
    <div class="topbarInner">
      <div>
        <div class="titleMain">لوحة الاعتراضات</div>
        <div class="subTitle" id="subtitle"></div>
        <div class="filters">
          <select id="selMuni" class="select"></select>
          <select id="selDept" class="select"></select>
          <select id="selType" class="select"></select>
          <button class="btn" id="btnApply">تطبيق</button>
        </div>
      </div>
      <div class="muted" id="ajadaInfo"></div>
    </div>
  </div>
</header>

<main class="container">
  <div class="rows" id="rows"></div>
</main>

<script>
let charts = [];

function destroyCharts(){
  charts.forEach(c => c && c.destroy());
  charts = [];
}
function sum(arr){ return arr.reduce((a,b)=>a+b,0); }

function buildSingleBarWithApprovedPart(canvas, labels, total, approved){
  const remaining = total.map((t,i)=> Math.max(0, t - approved[i]));
  const qLabels = labels.map(x => (x.split("-")[1] || x));

  const approvedColor = getComputedStyle(document.documentElement).getPropertyValue('--approved').trim();
  const rest = getComputedStyle(document.documentElement).getPropertyValue('--remaining').trim();

  return new Chart(canvas, {
    type: "bar",
    data: {
      labels: qLabels,
      datasets: [
        { label: "المقبولة", data: approved, stack:"s", backgroundColor: approvedColor, borderRadius: 8, borderSkipped:false },
        { label: "_remaining", data: remaining, stack:"s", backgroundColor: rest, borderRadius: 8, borderSkipped:false }
      ]
    },
    options: {
      responsive:true,
      maintainAspectRatio:false,
      plugins:{
        legend:{ labels:{ filter:(item)=> item.text === "المقبولة" } },
        tooltip:{
          callbacks:{
            label:(ctx)=> ctx.dataset.label === "المقبولة" ? `المقبولة: ${ctx.parsed.y ?? 0}` : null,
            footer:(items)=>{
              const idx = items?.[0]?.dataIndex ?? 0;
              const t = total[idx] ?? 0;
              const a = approved[idx] ?? 0;
              const rate = t ? ((a/t)*100).toFixed(1) : "0.0";
              return `الإجمالي: ${t} | نسبة القبول: ${rate}%`;
            }
          }
        }
      },
      scales:{
        x:{ stacked:true, grid:{display:false}, title:{display:true, text:"الربع"} },
        y:{ stacked:true, beginAtZero:true, ticks:{precision:0}, title:{display:true, text:"عدد الاعتراضات"} }
      }
    }
  });
}

function makeOption(sel, value, text){
  const opt = document.createElement("option");
  opt.value = value;
  opt.textContent = text;
  sel.appendChild(opt);
}

async function loadOptions(){
  const r = await fetch("/options", {cache:"no-store"});
  let j = null;
  try { j = await r.json(); }
  catch(e){
    throw new Error("فشل قراءة JSON من /options (غالباً فيه خطأ 500 في السيرفر).");
  }
  if(!r.ok){
    throw new Error(j?.error || "خطأ غير معروف في /options");
  }

  const selMuni = document.getElementById("selMuni");
  const selDept = document.getElementById("selDept");
  const selType = document.getElementById("selType");

  selMuni.innerHTML = ""; selDept.innerHTML = ""; selType.innerHTML = "";

  makeOption(selMuni, "ALL", "كل البلديات");
  (j.municipalities || []).forEach(x => makeOption(selMuni, x, x));

  makeOption(selDept, "ALL", "كل الإدارات");
  (j.departments || []).forEach(x => makeOption(selDept, x, x));

  makeOption(selType, "ALL", "كل أنواع الرقابة");
  (j.types || []).forEach(x => makeOption(selType, x, x));
}

function renderCards(payload){
  const rows = document.getElementById("rows");
  rows.innerHTML = "";
  destroyCharts();

  // ✅ حذف السطرين اللي طلبتي
  document.getElementById("subtitle").textContent = "";
  document.getElementById("ajadaInfo").textContent = "";

  if(!payload.cards || payload.cards.length === 0){
    const empty = document.createElement("div");
    empty.className = "card";
    empty.textContent = "لا توجد بيانات حسب التصفية الحالية.";
    rows.appendChild(empty);
    return;
  }

  const cfg = payload.config || {};
  const labels = cfg.labels || [];

  payload.cards.forEach(card => {
    const total = card.series.total;
    const approved = card.series.approved;

    const a = sum(approved);
    const t = sum(total);
    const rate = t ? ((a/t)*100).toFixed(1) : "0.0";

    const section = document.createElement("section");
    section.className = "row";

    section.innerHTML = `
      <div class="card">
        <div class="chartHead"><div class="title">${card.title}</div></div>
        <div class="canvasWrap"><canvas id="cv-${card.slug}"></canvas></div>
      </div>

      <aside class="card">
        <div class="kpi">
          <div class="kpiTitle">عدد الاعتراضات المقبولة</div>
          <div class="kpiValue">${a.toLocaleString("en-US")}</div>
          <div class="kpiStats">
            <div class="kpiStat">
              <div class="kpiStatLabel">الإجمالي</div>
              <div class="kpiStatValue">${t.toLocaleString("en-US")}</div>
            </div>
            <div class="kpiStat">
              <div class="kpiStatLabel">نسبة القبول</div>
              <div class="kpiStatValue">${rate}%</div>
            </div>
          </div>
        </div>
      </aside>
    `;

    rows.appendChild(section);

    const canvas = document.getElementById(`cv-${card.slug}`);
    charts.push(buildSingleBarWithApprovedPart(canvas, labels, total, approved));
  });
}

async function loadData(){
  const muni = document.getElementById("selMuni").value;
  const dept = document.getElementById("selDept").value;
  const type = document.getElementById("selType").value;

  const qs = new URLSearchParams({muni, dept, type}).toString();
  const r = await fetch(`/data?${qs}`, {cache:"no-store"});
  let j = null;
  try { j = await r.json(); }
  catch(e){
    throw new Error("فشل قراءة JSON من /data (غالباً فيه خطأ 500 في السيرفر).");
  }
  if(!r.ok){
    throw new Error(j?.error || "خطأ غير معروف في /data");
  }
  renderCards(j);
}

document.getElementById("btnApply").addEventListener("click", async ()=> {
  try { await loadData(); }
  catch(e){
    const rows = document.getElementById("rows");
    rows.innerHTML = `<div class="card">خطأ: ${e.message}</div>`;
  }
});

(async function init(){
  try{
    await loadOptions();
    await loadData();
  }catch(e){
    console.error(e);
    const rows = document.getElementById("rows");
    rows.innerHTML = `<div class="card">خطأ: ${e.message}</div>`;
  }
})();
</script>

</body>
</html>
"""

# =========================
# Routes
# =========================
_df_cache = None

@app.route("/")
def index():
    return Response(HTML, mimetype="text/html; charset=utf-8")

@app.route("/options")
def options():
    try:
        global _df_cache
        if _df_cache is None:
            _df_cache = prepare_df()
        return jsonify(build_options(_df_cache))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/data")
def data():
    try:
        global _df_cache
        if _df_cache is None:
            _df_cache = prepare_df()

        muni = request.args.get("muni", "ALL")
        dept = request.args.get("dept", "ALL")
        type_ = request.args.get("type", "ALL")

        return jsonify(build_data(_df_cache, muni, dept, type_))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ✅ تشغيل مناسب للنشر (Render وغيره)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)