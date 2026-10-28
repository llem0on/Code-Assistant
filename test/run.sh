#!/usr/bin/env bash
# Self-check for .claude/scripts/qa.sh with a dummy conversation. Run: bash .qa/test/run.sh
set -euo pipefail
QA="$(cd "$(dirname "$0")/.." && pwd)/scripts/qa.sh"
W=$(mktemp -d); trap 'kill ${PID:-} 2>/dev/null || true; rm -rf "$W"' EXIT
export QA_ROOT="$W/proj" QA_TRANSCRIPT_DIR="$W/tr"; mkdir -p "$QA_ROOT" "$QA_TRANSCRIPT_DIR"
ok() { echo "ok  - $1"; }; die() { echo "FAIL - $1" >&2; exit 1; }

# line <type> <ts> <content-json> [extra-json]
line() { local x="${4:-}"; [ -n "$x" ] || x='{}'
  jq -nc --arg t "$1" --arg ts "$2" --argjson c "$3" --argjson x "$x" \
  '{type:$t, uuid:("u-"+$ts), timestamp:$ts, isSidechain:false, message:{content:$c}} + $x'; }
txt() { jq -nc --arg s "$1" '[{type:"text",text:$s}]'; }

S1="$QA_TRANSCRIPT_DIR/s1.jsonl"; S2="$QA_TRANSCRIPT_DIR/s2.jsonl"
{ line user 2026-01-01T10:00:00.000Z '"OLD-SECRET: endpoint /legacy harus 410"'
  line assistant 2026-01-01T10:00:05.000Z "$(txt 'OLD-SECRET dicatat')"
  echo '{"type":"queue-operation","operation":"enqueue"}'
  line user 2026-01-01T10:01:00.000Z '"<command-message>qa-start</command-message>\n<command-name>/qa-start</command-name>"'
  line assistant 2026-01-01T10:01:01.000Z "$(txt 'QA-START: marker x')"; } > "$S1"
bash "$QA" start >/dev/null
[ "$(jq -r .ts "$QA_ROOT/.qa/marker.json")" = 2026-01-01T10:00:05.000Z ] && ok "marker = last real message" || die marker

# segment: after marker, across /clear (new file s2)
{ line user 2026-01-01T10:02:00.000Z '"GET /users/{id} harus 404 kalau tidak ada"'
  line assistant 2026-01-01T10:02:05.000Z '[{"type":"thinking","thinking":"THINK-LEAK"},{"type":"text","text":"Oke, 404 dengan body error JSON?"}]'
  line user 2026-01-01T10:02:10.000Z '"Ya. <system-reminder>REMINDER-LEAK\nbaris2</system-reminder>"'
  line user 2026-01-01T10:02:11.000Z '[{"type":"tool_result","content":"TOOL-LEAK"}]'; } >> "$S1"
{ echo 'garbled {'
  line user 2026-01-01T10:05:00.000Z "$(txt 'Setelah /clear: POST /users wajib email unik, 409 kalau duplikat')"
  line assistant 2026-01-01T10:05:05.000Z "$(txt 'SIDECHAIN-LEAK')" '{"isSidechain":true}'
  line user 2026-01-01T10:09:00.000Z "$(txt '<command-message>qa-end</command-message>')"; } > "$S2"
out=$(bash "$QA" end); SEG="$QA_ROOT/.qa/segment.txt"
[ "$(wc -l <<<"$out")" -eq 1 ] && grep -q ' 4 pesan' <<<"$out" && ok "4 pesan in segment" || die "count: $out"
grep -q 'GET /users' "$SEG" && grep -q 'Setelah /clear' "$SEG" && ok "survives /clear (2 files)" || die clear
! grep -qE 'OLD-SECRET|THINK-LEAK|REMINDER-LEAK|TOOL-LEAK|SIDECHAIN-LEAK|qa-start|qa-end' "$SEG" && ok "no pre-marker/thinking/tool/sidechain/reminder/qa chatter" || { cat "$SEG"; die leak; }

# /qa-end args: types (comma, no spaces) + effort
SC="$QA_ROOT/.qa/scope.json"
[ "$(jq -c . "$SC")" = '{"types":["api","logic","ui"],"effort":"medium"}' ] && ok "default scope semua jenis/medium" || die "default scope"
bash "$QA" end ui,request,logic high >/dev/null && [ "$(jq -c . "$SC")" = '{"types":["api","logic","ui"],"effort":"high"}' ] && ok "ui,request,logic high -> api,logic,ui" || die scope
bash "$QA" end "ui," logic 2>/dev/null && die "spasi diterima" || ok "'ui, logic' (pakai spasi) ditolak"
bash "$QA" end api extreme 2>/dev/null && die "effort invalid diterima" || ok "effort invalid ditolak"

# queue: next/run/report (needs curl + python3 for a throwaway server)
command -v python3 >/dev/null && command -v curl >/dev/null || { echo "skip - run/report (no python3/curl)"; exit 0; }
mkdir "$W/www"; echo '{"ok":true}' > "$W/www/health.json"
PORT=$((20000 + RANDOM % 10000)); (cd "$W/www" && python3 -m http.server "$PORT" >/dev/null 2>&1) & PID=$!; sleep 1
jq -n --arg b "http://127.0.0.1:$PORT" '{approved:false, base_url:$b, tests:[
  {id:"T-1",requirement_id:"G-1",title:"health ok",method:"GET",path:"/health.json",expect_status:200,expect_jq:".ok==true",status:"pending"},
  {id:"T-2",requirement_id:"G-1",title:"missing 404",method:"GET",path:"/nope",expect_status:404,status:"pending"},
  {id:"T-3",requirement_id:"C-1",title:"wrong expect",method:"GET",path:"/health.json",expect_status:201,status:"pending",inferred:true},
  {id:"T-4",requirement_id:"C-1",title:"post unconfirmed",method:"POST",path:"/x",expect_status:201,status:"pending"},
  {id:"T-5",requirement_id:"G-2",type:"logic",title:"logic ok",cmd:"echo hasil=ok",expect_output:"hasil=ok",status:"pending"},
  {id:"T-6",requirement_id:"G-2",type:"logic",title:"logic gagal",cmd:"echo boom; exit 3",status:"pending"},
  {id:"T-7",requirement_id:"G-3",type:"ui",title:"halaman register",url:"http://x/register",steps:["klik Daftar"],expect:"error tampil",status:"pending"}]}' > "$QA_ROOT/.qa/queue.json"
bash "$QA" run T-1 2>/dev/null && die "run before approve" || ok "run refused before approve"
bash "$QA" approve >/dev/null
for id in T-1 T-2 T-3 T-4 T-5 T-6 T-7; do bash "$QA" run "$id"; done > "$W/log"
grep -q 'PASS T-1' "$W/log" && grep -q 'PASS T-2' "$W/log" && grep -q 'FAIL T-3' "$W/log" && grep -q 'BLOCKED T-4' "$W/log" && ok "api: pass/pass/fail/blocked(mutating)" || { cat "$W/log"; die run; }
grep -q 'PASS T-5' "$W/log" && grep -q 'FAIL T-6 .*exit 3' "$W/log" && ok "logic: pass/fail(exit 3)" || { cat "$W/log"; die logic; }
grep -q '^UI T-7' "$W/log" && [ "$(bash "$QA" next | jq -r '.test.id')" = T-7 ] && ok "ui: run hands off to /browse, stays pending" || { cat "$W/log"; die ui; }
bash "$QA" set T-7 maybe 2>/dev/null && die "status invalid diterima" || ok "set status invalid ditolak"
bash "$QA" set T-7 pass "teks error tampil" | grep -q 'PASS T-7' && ok "ui: set pass" || die set
[ "$(bash "$QA" next | jq '.test')" = null ] && ok "queue drained" || die next
bash "$QA" report | grep -q 'pass 4 fail 2 blocked 1' && grep -q '| C-1 (inferred) | 0 | 1 | 1 | 0 | FAIL |' "$QA_ROOT/.qa/report.md" && grep -q '| T-6 | G-2 | logic |' "$QA_ROOT/.qa/report.md" && ok "report per requirement + type" || { cat "$QA_ROOT/.qa/report.md"; die report; }
echo "ALL OK"
