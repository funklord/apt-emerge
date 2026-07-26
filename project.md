# emerge-for-debian — project notes

A Gentoo Portage–flavoured package manager for Debian/Ubuntu, implemented as a
single stdlib-only Python 3 script. It speaks emerge's CLI dialect and paints
Portage-style output, but drives real Debian tooling underneath.

The current artifact is one file: `emerge` (~2900 lines), plus `test_emerge.py`
(a dev-only test suite — not shipped, and it loads `emerge` by path rather than
importing it, so the single-file rule is intact).

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

Line numbers below are from the ~2619-line version and have all shifted by a
few hundred since (the file is ~2900 lines now). Treat the *order* as the map
and re-grep for anything specific.

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

**Still not complete, and don't claim otherwise:**
- Alternatives within one dependency (`a | b`) are taken first-match, never
  branched on.
- The search is bounded by a 200,000-step budget. Hitting it raises
  `RuntimeError` explaining that this is a solver limit, *not* a wall — giving
  up early is not proof that no resolution exists. Do not turn that into an
  `NduWall`; it would be a lie.

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

`python3 -m unittest test_emerge` — 147 tests, stdlib only, ~0.1s. Covers
`vercmp`, `meets`, `parse_depends`, `parse_stanzas`, `merge3`, `_significant`,
`_dep_ok`, `ndu_solve`, `_wall_from_merges`, `_with_arg`, `_AptIndex.has`, the
signature-verification code, and session detection.

Two suites are **differential** rather than hand-written expectations, because
both reimplement something with an existing reference — keep them that way:
- `vercmp` is Debian policy 5.6.12 in Python, so every pair in the table is
  also run through `dpkg --compare-versions` and must agree.
- `merge3` claims `diff3 -m` equivalence, so its output is compared
  byte-for-byte against `diff3` on cases that merge cleanly.

Both skip themselves if the tool is missing, so the suite runs off a Debian box.

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
7. **The dpkg backend is still untested on a real apt-less box.** Everything
   above was validated on a machine that has apt; `--backend=dpkg` paths were
   exercised with a throwaway `TREE_DIR` and a synthetic USB repo, not on
   hardware that lacks `apt-get`.

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
survives hardened processes → unit test suite (147 tests, differential against
dpkg and diff3) → three `--no-dep-upgrade` fixes found by running the real
libsdl3-dev case → gpgv Release verification on the dpkg backend.
