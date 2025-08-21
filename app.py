
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

# --- Rubrics branding & theme (single source of truth) ---
RB_COLORS = {
    "blue":   "#001E4F",  # Rubrics Blue
    "medblue":"#2C5697",  # Rubrics Medium Blue
    "ltblue": "#7BA4DB",  # Rubrics Light Blue
    "grey":   "#D8D7DF",  # Rubrics Grey
    "orange": "#CF4520",  # Rubrics Orange
}
FUND_COLOR = {"GFI": RB_COLORS["blue"], "GCF": RB_COLORS["medblue"], "EYF": RB_COLORS["ltblue"], "Aggregate": RB_COLORS["orange"]}

def inject_brand_css():
    st.markdown("""
    <style>
      /* If you have Ringside as a webfont, swap these @imports for @font-face rules. */
      @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
      :root{ --rb-blue:#001E4F; --rb-mblue:#2C5697; --rb-lblue:#7BA4DB; --rb-grey:#D8D7DF; --rb-orange:#CF4520; }
      html, body, [class*="css"] { font-family: "Ringside", Inter, "Segoe UI", Roboto, Arial, sans-serif; }
      .stTabs [data-baseweb="tab-list"]{ border-bottom:1px solid var(--rb-grey); }
      .stTabs [data-baseweb="tab-highlight"]{ background:var(--rb-blue); }

      /* --- tooltip title row for charts/tables/KPIs --- */
      .rb-title { display:flex; align-items:center; justify-content:space-between; margin: .4rem 0 .2rem 0; }
      .rb-title .rb-label { font-weight:600; color: var(--rb-blue); }
      .rb-help { color: var(--rb-mblue); cursor: help; font-weight:700; user-select:none; }
      .rb-help:hover { color: var(--rb-orange); }
    </style>
    """, unsafe_allow_html=True)

def title_with_help(label: str, help_text: str):
    """Renders a label with a right-aligned 'ⓘ' native tooltip (title attr)."""
    st.markdown(
        f'<div class="rb-title"><div class="rb-label">{label}</div>'
        f'<div class="rb-help" title="{help_text}">ⓘ</div></div>',
        unsafe_allow_html=True
    )

def impact_text(increase: str, decrease: str) -> str:
    """Helper to summarise impact of increasing/decreasing a control."""
    return f" Increasing this: {increase}. Decreasing this: {decrease}."

BRAND_TEMPLATE = go.layout.Template(
    layout=go.Layout(
        colorway=[RB_COLORS["blue"], RB_COLORS["medblue"], RB_COLORS["ltblue"], RB_COLORS["grey"], RB_COLORS["orange"]],
        font=dict(family="Ringside, Inter, Segoe UI, Roboto, Arial, sans-serif"),
        legend=dict(orientation="h", y=1.02, yanchor="bottom", x=1, xanchor="right"),
        margin=dict(l=10, r=10, t=40, b=40),
        paper_bgcolor="#FFFFFF", plot_bgcolor="#FFFFFF", title=dict(font=dict(size=16))
    )
)
pio.templates["rubrics"] = BRAND_TEMPLATE
pio.templates.default = "rubrics"
plotly_default_config = {"displaylogo": False, "responsive": True}

# Theme helper (kept for API compatibility; template already applied globally)
def apply_theme(fig: go.Figure) -> go.Figure:
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
    page_icon=None,
    layout="wide",
    initial_sidebar_state="expanded",
)

inject_brand_css()

# Default file name if the user doesn't upload
DEFAULT_INPUT_FILE = "Optimiser_Input_Final_v3.xlsx"
SAMPLE_INPUT_FILE = "sample_data/Optimiser_Input_Sample.xlsx"

# Sheets
INPUT_SHEET = "Optimiser_Input"
METADATA_SHEET = "MetaData"

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

# Required columns for main sheet (Optimiser_Input)
REQUIRED_MAIN_COLS = [
    "Bloomberg_Ticker","Name","Instrument_Type",
    "Yield_Hedged_Pct","Roll_Down_bps_1Y","OAD_Years","OASD_Years",
    "KRD_2y","KRD_5y","KRD_10y","KRD_30y","Include"
]

# Required columns for MetaData sheet
REQUIRED_META_COLS = [
    "Bloomberg_Ticker",
    # classification booleans maintained in MetaData:
    "Is_Non_IG", "Is_EM", "Is_AT1", "Is_T2", "Is_Hybrid", "Is_Cash"   # if some are missing, we'll infer
]

# Synonyms for main sheet columns
MAIN_SYNONYMS = {
    "bloomberg_ticker": ["ticker","bbg_ticker"],
    "instrument_type": ["type","instr_type"],
    "yield_hedged_pct": ["yield_hedged","yield_hedged_percent","yield_hedged_%"],
    "roll_down_bps_1y": ["roll_down_bps","rolldown_bps_1y"],
    "oasd_years": ["spread_dur","sdur","asd_years"],
}

# Synonyms for MetaData sheet columns
META_SYNONYMS = {
    "bloomberg_ticker": ["ticker","bbg_ticker"],
    "is_non_ig": ["non_ig","isnonig","is_high_yield"],
    "is_em": ["is_emhc","is_em_hc","is_emerging"],
    "is_at1": ["is_bank_at1","is_additional_tier1"],
    "is_t2": ["is_bank_t2","is_tier2"],
    "is_hybrid": ["is_global_hybrid","is_hybrids"],
    "is_cash": ["is_tbill","is_cash_like"]
}

def _to_bool(s):
    """Coerce typical TRUE/FALSE/Yes/No/1/0 strings to bool; leave NaN -> False."""
    return pd.Series(s).astype(str).str.strip().str.lower().map(
        {"true": True, "t": True, "1": True, "yes": True, "y": True, "false": False, "f": False, "0": False, "no": False, "n": False}
    ).fillna(False).astype(bool)



    # Normalise Bloomberg_Ticker and Name in both frames
    df_norm = df.copy()
    meta_norm = meta.copy()
    
    # Normalise Bloomberg_Ticker
    if 'Bloomberg_Ticker' in df_norm.columns:
        df_norm['Bloomberg_Ticker_norm'] = df_norm['Bloomberg_Ticker'].astype(str).str.strip().str.upper()
    if 'Bloomberg_Ticker' in meta_norm.columns:
        meta_norm['Bloomberg_Ticker_norm'] = meta_norm['Bloomberg_Ticker'].astype(str).str.strip().str.upper()
    
    # Normalise Name
    if 'Name' in df_norm.columns:
        df_norm['Name_norm'] = df_norm['Name'].astype(str).str.strip().str.upper()
    if 'Name' in meta_norm.columns:
        meta_norm['Name_norm'] = meta_norm['Name'].astype(str).str.strip().str.upper()
    
    # Select join key: prefer Bloomberg_Ticker if present in both, else use Name
    if ('Bloomberg_Ticker_norm' in df_norm.columns and 
        'Bloomberg_Ticker_norm' in meta_norm.columns):
        join_key = 'Bloomberg_Ticker_norm'
        join_col = 'Bloomberg_Ticker'
    elif ('Name_norm' in df_norm.columns and 
          'Name_norm' in meta_norm.columns):
        join_key = 'Name_norm'
        join_col = 'Name'
    else:
        # No valid join key found
        return df, list(df.index)
    
    # Select metadata columns to merge
    meta_cols = ['Bloomberg_Ticker', 'Name', 'Credit_Quality', 'Is_AT1', 'Is_EM', 'Is_Non_IG', 'Is_Hybrid', 'Is_TBill']
    available_meta_cols = [col for col in meta_cols if col in meta_norm.columns]
    
    if not available_meta_cols:
        return df, list(df.index)
    
    # Merge metadata
    merged = df_norm.merge(
        meta_norm[available_meta_cols + [join_key]], 
        on=join_key, 
        how='left', 
        suffixes=('', '_meta')
    )
    
    # Overwrite existing columns with metadata versions
    take = ['Credit_Quality', 'Is_AT1', 'Is_EM', 'Is_Non_IG', 'Is_Hybrid', 'Is_TBill']
    for col in take:
        if col in merged.columns and f'{col}_meta' in merged.columns:
            merged[col] = merged[f'{col}_meta'].combine_first(merged[col])
            merged = merged.drop(columns=[f'{col}_meta'])
    
    # Coerce boolean columns
    for col in ['Is_AT1', 'Is_EM', 'Is_Non_IG', 'Is_Hybrid', 'Is_TBill']:
        if col in merged.columns:
            merged[col] = _to_bool(merged[col])
    
    # Clean up temporary columns
    merged = merged.drop(columns=[col for col in merged.columns if col.endswith('_norm')])
    
    # Identify records without metadata
    missing_meta = merged[merged[available_meta_cols].isna().all(axis=1)]
    missing_tickers = []
    
    if 'Bloomberg_Ticker' in merged.columns:
        missing_tickers = missing_meta['Bloomberg_Ticker'].dropna().tolist()
    elif 'Name' in merged.columns:
        missing_tickers = missing_meta['Name'].dropna().tolist()
    
    # Add metadata missing flag
    merged['_meta_missing'] = merged[available_meta_cols].isna().all(axis=1)
    
    return merged, missing_tickers

def _rename_with_synonyms(df, required, synonyms):
    low = {c.lower().strip(): c for c in df.columns}
    ren = {}
    for need in required:
        ln = need.lower()
        if ln in low and low[ln] != need:
            ren[low[ln]] = need
            continue
        for alt in synonyms.get(ln, []):
            if alt in low:
                ren[low[alt]] = need
                break
    if ren: df = df.rename(columns=ren)
    missing = [c for c in required if c not in df.columns]
    if missing:
        # we will tolerate some MetaData missing (we'll infer), but not missing join key
        if "Bloomberg_Ticker" in missing:
            raise ValueError(f"Missing required column(s): {', '.join(missing)}")
    return df

# -----------------------------
# 1) Data ingest & classification
# -----------------------------

@st.cache_data(show_spinner=False)
def load_joined_input(uploaded_file_bytes: bytes | None, path: str = DEFAULT_INPUT_FILE) -> pd.DataFrame:
    if uploaded_file_bytes is not None:
        bio = io.BytesIO(uploaded_file_bytes)
        xls = pd.ExcelFile(bio, engine="openpyxl")
    else:
        xls = pd.ExcelFile(path, engine="openpyxl")

    # Load sheets (sheet names fixed: 'Optimiser_Input' and 'MetaData')
    df_main = pd.read_excel(xls, sheet_name="Optimiser_Input")
    df_meta = pd.read_excel(xls, sheet_name="MetaData")

    df_main = _rename_with_synonyms(df_main, REQUIRED_MAIN_COLS, MAIN_SYNONYMS)
    df_meta = _rename_with_synonyms(df_meta, REQUIRED_META_COLS, META_SYNONYMS)

    # Coerce numerics
    for c in ["Yield_Hedged_Pct","Roll_Down_bps_1Y","OAD_Years","OASD_Years","KRD_2y","KRD_5y","KRD_10y","KRD_30y"]:
        if c in df_main.columns:
            df_main[c] = pd.to_numeric(df_main[c], errors="coerce").fillna(0.0)

    # Merge on Bloomberg_Ticker
    df = df_main.merge(df_meta, on="Bloomberg_Ticker", how="left", suffixes=("", "_meta"))

    # Build expected return (carry + roll)
    df["ExpRet_pct"] = df.get("Yield_Hedged_Pct", 0.0) + df.get("Roll_Down_bps_1Y", 0.0)/100.0

    # Include filter
    if "Include" in df.columns:
        df = df[df["Include"] == True].copy()

    # Ensure boolean flags exist (fallback inference if MetaData omitted some)
    def _bool(col, fallback=None):
        if col in df.columns: return df[col].fillna(False).astype(bool)
        if fallback is not None: return fallback
        return pd.Series(False, index=df.index)

    is_at1    = _bool("Is_AT1",    df["Instrument_Type"].str.upper().str.contains("AT1", na=False))
    is_t2     = _bool("Is_T2",     df["Instrument_Type"].str.upper().str.contains("T2",  na=False))
    is_em     = _bool("Is_EM",     df["Instrument_Type"].str.upper().eq("EM"))
    is_hybrid = _bool("Is_Hybrid", df["Name"].str.upper().str.contains("HYBRID", na=False))
    is_cash   = _bool("Is_Cash",   df["Instrument_Type"].str.upper().eq("CASH") | df["Name"].str.upper().str.contains("T-BILL|TBILL", na=False))
    # Non‑IG from MetaData, else conservative fallback (treat AT1/T2/EM as non‑IG)
    is_non_ig = _bool("Is_Non_IG", is_at1 | is_t2 | is_em)

    df["Is_AT1"] = is_at1
    df["Is_T2"] = is_t2
    df["Is_EM"] = is_em
    df["Is_Hybrid"] = is_hybrid
    df["Is_Cash"] = is_cash
    df["Is_Non_IG"] = is_non_ig

    # Build IG flag = not Non‑IG (gov implied IG)
    df["Is_IG"] = ~df["Is_Non_IG"]

    df.reset_index(drop=True, inplace=True)
    return df

def build_tags_from_meta(df: pd.DataFrame) -> dict:
    return {
        "is_tbill":  df["Is_Cash"].values.astype(bool),
        "is_at1":    df["Is_AT1"].values.astype(bool),
        "is_t2":     df["Is_T2"].values.astype(bool),
        "is_hybrid": df["Is_Hybrid"].values.astype(bool),
        "is_em":     df["Is_EM"].values.astype(bool),
        "is_non_ig": df["Is_Non_IG"].values.astype(bool),
        "is_ig":     df["Is_IG"].values.astype(bool),
        # HY bucket for spreads = non‑IG excluding AT1/EM so they can be shocked with specific vectors
        "is_hy_rating": (df["Is_Non_IG"] & ~df["Is_AT1"] & ~df["Is_EM"]).values.astype(bool),
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
    # mu is in decimals; convert to percent for display
    er_dec = float(mu @ w_opt)
    er_pct = er_dec * 100.0
    yld = float(df["Yield_Hedged_Pct"].values @ w_opt)
    oad = float(df["OAD_Years"].values @ w_opt)

    metrics = {"status": "OPTIMAL", "obj": prob.value, "ExpRet_pct": er_pct, "Yield_pct": yld, "OAD_years": oad,
               "VaR99_1M": var99, "CVaR99_1M": cvar99, "weights": w_opt}
    return w_opt, metrics

# -----------------------------
# 4) Visuals
# -----------------------------

def fmt_pct(x, digits=2):
    return f"{x*100:.{digits}f}%"

def fmt_pp(x, digits=2):
    return f"{x:.{digits}f}%"

def style_dataframe_percent(df, pct_cols, digits=2):
    df = df.copy()
    for c in pct_cols:
        df[c] = (df[c] * 100).round(digits)
    return df

def fmt_weight(series, digits=2):
    """Convert weights (decimal) to percentage values for display.
    Accepts numpy arrays, pandas Series, or lists.
    """
    return (np.asarray(series) * 100).round(digits)

def kpi_number(value: float, kind: str = "pct"):
    # kind: "pct" (value is decimal), "pp" (value is already percent value)
    val = value * 100 if kind == "pct" else value
    suffix = "%"
    fig = go.Figure(go.Indicator(mode="number", value=val, number={"suffix": suffix, "valueformat": ".2f"}))
    fig.update_layout(template="rubrics", margin=dict(l=5, r=5, t=6, b=6), height=110, showlegend=False, title={"text": ""})
    return fig

def bar_allocation(df, weights, title):
    ser = pd.Series(weights, index=df["Name"]).sort_values(ascending=False)
    fig = go.Figure(go.Bar(x=ser.index, y=ser.values))
    fig.update_layout(xaxis_title="Segment", yaxis_title="Weight", height=380, margin=dict(l=10,r=10,t=40,b=80))
    return apply_theme(fig)

def exposures_vs_budgets(df, weights, budgets: dict, title: str):
    """Overlay bars: grey = cap, blue = used (abs for KRD/Twist). Avoid hlines to make mapping clear."""
    is_ig_mask = build_tags_from_meta(df)["is_ig"]
    oasd = df["OASD_Years"].values

    used_vals = [
        abs(float(df["KRD_10y"].values @ weights)),
        abs(float((df["KRD_30y"].values - df["KRD_2y"].values) @ weights)),
        float(np.sum(oasd * weights * is_ig_mask)),
        float(np.sum(oasd * weights * (~is_ig_mask))),
    ]
    cap_vals = [
        budgets.get("limit_krd10y", 0.75),
        budgets.get("limit_twist", 0.40),
        budgets.get("limit_sdv01_ig", 3.0),
        budgets.get("limit_sdv01_hy", 1.5),
    ]
    labels = ["KRD 10y", "Twist (30y–2y)", "sDV01 IG", "sDV01 HY"]

    fig = go.Figure()
    fig.add_bar(name="Cap", x=labels, y=cap_vals, marker_color=RB_COLORS["grey"])
    fig.add_bar(name="Used", x=labels, y=used_vals, marker_color=RB_COLORS["blue"])
    fig.update_traces(opacity=0.35, selector=dict(name="Cap"))
    fig.update_layout(barmode="overlay", height=300, margin=dict(l=10,r=10,t=40,b=20), yaxis_title="Years")

    # Over-cap annotations per column
    for x_lbl, u, c in zip(labels, used_vals, cap_vals):
        if u > c:
            fig.add_annotation(x=x_lbl, y=u, text=f"Over by {u-c:.2f}", showarrow=False,
                               font=dict(color=RB_COLORS["orange"]))
    return fig

def scenario_histogram(port_pnl, title="Scenario P&L (1M)"):
    fig = go.Figure(data=[go.Histogram(x=port_pnl * 100, nbinsx=40)])
    var99, cvar99 = var_cvar_from_pnl(port_pnl, 0.99)
    fig.add_vline(x=-var99 * 100, line_dash="dash", annotation_text="VaR99", annotation_position="top left")
    fig.add_vline(x=-cvar99 * 100, line_dash="dot", annotation_text="CVaR99", annotation_position="top left")
    fig.update_layout(xaxis_title="% P&L", yaxis_title="Count", height=300, margin=dict(l=10,r=10,t=40,b=20))
    return apply_theme(fig)

def contributions_table(df, weights, mu):
    contr = pd.DataFrame({
        "Segment": df["Name"],
        "Weight": weights,
        # mu is decimal; show percent contribution in the table
        "ER_Contribution_pct": weights * (mu * 100.0),
        "Yield_pct": df["Yield_Hedged_Pct"].values,
        "RollDown_pct": df["Roll_Down_bps_1Y"].values/100.0,
        "OAD_Years": df["OAD_Years"].values,
        "OASD_Years": df["OASD_Years"].values
    }).sort_values("Weight", ascending=False)
    return contr

def heatmap_funds_losses(fund_results: dict):
    percs = [1,5,10,25,50,75,90,95,99]
    rows, labels = [], []
    for f,(res, pnl_assets) in fund_results.items():
        w = res["weights"]
        pnl_port = pnl_assets @ w
        rows.append([np.percentile(pnl_port, p) for p in percs])
        labels.append(f)
    fig = go.Figure(go.Heatmap(z=np.array(rows), x=[f"P{p}" for p in percs], y=labels, colorscale="RdBu", zmid=0))
    fig.update_layout(height=300, margin=dict(l=10,r=10,t=40,b=20))
    return fig

# --- Prospectus cap usage helpers -------------------------------------------

def calc_cap_usage(weights: np.ndarray, tags: dict, fund_caps: dict) -> dict:
    """
    Returns {cap_key: {'label': str, 'used': float, 'cap': float}} with values as decimals (0..1).
    """
    name_map = {
        "max_non_ig": "Non‑IG",
        "max_em": "EM",
        "max_hybrid": "Hybrid",
        "max_cash": "Cash",
        "max_at1": "AT1",
    }
    mask_map = {
        "max_non_ig": tags["is_non_ig"],
        "max_em":     tags["is_em"],
        "max_hybrid": tags.get("is_hybrid", np.zeros_like(weights, dtype=bool)),
        "max_cash":   tags["is_tbill"],
        "max_at1":    tags["is_at1"],
    }
    out = {}
    for k, cap in fund_caps.items():
        if k in mask_map:
            used = float(mask_map[k].astype(float) @ weights)  # portfolio weight in that sleeve
            out[k] = {"label": name_map[k], "used": used, "cap": float(cap)}
    return out


def cap_usage_chart(usage: dict) -> go.Figure:
    """
    Horizontal bullet-style bars: grey = cap, blue = used. Annotates overages.
    """
    labels = [v["label"] for v in usage.values()]
    used   = [v["used"] * 100 for v in usage.values()]
    caps   = [v["cap"]  * 100 for v in usage.values()]
    x_max = max([*(used or [0]), *(caps or [0])], default=0) * 1.15 or 1.0

    fig = go.Figure()
    # Cap (background)
    fig.add_bar(
        y=labels, x=caps, orientation="h", name="Cap",
        marker=dict(color=RB_COLORS["grey"]), hovertemplate="%{x:.2f}% cap"
    )
    # Used
    fig.add_bar(
        y=labels, x=used, orientation="h", name="Used",
        marker=dict(color=RB_COLORS["blue"]), hovertemplate="%{x:.2f}% used"
    )
    fig.update_traces(opacity=0.35, selector=dict(name="Cap"))
    fig.update_layout(
        barmode="overlay",
        height=220,
        margin=dict(l=10, r=10, t=40, b=20),
        xaxis_title="% of NAV",
        xaxis=dict(range=[0, x_max])
    )
    # Over-cap annotations
    for i, (u, c) in enumerate(zip(used, caps)):
        if c == 0 and u > 0:
            fig.add_annotation(y=labels[i], x=u, text="Cap = 0%", showarrow=False,
                               font=dict(color=RB_COLORS["orange"]))
        elif u > c:
            fig.add_annotation(y=labels[i], x=u, text=f"Over by {u-c:.2f}%", showarrow=False,
                               font=dict(color=RB_COLORS["orange"]))
    return fig

# --- Prospectus cap usage visuals -------------------------------------------

def render_cap_usage(df, tags, weights, fund):
    caps = FUND_CONSTRAINTS.get(fund, {})
    used = {
        "Non‑IG": float(tags["is_non_ig"].astype(float) @ weights),
        "EM":     float(tags["is_em"].astype(float)     @ weights),
        "Hybrid": float(tags["is_hybrid"].astype(float) @ weights),
        "AT1":    float(tags["is_at1"].astype(float)    @ weights),
        "Cash":   float(tags["is_tbill"].astype(float)  @ weights),
    }
    # Build a small table and a horizontal bar viz (used vs cap)
    rows = []
    for k, u in used.items():
        cap = caps.get(f"max_{k.lower()}", None)  # keys: max_non_ig, max_em, max_hybrid, max_at1, max_cash
        if cap is None: 
            continue
        rows.append({"Cap": k, "Used_pct": u*100, "Cap_pct": cap*100, "Headroom_pct": (cap-u)*100})
    tbl = pd.DataFrame(rows)
    if len(tbl) == 0:
        return

    st.markdown("**Prospectus cap usage (weights)**")
    st.dataframe(tbl.style.format({"Used_pct":"{:.2f}%","Cap_pct":"{:.2f}%","Headroom_pct":"{:.2f}%"}), use_container_width=True, height=180)

    fig = go.Figure()
    for _, r in tbl.iterrows():
        fig.add_bar(
            y=[r["Cap"]], x=[max(r["Used_pct"],0)], orientation="h",
            name="Used", marker_color=RB_COLORS["medblue"]
        )
        fig.add_bar(
            y=[r["Cap"]], x=[max(r["Cap_pct"] - max(r["Used_pct"],0),0)], orientation="h",
            name="Headroom", marker_color=RB_COLORS["ltblue"]
        )
    fig.update_layout(barmode="stack", height=220, margin=dict(l=10,r=10,t=30,b=10), xaxis_title="% of NAV", yaxis_title="")
    st.plotly_chart(fig, use_container_width=True, config=plotly_default_config)

def cap_usage_gauge(label: str, used_w: float, cap_w: float) -> go.Figure:
    """
    Gauge showing the portfolio weight used (as %) against the cap (as %).
    - used_w and cap_w are decimals (0..1)
    """
    used_pct = max(0.0, used_w * 100.0)
    cap_pct  = max(0.0, cap_w * 100.0)

    # Keep axis wide enough to show a breach comfortably
    axis_max = max(cap_pct if cap_pct > 0 else 1.0, used_pct * 1.10, 1.0)

    fig = go.Figure(
        go.Indicator(
            mode="gauge+number+delta",
            value=used_pct,
            number={"suffix": "%", "valueformat": ".2f"},
            delta={"reference": cap_pct,
                   "increasing": {"color": RB_COLORS["orange"]},
                   "decreasing": {"color": RB_COLORS["blue"]}},
            gauge={
                "axis": {"range": [0, axis_max]},
                "bar": {"color": RB_COLORS["medblue"]},
                "threshold": {"line": {"color": RB_COLORS["orange"], "width": 3}, "value": cap_pct},
                # Light grey background up to the cap
                "steps": [{"range": [0, cap_pct], "color": RB_COLORS["grey"]}]
            },
            title={"text": label}
        )
    )
    fig.update_layout(template="rubrics", height=150, margin=dict(l=4, r=4, t=30, b=4))
    return fig


def render_cap_usage_section(fund: str, w: np.ndarray, tags: dict, fc_current: dict):
    """
    Build gauges + a small table showing how much of each cap is used.
    fc_current contains the effective caps (from the sliders in the Fund page).
    """
    # Helper to compute weight in each sleeve
    def _w(mask: np.ndarray) -> float:
        return float(mask.astype(float) @ w)

    rows = []

    if "max_non_ig" in fc_current:
        rows.append(("Non‑IG", _w(tags["is_non_ig"]), float(fc_current["max_non_ig"])))
    if "max_em" in fc_current:
        rows.append(("EM", _w(tags["is_em"]), float(fc_current["max_em"])))
    if "max_hybrid" in fc_current:
        rows.append(("Hybrid (Global Hybrid)", _w(tags["is_hybrid"]), float(fc_current["max_hybrid"])))
    if "max_at1" in fc_current:
        rows.append(("AT1 (Bank Capital)", _w(tags["is_at1"]), float(fc_current["max_at1"])))
    if "max_cash" in fc_current:
        rows.append(("Cash (T‑Bills)", _w(tags["is_tbill"]), float(fc_current["max_cash"])))

    # Gauges
    n = len(rows)
    if n:
        cols = st.columns(n)
        for col, (lbl, used, cap) in zip(cols, rows):
            with col:
                title_with_help(
                    lbl,
                    "Usage of prospectus cap. Gauge shows proposed portfolio weight (needle) versus the cap (orange marker). "
                    "Values above the marker indicate a breach."
                )
                st.plotly_chart(cap_usage_gauge(lbl, used, cap), use_container_width=True)

    # Table (percent formatting + status)
    if n:
        data = []
        for lbl, used, cap in rows:
            status = "over cap" if cap > 0 and used > cap else ("n/a" if cap == 0 else "within cap")
            usage = (used / cap) if cap > 0 else np.nan
            data.append({
                "Cap": lbl,
                "Used %": used * 100.0,
                "Cap %": cap * 100.0,
                "Usage of cap": usage,   # ratio
                "Status": status
            })
        df_caps = pd.DataFrame(data)
        sty = (df_caps
               .style
               .format({"Used %": "{:.2f}%", "Cap %": "{:.2f}%", "Usage of cap": "{:.0%}"})
               .apply(lambda s: ["background-color: #ffe6e0" if (v == "over cap") else "" for v in s], subset=["Status"]))
        st.dataframe(sty, use_container_width=True, height=200)

        # Friendly summary if anything breaches
        if any(r[1] > r[2] and r[2] > 0 for r in rows):
            st.warning("One or more limits are over cap. Loosen the offending cap(s) or adjust factor budgets/turnover to find a feasible solution.")

# -----------------------------
# 5) App UI
# -----------------------------

st.title("Rubrics Fixed Income Optimiser")
st.caption("Forward‑looking allocation using carry + roll expected returns, KRD/sDV01 factor risk, Monte‑Carlo VaR, and fund‑specific prospectus caps.")
spacer(1)

with st.expander("How this optimiser works & what the controls do", expanded=False):
    st.markdown(
        """
- **Objective**: chooses the optimisation target (e.g., *Max Return*, *Max Sharpe*).
- **Expected return (%)**: carry + 1‑year roll‑down.
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
with st.expander("Data source (Excel) • sheets: \"Optimiser_Input\" and \"MetaData\". Join key: Bloomberg_Ticker. Required fields: Bloomberg_Ticker, Name, Instrument_Type, Yield_Hedged_Pct, Roll_Down_bps_1Y, OAD_Years, OASD_Years, KRD_2y/5y/10y/30y, Include.", expanded=False):
    upload = st.file_uploader("Upload Excel file with Optimiser_Input and MetaData sheets", type=["xlsx"], accept_multiple_files=False)
    st.write("If no file is uploaded, the app will try to read:", f"`{DEFAULT_INPUT_FILE}`")

# Load data
try:
    df = load_joined_input(upload.getvalue() if upload is not None else None, DEFAULT_INPUT_FILE)
except Exception as e:
    st.error(f"Failed to load input: {e}")
    st.stop()

if len(df) == 0:
    st.error("No rows found after applying Include==True. Please check the input file.")
    st.stop()

tags = build_tags_from_meta(df)

# Controls (global)
with st.sidebar:
    st.header("Global Settings")
    seed = st.number_input(
        "Random seed", min_value=0, value=42, step=1,
        help="Sets the random-number seed so results are reproducible." +
             impact_text("changes the exact scenarios drawn (numbers may nudge)",
                         "does not change the underlying distribution")
    )
    n_draws = st.number_input(
        "Monte Carlo draws (monthly)", min_value=200, max_value=10000, value=2000, step=100,
        help="Number of one‑month scenarios used to estimate VaR/CVaR. " +
             impact_text("smoother/steadier risk estimates but slower",
                         "faster runs but risk estimates are noisier")
    )
    st.write("Rate shocks (bp @99%):")
    c1, c2, c3, c4 = st.columns(4)
    with c1: RATES_BP99["2y"] = st.number_input("2y", value=float(RATES_BP99["2y"]),
        help="99th‑percentile monthly move in 2y yields (bp). " +
             impact_text("raises tail-rate risk & typically raises VaR",
                         "reduces tail-rate risk & typically lowers VaR"))
    with c2: RATES_BP99["5y"] = st.number_input("5y", value=float(RATES_BP99["5y"]),
        help="99th‑percentile monthly move in 5y yields (bp). " +
             impact_text("raises tail-rate risk & typically raises VaR",
                         "reduces tail-rate risk & typically lowers VaR"))
    with c3: RATES_BP99["10y"] = st.number_input("10y", value=float(RATES_BP99["10y"]),
        help="99th‑percentile monthly move in 10y yields (bp). " +
             impact_text("raises tail-rate risk & typically raises VaR",
                         "reduces tail-rate risk & typically lowers VaR"))
    with c4: RATES_BP99["30y"] = st.number_input("30y", value=float(RATES_BP99["30y"]),
        help="99th‑percentile monthly move in 30y yields (bp). " +
             impact_text("raises tail-rate risk & typically raises VaR",
                         "reduces tail-rate risk & typically lowers VaR"))

    st.write("Spread widenings (bp @99%):")
    c1, c2, c3, c4 = st.columns(4)
    with c1: SPREAD_BP99["IG"]  = st.number_input("IG", value=float(SPREAD_BP99["IG"]),
        help="99th‑percentile monthly credit spread widening (bp) for IG assets. " +
             impact_text("raises credit VaR/CVaR","reduces credit VaR/CVaR"))
    with c2: SPREAD_BP99["HY"]  = st.number_input("HY", value=float(SPREAD_BP99["HY"]),
        help="99th‑percentile monthly credit spread widening (bp) for HY assets. " +
             impact_text("raises credit VaR/CVaR","reduces credit VaR/CVaR"))
    with c3: SPREAD_BP99["AT1"] = st.number_input("AT1", value=float(SPREAD_BP99["AT1"]),
        help="99th‑percentile monthly widening (bp) for Bank AT1. " +
             impact_text("raises credit VaR/CVaR","reduces credit VaR/CVaR"))
    with c4: SPREAD_BP99["EM"]  = st.number_input("EM", value=float(SPREAD_BP99["EM"]),
        help="99th‑percentile monthly widening (bp) for EM hard-currency. " +
             impact_text("raises credit VaR/CVaR","reduces credit VaR/CVaR"))

    st.divider()
    st.subheader("Default Factor Budgets")
    limit_krd10y = st.number_input(
        "|KRD 10y| cap (yrs)", value=FACTOR_BUDGETS_DEFAULT["limit_krd10y"], step=0.05, format="%.2f",
        help="Budget for 10‑year rate exposure (duration years). " +
             impact_text("allows more 10y rate risk; potential for higher return and higher rate VaR",
                         "limits 10y rate risk; can reduce expected return and lower rate VaR")
    )
    limit_twist  = st.number_input(
        "Twist (30y–2y) cap (yrs)", value=FACTOR_BUDGETS_DEFAULT["limit_twist"], step=0.05, format="%.2f",
        help="Steepener/Flattener exposure budget (|KRD30y−KRD2y|). " +
             impact_text("permits larger curve-shape bets",
                         "forces the portfolio toward curve neutrality")
    )
    limit_sdv01_ig = st.number_input(
        "sDV01 IG cap (yrs)", value=FACTOR_BUDGETS_DEFAULT["limit_sdv01_ig"], step=0.1, format="%.1f",
        help="Credit spread duration budget for IG sleeves. " +
             impact_text("permits more IG spread exposure; may lift carry & credit VaR",
                         "reduces IG spread exposure; may dampen carry and lower credit VaR")
    )
    limit_sdv01_hy = st.number_input(
        "sDV01 HY cap (yrs)", value=FACTOR_BUDGETS_DEFAULT["limit_sdv01_hy"], step=0.1, format="%.1f",
        help="Credit spread duration budget for Non‑IG sleeves (HY/EM/AT1). " +
             impact_text("permits more HY/EM/AT1 spread exposure; higher carry and higher tail risk",
                         "limits HY/EM/AT1 risk; typically lowers VaR but may cut expected return")
    )

    st.divider()
    st.subheader("Turnover")
    penalty_bps = st.number_input(
        "Penalty (bps per 100% turnover)", value=TURNOVER_DEFAULTS["penalty_bps_per_100"], step=1.0,
        help="Transaction/friction penalty applied to weight changes. " +
             impact_text("discourages large reallocations; often lowers expected return but stabilises weights",
                         "allows larger, faster reallocations; may increase expected return and turnover")
    )
    max_turn = st.slider(
        "Max turnover per rebalance", 0.0, 1.0, TURNOVER_DEFAULTS["max_turnover"], 0.01,
        help="Hard cap on total absolute change in weights per rebalance. " +
             impact_text("permits bigger positioning changes; might improve objective but can raise costs",
                         "restricts rebalancing; can make problems infeasible if too low")
    )

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

    st.divider()
    if st.button("Reset all controls to defaults"):
        st.experimental_rerun()

    # Quick debug expander
    with st.expander("Data sanity (debug)", expanded=False):
        st.write("Rows:", len(df))
        for k in ["Is_Non_IG","Is_EM","Is_AT1","Is_T2","Is_Hybrid","Is_Cash","Is_IG"]:
            if k in df.columns:
                st.write(f"{k} = True:", int(df[k].sum()))

    st.subheader("Display options")
    min_weight_display = st.slider(
        "Hide weights below", 0.0, 0.01, 0.001, 0.0005, format="%.3f",
        help="Visual threshold for the allocation chart/table. " +
             impact_text("hides more small positions to declutter",
                         "reveals smaller positions")
    )

    with st.expander("Presets", expanded=False):
        if st.button("Save current settings"):
            preset = dict(
                rates=dict(RATES_BP99),
                spreads=dict(SPREAD_BP99),
                fb=dict(limit_krd10y=limit_krd10y, limit_twist=limit_twist, limit_sdv01_ig=limit_sdv01_ig, limit_sdv01_hy=limit_sdv01_hy),
                turnover=dict(penalty_bps=penalty_bps, max_turnover=max_turn)
            )
            st.download_button("Download preset.json", data=json.dumps(preset).encode(), file_name="preset.json", mime="application/json")
        preset_file = st.file_uploader("Load preset.json", type=["json"], key="preset_json")
        if preset_file:
            p = json.loads(preset_file.read())
            # Apply minimal set (rates/spreads) then suggest rerun for others
            for k,v in p.get('rates', {}).items():
                RATES_BP99[k] = v
            for k,v in p.get('spreads', {}).items():
                SPREAD_BP99[k] = v
            st.info("Preset loaded. Adjust budgets/turnover as needed, then rerun.")

# Prepare scenarios
mc = simulate_mc_draws(int(n_draws), int(seed), dict(RATES_BP99), dict(SPREAD_BP99))
pnl_matrix_assets = build_asset_pnl_matrix(df, tags, mc)  # S x N
# Expected return vector: convert percent to decimals for optimisation, keep percent for display
mu_percent = df["ExpRet_pct"].values.astype(float)
mu = mu_percent / 100.0

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
    objective = st.selectbox(
        "Objective",
        ["Max Return","Max Sharpe","Min VaR for Target Return","Max Drawdown Proxy"],
        index=0, key="overview_obj",
        help=("Choose the optimiser target. Max Return ignores risk beyond hard caps; "
              "Max Sharpe penalises CVaR; Min VaR meets a target return; Drawdown proxy minimises CVaR. ") +
             impact_text("shifts toward risk-taking and tends to raise expected return",
                         "tightens risk; expected return may fall")
    )

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
                title_with_help(
                    f"{f} – Expected Return",
                    "Annualised carry + 1‑year roll‑down (%). Higher is better for return."
                )
                st.plotly_chart(kpi_number(m["ExpRet_pct"], kind="pp"), use_container_width=True, config=plotly_default_config, key=f"kpi_{f}_er")

                title_with_help(
                    f"{f} – VaR99 1M",
                    "One‑month, 99% Value at Risk (loss). Lower is better. Compared against the fund’s VaR cap."
                )
                st.plotly_chart(kpi_number(m["VaR99_1M"], kind="pct"), use_container_width=True, config=plotly_default_config, key=f"kpi_{f}_var")

                cap = VAR99_CAP[f]
                status = "within cap" if m["VaR99_1M"] <= cap else "over cap"
                st.caption(f"VaR cap {cap*100:.2f}% — {status}")
            idx += 1
    # Aggregate (configurable)
    agg_mode = st.radio(
        "Aggregate weighting:", ["Equal-weight", "By expected return (proxy AUM)"], horizontal=True,
        help=("How to combine GFI/GCF/EYF into an aggregate. Equal-weight treats funds the same; "
              "'By expected return' tilts toward funds with higher ER.") +
             impact_text("tilts more to high-ER fund(s)", "gives each fund equal say")
    )
    if len(fund_outputs) > 0:
        Ws = np.vstack([fund_outputs[f][0]["weights"] for f in fund_outputs])
        if agg_mode == "Equal-weight":
            agg_weights = Ws.mean(axis=0)
        else:
            ers = np.array([fund_outputs[f][0]["ExpRet_pct"] for f in fund_outputs])  # percent values
            w_funds = ers / ers.sum() if ers.sum() != 0 else np.ones_like(ers) / len(ers)
            agg_weights = (w_funds.reshape(-1,1) * Ws).sum(axis=0)
        port_pnl_agg = pnl_matrix_assets @ agg_weights
        var99_agg, cvar99_agg = var_cvar_from_pnl(port_pnl_agg, 0.99)
        er_agg_pct = float(mu @ agg_weights) * 100.0
        with cols[3]:
            title_with_help("Aggregate – Expected Return", "Expected return of the equal/ER‑weighted aggregate (%).")
            st.plotly_chart(kpi_number(er_agg_pct, kind="pp"), use_container_width=True, config=plotly_default_config, key="kpi_agg_er")

            title_with_help("Aggregate – VaR99 1M", "One‑month, 99% VaR for the aggregate. Lower is better.")
            st.plotly_chart(kpi_number(var99_agg, kind="pct"), use_container_width=True, config=plotly_default_config, key="kpi_agg_var")

    spacer(1)
    # Allocation by segment (stacked bars)
    title_with_help(
        "Allocation by segment (GFI / GCF / EYF / Aggregate)",
        "Weights per sleeve. Use the factor budgets, VaR caps and prospectus caps to steer these weights."
    )
    alloc_df = pd.DataFrame(index=df["Name"])
    for f in ["GFI","GCF","EYF"]:
        if f in fund_outputs:
            alloc_df[f] = fund_outputs[f][0]["weights"]
    if agg_weights is not None:
        alloc_df["Aggregate"] = agg_weights
    alloc_df = alloc_df.fillna(0.0)
    alloc_df = alloc_df.mask(alloc_df.abs() < min_weight_display, other=0.0)
    alloc_df_display = alloc_df.apply(lambda col: pd.Series(fmt_weight(col), index=alloc_df.index))
    st.dataframe(alloc_df_display.sort_index().style.format("{:.2f}%"), use_container_width=True, height=260)
    fig_alloc = go.Figure()
    for col in alloc_df.columns:
        y = alloc_df[col].values
        y = np.where(np.abs(y) < min_weight_display, 0.0, y)
        color = FUND_COLOR.get(col, None)
        fig_alloc.add_bar(name=col, x=alloc_df.index, y=y, marker_color=color)
    fig_alloc.update_layout(barmode="group", height=380, margin=dict(l=10,r=10,t=40,b=80), xaxis_title="Segment", yaxis_title="Weight")
    st.plotly_chart(fig_alloc, use_container_width=True)

    spacer(1)
    # Factor exposures vs budgets (show KRD10, Twist, sDV01 IG/HY)
    title_with_help(
        "Factor exposures vs budgets (KRD & sDV01)",
        "Portfolio sensitivities in years with dotted lines marking budgets: |KRD 10y|, Twist(30y–2y), sDV01 IG/HY. Bars near the dotted lines mean the budget is binding."
    )
    fig_fb = go.Figure()
    for f in ["GFI","GCF","EYF"]:
        if f in fund_outputs:
            w = fund_outputs[f][0]["weights"]
            is_ig_mask = build_tags_from_meta(df)["is_ig"]
            oasd = df["OASD_Years"].values
            vals = {
                "KRD 10y": float(df["KRD_10y"].values @ w),
                "Twist (30y–2y)": float((df["KRD_30y"].values - df["KRD_2y"].values) @ w),
                "sDV01 IG": float(np.sum(oasd * w * is_ig_mask)),
                "sDV01 HY": float(np.sum(oasd * w * (~is_ig_mask))),
            }
            for k,v in vals.items():
                fig_fb.add_bar(name=f" {f} {k}", x=[k], y=[v])
    fig_fb.update_layout(barmode="group", height=300, margin=dict(l=10,r=10,t=40,b=20))
    st.plotly_chart(fig_fb, use_container_width=True)

    spacer(1)
    # Scenario percentiles heatmap
    if len(fund_outputs) > 0:
        title_with_help(
            "Scenario Distribution by Fund (Portfolio P&L percentiles)",
            "Each cell shows the portfolio %P&L percentile under monthly scenarios (P1…P99). Warmer colours to the right indicate stronger upside; cooler to the left indicate downside."
        )
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
        _fc_backup = {k:v for k,v in FUND_CONSTRAINTS[fund].items()}
        try:
            FUND_CONSTRAINTS[fund] = {"max_non_ig": max_non_ig, "max_em": max_em, "max_cash": max_cash, "max_at1": max_at1}
            if "max_hybrid" in fc:
                FUND_CONSTRAINTS[fund]["max_hybrid"] = max_hybrid
        finally:
            pass

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
        with cols[0]:
            title_with_help("Expected Return (ann.)", "Annualised carry + roll‑down (%).")
            st.plotly_chart(kpi_number(metrics["ExpRet_pct"], kind="pp"), use_container_width=True, config=plotly_default_config, key=f"kpi_{fund}_detail_er")
        with cols[1]:
            title_with_help("VaR99 1M", "One‑month, 99% Value at Risk (loss). Lower is better.")
            st.plotly_chart(kpi_number(var99, kind="pct"), use_container_width=True, config=plotly_default_config, key=f"kpi_{fund}_detail_var")
        with cols[2]:
            title_with_help("CVaR99 1M", "Average loss in the worst 1% of scenarios. Lower is better.")
            st.plotly_chart(kpi_number(cvar99, kind="pct"), use_container_width=True, config=plotly_default_config, key=f"kpi_{fund}_detail_cvar")
        with cols[3]:
            title_with_help("Portfolio Yield", "Yield component of return (%).")
            st.plotly_chart(kpi_number(metrics["Yield_pct"], kind="pp"), use_container_width=True, config=plotly_default_config, key=f"kpi_{fund}_detail_yield")

        cap = var_cap
        status = "within cap" if var99 <= cap else "over cap"
        st.caption(f"VaR99 1M: {var99*100:.2f}% (cap {cap*100:.2f}%) {status}")

        # Prospectus cap usage panel
        render_cap_usage(df, tags, w, fund)

        # Allocation
        title_with_help(f"{fund} – Allocation by Segment", "Weights per sleeve after optimisation under the current caps and budgets.")
        st.plotly_chart(bar_allocation(df, w, f"{fund} – Allocation by Segment"), use_container_width=True)

        # Exposures vs budgets
        title_with_help(f"{fund} – Factor Exposures vs Budgets", "KRD10y, Twist(30y–2y), and sDV01 IG/HY vs their budgets (dotted).")
        st.plotly_chart(exposures_vs_budgets(df, w, fb_over, f"{fund} – Factor Exposures vs Budgets"), use_container_width=True)

        # Scenario distribution
        title_with_help(f"{fund} – Scenario P&L Distribution", "Monthly %P&L distribution from Monte Carlo. Vertical lines show VaR99 and CVaR99.")
        st.plotly_chart(scenario_histogram(port_pnl, f"{fund} – Scenario P&L Distribution"), use_container_width=True)

        # Contributions table
        title_with_help("Segment contributions table", "Weights, expected return contribution (%), yield & roll‑down, and duration metrics per segment.")
        contr = contributions_table(df, w, mu)
        st.dataframe(contr, use_container_width=True, height=360)

        # Download
        out_csv = contr[["Segment","Weight","ER_Contribution_pct"]].to_csv(index=False).encode("utf-8")
        st.download_button("Download weights & ER contributions (CSV)", out_csv, file_name=f"{fund}_allocation.csv", mime="text/csv")

st.caption("© Rubrics – internal research tool. Forward-looking estimates; not investment advice.")
