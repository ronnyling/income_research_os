import json
ckpt = json.load(open(".funnel_checkpoint.json"))

# Check screen results for key tickers
screen = ckpt.get("screen_results", {})
for ticker in ["MCD", "ATO", "ACN", "MSFT", "NKE", "ICE"]:
    d = screen.get(ticker, {})
    checks = d.get("checks", {})
    notes = d.get("notes", [])
    print(f"\n=== {ticker} ===")
    print(f"  passed: {d.get('passed')}")
    print(f"  checks: {checks}")
    for n in notes[:5]:
        print(f"  note: {n}")

# Check MiMo results for all stocks
print("\n\n=== ALL MiMo CLASSIFICATIONS ===")
mimo = ckpt.get("mimo_results", {})
for ticker, m in mimo.items():
    c = m.get("classification", "?")
    conf = m.get("confidence", 0)
    ev = m.get("evidence_summary", "")[:120]
    flags = m.get("structural_flags", [])
    print(f"  {ticker:6s}  {c:30s}  conf={conf:.2f}  flags={flags}")
    print(f"         evidence: {ev}")
