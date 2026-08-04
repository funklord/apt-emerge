#!/usr/bin/env python3
#
# Copyright (C) 2026 Nabeel Sowan <nabeel@vibes.se>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# SPDX-License-Identifier: GPL-2.0-or-later
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

import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "emerge")
# dpkg wants start-stop-daemon and friends, which live in sbin
SBIN_PATH = os.environ.get("PATH", "") + ":/usr/sbin:/sbin"


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

		m.capture = lambda cmd: real_capture(inject(cmd))
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

	def merge(self, atoms, **kw):
		be = self.m.DpkgBackend()
		merges = be.resolve(atoms, **kw)
		be.merge(merges, atoms, {"fetchonly": False, "oneshot": False})
		return merges

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

		m.capture = lambda cmd: real_capture(inject(cmd))
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


if __name__ == "__main__":
	unittest.main(verbosity=2)
