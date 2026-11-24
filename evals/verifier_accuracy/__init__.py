"""Measuring verifier accuracy on a known-answer control set.

The first stage of docs/verifier-accuracy-measurement.md: the set builder
(`builder`, `strata`), the control directory and its mandatory attempt recorder
(`control`), the sandboxed worker exchange (`worker`), level assignment from the
host-recorded transcript (`transcript`), the runner with its canary probe (`runner`)
and the analysis with its coverage simulation (`analysis`). The command line is
``evals/scripts/verifier_accuracy.py``; flows/verifier-accuracy.md says when to run
each step. No statement or label of a set is ever written inside the repository:
they live only in the control directory under the service-state directory.
"""
