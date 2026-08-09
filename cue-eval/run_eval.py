#!/usr/bin/env python3
"""
CUE extraction eval — stdlib only, no pip install required.

  python3 run_eval.py                          # qwen2.5:14b via Ollama
  python3 run_eval.py --model qwen2.5:32b      # compare a bigger local model
  python3 run_eval.py --runs 5                 # variance across repeated runs
  python3 run_eval.py --provider anthropic     # needs ANTHROPIC_API_KEY
  python3 run_eval.py --band multi             # only the multi-commitment cases
  python3 run_eval.py --show T07               # print raw output for one case
  python3 run_eval.py --json                   # also print a JSON_SUMMARY: line
                                                # (app/observability/drift.py's
                                                # scheduled drift check parses
                                                # this rather than the human
                                                # table above — additive, the
                                                # human output is unchanged)
"""

import argparse, json, os, statistics, sys, time, urllib.error, urllib.request
from pathlib import Path

HERE = Path(__file__).parent
CASES = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
SCHEMA = json.loads((HERE / "schema.json").read_text(encoding="utf-8"))
TEMPLATE = (HERE / "prompt.txt").read_text(encoding="utf-8")

OLLAMA_URL = "http://localhost:11434/api/generate"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


# ----------------------------------------------------------------- prompt ---
def build_prompt(case):
    ctx = CASES["project_context"]
    milestones = "\n".join(
        "  - {}: {}".format(m["name"], m["due"]) for m in ctx["known_milestones"]
    )
    weekday = case.get("sent_weekday")
    return TEMPLATE.format(
        project=ctx["project"],
        client=ctx["client"],
        timezone=ctx["timezone"],
        venue=ctx["venue"],
        build_up=" to ".join(ctx["build_up"]),
        event_days=", ".join(ctx["event_days"]),
        doors=ctx["doors"],
        milestones=milestones,
        channel=case["channel"],
        party=case["party"],
        sent_at=case["sent_at"],
        weekday_hint=" ({})".format(weekday) if weekday else "",
        message=case["message"],
    )


# --------------------------------------------------------------- providers ---
def post(url, payload, headers, timeout=300):
    # 300s, not the original 180s: the README's "8-25s per case" baseline is
    # on warm local hardware (M3 Pro). A CPU-only cloud CI runner has no such
    # guarantee even once the model is warm (see cue-eval.yml's warmup step
    # for the separate, larger budget that covers the one-time cold-load
    # cost specifically) — this just gives real per-case inference more room
    # before deciding something is actually stuck.
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def call_ollama(prompt, model):
    body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "2h",
        "format": SCHEMA,
        "options": {"num_ctx": 16384, "temperature": 0},
    }
    out = post(OLLAMA_URL, body, {"Content-Type": "application/json"})
    return out["response"]


def call_anthropic(prompt, model):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY is not set.")
    body = {
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
    }
    headers = {
        "content-type": "application/json",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
    }
    out = post(ANTHROPIC_URL, body, headers)
    for block in out.get("content", []):
        if block.get("type") == "text":
            return block["text"]
    return ""


# ----------------------------------------------------------------- scoring ---
def norm_dt(v):
    """Compare timestamps loosely: date-only expectations match any time that day."""
    if v is None:
        return None
    return str(v).strip().replace(" ", "T")


def match_one(exp, got):
    """Score a single expected commitment against a candidate. Returns (hits, total)."""
    hits = total = 0
    for field, want in exp.items():
        total += 1
        if field == "deliverable_contains":
            blob = "{} {}".format(
                got.get("deliverable_en", ""), got.get("deliverable_original", "")
            ).lower()
            hits += 1 if str(want).lower() in blob else 0
        elif field == "due_at":
            have, want_s = norm_dt(got.get("due_at")), norm_dt(want)
            if have and want_s:
                hits += 1 if have.startswith(want_s) or want_s.startswith(have[:10]) else 0
        elif field == "amount":
            try:
                hits += 1 if abs(float(got.get("amount") or -1) - float(want)) < 0.01 else 0
            except (TypeError, ValueError):
                pass
        else:
            hits += 1 if got.get(field) == want else 0
    return hits, total


def score(case, parsed):
    """Greedy best-match each expected commitment against the returned set."""
    exp = case["expect"]
    got = parsed.get("commitments", []) if isinstance(parsed, dict) else []
    count_ok = len(got) == exp["count"]

    hits = total = 0
    pool = list(got)
    for e in exp["commitments"]:
        best, best_score, best_tot = None, -1, 0
        for cand in pool:
            h, t = match_one(e, cand)
            if h > best_score:
                best, best_score, best_tot = cand, h, t
        if best is not None:
            pool.remove(best)
            hits += best_score
            total += best_tot
        else:
            total += len(e)

    # evidence-span integrity: every returned span must exist in the message
    spans_ok = all(
        c.get("evidence_span", "") and c["evidence_span"] in case["message"] for c in got
    ) if got else False

    return {
        "count_ok": count_ok,
        "field_hits": hits,
        "field_total": total,
        "field_pct": (hits / total * 100) if total else 0.0,
        "spans_ok": spans_ok,
        "n_returned": len(got),
    }


# -------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="ollama", choices=["ollama", "anthropic"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--band", default=None)
    ap.add_argument("--show", default=None)
    ap.add_argument("--json", action="store_true",
                     help="also print a JSON_SUMMARY: line for machine consumption")
    args = ap.parse_args()

    model = args.model or ("qwen2.5:14b" if args.provider == "ollama" else "claude-haiku-4-5")
    caller = call_ollama if args.provider == "ollama" else call_anthropic

    cases = CASES["cases"]
    if args.band:
        cases = [c for c in cases if c["band"] == args.band]
    if args.show:
        cases = [c for c in cases if c["id"] == args.show]

    print("\n  provider {}   model {}   runs {}   cases {}\n".format(
        args.provider, model, args.runs, len(cases)))
    print("  {:<5} {:<15} {:>6} {:>7} {:>7} {:>7} {:>8}".format(
        "id", "band", "n", "count", "fields", "spans", "secs"))
    print("  " + "-" * 60)

    per_case, all_field_pcts, parse_fail = [], [], 0
    case_spans_ok, case_count_ok = [], []  # one bool per case (majority-of-runs), for --json

    for case in cases:
        prompt = build_prompt(case)
        run_pcts, run_counts, run_spans, run_times, run_n = [], [], [], [], []

        for _ in range(args.runs):
            t0 = time.time()
            try:
                raw = caller(prompt, model)
            except urllib.error.URLError as e:
                sys.exit("\n  Cannot reach {}: {}\n  Is Ollama running?".format(args.provider, e))
            elapsed = time.time() - t0

            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parse_fail += 1
                run_pcts.append(0.0); run_counts.append(False); run_spans.append(False)
                run_times.append(elapsed); run_n.append(0)
                if args.show:
                    print("\n  --- RAW (unparseable) ---\n{}\n".format(raw))
                continue

            if args.show:
                print("\n  --- RAW ---\n{}\n".format(json.dumps(parsed, ensure_ascii=False, indent=2)))

            s = score(case, parsed)
            run_pcts.append(s["field_pct"])
            run_counts.append(s["count_ok"])
            run_spans.append(s["spans_ok"])
            run_times.append(elapsed)
            run_n.append(s["n_returned"])

        avg_pct = statistics.mean(run_pcts)
        all_field_pcts.append(avg_pct)
        per_case.append((case, avg_pct))
        case_spans_ok.append(sum(run_spans) >= len(run_spans) / 2)
        case_count_ok.append(sum(run_counts) >= len(run_counts) / 2)

        print("  {:<5} {:<15} {:>6} {:>7} {:>6.0f}% {:>7} {:>8.1f}".format(
            case["id"], case["band"],
            "{:.0f}/{}".format(statistics.mean(run_n), case["expect"]["count"]),
            "{}/{}".format(sum(run_counts), len(run_counts)),
            avg_pct,
            "{}/{}".format(sum(run_spans), len(run_spans)),
            statistics.mean(run_times),
        ))

    print("  " + "-" * 60)
    print("  overall field accuracy   {:.1f}%".format(statistics.mean(all_field_pcts)))
    if parse_fail:
        print("  JSON parse failures      {}".format(parse_fail))

    by_band = {}
    for case, pct in per_case:
        by_band.setdefault(case["band"], []).append(pct)
    print("\n  by band:")
    for band, pcts in by_band.items():
        print("    {:<16} {:.1f}%".format(band, statistics.mean(pcts)))

    weak = [c["id"] for c, p in per_case if p < 70]
    if weak:
        print("\n  below 70% — candidates for frontier-model routing:")
        print("    " + ", ".join(weak))
    print()

    if args.json:
        summary = {
            "provider": args.provider,
            "model": model,
            "n_cases": len(cases),
            "overall_field_accuracy": statistics.mean(all_field_pcts) if all_field_pcts else 0.0,
            "by_band": {b: statistics.mean(pcts) for b, pcts in by_band.items()},
            "spans_ok": sum(case_spans_ok),
            "spans_total": len(case_spans_ok),
            "count_ok": sum(case_count_ok),
            "count_total": len(case_count_ok),
            "parse_failures": parse_fail,
            "weak_cases": weak,
        }
        # Unambiguous prefix, not "assume the last stdout line" — a
        # subprocess caller (app/observability/drift.py) greps for this
        # rather than parsing the human table above.
        print("JSON_SUMMARY:" + json.dumps(summary))


if __name__ == "__main__":
    main()
