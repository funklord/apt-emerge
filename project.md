# emerge-for-debian — project notes

A Gentoo Portage–flavoured package manager for Debian/Ubuntu, implemented as a
single stdlib-only Python 3 script. It speaks emerge's CLI dialect and paints
Portage-style output, but drives real Debian tooling underneath.

The shipped artifact is still one file, `emerge` (~3600 lines). Everything
else in the repository is development scaffolding and is not installed:

| file | |
|---|---|
| `emerge` | the whole program; the only thing that ships |
| `test_emerge.py` | unit tests (~3200 lines) |
| `test_integration.py` | end-to-end tests against throwaway roots |
| `README.md` | user-facing front page |
| `LICENSE` | GPL-2, verbatim from `/usr/share/common-licenses/GPL-2` |
| `.github/workflows/tests.yml` | CI |
| `project.md` | this file |

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

## Testing notes / what needs a real box

Chat-side testing used synthetic `Packages` trees under `TREE_DIR` and mocked
`apt-cache` output. What genuinely needs Claude Code + a real system:

- ~~**Real multi-version repos.**~~ Done: validated against a live trixie
  mirror with `-updates`/`-security` pockets. The `libsdl3-dev` deb13u1 wall
  reproduces exactly, and doing so found three bugs (see that section).
- ~~**A graphical session.**~~ Done on KDE Plasma Wayland + sddm; found and
  fixed the two permission traps in session detection. GNOME/gdm still unseen.
- **apt-less embedded box** — still outstanding, and now the biggest untested
  area. The dpkg backend's sync (including signature verification) was
  exercised against real archives and a synthetic USB `file://` repo using a
  throwaway `TREE_DIR`, but install / depclean world-closure have never run on
  hardware without `apt-get`.
- **Config merging** against real package upgrades that ship conffile changes —
  still outstanding. Needs a real install, not a pretend run.
- **Anything requiring a real install.** Everything validated so far has been
  read-only (`-p`) or written to a throwaway root; `merge`, `unmerge` and
  `dispatch_conf` have not been run against live system state on this box.

Handy synthetic-test pattern used so far: write a `Packages` stanza file into
`/var/lib/emerge-dpkg/tree/`, `dpkg -i` a hand-built `.deb` to simulate an
installed base, then run `--backend=dpkg -p ...`. For apt-path unit tests,
monkeypatch the module-global `capture` to return canned `apt-cache show`
output and call `ndu_solve` directly.

Always `python3 -m py_compile emerge` after edits.

## The test suite

`python3 -m unittest test_emerge test_integration` — 244 tests, stdlib only,
~0.7s.

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

`python3 -m unittest test_emerge` — 214 unit tests, stdlib only, ~0.2s. Covers
`vercmp`, `meets`, `parse_depends`, `parse_stanzas`, `merge3`, `_significant`,
`_dep_ok`, `ndu_solve`, `_wall_from_merges`, `_with_arg`, `_AptIndex.has`,
`_policy_batch`, `stream_apt`, `run_mergetool`, `_write`, the apt backend's
`unmerge_candidates` and `merge` aftermath, `print_unmerge_list`, the
signature-verification code, and session detection.

Two suites are **differential** rather than hand-written expectations, because
both reimplement something with an existing reference — keep them that way:
- `vercmp` is Debian policy 5.6.12 in Python, so every pair in the table is
  also run through `dpkg --compare-versions` and must agree.
- `merge3` claims `diff3 -m` equivalence, so its output is compared
  byte-for-byte against `diff3` on cases that merge cleanly.

Both skip themselves if the tool is missing, so the suite runs off a Debian box.

## CI

`.github/workflows/tests.yml`, on every push and pull request (the remote
carried both `main` and `master` at one point, so it is not branch-pinned).
Free on GitHub for a suite this size. Four jobs:

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
- **`stdlib-only`** — walks the AST for imports outside
  `sys.stdlib_module_names` and fails if a non-test `.py` appears beside
  `emerge`. This guards hard rule 1.

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
   + `Makefile` (build → amalgamated single file via a concat tool à la sqlite;
   `make test`; `make deb` — which can be `emerge -B emerge`, it can package
   itself) + `debian/` dir. Decision was pending. Recommended: modules for dev,
   amalgamate to one file for ship. Do NOT use cpack (drags in CMake); use
   `debian/` + `dpkg-buildpackage`, or `dpkg-deb --build` from a staging dir.
   Skip `zipapp` — a `.pyz` isn't `vi`-able on an embedded box, which defeats
   the single-file purpose.
2. ~~A real test suite~~ — **done**, see above. No CI wired up yet; that and a
   `make test` target are the obvious next step (item 1 covers the Makefile).
3. ~~`ndu_solve` completeness~~ — the greedy false wall is **fixed** (real
   backtracking, see above). What remains open is narrower: `a | b`
   alternatives are still first-match rather than branched on, and the step
   budget bounds the search. Neither has been observed to bite in practice.
4. ~~GPG verification~~ — **done**, see the verification section.
5. ~~`_SESSION_LEADER_COMMS` truncation audit~~ — done for KDE Plasma/sddm.
   Still unverified on GNOME/gdm and the greeter entries.
6. ~~Session-critical noise~~ — **done**. A same-upstream bump is now reported
   as `(session rebuild)` with an einfo saying the session keeps running; only
   a real upstream change keeps `(session)` and the restart warning. The
   derived set is still broad, but breadth now costs a mild note rather than a
   false alarm. See "Session-critical detection".
7. **The dpkg backend on real apt-less hardware.** Deprioritised by the owner
   — rare case, mostly works. It is now exercised end-to-end by
   `test_integration.py` against a throwaway dpkg root (sync, resolve,
   download+SHA256, merge, world file, `--no-dep-upgrade` pins and walls,
   `--with`, unmerge, depclean), so what remains untested is only the
   genuinely hardware-specific part: a box with no `apt-get` at all, and
   sync over real http rather than `file://`.
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
