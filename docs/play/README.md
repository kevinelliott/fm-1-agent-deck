# play/ — Agent Deck, running live in fm1-emu

This folder is a self-contained web player: the **fm1-emu** FM-1
emulator with Agent Deck as its only firmware, published via GitHub
Pages so the README can link a deck that actually runs.

- **Player (full page):** `index.html` at this folder's Pages URL
- **Auto-playing demo:** add `?fw=agentdeck&script=demo.txt` — the
  emulator's try-out mode plays `demo.txt` by itself after the page's
  one Start click (browsers gate sound behind a click; the deck is
  silent, but the emulator's audio thread is what starts): the
  daemon's frame stream goes in through the MIDI IN jack at ~2 s,
  HOME flips the GRID to boxes and back, the ALGORITHM encoder walks
  DETAIL → APPROVALS → MENU → ABOUT → GRID, and a key press selects
  the blocked agent. `python3 tools/emu_shots.py --player` writes it.
- **Embed (device only):** add `&embed=1` — just the FM-1 and its
  controls, for iframing into another site.

## Provenance

The emulator files (`index.html`, `main.js`, `fwsc.js`, `jit.js`,
`persistence.js`, `tryout.js`, `worklet.js`, `style.css`,
`fm1_web.wasm`, `og.png`) are the built web player of
**[fm1-emu](https://github.com/joakimeriksson/fm1-emu)** by Joakim
Eriksson, downloaded from its live site
(joakimeriksson.github.io/fm1-emu) on 2026-11-09 and redistributed
under its MIT license (`LICENSE-fm1-emu.txt`). Thank you — it's a
remarkable piece of work.

Local changes versus upstream, deliberately minimal:

1. `firmwares.json` (beside this file) replaces the upstream catalog
   with a single entry: Agent Deck, its `.fwsc` hosted right here so
   the fetch is same-origin.
2. One marked patch in `main.js` (`AGENT DECK VENDORED COPY`): the
   upstream page merges the Firmware Hub's catalog into its picker at
   load; this player is only about Agent Deck, so the merge is off.
   Nothing else in the file is touched.

To refresh the player: download the same file list from the upstream
site again, re-apply the two changes above, and re-check the demo.

`agentdeck-0.6.2.fwsc` is a copy of `../../dist/agentdeck-0.6.2.fwsc` —
refresh it when the firmware version changes (and update
`firmwares.json`'s version field to match).
