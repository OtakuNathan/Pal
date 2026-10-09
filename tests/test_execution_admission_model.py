from spec.execution.lifecycle_admission_model import explore


def test_admission_model_exhaustively_preserves_ownership():
    states, violation = explore()
    assert len(states) == 13
    assert violation is None


def test_model_exposes_repeated_cancellation_ownership_loss():
    _, trace = explore(broken_cancel=True)
    assert trace is not None
    assert trace.count("cancel waiter") == 2
