"""Run the cleanup-ownership regressions added after the original audit.

The original failing evidence is preserved in cleanup_ownership.log.
"""
from tests.test_plugin_publication_rollback import (
    staged_plugin,
    test_early_failure_retains_cleanup_until_detach_succeeds,
    test_failed_extension_cleanup_remains_owned_by_host,
)
from tests.test_execution_extension_admission import (
    execution,
    test_failed_install_cleanup_is_retried_without_republishing,
)
