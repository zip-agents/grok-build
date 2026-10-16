"""Fixed bounds that are not environment configuration.

These exist so memory stays independent of upload size. They are part of the
per-request overhead in the memory budget, not operator-facing limits.
"""

# Bytes read from the socket and fed to the compressor in one step.
READ_CHUNK_BYTES = 64 * 1024

# Multipart preamble and one part's headers are rejected past this size.
PART_HEADER_MAX_BYTES = 64 * 1024
PREAMBLE_MAX_BYTES = 64 * 1024

# Compressed-but-not-yet-written input chunks queued to the zip worker.
INPUT_QUEUE_CHUNKS = 4

# Conservative allowance for zlib's window, hash chains, and ZIP bookkeeping.
ZLIB_OVERHEAD_BYTES = 256 * 1024

# filename form field, from the contract.
FILENAME_FIELD_MAX_BYTES = 1024

DEFAULT_ARCHIVE_NAME = "zipped_files.zip"
ENTRY_NAME_MAX_BYTES = 255
# Bytes after the final dot. A longer suffix is not preserved when truncating.
SHORT_EXTENSION_MAX_BYTES = 16

REQUEST_ID_PATTERN = r"^[A-Za-z0-9._-]{1,128}$"
RETRY_AFTER_SECONDS = "5"


def per_request_overhead_bytes() -> int:
    """Bytes of per-request state on top of OUTPUT_BUFFER_MAX_BYTES.

    One read chunk, the worker queue, the zlib window, and the part-header cap.
    Kernel socket buffers are outside this figure; they are bounded by the OS
    and do not grow with the upload.
    """

    return (
        READ_CHUNK_BYTES
        + INPUT_QUEUE_CHUNKS * READ_CHUNK_BYTES
        + ZLIB_OVERHEAD_BYTES
        + PART_HEADER_MAX_BYTES
    )
