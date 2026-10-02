from __future__ import annotations

import csv
import io
import sys
import urllib.request
from pathlib import Path

URL = (
    "https://www.kdmid.ru/opendata/7704206201-country/"
    "data-20230119-structure-20200514.csv"
)
TARGET = Path("data/kdmid_entry_rules.csv")


def main() -> int:
    request = urllib.request.Request(
        URL,
        headers={"User-Agent": "A_TicketBot/1.0 (+GitHub Actions snapshot)"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        content_type = response.headers.get("Content-Type", "")
        raw = response.read()
    print(f"KDMID response: content_type={content_type!r}, bytes={len(raw)}")

    text = None
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            candidate = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        lowered = candidate.lower()
        if "страна" in lowered and "общеграждан" in lowered:
            text = candidate
            break
    if text is None:
        prefix = raw[:240]
        raise RuntimeError(
            "Could not decode KDMID CSV as UTF-8 or Windows-1251; "
            f"prefix={prefix!r}"
        )
    sample = text[:8192]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=";,\t").delimiter
    except csv.Error:
        delimiter = ";"

    rows = list(csv.DictReader(io.StringIO(text), delimiter=delimiter))
    if len(rows) < 100:
        raise RuntimeError(f"KDMID snapshot unexpectedly small: {len(rows)} rows")

    headers = [str(h or "").lower() for h in rows[0].keys()]
    if "country" not in headers and not any("страна" in h for h in headers):
        raise RuntimeError("KDMID snapshot has no country column")
    if "citizen" not in headers and not any("общеграждан" in h for h in headers):
        raise RuntimeError("KDMID snapshot has no ordinary-passport column")

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_bytes(raw)
    print(f"Saved {len(rows)} KDMID country rows to {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
