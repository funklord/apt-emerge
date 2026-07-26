#!/usr/bin/env python3
"""Unit tests for emerge.

The shipped artifact is a single extensionless script, so it is loaded here
by path rather than imported. Run with:  python3 -m unittest -v test_emerge
(or just ./test_emerge.py). Stdlib only, like the thing under test.

Tests that compare against real Debian tools (dpkg, diff3) skip themselves
when those tools are absent, so the suite still runs on a non-Debian box.
"""

import base64
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import tokenize
import unittest
import urllib.error

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
        # Deliberately mirrors _AptIndex.has: a purely virtual name is "known"
        # because something provides it, even though no package ships under
        # that name. A fake that answered False here would hide the case where
        # a virtual dependency is pushed onto the stack as if it were real.
        return bool(self.all_versions(name)) or bool(self.provides_of(name))

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

    # -- backtracking across independent dependencies ------------------------

    def test_backtracks_to_an_older_earlier_dependency(self):
        """The documented false wall. app needs liba and libb; libb only works
        with libc < 2.0, and liba 2.0 demands libc >= 2.0. The only resolution
        is the *older* liba, which a solver that commits to its first choice
        and only steps the package in front of it will never reach."""
        idx = FakeIndex({
            "app": [stanza("app", "1.0", depends="liba, libb")],
            "liba": [stanza("liba", "2.0", depends="libc (>= 2.0)"),
                     stanza("liba", "1.0", depends="libc (>= 1.0)")],
            "libb": [stanza("libb", "1.0", depends="libc (<< 2.0)")],
            "libc": [stanza("libc", "2.0"), stanza("libc", "1.0")],
        })
        _, merges = self.solve(idx, installed(), ["app"])
        self.assertEqual(self.version_of(merges, "liba"), "1.0")
        self.assertEqual(self.version_of(merges, "libc"), "1.0")

    def test_backtracking_walks_several_versions_back(self):
        idx = FakeIndex({
            "app": [stanza("app", "1.0", depends="liba, libb")],
            "liba": [stanza("liba", "4.0", depends="libc (>= 4.0)"),
                     stanza("liba", "3.0", depends="libc (>= 3.0)"),
                     stanza("liba", "2.0", depends="libc (>= 2.0)"),
                     stanza("liba", "1.0", depends="libc (>= 1.0)")],
            "libb": [stanza("libb", "1.0", depends="libc (<< 2.0)")],
            "libc": [stanza("libc", "4.0"), stanza("libc", "1.0")],
        })
        _, merges = self.solve(idx, installed(), ["app"])
        self.assertEqual(self.version_of(merges, "liba"), "1.0")
        self.assertEqual(self.version_of(merges, "libc"), "1.0")

    def test_backtracking_still_refuses_to_move_installed_packages(self):
        """Searching harder must not become a licence to upgrade something
        installed: the only combination that satisfies everything needs libc
        to move, so it is still a wall."""
        idx = FakeIndex({
            "app": [stanza("app", "1.0", depends="liba, libb")],
            "liba": [stanza("liba", "1.0", depends="libc (>= 2.0)")],
            "libb": [stanza("libb", "1.0", depends="libc (>= 2.0)")],
            "libc": [stanza("libc", "2.0"), stanza("libc", "1.0")],
        })
        with self.assertRaises(em.NduWall) as cm:
            self.solve(idx, installed(("libc", "1.0")), ["app"])
        self.assertEqual([m["name"] for m in cm.exception.movers], ["libc"])

    def test_constraint_arriving_after_a_choice_forces_a_retry(self):
        """libc is decided first and takes 2.0; libb is only then discovered
        to need libc < 2.0. The already-made choice has to be invalidated and
        retried, not left in the plan violating the constraint."""
        idx = FakeIndex({
            "app": [stanza("app", "1.0", depends="libc, libb")],
            "libb": [stanza("libb", "1.0", depends="libc (<< 2.0)")],
            "libc": [stanza("libc", "2.0"), stanza("libc", "1.0")],
        })
        _, merges = self.solve(idx, installed(), ["app"])
        self.assertEqual(self.version_of(merges, "libc"), "1.0")

    def test_installed_packages_contribute_no_dependencies(self):
        """A pinned package is represented by a synthetic stanza carrying only
        its name and version, so its Depends are never walked. That is
        deliberate -- it is installed, so its dependencies are already
        satisfied on the system -- and it keeps @world from re-deriving the
        entire installed closure. Worth pinning down: it is the reason a
        constraint can only ever reach an installed package from a
        not-installed dependent."""
        idx = FakeIndex({
            "base": [stanza("base", "1.0", depends="libc (>= 2.0)")],
            "libc": [stanza("libc", "2.0"), stanza("libc", "1.0")],
        })
        _, merges = self.solve(idx, installed(("base", "1.0"),
                                              ("libc", "1.0")), ["base"])
        self.assertNotIn("libc", self.names(merges))

    def test_long_dependency_chain_does_not_exhaust_the_stack(self):
        """The search is iterative on purpose -- an @world closure is far
        deeper than Python's recursion limit."""
        table = {}
        n = 600
        for i in range(n):
            dep = f"p{i + 1}" if i + 1 < n else ""
            table[f"p{i}"] = [stanza(f"p{i}", "1.0", depends=dep)]
        _, merges = self.solve(FakeIndex(table), installed(), ["p0"])
        self.assertEqual(len(merges), n)

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

    def test_virtual_dependency_does_not_become_a_bogus_wall(self):
        """A purely virtual name has no versions of its own. Deciding whether
        to substitute a provider by asking has() never fires -- has() is true
        for a virtual name *because* it is provided -- so the name lands on the
        stack as real, fails to resolve, and is reported as an installed
        package that must move. It is not installed, so --with cannot release
        it and the user loops forever. (gir1.2-gio-2.0-dev, provided by
        gir1.2-glib-2.0-dev, hit exactly this on trixie.)"""
        idx = FakeIndex({
            "app": [stanza("app", "1.0", depends="gir1.2-gio-2.0-dev")],
            "gir1.2-glib-2.0-dev": [stanza("gir1.2-glib-2.0-dev", "2.84.4",
                                           provides="gir1.2-gio-2.0-dev")],
        })
        self.assertTrue(idx.has("gir1.2-gio-2.0-dev"))
        self.assertEqual(idx.all_versions("gir1.2-gio-2.0-dev"), [])
        _, merges = self.solve(idx, installed(), ["app"])
        self.assertEqual(self.names(merges), {"app", "gir1.2-glib-2.0-dev"})

    def test_wall_suggestion_carries_earlier_grants(self):
        """Lockstep stacks wall one package at a time; a suggestion naming
        only the newest mover drops the grants already made and bounces the
        user back to the previous wall."""
        idx = FakeIndex({
            "app": [stanza("app", "1.0",
                           depends="libc (>= 2.0), libz (>= 2.0)")],
            "libc": [stanza("libc", "2.0"), stanza("libc", "1.0")],
            "libz": [stanza("libz", "2.0"), stanza("libz", "1.0")],
        })
        with self.assertRaises(em.NduWall) as cm:
            self.solve(idx, installed(("libc", "1.0"), ("libz", "1.0")),
                       ["app"], allow={"libc"})
        self.assertIn("--with libc,libz", str(cm.exception))

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
# The --no-dep-upgrade guarantee, independent of any solver
# ---------------------------------------------------------------------------

class TestWithArg(unittest.TestCase):
    def test_names_the_new_mover(self):
        self.assertEqual(em._with_arg(set(), [{"name": "libgbm1"}]),
                         "libgbm1")

    def test_carries_packages_already_permitted(self):
        self.assertEqual(
            em._with_arg({"libgbm1"}, [{"name": "mesa-libgallium"}]),
            "libgbm1,mesa-libgallium")

    def test_sorted_and_deduplicated(self):
        self.assertEqual(
            em._with_arg({"b", "a"}, [{"name": "a"}, {"name": "c"}]),
            "a,b,c")

    def test_accepts_no_allow_set(self):
        self.assertEqual(em._with_arg(None, [{"name": "x"}]), "x")


class TestWallFromMerges(unittest.TestCase):
    """The check that enforces the flag's promise on a finished merge list --
    applied to the solver's own plan and, on the apt backend, to apt's
    simulation of it."""

    def merges(self, *rows):
        return [(n, new, old, 0, "ebuild", "") for n, new, old in rows]

    def test_installing_new_packages_is_fine(self):
        em._wall_from_merges(self.merges(("brand-new", "1.0", None)), set())

    def test_reinstalling_the_same_version_is_not_a_move(self):
        em._wall_from_merges(self.merges(("app", "1.0", "1.0")), set())

    def test_upgrading_an_installed_package_walls(self):
        with self.assertRaises(em.NduWall) as cm:
            em._wall_from_merges(self.merges(("libc", "2.0", "1.0")), set())
        self.assertEqual([m["name"] for m in cm.exception.movers], ["libc"])

    def test_allow_permits_exactly_what_it_names(self):
        rows = self.merges(("libc", "2.0", "1.0"), ("libz", "2.0", "1.0"))
        with self.assertRaises(em.NduWall) as cm:
            em._wall_from_merges(rows, {"libc"})
        self.assertEqual([m["name"] for m in cm.exception.movers], ["libz"])

    def test_fully_allowed_list_passes(self):
        rows = self.merges(("libc", "2.0", "1.0"), ("libz", "2.0", "1.0"))
        em._wall_from_merges(rows, {"libc", "libz"})

    def test_lockstep_stack_is_reported_in_one_go(self):
        """Mesa moves as a block; reporting it a package at a time is what
        made the escape hatch feel endless."""
        rows = self.merges(*[(n, "25.0.7-2+deb13u1", "25.0.7-2")
                             for n in ("libgbm1", "libglx-mesa0",
                                       "mesa-libgallium")])
        with self.assertRaises(em.NduWall) as cm:
            em._wall_from_merges(rows, set())
        self.assertEqual(len(cm.exception.movers), 3)
        self.assertIn("--with libgbm1,libglx-mesa0,mesa-libgallium",
                      str(cm.exception))
        self.assertTrue(all(m["same_upstream"] for m in cm.exception.movers))

    def test_downgrades_are_not_counted_as_upgrades(self):
        em._wall_from_merges(self.merges(("app", "1.0", "2.0")), set())


class TestAptBackendHonoursNoDepUpgrade(unittest.TestCase):
    """The apt backend resolves with the shared solver but then re-simulates
    through apt, and it is apt's plan that gets executed. Pinning the versions
    the solver chose does not stop apt from upgrading installed packages it
    was never told about, so the promise has to be re-checked on that plan."""

    def setUp(self):
        self.mod = load()
        self.be = self.mod.AptBackend()
        # the solver half is covered elsewhere; stub it so this test is about
        # what happens to apt's answer afterwards
        self.be.expand_sets = lambda targets: (list(targets), [], False)

        def stub_resolve_no_upgrade(atoms, members, update, allow=None):
            self.be._action = ["install"] + list(atoms)
            return []
        self.be._resolve_no_upgrade = stub_resolve_no_upgrade
        # plain lambdas, not staticmethod(): a staticmethod object only
        # became callable in 3.10, and an instance attribute never goes
        # through the descriptor protocol that would unwrap it
        self.be._sizes = lambda names: {}
        self.be._installed_version = lambda pkg: None

    def fake_simulation(self, stdout):
        class R:
            returncode = 0
        R.stdout, R.stderr = stdout, ""
        self.mod.capture = lambda cmd: R

    def test_extra_upgrade_in_apt_plan_is_a_wall(self):
        self.fake_simulation(
            "Inst libsdl3-dev (3.2.10+ds-1 Debian:13/stable [amd64])\n"
            "Inst libgbm1 [25.0.7-2] (25.0.7-2+deb13u1 Debian:13 [amd64])\n")
        with self.assertRaises(self.mod.NduWall) as cm:
            self.be.resolve(["libsdl3-dev"], no_dep_upgrade=True, allow=set())
        self.assertEqual([m["name"] for m in cm.exception.movers], ["libgbm1"])

    def test_allowed_upgrade_in_apt_plan_passes(self):
        self.fake_simulation(
            "Inst libgbm1 [25.0.7-2] (25.0.7-2+deb13u1 Debian:13 [amd64])\n")
        merges = self.be.resolve(["libsdl3-dev"], no_dep_upgrade=True,
                                 allow={"libgbm1"})
        self.assertEqual([m[0] for m in merges], ["libgbm1"])

    def test_new_installs_in_apt_plan_pass(self):
        self.fake_simulation(
            "Inst libsdl3-dev (3.2.10+ds-1 Debian:13/stable [amd64])\n")
        merges = self.be.resolve(["libsdl3-dev"], no_dep_upgrade=True,
                                 allow=set())
        self.assertEqual([m[0] for m in merges], ["libsdl3-dev"])

    def test_check_does_not_apply_without_the_flag(self):
        self.fake_simulation(
            "Inst libgbm1 [25.0.7-2] (25.0.7-2+deb13u1 Debian:13 [amd64])\n")
        merges = self.be.resolve(["libsdl3-dev"])
        self.assertEqual([m[0] for m in merges], ["libgbm1"])


class TestUnmergeShowsTheCascade(unittest.TestCase):
    """`apt-get remove` takes every dependent with it. Showing only the names
    the user typed and then running apt-get -y means confirming a removal
    nobody was shown -- on a desktop, `emerge -C libjpeg62-turbo` displayed
    one package and would have removed 868."""

    def setUp(self):
        self.mod = load()
        self.be = self.mod.AptBackend()
        self.mod.installed_state = lambda: {
            "libjpeg62-turbo": {"Package": "libjpeg62-turbo",
                                "Version": "1:2.1.5-4", "Priority": "optional"},
            "tree": {"Package": "tree", "Version": "2.2.1-1",
                     "Priority": "optional"},
            "bash": {"Package": "bash", "Version": "5.2", "Essential": "yes",
                     "Priority": "required"},
        }
        self.be._installed_version = lambda p: None
        self.warnings = []
        self.mod.ewarn = self.warnings.append   # keep test output quiet
        self.mod.eerror = self.warnings.append

    def sim(self, stdout, returncode=0):
        class R:
            pass
        R.stdout, R.stderr, R.returncode = stdout, "", returncode
        self.mod.capture = lambda cmd: R

    def test_dependents_are_returned_not_just_the_target(self):
        self.sim("Remv libjpeg62-turbo [1:2.1.5-4]\n"
                 "Remv kmail [4:24.12]\nRemv libgtk-3-0t64 [3.24]\n")
        got = self.be.unmerge_candidates(["libjpeg62-turbo"],
                                         {"ask": True, "pretend": False})
        self.assertEqual([p for p, _ in got],
                         ["libjpeg62-turbo", "kmail", "libgtk-3-0t64"])

    def test_cascade_without_ask_or_pretend_is_refused(self):
        """Without -a there is no confirmation step at all before apt-get -y
        runs, so this is the last place it can be stopped."""
        self.sim("Remv libjpeg62-turbo [1:2.1.5-4]\nRemv kmail [4:24.12]\n")
        with self.assertRaises(SystemExit):
            self.be.unmerge_candidates(["libjpeg62-turbo"],
                                       {"ask": False, "pretend": False})

    def test_cascade_is_allowed_under_pretend(self):
        self.sim("Remv libjpeg62-turbo [1:2.1.5-4]\nRemv kmail [4:24.12]\n")
        got = self.be.unmerge_candidates(["libjpeg62-turbo"],
                                         {"ask": False, "pretend": True})
        self.assertEqual(len(got), 2)

    def test_leaf_removal_needs_no_confirmation(self):
        self.sim("Remv tree [2.2.1-1]\n")
        got = self.be.unmerge_candidates(["tree"],
                                         {"ask": False, "pretend": False})
        self.assertEqual(got, [("tree", "2.2.1-1")])

    def test_essential_package_is_refused_by_emerge(self):
        """The help promises this; only the dpkg backend was doing it, so on
        apt it fell through to a raw apt error after the user had already
        confirmed a list showing one package."""
        self.sim("")
        with self.assertRaises(SystemExit):
            self.be.unmerge_candidates(["bash"], {"ask": True,
                                                  "pretend": False})

    def test_failed_simulation_is_reported_not_ignored(self):
        self.sim("E: Unable to satisfy dependencies\n", returncode=100)
        with self.assertRaises(SystemExit):
            self.be.unmerge_candidates(["libjpeg62-turbo"],
                                       {"ask": True, "pretend": False})

    def test_uninstalled_target_is_skipped(self):
        self.sim("")
        self.assertEqual(
            self.be.unmerge_candidates(["nosuchpkg"],
                                       {"ask": True, "pretend": False}), [])

    def test_arch_qualifier_is_stripped_from_remv_lines(self):
        self.sim("Remv tree:amd64 [2.2.1-1]\n")
        got = self.be.unmerge_candidates(["tree"],
                                         {"ask": False, "pretend": False})
        self.assertEqual(got, [("tree", "2.2.1-1")])


class TestPrintUnmergeList(unittest.TestCase):
    def render(self, removals, requested=None):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            em.print_unmerge_list(removals, requested)
        return buf.getvalue()

    def test_collateral_is_listed_and_counted(self):
        out = self.render([("libjpeg62-turbo", "1:2"), ("kmail", "4:24"),
                           ("libgtk-3-0t64", "3.24")], ["libjpeg62-turbo"])
        self.assertIn("would also be removed", out)
        self.assertIn("kmail", out)
        self.assertIn("2 additional package(s)", out)
        self.assertIn("3 in total", out)

    def test_no_collateral_section_for_a_clean_leaf(self):
        out = self.render([("tree", "2.2.1-1")], ["tree"])
        self.assertNotIn("would also be removed", out)
        self.assertNotIn("additional package", out)

    def test_requested_packages_are_the_selected_ones(self):
        out = self.render([("a", "1"), ("b", "2")], ["a"])
        self.assertIn("All selected packages: a-1\n", out)

    def test_depclean_style_call_treats_everything_as_selected(self):
        """depclean passes no `requested`; nothing there is collateral."""
        out = self.render([("a", "1"), ("b", "2")])
        self.assertNotIn("would also be removed", out)
        self.assertIn("a-1 b-2", out)


POLICY_OUTPUT = """\
nano:
  Installed: 8.4-1+deb13u1
  Candidate: 8.4-1+deb13u1
  Version table:
 *** 8.4-1+deb13u1 500
        500 http://deb.debian.org/debian trixie/main amd64 Packages
        100 /var/lib/dpkg/status
libsdl3-dev:
  Installed: (none)
  Candidate: 3.2.10+ds-1
  Version table:
     3.2.10+ds-1 500
libgbm1:amd64:
  Installed: 25.0.7-2
  Candidate: 25.0.7-2+deb13u1
"""


class TestPolicyBatch(unittest.TestCase):
    """`emerge -s '^lib'` matches 29,000 packages on Debian. Asking apt-cache
    about each one separately made that search never finish."""

    def setUp(self):
        self.mod = load()

    def stub(self, output):
        self.calls = []

        class R:
            stdout, stderr, returncode = output, "", 0

        def capture(cmd):
            self.calls.append(cmd)
            return R
        self.mod.capture = capture

    def test_parses_installed_and_candidate(self):
        self.stub(POLICY_OUTPUT)
        got = self.mod.AptBackend._policy_batch(["nano", "libsdl3-dev"])
        self.assertEqual(got["nano"], ("8.4-1+deb13u1", "8.4-1+deb13u1"))
        self.assertEqual(got["libsdl3-dev"], ("(none)", "3.2.10+ds-1"))

    def test_arch_qualifier_in_the_header_is_dropped(self):
        self.stub(POLICY_OUTPUT)
        got = self.mod.AptBackend._policy_batch(["libgbm1"])
        self.assertEqual(got["libgbm1"], ("25.0.7-2", "25.0.7-2+deb13u1"))

    def test_version_table_lines_are_not_taken_as_packages(self):
        """'Version table:' also ends in a colon, but it is indented."""
        self.stub(POLICY_OUTPUT)
        got = self.mod.AptBackend._policy_batch(["nano"])
        self.assertNotIn("Version table", got)
        self.assertEqual(len(got), 3)

    def test_queries_are_batched(self):
        self.stub("")
        self.mod.AptBackend._policy_batch([f"p{i}" for i in range(1200)],
                                          chunk=500)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(self.calls[0]), 502)   # apt-cache policy + 500

    def test_no_names_means_no_calls(self):
        self.stub("")
        self.assertEqual(self.mod.AptBackend._policy_batch([]), {})
        self.assertEqual(self.calls, [])

    def test_unknown_package_is_simply_absent(self):
        self.stub(POLICY_OUTPUT)
        got = self.mod.AptBackend._policy_batch(["nosuchpkg"])
        self.assertNotIn("nosuchpkg", got)


class TestPortability(unittest.TestCase):
    """emerge is stdlib-only so it can be scp'd onto whatever a target box
    runs, which is not necessarily a current Python. Syntax that only a new
    interpreter accepts is therefore a real defect, and one that a modern
    dev machine cannot notice by running the tests."""

    FILES = ("emerge", "test_emerge.py", "test_integration.py")

    def paths(self):
        return [os.path.join(HERE, f) for f in self.FILES]

    def test_syntax_parses_as_far_back_as_3_8(self):
        import ast
        for path in self.paths():
            with self.subTest(file=os.path.basename(path)):
                with open(path) as f:
                    src = f.read()
                ast.parse(src, feature_version=(3, 8))

    @unittest.skipIf(not hasattr(tokenize, "FSTRING_START"),
                     "f-string tokens need Python 3.12+ to inspect")
    def test_no_f_string_field_spans_a_line_break(self):
        """A replacement field split across lines is PEP 701, i.e. 3.12+.
        ast.parse(feature_version=...) does NOT reject it -- that is handled
        by the tokeniser -- so the local check passed while Python 3.9 and
        3.11 failed to compile the script at all."""
        for path in self.paths():
            with self.subTest(file=os.path.basename(path)):
                with open(path) as f:
                    src = f.read()
                depth, field_line, in_f, bad = 0, None, 0, []
                for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                    if tok.type == tokenize.FSTRING_START:
                        in_f += 1
                    elif tok.type == tokenize.FSTRING_END:
                        in_f = max(0, in_f - 1)
                    elif in_f and tok.type == tokenize.OP:
                        if tok.string == "{":
                            depth += 1
                            if depth == 1:
                                field_line = tok.start[0]
                        elif tok.string == "}":
                            if (depth == 1 and field_line is not None
                                    and tok.end[0] != field_line):
                                bad.append((field_line, tok.end[0]))
                            depth = max(0, depth - 1)
                self.assertEqual(bad, [], f"multi-line f-string fields: {bad}")

    def test_shipped_script_imports_only_the_stdlib(self):
        """Hard rule 1. CI checks this too, but failing here is faster."""
        import ast
        if not hasattr(sys, "stdlib_module_names"):
            self.skipTest("stdlib_module_names needs Python 3.10+")
        with open(os.path.join(HERE, "emerge")) as f:
            tree = ast.parse(f.read())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif (isinstance(node, ast.ImportFrom)
                  and node.level == 0 and node.module):
                mods.add(node.module.split(".")[0])
        self.assertEqual(sorted(mods - set(sys.stdlib_module_names)), [])


class TestArgParsing(unittest.TestCase):
    """A mistyped safety flag must not be silently dropped. `emerge
    --no-dep-upgrades pkg` -- one letter off -- used to run an ordinary
    unprotected install and say nothing."""

    class Reached(Exception):
        """Raised in place of backend construction: parsing got that far."""

    def setUp(self):
        self.mod = load()
        self.errors = []
        self.mod.eerror = self.errors.append
        self.mod.ewarn = lambda m: None

        def stop(_flag):
            raise self.Reached()
        self.mod.pick_backend = stop

    def parse(self, argv):
        """Run the parser only. Returns normally if it rejected the input."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.mod.main(argv)
        return buf.getvalue()

    def assertAccepted(self, argv):
        with self.assertRaises(self.Reached, msg=f"{argv} was rejected"):
            self.parse(argv)

    def assertRejected(self, argv):
        with self.assertRaises(SystemExit, msg=f"{argv} was accepted"):
            self.parse(argv)

    # -- unknown options -----------------------------------------------------

    def test_unknown_long_option_is_rejected(self):
        self.assertRejected(["--nonsense", "nano"])
        self.assertTrue(any("--nonsense" in e for e in self.errors))

    def test_mistyped_safety_flag_is_rejected(self):
        self.assertRejected(["--no-dep-upgrades", "libsdl3-dev"])

    def test_unknown_short_option_is_rejected(self):
        self.assertRejected(["-Z", "nano"])

    # -- --with token consumption -------------------------------------------

    def test_with_followed_by_an_option_is_rejected(self):
        """The pending-token check runs before flag parsing, so without a
        guard `--with -a pkg` swallowed the -a."""
        self.assertRejected(["--no-dep-upgrade", "--with", "-a", "nano"])

    def test_with_followed_by_a_long_option_is_rejected(self):
        self.assertRejected(["--with", "--help"])

    def test_trailing_with_is_rejected(self):
        self.assertRejected(["--no-dep-upgrade", "nano", "--with"])

    def test_with_takes_the_next_token(self):
        self.assertAccepted(["--no-dep-upgrade", "--with", "libgbm1", "nano"])

    def test_with_equals_form_is_accepted(self):
        self.assertAccepted(["--no-dep-upgrade", "--with=libgbm1", "nano"])

    # -- everything that must keep working -----------------------------------

    def test_all_long_flags_are_accepted(self):
        for flag in sorted(em.LONG_FLAGS):
            with self.subTest(flag=flag):
                self.setUp()
                self.assertAccepted([flag, "nano"])

    def test_standalone_long_options_are_accepted(self):
        for flag in ("--no-dep-upgrade", "--depclean", "--no-verify",
                     "--backend=apt"):
            with self.subTest(flag=flag):
                self.setUp()
                self.assertAccepted([flag, "nano"])

    def test_bundled_short_flags_are_accepted(self):
        self.assertAccepted(["-pv1", "nano"])

    def test_ignored_compatibility_flags_are_accepted(self):
        self.assertAccepted(["-Nqt", "nano"])

    def test_set_names_are_accepted(self):
        for name in ("world", "system", "@world", "@system", "@selected"):
            with self.subTest(name=name):
                self.setUp()
                self.assertAccepted(["-u", name])

    def test_help_returns_without_building_a_backend(self):
        out = self.parse(["--help"])
        self.assertIn("--oneshot", out)

    def test_no_arguments_prints_help(self):
        self.assertIn("--oneshot", self.parse([]))


class TestDispatchConf(unittest.TestCase):
    """The interactive config-merge loop, driven with scripted answers over a
    synthetic /etc. This decides what happens to files people have edited, so
    every branch needs to be pinned -- including which version becomes the
    ancestor afterwards, which is what the next upgrade's 3-way merge starts
    from and where a past bug silently discarded every update."""

    def setUp(self):
        self.mod = load()
        self.dir = tempfile.mkdtemp(prefix="emerge-dc-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.etc = os.path.join(self.dir, "etc")
        os.makedirs(self.etc)
        self.conf = dict(self.mod.DEFAULT_CONF)
        self.conf["config-protect"] = self.etc
        self.conf["archive-dir"] = os.path.join(self.dir, "archive")
        self.mod.load_conf = lambda: self.conf
        self.mod.need_root = lambda: None
        self.mod.color_diff = lambda *a, **k: None
        self.answers = []
        self.mod.input = lambda prompt="": self.answers.pop(0)

    def park(self, name, current, incoming, ancestor=None,
             suffix=".dpkg-dist"):
        """A config file plus the update dpkg parked beside it."""
        target = os.path.join(self.etc, name)
        with open(target, "w") as f:
            f.write(current)
        with open(target + suffix, "w") as f:
            f.write(incoming)
        if ancestor is not None:
            a = self.mod.archive_path(self.conf, target)
            os.makedirs(os.path.dirname(a), exist_ok=True)
            with open(a, "w") as f:
                f.write(ancestor)
        return target

    def dispatch(self, *answers):
        self.answers = list(answers)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.mod.dispatch_conf({})
        return buf.getvalue()

    def content(self, path):
        with open(path) as f:
            return f.read()

    def archived(self, target):
        return self.content(self.mod.archive_path(self.conf, target))

    def parked_exists(self, target, suffix=".dpkg-dist"):
        return os.path.exists(target + suffix)

    # -- the automatic paths -------------------------------------------------

    def test_identical_update_is_retired_without_asking(self):
        t = self.park("same.conf", "a = 1\n", "a = 1\n")
        self.dispatch()                      # no answers: must not prompt
        self.assertEqual(self.content(t), "a = 1\n")
        self.assertFalse(self.parked_exists(t))

    def test_frozen_file_keeps_yours_and_drops_the_update(self):
        t = self.park("frozen.conf", "mine\n", "theirs\n")
        self.conf["frozen-files"] = t
        self.dispatch()
        self.assertEqual(self.content(t), "mine\n")
        self.assertFalse(self.parked_exists(t))

    def test_untouched_file_takes_the_new_version(self):
        """You never edited it, so there is nothing to preserve."""
        t = self.park("clean.conf", "old\n", "new\n", ancestor="old\n")
        self.dispatch()
        self.assertEqual(self.content(t), "new\n")
        self.assertFalse(self.parked_exists(t))

    def test_comment_only_difference_is_applied(self):
        t = self.park("ws.conf", "# yours\nkey = 1\n",
                      "# theirs, rewritten\nkey  =  1\n", ancestor="x\n")
        self.dispatch()
        self.assertEqual(self.content(t), "# theirs, rewritten\nkey  =  1\n")

    def test_conflict_free_three_way_is_merged(self):
        t = self.park("merge.conf",
                      "MINE\nb\nc\n",       # you changed the first line
                      "a\nb\nTHEIRS\n",     # they changed the last
                      ancestor="a\nb\nc\n")
        self.dispatch()
        self.assertEqual(self.content(t), "MINE\nb\nTHEIRS\n")
        self.assertFalse(self.parked_exists(t))

    def test_automerge_can_be_switched_off(self):
        self.conf["automerge"] = "no"
        t = self.park("merge.conf", "MINE\nb\nc\n", "a\nb\nTHEIRS\n",
                      ancestor="a\nb\nc\n")
        self.dispatch("1")                   # falls through to the prompt
        self.assertEqual(self.content(t), "MINE\nb\nc\n")

    # -- the interactive choices ---------------------------------------------

    def conflict(self):
        return self.park("conflict.conf", "a\nMINE\nc\n", "a\nTHEIRS\nc\n",
                         ancestor="a\nb\nc\n")

    def test_choice_1_keeps_your_version(self):
        t = self.conflict()
        self.dispatch("1")
        self.assertEqual(self.content(t), "a\nMINE\nc\n")
        self.assertFalse(self.parked_exists(t))

    def test_choice_2_takes_theirs(self):
        t = self.conflict()
        self.dispatch("2")
        self.assertEqual(self.content(t), "a\nTHEIRS\nc\n")
        self.assertFalse(self.parked_exists(t))

    def test_choice_3_writes_the_merge_with_markers(self):
        t = self.conflict()
        self.dispatch("3")
        body = self.content(t)
        self.assertIn("<<<<<<< current", body)
        self.assertIn("MINE", body)
        self.assertIn("THEIRS", body)

    def test_skip_leaves_the_file_and_the_update_in_place(self):
        t = self.conflict()
        self.dispatch("s")
        self.assertEqual(self.content(t), "a\nMINE\nc\n")
        self.assertTrue(self.parked_exists(t),
                        "skipping must leave it pending for next time")

    def test_quit_stops_and_leaves_the_rest_pending(self):
        t = self.conflict()
        self.dispatch("q")
        self.assertEqual(self.content(t), "a\nMINE\nc\n")
        self.assertTrue(self.parked_exists(t))

    def test_an_unrecognised_answer_asks_again(self):
        t = self.conflict()
        self.dispatch("wat", "", "2")
        self.assertEqual(self.content(t), "a\nTHEIRS\nc\n")

    def test_end_of_input_is_treated_as_quit(self):
        def eof(prompt=""):
            raise EOFError
        self.mod.input = eof
        t = self.conflict()
        self.dispatch()
        self.assertEqual(self.content(t), "a\nMINE\nc\n")
        self.assertTrue(self.parked_exists(t))

    # -- the ancestor, which the next upgrade depends on ---------------------

    def test_keeping_yours_still_records_what_was_shipped(self):
        """The archive must hold the version the package shipped, not the one
        you kept -- otherwise the next 3-way merge starts from the wrong base
        and re-offers changes you already rejected."""
        t = self.conflict()
        self.dispatch("1")
        self.assertEqual(self.archived(t), "a\nTHEIRS\nc\n")

    def test_taking_theirs_records_it_as_the_ancestor(self):
        t = self.conflict()
        self.dispatch("2")
        self.assertEqual(self.archived(t), "a\nTHEIRS\nc\n")

    def test_skipping_does_not_touch_the_ancestor(self):
        t = self.park("skip.conf", "a\nMINE\nc\n", "a\nTHEIRS\nc\n",
                      ancestor="a\nb\nc\n")
        self.dispatch("s")
        self.assertEqual(self.archived(t), "a\nb\nc\n")

    # -- scanning ------------------------------------------------------------

    def test_ucf_dist_files_are_picked_up_too(self):
        t = self.park("ucf.conf", "mine\n", "theirs\n", ancestor="mine\n",
                      suffix=".ucf-dist")
        self.dispatch()
        self.assertEqual(self.content(t), "theirs\n")

    def test_masked_paths_are_left_alone(self):
        t = self.park("masked.conf", "mine\n", "theirs\n")
        self.conf["config-protect-mask"] = self.etc
        self.dispatch()
        self.assertEqual(self.content(t), "mine\n")
        self.assertTrue(self.parked_exists(t))

    def test_nothing_pending_is_reported_and_does_not_prompt(self):
        out = self.dispatch()
        self.assertIn("up to date", out)

    def test_a_parked_file_with_no_target_is_ignored(self):
        """dpkg only parks an update beside a file that already exists; a
        stray .dpkg-dist on its own must not be applied to nothing."""
        with open(os.path.join(self.etc, "ghost.conf.dpkg-dist"), "w") as f:
            f.write("orphan\n")
        out = self.dispatch()
        self.assertIn("up to date", out)
        self.assertFalse(os.path.exists(os.path.join(self.etc, "ghost.conf")))


class TestBumpChangelog(unittest.TestCase):
    """The +local1 entry is what keeps a locally built package from being
    clobbered by the next @world upgrade, so the version it writes has to be
    exactly right -- and it must not crash on a changelog it cannot read."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        os.makedirs(os.path.join(self.dir, "debian"))

    def write(self, text):
        with open(os.path.join(self.dir, "debian", "changelog"), "w") as f:
            f.write(text)

    def read(self):
        with open(os.path.join(self.dir, "debian", "changelog")) as f:
            return f.read()

    REAL = ("tree (2.2.1-1) unstable; urgency=medium\n"
            "\n"
            "  * New upstream release.\n"
            "\n"
            " -- Some Maintainer <m@example.org>  "
            "Mon, 01 Jan 2025 00:00:00 +0000\n")

    def test_new_entry_is_prepended(self):
        self.write(self.REAL)
        em.AptBackend._bump_changelog(self.dir, "2.2.1-1+local1")
        self.assertTrue(self.read().startswith("tree (2.2.1-1+local1) "))

    def test_the_old_content_is_kept(self):
        self.write(self.REAL)
        em.AptBackend._bump_changelog(self.dir, "2.2.1-1+local1")
        self.assertIn(self.REAL, self.read())

    def test_source_name_is_taken_from_the_existing_header(self):
        """The source name often differs from the binary package name."""
        self.write("gcc-13 (13.3.0-1) unstable; urgency=low\n")
        em.AptBackend._bump_changelog(self.dir, "13.3.0-1+local1")
        self.assertTrue(self.read().startswith("gcc-13 ("))

    def test_epoch_is_preserved(self):
        self.write("qemu (1:10.0.8+ds-1) unstable; urgency=medium\n")
        em.AptBackend._bump_changelog(self.dir, "1:10.0.8+ds-1+local1")
        self.assertIn("qemu (1:10.0.8+ds-1+local1)", self.read())

    def test_result_is_parseable_as_a_changelog_again(self):
        """It has to survive a second build, so the new first line must match
        the same header pattern."""
        self.write(self.REAL)
        em.AptBackend._bump_changelog(self.dir, "2.2.1-1+local1")
        em.AptBackend._bump_changelog(self.dir, "2.2.1-1+local2")
        self.assertTrue(self.read().startswith("tree (2.2.1-1+local2)"))

    def test_local_version_sorts_above_the_repository_one(self):
        """The whole point: @world must not consider the build outdated."""
        self.assertGreater(em.vercmp("2.2.1-1+local1", "2.2.1-1"), 0)
        self.assertLess(em.vercmp("2.2.1-1+local1", "2.2.1-2"), 0)

    def test_unreadable_header_raises_instead_of_crashing(self):
        """It used to index a None match object and die with a traceback."""
        self.write("this is not a changelog header\n")
        with self.assertRaises(RuntimeError):
            em.AptBackend._bump_changelog(self.dir, "1.0+local1")

    def test_empty_changelog_raises(self):
        self.write("")
        with self.assertRaises(RuntimeError):
            em.AptBackend._bump_changelog(self.dir, "1.0+local1")


class TestStreamApt(unittest.TestCase):
    """Portage-style output means suppressing most of apt's chatter, but a
    run that fails has to explain itself. dpkg reports the real cause on
    "dpkg: ..." lines that look nothing like apt's E:/W:, so filtering for
    those left `emerge failed; see output above` pointing at nothing."""

    FAILING = (b"Get:1 http://mirror trixie/main amd64 foo 1.0 [10 kB]\n"
               b"Unpacking foo (1.0) ...\n"
               b"Setting up foo (1.0) ...\n"
               b"Job for foo.service failed.\n"
               b"dpkg: error processing package foo (--configure):\n"
               b" installed foo post-installation script returned error 1\n"
               b"Errors were encountered while processing:\n"
               b" foo\n"
               b"E: Sub-process /usr/bin/dpkg returned an error code (1)\n")

    class Proc:
        def __init__(self, data, rc):
            self.stdout = io.BytesIO(data)
            self._rc = rc

        def wait(self):
            return self._rc

    def relay(self, data, rc, handler=lambda line: False):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            status = em.stream_apt(self.Proc(data, rc), handler)
        return status, buf.getvalue()

    def immediate(self, out):
        """Only what was printed as it arrived. Asserting against the whole
        output proves nothing: a swallowed line still turns up in the context
        dump, so both the fixed and the broken version would pass."""
        return out.split("leading up to the failure")[0]

    def test_returns_the_exit_status(self):
        self.assertEqual(self.relay(self.FAILING, 100)[0], 100)

    def test_dpkg_error_is_shown_as_an_error(self):
        _, out = self.relay(self.FAILING, 100)
        self.assertIn("dpkg: error processing package foo",
                      self.immediate(out))

    def test_the_actual_reason_is_shown_as_an_error(self):
        """The continuation line carries the message that says what broke,
        and has to stay with the error it belongs to."""
        _, out = self.relay(self.FAILING, 100)
        self.assertIn("post-installation script returned error 1",
                      self.immediate(out))

    def test_apt_summary_error_is_shown_as_an_error(self):
        _, out = self.relay(self.FAILING, 100)
        self.assertIn("E: Sub-process", self.immediate(out))

    def test_dpkg_summary_line_is_shown_as_an_error(self):
        """"Errors were encountered while processing:" names which packages
        actually broke, and matches none of the other error patterns."""
        _, out = self.relay(self.FAILING, 100)
        self.assertIn("Errors were encountered while processing:",
                      self.immediate(out))
        self.assertIn(" foo", self.immediate(out))   # its continuation

    def test_ordinary_chatter_is_not_shown_as_an_error(self):
        _, out = self.relay(self.FAILING, 100)
        self.assertNotIn("Job for foo.service", self.immediate(out))

    def test_context_is_dumped_on_failure(self):
        _, out = self.relay(self.FAILING, 100)
        self.assertIn("leading up to the failure", out)
        self.assertIn("Job for foo.service failed.", out)

    def test_nothing_extra_is_printed_on_success(self):
        _, out = self.relay(b"Selecting previously unselected package foo.\n"
                          b"Unpacking foo (1.0) ...\n", 0)
        self.assertNotIn("leading up to the failure", out)
        self.assertNotIn("Selecting previously", out)

    def test_handled_lines_are_left_to_the_handler(self):
        seen = []

        def handler(line):
            if line.startswith("Unpacking"):
                seen.append(line)
                return True
            return False
        _, out = self.relay(b"Unpacking foo (1.0) ...\n", 0, handler)
        self.assertEqual(len(seen), 1)
        self.assertEqual(out, "")

    def test_a_handled_line_ends_an_error_block(self):
        """An indented line only continues an error if an error preceded it;
        after the handler takes over, indentation is just formatting."""
        data = (b"dpkg: error processing package foo (--configure):\n"
                b"Unpacking bar (1.0) ...\n"
                b"  indented but unrelated\n")
        _, out = self.relay(data, 0, lambda l: l.startswith("Unpacking"))
        self.assertNotIn("indented but unrelated", out)

    def test_buffer_is_bounded(self):
        noise = b"".join(b"chatter line %d\n" % i for i in range(1000))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            em.stream_apt(self.Proc(noise, 1), lambda line: False, keep=10)
        out = buf.getvalue()
        self.assertIn("chatter line 999", out)
        self.assertNotIn("chatter line 100\n", out)


class TestMergeAftermath(unittest.TestCase):
    """A merge that fails partway still installed something. Those packages'
    conffiles are settled on disk and have to become the new ancestor, and
    anything dpkg parked has to be announced -- a partial install is exactly
    when you need to be told config files are waiting."""

    def setUp(self):
        self.mod = load()
        self.be = self.mod.AptBackend()
        self.be._action = ["install", "foo"]
        self.mod.need_root = lambda: None
        self.mod.load_conf = lambda: {}
        self.archived, self.noticed, self.warned = [], [], []
        self.mod.archive_settled = lambda conf, pkgs: self.archived.append(pkgs)
        self.mod.pending_notice = lambda conf: self.noticed.append(True)
        self.mod.ewarn = self.warned.append
        self.mod.einfo = lambda m: None
        self.marked = []
        self.manual = set()
        self.be._manual_set = lambda: set(self.manual)

        class R:
            stdout, stderr, returncode = "", "", 0
        self.mod.capture = lambda cmd: (self.marked.append(cmd), R)[1]

    def run_merge(self, rc, opts=None):
        class P:
            def __init__(self):
                self.stdout = io.BytesIO(b"Unpacking foo (1.0) ...\n")

            def wait(self):
                return rc
        original = self.mod.subprocess.Popen
        self.mod.subprocess.Popen = lambda *a, **k: P()
        self.addCleanup(setattr, self.mod.subprocess, "Popen", original)
        merges = [("foo", "1.0", None, 0, "ebuild", "")]
        opts = opts or {"fetchonly": False, "oneshot": False}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            if rc:
                with self.assertRaises(SystemExit):
                    self.be.merge(merges, ["foo"], opts)
            else:
                self.be.merge(merges, ["foo"], opts)
        return buf.getvalue()

    def test_successful_merge_archives_and_notifies(self):
        self.run_merge(0)
        self.assertEqual(self.archived, [["foo"]])
        self.assertTrue(self.noticed)

    def test_failed_merge_still_archives(self):
        self.run_merge(100)
        self.assertEqual(self.archived, [["foo"]])

    def test_failed_merge_still_announces_parked_config(self):
        self.run_merge(100)
        self.assertTrue(self.noticed)

    def test_failure_is_still_reported(self):
        out = self.run_merge(100)
        self.assertIn("emerge failed", out)

    def test_without_oneshot_it_reports_recording(self):
        out = self.run_merge(0)
        self.assertIn("Recording targets", out)
        self.assertEqual(self.marked, [])       # nothing demoted

    def test_oneshot_marks_the_new_atom_auto(self):
        """apt marks everything it installs as manual, and @selected *is*
        `apt-mark showmanual`, so keeping a package out of world means
        marking it back to auto."""
        self.be._atoms = ["foo"]
        self.manual = set()
        self.run_merge(0, {"fetchonly": False, "oneshot": True})
        self.assertEqual(self.marked, [["apt-mark", "auto", "foo"]])

    def test_oneshot_does_not_evict_a_package_already_in_world(self):
        """--oneshot means "do not add this to world", never "remove what was
        already there"."""
        self.be._atoms = ["foo"]
        self.manual = {"foo"}
        self.run_merge(0, {"fetchonly": False, "oneshot": True})
        self.assertEqual(self.marked, [])

    def test_oneshot_only_demotes_the_newly_added_ones(self):
        self.be._atoms = ["foo", "bar"]
        self.manual = {"foo"}
        self.run_merge(0, {"fetchonly": False, "oneshot": True})
        self.assertEqual(self.marked, [["apt-mark", "auto", "bar"]])

    def test_oneshot_never_demotes_a_dependency(self):
        """Only the atoms the user named are world candidates; dependencies
        are already auto and must not be touched."""
        self.be._atoms = ["foo"]
        self.manual = set()
        self.run_merge(0, {"fetchonly": False, "oneshot": True})
        self.assertNotIn("libdep", str(self.marked))

    def test_oneshot_reports_what_it_kept_out_of_world(self):
        self.be._atoms = ["foo"]
        out = self.run_merge(0, {"fetchonly": False, "oneshot": True})
        self.assertIn("Not recording targets", out)
        self.assertNotIn("Recording targets in", out.replace(
            "Not recording targets", ""))

    def test_a_failed_mark_warns_instead_of_lying(self):
        class R:
            stdout, stderr, returncode = "", "apt-mark exploded", 1
        self.mod.capture = lambda cmd: R
        self.be._atoms = ["foo"]
        self.run_merge(0, {"fetchonly": False, "oneshot": True})
        self.assertTrue(any("stayed in @world" in w for w in self.warned))

    def test_oneshot_is_not_applied_when_the_merge_failed(self):
        self.be._atoms = ["foo"]
        self.run_merge(100, {"fetchonly": False, "oneshot": True})
        self.assertEqual(self.marked, [])


class TestRunMergetool(unittest.TestCase):
    """Two template styles with deliberately different quoting rules."""

    def setUp(self):
        self.mod = load()
        self.cmds = []
        self.mod.subprocess.call = lambda cmd, shell=False: (
            self.cmds.append(cmd), 0)[1]
        self.addCleanup(setattr, self.mod.subprocess, "call", subprocess.call)

    def conf(self, **kw):
        c = {"mergetool": "", "merge": ""}
        c.update(kw)
        return c

    def test_no_tool_configured_is_a_no_op(self):
        self.assertFalse(self.mod.run_mergetool(
            self.conf(), "b", "m", "t", "o"))
        self.assertEqual(self.cmds, [])

    def test_named_placeholders_are_quoted(self):
        self.mod.run_mergetool(
            self.conf(mergetool="meld {mine} {base} {theirs} -o {output}"),
            "/b", "/etc/my file", "/t", "/o")
        self.assertIn("'/etc/my file'", self.cmds[0])

    def test_named_form_substitutes_dev_null_for_a_missing_base(self):
        self.mod.run_mergetool(self.conf(mergetool="x {base}"),
                               None, "m", "t", "o")
        self.assertIn("/dev/null", self.cmds[0])

    def test_positional_form_is_not_quoted_again(self):
        """dispatch-conf templates quote %s themselves. Quoting here too
        nests the quotes and splits a path with a space into two arguments."""
        self.mod.run_mergetool(
            self.conf(merge="sdiff --output='%s' '%s' '%s'"),
            "/b", "/etc/my file", "/t", "/o")
        self.assertEqual(self.cmds[0],
                         "sdiff --output='/o' '/etc/my file' '/t'")

    def test_positional_argument_order_is_output_mine_theirs(self):
        self.mod.run_mergetool(self.conf(merge="tool %s %s %s"),
                               "/b", "/mine", "/theirs", "/out")
        self.assertEqual(self.cmds[0], "tool /out /mine /theirs")

    def test_mergetool_takes_precedence_over_merge(self):
        self.mod.run_mergetool(self.conf(mergetool="A {mine}", merge="B %s"),
                               "/b", "/m", "/t", "/o")
        self.assertTrue(self.cmds[0].startswith("A "))

    def test_failure_is_reported(self):
        self.mod.subprocess.call = lambda cmd, shell=False: 1
        self.assertFalse(self.mod.run_mergetool(
            self.conf(mergetool="x {mine}"), "/b", "/m", "/t", "/o"))


class TestConfigWrite(unittest.TestCase):
    """_write installs a merged file into /etc, so it has to be atomic,
    durable, and leave nothing behind when it fails."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "conf")
        with open(self.path, "w") as f:
            f.write("original\n")
        os.chmod(self.path, 0o600)

    def test_replaces_the_content(self):
        em._write(self.path, ["new\n", "lines\n"])
        with open(self.path) as f:
            self.assertEqual(f.read(), "new\nlines\n")

    def test_preserves_mode(self):
        em._write(self.path, ["x\n"])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_leaves_no_temporary_file(self):
        em._write(self.path, ["x\n"])
        self.assertEqual(os.listdir(self.dir), ["conf"])

    def test_contents_are_flushed_before_the_rename(self):
        """A rename is atomic against readers but not against power loss."""
        synced = []
        real_fsync = os.fsync
        os.fsync = lambda fd: (synced.append(fd), real_fsync(fd))[1]
        try:
            em._write(self.path, ["x\n"])
        finally:
            os.fsync = real_fsync
        self.assertGreaterEqual(len(synced), 2)   # the file, and its directory

    def failing_lines(self):
        """Lines that blow up partway through writelines, as a full disk
        would. The failure has to happen inside _write, not before it."""
        class Exploding(list):
            def __iter__(inner):
                yield "partial\n"
                raise OSError(28, "No space left on device")
        return Exploding()

    def test_a_failed_write_leaves_the_original_intact(self):
        with self.assertRaises(OSError):
            em._write(self.path, self.failing_lines())
        with open(self.path) as f:
            self.assertEqual(f.read(), "original\n")

    def test_a_failed_write_cleans_up_its_temporary(self):
        with self.assertRaises(OSError):
            em._write(self.path, self.failing_lines())
        self.assertEqual(os.listdir(self.dir), ["conf"])


class TestAptIndexHas(unittest.TestCase):
    """_AptIndex.has() drives provider substitution, so its exact answer
    matters. These poke the caches directly rather than shelling to apt."""

    def test_real_package_is_known(self):
        idx = em._AptIndex()
        idx._cache["real"] = [stanza("real", "1.0")]
        self.assertTrue(idx.has("real"))

    def test_virtual_name_is_known_when_provided(self):
        idx = em._AptIndex()
        idx._cache["virt"] = []
        idx._provides["virt"] = [("real", "1.0")]
        self.assertTrue(idx.has("virt"))

    def test_probed_marker_alone_does_not_make_a_name_known(self):
        """provides_of() leaves an empty list behind so it does not re-shell;
        testing key presence would read that marker as 'this exists'."""
        idx = em._AptIndex()
        idx._cache["ghost"] = []
        idx._provides["ghost"] = []
        self.assertFalse(idx.has("ghost"))


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
# How session impact is presented
# ---------------------------------------------------------------------------

class TestSessionWarningTiers(unittest.TestCase):
    """A rebuild and a real version change must not read the same. Telling a
    user their session may close over a Debian point-release rebuild makes
    them plan downtime they do not need."""

    def setUp(self):
        self.mod = load()
        self.mod._session_critical_cache = {"libgbm1"}
        self.mod._session_blind = False

    def render(self, merges, verbose=False):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.mod.print_merge_list(merges, verbose)
        return buf.getvalue()

    def row(self, name, newver, oldver):
        return (name, newver, oldver, 0, "ebuild", "")

    def test_rebuild_is_marked_and_described_as_survivable(self):
        out = self.render([self.row("libgbm1", "25.0.7-2+deb13u1",
                                    "25.0.7-2")])
        self.assertIn("(session rebuild)", out)
        self.assertNotIn("(session)", out)
        self.assertIn("keeps running", out)
        self.assertNotIn("close running apps", out)

    def test_real_upgrade_keeps_the_hard_warning(self):
        out = self.render([self.row("libgbm1", "25.1.0-1", "25.0.7-2")])
        self.assertIn("(session)", out)
        self.assertNotIn("(session rebuild)", out)
        self.assertIn("close running apps", out)

    def test_both_kinds_are_reported_separately(self):
        self.mod._session_critical_cache = {"libgbm1", "libegl1"}
        out = self.render([self.row("libgbm1", "25.0.7-2+deb13u1", "25.0.7-2"),
                           self.row("libegl1", "2.0-1", "1.0-1")])
        self.assertIn("would be rebuilt: libgbm1", out)
        self.assertIn("would be upgraded: libegl1", out)

    def test_new_installs_are_never_flagged(self):
        out = self.render([self.row("libgbm1", "25.0.7-2+deb13u1", None)])
        self.assertNotIn("session", out)

    def test_non_session_package_is_not_flagged(self):
        out = self.render([self.row("nano", "2.0", "1.0")])
        self.assertNotIn("session", out)

    def test_headless_flags_nothing(self):
        self.mod._session_critical_cache = set()
        out = self.render([self.row("libgbm1", "25.1.0-1", "25.0.7-2")])
        self.assertNotIn("session", out)

    # -- the same split inside a --no-dep-upgrade wall -----------------------

    def mover(self, same_upstream):
        return [{"name": "libgbm1", "installed": "25.0.7-2",
                 "wanted": "25.0.7-2+deb13u1" if same_upstream else "25.1.0-1",
                 "why": "libgbm-dev", "same_upstream": same_upstream,
                 "session_critical": True}]

    def test_wall_softens_for_a_rebuild(self):
        text = "\n".join(self.mod._format_movers(self.mover(True)))
        self.assertIn("session in use", text)
        self.assertIn("keeps running", text)
        self.assertNotIn("close running apps", text)

    def test_wall_stays_loud_for_a_real_upgrade(self):
        text = "\n".join(self.mod._format_movers(self.mover(False)))
        self.assertIn("session-critical", text)
        self.assertIn("close running apps", text)


# ---------------------------------------------------------------------------
# Repository signature verification
# ---------------------------------------------------------------------------

def armour(payload, headers=""):
    b64 = base64.b64encode(payload).decode()
    body = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
    return ("-----BEGIN PGP PUBLIC KEY BLOCK-----\n"
            + (headers + "\n" if headers else "")
            + "\n" + body + "\n=AbCd\n"
            + "-----END PGP PUBLIC KEY BLOCK-----\n").encode()


def clearsign(body, headers="Hash: SHA512"):
    return ("-----BEGIN PGP SIGNED MESSAGE-----\n" + headers + "\n\n"
            + body
            + "-----BEGIN PGP SIGNATURE-----\n\nAAAA\n-----END PGP "
              "SIGNATURE-----\n")


class TestDearmor(unittest.TestCase):
    def test_binary_keyring_passes_through(self):
        raw = b"\x99\x01\x0d\x04binary key packets"
        self.assertEqual(em.dearmor(raw), raw)

    def test_armoured_block_is_decoded(self):
        self.assertEqual(em.dearmor(armour(b"key packets here")),
                         b"key packets here")

    def test_armour_headers_are_skipped(self):
        self.assertEqual(
            em.dearmor(armour(b"payload", headers="Version: GnuPG v2")),
            b"payload")

    def test_crc_trailer_is_not_decoded(self):
        """The '=AbCd' line is a CRC24 checksum, not key material. Feeding it
        to the decoder appends three junk bytes to the keyring."""
        self.assertEqual(em.dearmor(armour(b"abc")), b"abc")
        payload = b"x" * 48          # encodes without '=' padding of its own
        self.assertEqual(em.dearmor(armour(payload)), payload)

    def test_multiple_blocks_concatenate(self):
        """A keyring file may hold several keys; gpgv wants them all."""
        both = armour(b"first") + armour(b"second")
        self.assertEqual(em.dearmor(both), b"firstsecond")

    def test_undecodable_block_does_not_raise(self):
        junk = (b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n!!!not base64!!!\n"
                b"-----END PGP PUBLIC KEY BLOCK-----\n")
        self.assertEqual(em.dearmor(junk), b"")


class TestClearsignedPayload(unittest.TestCase):
    def test_extracts_the_signed_body(self):
        body = "Origin: Debian\nSuite: stable\n"
        self.assertEqual(em.clearsigned_payload(clearsign(body)), body)

    def test_body_keeps_its_final_newline_only_once(self):
        """The newline before the signature armour terminates the last line;
        emitting it again makes the payload one byte longer than the detached
        Release it must match."""
        body = "A: 1\nB: 2\n"
        self.assertEqual(len(em.clearsigned_payload(clearsign(body))),
                         len(body))

    def test_dash_escaping_is_undone(self):
        signed = clearsign("- -----BEGIN SOMETHING-----\nreal line\n")
        self.assertEqual(em.clearsigned_payload(signed),
                         "-----BEGIN SOMETHING-----\nreal line\n")

    def test_multiple_hash_headers(self):
        body = "X: 1\n"
        self.assertEqual(
            em.clearsigned_payload(clearsign(body, "Hash: SHA256\nHash: SHA1")),
            body)

    def test_not_clearsigned_returns_none(self):
        self.assertIsNone(em.clearsigned_payload("Origin: Debian\n"))

    def test_missing_signature_block_returns_none(self):
        self.assertIsNone(em.clearsigned_payload(
            "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\nbody\n"))


RELEASE_SAMPLE = """\
Origin: Debian
Suite: stable
MD5Sum:
 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 1234 main/binary-amd64/Packages
 bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb 99 md5-only/Packages
SHA256:
 1111111111111111111111111111111111111111111111111111111111111111 1234 main/binary-amd64/Packages
 2222222222222222222222222222222222222222222222222222222222222222 567 main/binary-amd64/Packages.xz
Acquire-By-Hash: yes
"""


class TestReleaseHashes(unittest.TestCase):
    def test_reads_the_sha256_section(self):
        h = em.release_hashes(RELEASE_SAMPLE)
        self.assertEqual(h["main/binary-amd64/Packages"], "1" * 64)
        self.assertEqual(h["main/binary-amd64/Packages.xz"], "2" * 64)

    def test_md5_section_is_ignored(self):
        """MD5Sum lists the same paths and is written first, so a parser that
        read both would be saved only by SHA256 overwriting it. A path that
        appears solely under MD5Sum must not show up at all."""
        h = em.release_hashes(RELEASE_SAMPLE)
        self.assertNotIn("md5-only/Packages", h)
        self.assertNotIn("a" * 32, h.values())

    def test_section_ends_at_the_next_field(self):
        self.assertNotIn("yes", em.release_hashes(RELEASE_SAMPLE))
        self.assertEqual(len(em.release_hashes(RELEASE_SAMPLE)), 2)

    def test_release_without_sha256(self):
        self.assertEqual(em.release_hashes("Origin: Debian\n"), {})


class TestVerifier(unittest.TestCase):
    """gpgv itself is stubbed here -- these cover the decisions made around
    it. The signature check is exercised for real against the live archive."""

    def setUp(self):
        self.mod = load()
        self.patch(self.mod.shutil, "which", lambda n: "/usr/bin/" + n)
        self.warnings = []
        self.mod.ewarn = self.warnings.append   # keep test output quiet

    def patch(self, obj, attr, value):
        original = getattr(obj, attr)
        setattr(obj, attr, value)
        self.addCleanup(setattr, obj, attr, original)

    def make(self, gpgv_ok=True, files=None, enabled=True):
        v = self.mod.Verifier(enabled)
        self.addCleanup(v.close)
        v._gpgv = lambda ring, sig, data=None: gpgv_ok
        v._rings[None] = "/nonexistent/keyring.gpg"   # skip real keyring build
        files = files or {}

        def fake_fetch(url, timeout=60):
            for suffix, payload in files.items():
                if url.endswith(suffix):
                    return payload
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        self.mod.fetch = fake_fetch
        return v

    def test_inrelease_is_verified_and_unwrapped(self):
        v = self.make(files={"InRelease": clearsign(RELEASE_SAMPLE).encode()})
        self.assertEqual(v.release("http://x", "trixie"), RELEASE_SAMPLE)

    def test_detached_release_is_used_when_no_inrelease(self):
        v = self.make(files={"Release": RELEASE_SAMPLE.encode(),
                             "Release.gpg": b"signature"})
        self.assertEqual(v.release("http://x", "trixie"), RELEASE_SAMPLE)

    def test_bad_inrelease_signature_is_fatal(self):
        v = self.make(gpgv_ok=False,
                      files={"InRelease": clearsign(RELEASE_SAMPLE).encode()})
        with self.assertRaises(RuntimeError) as cm:
            v.release("http://x", "trixie")
        self.assertIn("FAILED", str(cm.exception))

    def test_bad_detached_signature_is_fatal(self):
        v = self.make(gpgv_ok=False,
                      files={"Release": RELEASE_SAMPLE.encode(),
                             "Release.gpg": b"signature"})
        with self.assertRaises(RuntimeError):
            v.release("http://x", "trixie")

    def test_missing_release_warns_but_does_not_raise(self):
        """A USB-stick repo has no Release at all; that must not be fatal."""
        v = self.make(files={})
        self.assertIsNone(v.release("http://x", "trixie"))
        self.assertTrue(v.had_warnings)

    def test_disabled_verifier_checks_nothing(self):
        v = self.make(enabled=False,
                      files={"InRelease": clearsign(RELEASE_SAMPLE).encode()})
        self.assertIsNone(v.release("http://x", "trixie"))

    def test_release_is_fetched_once_per_suite(self):
        calls = []
        v = self.make(files={"InRelease": clearsign(RELEASE_SAMPLE).encode()})
        inner = self.mod.fetch
        self.mod.fetch = lambda url, timeout=60: (calls.append(url),
                                                  inner(url, timeout))[1]
        v.release("http://x", "trixie")
        v.release("http://x", "trixie")
        self.assertEqual(len(calls), 1)

    # -- check_index ---------------------------------------------------------

    def index_verifier(self):
        return self.make(files={"InRelease": clearsign(RELEASE_SAMPLE).encode()})

    def test_matching_index_verifies(self):
        v = self.index_verifier()
        data = b"payload"
        digest = hashlib.sha256(data).hexdigest()
        self.patch(self.mod, "release_hashes",
                   lambda text: {"main/binary-amd64/Packages": digest})
        self.assertTrue(v.check_index("http://x", "trixie", None,
                                      "main/binary-amd64/Packages", data))
        self.assertEqual(v.checked, 1)

    def test_mismatching_index_raises(self):
        v = self.index_verifier()
        with self.assertRaises(RuntimeError) as cm:
            v.check_index("http://x", "trixie", None,
                          "main/binary-amd64/Packages", b"tampered")
        self.assertIn("SHA256 mismatch", str(cm.exception))

    def test_index_absent_from_release_warns(self):
        v = self.index_verifier()
        self.assertFalse(v.check_index("http://x", "trixie", None,
                                       "main/binary-i386/Packages", b"x"))
        self.assertTrue(v.had_warnings)

    def test_unverifiable_release_means_unchecked_not_failed(self):
        v = self.make(files={})
        self.assertFalse(v.check_index("http://x", "trixie", None,
                                       "main/binary-amd64/Packages", b"x"))

    # -- keyring assembly ----------------------------------------------------

    def test_keyring_merges_every_trusted_key(self):
        """gpgv fails a file whose signatures it cannot all check, and Debian
        signs InRelease several times, so the keys must arrive together."""
        with tempfile.TemporaryDirectory() as d:
            for name, payload in (("a.asc", b"AAA"), ("b.gpg", b"BBB")):
                with open(os.path.join(d, name), "wb") as f:
                    f.write(armour(payload) if name.endswith(".asc")
                            else payload)
            v = self.mod.Verifier(True)
            self.addCleanup(v.close)
            self.patch(self.mod, "TRUSTED_DIR", d)
            self.patch(self.mod, "TRUSTED_LEGACY", "/nonexistent")
            with open(v.keyring(), "rb") as f:
                self.assertEqual(f.read(), b"AAABBB")

    def test_keyring_includes_the_legacy_trusted_gpg(self):
        """Pre-deb822 systems keep keys in /etc/apt/trusted.gpg; dropping it
        would silently stop trusting repositories that still rely on it."""
        with tempfile.TemporaryDirectory() as d:
            legacy = os.path.join(d, "trusted.gpg")
            with open(legacy, "wb") as f:
                f.write(b"LEGACY")
            v = self.mod.Verifier(True)
            self.addCleanup(v.close)
            self.patch(self.mod, "TRUSTED_DIR", "/nonexistent")
            self.patch(self.mod, "TRUSTED_LEGACY", legacy)
            with open(v.keyring(), "rb") as f:
                self.assertEqual(f.read(), b"LEGACY")

    def test_signed_by_pins_one_keyring(self):
        with tempfile.TemporaryDirectory() as d:
            named = os.path.join(d, "repo.gpg")
            with open(named, "wb") as f:
                f.write(b"ONLYTHIS")
            v = self.mod.Verifier(True)
            self.addCleanup(v.close)
            self.patch(self.mod, "TRUSTED_DIR", d)
            with open(v.keyring(named), "rb") as f:
                self.assertEqual(f.read(), b"ONLYTHIS")

    def test_inline_signed_by_key_material(self):
        v = self.mod.Verifier(True)
        self.addCleanup(v.close)
        with open(v.keyring(armour(b"INLINE").decode()), "rb") as f:
            self.assertEqual(f.read(), b"INLINE")

    def test_no_keys_yields_no_keyring(self):
        v = self.mod.Verifier(True)
        self.addCleanup(v.close)
        self.patch(self.mod, "TRUSTED_DIR", "/nonexistent")
        self.patch(self.mod, "TRUSTED_LEGACY", "/nonexistent")
        self.assertIsNone(v.keyring())

    def test_close_removes_temporary_files(self):
        v = self.mod.Verifier(True)
        path = v._tmpfile(b"data", ".gpg")
        self.assertTrue(os.path.exists(path))
        v.close()
        self.assertFalse(os.path.exists(path))

    def test_missing_gpgv_disables_verification(self):
        self.patch(self.mod.shutil, "which", lambda n: None)
        v = self.mod.Verifier(True)
        self.addCleanup(v.close)
        self.assertFalse(v.enabled)
        self.assertTrue(v.wanted)


class TestReadSourcesSignedBy(unittest.TestCase):
    """read_sources reads fixed paths under /etc/apt, so this fakes that
    corner of the filesystem rather than touching the real one."""

    def setUp(self):
        self.mod = load()

    def parse(self, files):
        mod = self.mod

        def fake_isfile(p):
            return p in files

        def fake_isdir(p):
            return p == "/etc/apt/sources.list.d"

        def fake_listdir(p):
            prefix = "/etc/apt/sources.list.d/"
            return sorted(k[len(prefix):] for k in files
                          if k.startswith(prefix))

        def fake_open(path, *a, **kw):
            return io.StringIO(files[path])

        for obj, attr, val in ((mod.os.path, "isfile", fake_isfile),
                               (mod.os.path, "isdir", fake_isdir),
                               (mod.os, "listdir", fake_listdir)):
            original = getattr(obj, attr)
            setattr(obj, attr, val)
            self.addCleanup(setattr, obj, attr, original)
        mod.open = fake_open
        return mod.read_sources()

    def test_plain_one_line_entry(self):
        got = self.parse({"/etc/apt/sources.list":
                          "deb http://deb.debian.org/debian trixie main\n"})
        self.assertEqual(got, [("http://deb.debian.org/debian", "trixie",
                                ["main"], None)])

    def test_one_line_signed_by_is_captured(self):
        got = self.parse({"/etc/apt/sources.list":
                          "deb [signed-by=/usr/share/keyrings/x.gpg] "
                          "http://r/ trixie main\n"})
        self.assertEqual(got[0][3], "/usr/share/keyrings/x.gpg")

    def test_signed_by_among_other_options(self):
        got = self.parse({"/etc/apt/sources.list":
                          "deb [arch=amd64 signed-by=/k.gpg trusted=no] "
                          "http://r/ trixie main contrib\n"})
        self.assertEqual(got[0][3], "/k.gpg")
        self.assertEqual(got[0][2], ["main", "contrib"])

    def test_options_block_is_still_stripped_from_the_url(self):
        got = self.parse({"/etc/apt/sources.list":
                          "deb [arch=amd64] http://r/x trixie main\n"})
        self.assertEqual(got[0][0], "http://r/x")

    def test_deb822_signed_by(self):
        got = self.parse({"/etc/apt/sources.list.d/docker.sources":
                          "Types: deb\nURIs: https://download.docker.com/d\n"
                          "Suites: trixie\nComponents: stable\n"
                          "Signed-By: /etc/apt/keyrings/docker.asc\n"})
        self.assertEqual(got, [("https://download.docker.com/d", "trixie",
                                ["stable"], "/etc/apt/keyrings/docker.asc")])

    def test_deb822_without_signed_by(self):
        got = self.parse({"/etc/apt/sources.list.d/x.sources":
                          "Types: deb\nURIs: http://r\nSuites: trixie\n"
                          "Components: main\n"})
        self.assertIsNone(got[0][3])

    def test_deb_src_lines_are_skipped(self):
        got = self.parse({"/etc/apt/sources.list":
                          "deb-src http://r trixie main\n"
                          "deb http://r trixie main\n"})
        self.assertEqual(len(got), 1)

    def test_comments_are_skipped(self):
        got = self.parse({"/etc/apt/sources.list":
                          "# deb http://evil trixie main\n"
                          "deb http://r trixie main\n"})
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], "http://r")


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

    def no_maps(self):
        """Make the maps read fail outright. Leaving it unfaked reads the
        real /proc/PID/maps, which passes only where that happens to be
        unreadable -- true for an ordinary user, false for root."""
        def denied(path, *a, **kw):
            raise PermissionError(13, "Permission denied", str(path))
        self.mod.open = denied

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
        # deliberately our own pid, whose maps is always readable: if the
        # fake below is ever dropped this fails everywhere instead of
        # passing wherever /proc/1/maps happens to be off limits, which is
        # how it passed as a user and failed as root in a container
        self.no_maps()
        self.patch(self.mod.os, "readlink", lambda _p: "/usr/bin/kwin_wayland")
        self.assertEqual(
            self.mod._proc_mapped_code(str(os.getpid()), "kwin_wayland"),
            {"/usr/bin/kwin_wayland"})

    def test_truncated_comm_without_exe_yields_nothing(self):
        """comm is capped at 15 chars, so some names cannot be resolved; that
        is a miss, not a crash."""
        self.no_exe()
        self.patch(self.mod.shutil, "which", lambda _c: None)
        self.fake_maps(MAPS_LIBS_ONLY)
        files = self.mod._proc_mapped_code("1", "gdm-session-wor")
        self.assertEqual(files, {"/usr/lib/libfoo.so.1"})

    # -- how much a move actually disturbs the session -----------------------

    def test_impact_none_when_not_session_code(self):
        self.force({"libgbm1"}, False)
        self.assertIsNone(self.mod.session_impact("nano", "1.0", "2.0"))

    def test_same_upstream_bump_is_only_a_rebuild(self):
        """Mesa 25.0.7-2 -> 25.0.7-2+deb13u1 is a point-release rebuild. A
        running process keeps the inodes it already mapped, so nothing
        restarts and nothing closes."""
        self.force({"libgbm1"}, False)
        self.assertEqual(
            self.mod.session_impact("libgbm1", "25.0.7-2", "25.0.7-2+deb13u1"),
            "rebuild")

    def test_real_version_change_is_an_upgrade(self):
        self.force({"libgbm1"}, False)
        self.assertEqual(
            self.mod.session_impact("libgbm1", "25.0.7-2", "25.1.0-1"),
            "upgrade")

    def test_binnmu_is_a_rebuild(self):
        self.force({"libgbm1"}, False)
        self.assertEqual(
            self.mod.session_impact("libgbm1", "1.0-1", "1.0-1+b1"), "rebuild")

    def test_missing_versions_are_treated_as_an_upgrade(self):
        self.force({"libgbm1"}, False)
        self.assertEqual(self.mod.session_impact("libgbm1", None, "1.0"),
                         "upgrade")

    def test_leaders_are_returned_with_their_comm(self):
        self.patch(self.mod.os, "listdir", lambda _p: ["1", "2", "self"])
        self.patch(self.mod, "_proc_comm",
                   lambda pid: {"1": "kwin_wayland", "2": "bash"}.get(pid))
        self.assertEqual(self.mod._find_session_leaders(),
                         [("1", "kwin_wayland")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
