"""Quick scoring detail script — run from project root with PYTHONPATH=src."""
import sys
sys.path.insert(0, "src")

from incomos.data.edgar import EdgarClient
from incomos.data.market import get_price_snapshot
from incomos.data.fred import get_usd_myr_rate
from incomos.scoring.engine import score
from incomos.sizing import compute_position_size

edgar = EdgarClient()
usd_myr = get_usd_myr_rate()
print(f"USD/MYR: {usd_myr}")

for ticker in ["ABT", "MCD", "MDT"]:
    cik = edgar.resolve_cik(ticker)
    metrics = edgar.get_annual_metrics(ticker, cik, years=5)
    snap = get_price_snapshot(ticker)
    result = score(ticker, metrics, price_snap=snap)

    print(f"\n{'='*60}")
    print(f"TICKER: {ticker}")
    print(f"Price: ${snap.current_price:.2f}  52W High: ${snap.week52_high:.2f}  "
          f"Pct Below: {snap.pct_below_52w_high:.1%}  RSI: {snap.rsi_14:.1f}  "
          f"VolRatio: {snap.volume_ratio:.2f}x")
    print(f"\nIncome Quality:     {result.income_quality:.1f}/100")
    ib = result.income_breakdown
    print(f"  Dividend years:   {ib.get('dividend_continuity', {}).get('years','—')}/"
          f"{ib.get('dividend_continuity', {}).get('out_of','—')} "
          f"({ib.get('dividend_continuity', {}).get('pts','—')} pts)")
    print(f"  FCF payout ratio: {ib.get('fcf_payout_ratio', {}).get('ratio','—')}  "
          f"({ib.get('fcf_payout_ratio', {}).get('pts','—')} pts)")
    print(f"  DPS CAGR:         {ib.get('dividend_growth', {}).get('cagr','—')}  "
          f"({ib.get('dividend_growth', {}).get('pts','—')} pts)")
    print(f"  FCF positive yrs: {ib.get('fcf_consistency', {}).get('positive_years','—')}  "
          f"({ib.get('fcf_consistency', {}).get('pts','—')} pts)")

    print(f"\nBusiness Quality:   {result.business_quality:.1f}/100")
    bb = result.business_breakdown
    print(f"  Revenue CAGR:     {bb.get('revenue_cagr', {}).get('cagr','—')}  "
          f"({bb.get('revenue_cagr', {}).get('pts','—')} pts)")
    print(f"  FCF margin:       {bb.get('fcf_margin', {}).get('margin','—')}  "
          f"({bb.get('fcf_margin', {}).get('pts','—')} pts)")
    print(f"  FCF CAGR:         {bb.get('fcf_cagr', {}).get('cagr','—')}  "
          f"({bb.get('fcf_cagr', {}).get('pts','—')} pts)")
    print(f"  Net debt trend:   {bb.get('net_debt_trend', {}).get('trend','—')}  "
          f"({bb.get('net_debt_trend', {}).get('pts','—')} pts)")

    print(f"\nOversold Confidence:{result.oversold_confidence:.1f}/100")
    ob = result.oversold_breakdown
    print(f"  RSI:              {ob.get('rsi', {}).get('value','—')}  "
          f"({ob.get('rsi', {}).get('pts','—')} pts)")
    print(f"  Pct below 52W:    {ob.get('pct_below_52w', {}).get('value','—')}  "
          f"({ob.get('pct_below_52w', {}).get('pts','—')} pts)")
    print(f"  Volume ratio:     {ob.get('volume_ratio', {}).get('value','—')}  "
          f"({ob.get('volume_ratio', {}).get('pts','—')} pts)")

    print(f"\nDip Quality:        {result.dip_quality:.1f}/100 [STUB — MiMo 2.5 not configured]")
    print(f"\nCOMPOSITE SCORE:    {result.composite:.1f}/100  "
          f"(multiplier: {result.base_size_multiplier}x)  "
          f"{'[PARTIAL]' if result.is_partial else ''}")

    sz = compute_position_size(result, 500_000, exchange="US", usd_myr_rate=usd_myr)
    print(f"POSITION SIZE:      MYR {sz.adjusted_position_myr:,.0f}  "
          f"(USD {sz.position_usd:,.0f} at {usd_myr})")
