
# Rubrics Fixed Income Optimiser (Streamlit)
# ------------------------------------------
# Forward-looking, factor & scenario-aware optimisation with fund-specific caps and VaR controls.
# Open with: streamlit run app.py
#
# Inputs: Optimiser_Input_Final_v3.xlsx (sheet "Optimiser_Input")
# Author: GPT-5 Pro (assistant)
#
# Dependencies:
#   pip install streamlit pandas numpy plotly cvxpy osqp openpyxl

import io
import json
import math
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
import plotly.io as pio
# --- Rubrics branding ---
BRAND = {
    "blue":  "#001E4F",
    "mblue": "#2C5697",
    "lblue": "#7BA4DB",
    "grey":  "#D8D7DF",
    "orange":"#CF4520",
}
FUND_COLOURS = {"GFI": BRAND["blue"], "GCF": BRAND["mblue"], "EYF": BRAND["lblue"], "Aggregate": BRAND["grey"]}

def _load_css():
    try:
        with open("assets/styles/theme.css", "r", encoding="utf-8") as f:
            st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)
    except Exception:
        pass

_load_css()

# Plotly rubrics template
brand_template = go.layout.Template(
    layout=go.Layout(
        colorway=[BRAND["blue"], BRAND["mblue"], BRAND["lblue"], BRAND["grey"], BRAND["orange"]],
        font=dict(family="Ringside, Segoe UI, Arial, sans-serif"),
        title=dict(font=dict(size=16)),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=10, r=10, t=40, b=40),
    )
)
pio.templates["rubrics"] = brand_template
pio.templates.default = "rubrics"
# ----- Rubrics brand palette & theming -----
RB_COLORS = {
    "blue": "#001E4F",      # Rubrics Blue
    "medblue": "#2C5697",   # Rubrics Medium Blue
    "ltblue": "#7BA4DB",    # Rubrics Light Blue
    "grey": "#D8D7DF",      # Rubrics Grey
    "orange": "#CF4520",    # Rubrics Orange
}

def inject_brand_css():
    st.markdown(
        """
        <style>
          /* If you have a Ringside webfont, host locally and swap the @import below with @font-face rules. */
          @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');

          :root{
            --rb-blue:   #001E4F;
            --rb-mblue:  #2C5697;
            --rb-lblue:  #7BA4DB;
            --rb-grey:   #D8D7DF;
            --rb-orange: #CF4520;
          }
          html, body, [class*="css"] {
            font-family: "Ringside", Inter, "Segoe UI", Roboto, Arial, sans-serif;
          }
          /* Accent: titles & tabs */
          .stTabs [data-baseweb="tab-list"] {
            border-bottom: 1px solid var(--rb-grey);
          }
          .stTabs [data-baseweb="tab-highlight"] {
            background: var(--rb-blue);
          }
        </style>
        """,
        unsafe_allow_html=True,
    )

# Plotly theme and helper
THEME_LAYOUT = go.Layout(
    font=dict(
        family="Ringside, Inter, Segoe UI, Roboto, Arial, sans-serif",
        size=13,
        color=RB_COLORS["blue"],
    ),
    colorway=[RB_COLORS["blue"], RB_COLORS["medblue"], RB_COLORS["ltblue"], RB_COLORS["orange"]],
    paper_bgcolor="#FFFFFF",
    plot_bgcolor="#FFFFFF",
    title_x=0.01,
    margin=dict(l=10, r=10, t=40, b=30),
)
pio.templates.default = "plotly_white"

def apply_theme(fig: go.Figure) -> go.Figure:
    fig.update_layout(THEME_LAYOUT)
    return fig

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

inject_brand_css()

# Default file name if the user doesn't upload
DEFAULT_INPUT_FILE = "Optimiser_Input_Final_v3.xlsx"
INPUT_SHEET = "Optimiser_Input"

# Prospectus caps per fund (locked as provided)
FUND_CONSTRAINTS = {
    "GFI": { "max_non_ig": 0.25, "max_em": 0.30, "max_hybrid": 0.15, "max_cash": 0.20, "max_at1": 0.15 },
    "GCF": { "max_non_ig": 0.10, "max_em": 0.35,                         "max_cash": 0.20, "max_at1": 0.10 },
    "EYF": { "max_non_ig": 1.00, "max_em": 1.00,                         "max_cash": 0.20, "max_at1": 0.00 },
}

# Monthly VaR (99% one-tailed) caps per fund
VAR99_CAP = { "GFI": 0.050, "GCF": 0.055, "EYF": 0.100 }  # expressed as proportions (e.g., 0.05 = 5%)

# Factor budgets defaults (years)
FACTOR_BUDGETS_DEFAULT = {
    "limit_krd10y": 0.75,  # years
    "limit_twist":  0.40,  # |KRD_30y - KRD_2y|
    "limit_sdv01_ig": 3.0, # years
    "limit_sdv01_hy": 1.5, # years
}

TURNOVER_DEFAULTS = { "penalty_bps_per_100": 15.0, "max_turnover": 0.25 }

# Scenario calibration (monthly, conservative start points)
# Rates shocks are bp 99% approx; spreads are widenings in bp at p99
RATES_BP99 = { "2y": 60.0, "5y": 50.0, "10y": 45.0, "30y": 40.0 }
SPREAD_BP99 = { "IG": 100.0, "HY": 200.0, "AT1": 350.0, "EM": 250.0 }

# UI helper: empty line
def spacer(h=1):
    for _ in range(h):
        st.write("")

# -----------------------------
# Column normalization & validation
# -----------------------------

# Required columns that the optimiser logic depends on
REQUIRED_COLS = [
    "Segment_ID","Name","Instrument_Type","Credit_Quality","Include",
    "Yield_Hedged_Pct","Roll_Down_bps_1Y","OAD_Years","OASD_Years",
    "KRD_2y","KRD_5y","KRD_10y","KRD_30y",
]

# Common aliases (all compared in lowercase) that will be renamed to the required names
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
    # Map lower-case names to actual case from the file
    lower_to_actual = {c.lower().strip(): c for c in df.columns}
    # Start with identity mapping
    rename_map: dict[str, str] = {}
    for target in REQUIRED_COLS:
        target_l = target.lower()
        if target_l in lower_to_actual:
            # Column present with different case; ensure exact name
            src = lower_to_actual[target_l]
            if src != target:
                rename_map[src] = target
            continue
        # Try synonyms
        for alias in COLUMN_SYNONYMS.get(target_l, []):
            if alias in lower_to_actual:
                rename_map[lower_to_actual[alias]] = target
                break
    if rename_map:
        df = df.rename(columns=rename_map)
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
    # Normalize columns and validate required fields
    df = _normalize_columns(df)
    # Coerce numerics
    num_cols = ["Yield_Hedged_Pct","Roll_Down_bps_1Y","OAD_Years","OASD_Years","Convexity","KRD_2y","KRD_5y","KRD_10y","KRD_30y"]
    for c in num_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    # Expected return
    df["ExpRet_pct"] = df.get("Yield_Hedged_Pct", 0.0) + df.get("Roll_Down_bps_1Y", 0.0) / 100.0
    # Ensure Include
    if "Include" in df.columns:
        df = df[df["Include"] == True].copy()
    df.reset_index(drop=True, inplace=True)
    return df

def tag_segments(df: pd.DataFrame) -> dict:
    seg = df["Segment_ID"].fillna("").astype(str).str.upper()
    name = df["Name"].fillna("").astype(str).str.upper()
    typ  = df["Instrument_Type"].fillna("").astype(str).str.upper()
    qual = df["Credit_Quality"].fillna("").astype(str).str.upper()

    is_tbill  = seg.str.contains("TBILL")
    is_at1    = seg.str.contains("AT1")
    is_t2     = seg.str.contains("T2")
    is_hybrid = seg.str.contains("GLOBAL_HYBRID")  # Hybrid = Global Hybrid only
    is_em     = (typ == "EM")                      # EM = EM hard-currency sleeve only
    is_hy_rt  = qual.isin(["BB","B","CCC","HY"])

    # Non-IG = HY ratings OR BankCapital (AT1/T2) OR EM HC
    is_non_ig = is_hy_rt | is_at1 | is_t2 | is_em

    # IG used for IG sDV01 budget: treat Gov as IG too
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
# 2) Scenario engine & risk metrics
# -----------------------------

def bp99_to_sigma(bp99: float) -> float:
    # Rough mapping: 99th percentile of N(0, sigma) = 2.33*sigma
    return (bp99 / 10000.0) / 2.33  # to decimal

@st.cache_data(show_spinner=False)
def simulate_mc_draws(n_draws: int, seed: int, rates_bp99: dict, spreads_bp99: dict) -> dict:
    rng = np.random.default_rng(seed)
    # Convert bp limits to normal sigmas (decimal)
    sig_r = {k: bp99_to_sigma(v) for k, v in rates_bp99.items()}
    sig_s = {k: bp99_to_sigma(v) for k, v in spreads_bp99.items()}

    # Rate shocks (decimal): arrays shape (n_draws,)
    d2 = rng.normal(0, sig_r["2y"], size=n_draws)
    d5 = rng.normal(0, sig_r["5y"], size=n_draws)
    d10 = rng.normal(0, sig_r["10y"], size=n_draws)
    d30 = rng.normal(0, sig_r["30y"], size=n_draws)

    # Credit spread shocks (decimal widenings)
    dig  = rng.normal(0, sig_s["IG"],  size=n_draws)
    dhy  = rng.normal(0, sig_s["HY"],  size=n_draws)
    dat1 = rng.normal(0, sig_s["AT1"], size=n_draws)
    dem  = rng.normal(0, sig_s["EM"],  size=n_draws)

    return {"d2": d2, "d5": d5, "d10": d10, "d30": d30, "dig": dig, "dhy": dhy, "dat1": dat1, "dem": dem}

@st.cache_data(show_spinner=False)
def build_asset_pnl_matrix(df: pd.DataFrame, tags: dict, mc: dict) -> np.ndarray:
    """
    Return S x N matrix of asset % P&L for each Monte Carlo draw.
    Approximation: Price change (%) ≈ -(KRDs·dYield) - (OASD * dSpread)
    """
    krd = df[["KRD_2y","KRD_5y","KRD_10y","KRD_30y"]].values  # years
    sdur = df["OASD_Years"].values                             # years

    # Determine which spread shock applies per asset
    N = len(df)
    is_at1 = tags["is_at1"]
    is_em  = tags["is_em"]
    is_hy  = tags["is_hy_rating"] | is_at1 | is_em  # conservative: EM counts as HY for spread shocks
    # For each asset choose dig/dhy/dat1/dem, priority AT1 > EM > HY > IG
    # Build an (S x N) spread shock matrix
    S = len(mc["d2"])
    spread = np.zeros((S, N))
    # Default IG
    spread += mc["dig"].reshape(-1,1)
    # HY
    spread[:, is_hy] = mc["dhy"].reshape(-1,1)
    # EM overrides
    spread[:, is_em]  = mc["dem"].reshape(-1,1)
    # AT1 overrides
    spread[:, is_at1] = mc["dat1"].reshape(-1,1)

    # Rate shock vector per draw
    dy = np.vstack([mc["d2"], mc["d5"], mc["d10"], mc["d30"]]).T  # S x 4
    rate_pnl = -(dy @ krd.T)    # S x N
    credit_pnl = -(spread * sdur.reshape(1,-1))  # S x N
    pnl = rate_pnl + credit_pnl
    return pnl  # in % terms

def var_cvar_from_pnl(port_pnl: np.ndarray, alpha: float = 0.99) -> tuple[float, float]:
    """Compute VaR (one-tailed) and CVaR at confidence alpha from portfolio % P&L samples."""
    losses = -port_pnl
    if losses.size == 0:
        return 0.0, 0.0
    var = np.quantile(losses, alpha)
    tail = losses[losses >= var]
    cvar = tail.mean() if tail.size else var
    return float(var), float(cvar)

# -----------------------------
# 3) Optimiser
# -----------------------------

def compute_factor_exposures(df: pd.DataFrame) -> dict:
    """Aggregate portfolio factor exposures from weights; return closures to compute exposures quickly."""
    X_krd2  = df["KRD_2y"].values
    X_krd5  = df["KRD_5y"].values
    X_krd10 = df["KRD_10y"].values
    X_krd30 = df["KRD_30y"].values
    X_sdur  = df["OASD_Years"].values

    def exposures(w: np.ndarray) -> dict:
        return {
            "KRD_2y":  float(X_krd2 @ w),
            "KRD_5y":  float(X_krd5 @ w),
            "KRD_10y": float(X_krd10 @ w),
            "KRD_30y": float(X_krd30 @ w),
            "sDV01":   float(X_sdur @ w),
        }
    return exposures

def solve_portfolio(df: pd.DataFrame,
                    tags: dict,
                    mu: np.ndarray,
                    pnl_matrix: np.ndarray,
                    fund: str,
                    params: dict,
                    prev_w: np.ndarray | None = None) -> tuple[np.ndarray, dict]:
    """
    Solve max-return (or selected objective) under caps, factor budgets, VaR/CVaR control.
    Returns weights and a metrics dict.
    """
    if not CVXPY_AVAILABLE:
        st.warning("cvxpy is not installed in this environment. Showing equal-weight placeholder.")
        n = len(df)
        w = np.ones(n) / n
        return w, {"status": "NO_CVXPY", "message": CVXPY_ERROR}

    n = len(df)
    w = cp.Variable(n, nonneg=True)
    constraints = [cp.sum(w) == 1]

    # Category masks
    is_non_ig = tags["is_non_ig"].astype(float)  # includes EM & AT1/T2
    is_em     = tags["is_em"].astype(float)
    is_hybrid = tags["is_hybrid"].astype(float)  # Global Hybrid only
    is_at1    = tags["is_at1"].astype(float)
    is_tbill  = tags["is_tbill"].astype(float)

    fc = FUND_CONSTRAINTS[fund].copy()
    # Hard caps (only if key exists)
    if "max_non_ig" in fc: constraints += [is_non_ig @ w <= fc["max_non_ig"]]
    if "max_em"     in fc: constraints += [is_em     @ w <= fc["max_em"]]
    if "max_hybrid" in fc: constraints += [is_hybrid @ w <= fc["max_hybrid"]]
    if "max_cash"   in fc: constraints += [is_tbill  @ w <= fc["max_cash"]]
    if "max_at1"    in fc: constraints += [is_at1    @ w <= fc["max_at1"]]

    # Factor budgets
    fb = params.get("factor_budgets", FACTOR_BUDGETS_DEFAULT)
    X = df[["KRD_2y","KRD_5y","KRD_10y","KRD_30y","OASD_Years"]].values
    # KRD10y budget
    constraints += [cp.abs(X[:,2] @ w) <= fb.get("limit_krd10y", 0.75)]
    # Twist budget: (30y - 2y)
    constraints += [cp.abs((X[:,3] - X[:,0]) @ w) <= fb.get("limit_twist", 0.40)]
    # sDV01 budgets IG/HY: build masks
    is_ig = tags["is_ig"].astype(float)
    is_hy = (~tags["is_ig"]).astype(float)  # conservative: non-IG counts as HY
    constraints += [cp.abs((X[:,4] * is_ig) @ w) <= fb.get("limit_sdv01_ig", 3.0)]
    constraints += [cp.abs((X[:,4] * is_hy) @ w) <= fb.get("limit_sdv01_hy", 1.5)]

    # Turnover: only enforce if previous weights are supplied and non-zero
    apply_turnover = prev_w is not None and np.sum(prev_w) > 1e-8
    if not apply_turnover:
        prev_w = np.zeros(n)
    max_turnover = params.get("max_turnover", TURNOVER_DEFAULTS["max_turnover"])
    if apply_turnover:
        constraints += [cp.norm1(w - prev_w) <= max_turnover]
        turnover_penalty = params.get("turnover_penalty", TURNOVER_DEFAULTS["penalty_bps_per_100"]) / 10000.0
    else:
        turnover_penalty = 0.0

    # CVaR (99%) linearisation
    S = pnl_matrix.shape[0]
    z = cp.Variable(S)
    alpha = cp.Variable()
    losses = -pnl_matrix @ w  # S vector
    constraints += [z >= 0, z >= losses - alpha]
    cvar = alpha + (1/(0.01 * S)) * cp.sum(z)  # CVaR_99

    # Target CVaR cap chosen to try to satisfy VaR cap; user can tighten/loosen via slider
    cvar_cap = params.get("cvar_cap", VAR99_CAP[fund] * 1.15)  # start with 15% cushion over VaR cap
    constraints += [cvar <= cvar_cap]

    # Objective
    objective_name = params.get("objective", "Max Return")
    ridge = 1e-6
    if objective_name == "Max Return":
        obj = cp.Maximize(mu @ w - turnover_penalty * cp.norm1(w - prev_w) - ridge * cp.sum_squares(w))
    elif objective_name == "Max Sharpe":
        # No full covariance here; proxy with small penalty as variance placeholder
        obj = cp.Maximize(mu @ w - 10 * cvar - turnover_penalty * cp.norm1(w - prev_w) - ridge * cp.sum_squares(w))
    elif objective_name == "Min VaR for Target Return":
        target_ret = params.get("target_return", float(np.percentile(mu, 60)))
        constraints += [mu @ w >= target_ret]
        obj = cp.Minimize(cvar + turnover_penalty * cp.norm1(w - prev_w) + ridge * cp.sum_squares(w))
    else:  # Max Drawdown proxy -> minimise CVaR subject to target return
        target_ret = params.get("target_return", float(np.percentile(mu, 50)))
        constraints += [mu @ w >= target_ret]
        obj = cp.Minimize(cvar + turnover_penalty * cp.norm1(w - prev_w) + ridge * cp.sum_squares(w))

    prob = cp.Problem(obj, constraints)
    # Solver fallback chain for robustness
    solve_errors = []
    for solver, kwargs in [
        (cp.OSQP, {"verbose": False, "max_iter": 100000}),
        (cp.SCS,  {"verbose": False, "max_iters": 25000}),
        (cp.ECOS, {"verbose": False, "max_iters": 100000}),
    ]:
        try:
            prob.solve(solver=solver, **kwargs)
            break
        except Exception as e:
            solve_errors.append(f"{getattr(solver, '__name__', str(solver))}: {e}")
            continue

    if w.value is None:
        msg = " | ".join(solve_errors) if solve_errors else "Optimiser failed to find a feasible solution."
        return None, {"status": "INFEASIBLE", "message": msg}

    w_opt = np.array(w.value).ravel()
    # Compute metrics
    port_pnl = pnl_matrix @ w_opt
    var99, cvar99 = var_cvar_from_pnl(port_pnl, 0.99)
    # mu is in decimals; convert to pp for display
    er_dec = float(mu @ w_opt)
    er_pp  = er_dec * 100.0
    yld = float(df["Yield_Hedged_Pct"].values @ w_opt)
    oad = float(df["OAD_Years"].values @ w_opt)

    metrics = {"status": "OPTIMAL", "obj": prob.value, "ExpRet_pct": er_pp, "Yield_pct": yld, "OAD_years": oad,
               "VaR99_1M": var99, "CVaR99_1M": cvar99, "weights": w_opt}
    return w_opt, metrics

# -----------------------------
# 4) Visuals
# -----------------------------

def fmt_pct(x, digits=2):
    return f"{x*100:.{digits}f}%"

def fmt_pp(x, digits=2):
    return f"{x:.{digits}f}pp"

def style_dataframe_percent(df, pct_cols, digits=2):
    df = df.copy()
    for c in pct_cols:
        df[c] = (df[c] * 100).round(digits)
    return df

def kpi_number(title: str, value: float, kind: str = "pct"):
    # kind: "pct" -> x is decimal; "pp" -> x is already percent points
    if kind == "pp":
        val = value
        suffix = "pp"
        vf = ".2f"
    else:
        val = value * 100
        suffix = "%"
        vf = ".2f"
    fig = go.Figure(go.Indicator(
        mode="number",
        value=val,
        title={"text": title},
        number={"suffix": suffix, "valueformat": vf}
    ))
    fig.update_layout(template="rubrics", margin=dict(l=5,r=5,t=30,b=5), height=110, showlegend=False)
    return apply_theme(fig)

def bar_allocation(df, weights, title):
    ser = pd.Series(weights, index=df["Name"]).sort_values(ascending=False)
    fig = go.Figure(go.Bar(x=ser.index, y=ser.values))
    fig.update_layout(title=title, xaxis_title="Segment", yaxis_title="Weight", height=380, margin=dict(l=10,r=10,t=40,b=80))
    return apply_theme(fig)

def exposures_vs_budgets(df, weights, budgets: dict, title: str):
    is_ig_mask = tag_segments(df)["is_ig"]
    oasd = df["OASD_Years"].values
    vals = {
        "KRD 2y": float(df["KRD_2y"].values @ weights),
        "KRD 5y": float(df["KRD_5y"].values @ weights),
        "KRD 10y": float(df["KRD_10y"].values @ weights),
        "KRD 30y": float(df["KRD_30y"].values @ weights),
        "sDV01 IG": float(np.sum(oasd * weights * is_ig_mask)),
        "sDV01 HY": float(np.sum(oasd * weights * (~is_ig_mask))),
    }
    x = list(vals.keys()); y = list(vals.values())
    fig = go.Figure(go.Bar(x=x, y=y))
    # Budget lines (only those that map)
    fig.add_hline(y=budgets.get("limit_krd10y", 0.75), line_dash="dot", annotation_text="KRD10y cap", annotation_position="top left")
    fig.add_hline(y=budgets.get("limit_sdv01_ig", 3.0), line_dash="dot", annotation_text="sDV01 IG cap", annotation_position="bottom left")
    fig.add_hline(y=budgets.get("limit_sdv01_hy", 1.5), line_dash="dot", annotation_text="sDV01 HY cap", annotation_position="bottom left")
    fig.add_hline(y=budgets.get("limit_twist", 0.40), line_dash="dot", annotation_text="Twist cap", annotation_position="bottom left")
    fig.update_layout(title=title, height=300, margin=dict(l=10,r=10,t=40,b=20))
    return apply_theme(fig)

def scenario_histogram(port_pnl, title="Scenario P&L (1M)"):
    fig = go.Figure(data=[go.Histogram(x=port_pnl * 100, nbinsx=40)])
    var99, cvar99 = var_cvar_from_pnl(port_pnl, 0.99)
    fig.add_vline(x=-var99 * 100, line_dash="dash", annotation_text="VaR99", annotation_position="top left")
    fig.add_vline(x=-cvar99 * 100, line_dash="dot", annotation_text="CVaR99", annotation_position="top left")
    fig.update_layout(title=title, xaxis_title="% P&L", yaxis_title="Count", height=300, margin=dict(l=10,r=10,t=40,b=20))
    return apply_theme(fig)

def contributions_table(df, weights, mu):
    contr = pd.DataFrame({
        "Segment": df["Name"],
        "Weight": weights,
        # mu is decimal; show pp in the contributions view
        "ER_Contribution_pct": weights * (mu * 100.0),
        "Yield_pct": df["Yield_Hedged_Pct"].values,
        "RollDown_pct": df["Roll_Down_bps_1Y"].values/100.0,
        "OAD_Years": df["OAD_Years"].values,
        "OASD_Years": df["OASD_Years"].values
    }).sort_values("Weight", ascending=False)
    return contr

def heatmap_funds_losses(fund_results: dict):
    # funds x scenarios average loss; use mean of tail scenario losses or mean absolute? We'll use mean % loss
    mats = []
    funds = []
    for f,(res, pnl) in fund_results.items():
        funds.append(f)
        mats.append(pnl.mean(axis=1))  # naive summary across assets → mean across assets by scenario; but we need per scenario portfolio P&L -> compute now
    # Actually compute portfolio P&L per draw for each fund
    Z = []
    for f,(res, pnl) in fund_results.items():
        w = res["weights"]
        Z.append((pnl @ w))
    # Stack scenarios in columns; funds in rows
    Z = np.vstack(Z)
    # We have MC draws, not labeled scenarios; show quantiles by buckets (10,50,90). For simplicity, show mean (column) ; but better show distribution summary.
    # We'll show a heatmap of selected percentiles across funds.
    percs = [1,5,10,25,50,75,90,95,99]
    data = []
    for row in Z:
        data.append([np.percentile(row, p) for p in percs])
    fig = go.Figure(data=go.Heatmap(
        z=data,
        x=[f"P{p}" for p in percs],
        y=funds
    ))
    fig.update_layout(title="Scenario Distribution by Fund (Portfolio P&L percentiles)", height=300, margin=dict(l=10,r=10,t=40,b=20))
    return apply_theme(fig)

# -----------------------------
# 5) App UI
# -----------------------------

st.title("Rubrics Fixed Income Optimiser")
st.caption("Forward‑looking allocation using carry + roll expected returns, KRD/sDV01 factor risk, Monte‑Carlo VaR, and fund‑specific prospectus caps.")
spacer(1)

with st.expander("❓ How this optimiser works & what the controls do", expanded=False):
    st.markdown(
        """
- **Objective**: chooses the optimisation target (e.g., *Max Return*, *Max Sharpe*).
- **Expected return (pp)**: carry + 1‑year roll‑down. “pp” = percentage points.
- **VaR/CVaR (monthly, 99%)**: simulated 1‑month loss tail using rate/spread shocks.
- **Factor budgets**:
  - **KRD** (Key Rate Duration): rate sensitivity at selected maturities.
  - **Twist (30y–2y)**: steepener/flattening exposure budget.
  - **sDV01 IG/HY**: credit spread duration (IG vs Non‑IG sleeves).
- **Prospectus caps**: hard limits per fund (Non‑IG, EM, Hybrid, AT1, Cash).
- **Turnover**: limits change per rebalance and applies a penalty (in bps per 100% turnover).
Changing a slider updates the optimisation and the charts so you can see the impact immediately.
"""
    )

# File input
with st.expander("Data source (Excel) • required columns: Segment_ID, Name, Yield_Hedged_Pct, Roll_Down_bps_1Y, OAD_Years, OASD_Years, KRD_2y/5y/10y/30y, Credit_Quality, Instrument_Type, Include", expanded=False):
    upload = st.file_uploader("Upload Optimiser_Input_Final_v3.xlsx (sheet Optimiser_Input)", type=["xlsx"], accept_multiple_files=False)
    st.write("If no file is uploaded, the app will try to read:", f"`{DEFAULT_INPUT_FILE}`")

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

# Controls (global)
with st.sidebar:
    st.header("Global Settings")
    seed = st.number_input("Random seed", min_value=0, value=42, step=1)
    n_draws = st.number_input("Monte Carlo draws (monthly)", min_value=200, max_value=10000, value=2000, step=100, help="Number of monthly scenarios used to estimate VaR/CVaR; more = smoother but slower.")
    st.write("Rate shocks (bp @99%):")
    c1, c2, c3, c4 = st.columns(4)
    with c1: RATES_BP99["2y"] = st.number_input("2y", value=float(RATES_BP99["2y"]), help="Approximate 99th‑percentile monthly rate change in basis points at each key rate.")
    with c2: RATES_BP99["5y"] = st.number_input("5y", value=float(RATES_BP99["5y"]))
    with c3: RATES_BP99["10y"] = st.number_input("10y", value=float(RATES_BP99["10y"]))
    with c4: RATES_BP99["30y"] = st.number_input("30y", value=float(RATES_BP99["30y"]))

    st.write("Spread widenings (bp @99%):")
    c1, c2, c3, c4 = st.columns(4)
    with c1: SPREAD_BP99["IG"]  = st.number_input("IG", value=float(SPREAD_BP99["IG"]), help="Approximate 99th‑percentile monthly spread widening (bp) for each sleeve.")
    with c2: SPREAD_BP99["HY"]  = st.number_input("HY", value=float(SPREAD_BP99["HY"]))
    with c3: SPREAD_BP99["AT1"] = st.number_input("AT1", value=float(SPREAD_BP99["AT1"]))
    with c4: SPREAD_BP99["EM"]  = st.number_input("EM", value=float(SPREAD_BP99["EM"]))

    st.divider()
    st.subheader("Default Factor Budgets")
    limit_krd10y = st.number_input("|KRD 10y| cap (yrs)", value=FACTOR_BUDGETS_DEFAULT["limit_krd10y"], step=0.05, format="%.2f", help="Limit to 10y interest‑rate exposure (in duration years) for the portfolio.")
    limit_twist  = st.number_input("Twist (30y–2y) cap (yrs)", value=FACTOR_BUDGETS_DEFAULT["limit_twist"], step=0.05, format="%.2f", help="Twist exposure: how much steepener/flattening risk is allowed.")
    limit_sdv01_ig = st.number_input("sDV01 IG cap (yrs)", value=FACTOR_BUDGETS_DEFAULT["limit_sdv01_ig"], step=0.1, format="%.1f", help="Spread DV01 budget for Investment Grade sleeves.")
    limit_sdv01_hy = st.number_input("sDV01 HY cap (yrs)", value=FACTOR_BUDGETS_DEFAULT["limit_sdv01_hy"], step=0.1, format="%.1f", help="Spread DV01 budget for Non‑IG sleeves (HY/EM/AT1).")

    st.divider()
    st.subheader("Turnover")
    penalty_bps = st.number_input("Penalty (bps per 100% turnover)", value=TURNOVER_DEFAULTS["penalty_bps_per_100"], step=1.0, help="Transaction cost / frictions applied to changes in weights.")
    max_turn = st.slider("Max turnover per rebalance", 0.0, 1.0, TURNOVER_DEFAULTS["max_turnover"], 0.01, help="Hard cap on total absolute change in portfolio weights.")

    st.subheader("Previous Weights (optional)")
    prev_file = st.file_uploader("CSV with columns [Segment or Name, Weight]", type=["csv"], key="prev_weights")
    prev_w_vec = None
    if prev_file is not None:
        _pw = pd.read_csv(prev_file)
        name_col = "Segment" if "Segment" in _pw.columns else ("Name" if "Name" in _pw.columns else None)
        if name_col is not None and "Weight" in _pw.columns:
            _pw[name_col] = _pw[name_col].astype(str).str.strip()
            prev_w_vec = df["Name"].astype(str).str.strip().map(_pw.set_index(name_col)["Weight"]).fillna(0.0).values
            s = prev_w_vec.sum()
            if s > 0:
                prev_w_vec = prev_w_vec / s
        else:
            st.warning("Prev weights CSV must have columns [Segment or Name, Weight].")

    st.subheader("Display options")
    min_weight_display = st.slider("Hide weights below", 0.0, 0.01, 0.001, 0.0005, format="%.3f")

# Prepare scenarios
mc = simulate_mc_draws(int(n_draws), int(seed), dict(RATES_BP99), dict(SPREAD_BP99))
pnl_matrix_assets = build_asset_pnl_matrix(df, tags, mc)  # S x N
# Expected return vector: keep pp for display, convert to decimals for optimisation
mu_pp = df["ExpRet_pct"].values.astype(float)
mu = mu_pp / 100.0

# Helper: run optimisation for a single fund
def run_fund(fund: str, objective: str, var_cap_override: float | None = None, prev_w=None):
    params = {
        "factor_budgets": {
            "limit_krd10y": limit_krd10y, "limit_twist": limit_twist,
            "limit_sdv01_ig": limit_sdv01_ig, "limit_sdv01_hy": limit_sdv01_hy
        },
        "turnover_penalty": penalty_bps,
        "max_turnover": max_turn,
        "objective": objective,
        # CVaR cap set slightly above VaR cap (iteratively tightened via UI changes)
        "cvar_cap": (var_cap_override if var_cap_override is not None else VAR99_CAP[fund] * 1.15),
    }
    w, metrics = solve_portfolio(df, tags, mu, pnl_matrix_assets, fund, params, prev_w)
    if w is None:
        return None, metrics, None
    port_pnl = pnl_matrix_assets @ w
    return w, metrics, port_pnl

# Top-level tabs
tab_overview, tab_fund = st.tabs(["Overview (Compare Funds)", "Fund Detail (Tune One)"])

# -----------------------------
# Overview Tab: run each fund with its own VaR cap and defaults
# -----------------------------
with tab_overview:
    st.subheader("Compare Funds: positioning & risk")
    with st.expander("What these controls and charts mean"):
        st.markdown(
            """
**Objective** – chooses the optimiser’s target.  
**Expected Return** – annualised carry + 1y roll‑down (pp).  
**VaR/CVaR (1M)** – 99% tail metrics from monthly Monte Carlo scenarios.  
**Factor budgets** – caps on interest‑rate key‑rate exposures and spread duration (years).  
**Prospectus caps** – hard limits specific to each fund (Non‑IG, EM, AT1, Hybrid, Cash).  

**Tip:** Tightening budgets or VaR caps usually lowers expected return but improves downside risk. Raising them does the opposite.
"""
        )
    objective = st.selectbox("Objective", ["Max Return","Max Sharpe","Min VaR for Target Return","Max Drawdown Proxy"], index=0, key="overview_obj")

    # Run funds
    fund_outputs = {}
    for f in ["GFI","GCF","EYF"]:
        w, metrics, port_pnl = run_fund(f, objective, prev_w=prev_w_vec)
        if w is not None:
            metrics["weights"] = w
            fund_outputs[f] = (metrics, pnl_matrix_assets)  # keep asset pnl for heatmap
        else:
            st.warning(f"{f}: {metrics.get('status','')} — {metrics.get('message','')}")

    # KPI tiles
    cols = st.columns(4)
    idx = 0
    agg_weights = None
    for f in ["GFI","GCF","EYF"]:
        if f in fund_outputs:
            m,_ = fund_outputs[f]
            with cols[idx]:
                st.plotly_chart(kpi_number(f"{f} – Expected Return", m["ExpRet_pct"], kind="pp"), use_container_width=True)
                st.plotly_chart(kpi_number(f"{f} – VaR99 1M", m["VaR99_1M"], kind="pct"), use_container_width=True)
                cap = VAR99_CAP[f]
                status = "✅ within cap" if m["VaR99_1M"] <= cap else "❌ over cap"
                st.caption(f"VaR cap {cap*100:.2f}% — {status}")
            idx += 1
    # Aggregate (equal-weight of funds that solved)
    if len(fund_outputs) > 0:
        W = [fund_outputs[f][0]["weights"] for f in fund_outputs]
        agg_weights = np.mean(np.vstack(W), axis=0)
        port_pnl_agg = pnl_matrix_assets @ agg_weights
        var99_agg, cvar99_agg = var_cvar_from_pnl(port_pnl_agg, 0.99)
        er_agg_pp = float(mu @ agg_weights) * 100.0
        with cols[3]:
            st.plotly_chart(kpi_number("Aggregate – Expected Return", er_agg_pp, kind="pp"), use_container_width=True)
            st.plotly_chart(kpi_number("Aggregate – VaR99 1M", var99_agg, kind="pct"), use_container_width=True)

    spacer(1)
    # Allocation by segment (stacked bars)
    st.markdown("**Allocation by segment (GFI / GCF / EYF / Aggregate)**")
    alloc_df = pd.DataFrame(index=df["Name"])
    for f in ["GFI","GCF","EYF"]:
        if f in fund_outputs:
            alloc_df[f] = fund_outputs[f][0]["weights"]
    if agg_weights is not None:
        alloc_df["Aggregate"] = agg_weights
    alloc_df = alloc_df.fillna(0.0)
    alloc_df = alloc_df.mask(alloc_df.abs() < min_weight_display, other=0.0)
    st.dataframe(alloc_df.sort_index().style.format("{:.2%}"), use_container_width=True, height=260)
    fig_alloc = go.Figure()
    for col in alloc_df.columns:
        y = alloc_df[col].values
        y = np.where(np.abs(y) < min_weight_display, 0.0, y)
        color = {"GFI": RB_COLORS["blue"], "GCF": RB_COLORS["medblue"], "EYF": RB_COLORS["ltblue"], "Aggregate": RB_COLORS["orange"]}.get(col, None)
        fig_alloc.add_bar(name=col, x=alloc_df.index, y=y, marker_color=color)
    fig_alloc.update_layout(barmode="group", height=380, margin=dict(l=10,r=10,t=40,b=80), xaxis_title="Segment", yaxis_title="Weight")
    st.plotly_chart(fig_alloc, use_container_width=True)

    spacer(1)
    # Factor exposures vs budgets (show KRD10 and twist + sDV01 budgets summary)
    st.markdown("**Factor exposures vs budgets** (KRD & sDV01)")
    fig_fb = go.Figure()
    # Summaries for each fund
    for f in ["GFI","GCF","EYF"]:
        if f in fund_outputs:
            w = fund_outputs[f][0]["weights"]
            vals = {
                "KRD10y": float(df["KRD_10y"].values @ w),
                "Twist(30-2)": float((df["KRD_30y"].values - df["KRD_2y"].values) @ w),
                "sDV01": float(df["OASD_Years"].values @ w),
            }
            fig_fb.add_bar(name=f" {f} KRD10y", x=["KRD10y"], y=[vals["KRD10y"]])
            fig_fb.add_bar(name=f" {f} Twist", x=["Twist(30-2)"], y=[vals["Twist(30-2)"]])
            fig_fb.add_bar(name=f" {f} sDV01", x=["sDV01"], y=[vals["sDV01"]])
    fig_fb.update_layout(barmode="group", height=300, margin=dict(l=10,r=10,t=40,b=20))
    st.plotly_chart(fig_fb, use_container_width=True)

    spacer(1)
    # Scenario percentiles heatmap
    if len(fund_outputs) > 0:
        z_fig = heatmap_funds_losses(fund_outputs)
        st.plotly_chart(z_fig, use_container_width=True)

    spacer(1)
    st.markdown("**Download allocations**")
    if len(fund_outputs) > 0:
        out = pd.DataFrame({"Segment": df["Name"]})
        for f in ["GFI","GCF","EYF"]:
            if f in fund_outputs:
                out[f] = fund_outputs[f][0]["weights"]
        st.download_button("Download CSV (fund allocations)", out.to_csv(index=False).encode("utf-8"), file_name="allocations.csv", mime="text/csv")

# -----------------------------
# Fund Detail Tab
# -----------------------------
with tab_fund:
    c0, c1 = st.columns([1,2])
    with c0:
        fund = st.selectbox("Fund", ["GFI","GCF","EYF"], index=0, help="Choose which fund’s caps and budgets to tune and optimise.")
        objective = st.selectbox("Objective", ["Max Return","Max Sharpe","Min VaR for Target Return","Max Drawdown Proxy"], index=0, help="Select the optimiser target for this fund only.")
        var_cap = st.slider(f"{fund} monthly VaR99 cap (%)", 0.0, 15.0, float(VAR99_CAP[fund]*100), 0.1) / 100.0
        st.write("Prospectus caps:")
        fc = FUND_CONSTRAINTS[fund]
        # Show & allow temporary overrides
        max_non_ig = st.slider("Max Non‑IG weight", 0.0, 1.0, float(fc.get("max_non_ig",1.0)), 0.01, help="Includes HY ratings, EM hard‑currency, and Bank Capital (AT1/T2).")
        max_em     = st.slider("Max EM weight",     0.0, 1.0, float(fc.get("max_em",1.0)),     0.01, help="EM hard‑currency sleeve only.")
        max_hybrid = st.slider("Max Hybrid weight", 0.0, 1.0, float(fc.get("max_hybrid",1.0)) if "max_hybrid" in fc else 0.0, 0.01, help="Global Hybrid sleeve only.")
        max_cash   = st.slider("Max Cash weight",   0.0, 1.0, float(fc.get("max_cash",1.0)),   0.01, help="US T‑Bills sleeve; caps cash balance.")
        max_at1    = st.slider("Max AT1 weight",    0.0, 1.0, float(fc.get("max_at1",1.0)),    0.01, help="Bank Additional Tier‑1 sleeve (per prospectus restrictions).")

        # Build a temporary override dict (used only in this tab run). Backup and restore afterwards
        _fc_backup = FUND_CONSTRAINTS[fund].copy()
        FUND_CONSTRAINTS[fund] = {"max_non_ig": max_non_ig, "max_em": max_em, "max_cash": max_cash, "max_at1": max_at1}
        if "max_hybrid" in fc:
            FUND_CONSTRAINTS[fund]["max_hybrid"] = max_hybrid

        st.write("Factor budgets (yrs):")
        lk = st.number_input("|KRD 10y| cap", value=limit_krd10y, step=0.05, format="%.2f", help="Limit to 10y interest‑rate exposure (in duration years) for the portfolio.")
        lt = st.number_input("Twist (30y–2y) cap", value=limit_twist, step=0.05, format="%.2f", help="Twist exposure: how much steepener/flattening risk is allowed.")
        lig = st.number_input("sDV01 IG cap", value=limit_sdv01_ig, step=0.1, format="%.1f", help="Spread DV01 budget for Investment Grade sleeves.")
        lhy = st.number_input("sDV01 HY cap", value=limit_sdv01_hy, step=0.1, format="%.1f", help="Spread DV01 budget for Non‑IG sleeves (HY/EM/AT1).")

        fb_over = {"limit_krd10y": lk, "limit_twist": lt, "limit_sdv01_ig": lig, "limit_sdv01_hy": lhy}

    with c1:
        # Run optimisation for the chosen fund with overrides
        params = {
            "factor_budgets": fb_over,
            "turnover_penalty": penalty_bps,
            "max_turnover": max_turn,
            "objective": objective,
            "cvar_cap": var_cap * 1.15,  # CVaR cap above VaR target
        }
        w, metrics = None, None
        w, metrics = solve_portfolio(df, tags, mu, pnl_matrix_assets, fund, params, prev_w=prev_w_vec)
        # Restore constraints regardless of outcome
        FUND_CONSTRAINTS[fund] = _fc_backup
        if w is None:
            st.error(f"Optimisation failed: {metrics.get('status','')} – {metrics.get('message','')}")
            st.stop()

        port_pnl = pnl_matrix_assets @ w
        var99, cvar99 = var_cvar_from_pnl(port_pnl, 0.99)
        cols = st.columns(4)
        with cols[0]: st.plotly_chart(kpi_number("Expected Return (ann.)", metrics["ExpRet_pct"], kind="pp"), use_container_width=True)
        with cols[1]: st.plotly_chart(kpi_number("VaR99 1M", var99, kind="pct"), use_container_width=True)
        with cols[2]: st.plotly_chart(kpi_number("CVaR99 1M", cvar99, kind="pct"), use_container_width=True)
        with cols[3]: st.plotly_chart(kpi_number("Portfolio Yield", metrics["Yield_pct"], kind="pp"), use_container_width=True)

        cap = var_cap
        status = "✅ within cap" if var99 <= cap else "❌ over cap"
        st.caption(f"VaR99 1M: {var99*100:.2f}% (cap {cap*100:.2f}%) {status}")

        # Allocation
        st.plotly_chart(bar_allocation(df, w, f"{fund} – Allocation by Segment"), use_container_width=True)

        # Exposures vs budgets
        st.plotly_chart(exposures_vs_budgets(df, w, fb_over, f"{fund} – Factor Exposures vs Budgets"), use_container_width=True)

        # Scenario distribution
        st.plotly_chart(scenario_histogram(port_pnl, f"{fund} – Scenario P&L Distribution"), use_container_width=True)

        # Contributions table
        contr = contributions_table(df, w, mu)
        st.dataframe(contr, use_container_width=True, height=360)

        # Download
        out_csv = contr[["Segment","Weight","ER_Contribution_pct"]].to_csv(index=False).encode("utf-8")
        st.download_button("Download weights & ER contributions (CSV)", out_csv, file_name=f"{fund}_allocation.csv", mime="text/csv")

st.caption("© Rubrics – internal research tool. Forward-looking estimates; not investment advice.")
