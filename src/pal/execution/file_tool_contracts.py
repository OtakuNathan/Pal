"""Shared prose and defaults for structured text-file capabilities.

The executable input/output contract lives exclusively in the Pydantic models
bound by the capability descriptor.
"""

from __future__ import annotations


DEFAULT_FILE_READ_LIMIT = 2000

FILE_READ_DESCRIPTION = (
    "Read a UTF-8 text file and return line-numbered content. "
    "Use offset and limit for large files. Read the complete file before editing, "
    "overwriting, or deleting it so stale changes can be detected. Re-reading an "
    "unchanged line range already present in the current context returns a compact "
    "unchanged marker instead of duplicating its content. When that marker is returned, "
    "refer to the earlier read result and do not call read_file again unless the file "
    "has changed or a previously uncovered line range is needed."
)

FILE_EDIT_DESCRIPTION = (
    "Replace an exact old_string in a UTF-8 text file after reading the complete file. "
    "The match must be unique unless replace_all=true. Returns a unified diff on success."
)

FILE_WRITE_DESCRIPTION = (
    "Write complete UTF-8 text content to a file. Missing files and parent directories are created; "
    "existing files are overwritten only after the complete file has been read. "
    "Use file_edit for focused changes to existing files."
)
