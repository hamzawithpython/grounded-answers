"""
Every size and count limit for uploads, in one place (SPEC §6.1, ADR 0019).

The database check on document_blobs uses the same file limit (10 MiB). A change here must be
matched there by a new migration.
"""

from typing import Final

# The file itself: 10 MiB (SPEC §6.1).
MAX_FILE_BYTES: Final = 10 * 1024 * 1024

# The whole request body: the file plus room for the multipart framing (boundaries and headers).
# The body is cut off here while it streams, whatever Content-Length says (ADR 0019, D2).
MULTIPART_OVERHEAD_BYTES: Final = 64 * 1024
MAX_BODY_BYTES: Final = MAX_FILE_BYTES + MULTIPART_OVERHEAD_BYTES

# Form limits, enforced by the parser before the endpoint sees the form.
MAX_FORM_FILES: Final = 1
MAX_FORM_FIELDS: Final = 8
MAX_FORM_PART_BYTES: Final = 1024  # every non-file field is short; the file part is not a field

# DOCX (a ZIP archive). Declared sizes in a ZIP header can lie, so the reader also counts bytes.
MAX_ZIP_ENTRIES: Final = 2000
MAX_ZIP_TOTAL_BYTES: Final = 50 * 1024 * 1024
MAX_ZIP_ENTRY_BYTES: Final = 20 * 1024 * 1024
