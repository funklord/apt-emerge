# emerge-for-debian — project notes

A Gentoo Portage–flavoured package manager for Debian/Ubuntu, implemented as a
single stdlib-only Python 3 script. It speaks emerge's CLI dialect and paints
Portage-style output, but drives real Debian tooling underneath.

The current artifact is one file: `emerge` (~2600 lines). It has been developed
and tested chat-side without persistent system access, which is why we're moving
to Claude Code — several remaining tasks need a real Debian box, a session, and
multi-version repos to test against.

---

## What it is / core intent

- One command, `emerge`, that behaves like Gentoo's for the operations people
  actually use: install, remove, search, world upgrade, depclean, config-file
  merging, and source builds.
- **Two backends, auto-selected:**
  - **apt backend** — used when `apt-get` is present. Thin: apt resolves,
    downloads, verifies, installs; the script translates the experience into
    Portage-speak.
  - **dpkg backend** — used when `apt-get` is absent (embedded boxes with only
    dpkg). Self-contained: implements sources parsing, index sync, a native
    dependency resolver, `.deb` fetch+SHA256, install via dpkg, and a real
    `world` file.
  - Force with `--backend=apt|dpkg` or `EMERGE_BACKEND=`.
- **Zero dependencies** beyond the Python 3 stdlib. This is a hard constraint —
  the dpkg backend targets embedded systems where you `scp` one file and run it.
  Do not add pip/apt deps.
- Runs on real Debian (not just Ubuntu). Debian-specific version quirks
  (binNMU `+bN`, stable-update `+debNuN` suffixes) matter — see version compare.

## Hard design rules (don't regress these)

1. **Single file, stdlib only.** The deploy story for apt-less boxes is one
   `scp`. If we ever split into a project dir (see "Open decisions"), the
   *shipped artifact* must still be a single amalgamated file.
2. **dpkg backend is binary-only.** Source builds (`-b`/`-B`) are apt-only by
   design: a box big enough for `build-essential` can afford real apt. The dpkg
   backend refuses `-b/-B` with a pointer to the host-build workflow.
3. **No persistent pins, ever.** `--no-dep-upgrade` version selections are
   `pkg=version` arguments to a single `apt-get`/`dpkg` invocation — never
   `apt-mark hold`, `/etc/apt/preferences`, or `dpkg --set-selections`. A crash
   can therefore leave nothing pinned behind. The only `apt-mark` call anywhere
   is `showmanual` (read-only). Keep it this way.
4. **Respond in Portage's dialect.** Output format (`[ebuild N/U/R/D]`, `>>>
   Emerging`, the unmerge block, `--help` layout) mimics real emerge. New
   features should match that voice.

---

## File layout (single file, top to bottom)

Line numbers are approximate (as of ~2619 lines); use them as a map, re-grep
after edits.

- **Imports + path constants** (~11–39): stdlib only; `lzma` is optional
  (minimal builds strip it — code falls back to `.gz`/plain). Paths:
  - `LIB_DIR=/var/lib/emerge-dpkg`, `TREE_DIR=$LIB_DIR/tree`,
    `WORLD=$LIB_DIR/world`
  - `DISTFILES=/var/cache/emerge-dpkg/distfiles`,
    `BINPKGS=/var/cache/emerge-dpkg/binpkgs` (PKGDIR for `-b/-B`)
  - `STATUS=/var/lib/dpkg/status`
  - config-merge paths defined later: `CONF_FILE=/etc/emerge/dispatch-conf.conf`
    (alt `/etc/dispatch-conf.conf`), `ARCHIVE_DIR=/var/lib/emerge/config-archive`,
    `UCF_CACHE=/var/lib/ucf/cache`
- **Colours + message helpers** (~83–104): `einfo/ewarn/eerror`, and `eblock()`
  which prefixes every line of a multi-line error with `!!!` (used for
  `NduWall`).
- **`run`/`capture`/`stream_lines`** (~110–138): `stream_lines` splits a binary
  pipe on both `\r` and `\n` to tame dpkg's pty carriage-return progress
  (fixes run-together/space-padded output). All real apt/dpkg installs go
  through it with `-o Dpkg::Use-Pty=0`.
- **Debian version comparison** (~140–189): `vercmp` implements policy 5.6.12
  (epoch, upstream, revision; `~` sorts before everything). `meets(v,op,want)`
  applies a constraint. This is native (not shelling to `dpkg --compare-versions`)
  for speed and for the apt-less case.
- **`_dep_ok`** (~191): is a dependency alternative already satisfied by an
  installed/chosen package or a Provides? `provides_of` is a **callable**
  (name -> list), not a dict — a past bug called `.get()` on it.
- **`ndu_solve` + `_DpkgIndex`** (~212–414): THE shared `--no-dep-upgrade`
  solver (see its own section below).
- **Version-suffix + session-critical** (~416–555):
  - `upstream_version`, `same_upstream` — detect "same upstream, only the
    Debian revision/binNMU/security suffix differs" (the `libgbm1 25.0.7-2 ->
    25.0.7-2+deb13u1` case).
  - Live session-critical detection (see its own section).
- **`NduWall` + blocker formatting** (~557–603): structured exception carrying
  the list of packages that would have to move, so the CLI can offer an escape
  hatch. `_format_movers` renders the labelled, per-package wall.
- **Control-file parsing** (~605–639): `parse_stanzas` (RFC822 stanzas),
  `parse_depends` (handles `a (>= 1) | b, c`, arch quals, build-profile `[...]`).
- **Shared display** (~641–721): `print_merge_list` (now flags `(session)`
  upgrades), `print_unmerge_list`, `ask_continue` (defaults Yes),
  `ask_yesno` (defaults No — used for risky confirmations).
- **`installed_state`/`is_protected`/`system_set`** (~723–743): parse dpkg
  status; `@system` = Essential + Priority:required.
- **`read_sources`/`fetch`** (~745–786): parse `/etc/apt/sources.list` +
  deb822 `.sources`; `fetch` handles http(s):// and file:// (bare paths too).
- **`class DpkgBackend`** (~788–1308): world file, `sync` (incl. USB flat-dir
  indexing via `dpkg-deb`), `_load_index` (lazy all-versions), the classic
  resolver `_resolve_inner`, `_resolve_no_upgrade` (calls `ndu_solve`),
  download+SHA256, `merge` (dpkg `--unpack`/`--configure`), unmerge, depclean
  (world-closure), search.
- **`class _AptIndex`** (~1310–1367): lazy `apt-cache show`-backed index for
  `ndu_solve` on the apt backend; discovers virtual providers via
  `apt-cache showpkg`.
- **`class AptBackend`** (~1369–1762): `sync`=`apt-get update`; `resolve` builds
  an `apt-get` action and parses `-s` simulation; `_resolve_no_upgrade` uses
  `ndu_solve` via `_AptIndex`, executes as `pkg=version` args; `merge` streams
  translated apt output; unmerge/depclean delegate to apt; source builds
  (`resolve_source`/`build`) — `apt-get source` + `dpkg-buildpackage`, products
  to PKGDIR, `+local1` changelog bump so `@world` won't clobber local builds.
- **Config-merge subsystem** (~1790–2190): dispatch-conf. See its section.
- **`HELP`, `LONG_FLAGS`, `pick_backend`, `main`, `_finish_merge`**
  (~2195–end): arg parsing (Portage-style bundled short flags), action
  dispatch, `NduWall` handling with the interactive escape hatch, `argv[0]`
  dispatch so symlinking to `dispatch-conf`/`etc-update` works.

---

## Backends: mapping to Debian tools

apt backend uses: `apt-get` (install/remove/dist-upgrade/autoremove and
`-s -qq` simulation), `apt-cache` (search/policy/show/showsrc/madison/showpkg),
`apt-mark showmanual` (world = manual marks), `dpkg-query`. **aptitude is not
used at all.**

dpkg backend uses only: `dpkg`, `dpkg-deb`, `dpkg-query`, `tar`, `urllib`, and
its own everything-else.

### CLI surface (Portage dialect)

- `--sync` — apt: `apt-get update`; dpkg: fetch `Packages` indexes into
  `TREE_DIR` over http(s)/file. A `file://` flat entry pointing at a bare dir of
  `.deb`s gets an index generated on the fly (USB-stick repo).
- install atoms; `-a` ask, `-p` pretend, `-v` verbose, `-u` update, `-D` deep,
  `-1` oneshot (don't record in world), `-f` fetchonly.
- Sets: `@world` = `@selected` + `@system`. `@selected` = world file (dpkg) /
  `apt-mark showmanual` (apt). `@system` = Essential+required. `world`/`system`
  bare words accepted.
- `-C`/`--unmerge`, `--depclean`/`-c` (apt: `autoremove`; dpkg: world-closure).
- `-b`/`--buildpkg`, `-B`/`--buildpkgonly` (apt only).
- `--dispatch-conf` / `--etc-update` (config merging).
- `--no-dep-upgrade`, `--with pkg,pkg` (see below).
- Accepted-but-ignored for muscle memory: `-N`, `-q`, `-t`.

---

## `--no-dep-upgrade` (the big/subtle feature)

**Goal:** install/upgrade a target without *upgrading* anything already
installed — to avoid dragging up libc/systemd/kernel (reboots) or the graphics
stack (session restart) just to get a leaf package. Not a pin: it *searches*.

**The rule (final, after several iterations — get this right):**
- A dependency that **is installed and satisfies** its constraint → kept at
  exactly the installed version. Never version-searched.
- A dependency that **is not installed** → take the newest version whose whole
  subtree upgrades nothing installed; step back through its own older versions
  as conflicts are found.
- A dependency that **is installed but cannot satisfy** a constraint → genuine
  wall. Report it.

So version searching only ever ranges over *not-installed* packages. Installed
ones are pinned-to-disk or they're blockers. (Earlier wrong versions: searched
all deps to newest; then searched `>= installed` even for installed packages.)

**Architecture:** one shared solver `ndu_solve(index, inst, iprov, worklist,
atoms, update, allow)` over an abstract index with `all_versions(name)` /
`has(name)` / `provides_of(name)`. Both backends feed it:
- dpkg: `_DpkgIndex` over the in-memory tree (needs all versions per package —
  `_load_index(all_versions=True)`, lazy, only when `--no-dep-upgrade` runs).
- apt: `_AptIndex`, lazy `apt-cache show` per package, then execute the chosen
  versions as `apt-get install pkg=version ...`.

**Escape hatch** (for same-upstream / lockstep walls like the deb13u1 case):
- When the only blocker is a `same_upstream` revision bump, the wall says so
  and names the forcing package + exact constraint.
- Session-critical movers are flagged (see next section).
- `--with pkg,pkg` permits named installed packages to move while everything
  else stays strict; under `-a` the wall is followed by a **default-No** prompt
  offering to allow just the listed movers and re-resolve. `NduWall.movers`
  carries the structured data driving both.

**Known limitation (state honestly, don't claim completeness):** greedy with
single-package backtracking, NOT a complete SAT solver. A graph solvable only
by a non-obvious *combination* of older versions across two independent deps can
still report a false wall. Fixing fully means real backtracking search or
handing the whole constraint set to an external solver.

**Scope/perf:** works on atoms and `@world`; per-target search, so large sets
run many simulations and can be slow (accepted).

---

## Session-critical detection (newest work)

**Purpose:** warn that upgrading a package could restart X/Wayland and close
running GUI apps — the one class where "same-upstream, harmless" is false
(Mesa/compositor rebuilds), and where an "exclude-reboot" filter wouldn't help
(no reboot involved).

**Mechanism (derived, not a hardcoded list):**
1. `_find_session_leaders()` — scan `/proc/*/comm` for names in
   `_SESSION_LEADER_COMMS` (Xorg/Xwayland, gnome-shell/kwin/sway/weston/
   plasmashell/mutter/... , gdm/sddm/lightdm/greetd/...).
2. `_proc_mapped_libs(pid)` — read `/proc/PID/maps`, collect mapped `.so`s.
3. `compute_session_critical_packages()` — one **batched** `dpkg-query -S` maps
   those libs → packages (batched is ~8x faster than per-lib). Cached per
   process.
4. `is_session_critical(name)` — membership in the live set; if no session was
   found, fall back to the static `_SESSION_CRITICAL_EXACT`/`_PREFIX` sets.

**Measured cost:** ~200ms once on a desktop (dominated by the single
`dpkg-query -S`), ~2ms and empty on headless. Cached to 0ms after. Independent
of package count → cheap enough that it runs on **every** `-a/-p/-v` merge list,
not just `--no-dep-upgrade` walls: session-in-use upgrades get an inline
`(session)` marker + a summary warning.

**Remaining maintenance surface / gaps (test on a real desktop):**
- The maintained thing is now `_SESSION_LEADER_COMMS` (process names), much
  shorter/slower-changing than a library list. An exotic compositor not in it
  falls back to the static list.
- Only sees *currently mapped* libs; a lib the session `dlopen`s on demand
  (some driver plugins) may be missed at scan time.
- `comm` truncates at 15 chars (kernel limit) — some names in the set are
  already truncated (`gdm-session-wor`, `lightdm-gtk-gre`, `sddm-greeter` ok).
  **Verify these against real `/proc/*/comm` on target desktops** — this is a
  prime thing to check with system access.

---

## Config-file merging (dispatch-conf)

Debian already parks updated conffiles as `.dpkg-dist`/`.ucf-dist` (== Portage's
`._cfg0000_`). What was missing: a batched review tool, an archive of the
*previously shipped* version (the 3-way ancestor), and hunk-level merging.

- Every apt/dpkg install runs with `--force-confold --force-confdef` so nothing
  prompts mid-install; updates are parked.
- `archive_settled()` runs **after** install and archives the as-shipped state
  of conffiles dpkg did NOT park (unmodified → on-disk == as-shipped) to
  `ARCHIVE_DIR`. Parked files keep their older archive entry = the ancestor.
  `_retire()` promotes the parked copy to ancestor once resolved.
  **Timing is load-bearing** — a past bug archived the *incoming* .deb before
  install, making new==ancestor and silently discarding every update.
- `merge3()` — pure-Python diff3 (`difflib`), conflict markers
  current/as-shipped/new. `_significant()` strips comments+whitespace for the
  "wscomments-only" auto-apply.
- `dispatch_conf()` — auto-applies unmodified / wscomments-only / conflict-free
  3-way; interactive only for real conflicts (keep/replace/merged/$EDITOR/
  mergetool/skip/quit).
- Configurable via `/etc/emerge/dispatch-conf.conf` with Gentoo key names
  (archive-dir, config-protect{,-mask}, frozen-files, automerge,
  replace-unmodified, replace-wscomments, mergetool). `mergetool=` supports
  `{base}{mine}{theirs}{output}` and dispatch-conf's positional `'%s' '%s' '%s'`.
- Reachable as `--dispatch-conf`, `--etc-update`, or by symlinking the script.
- Scope: dpkg conffiles + ucf-managed files only. Files a maintainer script
  writes into `/etc` are NOT protected (would need pre/post snapshots).

---

## Crash-safety

- No persistent pins (see hard rule 3). Interrupted installs recover with the
  normal `dpkg --configure -a` / `apt-get -f install`.
- World file and config writes are atomic (temp + fsync + `os.replace`).
- `--with` allow-set is per-invocation, never persisted.

---

## Testing notes / what needs a real box

Chat-side testing used synthetic `Packages` trees under `TREE_DIR` and mocked
`apt-cache` output. What genuinely needs Claude Code + a real system:

- **Real multi-version repos.** The container's Ubuntu mirror kept only newest
  per package, so `--no-dep-upgrade` step-back and the deb13u1 wall were proven
  on synthetic trees + a `file://` local repo, not against a live mirror with
  `-updates`/`-security` pockets. `libsdl3-dev` doesn't exist in Ubuntu 24.04 —
  the reported failures came from a real Debian 13 box.
- **A graphical session** to validate session-critical detection end-to-end and
  to check `_SESSION_LEADER_COMMS` against actual `/proc/*/comm`.
- **apt-less embedded box** to validate the dpkg backend for real (sync over
  http and USB `file://`, install, depclean world-closure).
- **Config merging** against real package upgrades that ship conffile changes.

Handy synthetic-test pattern used so far: write a `Packages` stanza file into
`/var/lib/emerge-dpkg/tree/`, `dpkg -i` a hand-built `.deb` to simulate an
installed base, then run `--backend=dpkg -p ...`. For apt-path unit tests,
monkeypatch the module-global `capture` to return canned `apt-cache show`
output and call `ndu_solve` directly.

Always `python3 -m py_compile emerge` after edits. There's no test suite yet —
adding one is a natural first Claude Code task (see below).

---

## Open decisions / backlog

1. **Project split.** Whether to break the single file into a `src/` module tree
   + `Makefile` (build → amalgamated single file via a concat tool à la sqlite;
   `make test`; `make deb` — which can be `emerge -B emerge`, it can package
   itself) + `debian/` dir. Decision was pending. Recommended: modules for dev,
   amalgamate to one file for ship. Do NOT use cpack (drags in CMake); use
   `debian/` + `dpkg-buildpackage`, or `dpkg-deb --build` from a staging dir.
   Skip `zipapp` — a `.pyz` isn't `vi`-able on an embedded box, which defeats
   the single-file purpose.
2. **A real test suite** — unit tests for `vercmp`, `parse_depends`, `merge3`,
   and `ndu_solve` (the merge engine has real algorithmic content and deserves
   coverage). This is the natural first thing to do with a repo + CI.
3. **`ndu_solve` completeness** — the greedy false-wall limitation above.
4. **GPG verification** — the dpkg backend verifies each `.deb`'s SHA256 against
   the index but does NOT verify Release signatures. For a real apt-less system
   this is the one thing worth adding (shell to `gpgv`, which isn't "apt").
5. **`_SESSION_LEADER_COMMS` truncation audit** — verify 15-char `comm` names on
   real desktops.

---

## Gotchas that bit us before (don't repeat)

- dpkg pty progress → garbled output. Always `-o Dpkg::Use-Pty=0` + `stream_lines`.
- Config archive timing: archive AFTER install, only unmodified files; promote
  parked copies on resolve. Getting this wrong silently eats config updates.
- `provides_of` is a callable, not a dict.
- `+deb13u1`/`+bN` suffixes: same upstream, higher version — `same_upstream()`
  must special-case these for the escape-hatch labelling.
- `DEBIAN_FRONTEND=noninteractive` alone (without `--force-conf*`) silently
  keeps old conffiles and hides `.dpkg-dist` files — the whole reason
  dispatch-conf exists.
- Version comparison must be native `vercmp` (policy 5.6.12), not string compare
  and not thousands of `dpkg --compare-versions` shell-outs.

---

## Commit history shape (recent, most-relevant last)

emerge core (apt wrapper) → dpkg-only backend → unified single file w/ backend
select → `@world`/sets + source builds → streaming fix → dispatch-conf 3-way
merge → dispatch-conf archive-timing fix → `--no-dep-upgrade` (target-only) →
corrected to whole-closure w/ installed-pins → shared `ndu_solve` for both
backends + `_AptIndex` → `_dep_ok`/provides_of callable fix → same-upstream
escape hatch (`--with` + interactive + session flag) → world-file atomic write
→ live session-critical detection applied to all `-a/-p` merges.
