# Port Listener

A PuTTY-style serial (UART) monitor built with PyQt6 + pyserial, with two CSV logging modes.

Double-click **Port Listener.command** in Finder to start it, or from a terminal:

```bash
pip install -r requirements.txt
python3 port_listener.py
```

## Theme and colours

**Dark** and **Light** in the top-right corner; the change is instant. The dark theme is a deep
blue-slate, the light theme is a warm paper white — no flat system grey anywhere.

The **Colours** panel on the right lets you recolour the app yourself: click a swatch, pick a
colour, and it applies immediately. Twelve slots are exposed — window, panels, buttons, borders,
text, dim text, accent, terminal background and text, matched background and text, and the
recording green.

Your colours are stored **per theme**, so a change to the dark theme leaves the light one alone.
**Reset colours of this theme** goes back to the built-in palette. Colours are saved with the rest
of your settings; invalid values in the file are ignored rather than breaking the app.

The built-in palettes are `PALETTES` in [theme.py](theme.py) if you would rather edit defaults
directly.

## Settings

**Save settings** writes everything to `settings.json` next to the app: theme, your custom colours,
all connection parameters, terminal options, all three filter setups, your tags and removal list,
the log folders, the window size and both splitter positions. They are restored the next time you
start.

**Save on exit** (on by default) does the same automatically when you close the window.
**Reset** deletes `settings.json` and restores the built-in defaults.

Log *folders* are remembered, but the file name is always freshly dated, so a new session never
overwrites the previous capture. A missing or damaged `settings.json` is ignored rather than
crashing the app.

## Connection

Pick the port (**Refresh** rescans, so you can plug the board in after starting), set the
**baud rate** (dropdown with the standard rates, or type any custom value), data bits, parity,
stop bits and flow control, then press **Connect**. Settings are locked while connected.

The bottom input box sends text back to the board — pick the line ending (LF / CR / CRLF / none)
and press Enter.

## Function 1 — Raw log to CSV

Everything that arrives is timestamped and written to a CSV, minus the noise.

Columns: `date, time, elapsed_s, line`

Filtering options:

| Option | What it does |
| --- | --- |
| Strip escape codes and control characters | Removes ANSI/VT100 colour codes and non-printable bytes, so the CSV holds plain text |
| Drop noise lines above *N* % junk | Discards garbage lines, where "junk" is the share of characters that are not letters, digits or spaces. A line such as `!"§!%"§%"§$` scores 100 %; normal log text scores under 40 %. Default cut-off is 60 % |
| Drop empty lines | Skips blank lines |

Choose the file (a dated name under `~/Documents/PortListener/` is filled in for you) and press
**Start recording**. Rows are flushed to disk immediately, so the file is valid even if the board
is unplugged mid-capture. Pressing **Stop recording** closes the file and pre-fills a fresh name.

If you turn on **Hide junk lines**, filtered lines disappear from the terminal view too;
otherwise you still see them on screen but they never reach the CSV.

## Function 2 — Tag log to CSV

Only lines containing one of your tags are kept — every other line is ignored.

Type the tags into the **Tags / keywords** box, one per line or comma separated, for example:

```
<info>
<warn>, <error>
```

A line like `<info> boot ok` matches and is saved; `adc raw=2048` does not and is skipped.
Tags can be edited at any time, including while recording.

- **Regex** — treat each tag as a regular expression (e.g. `<(info|warn)>`, `TEMP=\d+`).
  Invalid patterns are reported under the box instead of crashing.
- **Case sensitive** — off by default, so `<INFO>` matches `<info>`.
- **Strip tag from saved text** — saves `boot ok` instead of `<info> boot ok`; the tag still gets
  its own column.

Columns: `date, time, elapsed_s, tag, line`

Matched lines also appear live in the green **Matched lines** pane under the terminal. The two logs
are independent — run either one alone or both at once.

## Function 3 — Remove words and symbols from saved lines

Function 2 decides *which lines* to keep. This one cleans up *what is inside* each line, and it
applies to **both** CSV files.

Put one entry per line in the **Remove words / symbols** box — one per line only, so that commas,
semicolons and other punctuation can be filtered too:

```
<info>
;;
 [V]
```

`<info> vbat 3.12 [V];; ok` is then saved as `vbat 3.12 ok`.

- **Regex** — treat each entry as a regular expression, e.g. `0x[0-9A-F]+` or `\s+ms`.
  Invalid patterns are reported instead of crashing.
- **Case sensitive** — off by default.
- **Tidy leftover spaces** — collapses the double spaces that removal leaves behind and trims the
  ends. On by default.
- **Apply in terminal too** — off by default, so the terminal keeps showing exactly what the board
  sent while the CSV holds the cleaned version.

Order matters and is deliberate: **tags are matched on the original line, before anything is
removed.** So you can filter `<info>` out of the saved text and still use `<info>` as a tag — the
tag also keeps its own column in the CSV.

## Notes

- Lines are split on `\n`. A partial line with no newline (a prompt, for instance) is flushed
  after one second of silence, so it is not lost.
- Reading happens on a background thread and the UI updates in ~50 ms batches, so high baud
  rates do not freeze the window.
- The terminal keeps the last 20 000 lines on screen; the CSV files keep everything.
- The status bar shows line, filtered and matched counts plus the current row count of each log.
