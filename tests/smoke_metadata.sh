#!/usr/bin/env bash
# Smoke test for the NEW endpoints only (book upsert + chapters replace).
set -uo pipefail
BASE="${BASE:-http://127.0.0.1:8001}"
PASS=0
FAIL=0
check() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    PASS=$((PASS+1)); printf '  ✅ %s\n' "$desc"
  else
    FAIL=$((FAIL+1)); printf '  ❌ %s (expected %s, got %s)\n' "$desc" "$expected" "$actual"
  fi
}

REG=$(curl -s -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' \
  -d '{"username":"carol_test","password":"verystrongpass1"}')
TOKEN=$(echo "$REG" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null)
if [ -z "$TOKEN" ]; then
  # Re-run: user already exists — log in instead.
  TOKEN=$(curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
    -d '{"username":"carol_test","password":"verystrongpass1"}' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null)
fi
H="Authorization: Bearer $TOKEN"
BOOK="book_beta_456"

echo "=== A1. PUT book progress (upsert) ==="
OUT=$(curl -s -X PUT "$BASE/api/progress/book/$BOOK" -H "$H" -H 'Content-Type: application/json' \
  -d '{"last_chapter_index": 2, "last_position_seconds": 124.5, "progress": 0.42}')
check "returns abs_item_id" "$BOOK" "$(echo "$OUT" | python3 -c 'import json,sys;print(json.load(sys.stdin)["abs_item_id"])' 2>/dev/null)"
check "last_chapter_index" "2" "$(echo "$OUT" | python3 -c 'import json,sys;print(json.load(sys.stdin)["last_chapter_index"])' 2>/dev/null)"
check "progress" "0.42" "$(echo "$OUT" | python3 -c 'import json,sys;print(json.load(sys.stdin)["progress"])' 2>/dev/null)"

echo "=== A2. Invalid progress out of range -> 422 ==="
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X PUT "$BASE/api/progress/book/$BOOK" -H "$H" \
  -H 'Content-Type: application/json' -d '{"last_chapter_index": 0, "last_position_seconds": 1.0, "progress": 1.5}')
check "progress >1 rejected" "422" "$CODE"

echo "=== B. PUT chapters replace (mark whole book) ==="
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X PUT "$BASE/api/progress/book/$BOOK/chapters" -H "$H" \
  -H 'Content-Type: application/json' -d '{"chapters": [0, 1, 2, 3]}')
check "replace 4 chapters" "204" "$CODE"

echo "=== C. Bulk shows both ==="
BULK=$(curl -s "$BASE/api/progress" -H "$H")
check "  chapters_done [0..3]" "1" "$(echo "$BULK" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(1 if d["chapters_done"].get("book_beta_456")==[0,1,2,3] else 0)')"
check "  book progress frac 0.42" "1" "$(echo "$BULK" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(1 if abs(d["books"]["book_beta_456"]["progress"]-0.42)<1e-6 else 0)')"

echo "=== D. Replace with partial set (toggle semantics) ==="
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X PUT "$BASE/api/progress/book/$BOOK/chapters" -H "$H" \
  -H 'Content-Type: application/json' -d '{"chapters": [0, 1]}')
check "replace with [0,1]" "204" "$CODE"
BULK2=$(curl -s "$BASE/api/progress" -H "$H")
check "  chapters_done now [0,1]" "1" "$(echo "$BULK2" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(1 if d["chapters_done"].get("book_beta_456")==[0,1] else 0)')"
check "  book progress preserved" "1" "$(echo "$BULK2" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(1 if abs(d["books"]["book_beta_456"]["progress"]-0.42)<1e-6 else 0)')"

echo "=== E. Replace with empty (unmark all) ==="
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X PUT "$BASE/api/progress/book/$BOOK/chapters" -H "$H" \
  -H 'Content-Type: application/json' -d '{"chapters": []}')
check "replace with empty" "204" "$CODE"
BULK3=$(curl -s "$BASE/api/progress" -H "$H")
check "  chapters_done empty" "1" "$(echo "$BULK3" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(1 if d["chapters_done"].get("book_beta_456") in (None, []) else 0)')"

echo "=== F. Negative index in replace -> 422 ==="
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X PUT "$BASE/api/progress/book/$BOOK/chapters" -H "$H" \
  -H 'Content-Type: application/json' -d '{"chapters": [0, -1]}')
check "negative index rejected" "422" "$CODE"

echo "=== G. No auth -> 401 ==="
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X PUT "$BASE/api/progress/book/$BOOK" \
  -H 'Content-Type: application/json' -d '{"last_chapter_index": 0, "last_position_seconds": 1.0}')
check "PUT book no auth" "401" "$CODE"

echo ""
echo "  PASSED: $PASS   FAILED: $FAIL"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1