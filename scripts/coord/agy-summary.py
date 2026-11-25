import json
import sys


def _dict(val):
    return val if isinstance(val, dict) else {}


def summarize(path):
    with open(path, encoding="utf-8") as fh:
        for l in fh:
            try:
                d = json.loads(l)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            e = d.get("event")
            if e == "init":
                init = _dict(d.get("init"))
                print("model", init.get("model"), "cwd", init.get("cwd"))
            elif e == "step_update":
                s = _dict(d.get("step_update"))
                if s.get("step_type") == "tool" and s.get("state") == "DONE":
                    p = _dict(_dict(s.get("tool_info")).get("parameters"))
                    arg = p.get("CommandLine") or p.get("Path") or p.get("TargetFile") or ""
                    print("tool", s.get("tool_name"), str(arg)[:110])
            elif e == "result":
                r = _dict(d.get("result"))
                resp = r.get("response")
                resp_tail = resp[-1500:] if isinstance(resp, str) else ""
                print("RESULT", r.get("status"), r.get("denied_actions"), r.get("usage"), "\n", resp_tail)


if __name__ == "__main__":
    summarize(sys.argv[1])
