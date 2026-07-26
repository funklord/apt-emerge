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

One file, no dependencies:

```sh
sudo install -m 755 emerge /usr/local/sbin/emerge
```

Python 3.9 or newer. That is the whole install — the single-file constraint
exists so an embedded box can be fixed with one `scp` and a text editor.

## What it does

Ordinary operations map onto the Portage verbs you already know:

| | |
|---|---|
| `emerge -av <pkg>` | install, after showing the plan and asking |
| `emerge -uD @world` | full system upgrade (`apt-get dist-upgrade`) |
| `emerge -C <pkg>` | unmerge |
| `emerge --depclean` | remove packages nothing depends on |
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
python3 -m unittest test_emerge test_integration
```

`test_emerge.py` is unit-level. `test_integration.py` drives both backends
end to end against a throwaway root — real `.debs`, a real repository, real
installs — and needs no privileges, because dpkg is given `--root` and
apt's state is redirected. It skips itself where that is unavailable.

Two suites are differential rather than hand-written: `vercmp` reimplements
Debian policy 5.6.12, so every version pair is also run through
`dpkg --compare-versions` and must agree; the 3-way merge documents itself
as `diff3 -m` equivalent, so its output is compared against `diff3`.

## Status and limits

Developed and used against Debian 13 (trixie). Honest about what is not
covered:

- The dependency search is not a complete SAT solver. It backtracks properly
  across versions, but alternatives within one dependency (`a | b`) are
  taken first-match, and the search is bounded by a step budget — exceeding
  it reports a solver limit rather than claiming no solution exists.
- The dpkg backend is exercised end to end in tests, but has not been run on
  a real machine that lacks `apt-get`.
- Session detection is verified on KDE Plasma with sddm; GNOME and the
  various greeters are not.
- `--no-dep-upgrade` searches per target, so on a large set it runs many
  dependency simulations and can be slow.

`project.md` carries the design notes, the reasoning behind the parts that
went through several revisions, and the mistakes worth not repeating.

## Licence

GPL-2.0-or-later. See [LICENSE](LICENSE).

Copyright (C) 2026 Nabeel Sowan &lt;nabeel@vibes.se&gt;

This program is free software; you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the Free
Software Foundation; either version 2 of the License, or (at your option)
any later version. It is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
