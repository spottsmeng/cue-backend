import json
from pathlib import Path
from typing import TypedDict

from seed_data.channel_types import CHANNEL_TYPES

# cue-eval/README.md: "The same cases.json shape seeds a FixtureAdapter, so the
# pipeline can be developed with no live channel." This is that adapter.
# Loaded from disk, same as app/ledger/schema.py — one source, not a copy.
_CUE_EVAL_DIR = Path(__file__).resolve().parents[2] / "cue-eval"

# cases.json's "channel" field is a channel_types.code (whatsapp, mattermost,
# ...), not a capability — the same code -> capability mapping seed_data/
# channel_types.py seeds into the real table, reused here rather than
# duplicated, since a fixture case has no DB row to look one up from.
_CAPABILITY_BY_CODE: dict[str, str | None] = dict(CHANNEL_TYPES)


class FixtureCase(TypedDict):
    id: str
    band: str
    lang: str
    channel: str
    party: str
    sent_at: str
    message: str
    sent_weekday: str | None
    channel_capability: str | None


class ProjectContext(TypedDict):
    project: str
    client: str
    timezone: str
    venue: str
    build_up: list[str]
    event_days: list[str]
    doors: str
    known_milestones: list[dict]
    vendors: list[dict]


def load_fixture_cases(band: str | None = None) -> tuple[ProjectContext, list[FixtureCase]]:
    """Real capture (WhatsApp/WeChat/Graph) is Phase 1, discovery-dependent —
    not built yet. This stands in for it during development, using the exact
    same labelled cases the eval harness scores against."""
    data = json.loads((_CUE_EVAL_DIR / "cases.json").read_text(encoding="utf-8"))
    cases = data["cases"]
    if band:
        cases = [c for c in cases if c["band"] == band]
    for case in cases:
        case["channel_capability"] = _CAPABILITY_BY_CODE.get(case["channel"])
    return data["project_context"], cases
