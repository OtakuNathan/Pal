"""Inspect committed in-memory state for failure-atomicity assertions only."""


def committed_state(session):
    # An open attempt is allowed to differ; all other session state must remain.
    return repr({key: value for key, value in vars(session).items() if key != "_active"})
