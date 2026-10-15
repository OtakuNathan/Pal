"""Minimal ST7789 SPI panel driver (240x320) for Pal's face panel.

Init sequence and wiring are taken verbatim from the verified bring-up in
~/.pal/plugins/community/st7789_bridge/st7789_test.py (2026-08-24).

Wiring (BCM): SCL=11 SDA=10 RESET=24 DC=25 CS=8 BL=23, VCC=3.3V only.
Panel: 240x320 rectangular ST7789V, MADCTL=0x00, INVOFF (INVON shows negative).
"""

from __future__ import annotations

import time

import RPi.GPIO as GPIO
import spidev

DC = 25
RESET = 24
BL = 23
SPI_BUS = 0
SPI_DEVICE = 0

# spidev caps a single writebytes() call at 4096 bytes; a 240x320 RGB565 frame
# is 153,600 bytes, so we stream it in chunks rather than one blocking call.
CHUNK_BYTES = 4096


class Panel:
    def __init__(
        self,
        *,
        width: int = 240,
        height: int = 320,
        x_offset: int = 0,
        y_offset: int = 0,
        speed_hz: int = 8_000_000,
        bl_active_high: bool = True,
    ) -> None:
        self.width = width
        self.height = height
        self.x_offset = x_offset
        self.y_offset = y_offset
        self.speed_hz = speed_hz
        self.bl_active_high = bl_active_high
        self.spi: spidev.SpiDev | None = None
        self._closed = False

        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(DC, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(RESET, GPIO.OUT, initial=GPIO.HIGH)
        GPIO.setup(BL, GPIO.OUT, initial=GPIO.LOW)

        self.spi = spidev.SpiDev()
        self.spi.open(SPI_BUS, SPI_DEVICE)
        self.spi.max_speed_hz = speed_hz
        self.spi.mode = 0
        self.spi.threewire = False
        self.spi.lsbfirst = False
        self.spi.cshigh = False

    # ---- low level -------------------------------------------------
    def _command(self, value: int) -> None:
        GPIO.output(DC, GPIO.LOW)
        self.spi.writebytes([value & 0xFF])

    def _data(self, values) -> None:
        GPIO.output(DC, GPIO.HIGH)
        if isinstance(values, int):
            values = [values]
        self.spi.writebytes(list(values))

    def _reset_panel(self) -> None:
        GPIO.output(RESET, GPIO.HIGH)
        time.sleep(0.01)
        GPIO.output(RESET, GPIO.LOW)
        time.sleep(0.05)
        GPIO.output(RESET, GPIO.HIGH)
        time.sleep(0.12)

    def set_window(self, x0: int, y0: int, x1: int, y1: int) -> None:
        self._command(0x2A)  # CASET
        self._data([(x0 >> 8) & 0xFF, x0 & 0xFF, (x1 >> 8) & 0xFF, x1 & 0xFF])
        self._command(0x2B)  # RASET
        self._data([(y0 >> 8) & 0xFF, y0 & 0xFF, (y1 >> 8) & 0xFF, y1 & 0xFF])
        self._command(0x2C)  # RAMWR

    def initialise(self) -> None:
        self._reset_panel()
        self._command(0x01)  # SWRESET
        time.sleep(0.15)
        self._command(0x11)  # SLPOUT
        time.sleep(0.12)
        self._command(0x3A)  # COLMOD 16-bit RGB565
        self._data(0x55)
        self._command(0x36)  # MADCTL
        self._data(0x00)
        self._command(0xB2)  # PORCTRL
        self._data([0x0C, 0x0C, 0x00, 0x33, 0x33])
        self._command(0xB7)  # GCTRL
        self._data(0x35)
        self._command(0xBB)  # VCOMS
        self._data(0x19)
        self._command(0xC0)  # LCMCTRL
        self._data(0x2C)
        self._command(0xC2)  # VDVVRHEN
        self._data(0x01)
        self._command(0xC3)  # VRHS
        self._data(0x12)
        self._command(0xC4)  # VDVS
        self._data(0x20)
        self._command(0xC6)  # FRCTRL2
        self._data(0x0F)
        self._command(0xD0)  # PWCTRL1
        self._data([0xA4, 0xA1])
        self._command(0xE0)  # PVGAMCTRL
        self._data([0xD0, 0x04, 0x0D, 0x11, 0x13, 0x2B, 0x3F, 0x54,
                    0x4C, 0x18, 0x0D, 0x0B, 0x1F, 0x23])
        self._command(0xE1)  # NVGAMCTRL
        self._data([0xD0, 0x04, 0x0C, 0x11, 0x13, 0x2C, 0x3F, 0x44,
                    0x51, 0x2F, 0x1F, 0x1F, 0x20, 0x23])
        self._command(0x20)  # INVOFF
        self._command(0x13)  # NORON
        time.sleep(0.01)
        self._command(0x29)  # DISPON
        time.sleep(0.1)

    # ---- frame transfer --------------------------------------------
    def blit(self, frame: bytes) -> None:
        """Push a full-frame RGB565 (big-endian) buffer to the panel."""
        self.set_window(
            self.x_offset,
            self.y_offset,
            self.x_offset + self.width - 1,
            self.y_offset + self.height - 1,
        )
        GPIO.output(DC, GPIO.HIGH)
        view = memoryview(frame)
        total = len(view)
        pos = 0
        while pos < total:
            end = min(pos + CHUNK_BYTES, total)
            self.spi.writebytes2(view[pos:end])
            pos = end

    def set_backlight(self, on: bool) -> None:
        level = GPIO.HIGH if on == self.bl_active_high else GPIO.LOW
        GPIO.output(BL, level)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.set_backlight(False)
        except Exception:
            pass
        if self.spi is not None:
            try:
                self.spi.close()
            except Exception:
                pass
            self.spi = None
        try:
            GPIO.cleanup()
        except Exception:
            pass
