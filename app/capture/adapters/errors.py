class CaptureConfigError(Exception):
    """Raised at adapter construction, not at first use — a missing
    credential should fail loudly and immediately (at CUE_CAPTURE_BACKEND=
    live startup / first dependency resolution), not partway into a live
    session. Mirrors app/documents/sharepoint.py's SharePointConfigError."""
