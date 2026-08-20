# CUE extraction eval

Hand-labelled vendor and internal messages that test the commitment-extraction tier. Runs against a local
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
| `cases.json` | Project context + labelled cases with expected extractions. A case may also carry `ledger_context`: the commitments already logged when that message arrived, rendered into the prompt exactly as `app/ledger/context.py` renders the real thing |
| `schema.json` | The JSON Schema passed to the model as a hard output constraint |
| `prompt.txt` | The extraction prompt template. **This is the artefact you tune** |
| `run_eval.py` | Runner and scorer |

## The bands

| Band | Cases | Tests |
|---|---|---|
| `easy` | T01–T04 | Single commitment, clean EN or 中文 |
| `code-switched` | T05–T06 | Mixed EN/中文 in one message — normal in this domain |
| `multi` | T07–T08 | One message containing 2–3 distinct commitments |
| `ambiguous-date` | T09–T10 | Relative dates: "next Tuesday", 后天 |
| `singlish` | S01–S05 | Sentence-final particles, bare-verb future, "X or not?" |
| `internal-channel-vendor` | IC01–IC02 | Internal channel; the vendor is named in the text, never the sender |
| `consequence-discussion` | CD01–CD02 | Staff discussing an already-logged commitment — must not become a new one |
| `merged-vendor` | CD03 | Two vendors, two deliverables, one message — must split into two |

## Scoring

Two numbers, because extraction fails in two opposite directions and one metric cannot see both.

- **n** — commitments returned / expected
- **count** — runs where the count was exactly right
- **fields** — per-field accuracy against the labelled expectation. Its denominator is the
  *labelled* fields, which makes it a recall-shaped number: it penalises a missed commitment
  and a wrong field, and is **structurally blind to an invented one** — an extra returned
  commitment matches nothing, so it adds nothing to the denominator and cannot lower the score.
  Kept because CLAUDE.md's baselines and `app/observability/drift.py`'s gate are recorded in it
- **P / R** — precision and recall over the returned *set*. An expected and a returned commitment
  count as the same one when at least half the labelled fields match (the conventional
  partial-match threshold for set-level extraction scoring); anything returned that matches
  nothing is **spurious**, anything expected that was not found is **missed**. Precision is the
  half `fields` cannot see, and inventing commitments is the failure that most directly destroys
  trust in a ledger
- **spurious / missed** — the raw counts behind P and R, summed across runs
- **spans** — every `evidence_span` returned is a genuine substring of the message.
  This one must stay at 100%; it is the provenance guarantee (PRD FR-LED-04) and a
  failure here means the model is inventing evidence

Expected/returned commitments are matched by an **exhaustive** best-total alignment, not greedily
in label order — greedy can let an early expected item consume a candidate a later one needed
more, understating a correct extraction. Alignment only chooses the pairing; it never changes how
a pair is scored.

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
- **These cases are synthetic.** They are a smoke test and a regression gate, not an accuracy
  measurement. The real corpus comes from channel forensics during discovery — see proposal §4.1

## Where this goes next

- The same `cases.json` shape seeds `FixtureAdapter`, so the pipeline can be developed with no
  live channel
- Every human correction captured in production (PRD FR-LED-09) appends here, and the corpus
  compounds
- When you want a nicer harness, port these cases to Promptfoo and run it as a CI gate. The
  scoring logic in `run_eval.py` maps directly onto Promptfoo `javascript` assertions
