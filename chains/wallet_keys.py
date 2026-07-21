"""File-backed signing keys. SHARED BY EVERY CHAIN IN THIS PACKAGE.

Both the BSC and the Solana back-ends load their signing key through here -
do not add a second key loader. House rule (same as the CEX clients): a
private key is NEVER read from a plain environment variable. The env var
holds a *path*; the code reads the file::

    export BSC_PRIVATE_KEY_FILE=/run/secrets/bsc.key   # file mode 0600
    key = load_secret_key("BSC_PRIVATE_KEY_FILE")      # -> SecretKey

The material is wrapped in :class:`SecretKey`, whose ``__repr__``/``__str__``/
``__format__`` all return ``<redacted>``, so it cannot leak through a log
line, an f-string, a ``%s``, or a traceback frame that renders its args.
The raw value is reachable only via an explicit ``.reveal()`` - keep that
call inline at the signing site and never bind it to a local that outlives
the statement.

Formats
    ``"evm"``     32-byte hex, optional ``0x``  (BSC / any EVM chain)
    ``"base58"``  base58 text                   (Solana secret key)
    ``"raw"``     any non-empty single-line blob (no format opinion)
"""

import logging
import os
import re
import stat

from .errors import ChainError

LOG = logging.getLogger(__name__)

_REDACTED = "<redacted>"

_EVM_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
# base58 alphabet: no 0, O, I, l
_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,120}$")


# ---------- public ----------

class SecretKey:
    """Opaque wrapper around key material.

    Everything that renders an object renders ``<redacted>``. Equality and
    hashing are deliberately not implemented so the value cannot be probed
    by comparison, and the instance is not picklable/copyable.
    """

    __slots__ = ("_value", "label")

    def __init__(self, value, label="key"):
        if not isinstance(value, str) or not value:
            raise ChainError("KEY_EMPTY", "key material is empty")
        self._value = value
        self.label = label

    # --- the whole point of this class ---
    def __repr__(self):
        return _REDACTED

    def __str__(self):
        return _REDACTED

    def __format__(self, spec):
        return _REDACTED

    def __reduce__(self):
        raise TypeError("SecretKey is not serialisable")

    def __deepcopy__(self, memo):
        raise TypeError("SecretKey is not copyable")

    def reveal(self):
        """Return the raw key. Call this inline at the signing site only."""
        return self._value


def load_secret_key(env_var, fmt="evm", label=None):
    """Read a signing key from the file whose path lives in ``env_var``.

    Whitespace (including a trailing newline from ``echo >`` or a k8s secret
    mount) is stripped before validation. Raises :class:`ChainError` if the
    var is unset, the file is missing/unreadable/empty, or the contents do
    not match ``fmt``.
    """
    path = os.environ.get(env_var, "").strip()
    if not path:
        raise ChainError(
            "KEY_ENV_MISSING",
            f"{env_var} is not set; it must hold the PATH to the key file "
            f"(never the key itself)",
        )
    try:
        with open(path, "r", encoding="utf-8") as fh:
            material = fh.read().strip()
    except OSError as exc:
        # Deliberately NOT echoing the path: the classic misconfiguration is
        # pasting the key into the env var, and then the "path" in the OSError
        # message IS the key. Report the errno only.
        raise ChainError(
            "KEY_FILE_UNREADABLE",
            f"cannot read the file named by {env_var} "
            f"({exc.strerror or type(exc).__name__}); the variable must hold a "
            f"PATH to the key file, never the key itself",
        )

    if not material:
        raise ChainError("KEY_FILE_EMPTY", f"key file at {env_var} path is empty")
    if "\n" in material:
        raise ChainError("KEY_FILE_MULTILINE", "key file must hold exactly one key")

    _warn_on_loose_permissions(path)
    material = _validate(material, fmt)
    return SecretKey(material, label=label or env_var)


def address_from_key(key):
    """Derive the checksummed EVM address for a :class:`SecretKey`.

    ``eth_account`` is imported lazily so this module stays importable (and
    unit-testable) on hosts without the web3 stack installed.
    """
    if not isinstance(key, SecretKey):
        raise ChainError("KEY_TYPE", "expected a SecretKey, not raw key material")
    try:
        from eth_account import Account
    except ImportError as exc:  # pragma: no cover - depends on host env
        raise ChainError("DEP_MISSING", f"eth_account is required: {exc}")
    try:
        return Account.from_key(key.reveal()).address
    except Exception as exc:
        # str(exc) from eth_account never echoes the key, but be explicit
        raise ChainError("KEY_INVALID", f"cannot derive address: {type(exc).__name__}")


# ---------- internal ----------

def _validate(material, fmt):
    if fmt == "evm":
        if not _EVM_RE.match(material):
            raise ChainError(
                "KEY_FORMAT",
                "expected a 32-byte hex private key (64 hex chars, optional 0x)",
            )
        # normalise: 0x-prefixed lowercase, which is what eth_account wants
        return "0x" + material[2:].lower() if material.startswith(("0x", "0X")) \
            else "0x" + material.lower()
    if fmt == "base58":
        if not _BASE58_RE.match(material):
            raise ChainError("KEY_FORMAT", "expected a base58-encoded secret key")
        return material
    if fmt == "raw":
        return material
    raise ChainError("KEY_FORMAT", f"unknown key format {fmt!r}")


def _warn_on_loose_permissions(path):
    """Log (do not fail) if the key file is readable by group/other."""
    try:
        mode = os.stat(path).st_mode
    except OSError:  # pragma: no cover - raced with us; the read already worked
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        LOG.warning("key file %s is group/world accessible (%o) - chmod 600 it",
                    path, stat.S_IMODE(mode))
