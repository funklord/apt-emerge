#!/usr/bin/env python3
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
        index = open(os.path.join(m.TREE_DIR, tree[0])).read()
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
