import pytest
from fleet.auth import SSHAuthenticator
from unittest.mock import patch, MagicMock

def test_auth_init():
    auth = SSHAuthenticator()
    assert auth is not None

def test_generate_nonce():
    auth = SSHAuthenticator()
    nonce = auth.generate_nonce()
    assert len(nonce) > 0

def test_load_trusted_keys(tmp_path):
    pub = tmp_path / "id_ed25519.pub"
    pub.write_bytes(b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA== user@host")
    auth = SSHAuthenticator(trusted_keys_paths=[pub])
    keys = auth.load_trusted_keys()
    assert len(keys) > 0

def test_auth_verify_error():
    auth = SSHAuthenticator()
    # verify_challenge does not throw exception, it returns False on error
    res = auth.verify_signature("invalid", b"bad_signature", b"bad_pk")
    assert res is False

@patch("fleet.auth.SSHAuthenticator.load_trusted_keys")
@patch("fleet.auth._load_public_key_from_blob")
@patch("fleet.auth._extract_raw_signature")
@patch("fleet.auth._verify_with_key")
def test_auth_verify_success(mock_verify, mock_extract, mock_load_pk, mock_load_trusted):
    auth = SSHAuthenticator()
    nonce = auth.generate_nonce()
    mock_load_trusted.return_value = [b"pk_blob"]
    mock_extract.return_value = (b"raw_sig", b"ssh-ed25519")
    
    res = auth.verify_signature(nonce, b"signature_envelope", b"pk_blob")
    assert res is True
    mock_verify.assert_called_once()

def test_load_public_key_from_blob():
    from fleet.auth import _load_public_key_from_blob
    import struct
    
    def pack_str(s: bytes):
        return struct.pack(">I", len(s)) + s
        
    # ed25519
    ed_blob = pack_str(b"ssh-ed25519") + pack_str(b"A" * 32)
    pk = _load_public_key_from_blob(ed_blob)
    assert pk is not None
    
    # rsa
    rsa_blob = pack_str(b"ssh-rsa") + pack_str(b"\x01\x00\x01") + pack_str(b"\x01" * 128)
    pk = _load_public_key_from_blob(rsa_blob)
    assert pk is not None
    
    # unsupported
    with pytest.raises(ValueError):
        _load_public_key_from_blob(pack_str(b"ssh-dss"))

def test_extract_raw_signature():
    from fleet.auth import _extract_raw_signature
    import struct
    def pack_str(s: bytes):
        return struct.pack(">I", len(s)) + s
        
    # Raw SSH agent format
    raw_data = pack_str(b"ssh-ed25519") + pack_str(b"my_signature")
    raw_sig, algo = _extract_raw_signature(raw_data)
    assert raw_sig == b"my_signature"
    assert algo == "ssh-ed25519"
    
    # SSHSIG format
    sshsig = b"SSHSIG\x00\x00\x00\x01" + pack_str(b"pk") + pack_str(b"namespace") + pack_str(b"") + pack_str(b"sha512") + pack_str(raw_data)
    raw_sig2, algo2 = _extract_raw_signature(sshsig)
    assert raw_sig2 == b"my_signature"
    assert algo2 == "ssh-ed25519"

