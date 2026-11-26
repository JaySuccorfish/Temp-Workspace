"""@file motion_analysis.py
@brief Replays the Rotation / Jack beacon's movement algorithms on the 0xB2 IMU stream for the live display
@version 1.0
@date 2026-11-22

The beacon sends no rotation count and no jack state over BLE (gigo-beacon-rotation-jack-VSCode app_protocol.c):
it only streams the IMU sample it took in the same 500 ms tick in which it ran rotation.c or jack.c (main.c
imuPollTimerHandler). The classes below are line-by-line ports of those two files, fed with the streamed samples, so
the monitor shows what the beacon decides. They start from zero when the monitor starts listening, while the beacon
has counted since its last reset: the counts agree from the moment both start together (beacon reset or mode write).
Pure Python, no Tk: unit tested in tests/test_live_views.py.
"""
import math


# rotation.c
ROTATION_SAMPLE_DT_S = 0.01
ROTATION_LPF_ALPHA = 0.97
ROTATION_COMPLEMENTARY_ALPHA = 0.98
RAD_TO_DEG = 57.2958

# rotation.h RotationDirection_t
DIR_STATIONARY = 0
DIR_MOVING_UP = 1
DIR_MOVING_DOWN = 2
DIR_MOVING_LEFT = 3
DIR_MOVING_RIGHT = 4
DIR_CW = 5
DIR_CCW = 6
DIRECTION_NAMES = {DIR_STATIONARY: "still", DIR_MOVING_UP: "up", DIR_MOVING_DOWN: "down", DIR_MOVING_LEFT: "left",
                   DIR_MOVING_RIGHT: "right", DIR_CW: "clockwise", DIR_CCW: "counter-clockwise"}
UPWARD_DIRECTIONS = (DIR_MOVING_UP, DIR_MOVING_RIGHT, DIR_CW)
DOWNWARD_DIRECTIONS = (DIR_MOVING_DOWN, DIR_MOVING_LEFT, DIR_CCW)

# Beacon actions (RotationBeaconAction_t / JackBeaconAction_t share the values)
ACTION_NONE = 0
ACTION_SUPPRESS = 1
ACTION_RESUME = 2
ACTION_NAMES = {ACTION_NONE: "", ACTION_SUPPRESS: "advertising suppressed", ACTION_RESUME: "advertising resumed"}

# app_config_store.c defaults
DEFAULT_ANGLE_THRESHOLD = 2
DEFAULT_DEAD_ZONE = 1
DEFAULT_DEBOUNCE_MS = 200
DEFAULT_ROTATION_COUNT = 3
DEFAULT_JACK_SENSITIVITY = 1

# jack.c: (acceleration motion in g, angular rate in deg/s) per sensitivity; unknown values use medium like the
# firmware's switch default.
JACK_THRESHOLDS = {0: (0.30, 10.0), 1: (0.15, 5.0), 2: (0.05, 1.0)}
JACK_GRAVITY_G = 1.0

# Beacon IMU modes (icm20948.h ImuOperationalMode_t)
IMU_MODE_DISABLED = 0
IMU_MODE_NET_DRUM = 1
IMU_MODE_HYDRAULIC_JACK = 2

FULL_TURN_DEG = 360.0
MS_PER_S = 1000.0


def _vector(sample: dict, prefix: str) -> tuple:
  """(x, y, z) of 'acc' or 'gyro' from a decoded 0xB2 sample."""
  return (float(sample[f"{prefix}_x"]), float(sample[f"{prefix}_y"]), float(sample[f"{prefix}_z"]))


class RotationCounter:
  """Port of rotation.c: complementary filter, direction classification, debounce and the CW / CCW counters that
  suppress the beacon after N upward events and resume it after N downward ones. update() also integrates the angular
  rate about z with the real sample spacing (drum_angle_deg) for the dial, which the firmware does not do.
  """

  def __init__(self, angle_threshold: int = DEFAULT_ANGLE_THRESHOLD, dead_zone: int = DEFAULT_DEAD_ZONE,
               debounce_ms: int = DEFAULT_DEBOUNCE_MS, rotation_count: int = DEFAULT_ROTATION_COUNT):
    """Tuning as read with 0xBA (firmware defaults otherwise)."""
    self.angle_threshold = angle_threshold
    self.dead_zone = dead_zone
    self.debounce_ms = debounce_ms
    self.rotation_count = rotation_count
    self.reset(0.0)

  def set_tuning(self, tuning: dict) -> None:
    """Take angle_threshold, dead_zone, debounce_ms and rotation_count from a decoded 0xBA reply."""
    self.angle_threshold = int(tuning.get("angle_threshold", self.angle_threshold))
    self.dead_zone = int(tuning.get("dead_zone", self.dead_zone))
    self.debounce_ms = int(tuning.get("debounce_ms", self.debounce_ms))
    self.rotation_count = int(tuning.get("rotation_count", self.rotation_count))

  def reset(self, now_ms: float) -> None:
    """RotationInit(): filters, debounce timer and counters back to zero, beacon advertising."""
    self.filtered_pitch = 0.0
    self.filtered_roll = 0.0
    self.integrated_pitch = 0.0
    self.integrated_roll = 0.0
    self.previous_pitch = 0.0
    self.previous_roll = 0.0
    self.integrated_z = 0.0
    self.last_detected_ms = now_ms
    self.cw_count = 0
    self.ccw_count = 0
    self.suppressed = False
    self.up_events = 0
    self.down_events = 0
    self.drum_angle_deg = 0.0
    self.last_sample_ms = None

  def _classify(self, acc: tuple, gyro: tuple, now_ms: float) -> int:
    """classifyDirection(): one direction per sample, or still."""
    ax, ay, az = (abs(value) for value in acc)
    if az > ay and az > ax:
      pitch = math.atan2(acc[1], math.sqrt(ax * ax + az * az)) * RAD_TO_DEG
      roll = math.atan2(acc[0], math.sqrt(ay * ay + az * az)) * RAD_TO_DEG
    elif ay > az and ay > ax:
      pitch = math.atan2(acc[2], math.sqrt(ax * ax + ay * ay)) * RAD_TO_DEG
      roll = math.atan2(acc[0], math.sqrt(ay * ay + az * az)) * RAD_TO_DEG
    else:
      pitch = math.atan2(acc[2], math.sqrt(ay * ay + ax * ax)) * RAD_TO_DEG
      roll = math.atan2(acc[1], math.sqrt(ax * ax + az * az)) * RAD_TO_DEG

    self.filtered_pitch = ROTATION_LPF_ALPHA * pitch + (1.0 - ROTATION_LPF_ALPHA) * self.filtered_pitch
    self.filtered_roll = ROTATION_LPF_ALPHA * roll + (1.0 - ROTATION_LPF_ALPHA) * self.filtered_roll
    self.integrated_pitch = (ROTATION_COMPLEMENTARY_ALPHA * (self.integrated_pitch + gyro[0] * ROTATION_SAMPLE_DT_S)
                             + (1.0 - ROTATION_COMPLEMENTARY_ALPHA) * self.filtered_pitch)
    self.integrated_roll = (ROTATION_COMPLEMENTARY_ALPHA * (self.integrated_roll + gyro[1] * ROTATION_SAMPLE_DT_S)
                            + (1.0 - ROTATION_COMPLEMENTARY_ALPHA) * self.filtered_roll)
    delta_pitch = self.integrated_pitch - self.previous_pitch
    delta_roll = self.integrated_roll - self.previous_roll
    self.previous_pitch = self.integrated_pitch
    self.previous_roll = self.integrated_roll
    self.integrated_z += gyro[2] * ROTATION_SAMPLE_DT_S

    debounced = (now_ms - self.last_detected_ms) > self.debounce_ms
    direction = DIR_STATIONARY
    if abs(delta_pitch) > self.dead_zone and abs(delta_pitch) > self.angle_threshold and debounced:
      direction = DIR_MOVING_UP if delta_pitch > 0.0 else DIR_MOVING_DOWN
      self.last_detected_ms = now_ms
    elif abs(delta_roll) > self.dead_zone and abs(delta_roll) > self.angle_threshold and debounced:
      direction = DIR_MOVING_RIGHT if delta_roll > 0.0 else DIR_MOVING_LEFT
      self.last_detected_ms = now_ms
    elif abs(self.integrated_z) > self.angle_threshold and debounced:
      direction = DIR_CW if self.integrated_z > 0.0 else DIR_CCW
      self.last_detected_ms = now_ms
      self.integrated_z = 0.0
    return direction

  def update(self, sample: dict, now_ms: float) -> dict:
    """RotationProcess() on one decoded 0xB2 sample taken at now_ms. Returns {'direction', 'direction_text',
    'action', 'cw_count', 'ccw_count', 'suppressed', 'up_events', 'down_events', 'rate_dps', 'drum_angle_deg',
    'turns'}.
    """
    acc = _vector(sample, "acc")
    gyro = _vector(sample, "gyro")
    direction = self._classify(acc, gyro, now_ms)
    action = ACTION_NONE
    if direction in UPWARD_DIRECTIONS:
      self.up_events += 1
    elif direction in DOWNWARD_DIRECTIONS:
      self.down_events += 1

    if direction in UPWARD_DIRECTIONS and not self.suppressed:
      self.cw_count += 1
      if self.cw_count >= self.rotation_count:
        self.suppressed = True
        self.cw_count = 0
        action = ACTION_SUPPRESS
    elif direction in DOWNWARD_DIRECTIONS and self.suppressed:
      self.ccw_count += 1
      if self.ccw_count >= self.rotation_count:
        self.suppressed = False
        self.ccw_count = 0
        action = ACTION_RESUME

    if self.last_sample_ms is not None and now_ms > self.last_sample_ms:
      self.drum_angle_deg += gyro[2] * (now_ms - self.last_sample_ms) / MS_PER_S
    self.last_sample_ms = now_ms
    return {"direction": direction, "direction_text": DIRECTION_NAMES[direction], "action": action,
            "cw_count": self.cw_count, "ccw_count": self.ccw_count, "suppressed": self.suppressed,
            "up_events": self.up_events, "down_events": self.down_events, "rate_dps": gyro[2],
            "drum_angle_deg": self.drum_angle_deg, "turns": self.drum_angle_deg / FULL_TURN_DEG}


def jack_thresholds(sensitivity: int) -> tuple:
  """(acceleration g, angular rate deg/s) trigger levels of a jack sensitivity (jack.c getThresholds)."""
  return JACK_THRESHOLDS.get(sensitivity, JACK_THRESHOLDS[DEFAULT_JACK_SENSITIVITY])


def jack_measure(sample: dict, sensitivity: int) -> dict:
  """jack.c sampleIsMoving() on one decoded 0xB2 sample: {'acc_motion_g', 'gyro_dps', 'acc_threshold_g',
  'gyro_threshold_dps', 'moving'}.
  """
  acc = _vector(sample, "acc")
  gyro = _vector(sample, "gyro")
  acc_threshold, gyro_threshold = jack_thresholds(sensitivity)
  acc_motion = abs(math.sqrt(sum(value * value for value in acc)) - JACK_GRAVITY_G)
  gyro_magnitude = math.sqrt(sum(value * value for value in gyro))
  return {"acc_motion_g": acc_motion, "gyro_dps": gyro_magnitude, "acc_threshold_g": acc_threshold,
          "gyro_threshold_dps": gyro_threshold,
          "moving": acc_motion > acc_threshold or gyro_magnitude > gyro_threshold}


class JackDetector:
  """Port of jack.c: moving / stationary per sample, a beacon action on every change; adds the number of
  movements and the time spent moving for the display.
  """

  def __init__(self, sensitivity: int = DEFAULT_JACK_SENSITIVITY):
    """Sensitivity as read with 0xBA (0 low, 1 medium, 2 high)."""
    self.sensitivity = sensitivity
    self.reset()

  def reset(self) -> None:
    """JackInit(): stationary, counters cleared."""
    self.was_moving = False
    self.movements = 0
    self.moving_s = 0.0
    self.last_sample_ms = None

  def update(self, sample: dict, now_ms: float) -> dict:
    """JackProcess() on one sample: jack_measure() plus {'action', 'movements', 'moving_s'}. The time between two
    samples counts as moving when the earlier one was moving (from the first moving sample to the next still one).
    """
    result = jack_measure(sample, self.sensitivity)
    if self.was_moving and self.last_sample_ms is not None and now_ms > self.last_sample_ms:
      self.moving_s += (now_ms - self.last_sample_ms) / MS_PER_S
    self.last_sample_ms = now_ms
    action = ACTION_NONE
    if result["moving"] != self.was_moving:
      action = ACTION_SUPPRESS if result["moving"] else ACTION_RESUME
      self.was_moving = result["moving"]
      if result["moving"]:
        self.movements += 1
    result.update(action=action, movements=self.movements, moving_s=self.moving_s)
    return result
