"""Quick smoke test for the swing scanner."""
from application.services import swing_scanner as s

s.shared_cache.jdelete(s._CACHE_KEY)
r = s.scan(force_refresh=True)
print("strategy   :", r.get("strategy"))
print("universe   :", r.get("universe_size"), "scanned:", r.get("scanned"))
print("grade_count:", r.get("grade_counts"))
print()
print(f"{'GRADE':<11}{'SCORE':>6}  {'NAME':<14} {'ADR%':>5} {'3M%':>6} {'RS3M':>6}  "
      f"{'ENTRY':>8} {'STOP':>8} {'(S%)':>5}  {'TGT':>8} {'(T%)':>5}  {'RR':>4}")
for x in r["stocks"][:15]:
    print(f"{x['grade']:<11}{x['score']:>6}  {x['name']:<14} {x['adr_pct']:>5.1f} "
          f"{x['ret_3m']:>6.1f} {x['rs_3m_excess']:>+6.1f}  "
          f"{x['entry']:>8.2f} {x['stop']:>8.2f} {x['stop_pct']:>5.1f}  "
          f"{x['target']:>8.2f} {x['target_pct']:>5.1f}  {x['risk_reward']:>4.2f}")
