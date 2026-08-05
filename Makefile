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
# clobbering a plain one. BUILD_DIR is the canonical name across these
# projects -- it was BUILD_DIR here, which is a BSD-make convention that only
# one sibling uses, and the two are not quite synonyms anyway: this holds a
# whole build tree of .deb, .changes and .buildinfo, not object files. There
# is nothing here that compiles to an object file at all.
BUILD_DIR  ?= dist

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

.PHONY: all check check-unit check-integration check-isolation style install \
        uninstall deb clean version-check help test veryclean distclean hooks

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
	@echo '  style       the indentation and whitespace gate'
	@echo '  install     install into $$(DESTDIR)$$(prefix)'
	@echo '  deb         build a binary package into dist/'
	@echo '  clean       remove build products'

# -- tests -------------------------------------------------------------------

check: style version-check check-unit check-integration check-isolation

check-unit:
	$(PYTHON) -m unittest test_emerge

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
style:
	$(PYTHON) tools/style_gate.py check
	$(PYTHON) tools/style_gate.py docs

# The Portage-dialect version the program reports and the Debian package
# version are two different things, and both are hand-written. This stops
# them drifting: debian/changelog must carry the same upstream version that
# `emerge -V` prints, minus the -deb dialect suffix.
# The VERSION file is this program's version and the source for the package.
# The script's VERSION is the Portage dialect it emulates and is deliberately
# not tied to it: they described the same number until apt-emerge had one of
# its own.
version-check:
	@file=$$(cat VERSION); \
	changelog=$$(dpkg-parsechangelog -SVersion 2>/dev/null); \
	if [ -z "$$changelog" ]; then \
		echo "version-check: skipped (dpkg-parsechangelog unavailable)"; \
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
# outside the tree and not ours to litter. Collect them into dist/ instead.
deb: version-check
	dpkg-buildpackage --build=binary --no-sign
	mkdir -p $(BUILD_DIR)
	set -e; ver=$$(dpkg-parsechangelog -SVersion); \
	arch=$$(dpkg-architecture -qDEB_HOST_ARCH); \
	moved=0; \
	for f in "../$(PACKAGE)_$${ver}_all.deb" \
	         "../$(PACKAGE)_$${ver}_$${arch}.deb" \
	         "../$(PACKAGE)_$${ver}_$${arch}.buildinfo" \
	         "../$(PACKAGE)_$${ver}_$${arch}.changes"; do \
		if [ -e "$$f" ]; then \
			mv -f "$$f" $(BUILD_DIR)/; moved=$$((moved + 1)); \
		fi; \
	done; \
	if [ "$$moved" -eq 0 ]; then \
		echo "deb: dpkg-buildpackage produced nothing to collect"; exit 1; \
	fi
	@echo
	@ls -l $(BUILD_DIR)/

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
CLEAN_DIRS = $(BUILD_DIR) debian/$(PACKAGE) debian/.debhelper __pycache__

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

distclean: veryclean
	find . -name '*~' -o -name '*.swp' -o -name '*.orig' | xargs -r rm -f
	find . -name __pycache__ -o -name .pytest_cache -type d -prune -exec rm -rf {} +

# The commit-msg hook lives in the tree so it is reviewable, survives a
# clone, and can be kept in sync. .git/hooks is untracked, so a hook that
# exists only there enforces a rule nobody can see and vanishes silently on
# a fresh clone.
hooks:
	@test -d .git || { echo "hooks: not a git repository" >&2; exit 1; }
	@install -m 0755 tools/hooks/commit-msg .git/hooks/commit-msg
	@echo "hooks: commit-msg installed from tools/hooks/"
