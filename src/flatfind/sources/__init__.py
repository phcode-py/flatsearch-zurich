from flatfind.sources import flatfox, homegate
from flatfind.sources.base import Source

SOURCES: dict[str, Source] = {s.NAME: s for s in (flatfox, homegate)}
