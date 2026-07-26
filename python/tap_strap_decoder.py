"""
Ternary tap-code input system for the Tap Strap (right hand).

Design:
- LETTER MODE (default): 3 consecutive taps of index/middle/ring encode
  a letter. A/N/O share a 4-tap overflow branch (see EXTENSION_MAP).
- COMMAND MODE: a thumb tap switches into command mode. The next 2
  taps encode punctuation/editing. A 3rd tap on the reserved "ring-
  middle" prefix reaches an overflow branch: apostrophe, caps-lock
  toggle, or entering number mode (see COMMAND_EXTENSION_MAP).
- NUMBER MODE: entered via thumb -> ring-middle -> ring. 2 taps encode
  a digit 0-6 or a decimal point; a 3rd tap on the reserved "ring-
  middle" prefix reaches 7, 8, or 9. A thumb tap while in number mode
  jumps straight back to letter mode.
- Caps lock persists until toggled again; it's applied inside the
  decoder, so on_letter always receives already-cased text.
- A timeout resets any incomplete buffer (and returns to letter mode)
  so a slow or abandoned sequence doesn't corrupt the next one.

This file is split into two independent parts:
  1. TernaryDecoder - pure logic, no hardware dependency. Testable via
     the built-in simulation CLI at the bottom.
  2. TapStrapRunner - wires TernaryDecoder up to the real Tap Strap via
     the official `tapsdk` package (https://github.com/TapWithUs/tap-python-sdk)
     and to keyboard output via `pynput`.

Run modes:
  python tap_ternary_input.py simulate   # test decode logic, no hardware
  python tap_ternary_input.py practice   # timed drill w/ WPM, no hardware
  python tap_ternary_input.py run        # connect to a real Tap Strap, with WPM logging
"""

import sys
import time
import difflib
import threading
import string

# ---------------------------------------------------------------------------
# 1. Encoding tables
# ---------------------------------------------------------------------------

DIGIT_TO_FINGER = {0: "index", 1: "middle", 2: "ring"}
FINGER_TO_DIGIT = {v: k for k, v in DIGIT_TO_FINGER.items()}

# Tap Strap tapcodes for single-finger taps (bit0=thumb ... bit4=pinky)
TAPCODE_TO_FINGER = {
    1: "thumb",
    2: "index",
    4: "middle",
    8: "ring",
    16: "pinky",
}

LETTER_ALPHABET = list(string.ascii_uppercase) + [" "]  # 27 symbols, base-3 3-digit


def _base3_digits(n: int, width: int) -> list:
    digits = []
    for _ in range(width):
        digits.append(n % 3)
        n //= 3
    return list(reversed(digits))


# position -> finger sequence, for reference / printing a cheat sheet
_FULL_LETTER_TABLE = {}
for pos, symbol in enumerate(LETTER_ALPHABET):
    d0, d1, d2 = _base3_digits(pos, 3)
    _FULL_LETTER_TABLE[(d0, d1, d2)] = symbol

# --- Hardware constraint -------------------------------------------------
# Tapping the same finger 3 times in a row is a built-in Tap Strap gesture
# that powers the device off. In the straight base-3 scheme above that
# happens to be exactly the codes for A (index x3), N (middle x3), and
# space (ring x3) -- all forbidden.
#
# Fix:
#   - space is dropped entirely from the letter tree (it's already
#     reachable via the thumb+index+index command, see COMMAND_TABLE).
#   - A and N need new homes. Rather than give each its own reserved
#     prefix (which would waste 2 whole 3-tap codes), they share ONE
#     4-tap "overflow" branch built on O's old code: tap middle-middle-
#     ring (O's position), then a 4th tap disambiguates:
#         + index  -> O   (displaced from its old 3-tap slot)
#         + middle -> A
#         + ring   -> N
#     This costs those 3 letters a 4th tap; the other 23 letters are
#     unaffected. See TernaryDecoder for the runtime guard that also
#     makes the forbidden codes physically unreachable regardless.
FORBIDDEN_TRIPLES = {(0, 0, 0), (1, 1, 1), (2, 2, 2)}  # A, N, space (unreachable)
EXTENSION_PREFIX = (1, 1, 2)  # O's old code, repurposed as the overflow trigger
EXTENSION_MAP = {0: "O", 1: "A", 2: "N"}  # keyed by the 4th tap's digit

LETTER_TABLE = {
    key: sym
    for key, sym in _FULL_LETTER_TABLE.items()
    if sym != " " and key not in FORBIDDEN_TRIPLES and key != EXTENSION_PREFIX
}

COMMAND_ACTIONS = [
    "space",       # 00
    "period",      # 01
    "comma",       # 02
    "backspace",   # 10
    "enter",       # 11
    "question",    # 12
    "exclamation", # 20
    "hyphen",      # 22
    # 21 is NOT a direct action -- it's the extension prefix, see below.
]
_COMMAND_POSITIONS = [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (2, 0), (2, 2)]
COMMAND_TABLE = dict(zip(_COMMAND_POSITIONS, COMMAND_ACTIONS))

# Command-menu overflow, same shared-branch pattern as O/A/N: thumb + the
# reserved "ring-middle" prefix, then a 3rd tap disambiguates. Using a
# prefix whose 2 digits differ (rather than a repeated digit like
# ring-ring) keeps all 3 branches reachable -- a repeated-digit prefix
# would make the branch matching that same finger an unreachable 3x-tap
# (caught by the runtime safety guard, i.e. permanently unusable).
COMMAND_EXTENSION_PREFIX = (2, 1)  # ring, middle
COMMAND_EXTENSION_MAP = {0: "apostrophe", 1: "caps_toggle", 2: "enter_numbers"}

# --- Number mode ----------------------------------------------------------
# A third top-level mode, entered via thumb -> ring-middle -> ring (the
# "enter_numbers" command extension above). Same shape as the punctuation
# menu: digits 0-6 and a decimal point are direct 2-tap codes; 7/8/9 share
# one more overflow branch (again using a non-repeating prefix so all 3
# branches stay reachable).
NUMBER_DIRECT = ["0", "1", "2", "3", "4", "5", "6", "."]  # 8 direct 2-tap codes
_NUMBER_POSITIONS = [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (2, 0), (2, 2)]
NUMBER_TABLE = dict(zip(_NUMBER_POSITIONS, NUMBER_DIRECT))

NUMBER_EXTENSION_PREFIX = (2, 1)  # ring, middle
NUMBER_EXTENSION_MAP = {0: "7", 1: "8", 2: "9"}


# ---------------------------------------------------------------------------
# 2. Pure decoder logic (hardware-independent, easy to unit test)
# ---------------------------------------------------------------------------

class TernaryDecoder:
    """
    Feed it finger names ('index' | 'middle' | 'ring' | 'thumb' | 'pinky')
    one at a time via .tap(finger). It calls back on_letter(symbol),
    on_command(action), or on_number(digit) when a full sequence
    resolves, and manages mode switching and timeout-based buffer reset
    itself.

    Three top-level modes:
      - letter (default): 3 taps -> a letter, 4 taps for the O/A/N
        overflow branch. Case is applied internally based on caps-lock
        state, so on_letter always receives ready-to-type text.
      - command (thumb + taps): 2 taps -> punctuation/editing, 3 taps
        for the hyphen/caps-toggle/enter-numbers overflow branch.
      - number (thumb -> ring-middle -> ring): 2 taps -> a digit, 3 taps
        for the 8/9/decimal-point overflow branch. A thumb tap while
        in number mode returns directly to letter mode.

    Optionally also calls on_partial(candidates, mode) after every tap
    (including a reset to the full set) so a UI can show the narrowing
    set of remaining possibilities. `mode` passed to on_partial is one
    of 'letter'/'command'/'number' or an extension variant of those
    ('letter_extension'/'command_extension'/'number_extension').

    Safety guard: this class tracks the last 2 *raw* finger taps (not
    just the current sequence's buffer) and refuses -- via on_danger --
    any tap that would be the 3rd identical finger in a row, whether
    that streak is within one sequence or spans the boundary between
    two. That's the exact gesture that powers a real Tap Strap off, so
    it's blocked unconditionally rather than merely discouraged by
    table design. A pinky tap always safely clears the streak without
    being part of any code, so it doubles as an on-demand "unstick" key.
    """

    # mode -> (table, taps_needed, extension_prefix, extension_map)
    _MODE_CONFIG = {
        "letter": (LETTER_TABLE, 3, EXTENSION_PREFIX, EXTENSION_MAP),
        "command": (COMMAND_TABLE, 2, COMMAND_EXTENSION_PREFIX, COMMAND_EXTENSION_MAP),
        "number": (NUMBER_TABLE, 2, NUMBER_EXTENSION_PREFIX, NUMBER_EXTENSION_MAP),
    }

    def __init__(self, on_letter, on_command, on_number=None, timeout_seconds=1.2,
                 on_partial=None, on_danger=None, on_pace_warning=None,
                 min_repeat_interval=0.35):
        self.on_letter = on_letter
        self.on_command = on_command
        self.on_number = on_number or (lambda digit: None)
        self.on_partial = on_partial
        self.on_danger = on_danger
        # Advisory only, never blocking: a fast repeat of the same finger
        # can trigger the Tap Strap's own built-in double-tap gesture
        # (e.g. a mode switch), separate from the hard power-off guard
        # above. Unlike that guard, many of our codes legitimately rely
        # on 2 same-finger taps in a row, so this only warns -- it never
        # refuses the tap. Only meaningful when taps carry real
        # wall-clock timing (i.e. real hardware), so it's opt-in via
        # on_pace_warning; simulate/practice modes leave it unset since
        # typed tokens arrive with no meaningful timing.
        self.on_pace_warning = on_pace_warning
        self.min_repeat_interval = min_repeat_interval
        self._last_tap_time = {}
        self.timeout_seconds = timeout_seconds
        self.buffer = []
        self.mode = "letter"
        self.in_extension = False
        self.caps_active = False
        self._raw_last2 = []
        self._timer = None
        self._announce_partial()

    def _config(self):
        return self._MODE_CONFIG[self.mode]

    def _announce_partial(self):
        if not self.on_partial:
            return
        table, needed, ext_prefix, ext_map = self._config()
        if self.in_extension:
            ordered = [ext_map[d] for d in sorted(ext_map)]
            self.on_partial(ordered, self.mode + "_extension")
            return
        prefix = tuple(self.buffer)
        remaining = [sym for key, sym in table.items() if key[: len(prefix)] == prefix]
        self.on_partial(remaining, self.mode)

    def _reset(self, to_mode="letter"):
        self.buffer = []
        self.mode = to_mode
        self.in_extension = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        self._announce_partial()

    def _arm_timeout(self):
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self.timeout_seconds, self._on_timeout)
        self._timer.daemon = True
        self._timer.start()

    def _on_timeout(self):
        # Abandoned sequence: drop it and fall back to letter mode.
        self.buffer = []
        self.mode = "letter"
        self.in_extension = False
        self._announce_partial()

    def _emit_letter(self, symbol):
        self.on_letter(symbol.upper() if self.caps_active else symbol.lower())

    def _emit_command(self, action):
        if action == "caps_toggle":
            self.caps_active = not self.caps_active
            self.on_command("caps_" + ("on" if self.caps_active else "off"))
            self._reset(to_mode="letter")
            return
        if action == "enter_numbers":
            self.on_command("enter_numbers")
            self._reset(to_mode="number")
            return
        self.on_command(action)
        self._reset(to_mode="letter")

    def tap(self, finger: str):
        if self.on_pace_warning is not None:
            now = time.monotonic()
            last = self._last_tap_time.get(finger)
            if last is not None and (now - last) < self.min_repeat_interval:
                self.on_pace_warning(finger, now - last)
            self._last_tap_time[finger] = now

        if finger == "pinky":
            # Manual safety valve: clears the same-finger streak and
            # aborts whatever partial sequence was in progress. Always
            # safe to tap -- pinky is never part of a code.
            self._raw_last2 = []
            self._reset(to_mode="letter")
            return

        if finger == "thumb":
            self._raw_last2 = []
            if self.mode == "number" and not self.buffer and not self.in_extension:
                # Quick exit: thumb jumps straight back to letters from
                # number mode, no detour through the punctuation menu.
                self._reset(to_mode="letter")
                return
            # Otherwise thumb (re)opens the punctuation/command menu.
            self.buffer = []
            self.mode = "command"
            self.in_extension = False
            self._arm_timeout()
            self._announce_partial()
            return

        if finger not in FINGER_TO_DIGIT:
            return  # unmapped input, ignore

        # Universal guard: refuse a 3rd consecutive identical raw tap.
        if self._raw_last2 == [finger, finger]:
            if self.on_danger:
                self.on_danger(finger)
            return  # tap refused -- tap a different finger (or pinky) first

        self._raw_last2 = (self._raw_last2 + [finger])[-2:]
        self._arm_timeout()

        table, needed, ext_prefix, ext_map = self._config()

        if self.in_extension:
            result = ext_map.get(FINGER_TO_DIGIT[finger])
            mode = self.mode
            if mode == "letter":
                self._reset(to_mode="letter")
                if result:
                    self._emit_letter(result)
            elif mode == "number":
                self._reset(to_mode="number")
                if result:
                    self.on_number(result)
            else:  # command
                if result:
                    self._emit_command(result)
                else:
                    self._reset(to_mode="letter")
            return

        self.buffer.append(FINGER_TO_DIGIT[finger])

        if len(self.buffer) < needed:
            self._announce_partial()
            return

        key = tuple(self.buffer)

        if key == ext_prefix:
            self.in_extension = True
            self._announce_partial()
            return

        result = table.get(key)
        if self.mode == "letter":
            self._reset(to_mode="letter")
            if result:
                self._emit_letter(result)
        elif self.mode == "number":
            self._reset(to_mode="number")
            if result:
                self.on_number(result)
        else:  # command
            if result:
                self._emit_command(result)
            else:
                self._reset(to_mode="letter")


# ---------------------------------------------------------------------------
# 3. WPM logger
# ---------------------------------------------------------------------------

# Characters that count as forward progress for WPM purposes (standard
# convention: 5 characters = 1 "word", regardless of actual word length).
_COMMAND_CHAR = {
    "space": " ", "period": ".", "comma": ",", "enter": "\n",
    "question": "?", "exclamation": "!", "apostrophe": "'", "hyphen": "-",
}


class WPMLogger:
    """
    Tracks committed output over a session and computes WPM.
    - Gross WPM counts every committed character (including ones later
      erased by backspace) -- reflects raw tapping speed.
    - Net WPM subtracts backspaces -- reflects effective output speed,
      the standard typing-test convention.
    Call record_letter()/record_command() as symbols are emitted, then
    report() at any time for a live readout, or final_report() at the end.
    """

    def __init__(self):
        self.start_time = None
        self.gross_chars = 0
        self.backspaces = 0
        self.transcript = []  # what's actually on "screen" after backspaces

    def _ensure_started(self):
        if self.start_time is None:
            self.start_time = time.monotonic()

    def record_letter(self, symbol):
        self._ensure_started()
        self.gross_chars += 1
        self.transcript.append(symbol)  # already correctly cased by the decoder

    def record_number(self, digit):
        self._ensure_started()
        self.gross_chars += 1
        self.transcript.append(digit)

    def record_command(self, action):
        self._ensure_started()
        if action in ("caps_on", "caps_off", "enter_numbers"):
            return  # mode/state switches, not typed characters
        self.gross_chars += 1
        if action == "backspace":
            self.backspaces += 1
            if self.transcript:
                self.transcript.pop()
        else:
            self.transcript.append(_COMMAND_CHAR.get(action, ""))

    def _elapsed_minutes(self):
        if self.start_time is None:
            return 0.0
        return max(time.monotonic() - self.start_time, 1e-6) / 60.0

    def report(self):
        minutes = self._elapsed_minutes()
        gross_wpm = (self.gross_chars / 5) / minutes if minutes > 0 else 0.0
        net_chars = max(self.gross_chars - 2 * self.backspaces, 0)
        net_wpm = (net_chars / 5) / minutes if minutes > 0 else 0.0
        return {
            "elapsed_sec": minutes * 60,
            "gross_chars": self.gross_chars,
            "backspaces": self.backspaces,
            "gross_wpm": gross_wpm,
            "net_wpm": net_wpm,
            "text": "".join(self.transcript),
        }

    def final_report(self, target=None):
        stats = self.report()
        print("\n--- Session report ---")
        print(f"  Elapsed:      {stats['elapsed_sec']:.1f}s")
        print(f"  Characters:   {stats['gross_chars']} ({stats['backspaces']} backspaces)")
        print(f"  Gross WPM:    {stats['gross_wpm']:.1f}")
        print(f"  Net WPM:      {stats['net_wpm']:.1f}")
        print(f"  Output text:  {stats['text']!r}")
        if target:
            ratio = difflib.SequenceMatcher(None, target.lower(), stats["text"]).ratio()
            print(f"  Accuracy vs target ({target!r}): {ratio * 100:.0f}% similarity")
        print("-----------------------\n")
        return stats


# ---------------------------------------------------------------------------
# 4. Hardware runner - Tap Strap + keyboard output
# ---------------------------------------------------------------------------

PARTIAL_LABELS = {
    "letter": "letters",
    "command": "commands",
    "number": "digits",
    "letter_extension": "overflow letters",
    "command_extension": "overflow commands",
    "number_extension": "overflow digits",
}


def run_hardware():
    try:
        from tapsdk import TapSDK, TapInputMode
    except ImportError:
        print("tapsdk not installed. Install with:")
        print("  git clone https://github.com/TapWithUs/tap-python-sdk")
        print("  cd tap-python-sdk && pip install .")
        sys.exit(1)

    try:
        from pynput.keyboard import Controller, Key
    except ImportError:
        print("pynput not installed. Install with: pip install pynput")
        sys.exit(1)

    kb = Controller()
    logger = WPMLogger()

    def on_letter(symbol):
        kb.type(symbol)
        logger.record_letter(symbol)
        stats = logger.report()
        print(f"  {symbol}   (gross {stats['gross_wpm']:.1f} wpm / net {stats['net_wpm']:.1f} wpm)")

    def on_number(digit):
        kb.type(digit)
        logger.record_number(digit)
        stats = logger.report()
        print(f"  {digit}   (gross {stats['gross_wpm']:.1f} wpm / net {stats['net_wpm']:.1f} wpm)")

    def on_command(action):
        if action == "space":
            kb.type(" ")
        elif action == "period":
            kb.type(".")
        elif action == "comma":
            kb.type(",")
        elif action == "backspace":
            kb.press(Key.backspace)
            kb.release(Key.backspace)
        elif action == "enter":
            kb.press(Key.enter)
            kb.release(Key.enter)
        elif action == "question":
            kb.type("?")
        elif action == "exclamation":
            kb.type("!")
        elif action == "apostrophe":
            kb.type("'")
        elif action == "hyphen":
            kb.type("-")
        elif action == "caps_on":
            print("  [CAPS LOCK ON]")
        elif action == "caps_off":
            print("  [CAPS LOCK OFF]")
        elif action == "enter_numbers":
            print("  [NUMBER MODE -- thumb to exit]")
        logger.record_command(action)
        stats = logger.report()
        print(f"  [{action}]   (gross {stats['gross_wpm']:.1f} wpm / net {stats['net_wpm']:.1f} wpm)")

    def _print_partial(candidates, mode):
        label = PARTIAL_LABELS.get(mode, mode)
        shown = " ".join(candidates)
        print(f"    [{len(candidates)} {label} remaining: {shown}]")

    def _print_danger(finger):
        print(f"    !! blocked: 3rd consecutive {finger} tap would power off the Tap Strap. "
              f"Tap a different finger (or pinky) to clear.")

    def _print_pace_warning(finger, interval):
        print(f"    ~~ heads up: that {finger} repeat was only {interval*1000:.0f}ms apart -- "
              f"fast enough it may have triggered the Tap Strap's own double-tap gesture "
              f"(a mode switch). If output looks wrong, check the device didn't switch modes; "
              f"try pacing repeats of the same finger a bit slower, or lower the Double Tap "
              f"Timeout in TapManager.")

    decoder = TernaryDecoder(on_letter, on_command, on_number=on_number,
                              on_partial=_print_partial, on_danger=_print_danger,
                              on_pace_warning=_print_pace_warning)

    def on_tap_event(identifier, tapcode):
        finger = TAPCODE_TO_FINGER.get(tapcode)
        if finger:
            decoder.tap(finger)

    tap_device = TapSDK()
    tap_device.register_tap_events(on_tap_event)
    tap_device.manager.set_input_mode(TapInputMode("controller"))

    print("Connected. Tap away (Ctrl+C to quit and see your session report).")
    try:
        while True:
            pass
    except KeyboardInterrupt:
        logger.final_report()


# ---------------------------------------------------------------------------
# 5. Practice CLI - timed drill with WPM, no hardware attached
# ---------------------------------------------------------------------------

def run_practice():
    print("Practice mode: a continuous timed drill (no hardware needed).")
    print("Enter finger names one at a time or several per line, e.g.:")
    print("    index index middle index index ring   (that's 'b' then 'c')")
    print("The timer starts on your first token. Type 'stop' on its own")
    print("line to end the session and see your WPM report.\n")

    target = input("Optional target phrase to type (blank to skip): ").strip()
    print()

    logger = WPMLogger()

    def on_partial(candidates, mode):
        label = PARTIAL_LABELS.get(mode, mode)
        shown = " ".join(c if mode.startswith("command") else repr(c) for c in candidates)
        print(f"    [{len(candidates)} {label} remaining: {shown}]")

    def on_danger(finger):
        print(f"    !! blocked: 3rd consecutive {finger} tap would power off the Tap Strap. "
              f"Tap a different finger (or pinky) to clear.")

    decoder = TernaryDecoder(
        on_letter=logger.record_letter,
        on_command=logger.record_command,
        on_number=logger.record_number,
        timeout_seconds=9999,  # no auto-reset mid-drill; 'stop' ends it explicitly
        on_partial=on_partial,
        on_danger=on_danger,
    )

    while True:
        line = input("> ").strip().lower()
        if line == "stop":
            break
        for token in line.split():
            decoder.tap(token)
            stats = logger.report()
            print(f"    ({stats['gross_wpm']:.1f} gross wpm / {stats['net_wpm']:.1f} net wpm so far)")

    logger.final_report(target=target if target else None)


# ---------------------------------------------------------------------------
# 6. Simulation CLI - test the decode logic with no hardware attached
# ---------------------------------------------------------------------------

def run_simulation():
    print("Simulation mode. Type finger names separated by spaces on one line,")
    print("e.g.:  index middle ring   (decodes one letter)")
    print("       thumb index index   (decodes one command)")
    print("Type 'quit' to exit, 'table' to print the full letter table.\n")

    def on_letter(symbol):
        print(f"  -> LETTER: {symbol!r}")

    def on_number(digit):
        print(f"  -> DIGIT: {digit!r}")

    def on_command(action):
        print(f"  -> COMMAND: {action}")

    def on_partial(candidates, mode):
        label = PARTIAL_LABELS.get(mode, mode)
        shown = " ".join(c if mode.startswith("command") else repr(c) for c in candidates)
        print(f"    [{len(candidates)} {label} remaining: {shown}]")

    def on_danger(finger):
        print(f"    !! blocked: 3rd consecutive {finger} tap would power off the Tap Strap. "
              f"Tap a different finger (or pinky) to clear.")

    decoder = TernaryDecoder(on_letter, on_command, on_number=on_number, timeout_seconds=9999,
                              on_partial=on_partial, on_danger=on_danger)

    while True:
        line = input("> ").strip().lower()
        if line == "quit":
            break
        if line == "table":
            for (d0, d1, d2), sym in sorted(LETTER_TABLE.items()):
                fingers = "-".join(DIGIT_TO_FINGER[d] for d in (d0, d1, d2))
                print(f"  {sym!r:>4}: {fingers}")
            ext_fingers = "-".join(DIGIT_TO_FINGER[d] for d in EXTENSION_PREFIX)
            for digit in sorted(EXTENSION_MAP):
                print(f"  {EXTENSION_MAP[digit]!r:>4}: {ext_fingers}-{DIGIT_TO_FINGER[digit]}  (overflow, 4 taps)")
            print()
            for (d0, d1), action in sorted(COMMAND_TABLE.items()):
                fingers = "-".join(DIGIT_TO_FINGER[d] for d in (d0, d1))
                print(f"  thumb -> {fingers}: {action}")
            cmd_ext_fingers = "-".join(DIGIT_TO_FINGER[d] for d in COMMAND_EXTENSION_PREFIX)
            for digit in sorted(COMMAND_EXTENSION_MAP):
                action = COMMAND_EXTENSION_MAP[digit]
                print(f"  thumb -> {cmd_ext_fingers}-{DIGIT_TO_FINGER[digit]}: {action}  (overflow, 3 taps after thumb)")
            print()
            print("  -- number mode (thumb -> ring-middle-ring to enter) --")
            for (d0, d1), digit in sorted(NUMBER_TABLE.items()):
                fingers = "-".join(DIGIT_TO_FINGER[d] for d in (d0, d1))
                print(f"  {digit!r:>4}: {fingers}")
            num_ext_fingers = "-".join(DIGIT_TO_FINGER[d] for d in NUMBER_EXTENSION_PREFIX)
            for digit in sorted(NUMBER_EXTENSION_MAP):
                print(f"  {NUMBER_EXTENSION_MAP[digit]!r:>4}: {num_ext_fingers}-{DIGIT_TO_FINGER[digit]}  (overflow, 3 taps)")
            print()
            print("  (space/A/N as 3x-same-finger codes are forbidden -- power-off gesture)")
            print("  (thumb while in number mode -> back to letters)")
            continue

        decoder._reset()  # each simulated line is a fresh sequence
        for token in line.split():
            decoder.tap(token)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "simulate"
    if mode == "run":
        run_hardware()
    elif mode == "practice":
        run_practice()
    else:
        run_simulation()
