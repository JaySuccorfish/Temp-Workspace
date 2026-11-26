"""@file constants.py
@brief Brand palette, protocol opcodes, BLE UUIDs and lookup tables shared by the whole monitor
@version 2.3
@date 2026-11-21

Change a value here, not at the call sites: every module imports these names. Device types live in device_profiles.py
and the byte layouts of the configuration frames in config_codecs.py.
"""
import re
import sys

if sys.platform == "darwin":
    FONT_UI_FAMILY = "Helvetica Neue"
    FONT_MONO_FAMILY = "Menlo"
elif sys.platform.startswith("win"):
    FONT_UI_FAMILY = "Segoe UI"
    FONT_MONO_FAMILY = "Consolas"
else:
    FONT_UI_FAMILY = "DejaVu Sans"
    FONT_MONO_FAMILY = "DejaVu Sans Mono"


def ui_font(size: int = 9, *styles: str) -> tuple:
    """Return a platform-appropriate UI font tuple."""
    return (FONT_UI_FAMILY, size, *styles)


def mono_font(size: int = 9, *styles: str) -> tuple:
    """Return a platform-appropriate monospace font tuple."""
    return (FONT_MONO_FAMILY, size, *styles)


# ---------------------------------------------------------------------------------------------
# BRAND
#
# One place for every colour, font and string the UI uses. Restyling the whole app means editing
# this dict, not hunting hex literals through 1400 lines. The legacy hex values the original code
# used are kept as aliases below so nothing that referenced them breaks.
# ---------------------------------------------------------------------------------------------
BRAND = {
    # Product identity
    "name":        "SUCCORFISH",
    "product":     "BLE Device Monitor",
    "company":     "Succorfish",
    "tagline":     "SUCCORFISH  BLE  DEVICE  MONITOR",
    "version":     "2.3.0",
    "copyright":   "(c) Succorfish",

    # Surfaces (darkest -> lightest)
    "bg":          "#0b141a",   # window / app background
    "surface":     "#121a21",   # panels, tab body
    "surface_alt": "#17222b",   # inputs, list rows
    "border":      "#1f2b35",

    # Brand accent
    "accent":      "#00b0b9",   # Succorfish teal
    "accent_dark": "#00808c",
    "accent_text": "#ffffff",

    # Text
    "text":        "#e6eef2",
    "text_dim":    "#94b2be",
    "text_muted":  "#5f7480",

    # Semantic
    "ok":          "#10b981",
    "warn":        "#f59e0b",
    "err":         "#ef4444",
    "info":        "#38bdf8",

    # Type
    "font_ui":     (FONT_UI_FAMILY, 9),
    "font_ui_b":   (FONT_UI_FAMILY, 9, "bold"),
    "font_h":      (FONT_UI_FAMILY, 12, "bold"),
    "font_mono":   (FONT_MONO_FAMILY, 9),
    "font_mono_b": (FONT_MONO_FAMILY, 9, "bold"),
}

# Legacy aliases -- keeps the original call sites working if they are ever restored verbatim.
C_BG = BRAND["bg"]
C_SURFACE = BRAND["surface"]
C_TEXT = BRAND["text"]


# --------------------------------------------------------------------------
# Constants & Crypto Config
# --------------------------------------------------------------------------
AES_BLOCK_SIZE = 16

AES_KEY_GIGO_4G = bytes([
    0x39, 0x99, 0xab, 0x12, 0x13, 0x88, 0x98, 0x47,
    0x33, 0x08, 0x49, 0x21, 0x12, 0xd4, 0x65, 0x79
])

AES_KEY_SC2_HUB = bytes([
    0x34, 0x99, 0xab, 0x12, 0x13, 0x88, 0x98, 0x47,
    0x33, 0x08, 0x49, 0x21, 0x12, 0xd4, 0x65, 0x74
])

GIGO_P2P_SERVICE_UUID = "0000fe40-cc7a-482a-984a-7f2ed5b3e58f"

# Valtrack command characteristic. 0x...ab01 was listed here as a "legacy firmware" one, but it is the GiGO Delphis /
# Rotation-Jack characteristic (device_profiles.GIGO_IMU_CMD_CHAR), so those devices were taken for a Valtrack.
COMMAND_CHAR_UUIDS = [
    "0000fe41-8e22-4541-9d4c-21edae82ed19",
]

# SC2 Gen2 (nRF52840) & SC2 Gen3 (nRF5340) Hub Service
SC2_HUB_SERVICE_UUID  = "0000ba00-0143-08b2-a708-e5f9b34fb005"
SC2_HUB_CMD_CHAR_UUID = "0000ba01-0143-08b2-a708-e5f9b34fb005"
SC2_HUB_DOTA_CHAR_UUID= "0000ba02-0143-08b2-a708-e5f9b34fb005"
SC2_HUB_BIN_CHAR_UUID = "0000ba03-0143-08b2-a708-e5f9b34fb005"

# DOTA Bootloader (STM_OTA) Constants
STM_OTA_SERVICE_UUID        = "0000fe20-cc7a-482a-984a-7f2ed5b3e58f"
STM_OTA_CTRL_CHAR           = "0000fe22-8e22-4541-9d4c-21edae82ed19"
STM_OTA_CONF_CHAR           = "0000fe23-8e22-4541-9d4c-21edae82ed19"
STM_OTA_DATA_CHAR           = "0000fe24-8e22-4541-9d4c-21edae82ed19"
REBOOT_CHAR_UUID            = "0000fe11-8e22-4541-9d4c-21edae82ed19"

REBOOT_MODE_BLE_OTA         = 0x01
APP_START_SECTOR            = 8
APP_SECTOR_COUNT_MAX        = 84
FLASH_SECTOR_BYTES          = 0x1000
COMMAND_APPLICATION_UPLOAD  = 0x02
COMMAND_UPLOAD_FINISHED     = 0x07
APP_BASE_ADDRESS_FIELD      = bytes([0x00, 0x80, 0x00])
CONFIRMATION_REBOOT         = 0x01

RAW_DATA_MAX_BYTES          = 248
ATT_HEADER_BYTES            = 3
DOUBLE_WORD_BYTES           = 8

IMAGE_TAG_POINTER_OFFSET    = 0x140
IMAGE_TAG_KEYWORD           = 0x94448A29
IMAGE_APP_BASE              = 0x08008000
SRAM_START                  = 0x20000000
SRAM_END                    = 0x20030000

STATUS_OK                   = 0
STATUS_FAILED               = 1
STATUS_DROPPED              = 2

DEFAULT_SUCCORFISH_NAME = "Succorfish Gigo"

# Auth opcodes (RAW, not AES-encrypted)
OPCODE_READ_AUTH_KEY      = 0x01
OPCODE_SEND_CONN_AUTH_KEY = 0x01
OPCODE_SET_AUTH_VALUE     = 0x02
OPCODE_SEND_AUTH_STATUS   = 0x02

# Active Opcodes
BLE_SEND_DEBUG_LOG     = 0xFD

# Valtrack status opcodes (app_ble_p2p.c, doc 07). Motion, low power, log erase and LEDs are 0xC1 fields / 0xFC, the
# live position is 0xE3: the 0xD1..0xD3 opcodes earlier builds of this tool sent were never implemented by the device.
BLE_READ_FIRMWARE_VERSION = 0xE5   # -> FW major+minor (BLE_OPCODE_TRACK_FW_VERSION; 0xD0 is an alias)
BLE_OPCODE_TRACK_BATT_PERCENT = 0xE4 # -> Battery percent + mV
BLE_OPCODE_SET_TIME       = 0x11   # -> RTC epoch time read/write
BLE_OPCODE_LIVE_TRACK     = 0xE3   # -> last GPS fix
BLE_OPCODE_FACTORY_RESET  = 0xE6   # -> defaults, logs and token cleared
BLE_CMD_LED_CONTROL       = 0xFC   # -> 'all LEDs' flag

OPCODE_CONFIG_DEVICE   = 0xC1
OPCODE_CONFIG_SIM      = 0xC3
OPCODE_CONFIG_SERVER   = 0xC4
OPCODE_DEVICE_RESET    = 0xC5
OPCODE_IMEI_REQ        = 0xE1
OPCODE_TOKEN_VALIDATION= 0xE2

# Scan name filters. Valtrack V1 / 2, GiGO Delphis (MGT) and the Rotation / Jack beacon all advertise "Succorfish Gigo"
# (told apart by manufacturer data and after connecting). NetTag and "GIGO_4G" devices are no longer in use.
ADV_NAME_PRESETS = [
    ("All Succorfish devices", "Succorfish"),
    ("Succorfish Gigo", "Succorfish Gigo"),
    ("Succorfish D&T", "Succorfish D&T"),
    ("Succorfish Gas", "Succorfish Gas"),
    ("Succorfish SC2", "Succorfish SC2"),
    ("STM_OTA (Valtrack bootloader)", "STM_OTA"),
    ("All BLE Devices", ""),
]


# --------------------------------------------------------------------------
# Diagnostics Dictionaries
# --------------------------------------------------------------------------
HCI_ERROR_CODES = {
    0x00: "Success",
    0x05: "Authentication Failure / Auth Timeout",
    0x08: "Connection Timeout",
    0x13: "Remote User Terminated Connection",
    0x14: "Remote Device Terminated Connection (low resources)",
    0x15: "Remote Device Terminated Connection (power off)",
    0x16: "Local Host Terminated Connection",
    0x1A: "Unsupported Remote Feature",
    0x1F: "Unspecified Error",
    0x22: "LL Response Timeout / Instant Passed",
    0x23: "LL Procedure Collision",
    0x28: "Instant Passed",
    0x29: "Pairing / Key Missing",
    0x3B: "Unacceptable Connection Parameters",
    0x3D: "Connection Failed to be Established",
    0x3E: "Connection Failed to be Established (MIC failure)",
    0xFF: "No disconnect event recorded",
}

DIAG_FAULT_NAMES = {
    0: "NONE",
    1: "HARDFAULT",
    2: "STACK_OVERFLOW",
    3: "MALLOC_FAILED",
    4: "ASSERT",
    5: "GSM_RESET",
    6: "BLE_RESET",
    7: "ADC_ERROR",
    8: "HARDFAULT_BAD_STACK",
    9: "NMI",
    10: "HAL_ERROR",
}

DIAG_RESET_RE = re.compile(r"DIAG rst\[(?P<flags>[^\]]*)\]\s+up=(?P<up>\d+)ms\s+disc=0x(?P<disc>[0-9A-Fa-f]{2})\s+heapMin=(?P<heap>\d+)")
DIAG_FAULT_RE = re.compile(r"DIAG fault=(?P<fault>\d+)\s+a=0x(?P<a>[0-9A-Fa-f]+)\s+b=0x(?P<b>[0-9A-Fa-f]+)")
DIAG_ALIVE_RE = re.compile(
    r"DIAG prev alive=(?P<alive>\d+)ms\s+crumb=0x(?P<crumb>[0-9A-Fa-f]+)(?:\s+task='(?P<task>[^']*)')?"
)

DIAG_CRUMB_NAMES = {
    100: "APPSM_GSM_BOOT_DONE",
    101: "APPSM_BEFORE_GPS_BOOT",
    102: "APPSM_GPS_BOOT_DONE",
    200: "GPS_TASK_CMD_RECEIVED",
    201: "GPS_BEFORE_POWERON",
    202: "GPS_AFTER_POWERON",
    203: "GPS_BEFORE_SETTLE_DELAY",
    204: "GPS_AFTER_SETTLE_DELAY",
    205: "GPS_BEFORE_UART_FLAG_CLEAR",
    206: "GPS_BEFORE_ENABLE_IRQ",
    207: "GPS_AFTER_ENABLE_IRQ",
    208: "GPS_BEFORE_INIT",
    209: "GPS_AFTER_INIT",
}


SERVER_HOST_OFFSET = 2
SERVER_HOST_LEN    = 33
