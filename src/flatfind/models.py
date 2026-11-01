"""Normalized listing model shared by all sources."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Listing:
    source: str  # "flatfox" | "homegate"
    source_id: str
    url: str
    title: str
    description: str = ""
    category: str = "other"  # apartment | house | room | parking | other
    rooms: float | None = None
    living_space_m2: float | None = None
    floor: int | None = None
    rent_gross: int | None = None  # CHF/month incl. charges
    rent_net: int | None = None
    rent_charges: int | None = None
    street: str | None = None
    zipcode: str | None = None
    city: str | None = None
    lat: float | None = None
    lon: float | None = None
    available_from: str | None = None  # ISO date, "immediately", "by agreement", or None
    is_furnished: bool | None = None
    is_temporary: bool | None = None
    is_top_floor: bool | None = None  # attic / roof flat or highest floor of the building
    features: list[str] = field(default_factory=list)  # canonical names, see sources.base.FEATURES
    images: list[str] = field(default_factory=list)
    published_at: str | None = None
    lister: str | None = None
    contact_url: str | None = None  # where to apply / request a viewing
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def id(self) -> str:
        return f"{self.source}:{self.source_id}"

    def to_dict(self, include_raw: bool = False) -> dict[str, Any]:
        d = {"id": self.id, **asdict(self)}
        if not include_raw:
            d.pop("raw")
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Listing:
        known = cls.__dataclass_fields__
        d = {k: v for k, v in d.items() if k in known}
        return cls(**d)
