"""The owner's oversight surface: a static, self-contained status page
generated from data already on disk.

`contract.py` is the part worth reading first. It answers "what may a status
surface claim?" before anything renders, because a dashboard is a trust
surface and the failure that matters is not an ugly page — it is a green one
with nothing behind it.
"""

from aef.dash.contract import (
    FORBIDDEN_HTML_CONSTRUCTS,
    PANELS,
    Disclosure,
    DisclosureError,
    Known,
    Panel,
    PanelSpec,
    PanelState,
    Reading,
    Unknown,
    UnknownReason,
    disclosure_of,
    panel_spec,
)

__all__ = [
    "FORBIDDEN_HTML_CONSTRUCTS",
    "PANELS",
    "Disclosure",
    "DisclosureError",
    "Known",
    "Panel",
    "PanelSpec",
    "PanelState",
    "Reading",
    "Unknown",
    "UnknownReason",
    "disclosure_of",
    "panel_spec",
]
