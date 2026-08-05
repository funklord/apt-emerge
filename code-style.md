<!-- The three rules and their detail are copied from
     ~/.claude/guidelines/code-style.md -- the source. Keep in sync; fix
     drift the moment you notice it. -->

# code-style.md

Code style for this project -- `emerge`, the tests, the `Makefile` and the
`debian/` packaging.

**The global source**, `~/.claude/guidelines/code-style.md`, applies to
every private project and sits above both this file and `project.md`. Where
either disagrees with it, that is **drift to fix, not a local override**. A
genuine divergence needs a technical reason and is raised rather than
decided in passing -- and when a conflict actually comes up, stop and ask
instead of picking a winner.

Nothing is vendored.

## The three rules

1. **`snake_case`, not `camelCase`,** for identifiers this project defines.
2. **Tabs for indentation, spaces for alignment.**
3. **Lowercase filenames,** unless a tool demands otherwise.

## 1. Naming

`snake_case` for functions, variables and attributes. The program is a
single stdlib-only Python script with no library surface, so no prefix is
used: a leading underscore (`_name`) is Python's own private marker and
stands in for "unprefixed" -- it reads as "this does not leave the module",
which here means "not part of what a reader of the CLI ever sees".

- **No abbreviations that are not already vocabulary.** This has teeth
  here for a specific reason: the program speaks a Portage dialect, and a
  name invented internally tends to surface in output a user reads
  alongside real `emerge` output. Where Portage has a word, use Portage's
  word.
- **One word per concept, everywhere** -- the same word in the function
  name, the option spelling, the message text and the man page.

## 2. Indentation and alignment

Tabs carry structural indent level; spaces carry alignment within a level.
A continuation line takes tabs to its enclosing statement's level, then
spaces to the alignment column. **Never a space before a tab** in leading
whitespace.

No tab width is prescribed. The viewer decides.

Python accepts this. The language's only hard rule is that indentation must
not be *ambiguous* across tab widths -- tabs-then-spaces is unambiguous at
every width, and continuation lines inside brackets are not
indentation-significant at all. A line at module level whose continuation
is aligned under an open paren therefore carries **no tab at all**, only
alignment spaces, and that is correct rather than an oversight.

### Settled exceptions

These are settled in the source and need no discussion here. An exception
not listed is not yet settled: raise it rather than deciding in passing.

- **Markdown** -- list continuation and code fences are space-indented by
  specification. `project.md`, `README.md` and this file are exempt.
- **YAML** -- the spec forbids tabs for indentation outright, so
  `.github/workflows/tests.yml` uses spaces. This is not drift to fix; a
  tab in it is a parse error.
- **Makefile recipe lines** -- `make` requires a literal tab, so `Makefile`
  and `debian/rules` are compliant by construction. Their variable
  continuations still align with spaces.
- **`debian/` control files** -- deb822 is a space-continuation format:
  `control`, `copyright` and `changelog` follow what dpkg parses.

### The conversion from 4-space indentation

This project was 4-space indented until 2026-08-04 and was converted in one
whitespace-only change: `emerge`, `test_emerge.py` and `test_integration.py`
together, 6424 lines rewritten with no line added or removed.

The conversion was structural rather than arithmetic, which mattered:
roughly 600 lines here align continuation arguments under an open paren, at
columns that are not multiples of four. Dividing the leading whitespace by
four would have converted that alignment into indentation and destroyed it.
Instead each line's depth came from `tokenize`'s INDENT/DEDENT stack, with
everything past that depth preserved as spaces. Rows inside multi-line
string literals were left untouched, their leading whitespace being content.

It was **verified rather than eyeballed**: `ast.dump()` of every file is
byte-identical before and after. That dump is position-free but carries
every string constant, so an identical dump proves neither the block
structure nor any literal moved. `make check` passes unchanged -- 378 unit
tests and 5 integration tests.

### No autoformatter

`black` and `ruff format` rewrite tabs to spaces unconditionally and cannot
be configured out of it, so either would silently revert the conversion
above on the next save. `pycodestyle` would need W191 disabled.

**Do not run any of them, not even ad hoc on a single file.**

## The gate

What is mechanised instead is a checker: `tools/style_gate.py`, run by `make
style` and by a CI job of the same name. It is shared verbatim with the
sibling projects -- fix drift, do not edit the copy -- and it checks
indentation, trailing whitespace, carriage returns and the final newline.
Naming and filenames stay review items, as the source says they are.

For Python it asks the stronger of the two available questions. A line rule
can only ask whether tabs precede spaces; the gate instead converts the file
with a `tokenize`-driven fixer and compares *tab counts* per line, so a line
indented to the wrong depth is caught as well. That comparison never
mentions a tab width -- the conversion above is what a width was needed for,
and it is over. `tools/style_gate.py fix` writes the conversion, and refuses
to write one that would change the file's AST.

### `.style-gate.toml`, and why it is load-bearing

The gate decides what to look at by suffix or exact name, and **`emerge` has
no suffix**. Without the `indent_names` line naming it, the gate walks past
the entire program and reports the remaining files as a clean tree. That is
not a hypothetical: dropping the name leaves it printing `9 files conform`,
exit 0.

The tool's own collapse floor does not catch this, for two reasons worth
knowing before trusting it: nine files is not a collapse, and the floor is
configured in the very file that stopped being read. So the invariant is
pinned by a test instead -- `TestStyleGate` asserts that `emerge`,
`Makefile`, `debian/rules` and both test modules are in the gate's list.

The config is TOML, read with `tomllib`, which is Python 3.11+. An older
interpreter does not fail: it prints one line to stderr and then checks a
smaller set of files *successfully*. `make style` therefore refuses to run
on one at all, because a green result that quietly meant less is the failure
this project keeps finding by other names.

## 3. Filenames

Lowercase for everything this project names itself. The program is
`emerge`, extensionless and executable; tests are `test_*.py`.

The exception is a name a tool will not accept lowercased: `Makefile`,
`README.md`, `LICENSE`, and the `debian/` files native packaging dictates.

## ASCII in source

Source and comments are ASCII. Write `--` where prose would use an em dash,
and "section" for a section sign.

This governs the text the repository writes about itself, not the data the
software handles. Documentation may use typographic punctuation; so may
user-facing text in UI software, and anything that genuinely requires
Unicode.

**This project is excepted** and does not enable the check: it prints status
ticks, and that output lives in the source as string literals.


The commit-msg hook is `tools/hooks/commit-msg`, installed with `make hooks`.
It rejects generator attribution and a subject over 75 columns. It lives in
the tree rather than only in `.git/hooks` so that it is reviewable and
survives a clone; the copy that runs is installed from it.

## See also

- **`~/.claude/guidelines/code-style.md`** -- the source this file copies.
- **`project.md`** -- the design and the reasoning. It wins over this file
  where the two disagree.
- **`~/.claude/tools/style_gate.py`** -- the source of `tools/style_gate.py`.
- **`../situ/code-style.md`** -- the sibling Python project, carrying the
  same rules. Its `tools/lint_conventions.py` is one of the three checkers
  the shared gate was merged from.
