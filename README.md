# emerge for Debian

A Gentoo Portage–flavoured package manager for Debian and Ubuntu, in one
stdlib-only Python file. It speaks emerge's command line and prints
Portage-style output, but everything underneath is real Debian tooling —
apt, dpkg and their indexes. Nothing is emulated and no state of its own is
invented: `@selected` really is apt's set of manually-installed packages.

```console
$ emerge -pv sl
Calculating dependencies... done!

These are the packages that would be merged, in order:

[ebuild  N    ] sl-5.02-1+b1 12.9 KiB

Total: 1 packages (0 upgrades, 1 new), Size of downloads: 0.0 MiB
```

## Install

Build a package and install it the normal way:

```sh
make deb
sudo apt install ./dist/apt-emerge_*_all.deb
```

That gives you `emerge`, the `dispatch-conf` and `etc-update` aliases, and
`emerge(1)`. Building needs `debhelper` and `dpkg-dev`; the package itself
depends on nothing but `python3` (3.9 or newer).

Or skip packaging entirely, because there is nothing to build:

```sh
sudo install -m 755 emerge /usr/local/sbin/emerge
```

The single-file constraint exists so an embedded box can be fixed with one
`scp` and a text editor — the packaging is a convenience on top of that, never
a requirement.

**That path gives you no man page**, which is the trade for having no install
step to run. Nothing is lost from the option list: `emerge --help` and
`emerge(1)` are held to each other in both directions by a test, so neither
documents an option the other omits — the page adds the explanation, not the
coverage. Copy it across too if you want it:

```sh
sudo install -m 644 emerge.1 /usr/local/share/man/man1/emerge.1
```

Other targets: `make check` runs everything, `make check-unit` runs the fast
half, `make style` is the indentation gate, and `make install` takes the
usual `DESTDIR` and `prefix`.

## What it does

**`man emerge` is the reference** — every option with its own entry, plus the
sets, the environment variables and the files it touches. `emerge --help` is
the same list in short form, and is what you have on a box you only copied
the script to. What follows here is the tour, not the reference.

Ordinary operations map onto the Portage verbs you already know:

| | |
|---|---|
| `emerge -av <pkg>` | install, after showing the plan and asking |
| `emerge -uD @world` | full system upgrade (`apt-get dist-upgrade`) |
| `emerge -C <pkg>` | unmerge |
| `emerge --deselect <pkg>` | stop counting it as one you asked for, without unmerging |
| `emerge --depclean` | remove packages nothing depends on |
| `emerge --info` | the versions and settings to put in a bug report |
| `emerge -s <regex>` | search |
| `emerge --sync` | refresh the package indexes |
| `emerge -b <pkg>` | build from the Debian source package and install it |

Sets work as expected: `@world` is `@selected` plus `@system`, where
`@system` is Debian's Essential and `Priority: required` packages.

Beyond translation, a few things it adds:

**`--no-dep-upgrade`** installs a package without *upgrading* anything
already on the system. It is not a pin — it searches, taking the newest
version of each not-yet-installed dependency whose whole subtree leaves
installed packages alone, and stepping back through older versions when one
does not fit. When nothing fits it says exactly which installed package
formed the wall, and `--with pkg` permits that one to move while everything
else stays put. Useful for installing a leaf without dragging up libc, the
kernel, or your graphics stack.

**Config-file merging.** Debian already parks updated conffiles as
`.dpkg-dist` / `.ucf-dist`, the same idea as Portage's `._cfg0000_`. What
was missing is the review step, so `emerge --dispatch-conf` archives each
package's as-shipped config at install time and uses it as the common
ancestor for a real 3-way merge of your edits against the new version.
Files you never touched, comment-only differences and conflict-free merges
apply automatically; only genuine conflicts are put to you. Also reachable
as `etc-update`, or by symlinking the script to either name.

For a file parked before `emerge` was ever installed there is no archived
ancestor, so it goes looking for one: the version each file was shipped
with is named in `/var/log/dpkg.log`, and the package comes from the local
apt cache, from an enabled repository, or from `snapshot.debian.org` —
verified against the same keyrings your own sources pin, so a recovered
file is trusted no further than the archive keys reach. Set
`recover-ancestor = no` in `/etc/emerge/dispatch-conf.conf` to keep a
review off the network. Where no ancestor can be had the merge is still
offered, marked up two-way rather than resolved, so the choice is never
just keep-everything or take-everything.

**It tells you when an upgrade touches your running desktop.** The set is
derived from the live session — the processes that *are* your session and
the code they have loaded — rather than a hardcoded list:

```console
[ebuild  U    ] libgl1-mesa-dri-25.0.7-2+deb13u1 [25.0.7-2] (session rebuild) 45.2 KiB
[ebuild  U    ] libglx-mesa0-25.0.7-2+deb13u1 [25.0.7-2] (session rebuild) 140.0 KiB
```

A Debian point-release rebuild is marked `(session rebuild)` and says your
session keeps running; only a real upstream version change gets the warning
that it may restart X or Wayland and close what you have open.

**Signature verification on the dpkg backend.** `--sync` checks the
archive's InRelease or Release signature with `gpgv`, honouring `signed-by`,
then checks each index against the hashes inside it. A failure is fatal;
being unable to check only warns, so unsigned local repositories keep
working.

## Two backends

The apt backend is used when `apt-get` is present: apt resolves, downloads,
verifies and installs, and this translates the experience.

The dpkg backend takes over when `apt-get` is absent — embedded systems that
carry only dpkg. It is self-contained, with its own sources parsing, index
sync, dependency resolver, `.deb` fetch and SHA256 check, and a real `world`
file. It is binary-only by design; a box big enough for `build-essential`
can afford real apt.

Force either with `--backend=apt|dpkg` or `EMERGE_BACKEND=`.

## Tests

```sh
make check          # or: python3 -m unittest test_emerge test_integration
```

`test_emerge.py` is unit-level. `test_integration.py` drives both backends
end to end against a throwaway root — real `.debs`, a real repository, real
installs — and needs no privileges, because dpkg is given `--root` and
apt's state is redirected. It skips itself where that is unavailable.

The parts that reimplement something are checked against a reference or an
oracle rather than against hand-written expectations, because a hand-written
case only encodes what its author believed:

- `vercmp` is Debian policy 5.6.12 in Python, so every pair — a fixed table
  and a seeded fuzz over generated versions — is run through
  `dpkg --compare-versions` and must agree.
- `merge3` is held to the four rules it documents, property-tested over tens
  of thousands of random inputs, and its clean merges are compared against
  real `diff3`. `merge2`, the two-way form used when no ancestor can be
  had, is held to the one invariant that matters: resolving every conflict
  to one side reproduces that side exactly, so nothing is dropped and
  nothing invented.
- The `--no-dep-upgrade` solver is checked against brute force on random
  package graphs small enough to enumerate: a plan may never move an
  installed package, it must satisfy every dependency it pulls in, and a
  wall may only be reported where exhaustive search agrees there is none.

## Status and limits

Developed and used against Debian 13 (trixie). Honest about what is not
covered:

- The dependency search is not a complete SAT solver. The first pass takes
  the first alternative of every `a | b`, as apt and dpkg do; only if that
  hits a wall does it search again branching on them, so a wall you are
  shown has survived that second pass. The search is bounded by a step
  budget, and exceeding it reports a solver limit rather than claiming no
  solution exists.
- The dpkg backend is exercised end to end in tests, but has not been run on
  a real machine that lacks `apt-get`.
- Session detection is verified on KDE Plasma with sddm; GNOME and the
  various greeters are not. Process names are matched against
  `/proc/PID/comm`, which the kernel truncates to 15 characters, so a
  greeter whose binary name is longer has to be listed in its truncated
  spelling — a rule the tests enforce, after SDDM 0.21 renamed its greeter
  and went undetected.
- `--no-dep-upgrade` searches per target, so on a large set it runs many
  dependency simulations and can be slow.
- The dpkg backend is single-architecture. It keys packages by name, so where
  a foreign architecture has been added (`dpkg --add-architecture i386`) and
  a package is installed for both, it sees only one of them. It says so
  rather than working from half a view, and refuses an unmerge it cannot
  disambiguate — but use the apt backend on such a system. Resolving is
  unaffected there, since apt does it. Config merging is not a backend
  feature and *is* affected on both: recovering a shipped config file looks
  in the native architecture's index, so a package installed only for a
  foreign one gets a two-way review instead of an ancestor. It degrades
  rather than guessing — the ancestor is never taken from a same-named
  package of another architecture.
- `emerge -C` means slightly different things on the two backends, because
  the underlying tools do. `apt-get remove` takes everything depending on the
  target with it, so the apt backend shows you that whole list and asks;
  dpkg refuses to remove a package another installed one needs, so the dpkg
  backend tells you to name the dependents in the same command.

`project.md` carries the design notes, the reasoning behind the parts that
went through several revisions, and the mistakes worth not repeating.

## Author

Nabeel Sowan <nabeel@vibes.se>

## Licence

GPL-2.0-or-later — see [LICENSE](LICENSE).

Copyright (C) 2026 Nabeel Sowan <nabeel@vibes.se>

This program is free software; you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the Free
Software Foundation; either version 2 of the License, or (at your option)
any later version. It is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
