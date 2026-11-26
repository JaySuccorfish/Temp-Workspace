"""@file protocol.py
@brief Wire-level helpers of the BLE command protocol: authentication answer and AES framing
@version 1.0
@date 2026-11-07

After authentication every command is sent as opcode, declared length, then the data AES-128 encrypted in 16-byte
blocks; the device answers in the same frame. The key lives in constants.py and is not repeated in comments.
"""
from .constants import AES_BLOCK_SIZE
from .constants import AES_KEY_GIGO_4G
from .constants import BLE_SEND_DEBUG_LOG
from Crypto.Cipher import AES


# --------------------------------------------------------------------------
# Protocol & Crypto Helpers
# --------------------------------------------------------------------------
def auth_key_value_gigo(m_auth_key: int) -> int:
    """Answer to the device's authentication challenge (the same transform as the firmware check)."""
    return (((m_auth_key * 79 + 8294) * 19) - (573 * m_auth_key + 989))


def auth_key_value_sc2_hub(m_auth_key: int) -> int:
    """Answer to the SC2 Gen2 / Gen3 hub authentication challenge (multiplier 75)."""
    return (((m_auth_key * 75 + 8294) * 19) - (573 * m_auth_key + 989))


def encrypt_command(plaintext_packet: bytes, aes_key: bytes = AES_KEY_GIGO_4G) -> bytes:
    """Frame a command for the wire: opcode and declared length stay plain, the data is zero-padded
    to whole 16-byte blocks and AES encrypted (ECB).
    """
    cmd = plaintext_packet[0]
    declared_len = plaintext_packet[1]
    data = plaintext_packet[2:]

    if len(data) == 0:
        return bytes([cmd, declared_len])

    pad_len = (-len(data)) % AES_BLOCK_SIZE
    padded = data + b"\x00" * pad_len
    cipher = AES.new(aes_key, AES.MODE_ECB)
    encrypted = cipher.encrypt(padded)

    return bytes([cmd, declared_len]) + encrypted


def decrypt_response(wire_packet: bytes, aes_key: bytes = AES_KEY_GIGO_4G) -> bytes:
    """Unframe a notification from the device: when the payload is whole AES blocks it is decrypted and
    cut to the declared length; anything else is returned as received (plain replies).
    """
    if len(wire_packet) < 2:
        return wire_packet

    cmd = wire_packet[0]
    declared_len = wire_packet[1]
    payload = wire_packet[2:]

    if len(payload) == 0:
        return bytes([cmd, declared_len])

    if len(payload) % AES_BLOCK_SIZE == 0:
        try:
            cipher = AES.new(aes_key, AES.MODE_ECB)
            decrypted = cipher.decrypt(payload)
            data = decrypted[:declared_len]
            return bytes([cmd, declared_len]) + data
        except Exception:
            pass

    return bytes([cmd, declared_len]) + payload[:declared_len]


def build_debug_log_cmd(enable: bool) -> bytes:
    """Build the plain 'debug log on/off' command (opcode BLE_SEND_DEBUG_LOG, one data byte)."""
    return bytes([BLE_SEND_DEBUG_LOG, 0x01, 0x01 if enable else 0x00])
