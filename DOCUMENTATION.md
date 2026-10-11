# Python Studio — Technical documentation

This document is the long-form reference for architecture, install options, CLI flags, dependencies, limitations, and security. The short overview lives in [`README.md`](README.md).

---

## 1. Disclaimer (binding)

**This software was produced with heavy use of artificial intelligence (AI-assisted / “vibe-coded” development). It has not undergone independent code review, formal security audit, or exhaustive device testing.**

You use this software **entirely at your own risk**.

The copyright holders, contributors, and anyone who published or shared this repository:

- provide the software **“AS IS”**, with **no warranties** of any kind (including merchantability, fitness for a particular purpose, and non-infringement);
- are **not liable** for any claim, damage, data loss, security incident, or other liability arising from use, inability to use, or misuse of the software;
- do **not** guarantee that the editor, the bridge to Termux:GUI, the Monaco delivery path, or the lint logic is correct, complete, or safe on your device.

By cloning, installing, or running Python Studio, **you accept sole responsibility** for backup of your files, verification of behavior, and any consequences. If you need a reviewed, supported product, do not use this project.

This disclaimer is in addition to the [MIT License](LICENSE).

---

## 2. What it is

Python Studio is an **experimental** full-screen Python editor for **Android Termux**. It runs inside a [Termux:GUI](https://github.com/termux/termux-gui) activity that hosts a WebView. The default editor UI is the [Monaco Editor](https://microsoft.github.io/monaco-editor/) (same core family as VS Code), delivered offline after a one-time download.

Typical flow:

1. You run `pyo path/to/file.py` in Termux.
2. A GUI window opens with toolbar, editor, and console.
3. You edit, optionally see live red error markers, then **Run**.
4. Closing the activity returns you to the Termux shell.

There is also a **basic editor** fallback (`--basic-editor`) that does not load Monaco.

---

## 3. Dependencies and upstream projects

### 3.1 Required on the device

| Item | Role | Official / project page |
|------|------|-------------------------|
| **Termux** | Linux environment on Android | https://termux.dev/ · https://github.com/termux/termux-app |
| **Termux:GUI** | Android UI (Activity + WebView) controlled from Termux | https://github.com/termux/termux-gui |
| **Python 3.8+** | Runtime for the app and for running your scripts | https://www.python.org/ · Termux: `pkg install python` |

Install Termux:GUI from the **same channel** as your Termux app (F-Droid or GitHub releases). Open the Termux:GUI app once so Android grants what it needs.

### 3.2 Python package dependency

Declared in `pyproject.toml`:

| Package | Version | Purpose | Page |
|---------|---------|---------|------|
| **termuxgui** | ≥ 0.1.5 | Python bindings for Termux:GUI protocol | https://pypi.org/project/termuxgui/ |

Installed automatically by `pip install .`.

### 3.3 Monaco Editor (optional at runtime, required for the full UI)

| Item | Detail |
|------|--------|
| Project | Monaco Editor |
| Site | https://microsoft.github.io/monaco-editor/ |
| Repo | https://github.com/microsoft/monaco-editor |
| License | MIT (Microsoft) |
| How this app uses it | Official `monaco-editor` npm tarball is fetched **once** by `pyo --fetch-monaco`, verified against the npm registry checksum, and stored under `~/.local/share/python-studio/monaco`. Only the AMD `min/vs` tree is kept. |
| Size | Download on the order of **~15 MB**; on-disk usable tree roughly **~4–5 MB**. Each editor launch pushes that tree into the WebView over the local Termux:GUI bridge (no public HTTP server). |

Without Monaco, the program exits with code **3** unless you pass `--basic-editor`.

### 3.4 Standard library only for lint

Live markers use Python’s built-in **`ast`** module (no `pyflakes` / `ruff` required). That covers:

- `SyntaxError`
- simple **undefined name** detection (e.g. typing `primt` instead of `print`)

It is **not** a full type checker or linter.

### 3.5 Build / test (developers)

- **setuptools** ≥ 64 (build backend)
- **pytest** (tests; not required to run the editor)
- **Node.js** (optional; some page tests stub Monaco under Node)

---

## 4. Installation

### 4.1 End-user path (recommended)

```bash
pkg update && pkg install python

# obtain the source (clone or unzip the release)
cd python-studio
pip install .

pyo --fetch-monaco    # once; needs network
pyo --version
echo 'print("hello from studio")' > hello.py
pyo hello.py
```

### 4.2 Editable install (development)

```bash
cd python-studio
pip install -e .
```

### 4.3 Environment overrides

| Variable | Meaning |
|----------|---------|
| `PSTUDIO_MONACO_DIR` | Directory that **contains** the `vs/` tree (alternative to the default share path) |

### 4.4 Self-contained wheel (advanced)

```bash
python -m python_studio.monaco_assets --bundle
# populates src/python_studio/assets/monaco/ before packaging
pip install .
```

Monaco is **not** committed to git by default (see `.gitignore`); only `assets/monaco/README.txt` is kept in-tree.

---

## 5. Command-line interface

Entry point: `pyo` → `python_studio.cli:main`.

```text
usage: pyo [-h] [--basic-editor] [--fetch-monaco] [--version] [file]
```

| Argument / flag | Description |
|-----------------|-------------|
| `file` | Path to the Python file to edit. Required unless `--fetch-monaco` or `--version` / `--help`. If the path does not exist and stdin is a TTY, you are asked whether to create it on first save. |
| `--basic-editor` | Use the lightweight HTML textarea editor instead of Monaco. |
| `--fetch-monaco` | Download and install the Monaco runtime, then exit. **Only intentional network use** of the app. |
| `--version` | Print version and exit. |
| `-h` / `--help` | Help text. |

### Examples

```bash
pyo script.py
pyo ~/projects/app/main.py
pyo --basic-editor notes.py
pyo --fetch-monaco
pyo --version
```

### Exit codes (practical)

| Code | Typical meaning |
|------|-----------------|
| 0 | Success / normal GUI session end |
| 1 | User declined creating a missing file, or a file error before GUI |
| 3 | Monaco required but not available (and not using `--basic-editor`) |

(Other non-zero codes may appear on internal failures.)

---

## 6. Features

### 6.1 Editor (Monaco)

- Python syntax highlighting and bracket aids  
- Completions (keywords, builtins, names already in the file)  
- Find, undo/redo, Tab (accept suggestion or indent)  
- Toolbar: Run, Stop, Save, Tab, Undo, Redo, Find, Console, Exit  
- Hardware shortcuts where available: Ctrl+S save, Ctrl+Enter run  

### 6.2 Live markers (“red squiggles”)

About **one second** after typing pauses, the page sends the buffer to Python (`lint` bridge op). Markers are applied with Monaco `setModelMarkers` for:

- syntax errors from `ast.parse`  
- undefined names from a simple AST walk  

### 6.3 Soft keyboard UX

- Extra key bar (Termux-like): ESC, TAB, arrows, HOME/END, PGUP/PGDN, sticky CTRL/ALT, `/`, `-`  
- Shown when the viewport shrinks (keyboard open)  
- Pinch-to-zoom adjusts editor `fontSize` / line height  
- Optional “Tap here to start typing” hint if Android blocks programmatic keyboard open  

### 6.4 Console

- Shows stdout, stderr, run status  
- **Console** toolbar button toggles visibility for more editor space  
- Opening **Run** forces the console visible  
- **Program input (stdin):** while a program runs, an input row appears under the output. *Send* (or Enter) sends one line to `input()` / `sys.stdin`; *EOF* closes stdin (like Ctrl-D). Typed lines are echoed in the console. Input goes through a small writer thread, so a program that never reads its input cannot freeze the GUI  
- **Output limit:** the console keeps the first 1,000,000 bytes of a run and says so once. A program that keeps printing far past that (more than 64 MiB discarded) is stopped automatically instead of burning CPU in the background  
- **Stop:** SIGTERM to the program's process group, SIGKILL to the whole group after 2 s (also when the main process already exited but a child ignored SIGTERM). Leaving the IDE (Exit, Back, `kill`, closing the Termux session) stops the program synchronously, so nothing keeps running afterwards  
- **Memory cap (optional):** `PSTUDIO_MEM_MB=<n> pyo file.py` limits the program's address space (`ulimit -v`). Off by default because libraries such as numpy reserve a lot of address space  

### 6.5 Not implemented

- Multi-file tabs  
- In-app file manager / project tree  
- Language server (Pylance-style)  
- Debugger UI  

---

## 7. Architecture (short)

```text
Termux shell
    └── pyo (CLI)
            └── app.py  →  Termux:GUI Activity + WebView
                    ├── setData(editor HTML)
                    ├── evaluateJS → push Monaco chunks, then PS.load(...)
                    └── webviewConsoleMessage ← "PSBRIDGE:{json}"
                            └── bridge.Controller
                                    ├── files.Document (read/save)
                                    ├── runner.Runner (subprocess python)
                                    └── lint.lint_python (markers)
```

Design constraints that shaped the code:

- Termux:GUI WebView belongs to the **Termux:GUI app**, not Termux itself → cleartext `http://127.0.0.1` is unreliable for serving Monaco.  
- Solution: **no local HTTP server**; Monaco files are base64-chunked over `evaluateJS` after `setData`.  
- `setData` pages have an opaque origin → **Web Workers disabled**; Monaco runs on the main-thread fallback.  
- Bridge ops are a **fixed allow-list** (`ready`, `save`, `run`, `stop`, `exit`, `lint`, `stdin`, `eof`) — not a generic remote shell.

More historical detail about failed HTTP approaches is preserved in older notes; the shipping path is the bridge delivery pipeline in `monaco_bundle.py` / `monaco_assets.py`.

---

## 8. Security and trust boundaries

- **Only network use:** `pyo --fetch-monaco` (HTTPS npm tarball + registry checksum check). Editing and running are offline with respect to the public internet.  
- **Running your code:** **Run** saves the buffer to the chosen file, then executes it with the same Python that launched the studio. That is equivalent in power to running `python yourfile.py` yourself.  
- **WebView:** executes the bundled page and the Monaco scripts that were delivered from your local Monaco directory (integrity checked with size + Adler-32 against corruption in transit, not against a malicious local replace). Anyone who can write to the Monaco install dir can change what the WebView executes.  
- **AI-generated code:** treat the whole repository as untrusted until you review it. Do not run it on devices or accounts you cannot afford to expose.

---

## 9. Limitations and known gaps

- Experimental status; device matrix is incomplete.  
- Monaco is large for low-end phones or metered links (use `--basic-editor` or skip install).  
- Android may refuse to open the soft keyboard without a user gesture.  
- Undefined-name lint is heuristic (no import resolution of third-party packages).  
- Single file per session.  
- A running program has no terminal: `input()` works, but `getpass`, `curses`, `readline` editing and anything that needs a TTY do not.  
- The IDE does not detect that the file was changed on disk by another program while it is open; Save overwrites it.  
- UI strings and extra-key labels are ASCII-oriented for embedding constraints.

---

## 10. Project layout

```text
python-studio/
├── README.md                 # short GitHub landing page
├── DOCUMENTATION.md          # this file
├── LICENSE                   # MIT
├── pyproject.toml
├── src/python_studio/
│   ├── cli.py                # argparse entry
│   ├── app.py                # Termux:GUI activity / WebView
│   ├── bridge.py             # PSBRIDGE protocol + Controller
│   ├── files.py              # Document load/save limits
│   ├── runner.py             # subprocess run / stop
│   ├── lint.py               # ast + undefined names
│   ├── monaco_assets.py      # fetch / locate Monaco
│   ├── monaco_bundle.py      # chunk delivery to the page
│   └── assets/
│       ├── editor.html       # Monaco UI + JS glue
│       ├── basic_editor.html
│       └── monaco/           # optional bundled vs/ (usually fetched to ~/.local/share)
└── tests/
```

---

## 11. License summary

- **Python Studio** source: [MIT](LICENSE)  
- **Monaco Editor**: MIT (Microsoft) — obtained via `--fetch-monaco` or a self-contained bundle  
- **termuxgui** / Termux:GUI: see their respective repositories and licenses  

Again: **no warranty; AI-assisted codebase; you bear full responsibility for use.**
