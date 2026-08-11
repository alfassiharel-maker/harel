# Security and Privacy

CCP Forge is a client-side application designed for privacy and security. This document covers the security architecture, threat model, and privacy practices.

## Privacy Model

### Data Collection

CCP Forge collects **nothing**:

- No analytics
- No crash reporting
- No telemetry
- No usage data
- No unique identifiers
- No outbound network traffic

All computation happens on the user's machine. The application never connects to the internet.

### Local Storage

Data at rest:

- **Artifacts**: stored in the user's application data directory (local filesystem only)
- **Ledger**: stored in a local file (`ledger.jsonl`)
- **Recent projects**: stored in a local index file
- **Session token**: generated at startup, stays in memory

Users control where data is stored. CCP Forge respects the application-data folder chosen by the platform:

- **Windows**: `%LOCALAPPDATA%\CCPForge\artifacts`
- **macOS**: `~/Library/Application Support/CCPForge/artifacts`
- **Linux**: `~/.local/share/CCPForge/artifacts`

### Third-Party Sharing

CCP Forge does not:

- Phone home with artifact metadata
- Report errors to a remote service
- Track user behavior
- Integrate with ad networks or analytics platforms
- Share data with third parties

## Security Architecture

### Defense in Depth

The application follows defense-in-depth principles:

1. **Input Validation**: all user input (file paths, uploads, API parameters) is validated
2. **Resource Limits**: maximum file counts, file sizes, and memory usage are enforced
3. **Isolation**: each artifact and session is isolated; no cross-contamination
4. **Error Handling**: errors are surfaced safely without exposing internals
5. **Cryptography**: artifacts are verified against recorded digests

### Layering

```
Browser (untrusted execution environment)
    ↓
HTTP Server (127.0.0.1, session-token protected)
    ↓
Application Controller (state machine, lock-protected)
    ↓
Product Engine (artifact lifecycle, sealed)
    ↓
Runtime (verification, selective access)
    ↓
Core (algorithm, no side effects)
```

Each layer trusts the layer below, not above. The browser is untrusted; financial values from the browser are never believed without server-side verification.

## Threat Model

### Threats

1. **Malicious Zip Archive**
   - **Attack**: crafted ZIP with path traversal (`../../etc/passwd`), symlinks, or zip bombs
   - **Defense**: `_zip_entry_rejection()` validates every entry; traversal and bomb attempts are rejected before decompression

2. **Resource Exhaustion**
   - **Attack**: crafted input claiming 1 TB of content
   - **Defense**: `InputLimits` enforces maximums (200k files, 2 GB total, 512 MB per file) before processing

3. **Corrupted Artifact**
   - **Attack**: user modifies artifact bytes, trying to hide tampering
   - **Defense**: `validate()` rebuilds every unit and checks digest; any mismatch is detected and reported

4. **Cross-Unit Inference**
   - **Attack**: reading selective windows from unit A to infer content of unit B
   - **Defense**: selective reads are windowed to one unit; no cross-unit leakage

5. **Local Privilege Escalation**
   - **Attack**: unprivileged user reads or modifies another user's artifacts
   - **Defense**: OS permissions (UNIX file mode, NTFS ACL) control access; the application does not bypass them

6. **Commercial Fraud**
   - **Attack**: customer edits browser memory to set `account_balance = 999999999`
   - **Defense**: the client calculates and displays, but doesn't have authority. A server would validate before settling.

### Non-Threats

The following are out of scope:

- **Physical tamper**: attacker with physical access to the machine can install rootkits
- **Hypervisor compromise**: if the OS is compromised, the application is compromised
- **Network eavesdropping**: no network traffic (local HTTP only, not encrypted)
- **Supply chain attacks**: compromised build tools or dependencies

These threats require protecting the entire machine, not just the application.

## Implementation Details

### Input Validation

#### Zip Entries

```python
def _zip_entry_rejection(info: zipfile.ZipInfo) -> str:
    """Reject entries that could escape the extraction context."""
    if info.is_dir():
        return "directory entry"
    if not info.filename:
        return "empty name"
    if info.filename.startswith(("/", "\\")):
        return "absolute path"
    if len(info.filename) > 1 and info.filename[1] == ":":
        return "drive-qualified path"
    normalised = info.filename.replace("\\", "/")
    if any(part == ".." for part in normalised.split("/")):
        return "path traversal"
    # Check for symlinks in external attributes
    if (info.external_attr >> 16) & 0xF000 == 0xA000:
        return "symbolic link"
    return ""
```

This rejects:
- Directory entries (unnecessary)
- Absolute paths (`/etc/passwd` or `C:\Windows`)
- Path traversal (`../../etc/passwd`)
- Symlinks (could point anywhere)

#### File Paths

Exported files are validated with `safe_export_path()`:

```python
def safe_export_path(base: str, uid: str) -> str:
    """Compute a safe export path that doesn't escape `base`."""
    # Resolve the requested path
    requested = os.path.join(base, uid)
    # Verify it's under base
    real_base = os.path.realpath(base)
    real_requested = os.path.realpath(requested)
    if not real_requested.startswith(real_base):
        raise ExportError("path escape attempt")
    return requested
```

This prevents:
- Symlink attacks (follows symlinks and verifies final path is under base)
- Relative path traversal
- Drive letter changes (Windows)

#### API Parameters

JSON request bodies are parsed and validated:
- JSON syntax is verified
- Request size is bounded (`MAX_JSON_BYTES = 1 MB`)
- Required fields are checked
- Numeric fields are type-checked

### Resource Limits

#### Input Analysis

Before building, the input is analyzed without processing:

```python
InputLimits(
    max_units=200_000,           # 200k files max
    max_total_bytes=2 << 30,     # 2 GB max
    max_unit_bytes=512 << 20,    # 512 MB per file
    max_archive_ratio=200.0,     # zip bomb guard
)
```

The analysis counts and sizes entries without decompressing. A zip that claims to expand from 1 KB to 5 TB is rejected before touching the compressed data.

#### Artifact Limits

The product layer enforces maximum artifact size:

```python
DEFAULT_MAX_ARTIFACT_BYTES = 4 << 30  # 4 GB max artifact
```

This prevents unbounded memory allocation during reconstruction.

### Artifact Verification

Reconstruction verifies every unit's digest:

```python
def validate(self) -> VerificationReport:
    """Reconstruct and check every unit."""
    for uid in self.units():
        reconstructed = self.materialize(uid)
        expected_digest = self.unit_info(uid).digest
        if sha256(reconstructed) != expected_digest:
            raise VerificationError(f"unit {uid} does not match digest")
```

If a single byte is modified, verification fails. This ensures the artifact hasn't been tampered with.

### Concurrency Safety

The application controller serializes all artifact access behind a lock:

```python
class AppController:
    def __init__(self):
        self._lock = threading.Lock()
        self._artifact = None

    def materialize(self, uid, offset, length):
        with self._lock:
            # Only one thread at a time can access the artifact
            return self._artifact.read_range(uid, offset, length)
```

This respects the Runtime's contract: "concurrent access is unsupported."

### Error Handling

Errors are typed and surfaced safely:

```python
try:
    return app.materialize(uid, offset, length)
except UnitNotFoundError as e:
    return {"error": {"title": "Unit not found", "what": uid, ...}}
except InvalidRangeError as e:
    return {"error": {"title": "Invalid range", "what": f"{offset}:{length}", ...}}
except VerificationError as e:
    return {"error": {"title": "Verification failed", "what": "...", ...}}
```

No traceback is shown to the user. The error message explains the problem and next steps.

## Commercial Layer Security

### Financial Authority

The commercial layer is client-side **for display only**. Financial decisions must happen server-side.

Client can:
- Calculate and display measurements
- Show account balance
- Record customer intention (e.g., "I accept the terms")

Client cannot:
- Authoritatively modify the ledger
- Change a customer's balance
- Reverse transactions
- Issue refunds

A production system would:

```
Client: "Measurement: 10KB saved, $0.10 credit"
  ↓
Server: "Verify artifact signature, recalculate measurement"
Server: "Store measurement in authoritative ledger"
Server: "Update customer balance in payment system"
  ↓
Client: "Balance updated: $X.XX"
```

### Sandbox-Only Payments

The `SandboxPaymentProvider` never charges real cards:

```python
class SandboxPaymentProvider(PaymentProvider):
    """Test-only implementation. No real money is moved."""
    def authorize_charge(self, ...):
        # Simulates the operation. Returns success.
        # No actual charge is placed.
        return PaymentStatus(success=True, transaction_id=fake_id)
```

Even if a customer's real credit card is registered with the sandbox, authorization always succeeds but **no charge is actually placed**. This is safe for development and testing.

To charge real money, a different `PaymentProvider` implementation would be required (Stripe, Square, etc.). **The application never touches real payment credentials or processing.**

### Ledger Integrity

The measurement ledger is append-only, never modified:

```python
def append(self, record: MeasurementRecord) -> None:
    """Append a record. This is the only write operation."""
    with open(self.ledger_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
```

To correct an error, a compensating entry is appended, not the original modified. This creates an audit trail.

A compromised client could delete lines from the ledger file, but a server-side ledger would be authoritative.

## OWASP Compliance

### A01: Broken Access Control

- ✓ No privilege escalation (users cannot access other users' data)
- ✓ Session tokens prevent CSRF
- ✓ Resource ownership is enforced (cannot export another user's artifact)

### A02: Cryptographic Failures

- ✓ Artifacts are verified by digest (SHA-256)
- ✓ No sensitive data in logs
- ✓ No hardcoded credentials

### A03: Injection

- ✓ No SQL (no database)
- ✓ Path traversal is prevented (`safe_export_path`)
- ✓ JSON parsing validates structure

### A04: Insecure Design

- ✓ Threat model is explicit
- ✓ Resource limits are enforced upfront
- ✓ Input validation is defense-in-depth

### A05: Security Misconfiguration

- ✓ Only 127.0.0.1 is bound (not 0.0.0.0)
- ✓ Session token is required
- ✓ Defaults are secure

### A06: Vulnerable and Outdated Components

- ✓ No external dependencies (stdlib only for the core)
- ✓ Python standard library is updated with OS patches

### A07: Identification and Authentication

- ✓ Session tokens are cryptographically random (24 bytes, base64)
- ✓ Tokens are single-use per session
- ✓ No password-based auth (local app, not a service)

### A08: Data Integrity Failures

- ✓ Artifacts are verified by digest
- ✓ Ledger is append-only
- ✓ Modification is detected during reconstruction

### A09: Logging and Monitoring

- ✓ Errors are logged to stderr (not to the network)
- ✓ No sensitive data in logs (artifacts are not logged)
- ✓ User actions are trackable via the ledger

### A10: SSRF

- ✓ No outbound network access
- ✓ Artifacts are not fetched from URLs
- ✓ Files are provided by the user locally

## Security Principles

1. **Least Privilege**: the application runs as the current user, not elevated
2. **Defense in Depth**: multiple layers (validation, limits, verification, error handling)
3. **Fail Safely**: errors result in rejection, not bypass
4. **Explicit Consent**: terms acceptance is mandatory before measurement
5. **Local Authority**: data is owned by the user; the application is a steward
6. **No Trust Boundaries**: the browser and CLI are considered untrusted for financial values

## Incident Response

If a security vulnerability is discovered:

1. **Do not post in public issues** (GitHub issues are public)
2. **Email security@example.com** with:
   - Description of the vulnerability
   - Steps to reproduce (if applicable)
   - Potential impact
3. **Coordinate with the maintainer** on a responsible disclosure timeline
4. **Patch and release** a fixed version
5. **Credit** the researcher (unless they prefer anonymity)

## Responsible Disclosure Policy

CCP Forge follows [responsible disclosure](https://en.wikipedia.org/wiki/Responsible_disclosure):

- **Report privately**: email security@example.com
- **Provide details**: version, steps to reproduce, proof of concept
- **Wait for response**: typically 48 hours
- **Coordinate timeline**: the maintainer proposes a fix date
- **Publish together**: vulnerability details are shared publicly after a patch is released
- **Credit given**: researcher is acknowledged (optional)

Example timeline:

```
Day 0: Vulnerability reported
Day 1: Maintainer acknowledges and begins work
Day 7: Patch ready, tested, version bumped
Day 8: Release published, announcement posted
Day 14: Vulnerability details published (after users have time to update)
```

## Security Checklist for Users

- [ ] Download CCP Forge from the official Microsoft Store or GitHub
- [ ] Verify the executable's signature (right-click → Properties → Digital Signatures)
- [ ] Keep Windows updated (security patches)
- [ ] Use antivirus/anti-malware software
- [ ] Do not run untrusted ZIP archives
- [ ] Do not share your `ledger.jsonl` file (it contains your measurements)
- [ ] Before payment integration: ensure merchant uses HTTPS and is PCI-DSS compliant

## Security by Design

CCP Forge's architecture makes many attacks impossible:

- **No network**: no SSRF, no exfiltration, no remote code execution
- **No database**: no SQL injection
- **No user authentication**: no credential leaks (single-user app)
- **No external dependencies**: no supply-chain attacks (for the core)
- **Sealed artifact lifecycle**: once built, an artifact is read-only
- **Deterministic algorithm**: reproducible results, no hidden behavior

These design choices are enforced at the architectural level, not patched after-the-fact.
