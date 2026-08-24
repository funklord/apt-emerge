<!-- The three rules and their detail are copied from
     ~/.claude/guidelines/code-style.md -- the source. Keep in sync; fix
     drift the moment you notice it. -->

# code-style.md

Code style for this project -- `emerge`, the tests, the `Makefile` and the
`debian/` packaging.

**The global source**, `~/.claude/guidelines/code-style.md`, applies to
every private project and sits above both this file and `project.md`. Where
either disagrees with it, that is **drift to fix, not a local override**. A
genuine divergence needs a technical reason and is signalled to the list in
`claude-guidelines`' `project.md` rather than decided in passing -- and
when a conflict actually comes up, stop and ask instead of picking a
winner.

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

- **Prefer the plain descriptive name over the redundant one.** Name the
  thing, not its category: `plan`, not `plan_struct` or `plan_result`. The
  script is clean of this today -- `print_merge_list` and `show_info` name
  a merge list and the `--info` block, which are the things themselves.
- **No abbreviations that are not already vocabulary.** This has teeth
  here for a specific reason: the program speaks a Portage dialect, and a
  name invented internally tends to surface in output a user reads
  alongside real `emerge` output. Where Portage has a word, use Portage's
  word.
- **One word per concept, everywhere** -- the same word in the function
  name, the option spelling, the message text and the man page.

### Prefixes, and visibility

Prefixes exist to keep this project's symbols from colliding with a
library's. So they follow **visibility**, and the choice is a matter of
judgement rather than a mechanical rule:

- **Anything with more than small visibility carries the project prefix** --
  the public API, and anything a linker or importer outside its own module
  can reach.
- **Module-private symbols are left unprefixed**, precisely so that the
  absence of a prefix reads as "this does not leave the module."

The middle case decides itself on link safety, not on taste. A symbol that
is internal by intent but still reaches the linker -- cross-file within a
library, not `static`, not part of the API -- is *not* private for this
purpose. Prefix it. A deliberate parallel copy of a function in two
libraries needs a **distinct** name, not the same name in both on the
assumption that nothing will ever link both sides; that assumption fails
later, at a call site that changed nothing, and names files you did not
touch.

Where a language enforces its own scheme, accept it rather than fight it,
and say in the project's copy that the toolchain is doing it:

- **Rust** -- `non_snake_case` and `non_camel_case_types` are on by default,
  so types are `PascalCase` and constants `SCREAMING_SNAKE_CASE`. That is
  the toolchain's, not a choice. Package systems that demand kebab-case
  (Cargo crate names, Debian package names) likewise read back with their
  own spelling; do not invent a third by naming the directory differently
  from the package.
- **Python** -- a leading underscore (`_name`) is the language's private
  marker and stands in for "unprefixed" above.

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
not listed is not yet settled: signal it to the list in
`claude-guidelines`' `project.md` rather than deciding in passing.

- **Markdown** -- list continuation and code fences are space-indented by
  specification. `project.md`, `README.md` and this file are exempt.
- **YAML** -- the spec forbids tabs for indentation outright, so
  `.github/workflows/tests.yml` uses spaces. This is not drift to fix; a
  tab in it is a parse error.
- **Makefile recipe lines** -- `make` requires a literal tab, so `Makefile`
  and `debian/rules` are compliant by construction. Their variable
  continuations still align with spaces.
- **Debian packaging files** -- `debian/changelog` and the deb822 files
  (`control`, `copyright`), for opposite reasons. The changelog's layout is
  fixed and a tab is not part of it: `dpkg-parsechangelog` calls a
  tab-indented change line "unrecognized" and loses the `--` trailer
  outright if a tab precedes it. A deb822 continuation *does* accept a
  leading tab -- `deb822(5)` allows SPACE or TAB and dpkg round-trips
  either -- but that leading whitespace is field syntax rather than
  indentation, so the rule does not reach it and what follows is alignment.
  Both measured against dpkg here.

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

### `.style-gate.toml`

Short, and shorter than it was. It named `emerge` because the gate selected
files by suffix and a command has none -- so the program itself was the one
file it never opened. That is fixed in the tool now: an extensionless file
starting with `#!` is a program and is in scope by itself. `debian/rules`
came back the same way.

What remains is `Makefile`, which has neither a suffix nor a shebang. Naming
it is not merely a relaxation of the tab rule either way: without the line
it is not checked **at all**, losing the trailing-whitespace and final-
newline checks with it. The shipped default leaves Makefiles out because an
`ifeq` body must be space-indented -- a tab there reads as a recipe line --
and this Makefile has none, so the rule costs nothing today. Add one and
that changes; raise it rather than deleting the line to quieten the gate.

**A config that is present is applied exactly, or the run fails.** The gate
enforces that itself now: an unreadable file, a directory or broken symlink
where the config should be, invalid TOML, an unknown key, or a value of the
wrong type all exit 2 with a diagnosis. The last is the one worth naming --
`indent_names = "emerge"`, quotes where brackets belong, is valid TOML, and
a `set()` of a string is a set of its characters, so the name matches
nothing and the scope silently shrinks. `TestStyleGate` pins these, because
this project is where the failure was found.

## 3. Filenames

**Lowercase, always**, for everything this project names itself. The
program is `emerge`, extensionless and executable; tests are `test_*.py`.

**The separator follows what the name binds to**, and the two cases are a
technical difference rather than a matter of taste:

- **`snake_case` where the filename becomes an identifier** -- a source
  file, a module. Python is where that bites here, since the filename *is*
  the module path and a hyphen is not legal in one: `test_resolver.py`
  could not be imported under a kebab-case name.
- **`kebab-case` for prose** -- documentation, design notes, decision
  records. Nothing imports `code-style.md`, so no identifier is at stake,
  and kebab-case is what markdown and URLs settled on long ago. It is also
  the spelling Debian requires of the package name, so the two agree by
  construction rather than by coincidence.

The rule used to say `snake_case` for prose too, and every private project
was quietly ignoring it. It was rewritten after measuring all fourteen
trees: of 197 tracked markdown basenames, 174 were already kebab-case. Both
halves already hold here, so nothing has to move.

The exception is a name a tool will not accept lowercased: `Makefile`,
`README.md`, `LICENSE`, `VERSION`, and the `debian/` files native packaging
dictates.

### Singular, unless somebody else standardised the plural

**Prefer the singular for a directory this project names itself.** `helper/`
rather than `helpers/`, `doc/` rather than `docs/`, `fixture/` rather than
`fixtures/`. The name says what kind of thing lives there, not how many;
one of them and forty of them go in the same place, and the directory
should not have to be renamed when the count changes.

There are two exceptions, and they are not equal. This is the same shape
as the lowercase rule above, which yields first to `Makefile` because make
will not read anything else, and only then to `README.md` because the world
settled it.

**First: a name a tool requires is not a name we choose.** It outranks the
singular exactly as it outranks lowercase, it needs no measurement and no
argument, and the test is whether something breaks when the name changes.
This is a *technical* fact, so it is open-ended rather than a list -- a
tool met tomorrow that demands a name gets the same answer, whether the
name it demands is plural, singular, capitalised or none of those.

Present here: **Cargo** looks for `tests/`, `examples/` and `benches/` by
those exact names, and `cargo-fuzz` for `fuzz_targets/`. **GitHub**
requires `.github/workflows/`. **git** keeps `hooks/`, which is why
`tools/hooks/` is spelled that way.

**Second: a plural an ecosystem has settled**, which is a convention rather
than a requirement -- nothing breaks, but a reader would be surprised by
the singular. Cargo workspaces conventionally keep members in `crates/`,
and that is this kind rather than the first. **These need measuring**, and
the project's copy names what it was measured against, so the next reader
does not reopen it.

Where the two are confused, the cost lands on whoever renames a directory
because it looked like a convention and finds the build no longer works.
So say which kind is being claimed.

**This rule does not reach the settled inventory.** Three canonical names in
`harmonization.md` are plural -- `tools/`, `docs/` and `docs/decisions/` --
and they stay until the copyright holder says otherwise, because renaming
them is a cross-project rewrite rather than a spelling change. Measured
before this was written: the decision records are cited by path 270 times in
netcfgd and 95 times in situ, and `tools/` is named as a path 161 times in
four projects alone, besides `sync.py`, every Makefile's hook target and the
`~/.claude/tools/` the copies are spread from. An inventory entry is a name
other things point at, which is exactly what makes it expensive and exactly
what makes it worth having.

## ASCII in source

Source and comments are ASCII. Write `--` where prose would use an em dash,
and "section" for a section sign.

This governs the text the repository writes about itself, not the data the
software handles. Documentation may use typographic punctuation; so may
user-facing text in UI software, and anything that genuinely requires
Unicode.

**This project enables the check**, and did not used to. `ascii_only` was
all-or-nothing: the program prints two status ticks which live in the
source as `f`-strings, so the only way to allow those was to allow an em
dash in a comment as well -- and one duly arrived, found by grepping for
non-ASCII rather than by any gate.

The gate distinguishes the two for Python now, which is what the rule
always said: a tick a program prints is output, not prose. Inside a string
literal Unicode passes; anywhere else -- comments, identifiers -- it does
not. Other languages keep the whole-file byte check, having no tokenizer
in the gate, and a Python file that will not tokenise falls back to it too,
because a file nobody can parse is not a file that has been cleared.

## Formatters

A formatter is allowed **only if it can be configured to honour the three
rules completely**. Configuration gaps are disqualifying, not something to
work around: a formatter that gets indentation right and alignment wrong
will rewrite the tree on somebody's next save.

So the decision is per tool, per project, and it is a real evaluation:

- If it can be made to comply, use it, and commit the config with a comment
  saying which setting is load-bearing and what happens without it.
- If it cannot, do not run it -- **not even ad hoc on a single file**. The
  failure mode is a silent conversion of files that were already correct,
  discovered later as a reverted commit rather than an error.
- If no existing tool fits and the rule is worth mechanising, write our
  own. A checker that only gates indentation is worth more than a formatter
  that reflows everything.

**Record the decision and the finding that produced it** in the project's
copy of this file -- which tool, what specifically failed, what would change
the answer. A verdict without its evidence gets re-litigated, and a tool
that improves later never gets reconsidered because nobody remembers what
was actually wrong with it.

Naming and filename rules are review items, not automated ones.

## Precedence

Three layers, and they are not equals:

1. **The global guidelines** (`~/.claude/CLAUDE.md` and the files it
   imports) -- the source, and they win.
2. **The project's `project.md`** -- project-specific design and conventions.
3. **The project's `code-style.md`** -- this file, copied.

A project copy that disagrees with the source is **drift, not an
override**: fix it. A project that genuinely needs to diverge needs a
technical reason, and that is not a decision to make while working on
something else -- signal it to the list in `claude-guidelines`'
`project.md` and keep following the source meanwhile.

**When a conflict between layers actually comes up, stop and ask.** Do not
silently pick a winner, even the global one.

This precedence rule lives here and in the global guidelines only. It does
not belong in a `project.md`.

## Keeping the copies in sync

Each private project keeps a copy of this file at its repo root -- except
the one this file lives in. `claude-guidelines` holds the source at
`guidelines/code-style.md`, and a copy beside it would be the same document
twice in one repository with nothing to keep the two honest; its root
`code-style.md` says so and points here. Every other private project carries
a copy, opening with a header that names the source:

```markdown
<!-- Copied from ~/.claude/guidelines/code-style.md -- the source. Keep in
     sync; fix drift the moment you notice it. -->
```

Below the copied rules, a project adds only what is genuinely its own: its
exempt paths, its formatter verdicts, its language-specific notes, its
tooling commands.

**This source is deliberately plain ASCII** -- no em dashes, no section
signs, no arrows -- so that a copy can be byte-verbatim in every project,
including one whose own rules restrict the characters its files may
contain. Keep it that way when editing: a typographic character introduced
here becomes a transliteration problem in every repository that carries a
copy.

Where a copy must still be adapted, **"do not diverge" means semantically
identical, not byte-identical**: a project transliterating to satisfy its
own character-set rule, or renumbering a heading to fit its own structure,
is that project's rule working correctly, **not drift, and not something to
reconcile back**. What must match is every rule and every exception, in
substance.

**`sync.py --check` now reports the copies that have fallen behind**, which
until 2026-08-24 nothing did. The three files spread verbatim were checked on
every run and this one -- the document that says what the rules are -- was
checked by nobody, so drift was indistinguishable from the adaptation the
section above asks for. It found real losses: four copies had dropped
*Precedence*, the section saying this source outranks them; three had dropped
*Formatters*, whose rule was paid for by a formatter rewriting committed
files; one had dropped *ASCII in source*; and seven had dropped this very
section, which is the one that would have told a reader to look.

It asks the weaker question that can actually be answered -- does the copy
still carry a section for every section here -- and it never writes this
file, because overwriting a copy would delete the part the project owns. A
heading that *extends* one of these satisfies it, since that is what
recording a project's own formatter verdict looks like.

**If you notice a copy diverging from the source, reconcile it as soon as
you notice** -- do not leave it for later and do not work around it. If the
divergence looks deliberate rather than stale, that is the conflict case
above: ask.

Noticing requires looking. **Re-read this source before writing or
reconciling any project's copy**, rather than working from what was loaded
at the start of the session -- it may have changed since, and a copy
reconciled against a stale source is drift being written rather than
fixed.

The project's `project.md` may state the three rules in brief and point
here for the detail. It does not restate the precedence rule.

## The commit-msg hook

The commit-msg hook is `tools/hooks/commit-msg`, installed with `make hooks`.
It rejects generator attribution and a subject over 75 columns. It lives in
the tree rather than only in `.git/hooks` so that it is reviewable and
survives a clone; the copy that runs is installed from it.

Two things it deliberately does not reject. The directory `.claude` and the
file `CLAUDE.md` are names, so a message may say where the shared tooling
comes from -- the ban is on crediting a generator, and neither spelling is
one. And it ignores what git is about to discard: comment lines, and the
diff that `git commit -v` puts below the scissors line. Reading those
refused commits over text that never reaches the message -- the hook's own
diff contains its own pattern list, so it rejected every commit that
edited it.

## See also

- **`~/.claude/guidelines/code-style.md`** -- the source this file copies.
- **`project.md`** -- the design and the reasoning. It wins over this file
  where the two disagree.
- **`~/.claude/tools/style_gate.py`** -- the source of `tools/style_gate.py`.
- **`../situ/code-style.md`** -- the sibling Python project, carrying the
  same rules. Its `tools/lint_conventions.py` is one of the three checkers
  the shared gate was merged from.
