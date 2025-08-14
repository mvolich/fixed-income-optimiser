# Rubrics Fixed Income Optimiser (Streamlit)

Production‑ready Streamlit app for fixed‑income portfolio construction. It allocates across index sleeves using **forward‑looking expected returns** (carry + roll‑down) and **factor/tail risk** (KRD & sDV01 with monthly 99% VaR/CVaR), enforcing **fund‑specific prospectus caps** (Non‑IG, EM, AT1, Hybrid, Cash).

## Features
- **Forward‑looking E[r]**: `Yield_Hedged_Pct + Roll_Down_bps_1Y/100` (per segment).
- **Risk in bond language**: KRD(2/5/10/30) & sDV01; scenario P&L; VaR99 & CVaR99 (1‑month).
- **Optimiser**: convex program (cvxpy/OSQP) with objectives (Max Return, Max Sharpe, Min VaR at target return).
- **Prospectus caps**: per fund (GFI/GCF/EYF) for Non‑IG, EM, AT1, Hybrid, Cash.
- **Enterprise UX**: Overview (compare funds & aggregate), Fund Detail (tune with sliders), export CSVs.
- **Configurable**: sliders for VaR, factor budgets, caps, turnover penalty & limit, scenario shocks.

## Repo structure
```
.
├── app.py                          # Streamlit app (single-file)
├── requirements.txt
├── README.md
├── LICENSE
├── .gitignore
├── .streamlit/
│   └── config.toml                 # theme & server settings
├── sample_data/
│   ├── Optimiser_Input_Sample.csv  # minimal sample input
│   └── Optimiser_Input_Sample.xlsx
├── configs/
│   └── fund_policies.yml           # optional external policy (future use)
├── tests/
│   └── test_core.py                # unit tests on core functions
├── docs/
│   └── wireframes/
│       ├── wireframe_overview_v2.png
│       └── wireframe_fund_detail_v2.png
├── Dockerfile
├── docker-compose.yml
├── .pre-commit-config.yaml
└── .github/
    └── workflows/
        └── ci.yml
```

## Quickstart
### Local
```bash
python -m venv .venv
source .venv/bin/activate   # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt
streamlit run app.py
```
> Place your **Optimiser_Input_Final_v3.xlsx** in the repo root (or upload it via the sidebar).

### Docker
```bash
docker build -t rubrics-optimiser .
docker run --rm -p 8501:8501 -v ${PWD}:/app rubrics-optimiser
# open http://localhost:8501
```

## Input data specification (sheet `Optimiser_Input`)
Required columns (one row per segment):
```
Segment_ID, Name, Bloomberg_Ticker, Instrument_Type, Credit_Quality, Maturity_Bucket,
Rates_Currency, Sector, Yield_Hedged_To, Rates_Hedge_Convention,
Yield_Local_Pct, FX_Hedge_Cost_bps, Yield_Hedged_Pct, OAS_bps, Z_Spread_bps, Roll_Down_bps_1Y,
OAD_Years, OASD_Years, Convexity, KRD_2y, KRD_5y, KRD_10y, KRD_30y, Include
```

## Fund policy (built-in, adjustable in UI)
- **VaR99 (1M)** caps: GFI 5.0%, GCF 5.5%, EYF 10.0%.
- **Prospectus caps**: Non‑IG, EM, AT1, Hybrid, Cash per fund.
- **Factor budgets**: |KRD10y|, |KRD30y–KRD2y| (twist), IG/HY sDV01.
- **Turnover**: penalty (bps/100%) and max turnover per rebalance.

## Tests
```bash
pytest -q
```
Tests exercise tagging, scenarios, P&L assembly, VaR/CVaR, and a small optimisation run (skips if cvxpy unavailable).

## CI
- **GitHub Actions** workflow (`.github/workflows/ci.yml`) runs lint (ruff) and tests on Python 3.11.

## Screens / Wireframes
![Overview](docs/wireframes/wireframe_overview_v2.png)
![Fund Detail](docs/wireframes/wireframe_fund_detail_v2.png)

## Notes
- The app is single‑file for easy Cursor editing. As it matures, extract core logic into a `core/` module and add YAML policy loading from `configs/fund_policies.yml`.
- VaR uses a factor shock engine at a monthly horizon; you can tighten/loosen shocks and caps in the UI.
