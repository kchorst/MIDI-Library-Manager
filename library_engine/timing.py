from __future__ import annotations

"""Authoritative runtime sequencing and responsiveness budgets.

These are control-plane deadlines, not MIDI parsing safety limits.  The latter
remain deliberately generous so a pathological source is isolated instead of
crashing the Engine.  Interactive lifecycle transitions preempt supervised
analyzer subprocesses and therefore must complete on a much shorter budget.
"""

# User-facing foreground work must own the analyzer/DB coordination budget
# before it begins parsing.  This covers pausing Deep work and waiting for its
# queue state to acknowledge supervised worker interruption.
FAST_PRIORITY_TRANSITION_SECONDS = 5.0

# Recommendation preparation has no analyzer subprocess to kill, so it receives
# a little more time to finish its current atomic candidate/DB boundary.  A Fast
# scan does not start while this background writer is still live.
RELATIONSHIP_YIELD_SECONDS = 5.0

# Pause/Cancel/maintenance use the same supervised worker interrupt primitive.
ANALYZER_PREEMPT_SECONDS = 3.0
FAST_PAUSE_TRANSITION_SECONDS = 4.0
DEEP_PAUSE_TRANSITION_SECONDS = 4.0
FAST_CANCEL_TRANSITION_SECONDS = 4.0
DEEP_SEED_YIELD_SECONDS = 2.0
MAINTENANCE_QUIESCE_SECONDS = 8.0

# One absolute Engine shutdown budget.  Nested cleanup stages consume what is
# left instead of each adding another independent timeout.  The client waits a
# small margin longer than the Engine's own hard boundary.
ENGINE_SHUTDOWN_HARD_SECONDS = 12.0
CLIENT_SHUTDOWN_WAIT_SECONDS = 15.0
# The Qt Manager owns a second, independent watchdog. If the graceful client
# thread itself stalls, emergency process-tree termination gets three seconds
# and the UI must reach a terminal result instead of counting forever.
MANAGER_FULL_EXIT_FORCE_SECONDS = 3.0
MANAGER_FULL_EXIT_ABSOLUTE_SECONDS = CLIENT_SHUTDOWN_WAIT_SECONDS + MANAGER_FULL_EXIT_FORCE_SECONDS + 1.0
SHUTDOWN_REQUEST_SECONDS = 3.0

# UI/status cadence.  These are intentionally documented next to lifecycle
# budgets so apparent responsiveness is reviewed as part of the same contract.
STATUS_POLL_SECONDS = 1.0
STATUS_METRICS_MAX_AGE_SECONDS = 1.5
ENGINE_STOP_FEEDBACK_SECONDS = 0.25
MANAGER_BUSY_FEEDBACK_SECONDS = 0.9
BROWSER_CLOSE_HARD_SECONDS = 8.0


def remaining(deadline: float, now: float) -> float:
    """Return a strictly non-negative remainder for a shared absolute deadline."""
    return max(0.0, float(deadline) - float(now))

# Bulk Deep Analysis owns sustained throughput. Anonymous relationship
# preparation is sampled sparsely while Deep is active; user-prioritized anchors
# bypass this throttle through the Engine priority path.
RELATIONSHIP_DEEP_COMPLETION_BATCH = 512
RELATIONSHIP_DEEP_ANCHOR_BUDGET = 2
