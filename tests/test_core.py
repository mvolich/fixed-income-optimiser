import math
import numpy as np
import pandas as pd
import importlib.util
import sys, os

# Import app.py as a module
spec = importlib.util.spec_from_file_location("app", os.path.join(os.path.dirname(__file__), "..", "app.py"))
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

def build_df():
    cols = [
        "Segment_ID","Name","Bloomberg_Ticker","Instrument_Type","Credit_Quality","Maturity_Bucket",
        "Rates_Currency","Sector","Yield_Hedged_To","Rates_Hedge_Convention",
        "Yield_Local_Pct","FX_Hedge_Cost_bps","Yield_Hedged_Pct","OAS_bps","Z_Spread_bps","Roll_Down_bps_1Y",
        "OAD_Years","OASD_Years","Convexity","KRD_2y","KRD_5y","KRD_10y","KRD_30y","Include"
    ]
    rows = [
        ["USD_TBILLS", "US T-Bills", "I31914US INDEX", "Cash", "Gov", "Cash", "USD", "Bills", "USD", "FXFwdOnly", 5.00, 0.00, 5.00, 0, 0, 0, 0.05, 0.00, 0.00, 0,0,0,0, True],
        ["USD_CORP_IG_3_5", "US Corp 3-5", "BUS3TRUU INDEX", "Corporate", "IG", "3-5", "USD", "Broad", "USD", "FXFwdOnly", 5.80, 0.00, 5.80, 95, 110, 20, 4.2, 3.8, 0.10, 0.2,0.9,2.8,0.3, True],
        ["USD_CORP_BB_ALL", "US Corp BB", "I00182US INDEX", "Corporate", "BB", "All", "USD", "Broad", "USD", "FXFwdOnly", 7.20, 0.00, 7.20, 260, 300, 10, 4.8, 4.5, 0.12, 0.1,0.6,3.5,0.6, True],
        ["BANK_AT1_GLOBAL", "Bank AT1", "H31415US INDEX", "BankCapital", "HY", "All", "MULTI", "Financials", "USD", "FXFwdOnly", 9.50, 0.00, 9.50, 450, 500, 15, 5.2, 5.0, 0.25, 0.0,0.4,3.0,1.0, True],
        ["EM_USD_AGG", "EM HC", "EMUSTRUU INDEX", "EM", "Mix", "All", "USD", "Sov+Corp", "USD", "FXFwdOnly", 7.80, 0.00, 7.80, 300, 320, 12, 6.8, 6.0, 0.18, 0.1,0.5,4.5,1.4, True],
        ["USD_TREAS_AGG", "US Agg Treasury", "LUATTRUU INDEX", "Sovereign", "Gov", "All", "USD", "Treasury", "USD", "FXFwdOnly", 4.40, 0.00, 4.40, 0, 0, 10, 6.5, 0.0, 0.02, 0.6,1.5,3.8,0.7, True],
    ]
    df = pd.DataFrame(rows, columns=cols)
    df["ExpRet_pct"] = df["Yield_Hedged_Pct"] + df["Roll_Down_bps_1Y"]/100.0
    return df

def test_tag_and_exposure_shapes():
    df = build_df()
    tags = app.tag_segments(df)
    E, mu = app.build_exposure_matrix(df)
    assert tags.shape[0] == df.shape[0]
    assert E.shape == (df.shape[0], 6)
    assert mu.shape == (df.shape[0],)

def test_scenarios_and_pnl():
    df = build_df()
    tags = app.tag_segments(df)
    E, mu = app.build_exposure_matrix(df)
    sc = app.default_scenarios()
    pnl = app.pnl_matrix_from_scenarios(E, df, tags, sc)
    assert pnl.shape[0] == sc.shape[0]
    assert pnl.shape[1] == df.shape[0]
    # Portfolio loss should respond sensibly to sign of shocks
    w = np.ones(df.shape[0]) / df.shape[0]
    var99, cvar99, port = app.var_cvar_from_pnl(w, pnl, alpha=0.99)
    assert var99 >= 0
    assert cvar99 >= var99

def test_optimiser_runs_if_cvxpy_present():
    try:
        import cvxpy as cp  # noqa
    except Exception:
        return  # skip if cvxpy missing
    df = build_df()
    tags = app.tag_segments(df)
    E, mu = app.build_exposure_matrix(df)
    pnl = app.pnl_matrix_from_scenarios(E, df, tags, app.default_scenarios())
    w, info = app.solve_portfolio(
        mu, E, pnl, df, tags, "GFI",
        {"max_non_ig":0.25,"max_em":0.30,"max_hybrid":0.15,"max_cash":0.20,"max_at1":0.15},
        {"limit_krd10y":0.75,"limit_twist":0.40,"limit_sdv01_ig":3.0,"limit_sdv01_hy":1.5},
        0.05, objective="Max Return", cvar_tightness=1.3, turnover={"w_prev":None,"lambda":0.0,"max_turnover":0.25}
    )
    assert w is not None
    assert abs(w.sum() - 1.0) < 1e-6
    assert (w >= -1e-8).all()
