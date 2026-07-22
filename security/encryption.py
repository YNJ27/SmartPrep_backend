import os
import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Load the master key at startup
_b64_key = os.getenv("MASTER_ENCRYPTION_KEY")
if not _b64_key:
    # In a real app, you might want to raise an exception if not found,
    # but for testing/dev if it's missing it could cause import errors.
    print("Warning: MASTER_ENCRYPTION_KEY not found in environment.")
    _key = None
    _aesgcm = None
else:
    try:
        _key = base64.b64decode(_b64_key)
        _aesgcm = AESGCM(_key)
    except Exception as e:
        print(f"Error initializing AESGCM: {e}")
        _key = None
        _aesgcm = None

def encrypt_api_key(api_key: str) -> str:
    if not _aesgcm:
        raise ValueError("Encryption system is not properly initialized.")
    
    nonce = os.urandom(12)
    ciphertext = _aesgcm.encrypt(nonce, api_key.encode('utf-8'), None)
    
    # Combine nonce and ciphertext and base64 encode
    combined = nonce + ciphertext
    return base64.b64encode(combined).decode('utf-8')

def decrypt_api_key(encrypted_base64: str) -> str:
    if not _aesgcm:
        raise ValueError("Encryption system is not properly initialized.")
        
    combined = base64.b64decode(encrypted_base64)
    nonce = combined[:12]
    ciphertext = combined[12:]
    
    plaintext = _aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext.decode('utf-8')
