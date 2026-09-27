#!/usr/bin/env python3
"""Derive a GitHub Release's title, notes, and Latest flag from a tag.

Used by .github/workflows/release.yml on every `v*` tag push, and runnable
locally to preview what the workflow will publish:

    python3 scripts/release-notes.py title  v1.37.0
    python3 scripts/release-notes.py notes  v1.37.0
    python3 scripts/release-notes.py latest v1.37.0   # prints true / false

Most tags are ANNOTATED (v1.5.0 onward, with exceptions such as v1.37.0),
so the annotated tag's message is the first source; lightweight tags fall
through to the tagged COMMIT and the tracked docs/releases/<tag>.md.

Title, first match wins. Every source accepts the styles this repo has used,
with an optional "llm-ferry " / "release:" / "Release " prefix and a ":" or
"—" separator after the tag:
       llm-ferry v1.33.0 — Promote a client into a host      (tag message)
       v1.36.0: the schematron extraction lane runs on the host GPU
       release: v1.37.0 — Claude subscription lanes          (commit subject)
       Release v1.33.0: ferry migrate promotes a client into a host
     all normalised to "vX.Y.Z — <text>".
  1. the annotated tag's subject line;
  2. the commit subject;
  3. the H1 of docs/releases/<tag>.md when it names the tag
     ("# llm-ferry v1.35.0 — a dedicated schematron door");
  4. the bare tag name.
A source that does not name THIS tag (v1.35.0's tag message is
"ferry up --schematron: ...") is skipped rather than guessed at.

Notes, first non-empty wins:
  1. the annotated tag's message body (below its subject line, which is
     already the title), git trailers removed; a subject-only tag message
     has no body and falls through;
  2. docs/releases/<tag>.md verbatim;
  3. the commit body with git trailers (Claude-Session-Id: etc.) removed;
  4. the one-line `git log` subjects since the previous tag.

Latest is true only when the tag is the highest stable semver among all v*
tags, so re-running the workflow for an old tag never steals the marker.

Stdlib only.
"""
import re
import subprocess
import sys
from pathlib import Path

SEMVER = re.compile(r'^v(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$')
SEP = r'\s*(?:—|–|-|:)\s*'
TRAILER = re.compile(r'^[A-Za-z][A-Za-z0-9-]*: \S')


def parse_semver(tag):
    """(major, minor, patch, prerelease-or-None), or None if not v-semver."""
    m = SEMVER.match(tag)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)


def _title_from_line(tag, line):
    """Pull '<text>' out of a line shaped like '<prefix> <tag><sep><text>'."""
    pattern = (r'^(?:release:?\s+|llm-ferry\s+)?'
               + re.escape(tag) + SEP + r'(.+?)\s*$')
    m = re.match(pattern, line.strip(), re.IGNORECASE)
    if m and m.group(1).strip():
        return f'{tag} — {m.group(1).strip()}'
    return None


def derive_title(tag, subject, notes_text='', tag_subject=''):
    for line in (tag_subject, subject):
        title = _title_from_line(tag, line or '')
        if title:
            return title
    for line in (notes_text or '').splitlines():
        if line.startswith('# '):
            title = _title_from_line(tag, line[2:])
            if title:
                return title
            break
    return tag


def strip_trailers(body):
    """Drop the final paragraph when every line of it is a git trailer."""
    text = (body or '').strip()
    if not text:
        return ''
    paragraphs = re.split(r'\n\s*\n', text)
    while paragraphs:
        lines = [ln for ln in paragraphs[-1].splitlines() if ln.strip()]
        if lines and all(TRAILER.match(ln) for ln in lines):
            paragraphs.pop()
        else:
            break
    return '\n\n'.join(paragraphs).strip()


def derive_notes(tag, notes_text, body, log_subjects, previous_tag=None, tag_body=''):
    tag_notes = strip_trailers(tag_body)
    if tag_notes:
        return tag_notes + '\n'
    if (notes_text or '').strip():
        return notes_text.rstrip() + '\n'
    cleaned = strip_trailers(body)
    if cleaned:
        return cleaned + '\n'
    if log_subjects:
        head = f'Changes since {previous_tag}:' if previous_tag else 'Changes:'
        return head + '\n\n' + '\n'.join(f'- {s}' for s in log_subjects) + '\n'
    return f'Release {tag}.\n'


def previous_tag(tag, all_tags):
    """Highest stable v-semver tag strictly below `tag`, or None."""
    me = parse_semver(tag)
    if not me:
        return None
    lower = [t for t in all_tags
             if (v := parse_semver(t)) and v[3] is None and v[:3] < me[:3]]
    return max(lower, key=lambda t: parse_semver(t)[:3], default=None)


def is_latest(tag, all_tags):
    me = parse_semver(tag)
    if not me or me[3] is not None:
        return False
    stable = [parse_semver(t)[:3] for t in all_tags
              if parse_semver(t) and parse_semver(t)[3] is None]
    return all(me[:3] >= v for v in stable)


def _git(*args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def read_tag_message(tag, root):
    """(subject, body) of an ANNOTATED tag's message; ('', '') if lightweight.

    %(contents:*) on a lightweight tag would silently return the COMMIT's
    message, so gate on the ref's object type first. The body excludes any
    signature block.
    """
    ref = f'refs/tags/{tag}'
    if _git('for-each-ref', '--format=%(objecttype)', ref, cwd=root).strip() != 'tag':
        return '', ''
    subject = _git('for-each-ref', '--format=%(contents:subject)', ref, cwd=root).strip()
    body = _git('for-each-ref', '--format=%(contents:body)', ref, cwd=root)
    return subject, body


def main(argv):
    if len(argv) != 3 or argv[1] not in ('title', 'notes', 'latest'):
        sys.stderr.write('usage: release-notes.py title|notes|latest <tag>\n')
        return 2
    field, tag = argv[1], argv[2]
    root = Path(__file__).resolve().parent.parent
    commit = f'{tag}^{{commit}}'
    all_tags = _git('tag', '--list', 'v*', cwd=root).split()
    notes_path = root / 'docs' / 'releases' / f'{tag}.md'
    notes_text = notes_path.read_text() if notes_path.is_file() else ''
    tag_subject, tag_body = read_tag_message(tag, root)
    if field == 'latest':
        print('true' if is_latest(tag, all_tags) else 'false')
    elif field == 'title':
        subject = _git('log', '-1', '--format=%s', commit, cwd=root).strip()
        print(derive_title(tag, subject, notes_text, tag_subject))
    else:
        body = _git('log', '-1', '--format=%b', commit, cwd=root)
        prev = previous_tag(tag, all_tags)
        rng = f'{prev}..{commit}' if prev else commit
        subjects = [s for s in _git('log', '--format=%s', rng, cwd=root).splitlines() if s]
        sys.stdout.write(derive_notes(tag, notes_text, body, subjects, prev, tag_body))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
