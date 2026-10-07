"""The self-rewiring harness (Zone B).

Everything in this package judges agent-authored candidates: zone
classification, candidate-diff extraction, the base-ref trust boundary, and
the sandbox. None of it is ever agent-writable — see `zones.py` and ADR 0044.
"""
