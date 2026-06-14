from __future__ import annotations

import base64
import logging
import os
import struct
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.serialization import load_ssh_public_key

log = logging.getLogger(__name__)

_NONCE_TTL_SECONDS = 60
_NONCE_BYTES = 32

_DEFAULT_TRUSTED_KEY_PATHS = [
    Path.home() / ".ssh" / "authorized_keys",
    Path.home() / ".ssh" / "id_ed25519.pub",
]

# SSH signature magic preamble per RFC 8709 / OpenSSH SSHSIG spec
_SSHSIG_MAGIC = b"SSHSIG"
_SSHSIG_NAMESPACE = b"fleet-auth"


def _parse_ssh_wire_string(data: bytes, offset: int) -> tuple[bytes, int]:
    # SSH wire format: uint32 length prefix + data
    if offset + 4 > len(data):
        raise ValueError("truncated ssh wire string")
    (length,) = struct.unpack(">I", data[offset : offset + 4])
    end = offset + 4 + length
    if end > len(data):
        raise ValueError("truncated ssh wire string payload")
    return data[offset + 4 : end], end


class SSHAuthenticator:
    def __init__(
        self,
        trusted_keys_paths: list[Path] | None = None,
    ) -> None:
        self._trusted_keys_paths = trusted_keys_paths or _DEFAULT_TRUSTED_KEY_PATHS
        self._active_nonces: dict[str, float] = {}

    def load_trusted_keys(self) -> list[bytes]:
        # Load all public keys from configured paths.
        # authorized_keys files can have multiple keys (one per line).
        # .pub files have a single key.
        keys: list[bytes] = []
        for path in self._trusted_keys_paths:
            if not path.exists():
                log.debug("trusted key path does not exist: %s", path)
                continue
            try:
                text = path.read_text()
            except OSError as exc:
                log.warning("cannot read %s: %s", path, exc)
                continue

            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # authorized_keys format: key_type base64_key [comment]
                parts = line.split(None, 2)
                if len(parts) < 2:
                    continue
                try:
                    key_blob = base64.b64decode(parts[1])
                    keys.append(key_blob)
                except Exception:
                    log.debug("skipping malformed key line in %s", path)
        return keys

    def generate_nonce(self) -> str:
        self._purge_expired()
        nonce = os.urandom(_NONCE_BYTES).hex()
        self._active_nonces[nonce] = time.monotonic()
        return nonce

    def _purge_expired(self) -> None:
        cutoff = time.monotonic() - _NONCE_TTL_SECONDS
        expired = [n for n, ts in self._active_nonces.items() if ts < cutoff]
        for n in expired:
            del self._active_nonces[n]

    def _consume_nonce(self, nonce: str) -> bool:
        # Consume nonce: returns True if valid and not expired, False otherwise.
        # One-time use — removed after consumption.
        self._purge_expired()
        ts = self._active_nonces.pop(nonce, None)
        if ts is None:
            return False
        if (time.monotonic() - ts) > _NONCE_TTL_SECONDS:
            return False
        return True

    def verify_signature(
        self,
        nonce: str,
        signature: bytes,
        public_key_blob: bytes,
    ) -> bool:
        # Verify an SSH signature over the nonce.
        # 1. Check nonce is active and consume it
        # 2. Check public key is in trusted set
        # 3. Verify cryptographic signature

        if not self._consume_nonce(nonce):
            log.warning("nonce invalid or expired")
            return False

        # Check trust
        trusted = self.load_trusted_keys()
        if public_key_blob not in trusted:
            log.warning("public key not in trusted set")
            return False

        # Parse the public key
        try:
            pub_key = _load_public_key_from_blob(public_key_blob)
        except Exception:
            log.exception("failed to parse public key blob")
            return False

        # Extract the raw signature from SSH signature format
        # SSH signatures can be in SSHSIG or raw format
        nonce_bytes = nonce.encode("utf-8")

        try:
            raw_sig, algo = _extract_raw_signature(signature)
        except Exception:
            log.exception("failed to parse signature envelope")
            return False

        # Verify based on key type
        try:
            _verify_with_key(pub_key, raw_sig, nonce_bytes, algo)
            return True
        except Exception:
            log.warning("signature verification failed")
            return False


def _load_public_key_from_blob(blob: bytes) -> object:
    # Parse SSH wire-format public key blob into a cryptography key object.
    # Determine key type from the blob
    key_type, offset = _parse_ssh_wire_string(blob, 0)
    key_type_str = key_type.decode("utf-8")

    if key_type_str == "ssh-ed25519":
        raw_key, _ = _parse_ssh_wire_string(blob, offset)
        return ed25519.Ed25519PublicKey.from_public_bytes(raw_key)

    elif key_type_str == "ssh-rsa":
        e_bytes, offset = _parse_ssh_wire_string(blob, offset)
        n_bytes, _ = _parse_ssh_wire_string(blob, offset)
        e = int.from_bytes(e_bytes, byteorder="big")
        n = int.from_bytes(n_bytes, byteorder="big")
        return rsa.RSAPublicNumbers(e, n).public_key()

    elif key_type_str.startswith("ecdsa-sha2-"):
        # curve identifier
        _curve_id, offset = _parse_ssh_wire_string(blob, offset)
        q_bytes, _ = _parse_ssh_wire_string(blob, offset)
        curve_id_str = _curve_id.decode("utf-8")
        curve_map = {
            "nistp256": ec.SECP256R1(),
            "nistp384": ec.SECP384R1(),
            "nistp521": ec.SECP521R1(),
        }
        curve = curve_map.get(curve_id_str)
        if curve is None:
            raise ValueError(f"unsupported ECDSA curve: {curve_id_str}")
        return ec.EllipticCurvePublicKey.from_encoded_point(curve, q_bytes)

    else:
        raise ValueError(f"unsupported key type: {key_type_str}")


def _extract_raw_signature(sig_data: bytes) -> tuple[bytes, str]:
    # Parse SSH signature format.
    # Two common formats:
    #   1. SSHSIG envelope (ssh-keygen -Y sign)
    #   2. Raw SSH agent signature (string algo + string sig_blob)
    if sig_data[:6] == _SSHSIG_MAGIC:
        # SSHSIG format: MAGIC || uint32 version || string pubkey ||
        # string namespace || string reserved || string hash_algo ||
        # string signature
        offset = 6
        # version
        (version,) = struct.unpack(">I", sig_data[offset : offset + 4])
        offset += 4
        # public key (skip)
        _, offset = _parse_ssh_wire_string(sig_data, offset)
        # namespace (skip)
        _, offset = _parse_ssh_wire_string(sig_data, offset)
        # reserved (skip)
        _, offset = _parse_ssh_wire_string(sig_data, offset)
        # hash algorithm (skip)
        _, offset = _parse_ssh_wire_string(sig_data, offset)
        # signature blob
        sig_blob, _ = _parse_ssh_wire_string(sig_data, offset)
        # sig_blob is itself: string algo + string raw_sig
        algo_bytes, inner_offset = _parse_ssh_wire_string(sig_blob, 0)
        raw_sig, _ = _parse_ssh_wire_string(sig_blob, inner_offset)
        return raw_sig, algo_bytes.decode("utf-8")
    else:
        # Raw SSH agent format: string algo + string sig_blob
        algo_bytes, offset = _parse_ssh_wire_string(sig_data, 0)
        raw_sig, _ = _parse_ssh_wire_string(sig_data, offset)
        return raw_sig, algo_bytes.decode("utf-8")


def _verify_with_key(
    pub_key: object,
    raw_sig: bytes,
    data: bytes,
    algo: str,
) -> None:
    from cryptography.hazmat.primitives import hashes

    if isinstance(pub_key, ed25519.Ed25519PublicKey):
        pub_key.verify(raw_sig, data)

    elif isinstance(pub_key, rsa.RSAPublicKey):
        hash_algo: hashes.HashAlgorithm
        if algo in ("rsa-sha2-256", "ssh-rsa"):
            hash_algo = hashes.SHA256()
        elif algo == "rsa-sha2-512":
            hash_algo = hashes.SHA512()
        else:
            hash_algo = hashes.SHA256()

        pub_key.verify(
            raw_sig,
            data,
            padding.PKCS1v15(),
            hash_algo,
        )

    elif isinstance(pub_key, ec.EllipticCurvePublicKey):
        hash_algo_ec: hashes.HashAlgorithm
        if algo == "ecdsa-sha2-nistp256":
            hash_algo_ec = hashes.SHA256()
        elif algo == "ecdsa-sha2-nistp384":
            hash_algo_ec = hashes.SHA384()
        elif algo == "ecdsa-sha2-nistp521":
            hash_algo_ec = hashes.SHA512()
        else:
            hash_algo_ec = hashes.SHA256()

        pub_key.verify(
            raw_sig,
            data,
            ec.ECDSA(hash_algo_ec),
        )

    else:
        raise ValueError(f"unsupported key type for verification: {type(pub_key)}")
