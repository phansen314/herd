"""Fail CI when the suite skipped something it should have run.

A green run has to MEAN the tests ran. conftest skips the real-hook tests when
bash/jq/sqlite3 are missing — right on a contributor's machine, dangerous here,
where "0 failures" looks identical to a run that skipped them. Measured: dropping
jq alone skips 159 of 648, and nothing in the summary says so.

But `skipped == 0` is too blunt, because ONE skip is legitimate: tomllib is stdlib
from 3.11, templates degrade to a friendly error below that, and the 3.9 matrix
entry skips those tests on purpose. So this allows skips whose reason matches the
version gate and rejects every other reason.

Reasons, not counts: the count changes whenever a test is added, and a hardcoded
number would be updated by whoever the check next annoys.

Not a grep of pytest's output, either — the skip banner is suppressed by -q, so the
obvious `pytest -q | grep MISSING` check can never match. Discovered by trying to
make it fire. The JUnit report cannot be formatted away.
"""
import sys
import xml.etree.ElementTree as ET

# The ONLY skip reasons CI accepts, as substrings of the skip message.
#
# Everything absent from this list is treated as a hollow run — in particular
# "needs bash, jq, sqlite3", which is conftest saying the real-hook tests could not
# run at all. That is the case this script exists to catch.
ALLOWED = (
    # tomllib is stdlib from 3.11; templates degrade to a friendly error below that
    # and the 3.9 matrix entry skips those tests on purpose.
    "templates need Python 3.11+",
    # One test drives the real `kitten` binary, which the runners do not have. The
    # point of it is the REAL subprocess, so stubbing would delete the test.
    "drives the real `kitten` binary",
)


def main(path):
    root = ET.parse(path).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    total, skipped = suite.get("tests", "0"), suite.get("skipped", "0")

    bad = []
    for case in suite.iter("testcase"):
        for skip in case.findall("skipped"):
            reason = (skip.get("message") or "").strip()
            if not any(a in reason for a in ALLOWED):
                bad.append(f"{case.get('classname')}::{case.get('name')} — {reason}")

    print(f"ran {total} tests, {skipped} skipped")
    if bad:
        print(f"::error::{len(bad)} test(s) skipped for an unapproved reason — a green")
        print("::error::run must not be a hollow one. Offenders:")
        for b in bad[:20]:
            print(f"  {b}")
        return 1
    if int(skipped):
        print(f"note: {skipped} skip(s), all for approved reasons")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "results.xml"))
