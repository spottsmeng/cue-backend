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

import argparse, itertools, json, os, statistics, sys, time, urllib.error, urllib.request
from pathlib import Path

HERE = Path(__file__).parent
CASES = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
SCHEMA = json.loads((HERE / "schema.json").read_text(encoding="utf-8"))
TEMPLATE = (HERE / "prompt.txt").read_text(encoding="utf-8")

OLLAMA_URL = "http://localhost:11434/api/generate"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


# ----------------------------------------------------------------- prompt ---
def render_ledger_context(case):
    """A case's optional "ledger_context": the commitments already logged when
    this message arrived, rendered exactly as app/ledger/context.py's
    render_ledger_context renders the real thing. Kept as a hand-written
    duplicate rather than an import for the same reason this whole script is
    stdlib-only — it must run with no app package and no database — but the
    two formats have to stay identical or the eval stops measuring what
    production sends. Cases without the key get the same "(none)" the real
    renderer produces for an empty ledger.
    """
    items = case.get("ledger_context") or []
    if not items:
        return "  (none — nothing has been logged for this project yet)"
    lines = []
    for i, item in enumerate(items, start=1):
        parts = ["{}: {}".format(item["vendor"], item["deliverable_en"])]
        if item.get("due_at"):
            parts.append("due {}".format(item["due_at"]))
        if item.get("amount"):
            parts.append(item["amount"])
        parts.append(item.get("state", "proposed"))
        lines.append("  C{} — {}".format(i, ", ".join(parts)))
    return "\n".join(lines)


def case_schema(case):
    """SCHEMA with `relates_to` narrowed to the refs this case actually offers
    — the same per-call enum injection app/ledger/schema.py's
    build_extraction_json_schema does in production. With no context the enum
    is [None], so the model cannot name a commitment that does not exist."""
    schema = json.loads(json.dumps(SCHEMA))  # cheap deep copy, stdlib only
    n = len(case.get("ledger_context") or [])
    props = schema["properties"]["commitments"]["items"]["properties"]
    # `type` is dropped alongside the enum for the same reason production does
    # it (app/ledger/schema.py's build_extraction_json_schema): Anthropic's
    # structured-outputs validator rejects a null enum value against a
    # ["string","null"] union, while Ollama accepts it happily. Keeping this
    # identical to production is the only reason that bug was findable here.
    props["relates_to"].pop("type", None)
    props["relates_to"]["enum"] = [None] + ["C{}".format(i) for i in range(1, n + 1)]
    return schema


def build_prompt(case):
    ctx = CASES["project_context"]
    milestones = "\n".join(
        "  - {}: {}".format(m["name"], m["due"]) for m in ctx["known_milestones"]
    )
    weekday = case.get("sent_weekday")
    return TEMPLATE.format(
        open_commitments=render_ledger_context(case),
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


def call_ollama(prompt, model, schema=None):
    body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "2h",
        "format": schema or SCHEMA,
        "options": {"num_ctx": 16384, "temperature": 0},
    }
    out = post(OLLAMA_URL, body, {"Content-Type": "application/json"})
    return out["response"]


def call_anthropic(prompt, model, schema=None):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY is not set.")
    body = {
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"format": {"type": "json_schema", "schema": schema or SCHEMA}},
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
        elif field == "counterparty_contains":
            blob = (got.get("counterparty_name") or "").lower()
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


# A returned commitment has to match at least this fraction of an expected
# commitment's labelled fields before the two are treated as *the same
# commitment* for precision/recall. Below it, the pair is not a detection at
# all: the expected one counts as missed, the returned one as spurious. 0.5 is
# the conventional partial-match threshold for set-level extraction scoring
# (MUC/ACE-style), chosen so that e.g. matching only `act_type` out of
# {act_type, due_at, deliverable_contains} is not scored as having found that
# commitment.
_MATCH_THRESHOLD = 0.5
# Above this many items on either side, the exhaustive alignment below is
# skipped for the greedy one. Real cases top out at 3 expected / a handful
# returned, so this is a guard against a pathological model response
# (hundreds of items), not a limit anything normal hits.
_MAX_EXACT_ALIGNMENT = 7


def _pair_matrix(expected, got):
    """matrix[i][j] = (hits, total) for expected item i against returned item j."""
    return [[match_one(e, c) for c in got] for e in expected]


def _align(expected, got):
    """One-to-one assignment of expected -> returned items, maximising total
    field hits. Returns a list, one entry per expected item, holding the index
    of the returned item it was matched to (or None if unmatched).

    Exhaustive rather than greedy. Greedy resolves each expected item against
    the best remaining candidate *in label order*, which can consume a
    candidate that a later expected item needed more — understating a
    correct extraction. Both are heuristics for the same objective; with the
    case sizes here (<=3 expected) the exhaustive one is a few hundred
    permutations, so there is no reason to approximate it.

    Note this only changes which pairing is chosen, never how a pair is
    scored — so a field-accuracy number from this function can be equal to or
    higher than the greedy one it replaces, never lower.
    """
    matrix = _pair_matrix(expected, got)
    n_exp, n_got = len(expected), len(got)
    if not n_exp or not n_got:
        return [None] * n_exp

    if n_exp <= _MAX_EXACT_ALIGNMENT and n_got <= _MAX_EXACT_ALIGNMENT:
        best_total, best_assignment = -1, [None] * n_exp
        for combo in itertools.permutations(range(n_got), min(n_exp, n_got)):
            assignment = list(combo) + [None] * (n_exp - len(combo))
            total = sum(matrix[i][j][0] for i, j in enumerate(assignment) if j is not None)
            if total > best_total:
                best_total, best_assignment = total, assignment
        return best_assignment

    assignment, taken = [], set()
    for i in range(n_exp):
        best_j, best_h = None, -1
        for j in range(n_got):
            if j in taken:
                continue
            if matrix[i][j][0] > best_h:
                best_j, best_h = j, matrix[i][j][0]
        if best_j is not None:
            taken.add(best_j)
        assignment.append(best_j)
    return assignment


def score(case, parsed):
    """Set-level scoring of one returned commitment list against the labels.

    Reports two different things on purpose, because they fail in opposite
    directions and this suite exists to catch both (see cue-eval/README.md's
    own note on over- vs under-splitting):

    `field_pct` — how well the commitments that *should* exist were filled in.
    Denominator is the labelled fields, so it is a recall-shaped number: it
    penalises a missed commitment and a wrong field, but is structurally blind
    to a *spurious* one, since an extra returned item adds nothing to the
    denominator. Kept because it is the number CLAUDE.md's own baselines and
    app/observability/drift.py's regression gate are recorded in.

    `precision` / `recall` / `f1` — how well the returned *set* matches the
    expected set, counting an unmatched returned item as a false positive.
    This is the half `field_pct` cannot see: a model that invents a commitment
    out of a qualifying remark scores an unchanged field_pct and a visibly
    lower precision. `spurious` / `missed` are the raw counts behind it, for
    reading a single case.
    """
    exp = case["expect"]
    got = parsed.get("commitments", []) if isinstance(parsed, dict) else []
    expected = exp["commitments"]
    count_ok = len(got) == exp["count"]

    # Every returned span must exist verbatim in the message — the one
    # invariant that is enforced in code downstream too (CLAUDE.md, and
    # app/ledger/extractor.py's own RejectedExtraction). A case that correctly
    # returns nothing is vacuously span-clean; one that returns nothing when
    # something was expected is not being judged on spans at all, so the
    # historical False for that case is kept rather than quietly relaxed.
    spans_ok = all(
        c.get("evidence_span", "") and c["evidence_span"] in case["message"] for c in got
    ) if got else (exp["count"] == 0)

    assignment = _align(expected, got)

    hits = total = 0
    true_positives = 0
    matched_returned = set()
    for i, e in enumerate(expected):
        j = assignment[i]
        if j is None:
            total += len(e)
            continue
        h, t = match_one(e, got[j])
        hits += h
        total += t
        if t and (h / t) >= _MATCH_THRESHOLD:
            true_positives += 1
            matched_returned.add(j)

    spurious = len(got) - len(matched_returned)
    missed = len(expected) - true_positives

    if exp["count"] == 0:
        # Nothing to field-match against, so field_pct is just "did it
        # correctly emit nothing" — falling through would divide 0/0 and score
        # 0% even when got == [] is right. Precision is likewise undefined with
        # no expected items; reported as 1.0 when the model correctly returned
        # none, 0.0 when it invented some, which is what the aggregate below
        # needs in order to let these cases pull precision down at all.
        field_pct = 100.0 if count_ok else 0.0
        precision = 1.0 if not got else 0.0
        recall = 1.0
    else:
        field_pct = (hits / total * 100) if total else 0.0
        precision = (true_positives / len(got)) if got else 0.0
        recall = true_positives / len(expected)

    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    return {
        "count_ok": count_ok,
        "field_hits": hits,
        "field_total": total,
        "field_pct": field_pct,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "spurious": spurious,
        "missed": missed,
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
    print("  {:<5} {:<15} {:>6} {:>7} {:>7} {:>6} {:>5} {:>7} {:>8}".format(
        "id", "band", "n", "count", "fields", "P", "R", "spans", "secs"))
    print("  " + "-" * 78)

    per_case, all_field_pcts, parse_fail = [], [], 0
    case_spans_ok, case_count_ok = [], []  # one bool per case (majority-of-runs), for --json
    all_precisions, all_recalls = [], []
    total_spurious = total_missed = 0

    for case in cases:
        prompt = build_prompt(case)
        schema = case_schema(case)
        run_pcts, run_counts, run_spans, run_times, run_n = [], [], [], [], []
        run_prec, run_rec, run_spur, run_miss = [], [], [], []

        for _ in range(args.runs):
            t0 = time.time()
            try:
                raw = caller(prompt, model, schema)
            except urllib.error.URLError as e:
                sys.exit("\n  Cannot reach {}: {}\n  Is Ollama running?".format(args.provider, e))
            elapsed = time.time() - t0

            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parse_fail += 1
                run_pcts.append(0.0); run_counts.append(False); run_spans.append(False)
                run_times.append(elapsed); run_n.append(0)
                run_prec.append(0.0); run_rec.append(0.0)
                run_spur.append(0); run_miss.append(case["expect"]["count"])
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
            run_prec.append(s["precision"]); run_rec.append(s["recall"])
            run_spur.append(s["spurious"]); run_miss.append(s["missed"])

        avg_pct = statistics.mean(run_pcts)
        avg_prec, avg_rec = statistics.mean(run_prec), statistics.mean(run_rec)
        all_field_pcts.append(avg_pct)
        all_precisions.append(avg_prec)
        all_recalls.append(avg_rec)
        total_spurious += sum(run_spur)
        total_missed += sum(run_miss)
        per_case.append((case, avg_pct, avg_prec, avg_rec))
        case_spans_ok.append(sum(run_spans) >= len(run_spans) / 2)
        case_count_ok.append(sum(run_counts) >= len(run_counts) / 2)

        print("  {:<5} {:<15} {:>6} {:>7} {:>6.0f}% {:>6.2f} {:>5.2f} {:>7} {:>8.1f}".format(
            case["id"], case["band"],
            "{:.0f}/{}".format(statistics.mean(run_n), case["expect"]["count"]),
            "{}/{}".format(sum(run_counts), len(run_counts)),
            avg_pct,
            avg_prec, avg_rec,
            "{}/{}".format(sum(run_spans), len(run_spans)),
            statistics.mean(run_times),
        ))

    macro_p = statistics.mean(all_precisions) if all_precisions else 0.0
    macro_r = statistics.mean(all_recalls) if all_recalls else 0.0
    macro_f1 = (2 * macro_p * macro_r / (macro_p + macro_r)) if (macro_p + macro_r) else 0.0

    print("  " + "-" * 78)
    print("  overall field accuracy   {:.1f}%".format(statistics.mean(all_field_pcts)))
    print("  precision / recall / F1  {:.3f} / {:.3f} / {:.3f}".format(macro_p, macro_r, macro_f1))
    print("  spurious / missed        {} / {}   (across {} runs)".format(
        total_spurious, total_missed, args.runs))
    print("  count exactly right      {}/{} cases".format(sum(case_count_ok), len(case_count_ok)))
    if parse_fail:
        print("  JSON parse failures      {}".format(parse_fail))

    by_band = {}
    for case, pct, _p, _r in per_case:
        by_band.setdefault(case["band"], []).append(pct)
    print("\n  by band:")
    for band, pcts in by_band.items():
        print("    {:<16} {:.1f}%".format(band, statistics.mean(pcts)))

    weak = [c["id"] for c, p, _p, _r in per_case if p < 70]
    if weak:
        print("\n  below 70% field accuracy — candidates for frontier-model routing:")
        print("    " + ", ".join(weak))
    # Split out separately from `weak`: a case can be 100% on field accuracy
    # and still be inventing commitments, which is exactly the failure that
    # number cannot see (see score()'s docstring).
    impure = [c["id"] for c, _pct, p, _r in per_case if p < 0.99]
    if impure:
        print("\n  precision < 1.0 — returning commitments that were not expected:")
        print("    " + ", ".join(impure))
    print()

    if args.json:
        summary = {
            "provider": args.provider,
            "model": model,
            "n_cases": len(cases),
            "overall_field_accuracy": statistics.mean(all_field_pcts) if all_field_pcts else 0.0,
            "precision": macro_p,
            "recall": macro_r,
            "f1": macro_f1,
            "spurious": total_spurious,
            "missed": total_missed,
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
