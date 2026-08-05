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
| `tools/hooks/commit-msg` | the shared commit-msg hook; `make hooks` installs it |
| `.style-gate.toml` | the gate's scope here, see `code-style.md` |
| `LICENSE` | GPL-2, verbatim from `/usr/share/common-licenses/GPL-2` |
| `.github/workflows/tests.yml` | CI: interpreters, Debian, package, style, stdlib-only |
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

The line numbers below are from a much older and much shorter version of the
file, and they have been wrong by a growing margin ever since. Treat the
*order* as the map and re-grep for anything specific — the sequence has not
changed, only the offsets. (This paragraph used to carry both figures and
they went stale twice, which is the same lesson the file table above states
as a rule: `wc -l` is right there.)

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

**The dpkg backend is single-architecture, and now says so.** `installed_state()`
is keyed by package name, and after `dpkg --add-architecture i386` that is
ambiguous: `libfoo:amd64` and `libfoo:i386` are two installed packages
sharing one name, and the later stanza in the status file silently won. So
the resolver planned against a view of the machine with packages missing
from it, and `emerge -C libfoo` went all the way to dpkg, which refuses —
"ambiguous package name 'libfoo' with more than one installed instance",
verified against real dpkg with two hand-built `.debs`.

Keying by `name:architecture` throughout is the real fix and a real piece of
work: it reaches the resolver, the world file, depclean, and the index,
which `sync` fetches for `binary-<native>` only, so a foreign-arch package
has no index entry to be resolved against at all. The backend exists for
embedded boxes, which are single-architecture, and the apt backend — what
runs on any desktop that has i386 enabled — delegates to apt and is
unaffected. So the position is: **detect it and say so**, rather than
half-supporting it.

- `installed_state()` records every colliding name in `MULTIARCH_INSTANCES`
  as it parses, since it is a property of the file and the function is
  called from a dozen places.
- `resolve` warns once, after the progress line, naming the packages.
- `unmerge` refuses a name it cannot disambiguate and prints the command
  that works (`dpkg -r libfoo:amd64`), rather than letting dpkg produce the
  raw error after the plan has already been shown and confirmed.

Whether to implement multiarch properly is open, and is the owner's call.

### CLI surface (Portage dialect)

- `--sync` — apt: `apt-get update`; dpkg: fetch `Packages` indexes into
  `TREE_DIR` over http(s)/file. A `file://` flat entry pointing at a bare dir of
  `.deb`s gets an index generated on the fly (USB-stick repo).
- install atoms; `-a` ask, `-p` pretend, `-v` verbose, `-u` update, `-D` deep,
  `-1` oneshot (don't record in world), `-f` fetchonly.
- Sets: `@world` = `@selected` + `@system`. `@selected` = world file (dpkg) /
  `apt-mark showmanual` (apt). `@system` = Essential+required. `world`/`system`
  bare words accepted. On the dpkg backend `@selected` is really the world
  file **intersected with what is installed** — see the open question in the
  backlog; an entry that is not installed is skipped, and now says so.
- **Atoms are Debian names, and Portage spellings are met halfway.** A
  category is accepted and dropped (`app-misc/sl` is `sl`), because Debian
  has none. A leading version operator is refused with the spelling that
  works: apt's `sl=5.02-1+b1`, not Portage's `=sl-5.02-1+b1`. Position is
  what separates them — the same `=` is Debian's syntax when it follows the
  name and Portage's when it leads.

  All of this used to fall through to apt and come back in apt's words, and
  the worst of them was actively misleading: `emerge app-misc/sl` answered
  *"Unable to locate package app-misc"*, naming a package nobody had asked
  for. A tool whose premise is that emerge's command line works here should
  not answer in the vocabulary of the thing it is driving.

  Two things are deliberately *not* reinterpreted, because Debian already
  owns the syntax: `sl:i386` is a multiarch qualifier rather than a Portage
  slot, and anything path-shaped is a local `.deb` for apt to install.
  That second one is why category-stripping is guarded — `pool/sl.deb` has
  a category-shaped first component, and dropping it hands apt a file that
  is not there.
- `-C`/`--unmerge`, `--depclean`/`-c` (apt: `autoremove`; dpkg: world-closure).
- `--deselect` — drop packages from `@selected` without unmerging them.
  apt: `apt-mark auto`; dpkg: edit the world file. Honours `-p`.
- `--info` — the block to paste into a bug report. Its rows were chosen from
  this program's own failures rather than from Portage's list, which is
  mostly compilers and USE flags: the backend in use, dpkg and apt versions,
  the architecture **and any foreign ones**, the locale, whether `gpgv` and
  `lzma` are present, how many source entries are enabled, and what the
  graphical session looks like from here. Every one of those has cost
  somebody a misdiagnosis in the notes above. Like `--version` and `--help`
  it answers and exits, constructing no backend and writing nothing.

  Reading `--backend=` in argv order was a wart the two of them shared:
  `emerge --info --backend=dpkg` reported the apt backend and
  `emerge --backend=dpkg --info` reported the dpkg one. The flag is scanned
  before the loop now, so the same command gives the same answer.
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

Those two figures are **not reproducible any more, and not because anything
regressed** — see the section below. Re-measured 2026-08-05 on the same box,
now fully updated: `--no-dep-upgrade -p libsdl3-dev` is 7.9s and
`--no-dep-upgrade -p @world` is 23.8s. Neither is comparable to the number
above it: the first used to be a *wall* (stop at the first thing that cannot
move) and is now a full 33-package resolve, and the second has far less left
to consider on an up-to-date system. A timing tied to live system state
measures the state as much as the code.

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

**This scenario has since expired, and nothing failed when it did.** The box
was updated: Mesa is at `25.0.7-2+deb13u1` now, so there is nothing left to
drag up and `--no-dep-upgrade -p libsdl3-dev` resolves cleanly — 33 new
packages, no wall. Anyone re-running it to check this section would find the
feature apparently doing nothing, and conclude either that the section is
wrong or that the flag is broken.

That is how live-system evidence always expires: silently, because the
machine moved rather than the code. The three bugs above stay covered by
their own unit tests, and the *scenario* is now pinned by
`TestTheLockstepWall`, which rebuilds it from a synthetic index — two
packages installed at `25.0.7-2`, a target needing both at
`25.0.7-2+deb13u1`. It walks the wall the way a user does: wall, take the
suggested `--with`, wall again, take that line, resolve. Written the obvious
way first with a single mover, where the assertion about accumulating grants
**could not fail**, because with one mover there is no earlier grant to
drop; the mutation walked straight through it. Two movers is the smallest
version of this scenario that tests what it claims to.

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
  One copy per file, overwritten, so the archive is bounded by the number of
  conffiles on the system (1,148 here) rather than growing per upgrade.
- **A file it cannot retire is skipped too**, for the same reason and with
  a sharper edge: `_accept` writes the merged file into `/etc` and *then*
  calls `_retire`, so a failure there ended the review with the change
  already applied, the parked copy still in place, every later file
  unreviewed and no summary of what had been decided. That is the shape a
  mergetool template with no placeholders once produced — the entry in the
  gotchas below — reached through a different door. Tolerating it is safe
  because both halves self-correct: a file left parked is offered again next
  run, where it now matches what is on disk and resolves without a question,
  and an unpromoted ancestor costs a 2-way review that dispatch-conf
  announces.
- **A file it cannot copy is skipped, not fatal.** The copy was unguarded, so
  one unreadable or unwritable conffile raised out of the whole pass — which
  runs after a *successful* install, and again from the failure path where
  its entire job is to record what landed and announce parked files before
  bailing out. An exception there replaces the error the user needed with a
  traceback and abandons every conffile after the failing one, each of which
  then has no ancestor. A full `/var` during an install is the ordinary way
  in. Losing one ancestor is cheap by comparison: dispatch-conf reviews that
  file 2-way and says so.
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

**A conffile is bytes, and not necessarily UTF-8 ones.** `read_lines` and
`_write` pair `encoding="utf-8", errors="surrogateescape"` so a byte that
does not decode survives the round trip exactly. They used to read with
`errors="replace"` and write back whatever that produced, which broke two
things at once on ordinary input — an older `/etc` file with a latin-1
accent in a comment:

- **The byte was rewritten.** `0xe9` came back as U+FFFD's three bytes,
  silently, on the one path in the program that edits `/etc`. Nothing else
  in the run mentioned it and there is no way back from it.
- **Two different files compared equal.** `dispatch_conf` opens with
  `if mine == theirs: _retire(...)`, and `caf\xe9` and `caf\xff` both
  decode to the same replacement character — so a genuine update was
  treated as already applied and discarded. Same shape as the archive-timing
  bug above, reached by a different road.

Reading losslessly costs something, and it has to be paid on the way out:
a byte that is not valid UTF-8 lives in the string as a lone surrogate, and
`print()` cannot encode one — it raises `UnicodeEncodeError` unless stdout
happens to be in UTF-8 mode. Displaying the file is the first thing a review
does, so that would have been a crash on exactly the files that used to be
corrupted. `printable()` renders the original byte escaped (`caf\xe9`), and
`color_diff` is the only display path it has to cover. There is a test that
encodes the captured output rather than just capturing it, because a
`StringIO` holds surrogates quite happily and would pass either way.

**A conffile is quite often a symlink**, and `_write` used to destroy it.
`os.replace` swaps the *link* for a regular file, so an admin who points
`/etc/nginx/nginx.conf` at a git-managed tree lost two things silently: the
indirection, and the update itself — the merged text landed at the link's
name while the file it pointed at kept the old content, so their source of
truth went stale and their next edit of it changed nothing. `_write` now
resolves with `os.path.realpath` first.

That the function was already inconsistent is the tell worth remembering:
`os.stat` follows symlinks, so the mode and owner it takes such care to
preserve were the *target's* all along, and were being applied to the
regular file that had just replaced the link. Two lines apart, disagreeing
about which file they were operating on.

What it still does not preserve is a **hard link** — `os.replace` swaps in
a new inode, so a conffile with a second link to it will diverge. Left
alone deliberately: fixing it means writing in place and giving up the
atomic replace, which is a far worse trade for `/etc` than a broken hard
link.

The same read-then-write-back shape appears in `_bump_changelog`, which is
the likeliest file in a source tree to carry such a byte — it is full of
maintainer names — and it raised `UnicodeDecodeError` rather than
corrupting. Fixed with it.

Not a locale bug, though the locale hides it: without an explicit encoding
a file means whatever `LANG` said, and CPython's UTF-8 mode covers the C
locale — the one anybody would think to test — while leaving a latin-1
locale to decode as latin-1. Measured before assuming: under `LC_ALL=C`,
`LC_ALL=POSIX`, and even with `PYTHONCOERCECLOCALE=0`, Python 3.13 reports
`utf-8` and `utf8_mode: 1`.

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

### How much a repository may spend of yours

Verifying *what* the bytes say left open *how many* of them there could be.
`--sync` read an index with a plain `read()` and unpacked it with
`gzip`/`lzma.decompress`, both of which produce whatever the input asks for
— and it did that **before** the hash check, so the bytes had been vouched
for by nobody at the moment they were unpacked.

Measured rather than assumed: 400 MB of zeros compresses to **61 KB of
`.xz`**, a ratio of 6,859, and `lzma.decompress` on it took peak RSS from
108 MB to 828 MB. A mirror serving nine megabytes can therefore ask for
sixty gigabytes — of the backend whose entire reason for existing is boxes
that do not have it, over plain http, as root.

Three changes, and the order of the first is the point:

- **Hash first, unpack second.** Release records the hash of the file *as
  transferred*, so the check reads the same bytes either way; doing it first
  means a mirror cannot spend this process's memory on the strength of bytes
  that have not been vouched for. One behaviour change comes with it: an
  index that is listed in Release and does not match is now refused outright
  rather than being quietly skipped in favour of another compression format.
  That is the correct reading of "failure is fatal, inability to check is
  not" — a mismatch is a failure.
- **A ceiling on decompression** (`MAX_INDEX_BYTES`, 512 MB), applied in
  bounded steps, which is the only defence a repository with no Release gets
  — the USB-stick case, where there is nothing to verify against.
- **A ceiling on the download**, from the `Size` the index already states
  for each `.deb`. The SHA256 still decides correctness; this only stops a
  mirror choosing how much memory is spent before reaching it.

The bounded reader is **no less strict than the one-shot calls it
replaced**, which had to be checked rather than hoped for: a corrupted
payload, a corrupted gzip CRC trailer, a removed trailer and a genuine
truncation are all refused, and there is a test pinning each against
`gzip.decompress` as the reference. The trailer cases are the ones worth
having: a deflate stream can end cleanly while the CRC32 after it says the
bytes are wrong, and a decompressor that stops at the end of the stream
never looks.

That test also had to be fixed before it meant anything. The first version
truncated its fixture at a fixed 120 bytes, and this index compresses to 88
— so it truncated nothing and passed by reading a whole file.

## Crash-safety

- No persistent pins (see hard rule 3). Interrupted installs recover with the
  normal `dpkg --configure -a` / `apt-get -f install`.
- World file, config **and index** writes are atomic (temp + fsync +
  `os.replace`), and all three go through one `write_atomic()`. Getting
  there took two passes, and the second corrected the first.

  **The index was not atomic at all.** `sync()` opened the file and started
  filling it, so an interrupted `--sync` — the slow operation that talks to
  the network, and therefore the one people interrupt — left a **truncated**
  `Packages` behind. Truncated does not mean unreadable, which is the whole
  problem: cutting a 500-package index a third of the way through leaves 168
  packages that parse perfectly, so the resolver plans against part of the
  archive and reports "there are no packages to satisfy" for things that
  plainly exist.

  **Then the three copies were merged, and one of them turned out to be
  wrong too.** The world file's copy was missing the cleanup on failure, so
  an interrupted write left a `world.tmp` beside it — the sort of thing
  nobody looks for until the next crash. That is the argument for one
  writer rather than three: every step has to be right, and a drifted copy
  looks identical from the outside. `_write` keeps what is genuinely its
  own through a `prepare` hook (mode, owner and xattrs, which `os.replace`
  drops with the old inode) and its own `.emerge-tmp` suffix, because a bare
  `.tmp` beside somebody's config in `/etc` says nothing about whose it is.
- `--with` allow-set is per-invocation, never persisted.

**What is not protected: two runs at once.** Nothing takes a lock. On the apt
backend that mostly does not matter, because apt takes dpkg's frontend lock
and the second run waits. The dpkg backend has no equivalent: two concurrent
runs would each read the world file, each modify their copy, and each write
it back, so one set of changes is lost — `merge`, `unmerge` and `--deselect`
are all read-modify-write against it. A `--sync` racing a resolve is safer
than it was now that indexes are replaced atomically rather than rewritten
in place, but the world file is not covered by that.

Whether to take a lock, and where it should live so that it does not
pretend to coordinate with apt when it cannot, is open and unstarted.

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

**`--fetchonly` had no coverage at all until it was looked for**, and
looking was misleading: the two test files mention `fetchonly` twenty-one
times between them and every one passes it as `False`. Grepping says the
flag is exercised; nothing ran it. Both backends are covered now, separately,
because they implement it differently — the dpkg one downloads and returns,
the apt one runs `apt-get -y -d` and exits with its status.

The apt half also shows why an assertion has to match the fixture: with a
`file://` repository apt uses the archive where it lies rather than copying
it into the cache ("Download complete and in download only mode"), so the
obvious check for a `.deb` under `Dir::Cache` fails for a reason that has
nothing to do with the flag. That test asserts the download-only invocation
instead, and says so.

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

**The scope is the part that goes wrong, and it did.** The gate used to
select files by suffix or exact name, and `emerge` has no suffix — it is a
command, not a module. Out of the box it therefore checked six files and
reported them clean while never opening the program. That was worked around
here first, by naming `emerge` in `.style-gate.toml`, and then **fixed in
the tool**: an extensionless file beginning with `#!` is a program and is in
scope on its own. Three projects had the same hole — `fmake` *is* fmake,
`situc` is situ's entry point — which is what made it the tool's problem
rather than this one's. The workaround is gone; `TestStyleGate` still pins
the outcome.

Everything else the config used to carry has gone the same way, and the
remaining file is short on purpose: only `Makefile` is still named, because
it has neither a suffix nor a shebang and is otherwise not checked at all.

The finding underneath all of it is one rule, now enforced by the tool:
**a config that is present is applied exactly, or the run fails.** There is
no half-applied setting, because the failure it produces is the worst one
available — a gate that checks a *different* set of files and reports
success, which reads exactly like a clean tree. Four ways in, all measured
before they were fixed:

- **`tomllib` is 3.11+**, and an older interpreter used to warn once and
  carry on with the defaults: `8 files conform`, exit 0, with `emerge` and
  `debian/rules` outside the set.
- **A config that is not a regular file** — a directory, or a symlink to
  nothing — answered False to `is_file()` and read as "no config here".
- **Invalid TOML, or a file that cannot be opened**, failed as a traceback,
  which reads as a broken tool rather than a wrong config and sends the
  wrong person looking.
- **A value of the wrong type**, which is the quiet one: `indent_names =
  "emerge"` — quotes where brackets belong — is valid TOML, and a `set()` of
  a string is a set of its *characters*, so the name matched nothing. One
  pair of quotes took a three-file list down to one, exit 0, no output but
  the count.

All four now exit 2 with a diagnosis naming the file and the problem; a
genuinely absent config still falls back to the defaults, and 3.9 is refused
only when there is a config it would have had to ignore. The interpreter
guard that used to live in this project's `style` target is gone with them —
the fix went upstream, where the other six projects get it too.

Mutation-tested rather than assumed. On the rules: a 4-space-indented
function appended to `emerge` is caught at the line (`indented 0 tab(s),
structure says 1`), so is one in `test_emerge.py`, so is a space-indented
line inside an existing block (as a `TabError`), so is trailing whitespace
in `project.md`. On the config handling: each of the four refusals above was
reverted in a scratch copy of the tool and the matching test fails, and so
does the one asserting the provenance header sits *below* the shebang —
above it the kernel does not see `#!`, and the file, which is mode 755, gets
run by the shell instead, where it hangs on the first unbalanced quote
rather than failing. All seven copies were shipped that way.

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
- **Version.** Two numbers that describe different things and are no longer
  tied. `VERSION` at the root states apt-emerge's own version and is what
  `debian/changelog` must agree with; `make version-check` and a unit test
  hold those two together. The script's `VERSION` is `PORTAGE_DIALECT` plus
  `-deb` -- the Portage release whose dialect this speaks, with `-deb`
  marking it as the Debian reimplementation. `emerge --version` prints it and
  scripts read it, so it is a compatibility surface rather than a version of
  this program. They carried the same number until apt-emerge had one of its
  own; tying them again would make the tool claim a Portage dialect that does
  not exist.

  Splitting them left the program unable to say which version it *was*:
  both `-V` and `--info` answered `Portage 3.0.66-deb`, which is the same
  string in every release. So the script carries `APT_EMERGE_VERSION` too.
  That duplicates the `VERSION` file deliberately — the shipped artifact is
  one script somebody scp'd onto a box where no `VERSION` file exists, and
  a bug report opened with `--info` is exactly where the answer is needed.
  Three hand-written strings now (file, changelog, script), and
  `make version-check` compares all three; the script/file half needs no
  `dpkg-parsechangelog`, so it still runs where the changelog check skips.
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
pages, and removes the package again. **`lintian` is a gate now, not a
note.** It ran with `|| true` for as long as nobody had read its output;
that has been done, and the result is below.

### What lintian said, the first time anyone looked

The binary package was already clean, and the one tag it emits —
`uses-dpkg-database-directly` — is the one `debian/apt-emerge.lintian-
overrides` answers. Confirmed the override is load-bearing rather than
decorative by removing it: the tag reappears and the run fails. So the
step now uses `--fail-on error,warning`, and a tag is a regression.

**The source package was never looked at at all.** `make deb` builds
`--build=binary`, so its `.changes` names a `.deb` and a `.buildinfo` and
no `.dsc` — every source-only check had nothing to inspect and said
nothing, which reads exactly like passing. Building a source package
separately found two things:

- **`override_dh_auto_test` ignored `DEB_BUILD_OPTIONS=nocheck`** — a real
  defect. `dh_auto_test` honours that option itself, and overriding the
  target threw it away, so a build that asked for no tests ran them
  anyway. Now guarded, and checked in both directions: with `nocheck` the
  build emits no test output at all, without it the suite runs.
- **`Standards-Version` was 4.7.0**, two releases behind.

The version bump was treated as a claim rather than a formality, because
that is what the field is. The 4.7.1 and 4.7.2 checklists were read (in a
container — `debian-policy` is not installed here), and one new rule bears
directly on this package: **two packages must not install programs of
different functionality under the same name on `PATH`.** This package
deliberately takes Gentoo's command names, so that was checked against the
archive rather than assumed — `apt-file` in a trixie container reports no
package shipping `emerge`, `dispatch-conf` or `etc-update` in `/usr/bin`
or `/usr/sbin`. The search was run with a positive control first
(`/usr/bin/dpkg`, `/usr/bin/apt-mark`), because an index that failed to
download answers "no collisions" in exactly the same words. The remaining
4.7.1 and 4.7.2 items are about `/usr/games`, the `/bin` and `/lib`
merged-usr symlinks, and requiring files under `/usr/share/{locale,man,
info}` — none of which this package does.

CI now lints the source package too, building one for the purpose. Info
tags do not fail it: `out-of-date-standards-version` fires the day Policy
is released, which is news about Debian rather than a defect here.

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

**Read the skip counts, not just the OK.** Run over this session's work the
matrix came back clean on every interpreter — and the unit suite reported
`OK (skipped=5)` on 3.9, `skipped=1` on 3.11 and nothing on 3.13. That
spread is the shape to expect, and it is worth knowing which five, because
the oldest supported interpreter is exactly where lost coverage would hide:

- three need `tomllib` (3.11+), and they test the **style gate**, which
  refuses to run without it anyway;
- one inspects f-string tokens, which only 3.12 tokenises that way;
- one needs `sys.stdlib_module_names` (3.10+), and it is the stdlib-only
  guard — covered in CI by the `stdlib-only` job, which runs on a current
  Python.

Every one is a check whose *own implementation* needs a newer interpreter,
not a behaviour a 3.9 user depends on. No product coverage is lost there. If
that count ever grows, something new is being skipped and the same question
has to be asked again.

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

**A guard that everything else has to stub out is a guard nothing tests.**
Sweeping `need_root()` — replacing each of the ten calls with `pass` — left
the whole suite green. The reason is structural rather than careless: the
integration harness *must* stub `need_root`, because it runs unprivileged on
purpose, and the unit tests reach those functions with it already replaced.
The guard was invisible precisely because everything else needs it gone.

Asserting `SystemExit(1)` was not enough, and looked like it was: without
the guard, `merge` and `unmerge` still exit 1, from dpkg failing on
permissions several steps later. Three mutations survived a test that
appeared to cover them. The distinction the guard exists for is *refusing
before doing anything*, so the test forbids subprocesses outright — any
command executed means it did not fire first — and checks the message says
superuser rather than something else. All ten are caught now.

**The wiring from a flag to the thing that acts on it is its own test.**
Found by sweeping: replace every *read* of an option in `main()` with
`False`, run both suites, and see which mutations nobody notices. Five did
— `-v`, `-u`, `-D`, `--no-dep-upgrade` and `--no-verify`. Every one of those
behaviours is tested hard (the solver against brute force, sync's
verification against real `gpgv`, `print_merge_list` against the README),
and the tests reach them by calling the backend directly, so the line in
`main()` that carries the flag could be deleted with nothing failing.

That is the shape of two bugs already shipped here: the `no_dep_upgrade`
branch of `AptBackend.resolve()` that did not return, so the flag produced
*exactly* the no-flag plan; and `emerge -uD @world foo` dropping `foo`.
Both were wiring rather than behaviour. `TestArgParsing` now asserts each
flag arrives, and asserts the default too — a flag that is always on is as
broken as one that never is, and a test that only checks the on case cannot
tell the difference.

A grep-based version of this survey was tried first and was wrong in both
directions: it reported `--buildpkg` and `--no-verify` as untested, because
their tests pass options through a helper's defaults rather than a literal
dict, and it counted `fetchonly`'s twenty-one mentions as coverage when
every one of them was `False`. Counting occurrences measures how a suite is
written; mutation measures what it protects.

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
   split, and there is no pressure for it: the file is still navigable and
   the layout map above is enough.
2. ~~A real test suite~~ — **done**, and so is everything this entry listed as
   next: `make check` (plus `check-unit` / `check-integration`) and the CI
   jobs, including one that builds the package, installs it and runs the
   installed binary. See **Packaging** and **CI**, which is where the job
   list lives — this entry carried a count of its own and fell a job behind
   the moment one was added.
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

   **Suggested first pass: Python only.** PEP 668 makes it the most-wanted,
   and it exercises the whole pipeline before anyone has to face npm's
   dependency graphs.

   **`stdeb` was evaluated for that job on 2026-08-05 and does not hold it.**
   Run, not read about: `python3-stdeb` 0.10.0-5 from trixie, driven with
   `py2dsc` in a container.

   - **It cannot process a `pyproject.toml`-only package at all.** It shells
     out to `setup.py`, and a modern sdist does not ship one:
     `can't open file '.../setup.py'`. That is most of PyPI now, and it is
     the finding that decides the question.
   - **Its version translation is wrong in the direction that matters.**
     `1.0.0.dev3` correctly becomes `1.0.0~dev3`, but `1.0.0rc1`,
     `2.0.0b2` and `1.0.0a1` pass through unchanged — and
     `dpkg --compare-versions 1.0.0rc1 gt 1.0.0` is **true**, so a packaged
     release candidate is never superseded by the release. `emerge -u
     @world` would decline the upgrade forever, silently. Confirmed against
     dpkg and against this project's own `vercmp`, which agree on all six
     forms tested.
   - **It crashes on ordinary missing metadata.** No `description` is a
     `TypeError` in `common.py`; no `long_description` is an
     `AttributeError` in `util.py`. Both are optional fields upstream.
   - **Its name mapping is mechanical**: `python3-<name lowercased>`, so
     PyPI `PyYAML` becomes `python3-pyyaml` while Debian ships
     `python3-yaml`. It does nothing about the collision; that stays ours.
   - **`fpm`, named above as the generic fallback, is not packaged for
     Debian at all** — neither `fpm` nor `ruby-fpm` is in trixie.

   **`dh-python` was then evaluated as the replacement, and it works.**
   Proven end to end on 2026-08-05 against `tomli` 2.4.1 from PyPI — a real
   sdist with `pyproject.toml` and no `setup.py`, the exact case `stdeb`
   cannot read. A hand-written `debian/` of four files (`control`,
   `changelog`, a three-line `rules` calling
   `dh $@ --buildsystem=pybuild`, and `source/format`) built
   `python3-tomli_2.4.1-1_all.deb`, which installs and imports, landing in
   `/usr/lib/python3.13/dist-packages/tomli/`.

   So the shape is: emerge generates that `debian/` directory and drives the
   `dpkg-buildpackage` machinery `-b`/`-B` already has. Nothing new is
   needed to build.

   **It moves the difficulty rather than removing it, and the new one is a
   second name mapping.** `Build-Depends` has to name the package providing
   the build backend, which is read from `build-system.requires` in
   `pyproject.toml` — `tomli` declares `flit_core.buildapi`, so `flit` had
   to be installed or the build does not start. Checked across the common
   backends: `setuptools`, `hatchling`, `poetry_core`, `pdm_backend`,
   `maturin` and `scikit_build_core` are all `python3-<name>` with
   underscores turned to hyphens — and `flit_core` is not. There is no
   `python3-flit-core` in trixie; the package is `flit`. Seven of eight
   mechanical and one exception, among the eight most common, means a table
   of exceptions is unavoidable here too.

   What this changes: the converter was supposed to be the part we did not
   write. Two of the three hard parts below — version translation and name
   mapping — have to be ours whichever tool is driven, and the tool that was
   going to do the rest cannot read a modern package. So the shape to
   evaluate next is `dh-python`/`pybuild-plugin-pyproject` (6.20250414,
   current, and what Debian itself builds with) with emerge generating a
   minimal `debian/` directory, rather than a converter that owns the
   process. That is a bigger piece of work than "drive stdeb", and worth
   knowing before starting rather than after.

   **The hard parts, none of them optional:**
   - *Name mapping.* PyPI `PyYAML` is Debian `python3-yaml`. Either keep a
     mapping or vendor everything and accept the duplication.
   - *Version translation.* PEP 440 `1.0.0rc1` must become `1.0.0~rc1` or it
     sorts **above** the final release — measured above, not feared.
     `vercmp` already gets `~` right, so
     this is mechanical — but getting it wrong is silent.
   - *Collision with archive packages*, which is the problem being solved
     reappearing in new clothes. A self-built `python3-requests` shadowing
     Debian's is no better than pip's mess. Needs a namespace
     (`pypi-requests`) or strict Provides/Conflicts, plus the existing
     `+local1` bump so `@world` will not clobber it.
   - *Transitive closure size*, npm especially.
   - *apt backend only*, like `-b/-B`: it needs build tooling, and the dpkg
     backend is binary-only by design.

10. **What `@selected` means when a world entry is not installed.** Open,
    and deliberately not settled while fixing the silence around it.

    The document above says `@selected` is the world file. The dpkg backend
    computes `world & installed`, so an entry naming a package that is not
    installed is dropped. The two readings differ in what `emerge @world`
    does after somebody removes a package with `dpkg -r` directly:

    - **Portage's reading** — the world file is what you asked to have, so
      `emerge @world` reinstalls the missing member. This is what real
      emerge does, and hard rule 4 says to answer in Portage's dialect.
    - **The current reading** — `@world` means the installed things you
      chose, so a package you removed stays removed and the stale entry is
      inert.

    Changing it is a real behaviour change: under Portage's reading, every
    `emerge -u @world` would try to reinstall what an admin deliberately
    removed by hand, which on Debian is a common way to work. That is the
    owner's call, not a decision to make while passing through.

    What *was* wrong either way is that the drop happened in silence, so
    the two ways of naming one package disagreed: `emerge -p foo` answers
    `there are no packages to satisfy "foo"` and exits 1, while the same
    package reached through `@world` printed an ordinary plan and exited 0.
    It is now reported, and the resolve still proceeds — on Debian a
    package leaving the archive between releases is ordinary, and refusing
    to compute `@world` until the file is tidied would block the upgrade
    that resolves it.

    Reporting it raised the obvious next question — and nothing could act
    on the report. `--deselect` closes that: see below. The semantic
    question above is untouched by it.

11. ~~No way to clear a world entry~~ — **done**, `--deselect`, Portage's
    own verb for it. `emerge -C` removes an entry only as a side effect of
    unmerging, and it *skips* a package that is not installed, which is
    exactly the entry worth clearing; the only remedy was a text editor,
    which is a poor answer for a file the program tells you is wrong.

    Both backends implement it, because `@selected` is a different thing on
    each: apt marks the package auto-installed, the dpkg backend edits the
    world file. Hard rule 3 is untouched — `apt-mark auto` records set
    membership, never a version, which is the distinction that let
    `--oneshot` use it too.

    Verified against a real `apt-mark` in `AptBackendEndToEnd`, where the
    assertion that matters is that the package is **still installed**
    afterwards. That is the whole difference from `--unmerge`, and it is
    the one a wrong implementation would get wrong.

    Two things an exit-code survey caught afterwards, both mine. It printed
    `Removing x` and *then* called `need_root()`, so a non-root run claimed
    to have done the work — and because stdout is block-buffered while the
    error is not, the permission failure appeared *above* the line saying it
    had already happened. And it exited 0 when nothing it named was in
    `@selected`, while `emerge -C` exits 1 when none of its targets were
    installed. Two verbs that both mean "you named things I could not act
    on" should not disagree about whether that is a failure.

    The survey itself needed redoing: the first pass read `$?` after a
    pipeline, so it was reporting `tail`'s status and every command looked
    like it exited 0 — including `--sync` without privileges, which would
    have been a much worse bug than the ones actually there.
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

### A later sweep, done by diffing the surface rather than reading it

Listing both classes' methods and counting the guards and messages in each
shared one is a cheap way to find the next instance: three of the nine
differed, two of them for good reasons (`sync` is one line on apt and
ninety on dpkg, because apt delegates to `apt-get update`).

The third was real. **`emerge -C` means different things on the two
backends, and the message denied it.** `apt-get remove` takes every
dependent with it, so on apt `-a` genuinely cascades — that is the 868-
package case above. dpkg does no such thing: it refuses to remove a package
another installed one needs. The guard nevertheless ended *"use -a to
override"*, so on the dpkg backend the user confirmed a removal that could
not happen and got a raw dpkg error for it:

```
>>> Unmerging (1 of 1) lib-1.0...
dpkg: dependency problems prevent removal of lib:
 app depends on lib.
!!! unmerge failed.
```

Verified against real dpkg, not argued from the manual. The guard now says
what dpkg will do and names the command that works (`emerge -C app lib`,
also verified — dpkg takes both in one call and orders them itself). `-a`
survives as an escape hatch rather than an override, because
`_reverse_deps` can over-report: it understands alternatives (`a | b` with
`b` installed is not a break) but not `Provides`, so a dependency satisfied
by a virtual package looks broken to it and not to dpkg.

**What is left open is the divergence itself**, and it is not a wording
question: on apt, `emerge -C libjpeg62-turbo` offers to remove 868
packages; on dpkg the same command refuses until you name them. Real
Portage does neither — it unmerges exactly what you name and warns that you
have broken something. Which of the three this project should mean is the
owner's call, so nothing here changed the behaviour.

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
  dispatch-conf exists. **And the reverse: `--force-conf*` alone is not
  enough either.** Those flags settle conffile questions; they do not stop a
  maintainer script asking something else. The apt backend always passed the
  variable and the dpkg backend never did, which is a hang rather than a
  cosmetic difference — `capture()` takes stdout while leaving stdin
  attached, so a postinst that prompts writes it where nobody can see and
  then waits. Reproduced as root in a container: without the variable, still
  blocked after six seconds; with it, 0.1s and rc=0. Every dpkg call that
  can run a maintainer script — unpack, configure, remove — now passes
  `dpkg_env()`.
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
- **apt and dpkg translate their output, and this program reads it.** Under
  `LANGUAGE=de`, `Installed:` is `Installiert:` and `Setting up x` is
  `Einrichten von x`, so every English pattern here silently stops matching.
  Two things broke and neither errored: `emerge -s` reported
  `Latest version available: ?` for every package, and a merge printed apt's
  raw output instead of Portage's — the one thing the program exists to do.
  Anything whose output is *read* now runs under `parse_env()` (`LC_ALL=C`,
  which beats `LANGUAGE`; checked rather than assumed).

  What was never at risk is worth knowing too, because it is why this is
  three call sites and not thirty: `Inst`/`Remv` from `apt-get -s`, the
  `apt-cache showpkg` headings and RFC822 field names are all emitted
  untranslated, so the resolver, the virtual-provider lookup and the index
  parser were always safe.
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
- **Two actions in one run used to mean the second was silently skipped.**
  `main()` dispatches in a fixed order and returns, so which one survived
  depended on the order of the branches rather than on anything visible:
  `emerge -C --depclean foo` ran depclean, dropped the removal, and exited 0
  — a destructive action nobody asked to run on its own, standing in for the
  one they did ask for. Portage answers *"Multiple actions requested"* and
  stops, which is both right and already this program's dialect. Grouped, so
  that `-s`/`-S` and `-b`/`-B` still name one action each rather than
  colliding with themselves.
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
- **Constructing a backend must not create state, and world seeding has now
  broken that twice.** `pick_backend()` used to run for `emerge -V`, so a
  version query, as root, created `/var/lib/emerge-dpkg` and announced it.
  That was fixed by deferring the seed to the first `_read_world()` — which
  was **worse**, because `merge()` reads the world file *after* dpkg has
  run, so the seed then captured what the merge had just installed and put
  every dependency of the first install into `@world` permanently.

  So seeding sits at construction, where it happens before anything is
  installed, and `emerge -V` avoids it by not constructing a backend at all
  (`backend_name()`). This entry said the opposite for a while; the code's
  own docstring is the record to trust here, and it explains why.

  The third door was `--pretend`, which constructs a backend like anything
  else: `emerge -p bash` on a fresh root box wrote a 92-entry world file, on
  a run that then failed for want of a package tree. A pretend run now seeds
  **in memory** — not skipped, because `@selected` reading empty would make
  `emerge -p @world` preview a smaller set than the run it is previewing.
  All three properties have tests, since each fix here has broken another.
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
- **A decompressor produces whatever its input asks for.** `gzip.decompress`
  and `lzma.decompress` have no ceiling, and neither does `read()` on a
  socket, so a repository decided how much memory `--sync` used. 61 KB of
  `.xz` expands to 400 MB. Anything read from a mirror goes through
  `fetch(..., limit=)` and `decompress_bounded`.
- **`errors="replace"` on a file you are going to write back is data loss**,
  not error handling. It is spelt like a courtesy and it destroys the byte.
  Anything read to be re-written uses `errors="surrogateescape"` on both
  sides; anything read only to be parsed may keep `replace`, which is why
  `/var/lib/dpkg/status` and the `Packages` indexes still do. And a string
  read that way cannot simply be printed: a lone surrogate raises
  `UnicodeEncodeError` on a strict stdout, so display goes through
  `printable()`.
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

Then a long session with no feature list at all, working outward from
whatever the last piece had exposed. The shape of it is worth keeping
because the same shape will happen again.

**Data loss in `/etc`, twice, on the one path that edits it.** A conffile is
bytes and not necessarily UTF-8 ones: `errors="replace"` on the way in and
an encode on the way out rewrote any byte that did not decode, and made two
different files compare equal so a genuine update was discarded as
already-applied. Then `_write` was found replacing a *symlinked* conffile
with a regular file, sending the merge to the link's name and leaving the
admin's real file stale. Both silent, both on the path whose own docstring
worries about writing a bad `sshd_config`.

**Things a repository or a locale can do to you.** `--sync` unpacked an
index before checking its hash and with no ceiling, so 61 KB of `.xz` could
ask for 400 MB — of the backend written for boxes that do not have it. It
wrote that index straight over the old one, so an interrupted sync left a
*truncated* `Packages` that parses perfectly with a third of the archive in
it. And every English pattern in the file stops matching under
`LANGUAGE=de`, which cost `emerge -s` its versions and a merge its
Portage-style output.

**Backend parity again, and again the backend that implements it itself.**
`emerge -C` promised `-a` would override a wall that dpkg will not let you
past; the dpkg backend ran maintainer scripts without
`DEBIAN_FRONTEND=noninteractive`, so a package that asks a debconf question
hangs with the prompt written where nobody can see it. Diffing the two
classes' shared methods by counting their guards found both in minutes,
which is worth remembering as a technique.

**Promises the CLI was not keeping.** `-p` created a 92-entry world file;
two actions in one command ran whichever the dispatch order reached first
and dropped the other; Portage's own atom spellings fell through to apt and
came back in apt's words; `--deselect` announced work before checking it
could do it. None of these failed loudly, and all of them were found by
asking what the program promises rather than by reading it.

**And the recurring lesson, which is about the tests rather than the code.**
Four separate times a check of mine passed for the wrong reason: a fixture
truncated at 120 bytes when the file was 88, a wall test with one mover
where the assertion needed two, an exit-code survey reading `$?` after a
pipe, and an archive test using `chmod 000` — invisible as an ordinary user,
readable as root, so it passed here and failed in the container. Mutation
testing caught every one; reading them caught none. The container job earned
its place again, and the habit that generalises is the one already written
above: after something passes, work out what would have had to be true for
it to fail, and go and make that true.
