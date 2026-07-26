# Ternary Typer

A text input method built around 3 fingers instead of a full keyboard: **index / middle / ring**, plus **thumb** (punctuation & mode switching) and **pinky** (word completion, numbers, escape).

Try it live: open `index.html` in a browser, or enable GitHub Pages on this repo (Settings → Pages → deploy from `main`) to get a shareable URL.

## How it works

Each letter is a base-3 number, 3 taps deep: `index / middle / ring` map to digits `0 / 1 / 2`, and 3 digits give `3^3 = 27` codes — exactly enough for A–Z plus space.

```
tap 1        tap 2        tap 3        →  letter
index    →   index    →   index        →  A
index    →   index    →   middle       →  B
...
ring     →   ring     →   ring         →  space
```

**Thumb** opens a punctuation/command menu (2 taps → period, comma, backspace, enter, question mark, exclamation point, plus a 3-tap overflow branch for apostrophe, caps lock, and entering number mode).

**Pinky** is context-sensitive: mid-word, it accepts the top word-completion suggestion; idle, it toggles number mode; mid-sequence, it clears/aborts.

### Features
- Word completion, ranked by real word frequency
- Spell-check (wavy underline for words outside the embedded ~9,900-word list)
- Auto-capitalization at sentence starts, plus the standard "i" → "I" fix
- Auto-space after sentence punctuation
- Adjustable hand size (S/M/L), scaling button size, spacing, and the arc layout together
- Works via real keyboard keys (`J`/`K`/`L`/`Space`/`;`) or on-screen touch buttons — same tool, either input method

## Project history

This started as an attempt to build the scheme on a **Tap Strap** (a wearable finger-tap keyboard). That turned out to be a dead end: the device has hardware-level gestures — 3 taps of the same finger powers it off, and fast repeated taps trigger its own mode-switching — that can't be reliably suppressed from the SDK layer. `python/tap_strap_decoder.py` is that original implementation, kept here since the core ternary-tree logic is unchanged; it's just not usable on that specific hardware without a lot of extra guard logic (which is in there, for the curious).

The current, actively-used version (`index.html`) drops all of that hardware-specific workaround code, since a keyboard key or a touch button has no such gestures to fight. It runs entirely in the browser.

## Attribution

The embedded word list (`WORD_LIST` in `index.html`) is the full ~9,900-entry list from [first20hours/google-10000-english](https://github.com/first20hours/google-10000-english) (MIT-style public frequency data, no-swears variant), itself derived from Peter Norvig's word-frequency compilation of Google's n-gram corpus. It's a frequency list, not a curated dictionary — expect some false positives/negatives from spell-check.

## License

MIT — see `LICENSE`.
