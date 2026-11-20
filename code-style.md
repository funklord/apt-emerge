<!-- Copied from ~/.claude/guidelines/code-style.md by tool/sync.py.
     Edit the source, not this part: this project's own rules follow
     the end-of-copy line below. -->

# code-style.md

Code style for private projects. This file is the **source**. Every private
project except the one holding it carries a copy at its repo root, written
by `tool/sync.py` -- see *Keeping the copies in sync* at the end.

Vendored submodules, generated sources and attic/historical material are
exempt: they keep whatever their upstream or generator produces. Each
project names its own exempt paths in its part of the file.

## The three rules

1. **`snake_case`, not `camelCase`,** for identifiers this project defines.
2. **Tabs for indentation, spaces for alignment.**
3. **Lowercase filenames,** unless a tool demands otherwise.

Everything below is these three rules in detail, plus the exceptions that
are already settled. An exception not listed here is not yet settled: raise
it rather than deciding in passing.

## 1. Naming

`snake_case` for functions, variables, type names and fields.

This holds **even inside a toolkit whose own API is `camelCase`**. Call the
foreign API exactly as it is spelled (`setParent`, `addWidget`) -- that is
the API's name, not a violation. But names *you* introduce stay
`snake_case`; do not let the surrounding convention pull them across.

- **Name the thing, not its category**: `plan`, not `plan_struct` or
  `plan_result`.
- **No abbreviations that are not already vocabulary.** `observed`, not
  `obs`; `interface`, not `iff`. This matters most where an internal name
  escapes into something you cannot rename later -- a wire format, a config
  key, a CLI output, an on-disk path.
- **One word per concept, everywhere** -- the same word in the type name,
  the file path, the subcommand and the documentation. A synonym introduced
  for variety reads as a second concept.

### Prefixes, and visibility

Prefixes keep this project's symbols from colliding with a library's, so
they follow **visibility**, and the choice is judgement rather than a
mechanical rule:

- **Anything with more than small visibility carries the project prefix**
  -- the public API, and anything a linker or importer outside its own
  module can reach.
- **Module-private symbols are left unprefixed**, so that the absence of a
  prefix reads as "this does not leave the module."

The middle case decides itself on link safety, not taste. A symbol internal
by intent but still reaching the linker -- cross-file within a library, not
`static`, not part of the API -- is *not* private for this purpose: prefix
it. A deliberate parallel copy of a function in two libraries needs a
**distinct** name; the assumption that nothing will ever link both sides
fails later, at a call site that changed nothing, and names files you did
not touch.

Where a language enforces its own scheme, accept it, and say in the
project's part of this file that the toolchain is doing it:

- **Rust** -- `non_snake_case` and `non_camel_case_types` are on by
  default, so types are `PascalCase` and constants `SCREAMING_SNAKE_CASE`
  -- the toolchain's, not a choice. Package systems that demand kebab-case
  (Cargo crates, Debian packages) read back with their own spelling; do not
  invent a third by naming the directory differently from the package.
- **Python** -- a leading underscore (`_name`) is the language's private
  marker and stands in for "unprefixed" above.

## 2. Indentation and alignment

Indent structural nesting with **tab** characters, one tab per level. When
lining up tokens *within* a line -- continuation parameters under an open
paren, a block comment's `*` column, an aligned trailing comment -- use
**spaces**, after the indent tabs. Alignment is then relative to the shared
leading tabs, so it survives at any tab width. No tab width is prescribed;
the viewer decides.

```c
int thing_do(thing_t *thing, const char *name, size_t name_len,
              uint8_t *out, size_t out_cap) {
>---if (!thing) return ERR_MALFORMED;
>---return thing_write(thing, name, name_len, out, out_cap,
>---                    THING_DEFAULT_FLAGS);
}
```

(`>---` marks a tab; everything lining up under `(` is spaces.)

Never mix tabs and spaces *within* the indent itself. Tabs come first and
spaces after; the reverse, or an alternation, breaks at a different tab
width -- and in Python it is a syntax error.

### Settled exceptions

Divergence needs a technical reason. These are accepted:

- **Makefiles** -- a recipe line requires a literal tab, and the body of an
  `ifeq`/`ifdef` outside a recipe may be space-indented, since after a rule
  a tab-led line is read as a recipe. Both are correct in one file, so the
  gate does not apply the tab rule to Makefiles.
- **YAML** -- the spec forbids tabs for indentation. Use spaces.
- **Markdown** -- list continuation and code fences are space-indented by
  specification. Exempt.
- **Debian packaging files** -- exempt, for two reasons measured against
  dpkg. `debian/changelog` has a fixed layout with no tab in it:
  `dpkg-parsechangelog` calls a tab-indented change line "unrecognized"
  and loses the trailer if a tab precedes `--`. A deb822 continuation in
  `control` or `copyright` may lead with a space *or* a tab, but that
  whitespace is field syntax rather than indentation, and everything past
  it is alignment.
- **Go** -- `gofmt` emits tabs natively. Compliant already.
- **Vendored, generated and attic sources** -- exempt, per the header.

**Python**: PEP 8 prefers spaces, but the language's only hard rule is that
indentation must not be *ambiguous*, and tabs-then-spaces is unambiguous at
every tab width. Continuation lines inside brackets are not
indentation-significant at all. Never a space *before* a tab in leading
whitespace -- that is what raises `TabError`.

Anything else that seems to need spaces: signal it to the list in
`claude-guidelines`' `project.md`, follow the rule meanwhile, and it is
settled here in a pass rather than in whichever project met it first.

## 3. Filenames

**Lowercase, always**, for everything the project names itself:
`main_window.cpp`, not `MainWindow.cpp`.

**The separator follows what the name binds to**, which is a technical
difference rather than taste:

- **`snake_case` where the filename becomes an identifier** -- a source
  file, a header, a module. `desired_state.rs` *is* the module
  `desired_state`, and `desired-state.rs` cannot be a module at all, a
  hyphen not being legal in a Rust path; Python imports are the same. That
  is the language's requirement, and not negotiable where it applies.
- **`kebab-case` for prose** -- documentation, design notes, decision
  records. Nothing imports `code-style.md`, so no identifier is at stake,
  and kebab-case is what markdown and URLs settled on. (When this rule said
  `snake_case` for documentation too, 174 of 197 markdown names across the
  workspace were already kebab-case.)

Settled exceptions:

- **Names a tool will not accept lowercased** -- `Makefile`,
  `CMakeLists.txt`, `AndroidManifest.xml`, `Dockerfile`, `Cargo.toml`.
- **Root files with an established convention** -- `README.md`, `LICENSE`,
  `CHANGELOG.md`, `AUTHORS`, `VERSION`. The last is this workspace's own: a
  project that packages or ships a version tracks one, and the build reads
  it for the package version and for what the program prints, so the
  number lives in one place. `claude-guidelines` packages nothing and has
  none.
- **Package-system spellings** -- kebab-case where Cargo or Debian require
  it, which is the spelling prose uses, so a crate directory and the design
  note beside it agree by construction.

### Singular, unless somebody else standardised the plural

**Prefer the singular for a directory this project names itself**:
`helper/`, `doc/`, `fixture/`. The name says what kind of thing lives
there, not how many, and should not have to change when the count does.

Two exceptions, and they are not equal -- the same shape as the lowercase
rule, which yields first to `Makefile` because make will read nothing else,
and only then to `README.md` because the world settled it.

**First: a name a tool requires is not a name we choose.** It outranks the
singular as it outranks lowercase, needs no measurement, and the test is
whether something breaks when the name changes. It is open-ended: a tool
met tomorrow that demands a name gets the same answer, whether the name is
plural, singular, capitalised or none of those. Present here: **Cargo**
looks for `tests/`, `examples/` and `benches/`, and `cargo-fuzz` for
`fuzz_targets/`; **GitHub** requires `.github/workflows/`; **git** keeps
`hooks/`, hence `tool/hooks/`. **A foreign package's directory names are
the same exception one layer out**: the Android SDK's `platform-tools/` and
`build-tools/` are the SDK's spelling. A word-boundary rewrite of `tools/`
once reached into `platform-tools/`, the hyphen being a boundary, and every
adb target in four projects pointed at a binary that was not there -- so a
sweep skips compound names it does not own. Package-system spellings (Cargo
crates, Debian packages) under *Filenames* above are this same carve-out.

**Second: a plural an ecosystem has settled** -- a convention rather than a
requirement: nothing breaks, but a reader would be surprised. Cargo
workspaces keep members in `crates/`. **These need measuring**, and the
project's part of this file names what it measured against, so the next
reader does not reopen it.

Say which kind is being claimed: confusing them lands on whoever renames a
directory that looked like a convention and finds the build broken. And an
inventory name in `harmonization.md` moves only when its owner says so, in
a deliberate pass with each project's gates run against the result -- as
`tools/`, `docs/` and `docs/decisions/` became `tool/`, `doc/` and
`doc/decision/` -- never as a side effect of a style rule.

## ASCII in source

Source and comments are ASCII. Write `--` where prose would use an em dash,
and "section" for a section sign.

This governs **the text the repository writes about itself**, not the data
the software handles. Three exceptions, which are the rule's shape rather
than holes in it:

- **Documentation.** Markdown may use typographic punctuation.
- **User-facing text in UI software.** A tick a program prints is output,
  not prose -- `GREEN('gpg ')` is correct as it stands.
- **Anything that genuinely requires Unicode**: a fixture for a UTF-8
  parser, a terminal emulator's character tables, a font tool.

Where a project needs the rule enforced, `ascii_only` in `.style-gate.toml`
turns it on. In Python and in C/C++ it enforces exactly that shape -- ASCII
outside string literals, Unicode allowed inside -- reading Python with
`tokenize` and C and C++ with a scanner written for the purpose. Every
other language gets a whole-file byte check, and so does a file in those
two that will not lex: a file nobody can parse is not a file that has been
cleared.

**An exception wider than its reason is how a rule stops being enforced.**
The check was once whole-file for everyone, so a project printing two
status ticks switched it off, and an em dash arrived in a comment; in a Qt
tree that kept it off for its toolbar glyphs, 854 of 1114 non-ASCII
characters turned out to be prose in comments.

## Formatters

A formatter is allowed **only if it can be configured to honour the three
rules completely**. Configuration gaps are disqualifying: a formatter that
gets indentation right and alignment wrong rewrites the tree on somebody's
next save. So the decision is per tool, per project, and a real evaluation:

- If it can be made to comply, use it, and commit the config with a comment
  saying which setting is load-bearing and what happens without it.
- If it cannot, do not run it -- **not even ad hoc on a single file**. The
  failure is a silent conversion of files that were already correct,
  found later as a reverted commit rather than an error.
- If no existing tool fits and the rule is worth mechanising, write our
  own. A checker that only gates indentation is worth more than a formatter
  that reflows everything.

**Record the decision and the finding that produced it** in the project's
part of this file -- which tool, what failed, what would change the answer.
A verdict without its evidence gets re-litigated, and a tool that improves
later is never reconsidered because nobody remembers what was wrong with
it.

Naming and filename rules are review items, not automated ones.

## Precedence

Three layers, and they are not equals:

1. **The global guidelines** (`~/.claude/CLAUDE.md` and the files it
   imports) -- the source, and they win.
2. **The project's `project.md`** -- project-specific design and
   conventions.
3. **The project's `code-style.md`** -- this file, copied, and the
   project's own part after it.

A project file that disagrees with the source is **drift, not an
override**: fix it. A project that genuinely needs to diverge needs a
technical reason, and that is not a decision to make while working on
something else -- signal it to the list in `claude-guidelines`'
`project.md` and keep following the source meanwhile.

**When a conflict between layers actually comes up, stop and ask.** Do not
silently pick a winner, even the global one.

This precedence rule lives here and in the global guidelines only. It does
not belong in a `project.md`.

## Keeping the copies in sync

Each private project keeps `code-style.md` at its root, in two parts: this
source, verbatim, under a header naming it; then, after a line marking the
end of the copy, **the project's own part**, holding only what is genuinely
its own -- its exempt paths, formatter verdicts, language-specific notes
and tooling commands, under its own headings. `claude-guidelines` is the
exception: it holds the source at `guidelines/code-style.md`, and its root
file points here.

**`tool/sync.py` writes the copied part and never touches the project's
part**, the same way it spreads the gate. So a copy cannot drift, and a
project that wants a rule to read differently changes the source, through
the signal list, rather than its copy. Until 2026-11-20 copies were adapted
by hand and audited only for their headings, and most had drifted in prose
no audit could see.

**Keep a census out of this document.** A count written here is copied
into every tree and goes stale in all of them at once; state the rule and
the criterion that decides it, and keep the count where it can be
recounted once.

**This source is plain ASCII**, so that it is valid in a tree whose own
rules restrict the characters its files may contain. Keep it so.

**Re-read this source before writing a project's own part, or anything
about the rules**, rather than working from what was loaded at the start of
the session: text written against a stale source is drift being written.
**A copied part that differs from the source** -- a tree the spread has not
reached, a hand edit -- is put right with `sync.py --commit <tree>`, never
by editing the copy; `sync.py --check` lists any that differ. A project's
`project.md` may state the three rules in brief and point here; it does not
restate the precedence rule.

<!-- End of the copied part. What follows is this project's own. -->

## This project

Code style for this project -- `emerge`, the tests, the `Makefile` and the
`debian/` packaging.

Nothing is vendored.

### Naming: one stdlib-only script, no prefix

`snake_case` for functions, variables and attributes. The program is a
single stdlib-only Python script with no library surface, so no prefix is
used: a leading underscore (`_name`) is Python's own private marker and
stands in for "unprefixed" -- it reads as "this does not leave the module",
which here means "not part of what a reader of the CLI ever sees".

- **Prefer the plain descriptive name over the redundant one.** The script
  is clean of this today -- `print_merge_list` and `show_info` name a merge
  list and the `--info` block, which are the things themselves.
- **No abbreviations that are not already vocabulary.** This has teeth
  here for a specific reason: the program speaks a Portage dialect, and a
  name invented internally tends to surface in output a user reads
  alongside real `emerge` output. Where Portage has a word, use Portage's
  word.
- **One word per concept, everywhere** -- the same word in the function
  name, the option spelling, the message text and the man page.

### Indentation: continuation at module level

A continuation line takes tabs to its enclosing statement's level, then
spaces to the alignment column. A line at module level whose continuation
is aligned under an open paren therefore carries **no tab at all**, only
alignment spaces, and that is correct rather than an oversight.

### Settled exceptions, as they fall in this tree

- **Markdown** -- `project.md`, `README.md` and this file are exempt.
- **YAML** -- `.github/workflows/tests.yml` uses spaces. This is not drift
  to fix; a tab in it is a parse error.
- **Makefile recipe lines** -- `Makefile` and `debian/rules` are compliant
  by construction. Their variable continuations still align with spaces.
- **Debian packaging files** -- `debian/changelog` and the deb822 files
  (`control`, `copyright`). Both halves of the exception were measured
  against dpkg here.

### The conversion from 4-space indentation

This project was 4-space indented until 2026-08-04 and was converted in one
whitespace-only change: `emerge`, `test_emerge.py` and
`test_integration.py` together, 6424 lines rewritten with no line added or
removed.

The conversion was structural rather than arithmetic, which mattered:
roughly 600 lines here align continuation arguments under an open paren, at
columns that are not multiples of four. Dividing the leading whitespace by
four would have converted that alignment into indentation and destroyed it.
Instead each line's depth came from `tokenize`'s INDENT/DEDENT stack, with
everything past that depth preserved as spaces. Rows inside multi-line
string literals were left untouched, their leading whitespace being
content.

It was **verified rather than eyeballed**: `ast.dump()` of every file is
byte-identical before and after. That dump is position-free but carries
every string constant, so an identical dump proves neither the block
structure nor any literal moved. `make check` passes unchanged -- 378 unit
tests and 5 integration tests.

### Formatters: no autoformatter

`black` and `ruff format` rewrite tabs to spaces unconditionally and cannot
be configured out of it, so either would silently revert the conversion
above on the next save. `pycodestyle` would need W191 disabled.

**Do not run any of them, not even ad hoc on a single file.**

### The gate

What is mechanised instead is a checker: `tool/style_gate.py`, run by
`make style` and by a CI job of the same name. It is shared verbatim with
the sibling projects -- fix drift, do not edit the copy -- and it checks
indentation, trailing whitespace, carriage returns and the final newline.
Naming and filenames stay review items, as the source says they are.

For Python it asks the stronger of the two available questions. A line rule
can only ask whether tabs precede spaces; the gate instead converts the
file with a `tokenize`-driven fixer and compares *tab counts* per line, so
a line indented to the wrong depth is caught as well. That comparison never
mentions a tab width -- the conversion above is what a width was needed
for, and it is over. `tool/style_gate.py fix` writes the conversion, and
refuses to write one that would change the file's AST.

#### `.style-gate.toml`

Short, and shorter than it was. It named `emerge` because the gate selected
files by suffix and a command has none -- so the program itself was the one
file it never opened. That is fixed in the tool now: an extensionless file
starting with `#!` is a program and is in scope by itself. `debian/rules`
came back the same way.

What remains is `Makefile`, which has neither a suffix nor a shebang.
Naming it is not merely a relaxation of the tab rule either way: without
the line it is not checked **at all**, losing the trailing-whitespace and
final-newline checks with it. The shipped default leaves Makefiles out
because an `ifeq` body must be space-indented -- a tab there reads as a
recipe line -- and this Makefile has none, so the rule costs nothing today.
Add one and that changes; raise it rather than deleting the line to quieten
the gate.

**A config that is present is applied exactly, or the run fails.** The gate
enforces that itself now: an unreadable file, a directory or broken symlink
where the config should be, invalid TOML, an unknown key, or a value of the
wrong type all exit 2 with a diagnosis. The last is the one worth naming --
`indent_names = "emerge"`, quotes where brackets belong, is valid TOML, and
a `set()` of a string is a set of its characters, so the name matches
nothing and the scope silently shrinks. `TestStyleGate` pins these, because
this project is where the failure was found.

### Filenames in this tree

The program is `emerge`, extensionless and executable; tests are
`test_*.py`. Python is where the identifier half of the separator rule
bites here, since the filename *is* the module path and a hyphen is not
legal in one: `test_resolver.py` could not be imported under a kebab-case
name. Kebab-case for prose is also the spelling Debian requires of the
package name, so the two agree by construction rather than by coincidence.
Both halves already hold here, so nothing has to move.

The tool-demanded and conventional names here are `Makefile`, `README.md`,
`LICENSE`, `VERSION`, and the `debian/` files native packaging dictates.

### ASCII in source: this project enables the check

**This project enables the check**, and did not used to. `ascii_only` was
all-or-nothing: the program prints two status ticks which live in the
source as `f`-strings, so the only way to allow those was to allow an em
dash in a comment as well -- and one duly arrived, found by grepping for
non-ASCII rather than by any gate.

The gate distinguishes the two for Python now, which is what the rule
always said: a tick a program prints is output, not prose. Inside a string
literal Unicode passes; anywhere else -- comments, identifiers -- it does
not.

### The commit-msg hook

The commit-msg hook is `tool/hooks/commit-msg`, installed with
`make hooks`. It rejects generator attribution, a subject over 75 columns,
and body prose over 75 columns. It lives in the tree rather than only in
`.git/hooks` so that it is reviewable and survives a clone; the copy that
runs is installed from it.

The body limit was stated long before anything checked it, and only the
subject was checked -- so a body line at 76 columns went through while a
subject at 76 was refused.

What it deliberately does not reject, in three groups.

**Three names are spared**, so a message may say where the shared tooling
comes from: the directory `.claude`, the file `CLAUDE.md`, and
`claude-guidelines`, the repository the guidelines live in. The ban is on
crediting a generator and none of the three is a spelling of that. Only the
names are neutralised, never the token around them -- a vendor word at the
end of a path under the tree is still refused.

**What git is about to discard**: comment lines, and the diff that
`git commit -v` puts below the scissors line. Reading those refused commits
over text that never reaches the message -- the hook's own diff contains
its own pattern list, so it rejected every commit that edited it.

**Three shapes the length check exempts**, each because wrapping it is the
actual mistake rather than a concession: a *trailer*, since git parses the
block a line at a time and a broken `Link:` stops being a trailer at all; a
line holding a *url*, which no longer works once it is split; and an
*indented* line, which is how a message quotes a compiler error or a stack
trace, where reflowing what you are quoting corrupts the one thing it was
included for. It cannot tell prose opening `Note:` from a trailer, so it
forgives that -- the wrong way round would refuse a real trailer, and that
is the expensive error.

### See also

- **`project.md`** -- the design and the reasoning. It wins over this file
  where the two disagree.
- **`../situ/code-style.md`** -- the sibling Python project, carrying the
  same rules. Its `tool/lint_conventions.py` is one of the three checkers
  the shared gate was merged from.
