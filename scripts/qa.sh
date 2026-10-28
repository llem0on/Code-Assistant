#!/usr/bin/env bash
# QA pipeline helper. Needs only bash + jq (+ curl for api tests).
# Usage: qa.sh start | end [types] [effort] | next | run <id> | set <id> <status> [actual] | approve | report
set -euo pipefail

ROOT="${QA_ROOT:-$PWD}"
# Claude Code stores transcripts in ~/.claude/projects/<cwd, non-alphanumerics -> "->/*.jsonl
TDIR="${QA_TRANSCRIPT_DIR:-$HOME/.claude/projects/$(printf %s "$ROOT" | sed 's/[^A-Za-z0-9]/-/g')}"
Q="$ROOT/.qa"
QF="$Q/queue.json"
MAXCHARS="${QA_MAXCHARS:-4000}"

# user/assistant text of every session file in the project: one JSON object per message.
# Skips sidechains, meta, tool output, thinking, system-reminders, and the QA commands' own chatter.
msgs() {
  jq -cR 'fromjson?
    | select((.type=="user" or .type=="assistant") and .isSidechain!=true and .isMeta!=true)
    | {ts:.timestamp, uuid, role:.type,
       text:((.message.content? // "")
             | if type=="string" then . else ([.[]? | select(.type=="text") | .text] | join("\n")) end
             | gsub("<system-reminder>.*?</system-reminder>";"";"p"))}
    | select((.text|test("\\S"))
             and (.text|test("^\\s*(<command-message>qa-|<local-command|QA-START:|QA-END:)")|not))' \
    "$TDIR"/*.jsonl
}

start() {
  [ -d "$TDIR" ] || { echo "transcript dir tidak ada: $TDIR" >&2; exit 1; }
  mkdir -p "$Q"
  # marker = timestamp of the newest message now (ISO-8601 UTC sorts as a string; survives /clear and resume)
  last=$(msgs | jq -s 'max_by(.ts) // {ts:"1970-01-01T00:00:00.000Z",uuid:null}')
  cur=$(ls -t "$TDIR"/*.jsonl | sed -n 1p)
  jq -n --argjson l "$last" --arg t "$cur" '{ts:$l.ts, uuid:$l.uuid, transcript:$t}' > "$Q/marker.json"
  echo "QA-START: marker $(jq -r .ts "$Q/marker.json") ($(basename "$cur"))"
}

end() { # [types] [effort]
  local types="${1:-ui,api,logic}" effort="${2:-medium}"
  [[ "$types" =~ ^(ui|api|request|logic)(,(ui|api|request|logic))*$ ]] \
    || { echo "jenis test tidak valid: '$types' (ui,api,logic dipisah koma tanpa spasi)" >&2; exit 2; }
  [[ "$effort" =~ ^(low|medium|high)$ ]] || { echo "effort tidak valid: '$effort' (low|medium|high)" >&2; exit 2; }
  [ -f "$Q/marker.json" ] || { echo "marker tidak ada, jalankan /qa-start dulu" >&2; exit 1; }
  jq -n --arg t "${types//request/api}" --arg e "$effort" '{types:($t|split(",")|unique), effort:$e}' > "$Q/scope.json"
  ts=$(jq -r .ts "$Q/marker.json")
  sel=$(msgs | jq -s --arg ts "$ts" 'map(select(.ts > $ts)) | sort_by(.ts)')
  printf %s "$sel" | jq -r --argjson max "$MAXCHARS" '.[]
    | "[\(.role|ascii_upcase)] " + (if (.text|length) > $max then .text[0:$max] + " …[dipotong]" else .text end) + "\n"' \
    > "$Q/segment.txt"
  echo "QA-END: $(printf %s "$sel" | jq length) pesan -> $Q/segment.txt | scope $(jq -r '.types|join(",")' "$Q/scope.json") / $effort"
}

next() {
  jq -c '[.tests[]|select(.status=="pending")] as $p
    | {n:((.tests|length)-($p|length)+1), total:(.tests|length), test:$p[0]}' "$QF"
}

upd() { # id status actual evidence
  local tmp; tmp=$(mktemp)
  jq --arg id "$1" --arg s "$2" --arg a "$3" --arg e "$4" \
    '(.tests[]|select(.id==$id)) |= (.status=$s | .actual=$a | .evidence=$e)' "$QF" > "$tmp" && mv "$tmp" "$QF"
}

mark() { # id status detail
  local i
  case $2 in pass) i="✔ PASS";; fail) i="✘ FAIL";; *) i="⚠ BLOCKED";; esac
  echo "$i $1 | $3"
}

approve() {
  local tmp; tmp=$(mktemp)
  jq '.approved=true' "$QF" > "$tmp" && mv "$tmp" "$QF"
  echo "queue approved: $(jq '.tests|length' "$QF") test"
}

run() {
  local id="$1" t
  t=$(jq -c --arg id "$id" '.tests[]|select(.id==$id)' "$QF")
  [ -n "$t" ] || { echo "id tidak ada: $id" >&2; exit 1; }
  [ "$(jq -r '.approved' "$QF")" = true ] || { echo "queue belum approved" >&2; exit 1; }
  case "$(jq -r '.type // "api"' <<<"$t")" in
    api) run_api "$id" "$t";;
    logic) run_logic "$id" "$t";;
    ui) echo "UI $id: jalankan langkahnya via /browse, lalu: qa.sh set $id pass|fail|blocked \"<actual>\"";;
    *) echo "type tidak dikenal di $id" >&2; exit 1;;
  esac
}

run_api() { # id test-json
  local id="$1" t="$2" base method path body exp ejq tmp code s actual h
  base=$(jq -r '.base_url // empty' "$QF")
  [ -n "$base" ] || { echo "base_url kosong di $QF" >&2; exit 1; }
  case "$base" in *prod*) [ "${QA_ALLOW_PROD:-}" = 1 ] || { echo "base_url mengandung 'prod', set QA_ALLOW_PROD=1 setelah konfirmasi user" >&2; exit 1; };; esac
  method=$(jq -r .method <<<"$t"); path=$(jq -r .path <<<"$t")
  body=$(jq -c '.body // empty' <<<"$t"); exp=$(jq -r .expect_status <<<"$t"); ejq=$(jq -r '.expect_jq // empty' <<<"$t")

  case "$method" in
    GET|HEAD|OPTIONS) ;;
    *) if [ "${QA_ALLOW_MUTATING:-}" != 1 ]; then
         upd "$id" blocked "tidak dijalankan: $method butuh konfirmasi user" "-"
         mark "$id" blocked "$method belum dikonfirmasi user"; return 0
       fi;;
  esac

  tmp=$(mktemp)
  args=(-sS -m 30 -X "$method" -o "$tmp" -w '%{http_code}')
  while IFS= read -r h; do args+=(-H "${h//\$QA_TOKEN/${QA_TOKEN:-}}"); done < <(jq -r '.headers[]?' <<<"$t")
  [ -z "$body" ] || args+=(-H 'Content-Type: application/json' --data "$body")
  code=$(curl "${args[@]}" "$base$path" 2>/dev/null) || code=000

  s=pass
  if [ "$code" = 000 ]; then s=blocked
  elif [ "$code" != "$exp" ]; then s=fail
  elif [ -n "$ejq" ] && ! jq -e "$ejq" "$tmp" >/dev/null 2>&1; then s=fail
  fi
  actual="$code $(head -c 300 "$tmp" | tr '\n' ' ')"
  upd "$id" "$s" "$actual" "curl -X $method $base$path -> $code"
  mark "$id" "$s" "expected $exp${ejq:+ & $ejq} | actual $actual"
  rm -f "$tmp"
}

run_logic() { # id test-json; pass = exit 0 (+ output contains expect_output)
  local id="$1" t="$2" cmd dir eo out rc s actual
  cmd=$(jq -r '.cmd // empty' <<<"$t"); [ -n "$cmd" ] || { echo "cmd kosong di $id" >&2; exit 1; }
  dir=$(jq -r '.cwd // empty' <<<"$t"); dir="${dir:-$ROOT}"; eo=$(jq -r '.expect_output // empty' <<<"$t")
  # ponytail: no timeout (macOS ships without `timeout`); a hanging cmd needs Ctrl-C
  rc=0; out=$(cd "$dir" && bash -c "$cmd" 2>&1) || rc=$?
  s=pass
  if [ "$rc" -ne 0 ]; then s=fail
  elif [ -n "$eo" ] && ! grep -qF -- "$eo" <<<"$out"; then s=fail
  fi
  actual="exit $rc $(printf %s "$out" | tail -c 300 | tr '\n' ' ')"
  upd "$id" "$s" "$actual" "(cd $dir) $cmd -> exit $rc"
  mark "$id" "$s" "expected exit 0${eo:+ & output berisi '$eo'} | actual $actual"
}

setres() { # id status [actual] -- result of a test run outside this script (ui via /browse)
  [[ "$2" =~ ^(pass|fail|blocked)$ ]] || { echo "status harus pass|fail|blocked" >&2; exit 2; }
  jq -e --arg id "$1" 'any(.tests[]; .id==$id)' "$QF" >/dev/null || { echo "id tidak ada: $1" >&2; exit 1; }
  upd "$1" "$2" "${3:-}" "manual via /browse"
  mark "$1" "$2" "actual ${3:-}"
}

report() {
  jq -r '
    def c(s): [.tests[]|select(.status==s)]|length;
    "# QA Report\n",
    "Base URL: \(.base_url)\n",
    "**Total \(.tests|length)** · ✔ pass \(c("pass")) · ✘ fail \(c("fail")) · ⚠ blocked \(c("blocked")) · pending \(c("pending"))\n",
    "## Per requirement\n",
    "| Requirement | Pass | Fail | Blocked | Pending | Verdict |", "|---|---|---|---|---|---|",
    (.tests | group_by(.requirement_id)[]
      | def n(s): map(select(.status==s))|length;
        "| \(.[0].requirement_id)\(if .[0].inferred then " (inferred)" else "" end) | \(n("pass")) | \(n("fail")) | \(n("blocked")) | \(n("pending")) | \(if n("fail")>0 then "FAIL" elif n("blocked")+n("pending")>0 then "INCOMPLETE" else "PASS" end) |"),
    "\n## Detail\n",
    "| Test | Req | Type | Title | Status | Actual |", "|---|---|---|---|---|---|",
    (.tests[] | "| \(.id) | \(.requirement_id) | \(.type // "api") | \(.title) | \(.status) | \((.actual // "-") | gsub("\\|";"/")) |")
  ' "$QF" > "$Q/report.md"
  jq -r 'def c(s): [.tests[]|select(.status==s)]|length;
    "report -> .qa/report.md | total \(.tests|length) pass \(c("pass")) fail \(c("fail")) blocked \(c("blocked")) pending \(c("pending"))"' "$QF"
}

case "${1:-}" in
  start) start;; next) next;; approve) approve;; report) report;;
  end) end "${2:-}" "${3:-}";;
  run) run "${2:?test id}";;
  set) setres "${2:?test id}" "${3:?status}" "${4:-}";;
  *) echo "usage: qa.sh start|end [types] [effort]|next|run <id>|set <id> <status> [actual]|approve|report" >&2; exit 2;;
esac
