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
"""Unit tests for emerge.

The shipped artifact is a single extensionless script, so it is loaded here
by path rather than imported. Run with:  python3 -m unittest -v test_emerge
(or just ./test_emerge.py). Stdlib only, like the thing under test.

Tests that compare against real Debian tools (dpkg, diff3) skip themselves
when those tools are absent, so the suite still runs on a non-Debian box.
"""

import base64
import contextlib
import fcntl
import gzip
import hashlib
import lzma
import importlib.machinery
import importlib.util
import io
import itertools
import os
import pty
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tokenize
import types
import unittest
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "emerge")


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
	if _SCRATCH_ROOT is not None:
		shutil.rmtree(_SCRATCH_ROOT, ignore_errors=True)
	changed = [name for name in _WATCHED if _resolve(name) is not _SNAPSHOT[name]]
	if changed:
		raise AssertionError(
		    "this module left shared standard-library functions patched: "
		    + ", ".join(changed)
		    + ". Capture the original *before* replacing it and restore it "
		      "with addCleanup, or the damage lands in whatever runs next.")


_SCRATCH_ROOT = None
_SCRATCH_SEQ = 0


def _scratch():
	"""A unique, unwritten path under one temp root, cleaned up at the end.

	Unit tests must never touch system state, and one silently did: as root,
	constructing DpkgBackend seeds the world file, so a bare `python3 -m
	unittest test_emerge` created a real /var/lib/emerge-dpkg/world with 135
	entries. Invisible as an ordinary user -- the seed guard returns early --
	and found by running the suite in a container as root.

	The paths are handed out rather than created; the code under test makes
	whatever it actually needs."""
	global _SCRATCH_ROOT, _SCRATCH_SEQ
	if _SCRATCH_ROOT is None:
		_SCRATCH_ROOT = tempfile.mkdtemp(prefix="emerge-unit-")
	_SCRATCH_SEQ += 1
	return os.path.join(_SCRATCH_ROOT, str(_SCRATCH_SEQ))


def load():
	loader = importlib.machinery.SourceFileLoader("emerge_under_test", SCRIPT)
	spec = importlib.util.spec_from_loader(loader.name, loader)
	mod = importlib.util.module_from_spec(spec)
	# Import with a stdout that is not a terminal, so the copy comes out
	# with USE_COLOR false. The script decides that once, at import, from
	# sys.stdout.isatty(), and bakes the answer into constants -- ARROW is
	# already BGREEN(">>>") by the time anything here could set the flag.
	# Without this, a test asserting on output passes when the suite is
	# piped and fails when it is run from a terminal, which is how
	# `make deb` broke for one person and nobody else.
	with contextlib.redirect_stdout(io.StringIO()):
		loader.exec_module(mod)
	# every path the dpkg backend writes to, out of the way of the system
	scratch = _scratch()
	mod.LIB_DIR = os.path.join(scratch, "lib")
	mod.TREE_DIR = os.path.join(scratch, "lib", "tree")
	mod.WORLD = os.path.join(scratch, "lib", "world")
	mod.DISTFILES = os.path.join(scratch, "distfiles")
	mod.BINPKGS = os.path.join(scratch, "binpkgs")
	mod.PORTAGE_TMPDIR = os.path.join(scratch, "portage")
	# Pretend we are headless: keeps tests off /proc and away from a
	# dpkg-query fork, and makes session annotations deterministic.
	mod._session_critical_cache = set()
	mod._session_blind = False
	return mod


em = load()

HAVE_DPKG = shutil.which("dpkg") is not None
HAVE_DIFF3 = shutil.which("diff3") is not None


class TestTheHarness(unittest.TestCase):
	"""What load() has to normalise for a test to mean the same thing
    wherever it runs."""

	def test_a_loaded_copy_never_colours_its_output(self):
		"""Six places used to turn colour off, and one of them forgot, so
        `Emerging (1 of 2) libb` was really `Emerging (1 of 2) \033[1;32m...`
        and the test passed only when stdout was a pipe. Running the suite
        from a terminal failed it, which is how `make deb` broke for the
        one person running it by hand. load() owns this now; asserting the
        flag alone is not enough, because ARROW is coloured at import and
        setting the flag afterwards would leave it that way."""
		mod = load()
		self.assertFalse(mod.USE_COLOR)
		self.assertEqual(mod.ARROW, ">>>")
		self.assertEqual(mod.BGREEN("x"), "x")


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


class TestWantColor(unittest.TestCase):
	"""NO_COLOR, and the isatty fallback underneath it."""

	class Stream:
		def __init__(self, tty):
			self.tty = tty

		def isatty(self):
			return self.tty

	def want(self, env, tty=True):
		return em.want_color(env, self.Stream(tty))

	def test_a_terminal_gets_colour(self):
		self.assertTrue(self.want({}))

	def test_a_pipe_does_not(self):
		self.assertFalse(self.want({}, tty=False))

	def test_no_color_turns_it_off(self):
		self.assertFalse(self.want({"NO_COLOR": "1"}))

	def test_no_color_is_presence_not_truth(self):
		"""The convention is that the variable being set is the signal,
        whatever it contains, so NO_COLOR=0 disables colour too. Reading
        the value is the mistake it exists to prevent: every tool would
        then pick its own spelling of "off" and none would agree."""
		self.assertFalse(self.want({"NO_COLOR": "0"}))
		self.assertFalse(self.want({"NO_COLOR": "false"}))
		self.assertFalse(self.want({"NO_COLOR": "no"}))

	def test_an_empty_no_color_counts_as_unset(self):
		"""Also the standard, and the case an `if "NO_COLOR" in env` would
        get wrong -- an exported-but-empty variable is how a shell leaves
        one behind."""
		self.assertTrue(self.want({"NO_COLOR": ""}))

	def test_portages_nocolor_turns_it_off(self):
		"""NOCOLOR is a make.conf variable, so it is read for its value
        rather than its presence. Case and surrounding space are not the
        user's problem."""
		for value in ("true", "yes", "1", "True", "YES", " true "):
			with self.subTest(value=value):
				self.assertFalse(self.want({"NOCOLOR": value}))

	def test_nocolor_saying_no_leaves_colour_alone(self):
		for value in ("false", "no", "0", "", "banana"):
			with self.subTest(value=value):
				self.assertTrue(self.want({"NOCOLOR": value}))

	def test_the_two_spellings_disagree_about_zero(self):
		"""The trap, pinned in one place because it looks like a typo.
        They are one character apart and mean opposite things by the same
        string: NO_COLOR is presence, so =0 disables; NOCOLOR is a value,
        so =0 does not. Neither can be implemented in terms of the other,
        and a future tidy-up that merges them breaks exactly this."""
		self.assertFalse(self.want({"NO_COLOR": "0"}))
		self.assertTrue(self.want({"NOCOLOR": "0"}))

	def test_it_is_wired_to_the_real_output(self):
		"""The function decides nothing unless USE_COLOR calls it, and a
        flag that never reaches the thing acting on it is the shape of two
        bugs already shipped here. So this drives the installed script
        through a real pty and reads the bytes back: without NO_COLOR the
        einfo carries an escape sequence, with it the same line is plain."""
		def run(env):
			# The slave stays open while reading. Once every slave fd is
			# closed, Linux answers EIO on the master rather than handing
			# back what is still buffered, so draining it non-blocking with
			# the slave held open is the way to get the bytes at all.
			master, slave = pty.openpty()
			try:
				# stderr goes to the same pty, as a terminal gives it:
				# "no targets given" is an eerror, so it arrives there.
				subprocess.run([sys.executable, SCRIPT, "-p"], stdout=slave,
				               stderr=slave, env=env, timeout=60)
				os.set_blocking(master, False)
				out = b""
				while True:
					try:
						chunk = os.read(master, 4096)
					except (BlockingIOError, OSError):
						break
					if not chunk:
						break
					out += chunk
				return out.decode("utf-8", "replace")
			finally:
				os.close(slave)
				os.close(master)

		base = {k: v for k, v in os.environ.items()
		        if k not in ("NO_COLOR", "NOCOLOR")}
		coloured = run(base)
		self.assertIn("no targets given", coloured)
		self.assertIn("\033[", coloured, "a terminal should have got colour")
		for var, value in (("NO_COLOR", "1"), ("NOCOLOR", "true")):
			with self.subTest(variable=var):
				plain = run(dict(base, **{var: value}))
				self.assertIn("no targets given", plain)
				self.assertNotIn("\033[", plain)


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

	@unittest.skipUnless(HAVE_DPKG, "dpkg not available")
	def test_random_versions_agree_with_dpkg(self):
		"""The table above is hand-written, so it encodes what its author
		believed policy 5.6.12 says. This generates versions instead and
		asks dpkg, which encodes what policy actually is -- the same reason
		the table is differential in the first place, applied to inputs
		nobody chose.

		Seeded, so a disagreement is reproducible. The alphabet is small and
		full of the awkward characters (`~`, `+`, leading zeros, epochs) so
		that collisions and edge cases turn up rather than being drowned in
		random noise."""
		rnd = random.Random(20260804)

		def version():
			body = "".join(rnd.choice("012ab.~+")
			               for _ in range(rnd.randint(1, 6)))
			if body[0] not in "012":
				body = rnd.choice("012") + body
			if rnd.random() < 0.35:
				body += "-" + "".join(rnd.choice("012ab.~+")
				                      for _ in range(rnd.randint(1, 3)))
			if rnd.random() < 0.25:
				body = f"{rnd.randint(0, 2)}:" + body
			return body

		for _ in range(120):
			a, b = version(), version()
			with self.subTest(a=a, b=b):
				self.assertEqual(sign(em.vercmp(a, b)), dpkg_cmp(a, b))

	def test_a_malformed_epoch_compares_instead_of_raising(self):
		"""int() on a non-numeric epoch raised ValueError, and vercmp sits on
        every code path there is -- a single odd string in an index would
        take down an operation that had nothing to do with it."""
		for odd in (":1.0", "a:1.0", "1.0:2", ":", "x:y:z"):
			with self.subTest(v=odd):
				self.assertEqual(em.vercmp(odd, odd), 0)
				self.assertEqual(sign(em.vercmp(odd, "1.0")),
				                 -sign(em.vercmp("1.0", odd)))

	def test_a_real_epoch_still_outranks_a_bare_version(self):
		"""The lenient fallback must not swallow epochs that are valid."""
		self.assertEqual(sign(em.vercmp("1:0.1", "9.9")), 1)
		self.assertEqual(sign(em.vercmp("2:1.0", "1:9.0")), 1)

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
		em._DEP_WARNED.clear()
		with contextlib.redirect_stderr(io.StringIO()):
			self.assertEqual(em.parse_depends("!!! , libc6"),
			                 [[("libc6", None, None)]])

	def test_an_unparsable_clause_says_so(self):
		"""Dropping it silently makes an unparseable dependency look
        *satisfied*, which is the one direction a resolver must never fail
        in. It cannot be fatal -- one odd field would break every operation
        -- but it must not pass unremarked."""
		em._DEP_WARNED.clear()
		err = io.StringIO()
		with contextlib.redirect_stderr(err):
			em.parse_depends("!!! , libc6")
		self.assertIn("cannot parse dependency", err.getvalue())
		self.assertIn("!!!", err.getvalue())

	def test_the_complaint_is_made_once_not_per_stanza(self):
		"""parse_depends runs over every stanza in the index; a per-call
        warning would print the same line thousands of times."""
		em._DEP_WARNED.clear()
		err = io.StringIO()
		with contextlib.redirect_stderr(err):
			for _ in range(50):
				em.parse_depends("!!! , libc6")
		self.assertEqual(err.getvalue().count("cannot parse dependency"), 1)

	def test_a_well_formed_field_says_nothing(self):
		em._DEP_WARNED.clear()
		err = io.StringIO()
		with contextlib.redirect_stderr(err):
			em.parse_depends("libc6 (>= 2.36), libgcc-s1 | libgcc1, ")
		self.assertEqual(err.getvalue(), "")


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

	def _fuzz_lines(self, rnd, alphabet="abcde", lo=0, hi=8):
		return [rnd.choice(alphabet) + "\n"
		        for _ in range(rnd.randint(lo, hi))]

	def _fuzz_mutate(self, rnd, lines):
		out = list(lines)
		for _ in range(rnd.randint(1, 4)):
			if not out or rnd.random() < 0.3:
				out.insert(rnd.randint(0, len(out)),
				           rnd.choice("XYZW") + "\n")
			elif rnd.random() < 0.5 and len(out) > 1:
				out.pop(rnd.randrange(len(out)))
			else:
				out[rnd.randrange(len(out))] = rnd.choice("XYZW") + "\n"
		return out

	def test_the_stated_rules_hold_under_fuzzing(self):
		"""The four rules merge3 documents, on random inputs.

		This is the contract, and it is worth property-testing rather than
		exampling: byte-equality with diff3 is *not* the contract and was
		measured not to hold, but these four are absolute. A violation would
		mean a config merge silently dropping one side's change.

		Seeded, so a failure is reproducible rather than a rumour."""
		rnd = random.Random(4242)
		for i in range(600):
			base = self._fuzz_lines(rnd)
			other = self._fuzz_mutate(rnd, base)
			with self.subTest(i=i, base=base, other=other):
				out, n = em.merge3(base, list(base), other)
				self.assertEqual((out, n), (other, 0), "only they changed it")

				out, n = em.merge3(base, other, list(base))
				self.assertEqual((out, n), (other, 0), "only you changed it")

				out, n = em.merge3(base, other, list(other))
				self.assertEqual((out, n), (other, 0), "same change both sides")

				out, n = em.merge3(base, list(base), list(base))
				self.assertEqual((out, n), (base, 0), "nobody changed anything")

	def test_a_conflict_carries_all_three_sides(self):
		"""Whatever alignment is chosen, a conflict block has to show what
		you have, what was shipped, and what is new -- losing the ancestor
		is what makes a conflict unresolvable by hand."""
		out, n = em.merge3(L("a"), L("m"), L("t"),
		                   labels=("MINE", "BASE", "THEIRS"))
		self.assertEqual(n, 1)
		text = "".join(out)
		for marker in ("<<<<<<< MINE", "||||||| BASE", "=======",
		               ">>>>>>> THEIRS"):
			self.assertIn(marker, text)
		self.assertIn("m\n", text)
		self.assertIn("a\n", text)
		self.assertIn("t\n", text)

	def test_an_identical_rewrite_is_taken_once_not_conflicted(self):
		"""base=[a, c] rewritten to [Y] by both sides. The answer is Y.

		`diff3 -m` reports a conflict here -- with no ancestor section at
		all -- which is why byte-equality with diff3 is not the contract and
		is not worth chasing. Three quarters of the measured divergence is
		this shape."""
		out, n = em.merge3(L("a", "c"), L("Y"), L("Y"))
		self.assertEqual((out, n), (L("Y"), 0))

	def test_an_identical_insertion_by_both_sides_appears_once(self):
		"""The commonest divergent shape on config-like input: both sides
		add the same line, one side also changes something else."""
		out, n = em.merge3(L("a", "b", "c"),
		                   L("new", "a", "b", "c"),
		                   L("new", "a", "X", "c"))
		self.assertEqual(n, 0)
		self.assertEqual(out, L("new", "a", "X", "c"))

	def test_a_deletion_both_sides_made_is_not_a_conflict(self):
		out, n = em.merge3(L("a", "b", "c"), L("a", "b"), L("b"))
		self.assertEqual((out, n), (L("b"), 0))

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


class TestMerge2(unittest.TestCase):
	"""The built-in merge with no ancestor to merge against.

    Property-based for the same reason merge3 is: a hand-written
    expectation here encodes what its author believed difflib does, so when
    the author is wrong the code and the test are wrong together and agree
    forever. The invariant below is the whole claim a merge makes."""

	def _resolve(self, merged, side):
		"""One side of every conflict, taken the way a reader with an editor
        takes it: keep the marked region you want, delete the markers and
        the region you do not."""
		out, keeping = [], True
		for line in merged:
			if line.startswith("<<<<<<< "):
				keeping = (side == "mine")
			elif line == "=======\n":
				keeping = (side == "theirs")
			elif line.startswith(">>>>>>> "):
				keeping = True
			else:
				if keeping:
					out.append(line)
		return out

	def test_resolving_left_gives_yours_and_right_gives_theirs(self):
		"""Nothing dropped, nothing invented, whatever alignment difflib
        picks. A violation is a config merge losing one side's lines --
        silently, into /etc.

        Seeded, so a failure is reproducible rather than a rumour."""
		rnd = random.Random(90210)
		fuzz = TestMerge3._fuzz_lines
		mutate = TestMerge3._fuzz_mutate
		for i in range(600):
			mine = fuzz(self, rnd)
			theirs = mutate(self, rnd, mine)
			with self.subTest(i=i, mine=mine, theirs=theirs):
				out, n = em.merge2(mine, theirs)
				self.assertEqual(self._resolve(out, "mine"), mine)
				self.assertEqual(self._resolve(out, "theirs"), theirs)
				self.assertEqual(n > 0, mine != theirs)

	def test_shared_runs_are_not_put_to_the_reader(self):
		"""The point of merging rather than offering keep-or-replace: only
        the differences are asked about."""
		out, n = em.merge2(L("a", "b", "mine", "d", "e"),
		                   L("a", "b", "theirs", "d", "e"))
		self.assertEqual(n, 1)
		self.assertEqual(out, ["a\n", "b\n", "<<<<<<< current\n", "mine\n",
		                       "=======\n", "theirs\n", ">>>>>>> new\n",
		                       "d\n", "e\n"])

	def test_identical_files_merge_to_themselves_without_conflict(self):
		out, n = em.merge2(L("a", "b"), L("a", "b"))
		self.assertEqual((out, n), (L("a", "b"), 0))

	def test_empty_inputs(self):
		self.assertEqual(em.merge2([], []), ([], 0))

	def test_a_last_line_without_a_newline_does_not_glue_the_marker(self):
		"""A config file's last line need not end in a newline. Appending a
        marker after one that does not produced `mine>>>>>>> new` -- one
        corrupt line, in the one place the reader has to look, and not
        removable by deleting marker lines."""
		out, _ = em.merge2(["a\n", "mine"], ["a\n", "theirs"])
		self.assertIn("=======\n", out)
		self.assertIn(">>>>>>> new\n", out)
		self.assertEqual("".join(out).count(">>>>>>>"), 1)
		for line in out[:-1]:
			self.assertTrue(line.endswith("\n"), out)

	def test_merge3_does_not_glue_it_either(self):
		"""Same defect, same fix, and it was latent there first -- every
        fuzz case above happens to end in a newline."""
		out, n = em.merge3(["base"], ["mine"], ["theirs"])
		self.assertEqual(n, 1)
		for line in out[:-1]:
			self.assertTrue(line.endswith("\n"), out)


class TestNativeArch(unittest.TestCase):
	"""One spelling of "what architecture is this", where there were three.

    They disagreed about every way it can fail -- no fallback, "unknown",
    and "all" -- which is the sort of difference that is invisible until
    the day one of them is the one that runs."""

	def setUp(self):
		self.mod = load()

	def patch_which(self, fn):
		original = self.mod.shutil.which
		self.addCleanup(setattr, self.mod.shutil, "which", original)
		self.mod.shutil.which = fn

	def test_it_reads_the_architecture(self):
		self.mod.capture = lambda cmd, env=None: types.SimpleNamespace(
		    stdout="amd64\n", returncode=0)
		self.patch_which(lambda n: "/usr/bin/" + n)
		self.assertEqual(self.mod.native_arch(), "amd64")

	def test_a_missing_dpkg_gives_the_caller_s_default(self):
		self.patch_which(lambda n: None)
		self.assertEqual(self.mod.native_arch(), "unknown")
		self.assertEqual(self.mod.native_arch("all"), "all")

	def test_an_empty_answer_gives_the_caller_s_default(self):
		"""dpkg present but saying nothing -- previously an empty string in
        two of the three call sites, which then went into a filename or an
        index path."""
		self.mod.capture = lambda cmd, env=None: types.SimpleNamespace(
		    stdout="\n", returncode=0)
		self.patch_which(lambda n: "/usr/bin/" + n)
		self.assertEqual(self.mod.native_arch("all"), "all")

	def test_it_reads_under_the_c_locale_like_every_other_parsed_output(self):
		"""Harmless here, since the output is not translated -- but an
        exception to that rule which is safe only by accident is one the
        next reader has to re-derive."""
		seen = {}
		self.mod.capture = lambda cmd, env=None: (
		    seen.update(env or {}), types.SimpleNamespace(
		        stdout="amd64\n", returncode=0))[1]
		self.patch_which(lambda n: "/usr/bin/" + n)
		self.mod.native_arch()
		self.assertEqual(seen.get("LC_ALL"), "C")


class TestAncestorRecovery(unittest.TestCase):
	"""Finding the version a config file was shipped with, when nothing
    archived it.

    The parsing half is where the bugs are, and one of them was found the
    expensive way: asking snapshot.debian.org for the moment of the upgrade
    returns an archive that already holds the *new* version, so the whole
    chain verified perfectly and found nothing."""

	LOG = [
	    "2026-04-10 12:04:36 upgrade bash:amd64 5.2.37-2+b7 5.2.37-2+b8\n",
	    "2026-04-10 12:04:37 status installed bash:amd64 5.2.37-2+b8\n",
	    "2026-05-01 09:00:00 upgrade bash:amd64 5.2.37-2+b8 5.2.37-2+b8\n",
	    "2026-06-15 13:37:29 upgrade bash:amd64 5.2.37-2+b8 5.2.37-2+b9\n",
	    "2026-06-15 13:37:30 upgrade nano:amd64 8.4-1 8.5-1\n",
	]

	def setUp(self):
		self.mod = load()

	def hist(self, name, lines=None):
		"""One package's history, through the batched reader the program
        itself uses. There used to be a `package_history` wrapper in the
        shipped file for this, and a `previous_version` beside it -- both
        reachable only from here once the caller was batched, and the
        second was a trap as well as dead weight: it called `_previous`
        without `installed`, so anyone reaching for the obvious-looking
        entry point would have skipped the agreement check."""
		return self.mod.package_histories([name], lines)[name]

	def patch_run(self, fn):
		"""Patch subprocess.run and shutil.which for one test.

        The originals are captured *before* the patch, not read back
        afterwards: `subprocess` is the same module object the script
        imports, so reading it inside addCleanup restores the patch over
        itself and leaks it into every test that follows, in every file.
        Written the wrong way first, which took out 84 tests and was named
        by the module sentinel rather than by anything nearer."""
		original_run = self.mod.subprocess.run
		original_which = self.mod.shutil.which
		self.addCleanup(setattr, self.mod.subprocess, "run", original_run)
		self.addCleanup(setattr, self.mod.shutil, "which", original_which)
		self.mod.subprocess.run = fn
		self.mod.shutil.which = lambda n: "/usr/bin/" + n

	def test_the_ancestor_is_the_version_before_the_current_one(self):
		v, when = self.mod._previous(self.hist("bash", self.LOG))
		self.assertEqual(v, "5.2.37-2+b8")

	def test_the_timestamp_is_when_the_old_version_was_installed(self):
		"""Not when it was replaced. At the moment of the upgrade the
        archive already holds the new version, so a snapshot taken then does
        not contain the one being looked for -- verified against the real
        snapshot.debian.org, which answered "not in trixie/main" for a
        package that plainly had been."""
		_, when = self.mod._previous(self.hist("bash", self.LOG))
		self.assertTrue(when.startswith("20260410"), when)

	def test_a_reinstall_is_not_a_version_change(self):
		"""dpkg logs a reinstall as `upgrade pkg 1.0 1.0`. Counted as a
        change, it makes a package its own ancestor -- and an ancestor
        identical to the new version silently discards the update, which is
        a bug this subsystem has already shipped once by another route."""
		hist = self.hist("bash", self.LOG)
		self.assertEqual([v for _, v in hist],
		                 ["5.2.37-2+b8", "5.2.37-2+b9"])

	def test_a_log_that_disagrees_with_dpkg_yields_no_ancestor(self):
		"""This walks a *log* to conclude something about the *system*, and
        the two can disagree -- /var/log on a volatile filesystem is
        ordinary on the embedded boxes the dpkg backend exists for, and a
        restored /var/lib/dpkg brings its own history. Where the log is
        behind, the entry before its last is the ancestor of nothing, and a
        wrong ancestor does not fail visibly: it decides which side of a
        merge wins, silently.

        A missing ancestor is safe; a wrong one is not, so disagreement
        means give up."""
		events = self.hist("bash", self.LOG)
		self.assertEqual(self.mod._previous(events, "5.2.37-2+b9")[0],
		                 "5.2.37-2+b8", "the agreeing case must still work")
		self.assertEqual(self.mod._previous(events, "6.0-1"), (None, None))

	def test_an_unknown_installed_version_does_not_veto(self):
		"""Not knowing is different from disagreeing."""
		events = self.hist("bash", self.LOG)
		self.assertEqual(self.mod._previous(events, None)[0], "5.2.37-2+b8")

	def test_recovery_skips_a_package_whose_log_is_behind(self):
		"""And the check has to be wired in, not merely available."""
		self._recovery_stubs()
		self.mod.installed_state = lambda: {
		    "pkg0": {"Version": "9.9-1"}}          # log says 1.1 is current
		tried = []
		self.mod._snapshot_deb = lambda *a, **k: tried.append(a[1])
		self.mod.recover_ancestors({"archive-dir": "/nonexistent"},
		                           ["/etc/a"])
		self.assertEqual(tried, [])

	def test_a_package_installed_once_has_no_ancestor(self):
		self.assertEqual(self.mod._previous(self.hist("nano", self.LOG)),
		                 (None, None))

	def test_an_unknown_package_has_no_ancestor(self):
		self.assertEqual(self.mod._previous(self.hist("nosuch", self.LOG)),
		                 (None, None))

	def test_the_stamp_is_utc_not_the_local_clock(self):
		"""dpkg writes local time with no zone and snapshot indexes UTC."""
		# Restored, not removed: popping would delete a TZ the environment
		# already had, for every test after this one. Unset here today,
		# which is exactly why the wrong version would have looked right.
		had = os.environ.get("TZ")
		self.addCleanup(time.tzset)
		self.addCleanup(
		    lambda: os.environ.__setitem__("TZ", had) if had is not None
		    else os.environ.pop("TZ", None))
		os.environ["TZ"] = "Europe/Stockholm"       # UTC+2 in June
		time.tzset()
		self.assertEqual(self.mod._log_stamp("2026-06-15", "13:37:29"),
		                 "20260615T113729Z")

	def test_rotated_logs_are_read_oldest_first(self):
		d = tempfile.mkdtemp(prefix="emerge-dpkglog-")
		self.addCleanup(shutil.rmtree, d, True)
		with open(os.path.join(d, "dpkg.log"), "w") as f:
			f.write("2026-06-15 13:37:29 upgrade p:amd64 2 3\n")
		with open(os.path.join(d, "dpkg.log.1"), "w") as f:
			f.write("2026-05-15 13:37:29 upgrade p:amd64 1 2\n")
		with gzip.open(os.path.join(d, "dpkg.log.2.gz"), "wt") as f:
			f.write("2026-04-15 13:37:29 install p:amd64 <none> 1\n")
		self.mod.DPKG_LOG = os.path.join(d, "dpkg.log")
		self.assertEqual([v for _, v in self.hist("p")],
		                 ["1", "2", "3"])

	def test_owners_of_reads_one_batched_dpkg_query(self):
		calls = []
		def fake(cmd, env=None):
			calls.append(cmd)
			return types.SimpleNamespace(
			    stdout="bash: /etc/bash.bashrc\n"
			           "nano, nano-tiny: /etc/nanorc\n", returncode=0)
		self.mod.capture = fake
		got = self.mod.owners_of(["/etc/bash.bashrc", "/etc/nanorc"])
		self.assertEqual(got, {"/etc/bash.bashrc": "bash",
		                       "/etc/nanorc": "nano"})
		self.assertEqual(len(calls), 1, "one call, not one per file")

	def test_every_package_is_read_in_one_pass_over_the_log(self):
		"""The log is 33,000 lines on the development box and a review can
        span twenty packages. Scanning it once per package is the same
        per-item mistake this file has paid for twice already."""
		passes = []
		real = self.mod.dpkg_log_lines
		def counted():
			passes.append(1)
			return iter(self.LOG)
		self.mod.dpkg_log_lines = counted
		got = self.mod.package_histories(["bash", "nano"])
		self.assertEqual(len(passes), 1, "one pass, not one per package")
		self.assertEqual([v for _, v in got["bash"]],
		                 ["5.2.37-2+b8", "5.2.37-2+b9"])
		self.assertEqual([v for _, v in got["nano"]], ["8.5-1"])

	def test_a_recovery_run_reads_the_log_once_for_all_packages(self):
		"""The one above pins the batched reader; this pins that recovery
        uses it. Without this, reverting the caller to a call per package
        passed everything -- the seam was tested, its use was not."""
		passes = []
		self.mod.dpkg_log_lines = lambda: (passes.append(1), iter(self.LOG))[1]
		self.mod.owners_of = lambda paths, chunk=500: {
		    "/etc/a": "bash", "/etc/b": "nano"}
		self.mod.ancestor_for = lambda conf, path: (None, None)
		self.mod._cached_deb = lambda *a: None
		self.mod._apt_downloaded_deb = lambda *a, **k: None
		self.mod._snapshot_deb = lambda *a, **k: None
		self.mod.capture = lambda cmd, env=None: types.SimpleNamespace(
		    stdout="amd64\n", returncode=0)
		self.mod.einfo = self.mod.ewarn = lambda msg: None
		self.mod.Verifier = lambda *a, **k: types.SimpleNamespace(
		    enabled=False, stalled=False, timeout=15)
		self.mod.recover_ancestors({"archive-dir": "/nonexistent"},
		                           ["/etc/a", "/etc/b"])
		self.assertEqual(len(passes), 1, "one pass, not one per package")

	def _recovery_stubs(self):
		"""Everything recovery touches except the part under test."""
		self.mod.owners_of = lambda paths, chunk=500: {
		    p: f"pkg{i}" for i, p in enumerate(paths)}
		self.mod.ancestor_for = lambda conf, path: (None, None)
		self.mod._cached_deb = lambda *a: None
		self.mod._apt_downloaded_deb = lambda *a, **k: None
		self.mod.capture = lambda cmd, env=None: types.SimpleNamespace(
		    stdout="amd64\n", returncode=0)
		self.mod.package_histories = lambda names, lines=None: {
		    n: [("20260101T000000Z", "1.0"), ("20260201T000000Z", "1.1")]
		    for n in names}
		self.warnings = []
		self.mod.einfo = lambda msg: None
		self.mod.ewarn = self.warnings.append

	def test_a_spent_budget_gives_up_instead_of_working_through_them(self):
		"""A network that drops packets rather than refusing leaves every
        request sitting for the socket timeout, and recovery makes a lot of
        them -- three per source per package, since the timestamp is in the
        snapshot URL and the per-source cache cannot span two packages. The
        per-request timeout bounds one request; only this bounds the pass."""
		self._recovery_stubs()
		tried = []
		self.mod._snapshot_deb = lambda *a, **k: tried.append(a[1])
		self.mod.recover_ancestors({"archive-dir": "/nonexistent"},
		                           ["/etc/a", "/etc/b", "/etc/c"],
		                           deadline=time.monotonic() - 1)
		self.assertEqual(tried, [], "nothing should be attempted")
		self.assertTrue(any("giving up" in w for w in self.warnings),
		                self.warnings)

	def test_a_stall_stops_the_remaining_packages_asking_too(self):
		"""Noticing the stall in one package is only half of it: without
        this, every package behind it repeats the same wait and the pass
        spends its whole budget instead of one request's worth. Measured
        against a socket that accepts and never answers: 120s becomes 30s."""
		self._recovery_stubs()
		tried = []
		self.mod._snapshot_deb = lambda *a, **k: tried.append(a[1])
		self.mod.recover_ancestors(
		    {"archive-dir": "/nonexistent"}, ["/etc/a", "/etc/b"],
		    verifier=types.SimpleNamespace(enabled=True, stalled=True,
		                                   timeout=15))
		self.assertEqual(tried, [])

	def test_the_deadline_reaches_the_thing_that_does_the_waiting(self):
		"""Passing it into recover_ancestors is not enough -- the requests
        are made further down, and a budget the fetching code never sees
        bounds nothing."""
		self._recovery_stubs()
		seen = []
		self.mod._snapshot_deb = lambda *a, **k: seen.append(a[-1])
		# In the future, because monotonic() counts from boot: a small
		# constant is a deadline already spent, and the first version of
		# this test asserted against one, which the give-up path answered
		# by never calling the function at all.
		when = time.monotonic() + 1000
		self.mod.recover_ancestors({"archive-dir": "/nonexistent"},
		                           ["/etc/a"], deadline=when)
		self.assertEqual(seen, [when])

	def test_a_source_is_dropped_once_the_budget_is_gone(self):
		self.mod.read_sources = lambda: [
		    ("http://deb.debian.org/debian", "trixie", ["main"], None)]
		fetched = []
		self.mod.fetch = lambda url, timeout=60, limit=None: \
		    fetched.append(url)
		v = types.SimpleNamespace(enabled=True, stalled=False, timeout=15)
		self.assertIsNone(self.mod._snapshot_deb(
		    v, "bash", "1.0", "amd64", "20260410T120436Z", "/tmp",
		    deadline=time.monotonic() - 1))
		self.assertEqual(fetched, [])

	# -- multiarch, which recovery does not do ------------------------------

	def test_two_architectures_in_lockstep_are_one_history(self):
		"""dpkg logs `libfoo:amd64` and `libfoo:i386` as separate lines, and
        a library installed for both carries the same version in each. The
        same-version rule collapses them back into one history, so the
        ordinary multiarch case is unaffected -- which is what makes
        native-only recovery a limitation rather than a bug."""
		log = ["2026-01-01 00:00:00 upgrade libfoo:amd64 0.9 1.0\n",
		       "2026-01-01 00:00:01 upgrade libfoo:i386 0.9 1.0\n",
		       "2026-02-01 00:00:00 upgrade libfoo:amd64 1.0 1.1\n",
		       "2026-02-01 00:00:01 upgrade libfoo:i386 1.0 1.1\n"]
		self.assertEqual([v for _, v in
		                  self.hist("libfoo", log)],
		                 ["1.0", "1.1"])
		self.assertEqual(self.mod._previous(
		    self.hist("libfoo", log), "1.1")[0], "1.0")

	def test_two_architectures_that_diverge_yield_no_ancestor(self):
		"""Where they are genuinely at different versions, the log's last
        entry stops matching what dpkg reports and the answer is refused
        rather than guessed -- installed_state is keyed by name alone, so
        which of the two it reports is not something to rely on."""
		log = ["2026-01-01 00:00:00 upgrade libfoo:i386 0.9 1.0\n",
		       "2026-02-01 00:00:00 upgrade libfoo:amd64 1.0 1.1\n"]
		self.assertEqual(self.mod._previous(
		    self.hist("libfoo", log), "1.0"), (None, None))

	def test_a_foreign_arch_package_is_not_found_in_the_native_index(self):
		"""The documented limitation, pinned so it degrades the safe way. A
        package installed only for a foreign architecture is absent from
        binary-<native>, so no ancestor is produced and the file gets a
        2-way review -- rather than an ancestor taken from a same-named
        package of another architecture."""
		index = ("Package: libfoo\nVersion: 1.0\nArchitecture: i386\n"
		         "Filename: pool/f.deb\nSize: 3\nSHA256: x\n\n")
		self.assertIsNone(
		    self.mod._index_stanza(index, "libfoo", "1.0", "amd64"))
		self.assertIsNotNone(
		    self.mod._index_stanza(index, "libfoo", "1.0", "i386"))

	def test_an_arch_all_package_is_still_found(self):
		"""The counterpart, and the reason the test above is about foreign
        architectures rather than about anything that is not the native
        one: Architecture: all is listed in every binary-<arch> index."""
		index = ("Package: libfoo\nVersion: 1.0\nArchitecture: all\n"
		         "Filename: pool/f.deb\nSize: 3\nSHA256: x\n\n")
		self.assertIsNotNone(
		    self.mod._index_stanza(index, "libfoo", "1.0", "amd64"))

	def test_a_diversion_is_not_read_as_a_package_name(self):
		"""dpkg-query answers a diverted path with
        `diversion by util-linux-extra from: /sbin/mkfs.bfs` and names no
        owner at all. Split on the colon it becomes the package "diversion
        by util-linux-extra from", and the reply also carries the
        diverted-to path, which nobody asked about."""
		self.mod.capture = lambda cmd, env=None: types.SimpleNamespace(
		    stdout="diversion by util-linux-extra from: /sbin/mkfs.bfs\n"
		           "diversion by util-linux-extra to: /sbin/mkfs.bfs.moved\n"
		           "bash: /etc/bash.bashrc\n", returncode=0)
		got = self.mod.owners_of(["/sbin/mkfs.bfs", "/etc/bash.bashrc"])
		self.assertEqual(got, {"/etc/bash.bashrc": "bash"})

	def test_the_query_is_chunked_so_the_argument_list_cannot_overflow(self):
		"""Same reason conffiles_of is chunked, and the failure is the whole
        call rather than a slow one."""
		sizes = []
		def fake(cmd, env=None):
			sizes.append(len(cmd) - 2)
			return types.SimpleNamespace(stdout="", returncode=0)
		self.mod.capture = fake
		self.mod.owners_of([f"/etc/f{i}" for i in range(1100)], chunk=500)
		self.assertEqual(sizes, [500, 500, 100])

	def test_a_cached_deb_is_found_through_the_epoch_escape(self):
		d = tempfile.mkdtemp(prefix="emerge-aptcache-")
		self.addCleanup(shutil.rmtree, d, True)
		open(os.path.join(d, "tzdata_4%3a1.2-3_all.deb"), "w").close()
		self.mod.APT_CACHE_DIR = d
		self.assertTrue(self.mod._cached_deb("tzdata", "4:1.2-3", "amd64"))
		self.assertIsNone(self.mod._cached_deb("tzdata", "4:1.2-4", "amd64"))

	def test_the_apt_download_path_asks_for_the_exact_version(self):
		"""The middle source, and the one nothing ran until it was looked
        for -- the same shape as --fetchonly, which twenty-one mentions in
        the suite never once executed."""
		d = tempfile.mkdtemp(prefix="emerge-aptdl-")
		self.addCleanup(shutil.rmtree, d, True)
		seen = []
		def fake_run(cmd, **kw):
			seen.append(cmd)
			open(os.path.join(kw["cwd"], "bash_5.2.37-2+b8_amd64.deb"),
			     "w").close()
			return types.SimpleNamespace(returncode=0)
		self.patch_run(fake_run)
		got = self.mod._apt_downloaded_deb("bash", "5.2.37-2+b8", d)
		self.assertTrue(got and got.endswith("bash_5.2.37-2+b8_amd64.deb"))
		self.assertIn("bash=5.2.37-2+b8", seen[0])

	def test_an_apt_download_that_hangs_is_given_up_on(self):
		"""apt's own timeouts are tuned for a command someone asked to run:
        Acquire::http::Timeout alone is 120s, and it retries. Inside an
        optional lookup that is the same unbounded wait by another road."""
		d = tempfile.mkdtemp(prefix="emerge-aptdl-")
		self.addCleanup(shutil.rmtree, d, True)
		passed = {}
		def fake_run(cmd, **kw):
			passed.update(kw)
			raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
		self.patch_run(fake_run)
		self.assertIsNone(self.mod._apt_downloaded_deb("bash", "1.0", d))
		self.assertTrue(passed.get("timeout"), "no timeout was asked for")

	def test_a_failed_apt_download_is_not_used_even_if_it_left_a_file(self):
		"""An interrupted download exits non-zero having written part of a
        .deb. Taking the file anyway hands dpkg-deb a truncated archive --
        which degrades to a 2-way review rather than corrupting anything,
        but for a reason nobody could find. Written without the leftover
        file first, where the assertion held whatever the code did."""
		d = tempfile.mkdtemp(prefix="emerge-aptdl-")
		self.addCleanup(shutil.rmtree, d, True)
		def fake_run(cmd, **kw):
			open(os.path.join(kw["cwd"], "bash_9.9_amd64.deb"), "w").close()
			return types.SimpleNamespace(returncode=1)
		self.patch_run(fake_run)
		self.assertIsNone(self.mod._apt_downloaded_deb("bash", "9.9", d))


class TestSnapshotChain(unittest.TestCase):
	"""The fetched .deb is trusted only as far as the machine's own keys
    reach: signed Release, index checked against it, .deb checked against
    the index. Snapshot is trusted for transport and nothing else."""

	def setUp(self):
		self.mod = load()
		self.warnings = []
		self.mod.ewarn = self.warnings.append     # not onto the run's output
		self.mod.read_sources = lambda: [
		    ("http://deb.debian.org/debian", "trixie", ["main"], None)]
		self.index = ("Package: bash\nVersion: 1.0\nArchitecture: amd64\n"
		              "Filename: pool/main/b/bash/bash_1.0_amd64.deb\n"
		              "Size: 3\nSHA256: %s\n\n"
		              % hashlib.sha256(b"deb").hexdigest())
		self.fetched = []

	def _verifier(self, ok=True):
		mod = self.mod
		class V:
			enabled = True
			stalled = False
			timeout = 15
			def release(self, base, suite, signed_by=None):
				return "SHA256:\n x 1 main/binary-amd64/Packages\n"
			def check_index(self, base, suite, signed_by, relpath, raw):
				return ok
		return V()

	def _fetch(self, body):
		def fake(url, timeout=60, limit=None):
			self.fetched.append(url)
			return body(url)
		return fake

	def test_the_deb_is_taken_when_every_link_holds(self):
		self.mod.fetch = self._fetch(
		    lambda u: self.index.encode() if u.endswith("Packages")
		    else b"deb")
		d = tempfile.mkdtemp(prefix="emerge-snap-")
		self.addCleanup(shutil.rmtree, d, True)
		got = self.mod._snapshot_deb(self._verifier(), "bash", "1.0",
		                             "amd64", "20260410T120436Z", d)
		self.assertTrue(got and os.path.isfile(got))
		self.assertTrue(any("snapshot.debian.org" in u and "20260410" in u
		                    for u in self.fetched), self.fetched)

	def test_a_deb_that_fails_its_hash_stops_the_run(self):
		"""A mismatch is a failure, not an inability -- the rule --sync
        applies to an index. Quietly falling back to a 2-way review would
        hide the one event this chain exists to detect."""
		self.mod.fetch = self._fetch(
		    lambda u: self.index.encode() if u.endswith("Packages")
		    else b"tampered")
		d = tempfile.mkdtemp(prefix="emerge-snap-")
		self.addCleanup(shutil.rmtree, d, True)
		with self.assertRaises(RuntimeError) as e:
			self.mod._snapshot_deb(self._verifier(), "bash", "1.0", "amd64",
			                       "20260410T120436Z", d)
		self.assertIn("SHA256 mismatch", str(e.exception))

	def test_an_index_that_does_not_verify_is_not_used(self):
		self.mod.fetch = self._fetch(lambda u: self.index.encode())
		d = tempfile.mkdtemp(prefix="emerge-snap-")
		self.addCleanup(shutil.rmtree, d, True)
		self.assertIsNone(
		    self.mod._snapshot_deb(self._verifier(ok=False), "bash", "1.0",
		                           "amd64", "20260410T120436Z", d))

	def test_nothing_is_fetched_without_gpgv(self):
		"""An ancestor that cannot be verified is not used. It is optional
        data, so refusing costs a 2-way review -- much cheaper than a wrong
        ancestor, which makes a wrong merge rather than a visible error."""
		self.mod.fetch = self._fetch(lambda u: b"")
		v = self._verifier()
		v.enabled = False
		self.assertIsNone(self.mod._snapshot_deb(v, "bash", "1.0", "amd64",
		                                         "20260410T120436Z", "/tmp"))
		self.assertEqual(self.fetched, [])

	def test_recovery_asks_for_a_shorter_socket_timeout_than_sync(self):
		"""Every request recovery makes, including the Release the Verifier
        fetches -- which is the first one, and the one that hangs when the
        network is not answering."""
		timeouts = []
		def fake(url, timeout=60, limit=None):
			timeouts.append(timeout)
			return self.index.encode() if url.endswith("Packages") else b"deb"
		self.mod.fetch = fake
		d = tempfile.mkdtemp(prefix="emerge-snap-")
		self.addCleanup(shutil.rmtree, d, True)
		self.mod._snapshot_deb(self._verifier(), "bash", "1.0", "amd64",
		                       "20260410T120436Z", d)
		self.assertTrue(timeouts, "nothing was fetched")
		self.assertTrue(all(t == self.mod.ANCESTOR_TIMEOUT for t in timeouts),
		                timeouts)
		self.assertLess(self.mod.ANCESTOR_TIMEOUT, 60)

	def test_the_verifier_passes_its_timeout_down_to_the_fetch(self):
		"""The Verifier makes the request recovery cannot reach directly,
        and it defaults to sync's 60s -- so the constructor argument is the
        only way the shorter one gets there at all."""
		timeouts = []
		self.mod.fetch = lambda url, timeout=60, limit=None: (
		    timeouts.append(timeout), b"")[1]
		v = self.mod.Verifier(True, timeout=7)
		self.addCleanup(v.close)
		# Set rather than patched into being true: `enabled` depends on
		# whether the machine has gpgv, and patching shutil.which to
		# arrange that is how the previous attempt leaked a stdlib
		# function into every test that followed.
		v.enabled = True
		v.keyring = lambda signed_by=None: "/dev/null"
		v._gpgv = lambda *a: False
		try:
			v.release("http://x/debian", "trixie")
		except RuntimeError:
			pass
		self.assertTrue(timeouts and all(t == 7 for t in timeouts), timeouts)
		self.assertEqual(self.mod.Verifier(False).timeout, 60,
		                 "sync's default must not change")

	def test_a_stalled_source_stops_the_run_asking_at_all(self):
		"""A refused connection or a 404 costs nothing and the next source
        is worth trying. A network that accepts and never answers will do
        the same to every source and every package behind this one, so the
        budget alone means two minutes of silence in a review. Noticing
        turns that into one wait.

        The threshold is `>= verifier.timeout`, so a zero timeout makes
        every answer a stall -- which is what pins the wiring without
        making the suite wait for a real one."""
		self.mod.read_sources = lambda: [
		    ("http://deb.debian.org/debian", "trixie", ["main"], None),
		    ("http://deb.debian.org/debian", "trixie-updates", ["main"],
		     None)]
		v = self._verifier()
		asked = []
		v.timeout = 0
		v.release = lambda base, suite, signed_by=None: asked.append(suite)
		self.mod.fetch = self._fetch(lambda u: b"")
		self.assertIsNone(self.mod._snapshot_deb(
		    v, "bash", "1.0", "amd64", "20260410T120436Z", "/tmp"))
		self.assertTrue(v.stalled)
		self.assertEqual(asked, ["trixie"], "the second source was tried")

	def test_a_source_that_simply_has_nothing_is_not_a_stall(self):
		"""The other half: a fast miss must leave the run willing to ask
        the next source, or one 404 turns off recovery for everything."""
		self.mod.read_sources = lambda: [
		    ("http://deb.debian.org/debian", "trixie", ["main"], None),
		    ("http://deb.debian.org/debian", "trixie-updates", ["main"],
		     None)]
		v = self._verifier()
		asked = []
		v.release = lambda base, suite, signed_by=None: asked.append(suite)
		self.mod.fetch = self._fetch(lambda u: b"")
		self.mod._snapshot_deb(v, "bash", "1.0", "amd64",
		                       "20260410T120436Z", "/tmp")
		self.assertFalse(v.stalled)
		self.assertEqual(asked, ["trixie", "trixie-updates"])

	def test_an_oversized_index_is_skipped_not_fatal(self):
		"""fetch and decompress_bounded raise RuntimeError for their size
        ceilings, which is a mirror spending this process's memory rather
        than a broken chain. Uncaught it took the whole review down with it
        -- so any source could abort a command whose job is reviewing
        config files, over data that is only an optimisation."""
		def body(url):
			if url.endswith("Packages"):
				raise RuntimeError("larger than 536870912 bytes; refusing")
			return b"deb"
		self.mod.fetch = self._fetch(body)
		self.assertIsNone(self.mod._snapshot_deb(
		    self._verifier(), "bash", "1.0", "amd64",
		    "20260410T120436Z", "/tmp"))

	def test_a_release_that_does_not_verify_is_skipped_and_said_out_loud(self):
		"""Not fatal: an archive key rotated since the snapshot fails
        exactly as a forgery does, and an old ancestor must not be able to
        block the review. Not silent either, because the other cause is a
        forgery."""
		v = self._verifier()
		def boom(base, suite, signed_by=None):
			raise RuntimeError("InRelease signature verification FAILED")
		v.release = boom
		self.mod.fetch = self._fetch(lambda u: b"")
		self.assertIsNone(self.mod._snapshot_deb(
		    v, "bash", "1.0", "amd64", "20260410T120436Z", "/tmp"))
		self.assertTrue(any("did not verify" in w for w in self.warnings),
		                self.warnings)

	def test_repositories_snapshot_does_not_carry_are_skipped(self):
		"""Third-party sources -- docker, a PPA -- are not on snapshot, and
        asking it for them is a request that can only 404."""
		self.mod.read_sources = lambda: [
		    ("https://download.docker.com/linux/debian", "trixie",
		     ["stable"], None)]
		self.mod.fetch = self._fetch(lambda u: b"")
		self.assertIsNone(self.mod._snapshot_deb(
		    self._verifier(), "docker-ce", "1.0", "amd64",
		    "20260410T120436Z", "/tmp"))
		self.assertEqual(self.fetched, [])


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

class TestTheLockstepWall(unittest.TestCase):
	"""The libsdl3-dev case, rebuilt so it no longer needs this machine.

    `--no-dep-upgrade` was validated against the live trixie tree, where
    installing libsdl3-dev dragged the Mesa stack from 25.0.7-2 to
    25.0.7-2+deb13u1 and produced a wall. Running it found three bugs, all
    fixed and unit-tested since. But the *scenario* lived only on the box,
    and the box has since been updated: Mesa is at 25.0.7-2+deb13u1 now, so
    the same command resolves cleanly with 33 new packages and no wall at
    all. The evidence expired without anything failing, which is the way
    live-system evidence always expires.

    So the shape is pinned here instead: a same-upstream revision bump that
    an uninstalled target requires, which is the case the escape hatch and
    the `(session rebuild)` label both exist for."""

	# Two packages, because a lockstep stack walls one at a time and the
	# interesting behaviour is what the second wall suggests. With only one
	# mover there are no earlier grants to carry, so the test that claims to
	# pin that cannot fail -- which is how this was first written, and the
	# mutation went straight through it.
	def index(self):
		return FakeIndex({
		    "libsdl3-dev": [stanza(
		        "libsdl3-dev", "3.2.10+ds-1",
		        depends="libgbm1 (>= 25.0.7-2+deb13u1), "
		                "mesa-libgallium (>= 25.0.7-2+deb13u1)")],
		    "libgbm1": [stanza("libgbm1", "25.0.7-2+deb13u1"),
		                stanza("libgbm1", "25.0.7-2")],
		    "mesa-libgallium": [stanza("mesa-libgallium", "25.0.7-2+deb13u1"),
		                        stanza("mesa-libgallium", "25.0.7-2")],
		})

	def solve(self, allow=None):
		inst = installed(("libgbm1", "25.0.7-2"),
		                 ("mesa-libgallium", "25.0.7-2"))
		return em.ndu_solve(self.index(), inst, {}, ["libsdl3-dev"],
		                    {"libsdl3-dev"}, False, allow)

	def test_it_walls_rather_than_dragging_the_installed_ones_up(self):
		with self.assertRaises(em.NduWall) as caught:
			self.solve()
		moved = {m["name"] for m in caught.exception.movers}
		self.assertTrue(moved <= {"libgbm1", "mesa-libgallium"}, moved)
		self.assertTrue(moved, "a wall that names nothing cannot be acted on")

	def test_the_wall_knows_it_is_only_a_revision_bump(self):
		"""What separates this from a real upgrade: same upstream version,
        so the session keeps running and the warning says `(session
        rebuild)` rather than threatening to close the user's apps."""
		with self.assertRaises(em.NduWall) as caught:
			self.solve()
		mover = caught.exception.movers[0]
		self.assertTrue(em.same_upstream(mover["installed"], mover["wanted"]),
		                f"{mover} should read as a same-upstream bump")

	def test_following_the_suggestion_converges_instead_of_looping(self):
		"""The hint has to accumulate. One of the three bugs found on the
        live tree was a --with suggestion that dropped the earlier grants,
        so following it walled on the package you had just permitted and
        sent you round again.

        Walked here the way a user would: wall, take the suggested line,
        wall again, take that line, resolve."""
		with self.assertRaises(em.NduWall) as first:
			self.solve()
		allow = set(em._with_arg(set(), first.exception.movers).split(","))

		with self.assertRaises(em.NduWall) as second:
			self.solve(allow=allow)
		suggested = em._with_arg(allow, second.exception.movers)
		self.assertEqual(sorted(suggested.split(",")),
		                 ["libgbm1", "mesa-libgallium"],
		                 "the second suggestion dropped the first grant, so "
		                 "following it returns to the wall it just cleared")

		_, merges = self.solve(allow=set(suggested.split(",")))
		self.assertEqual({row[0] for row in merges},
		                 {"libsdl3-dev", "libgbm1", "mesa-libgallium"})


class TestNduSolveAgainstBruteForce(unittest.TestCase):
	"""The solver against an exhaustive oracle, on random package graphs.

	ndu_solve is the most intricate code here and the costliest place for a
	silent wrong answer: it decides what actually gets installed. Its promise
	is checkable, and on graphs small enough to enumerate, so is its *whole*
	answer -- brute force every combination and see whether a no-upgrade
	solution existed at all.

	That matters because a false wall is a bug this project has already
	shipped once: the old greedy solver reported walls that did not exist,
	and no example-based test noticed. An oracle notices.

	Three invariants, all absolute:
	  1. a returned plan never moves an installed package outside `allow`;
	  2. a returned plan actually satisfies every dependency it pulls in;
	  3. a wall is only raised when brute force agrees there is no solution.

	Seeded, so a failure is reproducible."""

	class Index:
		def __init__(self, universe):
			self.universe = universe

		def all_versions(self, name):
			return list(self.universe.get(name, []))

		def has(self, name):
			return name in self.universe

		def provides_of(self, name):
			return []

	def generate(self, rnd):
		"""A small universe: a few packages, a few versions, random
		dependencies with version constraints, and a random installed set."""
		names = [f"p{i}" for i in range(rnd.randint(2, 4))]
		universe = {}
		for n in names:
			versions = sorted({rnd.randint(1, 3)
			                   for _ in range(rnd.randint(1, 3))},
			                  reverse=True)
			universe[n] = [{"Package": n, "Version": f"{v}.0"}
			               for v in versions]
		for n in names:
			for st in universe[n]:
				deps = []
				for other in names:
					if other == n or rnd.random() > 0.45:
						continue
					deps.append(f"{other} (>= {rnd.randint(1, 3)}.0)"
					            if rnd.random() < 0.5 else other)
				if deps:
					st["Depends"] = ", ".join(deps)
		installed = {}
		for n in names:
			if rnd.random() < 0.5:
				installed[n] = {"Package": n,
				                "Version": rnd.choice(universe[n])["Version"]}
		return names, universe, installed

	@staticmethod
	def satisfied(assignment, installed):
		have = {n: st["Version"] for n, st in assignment.items()}
		for n, st in installed.items():
			have.setdefault(n, st["Version"])
		for st in assignment.values():
			for alts in em.parse_depends(st.get("Depends", "")):
				if not any(dn in have
				           and (op is None or em.meets(have[dn], op, dv))
				           for dn, op, dv in alts):
					return False
		return True

	def solution_exists(self, target, universe, installed):
		"""Brute force: is there *any* choice of not-installed packages that
		installs the target with every installed package left where it is?"""
		free = [n for n in universe if n not in installed]
		for size in range(len(free) + 1):
			for subset in itertools.combinations(free, size):
				if target not in subset and target not in installed:
					continue
				pools = [universe[n] for n in subset]
				for combo in (itertools.product(*pools) if pools else [()]):
					assignment = dict(zip(subset, combo))
					for n, st in installed.items():
						assignment.setdefault(n, st)
					if self.satisfied(assignment, installed):
						return True
		return False

	def test_the_solver_agrees_with_exhaustive_search(self):
		rnd = random.Random(31337)
		solved = walls = 0
		for i in range(250):
			names, universe, installed = self.generate(rnd)
			target = rnd.choice(names)
			with self.subTest(i=i, target=target, universe=universe,
			                  installed=installed):
				try:
					plan, merges = em.ndu_search(
					    self.Index(universe), installed, {}, [target],
					    [target], False, set(), backtrack=3)
				except em.NduWall:
					walls += 1
					self.assertFalse(
					    self.solution_exists(target, universe, installed),
					    "walled on a graph that brute force can solve")
					continue
				except (RuntimeError, em.NduIncomplete):
					continue
				solved += 1
				for name, newver, old, *_ in merges:
					self.assertFalse(
					    old is not None and em.vercmp(newver, old) > 0,
					    f"{name} was upgraded from {old} to {newver}")
				assignment = {st["Package"]: st for st in plan}
				for n, st in installed.items():
					assignment.setdefault(n, st)
				self.assertTrue(self.satisfied(assignment, installed),
				                "the plan does not satisfy its own "
				                "dependencies")
		# a run that solved everything trivially would prove little
		self.assertGreater(walls, 5, "the wall path was barely exercised")
		self.assertGreater(solved, 50, "the solve path was barely exercised")


class TestNduSearch(unittest.TestCase):
	"""The escalating wrapper. The cheap pass takes the first alternative of
    every `a | b`, as apt and dpkg do; only when that fails is it worth
    branching on them. So a wall the user is shown has survived an exhaustive
    search, and a resolution that works first time pays nothing."""

	def setUp(self):
		self.mod = load()
		self.mod._session_critical_cache = set()
		self.mod._session_blind = False
		self.said = []
		self.mod.einfo = self.said.append

	# app needs `liba | libb`. liba, the first choice, demands a newer libc
	# than is installed; libb is fine. Only branching resolves it.
	def alternative_graph(self):
		return FakeIndex({
		    "app": [stanza("app", "1.0", depends="liba | libb")],
		    "liba": [stanza("liba", "1.0", depends="libc (>= 2.0)")],
		    "libb": [stanza("libb", "1.0")],
		    "libc": [stanza("libc", "2.0"), stanza("libc", "1.0")],
		})

	def no_alternative_graph(self):
		return FakeIndex({
		    "app": [stanza("app", "1.0", depends="libc (>= 2.0)")],
		    "libc": [stanza("libc", "2.0"), stanza("libc", "1.0")],
		})

	def search(self, idx, **kw):
		return self.mod.ndu_search(idx, installed(("libc", "1.0")), {},
		                           ["app"], {"app"}, False, **kw)

	def test_the_cheap_pass_alone_walls_on_this_graph(self):
		with self.assertRaises(self.mod.NduWall):
			self.mod.ndu_solve(self.alternative_graph(),
			                   installed(("libc", "1.0")), {}, ["app"],
			                   {"app"}, False)

	def test_escalating_finds_the_other_alternative(self):
		_, merges = self.search(self.alternative_graph())
		names = {m[0] for m in merges}
		self.assertIn("libb", names)
		self.assertNotIn("liba", names)
		self.assertNotIn("libc", names, "and it moves nothing installed")

	def test_the_retry_is_announced(self):
		self.search(self.alternative_graph())
		self.assertTrue(any("exhaustive" in s for s in self.said))

	def test_backtrack_zero_disables_the_retry(self):
		with self.assertRaises(self.mod.NduWall):
			self.search(self.alternative_graph(), backtrack=0)
		self.assertFalse(self.said)

	def test_a_graph_with_no_choice_is_not_retried(self):
		"""Nothing to branch on means the second pass would walk exactly the
        same graph, so running it only doubles the time before reporting the
        same wall."""
		with self.assertRaises(self.mod.NduWall):
			self.search(self.no_alternative_graph())
		self.assertFalse(self.said, "should not have escalated")

	def test_a_genuine_wall_survives_the_exhaustive_pass(self):
		"""Both alternatives need a newer libc, so there is no way through
        and the wall is real."""
		idx = FakeIndex({
		    "app": [stanza("app", "1.0", depends="liba | libb")],
		    "liba": [stanza("liba", "1.0", depends="libc (>= 2.0)")],
		    "libb": [stanza("libb", "1.0", depends="libc (>= 2.0)")],
		    "libc": [stanza("libc", "2.0"), stanza("libc", "1.0")],
		})
		with self.assertRaises(self.mod.NduWall) as cm:
			self.search(idx)
		self.assertTrue(any("exhaustive" in s for s in self.said))
		self.assertEqual([m["name"] for m in cm.exception.movers], ["libc"])

	def test_the_first_failure_is_the_one_reported(self):
		"""It names the blocker the user is most likely to act on."""
		idx = self.no_alternative_graph()
		with self.assertRaises(self.mod.NduWall) as cm:
			self.search(idx)
		self.assertEqual([m["name"] for m in cm.exception.movers], ["libc"])

	def test_a_clean_resolve_never_escalates(self):
		idx = FakeIndex({"app": [stanza("app", "1.0")]})
		self.mod.ndu_search(idx, installed(), {}, ["app"], {"app"}, False)
		self.assertFalse(self.said)

	def test_budget_exhaustion_is_not_reported_as_a_wall(self):
		"""Giving up early is not proof that no resolution exists."""
		idx = self.alternative_graph()
		with self.assertRaises(self.mod.NduIncomplete) as cm:
			self.mod.ndu_solve(idx, installed(("libc", "1.0")), {}, ["app"],
			                   {"app"}, False, budget=1)
		self.assertIn("--backtrack", str(cm.exception))

	def test_incomplete_is_still_a_runtime_error(self):
		"""Existing handlers catch RuntimeError; they must keep working."""
		self.assertTrue(issubclass(self.mod.NduIncomplete, RuntimeError))


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
		self.mod.capture = lambda cmd, env=None: R

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


class TestAptActionSelection(unittest.TestCase):
	"""Which apt-get command each flag combination turns into. Whatever ends
    up here is both what apt executes and -- because apt marks everything it
    is told to install as manual -- what lands in @selected."""

	def setUp(self):
		self.mod = load()
		self.be = self.mod.AptBackend()
		self.be._sizes = lambda names: {}
		self.be._installed_version = lambda pkg: None

		class R:
			stdout, stderr, returncode = "", "", 0
		self.mod.capture = lambda cmd, env=None: R

	def action(self, targets, members=(), had_world=False, **kw):
		self.be.expand_sets = lambda t: ([x for x in t if not x.startswith("@")],
		                                 list(members), had_world)
		self.be.resolve(list(targets), **kw)
		return self.be._action

	def test_full_upgrade_is_dist_upgrade(self):
		self.assertEqual(self.action(["@world"], had_world=True,
		                             update=True, deep=True),
		                 ["dist-upgrade"])

	def test_an_atom_alongside_world_is_not_silently_dropped(self):
		"""`emerge -uD @world nano` asks for two things. The dist-upgrade
        branch took the set and threw the atom away, so nano was never
        installed and nothing said so -- dist-upgrade does take package
        arguments."""
		self.assertEqual(self.action(["@world", "nano"], had_world=True,
		                             update=True, deep=True),
		                 ["dist-upgrade", "nano"])

	def test_update_without_deep_installs_the_members(self):
		self.assertEqual(self.action(["@world"], members=["a", "b"],
		                             had_world=True, update=True),
		                 ["install", "a", "b"])

	def test_no_update_remerges_like_gentoo(self):
		self.assertEqual(self.action(["nano"]),
		                 ["install", "--reinstall", "nano"])


class TestMaintainerScriptsAreNotInteractive(unittest.TestCase):
	"""Every dpkg run that can execute a maintainer script must pass
    DEBIAN_FRONTEND=noninteractive, and the dpkg backend did not.

    The apt backend has always set it. The dpkg backend ran dpkg through
    `capture()`, which takes stdout while leaving stdin attached -- so a
    postinst that asks debconf a question wrote the prompt somewhere nobody
    could see and then waited for an answer. Reproduced as root in a
    container with a package whose postinst reads a line: without the
    variable it was still blocked after six seconds; with it, 0.1s and
    rc=0.

    --force-confold and --force-confdef are what stop conffiles being
    replaced. They do not stop a script asking something else."""

	def setUp(self):
		self.mod = load()
		# Silenced before any backend is built: constructing one seeds the
		# world file and announces it, which as root drops "Seeded world
		# file with 170 packages" into the middle of the test output.
		# Invisible to an ordinary user, since the seed guard returns
		# early for one.
		self.mod.einfo = lambda m: None
		self.mod.need_root = lambda: None
		self.calls = []

		class R:
			returncode, stdout, stderr = 0, "", ""
		self.mod.capture = lambda cmd, env=None: (
		    self.calls.append((cmd, env)), R)[1]

	def envs_for(self, program):
		return [env for cmd, env in self.calls if cmd[:1] == [program]]

	def test_unpack_and_configure_run_noninteractive(self):
		be = self.mod.DpkgBackend()
		# A plan whose second member pre-depends on the first, so both the
		# unpack and the mid-run configure pass are exercised.
		be._plan = [{"Package": "b", "Version": "1"},
		            {"Package": "a", "Version": "1", "Pre-Depends": "b"}]
		be._download = lambda: ["/tmp/b.deb", "/tmp/a.deb"]
		be._read_world = lambda: set()
		be._write_world = lambda names: None
		self.mod.load_conf = lambda: {}
		self.mod.archive_settled = lambda conf, packages: 0
		self.mod.pending_notice = lambda conf: None
		with contextlib.redirect_stdout(io.StringIO()):
			be.merge([("a", "1", None, 0, "ebuild", "")], ["a"],
			         {"fetchonly": False, "oneshot": True})
		envs = self.envs_for("dpkg")
		self.assertTrue(envs, "no dpkg call was made")
		for env in envs:
			self.assertEqual((env or {}).get("DEBIAN_FRONTEND"),
			                 "noninteractive",
			                 "a dpkg run could stop for a debconf prompt "
			                 "nobody can see")

	def test_unmerge_runs_noninteractive(self):
		"""prerm and postrm ask questions too, and it is the same captured
        stdout on the way out as on the way in."""
		be = self.mod.DpkgBackend()
		be._read_world = lambda: set()
		be._write_world = lambda names: None
		with contextlib.redirect_stdout(io.StringIO()):
			be.unmerge([("tree", "2.0")])
		envs = self.envs_for("dpkg")
		self.assertTrue(envs)
		for env in envs:
			self.assertEqual((env or {}).get("DEBIAN_FRONTEND"),
			                 "noninteractive")


class TestPrivilegedOperationsNeedRoot(unittest.TestCase):
	"""Every operation that writes to the system refuses before it starts.

    Found by sweeping rather than by reading: replacing any of the ten
    `need_root()` calls with `pass` left the whole suite green. Nothing
    checked them, and for a good reason that makes it easy to miss -- the
    integration harness has to stub `need_root` out, because it runs
    unprivileged on purpose, and the unit tests reach these functions with
    it already replaced. The guard was untested precisely because
    everything else needs it gone.

    The guard is the first statement of each function, so what it buys is
    the difference between one clear line and a failure deep inside dpkg --
    or, for `dispatch_conf`, a review that shows diffs and asks questions
    and only then cannot write the answers."""

	def setUp(self):
		self.mod = load()
		original = os.geteuid
		self.addCleanup(setattr, os, "geteuid", original)
		os.geteuid = lambda: 1000          # anybody but root

	def assertRefuses(self, what, call):
		"""Refused *before doing anything*, which is the whole point.

        Asserting only on SystemExit(1) was not enough and looked fine:
        without the guard, `merge` and `unmerge` still exit 1 -- from dpkg
        failing on permissions several steps later -- so the mutation
        survived a test that appeared to cover it. Nothing may be executed,
        so any subprocess at all means the guard did not fire first."""
		def forbidden(*a, **kw):
			raise AssertionError(f"{what} ran a command without root")

		self.mod.capture = forbidden
		self.mod.run = forbidden
		self.mod.subprocess = types.SimpleNamespace(
		    Popen=forbidden, run=forbidden, DEVNULL=None, PIPE=None,
		    STDOUT=None, CalledProcessError=Exception)

		with self.subTest(operation=what):
			buf = io.StringIO()
			with self.assertRaises(SystemExit) as exit:
				with contextlib.redirect_stdout(buf), \
				     contextlib.redirect_stderr(buf):
					call()
			self.assertEqual(exit.exception.code, 1,
			                 f"{what} ran without root")
			self.assertIn("uperuser", buf.getvalue(),
			              f"{what} failed for some other reason: "
			              f"{buf.getvalue()[:120]}")

	def test_the_dpkg_backend_refuses_every_write(self):
		be = self.mod.DpkgBackend()
		# deselect asks for root only when it has something to remove, which
		# is deliberate -- so it needs a world file with the name in it, or
		# it correctly does nothing and correctly asks for nothing.
		be._read_world = lambda: {"x"}
		opts = {"fetchonly": False, "oneshot": False}
		self.assertRefuses("dpkg sync", lambda: be.sync())
		self.assertRefuses("dpkg merge", lambda: be.merge([], [], opts))
		self.assertRefuses("dpkg unmerge", lambda: be.unmerge([("x", "1")]))
		self.assertRefuses("dpkg deselect",
		                   lambda: be.deselect(["x"], False))

	def test_the_apt_backend_refuses_every_write(self):
		be = self.mod.AptBackend()
		be._manual_set = lambda: {"x"}
		opts = {"fetchonly": False, "oneshot": False}
		self.assertRefuses("apt sync", lambda: be.sync())
		self.assertRefuses("apt merge", lambda: be.merge([], [], opts))
		self.assertRefuses("apt unmerge", lambda: be.unmerge([("x", "1")]))
		self.assertRefuses("apt build", lambda: be.build(opts))
		self.assertRefuses("apt deselect",
		                   lambda: be.deselect(["x"], False))

	def test_dispatch_conf_refuses(self):
		self.assertRefuses("dispatch-conf",
		                   lambda: self.mod.dispatch_conf({}))

	def test_a_pretend_deselect_still_needs_none(self):
		"""The counterpart. Asking for root when nothing will be written is
        its own defect, and `--pretend` is the case that proves the guard
        is placed rather than sprinkled."""
		be = self.mod.DpkgBackend()
		be._read_world = lambda: {"x"}
		be._write_world = lambda names: self.fail("pretend wrote the world")
		with contextlib.redirect_stdout(io.StringIO()):
			be.deselect(["x"], True)


class TestDpkgUnmergeGuard(unittest.TestCase):
	"""The other half of the cascade story, and the backends really differ.

    `apt-get remove` takes the dependents with it, so on the apt backend -a
    genuinely cascades. dpkg does no such thing: it refuses to remove a
    package another installed one needs. The guard here used to end "use -a
    to override", which promised something dpkg does not allow -- the user
    confirmed, and the unmerge then failed with a raw dpkg error. Verified
    against real dpkg: `dpkg -r lib` with app installed exits 1 saying
    "dependency problems prevent removal of lib"."""

	def setUp(self):
		self.mod = load()
		# Silenced before any backend is built: constructing one seeds the
		# world file and announces it, which as root drops "Seeded world
		# file with 170 packages" into the middle of the test output.
		# Invisible to an ordinary user, since the seed guard returns
		# early for one.
		self.mod.einfo = lambda m: None
		self.be = self.mod.DpkgBackend()
		self.mod.installed_state = lambda: {
		    "lib": {"Package": "lib", "Version": "1.0",
		            "Priority": "optional"},
		    "app": {"Package": "app", "Version": "1.0",
		            "Priority": "optional", "Depends": "lib"},
		}
		self.said = []
		self.mod.ewarn = self.said.append
		self.mod.eerror = self.said.append

	def candidates(self, targets, ask=False, pretend=False):
		with contextlib.redirect_stdout(io.StringIO()):
			return self.be.unmerge_candidates(
			    targets, {"ask": ask, "pretend": pretend})

	def test_it_refuses_and_names_the_command_that_would_work(self):
		with self.assertRaises(SystemExit):
			self.candidates(["lib"])
		self.assertTrue(any("emerge -C app lib" in s for s in self.said),
		                f"no actionable suggestion in: {self.said}")

	def test_it_does_not_claim_that_ask_overrides(self):
		"""dpkg has nothing for -a to override, and saying so sent people
        into a confirmed unmerge that could only fail."""
		with self.assertRaises(SystemExit):
			self.candidates(["lib"])
		self.assertFalse(any("to override" in s for s in self.said),
		                 f"still promising an override: {self.said}")

	def test_ask_says_what_dpkg_will_do_before_the_prompt(self):
		"""-a stops the refusal but not the consequence, so the warning has
        to stand on its own -- it is the last thing shown before a prompt
        that leads straight to dpkg."""
		self.candidates(["lib"], ask=True)
		self.assertTrue(any("dpkg will not remove" in s for s in self.said),
		                f"nothing warned before the prompt: {self.said}")

	def test_naming_the_dependents_too_is_clean(self):
		"""The suggested command must actually be the quiet path, or the
        advice sends people around the same loop."""
		removals = self.candidates(["app", "lib"])
		self.assertEqual(sorted(removals), [("app", "1.0"), ("lib", "1.0")])
		self.assertEqual(self.said, [])


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
		self.mod.capture = lambda cmd, env=None: R

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


class TestDpkgBackendDrivesDpkg(unittest.TestCase):
	"""The exact dpkg command sequence the dpkg backend issues.

    dpkg is not a solver: it does what it is told, in the order it is told,
    and refuses anything that does not hold at that moment. Both sequences
    below were wrong in a way no simulation catches -- only running real dpkg
    against a throwaway root showed it."""

	def setUp(self):
		self.mod = load()
		self.mod.need_root = lambda: None
		self.mod.load_conf = lambda: {}
		self.mod.archive_settled = lambda conf, pkgs: 0
		self.mod.pending_notice = lambda conf: None
		self.mod.einfo = lambda m: None
		# after einfo is silenced, not before: constructing the backend seeds
		# the world file and announces it, which as root put "Seeded world
		# file with 170 packages" in the middle of the test output
		self.be = self.mod.DpkgBackend()
		self.calls = []

		class R:
			stdout, stderr, returncode = "", "", 0
		self.mod.capture = lambda cmd, env=None: (self.calls.append(cmd), R)[1]

	def dpkg_calls(self):
		return [c for c in self.calls if c and c[0] == "dpkg"]

	# -- merge ------------------------------------------------------------

	def run_merge(self, plan, oneshot=False):
		self.be._plan = plan
		self.be._atoms = [plan[0]["Package"]] if plan else []
		self.be._download = lambda: [f"/d/{s['Package']}.deb" for s in plan]
		self.be._read_world = lambda: set()
		self.be._write_world = lambda names: None
		with contextlib.redirect_stdout(io.StringIO()):
			self.be.merge([], [], {"fetchonly": False, "oneshot": oneshot})

	def test_a_pre_depends_is_configured_before_its_dependent_unpacks(self):
		"""Reproduced against real dpkg: it refuses to *unpack* a package
        whose Pre-Depends is merely unpacked --

            a pre-depends on libb
             libb is unpacked, but has never been configured.

        -- so deferring every configure to the end failed outright on any
        plan containing a new package that pre-depends on another new one."""
		self.run_merge([{"Package": "a", "Version": "1", "Pre-Depends": "libb"},
		                {"Package": "libb", "Version": "1"}])
		verbs = [c[c.index("--unpack") if "--unpack" in c else -2:]
		         for c in self.dpkg_calls()]
		seq = ["unpack:" + c[-1].split("/")[-1] if "--unpack" in c
		       else "configure" for c in self.dpkg_calls()]
		self.assertEqual(seq, ["unpack:libb.deb", "configure",
		                       "unpack:a.deb", "configure"],
		                 f"got {verbs}")

	def test_an_ordinary_plan_does_not_pay_for_extra_configures(self):
		"""Pre-Depends is rare; a plan without one must still be a single
        configure pass at the end."""
		self.run_merge([{"Package": "a", "Version": "1", "Depends": "libb"},
		                {"Package": "libb", "Version": "1"}])
		self.assertEqual(
		    len([c for c in self.dpkg_calls() if "--configure" in c]), 1)

	def test_the_first_pre_depends_needs_no_configure_before_it(self):
		"""Nothing is staged yet, so there is nothing to configure."""
		self.run_merge([{"Package": "a", "Version": "1",
		                 "Pre-Depends": "libc6"}])
		self.assertEqual([("configure" if "--configure" in c else "unpack")
		                  for c in self.dpkg_calls()],
		                 ["unpack", "configure"])

	def test_dependencies_are_unpacked_before_their_dependents(self):
		self.run_merge([{"Package": "a", "Version": "1"},
		                {"Package": "libb", "Version": "1"}])
		unpacks = [c[-1] for c in self.dpkg_calls() if "--unpack" in c]
		self.assertEqual(unpacks, ["/d/libb.deb", "/d/a.deb"])

	def test_progress_counts_upwards_in_the_order_things_happen(self):
		out = io.StringIO()
		self.be._plan = [{"Package": "a", "Version": "1"},
		                 {"Package": "libb", "Version": "1"}]
		self.be._atoms = ["a"]
		self.be._download = lambda: ["/d/a.deb", "/d/libb.deb"]
		self.be._read_world = lambda: set()
		self.be._write_world = lambda names: None
		with contextlib.redirect_stdout(out):
			self.be.merge([], [], {"fetchonly": False, "oneshot": False})
		text = out.getvalue()
		self.assertLess(text.index("Emerging (1 of 2) libb"),
		                text.index("Emerging (2 of 2) a"),
		                "the package merged first should be numbered first")

	# -- unmerge ----------------------------------------------------------

	def test_unmerge_is_one_call_so_dpkg_can_order_it(self):
		"""Reproduced against real dpkg: removing one at a time in the order
        the user typed dies as soon as an earlier victim is depended on by a
        later one --

            dpkg: dependency problems prevent removal of libb:
             dep depends on libb.

        -- while one call naming both succeeds, because dpkg orders removals
        itself."""
		with contextlib.redirect_stdout(io.StringIO()):
			self.be._read_world = lambda: {"libb", "dep"}
			self.be._write_world = lambda names: None
			self.be.unmerge([("libb", "1.0"), ("dep", "1.0")])
		removes = [c for c in self.dpkg_calls() if "-r" in c]
		self.assertEqual(removes, [["dpkg", "-r", "libb", "dep"]])

	def test_an_empty_unmerge_runs_no_dpkg_at_all(self):
		"""`dpkg -r` with no package names is an error, not a no-op."""
		with contextlib.redirect_stdout(io.StringIO()):
			self.be.unmerge([])
		self.assertEqual(self.dpkg_calls(), [])

	def test_the_world_file_loses_every_removed_package(self):
		written = []
		self.be._read_world = lambda: {"libb", "dep", "keep"}
		self.be._write_world = lambda names: written.append(set(names))
		with contextlib.redirect_stdout(io.StringIO()):
			self.be.unmerge([("libb", "1.0"), ("dep", "1.0")])
		self.assertEqual(written, [{"keep"}])


class TestWorldLock(unittest.TestCase):
	"""The world file is read-modify-written by merge, unmerge and
    --deselect, so two runs at once lose one set of changes. write_atomic
    prevents a torn file and says nothing about that.

    Exclusion is checked from a second open file description rather than a
    second process: flock is per description, so two opens conflict even
    inside one process -- measured before the tests were written on it."""

	def setUp(self):
		self.mod = load()
		os.makedirs(self.mod.LIB_DIR, exist_ok=True)

	def free(self):
		fd = os.open(self.mod.WORLD + ".lock",
		             os.O_CREAT | os.O_WRONLY, 0o644)
		try:
			fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
			fcntl.flock(fd, fcntl.LOCK_UN)
			return True
		except OSError:
			return False
		finally:
			os.close(fd)

	def test_it_excludes_another_holder(self):
		with self.mod.world_lock():
			self.assertFalse(self.free())

	def test_it_is_released_afterwards(self):
		with self.mod.world_lock():
			pass
		self.assertTrue(self.free())

	def test_a_failure_inside_still_releases_it(self):
		"""A lock a crashed run keeps is worse than no lock: every later
        run waits forever on a holder that is gone. flock also has the
        kernel drop it if the process dies, so there is nothing stale to
        clear -- this covers the ordinary raise."""
		with self.assertRaises(RuntimeError):
			with self.mod.world_lock():
				raise RuntimeError("boom")
		self.assertTrue(self.free())

	def test_it_lives_beside_the_world_file_it_locks(self):
		"""Not in a shared place, and emphatically not dpkg's frontend
        lock: it coordinates emerge with emerge and nothing else. Where it
        lives is how it says so."""
		with self.mod.world_lock():
			pass
		self.assertTrue(os.path.exists(self.mod.WORLD + ".lock"))
		self.assertEqual(os.path.dirname(self.mod.WORLD + ".lock"),
		                 os.path.dirname(self.mod.WORLD))

	def test_a_pretend_deselect_takes_none_and_leaves_nothing(self):
		"""`-p` creating a world file is a bug this project already
        shipped; a lock file is the same bug with a new name."""
		be = self.mod.DpkgBackend(pretend=True)
		with open(self.mod.WORLD, "w") as f:
			f.write("sl\n")
		if os.path.exists(self.mod.WORLD + ".lock"):
			os.unlink(self.mod.WORLD + ".lock")
		with contextlib.redirect_stdout(io.StringIO()):
			be.deselect(["sl"], pretend=True)
		self.assertFalse(os.path.exists(self.mod.WORLD + ".lock"))
		with open(self.mod.WORLD) as f:
			self.assertEqual(f.read().split(), ["sl"])

	def watch(self):
		"""Record entries into the real lock, so a caller that stopped
        taking it is visible."""
		entered = []
		real = self.mod.world_lock

		@contextlib.contextmanager
		def watched():
			entered.append(True)
			with real():
				yield

		self.mod.world_lock = watched
		return entered

	def test_merge_takes_it_around_recording_the_atoms(self):
		self.mod.need_root = lambda: None
		self.mod.load_conf = lambda: {}
		self.mod.archive_settled = lambda conf, pkgs: 0
		self.mod.pending_notice = lambda conf: None
		self.mod.einfo = lambda m: None

		class R:
			stdout, stderr, returncode = "", "", 0
		self.mod.capture = lambda cmd, env=None: R
		be = self.mod.DpkgBackend()
		be._plan = [{"Package": "sl", "Version": "1"}]
		be._atoms = ["sl"]
		be._download = lambda: ["/d/sl.deb"]
		entered = self.watch()
		with contextlib.redirect_stdout(io.StringIO()):
			be.merge([], [], {"fetchonly": False, "oneshot": False})
		self.assertEqual(entered, [True])
		with open(self.mod.WORLD) as f:
			self.assertIn("sl", f.read().split())

	def test_unmerge_takes_it_too(self):
		self.mod.need_root = lambda: None
		self.mod.einfo = lambda m: None

		class R:
			stdout, stderr, returncode = "", "", 0
		self.mod.capture = lambda cmd, env=None: R
		be = self.mod.DpkgBackend()
		with open(self.mod.WORLD, "w") as f:
			f.write("sl\nbash\n")
		entered = self.watch()
		with contextlib.redirect_stdout(io.StringIO()):
			be.unmerge([("sl", "1")])
		self.assertEqual(entered, [True])
		with open(self.mod.WORLD) as f:
			self.assertEqual(f.read().split(), ["bash"])

	def test_a_real_deselect_takes_it(self):
		"""The wiring, not the primitive: a lock nothing enters protects
        nothing, and the callers are where that goes wrong."""
		self.mod.need_root = lambda: None
		with open(self.mod.WORLD, "w") as f:
			f.write("sl\n")
		entered = []
		real = self.mod.world_lock

		@contextlib.contextmanager
		def watched():
			entered.append(True)
			with real():
				yield

		self.mod.world_lock = watched
		be = self.mod.DpkgBackend()
		with contextlib.redirect_stdout(io.StringIO()):
			be.deselect(["sl"], pretend=False)
		self.assertEqual(entered, [True])
		with open(self.mod.WORLD) as f:
			self.assertEqual(f.read().split(), [])


class TestSeedWorldTiming(unittest.TestCase):
	"""When the world file is seeded, which is load-bearing.

	A first run has no world file, and everything installed is what the user
	chose, so that is the seed. It has to be taken *before* the run installs
	anything: merge() reads the world file after dpkg has run, so seeding
	lazily on first read captured the packages the merge had just installed
	and pulled every dependency of the first install into @world for good.

	That is invisible unless you are root -- the guard returns early
	otherwise -- so it passed on a workstation and only failed in a
	debian:trixie container. These tests fake root so they see it here."""

	def setUp(self):
		self.mod = load()
		self.seeded = []
		self.installed = {"bash": {"Package": "bash"},
		                  "coreutils": {"Package": "coreutils"}}
		self.mod.installed_state = lambda: dict(self.installed)
		self.mod.einfo = self.seeded.append
		# Only root is faked. The seed writes for real, into the scratch
		# directory load() hands out -- it used to be observed through a
		# fake `open`, which stopped seeing anything the moment seeding
		# started going through write_atomic like every other world write.
		# Reading the file back is what the test meant all along.
		self.patch(self.mod.os, "geteuid", lambda: 0)

	def patch(self, obj, attr, value):
		original = getattr(obj, attr)
		self.addCleanup(setattr, obj, attr, original)
		setattr(obj, attr, value)

	def seeded_names(self):
		if not os.path.exists(self.mod.WORLD):
			return []
		with open(self.mod.WORLD) as f:
			return sorted(f.read().split())

	def created(self):
		"""Everything the run left behind, lock file included."""
		return sorted(p for p in (self.mod.WORLD, self.mod.WORLD + ".lock",
		                          self.mod.LIB_DIR) if os.path.exists(p))

	def test_the_seed_is_taken_at_construction(self):
		self.mod.DpkgBackend()
		self.assertEqual(self.seeded_names(), ["bash", "coreutils"])

	def test_the_seed_predates_anything_the_run_installs(self):
		"""The regression, stated directly: a package installed after the
		backend exists must not appear in the seed."""
		be = self.mod.DpkgBackend()
		self.installed["freshly-installed-dep"] = {
		    "Package": "freshly-installed-dep"}
		be._read_world()
		self.assertNotIn("freshly-installed-dep", self.seeded_names())

	def test_the_seed_goes_through_the_shared_writer(self):
		"""It had its own `open(WORLD, "w")` and was therefore the one
        world write that an interrupt could truncate -- while project.md
        stated that all of them were atomic. A private copy of a durability
        sequence is exactly how that gap opens."""
		wrote = []
		real = self.mod.write_atomic

		def watched(path, data, *a, **kw):
			wrote.append(path)
			return real(path, data, *a, **kw)

		self.patch(self.mod, "write_atomic", watched)
		self.mod.DpkgBackend()
		self.assertEqual(wrote, [self.mod.WORLD])

	def test_seeding_happens_once(self):
		be = self.mod.DpkgBackend()
		for _ in range(4):
			be._read_world()
		self.assertEqual(len(self.seeded), 1)

	def test_an_existing_world_file_is_never_reseeded(self):
		os.makedirs(self.mod.LIB_DIR, exist_ok=True)
		with open(self.mod.WORLD, "w") as f:
			f.write("chosen-by-hand\n")
		self.mod.DpkgBackend()
		self.assertEqual(self.seeded, [])
		self.assertEqual(self.seeded_names(), ["chosen-by-hand"])

	def test_version_creates_nothing(self):
		"""-V wants the backend's name, not a backend. Constructing one
		seeds the world file, and a version query must not write to
		/var/lib -- nor leave a lock file there, which is the same
		promise with a new way to break it."""
		self.patch(self.mod.shutil, "which", lambda p: None)   # force dpkg
		out = io.StringIO()
		with contextlib.redirect_stdout(out):
			self.mod.main(["-V"])
		self.assertIn("dpkg backend", out.getvalue())
		self.assertEqual(self.seeded, [])
		self.assertEqual(self.created(), [])

	def test_help_creates_nothing_either(self):
		self.patch(self.mod.shutil, "which", lambda p: None)
		with contextlib.redirect_stdout(io.StringIO()):
			self.mod.main(["--help"])
		self.assertEqual(self.created(), [])

	def test_backend_name_agrees_with_the_backend_it_would_build(self):
		for forced in ("apt", "dpkg"):
			with self.subTest(forced=forced):
				self.assertEqual(self.mod.backend_name(forced),
				                 self.mod.pick_backend(forced).name)


class TestStaleWorldEntries(unittest.TestCase):
	"""A world entry that is not installed is dropped from @selected, and it
    used to be dropped without a word.

    That left the two ways of asking for one package disagreeing: naming it
    outright answers `there are no packages to satisfy "foo"` and exits 1,
    while reaching the same package through @world printed an ordinary plan
    and exited 0. The world file is the durable record of what the admin
    asked for, and a package leaving the Debian archive between releases is
    ordinary, so the entry that can no longer be satisfied is exactly the
    thing worth saying out loud."""

	def setUp(self):
		self.mod = load()
		root = _scratch()
		lib = os.path.join(root, "lib")
		os.makedirs(os.path.join(lib, "tree"))
		self.mod.LIB_DIR = lib
		self.mod.TREE_DIR = os.path.join(lib, "tree")
		self.mod.WORLD = os.path.join(lib, "world")
		self.mod.STATUS = os.path.join(root, "status")
		self.put(self.mod.STATUS,
		         "Package: real\nStatus: install ok installed\n"
		         "Version: 1.0\nArchitecture: all\nPriority: optional\n\n")
		self.put(os.path.join(self.mod.TREE_DIR, "Packages"),
		         "Package: real\nVersion: 1.0\nArchitecture: all\n"
		         f"Filename: pool/real.deb\nSize: 100\nSHA256: {'0' * 64}\n\n")

	def put(self, path, text):
		with open(path, "w") as f:
			f.write(text)

	def world(self, *names):
		self.put(self.mod.WORLD, "".join(n + "\n" for n in names))

	def plan(self):
		"""stdout of `emerge --backend=dpkg -p @world`."""
		buf = io.StringIO()
		with contextlib.redirect_stdout(buf):
			try:
				self.mod.main(["--backend=dpkg", "-p", "@world"])
			except SystemExit:
				pass
		return buf.getvalue()

	def test_an_entry_that_is_not_installed_is_named(self):
		self.world("real", "gone-from-the-archive")
		out = self.plan()
		self.assertIn("gone-from-the-archive", out,
		              "the world file names a package that cannot be "
		              "satisfied and nothing said so")

	def test_the_plan_is_still_produced(self):
		"""Warn, do not fail. Refusing to compute @world until the file was
        tidied would block the upgrade that resolves it."""
		self.world("real", "gone-from-the-archive")
		out = self.plan()
		self.assertIn("[ebuild  R    ] real-1.0", out)

	def test_nothing_is_said_when_every_entry_is_installed(self):
		self.world("real")
		self.assertNotIn("not installed", self.plan())

	def test_the_warning_waits_for_the_progress_line_to_finish(self):
		"""`Calculating dependencies... ` is left open with no newline, so a
        warning printed during the resolve lands in the middle of it. The
        sync path had this bug and fixed it by reordering; a resolve cannot
        be reordered, because the problem is found inside it."""
		self.world("real", "gone-from-the-archive")
		self.assertIn("Calculating dependencies... done!",
		              self.plan().splitlines()[0],
		              "the advisory broke into the progress line")

	def test_it_is_said_once_however_often_the_set_is_expanded(self):
		self.world("real", "gone-from-the-archive")
		backend = self.mod.DpkgBackend()
		inst = self.mod.installed_state()
		said = []
		self.mod.ewarn_later = said.append
		backend.expand_sets(["@world"])
		backend.expand_sets(["@selected"])
		backend._selected(inst)
		self.assertEqual(len(said), 2,
		                 "one advisory is two lines; repeating it per "
		                 "expansion would bury the plan")


class TestDeselect(unittest.TestCase):
	"""--deselect drops a package from @selected without unmerging it.

    It exists because nothing else could. `emerge -C` removes a world entry,
    but only as a side effect of unmerging, and it skips a package that is
    not installed -- which is precisely the entry worth removing when the
    archive has dropped a package. Until this, the resolver would report a
    stale entry on every run and offer no way to clear it but a text
    editor."""

	def setUp(self):
		self.mod = load()
		root = _scratch()
		lib = os.path.join(root, "lib")
		os.makedirs(os.path.join(lib, "tree"))
		self.mod.LIB_DIR = lib
		self.mod.TREE_DIR = os.path.join(lib, "tree")
		self.mod.WORLD = os.path.join(lib, "world")
		self.mod.STATUS = os.path.join(root, "status")
		self.mod.need_root = lambda: None
		with open(self.mod.STATUS, "w") as f:
			f.write("Package: real\nStatus: install ok installed\n"
			        "Version: 1.0\nArchitecture: all\nPriority: optional\n\n")
		self.set_world("real", "gone-from-the-archive")

	def set_world(self, *names):
		with open(self.mod.WORLD, "w") as f:
			f.write("".join(n + "\n" for n in names))

	def world(self):
		with open(self.mod.WORLD) as f:
			return {l.strip() for l in f if l.strip()}

	def deselect(self, *names, pretend=False):
		buf = io.StringIO()
		with contextlib.redirect_stdout(buf):
			self.mod.DpkgBackend().deselect(list(names), pretend)
		return buf.getvalue()

	def test_it_removes_an_entry_whose_package_is_gone(self):
		"""The case --unmerge cannot reach: not installed, so it is skipped
        there, and the world file keeps naming it forever."""
		self.deselect("gone-from-the-archive")
		self.assertEqual(self.world(), {"real"})

	def test_it_does_not_unmerge_what_it_deselects(self):
		"""The whole distinction from --unmerge. `real` is installed; after
        deselecting it, it must still be installed and merely unchosen."""
		self.deselect("real")
		self.assertEqual(self.world(), {"gone-from-the-archive"})
		self.assertIn("real", self.mod.installed_state())

	def test_pretend_changes_nothing(self):
		out = self.deselect("gone-from-the-archive", pretend=True)
		self.assertEqual(self.world(), {"real", "gone-from-the-archive"})
		self.assertIn("Would remove", out,
		              "a pretend run should not claim to have removed it")

	def test_a_name_that_is_not_selected_is_reported_and_skipped(self):
		said = []
		self.mod.ewarn = said.append
		self.deselect("never-chosen")
		self.assertEqual(self.world(), {"real", "gone-from-the-archive"})
		self.assertTrue(any("never-chosen" in s for s in said))

	def test_one_bad_name_does_not_stop_the_others(self):
		self.deselect("never-chosen", "gone-from-the-archive")
		self.assertEqual(self.world(), {"real"})

	def test_it_asks_for_root_before_it_announces_anything(self):
		"""It printed `Removing x` and then discovered it was not root, so a
        non-root run claimed to have done the work. Worse in practice than
        it sounds: stdout is block-buffered and the error is not, so the
        permission failure appeared *above* the line saying it had already
        happened."""
		order = []
		self.mod.need_root = lambda: order.append("need_root")
		buf = io.StringIO()
		with contextlib.redirect_stdout(buf):
			self.mod.DpkgBackend().deselect(["real"], False)
		self.assertEqual(order, ["need_root"], "need_root was never called")
		self.assertIn("Removing", buf.getvalue())
		# and nothing is announced when there is nothing to do
		order.clear()
		with contextlib.redirect_stdout(io.StringIO()):
			self.mod.DpkgBackend().deselect(["never-chosen"], False)
		self.assertEqual(order, [], "asked for root with nothing to remove")

	def test_pretend_needs_no_privileges(self):
		def refuse():
			raise AssertionError("--pretend asked for root")
		self.mod.need_root = refuse
		with contextlib.redirect_stdout(io.StringIO()):
			self.mod.DpkgBackend().deselect(["real"], True)

	def test_it_reports_what_it_dropped(self):
		"""main turns an empty result into a non-zero exit, because `emerge
        -C` already does that when none of its targets were installed, and
        two verbs meaning "you named things I could not act on" should not
        disagree about whether that is a failure."""
		with contextlib.redirect_stdout(io.StringIO()):
			dropped = self.mod.DpkgBackend().deselect(
			    ["real", "never-chosen"], False)
			nothing = self.mod.DpkgBackend().deselect(["never-chosen"], False)
		self.assertEqual(dropped, ["real"])
		self.assertEqual(nothing, [])

	def test_naming_nothing_that_is_selected_exits_non_zero(self):
		with self.assertRaises(SystemExit) as exit:
			with contextlib.redirect_stdout(io.StringIO()):
				self.mod.main(["--backend=dpkg", "--deselect", "never-chosen"])
		self.assertEqual(exit.exception.code, 1)

	def test_the_option_reaches_the_backend(self):
		"""Wiring: the flag is parsed, and --deselect with no targets is an
        error rather than a silent no-op."""
		seen = {}

		def stub(self_, names, pretend):
			seen.update(names=names, pretend=pretend)
			return list(names)     # what it dropped; empty means nothing did
		self.mod.DpkgBackend.deselect = stub
		with contextlib.redirect_stdout(io.StringIO()):
			self.mod.main(["--backend=dpkg", "--deselect", "-p", "foo"])
		self.assertEqual(seen, {"names": ["foo"], "pretend": True})
		with self.assertRaises(SystemExit) as caught:
			with contextlib.redirect_stdout(io.StringIO()):
				self.mod.main(["--backend=dpkg", "--deselect"])
		self.assertEqual(caught.exception.code, 1)


class TestPretendWritesNothing(unittest.TestCase):
	"""--pretend must leave the system exactly as it found it.

    The dpkg backend seeds its world file when the backend is constructed,
    and that timing is load-bearing: deferring it to the first read meant
    merge() seeded *after* dpkg had run, pulling every dependency of the
    first install into @world for good. But construction happens for a
    pretend run too, so on a fresh root box `emerge -p bash` created a
    92-entry world file -- on a run that then failed for want of a package
    tree. That is the shape of the `emerge -V` bug that moved seeding out of
    pick_backend(), arriving through a different door.

    Both properties are asserted here because the fix has to keep the first
    while removing the second, and either one alone is easy."""

	def setUp(self):
		self.mod = load()
		root = _scratch()
		os.makedirs(root)
		self.mod.LIB_DIR = os.path.join(root, "lib")
		self.mod.WORLD = os.path.join(self.mod.LIB_DIR, "world")
		self.mod.STATUS = os.path.join(root, "status")
		with open(self.mod.STATUS, "w") as f:
			for name in ("bash", "coreutils", "tree"):
				f.write(f"Package: {name}\nStatus: install ok installed\n"
				        f"Version: 1.0\nArchitecture: all\n"
				        f"Priority: optional\n\n")
		# Seeding returns early for anyone but root, which is why this went
		# unnoticed on a workstation and showed up in a container.
		original = os.geteuid
		self.addCleanup(setattr, os, "geteuid", original)
		os.geteuid = lambda: 0

	def test_a_pretend_run_creates_no_world_file(self):
		with contextlib.redirect_stdout(io.StringIO()):
			self.mod.DpkgBackend(pretend=True)
		self.assertFalse(os.path.exists(self.mod.WORLD),
		                 "--pretend wrote a world file")
		self.assertFalse(os.path.exists(self.mod.LIB_DIR),
		                 "--pretend created /var/lib/emerge-dpkg")

	def test_a_pretend_run_still_shows_the_set_a_real_run_would_use(self):
		"""Skipping the seed entirely would have been the easy fix and the
        wrong one: @selected would read empty, so `emerge -p @world` would
        show a smaller set than the run it is previewing."""
		with contextlib.redirect_stdout(io.StringIO()):
			pretend = self.mod.DpkgBackend(pretend=True)
			_, members = pretend.expand_sets(["@selected"])
		self.assertEqual(sorted(members), ["bash", "coreutils", "tree"])

	def test_a_real_run_still_seeds_at_construction(self):
		"""The load-bearing half. Seeding must happen before anything is
        installed, so it belongs at construction and not at first read."""
		with contextlib.redirect_stdout(io.StringIO()):
			self.mod.DpkgBackend()
		with open(self.mod.WORLD) as f:
			self.assertEqual(sorted(f.read().split()),
			                 ["bash", "coreutils", "tree"])

	def test_pick_backend_passes_the_flag_through(self):
		"""The wiring is the part that silently does nothing when it is
        wrong, because the default is the old behaviour.

        Forced by name rather than by hiding apt-get: stubbing shutil.which
        would patch the module object the script shares with this process,
        and the first version of this test did exactly that and restored the
        stub over itself. The module sentinel caught it."""
		with contextlib.redirect_stdout(io.StringIO()):
			self.mod.pick_backend("dpkg", pretend=True)
		self.assertFalse(os.path.exists(self.mod.WORLD),
		                 "pick_backend did not pass --pretend to the backend")


class TestMultiarchIsNoticed(unittest.TestCase):
	"""`installed_state()` is keyed by name, and a multiarch system makes
    that ambiguous.

    After `dpkg --add-architecture i386`, `libfoo:amd64` and `libfoo:i386`
    are two installed packages sharing one name, and the later stanza won --
    silently, so the resolver planned against a view of the machine with
    packages missing from it. `emerge -C libfoo` then reached dpkg, which
    refuses: "ambiguous package name 'libfoo' with more than one installed
    instance", verified against real dpkg with two hand-built .debs.

    Keying by name:architecture throughout is the real fix and a real piece
    of work -- it reaches the resolver, the world file, depclean and the
    index, which is fetched for the native architecture only. The backend is
    for single-architecture embedded boxes, so this says so rather than
    pretending otherwise."""

	MULTI = ("Package: libfoo\nStatus: install ok installed\nVersion: 1.0\n"
	         "Architecture: amd64\nPriority: optional\n\n"
	         "Package: libfoo\nStatus: install ok installed\nVersion: 0.9\n"
	         "Architecture: i386\nPriority: optional\n\n"
	         "Package: tree\nStatus: install ok installed\nVersion: 2.0\n"
	         "Architecture: amd64\nPriority: optional\n\n")
	SINGLE = ("Package: tree\nStatus: install ok installed\nVersion: 2.0\n"
	          "Architecture: amd64\nPriority: optional\n\n")

	def setUp(self):
		self.mod = load()
		# Silenced before any backend is built: constructing one seeds the
		# world file and announces it, which as root drops "Seeded world
		# file with 170 packages" into the middle of the test output.
		# Invisible to an ordinary user, since the seed guard returns
		# early for one.
		self.mod.einfo = lambda m: None
		root = _scratch()
		os.makedirs(root)
		self.mod.STATUS = os.path.join(root, "status")
		self.said = []
		self.mod.ewarn = self.said.append
		self.mod.ewarn_later = self.said.append
		self.mod.eerror = self.said.append

	def status(self, text):
		with open(self.mod.STATUS, "w") as f:
			f.write(text)

	def test_an_ordinary_single_architecture_system_says_nothing(self):
		"""The false-positive case, and the one that matters most: every
        normal machine must go through here silently."""
		self.status(self.SINGLE)
		self.mod.installed_state()
		self.assertEqual(self.mod.MULTIARCH_INSTANCES, {})

	def test_the_collision_is_recorded_with_its_architectures(self):
		self.status(self.MULTI)
		inst = self.mod.installed_state()
		self.assertEqual(self.mod.MULTIARCH_INSTANCES,
		                 {"libfoo": ["amd64", "i386"]})
		self.assertEqual(len(inst), 2, "still one entry per name")

	def test_it_is_cleared_when_the_system_no_longer_has_one(self):
		"""It is module state, so a stale entry would outlive its status
        file and warn about a machine that had been fixed."""
		self.status(self.MULTI)
		self.mod.installed_state()
		self.status(self.SINGLE)
		self.mod.installed_state()
		self.assertEqual(self.mod.MULTIARCH_INSTANCES, {})

	def test_unmerging_an_ambiguous_name_is_refused_with_the_instances(self):
		self.status(self.MULTI)
		be = self.mod.DpkgBackend()
		with self.assertRaises(SystemExit):
			with contextlib.redirect_stdout(io.StringIO()):
				be.unmerge_candidates(["libfoo"],
				                      {"ask": False, "pretend": False})
		joined = " ".join(self.said)
		self.assertIn("libfoo:amd64", joined)
		self.assertIn("libfoo:i386", joined)
		self.assertIn("dpkg -r libfoo:amd64", joined,
		              "the refusal should name the command that works")

	def test_an_unambiguous_name_on_the_same_system_still_unmerges(self):
		"""The warning must not become a refusal for everything else on the
        machine."""
		self.status(self.MULTI)
		be = self.mod.DpkgBackend()
		with contextlib.redirect_stdout(io.StringIO()):
			removals = be.unmerge_candidates(["tree"],
			                                 {"ask": False, "pretend": False})
		self.assertEqual(removals, [("tree", "2.0")])


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

		def capture(cmd, env=None):
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


class TestPrompts(unittest.TestCase):
	"""The two confirmation prompts default in opposite directions on
    purpose, and both are the last thing standing between a user and an
    irreversible action. A future edit flipping either would silently
    auto-confirm things like an 868-package unmerge, so pin them."""

	def setUp(self):
		self.mod = load()

	def answer(self, text):
		self.mod.input = lambda prompt="": text

	def refuse(self, exc):
		def boom(prompt=""):
			raise exc
		self.mod.input = boom

	def ask_continue(self):
		with contextlib.redirect_stdout(io.StringIO()):
			return self.mod.ask_continue()

	def ask_yesno(self):
		with contextlib.redirect_stdout(io.StringIO()):
			return self.mod.ask_yesno("risky?")

	# -- ask_continue: proceeding is the default, as in Portage -------------

	def test_continue_defaults_to_yes_on_a_bare_return(self):
		self.answer("")
		self.assertTrue(self.ask_continue())

	def test_continue_accepts_the_usual_spellings(self):
		for text in ("y", "Y", "yes", "YES", " yes "):
			with self.subTest(text=text):
				self.answer(text)
				self.assertTrue(self.ask_continue())

	def test_continue_treats_anything_else_as_no(self):
		for text in ("n", "no", "q", "yeah", "later"):
			with self.subTest(text=text):
				self.answer(text)
				self.assertFalse(self.ask_continue())

	# -- ask_yesno: declining is the default, for risky confirmations -------

	def test_yesno_defaults_to_no_on_a_bare_return(self):
		"""Used to allow session-critical packages to move. Silence must
        never be consent here."""
		self.answer("")
		self.assertFalse(self.ask_yesno())

	def test_yesno_needs_an_explicit_yes(self):
		for text in ("y", "Y", "yes", " YES "):
			with self.subTest(text=text):
				self.answer(text)
				self.assertTrue(self.ask_yesno())

	def test_yesno_rejects_everything_else(self):
		for text in ("n", "no", "sure", "ok", "1"):
			with self.subTest(text=text):
				self.answer(text)
				self.assertFalse(self.ask_yesno())

	# -- neither may proceed when there is nobody to ask --------------------

	def test_both_decline_at_end_of_input(self):
		for exc in (EOFError, KeyboardInterrupt):
			with self.subTest(exc=exc.__name__):
				self.refuse(exc)
				self.assertFalse(self.ask_continue())
				self.refuse(exc)
				self.assertFalse(self.ask_yesno())


class TestBackendSelection(unittest.TestCase):
	"""Which backend gets picked, and what `-V` says when it cannot be used.

	`-V` used to construct a backend just to read its name, which on the dpkg
	side seeds the world file -- so a version query created state. It asks
	backend_name() now, and that changed one edge: `--backend=apt` on a box
	with no apt-get used to make `-V` exit 1, and now reports "apt backend".

	That is deliberate. `-V` is informational and reports the backend that is
	configured; it does not try to use one. Anything that actually needs the
	backend still refuses, clearly, which is the message worth having."""

	def setUp(self):
		self.mod = load()
		self.errors = []
		self.mod.eerror = self.errors.append
		# constructing the dpkg backend seeds the world file and announces
		# it, which as root put the notice in the middle of the test output
		self.mod.einfo = lambda m: None

	def without_apt(self):
		original = self.mod.shutil.which
		self.addCleanup(setattr, self.mod.shutil, "which", original)
		self.mod.shutil.which = lambda n: (None if n == "apt-get"
		                                   else original(n))

	def test_apt_is_chosen_when_apt_get_is_present(self):
		self.assertEqual(self.mod.backend_name(None), "apt")

	def test_dpkg_is_chosen_when_apt_get_is_absent(self):
		self.without_apt()
		self.assertEqual(self.mod.backend_name(None), "dpkg")
		self.assertEqual(self.mod.pick_backend(None).name, "dpkg")

	def test_an_explicit_choice_wins_over_detection(self):
		self.assertEqual(self.mod.backend_name("dpkg"), "dpkg")
		self.without_apt()
		self.assertEqual(self.mod.backend_name("apt"), "apt")

	def test_the_environment_variable_is_honoured(self):
		self.addCleanup(os.environ.pop, "EMERGE_BACKEND", None)
		os.environ["EMERGE_BACKEND"] = "dpkg"
		self.assertEqual(self.mod.backend_name(None), "dpkg")

	def test_an_unknown_backend_is_rejected(self):
		with self.assertRaises(SystemExit):
			self.mod.backend_name("nonsense")
		self.assertTrue(any("unknown backend" in e for e in self.errors))

	def test_version_reports_the_requested_backend_even_if_unusable(self):
		self.without_apt()
		out = io.StringIO()
		with contextlib.redirect_stdout(out):
			self.mod.main(["--backend=apt", "-V"])
		self.assertIn("apt backend", out.getvalue())

	def test_but_using_it_refuses_clearly(self):
		self.without_apt()
		with self.assertRaises(SystemExit):
			self.mod.pick_backend("apt")
		self.assertTrue(any("apt-get not found" in e for e in self.errors),
		                self.errors)


class TestPipeBehaviour(unittest.TestCase):
	"""`emerge -pv @world | head` is how people read a long package list.

	Python ignores SIGPIPE and raises BrokenPipeError instead, which surfaces
	at interpreter shutdown as "Exception ignored on flushing sys.stdout" and
	exit status 120 -- a traceback and an error for doing something entirely
	ordinary. Restoring the default handler makes it behave like every other
	Unix tool. This drives the real script through a real pipe, because the
	failure only happens at shutdown and no in-process test would see it."""

	def piped(self, args, take=2):
		head = subprocess.Popen(["head", f"-{take}"],
		                        stdin=subprocess.PIPE,
		                        stdout=subprocess.DEVNULL)
		proc = subprocess.Popen([sys.executable, SCRIPT] + args,
		                        stdout=head.stdin,
		                        stderr=subprocess.PIPE, text=True)
		head.stdin.close()
		_, err = proc.communicate()
		head.wait()
		return proc.returncode, err

	def test_help_through_a_closed_pipe_is_silent(self):
		rc, err = self.piped(["--help"])
		self.assertEqual(err, "", f"noise on stderr: {err}")

	def test_it_does_not_report_a_python_traceback(self):
		rc, err = self.piped(["--help"])
		self.assertNotIn("BrokenPipeError", err)
		self.assertNotIn("Traceback", err)

	def test_the_exit_status_is_not_pythons_flush_failure(self):
		"""120 is the interpreter failing to flush at shutdown. Anything
		else -- 0, or 141 for a SIGPIPE death -- is a shell-normal answer."""
		rc, _ = self.piped(["--help"])
		self.assertNotEqual(rc, 120)
		self.assertIn(rc, (0, 141, -13))

	def test_a_full_read_is_unaffected(self):
		"""The fix must not change the ordinary case."""
		r = subprocess.run([sys.executable, SCRIPT, "--help"],
		                   capture_output=True, text=True)
		self.assertEqual(r.returncode, 0)
		self.assertIn("Usage:", r.stdout)
		self.assertEqual(r.stderr, "")


class TestPackaging(unittest.TestCase):
	"""The packaging carries three copies of things the script also knows:
    the version, the option list, and the names it answers to. Nothing at
    build time compares them, so an option added to --help and forgotten in
    the man page ships as a documented feature nobody can look up."""

	def read(self, name):
		path = os.path.join(HERE, name)
		if not os.path.exists(path):
			self.skipTest(f"{name} is not present in this tree")
		with open(path) as f:
			return f.read()

	def test_the_package_version_matches_the_version_file(self):
		"""VERSION states this program's version; debian/changelog is the
        only other place dpkg will read one from. `make version-check`
        enforces it too, but failing here is faster and works without
        dpkg-dev installed."""
		head = self.read("debian/changelog").splitlines()[0]
		m = re.match(r"^apt-emerge \(([^)]+)\)", head)
		self.assertIsNotNone(m, f"unparsable changelog header: {head}")
		self.assertEqual(m.group(1), self.read("VERSION").strip(),
		                 "debian/changelog and VERSION disagree")

	def test_the_script_reports_its_own_version(self):
		"""The shipped artifact is one file somebody scp'd onto a box with
        nothing else of ours on it, so the VERSION file is not there to be
        read. A program that cannot say which version it is answers with
        the Portage dialect, which is identical in every release -- and
        `--info` exists precisely to be pasted into a bug report.

        `make version-check` enforces this too; failing here is faster and
        needs no dpkg-dev."""
		self.assertEqual(em.APT_EMERGE_VERSION, self.read("VERSION").strip(),
		                 "the script and the VERSION file disagree")

	def test_the_dialect_version_is_not_this_program_s_version(self):
		"""em.VERSION is the Portage release whose dialect this speaks, and
        is deliberately independent of the package version. They described
        the same number until apt-emerge had one of its own, and tying them
        again would make `emerge --version` claim a Portage dialect that
        does not exist."""
		self.assertTrue(em.VERSION.endswith("-deb"))
		self.assertEqual(em.VERSION, em.PORTAGE_DIALECT + "-deb")

	def man_entries(self):
		"""Long options the man page gives an entry of their own.

        Deliberately not a substring search over the whole page: options are
        cross-referenced from each other's prose all the time, so "is the
        string present" passes for an option that has no entry at all. Only
        a .TP tagged paragraph counts as documenting it."""
		man = self.read("emerge.1")
		tagged = re.findall(r"^\.TP\n(.*)$", man, re.M)
		return {opt for line in tagged
		        for opt in re.findall(r"--[a-z][a-z-]*", line)}

	def _key_list(self, text, start, end):
		"""The sentence that enumerates the config keys, and only it.

        Scoped rather than searched over the whole document for the reason
        the option scrape is: `diff` and `merge` are ordinary words, so "is
        the string present" would pass for a key nobody had listed."""
		i = text.find(start)
		self.assertNotEqual(i, -1, f"key list not found: {start!r}")
		j = text.find(end, i)
		self.assertNotEqual(j, -1, f"key list has no end: {end!r}")
		return text[i:j]

	def test_every_config_key_is_listed_in_the_man_page(self):
		"""Options are held to the man page in both directions; the config
        keys were held to nothing, and a man page cannot point at
        DEFAULT_CONF the way project.md now does. Both lists had gone
        behind it -- the page missing recover-ancestor, --help missing that
        and `diff` besides."""
		listed = self._key_list(self.read("emerge.1"),
		                        "Config-merging settings. Keys:", ".\n")
		missing = sorted(k for k in em.DEFAULT_CONF if k not in listed)
		self.assertEqual(missing, [],
		                 f"in DEFAULT_CONF but not listed in emerge.1: "
		                 f"{missing}")

	def test_every_config_key_is_listed_in_help(self):
		"""--help is what you have on a box you only copied the script to,
        so the list there has to be the whole list too."""
		listed = self._key_list(em.HELP, "keys:", "For example")
		missing = sorted(k for k in em.DEFAULT_CONF if k not in listed)
		self.assertEqual(missing, [],
		                 f"in DEFAULT_CONF but not listed in --help: "
		                 f"{missing}")

	def test_every_option_in_help_has_its_own_man_page_entry(self):
		documented = set(re.findall(r"^ {3}(--[a-z][a-z-]*)", em.HELP, re.M))
		self.assertGreater(len(documented), 15, "option scrape found too few")
		missing = sorted(documented - self.man_entries())
		self.assertEqual(missing, [],
		                 f"documented in --help but with no entry in "
		                 f"emerge.1: {missing}")

	def test_the_man_page_documents_no_option_the_script_rejects(self):
		"""Drift in the other direction: an entry for a flag that was
        renamed or removed sends people to `unknown option: ...`."""
		src = self.read("emerge")
		for opt in sorted(self.man_entries()):
			with self.subTest(option=opt):
				self.assertIn(opt, src,
				              f"emerge.1 documents {opt}, which the script "
				              f"does not mention at all")

	def test_the_man_page_has_the_sections_the_readme_sends_people_to(self):
		"""The README calls `man emerge` the reference and says what is in
        it, because the single-file install carries no page and a reader has
        to know what they are missing. A section that goes away makes that
        sentence a lie, silently -- the README is prose and nothing else
        reads it."""
		man = self.read("emerge.1")
		present = set(re.findall(r"^\.SH (.+)$", man, re.M))
		self.assertGreater(len(present), 5, "the .SH scrape found too few")
		for section in ("OPTIONS", "SETS", "ENVIRONMENT", "FILES"):
			with self.subTest(section=section):
				self.assertIn(section, present,
				              f"README promises {section} in emerge.1")

	def test_every_name_the_script_answers_to_is_shipped(self):
		"""argv[0] selects the action, so a name the script recognises but
        the package does not install is a feature that exists only for
        people who symlink it by hand."""
		src = self.read("emerge")
		m = re.search(r"_self in \(([^)]*)\)", src)
		self.assertIsNotNone(m)
		answers_to = set(re.findall(r'"([^"]+)"', m.group(1)))
		makefile = self.read("Makefile")
		shipped = set(re.search(r"^ALIASES = (.*)$", makefile, re.M)
		              .group(1).split())
		self.assertEqual(answers_to, shipped,
		                 "the script and the Makefile disagree about the "
		                 "names it should be installed under")

	def test_the_readme_shows_output_the_program_actually_prints(self):
		"""The README opens with console output, and it is the first thing
		anyone reads. A format change that leaves it behind makes the
		project advertise something it no longer does -- and the examples
		are real captures, so they can simply be re-rendered and compared."""
		readme = self.read("README.md")
		mod = load()
		mod._session_critical_cache = {"libgl1-mesa-dri", "libglx-mesa0"}
		mod._session_blind = False
		buf = io.StringIO()
		with contextlib.redirect_stdout(buf):
			mod.print_merge_list(
			    [("libgl1-mesa-dri", "25.0.7-2+deb13u1", "25.0.7-2",
			      46284, "ebuild", ""),
			     ("libglx-mesa0", "25.0.7-2+deb13u1", "25.0.7-2",
			      143360, "ebuild", "")], True)
		for line in buf.getvalue().splitlines()[:2]:
			self.assertIn(line, readme,
			              "the README's session example is not what "
			              "print_merge_list produces any more")

	def test_the_readme_install_example_matches_the_merge_list_format(self):
		readme = self.read("README.md")
		mod = load()
		buf = io.StringIO()
		with contextlib.redirect_stdout(buf):
			mod.print_merge_list([("sl", "5.02-1+b1", None, 13210,
			                       "ebuild", "")], True)
		lines = [l for l in buf.getvalue().splitlines() if l.strip()]
		for line in lines:
			self.assertIn(line, readme,
			              f"README no longer shows what -pv prints: {line!r}")

	def test_every_command_the_readme_advertises_is_accepted(self):
		"""The README opens with a table of verbs, and nothing checked it.

        The man page and --help are cross-checked in both directions two
        tests above, precisely because an option can be renamed in one place
        and left behind in another -- and the README is the copy a user
        meets first. A row advertising a flag the parser rejects is worse
        than an undocumented one: it is an instruction that fails.

        Only parsing is exercised. `pick_backend` is stubbed, so nothing
        here reaches apt, dpkg or the network."""
		readme = self.read("README.md")
		rows = re.findall(r"^\| `emerge ([^`]+)`", readme, re.M)
		self.assertGreater(len(rows), 5,
		                   "the verb table scrape found almost nothing; it "
		                   "would pass for a README that lost its table")
		mod = load()

		class Reached(Exception):
			pass

		mod.pick_backend = lambda flag, pretend=False: (_ for _ in ()).throw(
		    Reached())
		for row in rows:
			# The table writes placeholders; a parser needs real words.
			argv = [{"<pkg>": "nano", "<regex>": "^sl$"}.get(a, a)
			        for a in row.split()]
			with self.subTest(command=f"emerge {row}"):
				try:
					with contextlib.redirect_stdout(io.StringIO()):
						mod.main(argv)
				except Reached:
					pass          # parsed, and stopped before doing anything
				except SystemExit as exit:
					self.fail(f"the README advertises `emerge {row}` and the "
					          f"parser rejected it (exit {exit.code})")

	def test_the_installed_paths_are_system_ones(self):
		"""A .deb may not install into /usr/local; that belongs to the
        local admin, and dpkg must not own anything there."""
		makefile = self.read("Makefile")
		self.assertNotIn("/usr/local", makefile)

	def test_the_script_is_installed_executable(self):
		makefile = self.read("Makefile")
		self.assertRegex(makefile,
		                 r"INSTALL_PROGRAM\s*=\s*\$\(INSTALL\) -m 755")
		self.assertIn("$(INSTALL_PROGRAM) emerge", makefile)

	def test_debian_rules_is_executable(self):
		path = os.path.join(HERE, "debian", "rules")
		if not os.path.exists(path):
			self.skipTest("debian/rules is not present in this tree")
		self.assertTrue(os.access(path, os.X_OK),
		                "dpkg-buildpackage cannot run a non-executable rules")

	def test_the_man_page_names_the_licence_the_script_reports(self):
		self.assertIn(em.LICENCE, self.read("emerge.1"))

	def test_the_author_is_the_same_everywhere(self):
		for name in ("emerge.1", "debian/control", "debian/copyright"):
			with self.subTest(file=name):
				self.assertIn(em.AUTHOR, self.read(name))


class TestStyleGate(unittest.TestCase):
	"""The gate reports what it checked, and it is the file list that goes
    wrong -- not the rules.

    Two things are pinned here. That the gate's scope still covers what this
    project writes: `emerge` has no suffix, being a command rather than a
    module, and was invisible to a version of the gate that selected on
    suffix alone -- it reported the documentation clean and never opened the
    program. And that a config it cannot apply stops the run, because the
    alternative is falling back to defaults and checking a DIFFERENT set of
    files successfully, which is indistinguishable from a clean tree."""

	GATE = os.path.join(HERE, "tools", "style_gate.py")

	# Everything this project writes itself in a language whose indentation
	# is ours to govern. A file may leave this list when it leaves the tree,
	# not because the gate stopped noticing it.
	GOVERNED = ("emerge", "Makefile", "debian/rules",
	            "test_emerge.py", "test_integration.py")

	def setUp(self):
		if not os.path.exists(self.GATE):
			self.skipTest("tools/style_gate.py is not present in this tree")

	def gate(self, *args, root=None):
		return subprocess.run([sys.executable, self.GATE, *args,
		                       "--root", root or HERE],
		                      capture_output=True, text=True, timeout=120)

	def rooted(self, config):
		"""A throwaway tree holding one source file and the given config.

        `config` is written as bytes so a test can hand over something that
        is not valid TOML at all, and None means 'make it a directory' --
        the case where a config exists but cannot be opened."""
		root = _scratch()
		os.makedirs(root)
		with open(os.path.join(root, "sample.py"), "w") as f:
			f.write("def f():\n\tpass\n")
		path = os.path.join(root, ".style-gate.toml")
		if config is None:
			os.makedirs(path)
		else:
			with open(path, "wb") as f:
				f.write(config)
		return root

	@unittest.skipUnless(sys.version_info >= (3, 11),
	                     "the gate needs tomllib to read .style-gate.toml, "
	                     "and refuses to run at all without it -- which is "
	                     "the behaviour the other tests here pin")
	def test_the_gate_looks_at_the_file_that_ships(self):
		proc = self.gate("list")
		self.assertEqual(proc.returncode, 0, proc.stderr)
		listed = {line.split("\t")[0] for line in proc.stdout.splitlines()
		          if "\t" in line}
		missing = sorted(set(self.GOVERNED) - listed)
		self.assertEqual(missing, [],
		                 f"the style gate does not look at {missing}; check "
		                 f"indent_names in .style-gate.toml")

	def test_a_config_it_cannot_open_is_refused_not_ignored(self):
		"""A .style-gate.toml that is a directory -- or a broken symlink --
        answers False to is_file(), which once read as 'no config here' and
        fell back to the defaults. Both mean somebody intended a config.

        This case needs no tomllib, so it holds on every interpreter the
        project supports: the refusal happens before the parse."""
		proc = self.gate("check", root=self.rooted(None))
		self.assertEqual(proc.returncode, 2,
		                 f"expected a refusal, got {proc.returncode}: "
		                 f"{proc.stdout}{proc.stderr}")
		self.assertIn(".style-gate.toml", proc.stderr)

	@unittest.skipUnless(sys.version_info >= (3, 11), "needs tomllib")
	def test_a_config_that_is_not_valid_toml_is_refused(self):
		proc = self.gate("check", root=self.rooted(b'indent_names = ["x"\n'))
		self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
		self.assertIn("not valid TOML", proc.stderr)

	@unittest.skipUnless(sys.version_info >= (3, 11), "needs tomllib")
	def test_a_wrong_typed_value_is_refused_rather_than_half_applied(self):
		"""The quiet one, and the reason the type check exists.

        `indent_names = "emerge"` -- quotes where brackets belong -- is
        valid TOML and was accepted. A set() of a string is a set of its
        characters, so the name matched nothing, the scope silently shrank,
        and the run passed. Measured at the time: a three-file list became
        one, exit 0, with no output but the count."""
		proc = self.gate("check", root=self.rooted(b'indent_names = "sample"\n'))
		self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
		self.assertIn("indent_names", proc.stderr)
		self.assertIn("list of strings", proc.stderr)

	def test_the_local_copy_is_verbatim_apart_from_its_provenance_header(self):
		"""The gate is one tool spread across the projects, so the copy here
        must not drift from the source. It carries two added lines saying
        where it came from, and they sit BELOW the shebang: above it the
        kernel does not see `#!` at all, and the file -- which is mode 755 --
        gets run by the shell instead, where it hangs on the first unbalanced
        quote in the docstring rather than failing."""
		with open(self.GATE, encoding="utf-8") as f:
			lines = f.read().splitlines()
		self.assertTrue(lines[0].startswith("#!"),
		                "the shebang must be the first line or the kernel "
		                "will not honour it")
		self.assertIn("Copied from", lines[1] + lines[2])


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


class TestInfo(unittest.TestCase):
	"""`--info` is what a bug report should open with, so every line has to
    be a fact rather than a hope.

    The rows were chosen from this program's own failures rather than from
    Portage's list, which is mostly compilers and USE flags: a foreign
    architecture the dpkg backend cannot represent, a missing `lzma` that
    changes which index `--sync` fetches, an absent `gpgv` so nothing
    verifies the archive, and a locale that used to stop every parser in the
    file from matching."""

	def setUp(self):
		self.mod = load()
		self.mod._find_session_leaders = lambda: []

	def info(self, *argv):
		buf = io.StringIO()
		with contextlib.redirect_stdout(buf):
			self.mod.main(list(argv))
		return buf.getvalue()

	def test_it_reports_what_decides_a_config_review(self):
		"""Config merging's failures are all quiet ones: a review that only
        ever offers a 2-way merge looks like a design choice rather than a
        missing ancestor, and the three things that decide it -- an empty
        archive, recovery switched off, an absent dpkg.log -- are invisible
        from outside. None of them was in here."""
		d = tempfile.mkdtemp(prefix="emerge-info-")
		self.addCleanup(shutil.rmtree, d, True)
		self.mod.load_conf = lambda: {**self.mod.DEFAULT_CONF,
		                              "archive-dir": d,
		                              "config-protect": d}
		self.mod.DPKG_LOG = os.path.join(d, "nosuch.log")
		out = self.info("--info")
		self.assertIn("Config archive", out)
		self.assertIn("2-way until it fills", out)
		self.assertIn("Ancestor recovery", out)
		self.assertIn("Config files pending", out)
		self.assertIn("empty or absent", out)

	def test_the_recovery_row_says_when_it_is_switched_off(self):
		d = tempfile.mkdtemp(prefix="emerge-info-")
		self.addCleanup(shutil.rmtree, d, True)
		self.mod.load_conf = lambda: {**self.mod.DEFAULT_CONF,
		                              "archive-dir": d, "config-protect": d,
		                              "recover-ancestor": "no"}
		self.assertIn("recover-ancestor=no", self.info("--info"))

	def test_it_names_this_program_s_own_version(self):
		"""The first question a bug report answers, and the one the header
        line above it cannot: that names the Portage dialect, which is the
        same string in every release of apt-emerge."""
		out = self.info("--info")
		self.assertIn(self.mod.APT_EMERGE_VERSION, out)
		self.assertIn("apt-emerge", out)

	def test_it_reports_the_things_that_have_caused_misdiagnoses(self):
		out = self.info("--info")
		for row in ("Architecture", "Locale", "gpgv", "lzma", "Sources",
		            "Distribution", "Graphical session"):
			with self.subTest(row=row):
				self.assertIn(row, out)

	def test_it_names_the_backend_whichever_order_the_flags_come_in(self):
		"""--info answers and returns from inside the argument loop, so a
        --backend= after it was not seen: the same command reported two
        different backends depending on which word came first."""
		self.assertIn("dpkg backend", self.info("--info", "--backend=dpkg"))
		self.assertIn("dpkg backend", self.info("--backend=dpkg", "--info"))
		self.assertIn("apt backend", self.info("--info", "--backend=apt"))

	def test_it_says_when_lzma_is_missing_rather_than_staying_quiet(self):
		"""A minimal Python without lzma silently changes which index
        --sync fetches, which is exactly the sort of thing a bug report
        needs to carry."""
		self.mod.lzma = None
		self.assertIn("absent", self.info("--info"))

	def test_the_multiarch_row_is_not_dead_code(self):
		"""It reads state that only installed_state() fills in, and --info
        had no reason to call it -- so the row could never appear. Written
        that way first."""
		self.mod.installed_state = lambda: self.mod.MULTIARCH_INSTANCES.update(
		    {"libfoo": ["amd64", "i386"]}) or {}
		self.assertIn("Multiarch", self.info("--info"))

	def test_it_changes_nothing(self):
		"""Like --version and --help. It runs as any user, and constructing
        a backend is what once created /var/lib/emerge-dpkg on a query."""
		def refuse(*a, **k):
			raise AssertionError("--info constructed a backend")
		self.mod.pick_backend = refuse
		self.mod.need_root = refuse
		self.info("--info")


class TestSearchPatterns(unittest.TestCase):
	"""A search term is a regex, and users mistype regexes.

    The two backends failed differently on the same input, which is why the
    check is in main() rather than in either of them. The dpkg backend
    compiles the pattern itself and raised `unterminated character set` as
    a traceback. The apt backend hands it to `apt-cache search`, which
    prints "E: Regex compilation error", exits 0 regardless, and so was
    reported as `[ Applications found : 0 ]` -- a wrong answer rather than
    a complaint, and the worse of the two, because nothing about it looks
    like a failure."""

	def setUp(self):
		self.mod = load()
		self.said = []
		self.mod.eerror = self.said.append

		class B:
			name = "apt"
			searched = []

			def search(_s, terms, desc):
				B.searched.append(list(terms))
		self.backend = B
		B.searched = []
		self.mod.pick_backend = lambda flag, pretend=False: B()

	def search(self, *terms):
		with contextlib.redirect_stdout(io.StringIO()):
			self.mod.main(["-s", *terms])

	def test_a_broken_pattern_is_refused_before_any_backend_sees_it(self):
		for bad in ("[", "a(", "*x", "(?P<"):
			with self.subTest(pattern=bad):
				self.said.clear()
				self.backend.searched = []
				with self.assertRaises(SystemExit) as exit:
					self.search(bad)
				self.assertEqual(exit.exception.code, 1)
				self.assertEqual(self.backend.searched, [],
				                 "the backend was asked to search anyway")
				self.assertTrue(any(bad in s for s in self.said),
				                f"the complaint should quote it: {self.said}")

	def test_it_says_where_the_pattern_went_wrong(self):
		"""Naming the position is the difference between a usable message
        and one that says only that something is wrong."""
		with self.assertRaises(SystemExit):
			self.search("a(")
		joined = " ".join(self.said)
		self.assertIn("position", joined)
		self.assertIn("quote it", joined,
		              "the usual cause is the shell, so say so")

	def test_an_ordinary_pattern_still_reaches_the_backend(self):
		self.search("^sl$")
		self.assertEqual(self.backend.searched, [["^sl$"]])

	def test_every_term_is_checked_not_just_the_first(self):
		with self.assertRaises(SystemExit):
			self.search("^sl$", "[")
		self.assertEqual(self.backend.searched, [])


class TestPortageAtoms(unittest.TestCase):
	"""The premise is that emerge's command line works here, so the spellings
    a Portage user's fingers produce should not fall through to apt and come
    back in apt's words.

    `emerge app-misc/sl` used to answer "Unable to locate package app-misc"
    -- naming a package nobody asked for, and leaking which tool was really
    being driven. `=sl-5.02-1+b1` answered "Unable to locate package" with no
    name in it at all."""

	def kept(self, atom):
		name, complaint = em.translate_atom(atom)
		self.assertIsNone(complaint, f"{atom} was rejected: {complaint}")
		return name

	def test_a_portage_category_is_dropped(self):
		"""Debian has no categories, so the part before the slash is noise
        rather than a name."""
		self.assertEqual(self.kept("app-misc/sl"), "sl")
		self.assertEqual(self.kept("virtual/editor"), "editor")
		self.assertEqual(self.kept("sys-apps/coreutils"), "coreutils")

	def test_an_ordinary_name_is_untouched(self):
		for atom in ("sl", "libsdl3-dev", "gcc-12", "python3.11", "@world",
		             "world", "@system"):
			with self.subTest(atom=atom):
				self.assertEqual(self.kept(atom), atom)

	def test_apts_own_exact_version_spelling_still_works(self):
		"""The one that must not regress: `sl=5.02-1+b1` is Debian's syntax,
        apt accepts it, and it contains the same `=` the Portage form does.
        Position is what tells them apart -- Portage's operator leads."""
		self.assertEqual(self.kept("sl=5.02-1+b1"), "sl=5.02-1+b1")

	def test_a_multiarch_qualifier_is_left_alone(self):
		"""`sl:i386` is Debian's own syntax and apt handles it. Portage's
        slot atom is spelled the same way, and Debian's meaning wins: this
        is not ours to reinterpret."""
		self.assertEqual(self.kept("sl:i386"), "sl:i386")

	def test_a_local_deb_is_a_path_and_not_an_atom(self):
		"""apt installs a .deb given by path, and every one of these has a
        slash in it. Category stripping had to be taught the difference
        first, or `emerge ./sl.deb` becomes `emerge sl.deb`."""
		for atom in ("./sl.deb", "../build/sl.deb", "/tmp/sl.deb", "sl.deb",
		             # The case that needs the guard. The others are caught
		             # anyway, because `.`, `..` and the empty string before
		             # a leading slash are not category-shaped -- but `pool`
		             # is, so without the guard this becomes `sl.deb` and
		             # apt is handed a file that is not there.
		             "pool/sl.deb"):
			with self.subTest(atom=atom):
				self.assertEqual(self.kept(atom), atom)

	def test_something_that_is_not_a_category_keeps_its_slash(self):
		"""Only a Portage-shaped category is dropped. Anything else with a
        slash is left for apt to reject in its own words, which is right --
        we do not know what it is."""
		self.assertEqual(self.kept("Foo/bar"), "Foo/bar")
		self.assertEqual(self.kept("a/b/c"), "a/b/c")

	def test_a_leading_version_operator_is_explained(self):
		for atom in ("=sl-5.02-1+b1", ">=sl-5.0", "<sl-6", "~sl-5.0"):
			with self.subTest(atom=atom):
				_, complaint = em.translate_atom(atom)
				self.assertIsNotNone(complaint, f"{atom} was accepted")
				joined = " ".join(complaint)
				self.assertIn(atom, joined, "the complaint should quote it")
				self.assertIn("sl=5.02-1+b1", joined,
				              "and name the spelling that works")

	def test_the_whole_command_stops_rather_than_running_a_partial_list(self):
		"""A rejected atom must not leave the others to be installed on
        their own -- the user asked for a set of things."""
		mod = load()
		said = []
		mod.eerror = said.append
		mod.pick_backend = lambda flag, pretend=False: self.fail(
		    "reached the backend with a bad atom in the list")
		with self.assertRaises(SystemExit) as exit:
			with contextlib.redirect_stdout(io.StringIO()):
				mod.main(["-p", "sl", "=sl-5.02"])
		self.assertEqual(exit.exception.code, 1)
		self.assertTrue(said)


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

		def stop(_flag, pretend=False):
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

	# -- flags reaching the thing that acts on them --------------------------
	#
	# Found by mutation rather than by reading: replacing every *read* of an
	# option in main() with False and running both suites leaves -v, -u, -D,
	# --no-dep-upgrade and --no-verify entirely unnoticed. Their behaviour is
	# tested thoroughly -- ndu_solve against brute force, sync's verification
	# against real gpgv, print_merge_list against the README -- but the line
	# in main() that carries the flag to any of them could be deleted and
	# nothing would fail.
	#
	# That is the shape of two bugs this project has already shipped: the
	# no_dep_upgrade branch of AptBackend.resolve() that did not return, so
	# the flag produced exactly the no-flag plan; and `emerge -uD @world foo`
	# dropping foo. Both were wiring, not behaviour.

	def test_update_and_deep_reach_the_resolver(self):
		self.assertEqual(self.resolved(["-uD", "bash"])["kw"],
		                 {"update": True, "deep": True,
		                  "no_dep_upgrade": False, "allow": set()})

	def test_neither_is_set_when_not_asked_for(self):
		"""The other half: a flag that is always on is as broken as one that
        never is, and a test that only checks the on case cannot tell."""
		kw = self.resolved(["bash"])["kw"]
		self.assertEqual((kw["update"], kw["deep"]), (False, False))

	def test_no_dep_upgrade_reaches_the_resolver(self):
		self.assertTrue(self.resolved(["--no-dep-upgrade", "bash"])["kw"]
		                ["no_dep_upgrade"])
		self.assertFalse(self.resolved(["bash"])["kw"]["no_dep_upgrade"])

	def test_no_verify_reaches_sync(self):
		"""`--no-verify` turns off the archive signature check, so the wire
        from the flag to `sync(verify=...)` is worth as much as the check
        itself."""
		seen = {}

		class B:
			name = "dpkg"

			def sync(_s, **kw):
				seen.update(kw)
		self.mod.pick_backend = lambda flag, pretend=False: B()
		with contextlib.redirect_stdout(io.StringIO()):
			self.mod.main(["--sync", "--no-verify"])
		self.assertEqual(seen, {"verify": False})
		seen.clear()
		with contextlib.redirect_stdout(io.StringIO()):
			self.mod.main(["--sync"])
		self.assertEqual(seen, {"verify": True},
		                 "the default must still verify")

	def test_verbose_reaches_the_merge_list(self):
		seen = []
		self.mod.print_merge_list = lambda merges, verbose: seen.append(verbose)

		class B:
			name = "apt"

			def resolve(_s, targets, **kw):
				return [("bash", "1.0", None, 0, "ebuild", "")]
		self.mod.pick_backend = lambda flag, pretend=False: B()
		with contextlib.redirect_stdout(io.StringIO()):
			self.mod.main(["-pv", "bash"])
			self.mod.main(["-p", "bash"])
		self.assertEqual(seen, [True, False])

	# -- one action per run --------------------------------------------------
	#
	# main() dispatches in a fixed order and returns, so a second action was
	# not rejected but silently skipped, and which one survived depended on
	# the order of the branches rather than on anything visible. The worst
	# shape was `emerge -C --depclean foo`: it ran depclean, dropped the
	# removal, and exited 0 -- a destructive action nobody asked to run on
	# its own, standing in for the one they did ask for.

	def test_two_actions_are_refused(self):
		for argv in (["--depclean", "-C", "bash"],
		             ["--deselect", "-C", "bash"],
		             ["-s", "tree", "--depclean"],
		             ["--sync", "--depclean"],
		             ["--dispatch-conf", "--depclean"],
		             ["--depclean", "-b", "bash"]):
			with self.subTest(argv=argv):
				self.assertRejected(argv)

	def test_the_refusal_names_both_of_them(self):
		"""Naming one, or neither, leaves the user to guess which pair of
        flags it objected to.

        eerror writes to stderr, which `parse` does not capture -- the
        first version of this test read an empty string and asserted
        against it."""
		said = []
		self.mod.eerror = said.append
		self.assertRejected(["--depclean", "-C", "bash"])
		joined = " ".join(said)
		self.assertIn("--depclean", joined)
		self.assertIn("--unmerge", joined)

	def test_flags_naming_the_same_action_do_not_conflict(self):
		"""-s and -S are both search; -b and -B are both the source-build
        form of an install. Grouping them is the difference between this
        check and one that refuses ordinary commands."""
		self.assertAccepted(["-s", "-S", "tree"])
		self.assertAccepted(["-b", "-B", "bash"])

	def test_a_single_action_is_still_accepted(self):
		"""The regression this guard could easily cause."""
		for argv in (["--depclean"], ["-C", "bash"], ["--deselect", "bash"],
		             ["-s", "tree"], ["--sync"], ["-b", "bash"],
		             ["bash"], ["-uD", "@world"]):
			with self.subTest(argv=argv):
				self.assertAccepted(argv)

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

	def resolved(self, argv):
		"""Run the parser through to the resolve call and report what the
        backend was actually asked for."""
		seen, reached = {}, self.Reached

		class B:
			name = "apt"

			def resolve(_s, targets, **kw):
				seen["targets"] = list(targets)
				seen["allow"] = set(kw.get("allow") or ())
				seen["kw"] = dict(kw)
				raise reached()

		self.mod.pick_backend = lambda flag, pretend=False: B()
		with self.assertRaises(self.Reached):
			with contextlib.redirect_stdout(io.StringIO()):
				self.mod.main(argv)
		return seen

	def test_with_does_not_lose_the_target_to_an_earlier_long_option(self):
		"""The guard used to sit below the specifically-recognised long
        options, so each of them claimed its token first and `--with` then
        ate the *package* instead:

            $ emerge --no-dep-upgrade --with --backtrack=5 libsdl3-dev
             * no targets given.
        """
		self.assertRejected(["--no-dep-upgrade", "--with",
		                     "--backtrack=5", "libsdl3-dev"])

	def test_every_recognised_long_option_is_refused_after_bare_with(self):
		for opt in ("--sync", "--depclean", "--no-verify", "--no-dep-upgrade",
		            "--dispatch-conf", "--backtrack=5", "--backend=dpkg",
		            "--with=libz", "--help", "-a"):
			with self.subTest(opt=opt):
				self.errors = []
				self.mod.eerror = self.errors.append
				self.assertRejected(["--no-dep-upgrade", "--with", opt, "nano"])

	def test_the_allow_set_holds_the_packages_and_the_target_survives(self):
		got = self.resolved(["--no-dep-upgrade", "--with", "libgbm1,libz1",
		                     "libsdl3-dev"])
		self.assertEqual(got["allow"], {"libgbm1", "libz1"})
		self.assertEqual(got["targets"], ["libsdl3-dev"])

	def test_with_still_works_when_it_follows_another_option(self):
		got = self.resolved(["--backtrack=5", "--no-dep-upgrade",
		                     "--with", "libgbm1", "libsdl3-dev"])
		self.assertEqual(got["allow"], {"libgbm1"})
		self.assertEqual(got["targets"], ["libsdl3-dev"])

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

	def test_backtrack_is_accepted_and_parsed(self):
		original = em.BACKTRACK
		try:
			self.assertAccepted(["--backtrack=25", "nano"])
			self.assertEqual(self.mod.BACKTRACK, 25)
		finally:
			em.BACKTRACK = original

	def test_backtrack_zero_is_valid(self):
		self.assertAccepted(["--backtrack=0", "nano"])
		self.assertEqual(self.mod.BACKTRACK, 0)

	def test_backtrack_rejects_a_non_number(self):
		self.assertRejected(["--backtrack=lots", "nano"])

	def test_bundled_short_flags_are_accepted(self):
		self.assertAccepted(["-pv1", "nano"])

	def test_ignored_compatibility_flags_are_accepted(self):
		self.assertAccepted(["-Nqt", "nano"])

	def test_set_names_are_accepted(self):
		for name in ("world", "system", "@world", "@system", "@selected"):
			with self.subTest(name=name):
				self.setUp()
				self.assertAccepted(["-u", name])

	# -- version ------------------------------------------------------------

	def version_output(self, argv):
		self.mod.pick_backend = lambda _f: types.SimpleNamespace(name="apt")
		buf = io.StringIO()
		with contextlib.redirect_stdout(buf):
			self.mod.main(argv)
		return buf.getvalue()

	def test_dash_capital_v_is_accepted(self):
		"""Portage spells it -V; -v is verbose and must stay that way."""
		self.assertIn("Portage", self.version_output(["-V"]))

	def test_long_and_short_version_agree(self):
		self.assertEqual(self.version_output(["-V"]),
		                 self.version_output(["--version"]))

	def test_version_names_the_author(self):
		out = self.version_output(["-V"])
		self.assertIn(em.AUTHOR, out)
		self.assertIn("@", em.AUTHOR, "the author line should carry an email")

	def test_version_states_copyright_and_licence(self):
		out = self.version_output(["-V"])
		self.assertIn(em.COPYRIGHT, out)
		self.assertIn(em.LICENCE, out)
		self.assertIn("NO WARRANTY", out)

	def test_version_reports_the_backend_and_interpreter(self):
		out = self.version_output(["-V"])
		self.assertIn("apt backend", out)
		self.assertIn(sys.version.split()[0], out)

	def test_the_file_header_and_version_output_agree(self):
		"""The notice at the top of emerge is a comment and cannot share the
        constant, so check the two have not drifted apart."""
		with open(os.path.join(HERE, "emerge")) as f:
			header = "".join(f.readline() for _ in range(25))
		self.assertIn(em.AUTHOR, header)
		self.assertIn(em.COPYRIGHT, header)
		self.assertIn(em.LICENCE, header)

	def test_lowercase_v_is_still_verbose(self):
		self.assertAccepted(["-v", "nano"])

	def test_help_returns_without_building_a_backend(self):
		out = self.parse(["--help"])
		self.assertIn("--oneshot", out)

	def test_no_arguments_prints_help(self):
		self.assertIn("--oneshot", self.parse([]))


class TestLoadConf(unittest.TestCase):
	"""dispatch-conf.conf uses Gentoo's key names, so an operator can bring
    habits across. Unknown keys must be ignored rather than crash."""

	def setUp(self):
		self.mod = load()
		self.dir = tempfile.mkdtemp(prefix="emerge-conf-")
		self.addCleanup(shutil.rmtree, self.dir, True)

	def parse(self, text, alt=None):
		main = os.path.join(self.dir, "conf")
		with open(main, "w") as f:
			f.write(text)
		self.mod.CONF_FILE = main
		self.mod.CONF_FILE_ALT = alt or os.path.join(self.dir, "absent")
		return self.mod.load_conf()

	def test_defaults_when_no_file_exists(self):
		self.mod.CONF_FILE = os.path.join(self.dir, "nope")
		self.mod.CONF_FILE_ALT = os.path.join(self.dir, "nope2")
		self.assertEqual(self.mod.load_conf(), self.mod.DEFAULT_CONF)

	def test_simple_assignment(self):
		self.assertEqual(self.parse("automerge=no\n")["automerge"], "no")

	def test_surrounding_whitespace_is_trimmed(self):
		self.assertEqual(self.parse("  automerge  =  no  \n")["automerge"],
		                 "no")

	def test_quotes_are_stripped(self):
		c = self.parse('mergetool="meld {mine} {theirs}"\n')
		self.assertEqual(c["mergetool"], "meld {mine} {theirs}")

	def test_single_quotes_too(self):
		self.assertEqual(self.parse("frozen-files='/etc/a /etc/b'\n")
		                 ["frozen-files"], "/etc/a /etc/b")

	def test_comments_and_blank_lines_are_ignored(self):
		c = self.parse("# automerge=yes\n\n   \nautomerge=no\n")
		self.assertEqual(c["automerge"], "no")

	def test_unknown_keys_are_ignored(self):
		c = self.parse("nonsense=1\nautomerge=no\n")
		self.assertNotIn("nonsense", c)
		self.assertEqual(c["automerge"], "no")

	def test_lines_without_an_equals_are_ignored(self):
		self.assertEqual(self.parse("garbage\nautomerge=no\n")["automerge"],
		                 "no")

	def test_conf_yes_spellings(self):
		for text in ("yes", "YES", "true", "1", "on"):
			with self.subTest(text=text):
				self.assertTrue(self.mod.conf_yes({"k": text}, "k"))
		for text in ("no", "false", "0", "off", "", "maybe"):
			with self.subTest(text=text):
				self.assertFalse(self.mod.conf_yes({"k": text}, "k"))

	def test_conf_yes_defaults_to_no_for_a_missing_key(self):
		self.assertFalse(self.mod.conf_yes({}, "absent"))


class TestPkgConffiles(unittest.TestCase):
	"""Parses dpkg's Conffiles field, which is what tells the archiver which
    files it is allowed to touch."""

	def setUp(self):
		self.mod = load()

	def one(self, pkg):
		"""One package's conffiles, through the batched reader the program
        itself uses. `pkg_conffiles` was a single-name wrapper for exactly
        this, in the shipped file, called by nothing but these four tests."""
		return self.mod.conffiles_of([pkg]).get(pkg, [])

	def stub(self, stdout):
		"""Fake dpkg-query's real batched layout, verified against dpkg 1.22:
        `-f=${Package}:${Conffiles}\\n` puts the first conffile on the
        package's own line and indents the rest beneath it."""
		class R:
			pass
		R.stdout, R.stderr, R.returncode = stdout, "", 0
		self.calls = []
		def capture(cmd, env=None):
			self.calls.append(cmd)
			return R
		self.mod.capture = capture

	def test_reads_the_paths(self):
		self.stub("p: /etc/foo.conf 0123abc\n /etc/bar.conf 4567def\n")
		self.assertEqual(self.one("p"),
		                 ["/etc/foo.conf", "/etc/bar.conf"])

	def test_obsolete_entries_are_skipped(self):
		"""dpkg keeps removed conffiles listed as obsolete; archiving one
        would resurrect a file the package no longer ships."""
		self.stub("p: /etc/keep.conf 0123abc\n"
		          " /etc/gone.conf 4567def obsolete\n")
		self.assertEqual(self.one("p"), ["/etc/keep.conf"])

	def test_blank_and_relative_lines_are_ignored(self):
		self.stub("p:\n\n not-a-path 0123\n /etc/ok.conf 4567\n")
		self.assertEqual(self.one("p"), ["/etc/ok.conf"])

	def test_package_with_no_conffiles(self):
		self.stub("p:\n")
		self.assertEqual(self.one("p"), [])

	def test_a_batch_keeps_each_package_to_its_own_files(self):
		"""The whole point of batching: one call, but the paths must not
        smear across the package boundary."""
		self.stub("a: /etc/a1.conf 01\n /etc/a2.conf 02\n"
		          "b:\n"
		          "c: /etc/c1.conf 03\n")
		self.assertEqual(self.mod.conffiles_of(["a", "b", "c"]),
		                 {"a": ["/etc/a1.conf", "/etc/a2.conf"],
		                  "b": [],
		                  "c": ["/etc/c1.conf"]})

	def test_many_packages_are_one_call_not_one_each(self):
		"""The regression this replaced: a fork per package cost ~50ms, so a
        1500-package upgrade spent over a minute starting processes."""
		self.stub("".join(f"p{i}:\n" for i in range(300)))
		self.mod.conffiles_of([f"p{i}" for i in range(300)], chunk=500)
		self.assertEqual(len(self.calls), 1)
		self.assertEqual(self.calls[0][:3], ["dpkg-query", "-W",
		                                     "-f=${Package}:${Conffiles}\n"])

	def test_a_stray_line_does_not_hijack_the_current_package(self):
		"""Only "<package>:" starts a new package. Anything else -- a blank
        line, a stray note -- must be ignored rather than become the current
        one, or every conffile after it is filed under the wrong package."""
		self.stub("a: /etc/a1.conf 01\n"
		          "\n"
		          "unexpected chatter\n"
		          " /etc/a2.conf 02\n")
		self.assertEqual(self.mod.conffiles_of(["a"]),
		                 {"a": ["/etc/a1.conf", "/etc/a2.conf"]})

	def test_the_batch_is_chunked_so_the_argument_list_cannot_overflow(self):
		self.stub("")
		self.mod.conffiles_of([f"p{i}" for i in range(1100)], chunk=500)
		self.assertEqual(len(self.calls), 3)


class TestArchiveSettled(unittest.TestCase):
	"""The half of config merging that decides what the *next* upgrade will
    treat as the common ancestor.

    Its timing is the load-bearing part and has been wrong before: archiving
    the incoming version instead of the settled one makes new == ancestor,
    so the 3-way merge sees no incoming change and every update is silently
    discarded. These pin the rule -- archive what dpkg left in place, never
    what it parked."""

	def setUp(self):
		self.mod = load()
		self.dir = tempfile.mkdtemp(prefix="emerge-arch-")
		self.addCleanup(shutil.rmtree, self.dir, True)
		self.etc = os.path.join(self.dir, "etc")
		os.makedirs(self.etc)
		self.conf = dict(self.mod.DEFAULT_CONF)
		self.conf["archive-dir"] = os.path.join(self.dir, "archive")
		self.conffiles = []
		# archive_settled asks for every package's conffiles in one batched
		# call; the files under test all belong to the first named package
		self.mod.conffiles_of = lambda pkgs, **kw: {
		    p: (list(self.conffiles) if i == 0 else [])
		    for i, p in enumerate(pkgs)}

	def conffile(self, name, content, parked=None,
	             suffix=".dpkg-dist"):
		path = os.path.join(self.etc, name)
		with open(path, "w") as f:
			f.write(content)
		if parked is not None:
			with open(path + suffix, "w") as f:
				f.write(parked)
		self.conffiles.append(path)
		return path

	def archived(self, path):
		with open(self.mod.archive_path(self.conf, path)) as f:
			return f.read()

	def has_archive(self, path):
		return os.path.exists(self.mod.archive_path(self.conf, path))

	def test_one_unarchivable_file_does_not_abandon_the_rest(self):
		"""This runs after a successful install, and again from the failure
        path where its whole job is to record what landed and announce
        parked files before bailing out. An exception here replaced the
        error the user needed with a traceback and abandoned every conffile
        after the failing one -- each of which then has no ancestor, so the
        next upgrade reviews it by hand instead of merging it.

        A full /var during an install is the ordinary way in, so the failure
        is *faked* rather than arranged. The first version of this test used
        `chmod 000` on the source file, which fails for an ordinary user and
        not for root -- so it passed here and failed in the container, where
        everything runs as root and a 000 file is readable. Exactly the
        machine-dependence this project has been bitten by before, and the
        reason the container job exists."""
		first = self.conffile("a.conf", "one\n")
		bad = self.conffile("unreadable.conf", "two\n")
		last = self.conffile("c.conf", "three\n")
		real_store = self.mod._store_ancestor

		def store(conf, target, source):
			if target == bad:
				raise OSError(28, "No space left on device")
			return real_store(conf, target, source)
		self.mod._store_ancestor = store
		said = []
		self.mod.ewarn = said.append

		n = self.mod.archive_settled(self.conf, ["pkg"])

		self.assertEqual(n, 2, "the count should exclude what it could not do")
		self.assertTrue(self.has_archive(first))
		self.assertTrue(self.has_archive(last),
		                "the file after the failing one was never archived")
		self.assertFalse(self.has_archive(bad))
		self.assertTrue(any("unreadable.conf" in s for s in said),
		                f"the failure was swallowed silently: {said}")

	def test_an_unmodified_conffile_becomes_the_ancestor(self):
		"""dpkg installed it without parking, so what is on disk is exactly
        what the package ships -- the right thing to remember."""
		p = self.conffile("plain.conf", "shipped v2\n")
		self.assertEqual(self.mod.archive_settled(self.conf, ["pkg"]), 1)
		self.assertEqual(self.archived(p), "shipped v2\n")

	def test_a_parked_conffile_is_left_alone(self):
		"""This is the one that matters. You edited it, so dpkg parked the
        new version and your edits are still on disk. Archiving the file as
        it stands would record *your* version as what the package shipped,
        and the next merge would have no idea anything changed."""
		p = self.conffile("edited.conf", "my edits\n", parked="shipped v2\n")
		self.assertEqual(self.mod.archive_settled(self.conf, ["pkg"]), 0)
		self.assertFalse(self.has_archive(p))

	def test_an_older_ancestor_survives_a_parked_update(self):
		p = self.conffile("edited.conf", "my edits\n", parked="shipped v2\n")
		dest = self.mod.archive_path(self.conf, p)
		os.makedirs(os.path.dirname(dest), exist_ok=True)
		with open(dest, "w") as f:
			f.write("shipped v1\n")
		self.mod.archive_settled(self.conf, ["pkg"])
		self.assertEqual(self.archived(p), "shipped v1\n",
		                 "the previously shipped version is the ancestor")

	def test_every_parked_suffix_counts(self):
		for suffix in (".dpkg-dist", ".dpkg-new", ".ucf-dist", ".ucf-new"):
			with self.subTest(suffix=suffix):
				self.conffiles = []
				p = self.conffile(f"c{suffix.replace('.', '')}.conf", "mine\n",
				                  parked="theirs\n", suffix=suffix)
				self.assertEqual(self.mod.archive_settled(self.conf, ["pkg"]),
				                 0)
				self.assertFalse(self.has_archive(p))

	def test_a_missing_conffile_is_skipped(self):
		self.conffiles.append(os.path.join(self.etc, "never-existed.conf"))
		self.assertEqual(self.mod.archive_settled(self.conf, ["pkg"]), 0)

	def test_mode_is_preserved_in_the_archive(self):
		p = self.conffile("secret.conf", "token\n")
		os.chmod(p, 0o600)
		self.mod.archive_settled(self.conf, ["pkg"])
		mode = os.stat(self.mod.archive_path(self.conf, p)).st_mode & 0o777
		self.assertEqual(mode, 0o600)

	def test_a_later_run_refreshes_the_ancestor(self):
		p = self.conffile("plain.conf", "shipped v2\n")
		self.mod.archive_settled(self.conf, ["pkg"])
		with open(p, "w") as f:
			f.write("shipped v3\n")
		self.mod.archive_settled(self.conf, ["pkg"])
		self.assertEqual(self.archived(p), "shipped v3\n")


class TestAncestorFor(unittest.TestCase):
	"""Where the common ancestor comes from, in order of preference."""

	def setUp(self):
		self.mod = load()
		self.dir = tempfile.mkdtemp(prefix="emerge-anc-")
		self.addCleanup(shutil.rmtree, self.dir, True)
		self.conf = dict(self.mod.DEFAULT_CONF)
		self.conf["archive-dir"] = os.path.join(self.dir, "archive")
		self.ucf = os.path.join(self.dir, "ucf")
		os.makedirs(self.ucf)
		self.mod.UCF_CACHE = self.ucf
		self.target = "/etc/thing.conf"

	def put_archive(self, text):
		dest = self.mod.archive_path(self.conf, self.target)
		os.makedirs(os.path.dirname(dest), exist_ok=True)
		with open(dest, "w") as f:
			f.write(text)

	def put_ucf(self, text):
		# ucf mangles the path into a single filename
		with open(os.path.join(self.ucf, self.target.replace("/", ":")),
		          "w") as f:
			f.write(text)

	def test_our_archive_is_preferred(self):
		self.put_archive("ours\n")
		self.put_ucf("ucfs\n")
		lines, src = self.mod.ancestor_for(self.conf, self.target)
		self.assertEqual(lines, ["ours\n"])
		self.assertEqual(src, "archive")

	def test_ucf_cache_is_the_fallback(self):
		"""ucf keeps the previously shipped version for the files it
        manages, which is the same thing our archive holds."""
		self.put_ucf("ucfs\n")
		lines, src = self.mod.ancestor_for(self.conf, self.target)
		self.assertEqual(lines, ["ucfs\n"])
		self.assertEqual(src, "ucf cache")

	def test_no_ancestor_anywhere(self):
		"""First upgrade after installing emerge: 2-way review only."""
		self.assertEqual(self.mod.ancestor_for(self.conf, self.target),
		                 (None, None))


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
		# Off here, and covered by TestAncestorRecovery instead. Left on,
		# every one of these tests would fork dpkg-query and read the host's
		# /var/log/dpkg.log -- which is slow (measured: 0.2s to 4.2s for
		# this class) and, worse, makes the result depend on what the
		# machine running the suite happens to have installed.
		self.conf["recover-ancestor"] = "no"
		self.mod.load_conf = lambda: self.conf
		self.mod.need_root = lambda: None
		self.mod.color_diff = lambda *a, **k: None
		self.answers = []
		self.mod.input = lambda prompt="": self.answers.pop(0)

	def test_a_file_that_cannot_be_retired_does_not_end_the_review(self):
		"""`_accept` writes the merged file into /etc and *then* retires the
        parked copy, so a failure there used to abort with the change
        already applied, the parked file still in place, every later file
        unreviewed and no summary of what had been decided. That is the
        same shape a mergetool template with no placeholders once produced,
        which is why that one is guarded.

        Reproduced with a full disk rather than permissions, because this
        runs as root and root can write where an ordinary user cannot."""
		first = self.park("first.conf", "old\n", "new\n", ancestor="old\n")
		second = self.park("second.conf", "old\n", "new\n", ancestor="old\n")
		real = self.mod._store_ancestor

		def store(conf, target, source):
			if source.endswith(".dpkg-dist"):     # the promotion _retire does
				raise OSError(28, "No space left on device")
			return real(conf, target, source)
		self.mod._store_ancestor = store
		said = []
		self.mod.ewarn = said.append

		out = self.dispatch()          # both auto-apply; no answers needed

		self.assertEqual(sum("could not retire" in s for s in said), 2,
		                 f"the review stopped at the first failure: {said}")
		for target in (first, second):
			with self.subTest(target=os.path.basename(target)):
				self.assertEqual(self.content(target), "new\n",
				                 "the update was applied, as it was before")
				self.assertTrue(self.parked_exists(target),
				                "a file that could not be retired must stay "
				                "parked, so it comes back next run")

	def test_a_file_left_parked_resolves_itself_next_run(self):
		"""The claim that makes tolerating the failure safe: the file comes
        back, and the second time it needs no decision because what is on
        disk already matches what was parked."""
		target = self.park("x.conf", "new\n", "new\n", ancestor="old\n")
		self.dispatch()
		self.assertFalse(self.parked_exists(target),
		                 "an identical parked file should retire itself")

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

	# -- the mergetool branch ------------------------------------------------

	def errors(self):
		seen = []
		self.mod.eerror = seen.append
		return seen

	def test_a_mergetool_that_writes_nothing_is_not_taken_as_a_result(self):
		"""The output file is seeded with your current version so the tool
        has something to edit. A tool that exits 0 without touching it would
        otherwise have that seed handed straight back as a merge result --
        silently "merging" to exactly what you already had, and retiring the
        update as though you had considered it."""
		errs = self.errors()
		self.conf["mergetool"] = "true {output}"
		t = self.park("app.conf", "mine\n", "theirs\n")
		self.dispatch("5", "s")
		self.assertEqual(self.content(t), "mine\n")
		self.assertTrue(self.parked_exists(t),
		                "the update must still be pending review")
		self.assertTrue(any("without changing the output" in e for e in errs),
		                errs)

	def test_a_real_mergetool_result_is_applied(self):
		self.conf["mergetool"] = "cp {theirs} {output}"
		t = self.park("app.conf", "mine\n", "theirs\n")
		self.dispatch("5")
		self.assertEqual(self.content(t), "theirs\n")
		self.assertFalse(self.parked_exists(t))
		self.assertEqual(self.archived(t), "theirs\n")

	def test_a_template_with_no_placeholders_is_refused_not_a_crash(self):
		"""`mergetool=meld` -- no placeholders at all -- made
        "meld" % (a, b, c) a TypeError, which took dispatch-conf down in the
        middle of a review, with earlier files already retired and no
        summary of what had been decided."""
		errs = self.errors()
		# A program that certainly exists, because the not-installed check
		# runs first and would otherwise answer instead of the guard under
		# test. sys.executable rather than a named tool: `meld` skipped the
		# guard on any box without meld, and `sdiff` did the same on any
		# host without diffutils -- caught by running the unit half on
		# Alpine, where neither exists.
		self.conf["mergetool"] = sys.executable
		t = self.park("app.conf", "mine\n", "theirs\n")
		self.dispatch("5", "s")                    # must not raise
		self.assertEqual(self.content(t), "mine\n")
		self.assertTrue(self.parked_exists(t))
		self.assertTrue(any("cannot use the configured mergetool" in e
		                    for e in errs), errs)

	def test_a_positional_template_of_the_wrong_arity_is_refused(self):
		errs = self.errors()
		self.conf["merge"] = f"{sys.executable} '%s' '%s'"   # two, not three
		t = self.park("app.conf", "mine\n", "theirs\n")
		self.dispatch("5", "s")
		self.assertTrue(self.parked_exists(t))
		self.assertTrue(any("cannot use the configured mergetool" in e
		                    for e in errs), errs)

	def test_an_unknown_named_placeholder_is_refused(self):
		errs = self.errors()
		self.conf["mergetool"] = f"{sys.executable} {{mine}} {{nosuchkey}}"
		t = self.park("app.conf", "mine\n", "theirs\n")
		self.dispatch("5", "s")
		self.assertTrue(self.parked_exists(t))
		self.assertTrue(any("cannot use the configured mergetool" in e
		                    for e in errs), errs)

	def test_a_mergetool_that_reports_trouble_is_reported(self):
		errs = self.errors()
		self.conf["mergetool"] = "sh -c 'exit 2' -- {output}"
		t = self.park("app.conf", "mine\n", "theirs\n")
		self.dispatch("5", "s")
		self.assertEqual(self.content(t), "mine\n")
		self.assertTrue(any("did not produce a result" in e for e in errs),
		                errs)

	def test_a_merge_that_exits_1_is_still_a_merge(self):
		"""sdiff is from the diff family, where 1 means "the files differed"
        -- which they always did, that being why the file is under review.
        Requiring 0 threw away every successful sdiff merge and reported it
        as no result, with the merged text already written."""
		self.conf["mergetool"] = "sh -c 'cp \"$1\" \"$2\"; exit 1' -- " \
		                         "{theirs} {output}"
		t = self.park("app.conf", "mine\n", "theirs\n")
		self.dispatch("5")
		self.assertEqual(self.content(t), "theirs\n")
		self.assertFalse(self.parked_exists(t))

	def test_no_mergetool_configured_says_so_rather_than_running_nothing(self):
		errs = self.errors()
		self.conf["mergetool"] = self.conf["merge"] = ""
		t = self.park("app.conf", "mine\n", "theirs\n")
		self.dispatch("5", "s")
		self.assertTrue(any("no mergetool configured" in e for e in errs), errs)
		self.assertTrue(self.parked_exists(t))

	def test_a_recovered_ancestor_turns_the_review_three_way(self):
		"""The whole point of recovery, and nothing covered it: every test
        called recover_ancestors directly, so the path from what it
        archives to what the review merges against was never walked. This
        program has shipped two bugs of exactly that shape -- a helper
        nothing wires up -- which is why the wiring gets its own test.

        Two-way would conflict over the whole difference; three-way takes
        each side's change and asks nothing."""
		self.conf["recover-ancestor"] = "yes"
		target = self.park("app.conf",
		                   "mine = yes\nshared = 1\nkeep = old\n",
		                   "mine = no\nshared = 1\nkeep = new\n")
		# What recovery would have archived: the version both sides began
		# from. Only `keep` moved on their side, only `mine` on ours.
		def fake_recover(conf, targets, verifier=None, deadline=None):
			for t in targets:
				a = self.mod.archive_path(conf, t)
				os.makedirs(os.path.dirname(a), exist_ok=True)
				with open(a, "w") as f:
					f.write("mine = no\nshared = 1\nkeep = old\n")
			return len(targets)
		self.mod.recover_ancestors = fake_recover
		out = self.dispatch()
		self.assertIn("auto-merged", out)
		self.assertEqual(self.content(target),
		                 "mine = yes\nshared = 1\nkeep = new\n")
		self.assertFalse(self.parked_exists(target))

	def test_nothing_is_fetched_for_a_file_the_review_never_asks_about(self):
		"""A frozen file and one whose update is byte-identical are both
        resolved without ever consulting an ancestor. Fetching ten
        megabytes to answer a question nobody asks is how a default gets
        turned off."""
		asked = []
		self.mod.recover_ancestors = lambda conf, targets, verifier=None: \
		    asked.extend(targets)
		self.conf["recover-ancestor"] = "yes"
		self.conf["frozen-files"] = os.path.join(self.etc, "frozen.conf")
		self.park("frozen.conf", "mine\n", "theirs\n")
		self.park("same.conf", "same\n", "same\n")
		self.park("real.conf", "mine\n", "theirs\n")
		self.dispatch("s")
		self.assertEqual(asked, [os.path.join(self.etc, "real.conf")])

	def test_the_menu_names_the_tool_option_5_will_launch(self):
		"""Option 5 used to read "launch mergetool" whatever the state, and
        answer "no mergetool configured" only once it had been pressed. On a
        review with no archived ancestor that is the *only* entry which
        merges anything -- 3 and 4 need an ancestor -- so the review
        dead-ended on a menu item the program already knew could not run."""
		tool = self.mod.mergetool_program(self.mod.DEFAULT_CONF)
		self.park("app.conf", "mine\n", "theirs\n")
		out = self.dispatch("s")
		# Open-ended on purpose: the line reads "(sdiff)" where it is
		# installed and "(sdiff: not installed)" where it is not, and this
		# test is about the tool being named either way.
		self.assertIn(f"launch mergetool ({tool}", out)

	def test_a_two_way_review_still_offers_the_built_in_merge(self):
		"""The built-in merge is what this program offers instead of dpkg's
        keep-or-replace, and it used to withdraw itself exactly where it was
        most needed: with no ancestor, 3 and 4 were absent and the only way
        left to combine the two files was an external tool. That is
        backwards -- the external tool is for someone who prefers their
        own."""
		self.park("app.conf", "mine\n", "theirs\n")     # no ancestor
		out = self.dispatch("s")
		self.assertIn("use the merged version", out)
		self.assertIn("edit the merged version", out)

	def test_a_two_way_merge_marks_up_both_versions(self):
		t = self.park("app.conf", "shared\nmine\n", "shared\ntheirs\n")
		self.dispatch("3")
		got = self.content(t)
		self.assertEqual(got, "shared\n<<<<<<< current\nmine\n=======\n"
		                      "theirs\n>>>>>>> new\n")
		self.assertFalse(self.parked_exists(t))

	def test_the_menu_says_when_no_tool_is_configured_at_all(self):
		self.conf["mergetool"] = self.conf["merge"] = ""
		self.park("app.conf", "mine\n", "theirs\n")
		out = self.dispatch("s")
		self.assertIn("none configured", out)

	def test_an_uninstalled_mergetool_is_named_before_it_is_run(self):
		"""A missing program is a shell exit of 127, which the result check
        reports as "did not produce a result" -- the symptom, with the cause
        hidden. Say which tool, and say it in the menu too."""
		errs = self.errors()
		self.conf["mergetool"] = "nosuchmergetool-xyzzy {mine} {output}"
		t = self.park("app.conf", "mine\n", "theirs\n")
		out = self.dispatch("5", "s")
		self.assertIn("not installed", out)
		self.assertTrue(any("nosuchmergetool-xyzzy" in e and "not installed"
		                    in e for e in errs), errs)
		self.assertFalse(any("did not produce a result" in e for e in errs),
		                 errs)
		self.assertTrue(self.parked_exists(t))
		self.assertEqual(self.content(t), "mine\n")

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

	def test_portage_style_cfg_files_are_picked_up(self):
		"""Portage parks updates as ._cfg0000_<name>. Debian does not
        produce these, but the tool answers to dispatch-conf and etc-update,
        so someone will arrive with them."""
		target = os.path.join(self.etc, "cfg.conf")
		with open(target, "w") as f:
			f.write("mine\n")
		with open(os.path.join(self.etc, "._cfg0000_cfg.conf"), "w") as f:
			f.write("theirs\n")
		self.dispatch("2")
		self.assertEqual(self.content(target), "theirs\n")

	def test_the_diff_display_runs(self):
		"""color_diff only renders, but it runs on every conflict and a
        traceback there would strand the review half way through."""
		self.mod = load()          # undo setUp's stub of color_diff
		self.mod.load_conf = lambda: self.conf
		self.mod.need_root = lambda: None
		self.mod.input = lambda prompt="": "1"
		self.park("shown.conf", "a\nMINE\nc\n", "a\nTHEIRS\nc\n",
		          ancestor="a\nb\nc\n")
		out = self.dispatch("1")
		self.assertIn("shown.conf", out)

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


class TestParsedOutputIsNotTranslated(unittest.TestCase):
	"""apt and dpkg translate their messages, and this file reads them.

    Under `LANGUAGE=de`, `Installed:` becomes `Installiert:` and `Setting up
    x` becomes `Einrichten von x`, so every English pattern here silently
    stops matching. Measured before the fix: `emerge -s '^tree$'` reported
    `Latest version available: ?`, and a merge printed apt's raw output
    instead of Portage's -- which is the one thing this program exists to
    do. Nothing errored in either case.

    Not everything was at risk, and the difference is why these three sites
    and no others: `Inst`/`Remv` from `apt-get -s`, the `apt-cache showpkg`
    headings and RFC822 field names are emitted untranslated, so the
    resolver, the virtual-provider lookup and the index parser were always
    safe. Checked, not assumed."""

	def setUp(self):
		self.mod = load()
		self.mod.need_root = lambda: None
		self.mod.load_conf = lambda: {}
		self.mod.archive_settled = lambda conf, pkgs: 0
		self.mod.pending_notice = lambda conf: None
		self.mod.einfo = lambda m: None
		self.envs = []

	def record_popen(self):
		class P:
			def __init__(self):
				# Per instance: stream_apt closes the pipe when it is done,
				# so a shared one leaves the second call reading a closed
				# file.
				self.stdout = io.BytesIO(b"")

			def wait(self):
				return 0
		original = self.mod.subprocess.Popen
		self.addCleanup(setattr, self.mod.subprocess, "Popen", original)
		self.mod.subprocess.Popen = lambda *a, **k: (
		    self.envs.append(k.get("env")), P())[1]

	def test_apt_cache_policy_is_read_in_c(self):
		"""The one that produced wrong data rather than plain output: every
        version came back `?`."""
		class R:
			stdout, stderr, returncode = "", "", 0
		self.mod.capture = lambda cmd, env=None: (self.envs.append(env), R)[1]
		self.mod.AptBackend()._policy_batch(["bash"])
		self.assertEqual(self.envs[0].get("LC_ALL"), "C")

	def test_the_merge_stream_is_read_in_c(self):
		self.record_popen()
		be = self.mod.AptBackend()
		be._action, be._atoms = ["install", "foo"], ["foo"]
		be._manual_set = lambda: set()
		be._apply_marks = lambda before, oneshot: None
		with contextlib.redirect_stdout(io.StringIO()):
			be.merge([("foo", "1.0", None, 0, "ebuild", "")], ["foo"],
			         {"fetchonly": False, "oneshot": False})
		self.assertEqual(self.envs[0].get("LC_ALL"), "C")

	def test_forcing_the_locale_did_not_drop_the_frontend(self):
		"""Both stream sites already passed DEBIAN_FRONTEND, and adding the
        locale to them is exactly where it would get overwritten instead of
        joined -- which is what happened on the first attempt."""
		self.record_popen()
		be = self.mod.AptBackend()
		be._action, be._atoms = ["install", "foo"], ["foo"]
		be._manual_set = lambda: set()
		be._apply_marks = lambda before, oneshot: None
		with contextlib.redirect_stdout(io.StringIO()):
			be.merge([("foo", "1.0", None, 0, "ebuild", "")], ["foo"],
			         {"fetchonly": False, "oneshot": False})
			be.unmerge([("foo", "1.0")])
		self.assertEqual(len(self.envs), 2, "expected a merge and an unmerge")
		for env in self.envs:
			self.assertEqual(env.get("LC_ALL"), "C")
			self.assertEqual(env.get("DEBIAN_FRONTEND"), "noninteractive")


class TestMergeAftermath(unittest.TestCase):
	"""A merge that fails partway still installed something. Those packages'
    conffiles are settled on disk and have to become the new ancestor, and
    anything dpkg parked has to be announced -- a partial install is exactly
    when you need to be told config files are waiting."""

	def setUp(self):
		self.mod = load()
		self.be = self.mod.AptBackend()
		self.be._action = ["install", "foo"]
		self.be._atoms = ["foo"]
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
		self.mod.capture = lambda cmd, env=None: (self.marked.append(cmd), R)[1]

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
		# Both streams: failures go to stderr, progress to stdout, and a
		# caller that wants to assert on either needs to see both. The
		# separate copies are kept for the test that checks which is which.
		self.out, self.err = io.StringIO(), io.StringIO()
		with contextlib.redirect_stdout(self.out), \
		     contextlib.redirect_stderr(self.err):
			if rc:
				with self.assertRaises(SystemExit):
					self.be.merge(merges, ["foo"], opts)
			else:
				self.be.merge(merges, ["foo"], opts)
		return self.out.getvalue() + self.err.getvalue()

	def test_a_failure_goes_to_stderr_not_into_the_plan(self):
		"""`!!!` messages went to stdout. That is wrong in the case
        `--pretend` exists for: `emerge -p foo > plan.txt` wrote "Unable to
        locate package" into the plan file and left the terminal empty, and
        it fed errors into `emerge -pv @world | head`, which project.md
        names as the normal way to read a long list."""
		self.run_merge(1)
		self.assertIn("emerge failed", self.err.getvalue())
		self.assertNotIn("emerge failed", self.out.getvalue())

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
		self.be._action = ["install", "foo", "bar"]
		self.manual = {"foo"}
		self.run_merge(0, {"fetchonly": False, "oneshot": True})
		self.assertEqual(self.marked, [["apt-mark", "auto", "bar"]])

	def test_a_dependency_apt_was_told_to_install_does_not_enter_world(self):
		"""The leak this closed. --no-dep-upgrade resolves the whole closure
        itself and hands apt every package as an explicit `pkg=version` pin,
        and apt marks everything on its command line manually installed. One
        `emerge --no-dep-upgrade libsdl3-dev` therefore moved 32 dependencies
        into @world, where --depclean could never reclaim them again."""
		self.be._atoms = ["foo"]
		self.be._action = ["install", "foo=1.0", "libdep=2.0", "libdep2=3.0"]
		self.manual = set()
		self.run_merge(0, {"fetchonly": False, "oneshot": False})
		self.assertEqual(self.marked,
		                 [["apt-mark", "auto", "libdep", "libdep2"]])

	def test_the_target_itself_still_enters_world(self):
		"""The other half: demoting the dependencies must not demote the
        package the user actually asked for."""
		self.be._atoms = ["foo"]
		self.be._action = ["install", "foo=1.0", "libdep=2.0"]
		self.manual = set()
		self.run_merge(0, {"fetchonly": False, "oneshot": False})
		self.assertNotIn("foo", self.marked[0][2:])

	def test_a_dependency_already_in_world_is_left_alone(self):
		"""Someone installed libdep deliberately once. Pulling it in as a
        dependency today is no reason to take it out of their world set."""
		self.be._atoms = ["foo"]
		self.be._action = ["install", "foo=1.0", "libdep=2.0"]
		self.manual = {"libdep"}
		self.run_merge(0, {"fetchonly": False, "oneshot": False})
		self.assertEqual(self.marked, [])

	def test_oneshot_demotes_the_atom_and_its_dependencies_alike(self):
		self.be._atoms = ["foo"]
		self.be._action = ["install", "foo=1.0", "libdep=2.0"]
		self.manual = set()
		self.run_merge(0, {"fetchonly": False, "oneshot": True})
		self.assertEqual(self.marked, [["apt-mark", "auto", "foo", "libdep"]])

	def test_option_flags_are_not_mistaken_for_package_names(self):
		"""`install --reinstall foo` names one package, not two."""
		self.be._atoms = ["foo"]
		self.be._action = ["install", "--reinstall", "foo"]
		self.manual = set()
		self.run_merge(0, {"fetchonly": False, "oneshot": True})
		self.assertEqual(self.marked, [["apt-mark", "auto", "foo"]])

	def test_oneshot_reports_what_it_kept_out_of_world(self):
		self.be._atoms = ["foo"]
		out = self.run_merge(0, {"fetchonly": False, "oneshot": True})
		self.assertIn("Not recording targets", out)
		self.assertNotIn("Recording targets in", out.replace(
		    "Not recording targets", ""))

	def test_a_failed_mark_warns_instead_of_lying(self):
		class R:
			stdout, stderr, returncode = "", "apt-mark exploded", 1
		self.mod.capture = lambda cmd, env=None: R
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
		# subprocess is the same module object the script imports, so the
		# original has to be captured *before* the patch. Reading it back
		# inside addCleanup restores the patch over itself and leaks it into
		# every test that follows, in every file.
		self.original_call = subprocess.call
		self.addCleanup(setattr, self.mod.subprocess, "call",
		                self.original_call)
		self.patch_call(lambda cmd, shell=False: (self.cmds.append(cmd), 0)[1])

	def patch_call(self, fn):
		self.mod.subprocess.call = fn

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

	def test_trouble_is_reported(self):
		self.patch_call(lambda cmd, shell=False: 2)
		self.assertFalse(self.mod.run_mergetool(
		    self.conf(mergetool="x {mine}"), "/b", "/m", "/t", "/o"))

	def test_the_diff_familys_1_means_differed_not_failed(self):
		"""Every file dispatch-conf reviews differs, so sdiff exits 1 on
        every successful merge it performs. Measured: 1 is a completed
        merge, 2 is quit / EOF / bad arguments, and 2 empties the output."""
		self.patch_call(lambda cmd, shell=False: 1)
		self.assertTrue(self.mod.run_mergetool(
		    self.conf(mergetool="x {mine}"), "/b", "/m", "/t", "/o"))

	# -- the shipped default -------------------------------------------------

	def test_the_default_template_is_a_usable_command(self):
		"""The default has to survive the same substitution a user's does,
        or option 5 fails on a machine nobody has configured -- which is
        every machine, since there is no config file until someone writes
        one."""
		self.assertTrue(self.mod.run_mergetool(
		    dict(self.mod.DEFAULT_CONF), "/b", "/mine", "/theirs", "/out"))
		self.assertEqual(self.cmds[0],
		                 "sdiff --suppress-common-lines "
		                 "--output='/out' '/mine' '/theirs'")

	def test_the_default_mergetool_is_the_one_it_claims_to_be(self):
		"""Which tool is shipped as the default is a fact about this file
        and holds anywhere, so it is asserted unconditionally."""
		self.assertEqual(
		    self.mod.mergetool_program(self.mod.DEFAULT_CONF), "sdiff")

	@unittest.skipUnless(HAVE_DPKG, "not a Debian-ish box")
	def test_the_default_mergetool_is_installed_on_a_debian_box(self):
		"""sdiff is from diffutils, which is Essential on Debian -- the
        whole reason the default is sdiff rather than a nicer tool, since
        meld and kdiff3 would be a default that is absent by default.

        Skipped rather than failed off Debian, because that is a claim
        about Debian and this is the half of the suite that is meant to
        run anywhere. Written as a bare assertion first, which would have
        failed the unit suite on any machine without diffutils rather
        than reporting that the claim was not checkable there."""
		tool = self.mod.mergetool_program(self.mod.DEFAULT_CONF)
		self.assertIsNotNone(shutil.which(tool),
		                     f"the default mergetool {tool!r} is not installed")

	def test_mergetool_program_reads_the_first_word(self):
		self.assertIsNone(self.mod.mergetool_program(self.conf()))
		self.assertEqual(self.mod.mergetool_program(
		    self.conf(mergetool="meld {mine} {theirs}")), "meld")
		self.assertEqual(self.mod.mergetool_program(
		    self.conf(merge="sdiff --output='%s' '%s' '%s'")), "sdiff")

	def test_mergetool_program_does_not_raise_on_an_unreadable_template(self):
		"""It only names the tool for the menu, so a template it cannot
        parse must fall back to offering it, not to hiding it behind a
        crash."""
		self.assertIsNone(self.mod.mergetool_program(
		    self.conf(mergetool="meld '{mine}")))


class TestAncestorWrite(unittest.TestCase):
	"""The archive is the fourth piece of state to need an atomic write,
    after the world file, the config file and the index.

    A torn ancestor is not an unreadable one -- it parses perfectly with
    lines missing, and every later 3-way merge is then made against a file
    the package never shipped. Recovery sharpened it: this now runs during
    an interactive command, right after a ten-megabyte download, and a
    Ctrl-C during a long download is the exact case the index was made
    atomic for."""

	def setUp(self):
		self.dir = tempfile.mkdtemp(prefix="emerge-anc-write-")
		self.addCleanup(shutil.rmtree, self.dir, True)
		self.conf = {"archive-dir": os.path.join(self.dir, "archive")}
		self.src = os.path.join(self.dir, "shipped")
		with open(self.src, "w") as f:
			f.write("shipped\n")

	def dest(self):
		return em.archive_path(self.conf, "/etc/app.conf")

	def test_it_stores_the_content(self):
		em._store_ancestor(self.conf, "/etc/app.conf", self.src)
		with open(self.dest()) as f:
			self.assertEqual(f.read(), "shipped\n")

	def test_a_failed_write_leaves_the_previous_ancestor_intact(self):
		em._store_ancestor(self.conf, "/etc/app.conf", self.src)
		with open(self.src, "w") as f:
			f.write("newer\n")
		real = em.os.replace
		em.os.replace = lambda *a: (_ for _ in ()).throw(OSError(28, "full"))
		self.addCleanup(setattr, em.os, "replace", real)
		with self.assertRaises(OSError):
			em._store_ancestor(self.conf, "/etc/app.conf", self.src)
		with open(self.dest()) as f:
			self.assertEqual(f.read(), "shipped\n", "the old one was lost")

	def test_a_failed_write_leaves_no_temporary_behind(self):
		real = em.os.replace
		em.os.replace = lambda *a: (_ for _ in ()).throw(OSError(28, "full"))
		self.addCleanup(setattr, em.os, "replace", real)
		with self.assertRaises(OSError):
			em._store_ancestor(self.conf, "/etc/app.conf", self.src)
		self.assertEqual(os.listdir(os.path.dirname(self.dest())), [])

	def test_the_mode_survives_the_rename(self):
		"""os.replace swaps in a new inode and copy2 preserved the mode: the
        ancestor of a conffile that is mode 600 must not come out
        world-readable."""
		os.chmod(self.src, 0o600)
		em._store_ancestor(self.conf, "/etc/app.conf", self.src)
		self.assertEqual(os.stat(self.dest()).st_mode & 0o777, 0o600)


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

	# A conffile is bytes, and not necessarily UTF-8 ones. An older /etc
	# file with a latin-1 accent in a comment is the ordinary case, and it
	# used to come back through dispatch-conf with that byte replaced by
	# U+FFFD -- read with errors="replace", written back as the replacement
	# character, no way to recover the original. Silent, and on the one path
	# in the program that edits /etc.
	LATIN1 = b"# caf\xe9 settings\nkey = value\n"

	def test_a_byte_that_is_not_utf8_survives_the_round_trip(self):
		with open(self.path, "wb") as f:
			f.write(self.LATIN1)
		em._write(self.path, em.read_lines(self.path))
		with open(self.path, "rb") as f:
			self.assertEqual(f.read(), self.LATIN1,
			                 "dispatch-conf rewrote a byte it was only "
			                 "supposed to be moving")

	def test_the_encoding_does_not_depend_on_the_locale(self):
		"""Without an explicit encoding, what a file means is whatever LANG
        said. CPython's UTF-8 mode covers the C locale -- the one anybody
        would think to test -- and not a latin-1 one, so the bug would have
        been invisible exactly where it was looked for."""
		with open(self.path, "wb") as f:
			f.write(self.LATIN1)
		self.assertEqual(em.read_lines(self.path)[0],
		                 "# caf\udce9 settings\n")

	def test_content_that_is_not_utf8_can_still_be_displayed(self):
		"""Reading losslessly puts lone surrogates in the string, and print()
        cannot encode one: it raises UnicodeEncodeError unless stdout is in
        UTF-8 mode. Displaying the file is the first thing a review does, so
        that would have been a crash on the same files, which is worse than
        the corruption it replaced. Encoding the captured output is what
        makes this a real check -- a StringIO holds surrogates happily and
        would pass either way."""
		with open(self.path, "wb") as f:
			f.write(self.LATIN1)
		mod = load()
		buf = io.StringIO()
		with contextlib.redirect_stdout(buf):
			mod.color_diff(mod.read_lines(self.path), ["# other\n"],
			               "a", "b")
		buf.getvalue().encode("utf-8")
		self.assertIn(r"caf\xe9", buf.getvalue(),
		              "the undecodable byte should be shown escaped, not "
		              "dropped or replaced")

	def test_preserves_mode(self):
		em._write(self.path, ["x\n"])
		self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

	# A conffile is quite often a symlink -- an admin pointing /etc/foo at a
	# git-managed tree, or Debian's own alternatives. os.replace swaps the
	# LINK for a regular file, so the indirection vanished and the merged
	# text landed at the link's name while the file it pointed at kept the
	# old content. Both halves are asserted below, because fixing only the
	# first would leave the update going to the wrong file.
	def link_to(self, name="link.conf"):
		link = os.path.join(self.dir, name)
		os.symlink(self.path, link)
		return link

	def test_a_symlinked_conffile_is_still_a_symlink_afterwards(self):
		link = self.link_to()
		em._write(link, ["merged\n"])
		self.assertTrue(os.path.islink(link),
		                "dispatch-conf replaced the admin's symlink with a "
		                "regular file")

	def test_writing_through_a_symlink_updates_the_file_it_points_at(self):
		link = self.link_to()
		em._write(link, ["merged\n"])
		with open(self.path) as f:
			self.assertEqual(f.read(), "merged\n",
			                 "the update landed at the link's name and left "
			                 "the real file stale")

	def test_a_relative_symlink_resolves_too(self):
		"""The usual spelling in /etc, and the one that breaks if the target
        is joined against the wrong directory."""
		sub = os.path.join(self.dir, "sub")
		os.mkdir(sub)
		target = os.path.join(sub, "real.conf")
		with open(target, "w") as f:
			f.write("original\n")
		link = os.path.join(self.dir, "rel.conf")
		os.symlink(os.path.join("sub", "real.conf"), link)
		em._write(link, ["merged\n"])
		self.assertTrue(os.path.islink(link))
		with open(target) as f:
			self.assertEqual(f.read(), "merged\n")

	def test_the_temporary_file_lands_beside_the_target(self):
		"""os.replace is only atomic within one filesystem, so the temp file
        has to be made next to the file it will replace -- not next to the
        symlink, which may be on another mount entirely.

        Asserted by capturing what os.replace was actually called with. The
        obvious version of this test -- write, then look for leftovers --
        passes whether or not the fix is present, because there is no
        leftover either way; it was written that way first and the mutation
        went straight through it."""
		sub = os.path.join(self.dir, "elsewhere")
		os.mkdir(sub)
		target = os.path.join(sub, "real.conf")
		with open(target, "w") as f:
			f.write("original\n")
		link = os.path.join(self.dir, "link.conf")
		os.symlink(target, link)

		seen = []
		original = os.replace          # captured BEFORE patching, or the
		self.addCleanup(setattr, os, "replace", original)   # cleanup restores
		def spy(src, dst):             # the patch over itself
			seen.append(src)
			return original(src, dst)
		os.replace = spy

		em._write(link, ["merged\n"])
		self.assertEqual([os.path.dirname(s) for s in seen], [sub],
		                 "the temporary file was created next to the symlink "
		                 "rather than next to the file being replaced")

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

	def test_extended_attributes_survive_the_replacement(self):
		"""os.replace swaps in a different inode, so a freshly written file
        starts with none of what the original carried outside the stat
        struct: SELinux labels, POSIX ACLs, file capabilities. An /etc file
        that comes back unlabelled can stop being readable by the one daemon
        that needs it, and nothing about the merge would suggest why."""
		try:
			os.setxattr(self.path, "user.emerge-test", b"label")
		except OSError as e:
			self.skipTest(f"filesystem does not support xattrs: {e}")
		em._write(self.path, ["x\n"])
		self.assertEqual(os.getxattr(self.path, "user.emerge-test"), b"label")

	def test_a_file_without_xattrs_is_written_normally(self):
		em._write(self.path, ["x\n"])
		with open(self.path) as f:
			self.assertEqual(f.read(), "x\n")


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


class TestBoundedIndexReading(unittest.TestCase):
	"""A repository decides how many bytes it sends and how far they expand.

    `--sync` fetched an index with a plain read() and unpacked it with
    gzip/lzma.decompress, both of which produce whatever the input asks for.
    Measured before this existed: 61 KB of .xz expands to 400 MB, a ratio of
    6,859, so a mirror serving nine megabytes can demand sixty gigabytes --
    of a backend whose reason for existing is boxes that do not have it. The
    one-shot call took peak RSS to 828 MB for that 61 KB input; refusing at
    a 64 MB ceiling held it to 108 MB.

    It ran on unverified bytes, too: the index was unpacked first and
    checked against the signed Release afterwards."""

	INDEX = b"Package: tree\nVersion: 2.2.1-1\n\n" * 200

	def test_it_reads_a_real_index_in_each_encoding(self):
		for ext, data in ((".gz", gzip.compress(self.INDEX)),
		                  (".xz", lzma.compress(self.INDEX)),
		                  ("", self.INDEX)):
			with self.subTest(encoding=ext or "plain"):
				self.assertEqual(em.decompress_bounded(data, ext), self.INDEX)

	def test_it_refuses_something_that_expands_past_the_ceiling(self):
		for ext, compress in ((".gz", gzip.compress), (".xz", lzma.compress)):
			with self.subTest(encoding=ext):
				bomb = compress(b"\0" * (4 * 1024 * 1024))
				self.assertLess(len(bomb), 100 * 1024,
				                "the fixture is not a bomb; the test would "
				                "pass for the wrong reason")
				with self.assertRaises(RuntimeError) as caught:
					em.decompress_bounded(bomb, ext, limit=1024 * 1024)
				self.assertIn("expands", str(caught.exception))

	def test_a_truncated_index_is_refused_rather_than_returned_short(self):
		"""Silently returning the part that unpacked would be worse than
        failing: a truncated Packages parses fine and just has fewer
        packages in it, so the resolver would answer from half an index.

        Cut at half the *compressed* length, which had to be measured. The
        first version of this test cut at a fixed 120 bytes, and this index
        compresses to 88 -- so it truncated nothing, and passed by reading a
        whole file."""
		for ext, compress in ((".gz", gzip.compress), (".xz", lzma.compress)):
			with self.subTest(encoding=ext):
				whole = compress(self.INDEX)
				with self.assertRaises(RuntimeError):
					em.decompress_bounded(whole[:len(whole) // 2], ext)

	def test_it_is_no_less_strict_than_the_one_shot_calls_it_replaced(self):
		"""Reading in bounded steps must not cost the integrity checking that
        came free with gzip.decompress. The gzip trailer is the case worth
        pinning: the deflate stream can end cleanly while the CRC32 that
        follows it says the bytes are wrong, and a decompressor that stops
        at the end of the stream never looks."""
		whole = gzip.compress(self.INDEX)
		for name, data in (
		    ("payload corrupted",
		     whole[:30] + bytes([whole[30] ^ 0xFF]) + whole[31:]),
		    ("CRC trailer corrupted", whole[:-8] + b"\x00" * 8),
		    ("trailer removed", whole[:-8]),
		):
			with self.subTest(case=name):
				with self.assertRaises(Exception):
					gzip.decompress(data)          # the reference refuses it
				with self.assertRaises(RuntimeError):
					em.decompress_bounded(data, ".gz")

	def test_plain_bytes_past_the_ceiling_are_refused_too(self):
		with self.assertRaises(RuntimeError):
			em.decompress_bounded(b"x" * 2048, "", limit=1024)

	def test_fetch_stops_at_the_limit(self):
		root = _scratch()
		os.makedirs(root)
		path = os.path.join(root, "index")
		with open(path, "wb") as f:
			f.write(b"x" * 4096)
		self.assertEqual(len(em.fetch(path, limit=4096)), 4096,
		                 "a file exactly at the limit is not too big")
		with self.assertRaises(RuntimeError):
			em.fetch(path, limit=4095)
		self.assertEqual(len(em.fetch(path)), 4096,
		                 "no limit given, no limit applied")


class TestAtomicIndexWrite(unittest.TestCase):
	"""A half-written index is the worst kind, because it reads as a whole
    one.

    `sync()` used to open the index and start filling it, so a Ctrl-C during
    a long download -- and `--sync` is the operation people interrupt, being
    the slow one that talks to the network -- left a truncated Packages
    behind. Truncated does not mean unreadable: measured, cutting a
    500-package index a third of the way through leaves 168 packages that
    parse perfectly, so the resolver answers from part of the archive and
    reports "there are no packages to satisfy" for things that exist.

    Config files and the world file were already written temp-then-rename.
    The index was the one that was not."""

	def setUp(self):
		self.dir = _scratch()
		os.makedirs(self.dir)
		self.path = os.path.join(self.dir, "repo_Packages")
		with open(self.path, "wb") as f:
			f.write(b"Package: old\n\n")

	def test_it_replaces_the_content(self):
		em.write_atomic(self.path, b"Package: new\n\n")
		with open(self.path, "rb") as f:
			self.assertEqual(f.read(), b"Package: new\n\n")

	def test_it_leaves_no_temporary_file(self):
		em.write_atomic(self.path, b"Package: new\n\n")
		self.assertEqual(os.listdir(self.dir), ["repo_Packages"])

	def test_a_failed_write_leaves_the_previous_index_intact(self):
		"""The property that matters. A reader during or after a failed sync
        must see the whole old index, never a piece of the new one."""
		original = os.fsync
		self.addCleanup(setattr, os, "fsync", original)

		def boom(fd):
			raise OSError(28, "No space left on device")
		os.fsync = boom

		with self.assertRaises(OSError):
			em.write_atomic(self.path, b"Package: new\n\n" * 100)
		with open(self.path, "rb") as f:
			self.assertEqual(f.read(), b"Package: old\n\n",
			                 "a failed write damaged the index that was "
			                 "already there")
		self.assertEqual(os.listdir(self.dir), ["repo_Packages"],
		                 "a failed write left its temporary file in the tree")

	def test_the_temporary_file_is_named_for_this_program(self):
		"""`_write` installs into /etc, where a bare `.tmp` beside somebody's
        config says nothing about whose it is. The suffix survived the three
        writers being merged into one, which is exactly the sort of detail a
        consolidation quietly drops."""
		seen = []
		original = os.replace
		self.addCleanup(setattr, os, "replace", original)
		os.replace = lambda src, dst: (seen.append(src), original(src, dst))[1]
		em._write(self.path, ["x\n"])
		self.assertTrue(seen[0].endswith(".emerge-tmp"), seen)

	def test_a_failed_world_write_leaves_no_temporary_behind(self):
		"""The bug the consolidation found. `_write_world` had its own copy
        of the durability sequence and was the one missing the cleanup, so
        an interrupted write left a world.tmp next to the world file --
        invisible, because nothing looks there until the next crash."""
		mod = load()
		root = _scratch()
		os.makedirs(root)
		mod.LIB_DIR = root
		mod.WORLD = os.path.join(root, "world")
		original = os.fsync
		self.addCleanup(setattr, os, "fsync", original)

		def boom(fd):
			raise OSError(28, "No space left on device")
		os.fsync = boom

		with self.assertRaises(OSError):
			mod.DpkgBackend(pretend=True)._write_world({"bash", "tree"})
		self.assertEqual(os.listdir(root), [],
		                 "a failed world write left its temporary file")

	def test_sync_writes_the_index_through_it(self):
		"""The wiring, checked at the source, which is weak and deliberate.

        The integration suite runs real syncs, so the call site works -- but
        a real sync is never interrupted there, so nothing in the suite
        would notice it going back to a plain open. Grepping the one line is
        the honest amount of coverage available; claiming more would mean
        writing a test that cannot fail."""
		with open(SCRIPT) as f:
			src = f.read()
		self.assertIn("write_atomic(self._tree_file(", src)
		self.assertNotIn("with open(self._tree_file", src)


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

	def test_a_disabled_stanza_is_skipped(self):
		"""sources.list(5): "it is usually easier to add the field
        Enabled: no to the stanza to disable the entry". Syncing from a
        repository the admin deliberately switched off is not a small
        oversight -- it is the one they explicitly asked us not to touch."""
		got = self.parse({"/etc/apt/sources.list.d/off.sources":
		                  "Types: deb\n"
		                  "URIs: http://example.invalid/debian\n"
		                  "Suites: trixie\nComponents: main\n"
		                  "Enabled: no\n"})
		self.assertEqual(got, [])

	def test_enabled_no_is_case_insensitive(self):
		got = self.parse({"/etc/apt/sources.list.d/off.sources":
		                  "Types: deb\nURIs: http://x.invalid/d\n"
		                  "Suites: trixie\nComponents: main\n"
		                  "Enabled: NO\n"})
		self.assertEqual(got, [])

	def test_enabled_yes_and_a_missing_field_both_stay(self):
		"""Removing the field or setting it to yes re-enables it, so the
        default when it is absent has to be on."""
		got = self.parse({"/etc/apt/sources.list.d/on.sources":
		                  "Types: deb\nURIs: http://a.invalid/d\n"
		                  "Suites: trixie\nComponents: main\nEnabled: yes\n"
		                  "\n"
		                  "Types: deb\nURIs: http://b.invalid/d\n"
		                  "Suites: trixie\nComponents: main\n"})
		self.assertEqual([u for u, *_ in got],
		                 ["http://a.invalid/d", "http://b.invalid/d"])

	def test_only_the_disabled_stanza_of_a_file_is_dropped(self):
		got = self.parse({"/etc/apt/sources.list.d/mixed.sources":
		                  "Types: deb\nURIs: http://live.invalid/d\n"
		                  "Suites: trixie\nComponents: main\n"
		                  "\n"
		                  "Types: deb\nURIs: http://dead.invalid/d\n"
		                  "Suites: trixie\nComponents: main\n"
		                  "Enabled: no\n"})
		self.assertEqual([u for u, *_ in got], ["http://live.invalid/d"])

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


class TestFetch(unittest.TestCase):
	"""fetch() is the one place a source URL becomes a request. http(s) and
	file:// are covered end to end by test_integration; this covers the third
	form, which no integration test produces: a bare path.

	`deb /media/usb ./` is a legal sources.list line, and read_sources hands
	that through with no scheme at all. Without the conversion urllib raises
	`unknown url type`, which reads as a broken repository rather than a
	line the user is entitled to write."""

	def setUp(self):
		self.dir = tempfile.mkdtemp(prefix="emerge-fetch-")
		self.addCleanup(shutil.rmtree, self.dir, True)
		self.path = os.path.join(self.dir, "Packages")
		with open(self.path, "wb") as f:
			f.write(b"Package: emtest\nVersion: 1.0\n")

	def test_a_bare_path_is_read_as_a_local_file(self):
		self.assertEqual(em.fetch(self.path),
		                 b"Package: emtest\nVersion: 1.0\n")

	def test_an_explicit_file_url_still_works(self):
		self.assertEqual(em.fetch("file://" + self.path),
		                 b"Package: emtest\nVersion: 1.0\n")

	def test_a_missing_bare_path_raises_rather_than_returning_empty(self):
		"""sync() decides a repository is unusable by catching the error, so
		a silent empty result would be written out as a valid empty index."""
		with self.assertRaises(Exception):
			em.fetch(os.path.join(self.dir, "no-such-file"))


class TestSessionLeaderComms(unittest.TestCase):
	"""/proc/PID/comm is capped by the kernel at TASK_COMM_LEN-1 = 15
	characters, so a leader whose binary name is longer only ever appears
	truncated.

	This is a silent failure in both directions: an entry written out in full
	never matches the truncated comm, and an entry longer than 15 characters
	can never match anything at all. Neither produces an error -- the session
	simply goes undetected, and `emerge -u` stops warning that an upgrade is
	about to restart the desktop.

	It has already happened once. SDDM 0.21 renamed its greeter to
	sddm-greeter-qt6, one character over the limit, so on Debian trixie the
	entry "sddm-greeter" matched nothing."""

	def setUp(self):
		self.mod = load()

	COMM_MAX = 15

	def test_no_entry_is_too_long_to_ever_match(self):
		too_long = sorted(c for c in self.mod._SESSION_LEADER_COMMS
		                  if len(c) > self.COMM_MAX)
		self.assertEqual(too_long, [],
		                 f"these can never match a real comm; use the "
		                 f"truncated spelling: {too_long}")

	def test_the_kernel_really_does_truncate_where_we_think(self):
		"""Measured rather than assumed -- the whole entry list is derived
		from this limit, so it is worth pinning to the running kernel rather
		than to a constant someone remembered."""
		with tempfile.TemporaryDirectory() as d:
			long_name = "abcdefghijklmnopqrstuvwxyz"[:20]
			path = os.path.join(d, long_name)
			shutil.copy("/bin/sleep", path)
			proc = subprocess.Popen([path, "5"])
			try:
				# Poll until comm *changes to the child's own name*, not
				# merely until it is readable. Popen forks and then execs,
				# and in between the child still carries the parent's comm
				# -- so reading once can return "python3" and fail this test
				# at random, which is exactly what it must not do.
				want = long_name[:self.COMM_MAX]
				comm = None
				deadline = time.monotonic() + 5
				while time.monotonic() < deadline:
					try:
						with open(f"/proc/{proc.pid}/comm") as f:
							comm = f.read().strip()
					except OSError:
						comm = None
					if comm == want:
						break
					time.sleep(0.01)
				self.assertEqual(comm, want,
				                 "the child never reported its own comm")
				self.assertEqual(len(comm), self.COMM_MAX)
			finally:
				proc.kill()
				proc.wait()

	def test_the_known_long_binaries_are_listed_truncated(self):
		"""Each of these is a real binary whose name exceeds the limit. The
		expected value is the truncation, not the name."""
		for binary in ("gdm-session-worker", "sddm-greeter-qt6",
		               "lightdm-gtk-greeter"):
			with self.subTest(binary=binary):
				self.assertGreater(len(binary), self.COMM_MAX,
				                   "this case is pointless if it fits")
				self.assertIn(binary[:self.COMM_MAX],
				              self.mod._SESSION_LEADER_COMMS)

	def test_the_short_names_are_left_alone(self):
		"""A name that fits must be listed in full, not trimmed."""
		for binary in ("plasmashell", "kwin_wayland", "gnome-shell", "sddm"):
			with self.subTest(binary=binary):
				self.assertLessEqual(len(binary), self.COMM_MAX)
				self.assertIn(binary, self.mod._SESSION_LEADER_COMMS)

	def fake_proc(self, comms):
		"""A /proc holding exactly these {pid: comm}."""
		original_listdir = self.mod.os.listdir
		self.addCleanup(setattr, self.mod.os, "listdir", original_listdir)
		self.mod.os.listdir = lambda p: (list(comms) if p == "/proc"
		                                 else original_listdir(p))

		def fake_open(path, *a, **kw):
			pid = str(path).split("/")[2]
			return io.StringIO(comms[pid] + "\n")
		self.mod.open = fake_open

	def test_a_truncated_greeter_is_actually_found(self):
		"""End of the chain. The entries above are only useful if the
		truncated comm is what _find_session_leaders ends up matching, so
		this drives the real lookup rather than re-checking the set."""
		self.fake_proc({"41": "sddm-greeter-qt", "42": "bash"})
		self.assertEqual(self.mod._find_session_leaders(),
		                 [("41", "sddm-greeter-qt")])

	def test_the_untruncated_name_is_what_would_have_missed(self):
		"""Why the entry had to change: a session running SDDM 0.21 reports
		the truncated comm, and nothing else does."""
		self.fake_proc({"41": "sddm-greeter-qt6"})
		self.assertEqual(self.mod._find_session_leaders(), [],
		                 "an untruncated comm cannot occur; if this ever "
		                 "matches, the kernel limit assumption is wrong")

	def test_an_ordinary_process_is_not_a_leader(self):
		self.fake_proc({"41": "bash", "42": "sshd"})
		self.assertEqual(self.mod._find_session_leaders(), [])


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

	def test_a_mapped_path_containing_spaces_is_kept_whole(self):
		"""/proc/PID/maps puts the pathname last precisely because it may
        contain spaces. Splitting on every space and taking field 6 truncated
        such a mapping to its first word, so the library went unrecognised
        and its package was never flagged."""
		self.no_exe()
		self.patch(self.mod.shutil, "which", lambda _c: None)
		self.fake_maps(
		    "7f00-7f01 r-xp 00000000 08:01 100 /usr/lib/my libs/libodd.so.1\n")
		self.assertEqual(self.mod._proc_mapped_code("1", "kwin_wayland"),
		                 {"/usr/lib/my libs/libodd.so.1"})

	def test_a_deleted_mapping_is_stripped_too(self):
		"""An upgraded library keeps its inode mapped and the kernel appends
        ' (deleted)'; that is exactly the case worth reporting, so the marker
        must come off rather than the path being lost."""
		self.no_exe()
		self.patch(self.mod.shutil, "which", lambda _c: None)
		self.fake_maps(
		    "7f00-7f01 r-xp 00000000 08:01 100 /usr/lib/libgbm.so.1 (deleted)\n")
		self.assertEqual(self.mod._proc_mapped_code("1", "kwin_wayland"),
		                 {"/usr/lib/libgbm.so.1"})

	def test_anonymous_mappings_are_still_ignored(self):
		self.no_exe()
		self.patch(self.mod.shutil, "which", lambda _c: None)
		self.fake_maps("7f00-7f01 rw-p 00000000 00:00 0 \n"
		               "7f02-7f03 r-xp 00000000 00:00 0 [vdso]\n"
		               "7f04-7f05 r-xp 00000000 08:01 1 /usr/lib/libok.so\n")
		self.assertEqual(self.mod._proc_mapped_code("1", "x"),
		                 {"/usr/lib/libok.so"})

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
