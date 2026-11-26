"""@file device_profiles.py
@brief Registry of every supported device type: identity, BLE transport, crypto, capabilities and detection rules
@version 1.2
@date 2026-11-22

A profile key names the PROTOCOL family a session speaks ('valtrack', 'sc2_gen2', 'sc2_gen3', 'gigo_delphis',
'gigo_beacon', 'gigo_dt', 'gigo_gas'); a device type adds the product name the operator picks or sees (Valtrack V1 and
Valtrack 2 share one protocol). Facts come from each device's firmware SOURCE (where a document differs, the code wins):
    Valtrack V1/V2  STM32WB55   service 0xFE40, char 0xFE41, Valtrack AES key, auth x79
    SC2 Gen2/Gen3   STM32+nRF   service 0xBA00, char 0xBA01, SC2 key, auth x75; Gen3 BLE firmware major >= 7
    GiGO Delphis    nRF52       service 0xAB00, char 0xAB01 (ble_gigo_service.c; doc 03 says 0xAB05), GiGO key, x79
    GiGO beacon     nRF52       same service, characteristic and name ("Succorfish Gigo") as Delphis; adds 0xB7..0xBA
    GiGO D&T        nRF52805    service 0xAB00, char 0xAB05, name "Succorfish D&T"; control 0x03..0x09, logger 0x10..0x2A
    GiGO Gas        nRF52 + BME690   service 0xAB00, char 0xAB05, name "Succorfish Gas"; D&T opcodes + 0x30..0x37
Valtrack V1 / 2, Delphis (MyGearTag) and the beacon all advertise "Succorfish Gigo". Before connecting, the manufacturer
data tells Valtrack (company id 0x8301: the ST BLE SDK version byte 0x01 then CFG_DEV_ID_P2P_SERVER1 0x83, app_ble.c)
from the nRF devices (Nordic id 0x0059 with address, battery and firmware). The characteristic splits the GiGO nRF
devices in two pairs. Delphis and the beacon cannot be told apart from the advertisement: after authentication 0xB8
is probed, which only the beacon answers (Delphis drops unknown opcodes). The D&T and gas loggers are told apart by
name, else by 0x34 (gas: device information; D&T: INVALID_PARAM). NetTag and "GIGO_4G" devices are no longer in use.
"""
from dataclasses import dataclass


VALTRACK_CMD_CHAR = "0000fe41-8e22-4541-9d4c-21edae82ed19"
SC2_HUB_SERVICE = "0000ba00-0143-08b2-a708-e5f9b34fb005"
SC2_HUB_CMD_CHAR = "0000ba01-0143-08b2-a708-e5f9b34fb005"
GIGO_SERVICE = "0000ab00-0143-0800-0008-e5f9b34fb000"
# Delphis and the Rotation / Jack beacon (BLE_UUID_GIGO_CMD_CHARACTERISTIC 0xab01). Earlier builds of this tool took
# it for an old Valtrack characteristic, so both devices were connected as Valtrack.
GIGO_IMU_CMD_CHAR = "0000ab01-0143-0800-0008-e5f9b34fb000"
# Depth & Temp and gas loggers (0xab05).
GIGO_LOGGER_CMD_CHAR = "0000ab05-0143-0800-0008-e5f9b34fb000"
STM_OTA_SERVICE = "0000fe20-cc7a-482a-984a-7f2ed5b3e58f"

AUTH_MULTIPLIER_GIGO = 79
AUTH_MULTIPLIER_SC2_HUB = 75
SC2_GEN3_MIN_FW_MAJOR = 7

# Manufacturer-data company ids (little-endian first two bytes of the AD structure)
NORDIC_COMPANY_ID = 0x0059
VALTRACK_COMPANY_ID = 0x8301

# Probe opcodes that only one GiGO family of a pair answers (see the module header).
PROBE_BEACON_OPCODE = 0xB8
PROBE_GAS_OPCODE = 0x34
PROBE_TIMEOUT_S = 1.5


@dataclass(frozen=True)
class DeviceProfile:
  """What the monitor needs to know to talk to one protocol family."""
  key: str
  label: str
  hardware: str
  cmd_char_uuid: str
  auth_multiplier: int
  uses_sc2_hub_key: bool
  has_debug_log: bool
  has_cycle_telemetry: bool
  config_window: str


PROFILES = {
  "valtrack": DeviceProfile("valtrack", "Valtrack", "STM32WB55 + A7672 modem + GNSS", VALTRACK_CMD_CHAR,
                            AUTH_MULTIPLIER_GIGO, False, True, True, "valtrack"),
  "sc2_gen2": DeviceProfile("sc2_gen2", "SC2 Gen2", "STM32 + nRF52840 BLE co-processor", SC2_HUB_CMD_CHAR,
                            AUTH_MULTIPLIER_SC2_HUB, True, True, False, "sc2_hub"),
  "sc2_gen3": DeviceProfile("sc2_gen3", "SC2 Gen3", "STM32 + nRF5340 BLE co-processor", SC2_HUB_CMD_CHAR,
                            AUTH_MULTIPLIER_SC2_HUB, True, True, False, "sc2_hub"),
  "gigo_delphis": DeviceProfile("gigo_delphis", "GiGO Delphis", "nRF52 + ICM-20948 IMU + nano-modem",
                                GIGO_IMU_CMD_CHAR, AUTH_MULTIPLIER_GIGO, False, False, False, "gigo"),
  "gigo_beacon": DeviceProfile("gigo_beacon", "GiGO Rotation / Jack", "nRF52 beacon + ICM-20948 IMU",
                               GIGO_IMU_CMD_CHAR, AUTH_MULTIPLIER_GIGO, False, False, False, "gigo"),
  "gigo_dt": DeviceProfile("gigo_dt", "GiGO Depth & Temp", "nRF52805 + Keller pressure sensor", GIGO_LOGGER_CMD_CHAR,
                           AUTH_MULTIPLIER_GIGO, False, False, False, "dt_logger"),
  "gigo_gas": DeviceProfile("gigo_gas", "GiGO Gas", "nRF52 + Bosch BME690 gas sensor", GIGO_LOGGER_CMD_CHAR,
                            AUTH_MULTIPLIER_GIGO, False, False, False, "gas_logger"),
}

# Older builds of the monitor called the D&T logger profile "sc2"; sessions or settings using it still resolve.
LEGACY_PROFILE_KEYS = {"sc2": "gigo_dt"}

# Device types offered in the 'Device type' selector: (type key, label, profile key; None = auto-detect).
DEVICE_TYPES = (
  ("auto", "Auto-detect", None),
  ("valtrack_v1", "Valtrack V1", "valtrack"),
  ("valtrack_v2", "Valtrack 2", "valtrack"),
  ("sc2_gen2", "SC2 Gen2", "sc2_gen2"),
  ("sc2_gen3", "SC2 Gen3", "sc2_gen3"),
  ("gigo_delphis", "GiGO Delphis", "gigo_delphis"),
  ("gigo_beacon", "GiGO Rotation / Jack movement", "gigo_beacon"),
  ("gigo_dt", "GiGO Depth & Temp", "gigo_dt"),
  ("gigo_gas", "GiGO Gas (BME690)", "gigo_gas"),
)


def normalise_profile(key: "str | None") -> "str | None":
  """Map a legacy key to its current one; unknown or empty keys give None."""
  if not key:
    return None
  key = LEGACY_PROFILE_KEYS.get(key, key)
  return key if key in PROFILES else None


def profile(key: "str | None") -> "DeviceProfile | None":
  """The DeviceProfile of a (possibly legacy) key, or None."""
  normalised = normalise_profile(key)
  return PROFILES.get(normalised) if normalised else None


def profile_label(key: "str | None", device_type: "str | None" = None) -> str:
  """Display name: the chosen device type when it belongs to the profile, else the profile label, else 'Unknown'."""
  for type_key, type_label, type_profile in DEVICE_TYPES:
    if device_type and type_key == device_type and type_profile == normalise_profile(key):
      return type_label
  found = profile(key)
  if found is None:
    return "Unknown"
  return "Valtrack V1 / 2" if found.key == "valtrack" else found.label


def device_type_profile(type_key: "str | None") -> "str | None":
  """Profile key of a device type ('valtrack_v2' -> 'valtrack'); None for auto-detect or an unknown type."""
  for key, _label, profile_key in DEVICE_TYPES:
    if key == type_key:
      return profile_key
  return None


def classify_advertisement(name: str, service_uuids: "list[str]", manufacturer: "bytes | None",
                           company_ids=()) -> dict:
  """Best guess from a scan result, before connecting. manufacturer is the Nordic (0x0059) payload, company_ids every
  manufacturer company id seen. Returns {'profile', 'is_ota', 'fw', 'battery_pct', 'certain', 'kind'}; 'certain' is
  False when the guess must be confirmed after connecting (Delphis vs beacon, or a GiGO device without its usual
  name); 'kind' is a short text for the scan list.
  """
  lowered = (name or "").lower()
  uuids = [uuid.lower() for uuid in (service_uuids or [])]
  result = {"profile": None, "is_ota": False, "fw": "", "battery_pct": None, "certain": False, "kind": ""}
  data = bytes(manufacturer) if manufacturer else b""

  if lowered.startswith("stm_ota") or STM_OTA_SERVICE in uuids:
    result.update(profile="valtrack", is_ota=True, certain=True, kind="Valtrack bootloader")
    return result

  if VALTRACK_COMPANY_ID in company_ids:
    result.update(profile="valtrack", certain=True, kind="Valtrack V1 / 2")
    return result

  if SC2_HUB_SERVICE in uuids or lowered.startswith("succorfish sc2"):
    if len(data) >= 8:
      result["fw"] = f"v{data[6]}.{data[7]}"
      result["profile"] = "sc2_gen3" if data[6] >= SC2_GEN3_MIN_FW_MAJOR else "sc2_gen2"
      result["certain"] = True
    else:
      result["profile"] = "sc2_gen2"
    result["kind"] = profile_label(result["profile"])
    return result

  if len(data) >= 10:
    result["battery_pct"] = data[6]
    result["fw"] = f"v{data[8]}.{data[9]}"

  if "d&t" in lowered:
    result.update(profile="gigo_dt", certain=True, kind="Depth & Temp")
    return result

  if "succorfish gas" in lowered:
    result.update(profile="gigo_gas", certain=True, kind="Gas")
    return result

  if GIGO_SERVICE in uuids or len(data) >= 10:
    # A 10-byte 0x0059 payload is the GiGO nRF scan response (address, battery, charging, firmware); Valtrack sends
    # its own company id instead (handled above).
    result.update(profile="gigo_beacon", kind="Delphis or Rotation / Jack")
    return result

  if "gigo" in lowered:
    # "Succorfish Gigo" without manufacturer data (passive scan): Valtrack, Delphis or beacon; GATT decides.
    result.update(profile="valtrack", kind="Gigo (type after connect)")
  return result


# GiGO roles chosen by configuration (the split Delphis / beacon firmware keeps the flags of the original combined
# "GIGO beacon + MyGearTag" firmware): what the device does decides what the monitor offers.
ROLE_ACOUSTIC = "acoustic"
ROLE_IMU = "imu"
ROLE_NET_DRUM = "net_drum"
ROLE_JACK = "jack"
ROLE_ADVERTISING = "advertising"
ROLE_NAMES = {ROLE_ACOUSTIC: "acoustic modem (messages, pictures)", ROLE_IMU: "IMU (attitude)",
              ROLE_NET_DRUM: "rotation (net drum)", ROLE_JACK: "hydraulic jack movement",
              ROLE_ADVERTISING: "BLE advertising only (nothing enabled)"}


def gigo_roles(profile_key: str, config: "dict | None", imu_mode: "int | None" = None) -> tuple:
  """Roles of a GiGO Delphis / beacon from its 0x06 configuration (and the beacon's 0xB8 mode):
  Delphis: nano_modem_enable -> acoustic, imu_enable -> imu; beacon: imu_enable with mode 1 -> net drum, 2 -> jack.
  Nothing enabled -> (ROLE_ADVERTISING,). An unread configuration gives ().
  """
  if not config:
    return ()
  roles = []
  if profile_key == "gigo_delphis":
    if config.get("nano_modem_enable"):
      roles.append(ROLE_ACOUSTIC)
    if config.get("imu_enable"):
      roles.append(ROLE_IMU)
  elif profile_key == "gigo_beacon" and config.get("imu_enable"):
    roles.append({1: ROLE_NET_DRUM, 2: ROLE_JACK}.get(imu_mode, ROLE_IMU) if imu_mode else ROLE_ADVERTISING)
  return tuple(roles) or (ROLE_ADVERTISING,)


def roles_text(roles: tuple) -> str:
  """'acoustic modem (messages, pictures) + IMU (attitude)' etc."""
  return " + ".join(ROLE_NAMES.get(role, role) for role in roles) if roles else "configuration not read"


def classify_gatt(characteristics: "set[str]") -> "str | None":
  """Protocol family from the characteristics found after connecting: 'valtrack', 'sc2', 'gigo_imu' (Delphis or
  beacon, 0xAB01), 'gigo_logger' (D&T or gas, 0xAB05), or None when no known command characteristic is present.
  """
  lowered = {uuid.lower() for uuid in characteristics}
  if VALTRACK_CMD_CHAR in lowered:
    return "valtrack"
  if SC2_HUB_CMD_CHAR in lowered:
    return "sc2"
  if GIGO_IMU_CMD_CHAR in lowered:
    return "gigo_imu"
  if GIGO_LOGGER_CMD_CHAR in lowered:
    return "gigo_logger"
  return None


def gatt_family_profiles(gatt_family: "str | None") -> tuple:
  """Profiles that can sit behind a GATT family ('gigo_imu' -> Delphis, beacon)."""
  return {"valtrack": ("valtrack",), "sc2": ("sc2_gen2", "sc2_gen3"), "gigo_imu": ("gigo_delphis", "gigo_beacon"),
          "gigo_logger": ("gigo_dt", "gigo_gas")}.get(gatt_family or "", ())
