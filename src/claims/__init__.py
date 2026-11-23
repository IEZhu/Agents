"""The validated-claims store: verdicts about factual claims, kept append-only and filtered.

Layer 7 of docs/delegation-reliability-plan.md, built on the verification levels the
thread-close flow already assigns. ``model`` turns a claim's verdicts into a confidence
with a Kalman filter; ``store`` keeps the verdicts per workspace. See
docs/validated-claims.md.
"""
