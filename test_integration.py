#!/usr/bin/env python3
#
# Copyright (C) 2026 Nabeel Sowan <nabeel@vibes.se>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at
# your option) any later version.
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end tests for the dpkg backend against a throwaway dpkg root.

test_emerge.py covers logic with fakes. This drives the real thing: real
.debs built with dpkg-deb, a real file:// repository, a real dpkg install
into a scratch root, and the real resolver deciding what to do. It is the
only place `merge`, `unmerge` and the download/SHA256 path actually run.

Nothing touches the system. dpkg is given --root and every path constant in
the module is repointed under a temporary directory, so this needs no
privileges -- `dpkg --force-not-root` is enough to unpack into a tree you
own. The whole file skips if that turns out not to work here.

Run with:  python3 -m unittest test_integration
"""

import hashlib
import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

# Every class here skips itself when the tool it drives is missing, so the
# suite runs on a machine that is not Debian. That is also how a whole
# capability goes silently untested: a skipped class reports OK exactly as
# loudly as a passing one, and CI installed gpgv but not gpg for a while, so
# the signature suite never ran there at all.
#
# Set EMERGE_TESTS_REQUIRE_ALL=1 -- CI does -- and the check below turns a
# missing capability into one clear failure naming it, instead of silence.
STRICT = bool(os.environ.get("EMERGE_TESTS_REQUIRE_ALL"))
SCRIPT = os.path.join(HERE, "emerge")
# dpkg wants start-stop-daemon and friends, which live in sbin
SBIN_PATH = os.environ.get("PATH", "") + ":/usr/sbin:/sbin"


# --- shared-module sentinel -------------------------------------------------
#
# `load()` gives each test a fresh copy of the script, but the modules that
# copy imports -- os, shutil, subprocess -- are the *same objects* the test
# process uses. A test that replaces an attribute on one of them and does not
# restore it corrupts everything that runs afterwards, in another file, with
# no hint of where it came from. It has happened: a shutil.which stub that
# found nothing leaked out of the world-seeding tests and made the entire
# gpgv suite fail, while each module passed on its own.
#
# Running both modules in one interpreter finds that, but costs a second full
# pass over a suite that drives real apt and takes over a minute. This costs
# nothing and says which module did it.
_WATCHED = ("os.listdir", "os.geteuid", "os.makedirs", "os.path.exists",
            "os.path.isfile", "os.path.isdir", "os.readlink", "os.stat",
            "shutil.which", "shutil.copy2", "shutil.rmtree",
            "subprocess.run", "subprocess.Popen", "subprocess.call")
_SNAPSHOT = {}


def _resolve(dotted):
	obj = {"os": os, "shutil": shutil, "subprocess": subprocess}[
	    dotted.split(".")[0]]
	for part in dotted.split(".")[1:]:
		obj = getattr(obj, part)
	return obj


def setUpModule():
	for name in _WATCHED:
		_SNAPSHOT[name] = _resolve(name)


def tearDownModule():
	changed = [name for name in _WATCHED if _resolve(name) is not _SNAPSHOT[name]]
	if changed:
		raise AssertionError(
		    "this module left shared standard-library functions patched: "
		    + ", ".join(changed)
		    + ". Capture the original *before* replacing it and restore it "
		      "with addCleanup, or the damage lands in whatever runs next.")


def _rootless_dpkg_works():
	"""Can we unpack into a directory we own, without being root?"""
	if not (shutil.which("dpkg") and shutil.which("dpkg-deb")):
		return False
	with tempfile.TemporaryDirectory() as d:
		admin = os.path.join(d, "root", "var", "lib", "dpkg")
		for sub in ("info", "updates", "triggers"):
			os.makedirs(os.path.join(admin, sub))
		for f in ("status", "available"):
			open(os.path.join(admin, f), "a").close()
		pkg = os.path.join(d, "probe")
		os.makedirs(os.path.join(pkg, "DEBIAN"))
		with open(os.path.join(pkg, "DEBIAN", "control"), "w") as f:
			f.write("Package: probe\nVersion: 1\nArchitecture: all\n"
			        "Maintainer: t <t@t>\nDescription: probe\n")
		deb = os.path.join(d, "probe.deb")
		env = {**os.environ, "PATH": SBIN_PATH}
		if subprocess.run(["dpkg-deb", "--build", "-Znone", pkg, deb],
		                  capture_output=True, env=env).returncode:
			return False
		return subprocess.run(
		    ["dpkg", f"--root={os.path.join(d, 'root')}", "--force-not-root",
		     f"--log={os.path.join(d, 'log')}", "--unpack", deb],
		    capture_output=True, env=env).returncode == 0


HAVE_DPKG_ROOT = _rootless_dpkg_works()


@unittest.skipUnless(HAVE_DPKG_ROOT, "rootless `dpkg --root` unavailable")
class DpkgBackendEndToEnd(unittest.TestCase):
	"""One ordered scenario: the state each step leaves is the next step's
    input, so this is a single test using subTest per assertion rather than
    a method per phase."""

	def setUp(self):
		self.dir = tempfile.mkdtemp(prefix="emerge-itest-")
		self.addCleanup(shutil.rmtree, self.dir, True)
		self.sysroot = os.path.join(self.dir, "sysroot")
		self.repo = os.path.join(self.dir, "repo")
		self.state = os.path.join(self.dir, "state")
		for sub in ("info", "updates", "triggers"):
			os.makedirs(os.path.join(self.sysroot, "var/lib/dpkg", sub))
		for f in ("status", "available"):
			open(os.path.join(self.sysroot, "var/lib/dpkg", f), "a").close()
		os.makedirs(self.repo)
		os.makedirs(self.state)
		self.env = {**os.environ, "PATH": SBIN_PATH}
		# emerge spawns dpkg itself and inherits the ambient environment, so
		# sbin has to be on PATH for the process, not just for our own calls
		original_path = os.environ.get("PATH", "")
		os.environ["PATH"] = SBIN_PATH
		self.addCleanup(os.environ.__setitem__, "PATH", original_path)
		self.m = self.load()
		self.quiet()

	# -- fixture -----------------------------------------------------------

	def sh(self, *cmd):
		return subprocess.run(cmd, capture_output=True, text=True,
		                      env=self.env)

	def make_deb(self, name, version, depends=None, pre_depends=None):
		d = os.path.join(self.dir, "build", f"{name}_{version}")
		shutil.rmtree(d, ignore_errors=True)
		os.makedirs(os.path.join(d, "DEBIAN"))
		os.makedirs(os.path.join(d, "usr/share/emtest"))
		ctl = [f"Package: {name}", f"Version: {version}",
		       "Architecture: all", "Maintainer: t <t@t>"]
		if pre_depends:
			ctl.append(f"Pre-Depends: {pre_depends}")
		if depends:
			ctl.append(f"Depends: {depends}")
		ctl.append(f"Description: test package {name}")
		with open(os.path.join(d, "DEBIAN", "control"), "w") as f:
			f.write("\n".join(ctl) + "\n")
		with open(os.path.join(d, f"usr/share/emtest/{name}.txt"), "w") as f:
			f.write(f"{name} {version}\n")
		out = os.path.join(self.repo, f"{name}_{version}_all.deb")
		r = self.sh("dpkg-deb", "--build", "-Znone", d, out)
		self.assertEqual(r.returncode, 0, r.stderr)
		return out

	def load(self):
		loader = importlib.machinery.SourceFileLoader("emerge_itest", SCRIPT)
		spec = importlib.util.spec_from_loader(loader.name, loader)
		m = importlib.util.module_from_spec(spec)
		loader.exec_module(m)

		m.LIB_DIR = os.path.join(self.state, "lib")
		m.TREE_DIR = os.path.join(self.state, "tree")
		m.WORLD = os.path.join(self.state, "lib", "world")
		m.DISTFILES = os.path.join(self.state, "distfiles")
		m.BINPKGS = os.path.join(self.state, "binpkgs")
		m.STATUS = os.path.join(self.sysroot, "var/lib/dpkg/status")
		m.need_root = lambda: None
		m.read_sources = lambda: [("file://" + self.repo, "./", ["main"],
		                           None)]

		log = os.path.join(self.state, "dpkg.log")
		real_capture, real_run = m.capture, m.run

		def inject(cmd):
			"""Point dpkg at the scratch root; leave other tools alone."""
			if (cmd and cmd[0] == "dpkg"
			        and cmd[1] != "--print-architecture"):
				return [cmd[0], f"--root={self.sysroot}", "--force-not-root",
				        f"--log={log}"] + list(cmd[1:])
			return cmd

		m.capture = lambda cmd, env=None: real_capture(inject(cmd), env=env)
		m.run = lambda cmd, **kw: real_run(inject(cmd), **kw)
		return m

	def installed(self):
		return {n: st["Version"]
		        for n, st in self.m.installed_state().items()}

	def world(self):
		"""The world file, or [] if it was never written. A missing file is
        a failure to report, not a traceback to blow up on."""
		try:
			with open(self.m.WORLD) as f:
				return f.read().split()
		except OSError:
			return []

	def merge(self, atoms, fetchonly=False, **kw):
		be = self.m.DpkgBackend()
		merges = be.resolve(atoms, **kw)
		be.merge(merges, atoms,
		         {"fetchonly": fetchonly, "oneshot": False})
		return merges

	def test_fetchonly_downloads_without_installing(self):
		"""`-f` had no coverage at all. Both test files mention `fetchonly`
        twenty-one times between them and every one passes it as False, so
        grepping for it suggests the flag is exercised while nothing runs
        it -- a green check that checks nothing, in the shape this project
        keeps finding.

        What it must do is download and then stop: the .deb in DISTFILES,
        nothing installed, and the world file untouched."""
		self.quiet()
		self.make_deb("emtest-fetch", "1.0")
		self.m.DpkgBackend().sync(verify=False)

		self.merge(["emtest-fetch"], fetchonly=True)

		with self.subTest("the archive was downloaded"):
			self.assertTrue(
			    [f for f in os.listdir(self.m.DISTFILES)
			     if f.startswith("emtest-fetch")],
			    f"nothing landed in {self.m.DISTFILES}")
		with self.subTest("nothing was installed"):
			self.assertNotIn("emtest-fetch", self.installed())
		with self.subTest("the world file was left alone"):
			self.assertNotIn("emtest-fetch", self.world())

		# and the same command without -f does install it, so the assertions
		# above are about the flag rather than about a broken fixture
		self.merge(["emtest-fetch"])
		self.assertEqual(self.installed().get("emtest-fetch"), "1.0")

	def quiet(self):
		"""Silence the Portage-style narration; failures still surface."""
		self.m.print = lambda *a, **k: None

	# -- the scenario ------------------------------------------------------

	def test_dpkg_backend_end_to_end(self):
		m = self.m
		self.make_deb("emtest-lib", "1.0")
		self.make_deb("emtest-lib", "2.0")
		self.make_deb("emtest-app", "1.0", depends="emtest-lib (>= 1.0)")
		self.make_deb("emtest-tool", "1.0", depends="emtest-lib (>= 2.0)")

		# -- sync: a bare directory of .debs is indexed on the fly ---------
		m.DpkgBackend().sync(verify=False)
		tree = os.listdir(m.TREE_DIR)
		with self.subTest("sync writes an index"):
			self.assertTrue(tree)
		with open(os.path.join(m.TREE_DIR, tree[0])) as f:
			index = f.read()
		with self.subTest("every .deb is indexed"):
			self.assertEqual(index.count("Package:"), 4)
		with self.subTest("index carries the hashes the merge will check"):
			self.assertIn("SHA256:", index)

		# -- merge: the dependency comes along -----------------------------
		merges = self.merge(["emtest-app"])
		with self.subTest("resolve pulls in the dependency"):
			self.assertEqual({x[0] for x in merges},
			                 {"emtest-app", "emtest-lib"})
		with self.subTest("both are installed"):
			self.assertEqual(self.installed().get("emtest-app"), "1.0")
			self.assertEqual(self.installed().get("emtest-lib"), "2.0")
		with self.subTest("payload reached the filesystem"):
			self.assertTrue(os.path.exists(
			    os.path.join(self.sysroot, "usr/share/emtest/emtest-app.txt")))
		with self.subTest("the atom is recorded in world"):
			self.assertIn("emtest-app", self.world())
		with self.subTest("the dependency is not"):
			self.assertNotIn("emtest-lib", self.world())

		# -- a corrupted .deb must not be installed ------------------------
		# The index recorded the good hash at sync time; corrupt the file it
		# points at. This is the entire trust chain on this backend.
		self.sh("dpkg", f"--root={self.sysroot}", "--force-not-root",
		        "-r", "emtest-app")
		shutil.rmtree(m.DISTFILES, ignore_errors=True)
		with open(os.path.join(self.repo, "emtest-app_1.0_all.deb"),
		          "ab") as f:
			f.write(b"tampered")
		with self.subTest("a corrupted .deb is refused"):
			with self.assertRaises(SystemExit):
				self.merge(["emtest-app"])
		with self.subTest("and is not installed"):
			self.assertNotIn("emtest-app", self.installed())
		self.make_deb("emtest-app", "1.0", depends="emtest-lib (>= 1.0)")
		shutil.rmtree(m.DISTFILES, ignore_errors=True)
		m.DpkgBackend().sync(verify=False)

		# -- an index entry with no hash must not pass silently -------------
		# Every real Packages file has SHA256, so its absence means the index
		# is not what we think it is. Installing anyway without saying so
		# would make the checksum look enforced when it is not.
		tree = os.path.join(m.TREE_DIR, os.listdir(m.TREE_DIR)[0])
		with open(tree) as f:
			stripped = "\n".join(l for l in f.read().splitlines()
			                     if not l.startswith("SHA256:"))
		with open(tree, "w") as f:
			f.write(stripped)
		warned = []
		real_warn, m.ewarn = m.ewarn, warned.append
		try:
			self.sh("dpkg", f"--root={self.sysroot}", "--force-not-root",
			        "-r", "emtest-app")
			shutil.rmtree(m.DISTFILES, ignore_errors=True)
			be = m.DpkgBackend()
			be.merge(be.resolve(["emtest-app"]), ["emtest-app"],
			         {"fetchonly": False, "oneshot": False})
		finally:
			m.ewarn = real_warn
		with self.subTest("an unchecked download is reported"):
			self.assertTrue(any("without a checksum" in w for w in warned),
			                f"no warning; got {warned}")
		m.DpkgBackend().sync(verify=False)          # restore the real index

		# -- --no-dep-upgrade against real installed state -----------------
		for pkg in ("emtest-app", "emtest-lib"):
			self.sh("dpkg", f"--root={self.sysroot}", "--force-not-root",
			        "-r", pkg)
		self.sh("dpkg", f"--root={self.sysroot}", "--force-not-root",
		        "--force-depends", "-i",
		        os.path.join(self.repo, "emtest-lib_1.0_all.deb"))
		with self.subTest("baseline: the dependency is held at 1.0"):
			self.assertEqual(self.installed().get("emtest-lib"), "1.0")

		be = m.DpkgBackend()
		plan = be.resolve(["emtest-app"], no_dep_upgrade=True)
		with self.subTest("an installed dependency that fits is left alone"):
			self.assertEqual([x[0] for x in plan], ["emtest-app"])

		with self.subTest("one that cannot fit is a wall"):
			with self.assertRaises(m.NduWall) as cm:
				m.DpkgBackend().resolve(["emtest-tool"], no_dep_upgrade=True)
			self.assertEqual([mv["name"] for mv in cm.exception.movers],
			                 ["emtest-lib"])

		plan = m.DpkgBackend().resolve(["emtest-tool"], no_dep_upgrade=True,
		                               allow={"emtest-lib"})
		with self.subTest("--with lets exactly that package move"):
			self.assertEqual({x[0] for x in plan},
			                 {"emtest-tool", "emtest-lib"})

		# -- unmerge and depclean ------------------------------------------
		self.merge(["emtest-app"])
		be = m.DpkgBackend()
		with self.subTest("unmerge finds the target"):
			self.assertEqual(
			    [r[0] for r in be.unmerge_candidates(
			        ["emtest-lib"], {"ask": False, "pretend": True})],
			    ["emtest-lib"])
		be.unmerge([("emtest-app", "1.0")])
		with self.subTest("the unmerged package is gone"):
			self.assertNotIn("emtest-app", self.installed())
		with self.subTest("its dependency stays behind"):
			self.assertIn("emtest-lib", self.installed())
		with self.subTest("and is then a depclean candidate"):
			self.assertIn("emtest-lib",
			              [c[0] for c in
			               m.DpkgBackend().depclean_candidates()])

	def test_a_new_pre_dependency_is_configured_before_its_dependent(self):
		"""dpkg refuses to *unpack* a package whose Pre-Depends is merely
        unpacked -- it must already be configured:

            emtest-front pre-depends on emtest-core
             emtest-core is unpacked, but has never been configured.

        Unpacking the whole plan and configuring once at the end therefore
        failed outright on any plan containing a new package that pre-depends
        on another new one. Nothing short of real dpkg shows this: the
        simulation, the resolver and the merge list are all perfectly happy.
        """
		self.make_deb("emtest-core", "1.0")
		self.make_deb("emtest-front", "1.0", pre_depends="emtest-core")
		self.m.DpkgBackend().sync(verify=False)

		self.merge(["emtest-front"])

		installed = self.installed()
		self.assertEqual(installed.get("emtest-core"), "1.0")
		self.assertEqual(installed.get("emtest-front"), "1.0",
		                 "the pre-dependent must actually be installed")
		self.assertTrue(os.path.exists(os.path.join(
		    self.sysroot, "usr/share/emtest/emtest-front.txt")))

	def test_a_pre_dependency_is_fully_configured_not_merely_unpacked(self):
		"""The half that makes the merge real rather than half-done: both
        packages must end up in state `installed`, not `unpacked`."""
		self.make_deb("emtest-core", "1.0")
		self.make_deb("emtest-front", "1.0", pre_depends="emtest-core")
		self.m.DpkgBackend().sync(verify=False)
		self.merge(["emtest-front"])

		with open(self.m.STATUS) as f:
			states = {st["Package"]: st.get("Status", "")
			          for st in self.m.parse_stanzas(f.read())}
		for pkg in ("emtest-core", "emtest-front"):
			with self.subTest(pkg=pkg):
				self.assertIn("install ok installed", states.get(pkg, ""))

	def test_unmerging_a_package_and_its_dependency_together_succeeds(self):
		"""dpkg orders removals itself when given the whole list, but refuses
        one at a time in the order the user typed:

            dpkg: dependency problems prevent removal of emtest-lib:
             emtest-app depends on emtest-lib.

        `emerge -C emtest-lib emtest-app` named the dependency first and died
        on the very first package."""
		self.make_deb("emtest-lib", "1.0")
		self.make_deb("emtest-app", "1.0", depends="emtest-lib")
		self.m.DpkgBackend().sync(verify=False)
		self.merge(["emtest-app"])
		self.assertIn("emtest-lib", self.installed())

		# deliberately the order that fails: dependency before dependent
		self.m.DpkgBackend().unmerge([("emtest-lib", "1.0"),
		                              ("emtest-app", "1.0")])

		left = self.installed()
		self.assertNotIn("emtest-lib", left)
		self.assertNotIn("emtest-app", left)


def _rootless_apt_works():
	"""apt can be pointed entirely at scratch directories, and told to hand
    dpkg a --root, so a whole install runs unprivileged."""
	if not (HAVE_DPKG_ROOT and shutil.which("apt-get")
	        and shutil.which("dpkg-scanpackages")):
		return False
	return True


HAVE_APT_ROOT = _rootless_apt_works()


@unittest.skipUnless(HAVE_APT_ROOT, "rootless apt-into-scratch-root "
                                    "unavailable")
class AptBackendEndToEnd(unittest.TestCase):
	"""The apt backend against a real apt driving a real dpkg, both aimed at
    a scratch tree. Every piece of apt state -- status, lists, cache, logs
    and extended_states -- is redirected, so this needs no privileges and
    leaves nothing behind. It is the only place AptBackend.merge, unmerge
    and --oneshot actually execute."""

	def setUp(self):
		self.dir = tempfile.mkdtemp(prefix="emerge-apt-itest-")
		self.addCleanup(shutil.rmtree, self.dir, True)
		# When apt runs as root it drops to APT::Sandbox::User (_apt) to
		# fetch, and mkdtemp gives 0700, so _apt cannot traverse the file://
		# repository. Invisible when the tests run as an ordinary user --
		# apt only sandboxes when it is root -- which is why this passed
		# locally and failed in a container.
		os.chmod(self.dir, 0o755)
		self.sysroot = os.path.join(self.dir, "sysroot")
		self.repo = os.path.join(self.dir, "repo")
		for sub in ("info", "updates", "triggers"):
			os.makedirs(os.path.join(self.sysroot, "var/lib/dpkg", sub))
		for f in ("status", "available"):
			open(os.path.join(self.sysroot, "var/lib/dpkg", f), "a").close()
		for d in ("repo", "lists/partial", "cache/archives/partial", "log",
		          "etc"):
			os.makedirs(os.path.join(self.dir, d), exist_ok=True)
		self.states = os.path.join(self.dir, "extended_states")
		open(self.states, "a").close()

		original_path = os.environ.get("PATH", "")
		os.environ["PATH"] = SBIN_PATH
		self.addCleanup(os.environ.__setitem__, "PATH", original_path)
		self.env = {**os.environ, "PATH": SBIN_PATH}

		self.sources = os.path.join(self.dir, "etc", "sources.list")
		with open(self.sources, "w") as f:
			f.write(f"deb [trusted=yes] file://{self.repo} ./\n")

		self.admin = os.path.join(self.sysroot, "var/lib/dpkg")
		self.apt_opts = [
		    "-o", f"Dir::State::status={self.admin}/status",
		    "-o", f"Dir::State::lists={self.dir}/lists",
		    "-o", f"Dir::State::extended_states={self.states}",
		    "-o", f"Dir::Cache={self.dir}/cache",
		    "-o", f"Dir::Log={self.dir}/log",
		    "-o", f"Dir::Etc::sourcelist={self.sources}",
		    "-o", "Dir::Etc::sourceparts=/dev/null",
		    "-o", "Dir::Etc::preferences=/dev/null",
		    "-o", "Dir::Etc::preferencesparts=/dev/null",
		    "-o", "Debug::NoLocking=1",
			# belt and braces with the chmod above: do not drop to _apt at all
		    "-o", "APT::Sandbox::User=root",
		    "-o", f"DPkg::Options::=--root={self.sysroot}",
		    "-o", "DPkg::Options::=--force-not-root",
		    "-o", f"DPkg::Options::=--log={self.dir}/dpkg.log",
		]

	# -- fixture -----------------------------------------------------------

	def sh(self, *cmd, **kw):
		return subprocess.run(cmd, capture_output=True, text=True,
		                      env=self.env, **kw)

	def make_deb(self, name, version, depends=None):
		d = os.path.join(self.dir, "build", f"{name}_{version}")
		shutil.rmtree(d, ignore_errors=True)
		os.makedirs(os.path.join(d, "DEBIAN"))
		os.makedirs(os.path.join(d, "usr/share/emtest"))
		ctl = [f"Package: {name}", f"Version: {version}",
		       "Architecture: all", "Maintainer: t <t@t>"]
		if depends:
			ctl.append(f"Depends: {depends}")
		ctl.append(f"Description: test package {name}")
		with open(os.path.join(d, "DEBIAN", "control"), "w") as f:
			f.write("\n".join(ctl) + "\n")
		with open(os.path.join(d, f"usr/share/emtest/{name}.txt"), "w") as f:
			f.write(f"{name} {version}\n")
		r = self.sh("dpkg-deb", "--build", "-Znone", d,
		            os.path.join(self.repo, f"{name}_{version}_all.deb"))
		self.assertEqual(r.returncode, 0, r.stderr)

	def publish(self):
		r = self.sh("dpkg-scanpackages", ".", "/dev/null", cwd=self.repo)
		self.assertEqual(r.returncode, 0, r.stderr)
		with open(os.path.join(self.repo, "Packages"), "w") as f:
			f.write(r.stdout)
		self.assertEqual(
		    self.sh("apt-get", *self.apt_opts, "update").returncode, 0)

	def load(self):
		loader = importlib.machinery.SourceFileLoader("emerge_apt_itest",
		                                              SCRIPT)
		spec = importlib.util.spec_from_loader(loader.name, loader)
		m = importlib.util.module_from_spec(spec)
		loader.exec_module(m)
		m.STATUS = os.path.join(self.admin, "status")
		m.need_root = lambda: None
		m.print = lambda *a, **k: None
		# never let session detection touch the real desktop
		m._session_critical_cache = set()
		m._session_blind = False

		opts, admin, sysroot = self.apt_opts, self.admin, self.sysroot
		real_capture, real_run, real_popen = (m.capture, m.run,
		                                      m.subprocess.Popen)

		def inject(cmd):
			cmd = list(cmd)
			if not cmd:
				return cmd
			if cmd[0] in ("apt-get", "apt-cache", "apt-mark"):
				return [cmd[0]] + opts + cmd[1:]
			if cmd[0] == "dpkg-query":
				return [cmd[0], f"--admindir={admin}"] + cmd[1:]
			if cmd[0] == "dpkg" and cmd[1] != "--print-architecture":
				return [cmd[0], f"--root={sysroot}",
				        "--force-not-root"] + cmd[1:]
			return cmd

		m.capture = lambda cmd, env=None: real_capture(inject(cmd), env=env)
		m.run = lambda cmd, **kw: real_run(inject(cmd), **kw)
		m.subprocess.Popen = lambda cmd, **kw: real_popen(inject(cmd), **kw)
		self.addCleanup(setattr, m.subprocess, "Popen", real_popen)
		return m

	def installed(self):
		return {n: st["Version"]
		        for n, st in self.m.installed_state().items()}

	def manual(self):
		return self.m.AptBackend()._manual_set()

	def merge(self, atoms, oneshot=False, **kw):
		be = self.m.AptBackend()
		merges = be.resolve(atoms, **kw)
		be.merge(merges, atoms, {"fetchonly": False, "oneshot": oneshot})
		# kept so a test can assert what apt was actually told, which is what
		# decides the manual-install marks
		self.last_backend = be
		return merges

	# -- the scenario ------------------------------------------------------

	def test_apt_backend_end_to_end(self):
		self.make_deb("emtest-lib", "1.0")
		self.make_deb("emtest-app", "1.0", depends="emtest-lib (>= 1.0)")
		self.publish()
		self.m = self.load()

		# -- merge ---------------------------------------------------------
		merges = self.merge(["emtest-app"])
		with self.subTest("resolve reports the dependency too"):
			self.assertEqual({x[0] for x in merges},
			                 {"emtest-app", "emtest-lib"})
		with self.subTest("both are installed"):
			self.assertEqual(self.installed().get("emtest-app"), "1.0")
			self.assertEqual(self.installed().get("emtest-lib"), "1.0")
		with self.subTest("payload reached the scratch filesystem"):
			self.assertTrue(os.path.exists(os.path.join(
			    self.sysroot, "usr/share/emtest/emtest-app.txt")))
		with self.subTest("the atom lands in @world"):
			self.assertIn("emtest-app", self.manual())
		with self.subTest("the dependency does not"):
			self.assertNotIn("emtest-lib", self.manual())

		# -- unmerge shows the cascade -------------------------------------
		be = self.m.AptBackend()
		removals = be.unmerge_candidates(["emtest-lib"],
		                                 {"ask": False, "pretend": True})
		with self.subTest("removing a dependency reports its dependents"):
			self.assertEqual({r[0] for r in removals},
			                 {"emtest-lib", "emtest-app"})

		be = self.m.AptBackend()
		be.unmerge(be.unmerge_candidates(["emtest-app"],
		                                 {"ask": False, "pretend": False}))
		with self.subTest("the package is really gone"):
			self.assertNotIn("emtest-app", self.installed())
		with self.subTest("its dependency stays"):
			self.assertIn("emtest-lib", self.installed())

		# -- depclean ------------------------------------------------------
		with self.subTest("the orphan is a depclean candidate"):
			self.assertIn("emtest-lib",
			              [c[0] for c in
			               self.m.AptBackend().depclean_candidates()])

		# -- --oneshot, end to end -----------------------------------------
		# apt marks everything it installs as manual; --oneshot has to put
		# that back. This is the step that could not be checked without an
		# install.
		self.sh("apt-get", *self.apt_opts, "-y", "remove", "emtest-lib")
		self.merge(["emtest-app"], oneshot=True)
		with self.subTest("--oneshot still installs"):
			self.assertEqual(self.installed().get("emtest-app"), "1.0")
		with self.subTest("--oneshot keeps the atom out of @world"):
			self.assertNotIn("emtest-app", self.manual())

		# and it must not evict something already there
		self.sh("apt-get", *self.apt_opts, "-y", "remove", "emtest-app")
		self.merge(["emtest-app"])
		self.assertIn("emtest-app", self.manual())
		self.merge(["emtest-app"], oneshot=True)
		with self.subTest("--oneshot never removes an existing world entry"):
			self.assertIn("emtest-app", self.manual())

		# -- --deselect ----------------------------------------------------
		# @selected on this backend *is* `apt-mark showmanual`, so this is a
		# real apt-mark auto against a real apt. The assertion that matters
		# is the last one: deselecting must not remove anything, which is the
		# whole difference between it and --unmerge.
		be = self.m.AptBackend()
		be.deselect(["emtest-app"], pretend=True)
		with self.subTest("--deselect --pretend changes nothing"):
			self.assertIn("emtest-app", self.manual())
		be.deselect(["emtest-app"], pretend=False)
		with self.subTest("--deselect drops it from @selected"):
			self.assertNotIn("emtest-app", self.manual())
		with self.subTest("--deselect leaves the package installed"):
			self.assertEqual(self.installed().get("emtest-app"), "1.0")

	def test_fetchonly_downloads_without_installing(self):
		"""The apt half of a flag neither backend had a test for. It is a
        different implementation -- `apt-get -y -d`, then `sys.exit` rather
        than a return -- so the dpkg test says nothing about it.

        The exit is part of the contract here: exiting non-zero on a failed
        download is how a script driving `-f` finds out."""
		self.make_deb("emtest-fetch", "1.0")
		self.publish()
		self.m = self.load()
		be = self.m.AptBackend()
		merges = be.resolve(["emtest-fetch"])

		seen = []
		real_run = self.m.run
		self.m.run = lambda cmd, **kw: (seen.append(list(cmd)),
		                                real_run(cmd, **kw))[1]
		with self.assertRaises(SystemExit) as exit:
			be.merge(merges, ["emtest-fetch"],
			         {"fetchonly": True, "oneshot": False})
		self.assertEqual(exit.exception.code, 0,
		                 "a successful fetch should exit 0")

		with self.subTest("nothing was installed"):
			self.assertNotIn("emtest-fetch", self.installed())
		with self.subTest("@selected was left alone"):
			self.assertNotIn("emtest-fetch", self.manual())
		with self.subTest("apt was told to download and stop"):
			# Asserted on the invocation rather than on apt's cache. This
			# repository is a file:// one, and apt uses a local archive
			# where it lies instead of copying it in -- it reports
			# "Download complete and in download only mode" and the cache
			# stays empty. The first version of this test looked for the
			# .deb there and was about to report a bug that is not there.
			self.assertTrue(any("-d" in cmd for cmd in seen),
			                f"no download-only invocation among {seen}")

	def test_no_dep_upgrade_does_not_drag_dependencies_into_world(self):
		"""The @world leak, against a real apt and a real apt-mark.

		--no-dep-upgrade resolves the closure itself and hands apt every
		package as an explicit `pkg=version` pin. apt marks everything named
		on its command line as manually installed -- apt-mark(8): "the
		package you installed explicitly is marked as manually installed" --
		and on this backend @selected *is* `apt-mark showmanual`. So one
		`emerge --no-dep-upgrade libsdl3-dev` moved 32 dependencies into
		@world, where --depclean could never reclaim them again.

		The unit tests cover this with a faked apt-mark. This is the same
		claim made against the real one, which is where it actually has to
		hold: the whole bug was a fact about apt's behaviour, not ours."""
		self.make_deb("emtest-lib", "1.0")
		self.make_deb("emtest-app", "1.0", depends="emtest-lib (>= 1.0)")
		self.publish()
		self.m = self.load()

		merges = self.merge(["emtest-app"], no_dep_upgrade=True)

		with self.subTest("the closure really was resolved and installed"):
			self.assertEqual({m[0] for m in merges},
			                 {"emtest-app", "emtest-lib"})
			self.assertEqual(self.installed().get("emtest-app"), "1.0")
			self.assertEqual(self.installed().get("emtest-lib"), "1.0")

		with self.subTest("apt was told about the dependency explicitly"):
			# The precondition for the bug, asserted rather than assumed: if
			# the dependency were never named on apt's command line, apt
			# would not have marked it manual, and the assertion below would
			# hold no matter what _apply_marks did.
			self.assertIn("emtest-lib",
			              [p.split("=")[0]
			               for p in self.last_backend._named_packages()])

		manual = self.manual()
		with self.subTest("the target the user asked for is in @world"):
			self.assertIn("emtest-app", manual)
		with self.subTest("the dependency is not"):
			self.assertNotIn("emtest-lib", manual)

		with self.subTest("so the dependency is reclaimable by depclean"):
			self.sh("apt-get", *self.apt_opts, "-y", "remove", "emtest-app")
			self.assertIn("emtest-lib",
			              [c[0] for c in
			               self.m.AptBackend().depclean_candidates()])


@unittest.skipUnless(HAVE_DPKG_ROOT, "rootless `dpkg --root` unavailable")
class AptlessHttpEndToEnd(unittest.TestCase):
	"""The dpkg backend as an apt-less box actually meets it.

	Three things nothing else covers, and none of them needed hardware:

	  - **No apt-get.** pick_backend() picks the dpkg backend by asking
	    shutil.which, so removing apt-get from its view is the whole of
	    "apt-less" as far as the program is concerned.
	  - **Real HTTP.** Every other test serves file://, which never exercises
	    urllib, the User-Agent, or a server that can 404. A local server on
	    127.0.0.1 is real HTTP without a network.
	  - **Compressed indexes.** sync() tries .xz, then .gz, then plain, and
	    the generated USB index is always plain -- so the decompression path,
	    which is what every real archive serves, had never run.

	And it drives `main()` rather than the backend methods, so argument
	parsing, backend selection, resolution, install and the world file run as
	one path for the first time."""

	def setUp(self):
		self.dir = tempfile.mkdtemp(prefix="emerge-http-itest-")
		self.addCleanup(shutil.rmtree, self.dir, True)
		self.sysroot = os.path.join(self.dir, "sysroot")
		self.repo = os.path.join(self.dir, "repo")
		self.state = os.path.join(self.dir, "state")
		for sub in ("info", "updates", "triggers"):
			os.makedirs(os.path.join(self.sysroot, "var/lib/dpkg", sub))
		for f in ("status", "available"):
			open(os.path.join(self.sysroot, "var/lib/dpkg", f), "a").close()
		os.makedirs(self.repo)
		os.makedirs(self.state)
		original_path = os.environ.get("PATH", "")
		os.environ["PATH"] = SBIN_PATH
		self.addCleanup(os.environ.__setitem__, "PATH", original_path)
		self.env = {**os.environ, "PATH": SBIN_PATH}
		self.base = self.serve(self.repo)
		self.m = self.load()

	# -- fixture -----------------------------------------------------------

	def serve(self, directory):
		"""A local HTTP server, so fetch() goes through urllib for real."""
		import http.server
		import threading

		class Quiet(http.server.SimpleHTTPRequestHandler):
			def log_message(self, *a):
				pass

		def handler(*args, **kw):
			return Quiet(*args, directory=directory, **kw)

		httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
		thread = threading.Thread(target=httpd.serve_forever, daemon=True)
		thread.start()
		# Cleanups run last-registered-first, so this reads bottom-up:
		# stop serving, wait for the thread, then close the listening socket.
		# Without the close the suite leaks a socket per test, which the
		# ResourceWarning CI step turns into a failure.
		self.addCleanup(httpd.server_close)
		self.addCleanup(thread.join, 5)
		self.addCleanup(httpd.shutdown)
		return f"http://127.0.0.1:{httpd.server_address[1]}"

	def load(self):
		loader = importlib.machinery.SourceFileLoader("emerge_http_itest",
		                                              SCRIPT)
		spec = importlib.util.spec_from_loader(loader.name, loader)
		m = importlib.util.module_from_spec(spec)
		loader.exec_module(m)
		m.LIB_DIR = os.path.join(self.state, "lib")
		m.TREE_DIR = os.path.join(self.state, "tree")
		m.WORLD = os.path.join(self.state, "lib", "world")
		m.DISTFILES = os.path.join(self.state, "distfiles")
		m.STATUS = os.path.join(self.sysroot, "var/lib/dpkg/status")
		m.need_root = lambda: None
		m.print = lambda *a, **k: None
		m.read_sources = lambda: [(self.base, "./", ["main"], None)]

		# The point of the class: no apt-get anywhere, so pick_backend has to
		# choose the dpkg backend on its own. shutil is a shared module
		# object, hence the restore.
		real_which = m.shutil.which
		self.addCleanup(setattr, m.shutil, "which", real_which)
		m.shutil.which = lambda n: None if n == "apt-get" else real_which(n)

		log = os.path.join(self.state, "dpkg.log")
		real_capture, real_run = m.capture, m.run

		def inject(cmd):
			if (cmd and cmd[0] == "dpkg"
			        and cmd[1] != "--print-architecture"):
				return [cmd[0], f"--root={self.sysroot}", "--force-not-root",
				        f"--log={log}"] + list(cmd[1:])
			return cmd
		m.capture = lambda cmd, env=None: real_capture(inject(cmd), env=env)
		m.run = lambda cmd, **kw: real_run(inject(cmd), **kw)
		return m

	def make_deb(self, name, version, depends=None):
		d = os.path.join(self.dir, "build", f"{name}_{version}")
		shutil.rmtree(d, ignore_errors=True)
		os.makedirs(os.path.join(d, "DEBIAN"))
		os.makedirs(os.path.join(d, "usr/share/emtest"))
		ctl = [f"Package: {name}", f"Version: {version}",
		       "Architecture: all", "Maintainer: t <t@t>"]
		if depends:
			ctl.append(f"Depends: {depends}")
		ctl.append(f"Description: test package {name}")
		with open(os.path.join(d, "DEBIAN", "control"), "w") as f:
			f.write("\n".join(ctl) + "\n")
		with open(os.path.join(d, f"usr/share/emtest/{name}.txt"), "w") as f:
			f.write(f"{name} {version}\n")
		fn = f"{name}_{version}_all.deb"
		r = subprocess.run(["dpkg-deb", "--build", "-Znone", d,
		                    os.path.join(self.repo, fn)],
		                   capture_output=True, text=True, env=self.env)
		self.assertEqual(r.returncode, 0, r.stderr)
		return fn

	def write_index(self, filenames, compression=None):
		"""A Packages index for these .debs, optionally compressed.

		Only one form is written, so a test that asks for .gz proves the
		decompression ran rather than that a plain fallback was found."""
		stanzas = []
		for fn in filenames:
			path = os.path.join(self.repo, fn)
			r = subprocess.run(["dpkg-deb", "-f", path], capture_output=True,
			                   text=True, env=self.env)
			self.assertEqual(r.returncode, 0, r.stderr)
			with open(path, "rb") as f:
				blob = f.read()
			stanzas.append(r.stdout.rstrip("\n")
			               + f"\nFilename: {fn}"
			               + f"\nSize: {len(blob)}"
			               + f"\nSHA256: {hashlib.sha256(blob).hexdigest()}")
		raw = ("\n\n".join(stanzas) + "\n").encode()
		for stale in ("Packages", "Packages.gz", "Packages.xz"):
			try:
				os.unlink(os.path.join(self.repo, stale))
			except OSError:
				pass
		if compression == "gz":
			import gzip as _gz
			name, payload = "Packages.gz", _gz.compress(raw)
		elif compression == "xz":
			import lzma as _xz
			name, payload = "Packages.xz", _xz.compress(raw)
		else:
			name, payload = "Packages", raw
		with open(os.path.join(self.repo, name), "wb") as f:
			f.write(payload)
		return raw

	def installed(self):
		return {n: st["Version"]
		        for n, st in self.m.installed_state().items()}

	def tree_text(self):
		files = os.listdir(self.m.TREE_DIR)
		self.assertTrue(files, "sync wrote no index")
		with open(os.path.join(self.m.TREE_DIR, files[0])) as f:
			return f.read()

	def world(self):
		try:
			with open(self.m.WORLD) as f:
				return f.read().split()
		except OSError:
			return []

	# -- the sync half -----------------------------------------------------

	def test_a_gzipped_index_is_fetched_over_http_and_decompressed(self):
		self.make_deb("emtest-http", "1.0")
		raw = self.write_index(["emtest-http_1.0_all.deb"], compression="gz")
		self.m.main(["--sync"])
		self.assertEqual(self.tree_text(), raw.decode(),
		                 "the stored index is not the decompressed original")

	def test_an_xz_index_is_preferred_when_present(self):
		"""emerge guards its lzma import -- minimal Python builds ship
		without it -- so skip where the module under test also would."""
		if self.m.lzma is None:
			self.skipTest("this Python has no lzma, as emerge allows for")
		self.make_deb("emtest-http", "1.0")
		raw = self.write_index(["emtest-http_1.0_all.deb"], compression="xz")
		self.m.main(["--sync"])
		self.assertEqual(self.tree_text(), raw.decode())

	def test_a_plain_index_still_works_as_the_last_resort(self):
		self.make_deb("emtest-http", "1.0")
		raw = self.write_index(["emtest-http_1.0_all.deb"])
		self.m.main(["--sync"])
		self.assertEqual(self.tree_text(), raw.decode())

	def test_a_repository_that_serves_nothing_is_reported(self):
		"""404 on every candidate. The sync must say so rather than write an
		empty index and let the resolver blame the user's atom."""
		self.m.main(["--sync"])
		self.assertFalse(os.listdir(self.m.TREE_DIR))

	# -- the whole CLI path ------------------------------------------------

	def test_without_apt_get_the_dpkg_backend_is_chosen(self):
		self.assertEqual(self.m.pick_backend(None).name, "dpkg")

	def test_install_through_main_over_http(self):
		"""Argument parsing, backend selection, resolution, the HTTP fetch of
		the .deb, its SHA256 check, the dpkg install and the world file --
		one path, the way a user runs it."""
		self.make_deb("emtest-lib", "1.0")
		self.make_deb("emtest-http", "1.0", depends="emtest-lib")
		self.write_index(["emtest-lib_1.0_all.deb",
		                  "emtest-http_1.0_all.deb"], compression="gz")
		self.m.main(["--sync"])
		self.m.main(["emtest-http"])

		installed = self.installed()
		self.assertEqual(installed.get("emtest-http"), "1.0")
		self.assertEqual(installed.get("emtest-lib"), "1.0",
		                 "the dependency was not pulled in")
		self.assertTrue(os.path.exists(os.path.join(
		    self.sysroot, "usr/share/emtest/emtest-http.txt")))
		self.assertIn("emtest-http", self.world())
		self.assertNotIn("emtest-lib", self.world(),
		                 "a dependency must not enter world")

	def test_pretend_through_main_installs_nothing(self):
		self.make_deb("emtest-http", "1.0")
		self.write_index(["emtest-http_1.0_all.deb"], compression="gz")
		self.m.main(["--sync"])
		self.m.main(["-p", "emtest-http"])
		self.assertNotIn("emtest-http", self.installed())

	def test_oneshot_through_main_keeps_it_out_of_world(self):
		self.make_deb("emtest-http", "1.0")
		self.write_index(["emtest-http_1.0_all.deb"], compression="gz")
		self.m.main(["--sync"])
		self.m.main(["-1", "emtest-http"])
		self.assertEqual(self.installed().get("emtest-http"), "1.0")
		self.assertEqual(self.world(), [])

	def test_unmerge_through_main(self):
		self.make_deb("emtest-http", "1.0")
		self.write_index(["emtest-http_1.0_all.deb"], compression="gz")
		self.m.main(["--sync"])
		self.m.main(["emtest-http"])
		self.m.main(["-C", "emtest-http"])
		self.assertNotIn("emtest-http", self.installed())
		self.assertEqual(self.world(), [])

	def test_a_corrupt_deb_served_over_http_is_refused(self):
		"""The SHA256 in the index is the whole trust chain once the index
		itself is trusted, and it has to hold over HTTP too."""
		self.make_deb("emtest-http", "1.0")
		self.write_index(["emtest-http_1.0_all.deb"], compression="gz")
		self.m.main(["--sync"])
		with open(os.path.join(self.repo, "emtest-http_1.0_all.deb"),
		          "ab") as f:
			f.write(b"tampered")
		with self.assertRaises(SystemExit):
			self.m.main(["emtest-http"])
		self.assertNotIn("emtest-http", self.installed())


@unittest.skipUnless(HAVE_DPKG_ROOT, "rootless `dpkg --root` unavailable")
class ConfigMergingEndToEnd(unittest.TestCase):
	"""The config-merging feature against real dpkg, from both ends.

	Everything in this feature rests on one assumption about dpkg that
	test_emerge.py cannot check, because it hand-places the parked files it
	then reasons about: that `dpkg --force-confold --force-confdef` parks the
	incoming version as `<file>.dpkg-dist` when you have edited a conffile,
	and silently replaces it when you have not.

	If that were wrong -- or conditional on something we do not set -- the
	whole of dispatch-conf would be dead code that never fires, and every
	unit test would still pass. So it is proven here first, and then the
	round trip is run on top of it: archive at install time, edit, upgrade,
	3-way merge against the archived ancestor.

	The archive timing is the part that has been wrong before. Archiving the
	*incoming* version instead of the settled one makes new == ancestor, so
	the merge sees no incoming change and every update is silently
	discarded."""

	CONFFILE = "/etc/emtest.conf"

	def setUp(self):
		self.dir = tempfile.mkdtemp(prefix="emerge-cfg-itest-")
		self.addCleanup(shutil.rmtree, self.dir, True)
		self.sysroot = os.path.join(self.dir, "sysroot")
		self.repo = os.path.join(self.dir, "repo")
		for sub in ("info", "updates", "triggers"):
			os.makedirs(os.path.join(self.sysroot, "var/lib/dpkg", sub))
		for f in ("status", "available"):
			open(os.path.join(self.sysroot, "var/lib/dpkg", f), "a").close()
		os.makedirs(os.path.join(self.sysroot, "etc"))
		os.makedirs(self.repo)
		original_path = os.environ.get("PATH", "")
		os.environ["PATH"] = SBIN_PATH
		self.addCleanup(os.environ.__setitem__, "PATH", original_path)
		self.env = {**os.environ, "PATH": SBIN_PATH}
		self.m = self.load()
		self.etc = os.path.join(self.sysroot, "etc")
		self.conf = dict(self.m.DEFAULT_CONF)
		self.conf["config-protect"] = self.etc
		self.conf["archive-dir"] = os.path.join(self.dir, "archive")

	# -- fixture -----------------------------------------------------------

	def load(self):
		loader = importlib.machinery.SourceFileLoader("emerge_cfg_itest",
		                                              SCRIPT)
		spec = importlib.util.spec_from_loader(loader.name, loader)
		m = importlib.util.module_from_spec(spec)
		loader.exec_module(m)
		m.STATUS = os.path.join(self.sysroot, "var/lib/dpkg/status")
		m.need_root = lambda: None
		m.print = lambda *a, **k: None
		admin = os.path.join(self.sysroot, "var/lib/dpkg")
		real_capture = m.capture

		def inject(cmd):
			if cmd and cmd[0] == "dpkg-query":
				return [cmd[0], f"--admindir={admin}"] + list(cmd[1:])
			return cmd
		m.capture = lambda cmd, env=None: real_capture(inject(cmd), env=env)
		return m

	def build(self, version, body):
		d = os.path.join(self.dir, "build", version)
		shutil.rmtree(d, ignore_errors=True)
		os.makedirs(os.path.join(d, "DEBIAN"))
		os.makedirs(os.path.join(d, "etc"))
		with open(os.path.join(d, "DEBIAN", "control"), "w") as f:
			f.write(f"Package: emtest-cfg\nVersion: {version}\n"
			        f"Architecture: all\nMaintainer: t <t@t>\n"
			        f"Description: config test\n")
		with open(os.path.join(d, "DEBIAN", "conffiles"), "w") as f:
			f.write(self.CONFFILE + "\n")
		with open(os.path.join(d, "etc", "emtest.conf"), "w") as f:
			f.write(body)
		deb = os.path.join(self.repo, f"emtest-cfg_{version}.deb")
		r = subprocess.run(["dpkg-deb", "--build", "-Znone", d, deb],
		                   capture_output=True, text=True, env=self.env)
		self.assertEqual(r.returncode, 0, r.stderr)
		return deb

	def dpkg_install(self, deb):
		"""Exactly the flags DpkgBackend.merge uses."""
		r = subprocess.run(
		    ["dpkg", f"--root={self.sysroot}", "--force-not-root",
		     f"--log={self.dir}/dpkg.log", "--force-confold",
		     "--force-confdef", "-i", deb],
		    capture_output=True, text=True, env=self.env)
		self.assertEqual(r.returncode, 0, r.stderr)

	def target(self):
		return os.path.join(self.etc, "emtest.conf")

	def archiver_sees_the_scratch_tree(self):
		"""Point conffiles_of at where the file really is.

		dpkg records conffile paths relative to `/`, and reports
		`/etc/emtest.conf` no matter what `--root` it was given. Everything
		downstream of it -- archive_settled, scan_pending, ancestor_for --
		then works in absolute paths, which on a real system is correct and
		here is the host's own /etc. Substituting the scratch path is the
		only untruthful line in this class, and it is the offset `--root`
		introduced rather than anything the code does.

		The filtering archive_settled performs is still the real thing,
		operating on files real dpkg really produced."""
		path = self.target()
		self.m.conffiles_of = lambda pkgs, **kw: {p: [path] for p in pkgs}

	def read(self, path):
		with open(path) as f:
			return f.read()

	def write(self, path, text):
		with open(path, "w") as f:
			f.write(text)

	# -- the assumption the whole feature rests on -------------------------

	def test_dpkg_parks_an_edited_conffile_under_our_flags(self):
		self.dpkg_install(self.build("1.0", "setting = original\nshared = keep\n"))
		self.write(self.target(), "setting = MY EDIT\nshared = keep\n")
		self.dpkg_install(self.build(
		    "2.0", "setting = original\nshared = keep\nnewkey = added\n"))

		parked = self.target() + ".dpkg-dist"
		self.assertTrue(os.path.exists(parked),
		                "dpkg did not park the update; dispatch-conf would "
		                "never have anything to do")
		self.assertEqual(self.read(self.target()),
		                 "setting = MY EDIT\nshared = keep\n",
		                 "the edit must survive the upgrade")
		self.assertIn("newkey = added", self.read(parked))

	def test_an_untouched_conffile_is_replaced_with_no_parked_copy(self):
		"""The other half, and what archive_settled's timing depends on: if
		nothing is parked, what is on disk *is* the as-shipped version and is
		safe to record as the next ancestor."""
		self.dpkg_install(self.build("1.0", "setting = original\n"))
		self.dpkg_install(self.build("2.0", "setting = updated\n"))
		self.assertFalse(os.path.exists(self.target() + ".dpkg-dist"))
		self.assertEqual(self.read(self.target()), "setting = updated\n")

	def test_dpkg_reports_the_conffile_the_archiver_asks_about(self):
		"""conffiles_of drives the archiver off dpkg's own database, so it
		has to agree with what dpkg actually recorded."""
		self.dpkg_install(self.build("1.0", "setting = original\n"))
		self.assertEqual(self.m.conffiles_of(["emtest-cfg"]),
		                 {"emtest-cfg": [self.CONFFILE]})

	# -- the round trip ----------------------------------------------------

	def test_archive_then_merge_keeps_both_sides_of_the_change(self):
		"""The whole feature, end to end, with real dpkg doing the parking.

		v1 is archived at install time. The user edits one line. v2 changes a
		different line. The 3-way merge against the archived v1 must keep the
		user's edit *and* take the new key -- which is exactly what dpkg
		alone cannot do, and the reason this exists."""
		self.archiver_sees_the_scratch_tree()
		self.dpkg_install(self.build(
		    "1.0", "setting = original\nshared = keep\n"))
		# what merge() does after a successful install
		self.assertEqual(self.m.archive_settled(self.conf, ["emtest-cfg"]), 1)

		self.write(self.target(), "setting = MY EDIT\nshared = keep\n")
		self.dpkg_install(self.build(
		    "2.0", "setting = original\nshared = keep\nnewkey = added\n"))
		# the parked file must not become the ancestor
		self.assertEqual(self.m.archive_settled(self.conf, ["emtest-cfg"]), 0,
		                 "a parked conffile must be skipped by the archiver")
		archived = self.m.archive_path(self.conf, self.target())
		self.assertEqual(self.read(archived),
		                 "setting = original\nshared = keep\n",
		                 "the ancestor must still be the previously shipped "
		                 "version, not the incoming one")

		pending = self.m.scan_pending(self.conf)
		self.assertEqual([p for p, _ in pending], [self.target()])

		base, src = self.m.ancestor_for(self.conf, self.target())
		self.assertIsNotNone(base, "no ancestor found to merge against")
		merged, conflicts = self.m.merge3(
		    base,
		    self.m.read_lines(self.target()),
		    self.m.read_lines(self.target() + ".dpkg-dist"))
		self.assertEqual(conflicts, 0,
		                 "edits on different lines must merge cleanly")
		text = "".join(merged)
		self.assertIn("setting = MY EDIT", text, "the user's edit was lost")
		self.assertIn("newkey = added", text, "the update was lost")

	def test_a_genuine_conflict_is_not_merged_silently(self):
		"""Both sides changed the same line. This is the case that has to
		reach the user rather than be resolved by guessing."""
		self.archiver_sees_the_scratch_tree()
		self.dpkg_install(self.build("1.0", "setting = original\n"))
		self.m.archive_settled(self.conf, ["emtest-cfg"])
		self.write(self.target(), "setting = mine\n")
		self.dpkg_install(self.build("2.0", "setting = theirs\n"))

		base, _ = self.m.ancestor_for(self.conf, self.target())
		merged, conflicts = self.m.merge3(
		    base,
		    self.m.read_lines(self.target()),
		    self.m.read_lines(self.target() + ".dpkg-dist"))
		self.assertEqual(conflicts, 1)
		self.assertIn("<<<<<<<", "".join(merged))

	def test_the_archive_is_refreshed_once_the_update_is_accepted(self):
		"""After review the parked file becomes the next ancestor, whatever
		the user chose to keep on disk. Getting this wrong makes the next
		upgrade merge against a version two releases old."""
		self.archiver_sees_the_scratch_tree()
		self.dpkg_install(self.build("1.0", "setting = original\n"))
		self.m.archive_settled(self.conf, ["emtest-cfg"])
		self.write(self.target(), "setting = mine\n")
		self.dpkg_install(self.build("2.0", "setting = v2\n"))

		parked = self.target() + ".dpkg-dist"
		self.m._retire(self.conf, self.target(), parked)
		self.assertFalse(os.path.exists(parked), "the parked file must go")
		self.assertEqual(
		    self.read(self.m.archive_path(self.conf, self.target())),
		    "setting = v2\n")


def _source_build_works():
	if not (HAVE_DPKG_ROOT and shutil.which("apt-get")
	        and shutil.which("dpkg-source")
	        and shutil.which("dpkg-scansources")
	        and shutil.which("dpkg-buildpackage")):
		return False
	# `apt-get build-dep` pulls build-essential in implicitly, whatever the
	# source package's own Build-Depends say. Without it installed there is
	# nothing to satisfy that from -- the test repository carries sources
	# only -- and build-dep fails with "you have held broken packages",
	# which reads as a bug in resolve_source rather than a missing package.
	# Found by running the suite in a debian:trixie container, where it is
	# absent; a developer box has it and never sees this.
	r = subprocess.run(["dpkg-query", "-W", "-f=${Status}", "build-essential"],
	                   capture_output=True, text=True)
	return r.stdout.startswith("install ok installed")


HAVE_SOURCE_BUILD = _source_build_works()


@unittest.skipUnless(HAVE_SOURCE_BUILD, "source-build tooling unavailable")
class SourceBuildEndToEnd(unittest.TestCase):
	"""`emerge -b` / `-B`, which had never executed.

	The README and the man page both advertise building from the Debian
	source package, and `resolve_source`, `_src_version` and `_build_use` had
	no test of any kind -- the same shape as dispatch-conf before its premise
	was checked: a documented headline feature that could have been dead and
	nothing would have said so.

	It needs a real source package, so this makes one: a minimal native
	package with a hand-written `debian/rules` that produces a .deb with
	`dpkg-deb` directly. No debhelper, deliberately -- it keeps the build to
	about a second and removes a dependency the tests would otherwise need.

	`apt-get build-dep` runs for real. `Build-Depends: dpkg-dev` is satisfied
	on any machine that can run these tests at all, so it resolves to no
	action; apt is still pointed at the scratch tree in case that ever stops
	being true."""

	SRC = "emtest-src"

	def setUp(self):
		self.dir = tempfile.mkdtemp(prefix="emerge-src-itest-")
		self.addCleanup(shutil.rmtree, self.dir, True)
		os.chmod(self.dir, 0o755)
		self.sysroot = os.path.join(self.dir, "sysroot")
		self.repo = os.path.join(self.dir, "repo")
		for sub in ("info", "updates", "triggers"):
			os.makedirs(os.path.join(self.sysroot, "var/lib/dpkg", sub))
		for f in ("status", "available"):
			open(os.path.join(self.sysroot, "var/lib/dpkg", f), "a").close()
		for d in ("repo", "lists/partial", "cache/archives/partial", "log",
		          "etc"):
			os.makedirs(os.path.join(self.dir, d), exist_ok=True)
		original_path = os.environ.get("PATH", "")
		os.environ["PATH"] = SBIN_PATH
		self.addCleanup(os.environ.__setitem__, "PATH", original_path)
		self.env = {**os.environ, "PATH": SBIN_PATH}

		self.sources = os.path.join(self.dir, "etc", "sources.list")
		with open(self.sources, "w") as f:
			f.write(f"deb-src [trusted=yes] file://{self.repo} ./\n")
		# The host's real dpkg status, read-only: build-dep has to see what is
		# actually installed or it would try to install dpkg-dev's entire
		# tree from a repository that has no binaries in it.
		self.states = os.path.join(self.dir, "extended_states")
		open(self.states, "a").close()
		self.apt_opts = [
		    "-o", f"Dir::State::lists={self.dir}/lists",
		    # apt writes the auto/manual marks at the end of every install,
		    # and fails the whole run if it cannot -- after the package is
		    # already unpacked and configured, which reads as a build failure
		    "-o", f"Dir::State::extended_states={self.states}",
		    "-o", f"Dir::Cache={self.dir}/cache",
		    "-o", f"Dir::Log={self.dir}/log",
		    "-o", f"Dir::Etc::sourcelist={self.sources}",
		    "-o", "Dir::Etc::sourceparts=/dev/null",
		    "-o", "Dir::Etc::preferences=/dev/null",
		    "-o", "Dir::Etc::preferencesparts=/dev/null",
		    "-o", "Debug::NoLocking=1",
		    "-o", "APT::Sandbox::User=root",
		    "-o", f"DPkg::Options::=--root={self.sysroot}",
		    "-o", "DPkg::Options::=--force-not-root",
		    "-o", f"DPkg::Options::=--log={self.dir}/dpkg.log",
		]
		self.m = self.load()

	# -- fixture -----------------------------------------------------------

	def sh(self, *cmd, **kw):
		return subprocess.run(cmd, capture_output=True, text=True,
		                      env=self.env, **kw)

	RULES = """#!/usr/bin/make -f
D = debian/emtest-src
clean:
\trm -rf $(D) debian/files debian/*.substvars
build build-arch build-indep:
binary-indep:
\tmkdir -p $(D)/DEBIAN $(D)/usr/share/emtest-src
\techo built-from-source > $(D)/usr/share/emtest-src/marker
\tdpkg-gencontrol -pemtest-src -P$(D)
\tdpkg-deb --build -Znone $(D) ..
binary: binary-indep
.PHONY: clean build build-arch build-indep binary binary-indep
"""

	def publish_source(self, version="1.0"):
		src = os.path.join(self.dir, "src", f"{self.SRC}-{version}")
		shutil.rmtree(os.path.join(self.dir, "src"), ignore_errors=True)
		os.makedirs(os.path.join(src, "debian", "source"))
		with open(os.path.join(src, "debian", "control"), "w") as f:
			f.write(f"Source: {self.SRC}\nSection: misc\nPriority: optional\n"
			        f"Maintainer: t <t@t>\nBuild-Depends: dpkg-dev\n"
			        f"Standards-Version: 4.7.0\n\n"
			        f"Package: {self.SRC}\nArchitecture: all\n"
			        f"Description: minimal source-built test package\n"
			        f" One line.\n")
		with open(os.path.join(src, "debian", "changelog"), "w") as f:
			f.write(f"{self.SRC} ({version}) unstable; urgency=medium\n\n"
			        f"  * Initial.\n\n"
			        f" -- t <t@t>  Tue, 04 Aug 2026 12:00:00 +0200\n")
		with open(os.path.join(src, "debian", "source", "format"), "w") as f:
			f.write("3.0 (native)\n")
		rules = os.path.join(src, "debian", "rules")
		with open(rules, "w") as f:
			f.write(self.RULES)
		os.chmod(rules, 0o755)

		r = self.sh("dpkg-source", "-b", f"{self.SRC}-{version}",
		            cwd=os.path.join(self.dir, "src"))
		self.assertEqual(r.returncode, 0, r.stderr)
		for fn in os.listdir(os.path.join(self.dir, "src")):
			if fn.endswith((".dsc", ".tar.xz", ".tar.gz")):
				shutil.copy2(os.path.join(self.dir, "src", fn), self.repo)
		r = self.sh("dpkg-scansources", ".", cwd=self.repo)
		self.assertEqual(r.returncode, 0, r.stderr)
		with open(os.path.join(self.repo, "Sources"), "w") as f:
			f.write(r.stdout)
		self.assertEqual(
		    self.sh("apt-get", *self.apt_opts, "update").returncode, 0)

	def load(self):
		loader = importlib.machinery.SourceFileLoader("emerge_src_itest",
		                                              SCRIPT)
		spec = importlib.util.spec_from_loader(loader.name, loader)
		m = importlib.util.module_from_spec(spec)
		loader.exec_module(m)
		m.STATUS = os.path.join(self.sysroot, "var/lib/dpkg/status")
		m.BINPKGS = os.path.join(self.dir, "binpkgs")
		m.PORTAGE_TMPDIR = os.path.join(self.dir, "portage")
		m.need_root = lambda: None
		m.print = lambda *a, **k: None
		m._session_critical_cache = set()
		m._session_blind = False

		opts, sysroot = self.apt_opts, self.sysroot
		admin = os.path.join(sysroot, "var/lib/dpkg")
		real_capture, real_run, real_popen = (m.capture, m.run,
		                                      m.subprocess.Popen)

		def inject(cmd):
			cmd = list(cmd)
			if not cmd:
				return cmd
			if cmd[0] in ("apt-get", "apt-cache", "apt-mark"):
				return [cmd[0]] + opts + cmd[1:]
			if cmd[0] == "dpkg-query":
				return [cmd[0], f"--admindir={admin}"] + cmd[1:]
			if cmd[0] == "dpkg" and cmd[1] != "--print-architecture":
				return [cmd[0], f"--root={sysroot}",
				        "--force-not-root"] + cmd[1:]
			return cmd

		m.capture = lambda cmd, env=None: real_capture(inject(cmd), env=env)
		m.run = lambda cmd, **kw: real_run(inject(cmd), **kw)
		m.subprocess.Popen = lambda cmd, **kw: real_popen(inject(cmd), **kw)
		self.addCleanup(setattr, m.subprocess, "Popen", real_popen)
		return m

	def installed(self):
		return {n: st["Version"]
		        for n, st in self.m.installed_state().items()}

	def opts(self, **kw):
		o = {"buildpkgonly": False, "oneshot": False, "fetchonly": False}
		o.update(kw)
		return o

	# -- planning ----------------------------------------------------------

	def test_the_source_version_is_found(self):
		self.publish_source("1.0")
		self.assertEqual(self.m.AptBackend()._src_version(self.SRC), "1.0")

	def test_a_missing_source_package_is_reported_not_guessed(self):
		self.publish_source("1.0")
		be = self.m.AptBackend()
		with self.assertRaises(RuntimeError) as cm:
			be.resolve_source(["no-such-source"], self.opts())
		self.assertIn("no source package found", str(cm.exception))

	def test_resolve_source_plans_a_local_version(self):
		"""The built version is suffixed +local1 so a later @world upgrade
		will not silently clobber it -- vercmp puts it above the archive's
		1.0, which is the whole point."""
		self.publish_source("1.0")
		merges = self.m.AptBackend().resolve_source([self.SRC], self.opts())
		names = {m[0]: m for m in merges}
		self.assertIn(self.SRC, names)
		self.assertEqual(names[self.SRC][1], "1.0+local1")
		self.assertGreater(self.m.vercmp("1.0+local1", "1.0"), 0)

	def test_the_plan_carries_deb_build_options_as_use_flags(self):
		self.publish_source("1.0")
		merges = self.m.AptBackend().resolve_source([self.SRC], self.opts())
		use = [m[5] for m in merges if m[0] == self.SRC][0]
		self.assertIn("nocheck", use)

	# -- building ----------------------------------------------------------

	def test_buildpkgonly_produces_a_deb_and_installs_nothing(self):
		self.publish_source("1.0")
		be = self.m.AptBackend()
		be.resolve_source([self.SRC], self.opts(buildpkgonly=True))
		be.build(self.opts(buildpkgonly=True))

		products = os.listdir(self.m.BINPKGS)
		self.assertTrue(any(p.startswith(f"{self.SRC}_1.0+local1_")
		                    and p.endswith(".deb") for p in products),
		                f"no +local1 .deb in PKGDIR: {products}")
		self.assertNotIn(self.SRC, self.installed(),
		                 "-B must not install what it builds")

	def test_buildpkg_installs_what_it_built(self):
		self.publish_source("1.0")
		be = self.m.AptBackend()
		be.resolve_source([self.SRC], self.opts())
		be.build(self.opts())

		self.assertEqual(self.installed().get(self.SRC), "1.0+local1",
		                 "the installed version must be the local build")
		marker = os.path.join(self.sysroot,
		                      "usr/share/emtest-src/marker")
		self.assertTrue(os.path.exists(marker),
		                "the built payload never reached the filesystem")
		with open(marker) as f:
			self.assertEqual(f.read().strip(), "built-from-source")

	def test_the_build_happens_under_portage_tmpdir(self):
		"""It used to be written inline as /var/tmp/portage, which is the one
		path the source builder writes to and the only one that could not be
		repointed."""
		self.publish_source("1.0")
		be = self.m.AptBackend()
		be.resolve_source([self.SRC], self.opts(buildpkgonly=True))
		be.build(self.opts(buildpkgonly=True))
		self.assertTrue(os.path.isdir(self.m.PORTAGE_TMPDIR))
		self.assertIn(f"{self.SRC}-1.0", os.listdir(self.m.PORTAGE_TMPDIR))

	def test_a_rebuild_replaces_the_previous_work_tree(self):
		"""build() rmtree's the work directory first, so a second run cannot
		pick up a stale source tree."""
		self.publish_source("1.0")
		be = self.m.AptBackend()
		be.resolve_source([self.SRC], self.opts(buildpkgonly=True))
		be.build(self.opts(buildpkgonly=True))
		stale = os.path.join(self.m.PORTAGE_TMPDIR, f"{self.SRC}-1.0",
		                     "work", "STALE")
		with open(stale, "w") as f:
			f.write("x")
		be = self.m.AptBackend()
		be.resolve_source([self.SRC], self.opts(buildpkgonly=True))
		be.build(self.opts(buildpkgonly=True))
		self.assertFalse(os.path.exists(stale))


def _gpg_works():
	return bool(shutil.which("gpg") and shutil.which("gpgv")
	            and shutil.which("dpkg"))


HAVE_GPG = _gpg_works()

# Named rather than tested inline at the class, so that a machine without
# it is reported by TestEveryCapabilityIsPresent instead of quietly
# skipping the class. An unnamed skip condition is the vacuous pass that
# test exists to prevent, and this file has already lost eleven tests to
# one -- gpgv installed in CI, gpg not.
HAVE_DPKG_DEB = bool(shutil.which("dpkg-deb"))


@unittest.skipUnless(HAVE_GPG, "gpg/gpgv unavailable")
class SignatureVerificationEndToEnd(unittest.TestCase):
	"""The dpkg backend's trust anchor, against real gpg and real gpgv.

	Everything else on this backend is checked against a SHA256 taken from
	the Packages index, so the whole chain is worth exactly as much as the
	signature over that index. test_emerge.py stubs `_gpgv` and pre-seeds a
	keyring, which covers the decisions made around the check but never the
	check -- and the two links it cannot cover are the ones most likely to be
	silently wrong:

	  - `dearmor` exists because gpgv reads binary keyrings only and refuses
	    an armoured .asc outright, which is how Debian ships its keys. If it
	    emitted subtly wrong bytes nothing here would say so.
	  - a keyring that comes out *empty* does not fail. It warns and returns
	    None, and the sync then proceeds unverified. That is the dangerous
	    direction, and it is invisible unless a test asserts the check
	    actually ran rather than that the sync succeeded.

	So these generate a real key, sign a real Release with it, and assert
	both that a good one passes *and that it was checked*."""

	def setUp(self):
		self.dir = tempfile.mkdtemp(prefix="emerge-gpg-itest-")
		self.addCleanup(shutil.rmtree, self.dir, True)
		self.gnupg = os.path.join(self.dir, "gnupg")
		os.makedirs(self.gnupg, mode=0o700)
		self.repo = os.path.join(self.dir, "repo")
		self.trusted = os.path.join(self.dir, "trusted.gpg.d")
		os.makedirs(self.repo)
		os.makedirs(self.trusted)
		self.env = {**os.environ, "GNUPGHOME": self.gnupg,
		            "PATH": SBIN_PATH}
		self.m = self.load()

	# -- fixture -----------------------------------------------------------

	def gpg(self, *args, **kw):
		r = subprocess.run(["gpg", "--batch", "--pinentry-mode", "loopback",
		                    "--passphrase", ""] + list(args),
		                   capture_output=True, text=True, env=self.env, **kw)
		return r

	def make_key(self, uid):
		"""A real signing key. ed25519 so this costs well under a second."""
		r = self.gpg("--quick-generate-key", uid, "ed25519", "sign", "0")
		self.assertEqual(r.returncode, 0, r.stderr)

	def export_armoured(self, uid, path):
		"""Armoured .asc, which is how Debian actually ships archive keys and
		the whole reason dearmor exists."""
		r = self.gpg("--armor", "--export", uid)
		self.assertEqual(r.returncode, 0, r.stderr)
		self.assertIn("BEGIN PGP PUBLIC KEY BLOCK", r.stdout)
		with open(path, "w") as f:
			f.write(r.stdout)

	def publish(self, packages=b"Package: emtest\nVersion: 1.0\n",
	            sign_with=None, corrupt_release=False, corrupt_index=False,
	            sign=True):
		"""A flat file:// repository with a signed InRelease over Release."""
		with open(os.path.join(self.repo, "Packages"), "wb") as f:
			f.write(packages)
		digest = hashlib.sha256(packages).hexdigest()
		release = (f"Suite: trixie\nComponents: main\n"
		           f"SHA256:\n {digest} {len(packages)} Packages\n")
		if corrupt_index:
			# the signature stays valid; the index no longer matches it
			with open(os.path.join(self.repo, "Packages"), "wb") as f:
				f.write(packages + b"Package: smuggled\nVersion: 9\n")
		if not sign:
			return
		src = os.path.join(self.dir, "Release.in")
		with open(src, "w") as f:
			f.write(release)
		out = os.path.join(self.repo, "InRelease")
		args = ["--clearsign", "-o", out]
		if sign_with:
			args += ["--local-user", sign_with]
		r = self.gpg(*args, src)
		self.assertEqual(r.returncode, 0, r.stderr)
		if corrupt_release:
			# flip a byte inside the signed payload
			with open(out) as f:
				text = f.read()
			with open(out, "w") as f:
				f.write(text.replace("Suite: trixie", "Suite: bookwrm"))

	def load(self):
		loader = importlib.machinery.SourceFileLoader("emerge_gpg_itest",
		                                              SCRIPT)
		spec = importlib.util.spec_from_loader(loader.name, loader)
		m = importlib.util.module_from_spec(spec)
		loader.exec_module(m)
		m.TREE_DIR = os.path.join(self.dir, "tree")
		m.LIB_DIR = os.path.join(self.dir, "lib")
		m.WORLD = os.path.join(self.dir, "lib", "world")
		m.TRUSTED_DIR = self.trusted
		m.TRUSTED_LEGACY = os.path.join(self.dir, "nonexistent.gpg")
		m.need_root = lambda: None
		m.print = lambda *a, **k: None
		m.read_sources = lambda: [("file://" + self.repo, "./", ["main"],
		                           self.signed_by)]
		self.warnings = []
		m.ewarn = self.warnings.append
		m.einfo = lambda msg: None
		return m

	signed_by = None

	def sync(self, verify=True):
		self.m.DpkgBackend().sync(verify=verify)

	# -- the good case, and proof that it was actually checked -------------

	def test_a_real_signature_verifies_through_dearmor_and_gpgv(self):
		"""The link the stubbed tests cannot reach: an armoured key, run
		through dearmor, handed to real gpgv, over a real signature."""
		self.make_key("Emerge Archive <a@t>")
		self.export_armoured("Emerge Archive",
		                     os.path.join(self.trusted, "archive.asc"))
		self.publish()
		v = self.m.Verifier(True)
		self.addCleanup(v.close)
		self.assertTrue(v.enabled, "gpgv should have been found")

		text = v.release("file://" + self.repo, "./")
		self.assertIsNotNone(text, f"Release did not verify; {self.warnings}")
		self.assertIn("Suite: trixie", text)

		with open(os.path.join(self.repo, "Packages"), "rb") as f:
			raw = f.read()
		self.assertTrue(v.check_index("file://" + self.repo, "./", None,
		                              "Packages", raw))
		self.assertEqual(v.checked, 1,
		                 "the hash check must have actually run")

	def test_the_keyring_built_from_an_armoured_key_is_not_empty(self):
		"""An empty keyring does not fail -- it warns and the sync proceeds
		unverified. That silent path is the one worth pinning."""
		self.make_key("Emerge Archive <a@t>")
		self.export_armoured("Emerge Archive",
		                     os.path.join(self.trusted, "archive.asc"))
		v = self.m.Verifier(True)
		self.addCleanup(v.close)
		ring = v.keyring(None)
		self.assertIsNotNone(ring, "no keyring was built from the trust dir")
		self.assertGreater(os.path.getsize(ring), 0)

	def test_a_full_sync_reports_the_index_as_verified(self):
		self.make_key("Emerge Archive <a@t>")
		self.export_armoured("Emerge Archive",
		                     os.path.join(self.trusted, "archive.asc"))
		self.publish()
		self.sync()
		self.assertEqual(
		    [w for w in self.warnings if "NOT" in w or "could not" in w], [],
		    f"sync warned about verification: {self.warnings}")
		self.assertTrue(os.listdir(self.m.TREE_DIR))

	# -- the failures, which must be fatal ---------------------------------

	def test_a_tampered_release_aborts_the_sync(self):
		self.make_key("Emerge Archive <a@t>")
		self.export_armoured("Emerge Archive",
		                     os.path.join(self.trusted, "archive.asc"))
		self.publish(corrupt_release=True)
		with self.assertRaises(SystemExit) as cm:
			self.sync()
		self.assertNotEqual(cm.exception.code, 0)

	def test_an_index_that_does_not_match_the_signed_release_aborts(self):
		"""The signature is valid and the Release is genuine; the index
		behind it is not. This is the attack the hash chain exists for."""
		self.make_key("Emerge Archive <a@t>")
		self.export_armoured("Emerge Archive",
		                     os.path.join(self.trusted, "archive.asc"))
		self.publish(corrupt_index=True)
		with self.assertRaises(SystemExit) as cm:
			self.sync()
		self.assertNotEqual(cm.exception.code, 0)

	def test_a_signature_from_an_untrusted_key_aborts(self):
		"""Signed correctly, by a key the trust store does not carry."""
		self.make_key("Emerge Archive <a@t>")
		self.make_key("Some Rando <r@t>")
		self.export_armoured("Emerge Archive",
		                     os.path.join(self.trusted, "archive.asc"))
		self.publish(sign_with="Some Rando")
		with self.assertRaises(SystemExit) as cm:
			self.sync()
		self.assertNotEqual(cm.exception.code, 0)

	# -- the deliberate non-failures ---------------------------------------

	def test_no_verify_skips_the_check_entirely(self):
		self.make_key("Emerge Archive <a@t>")
		self.export_armoured("Emerge Archive",
		                     os.path.join(self.trusted, "archive.asc"))
		self.publish(corrupt_release=True)
		self.sync(verify=False)          # must not raise
		self.assertTrue(os.listdir(self.m.TREE_DIR))

	def test_an_unsigned_local_repository_still_works(self):
		"""Being unable to check only warns. Turning verification on must not
		break a USB-stick repo that never had a Release at all.

		The key has to be present for this to test what it says: without one
		the run stops at "no usable keys" and never reaches the missing
		Release at all."""
		self.make_key("Emerge Archive <a@t>")
		self.export_armoured("Emerge Archive",
		                     os.path.join(self.trusted, "archive.asc"))
		self.publish(sign=False)
		self.sync()
		self.assertTrue(os.listdir(self.m.TREE_DIR))
		self.assertTrue(any("no verifiable InRelease or Release" in w
		                    for w in self.warnings),
		                f"the unverified sync said nothing: {self.warnings}")

	def test_an_empty_trust_store_warns_and_does_not_pretend(self):
		"""The other half of "unable to check": a signed repository, and
		nothing to check it against. It must proceed -- and say so, because
		the alternative is a sync that looks verified and is not."""
		self.make_key("Emerge Archive <a@t>")
		self.publish()          # properly signed, but nothing trusts the key
		self.sync()
		self.assertTrue(os.listdir(self.m.TREE_DIR))
		self.assertTrue(any("no usable keys" in w for w in self.warnings),
		                f"missing keys went unmentioned: {self.warnings}")
		self.assertTrue(any("0 of 1" in w for w in self.warnings),
		                f"the tally did not admit nothing was checked: "
		                f"{self.warnings}")

	# -- signed-by, which is a different trust decision --------------------

	def test_signed_by_accepts_the_key_it_names(self):
		self.make_key("Emerge Archive <a@t>")
		key = os.path.join(self.dir, "byname.asc")
		self.export_armoured("Emerge Archive", key)
		self.publish()
		self.signed_by = key
		self.addCleanup(setattr, type(self), "signed_by", None)
		self.m = self.load()
		self.sync()
		self.assertEqual(
		    [w for w in self.warnings if "NOT" in w], [],
		    f"signed-by verification warned: {self.warnings}")

	def test_signed_by_rejects_a_key_it_does_not_name(self):
		"""signed-by is the whole difference between apt's trust model and
		"anything in the keyring will do": the archive key sits in the trust
		directory, but this entry names a different one."""
		self.make_key("Emerge Archive <a@t>")
		self.make_key("Other Key <o@t>")
		self.export_armoured("Emerge Archive",
		                     os.path.join(self.trusted, "archive.asc"))
		other = os.path.join(self.dir, "other.asc")
		self.export_armoured("Other Key", other)
		self.publish(sign_with="Emerge Archive")
		self.signed_by = other
		self.addCleanup(setattr, type(self), "signed_by", None)
		self.m = self.load()
		with self.assertRaises(SystemExit):
			self.sync()


@unittest.skipUnless(HAVE_GPG, "gpg/gpgv unavailable")
@unittest.skipUnless(HAVE_DPKG_DEB, "dpkg-deb unavailable")
class AncestorRecoveryEndToEnd(unittest.TestCase):
	"""Recovering the previously shipped config file, whole chain, no fakes.

    The unit tests stub the verifier and the fetch, which covers the
    decisions but never the chain itself -- and the chain is four links that
    each fail silently in the same direction: a signature that was never
    checked, an index that was never matched to it, a .deb that was never
    matched to the index, and a conffile that was never actually extracted.
    Each of those alone still ends with a plausible file in the archive and
    a review that looks like it worked.

    So this signs a real Release with a real key, serves a snapshot-shaped
    tree, and asserts the ancestor that lands in the archive is the one out
    of the .deb -- then tampers with the .deb and asserts the run stops."""

	SUITE, ARCH = "trixie", "amd64"

	def setUp(self):
		self.dir = tempfile.mkdtemp(prefix="emerge-anc-itest-")
		self.addCleanup(shutil.rmtree, self.dir, True)
		self.gnupg = os.path.join(self.dir, "gnupg")
		os.makedirs(self.gnupg, mode=0o700)
		self.trusted = os.path.join(self.dir, "trusted.gpg.d")
		self.archive_dir = os.path.join(self.dir, "config-archive")
		self.snap = os.path.join(self.dir, "snapshot")
		self.stamp = "20260410T120436Z"
		self.root = os.path.join(self.snap, "debian", self.stamp)
		os.makedirs(self.trusted)
		os.makedirs(os.path.join(self.root, "pool"))
		self.env = {**os.environ, "GNUPGHOME": self.gnupg, "PATH": SBIN_PATH}

	def gpg(self, *args):
		return subprocess.run(["gpg", "--batch", "--pinentry-mode",
		                       "loopback", "--passphrase", ""] + list(args),
		                      capture_output=True, text=True, env=self.env)

	def make_deb(self, content):
		"""A real .deb carrying one conffile -- the ancestor to recover."""
		d = os.path.join(self.dir, "build")
		shutil.rmtree(d, ignore_errors=True)
		os.makedirs(os.path.join(d, "DEBIAN"))
		os.makedirs(os.path.join(d, "etc"))
		with open(os.path.join(d, "DEBIAN", "control"), "w") as f:
			f.write("Package: emtest\nVersion: 1.0\nArchitecture: amd64\n"
			        "Maintainer: t <t@t>\nDescription: fixture\n")
		with open(os.path.join(d, "DEBIAN", "conffiles"), "w") as f:
			f.write("/etc/emtest.conf\n")
		with open(os.path.join(d, "etc", "emtest.conf"), "w") as f:
			f.write(content)
		out = os.path.join(self.root, "pool", "emtest_1.0_amd64.deb")
		r = subprocess.run(["dpkg-deb", "--build", "-Znone", d, out],
		                   capture_output=True, text=True, env=self.env)
		self.assertEqual(r.returncode, 0, r.stderr)
		return out

	def publish(self, deb, tamper=False, tamper_index=False):
		"""dists/<suite>/... signed, with the .deb's real hash in the index.

        Signed first and tampered after, so the tampered case differs from
        the good one only in the bytes of the .deb -- which is the property
        under test."""
		with open(deb, "rb") as f:
			raw = f.read()
		index = (f"Package: emtest\nVersion: 1.0\nArchitecture: amd64\n"
		         f"Filename: pool/emtest_1.0_amd64.deb\n"
		         f"Size: {len(raw)}\n"
		         f"SHA256: {hashlib.sha256(raw).hexdigest()}\n\n").encode()
		rel = os.path.join(self.root, "dists", self.SUITE)
		os.makedirs(os.path.join(rel, "main", f"binary-{self.ARCH}"))
		ipath = os.path.join(rel, "main", f"binary-{self.ARCH}", "Packages")
		with open(ipath, "wb") as f:
			f.write(index)
		release = (f"Suite: {self.SUITE}\nComponents: main\nSHA256:\n"
		           f" {hashlib.sha256(index).hexdigest()} {len(index)} "
		           f"main/binary-{self.ARCH}/Packages\n")
		src = os.path.join(self.dir, "Release.in")
		with open(src, "w") as f:
			f.write(release)
		r = self.gpg("--clearsign", "-o", os.path.join(rel, "InRelease"), src)
		self.assertEqual(r.returncode, 0, r.stderr)
		if tamper_index:
			# The signature stays valid and covers a hash the index no
			# longer has: the case a stubbed verifier cannot reach.
			with open(ipath, "wb") as f:
				f.write(index.replace(b"Version: 1.0", b"Version: 9.9"))
		if tamper:
			with open(deb, "r+b") as f:
				f.seek(len(raw) - 1)
				f.write(bytes([raw[-1] ^ 0xff]))

	def load(self, etc):
		loader = importlib.machinery.SourceFileLoader("emerge_anc_itest",
		                                              SCRIPT)
		spec = importlib.util.spec_from_loader(loader.name, loader)
		m = importlib.util.module_from_spec(spec)
		loader.exec_module(m)
		m.TRUSTED_DIR = self.trusted
		m.TRUSTED_LEGACY = os.path.join(self.dir, "nonexistent.gpg")
		m.SNAPSHOT_URL = "file://" + self.snap
		m.APT_CACHE_DIR = os.path.join(self.dir, "empty-cache")
		m.read_sources = lambda: [("http://deb.debian.org/debian",
		                           self.SUITE, ["main"], None)]
		# The two local lookups are what this test is not about: no .deb in
		# the cache, and apt is not asked. What remains is the network path.
		m._apt_downloaded_deb = lambda *a, **k: None
		# Stubbed at the seam recovery actually uses. It used to be stubbed
		# at a per-package wrapper, which stopped being called when the log
		# was made a single pass and has since been deleted; these tests
		# caught that themselves, by recovering nothing.
		m.package_histories = lambda names, lines=None: {
		    n: [(self.stamp, "1.0"), ("20260615T133729Z", "1.1")]
		    for n in names}
		m.owners_of = lambda paths: {p: "emtest" for p in paths}
		m.capture = lambda cmd, env=None: types.SimpleNamespace(
		    stdout=self.ARCH + "\n", returncode=0)
		m.need_root = lambda: None
		m.print = lambda *a, **k: None
		m.einfo = m.ewarn = lambda msg: None
		return m

	def conf(self):
		return {**dict.fromkeys(("config-protect-mask", "frozen-files"), ""),
		        "archive-dir": self.archive_dir, "recover-ancestor": "yes",
		        "config-protect": "/etc"}

	def test_the_ancestor_comes_back_out_of_a_signed_chain(self):
		deb = self.make_deb("shipped = yes\n")
		r = self.gpg("--quick-generate-key", "Emerge Archive <a@t>",
		             "ed25519", "sign", "0")
		self.assertEqual(r.returncode, 0, r.stderr)
		r = self.gpg("--armor", "--export", "Emerge Archive")
		with open(os.path.join(self.trusted, "archive.asc"), "w") as f:
			f.write(r.stdout)
		self.publish(deb)

		m = self.load(self.dir)
		got = m.recover_ancestors(self.conf(), ["/etc/emtest.conf"])
		self.assertEqual(got, 1)
		stored = m.archive_path(self.conf(), "/etc/emtest.conf")
		self.assertTrue(os.path.isfile(stored), "nothing was archived")
		with open(stored) as f:
			self.assertEqual(f.read(), "shipped = yes\n",
			                 "the archived ancestor is not the shipped file")

	def test_a_tampered_deb_stops_the_run(self):
		"""The whole point of the chain. A .deb whose bytes do not match the
        signed index must not become an ancestor -- an ancestor decides
        which side of a merge wins, silently, in /etc."""
		deb = self.make_deb("shipped = yes\n")
		r = self.gpg("--quick-generate-key", "Emerge Archive <a@t>",
		             "ed25519", "sign", "0")
		self.assertEqual(r.returncode, 0, r.stderr)
		r = self.gpg("--armor", "--export", "Emerge Archive")
		with open(os.path.join(self.trusted, "archive.asc"), "w") as f:
			f.write(r.stdout)
		self.publish(deb, tamper=True)

		m = self.load(self.dir)
		with self.assertRaises(RuntimeError) as e:
			m.recover_ancestors(self.conf(), ["/etc/emtest.conf"])
		self.assertIn("SHA256 mismatch", str(e.exception))
		self.assertFalse(os.path.exists(
		    m.archive_path(self.conf(), "/etc/emtest.conf")))

	def _trusted_key(self):
		r = self.gpg("--quick-generate-key", "Emerge Archive <a@t>",
		             "ed25519", "sign", "0")
		self.assertEqual(r.returncode, 0, r.stderr)
		r = self.gpg("--armor", "--export", "Emerge Archive")
		with open(os.path.join(self.trusted, "archive.asc"), "w") as f:
			f.write(r.stdout)

	def test_an_index_that_does_not_match_the_release_recovers_nothing(self):
		"""A valid signature over a Release the index no longer matches --
        the middle link, and the one a stubbed verifier cannot test. Found
        by mutation: bypassing check_index left every end-to-end test here
        passing.

        Fatal rather than quiet, which is the same rule --sync applies to
        an index: a hash that does not match is a failure, where a Release
        that cannot be fetched at all is only an inability."""
		deb = self.make_deb("shipped = yes\n")
		self._trusted_key()
		self.publish(deb, tamper_index=True)
		m = self.load(self.dir)
		with self.assertRaises(RuntimeError) as e:
			m.recover_ancestors(self.conf(), ["/etc/emtest.conf"])
		self.assertIn("does not match the signed Release", str(e.exception))
		self.assertFalse(os.path.exists(
		    m.archive_path(self.conf(), "/etc/emtest.conf")))

	def test_an_untrusted_signature_recovers_nothing(self):
		"""Signed by a key the machine does not trust. The Release does not
        verify, so the index is never used and no ancestor appears -- the
        quiet direction, which is why it is asserted rather than assumed."""
		deb = self.make_deb("shipped = yes\n")
		r = self.gpg("--quick-generate-key", "Somebody Else <b@t>",
		             "ed25519", "sign", "0")
		self.assertEqual(r.returncode, 0, r.stderr)
		self.publish(deb)                      # signed, but nothing trusts it
		m = self.load(self.dir)
		self.assertEqual(m.recover_ancestors(self.conf(),
		                                     ["/etc/emtest.conf"]), 0)
		self.assertFalse(os.path.exists(
		    m.archive_path(self.conf(), "/etc/emtest.conf")))


class TestEveryCapabilityIsPresent(unittest.TestCase):
	"""Fails only under EMERGE_TESTS_REQUIRE_ALL, and only to say what is
	missing. A green run of this file means nothing if half of it skipped."""

	def test_nothing_is_silently_skipped(self):
		if not STRICT:
			self.skipTest("set EMERGE_TESTS_REQUIRE_ALL=1 to enforce")
		missing = [name for name, ok in (
		    ("rootless dpkg --root", HAVE_DPKG_ROOT),
		    ("rootless apt", HAVE_APT_ROOT),
		    ("source-build tooling (dpkg-dev)", HAVE_SOURCE_BUILD),
		    ("gpg and gpgv", HAVE_GPG),
		    ("dpkg-deb", HAVE_DPKG_DEB),
		) if not ok]
		self.assertEqual(missing, [],
		                 f"these capabilities are unavailable, so the tests "
		                 f"that need them skipped: {missing}")


if __name__ == "__main__":
	unittest.main(verbosity=2)
