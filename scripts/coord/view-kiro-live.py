#!/usr/bin/env python3
"""Read-only live view of the REAL Kiro coordinator stream logs (logs/raw/kiro-coord-*.jsonl).
Follows the newest log; prints messages, tool calls and status changes. Never writes anything."""
import glob, json, os, time
os.chdir(os.path.join(os.path.dirname(__file__), "..", ".."))
cur, fh, buf, part = None, None, "", b""
def newest():
    fs = sorted(glob.glob("logs/raw/kiro-coord-2*.jsonl"), key=os.path.getmtime)
    return fs[-1] if fs else None
while True:
    n = newest()
    if n != cur and n:
        cur, fh, part = n, open(n, "rb"), b""; print(f"\n===== following {cur} =====", flush=True)
    line = fh.readline() if fh else b""
    if line and not line.endswith(b"\n"):
        # Linear full review D13: a record still being written; keep it until its newline.
        part += line; time.sleep(0.2); continue
    if line and part:
        line, part = part + line, b""
    # Decoded only once the record is complete (a split multibyte character is no error).
    line = line.decode("utf-8", errors="replace")
    if not line:
        if buf: print(buf, flush=True); buf = ""
        time.sleep(1); continue
    try: d = json.loads(line)
    except Exception: continue
    u = d.get("data", {}).get("update", {}); k = u.get("sessionUpdate")
    if k == "agent_message_chunk":
        buf += u.get("content", {}).get("text", "")
        if len(buf) > 300 or buf.endswith("\n"): print(buf, end="", flush=True); buf = ""
    elif k == "tool_call":
        if buf: print(buf, flush=True); buf = ""
        print(f"\n[{time.strftime('%H:%M:%S')}] TOOL {u.get('title','')}", flush=True)
    elif k == "tool_call_update" and u.get("status") in ("completed", "failed"):
        print(f"  -> {u.get('status')}", flush=True)
    elif d.get("type") == "metadata" and d["data"].get("stopReason"):
        print(f"\n[turn end: {d['data']['stopReason']}]", flush=True)
