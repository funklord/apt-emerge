#!/usr/bin/env python3
"""Unit tests for emerge.

The shipped artifact is a single extensionless script, so it is loaded here
by path rather than imported. Run with:  python3 -m unittest -v test_emerge
(or just ./test_emerge.py). Stdlib only, like the thing under test.

Tests that compare against real Debian tools (dpkg, diff3) skip themselves
when those tools are absent, so the suite still runs on a non-Debian box.
"""

import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "emerge")


def load():
    loader = importlib.machinery.SourceFileLoader("emerge_under_test", SCRIPT)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    # Pretend we are headless: keeps tests off /proc and away from a
    # dpkg-query fork, and makes session annotations deterministic.
    mod._session_critical_cache = set()
    mod._session_blind = False
    return mod


em = load()

HAVE_DPKG = shutil.which("dpkg") is not None
HAVE_DIFF3 = shutil.which("diff3") is not None


# ---------------------------------------------------------------------------
# vercmp -- Debian policy 5.6.12
# ---------------------------------------------------------------------------

# (a, b, expected sign of vercmp(a, b))
VERSION_PAIRS = [
    # equality, and epoch 0 being implicit
    ("1.0", "1.0", 0),
    ("0:1.0", "1.0", 0),
    ("1.0-1", "1.0-1", 0),
    # epochs dominate everything
    ("1:1.0", "2.0", 1),
    ("1:1.0", "2:0.1", -1),
    ("2:1.0", "1:9.9", 1),
    # plain upstream ordering
    ("1.1", "1.0", 1),
    ("1.10", "1.9", 1),
    ("1.0.1", "1.0", 1),
    # numeric components compare numerically, not as text
    ("1.01", "1.1", 0),
    ("1.007", "1.7", 0),
    ("1.010", "1.9", 1),
    # '~' sorts before everything, including end-of-string
    ("1.0~rc1", "1.0", -1),
    ("1.0~~", "1.0~", -1),
    ("1.0~~a", "1.0~~", 1),
    ("1.0~rc1", "1.0~rc2", -1),
    ("1.0~beta", "1.0~rc", -1),
    # letters sort after end-of-string but before non-alphanumerics
    ("1.0a", "1.0", 1),
    ("1.0a", "1.0+", -1),
    # Debian revision is compared only when upstream ties
    ("1.0-2", "1.0-1", 1),
    ("1.0-1", "1.0", 1),
    ("1.1-1", "1.0-9", 1),
    ("1.0-1.1", "1.0-1", 1),
    # binNMU suffix: same source, higher version
    ("1.0-1+b1", "1.0-1", 1),
    ("1.0-1+b2", "1.0-1+b1", 1),
    # Debian stable-update / security suffixes (the deb13u1 case)
    ("5.8.1-1+deb13u1", "5.8.1-1", 1),
    ("5.8.1-1+deb13u2", "5.8.1-1+deb13u1", 1),
    ("2.4.7-21+deb13u1+b4", "2.4.7-21+deb13u1+b3", 1),
    ("25.0.7-2+deb13u1", "25.0.7-2", 1),
    # Ubuntu-style revisions, since sources.list may carry a PPA
    ("9.1-1ubuntu7.18", "9.1-1ubuntu7.9", 1),
    ("2.6.4-ppa1~ubuntu24.04", "2.5.4-ppa2~ubuntu24.04", 1),
    # real pairs seen on a trixie box
    ("1:10.0.11+ds-0+deb13u1", "1:10.0.8+ds-0+deb13u1+b2", 1),
    ("7:7.1.5-0+deb13u1", "7:7.1.4-0+deb13u1", 1),
    ("4:14.1.6-0debian13.0.0+0", "4:14.1.5-0debian13.0.0+0", 1),
    ("6.12.96-1", "6.12.90-2", 1),
    ("3.21.12-11+deb13u1", "3.21.12-11", 1),
]


def sign(n):
    return (n > 0) - (n < 0)


class TestVercmp(unittest.TestCase):
    def test_table(self):
        for a, b, want in VERSION_PAIRS:
            with self.subTest(a=a, b=b):
                self.assertEqual(sign(em.vercmp(a, b)), want)

    def test_antisymmetry(self):
        """vercmp(a,b) must be the exact negation of vercmp(b,a)."""
        for a, b, _ in VERSION_PAIRS:
            with self.subTest(a=a, b=b):
                self.assertEqual(sign(em.vercmp(a, b)),
                                 -sign(em.vercmp(b, a)))

    def test_reflexive(self):
        for a, b, _ in VERSION_PAIRS:
            self.assertEqual(em.vercmp(a, a), 0)
            self.assertEqual(em.vercmp(b, b), 0)

    def test_sort_is_total_order(self):
        """Sorting by vercmp must agree with pairwise comparison."""
        import functools
        vs = ["1.0~rc1", "1.0", "1.0-1", "1.0-1+b1", "1:0.1", "0.9", "1.0a"]
        ordered = sorted(vs, key=functools.cmp_to_key(em.vercmp))
        for i in range(len(ordered) - 1):
            self.assertLessEqual(em.vercmp(ordered[i], ordered[i + 1]), 0,
                                 f"{ordered[i]} !<= {ordered[i+1]}")

    @unittest.skipUnless(HAVE_DPKG, "dpkg not available")
    def test_matches_dpkg(self):
        """Differential test: vercmp reimplements dpkg's algorithm, so it must
        agree with dpkg itself on every pair in the table."""
        for a, b, _ in VERSION_PAIRS:
            with self.subTest(a=a, b=b):
                self.assertEqual(sign(em.vercmp(a, b)), dpkg_cmp(a, b))


def dpkg_cmp(a, b):
    def rel(op):
        return subprocess.run(["dpkg", "--compare-versions", a, op, b],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    if rel("eq"):
        return 0
    return -1 if rel("lt") else 1


class TestMeets(unittest.TestCase):
    def test_operators(self):
        cases = [
            ("1.0", ">=", "1.0", True),
            ("1.0", ">=", "1.1", False),
            ("1.1", ">>", "1.0", True),
            ("1.0", ">>", "1.0", False),
            ("1.0", "<<", "1.1", True),
            ("1.0", "<<", "1.0", False),
            ("1.0", "<=", "1.0", True),
            ("1.0", "=", "1.0", True),
            ("1.0", "=", "1.0-1", False),
        ]
        for ver, op, want, expect in cases:
            with self.subTest(ver=ver, op=op, want=want):
                self.assertIs(em.meets(ver, op, want), expect)

    def test_deprecated_operators_are_inclusive(self):
        """Bare '<' and '>' mean <= and >= in Debian control files."""
        self.assertTrue(em.meets("1.0", "<", "1.0"))
        self.assertTrue(em.meets("1.0", ">", "1.0"))


# ---------------------------------------------------------------------------
# parse_depends / parse_stanzas
# ---------------------------------------------------------------------------

class TestParseDepends(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(em.parse_depends(""), [])
        self.assertEqual(em.parse_depends("   "), [])

    def test_simple(self):
        self.assertEqual(em.parse_depends("libc6"),
                         [[("libc6", None, None)]])

    def test_versioned(self):
        self.assertEqual(em.parse_depends("libc6 (>= 2.36)"),
                         [[("libc6", ">=", "2.36")]])

    def test_alternatives_and_clauses(self):
        self.assertEqual(
            em.parse_depends("a (>= 1) | b, c"),
            [[("a", ">=", "1"), ("b", None, None)], [("c", None, None)]])

    def test_arch_qualifier_is_stripped(self):
        self.assertEqual(em.parse_depends("libc6:amd64 (>= 2.36)"),
                         [[("libc6", ">=", "2.36")]])
        self.assertEqual(em.parse_depends("libc6:any"),
                         [[("libc6", None, None)]])

    def test_build_profile_is_ignored(self):
        self.assertEqual(em.parse_depends("dh-sequence-python3 [!nocheck]"),
                         [[("dh-sequence-python3", None, None)]])
        self.assertEqual(em.parse_depends("foo (>= 1) [linux-any]"),
                         [[("foo", ">=", "1")]])

    def test_whitespace_inside_parens(self):
        self.assertEqual(em.parse_depends("foo (  >=   1.0  )"),
                         [[("foo", ">=", "1.0")]])

    def test_all_operators(self):
        for op in ("<<", "<=", "=", ">=", ">>", "<", ">"):
            with self.subTest(op=op):
                self.assertEqual(em.parse_depends(f"foo ({op} 1.0)"),
                                 [[("foo", op, "1.0")]])

    def test_package_names_with_punctuation(self):
        self.assertEqual(em.parse_depends("libstdc++6, gcc-13-base, g++"),
                         [[("libstdc++6", None, None)],
                          [("gcc-13-base", None, None)],
                          [("g++", None, None)]])

    def test_epoch_in_constraint(self):
        self.assertEqual(em.parse_depends("qemu-utils (>= 1:10.0.8+ds)"),
                         [[("qemu-utils", ">=", "1:10.0.8+ds")]])

    def test_unparsable_alternative_is_dropped_not_fatal(self):
        # a clause whose alternatives are all junk yields no clause at all
        self.assertEqual(em.parse_depends("!!! , libc6"),
                         [[("libc6", None, None)]])


class TestParseStanzas(unittest.TestCase):
    def test_multiple_stanzas(self):
        text = ("Package: a\nVersion: 1.0\n\n"
                "Package: b\nVersion: 2.0\n")
        got = list(em.parse_stanzas(text))
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]["Package"], "a")
        self.assertEqual(got[1]["Version"], "2.0")

    def test_continuation_lines(self):
        text = "Package: a\nDescription: one\n two\n three\n"
        got = list(em.parse_stanzas(text))[0]
        self.assertEqual(got["Description"], "one\ntwo\nthree")

    def test_trailing_stanza_without_blank_line(self):
        self.assertEqual(len(list(em.parse_stanzas("Package: a\n"))), 1)

    def test_blank_input(self):
        self.assertEqual(list(em.parse_stanzas("")), [])


# ---------------------------------------------------------------------------
# upstream_version / same_upstream
# ---------------------------------------------------------------------------

class TestUpstream(unittest.TestCase):
    def test_upstream_version(self):
        self.assertEqual(em.upstream_version("25.0.7-2+deb13u1"), "25.0.7")
        self.assertEqual(em.upstream_version("2:9.1-1ubuntu7.18"), "9.1")
        self.assertEqual(em.upstream_version("1.0"), "1.0")
        self.assertEqual(em.upstream_version("1:10.0.11+ds-0+deb13u1"),
                         "10.0.11+ds")

    def test_same_upstream_true_for_revision_bumps(self):
        self.assertTrue(em.same_upstream("25.0.7-2", "25.0.7-2+deb13u1"))
        self.assertTrue(em.same_upstream("1.0-1", "1.0-1+b1"))
        self.assertTrue(em.same_upstream("5.8.1-1", "5.8.1-1+deb13u1"))

    def test_same_upstream_false_for_real_version_changes(self):
        self.assertFalse(em.same_upstream("25.0.7-2", "25.1.0-1"))
        self.assertFalse(em.same_upstream("1.0-1", "2.0-1"))

    def test_same_upstream_false_when_identical(self):
        """An unchanged version is not a rebuild."""
        self.assertFalse(em.same_upstream("1.0-1", "1.0-1"))


# ---------------------------------------------------------------------------
# merge3 / _significant
# ---------------------------------------------------------------------------

def L(*lines):
    return [l + "\n" for l in lines]


class TestMerge3(unittest.TestCase):
    def test_no_change_anywhere(self):
        base = L("a", "b", "c")
        out, n = em.merge3(base, list(base), list(base))
        self.assertEqual(out, base)
        self.assertEqual(n, 0)

    def test_only_theirs_changed(self):
        base = L("a", "b", "c")
        theirs = L("a", "B", "c")
        out, n = em.merge3(base, list(base), theirs)
        self.assertEqual(out, theirs)
        self.assertEqual(n, 0)

    def test_only_mine_changed(self):
        base = L("a", "b", "c")
        mine = L("a", "MINE", "c")
        out, n = em.merge3(base, mine, list(base))
        self.assertEqual(out, mine)
        self.assertEqual(n, 0)

    def test_both_made_the_same_change(self):
        base = L("a", "b", "c")
        both = L("a", "SAME", "c")
        out, n = em.merge3(base, list(both), list(both))
        self.assertEqual(out, both)
        self.assertEqual(n, 0)

    def test_disjoint_changes_both_survive(self):
        base = L("a", "b", "c", "d", "e")
        mine = L("A", "b", "c", "d", "e")
        theirs = L("a", "b", "c", "d", "E")
        out, n = em.merge3(base, mine, theirs)
        self.assertEqual(n, 0)
        self.assertEqual(out, L("A", "b", "c", "d", "E"))

    def test_conflict_emits_all_three_sides(self):
        base = L("a", "b", "c")
        mine = L("a", "MINE", "c")
        theirs = L("a", "THEIRS", "c")
        out, n = em.merge3(base, mine, theirs)
        self.assertEqual(n, 1)
        text = "".join(out)
        self.assertIn("<<<<<<< current\n", text)
        self.assertIn("||||||| as-shipped\n", text)
        self.assertIn("=======\n", text)
        self.assertIn(">>>>>>> new\n", text)
        # every side's content is preserved for the user to resolve
        self.assertIn("MINE\n", text)
        self.assertIn("b\n", text)
        self.assertIn("THEIRS\n", text)

    def test_conflict_labels_are_configurable(self):
        base, mine, theirs = L("b"), L("m"), L("t")
        out, n = em.merge3(base, mine, theirs, labels=("X", "Y", "Z"))
        self.assertEqual(n, 1)
        self.assertIn("<<<<<<< X\n", out)
        self.assertIn("||||||| Y\n", out)
        self.assertIn(">>>>>>> Z\n", out)

    def test_addition_on_both_sides_conflicts(self):
        base = L("a")
        mine = L("a", "mine-tail")
        theirs = L("a", "theirs-tail")
        out, n = em.merge3(base, mine, theirs)
        self.assertEqual(n, 1)

    def test_deletion_by_them_is_taken(self):
        base = L("a", "b", "c")
        theirs = L("a", "c")
        out, n = em.merge3(base, list(base), theirs)
        self.assertEqual(n, 0)
        self.assertEqual(out, theirs)

    def test_empty_inputs(self):
        out, n = em.merge3([], [], [])
        self.assertEqual((out, n), ([], 0))

    @unittest.skipUnless(HAVE_DIFF3, "diff3 not available")
    def test_clean_merges_match_diff3(self):
        """merge3 claims diff3 -m equivalence; hold it to that on the cases
        where diff3's output is unambiguous (no conflict markers involved)."""
        import tempfile
        cases = [
            (L("a", "b", "c"), L("a", "b", "c"), L("a", "B", "c")),
            (L("a", "b", "c"), L("A", "b", "c"), L("a", "b", "c")),
            (L("a", "b", "c", "d", "e"), L("A", "b", "c", "d", "e"),
             L("a", "b", "c", "d", "E")),
            (L("a", "b", "c"), L("a", "b", "c"), L("a", "c")),
            (L("x"), L("x", "y"), L("x")),
        ]
        for base, mine, theirs in cases:
            with self.subTest(base=base, mine=mine, theirs=theirs):
                with tempfile.TemporaryDirectory() as d:
                    paths = []
                    for nm, lines in (("mine", mine), ("base", base),
                                      ("theirs", theirs)):
                        p = os.path.join(d, nm)
                        with open(p, "w") as f:
                            f.writelines(lines)
                        paths.append(p)
                    r = subprocess.run(["diff3", "-m"] + paths,
                                       capture_output=True, text=True)
                    self.assertEqual(r.returncode, 0, "expected a clean merge")
                    out, n = em.merge3(base, mine, theirs)
                    self.assertEqual(n, 0)
                    self.assertEqual("".join(out), r.stdout)


class TestSignificant(unittest.TestCase):
    def test_strips_comments_and_blank_lines(self):
        lines = L("# comment", "", "  ", "key = value", "; ini comment",
                  "// c comment", "other")
        self.assertEqual(em._significant(lines), ["key = value", "other"])

    def test_collapses_internal_whitespace(self):
        self.assertEqual(em._significant(L("key   =    value")),
                         ["key = value"])

    def test_indentation_is_not_significant(self):
        self.assertEqual(em._significant(L("    key = value")),
                         em._significant(L("key = value")))

    def test_comment_only_difference_compares_equal(self):
        a = L("# old blurb", "setting = 1")
        b = L("# new blurb rewritten by the maintainer", "setting  =  1", "")
        self.assertEqual(em._significant(a), em._significant(b))


# ---------------------------------------------------------------------------
# ndu_solve -- the --no-dep-upgrade closure solver
# ---------------------------------------------------------------------------

def stanza(name, version, depends="", provides="", size=0):
    st = {"Package": name, "Version": version}
    if depends:
        st["Depends"] = depends
    if provides:
        st["Provides"] = provides
    if size:
        st["Size"] = str(size)
    return st


class FakeIndex:
    """ndu_solve index over a literal {name: [stanza, ...]} table.

    Versions are held newest-first, as both real indexes deliver them."""

    def __init__(self, table):
        self.table = table

    def all_versions(self, name):
        return self.table.get(name, [])

    def has(self, name):
        return name in self.table

    def provides_of(self, name):
        out = []
        for pkg, versions in self.table.items():
            for st in versions:
                for entry in st.get("Provides", "").split(","):
                    entry = entry.strip()
                    if entry and entry.split()[0] == name:
                        out.append((pkg, None))
                        break
        return out


def installed(*pairs):
    return {n: {"Package": n, "Version": v} for n, v in pairs}


class TestNduSolve(unittest.TestCase):
    def solve(self, index, inst, worklist, atoms=None, update=False,
              allow=None, iprov=None):
        return em.ndu_solve(index, inst, iprov or {}, worklist,
                            set(atoms if atoms is not None else worklist),
                            update, allow)

    def names(self, merges):
        return {row[0] for row in merges}

    def version_of(self, merges, name):
        for row in merges:
            if row[0] == name:
                return row[1]
        return None

    # -- the core rule: installed deps are pinned, not searched --------------

    def test_installed_satisfying_dep_is_left_alone(self):
        idx = FakeIndex({
            "app": [stanza("app", "2.0", depends="libfoo (>= 1.0)")],
            "libfoo": [stanza("libfoo", "2.0"), stanza("libfoo", "1.0")],
        })
        _, merges = self.solve(idx, installed(("libfoo", "1.0")), ["app"])
        self.assertEqual(self.names(merges), {"app"})

    def test_installed_dep_is_pinned_even_though_newer_exists(self):
        """The whole point: a newer libfoo is available and would satisfy the
        constraint, but an installed package is never version-searched."""
        idx = FakeIndex({
            "app": [stanza("app", "2.0", depends="libfoo (>= 1.0)")],
            "libfoo": [stanza("libfoo", "9.9"), stanza("libfoo", "1.0")],
        })
        plan, _ = self.solve(idx, installed(("libfoo", "1.0")), ["app"])
        chosen = {st["Package"]: st["Version"] for st in plan}
        self.assertNotIn("libfoo", chosen)

    def test_not_installed_dep_takes_the_newest_version(self):
        idx = FakeIndex({
            "app": [stanza("app", "1.0", depends="libnew")],
            "libnew": [stanza("libnew", "3.0"), stanza("libnew", "1.0")],
        })
        _, merges = self.solve(idx, installed(), ["app"])
        self.assertEqual(self.version_of(merges, "libnew"), "3.0")

    def test_not_installed_dep_steps_back_to_avoid_an_upgrade(self):
        """libnew 2.0 needs a newer libc than is installed, so the solver must
        fall back to libnew 1.0 rather than dragging libc up."""
        idx = FakeIndex({
            "app": [stanza("app", "1.0", depends="libnew (>= 1.0)")],
            "libnew": [stanza("libnew", "2.0", depends="libc (>= 2.0)"),
                       stanza("libnew", "1.0", depends="libc (>= 1.0)")],
            "libc": [stanza("libc", "2.0"), stanza("libc", "1.0")],
        })
        _, merges = self.solve(idx, installed(("libc", "1.0")), ["app"])
        self.assertEqual(self.version_of(merges, "libnew"), "1.0")
        self.assertNotIn("libc", self.names(merges))

    def test_step_back_walks_past_several_versions(self):
        idx = FakeIndex({
            "app": [stanza("app", "1.0", depends="libnew")],
            "libnew": [stanza("libnew", "4.0", depends="libc (>= 4.0)"),
                       stanza("libnew", "3.0", depends="libc (>= 3.0)"),
                       stanza("libnew", "2.0", depends="libc (>= 2.0)"),
                       stanza("libnew", "1.0", depends="libc (>= 1.0)")],
            "libc": [stanza("libc", "4.0"), stanza("libc", "1.0")],
        })
        _, merges = self.solve(idx, installed(("libc", "1.0")), ["app"])
        self.assertEqual(self.version_of(merges, "libnew"), "1.0")

    # -- genuine walls -------------------------------------------------------

    def test_unsatisfiable_installed_dep_is_a_wall(self):
        idx = FakeIndex({
            "app": [stanza("app", "1.0", depends="libc (>= 2.0)")],
            "libc": [stanza("libc", "2.0"), stanza("libc", "1.0")],
        })
        with self.assertRaises(em.NduWall) as cm:
            self.solve(idx, installed(("libc", "1.0")), ["app"])
        movers = cm.exception.movers
        self.assertEqual([m["name"] for m in movers], ["libc"])
        self.assertEqual(movers[0]["installed"], "1.0")
        self.assertEqual(movers[0]["wanted"], "2.0")

    def test_wall_names_the_package_that_forced_the_move(self):
        idx = FakeIndex({
            "app": [stanza("app", "1.0", depends="libc (>= 2.0)")],
            "libc": [stanza("libc", "2.0"), stanza("libc", "1.0")],
        })
        with self.assertRaises(em.NduWall) as cm:
            self.solve(idx, installed(("libc", "1.0")), ["app"])
        self.assertEqual(cm.exception.movers[0]["why"], "app")

    def test_wall_flags_a_same_upstream_revision_bump(self):
        """The libgbm1 25.0.7-2 -> 25.0.7-2+deb13u1 case: the escape hatch
        needs to know this is a rebuild, not a real version change."""
        idx = FakeIndex({
            "app": [stanza("app", "1.0",
                           depends="libgbm1 (>= 25.0.7-2+deb13u1)")],
            "libgbm1": [stanza("libgbm1", "25.0.7-2+deb13u1"),
                        stanza("libgbm1", "25.0.7-2")],
        })
        with self.assertRaises(em.NduWall) as cm:
            self.solve(idx, installed(("libgbm1", "25.0.7-2")), ["app"])
        mover = cm.exception.movers[0]
        self.assertTrue(mover["same_upstream"])
        self.assertIn("same upstream", str(cm.exception))

    def test_wall_message_offers_the_with_escape_hatch(self):
        idx = FakeIndex({
            "app": [stanza("app", "1.0", depends="libc (>= 2.0)")],
            "libc": [stanza("libc", "2.0"), stanza("libc", "1.0")],
        })
        with self.assertRaises(em.NduWall) as cm:
            self.solve(idx, installed(("libc", "1.0")), ["app"])
        self.assertIn("--with libc", str(cm.exception))

    # -- the --with allow-set ------------------------------------------------

    def test_allow_lets_the_named_package_move(self):
        idx = FakeIndex({
            "app": [stanza("app", "1.0", depends="libc (>= 2.0)")],
            "libc": [stanza("libc", "2.0"), stanza("libc", "1.0")],
        })
        _, merges = self.solve(idx, installed(("libc", "1.0")), ["app"],
                               allow={"libc"})
        self.assertEqual(self.version_of(merges, "libc"), "2.0")

    def test_allow_does_not_loosen_anything_else(self):
        """libc is permitted to move; libz is not, and still walls."""
        idx = FakeIndex({
            "app": [stanza("app", "1.0",
                           depends="libc (>= 2.0), libz (>= 2.0)")],
            "libc": [stanza("libc", "2.0"), stanza("libc", "1.0")],
            "libz": [stanza("libz", "2.0"), stanza("libz", "1.0")],
        })
        with self.assertRaises(em.NduWall) as cm:
            self.solve(idx, installed(("libc", "1.0"), ("libz", "1.0")),
                       ["app"], allow={"libc"})
        self.assertIn("libz", [m["name"] for m in cm.exception.movers])

    # -- alternatives, provides, and other resolution behaviour --------------

    def test_satisfied_alternative_short_circuits(self):
        """`a | b` with a installed and satisfying must not pull in b."""
        idx = FakeIndex({
            "app": [stanza("app", "1.0", depends="liba | libb")],
            "liba": [stanza("liba", "1.0")],
            "libb": [stanza("libb", "1.0")],
        })
        _, merges = self.solve(idx, installed(("liba", "1.0")), ["app"])
        self.assertNotIn("libb", self.names(merges))

    def test_first_alternative_is_taken_when_none_are_installed(self):
        idx = FakeIndex({
            "app": [stanza("app", "1.0", depends="liba | libb")],
            "liba": [stanza("liba", "1.0")],
            "libb": [stanza("libb", "1.0")],
        })
        _, merges = self.solve(idx, installed(), ["app"])
        self.assertIn("liba", self.names(merges))
        self.assertNotIn("libb", self.names(merges))

    def test_virtual_dependency_resolves_through_provides(self):
        idx = FakeIndex({
            "app": [stanza("app", "1.0", depends="mail-transport-agent")],
            "postfix": [stanza("postfix", "3.0",
                               provides="mail-transport-agent")],
        })
        _, merges = self.solve(idx, installed(), ["app"])
        self.assertIn("postfix", self.names(merges))

    def test_installed_provider_satisfies_a_virtual_dependency(self):
        idx = FakeIndex({
            "app": [stanza("app", "1.0", depends="mail-transport-agent")],
            "postfix": [stanza("postfix", "3.0",
                               provides="mail-transport-agent")],
        })
        iprov = {"mail-transport-agent": [("exim4", None)]}
        _, merges = self.solve(idx, installed(("exim4", "4.0")), ["app"],
                               iprov=iprov)
        self.assertNotIn("postfix", self.names(merges))

    def test_transitive_dependencies_are_pulled_in(self):
        idx = FakeIndex({
            "app": [stanza("app", "1.0", depends="mid")],
            "mid": [stanza("mid", "1.0", depends="leaf")],
            "leaf": [stanza("leaf", "1.0")],
        })
        _, merges = self.solve(idx, installed(), ["app"])
        self.assertEqual(self.names(merges), {"app", "mid", "leaf"})

    def test_pre_depends_are_honoured(self):
        idx = FakeIndex({
            "app": [{"Package": "app", "Version": "1.0",
                     "Pre-Depends": "libc (>= 2.0)"}],
            "libc": [stanza("libc", "2.0"), stanza("libc", "1.0")],
        })
        with self.assertRaises(em.NduWall):
            self.solve(idx, installed(("libc", "1.0")), ["app"])

    def test_dependency_cycle_terminates(self):
        idx = FakeIndex({
            "a": [stanza("a", "1.0", depends="b")],
            "b": [stanza("b", "1.0", depends="a")],
        })
        _, merges = self.solve(idx, installed(), ["a"])
        self.assertEqual(self.names(merges), {"a", "b"})

    def test_unknown_atom_is_an_error(self):
        idx = FakeIndex({})
        with self.assertRaises(RuntimeError):
            self.solve(idx, installed(), ["nosuchpkg"])

    # -- reinstall / update semantics ----------------------------------------

    def test_explicit_atom_at_current_version_is_a_reinstall(self):
        idx = FakeIndex({"app": [stanza("app", "1.0")]})
        _, merges = self.solve(idx, installed(("app", "1.0")), ["app"],
                               update=False)
        self.assertEqual(self.names(merges), {"app"})

    def test_update_skips_an_atom_already_at_the_newest_version(self):
        idx = FakeIndex({"app": [stanza("app", "1.0")]})
        _, merges = self.solve(idx, installed(("app", "1.0")), ["app"],
                               update=True)
        self.assertEqual(merges, [])

    def test_merges_carry_the_previous_version_and_size(self):
        idx = FakeIndex({"app": [stanza("app", "2.0", size=4096)]})
        _, merges = self.solve(idx, installed(("app", "1.0")), ["app"],
                               allow={"app"})
        name, newv, oldv, size, kind, _use = merges[0]
        self.assertEqual((name, newv, oldv, size, kind),
                         ("app", "2.0", "1.0", 4096, "ebuild"))


# ---------------------------------------------------------------------------
# _dep_ok
# ---------------------------------------------------------------------------

class TestDepOk(unittest.TestCase):
    def none_provides(self, name):
        return []

    def test_installed_and_satisfying(self):
        self.assertTrue(em._dep_ok(("libc", ">=", "1.0"),
                                   installed(("libc", "1.0")), {}, {},
                                   self.none_provides))

    def test_installed_but_too_old(self):
        self.assertFalse(em._dep_ok(("libc", ">=", "2.0"),
                                    installed(("libc", "1.0")), {}, {},
                                    self.none_provides))

    def test_unversioned_dep_on_an_installed_package(self):
        self.assertTrue(em._dep_ok(("libc", None, None),
                                   installed(("libc", "1.0")), {}, {},
                                   self.none_provides))

    def test_already_chosen_counts(self):
        chosen = {"libc": {"Package": "libc", "Version": "2.0"}}
        self.assertTrue(em._dep_ok(("libc", ">=", "2.0"), {}, chosen, {},
                                   self.none_provides))

    def test_installed_provides_satisfies_unversioned(self):
        iprov = {"httpd": [("nginx", None)]}
        self.assertTrue(em._dep_ok(("httpd", None, None), {}, {}, iprov,
                                   self.none_provides))

    def test_versioned_provides_is_checked(self):
        iprov = {"httpd": [("nginx", "1.0")]}
        self.assertTrue(em._dep_ok(("httpd", ">=", "1.0"), {}, {}, iprov,
                                   self.none_provides))
        self.assertFalse(em._dep_ok(("httpd", ">=", "2.0"), {}, {}, iprov,
                                    self.none_provides))

    def test_provides_of_is_called_not_subscripted(self):
        """Regression: provides_of is a callable, and was once used as a dict
        (`.get(...)`), which raised AttributeError at resolve time."""
        calls = []

        def provides_of(name):
            calls.append(name)
            return [("nginx", None)]

        chosen = {"nginx": {"Package": "nginx", "Version": "1.0"}}
        self.assertTrue(em._dep_ok(("httpd", None, None), {}, chosen, {},
                                   provides_of))
        self.assertEqual(calls, ["httpd"])

    def test_missing_everywhere(self):
        self.assertFalse(em._dep_ok(("nope", None, None), {}, {}, {},
                                    self.none_provides))


# ---------------------------------------------------------------------------
# Session-critical detection
# ---------------------------------------------------------------------------

# A leader mapping its own binary, two libraries, a font and anonymous memory.
MAPS_FULL = """\
55a1b2c00000-55a1b2c22000 r-xp 00000000 08:01 131 /usr/bin/kwin_wayland
7f0000000000-7f0000001000 r--p 00000000 08:01 132 /usr/share/fonts/x.ttf
7f0000002000-7f0000003000 r-xp 00000000 08:01 133 /usr/lib/libfoo.so.1
7f0000004000-7f0000005000 r--p 00000000 08:01 134 /usr/lib/libbar.so.2
7f0000006000-7f0000007000 rw-p 00000000 00:00 0 [heap]
"""

# The same process as seen when only its libraries are mapped from a file.
MAPS_LIBS_ONLY = """\
7f0000002000-7f0000003000 r-xp 00000000 08:01 133 /usr/lib/libfoo.so.1
"""


class TestSessionCritical(unittest.TestCase):
    """These drive the detector with a synthetic /proc view, so they behave
    identically on a desktop, a headless server and a build box."""

    def setUp(self):
        self.mod = load()

    def force(self, live, blind):
        self.mod._session_critical_cache = live
        self.mod._session_blind = blind

    # `os` and `shutil` are shared module objects, so anything patched on them
    # has to be put back or it leaks into the rest of the suite.
    def patch(self, obj, attr, value):
        original = getattr(obj, attr)
        setattr(obj, attr, value)
        self.addCleanup(setattr, obj, attr, original)

    def fake_maps(self, content):
        def fake_open(path, *a, **kw):
            if str(path).endswith("/maps"):
                return io.StringIO(content)
            raise FileNotFoundError(path)
        self.mod.open = fake_open

    def no_exe(self):
        def denied(_path):
            raise PermissionError(13, "Permission denied")
        self.patch(self.mod.os, "readlink", denied)

    def test_headless_reports_nothing(self):
        self.force(set(), False)
        self.assertFalse(self.mod.is_session_critical("libgbm1"))
        self.assertFalse(self.mod.is_session_critical("xwayland"))

    def test_live_membership_wins(self):
        self.force({"libfoo"}, False)
        self.assertTrue(self.mod.is_session_critical("libfoo"))

    def test_static_set_backs_up_a_live_session(self):
        """The live scan cannot see leaders we may not read, so the static
        recogniser must still apply when a session was detected."""
        self.force({"libfoo"}, False)
        self.assertTrue(self.mod.is_session_critical("xserver-xorg-core"))
        self.assertTrue(self.mod.is_session_critical("libgtk-3-0"))  # prefix
        self.assertFalse(self.mod.is_session_critical("nano"))

    def test_blind_falls_back_to_the_static_set(self):
        self.force(set(), True)
        self.assertTrue(self.mod.is_session_critical("libgbm1"))
        self.assertFalse(self.mod.is_session_critical("nano"))

    def test_exe_deleted_suffix_is_stripped(self):
        """After an upgrade the kernel appends ' (deleted)' to /proc/PID/exe;
        the raw string would never match a dpkg path."""
        self.patch(self.mod.os, "readlink",
                   lambda _p: "/usr/bin/kwin_wayland (deleted)")
        self.assertEqual(self.mod._proc_exe("1"), "/usr/bin/kwin_wayland")

    def test_proc_exe_survives_permission_denied(self):
        self.no_exe()
        self.assertIsNone(self.mod._proc_exe("1"))

    def test_unreadable_proc_is_blind_not_headless(self):
        def denied(_path):
            raise PermissionError(13, "Permission denied")
        self.patch(self.mod.os, "listdir", denied)
        self.assertIsNone(self.mod._find_session_leaders())

    # -- what _proc_mapped_code actually collects ---------------------------

    def test_only_code_mappings_are_collected(self):
        """Executable mappings and shared libraries, but not data the process
        happens to have mmap'd -- a font upgrade cannot restart a session."""
        self.no_exe()
        self.patch(self.mod.shutil, "which", lambda _c: None)
        self.fake_maps(MAPS_FULL)
        files = self.mod._proc_mapped_code("1", "kwin_wayland")
        self.assertEqual(files, {"/usr/bin/kwin_wayland",
                                 "/usr/lib/libfoo.so.1",
                                 "/usr/lib/libbar.so.2"})

    def test_executable_is_collected_from_exe(self):
        """The compositor binary is what identifies its package; missing it is
        how kwin-wayland/xwayland/sddm went unflagged on a live desktop."""
        self.patch(self.mod.os, "readlink", lambda _p: "/usr/bin/plasmashell")
        self.fake_maps(MAPS_LIBS_ONLY)
        files = self.mod._proc_mapped_code("1", "plasmashell")
        self.assertIn("/usr/bin/plasmashell", files)

    def test_path_fallback_names_a_hardened_leader(self):
        """A setcap'd compositor runs with dumpable=0, so a non-root emerge
        can read neither its exe nor its maps; resolving comm on PATH is the
        only way to learn which package ships it."""
        self.no_exe()
        self.patch(self.mod.shutil, "which",
                   lambda c: "/usr/bin/kwin_wayland"
                   if c == "kwin_wayland" else None)
        self.fake_maps(MAPS_LIBS_ONLY)
        files = self.mod._proc_mapped_code("1", "kwin_wayland")
        self.assertIn("/usr/bin/kwin_wayland", files)

    def test_path_fallback_is_not_used_when_exe_is_readable(self):
        self.patch(self.mod.os, "readlink", lambda _p: "/usr/bin/real")
        self.patch(self.mod.shutil, "which",
                   lambda _c: self.fail("PATH consulted despite a usable exe"))
        self.fake_maps(MAPS_LIBS_ONLY)
        self.assertIn("/usr/bin/real",
                      self.mod._proc_mapped_code("1", "kwin_wayland"))

    def test_unreadable_maps_still_yields_the_executable(self):
        self.patch(self.mod.os, "readlink", lambda _p: "/usr/bin/kwin_wayland")
        self.assertEqual(self.mod._proc_mapped_code("1", "kwin_wayland"),
                         {"/usr/bin/kwin_wayland"})

    def test_truncated_comm_without_exe_yields_nothing(self):
        """comm is capped at 15 chars, so some names cannot be resolved; that
        is a miss, not a crash."""
        self.no_exe()
        self.patch(self.mod.shutil, "which", lambda _c: None)
        self.fake_maps(MAPS_LIBS_ONLY)
        files = self.mod._proc_mapped_code("1", "gdm-session-wor")
        self.assertEqual(files, {"/usr/lib/libfoo.so.1"})

    def test_leaders_are_returned_with_their_comm(self):
        self.patch(self.mod.os, "listdir", lambda _p: ["1", "2", "self"])
        self.patch(self.mod, "_proc_comm",
                   lambda pid: {"1": "kwin_wayland", "2": "bash"}.get(pid))
        self.assertEqual(self.mod._find_session_leaders(),
                         [("1", "kwin_wayland")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
