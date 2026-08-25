# emerge for Debian -- build and install rules.
#
# There is nothing to compile: the shipped artifact is one stdlib-only Python
# file, deliberately (hard rule 1 -- an embedded box has to be fixable with
# one scp and a text editor). So `all` really is a no-op, and this file
# exists for `install`, `check` and `deb`.
#
# The install rule is the single source of truth for what lands where:
# debian/rules calls it through dh_auto_install rather than keeping a second
# list in debian/install that could drift from it.

PACKAGE     = apt-emerge
PYTHON     ?= python3

# Where build products land. Settable so an isolated build can be kept from
# clobbering a plain one.
#
# BUILD_DIR is the canonical name across these projects. It was OBJDIR here,
# which is a BSD-make convention only one sibling used, and the two are not
# quite synonyms anyway: this holds a whole build tree of .deb, .changes and
# .buildinfo, not object files. Nothing here compiles to an object file at
# all. (The sentence above used to say the name had been BUILD_DIR, because
# the rename rewrote the word it was contrasting against.)
#
# `build`, not `dist`. Every other private project defaults this to `build`
# and this was the last one that did not, which made `make clean` and every
# instruction in the README a special case for one tree. `dist` is also the
# wrong word for it: a dist directory conventionally holds something ready
# to ship, and this holds whatever the last local build produced.
BUILD_DIR  ?= build

# Where the finished packages land. `dpkg-buildpackage` writes to the PARENT
# directory, so they have to be moved somewhere regardless; this names it.
#
# A `deb/` subdirectory of the build tree rather than the build tree itself,
# which is what five of the ten packaging projects already did and is now the
# settled answer. Two reasons beyond consistency: it keeps a set of artifacts
# separable from object files, so `ls` and `clean` can both speak about them
# by name; and it is the only spelling that still works when BUILD_DIR is `.`,
# as openmlx4 sets it, where the build tree and the source tree are one.
DEB_DIR ?= $(BUILD_DIR)/deb

prefix     ?= /usr
bindir     ?= $(prefix)/bin
datarootdir?= $(prefix)/share
mandir     ?= $(datarootdir)/man
man1dir     = $(mandir)/man1
docdir     ?= $(datarootdir)/doc/$(PACKAGE)

INSTALL         = install
INSTALL_PROGRAM = $(INSTALL) -m 755
INSTALL_DATA    = $(INSTALL) -m 644

# The script answers to these names too -- argv[0] selects the action, so a
# symlink is the whole implementation (see the __main__ block in `emerge`).
ALIASES = dispatch-conf etc-update

.PHONY: all check check-unit check-integration check-isolation check-tty \
        style style-source style-docs install uninstall deb clean \
        version-check help test veryclean distclean hooks

all:
	@:

help:
	@echo 'Targets:'
	@echo '  check       everything; slowest, the apt tests drive real apt'
	@echo '  check-unit  unit tests only, a few seconds -- the fast loop'
	@echo '  check-integration'
	@echo '              real dpkg, apt, gpgv and http against throwaway roots'
	@echo '  check-isolation'
	@echo '              both modules in one interpreter, for leaks the'
	@echo '              per-module sentinel cannot see'
	@echo '  check-tty   the unit suite under a pty, where the script'
	@echo '              colours its output and every other run does not'
	@echo '  style       the indentation and whitespace gate'
	@echo '  install     install into $$(DESTDIR)$$(prefix)'
	@echo '  deb         build a binary package into $$(BUILD_DIR)'
	@echo '  clean       remove build products'

# -- tests -------------------------------------------------------------------

check: style version-check check-unit check-integration check-isolation \
       check-tty

check-unit:
	$(PYTHON) -m unittest test_emerge

# The same suite with stdout attached to a terminal, which is the one
# condition every other way of running it destroys.
#
# `emerge` decides USE_COLOR once, from sys.stdout.isatty(), so a copy
# loaded under a pty paints its output and one loaded under a pipe does not.
# CI pipes, make pipes, an editor pipes -- so a test asserting on output
# could pass everywhere here and fail for the person who typed the command,
# which is exactly what happened: one test looked for `Emerging (1 of 2)
# libb` in text that read `Emerging (1 of 2) \033[1;32mlibb-1\033[0m`, and
# took `make deb` down with it through dh_auto_test.
#
# load() normalises the flag now and TestTheHarness pins it, so this target
# is the backstop rather than the fix. pty.spawn is stdlib, needs no
# util-linux `script`, and works with stdin closed and inside a container --
# both checked, because CI has neither a terminal on stdin nor a desktop.
check-tty:
	$(PYTHON) -c 'import pty, sys; raise SystemExit(pty.spawn(\
	    [sys.executable, "-m", "unittest", "test_emerge"]) >> 8)'

# Both modules in one interpreter, which is the only way to see one module's
# leftovers break another.
#
# The common case no longer needs it: each module now has a tearDownModule
# sentinel that fails if it left os/shutil/subprocess patched, which is what
# every leak so far has been, and which it reports by name in the run that
# caused it. This stays for what the sentinel cannot see -- environment
# variables, the working directory, state inside the loaded copies of the
# script -- and for the genuine end-to-end reassurance.
#
# It is also the slow one: it re-runs everything, and the apt tests drive a
# real apt-get. `make check-unit` is the fast loop at a few seconds.
check-isolation:
	$(PYTHON) -m unittest test_emerge test_integration

# Skips itself where rootless dpkg/apt is unavailable, so it is safe to run
# anywhere -- including inside a package build.
check-integration:
	$(PYTHON) -m unittest test_integration

# The indentation and whitespace gate, shared verbatim with the sibling
# projects; `docs` additionally holds project.md to the tree it describes.
#
# There was a guard here refusing to run on a Python older than 3.11, on the
# grounds that the gate would then be unable to read .style-gate.toml, would
# fall back to its defaults, and would check a smaller set of files
# successfully. That was worth guarding, and it is no longer this Makefile's
# job: the gate refuses a config it cannot read on its own now. The fix went
# upstream instead of being kept here, so the other six projects get it too.
style: style-source style-docs

style-source:
	$(PYTHON) tool/style_gate.py check

style-docs:
	$(PYTHON) tool/style_gate.py docs

# The Portage-dialect version the program reports and the Debian package
# version are two different things, and both are hand-written. This stops
# them drifting: debian/changelog must carry the same upstream version that
# `emerge -V` prints, minus the -deb dialect suffix.
# The VERSION file is this program's version and the source for the package.
# The script's VERSION is the Portage dialect it emulates and is deliberately
# not tied to it: they described the same number until apt-emerge had one of
# its own.
#
# Three places now, not two. The script carries the version as well, because
# the shipped artifact is one file somebody scp'd onto a box where there is
# no VERSION file to read -- and a program that cannot say which version it
# is answers with the Portage dialect, which is the same in every release.
version-check:
	@file=$$(cat VERSION); \
	script=$$(sed -n 's/^APT_EMERGE_VERSION *= *"\(.*\)"/\1/p' emerge); \
	changelog=$$(dpkg-parsechangelog -SVersion 2>/dev/null); \
	if [ "$$file" != "$$script" ]; then \
		echo "version-check: VERSION says $$file but"; \
		echo "               emerge says $$script"; \
		exit 1; \
	fi; \
	if [ -z "$$changelog" ]; then \
		echo "version-check: $$file, script in step "; \
		echo "               (changelog skipped, no dpkg-parsechangelog)"; \
	elif [ "$$file" != "$$changelog" ]; then \
		echo "version-check: VERSION says $$file but"; \
		echo "               debian/changelog says $$changelog"; \
		exit 1; \
	else \
		echo "version-check: $$file, in step"; \
	fi

# -- install -----------------------------------------------------------------

install:
	$(INSTALL) -d $(DESTDIR)$(bindir) $(DESTDIR)$(man1dir) \
	              $(DESTDIR)$(docdir)
	$(INSTALL_PROGRAM) emerge $(DESTDIR)$(bindir)/emerge
	$(INSTALL_DATA) emerge.1 $(DESTDIR)$(man1dir)/emerge.1
	$(INSTALL_DATA) README.md $(DESTDIR)$(docdir)/README.md
	set -e; for a in $(ALIASES); do \
		ln -sf emerge $(DESTDIR)$(bindir)/$$a; \
		ln -sf emerge.1 $(DESTDIR)$(man1dir)/$$a.1; \
	done

uninstall:
	rm -f $(DESTDIR)$(bindir)/emerge $(DESTDIR)$(man1dir)/emerge.1
	set -e; for a in $(ALIASES); do \
		rm -f $(DESTDIR)$(bindir)/$$a $(DESTDIR)$(man1dir)/$$a.1; \
	done

# -- package -----------------------------------------------------------------

# dpkg-buildpackage writes its products to the parent directory, which is
# outside the tree and not ours to litter -- with these projects side by side
# in one directory, that is a sibling's tree. Collect them into BUILD_DIR
# instead.
deb: version-check
	dpkg-buildpackage --build=binary --no-sign
	mkdir -p $(DEB_DIR)
	set -e; ver=$$(dpkg-parsechangelog -SVersion); \
	arch=$$(dpkg-architecture -qDEB_HOST_ARCH); \
	moved=0; \
	for f in "../$(PACKAGE)_$${ver}_all.deb" \
	         "../$(PACKAGE)_$${ver}_$${arch}.deb" \
	         "../$(PACKAGE)_$${ver}_$${arch}.buildinfo" \
	         "../$(PACKAGE)_$${ver}_$${arch}.changes"; do \
		if [ -e "$$f" ]; then \
			mv -f "$$f" $(DEB_DIR)/; moved=$$((moved + 1)); \
		fi; \
	done; \
	if [ "$$moved" -eq 0 ]; then \
		echo "deb: dpkg-buildpackage produced nothing to collect"; exit 1; \
	fi
	@echo
	@ls -l $(DEB_DIR)/

# Files clean removes, named individually rather than swept up by a wildcard.
# `clean` is the one target everybody runs without reading it, so what it
# deletes is a safety property: a glob that matches more than intended, or an
# unset variable inside an rm, is how a clean target eats a source tree.
CLEAN_FILES = debian/files \
              debian/debhelper-build-stamp \
              debian/$(PACKAGE).substvars \
              debian/$(PACKAGE).debhelper.log

# Directories the build itself creates as staging or output trees, and which
# are therefore disposable whole. Each is still checked before removal: an
# absolute path or a parent traversal aborts, because BUILD_DIR is settable and
# `make clean BUILD_DIR=/` must not be a working command.
#
# The unset-variable case needs no check. These are iterated as shell words,
# so an empty BUILD_DIR disappears in word splitting and removes nothing -- it
# is `rm -rf $(VAR)` as a single command, where an empty VAR leaves a bare
# `rm -rf`, that turns a typo into a disaster.
CLEAN_DIRS = $(DEB_DIR) debian/$(PACKAGE) debian/.debhelper __pycache__

clean:
	rm -f $(CLEAN_FILES)
	@set -e; for d in $(CLEAN_DIRS); do \
		case "$$d" in \
		/* | *..*) \
			echo "clean: refusing to remove '$$d'" >&2; exit 1 ;; \
		esac; \
		if [ -d "$$d" ]; then echo "rm -r $$d"; rm -r "$$d"; fi; \
	done

# `test` is the suite alone; `check` above is everything that must pass first.
test: check-unit check-integration

# The clean ladder, matching the sibling projects.
veryclean: clean
	rm -rf $(BUILD_DIR)

# **`distclean` no longer sweeps the tree for editor droppings.** `*~`,
# `*.swp` and `*.orig` are not build output: they belong to somebody's
# editor, and a `.orig` belongs to a merge they may be in the middle of.
# The sweep was also unbounded -- `find .` walks `.git`, and it was measured
# deleting files in there. `git clean -xdn` lists that class and is the
# person's call rather than the build system's.
#
# **The cache line was doing nothing for the case it names first, and that
# was measured rather than suspected.** It read
#
#     find . -name __pycache__ -o -name .pytest_cache -type d -prune -exec rm
#
# and `-a` binds tighter than `-o`, so it parses as `__pycache__` OR
# (`.pytest_cache` AND -type d AND -prune AND -exec). A `__pycache__` match
# satisfies the left branch, the action never runs, and there is no implicit
# -print to make the silence visible: the target removed `.pytest_cache` and
# left every `__pycache__` where it was. Parenthesising the two names is the
# fix.
#
# What is removed is named exactly and is disposable by construction; the
# search is a wildcard only because a cache appears beside whatever ran.
# `.git` is pruned, and every removal is printed, because a clean target
# that deletes silently is one nobody can check.
distclean: veryclean
	@find . -name .git -prune -o \
	        \( -name __pycache__ -o -name .pytest_cache \) \
	        -type d -prune -print -exec rm -rf {} +

# The commit-msg hook lives in the tree so it is reviewable, survives a
# clone, and can be kept in sync. .git/hooks is untracked, so a hook that
# exists only there enforces a rule nobody can see and vanishes silently on
# a fresh clone.
hooks:
	@test -d .git || { echo "hooks: not a git repository" >&2; exit 1; }
	@install -m 0755 tool/hooks/commit-msg .git/hooks/commit-msg
	@echo "hooks: commit-msg installed from tool/hooks/"
