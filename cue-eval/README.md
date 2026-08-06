# CUE extraction eval

Ten hand-labelled vendor messages that test the commitment-extraction tier. Runs against a local
Ollama model today and against a hosted model later, with the same cases and the same scoring —
so a number measured now is comparable to a number measured in production.

Stdlib only. No `pip install`.

## Run it

```bash
python3 run_eval.py                      # qwen2.5:14b via Ollama (default)
python3 run_eval.py --model qwen2.5:32b  # compare a larger local model
python3 run_eval.py --runs 5             # variance across repeated runs
python3 run_eval.py --band multi         # one band only
python3 run_eval.py --show T07           # print the raw JSON for one case
python3 run_eval.py --provider anthropic # needs ANTHROPIC_API_KEY
```

## Files

| File | What it is |
|---|---|
| `cases.json` | Project context + 10 labelled cases with expected extractions |
| `schema.json` | The JSON Schema passed to the model as a hard output constraint |
| `prompt.txt` | The extraction prompt template. **This is the artefact you tune** |
| `run_eval.py` | Runner and scorer |

## The four bands

| Band | Cases | Tests |
|---|---|---|
| `easy` | T01–T04 | Single commitment, clean EN or 中文 |
| `code-switched` | T05–T06 | Mixed EN/中文 in one message — normal in this domain |
| `multi` | T07–T08 | One message containing 2–3 distinct commitments |
| `ambiguous-date` | T09–T10 | Relative dates: "next Tuesday", 后天 |

## Scoring

- **n** — commitments returned / expected. Over-splitting is the most common failure
- **count** — runs where the count was exactly right
- **fields** — per-field accuracy against the labelled expectation
- **spans** — every `evidence_span` returned is a genuine substring of the message.
  This one must stay at 100%; it is the provenance guarantee (PRD FR-LED-04) and a
  failure here means the model is inventing evidence

## Baseline — qwen2.5:14b, 6 Aug 2026

```
overall field accuracy   83.3%
  easy             87.5%
  code-switched    83.3%
  multi            83.3%
  ambiguous-date   75.0%
spans                    10/10
latency                  ~8-25s per case (M3 Pro, warm)
```

Below 70% and therefore candidates for frontier-model routing: **T04, T05, T08, T09.**

## What this baseline already tells you

**Evidence spans held at 10/10.** The provenance guarantee survives contact with a small model —
the constraint is structural, not a matter of model capability.

**Multi-commitment extraction works.** T07 and T08 both returned exactly 3 objects. That was the
capability most at risk on a 14B, and it held.

**The weak band is `ambiguous-date` (75%)**, which is the expected result and matches the routing
rule already written into the PRD: relative-date resolution requiring a reach into the milestone
list is the case that should escalate to the frontier tier.

**Over-splitting is the residual failure mode** — T03, T04 and T05 returned 2 objects where 1 was
labelled. Worth reviewing whether the label or the model is right in each case before tuning
further; on T03 in particular ("proof approved **and** printed, panels delivered") a two-commitment
reading is defensible, and the label may be wrong rather than the model.

## The loop

1. Run the suite. Note which cases fail and how
2. Change **one thing** in `prompt.txt`
3. Re-run. Check you fixed the target case **without regressing others**
4. Repeat

Two real examples from building this:

> Fixing over-splitting on T01 caused the model to bury the timestamp in the deliverable text and
> drop `due_at` entirely — the field score stayed at 75% for a completely different reason. A rule
> forbidding times and prices inside `deliverable_*` fixed it and took T01 to 100%.

That is why you re-run the whole suite after every prompt change, never just the case you were
fixing.

## Before you trust a number

- **`--runs 5` before believing any single result.** One sample tells you nothing about variance
- **Never cite an Ollama figure to Pico.** Local numbers are for iteration. The thresholds in the
  proposal (0.92 commitment recall, 0.97 monetary precision) are measured against the models you
  actually ship, on the labelled corpus that comes out of discovery
- **These 10 cases are synthetic.** They are a smoke test and a regression gate, not an accuracy
  measurement. The real corpus comes from channel forensics during discovery — see proposal §4.1

## Where this goes next

- The same `cases.json` shape seeds `FixtureAdapter`, so the pipeline can be developed with no
  live channel
- Every human correction captured in production (PRD FR-LED-09) appends here, and the corpus
  compounds
- When you want a nicer harness, port these cases to Promptfoo and run it as a CI gate. The
  scoring logic in `run_eval.py` maps directly onto Promptfoo `javascript` assertions
