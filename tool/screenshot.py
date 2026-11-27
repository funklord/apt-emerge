#!/usr/bin/env python3
"""Pictures of emerge at work, for the README: `make screenshots`.

Each scene runs the real `emerge` from this tree under a pseudo-terminal,
so it colours its output exactly as it would for a person, and renders
what it printed as an SVG: text rather than pixels, a few kilobytes,
readable in a diff, sharp at any zoom, and drawn by GitHub like any image.
Only pretend runs (-p), so nothing is installed and no root is needed --
but they ask this machine's apt and the network, so a scene shows the
day it was taken. Regenerate when the output changes visibly, not per
build: each file is replaced, never versioned beside the last.

Stdlib only, like the program it photographs.
"""

import html
import os
import pty
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "doc", "screenshot")

# name, command line shown, argv. Chosen to show what the README claims.
# One build from each registry the README describes, in one plan.
SCENES = [
	("registry-build",
	 "emerge -pv -b pypi-requests cargo-ripgrep npm-prettier gem-rubocop",
	 ["-pv", "-b", "pypi-requests", "cargo-ripgrep", "npm-prettier",
	  "gem-rubocop"]),
]

# xterm's 16 colours, as most dark terminal themes draw them
PALETTE = ["#000000", "#cd3131", "#0dbc79", "#e5e510", "#2472c8", "#bc3fbc",
           "#11a8cd", "#e5e5e5", "#666666", "#f14c4c", "#23d18b", "#f5f543",
           "#3b8eea", "#d670d6", "#29b8db", "#ffffff"]
FG, BG = "#d4d4d4", "#1e1e1e"
FONT, CHAR_W, LINE_H, PAD = 14, 8.43, 18, 16


def run(argv, limit=1 << 20, timeout=300):
	"""Run emerge under a pty and return what it wrote. A pty, not a pipe,
	because emerge colours only a terminal -- which is the point."""
	pid, fd = pty.fork()
	if pid == 0:
		os.environ.pop("NO_COLOR", None)
		os.environ["TERM"] = "xterm-256color"
		os.environ["COLUMNS"] = "100"
		os.execvp(sys.executable,
		          [sys.executable, os.path.join(ROOT, "emerge")] + argv)
	out = bytearray()
	import select, time
	deadline = time.monotonic() + timeout
	while len(out) < limit:
		left = deadline - time.monotonic()
		if left <= 0:
			os.kill(pid, 9)
			raise RuntimeError(f"emerge {' '.join(argv)}: no end in {timeout}s")
		r, _, _ = select.select([fd], [], [], left)
		if not r:
			continue
		try:
			chunk = os.read(fd, 65536)
		except OSError:             # EIO: the child closed the terminal
			break
		if not chunk:
			break
		out += chunk
	os.close(fd)
	_, status = os.waitpid(pid, 0)
	return out.decode("utf-8", "replace"), os.waitstatus_to_exitcode(status)


def spans(line):
	"""Split one line at its SGR sequences: [(text, colour, bold)]."""
	out, fg, bold = [], None, False
	for part in re.split(r"(\x1b\[[0-9;]*m)", line):
		m = re.fullmatch(r"\x1b\[([0-9;]*)m", part)
		if not m:
			if part:
				out.append((part, fg, bold))
			continue
		for code in [int(c) if c else 0 for c in m[1].split(";")]:
			if code == 0:
				fg, bold = None, False
			elif code == 1:
				bold = True
			elif code == 22:
				bold = False
			elif 30 <= code <= 37:
				fg = code - 30
			elif 90 <= code <= 97:
				fg = code - 90 + 8
			elif code == 39:
				fg = None
	return out


def svg(title, text):
	text = re.sub(r"\x1b\[[0-9;]*[A-LN-Za-ln-z]", "", text)   # not SGR
	lines = ["$ " + title] + [l.split("\r")[-1] for l in
	                          text.replace("\r\n", "\n").rstrip("\n").split("\n")]
	cols = max(len(re.sub(r"\x1b\[[0-9;]*m", "", l)) for l in lines)
	w = int(cols * CHAR_W + 2 * PAD)
	h = len(lines) * LINE_H + 2 * PAD
	body = []
	for i, line in enumerate(lines):
		y = PAD + (i + 1) * LINE_H - 4
		parts = [(line[2:], None, True)] if i == 0 else spans(line)
		if i == 0:
			parts.insert(0, ("$ ", 2, True))
		tspans = []
		for t, fg, bold in parts:
			colour = PALETTE[fg + (8 if bold and fg is not None and fg < 8 else 0)] \
			         if fg is not None else FG
			weight = ' font-weight="bold"' if bold else ""
			tspans.append(f'<tspan fill="{colour}"{weight}>{html.escape(t)}</tspan>')
		body.append(f'<text x="{PAD}" y="{y}" xml:space="preserve">'
		            + "".join(tspans) + "</text>")
	return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
	        f'viewBox="0 0 {w} {h}" role="img">\n<title>{html.escape(title)}</title>\n'
	        f'<rect width="100%" height="100%" rx="6" fill="{BG}"/>\n'
	        f'<g font-family="DejaVu Sans Mono, Menlo, Consolas, monospace" '
	        f'font-size="{FONT}">\n' + "\n".join(body) + "\n</g>\n</svg>\n")


def main():
	os.makedirs(OUT, exist_ok=True)
	for name, title, argv in SCENES:
		text, rc = run(argv)
		if rc != 0:
			sys.stderr.write(text)
			sys.exit(f"screenshot {name}: emerge exited {rc}")
		path = os.path.join(OUT, name + ".svg")
		with open(path + ".new", "w", encoding="utf-8") as f:
			f.write(svg(title, text))
		os.replace(path + ".new", path)
		print(f"wrote {os.path.relpath(path, ROOT)}")


if __name__ == "__main__":
	main()
