#!/usr/bin/env python3

"""
The daemon responsible for changing the volume in response to a turn or press
of the volume knob.

The volume knob is a rotary encoder. It turns infinitely in either direction.
Turning it to the right will increase the volume; turning it to the left will
decrease the volume. The knob can also be pressed like a button in order to
turn muting on or off.

The knob uses two GPIO pins and we need some extra logic to decode it. The
button we can just treat like an ordinary button. Rather than poll
constantly, we use threads and interrupts to listen on all three pins in one
script.
"""

import os
import signal
import subprocess
import sys
import threading
import mpdcontrol
import logging

from RPi import GPIO
from queue import Queue

BOUNCETIME = 400

DEBUG = False

# SETTINGS
# ========

# The two pins that the encoder uses (BCM numbering).
GPIO_A = 23
GPIO_B = 22

# The pin that the knob's button is hooked up to. If you have no button, set
# this to None.
GPIO_BUTTON = 24

GPIO_BT_UP = 17
GPIO_BT_DOWN = 4
GPIO_BT_LEFT = 27
GPIO_BT_RIGHT = 18


# The minimum and maximum volumes, as percentages.
#
# The default max is less than 100 to prevent distortion. The default min is
# greater than zero because if your system is like mine, sound gets
# completely inaudible _long_ before 0%. If you've got a hardware amp or
# serious speakers or something, your results will vary.
VOLUME_MIN = 24
VOLUME_MAX = 92

# The amount you want one click of the knob to increase or decrease the
# volume. I don't think that non-integer values work here, but you're welcome
# to try.
VOLUME_INCREMENT = 2

# (END SETTINGS)
#


# When the knob is turned, the callback happens in a separate thread. If
# those turn callbacks fire erratically or out of order, we'll get confused
# about which direction the knob is being turned, so we'll use a queue to
# enforce FIFO. The callback will push onto a queue, and all the actual
# volume-changing will happen in the main thread.
RotEncQUEUE = Queue()
NavQueue = Queue()

# When we put something in the queue, we'll use an event to signal to the
# main thread that there's something in there. Then the main thread will
# process the queue and reset the event. If the knob is turned very quickly,
# this event loop will fall behind, but that's OK because it consumes the
# queue completely each time through the loop, so it's guaranteed to catch up.
EVENT = threading.Event()

def debug(str):
  if not DEBUG:
    return
  print(str)

class RotaryEncoder:
  """
  A class to decode mechanical rotary encoder pulses.

  Ported to RPi.GPIO from the pigpio sample here:
  http://abyz.co.uk/rpi/pigpio/examples.html
  """

  def __init__(self, gpioA, gpioB, callback=None, buttonPin=None, buttonCallback=None):
    """
    Instantiate the class. Takes three arguments: the two pin numbers to
    which the rotary encoder is connected, plus a callback to run when the
    switch is turned.

    The callback receives one argument: a `delta` that will be either 1 or -1.
    One of them means that the dial is being turned to the right; the other
    means that the dial is being turned to the left. I'll be damned if I know
    yet which one is which.
    """

    self.lastGpio = None
    self.gpioA    = gpioA
    self.gpioB    = gpioB
    self.callback = callback

    self.gpioButton     = buttonPin
    self.buttonCallback = buttonCallback

    self.levA = 0
    self.levB = 0

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(self.gpioA, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(self.gpioB, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    GPIO.add_event_detect(self.gpioA, GPIO.BOTH, self._callback)
    GPIO.add_event_detect(self.gpioB, GPIO.BOTH, self._callback)

    if self.gpioButton:
      GPIO.setup(self.gpioButton, GPIO.IN, pull_up_down=GPIO.PUD_UP)
      GPIO.add_event_detect(self.gpioButton, GPIO.FALLING, self._buttonCallback, bouncetime=BOUNCETIME)


  def destroy(self):
    GPIO.remove_event_detect(self.gpioA)
    GPIO.remove_event_detect(self.gpioB)
    #GPIO.cleanup()

  def _buttonCallback(self, channel):
    self.buttonCallback(GPIO.input(channel))

  def _callback(self, channel):
    level = GPIO.input(channel)
    if channel == self.gpioA:
      self.levA = level
    else:
      self.levB = level

    # Debounce.
    if channel == self.lastGpio:
      return

    # When both inputs are at 1, we'll fire a callback. If A was the most
    # recent pin set high, it'll be forward, and if B was the most recent pin
    # set high, it'll be reverse.
    self.lastGpio = channel
    if channel == self.gpioA and level == 1:
      if self.levB == 1:
        self.callback(1)
    elif channel == self.gpioB and level == 1:
      if self.levA == 1:
        self.callback(-1)

class NavButton:

  def __init__(self, gpioLeft, gpioRight, gpioUp, gpioDown, callback=None):
    """
    Instantiate the class.
    """
    self.gpioLeft = gpioLeft
    self.gpioRight = gpioRight
    self.gpioUp = gpioUp
    self.gpioDown = gpioDown

    self.callback = callback

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(self.gpioLeft, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(self.gpioRight, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(self.gpioUp, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(self.gpioDown, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    GPIO.add_event_detect(self.gpioLeft, GPIO.FALLING, self._callback,bouncetime=BOUNCETIME)
    GPIO.add_event_detect(self.gpioRight, GPIO.FALLING, self._callback,bouncetime=BOUNCETIME)
    GPIO.add_event_detect(self.gpioUp, GPIO.FALLING, self._callback,bouncetime=BOUNCETIME)
    GPIO.add_event_detect(self.gpioDown, GPIO.FALLING, self._callback,bouncetime=BOUNCETIME)

  def destroy(self):
    GPIO.remove_event_detect(self.gpioLeft)
    GPIO.remove_event_detect(self.gpioRight)
    GPIO.remove_event_detect(self.gpioUp)
    GPIO.remove_event_detect(self.gpioDown)
    GPIO.cleanup()


  def _callback(self, channel):
    self.callback(channel)

class VolumeError(Exception):
  pass

class Volume:
  """
  A wrapper API for interacting with the volume settings on the RPi.
  """
  MIN = VOLUME_MIN
  MAX = VOLUME_MAX
  INCREMENT = VOLUME_INCREMENT

  def __init__(self):
    # Set an initial value for last_volume in case we're muted when we start.
    self.last_volume = self.MIN
    self.is_muted = True
    self.volume = 0
    self._sync()

  def up(self):
    """
    Increases the volume by one increment.
    """
    return self.change(self.INCREMENT)

  def down(self):
    """
    Decreases the volume by one increment.
    """
    return self.change(-self.INCREMENT)

  def change(self, delta):
    v = self.volume + delta
    v = self._constrain(v)
    return self.set_volume(v)

  def set_volume(self, v):
    """
    Sets volume to a specific value.
    """
    self.volume = self._constrain(v)
    output = self.amixer("set 'PCM' unmute {}%".format(v))
    #self._sync(output)
    return self.volume

  def toggle(self):
    """
    Toggles muting between on and off.
    """
    if self.is_muted:
      output = self.amixer("set 'PCM' unmute")
    else:
      # We're about to mute ourselves, so we should remember the last volume
      # value we had because we'll want to restore it later.
      self.last_volume = self.volume
      output = self.amixer("set 'PCM' mute")

    self._sync(output)
    if not self.is_muted:
      # If we just unmuted ourselves, we should restore whatever volume we
      # had previously.
      self.set_volume(self.last_volume)
    return self.is_muted

  def status(self):
    if self.is_muted:
      return "{}% (muted)".format(self.volume)
    return "{}%".format(self.volume)

  # Read the output of `amixer` to get the system volume and mute state.
  #
  # This is designed not to do much work because it'll get called with every
  # click of the knob in either direction, which is why we're doing simple
  # string scanning and not regular expressions.
  def _sync(self, output=None):
    if output is None:
      doparsing=True
      try:
        output = self.amixer("get 'PCM'")
      except:
        doparsing=False
        pass

    if doparsing:
      lines = output.readlines()
      if DEBUG:
        strings = [line.decode('utf8') for line in lines]
        debug("OUTPUT:")
        debug("".join(strings))
      last = lines[-1].decode('utf-8')

      # The last line of output will have two values in square brackets. The
      # first will be the volume (e.g., "[95%]") and the second will be the
      # mute state ("[off]" or "[on]").
      i1 = last.rindex('[') + 1
      i2 = last.rindex(']')

      self.is_muted = last[i1:i2] == 'off'

      i1 = last.index('[') + 1
      i2 = last.index('%')
      # In between these two will be the percentage value.
      pct = last[i1:i2]

      self.volume = int(pct)

  # Ensures the volume value is between our minimum and maximum.
  def _constrain(self, v):
    if v < self.MIN:
      return self.MIN
    if v > self.MAX:
      return self.MAX
    return v

  def amixer(self, cmd):
    p = subprocess.Popen("amixer {}".format(cmd), shell=True, stdout=subprocess.PIPE)
    code = p.wait()
    if code != 0:
      raise VolumeError("Unknown error")

    return p.stdout

def main():

  logging.basicConfig(filename='/home/pi/hoerkoffer.log',
                      format='%(asctime)s %(levelname)7s %(module)12s %(funcName)s %(message)s', level=logging.DEBUG)

  gpioA = GPIO_A
  gpioB = GPIO_B
  gpioButton = GPIO_BUTTON

  v = Volume()
  logging.debug('Generate MpdControl object')
  mpdObj = mpdcontrol.MpdControl()

  def on_press(value):
    # v.toggle()
    mpdObj.togglePause()
    print("Toggled pause: {}".format(mpdObj.client.status()['state']))
    EVENT.set()

  def on_button_press(channel):
    print("Button callback {}".format(channel))
    logging.debug("Button callback {}".format(channel))
    NavQueue.put(channel)
    EVENT.set()

  # This callback runs in the background thread. All it does is put turn
  # events into a queue and flag the main thread to process them. The
  # queueing ensures that we won't miss anything if the knob is turned
  # extremely quickly.
  def on_turn(delta):
    RotEncQUEUE.put(delta)
    EVENT.set()

  def consume_queue():
    while not RotEncQUEUE.empty():
      delta = RotEncQUEUE.get()
      handle_delta(delta)
    while not NavQueue.empty():
      button = NavQueue.get()
      handle_button(button)

  def handle_delta(delta):
    if v.is_muted:
      debug("Unmuting")
      v.toggle()
    if delta == 1:
      vol = v.up()
    else:
      vol = v.down()
    print("Set volume to: {}".format(vol))

  def handle_button(button):
    logging.debug('Button pressed: {}'.format(button))
    if button == GPIO_BT_RIGHT:
      print("Right")
      mpdObj.next()
    elif button == GPIO_BT_LEFT:
      print("Left")
      mpdObj.prev()
    elif button == GPIO_BT_UP:
      print("Up")
      mpdObj.prevplaylist()
    elif button == GPIO_BT_DOWN:
      print("Down")
      mpdObj.nextplaylist()

  def on_exit(a, b):
    print("Exiting...")
    encoder.destroy()
    navbuttons.destroy()
    sys.exit(0)

  debug("Volume knob using pins {} and {}".format(gpioA, gpioB))

  if gpioButton != None:
    debug("Volume button using pin {}".format(gpioButton))

  # debug("Initial volume: {}".format(v.volume))

  encoder = RotaryEncoder(GPIO_A, GPIO_B, callback=on_turn, buttonPin=GPIO_BUTTON, buttonCallback=on_press)
  navbuttons = NavButton(GPIO_BT_LEFT, GPIO_BT_RIGHT, GPIO_BT_UP, GPIO_BT_DOWN, callback=on_button_press)
  signal.signal(signal.SIGINT, on_exit)
  signal.signal(signal.SIGTERM, on_exit)

  while True:
    # This is the best way I could come up with to ensure that this script
    # runs indefinitely without wasting CPU by polling. The main thread will
    # block quietly while waiting for the event to get flagged. When the knob
    # is turned we're able to respond immediately, but when it's not being
    # turned we're not looping at all.
    #
    # The 1200-second (20 minute) timeout is a hack; for some reason, if I
    # don't specify a timeout, I'm unable to get the SIGINT handler above to
    # work properly. But if there is a timeout set, even if it's a very long
    # timeout, then Ctrl-C works as intended. No idea why.
    EVENT.wait(1200)
    consume_queue()
    EVENT.clear()



if __name__ == "__main__":
  main()