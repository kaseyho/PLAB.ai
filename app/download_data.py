"""Fetch the organizer CSV for hosting and verify its exact released bytes."""

import hashlib
import os
import shutil
import urllib.request
from pathlib import Path

CSV_SHA256 = "bfb7fc2112a1a98b748fa1c5bbba57f6f8f14acbd6419ea4cf84f4470ed5d79e"
DEFAULT_URL = "https://github.com/kaseyho/PLAB.ai/releases/download/organizer-data-v1/flights.csv"


def main() -> None:
    target = Path(os.getenv("PLAB_DATA_DIR", "data")) / "flights.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        with target.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() == CSV_SHA256:
                print("Organizer flight data verified.")
                return
    url = os.getenv("PLAB_FLIGHTS_URL", DEFAULT_URL)
    if not url.startswith("https://"):
        raise ValueError("The flight-data download must use HTTPS")
    temporary = target.with_suffix(".download")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        with temporary.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != CSV_SHA256:
                raise ValueError("Flight data differs from the verified organizer release")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    print("Organizer flight data downloaded and verified.")


if __name__ == "__main__":
    main()
