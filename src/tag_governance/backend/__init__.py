"""Backend package.

The tag-governance logic modules (db, queries, tagging, writer, jobs, authz,
ws_clients, writer_job, rollback_job) are framework-agnostic and import each
other by bare top-level name (e.g. `import db`). Add this directory to sys.path
so those imports resolve as siblings without rewriting every import statement.
"""
import os
import sys

# Corporate networks (like the one this dev environment sits behind) do TLS
# interception with a self-signed root CA. Python's bundled certifi doesn't trust
# it, so the SQL connector fails with CERTIFICATE_VERIFY_FAILED locally. truststore
# routes verification through the OS trust store (macOS keychain), which DOES have
# the corp cert. Harmless in prod (Apps has no interception). Must run before any
# TLS connection is created, so it's here at package import.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001 — never block startup on this
    pass

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)
