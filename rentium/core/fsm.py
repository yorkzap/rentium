"""
Minimal finite-state-machine helper (no dependency on django-fsm).

Usage on a model:

    class WorkOrder(models.Model):
        TRANSITIONS = {
            Status.NEW: {Status.SCHEDULED, Status.IN_PROGRESS, Status.CANCELLED},
            ...
        }

        def transition_to(self, new_status, by=None):
            return transition(self, "status", new_status, self.TRANSITIONS, by=by)

Why: leases, work orders, etc. have strict lifecycles. Loose CRUD lets a
tenant "sign" a TERMINATED lease or a job jump NEW -> COMPLETED with no
schedule/cost. Enforcing legal moves here removes a whole class of bugs and
gives the event stream predictable, meaningful transitions.
"""

from django.core.exceptions import ValidationError


class IllegalTransition(ValidationError):
    """Raised when a state change is not allowed by the model's TRANSITIONS map."""


def transition(instance, field: str, new_state: str, transitions: dict, save: bool = True, by=None):
    """
    Move `instance.<field>` to `new_state` if the move is legal, save, and
    return (old_state, new_state). Raises IllegalTransition otherwise.

    A no-op (same state) is allowed and returns without saving.
    """
    old_state = getattr(instance, field)
    if old_state == new_state:
        return old_state, new_state

    allowed = transitions.get(old_state, set())
    if new_state not in allowed:
        raise IllegalTransition(
            {field: f"Cannot move from {old_state} to {new_state}. Allowed: {sorted(allowed) or 'none (terminal state)'}."}
        )

    setattr(instance, field, new_state)
    if save:
        instance.save(update_fields=[field, "updated_at"] if hasattr(instance, "updated_at") else [field])
    return old_state, new_state
