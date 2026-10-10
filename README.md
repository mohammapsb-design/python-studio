# Python Studio for Termux

> **Experimental.** Full-screen Python editor on Android via [Termux:GUI](https://github.com/termux/termux-gui) and the [Monaco Editor](https://microsoft.github.io/monaco-editor/) (the engine behind VS Code).

```bash
python-studio hello.py
```

Edit, Run, live syntax / undefined-name markers, soft-keyboard extra keys (Termux-style), pinch-to-zoom, and a toggleable console.

---

## IMPORTANT DISCLAIMER — READ BEFORE USE

**This project was created entirely with the assistance of artificial intelligence (“vibe-coded”). It has not been independently audited, security-reviewed, or formally tested on a full range of devices.**

- There is **no warranty** of any kind — express or implied.
- The authors and contributors accept **no liability** for data loss, security issues, crashes, incorrect program behavior, or any other damage arising from use of this software.
- By installing or running this software, **you accept full and sole responsibility** for any consequences.
- Use it only if you understand the risks of running experimental, AI-generated code on your device.

See the MIT License in `LICENSE` and the longer technical notes in [`DOCUMENTATION.md`](DOCUMENTATION.md).

---

## Requirements

| Component | Notes |
|-----------|--------|
| [Termux](https://termux.dev/) | Android terminal |
| [Termux:GUI](https://github.com/termux/termux-gui) | Same source as your Termux app (F-Droid or GitHub). Open it once. |
| Python 3.8+ | `pkg install python` |
| Network (once) | Only for `python-studio --fetch-monaco` (~15 MB) |

Python dependency (installed by pip): [`termuxgui`](https://pypi.org/project/termuxgui/) ≥ 0.1.5.

---

## Quick install

```bash
# 1. Termux packages
pkg update && pkg install python

# 2. Clone / unpack this project, then:
cd python-studio
pip install .

# 3. One-time Monaco download (offline after this)
python-studio --fetch-monaco

# 4. Run
echo 'print("hello")' > hello.py
python-studio hello.py
```

Update later with `pip install -U .` from the project directory.

---

## Common commands

```bash
python-studio hello.py              # open file in Monaco editor
python-studio --basic-editor f.py   # lightweight editor (no Monaco)
python-studio --fetch-monaco        # download Monaco runtime, then exit
python-studio --version
python-studio --help
```

---

## Features (short)

- Monaco editor (syntax highlighting, find, undo/redo, completions)
- Live red markers for syntax errors and undefined names (e.g. `primt`)
- Termux-style extra key row (ESC, TAB, arrows, …) above the soft keyboard
- Pinch-to-zoom font size
- Console show/hide button
- Run / Stop with stdout & stderr in the console

**Not included yet:** multi-file tabs, built-in file manager.

---

## More documentation

Full technical write-up, architecture, flags, limitations, and security notes:

**→ [`DOCUMENTATION.md`](DOCUMENTATION.md)**

---

## License

[MIT](LICENSE) — see also the disclaimer above. Monaco Editor is separately MIT-licensed by Microsoft.
