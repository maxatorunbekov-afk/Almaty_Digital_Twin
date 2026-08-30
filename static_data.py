from __future__ import annotations

from .domain import ConsumerReading, Snapshot, utc_now


RESEARCH_DATA = {
    "183 Kazybek Bi St": (0.373059, 0.141456, 8.2, 4.0),
    "30 Isaeva St": (0.235181, 0.060480, 7.8, 3.7),
    "129 Chokina St": (0.470968, 0.100389, 7.8, 3.7),
    "194 Aiteke Bi St": (0.305275, 0.073440, 7.5, 3.5),
    "28 Isaeva St": (0.221234, 0.056160, 7.8, 3.7),
}


def research_snapshot(source: str = "research_table") -> Snapshot:
    return Snapshot(
        timestamp=utc_now(),
        source=source,
        consumers={
            address: ConsumerReading(address, heating, dhw, p1, p2)
            for address, (heating, dhw, p1, p2) in RESEARCH_DATA.items()
        },
    )

