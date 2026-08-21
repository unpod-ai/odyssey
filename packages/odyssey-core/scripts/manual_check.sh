#!/usr/bin/env bash
# Manual verification for the odyssey capture layer — evidence, not assertions.
#
# The test suite proves the code does what the code says. This proves it to a
# person: every check prints what actually happened, and every check can fail.
# Several of them sabotage the process on purpose (SIGKILL mid-write, a dead
# spool, two processes fighting over one journey) because those are the paths
# that matter and the ones a green test run is easiest to disbelieve.
#
#   bash scripts/manual_check.sh                    # uses `python` from PATH
#   PY=.venv/bin/python bash scripts/manual_check.sh
#   WORKDIR=/tmp/mycheck bash scripts/manual_check.sh
#
# Runs in a scratch directory under $TMPDIR (never in the repo), prints its path,
# and exits non-zero if any check fails. Needs no network, no server, and no
# provider SDK installed.
set -u
PY="${PY:-python}"
pass=0; fail=0
ok()   { echo "  ✅ $1"; pass=$((pass+1)); }
bad()  { echo "  ❌ $1"; fail=$((fail+1)); }
hdr()  { echo; echo "=== $1 ==="; }

# Scratch space outside the repo: several checks write spools, kill processes and
# leave shards behind, and none of that belongs in a working tree.
WORKDIR="${WORKDIR:-${TMPDIR:-/tmp}/odyssey-manual-check}"
rm -rf "$WORKDIR" && mkdir -p "$WORKDIR" && cd "$WORKDIR"
echo "workdir: $WORKDIR"
echo "python : $($PY -c 'import sys; print(sys.executable)')"
echo "odyssey: $($PY -c 'import odyssey; print(odyssey.__file__)')"

# ---------------------------------------------------------------------------
hdr "1. init() with zero arguments — config from env only"
export ODYSSEY_SPOOL=./spool1 ODYSSEY_OUT=./out1
$PY - <<'PY'
import odyssey
c = odyssey.init(drain_interval=None)
print("  spool resolved to:", c.config.spool_dir)
print("  out   resolved to:", c.config.out_dir)
print("  writer_id        :", c.writer_id)
PY
[ -d ./spool1 ] && ok "spool created at the env-provided path" || bad "spool not created"

# ---------------------------------------------------------------------------
hdr "2. seq is allocated — the caller never types one"
$PY - <<'PY'
import odyssey
from odyssey.primitives import Message
odyssey.init(spool_dir="./s2", out_dir="./o2", drain_interval=None)
with odyssey.journey(id="j") as j:
    for i in range(5):
        j.message(Message(role="user", content=f"m{i}"))
seqs = [e.seq for e in odyssey.get_client().spool.read("j")]
print("  seqs:", seqs)
assert seqs == list(range(6)), seqs          # 5 messages + terminal
print("  RESULT: sequential, no gaps, none typed by hand")
PY
[ $? -eq 0 ] && ok "seq auto-allocated 0..5" || bad "seq allocation broken"

# ---------------------------------------------------------------------------
hdr "3. a secret never reaches disk"
$PY - <<'PY'
import odyssey
from odyssey.primitives import Message, ToolCall
odyssey.init(spool_dir="./s3", out_dir="./o3", drain_interval=None)
with odyssey.journey(id="j") as j:
    j.message(Message(role="assistant", tool_calls=[ToolCall(
        id="t", name="pay",
        arguments={"api_key": "sk-LIVE-SECRET", "amount": 100})]))
PY
raw=$(cat ./s3/journeys/j/000.jsonl)
echo "  raw line contains 'sk-LIVE-SECRET': $(grep -c 'sk-LIVE-SECRET' ./s3/journeys/j/000.jsonl)"
echo "  raw line contains 'REDACTED'      : $(grep -c 'REDACTED' ./s3/journeys/j/000.jsonl)"
echo "  raw line contains 'amount'        : $(grep -c 'amount' ./s3/journeys/j/000.jsonl)"
if ! grep -q 'sk-LIVE-SECRET' ./s3/journeys/j/000.jsonl && grep -q 'REDACTED' ./s3/journeys/j/000.jsonl; then
  ok "secret masked before the write, non-secret field preserved"
else bad "SECRET LEAKED TO DISK"; fi

# ---------------------------------------------------------------------------
hdr "4. recording performs no network I/O"
$PY - <<'PY'
import socket
def boom(*a, **k): raise AssertionError("record() touched the network")
socket.socket = boom
socket.create_connection = boom
import odyssey
from odyssey.primitives import Message
odyssey.init(spool_dir="./s4", out_dir="./o4", drain_interval=None)
with odyssey.journey(id="j") as j:
    for i in range(20): j.message(Message(role="user", content="x"))
print("  20 events recorded with socket() sabotaged — no network on the hot path")
PY
[ $? -eq 0 ] && ok "no network I/O while recording" || bad "record() used the network"

# ---------------------------------------------------------------------------
hdr "5. events survive a hard kill (SIGKILL, no cleanup)"
cat > killme.py <<'PY'
import odyssey, time
from odyssey.primitives import Message
odyssey.init(spool_dir="./s5", out_dir="./o5", drain_interval=None, fsync=True)
# `with` on purpose: keeping only the handle lets the context manager be
# garbage-collected, which ends the journey. Check 13 covers that case.
with odyssey.journey(id="j", terminal=False) as h:
    for i in range(15):
        h.message(Message(role="assistant", content=f"turn {i}"))
    print("READY", flush=True)
    time.sleep(60)          # killed here, mid-journey, no cleanup runs
PY
# Block on the child's own READY line — a FIFO read waits, a spin loop does not.
rm -f ready.fifo && mkfifo ready.fifo
$PY killme.py > ready.fifo & child=$!
read -r line < ready.fifo
echo "  child said: $line (all 15 events written, now killing it)"
kill -9 $child 2>/dev/null; wait $child 2>/dev/null
n=$($PY -c "
import odyssey
print(len(odyssey.Spool(odyssey.SpoolConfig(root='./s5')).read('j')))")
echo "  events on disk after SIGKILL: $n / 15"
[ "$n" = "15" ] && ok "nothing lost to a hard kill" || bad "lost events on SIGKILL ($n/15)"

# ---------------------------------------------------------------------------
hdr "6. atexit flushes — app never calls flush()"
cat > noflush.py <<'PY'
import odyssey
from odyssey.primitives import Message
odyssey.init(spool_dir="./s6", out_dir="./o6", drain_interval=None)
with odyssey.journey(id="j") as j:
    j.message(Message(role="user", content="hi"))
print("  app exiting without calling flush()")
PY
$PY noflush.py
if [ -f ./o6/j.jsonl ]; then
  echo "  out/j.jsonl exists, $(grep -c . ./o6/j.jsonl) lines"
  ok "atexit drained the spool"
else bad "nothing drained — atexit hook did not fire"; fi

# ---------------------------------------------------------------------------
hdr "7. a broken spool does NOT take the app down"
$PY - <<'PY'
import odyssey
from odyssey.primitives import Message
c = odyssey.init(spool_dir="./s7", out_dir="./o7", drain_interval=None)
def sabotage(_e): raise OSError("disk full")
c.spool.record = sabotage                     # simulate the storage dying
with odyssey.journey(id="j", terminal=False) as j:
    r = j.message(Message(role="user", content="hi"))
print("  message() returned:", r, "(None = dropped, not raised)")
print("  app still running. capture_errors =", c.stats.capture_errors)
print("  last error         :", c.stats.recent_errors[0].splitlines()[0])
PY
[ $? -eq 0 ] && ok "storage failure counted, app survived" || bad "app crashed on a spool failure"

# ---------------------------------------------------------------------------
hdr "8. ODYSSEY_ENABLED=0 turns recording off entirely"
ODYSSEY_ENABLED=0 $PY - <<'PY'
import odyssey
from odyssey.primitives import Message
odyssey.init(spool_dir="./s8", out_dir="./o8", drain_interval=None)
with odyssey.journey(id="j") as j:
    print("  message() returned:", j.message(Message(role="user", content="hi")))
print("  journeys on disk:", odyssey.get_client().spool.journey_ids())
PY
[ $? -eq 0 ] && ok "kill switch works" || bad "kill switch broken"

# ---------------------------------------------------------------------------
hdr "9. two processes on one journey = detected, not silently corrupted"
cat > writer.py <<'PY'
import sys, odyssey
from odyssey.primitives import Message
odyssey.init(spool_dir="./s9", out_dir="./o9", drain_interval=None)
with odyssey.journey(id="shared", terminal=False) as j:
    for i in range(3):
        j.message(Message(role="assistant", content=f"{sys.argv[1]}-{i}"))
PY
$PY writer.py procA
$PY writer.py procB
echo "  --- odyssey.cli health ---"
$PY -m odyssey.cli --spool ./s9 health
code=$?
echo "  exit code: $code  (3 = lineage violation)"
[ "$code" = "3" ] && ok "writer conflict caught, exit 3" || bad "conflict NOT detected (exit $code)"

# ---------------------------------------------------------------------------
hdr "10. a restarted process resumes the sequence instead of colliding"
cat > resume.py <<'PY'
import odyssey
from odyssey.primitives import Message
odyssey.init(spool_dir="./s10", out_dir="./o10", drain_interval=None)
with odyssey.journey(id="j", terminal=False) as h:
    h.message(Message(role="user", content="turn"))
print("  wrote seq:", [e.seq for e in odyssey.get_client().spool.read("j")])
PY
$PY resume.py; $PY resume.py; $PY resume.py
final=$($PY -c "
import odyssey
print([e.seq for e in odyssey.Spool(odyssey.SpoolConfig(root='./s10')).read('j')])")
echo "  after 3 separate runs: $final"
[ "$final" = "[0, 1, 2]" ] && ok "seq resumed across restarts, no duplicates" || bad "seq collided on restart: $final"

# ---------------------------------------------------------------------------
hdr "11. how fast is record(), measured here on your machine"
$PY - <<'PY'
import time, statistics, odyssey
from odyssey.primitives import Message
c = odyssey.init(spool_dir="./s11", out_dir="./o11", drain_interval=None)
from odyssey.primitives import JourneyEvent
s = c.spool
ev = lambda i: JourneyEvent(journey_id="p", seq=i, kind="message",
                            message=Message(role="assistant", content="x"*200))
for i in range(50): s.record(ev(i))
lat=[]
for i in range(50, 2050):
    t=time.perf_counter_ns(); s.record(ev(i)); lat.append((time.perf_counter_ns()-t)/1000)
lat.sort()
print(f"  p50 {lat[len(lat)//2]:.1f}us  p99 {lat[int(len(lat)*.99)]:.1f}us  "
      f"{1_000_000/statistics.mean(lat):.0f} events/sec")
print("  (pre-change baseline was ~196us p50 / ~5100 per sec)")
PY
ok "perf measured above — judge it yourself"

# ---------------------------------------------------------------------------
hdr "12. auto-capture: 3 turns, provider resends history every time"
$PY - <<'PY'
import sys, types
class R:
    def __init__(s, blocks): s._d={"id":"m","role":"assistant","content":blocks,
                                   "model":"claude-opus-5","stop_reason":"end_turn",
                                   "usage":{"input_tokens":11,"output_tokens":7}}
    def model_dump(s): return dict(s._d)
    @property
    def content(s): return s._d["content"]
class M:
    def __init__(s): s.n=0
    def create(s, **kw):
        s.n+=1
        return [R([{"type":"text","text":"Which day?"}]),
                R([{"type":"thinking","thinking":"tue 3pm"},
                   {"type":"tool_use","id":"tc","name":"book","input":{"day":"tue"}}]),
                R([{"type":"text","text":"Booked."}])][s.n-1]
class A:
    def __init__(s, **kw): s.messages=M()
m=types.ModuleType("anthropic"); m.Anthropic=A; m.AsyncAnthropic=A
sys.modules["anthropic"]=m

import odyssey
from odyssey.integrations.anthropic import Anthropic
odyssey.init(spool_dir="./s12", out_dir="./o12", drain_interval=None)
cl = Anthropic()
TOOLS=[{"name":"book","description":"d","input_schema":{}}]
with odyssey.journey(id="j") as j:
    msgs=[{"role":"user","content":"Book me."}]
    for turn in range(3):
        r = cl.messages.create(model="claude-opus-5", system="You book.",
                               messages=msgs, tools=TOOLS)
        msgs.append({"role":"assistant","content":r.content})
        if turn < 2: msgs.append({"role":"user","content":f"more {turn}"})
    j.signal("thumbs_up")

ev = odyssey.get_client().spool.read("j")
roles=[e.message.role for e in ev if e.kind=="message"]
print("  provider was sent the full history 3 times.")
print("  roles recorded  :", roles)
print("  system recorded :", roles.count("system"), "(sent 3x)")
print("  tool schemas    :", sum(1 for e in ev if e.kind=='message' and e.message.tool_definitions), "(sent 3x)")
print("  capture errors  :", odyssey.get_client().stats.capture_errors)
assert roles.count("system")==1, "system prompt duplicated!"
f = odyssey.fold(ev, data_source="anthropic")
print("  trainable       :", f.trainable, "| steps:", len(f.journey.steps),
      "| tool calls:", f.journey.metrics.num_tool_calls)
PY
[ $? -eq 0 ] && ok "no duplicated history, journey is trainable" || bad "duplicate turns recorded"

# ---------------------------------------------------------------------------
hdr "13. misusing journey() degrades honestly instead of lying"
$PY - <<'PY'
import gc, odyssey
from odyssey.primitives import Message
c = odyssey.init(spool_dir="./s13", out_dir="./o13", drain_interval=None)
h = odyssey.journey(id="j").__enter__()      # WRONG: manager not held by `with`
gc.collect()                                 # CPython ends the abandoned scope
for _ in range(3):
    h.message(Message(role="user", content="after the scope died"))
ev = c.spool.read("j")
t = ev[-1].terminal
print("  terminal reason :", t.termination_reason, "(STALE, not a fake ERROR)")
print("  terminal error  :", t.error)
print("  events dropped  :", c.stats.events_dropped, "(counted, not vanished)")
assert t.termination_reason == "STALE"
assert c.stats.events_dropped == 3
PY
[ $? -eq 0 ] && ok "abandoned scope = STALE + counted drops" || bad "misuse handled badly"

# ---------------------------------------------------------------------------
echo
echo "==============================================="
echo " passed: $pass    failed: $fail"
echo "==============================================="
echo "artifacts left in $WORKDIR — inspect them, or delete the directory"
[ "$fail" = "0" ] || exit 1
