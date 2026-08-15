"""Pelco-D frame construction for the Bosch MIC 612.

Every frame is 7 bytes:

    FF ADDR CMD1 CMD2 DATA1 DATA2 CS      CS = (ADDR..DATA2) mod 256

Byte patterns here were audited against the official Pelco documents
(Engineering Design Standard TF-0002, 2003, and the v5.2.2 protocol manual)
with MIC 612-specific behaviour cross-checked against the Bosch MIC612
operation manuals v2.1/v2.2. Three confidence levels apply:

  VERIFIED    - observed working on this physical camera
  CONFIRMED   - matches the primary documentation, not yet sent to this unit
  UNCONFIRMED - spec-correct but MIC support unknown; gated off

Dangerous opcodes deliberately NOT implemented - do not add them:
  0x29  reset camera defaults (factory reset)
  0x49  Set Zero Position - a persistent azimuth-calibration WRITE, not a
        movement. An earlier draft of this module's absolute-position support
        mistook it for set-pan; sending it re-zeroes the camera's compass.
"""

# Command-2 direction bits (standard motion command).
PAN_RIGHT = 0x02
PAN_LEFT = 0x04
TILT_UP = 0x08
TILT_DOWN = 0x10


class pelcoD:
    """Builds Pelco-D frames. Pure construction - nothing here touches I/O."""

    # Absolute positioning (extended opcodes 0x4B/0x4D/0x4F) is spec-correct
    # but has never been exercised on this camera. The methods refuse to build
    # frames until this is flipped after a bench test. The stubs they replace
    # were worse: they silently returned STOP frames whatever you asked for.
    ABSOLUTE_POSITION_ENABLED = False

    def __init__(self, address: int = 0x01):
        # MIC accepts Pelco addresses 1-254. The thermal imager of a dual
        # camera is a second device at optical address + 1.
        if not 1 <= int(address) <= 254:
            raise ValueError("Pelco-D address must be 1-254")
        self.address = int(address)

    # -- frame plumbing ----------------------------------------------------
    @staticmethod
    def _clamp_speed(speed) -> int:
        """Clamp to 0x00-0x3F.

        0x40 is 'turbo' in the spec but unconfirmed on the MIC, and
        over-range values can be read as stop by some receivers - so the
        clamp is a hard ceiling, not a convention.
        """
        try:
            speed = int(speed)
        except (TypeError, ValueError):
            speed = 0
        return max(0, min(0x3F, speed))

    def _frame(self, cmd1: int, cmd2: int, d1: int, d2: int) -> bytearray:
        body = [self.address, cmd1 & 0xFF, cmd2 & 0xFF, d1 & 0xFF, d2 & 0xFF]
        return bytearray([0xFF] + body + [sum(body) % 256])

    # -- motion (VERIFIED patterns) ----------------------------------------
    def panleft(self, speed):
        return self._frame(0x00, PAN_LEFT, self._clamp_speed(speed), 0x00)

    def panright(self, speed):
        return self._frame(0x00, PAN_RIGHT, self._clamp_speed(speed), 0x00)

    def tiltup(self, speed):
        return self._frame(0x00, TILT_UP, 0x00, self._clamp_speed(speed))

    def tiltdown(self, speed):
        return self._frame(0x00, TILT_DOWN, 0x00, self._clamp_speed(speed))

    def stop(self):
        return self._frame(0x00, 0x00, 0x00, 0x00)

    def move(self, pan_dir: int, tilt_dir: int, pan_speed=25, tilt_speed=25):
        """Combined pan+tilt motion. pan_dir/tilt_dir: -1, 0 or +1.

        pan +1 = right, tilt +1 = up. Both zero builds a stop frame.
        Single-axis frames are byte-identical to the methods above
        (VERIFIED). Diagonal combinations are spec-CONFIRMED but have never
        been sent to this camera - the service gates them behind
        DIAGONALS_ENABLED until one bench test passes.
        """
        cmd2 = 0x00
        if pan_dir > 0:
            cmd2 |= PAN_RIGHT
        elif pan_dir < 0:
            cmd2 |= PAN_LEFT
        if tilt_dir > 0:
            cmd2 |= TILT_UP
        elif tilt_dir < 0:
            cmd2 |= TILT_DOWN
        if cmd2 == 0x00:
            return self.stop()          # canonical all-zero stop frame
        return self._frame(0x00, cmd2,
                           self._clamp_speed(pan_speed),
                           self._clamp_speed(tilt_speed))

    # -- lens (VERIFIED patterns except iris, which is CONFIRMED) ----------
    def zoomtele(self):
        return self._frame(0x00, 0x20, 0x00, 0x00)

    def zoomwide(self):
        return self._frame(0x00, 0x40, 0x00, 0x00)

    def focusfar(self):
        return self._frame(0x00, 0x80, 0x00, 0x00)

    def focusnear(self):
        return self._frame(0x01, 0x00, 0x00, 0x00)

    def irisopen(self):
        # cmd1 bit 1. Doubles as the OSD "Select" key while the camera's
        # Pelco setup menu is open - without it the menu cannot be used.
        return self._frame(0x02, 0x00, 0x00, 0x00)

    def irisclose(self):
        # cmd1 bit 2.
        return self._frame(0x04, 0x00, 0x00, 0x00)

    # -- presets and aux (VERIFIED patterns) -------------------------------
    def setpreset(self, preset):
        return self._frame(0x00, 0x03, 0x00, int(preset) & 0xFF)

    def gotopreset(self, preset):
        return self._frame(0x00, 0x07, 0x00, int(preset) & 0xFF)

    def clearpreset(self, preset):
        return self._frame(0x00, 0x05, 0x00, int(preset) & 0xFF)

    def auxon(self, aux):
        return self._frame(0x00, 0x09, 0x00, int(aux) & 0xFF)

    def auxoff(self, aux):
        return self._frame(0x00, 0x0B, 0x00, int(aux) & 0xFF)

    def openmenu(self):
        # Set Preset 95: the documented Pelco-mode setup-menu opener on the
        # MIC 612. On THIS unit the verified opener is Aux 2 On (a local
        # remap); this is the documented fallback that survives a factory
        # reset.
        return self.setpreset(95)

    # -- absolute position (UNCONFIRMED - gated) ---------------------------
    def _require_absolute(self):
        if not self.ABSOLUTE_POSITION_ENABLED:
            raise RuntimeError(
                "Absolute positioning is disabled: opcodes 0x4B/0x4D/0x4F "
                "are unconfirmed on the MIC 612. Bench-test before enabling."
            )

    def setpanposition(self, MSB, LSB):
        # Opcode 0x4B, position in hundredths of a degree 0-35999.
        # NOT 0x49 - 0x49 is the Set Zero Position calibration write.
        self._require_absolute()
        return self._frame(0x00, 0x4B, int(MSB) & 0xFF, int(LSB) & 0xFF)

    def settiltposition(self, MSB, LSB):
        # Opcode 0x4D, 0 = horizon, hundredths of a degree.
        self._require_absolute()
        return self._frame(0x00, 0x4D, int(MSB) & 0xFF, int(LSB) & 0xFF)

    def setzoomposition(self, MSB, LSB):
        # Opcode 0x4F, fraction of zoom range x 65535.
        self._require_absolute()
        return self._frame(0x00, 0x4F, int(MSB) & 0xFF, int(LSB) & 0xFF)
