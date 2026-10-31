import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.scenarios import ScenarioSnapshot, snapshot_from_dict  # noqa: E402

D01 = json.loads((ROOT / "scenarios" / "D01.json").read_text())


def scenario(mutate=None) -> ScenarioSnapshot:
    """The Appendix A fixture, optionally edited by `mutate(raw_dict)` before validation."""
    raw = copy.deepcopy(D01)
    if mutate:
        mutate(raw)
    return snapshot_from_dict(raw)


def result(results, rule_id):
    return next(r for r in results if r.rule_id == rule_id)
