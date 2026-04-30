"""Standard CLI exit codes for ChangeBrief."""


class ExitCodes:
    """Exit codes used by the CLI and error handler."""

    SUCCESS = 0
    UNKNOWN_ERROR = 1
    VALIDATION_ERROR = 2
    CONFIG_ERROR = 3
    # Used by `validate` when the merge-risk gate (`--fail-on`) trips.
    MERGE_RISK_GATE = 30
