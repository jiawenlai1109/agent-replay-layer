"""Local view that puts the recovery layer's verdicts next to the upstream run record.

The upstream dashboard shows rounds: plan, executor output, audit, terminal report. It has
no notion of a promise, so "which promise went red at which provider step, where the
rewind targeted, what the generated breakpoint said" is invisible there even though every
one of those values is already on disk. This reads the same durable artifacts the report
scripts read and renders them per run.

Read-only: it never starts a run, never calls a provider, never writes anything. It binds
loopback because the harness service on this box has no auth, and it escapes everything it
prints because task text and agent output are untrusted input.

    python3 panel.py                      # http://127.0.0.1:8899
    python3 panel.py --print <run_id>     # dump one run as HTML to stdout, no server
    SBR_PANEL_PORT=8900 python3 panel.py
"""

from __future__ import annotations

import html
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import sbr_paths

PORT = int(os.environ.get("SBR_PANEL_PORT", "8899"))
UPSTREAM = os.environ.get("LH_API", "http://127.0.0.1:8800")

#: The four verdict symbols. `na` is never a pass, so it gets its own colour.
CELL = {"pass": ("绿", "#e8f5e9"), "fail": ("红", "#ffebee"), "na": ("未到", "#f5f5f5")}
STYLE = "body{font:14px/1.5 system-ui,sans-serif;margin:24px;max-width:1200px}" \
        "table{border-collapse:collapse}td,th{border:1px solid #bbb;padding:3px 7px}" \
        "pre{white-space:pre-wrap;background:#fafafa;padding:10px;border:1px solid #ddd}" \
        ".warn{color:#b00020;font-weight:600}.dim{color:#666}h1,h2{font-size:18px}"


def load() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(sbr_paths.MANIFESTS.glob("*.json"))]


def rescored() -> dict[str, dict[str, str]]:
    """`results/verdict_rescore.csv`, keyed by run id.

    A manifest stores the verdict as judged at the time it ran, and the predicates have
    since changed, so the stored column is evidence about the instrument as much as about
    the agent. `rescore_verdicts.py` re-runs the current ledger over the durable artifacts;
    where it has covered a run, that row wins and the page says which one it used.
    """
    path = sbr_paths.RESULTS / "verdict_rescore.csv"
    if not path.is_file():
        return {}
    import csv

    out = {}
    for r in csv.DictReader(path.open(encoding="utf-8")):
        out[r["run_id"]] = {"new": r["new"], "old": r["old"], "pass": r.get("pass", ""),
                            "fail": r.get("fail", ""), "na": r.get("na", ""),
                            "unevaluated": r.get("unevaluated", "")}
    return out


def steps_of(m: dict) -> int:
    return int((m.get("metrics") or {}).get("steps") or 0)


def red_at(m: dict) -> list[dict]:
    """Every promise's first red, with the step it appeared on.

    `status_flips` is the driver's own record; older manifests predate it, so walk the
    readings instead of reporting nothing.
    """
    first: dict[str, int] = {}
    for f in m.get("status_flips") or []:
        if f.get("to") == "fail":
            cid, step = f.get("commitment_id"), int(f["step"])
            if cid and (cid not in first or step < first[cid]):
                first[cid] = step
    if not first:
        for r in m.get("readings") or []:
            for cid, v in r.items():
                if isinstance(v, dict) and v.get("status") == "fail" and cid not in first:
                    first[cid] = int(r["step"])
    injected = m.get("injected_at_provider_step")
    alarm_id = (m.get("sensor") or {}).get("commitment_id")
    return [{"commitment": cid, "step": step,
             "lag": step - injected if isinstance(injected, int) else None,
             "is_sensor": cid == alarm_id}
            for cid, step in sorted(first.items(), key=lambda kv: (kv[1], kv[0]))]


def alarm(m: dict) -> dict:
    s = m.get("sensor") or {}
    injected = m.get("injected_at_provider_step")
    detect = s.get("provider_step")
    inj = m.get("injection") or {}
    return {"commitment": s.get("commitment_id"), "step": detect, "index": s.get("index"),
            "injected_at": injected,
            "lag": detect - injected if isinstance(detect, int) and isinstance(injected, int) else None,
            "root_cause": inj.get("root_cause"), "cascade": inj.get("cascade") or [],
            "expected": s.get("expected"), "actual": s.get("actual")}


def rewind(m: dict) -> dict:
    return {"target": m.get("last_invariant_holding_provider_step"),
            "ok": m.get("rollback_ok"), "no_rewind": bool(m.get("no_rewind"))}


def directive_text(m: dict) -> str:
    path = m.get("recovery_md")
    if not path:
        return "（这条 run 没有断点说明：D 臂刻意不写，只回退的臂也不给说明）"
    p = Path(path)
    if not p.is_file():
        return "（manifest 记了路径，但工作区里那份已经不在了）"
    return p.read_text(encoding="utf-8", errors="replace")


def claim(m: dict, re_map: dict[str, dict[str, str]] | None = None) -> dict:
    v = m.get("verdict_end_state") or {}
    outcome = m.get("outcome") or {}
    stored = v.get("ledger_verdict") or ("satisfied" if outcome.get("satisfied_by_ledger") else "not_satisfied")
    verdict, source = stored, "当时记录"
    rec = (re_map or {}).get(m.get("run_id"))
    if rec and rec["new"] != "unknown":
        verdict, source = rec["new"], "离线重算"
    report = m.get("report") or {}
    re_counts = {}
    if source == "离线重算" and rec:
        re_counts = {k: int(v) for k, v in rec.items()
                     if k in ("pass", "fail", "na", "unevaluated") and str(v).isdigit()}
    return {"ledger": verdict, "stored": stored, "source": source,
            "superseded": verdict != stored,
            "rescore_old": (stored if source == "离线重算" else (rec or {}).get("old", "")),
            "re_counts": re_counts,
            "violated": v.get("violated") or outcome.get("violated") or [],
            "counts": v.get("latched_counts") or outcome.get("latched_counts") or {},
            "upstream_status": report.get("report_status"),
            "upstream_satisfied": report.get("completion_satisfied"),
            "abort": report.get("abort_reason") or ""}


def row(m: dict, re_map: dict[str, dict[str, str]] | None = None) -> dict:
    a, w, c = alarm(m), rewind(m), claim(m, re_map)
    return {"run_id": m.get("run_id"), "arm": m.get("arm"), "model": m.get("model"),
            "task": m.get("task_id"), "started": m.get("started_at"), "steps": steps_of(m),
            "alarm_commitment": a["commitment"], "alarm_step": a["step"], "lag": a["lag"],
            "rewind_to": w["target"], "rolled_back": w["ok"],
            "ledger": c["ledger"], "stored": c["stored"], "source": c["source"],
            "superseded": c["superseded"],
            "upstream_satisfied": c["upstream_satisfied"],
            "conflict": c["upstream_satisfied"] is True and c["ledger"] != "satisfied",
            "valid": steps_of(m) > 0}


def e(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def render_index(rows: list[dict]) -> str:
    body = []
    for r in sorted(rows, key=lambda x: (not x["valid"], str(x["started"]))):
        name = f"<a href='/run/{e(r['run_id'])}'>{e(r['run_id'])}</a>" if r["valid"] else e(r["run_id"])
        ledger = e(r["ledger"]) + ("" if r["source"] == "离线重算" else " <span class='dim'>（当时记录）</span>")
        body.append("<tr>" + "".join(f"<td>{c}</td>" for c in (
            name, e(r["arm"]), e(r["model"]), e(r["steps"]), e(r["alarm_commitment"]),
            e(r["alarm_step"]), e(r["lag"]), e(r["rewind_to"]),
            "是" if r["rolled_back"] else "", f"<span class='warn'>{ledger}</span>"
            if r["conflict"] else ledger,
            "T" if r["upstream_satisfied"] is True else "")) + "</tr>")
    return (f"<html><head><meta charset='utf-8'><style>{STYLE}</style></head><body>"
            f"<h1>恢复层台账视图</h1><p class='dim'>{len(rows)} 条 manifest。"
            f"报警=第一条变红的承诺与它在第几个 provider 步；lag=报警步减注入步；"
            f"回滚=回退目标步；台账=终态（优先用离线重算，没覆盖到的标出用的是当时记录）；"
            f"T=上游自己报了完成。台账标红=上游说完成而当前判据不满足。"
            f"作废的 run（0 步）不进统计但列在下面。</p>"
            f"<table><tr><th>run</th><th>臂</th><th>模型</th><th>步</th><th>报警承诺</th>"
            f"<th>报警步</th><th>lag</th><th>回滚到</th><th>回滚成</th><th>台账终态</th>"
            f"<th>上游称完成</th></tr>{chr(10).join(body)}</table></body></html>")


def matrix(m: dict) -> str:
    readings = m.get("readings") or []
    if not readings:
        return "<p class='dim'>这条 run 没有逐拍读数（D 臂，或没检出漂移的出口），矩阵无从渲染。</p>"
    commits = list(m.get("sensor_commitments") or [])
    for r in readings:
        for cid, v in r.items():
            if isinstance(v, dict) and cid not in commits:
                commits.append(cid)
    cells = {int(r["step"]): r for r in readings}
    order = sorted(cells)
    head = "".join(f"<th>步 {s}</th>" for s in order)
    alarm_id = (m.get("sensor") or {}).get("commitment_id")
    lines = []
    for cid in commits:
        tds = []
        for s in order:
            v = cells[s].get(cid)
            status = v.get("status") if isinstance(v, dict) else None
            label, color = CELL.get(status, ("未评", "#fff"))
            tip = f" title='期望 {e(v.get('expected'))} / 实测 {e(v.get('actual'))}'" if isinstance(v, dict) else ""
            tds.append(f"<td{tip} style='background:{color}'>{e(label)}</td>")
        lines.append(f"<tr><td>{e(cid)}{' ★' if cid == alarm_id else ''}</td>{''.join(tds)}</tr>")
    costs = "".join(f"<th>{e(cells[s].get('cost_usd'))}</th>" for s in order)
    return (f"<table><tr><th>承诺 / provider 步</th>{head}</tr>{chr(10).join(lines)}"
            f"<tr><td class='dim'>累计成本 USD</td>{costs}</tr></table>"
            f"<p class='dim'>★=探针最终点名的承诺。鼠标停在格子上能看到那条判据的期望值与实测值。"
            f"'未到' 是它涉及的文件还没上场，不算通过。</p>")


def render_run(m: dict, re_map: dict[str, dict[str, str]] | None = None) -> str:
    a, w, c, halt = alarm(m), rewind(m), claim(m, re_map), m.get("at_halt") or {}
    met, bud = m.get("metrics") or {}, m.get("budget") or {}
    reds = "".join(
        "<li>" + e(x["commitment"]) + f" 在第 {e(x['step'])} 步变红"
        + (e(f"（注入后第 {x['lag']} 步）") if x["lag"] is not None else "")
        + ("，正是探针点名的根因" if x["is_sensor"] else "") + "</li>"
        for x in red_at(m)) or "<li class='dim'>整段没有新红灯（未注入，或注入被 agent 自己重写掉了）</li>"
    if w["no_rewind"]:
        rew = "D 臂：检测到，但不回滚、不给说明"
    elif w["target"] is not None:
        rew = f"回滚目标 = 第 {e(w['target'])} 步，结果 {'成功' if w['ok'] else '没成功'}"
    else:
        rew = "没有回滚（探测器未触发，或该臂本来就不回退）"
    conflict = "<p class='warn'>冲突：上游自称完成，重算的台账不满足。</p>" if (
        c["upstream_satisfied"] is True and c["ledger"] != "satisfied") else ""
    old_counts = json.dumps(c["counts"], ensure_ascii=False)
    new_counts = json.dumps(c["re_counts"] or c["counts"], ensure_ascii=False)
    viol = ", ".join(c["violated"])
    stale = ""
    if c["source"] == "离线重算" and c["superseded"]:
        stale = "<li>这条 run 当时记录是 " + e(c["stored"]) + "，当时计数 " + e(old_counts)
        if viol:
            stale += "，当时列出的违反项 " + e(viol)
        stale += ("。用现在的判据离线重算为 " + e(c["ledger"]) +
                  "，本页的判定和冲突标记都以重算为准</li>")
    elif c["rescore_old"]:
        stale = "<li>离线重算表里这条记为 " + e(c["rescore_old"]) + "</li>"
    return (f"<html><head><meta charset='utf-8'><style>{STYLE}</style></head><body>"
            f"<p><a href='/'>全部 run</a></p><h1>{e(m.get('run_id'))}</h1>"
            f"<p>{e(m.get('arm'))} | {e(m.get('model'))} | {e(m.get('agent'))} | 任务 {e(m.get('task_id'))}"
            f" | 起于 {e(m.get('started_at'))} | 触发规则 {e(m.get('trigger_rule'))}"
            f" | 预算：轮数 {e(bud.get('max_rounds'))}，恢复追加 {e(bud.get('extra_rounds'))}，"
            f"注入时机 {e(bud.get('inject_after'))}，分钟上限 {e(bud.get('minutes'))}</p>"
            f"<h2>判定</h2><ul>"
            f"<li>报警承诺 {e(a['commitment'])}，在第 {e(a['step'])} 个 provider 步"
            f"（归一化索引 {e(a['index'])}）；注入在第 {e(a['injected_at'])} 步，lag {e(a['lag'])}</li>"
            f"<li>配方点名的根因 {e(a['root_cause'])}；声明的连锁 {e(','.join(a['cascade'])) or '无'}</li>"
            f"<li>期望 {e(a['expected'])}</li><li>实测 {e(a['actual'])}</li><li>{e(rew)}</li></ul>"
            f"<h2>每条承诺第一次变红</h2><ul>{reds}</ul>"
            f"<h2>判据矩阵</h2>{matrix(m)}"
            f"<h2>断点说明原文</h2><pre>{e(directive_text(m))}</pre>"
            f"<h2>停机这件事</h2><ul>"
            f"<li>报警到确认停机之间完成的 provider 步 {e(halt.get('steps_after_detection'))}，"
            f"成本差 {e(halt.get('cost_after_detection_usd'))} USD，停机时仍开着的步 "
            f"{e(halt.get('unclosed_steps'))}，被砍的中途步 {e(halt.get('mid_flight_step_cut'))}</li>"
            f"<li>全 run 合计 {e(met.get('steps'))} 步 / {e(met.get('tokens'))} token"
            f"（缓存读 {e(met.get('cache_read'))}）/ {e(met.get('cost'))} USD / {e(met.get('rounds'))} 轮</li></ul>"
            f"<h2>终态对比</h2><ul>"
            f"<li>台账终态（依据：{e(c['source'])}）{e(c['ledger'])}，计数 {e(new_counts)}</li>"
            f"{stale}"
            f"<li>上游自己的记录 report_status={e(c['upstream_status'])}，"
            f"completion_satisfied={e(c['upstream_satisfied'])}，abort_reason={e(c['abort'])}</li></ul>"
            f"{conflict}"
            f"<h2>上游侧（现问控制面，不落盘）</h2>"
            f"<div id='up'><button onclick=\"fetch('/api/upstream/{e(m.get('run_id'))}')"
            f".then(r=>r.text()).then(t=>document.getElementById('up').innerText=t)\">"
            f"问上游要这条 run 的状态</button></div></body></html>")


def upstream_status(run_id: str) -> str:
    try:
        with urllib.request.urlopen(f"{UPSTREAM}/api/runs/{run_id}/status", timeout=3) as r:
            return json.dumps(json.loads(r.read().decode("utf-8", "replace")), ensure_ascii=False, indent=2)
    except urllib.error.HTTPError as exc:
        return f"上游返回 {exc.code}: {exc.read().decode('utf-8', 'replace')[:400]}"
    except Exception as exc:
        return (f"问不到上游（{type(exc).__name__}: {exc}）。它只在 `lh-harness web` 起着的时候在，"
                f"起法 bash ~/sbr/restart_api.sh。这不影响本页由盘上记录支撑的内容。")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, kind: str, text: str) -> None:
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - name fixed by http.server
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        runs = {m.get("run_id"): m for m in load()}
        re_map = rescored()
        if not parts:
            self._send(200, "text/html", render_index([row(m, re_map) for m in runs.values()]))
        elif parts == ["api", "runs"]:
            self._send(200, "application/json",
                       json.dumps([row(m, re_map) for m in runs.values()], ensure_ascii=False, indent=2))
        elif len(parts) == 2 and parts[0] == "run" and parts[1] in runs:
            self._send(200, "text/html", render_run(runs[parts[1]], re_map))
        elif len(parts) == 3 and parts[:2] == ["api", "upstream"] and parts[2] in runs:
            self._send(200, "text/plain", upstream_status(parts[2]))
        else:
            self._send(404, "text/plain", "unknown path")

    def log_message(self, fmt: str, *args: Any) -> None:
        pass


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="render the recovery layer's verdicts from durable artifacts")
    ap.add_argument("--print", dest="dump", metavar="RUN_ID", help="print one run as HTML and exit")
    args = ap.parse_args()

    if not sbr_paths.MANIFESTS.is_dir():
        print(f"no manifests under {sbr_paths.MANIFESTS}", file=sys.stderr)
        return 1
    if args.dump:
        for m in load():
            if m.get("run_id") == args.dump:
                print(render_run(m, rescored()))
                return 0
        print(f"no manifest for {args.dump}", file=sys.stderr)
        return 1

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"panel on http://127.0.0.1:{PORT} over {sbr_paths.MANIFESTS}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
