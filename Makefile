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

.PHONY: all check check-unit check-integration install uninstall deb clean \
        version-check help

all:
	@:

help:
	@echo 'Targets:'
	@echo '  check       run the whole test suite (unit + integration)'
	@echo '  check-unit  unit tests only -- no dpkg, no apt, no filesystem'
	@echo '  install     install into $$(DESTDIR)$$(prefix)'
	@echo '  deb         build a binary package into dist/'
	@echo '  clean       remove build products'

# -- tests -------------------------------------------------------------------

check: version-check check-unit check-integration

check-unit:
	$(PYTHON) -m unittest test_emerge

# Skips itself where rootless dpkg/apt is unavailable, so it is safe to run
# anywhere -- including inside a package build.
check-integration:
	$(PYTHON) -m unittest test_integration

# The Portage-dialect version the program reports and the Debian package
# version are two different things, and both are hand-written. This stops
# them drifting: debian/changelog must carry the same upstream version that
# `emerge -V` prints, minus the -deb dialect suffix.
version-check:
	@script=$$(sed -n 's/^VERSION *= *"\(.*\)-deb"/\1/p' emerge); \
	changelog=$$(dpkg-parsechangelog -SVersion 2>/dev/null \
	             | sed 's/-[^-]*$$//'); \
	if [ -z "$$changelog" ]; then \
		echo "version-check: skipped (dpkg-parsechangelog unavailable)"; \
	elif [ "$$script" != "$$changelog" ]; then \
		echo "version-check: emerge says $$script-deb but"; \
		echo "               debian/changelog says $$changelog"; \
		exit 1; \
	else \
		echo "version-check: $$script, in step"; \
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
	mkdir -p dist
	set -e; ver=$$(dpkg-parsechangelog -SVersion); \
	arch=$$(dpkg-architecture -qDEB_HOST_ARCH); \
	moved=0; \
	for f in "../$(PACKAGE)_$${ver}_all.deb" \
	         "../$(PACKAGE)_$${ver}_$${arch}.deb" \
	         "../$(PACKAGE)_$${ver}_$${arch}.buildinfo" \
	         "../$(PACKAGE)_$${ver}_$${arch}.changes"; do \
		if [ -e "$$f" ]; then mv -f "$$f" dist/; moved=$$((moved + 1)); fi; \
	done; \
	if [ "$$moved" -eq 0 ]; then \
		echo "deb: dpkg-buildpackage produced nothing to collect"; exit 1; \
	fi
	@echo
	@ls -l dist/

clean:
	rm -rf dist debian/$(PACKAGE) debian/.debhelper debian/files \
	       debian/debhelper-build-stamp debian/*.substvars debian/*.log
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
