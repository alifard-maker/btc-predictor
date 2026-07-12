#!/bin/bash
set -euo pipefail
cd /Users/afard/Documents/btc-predictor
set -a; source .env; set +a
BASE="https://btc-predictor-production-f460.up.railway.app"
LOG="/Users/afard/Documents/btc-predictor/.monitor_5027.log"
RESULT="/Users/afard/Documents/btc-predictor/.monitor_5027_result.json"
COOKIE=$(mktemp)
RAILWAY_SCRIPTS_DONE=0
CONSEC=0
MAX_MIN=30
INTERVAL=120
START=$(date +%s)
END=$((START + MAX_MIN * 60))

login() {
  /usr/bin/curl -sS -c "$COOKIE" -b "$COOKIE" -L -X POST "$BASE/api/auth/login" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "password=${APP_PASSWORD}" -o /dev/null -w "%{http_code}"
}

fetch() {
  local path="$1"
  local code body
  body=$(/usr/bin/curl -sS -b "$COOKIE" -w "\n__HTTP__%{http_code}" "$BASE$path" 2>/dev/null || echo "__HTTP__000")
  code="${body##*__HTTP__}"
  body="${body%__HTTP__*}"
  echo "$code|$body"
}

run_railway_scripts() {
  echo "[$(date -Iseconds)] Running railway repair scripts..." | tee -a "$LOG"
  cd /Users/afard/Documents/btc-predictor
  /Users/afard/.railway/bin/railway ssh -- python3 /app/scripts/repair_v3_ranking.py 2>&1 | tee -a "$LOG" || echo "repair_v3_ranking FAILED" | tee -a "$LOG"
  /Users/afard/.railway/bin/railway ssh -- python3 /app/scripts/pnl_first_paper_ab_report.py 2>&1 | tee -a "$LOG" || echo "pnl_first_paper_ab_report FAILED" | tee -a "$LOG"
}

check_all() {
  local iter="$1"
  local -a results
  local all_ok=1
  local detail=""

  login_code=$(login)
  echo "[$(date -Iseconds)] iter=$iter login_http=$login_code" >> "$LOG"

  # 1 health version
  r=$(fetch "/health")
  code="${r%%|*}"; body="${r#*|}"
  ver=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('version',''))" 2>/dev/null || echo "")
  v_ok=0; [[ "$ver" == "Beta 5.0.27" ]] && v_ok=1
  [[ $v_ok -eq 0 ]] && all_ok=0
  detail+="1_health_version|${v_ok}|${ver}|HTTP${code}\n"

  # 2 pnl-first health
  r=$(fetch "/api/pnl-first/health")
  code="${r%%|*}"; body="${r#*|}"
  pf_ok=$(echo "$body" | python3 -c "
import sys,json
try:
 d=json.load(sys.stdin)
 ok = (d.get('ok') is True) and (d.get('candles_ok') is True)
 print(1 if ok else 0)
except: print(0)
" 2>/dev/null || echo 0)
  [[ "$code" != "200" ]] && pf_ok=0
  [[ $pf_ok -eq 0 ]] && all_ok=0
  detail+="2_pnl_health|${pf_ok}|HTTP${code}\n"

  # 3 manager
  r=$(fetch "/api/pnl-first/manager")
  code="${r%%|*}"; body="${r#*|}"
  mgr_ok=0; [[ "$code" == "200" ]] && mgr_ok=1
  [[ $mgr_ok -eq 0 ]] && all_ok=0
  detail+="3_manager|${mgr_ok}|HTTP${code}\n"

  # 4 kalshi-live-report
  r=$(fetch "/api/pnl-first/kalshi-live-report")
  code="${r%%|*}"; body="${r#*|}"
  kl_ok=$(echo "$body" | python3 -c "
import sys,json
try:
 d=json.load(sys.stdin)
 ok = ('closed_legs' in d) and ('total_pnl_usd' in d)
 print(1 if ok else 0)
except: print(0)
" 2>/dev/null || echo 0)
  [[ "$code" != "200" ]] && kl_ok=0
  [[ $kl_ok -eq 0 ]] && all_ok=0
  detail+="4_kalshi_live|${kl_ok}|HTTP${code}\n"

  # 5 epoch-reconcile kalshi_pnl ~ -16
  r=$(fetch "/api/pnl-first/epoch-reconcile?asset=btc")
  code="${r%%|*}"; body="${r#*|}"
  ep_ok=$(echo "$body" | python3 -c "
import sys,json
try:
 d=json.load(sys.stdin)
 v = d.get('kalshi_pnl')
 if v is None:
  # nested?
  for k in ('summary','reconcile','epoch'):
   if isinstance(d.get(k), dict) and 'kalshi_pnl' in d[k]:
    v = d[k]['kalshi_pnl']; break
 if v is None:
  print(0); sys.exit()
 fv = float(v)
 ok = -25 <= fv <= -8
 print(1 if ok else 0)
 print(fv)
except Exception as e:
 print(0)
 print('err')
" 2>/dev/null || echo -e "0\nerr")
  ep_line1=$(echo "$ep_ok" | head -1)
  ep_val=$(echo "$ep_ok" | tail -1)
  [[ "$code" != "200" ]] && ep_line1=0
  [[ $ep_line1 -eq 0 ]] && all_ok=0
  detail+="5_epoch_reconcile|${ep_line1}|kalshi_pnl=${ep_val}|HTTP${code}\n"

  # 6 regroup-milestones
  r=$(fetch "/api/pnl-first/regroup-milestones")
  code="${r%%|*}"; body="${r#*|}"
  rg_ok=0
  [[ "$code" == "200" ]] && rg_ok=$(echo "$body" | python3 -c "import sys,json; json.load(sys.stdin); print(1)" 2>/dev/null || echo 0)
  [[ $rg_ok -eq 0 ]] && all_ok=0
  detail+="6_regroup_milestones|${rg_ok}|HTTP${code}\n"

  echo -e "[$(date -Iseconds)] iter=$iter all_ok=$all_ok consec_before=$CONSEC\n$detail" | tee -a "$LOG"

  # deploy + scripts
  if [[ $v_ok -eq 1 && $RAILWAY_SCRIPTS_DONE -eq 0 ]]; then
    RAILWAY_SCRIPTS_DONE=1
    run_railway_scripts
    sleep 15
    return 2  # re-check after scripts without counting consec yet
  fi

  if [[ $all_ok -eq 1 && $v_ok -eq 1 ]]; then
    CONSEC=$((CONSEC + 1))
  else
    CONSEC=0
  fi

  return $([[ $all_ok -eq 1 ]] && echo 0 || echo 1)
}

: > "$LOG"
iter=0
while [[ $(date +%s) -lt $END ]]; do
  iter=$((iter + 1))
  set +e
  check_all "$iter"
  rc=$?
  set -e
  if [[ $rc -eq 2 ]]; then
    iter=$((iter + 1))
    check_all "$iter"
    rc=$?
  fi
  if [[ $CONSEC -ge 2 ]]; then
    echo "SUCCESS: two consecutive full passes" | tee -a "$LOG"
    break
  fi
  if [[ $(date +%s) -ge $END ]]; then
    echo "TIMEOUT after ${MAX_MIN}m" | tee -a "$LOG"
    break
  fi
  sleep $INTERVAL
done

# final snapshot
login >/dev/null
for path in "/health" "/api/pnl-first/health" "/api/pnl-first/manager" "/api/pnl-first/kalshi-live-report" "/api/pnl-first/epoch-reconcile?asset=btc" "/api/pnl-first/regroup-milestones"; do
  r=$(fetch "$path")
  echo "FINAL $path: ${r%%|*} bytes=${#r}" >> "$LOG"
done

rm -f "$COOKIE"
echo "DONE consec=$CONSEC scripts=$RAILWAY_SCRIPTS_DONE" >> "$LOG"
