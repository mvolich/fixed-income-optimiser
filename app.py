# app.py  — Rubrics Fixed Income Optimiser (branded, guided)
# ----------------------------------------------------------
# Forward-looking, factor & scenario-aware optimisation with fund-specific caps and VaR controls.
# Open with: streamlit run app.py
#
# Inputs: Optimiser_Input_Final_v3.xlsx (sheet "Optimiser_Input")
# Dependencies:
#   pip install streamlit pandas numpy plotly cvxpy osqp openpyxl

import io, base64, math, json
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# Try to import cvxpy; if missing, provide a graceful message
try:
    import cvxpy as cp
    CVXPY_AVAILABLE = True
except Exception as e:
    CVXPY_AVAILABLE = False
    CVXPY_ERROR = str(e)

# -----------------------------
# 0) Global configuration & defaults
# -----------------------------

st.set_page_config(
    page_title="Rubrics Fixed Income Optimiser",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---- Brand palette (Rubrics) ----
RBX = {
    "blue":      "#001E4F",
    "med_blue":  "#2C5697",
    "light_blue":"#7BA4DB",
    "grey":      "#D8D7DF",
    "orange":    "#CF4520",
}
COLORWAY = [RBX["blue"], RBX["med_blue"], RBX["light_blue"], RBX["orange"], RBX["grey"]]

def inject_brand_css_try_fonts():
    """
    Load Ringside font if user placed WOFF2 files at .streamlit/fonts/.
    Falls back gracefully to Inter/Segoe UI/Helvetica/Arial if not present.
    """
    def _b64_if_exists(path):
        try:
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode()
        except Exception:
            return None

    reg = _b64_if_exists(".streamlit/fonts/Ringside-Regular.woff2")
    bold = _b64_if_exists(".streamlit/fonts/Ringside-Bold.woff2")

    css = ["<style>"]
    if reg:
        css.append(
            f"""@font-face {{
                    font-family: 'Ringside';
                    src: url(data:font/woff2;base64,{reg}) format('woff2');
                    font-weight: 400; font-style: normal; font-display: swap;
                }}"""
        )
    if bold:
        css.append(
            f"""@font-face {{
                    font-family: 'Ringside';
                    src: url(data:font/woff2;base64,{bold}) format('woff2');
                    font-weight: 700; font-style: normal; font-display: swap;
                }}"""
        )

    css.append(f"""
        :root {{
          --rbx-blue:{RBX["blue"]}; --rbx-med:{RBX["med_blue"]};
          --rbx-light:{RBX["light_blue"]}; --rbx-grey:{RBX["grey"]};
          --rbx-orange:{RBX["orange"]};
        }}
        html, body, [class*="css"]  {{
          font-family: {'Ringside, ' if reg else ''}Inter, "Segoe UI", Helvetica, Arial, sans-serif !important;
          color: var(--rbx-blue);
        }}
        h1,h2,h3,h4,h5,h6 {{ color: var(--rbx-blue); }}
        .stMetricValue, .stPlotlyChart {{ font-family: {'Ringside, ' if reg else ''}Inter, "Segoe UI", Helvetica, Arial, sans-serif !important; }}
    """)
    css.append("</style>")
    st.markdown("\n".join(css), unsafe_allow_html=True)

def brandify(fig: go.Figure) -> go.Figure:
    fig.update_layout(
        template="none",
        colorway=COLORWAY,
        font=dict(family="Ringside, Inter, Segoe UI, Helvetica, Arial, sans-serif", color=RBX["blue"]),
        title=dict(font=dict(color=RBX["blue"], size=16)),
        margin=dict(l=10, r=10, t=40, b=50),
    )
    return fig

inject_brand_css_try_fonts()

# ---- Defaults & constraints ----
DEFAULT_INPUT_FILE = "Optimiser_Input_Final_v3.xlsx"
INPUT_SHEET = "Optimiser_Input"

FUND_CONSTRAINTS = {
    "GFI": {"max_non_ig": 0.25, "max_em": 0.30, "max_hybrid": 0.15, "max_cash": 0.20, "max_at1": 0.15},
    "GCF": {"max_non_ig": 0.10, "max_em": 0.35,                       "max_cash": 0.20, "max_at1": 0.10},
    "EYF": {"max_non_ig": 1.00, "max_em": 1.00,                       "max_cash": 0.20, "max_at1": 0.00},
}
# Monthly VaR (99%) caps (proportions)
VAR99_CAP = {"GFI": 0.050, "GCF": 0.055, "EYF": 0.100}

FACTOR_BUDGETS_DEFAULT = {
    "limit_krd10y": 0.75,   # |10y KRD| budget (yrs)
    "limit_twist":  0.40,   # |30y - 2y| KRD (yrs)
    "limit_sdv01_ig": 3.0,  # IG spread duration (yrs)
    "limit_sdv01_hy": 1.5,  # HY spread duration (yrs)
}
TURNOVER_DEFAULTS = {"penalty_bps_per_100": 15.0, "max_turnover": 0.25}

# Scenario calibration: rate moves & spread widenings (bp @99%)
RATES_BP99  = {"2y": 60.0, "5y": 50.0, "10y": 45.0, "30y": 40.0}
SPREAD_BP99 = {"IG": 100.0, "HY": 200.0, "AT1": 350.0, "EM": 250.0}

def spacer(h=1):
    for _ in range(h): st.write("")

# -----------------------------
# Column normalization & validation
# -----------------------------
REQUIRED_COLS = [
    "Segment_ID","Name","Instrument_Type","Credit_Quality","Include",
    "Yield_Hedged_Pct","Roll_Down_bps_1Y","OAD_Years","OASD_Years",
    "KRD_2y","KRD_5y","KRD_10y","KRD_30y",
]
COLUMN_SYNONYMS = {
    "oasd_years": ["asd_years"],
    "krd_2y": ["krd_2","krd2y"],
    "krd_5y": ["krd_5","krd5y"],
    "krd_10y": ["krd_10","krd10y"],
    "krd_30y": ["krd_30","krd30y"],
    "yield_hedged_pct": ["yield_hedged","yield_hedged_%","yield_hedged_percent"],
    "roll_down_bps_1y": ["roll_down_bps","rolldown_bps_1y"],
    "instrument_type": ["type"],
    "credit_quality": ["rating","quality"],
}

def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    lower_to_actual = {c.lower().strip(): c for c in df.columns}
    rename_map = {}
    for target in REQUIRED_COLS:
        t = target.lower()
        if t in lower_to_actual:
            src = lower_to_actual[t]
            if src != target: rename_map[src] = target
            continue
        for alias in COLUMN_SYNONYMS.get(t, []):
            if alias in lower_to_actual:
                rename_map[lower_to_actual[alias]] = target
                break
    if rename_map: df = df.rename(columns=rename_map)
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")
    return df

# -----------------------------
# 1) Data ingest & classification
# -----------------------------
@st.cache_data(show_spinner=False)
def load_input_table(uploaded_file_bytes: bytes | None, path: str = DEFAULT_INPUT_FILE) -> pd.DataFrame:
    if uploaded_file_bytes is not None:
        bio = io.BytesIO(uploaded_file_bytes)
        df = pd.read_excel(bio, sheet_name=INPUT_SHEET, engine="openpyxl")
    else:
        df = pd.read_excel(path, sheet_name=INPUT_SHEET, engine="openpyxl")
    df = _normalize_columns(df)
    # numerics
    for c in ["Yield_Hedged_Pct","Roll_Down_bps_1Y","OAD_Years","OASD_Years","Convexity","KRD_2y","KRD_5y","KRD_10y","KRD_30y"]:
        if c in df.columns: df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    # Expected return (pp): carry% + roll-down(bp)/100
    df["ExpRet_pp"] = df.get("Yield_Hedged_Pct", 0.0) + df.get("Roll_Down_bps_1Y", 0.0) / 100.0
    if "Include" in df.columns:
        df = df[df["Include"] == True].copy()
    df.reset_index(drop=True, inplace=True)
    return df

def tag_segments(df: pd.DataFrame) -> dict:
    seg  = df["Segment_ID"].fillna("").astype(str).str.upper()
    typ  = df["Instrument_Type"].fillna("").astype(str).str.upper()
    qual = df["Credit_Quality"].fillna("").astype(str).str.upper()

    is_tbill  = seg.str.contains("TBILL")
    is_at1    = seg.str.contains("AT1")
    is_t2     = seg.str.contains("T2")
    is_hybrid = seg.str.contains("GLOBAL_HYBRID")  # Hybrid = Global Hybrid only
    is_em     = (typ == "EM")                      # EM = hard-currency sleeve only
    is_hy_rt  = qual.isin(["BB","B","CCC","HY"])

    is_non_ig = is_hy_rt | is_at1 | is_t2 | is_em
    is_ig = qual.isin(["IG","GOV","AA","A","BBB","AAA","AA+","AA-","A+","A-","BBB+","BBB","BBB-"])

    return {
        "is_tbill": is_tbill.values.astype(bool),
        "is_at1":   is_at1.values.astype(bool),
        "is_t2":    is_t2.values.astype(bool),
        "is_hybrid":is_hybrid.values.astype(bool),
        "is_em":    is_em.values.astype(bool),
        "is_non_ig":is_non_ig.values.astype(bool),
        "is_ig":    is_ig.values.astype(bool),
        "is_hy_rating": is_hy_rt.values.astype(bool),
    }

# -----------------------------
# 2) Scenario engine & risk
# -----------------------------
def bp99_to_sigma(bp99: float) -> float:
    # 99th percentile of N(0,σ) ≈ 2.33σ
    return (bp99 / 10_000.0) / 2.33

@st.cache_data(show_spinner=False)
def simulate_mc_draws(n_draws: int, seed: int, rates_bp99: dict, spreads_bp99: dict) -> dict:
    rng = np.random.default_rng(seed)
    sig_r = {k: bp99_to_sigma(v) for k, v in rates_bp99.items()}
    sig_s = {k: bp99_to_sigma(v) for k, v in spreads_bp99.items()}

    d2  = rng.normal(0, sig_r["2y"],  size=n_draws)
    d5  = rng.normal(0, sig_r["5y"],  size=n_draws)
    d10 = rng.normal(0, sig_r["10y"], size=n_draws)
    d30 = rng.normal(0, sig_r["30y"], size=n_draws)

    dig  = rng.normal(0, sig_s["IG"],  size=n_draws)
    dhy  = rng.normal(0, sig_s["HY"],  size=n_draws)
    dat1 = rng.normal(0, sig_s["AT1"], size=n_draws)
    dem  = rng.normal(0, sig_s["EM"],  size=n_draws)

    return {"d2": d2, "d5": d5, "d10": d10, "d30": d30, "dig": dig, "dhy": dhy, "dat1": dat1, "dem": dem}

@st.cache_data(show_spinner=False)
def build_asset_pnl_matrix(df: pd.DataFrame, tags: dict, mc: dict) -> np.ndarray:
    """S × N matrix of asset % P&L per Monte Carlo draw.
       Approx: Price% ≈ -(KRDs·dYield) - (OASD * dSpread)"""
    krd  = df[["KRD_2y","KRD_5y","KRD_10y","KRD_30y"]].values
    sdur = df["OASD_Years"].values

    N = len(df)
    S = len(mc["d2"])
    is_at1 = tags["is_at1"]; is_em = tags["is_em"]
    is_hy  = tags["is_hy_rating"] | is_at1 | is_em

    spread = np.zeros((S, N))
    spread += mc["dig"].reshape(-1,1)
    spread[:, is_hy]  = mc["dhy"].reshape(-1,1)
    spread[:, is_em]  = mc["dem"].reshape(-1,1)
    spread[:, is_at1] = mc["dat1"].reshape(-1,1)

    dy = np.vstack([mc["d2"], mc["d5"], mc["d10"], mc["d30"]]).T
    rate_pnl   = -(dy @ krd.T)
    credit_pnl = -(spread * sdur.reshape(1, -1))
    return rate_pnl + credit_pnl  # in %

def var_cvar_from_pnl(port_pnl: np.ndarray, alpha: float = 0.99) -> tuple[float, float]:
    losses = -port_pnl
    if losses.size == 0:
        return 0.0, 0.0
    var  = np.quantile(losses, alpha)
    tail = losses[losses >= var]
    cvar = tail.mean() if tail.size else var
    return float(var), float(cvar)

# -----------------------------
# 3) Optimiser
# -----------------------------
def solve_portfolio(df, tags, mu_dec, pnl_matrix, fund: str, params: dict, prev_w=None):
    """Return (weights, metrics)."""
    if not CVXPY_AVAILABLE:
        st.warning("cvxpy is not installed in this environment. Showing equal-weight placeholder.")
        n = len(df); w = np.ones(n)/n
        return w, {"status":"NO_CVXPY","message":CVXPY_ERROR}

    n = len(df)
    w = cp.Variable(n, nonneg=True)
    cons = [cp.sum(w) == 1]

    is_non_ig = tags["is_non_ig"].astype(float)
    is_em     = tags["is_em"].astype(float)
    is_hybrid = tags["is_hybrid"].astype(float)
    is_at1    = tags["is_at1"].astype(float)
    is_tbill  = tags["is_tbill"].astype(float)

    fc = FUND_CONSTRAINTS[fund].copy()
    if "max_non_ig" in fc: cons += [is_non_ig @ w <= fc["max_non_ig"]]
    if "max_em"     in fc: cons += [is_em     @ w <= fc["max_em"]]
    if "max_hybrid" in fc: cons += [is_hybrid @ w <= fc["max_hybrid"]]
    if "max_cash"   in fc: cons += [is_tbill  @ w <= fc["max_cash"]]
    if "max_at1"    in fc: cons += [is_at1    @ w <= fc["max_at1"]]

    fb = params.get("factor_budgets", FACTOR_BUDGETS_DEFAULT)
    X = df[["KRD_2y","KRD_5y","KRD_10y","KRD_30y","OASD_Years"]].values
    cons += [cp.abs(X[:,2] @ w)          <= fb.get("limit_krd10y", 0.75)]      # |KRD10y|
    cons += [cp.abs((X[:,3]-X[:,0]) @ w) <= fb.get("limit_twist", 0.40)]       # |30y-2y|
    is_ig = tags["is_ig"].astype(float)
    is_hy = (~tags["is_ig"]).astype(float)
    cons += [cp.abs((X[:,4]*is_ig) @ w)  <= fb.get("limit_sdv01_ig", 3.0)]     # sDV01 IG
    cons += [cp.abs((X[:,4]*is_hy) @ w)  <= fb.get("limit_sdv01_hy", 1.5)]     # sDV01 HY

    if prev_w is None: prev_w = np.zeros(n)
    max_turn = params.get("max_turnover", TURNOVER_DEFAULTS["max_turnover"])
    cons += [cp.norm1(w - prev_w) <= max_turn]
    turn_pen = params.get("turnover_penalty", TURNOVER_DEFAULTS["penalty_bps_per_100"])/10_000.0

    # CVaR_99 linearisation
    S = pnl_matrix.shape[0]
    z = cp.Variable(S)
    alpha = cp.Variable()
    losses = -pnl_matrix @ w
    cons += [z >= 0, z >= losses - alpha]
    cvar = alpha + (1/(0.01*S)) * cp.sum(z)
    cons += [cvar <= params.get("cvar_cap", VAR99_CAP[fund]*1.15)]

    objective = params.get("objective","Max Return")
    ridge = 1e-6
    if objective == "Max Return":
        obj = cp.Maximize(mu_dec @ w - turn_pen*cp.norm1(w - prev_w) - ridge*cp.sum_squares(w))
    elif objective == "Max Sharpe":
        obj = cp.Maximize(mu_dec @ w - 10*cvar - turn_pen*cp.norm1(w - prev_w) - ridge*cp.sum_squares(w))
    elif objective == "Min VaR for Target Return":
        target = params.get("target_return", float(np.percentile(mu_dec, 60)))
        cons += [mu_dec @ w >= target]
        obj = cp.Minimize(cvar + turn_pen*cp.norm1(w - prev_w) + ridge*cp.sum_squares(w))
    else:
        target = params.get("target_return", float(np.percentile(mu_dec, 50)))
        cons += [mu_dec @ w >= target]
        obj = cp.Minimize(cvar + turn_pen*cp.norm1(w - prev_w) + ridge*cp.sum_squares(w))

    prob = cp.Problem(obj, cons)
    errors = []
    for solver, kwargs in [
        (cp.OSQP, {"verbose": False, "max_iter": 100000}),
        (cp.SCS,  {"verbose": False, "max_iters": 25000}),
        (cp.ECOS, {"verbose": False, "max_iters": 100000}),
    ]:
        try:
            prob.solve(solver=solver, **kwargs); break
        except Exception as e:
            errors.append(f"{getattr(solver,'__name__',str(solver))}: {e}")

    if w.value is None:
        return None, {"status":"INFEASIBLE","message":" | ".join(errors) or "No feasible solution."}

    w_opt = np.array(w.value).ravel()
    port_pnl = pnl_matrix @ w_opt
    var99, cvar99 = var_cvar_from_pnl(port_pnl, 0.99)
    er_pp  = float((mu_dec @ w_opt) * 100.0)   # show as pp
    yld_pp = float(df["Yield_Hedged_Pct"].values @ w_opt)

    metrics = {
        "status":"OPTIMAL","obj":prob.value,
        "ExpRet_pp": er_pp, "Yield_pp": yld_pp,
        "VaR99_1M": var99, "CVaR99_1M": cvar99, "weights": w_opt
    }
    return w_opt, metrics

# -----------------------------
# 4) Visual helpers (formatted)
# -----------------------------
def gauge(title: str, value: float, suffix: str = "%"):
    fig = go.Figure(go.Indicator(
        mode="number",
        value=value * (100 if suffix=="%" else 1),
        number={"suffix": suffix, "valueformat": ".2f"},
        title={"text": title},
    ))
    fig.update_layout(height=110, margin=dict(l=5,r=5,t=30,b=5), font=dict(color=RBX["blue"]))
    return brandify(fig)

def bar_allocation(df, weights, title):
    ser = pd.Series(weights, index=df["Name"]).sort_values(ascending=False)
    fig = go.Figure(go.Bar(x=ser.index, y=ser.values, marker_color=RBX["blue"]))
    fig.update_layout(title=title, xaxis_title="Segment", yaxis_title="Weight (%)")
    return brandify(fig)

def exposures_vs_budgets(df, weights, budgets: dict, title: str):
    is_ig_mask = tag_segments(df)["is_ig"]
    oasd = df["OASD_Years"].values
    vals = {
        "KRD 2y":  float(df["KRD_2y"].values @ weights),
        "KRD 5y":  float(df["KRD_5y"].values @ weights),
        "KRD 10y": float(df["KRD_10y"].values @ weights),
        "KRD 30y": float(df["KRD_30y"].values @ weights),
        "sDV01 IG": float(np.sum(oasd * weights * is_ig_mask)),
        "sDV01 HY": float(np.sum(oasd * weights * (~is_ig_mask))),
    }
    x = list(vals.keys()); y = list(vals.values())
    fig = go.Figure(go.Bar(x=x, y=y))
    fig.add_hline(y=budgets.get("limit_krd10y",0.75), line_dash="dot", line_color=RBX["grey"],
                  annotation_text="KRD10y cap", annotation_position="top left")
    fig.add_hline(y=budgets.get("limit_sdv01_ig",3.0), line_dash="dot", line_color=RBX["grey"],
                  annotation_text="sDV01 IG cap", annotation_position="bottom left")
    fig.add_hline(y=budgets.get("limit_sdv01_hy",1.5), line_dash="dot", line_color=RBX["grey"],
                  annotation_text="sDV01 HY cap", annotation_position="bottom left")
    fig.update_layout(title=title, yaxis_title="Years")
    return brandify(fig)

def scenario_histogram(port_pnl, title="Scenario P&L (1M)"):
    fig = go.Figure(data=[go.Histogram(x=port_pnl*100, nbinsx=40, marker_color=RBX["med_blue"])])
    var99, cvar99 = var_cvar_from_pnl(port_pnl, 0.99)
    fig.add_vline(x=-var99*100, line_dash="dash", line_color=RBX["orange"],
                  annotation_text="VaR 99%", annotation_position="top left")
    fig.add_vline(x=-cvar99*100, line_dash="dot", line_color=RBX["orange"],
                  annotation_text="CVaR 99%", annotation_position="top left")
    fig.update_layout(title=title, xaxis_title="% P&L", yaxis_title="Count")
    return brandify(fig)

def format_weights_df(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy where numeric weights are formatted as % strings."""
    out = df.copy()
    for c in out.columns:
        if out[c].dtype.kind in "fc":
            out[c] = (out[c]*100).map(lambda x: f"{x:,.2f}%")
    return out

def contributions_table(df, weights, mu_pp):
    d = pd.DataFrame({
        "Segment": df["Name"],
        "Weight (%)": weights*100.0,
        "Expected return contribution (pp)": weights * mu_pp,
        "Carry (pp)": df["Yield_Hedged_Pct"].values,
        "Roll‑down (pp)": df["Roll_Down_bps_1Y"].values/100.0,
        "Rates duration (OAD, yrs)": df["OAD_Years"].values,
        "Spread duration (sDV01, yrs)": df["OASD_Years"].values,
    }).sort_values("Weight (%)", ascending=False)
    # Format percents nicely for display
    d["Weight (%)"] = d["Weight (%)"].map(lambda x: f"{x:,.2f}%")
    for col in ["Expected return contribution (pp)","Carry (pp)","Roll‑down (pp)"]:
        d[col] = d[col].map(lambda x: f"{x:,.2f}")
    return d

def heatmap_funds_losses(fund_results: dict):
    # Build portfolio P&L percentiles across funds
    Z, funds = [], []
    for f,(res, pnl) in fund_results.items():
        funds.append(f); Z.append(pnl @ res["weights"])
    Z = np.vstack(Z)
    percs = [1,5,10,25,50,75,90,95,99]
    data = [[np.percentile(row, p) for p in percs] for row in Z]
    fig = go.Figure(go.Heatmap(
        z=data, x=[f"P{p}" for p in percs], y=funds, colorscale="Blues"
    ))
    fig.update_layout(title="Scenario Distribution by Fund (Portfolio P&L percentiles)")
    return brandify(fig)

# -----------------------------
# 5) App UI
# -----------------------------
st.title("Rubrics Fixed Income Optimiser")
st.caption("Forward-looking allocation with carry/roll expected returns, KRD & sDV01 factor risk, and fund‑specific prospectus constraints.")

with st.expander(
    "Data source (Excel) • required columns: Segment_ID, Name, Yield_Hedged_Pct, Roll_Down_bps_1Y, OAD_Years, OASD_Years, KRD_2y/5y/10y/30y, Credit_Quality, Instrument_Type, Include",
    expanded=False
):
    upload = st.file_uploader(
        "Upload Optimiser_Input_Final_v3.xlsx (sheet Optimiser_Input)",
        type=["xlsx"],
        accept_multiple_files=False,
        help="If you don’t upload, the app loads the default sample file in the project folder."
    )
    st.write("If no file is uploaded, the app will try to read: ", f"`{DEFAULT_INPUT_FILE}`")

# Load data
try:
    df = load_input_table(upload.getvalue() if upload is not None else None, DEFAULT_INPUT_FILE)
except Exception as e:
    st.error(f"Failed to load input: {e}")
    st.stop()
if len(df) == 0:
    st.error("No rows found after applying Include==True. Please check the input file.")
    st.stop()

tags = tag_segments(df)

# -------- Sidebar controls with tooltips (education) --------
with st.sidebar:
    st.header("Global settings")
    seed = st.number_input("Random seed", min_value=0, value=42, step=1,
                           help="Controls repeatability of Monte Carlo draws.")
    n_draws = st.number_input("Monte Carlo draws (monthly)", min_value=200, max_value=10000, value=2000, step=100,
                              help="Higher = smoother risk estimates but slower.")

    st.subheader("Rate shocks (bp @99%)")
    c1,c2,c3,c4 = st.columns(4)
    with c1: RATES_BP99["2y"]  = st.number_input("2y",  value=float(RATES_BP99["2y"]),  help="One‑month 2‑year yield shock used to compute KRD impact.")
    with c2: RATES_BP99["5y"]  = st.number_input("5y",  value=float(RATES_BP99["5y"]),  help="One‑month 5‑year yield shock.")
    with c3: RATES_BP99["10y"] = st.number_input("10y", value=float(RATES_BP99["10y"]), help="One‑month 10‑year yield shock.")
    with c4: RATES_BP99["30y"] = st.number_input("30y", value=float(RATES_BP99["30y"]), help="One‑month 30‑year yield shock.")

    st.subheader("Spread widenings (bp @99%)")
    c1,c2,c3,c4 = st.columns(4)
    with c1: SPREAD_BP99["IG"]  = st.number_input("IG",  value=float(SPREAD_BP99["IG"]),  help="IG OAS widening used with sDV01.")
    with c2: SPREAD_BP99["HY"]  = st.number_input("HY",  value=float(SPREAD_BP99["HY"]),  help="HY OAS widening used with sDV01.")
    with c3: SPREAD_BP99["AT1"] = st.number_input("AT1", value=float(SPREAD_BP99["AT1"]), help="Bank AT1 widening.")
    with c4: SPREAD_BP99["EM"]  = st.number_input("EM",  value=float(SPREAD_BP99["EM"]),  help="EM hard‑currency widening.")

    st.divider()
    st.subheader("Default factor budgets (yrs)")
    limit_krd10y = st.number_input("|10‑year KRD| budget", value=FACTOR_BUDGETS_DEFAULT["limit_krd10y"], step=0.05, format="%.2f",
                                   help="Caps absolute 10‑year curve exposure. Increasing allows more duration around 10‑year.")
    limit_twist  = st.number_input("Curve twist budget (30y–2y, yrs)", value=FACTOR_BUDGETS_DEFAULT["limit_twist"], step=0.05, format="%.2f",
                                   help="Caps steepener/flattener exposure; higher = more curve shape risk.")
    limit_sdv01_ig = st.number_input("Credit spread duration budget – IG (yrs)", value=FACTOR_BUDGETS_DEFAULT["limit_sdv01_ig"], step=0.1, format="%.1f",
                                     help="Caps IG spread risk; higher allows more IG credit beta.")
    limit_sdv01_hy = st.number_input("Credit spread duration budget – HY (yrs)", value=FACTOR_BUDGETS_DEFAULT["limit_sdv01_hy"], step=0.1, format="%.1f",
                                     help="Caps HY/Non‑IG spread risk; higher allows more HY/AT1/EM exposure.")

    st.divider()
    st.subheader("Turnover")
    penalty_bps = st.number_input("Penalty (bps per 100% turnover)", value=TURNOVER_DEFAULTS["penalty_bps_per_100"], step=1.0,
                                  help="Softly discourages trading; higher penalty reduces changes in weights.")
    max_turn = st.slider("Max turnover per rebalance", 0.0, 1.0, TURNOVER_DEFAULTS["max_turnover"], 0.01,
                         help="Hard cap on ∑|w_new − w_old|. 0.25 = at most 25% of the portfolio can change.")

    st.subheader("Previous weights (optional)")
    prev_file = st.file_uploader("CSV with columns [Segment or Name, Weight]", type=["csv"], key="prev_weights",
                                 help="If provided, turnover is measured vs these weights.")
    prev_w_vec = None
    if prev_file is not None:
        _pw = pd.read_csv(prev_file)
        name_col = "Segment" if "Segment" in _pw.columns else ("Name" if "Name" in _pw.columns else None)
        if name_col is not None and "Weight" in _pw.columns:
            _pw[name_col] = _pw[name_col].astype(str).str.strip()
            prev_w_vec = df["Name"].astype(str).str.strip().map(_pw.set_index(name_col)["Weight"]).fillna(0.0).values
            s = prev_w_vec.sum()
            if s > 0: prev_w_vec = prev_w_vec / s
        else:
            st.warning("Prev weights CSV must have columns [Segment or Name, Weight].")

# Scenarios & expected returns
mc  = simulate_mc_draws(int(n_draws), int(seed), dict(RATES_BP99), dict(SPREAD_BP99))
pnl_matrix_assets = build_asset_pnl_matrix(df, tags, mc)
mu_pp  = df["ExpRet_pp"].values.astype(float)       # pp for display
mu_dec = mu_pp / 100.0                              # decimals for optimisation

def run_fund(fund: str, objective: str, var_cap_override: float | None = None, prev_w=None):
    params = {
        "factor_budgets": {"limit_krd10y":limit_krd10y, "limit_twist":limit_twist,
                           "limit_sdv01_ig":limit_sdv01_ig, "limit_sdv01_hy":limit_sdv01_hy},
        "turnover_penalty": penalty_bps,
        "max_turnover": max_turn,
        "objective": objective,
        "cvar_cap": (var_cap_override if var_cap_override is not None else VAR99_CAP[fund]*1.15),
    }
    w, metrics = solve_portfolio(df, tags, mu_dec, pnl_matrix_assets, fund, params, prev_w)
    if w is None: return None, metrics, None
    return w, metrics, pnl_matrix_assets @ w

# ---- Tabs
tab_overview, tab_fund, tab_learn = st.tabs(["Overview (Compare Funds)", "Fund detail (Tune one)", "Learn (Glossary & What‑ifs)"])

# -----------------------------
# Overview
# -----------------------------
with tab_overview:
    st.subheader("Cross‑fund positioning and risk")
    objective = st.selectbox(
        "Objective",
        ["Max Return","Max Sharpe","Min VaR for Target Return","Max Drawdown Proxy"], index=0,
        help="Choose how the optimiser trades off return vs risk."
    )

    fund_outputs = {}
    for f in ["GFI","GCF","EYF"]:
        w, m, port_pnl = run_fund(f, objective, prev_w=prev_w_vec)
        if w is not None:
            m["weights"] = w; fund_outputs[f] = (m, pnl_matrix_assets)
        else:
            st.warning(f"{f}: {m.get('status','')} — {m.get('message','')}")

    cols = st.columns(4)
    idx = 0; agg_weights = None
    for f in ["GFI","GCF","EYF"]:
        if f in fund_outputs:
            m,_ = fund_outputs[f]
            with cols[idx]:
                st.plotly_chart(gauge(f"{f} – Expected return", m["ExpRet_pp"], suffix="pp"), use_container_width=True)
                st.plotly_chart(gauge(f"{f} – VaR 99% (1M)", m["VaR99_1M"]), use_container_width=True)
                cap = VAR99_CAP[f]; ok = "✅ within cap" if m["VaR99_1M"] <= cap else "❌ over cap"
                st.caption(f"VaR cap {cap*100:.2f}% — {ok}")
            idx += 1

    if len(fund_outputs) > 0:
        W = [fund_outputs[f][0]["weights"] for f in fund_outputs]
        agg_weights = np.mean(np.vstack(W), axis=0)
        port_pnl_agg = pnl_matrix_assets @ agg_weights
        var_agg, cvar_agg = var_cvar_from_pnl(port_pnl_agg, 0.99)
        er_agg = float(mu_dec @ agg_weights)
        with cols[3]:
            st.plotly_chart(gauge("Aggregate – Expected return", er_agg, suffix="pp"), use_container_width=True)
            st.plotly_chart(gauge("Aggregate – VaR 99% (1M)", var_agg), use_container_width=True)

    spacer(1)
    st.markdown("**Allocation by segment (GFI / GCF / EYF / Aggregate)**")
    alloc_df = pd.DataFrame(index=df["Name"])
    for f in ["GFI","GCF","EYF"]:
        if f in fund_outputs: alloc_df[f] = fund_outputs[f][0]["weights"]
    if agg_weights is not None: alloc_df["Aggregate"] = agg_weights
    alloc_df = alloc_df.fillna(0.0)
    st.dataframe(format_weights_df(alloc_df), use_container_width=True, height=260)

    fig_alloc = go.Figure()
    fund_cols = {"GFI":RBX["blue"], "GCF":RBX["med_blue"], "EYF":RBX["light_blue"], "Aggregate":RBX["orange"]}
    for col in alloc_df.columns:
        fig_alloc.add_bar(name=col, x=alloc_df.index, y=alloc_df[col].values, marker_color=fund_cols.get(col,RBX["grey"]))
    fig_alloc.update_layout(barmode="group", xaxis_title="Segment", yaxis_title="Weight (%)")
    st.plotly_chart(brandify(fig_alloc), use_container_width=True)

    spacer(1)
    st.markdown("**Factor exposures vs budgets** (KRD & sDV01)")
    fig_fb = go.Figure()
    for f in ["GFI","GCF","EYF"]:
        if f in fund_outputs:
            w = fund_outputs[f][0]["weights"]
            vals = {
                "KRD10y": float(df["KRD_10y"].values @ w),
                "Twist(30-2)": float((df["KRD_30y"].values - df["KRD_2y"].values) @ w),
                "sDV01": float(df["OASD_Years"].values @ w),
            }
            fig_fb.add_bar(name=f"{f} KRD10y",     x=["KRD10y"],     y=[vals["KRD10y"]], marker_color=fund_cols[f])
            fig_fb.add_bar(name=f"{f} Twist",      x=["Twist(30-2)"],y=[vals["Twist(30-2)"]], marker_color=fund_cols[f])
            fig_fb.add_bar(name=f"{f} sDV01",      x=["sDV01"],      y=[vals["sDV01"]], marker_color=fund_cols[f])
    fig_fb.update_layout(barmode="group", yaxis_title="Years")
    st.plotly_chart(brandify(fig_fb), use_container_width=True)

    spacer(1)
    if len(fund_outputs) > 0:
        st.plotly_chart(heatmap_funds_losses(fund_outputs), use_container_width=True)

    spacer(1)
    st.markdown("**Download allocations**")
    if len(fund_outputs) > 0:
        out = pd.DataFrame({"Segment": df["Name"]})
        for f in ["GFI","GCF","EYF"]:
            if f in fund_outputs: out[f] = fund_outputs[f][0]["weights"]
        st.download_button("Download CSV (fund allocations)", out.to_csv(index=False).encode("utf-8"),
                           file_name="allocations.csv", mime="text/csv")

# -----------------------------
# Fund detail
# -----------------------------
with tab_fund:
    c0, c1 = st.columns([1,2])
    with c0:
        fund = st.selectbox("Fund", ["GFI","GCF","EYF"], index=0)
        objective = st.selectbox("Objective", ["Max Return","Max Sharpe","Min VaR for Target Return","Max Drawdown Proxy"], index=0)
        var_cap = st.slider(f"{fund} monthly VaR 99% cap (%)", 0.0, 15.0, float(VAR99_CAP[fund]*100), 0.1,
                            help="Tightening lowers risk and typically lowers expected return.")/100.0

        st.write("**Prospectus caps (hard limits on allocations)**")
        fc = FUND_CONSTRAINTS[fund]
        max_non_ig = st.slider("Max Non‑IG weight", 0.0, 1.0, float(fc.get("max_non_ig",1.0)), 0.01,
                               help="Upper bound on HY/AT1/EM exposures combined.")
        max_em     = st.slider("Max EM hard‑currency weight", 0.0, 1.0, float(fc.get("max_em",1.0)), 0.01,
                               help="Upper bound on EM hard‑currency sleeve.")
        max_hybrid = st.slider("Max Global Hybrid weight", 0.0, 1.0, float(fc.get("max_hybrid",0.0)), 0.01,
                               help="Upper bound on Global Hybrid sleeve.") if "max_hybrid" in fc else 0.0
        max_cash   = st.slider("Max Cash/T‑Bills weight", 0.0, 1.0, float(fc.get("max_cash",1.0)), 0.01,
                               help="Limits liquidity sleeve; higher = more room for de‑risking.")
        max_at1    = st.slider("Max AT1 weight", 0.0, 1.0, float(fc.get("max_at1",1.0)), 0.01,
                               help="Upper bound on AT1 (bank capital) exposure.")

        # Temporary override for this run only
        _backup = FUND_CONSTRAINTS[fund].copy()
        FUND_CONSTRAINTS[fund] = {"max_non_ig":max_non_ig,"max_em":max_em,"max_cash":max_cash,"max_at1":max_at1}
        if "max_hybrid" in _backup: FUND_CONSTRAINTS[fund]["max_hybrid"] = max_hybrid

        st.write("**Factor budgets (yrs)**")
        lk  = st.number_input("|10‑year KRD| budget", value=limit_krd10y, step=0.05, format="%.2f",
                              help="Higher allows more 10‑year rate sensitivity.")
        lt  = st.number_input("Curve twist budget (30y–2y, yrs)", value=limit_twist, step=0.05, format="%.2f",
                              help="Higher allows larger steepener/flattener bets.")
        lig = st.number_input("Credit spread duration budget – IG (yrs)", value=limit_sdv01_ig, step=0.1, format="%.1f",
                              help="Higher allows more IG credit beta.")
        lhy = st.number_input("Credit spread duration budget – HY (yrs)", value=limit_sdv01_hy, step=0.1, format="%.1f",
                              help="Higher allows more HY/AT1/EM credit beta.")
        fb_over = {"limit_krd10y": lk, "limit_twist": lt, "limit_sdv01_ig": lig, "limit_sdv01_hy": lhy}

    with c1:
        params = {"factor_budgets":fb_over, "turnover_penalty":penalty_bps,
                  "max_turnover":max_turn, "objective":objective, "cvar_cap":var_cap*1.15}
        w, m = solve_portfolio(df, tags, mu_dec, pnl_matrix_assets, fund, params, prev_w=prev_w_vec)
        FUND_CONSTRAINTS[fund] = _backup
        if w is None:
            st.error(f"Optimisation failed: {m.get('status','')} – {m.get('message','')}")
            st.stop()

        port_pnl = pnl_matrix_assets @ w
        var99, cvar99 = var_cvar_from_pnl(port_pnl, 0.99)
        cols = st.columns(4)
        with cols[0]: st.plotly_chart(gauge("Expected return (ann.)", m["ExpRet_pp"], suffix="pp"), use_container_width=True)
        with cols[1]: st.plotly_chart(gauge("VaR 99% (1M)", var99), use_container_width=True)
        with cols[2]: st.plotly_chart(gauge("CVaR 99% (1M)", cvar99), use_container_width=True)
        with cols[3]: st.plotly_chart(gauge("Portfolio carry", m["Yield_pp"], suffix="pp"), use_container_width=True)

        status = "✅ within cap" if var99 <= var_cap else "❌ over cap"
        st.caption(f"VaR 99%: {var99*100:.2f}% (cap {var_cap*100:.2f}%) {status}")

        st.plotly_chart(bar_allocation(df, w, f"{fund} – Allocation by segment (weights % )"), use_container_width=True)
        st.plotly_chart(exposures_vs_budgets(df, w, fb_over, f"{fund} – Factor exposures vs budgets"), use_container_width=True)
        st.plotly_chart(scenario_histogram(port_pnl, f"{fund} – Scenario P&L distribution"), use_container_width=True)

        contr = contributions_table(df, w, mu_pp)
        st.dataframe(contr, use_container_width=True, height=360)

        out_csv = contr[["Segment","Weight (%)","Expected return contribution (pp)"]].to_csv(index=False).encode("utf-8")
        st.download_button("Download weights & ER contributions (CSV)", out_csv,
                           file_name=f"{fund}_allocation.csv", mime="text/csv")

# -----------------------------
# Learn
# -----------------------------
with tab_learn:
    st.markdown("### Glossary and what‑ifs")
    st.markdown("""
- **Expected return (pp)** – annualised *carry + roll‑down*. Moving caps up generally lets the optimiser seek higher carry/roll trades.
- **VaR 99% (1M)** – worst expected 1‑month loss **not exceeded 99% of the time**. Tightening the VaR cap reduces risk and usually lowers return.
- **CVaR 99%** – the *average* loss **inside** that 1% tail. The optimiser constrains CVaR to remain aligned with your VaR cap.
- **KRDs (Key Rate Durations)** – interest‑rate sensitivities at different points on the curve.
  - **|10‑year KRD| budget** caps overall exposure around the 10‑year point.
  - **Curve twist (30y–2y)** caps steepener/flattener risk.
- **sDV01 (spread duration)** – credit spread sensitivity (yrs). We cap IG and HY/Non‑IG separately.
- **Turnover caps/penalties** – limit trading from previous weights and apply a cost to excessive churn.
- **Prospectus caps** – hard allocation limits per sleeve (e.g., Non‑IG, EM, AT1, Cash). The optimiser will never violate these.
""")
    st.info("Tip: Hover the small ⓘ icons in the sidebar and fund tab for in‑place explanations of each control.")
