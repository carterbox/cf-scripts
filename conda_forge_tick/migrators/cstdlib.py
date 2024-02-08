import os
import re

from conda_forge_tick.migrators.core import MiniMigrator
from conda_forge_tick.migrators.libboost import _slice_into_output_sections

pat_stub = re.compile(r"(c|cxx|fortran)_compiler_stub")
rgx_idt = r"(?P<indent>\s*)-\s*"
rgx_pre = r"(?P<compiler>\{\{\s*compiler\([\"\']"
rgx_post = r"[\"\']\)\s*\}\})"
rgx_sel = r"\s*(?P<selector>\#\s+\[[\w\s()<>!=.,\-\'\"]+\])?"

pat_compiler_c = re.compile(
    "".join([rgx_idt, rgx_pre, "(m2w64_)?c", rgx_post, rgx_sel])
)
pat_compiler_other = re.compile(
    "".join([rgx_idt, rgx_pre, "(m2w64_)?(cxx|fortran)", rgx_post, rgx_sel])
)
pat_compiler = re.compile(
    "".join([rgx_idt, rgx_pre, "(m2w64_)?(c|cxx|fortran)", rgx_post, rgx_sel])
)
pat_stdlib = re.compile(r".*\{\{\s*stdlib\([\"\']c[\"\']\)\s*\}\}.*")


def _process_section(name, attrs, lines):
    """
    Migrate requirements per section.

    We want to migrate as follows:
    - if there's _any_ `{{ stdlib("c") }}` in the recipe, abort (consider it migrated)
    - if there's `{{ compiler("c") }}` in build, add `{{ stdlib("c") }}` in host
    - where there's no host-section, add it
    """
    outputs = attrs["meta_yaml"].get("outputs", [])
    global_reqs = attrs["meta_yaml"].get("requirements", {})
    if name == "global":
        reqs = global_reqs
    else:
        filtered = [o for o in outputs if o["name"] == name]
        if len(filtered) == 0:
            raise RuntimeError(f"Could not find output {name}!")
        reqs = filtered[0].get("requirements", {})

    build_reqs = reqs.get("build", set()) or set()
    global_build_reqs = global_reqs.get("build", set()) or set()

    # either there's a compiler in the output we're processing, or the
    # current output has no build-section but relies on the global one
    needs_stdlib = any(pat_stub.search(x or "") for x in build_reqs)
    needs_stdlib |= not bool(build_reqs) and any(
        pat_stub.search(x or "") for x in global_build_reqs
    )

    if not needs_stdlib:
        # no change
        return lines

    line_build = line_compiler_c = line_compiler_other = 0
    line_host = line_run = line_constrain = line_test = 0
    indent_c = selector_c = indent_other = selector_other = ""
    for i, line in enumerate(lines):
        if re.match(r".*build:.*", line):
            # always update this, as requirements.build follows build.{number,...}
            line_build = i
        elif pat_compiler_c.search(line):
            line_compiler_c = i
            indent_c = pat_compiler_c.match(line).group("indent")
            selector_c = pat_compiler_c.match(line).group("selector") or ""
        elif pat_compiler_other.search(line):
            line_compiler_other = i
            indent_other = pat_compiler_other.match(line).group("indent")
            selector_other = pat_compiler_other.match(line).group("selector") or ""
        elif re.match(r".*host:.*", line):
            line_host = i
        elif re.match(r".*run:.*", line):
            line_run = i
        elif re.match(r".*run_constrained:.*", line):
            line_constrain = i
        elif re.match(r".*test:.*", line):
            line_test = i
            # ensure we don't read past test section (may contain unrelated deps)
            break

    # in case of several compilers, prefer line, indent & selector of c compiler
    line_compiler = line_compiler_c or line_compiler_other
    indent = indent_c or indent_other
    selector = selector_c or selector_other
    if indent == "":
        # no compiler in current output; take first line of section as reference (without last \n);
        # ensure it works for both global build section as well as for `- name: <output>`.
        indent = (
            re.sub(r"^([\s\-]*).*", r"\1", lines[0][:-1]).replace("-", " ") + " " * 4
        )

    to_insert = indent + '- {{ stdlib("c") }}' + selector + "\n"
    if line_build == 0:
        # no build section, need to add it
        to_insert = indent[:-2] + "build:\n" + to_insert

    # if there's no build section, try to insert (in order of preference)
    # before the sections for host, run, run_constrained, test
    line_insert = line_host or line_run or line_constrain or line_test
    if not line_insert:
        raise RuntimeError("Don't know where to insert build section!")
    if line_compiler:
        # by default, we insert directly after the compiler
        line_insert = line_compiler + 1

    return lines[:line_insert] + [to_insert] + lines[line_insert:]


class StdlibMigrator(MiniMigrator):
    def filter(self, attrs, not_bad_str_start=""):
        lines = attrs["raw_meta_yaml"].splitlines()
        already_migrated = any(pat_stdlib.search(line) for line in lines)
        has_compiler = any(pat_compiler.search(line) for line in lines)
        # filter() returns True if we _don't_ want to migrate
        return already_migrated or not has_compiler

    def migrate(self, recipe_dir, attrs, **kwargs):
        outputs = attrs["meta_yaml"].get("outputs", [])

        fname = os.path.join(recipe_dir, "meta.yaml")
        if os.path.exists(fname):
            with open(fname) as fp:
                lines = fp.readlines()

            new_lines = []
            sections = _slice_into_output_sections(lines, attrs)
            for name, section in sections.items():
                # _process_section returns list of lines already
                new_lines += _process_section(name, attrs, section)

            with open(fname, "w") as fp:
                fp.write("".join(new_lines))
