"""Strict, read-only import of the organizer workbook and hash-bound flight records.

The workbook is authoritative for limits, not for a live airport state. Unsupported
labels, units, formulas, duplicate airports and missing values fail closed.
"""

import csv
import hashlib
import re
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from app.contracts import ScenarioSnapshot, ServiceError
from app.scenarios import WorkbookPolicy

WORKBOOK_NAME = "challenge2_operational_constraints.xlsx"
FLIGHT_ID_FIELDS = (
    "YEAR",
    "MONTH",
    "DAY",
    "AIRLINE",
    "FLIGHT_NUMBER",
    "TAIL_NUMBER",
    "ORIGIN_AIRPORT",
    "DESTINATION_AIRPORT",
    "SCHEDULED_DEPARTURE",
)
AIRPORT_HEADERS = (
    "IATA_CODE",
    "AIRPORT",
    "CITY",
    "STATE",
    "delayed_departures_in_flights_csv",
    "size_class",
    "active_runways",
    "gates",
    "max_runway_ops_per_hour",
    "min_turnaround_min",
    "spare_gates_at_any_time",
)
WAKE_CLASSES = {"Super (A380)": "S", "Heavy": "H", "Large": "M", "Small": "L"}


def file_hash(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def verify_source_files(snapshot: ScenarioSnapshot, data_dir: Path) -> None:
    """Block a run or approval if its hash-bound files changed since startup."""
    policy = next(
        (f.value for f in snapshot.facts if f.fact_id == "policy.operational_limits"), None
    )
    if policy is None:
        return  # Legacy test scenarios are explicitly separate from the submission profile.
    limits = WorkbookPolicy.model_validate(policy)
    for filename, expected in (
        (snapshot.source.dataset_id, snapshot.source.dataset_sha256),
        (limits.workbook_file, limits.workbook_sha256),
    ):
        path = (data_dir / filename).resolve()
        if not path.is_relative_to(data_dir.resolve()):
            raise ServiceError("SOURCE_PATH_OUTSIDE_DATA_DIRECTORY", 422)
        if not path.is_file():
            raise ServiceError("SOURCE_FILE_NOT_FOUND", 503)
        if file_hash(path) != expected:
            raise ServiceError("SOURCE_FILE_CHANGED", 409)


def positive_integer(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("Workbook limits must be positive integer values")
    return value


def read_workbook(path: Path, airport: str) -> WorkbookPolicy:
    if not path.is_file():
        raise ServiceError("WORKBOOK_FILE_NOT_FOUND", 503)
    book = load_workbook(path, read_only=True, data_only=False)
    try:
        airport_rows = list(book["airport_capacity"].values)
        if tuple(airport_rows[2]) != AIRPORT_HEADERS:
            raise ValueError("Unsupported airport capacity headers")
        records = airport_rows[3:]
        codes = [row[0] for row in records]
        if len(codes) != len(set(codes)):
            raise ValueError("Duplicate airport code")
        matches = [(index, row) for index, row in enumerate(records, 4) if row[0] == airport]
        if len(matches) != 1:
            raise ValueError("Airport absent from workbook")
        row_number, row = matches[0]
        numbers = [positive_integer(value) for value in row[6:11]]
        runways, gates, capacity, turnaround_min, spare = numbers
        if spare > gates:
            raise ValueError("Spare gates exceed airport inventory")

        wake_rows = list(book["wake_separation"].values)
        if tuple(wake_rows[2]) != (
            "leader",
            "follower",
            "min_separation_nm",
            "min_time_s (same runway departures)",
        ):
            raise ValueError("Unsupported wake units or scope")
        default = [r for r in wake_rows[3:] if r[0] == "Any other pair"]
        if len(default) != 1 or default[0][1] is not None:
            raise ValueError("Missing default wake pair")
        seconds = {
            f"{a}>{b}": positive_integer(default[0][3])
            for a in WAKE_CLASSES.values()
            for b in WAKE_CLASSES.values()
        }
        nautical_miles = {key: positive_integer(default[0][2]) for key in seconds}
        seen = set()
        for leader, follower, distance, duration in wake_rows[3:]:
            if leader == "Any other pair":
                continue
            key = f"{WAKE_CLASSES[str(leader)]}>{WAKE_CLASSES[str(follower)]}"
            if key in seen:
                raise ValueError("Duplicate wake pair")
            seen.add(key)
            seconds[key] = positive_integer(duration)
            nautical_miles[key] = positive_integer(distance)

        fuel_rows = list(book["fuel_and_crew"].values)
        if tuple(fuel_rows[0]) != ("rule", "value", "applies_to"):
            raise ValueError("Unsupported fuel/crew headers")
        rules = {r[0]: (i, r[1]) for i, r in enumerate(fuel_rows[1:], 2)}
        if len(rules) != len(fuel_rows) - 1:
            raise ValueError("Duplicate fuel/crew rule")

        def number(label: str, pattern: str) -> int:
            match = re.fullmatch(pattern, str(rules[label][1]))
            if match is None:
                raise ValueError(f"Unsupported units or text for {label}")
            return positive_integer(int(match[1].replace(",", "")))

        shift = re.fullmatch(
            r"(\d+) hours; overtime costs (\d+(?:\.\d+)?)x after that",
            str(rules["Ground crew shift"][1]),
        )
        if shift is None:
            raise ValueError("Unsupported ground shift units")
        return WorkbookPolicy(
            workbook_file=path.name,
            workbook_sha256=file_hash(path),
            airport_code=airport,
            active_runways=runways,
            gates=gates,
            spare_gates=spare,
            max_ops_per_hour=capacity,
            min_turnaround_s=turnaround_min * 60,
            separation_s=seconds,
            separation_nm=nautical_miles,
            reserve_hold_min=number("Final fuel reserve", r"(\d+) minutes of holding fuel"),
            contingency_percent=number("Contingency fuel", r"(\d+)% of trip fuel"),
            narrowbody_hold_kg_per_hour=number(
                "Holding fuel burn (narrowbody)", r"([\d,]+) kg per hour"
            ),
            widebody_hold_kg_per_hour=number(
                "Holding fuel burn (widebody)", r"([\d,]+) kg per hour"
            ),
            spare_hold_min=number(
                "Assumed spare fuel on arrival",
                r"(\d+) minutes of holding \(unless the team states otherwise\)",
            ),
            flight_crew_duty_s=number("Flight crew max duty", r"(\d+) hours from report time")
            * 3600,
            ground_shift_s=int(shift[1]) * 3600,
            overtime_multiplier=float(shift[2]),
            gate_notice_s=number("Gate change notice", r"At least (\d+) minutes before arrival")
            * 60,
            source_cells={
                "airport": f"airport_capacity!A{row_number}:K{row_number}",
                "wake": "wake_separation!A3:D11",
                **{
                    str(label): f"fuel_and_crew!A{index}:C{index}"
                    for label, (index, _) in rules.items()
                },
            },
        )
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise ServiceError("WORKBOOK_SCHEMA_INVALID", 422) from error
    finally:
        book.close()


def profile_flights(path: Path) -> dict[str, Any]:
    """Streaming release checks; raw rows never become an airport movement schedule."""
    count, invalid, cancelled, diverted = 0, 0, 0, 0
    with path.open(newline="") as source:
        reader = csv.DictReader(source)
        required = set(FLIGHT_ID_FIELDS) | {
            "WEATHER_DELAY",
            "LATE_AIRCRAFT_DELAY",
            "CANCELLED",
            "DIVERTED",
        }
        if not required.issubset(reader.fieldnames or []):
            raise ServiceError("FLIGHT_SCHEMA_INVALID", 422)
        for row in reader:
            count += 1
            try:
                qualifies = (
                    float(row["WEATHER_DELAY"] or 0) > 15
                    or float(row["LATE_AIRCRAFT_DELAY"] or 0) > 20
                )
                invalid += int(row["YEAR"] != "2015" or not qualifies)
            except ValueError:
                invalid += 1
            cancelled += int(row["CANCELLED"] != "0")
            diverted += int(row["DIVERTED"] != "0")
    return {
        "file": path.name,
        "sha256": file_hash(path),
        "rows": count,
        "invalid_year_or_filter": invalid,
        "cancelled": cancelled,
        "diverted": diverted,
    }
