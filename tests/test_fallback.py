"""Deterministic fallback tests. Expected catalogs are hand-derived from scenarios/D01.json."""

from app.fallback import enumerate_candidates, evaluate_catalog, select_fallback
from tests.conftest import scenario

# Hand derivation for D01 (60 s grid, 600..2400 s):
#   separation with BG2 at 780 s allows landing 600 s or >= 960 s;
#   G2-route: fuel margin 540 - 50*hold_min >= 0  -> hold <= 10.8 min -> 600, 960..1200 (6 slots)
#   G1-long:  gate free at 1500 needs >= 960; fuel 520 - 50*hold >= 0 -> <= 1200 (5 slots)
#   G1-short: gate free at 1500 needs >= 1200; fuel 600 - 50*hold >= 0 -> <= 1320 (3 slots)
EXPECTED_FEASIBLE = (
    {f"L{t}-G2-route" for t in (600, 960, 1020, 1080, 1140, 1200)}
    | {f"L{t}-G1-long" for t in (960, 1020, 1080, 1140, 1200)}
    | {f"L{t}-G1-short" for t in (1200, 1260, 1320)}
)


def test_catalog_is_bounded_and_matches_hand_derived_feasible_set():
    catalog = evaluate_catalog(scenario())
    assert len(catalog) == 31 * 3  # inclusive 30-minute window at 60 s, three declared routes
    assert {c.plan.candidate_id for c in catalog if c.feasible} == EXPECTED_FEASIBLE


def test_enumeration_order_is_stable():
    first = [p.candidate_id for p in enumerate_candidates(scenario())]
    assert first == [p.candidate_id for p in enumerate_candidates(scenario())]
    assert first[:3] == ["L600-G1-long", "L600-G1-short", "L600-G2-route"]


def test_fallback_picks_earliest_feasible_plan_with_zero_model_calls():
    outcome = select_fallback(scenario(), "PILOT rejected in round 2")
    assert outcome.status == "FEASIBLE_FALLBACK"
    assert outcome.plan.candidate_id == "L600-G2-route"  # Appendix A.3: fallback returns C1
    assert outcome.fallback_reason == "PILOT rejected in round 2"
    assert outcome.model_calls == 0
    assert all(check.status == "PASS" for check in outcome.checks)
    assert outcome.feasible_count == len(EXPECTED_FEASIBLE)


def test_fallback_is_deterministic():
    assert select_fallback(scenario(), "timeout") == select_fallback(scenario(), "timeout")


def test_no_fuel_envelope_gives_no_feasible_option_not_least_unsafe():
    # Appendix A.3 variant: 2000 kg projected is below the 2300 kg floor even at 600 s.
    outcome = select_fallback(
        scenario(lambda raw: raw["overlay"]["fuel"].update(projected_fuel_at_earliest_kg=2000)),
        "dissent",
    )
    assert outcome.status == "NO_FEASIBLE_OPTION_IN_SEARCH_SPACE"
    assert outcome.plan is None
    assert outcome.blockers["PILOT-FUEL-01"] == outcome.evaluated_count == 93
    assert (outcome.search_bounds.earliest_landing_s, outcome.search_bounds.latest_landing_s) == (
        600,
        2400,
    )


def test_both_gates_unavailable_gives_no_feasible_option():
    def block_gates(raw):
        for gate in raw["overlay"]["gates"]:
            gate["reservations"] = [[0, 86400]]

    outcome = select_fallback(scenario(block_gates), "dissent")
    assert outcome.status == "NO_FEASIBLE_OPTION_IN_SEARCH_SPACE"
    assert outcome.blockers["GROUND-OCC-01"] == 93


def test_missing_fuel_gives_insufficient_data_before_enumeration():
    outcome = select_fallback(
        scenario(lambda raw: raw["overlay"]["fuel"].update(projected_fuel_at_earliest_kg=None)),
        "timeout",
    )
    assert outcome.status == "INSUFFICIENT_DATA"
    assert outcome.missing_facts == ["fuel.projected_fuel_at_earliest_kg"]
    assert outcome.plan is None and outcome.evaluated_count == 0


def test_unknown_rule_applicability_gives_insufficient_data_not_no_option():
    outcome = select_fallback(
        scenario(lambda raw: raw["overlay"].update(separation_s={})), "timeout"
    )
    assert outcome.status == "INSUFFICIENT_DATA"
    assert outcome.plan is None


def test_checker_fault_gives_system_error(monkeypatch):
    def broken(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.fallback.check_plan", broken)
    outcome = select_fallback(scenario(), "timeout")
    assert outcome.status == "SYSTEM_ERROR"
    assert outcome.plan is None


def test_less_fuel_never_adds_feasible_candidates():
    for fuel in (3000, 2800, 2600, 2400, 2300):
        richer = feasible_ids(scenario(_fuel(fuel)))
        poorer = feasible_ids(scenario(_fuel(fuel - 100)))
        assert poorer <= richer


def test_longer_gate_occupation_never_adds_feasible_candidates():
    for release in (1500, 1600, 1800, 2400):
        shorter = feasible_ids(scenario(_g1_release(release)))
        longer = feasible_ids(scenario(_g1_release(release + 120)))
        assert longer <= shorter


def feasible_ids(snapshot):
    return {c.plan.candidate_id for c in evaluate_catalog(snapshot) if c.feasible}


def _fuel(kg):
    return lambda raw: raw["overlay"]["fuel"].update(projected_fuel_at_earliest_kg=kg)


def _g1_release(end):
    return lambda raw: raw["overlay"]["gates"][0].update(reservations=[[0, end]])
