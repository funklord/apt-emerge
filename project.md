# emerge-for-debian — project notes

A Gentoo Portage–flavoured package manager for Debian/Ubuntu, implemented as a
single stdlib-only Python 3 script. It speaks emerge's CLI dialect and paints
Portage-style output, but drives real Debian tooling underneath.

The shipped artifact is one file, `emerge`. Everything else is development
scaffolding or packaging; only `emerge`, its man page and the docs are
installed.

| file | |
|---|---|
| `emerge` | the whole program; the only code that ships |
| `emerge.1` | man page, hand-written, cross-checked against `--help` |
| `test_emerge.py` | unit tests |
| `test_integration.py` | end-to-end against throwaway roots, real tools |
| `Makefile` | `check`, `style`, `install`, `deb`, `clean`; owns the install list |
| `debian/` | native package; `debian/rules` defers to the Makefile |
| `README.md` | user-facing front page |
| `code-style.md` | copy of the global style source, plus this project's own |
| `tools/style_gate.py` | the shared indentation gate, copied verbatim |
| `.style-gate.toml` | its scope here; load-bearing, see `code-style.md` |
| `LICENSE` | GPL-2, verbatim from `/usr/share/common-licenses/GPL-2` |
| `.github/workflows/tests.yml` | CI: interpreters, Debian, package, stdlib-only |
| `project.md` | this file |

No line counts here on purpose — every one written down has gone stale,
twice within a single session. `wc -l` is right there.

Both test files load `emerge` **by path** rather than importing it, so the
single-file rule survives having tests at all.

**Licensed GPL-2.0-or-later** (owner's decision). Each source file carries
the standard notice plus an SPDX tag — that per-file notice, not `LICENSE`,
is what makes the "or later" grant effective. `AUTHOR`, `COPYRIGHT`,
`LICENCE` and `VERSION` are module constants driving `emerge -V`; the notice
at the top of the file is a comment and cannot share them, so a test reads
the header back and fails if the two drift apart.

Originally developed and tested chat-side without system access. It has since
been run against a real Debian 13 (trixie) desktop with `trixie-updates` and
`trixie-security` pockets, a live KDE Plasma Wayland session on sddm, and
`gpgv` present — which is what the remaining tasks had been waiting for.

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

**Code style lives in `code-style.md`**, not here: `snake_case`, tabs for
indentation with spaces for alignment, lowercase filenames — and, importantly,
**no autoformatter may be run on this tree**, because `black` and `ruff format`
rewrite tabs to spaces unconditionally and would silently revert the
conversion. Read that file before editing; do not work from memory of it.

## Hard design rules (don't regress these)

1. **Single file, stdlib only.** The deploy story for apt-less boxes is one
   `scp`. If we ever split into a project dir (see "Open decisions"), the
   *shipped artifact* must still be a single amalgamated file.
2. **dpkg backend is binary-only.** Source builds (`-b`/`-B`) are apt-only by
   design: a box big enough for `build-essential` can afford real apt. The dpkg
   backend refuses `-b/-B` with a pointer to the host-build workflow.
3. **No persistent version pins, ever.** `--no-dep-upgrade` version selections
   are `pkg=version` arguments to a single `apt-get`/`dpkg` invocation — never
   `apt-mark hold`, `/etc/apt/preferences`, or `dpkg --set-selections`. A crash
   can therefore leave nothing pinned behind. This part is absolute.

   The rule used to end "the only `apt-mark` call anywhere is `showmanual`",
   which was too broad and was relaxed deliberately. `apt-mark auto|manual` is
   not a pin: it records *set membership*, and on the apt backend `@selected`
   **is** `apt-mark showmanual`, so writing that flag is the only way to
   express world membership at all. `apt-get install` already writes a manual
   mark on every install; `--oneshot` needs the symmetric call to undo it.
   Crash-safety is unaffected — an interrupted run leaves the package marked
   manual, i.e. in `@world`, which is the default outcome anyway.
   **Still forbidden: `apt-mark hold`/`unhold` and anything else that pins a
   version.**
4. **Respond in Portage's dialect.** Output format (`[ebuild N/U/R/D]`, `>>>
   Emerging`, the unmerge block, `--help` layout) mimics real emerge. New
   features should match that voice.

---

## File layout (single file, top to bottom)

Line numbers below are from the ~2619-line version and are now roughly 1,000
lines out (the file is ~3600 lines). Treat the *order* as the map and re-grep
for anything specific — the sequence has not changed, only the offsets.

- **Imports + path constants** (~11–39): stdlib only; `lzma` is optional
  (minimal builds strip it — code falls back to `.gz`/plain). Paths:
  - `LIB_DIR=/var/lib/emerge-dpkg`, `TREE_DIR=$LIB_DIR/tree`,
    `WORLD=$LIB_DIR/world`
  - `DISTFILES=/var/cache/emerge-dpkg/distfiles`,
    `BINPKGS=/var/cache/emerge-dpkg/binpkgs` (PKGDIR for `-b/-B`),
    `PORTAGE_TMPDIR=/var/tmp/portage` (where `-b/-B` unpack and build)
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

**The guarantee is enforced on apt's plan, not just the solver's.** On the apt
backend `resolve()` runs the solver, turns its choices into `pkg=version` pins,
and then *re-simulates through `apt-get -s`* — and it is apt's plan that gets
executed. Pinning the packages the solver chose does not stop apt from
upgrading installed ones it was never told about, so `_wall_from_merges()` is
applied to apt's simulation as well. Do not remove that second check: without
it the flag silently did nothing on lockstep stacks (see below).

**Virtual dependencies:** provider substitution triggers on "this name has no
versions of its own", NOT on `index.has()`. `has()` is *true* for a virtual
name precisely because something provides it, so testing it never fires and the
virtual name lands on the stack as if real. Also note `_AptIndex.provides_of()`
leaves an empty list behind as a "already probed" marker — `has()` must test
`bool(self._provides.get(name))`, not key presence.

**Search:** real backtracking, iterative with an explicit decision stack (an
`@world` closure is far deeper than Python's recursion limit, so recursion is
not an option). Each decision keeps the state that preceded it; exhausting one
package's versions resumes the previous decision at its next candidate. This
replaced a greedy solver that committed permanently to each choice and only
stepped the package in front of it — it reported false walls on any graph that
only resolves with an older version of an *earlier* dependency, and the
give-away was a wall naming a package with installed version `?` (i.e. one that
is not installed at all).

**Two passes, escalating on demand** (`ndu_search`). The first takes the
first alternative of every `a | b`, which is what apt and dpkg do and what
almost every graph wants. Only if that reports a wall or runs out of budget
does it run again with alternatives branched on as decisions of their own.
So the common case never pays for a search it did not need, and a wall the
user is shown has already survived an exhaustive pass.

- The retry is **skipped when the closure contained no real choice** — `a |
  b` where both resolve to the same package is not a decision, and retrying
  would only double the time before reporting the same wall. `ndu_solve`
  records this and tags the exception with `had_alternatives`.
- If the second pass fails too, the **first** failure is raised: it names the
  blocker the user can actually act on.
- `--backtrack=N` (Portage's spelling) multiplies the step budget for the
  retry; `--backtrack=0` disables it. Default 10.
- Budget exhaustion raises `NduIncomplete`, a `RuntimeError` subclass so old
  handlers still work, but distinguishable so the caller can try harder. Do
  **not** turn it into an `NduWall`: giving up early is not proof that no
  resolution exists.

**Still not complete:** this branches over versions and alternatives, which
covers the cases seen in practice, but it is not a SAT solver and makes no
completeness guarantee beyond the step budget.

**Two semantics worth knowing before editing:**
- A pinned (installed) package is represented by a synthetic stanza with no
  `Depends`, so it contributes nothing further to the closure. Deliberate: it
  is installed, so its deps are already satisfied, and walking them would make
  `@world` re-derive the entire installed tree.
- Consequently a constraint can only reach an installed package from a
  not-installed dependent, which `forces_installed_upgrade()` catches first.
  The `satisfies()` test on the pin in `candidates()` is a backstop — verified
  by instrumentation to fire zero times across the libsdl3-dev wall, the
  full-allow-set resolve and `@world`. Keep it, but don't expect a test to
  reach it.

**Perf:** faster than the greedy version it replaced, because each decision is
made once instead of being repeatedly rejected and restarted. On this box:
libsdl3-dev wall 4.6s (was 7.7s), `@world` 36s (was 47s).

### Validated against the live trixie tree (the libsdl3-dev case)

`libsdl3-dev` is not installed on trixie and pulls the Mesa stack from
`25.0.7-2` to `25.0.7-2+deb13u1` — 9 upgrades, 7 of them session-critical. This
reproduces the originally reported failure exactly, and running it turned up
three bugs, all now fixed and regression-tested:

1. **apt's extra upgrades went unchecked** (the serious one). The
   `no_dep_upgrade` branch of `AptBackend.resolve()` did not return; it fell
   through to the apt simulation, which rebuilt the merge list from apt's
   answer and overwrote the solver's. `--no-dep-upgrade --with libgbm1,
   mesa-libgallium libsdl3-dev` therefore produced *exactly* the no-flag plan:
   9 upgrades, none of them permitted, no warning.
2. **Virtual packages became inescapable walls.** `gir1.2-gio-2.0-dev` (a pure
   virtual provided by `gir1.2-glib-2.0-dev`) was reported as an installed
   package that had to move. It is not installed, so `--with` could never
   release it — the wall repeated forever.
3. **The suggested `--with` dropped earlier grants**, so following the hint
   bounced you back to the previous wall. `_with_arg()` now merges the
   accumulated allow-set in.

Current behaviour: the wall reports the whole Mesa block at once with the full
`--with` line, and converges in two steps. Lockstep stacks genuinely move
together — that is the archive's doing, not a solver defect.

---

## Session-critical detection (newest work)

**Purpose:** warn that upgrading a package could restart X/Wayland and close
running GUI apps — the one class where "same-upstream, harmless" is false
(Mesa/compositor rebuilds), and where an "exclude-reboot" filter wouldn't help
(no reboot involved).

**Mechanism (derived, not a hardcoded list):**
1. `_find_session_leaders()` — scan `/proc/*/comm` for names in
   `_SESSION_LEADER_COMMS` (Xorg/Xwayland, gnome-shell/kwin/sway/weston/
   plasmashell/mutter/... , gdm/sddm/lightdm/greetd/...). Returns
   `(pid, comm)` pairs, or **None** if `/proc` could not be scanned at all —
   "we cannot tell" is a different answer from "nothing is running".
2. `_proc_mapped_code(pid, comm)` — the session's *code*: `/proc/PID/exe` plus
   every mapping from `/proc/PID/maps` that carries the execute bit or is named
   `.so`. Data mappings (fonts, icon caches) are skipped.
3. `compute_session_critical_packages()` — one **batched** `dpkg-query -S` maps
   those paths → packages (batched is ~8x faster than per-file). Cached per
   process.
4. `is_session_critical(name)` — the live set, with the static
   `_SESSION_CRITICAL_EXACT`/`_PREFIX` sets as a **floor underneath it**
   whenever a session exists. No session and we could look → nothing is
   critical. Could not look (`_session_blind`) → static set only.

**Two permission traps this had to be fixed for** (both real, both found on the
trixie desktop — don't reintroduce them):
- **Collect the executable, not just libraries.** Matching only `.so` meant the
  packages shipping the running compositor, X server and display manager were
  never flagged: `kwin-wayland`, `xwayland`, `sddm`, `xserver-xorg-core` all
  came back false. `plasma-workspace` was flagged only by accident, because it
  happens to ship a library `plasmashell` maps.
- **Hardened leaders are opaque to non-root.** A setcap'd compositor runs with
  `dumpable=0`, so `/proc/PID/{maps,exe}` is root-only — as a normal user,
  `kwin_wayland`, `Xorg` and `sddm` (three of five leaders here) contribute
  nothing, and `emerge -p` is usually run unprivileged. When `exe` is
  unreadable the leader's `comm` is resolved on `PATH` instead, which still
  names the binary.

**Measured cost:** ~120ms once on this desktop (dominated by the single
`dpkg-query -S`), ~2ms and empty on headless. Cached to 0ms after. Independent
of package count → cheap enough that it runs on **every** `-a/-p/-v` merge list,
not just `--no-dep-upgrade` walls: session-in-use upgrades get an inline
`(session)` marker + a summary warning.

**Verified on the target desktop:** all five leaders (`sddm`, `Xorg`,
`kwin_wayland`, `Xwayland`, `plasmashell`) are matched with no `comm`
truncation problems, so the 15-char audit is clean for KDE/sddm at least. GNOME
and the greeter entries (`gdm-session-wor`, `lightdm-gtk-gre`) are still
unverified against a real session.

**Remaining gaps:**
- The maintained thing is `_SESSION_LEADER_COMMS` (process names), much
  shorter/slower-changing than a library list. An exotic compositor not in it
  falls back to the static floor.
- Only sees *currently mapped* code; a lib the session `dlopen`s on demand
  (some driver plugins) may be missed at scan time.
- **The derived set is broad and will cry wolf.** 428 packages here, because
  anything `plasmashell`/`kwin` has mapped counts — including `libacl1`,
  `libaom3`, `ark`. Upgrading those will not close anyone's session, so
  `(session)` overstates the risk on a KDE box. Left as-is deliberately: the
  claim "in use by your graphical session" is literally true, and narrowing it
  means guessing which libraries a restart actually depends on. Worth
  revisiting if the warning proves noisy in practice.

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

## Repository signature verification (dpkg backend)

The backend always checked each `.deb`'s SHA256 against the index; nothing
vouched for the *index*, so the chain was anchored in attacker-suppliable
bytes. `--sync` now verifies the archive signature over Release, then checks
each downloaded index against the hashes inside that verified Release.

- **`gpgv` is the only new dependency** — one verify-only binary from gnupg,
  not apt, so an apt-less box can still have it. Stdlib-only still holds.
- **`dearmor()`** unwraps armoured `.asc` keyrings with `base64` from the
  stdlib. Necessary because gpgv reads *binary* keyrings only and Debian ships
  its archive keys armoured; shelling to `gpg(1)` to convert would pull in the
  exact dependency this backend exists to avoid.
- **Keys are merged into one keyring per source.** A Debian InRelease carries
  several signatures and gpgv fails the whole file if it cannot check even one,
  so passing keyrings one at a time does not work — this was measured, not
  assumed.
- **Trust follows apt's model**, not "any key in the store will do":
  `[signed-by=...]` on one-line entries and `Signed-By:` in deb822 stanzas both
  pin the keyring for that source (inline key material supported).
  `read_sources()` therefore returns **4-tuples** now — `(base, suite, comps,
  signed_by)`.
- InRelease preferred, falling back to detached `Release` + `Release.gpg`. The
  clearsigned payload is extracted with `clearsigned_payload()`, covering only
  the region gpgv verified. Getting that extraction wrong can only cause a hash
  mismatch, never a silent pass — it is fail-safe by construction.
- **Failure is fatal; inability to check is not.** No gpgv, no keys, or no
  Release at all (a USB-stick repo has none) warn and continue, so enabling
  this cannot break a working setup. `--no-verify` skips it.
- Release is fetched and verified *before* the progress line is opened, so
  warnings and failure reports don't land mid-line.

Verified against the live archive: real InRelease and detached Release both
verify; the extracted payload is byte-identical to the detached Release; a
tampered index, a forged InRelease, and a source pinned to the wrong keyring
are all refused.

## Crash-safety

- No persistent pins (see hard rule 3). Interrupted installs recover with the
  normal `dpkg --configure -a` / `apt-get -f install`.
- World file and config writes are atomic (temp + fsync + `os.replace`).
- `--with` allow-set is per-invocation, never persisted.

---

## What needed a real box, and what only looked like it did

This began as a list of things deferred until hardware was available. Most
of them turned out not to need any, which is the lesson worth keeping:
**before deferring something as hardware-only, work out what the program
actually distinguishes.** "apt-less" is `shutil.which("apt-get")` returning
None. A conffile upgrade is two `.debs` and a throwaway root. Running as
root is a container. Three items came off this list that way, and one of
those found four live defects on the day it was tried.

What is left genuinely is the hardware: a box that really has no apt, and a
GNOME session. The record:

- ~~**Real multi-version repos.**~~ Done: validated against a live trixie
  mirror with `-updates`/`-security` pockets. The `libsdl3-dev` deb13u1 wall
  reproduces exactly, and doing so found three bugs (see that section).
- ~~**A graphical session.**~~ Done on KDE Plasma Wayland + sddm; found and
  fixed the two permission traps in session detection. GNOME/gdm still unseen.
- **apt-less embedded box** — mostly closed, and not by finding hardware. See
  backlog item 7: `pick_backend` decides on `shutil.which("apt-get")`, so
  hiding it is the whole of "apt-less" from the program's side, and a local
  HTTP server covers sync over http. What is left is confidence rather than
  coverage: nobody has run it on a machine that genuinely lacks apt.
- ~~**Config merging** against real package upgrades that ship conffile
  changes.~~ Done — and it did not need a real box after all, which is worth
  remembering before deferring something else as hardware-only. Two `.debs`
  built with a `DEBIAN/conffiles` entry, installed into a throwaway root with
  the exact flags `merge()` uses, is a real conffile upgrade in every respect
  dpkg cares about. `ConfigMergingEndToEnd` proves the assumption the whole
  feature rests on (`--force-confold` parks an edited conffile as
  `.dpkg-dist`, and silently replaces an untouched one) and then runs the
  round trip on top of it: archive, edit, upgrade, 3-way merge against the
  archived ancestor.
- **Anything requiring a real install** on *this* box. Everything validated so
  far has been read-only (`-p`) or written to a throwaway root. That is a
  deliberate limit, not a gap to close: the throwaway root exercises `merge`,
  `unmerge`, `dispatch_conf`'s inputs, signature verification and the
  conffile round trip against the real tools, and the remaining difference —
  running as root against live system state — is the one thing worth *not*
  testing on a machine somebody uses.

Handy synthetic-test pattern used so far: write a `Packages` stanza file into
`/var/lib/emerge-dpkg/tree/`, `dpkg -i` a hand-built `.deb` to simulate an
installed base, then run `--backend=dpkg -p ...`. For apt-path unit tests,
monkeypatch the module-global `capture` to return canned `apt-cache show`
output and call `ndu_solve` directly.

Always `python3 -m py_compile emerge` after edits.

## The test suite

`make check` runs everything, stdlib only — a few hundred unit tests and a
few dozen end-to-end ones. (No count here: the last one written down went
stale inside a single session.) The unit half is a few seconds. The integration half is dominated by one
test that drives a real `apt-get`, and its wall time swings hard with system
load — measured between 14s and 115s for the same suite on the same machine,
so treat a slow run as load rather than a hang. `make check-unit` is the fast
loop.

**Every integration class skips itself when the tool it drives is missing**,
which is what lets the suite run off a Debian box — and is also how a whole
capability goes untested while the run still reports `OK`, because a skip is
as green as a pass. `EMERGE_TESTS_REQUIRE_ALL=1` turns a missing capability
into one failure naming it, and the `debian` CI job sets it because that job
is the one meant to cover everything.

It found a real gap the moment it existed: **CI installed `gpgv` but not
`gpg`**, so the eleven tests covering the dpkg backend's entire trust anchor
had never run there once. They were written, they passed locally, and CI
reported green without executing any of them.

**Every module carries a `tearDownModule` sentinel** that fails the run if it
left `os`, `shutil` or `subprocess` patched. `load()` gives each test a fresh
copy of the *script*, but those modules are the same objects the test process
uses, so a missing cleanup corrupts whatever runs next — in another file,
with no clue where it came from. Three such leaks have happened; the sentinel
names the culprit in the run that caused it, and costs nothing.

`test_integration.py` is the end-to-end half: real `.debs` built with
`dpkg-deb`, a real `file://` repository, real installs, and the real resolver
deciding what to do. It is the **only** place `merge`, `unmerge`, `depclean`,
the download/SHA256 path and `--oneshot` actually execute — everything else
uses fakes. It covers *both* backends:

- **dpkg** — `dpkg --root=<scratch> --force-not-root`.
- **apt** — every piece of apt state redirected (`Dir::State::status`,
  `::lists`, `::extended_states`, `Dir::Cache`, `Dir::Log`, `Dir::Etc::*`)
  plus `DPkg::Options::=--root=...`, so a real `apt-get install` runs
  unprivileged into a scratch tree. That is how `--oneshot` was finally
  verified end to end, including that it does not evict a package already in
  `@world`. It needs no privileges: `dpkg --root=<scratch> --force-not-root`
unpacks into a tree you own, and every path constant in the module is
repointed under a temp directory. The file skips itself if that does not
work. Two things it gets right that are easy to get wrong when extending it:
`/usr/sbin` must be on `PATH` for the *process* (emerge spawns dpkg itself
and inherits the ambient environment), and it covers the corrupted-`.deb`
path, which is the entire trust chain on that backend.

Beyond the two backends, `test_integration.py` also covers:

- **Signature verification** against real `gpg` and real `gpgv` — a generated
  key, a real signature over a real `Release`, and the assertion that a good
  one *was checked* rather than merely that the sync succeeded. An empty
  keyring does not fail; it warns and proceeds unverified, which is the
  direction worth pinning.
- **Config merging** against real dpkg, premise first: that
  `--force-confold` parks an edited conffile as `.dpkg-dist` and silently
  replaces an untouched one. If that were false the whole feature would be
  dead code and every unit test would still pass.
- **Source builds (`-b` / `-B`)** against a real source package. `build()`
  had never executed and `resolve_source`/`_src_version`/`_build_use` had no
  test at all — the same shape as dispatch-conf before its premise was
  checked. The fixture makes a minimal *native* source package with a
  hand-written `debian/rules` that produces its `.deb` with `dpkg-deb`
  directly: no debhelper, which keeps the build near a second and removes a
  dependency the suite would otherwise need. `apt-get build-dep` runs for
  real, satisfied by `Build-Depends: dpkg-dev`.
- **Apt-less operation over real HTTP** — a stdlib server on 127.0.0.1, a
  compressed index (the `.gz`/`.xz` path no `file://` test ever ran), and
  `shutil.which` hiding `apt-get` so `pick_backend` has to choose dpkg on its
  own. This class drives `main()` rather than backend methods, so argument
  parsing → backend selection → resolve → fetch → SHA256 → install → world
  file finally runs as one path.

`python3 -m unittest test_emerge` — the unit half, stdlib only. Covers
`vercmp`, `meets`, `parse_depends`, `parse_stanzas`, `merge3`, `_significant`,
`_dep_ok`, `ndu_solve`, `_wall_from_merges`, `_with_arg`, `_AptIndex.has`,
`_policy_batch`, `stream_apt`, `run_mergetool`, `_write`, the apt backend's
`unmerge_candidates` and `merge` aftermath, `print_unmerge_list`, the
signature-verification code, and session detection.

Three suites are **differential or property-based** rather than hand-written
expectations, because a hand-authored case encodes what its author believed
the reference does — so when the author misreads it, the code and the test
are wrong together and agree forever. Keep them that way:

- `ndu_solve` is checked against **brute force** on random package graphs
  small enough to enumerate. Three invariants: a returned plan never moves an
  installed package, it satisfies every dependency it pulls in, and a wall is
  raised only where exhaustive search agrees there is none. The false wall is
  a bug this project has already shipped once — the old greedy solver
  reported walls that did not exist and no example-based test noticed.

- `vercmp` is Debian policy 5.6.12 in Python, so every pair in the table is
  also run through `dpkg --compare-versions` and must agree — plus a seeded
  fuzz over generated versions, because the table encodes what its author
  believed policy says and dpkg encodes what it is. 4,000 random pairs
  during development, no disagreement; 120 per run in the suite.
- `merge3` implements `diff3 -m`'s **rules**, and those four are
  property-tested on random inputs: one-sided change taken from that side,
  identical change taken once, unchanged region kept, real divergence
  conflicted. 80,000 random triples, no violation.

  It is **not** byte-identical to `diff3 -m`, and the docstring used to say
  it was. That gap was investigated properly rather than left as a caveat,
  and the conclusion is that **closing it is not worth wanting**:

  - On config-shaped input, 96% identical. In *every* divergent case the two
    underlying 2-way diffs were identical to GNU diff's, so the alignment is
    not the cause and a better diff cannot fix it. Two rewrites were tried
    and both produced byte-identical output to what is there now: computing
    stable regions as diff3 does (the complement of the two change sets),
    and replacing difflib with a minimal Myers diff. Neither changed a
    single merge out of 12,000.
  - Classifying the 85 divergent merges out of 2,000: **57** are diff3
    emitting a conflict with no ancestor section at all for a change both
    sides made identically, **8** are diff3 conflicting although mine and
    theirs are the same text, and **20** are both conflicting over
    differently chosen but equally valid regions.

  So three quarters of the difference is diff3 producing the worse answer.
  The minimal case is `base=[a, c]`, `mine=[Y]`, `theirs=[Y]`: both sides
  made the same change, the answer is plainly `Y`, and `diff3 -m` reports a
  conflict. Matching diff3 byte-for-byte would mean reproducing that.
  **The four rules are the contract; diff3 is a reference for them, not an
  oracle.** There are tests pinning the cases where the two disagree and we
  are right.

  What did come out of the investigation: `_sync_regions` now states diff3's
  stable-region model directly instead of intersecting matching blocks and
  walking the result greedily. The greedy step carried a guard that could
  never fire — matching blocks are already ordered in both their sequences,
  so the intersections are too — and a correctness check that cannot fail is
  worse than none. Identical output, measured; same speed at 600, 2,000 and
  5,000 lines.

  The hand-written clean-merge cases are still compared byte-for-byte
  against real `diff3`, because on unambiguous input the two do agree and
  that is worth holding.

Both skip themselves if the tool is missing, so the suite runs off a Debian box.

## The style gate

`make style` runs `tools/style_gate.py`, the indentation and whitespace
checker shared verbatim across the private projects (source:
`~/.claude/tools/style_gate.py` — fix drift, do not edit the copy). It was
adopted here after the tab conversion, which until then had nothing
mechanical holding it: `code-style.md` said indentation was a review item.
The tree passed on the first run, so nothing needed fixing — what needed
doing was making the check exist and cover the right files.

**The scope is the part that goes wrong, and it did.** The gate selects
files by suffix or by exact name, and `emerge` has no suffix — it is a
command, not a module. Out of the box it therefore checked six files and
reported them clean while never opening the program. `.style-gate.toml`
names it (and `debian/rules`, which is Makefile syntax under a name dpkg
chose), taking the list to ten.

Two things about that config are worth knowing before trusting the gate:

- **The tool's collapse floor cannot protect it.** Dropping `emerge` from
  `indent_names` takes the list from 10 to 9, which is not a collapse — and
  the floor is configured in the file that stopped being read anyway. So the
  scope is pinned by `TestStyleGate` instead, which asserts that `emerge`,
  `Makefile`, `debian/rules` and both test modules are in the gate's list.
- **`tomllib` is 3.11+, and an older interpreter does not fail.** It prints
  one line to stderr and then checks a *smaller* set of files successfully:
  measured at `8 files conform`, exit 0, with `emerge` and `debian/rules`
  silently outside it. `make style` refuses to run on such an interpreter,
  and the CI job pins 3.13 rather than trusting the runner's default.
- **The gate has two discovery paths and they must be made to agree.** Git
  is preferred and drops ignored files on its own; with no `.git` it falls
  back to a plain walk, which is exactly how the container recipe below runs
  it. That walk found twelve files rather than ten — pytest's cache README
  and `.claude/settings.local.json`, neither of them this project's content.
  Both conformed, so the gate passed on luck; a generated file that did not
  would have failed CI with no defect behind it. The `exclude` list closes
  it, and the two paths now list the same ten files.

Mutation-tested rather than assumed, all five confirmed by running them: a
4-space-indented function appended to `emerge` is caught at the line
(`indented 0 tab(s), structure says 1`), so is one in `test_emerge.py`, so
is a space-indented line inside an existing block (as a `TabError`), so is
trailing whitespace in `project.md` — and dropping `emerge` from the config
is **not**, which is why the test above exists. Note also that
`make style PYTHON=/bin/false` fails whether or not the guard is there, so
the test asserting the guard checks the *message*, not the exit status.

## Packaging

`make deb` builds `$(BUILD_DIR)/apt-emerge_<version>_all.deb`, where
`BUILD_DIR` defaults to `dist/` and is settable so an isolated build cannot
clobber a plain one. The variable was `BUILD_DIR` until 2026-08-05; `BUILD_DIR`
is the canonical spelling across these projects, and the two are not
synonyms in any case — this holds a build tree of `.deb`, `.changes` and
`.buildinfo`, and nothing here compiles to an object file at all. Source format is **`3.0 (native)`**: this repo *is* upstream, there
is no separate tarball, and a quilt package would mean inventing an
upstream/packaging split that does not exist.

`clean` removes the files it names and no others — no wildcard sweeps, no
`rm -rf` of a bare variable. The directories it does remove whole are ones the
build created (`$(BUILD_DIR)`, `debian/apt-emerge`, `debian/.debhelper`), and
each is rejected if it is absolute or contains `..`, so `make clean BUILD_DIR=/`
is not
a working command. This is a safety property rather than a style choice:
`clean` is the one target everybody runs without reading it.

Layout decisions worth not re-litigating:

- **The `Makefile` owns the file list**, and `debian/rules` reaches it through
  `dh_auto_install`. A `debian/install` file would be a second list that
  drifts from the first. `debian/rules` is otherwise plain `dh` with
  `dh_auto_build` stubbed out — there is nothing to compile.
- **Ships `/usr/bin/emerge`** plus `dispatch-conf` and `etc-update` symlinks,
  because `argv[0]` selects the action (see the `__main__` block). A name the
  script answers to but the package does not install is a feature that only
  exists for people who symlink by hand. `TestPackaging` asserts the two lists
  agree.
- **Not `/usr/local`.** That belongs to the local admin and dpkg must not own
  anything there. The manual install in the README still uses it, correctly —
  that is the un-packaged path.
- **`Rules-Requires-Root: no`**, so the build needs no fakeroot.
- **Version.** `debian/changelog` carries `3.0.66`; the script reports
  `3.0.66-deb`, the `-deb` marking it as the Debian reimplementation rather
  than real Portage. These are two hand-written strings, so both `make
  version-check` (wired into `dh_auto_test`) and a unit test assert they stay
  in step. Bump both together.
- **`emerge.1`** is hand-written, and `TestPackaging` checks it against
  `--help` in *both* directions: every option documented in `--help` must have
  its own `.TP` entry, and every option the man page gives an entry must exist
  in the script. The first check deliberately does not do a substring search
  over the whole page — options cross-reference each other constantly, so
  "the string is present" passes for an option with no entry at all.

The **`package` CI job** does the part that matters: building a `.deb` proves
very little, since one full of wrong paths builds perfectly. So it installs
the result with `apt-get`, runs `emerge --version`, `--help` and `-pv bash`,
checks `argv[0]` dispatch through the installed symlinks, renders the man
pages, and removes the package again. `lintian` runs informationally
(`|| true`) — it has never been run against this package by the author, so
treat its first output as a to-do list rather than a regression.

## Running CI locally

The `debian` and `package` jobs can be run for real, and should be before
touching either:

```sh
docker run --rm -v "$PWD":/src:ro debian:trixie bash -c '
  apt-get update -qq
  apt-get install -y --no-install-recommends python3 dpkg-dev \
    build-essential diffutils gpgv gnupg ca-certificates
  cp -r /src /work && cd /work && rm -rf .git dist debian/apt-emerge
  python3 -m unittest test_emerge
  EMERGE_TESTS_REQUIRE_ALL=1 python3 -m unittest test_integration'
```

**This is worth the trouble, because the container runs as root and a
workstation does not.** Doing it for the first time found four defects that
every local run had reported green:

- the world file was seeded at the wrong moment, so the first install pulled
  its own dependencies into `@world` (see `_seed_world` — the guard returns
  early for non-root, which is why nothing local saw it);
- the unit suite wrote a real `/var/lib/emerge-dpkg/world`, 135 entries, on
  the machine running the tests;
- `SourceBuildEndToEnd` needs `build-essential`, which `apt-get build-dep`
  pulls in implicitly whatever the source package asks for. A developer box
  has it; the container did not, and the failure reads as a resolver bug;
- `emerge --help | head` printed a `BrokenPipeError` and exited 120.

## CI

`.github/workflows/tests.yml`, on every push and pull request (the remote
carried both `main` and `master` at one point, so it is not branch-pinned).
Free on GitHub for a suite this size. Five jobs:

- **`unit`** — both suites on Python 3.9, 3.11 and 3.13, plus a run under
  `-W error::ResourceWarning`, because a leaked file handle otherwise passes
  by accident of CPython refcounting. That is exactly how five leaks in
  `emerge` went unnoticed until CI ran.
- **`debian`** — the same suites in a `debian:trixie` container. This runs as
  **root**, which is the one condition not reproducible on a normal
  workstation and the only reason a test reading real `/proc` was ever
  caught. Its unit and integration steps are deliberately separate so a
  failure says which half broke without needing the logs — job logs need repo
  admin rights, which a helper may not have.
- **`style`** — `make style`, on a pinned 3.13. See "The style gate" below
  for why the interpreter is pinned rather than left to the runner.
- **`stdlib-only`** — walks the AST for imports outside
  `sys.stdlib_module_names` and fails if a non-test `.py` appears beside
  `emerge`. This guards hard rule 1. `tools/` is a subdirectory and so does
  not trip it, correctly: the gate is development scaffolding and is not
  installed.

**The first CI run failed three times, and all three were test defects, not
product bugs** — every one invisible on the development machine. See the
testing section: they are the reason the local-matrix recipe and
`TestPortability` exist.

**Run the CI matrix locally before pushing.** `uv` fetches real
interpreters in seconds, which is the only honest way to check portability:

```sh
uv python install 3.9 3.11
for v in 3.9 3.11 3.13; do
    "$(uv python find $v)" -m unittest test_emerge test_integration
done
LC_ALL=C python3 -m unittest test_emerge test_integration   # C locale
python3 -W error::ResourceWarning -m unittest test_emerge test_integration
```

That covers everything CI does except the `debian:trixie` container, which
runs as **root** — the one condition not reproducible here, and the one that
caught a test reading real `/proc`.

**Tests must not depend on the machine they run on.** Two escaped review
and were caught only by CI, both passing here for reasons that had nothing
to do with what they claimed to check:

- `test_unreadable_maps_still_yields_the_executable` never faked the maps
  read, so it read the real `/proc/1/maps`. That is unreadable to an
  ordinary user, so it passed on a desktop and on GitHub's runners, and
  failed in a container where everything is root and pid 1 is readable. It
  now fakes the read *and* uses its own pid, so dropping the fake fails
  everywhere rather than only where /proc happens to be off limits.
- A stub used `staticmethod(lambda ...)` on an instance attribute, which is
  callable only on 3.10+.

For portability, run a real old interpreter rather than reasoning about one:
`uv python install 3.9` takes seconds. `ast.parse(feature_version=...)` is
*not* a substitute — it does not reject PEP 701 syntax, which is how a
3.12-only f-string reached CI.

**Test the tests by mutation, not by watching them pass.** Everything here was
checked by deliberately breaking `emerge` and confirming the suite fails —
that is how three tests that never actually exercised their target were found
(the fakes were too kind: `FakeIndex.has()` returned False for virtual names,
where the real `_AptIndex.has()` returns True, which is precisely the bug).
Copy `emerge` + `test_emerge.py` to a scratch dir, apply a one-line breakage,
run the suite, restore.

Useful patterns already in the file: patch `mod.open` for a fake `/proc` or
`/etc/apt`; patch `mod.capture` / `mod.fetch` for canned tool output; force
`_session_critical_cache` and `_session_blind` to pin session state; stub
`Verifier._gpgv` to test the decisions around gpgv without invoking it.

**Exercising apt's write paths without root.** apt honours `-o` config
overrides, so its state files can be pointed at throwaway copies:

```sh
cp /var/lib/apt/extended_states /tmp/es
apt-mark -o Dir::State::extended_states=/tmp/es auto tree   # no root needed
```

Wrap `mod.capture` to inject that `-o` into any `apt-mark` call and the real
`_apply_oneshot`/`_manual_set` run against the real binary with zero system
impact. That is how `--oneshot` was verified in both directions (a new atom
demoted to auto, one already in `@world` left alone) — better coverage than
a real install, which could only have shown the first case. The same trick
should work for `Dir::State::status` and `Dir::Cache` if more of the apt
write surface ever needs testing.

---

## Open decisions / backlog

1. **Project split.** Whether to break the single file into a `src/` module tree
   + `Makefile` (build → amalgamated single file via a concat tool à la sqlite)
   + `debian/` dir. Skip `zipapp` — a `.pyz` isn't `vi`-able on an embedded box,
   which defeats the single-file purpose.

   **Half of this is now done.** The owner chose to keep one file, so there is
   no `src/` tree and no amalgamation step, but the `Makefile` and `debian/`
   dir exist — see **Packaging** below. What is still open is only the module
   split, and there is no pressure for it: at ~3,700 lines the file is
   navigable and the layout map above is enough.
2. ~~A real test suite~~ — **done**, and so is everything this entry listed as
   next: `make check` (plus `check-unit` / `check-integration`) and four CI
   jobs, including one that builds the package, installs it and runs the
   installed binary. See **Packaging** and **CI**.
3. ~~`ndu_solve` completeness~~ — the greedy false wall is **fixed** (real
   backtracking), and so is the alternatives half: `ndu_search` escalates to
   an exhaustive pass that branches on `a | b` before a wall is believed, so
   a wall the user is shown has survived that pass. What remains open is
   narrower still:
   - the *cheap* pass is first-match, deliberately — that is what makes the
     common case free, and the escalation is what makes it safe;
   - the step budget bounds the search, so `NduIncomplete` means "not
     proven", never "no solution exists". `--backtrack=N` raises it.

   Neither has been observed to bite in practice.
4. ~~GPG verification~~ — **done**, see the verification section.
5. **`_SESSION_LEADER_COMMS` truncation audit** — done properly now, and it
   found a live bug rather than confirming the list. `/proc/PID/comm` is
   capped at 15 characters, and SDDM 0.21 renamed its greeter binary to
   `sddm-greeter-qt6` — 16 — so on Debian trixie it reports
   `sddm-greeter-qt` and the entry `sddm-greeter` matched nothing. Both
   spellings are now listed, and `TestSessionLeaderComms` enforces the
   invariant in both directions: no entry may exceed 15 characters (it could
   never match), and the known long binaries must appear truncated. The
   15-character limit is measured against the running kernel rather than
   hardcoded from memory.

   **Still unverified: GNOME/gdm.** Deliberately not guessed at — the whole
   point of the audit is that a wrong entry fails silently, and adding
   plausible names without a box to check them on is how the sddm entry got
   there. `gnome-shell` and `gdm-session-wor` are the ones that matter and
   both are present; what is unchecked is whether GNOME has other leaders
   worth naming.
6. ~~Session-critical noise~~ — **done**. A same-upstream bump is now reported
   as `(session rebuild)` with an einfo saying the session keeps running; only
   a real upstream change keeps `(session)` and the restart warning. The
   derived set is still broad, but breadth now costs a mild note rather than a
   false alarm. See "Session-critical detection".
7. **The dpkg backend on real apt-less hardware.** Deprioritised by the owner
   — rare case, mostly works. Both things this entry called
   hardware-specific have since been closed without any, which is the second
   time that has happened (see config merging in the testing notes):

   - *A box with no `apt-get`.* `pick_backend()` decides by asking
     `shutil.which`, so hiding `apt-get` from it **is** apt-less as far as
     the program is concerned. `AptlessHttpEndToEnd` does exactly that and
     asserts the dpkg backend is chosen.
   - *Sync over real http.* A stdlib `ThreadingHTTPServer` on 127.0.0.1 is
     real HTTP without a network, and it caught a path nothing else ran:
     every other test serves `file://`, so **the gzip/xz decompression in
     `sync()` had never executed** even though it is what every real archive
     serves.

   That class also drives `main()` rather than the backend methods, so
   argument parsing → backend selection → resolve → HTTP fetch → SHA256 →
   install → world file finally runs as one path, including `-p`, `-1` and
   `-C`.

   What genuinely remains is only this: nobody has run it on a machine that
   has no `apt-get` *installed*, where the difference is the box, not the
   program. That is worth doing once for confidence and is not blocking
   anything.
8. ~~`-1`/`--oneshot` on the apt backend~~ — **done**. The owner relaxed hard
   rule 3 (see it: the version-pin ban is intact, only the blanket `apt-mark`
   ban was too broad). `--oneshot` now marks the newly-added atoms back to
   auto after the install, never touching one that was already in `@world`.
   Verified end to end in `test_integration.py` against a real apt install.


9. **Packaging from language package managers (pip / npm / gem / cargo).**
   Not started; recorded because it addresses a live problem rather than a
   hypothetical one — modern Debian refuses `pip install` into the system
   (PEP 668 `externally-managed-environment`) precisely because of the
   conflict this would avoid.

   **Shape.** emerge orchestrates; it does not become a packager. Converters
   already exist and are packaged: `stdeb`/`pybuild` (Python), `gem2deb`,
   `npm2deb`, `debcargo`, and generic `fpm`. Fetch the upstream artefact,
   drive the converter, build with the `dpkg-buildpackage` machinery `-b/-B`
   already has, leave products in PKGDIR, install the `.deb`. That keeps hard
   rule 1 intact: they are external programs, exactly like dpkg-buildpackage
   is today.

   **Suggested first pass: Python only.** `stdeb` is mature, PEP 668 makes it
   the most-wanted, and it exercises the whole pipeline before anyone has to
   face npm's dependency graphs.

   **The hard parts, none of them optional:**
   - *Name mapping.* PyPI `PyYAML` is Debian `python3-yaml`. Either keep a
     mapping or vendor everything and accept the duplication.
   - *Version translation.* PEP 440 `1.0.0rc1` must become `1.0.0~rc1` or it
     sorts **above** the final release. `vercmp` already gets `~` right, so
     this is mechanical — but getting it wrong is silent.
   - *Collision with archive packages*, which is the problem being solved
     reappearing in new clothes. A self-built `python3-requests` shadowing
     Debian's is no better than pip's mess. Needs a namespace
     (`pypi-requests`) or strict Provides/Conflicts, plus the existing
     `+local1` bump so `@world` will not clobber it.
   - *Transitive closure size*, npm especially.
   - *apt backend only*, like `-b/-B`: it needs build tooling, and the dpkg
     backend is binary-only by design.
---

## Backend parity is the main source of bugs

The single most productive thing done so far was diffing the two backends'
shared surface (`resolve`, `merge`, `unmerge`, `depclean`, `sync`, `search`).
**Five defects in a row had the same shape: the dpkg backend did the right
thing and the apt backend did not** — and apt is what runs on every normal
Debian box, so the broken one is the one everybody uses.

(The later review below found the reverse direction too: the apt backend
delegates ordering to apt, so the dpkg backend is the one that has to get
`Pre-Depends` and removal order right by hand — and did not. The rule is
symmetric: **whichever backend implements something itself is the one
carrying the bug.**)

- `unmerge` listed only the names you typed while `apt-get remove` cascaded.
  `emerge -C libjpeg62-turbo` showed 1 package and would have removed 868.
- No `is_protected` check at all on apt, though `--help` promises one.
- A failed `apt-get -s` was ignored, so an impossible removal reported success.
- Failure output was filtered away, so `merge` said "see output above" with
  nothing above it and `unmerge` said nothing whatsoever. The dpkg backend has
  always printed stderr.
- `archive_settled`/`pending_notice` ran only on success in *both*, so a
  partial install silently left stale ancestors and unannounced parked files.

**When touching one backend, check the other.** They were written months
apart and drift silently because nothing enforces the shared contract.

## A green check is not evidence until you know what it checked

The parity lesson above came from diffing two implementations. This one came
from auditing the *checks*, and it caught more. Every item below reported
success while testing nothing, and none of them looked wrong:

- **A skipped test is as green as a passing one.** CI installed `gpgv` but
  not `gpg`, so the eleven tests covering the dpkg backend's whole trust
  anchor never ran there — written, passing locally, silently absent where
  it mattered. `EMERGE_TESTS_REQUIRE_ALL=1` now turns a missing capability
  into a failure naming it.
- **A test can assert nothing and look fine.** One exercised a function that
  returns early unless run as root. One restored a monkeypatch over itself
  in cleanup, so it verified the patch. Neither was caught by reading them;
  both were caught by breaking the code and noticing the test stayed green.
  **Mutation-test every new test**, and when a mutation survives, work out
  whether it is a gap or an equivalent mutant — several here were genuinely
  equivalent, and saying so is part of the job.
- **A measurement can be of the wrong thing.** Timings of "5.7s" for a suite
  that takes a minute were of runs that had been *killed* — a `pkill -f
  gpg-agent` matched its own command line. Two claims were published from
  that before it was noticed.
- **Documentation verified once decays.** The README's console blocks were
  real captures, checked by hand, and three prose claims around them went
  stale within the session. They are re-rendered and compared by a test now.
- **A check that cannot fail is worse than none**, because the next reader
  trusts it: an unreachable `""` branch in `clean`, and a monotonicity guard
  in `_sync_regions` that could never reject. Both removed.
- **The environment is part of the check.** Running CI in a `debian:trixie`
  container — as *root*, which a workstation is not — found four defects
  that every local run called green, one of them a regression introduced
  earlier the same day. See "Running CI locally"; it is worth the trouble.

The habit that generalises: after something passes, ask what would have had
to be true for it to fail, and go and make that true.

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
- `index.has()` is true for a *virtual* package name — it answers "is this name
  satisfiable", not "is there a real package here". Using it to decide provider
  substitution silently breaks every virtual dependency.
- On the apt backend, the solver's plan is advisory: `resolve()` re-simulates
  through `apt-get -s` and *that* is what runs. Any guarantee the solver makes
  has to be re-checked against apt's answer.
- Session detection runs unprivileged most of the time (`emerge -p`). Hardened
  processes hide `/proc/PID/{maps,exe}` from non-root, so anything that reads
  only those will silently see a fraction of the session.
- gpgv reads binary keyrings only, and rejects armoured `.asc` outright
  (`invalid packet (ctb=2d)` — that's the leading `-`). It also fails a file
  whose signatures it cannot *all* check, so keys must be presented together.
- `is_protected` (Essential + `Priority: required`) does **not** cover
  `libc6` — Debian ships it `Priority: optional` now. What stops
  `emerge -C libc6` is apt refusing to break `dpkg`'s Pre-Depends, not us.
- **`/proc/PID/comm` is capped at 15 characters** (`TASK_COMM_LEN - 1`), so a
  session leader whose binary name is longer only ever appears truncated.
  This fails silently in both directions — an entry written out in full never
  matches, and an entry over 15 characters can never match anything — and the
  symptom is not an error but a session that stops being detected, so
  `emerge -u` quietly stops warning that an upgrade may restart the desktop.
  It has already bitten once (`sddm-greeter-qt6`). Tests enforce it now; add
  new entries in their truncated spelling.
- **`/proc/PID/comm` is the *parent's* name between fork and exec.**
  `subprocess.Popen` returns as soon as the fork happens, so a test that
  reads the child's comm immediately can get `python3` instead of the
  binary's name. Measured at 3 in 200 — rare enough to look like an
  unrelated flake and never reproduce on demand. Poll until the value is
  what you expect, not until it is readable.
- **Tests that patch `os`, `shutil` or `subprocess` are patching the module
  objects the script itself imports.** Both test modules carry a
  `tearDownModule` sentinel that fails the run if one of those is left
  replaced, naming it — added after the second such leak, and it found a
  third (`subprocess.call`, patched twice: once with no cleanup at all, once
  with a cleanup that read the attribute back *after* patching and so
  restored the patch over itself). Without a cleanup that restores the
  original — captured *before* the patch, or `addCleanup` restores the patch
  over itself — the damage lands in whatever runs next and nowhere else.
  `make check-unit` and `make check-integration` are separate processes and
  structurally cannot see it: a `shutil.which` stub returning None leaked out
  of the world-seeding tests and made the entire gpgv suite fail, while each
  module passed alone. `make check-isolation` (and a CI step) runs both in
  one interpreter for exactly this.
- **`apt-get dist-upgrade` does take package arguments**, despite its synopsis
  in apt-get(8) showing them only for `install`. Verified:
  `apt-get -s dist-upgrade sl` emits `Inst sl`. This matters because the
  `emerge -uD @world foo` branch appends the atoms to `dist-upgrade`, and the
  synopsis is exactly the sort of thing that gets someone to "fix" that back —
  reintroducing the bug where `foo` was silently dropped.
- Anything parsing apt's `Inst`/`Remv` lines must stop the package name at
  `:` — `(\S+)` swallows the `:arch` qualifier apt emits on multiarch, and a
  name captured as `tree:amd64` then matches nothing you asked for.
- `run_mergetool`'s two template styles quote **differently on purpose**. The
  `{named}` form is shlex-quoted here; the positional `%s` form is not,
  because dispatch-conf templates quote the placeholders themselves
  (`--output='%s'`). Quoting again nests quotes and splits paths containing
  spaces. There is a test asserting this; don't "fix" it.
- `DpkgBackend._resolve_inner`'s `select()` is **recursive**, one frame per
  dependency level, so it raises `RecursionError` on a chain deeper than
  about 1,000. Measured, not guessed: 900 works, 1,500 does not. Real Debian
  depth is nowhere near that (plasma-desktop's 1,484 packages are breadth),
  so this is a documented bound rather than a bug to fix — `ndu_solve` was
  made iterative because `@world` genuinely is deep, and this is not.
- An unrecognised **long** option used to match nothing and fall through both
  arms of the final branch, so it was silently discarded. Harmless for a typo
  like `--nonsense`; dangerous for `--no-dep-upgrades`, one letter off, which
  ran an ordinary unprotected install. Short options always rejected unknown
  letters; long ones now do too. Likewise a bare `--with` consumed the next
  token before flag parsing, so `--with -a pkg` silently ate the `-a`.
- Anything beginning with `@` that is not a known set is an error. Letting it
  fall through to the resolver produced "no packages to satisfy @nosuchset",
  which answers a question nobody asked.
- **Python ignores SIGPIPE**, so a closed pipe becomes a `BrokenPipeError`
  at interpreter shutdown — "Exception ignored on flushing sys.stdout" and
  exit status 120. `emerge -pv @world | head` is the normal way to read a
  long list, so `__main__` restores the default handler and it dies quietly
  like any other Unix tool.
- **`lintian` flags `uses-dpkg-database-directly`**, and it is right that
  parsing `/var/lib/dpkg/status` is unusual. It is deliberate: the dpkg
  backend exists for boxes with no apt, so libapt is not available to it by
  definition, and the alternative is a `dpkg-query` fork per package on
  every run. `debian/apt-emerge.lintian-overrides` records that, so the
  package lints clean and a *new* tag is visible rather than lost in noise.
- Never fork per search result. `emerge -s '^lib'` matches 29,185 packages;
  one `apt-cache policy` per hit meant the search never finished. Batch it.
  The same mistake reappeared in `archive_settled`, which forked
  `dpkg-query` once per package: **163× slower** than one batched call
  (9.98s vs 0.061s for 200 packages), so a 1,500-package dist-upgrade spent
  over a minute doing nothing else — on the failure path too. On Debian the
  package count is always the scale that matters.
- **apt marks everything named on its command line as manually installed**
  (apt-mark(8): "the package you installed explicitly is marked as manually
  installed"). Since `@selected` *is* `apt-mark showmanual` on this backend,
  every name we pass is a world entry. `--no-dep-upgrade` resolves the whole
  closure itself and passes each package as an explicit `pkg=version` pin, so
  one `emerge --no-dep-upgrade libsdl3-dev` put **32 dependencies** into
  `@world` permanently, where `--depclean` could never reclaim them.
  `AptBackend._apply_marks` now snapshots the manual set before the install
  and puts everything back to auto except the atoms the user typed. Anything
  new that adds names to `self._action` must keep that true.
- **dpkg is not a solver.** It does exactly what it is told, in the order it
  is told, and both sequences the dpkg backend issued were wrong in ways only
  real dpkg reveals — the resolver, the simulation and the merge list were all
  perfectly happy:
  - `--unpack` everything then `--configure -a` once fails on `Pre-Depends`:
    dpkg requires a pre-dependency to be *configured*, not merely unpacked
    ("libb is unpacked, but has never been configured"). `merge()` now
    configures what is staged before unpacking anything with a `Pre-Depends`
    field. One `dpkg -i` with the whole plan does **not** fix this — dpkg
    does not reorder either.
  - `dpkg -r` per package in the order the user typed fails as soon as an
    earlier victim is depended on by a later one. One call naming all of them
    works, because dpkg *does* order removals itself.
- `sources.list(5)` disables a deb822 stanza with `Enabled: no`, and calls it
  the easier alternative to commenting every line out. Ignoring the field
  meant `--sync` fetching from repositories the admin had switched off.
- A mergetool template is user-written configuration, so it can be wrong.
  `mergetool=meld` — no placeholders — made `"meld" % (a, b, c)` a `TypeError`
  that crashed dispatch-conf mid-review, with earlier files already retired
  and no summary of what had been decided. Both substitution styles are now
  guarded.
- The `out` file handed to a mergetool is **seeded with your current version**
  so the tool has something to edit. A tool that exits 0 without writing would
  otherwise have that seed accepted back as a merge result — silently
  "merging" to exactly what you already had and retiring the update.
- Constructing a backend must not do work. `pick_backend()` runs for
  `emerge -V` too, and `DpkgBackend.__init__` seeded the world file — so a
  version query, as root, created `/var/lib/emerge-dpkg` and announced it.
  Seeding is deferred to the first `_read_world()`.
- `int(epoch)` in `vercmp` raised `ValueError` on a malformed version, and
  `vercmp` sits on every code path there is: one odd string in an index took
  down an operation that had nothing to do with it. A non-numeric epoch now
  falls back to treating the whole string as the version.
- `parse_depends` silently dropped a clause whose alternatives it could not
  parse, which makes an unparseable dependency look **satisfied** — the one
  direction a resolver must never fail in. It cannot be fatal (one odd field
  would break every operation), so it warns once per distinct clause.
- `/proc/PID/maps` puts the pathname last precisely because it may contain
  spaces. `line.split()[5]` truncated such a mapping to its first word, so the
  library went unrecognised and its package was never flagged as
  session-critical. Use `split(None, 5)`.
- `os.replace` swaps in a different inode, so a file written fresh keeps
  nothing the original carried outside the stat struct — SELinux labels, ACLs,
  file capabilities. `_write` copies xattrs across; an `/etc` file that comes
  back unlabelled can stop being readable by the one daemon that needs it.
- `emerge -uD @world foo` dropped `foo`: the `dist-upgrade` branch took the
  set and threw the atoms away. `apt-get dist-upgrade` does take package
  arguments.

---

## Commit history shape (recent, most-relevant last)

emerge core (apt wrapper) → dpkg-only backend → unified single file w/ backend
select → `@world`/sets + source builds → streaming fix → dispatch-conf 3-way
merge → dispatch-conf archive-timing fix → `--no-dep-upgrade` (target-only) →
corrected to whole-closure w/ installed-pins → shared `ndu_solve` for both
backends + `_AptIndex` → `_dep_ok`/provides_of callable fix → same-upstream
escape hatch (`--with` + interactive + session flag) → world-file atomic write
→ live session-critical detection applied to all `-a/-p` merges.

First pass on a real Debian 13 box: session detection reads binaries and
survives hardened processes → unit test suite (differential against dpkg and
diff3) → three `--no-dep-upgrade` fixes found by running the real libsdl3-dev
case → gpgv Release verification on the dpkg backend.

Then, mostly driven by auditing rather than by a feature list — the recurring
theme being that the **apt backend** lagged the dpkg one on everything the
two share:

- **`--no-dep-upgrade` usability**: the `-a` escape hatch loops until it
  resolves instead of retrying once; a same-upstream bump reads as
  `(session rebuild)` rather than a restart warning; `ndu_solve` gained real
  backtracking, then an on-demand exhaustive pass (`ndu_search`,
  `--backtrack`).
- **Honesty about destructive actions**: `emerge -C` showed one package while
  apt would have removed 868; failures were filtered out of the output so
  "see output above" pointed at nothing; config writes were not durable;
  `.deb`s with no index checksum installed silently.
- **Parity sweep**: archiving on failure, `--oneshot` (which needed hard
  rule 3 relaxed), search that forked per result and never finished on
  `^lib`.
- **Arg parsing**: unknown long options were silently discarded — including
  a mistyped `--no-dep-upgrades`.
- **Coverage**: end-to-end tests against throwaway dpkg *and* apt roots, the
  dispatch-conf interactive loop, and the archiving half of config merging.
- **Repository**: README, GPL-2.0-or-later, `-V`, and CI — whose first run
  failed three times, all of them test defects invisible on the dev machine.

Then a review pass and everything it pulled behind it. The recurring theme
this time was different, and it is the one worth carrying forward: **a green
check is not evidence until you know what it checked.**

- **Thirteen defects from reading the whole script**, the worst three
  reproduced against real dpkg and real apt rather than argued about: dpkg
  refuses to unpack a package whose `Pre-Depends` is only unpacked; `dpkg -r`
  one at a time dies on a dependency; and `--no-dep-upgrade` named the whole
  closure to apt, which marks every one of them manual, so 32 dependencies
  entered `@world` for good.
- **Packaging**: `make deb`, `debian/`, `emerge.1`, and a CI job that
  installs the package and runs it — because a `.deb` full of wrong paths
  builds perfectly.
- **The premises under the features nobody had tested.** dispatch-conf rests
  on dpkg parking an edited conffile as `.dpkg-dist`; that is now proven
  before the round trip is. `-b`/`-B` had never executed once. Signature
  verification had only ever run with `gpgv` stubbed.
- **Differential and property testing** where a reimplementation exists:
  `vercmp` fuzzed against dpkg, `merge3` against its four rules, the solver
  against brute force on graphs small enough to enumerate. The last of these
  matters because a false wall is a bug this project has already shipped.
- **Checks that were not checking.** The gpg suite skipped in CI for want of
  `gpg` while reporting OK; `make check-unit` and `check-integration` are
  separate processes and structurally cannot see a leak between modules; a
  test failed once in twenty runs because `Popen` returns before `exec` and
  `/proc/PID/comm` still held the parent's name.
- **CI run for real in a container**, which runs as root as the workstation
  does not. It found four things every local run called green — including a
  regression introduced earlier in the same session.
- **Two investigations that ended in "no change".** `merge3` is not
  `diff3 -m` equivalent and should not try to be: three quarters of the
  difference is diff3 producing the worse answer. Replacing difflib with a
  minimal Myers diff changed nothing in 12,000 merges. Both are written down
  so nobody spends the day again.
