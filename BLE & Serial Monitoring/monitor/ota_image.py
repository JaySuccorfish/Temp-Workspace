"""@file ota_image.py
@brief Checks a firmware image before a BLE update (DOTA), with the same rules the loader uses
@version 1.0
@date 2026-11-07

A file that fails here would be refused by the loader or would not start, so the upload is not even begun.
"""
from .constants import APP_SECTOR_COUNT_MAX
from .constants import FLASH_SECTOR_BYTES
from .constants import IMAGE_APP_BASE
from .constants import IMAGE_TAG_KEYWORD
from .constants import IMAGE_TAG_POINTER_OFFSET
from .constants import SRAM_END
from .constants import SRAM_START
from .constants import STATUS_FAILED
from .constants import STATUS_OK
from .logbus import log_system
import re
import struct


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    """Remove ANSI colour escape sequences from a text line."""
    return _ANSI_ESCAPE_RE.sub("", s)


def read_word(data: bytes, offset: int) -> int:
    """Little-endian 32-bit word at offset in data."""
    return struct.unpack_from("<I", data, offset)[0]


def check_image(image: bytes) -> int:
    """Same checks the loader makes before it trusts a staged image; returns STATUS_OK or STATUS_FAILED.

    Flow: size fits the application region -> stack pointer in SRAM -> reset vector (Thumb) inside the
    image -> end-tag pointer at +0x140 inside the image -> the word it points to is the OTA end tag.
    """
    if len(image) < IMAGE_TAG_POINTER_OFFSET + 4:
        log_system(f"image is only {len(image)} bytes, too short to hold vector table and end tag pointer", "#ef4444")
        return STATUS_FAILED
    if len(image) > APP_SECTOR_COUNT_MAX * FLASH_SECTOR_BYTES:
        log_system(f"image is {len(image)} bytes, larger than region limit", "#ef4444")
        return STATUS_FAILED

    stack_pointer = read_word(image, 0)
    reset_vector = read_word(image, 4)
    tag_pointer = read_word(image, IMAGE_TAG_POINTER_OFFSET)
    image_end = IMAGE_APP_BASE + len(image)

    if not (SRAM_START <= stack_pointer <= SRAM_END) or stack_pointer % 4 != 0:
        log_system(f"stack pointer 0x{stack_pointer:08X} is not in SRAM", "#ef4444")
        return STATUS_FAILED
    if reset_vector % 2 == 0 or not (IMAGE_APP_BASE <= reset_vector & ~1 < image_end):
        log_system(f"reset vector 0x{reset_vector:08X} is not inside image", "#ef4444")
        return STATUS_FAILED
    if tag_pointer % 4 != 0 or not (IMAGE_APP_BASE + IMAGE_TAG_POINTER_OFFSET < tag_pointer <= image_end - 4):
        log_system(f"end tag pointer 0x{tag_pointer:08X} is outside image", "#ef4444")
        return STATUS_FAILED
    keyword = read_word(image, tag_pointer - IMAGE_APP_BASE)
    if keyword != IMAGE_TAG_KEYWORD:
        log_system(f"end tag holds 0x{keyword:08X}, expected 0x{IMAGE_TAG_KEYWORD:08X}", "#ef4444")
        return STATUS_FAILED

    log_system(f"Image validated ok: {len(image)} bytes, SP=0x{stack_pointer:08X}, reset=0x{reset_vector:08X}", "#10b981")
    return STATUS_OK
