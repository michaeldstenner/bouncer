# Vendored from micro-yaml v1.0.0 — do not edit here.
#   source: ../../micro-yaml/micro_yaml.py
#   drift : cd ~/Documents/Code/micro-yaml && ./check-vendored ../bouncer/bouncer/yaml.py
# Fix bugs upstream, then re-run `./vendor ../bouncer/bouncer/yaml.py`.
"""MicroYAML — a single-file, zero-dependency YAML subset parser.

Copy this whole file into your project. It imports only `re` from the
standard library and defines one class, so it works equally well pasted
into a one-file script or dropped into a package as a module.

    from micro_yaml import MicroYAML
    data = MicroYAML().load(text)
    docs = MicroYAML().load_all(text)      # multi-document

WHAT IT SUPPORTS ("Tier 3"): block mappings and sequences, nesting by
indentation, comments, single/double quoted scalars, int/float/bool/null
scalars, flow style (`[1, 2]`, `{a: 1}`), literal and folded block
scalars with chomping, and `---` document separators.

WHAT IT REFUSES: anchors (`&x`), aliases (`*x`), merge keys and explicit
tags (`!!str`) raise MicroYAMLError rather than parsing to something
plausible but wrong. Quote the value if you meant a literal `&`, `*` or
`!` at the start of a scalar.

WHAT IT DELIBERATELY GETS "WRONG" (vs PyYAML): sexagesimals and
timestamps stay strings. `12:30` is the string "12:30", not 750, and
`2026-07-28` is a string, not a date. Those YAML 1.1 conversions cause
more bugs than they solve; see README.

It raises MicroYAMLError on input it cannot represent, rather than
silently returning partial data. That is the whole point of using it for
configuration.
"""
import re

__version__ = "1.0.0"


class MicroYAMLError(ValueError):
    """Malformed, ambiguous, or unsupported YAML."""


class MicroYAML:
    _INT = re.compile(r'^[-+]?[0-9][0-9_]*$')
    _HEX = re.compile(r'^[-+]?0[xX][0-9a-fA-F_]+$')
    # A dot is required before an exponent: YAML 1.1 says `1e3` is a
    # string, and PyYAML agrees. Matching that avoids surprise floats.
    _FLOAT = re.compile(
        r'^[-+]?(\.[0-9]+|[0-9][0-9_]*\.[0-9]*)([eE][-+]?[0-9]+)?$'
    )
    _SPECIAL = {
        'true': True, 'yes': True, 'on': True,
        'false': False, 'no': False, 'off': False,
        'null': None, '~': None, '': None,
        '.inf': float('inf'), '-.inf': float('-inf'),
        '.nan': float('nan'),
    }
    _BLOCK_HEADERS = ('|', '>', '|-', '>-', '|+', '>+')

    def __init__(self):
        self.lines = []
        self.line_idx = 0

    # -- public ----------------------------------------------------
    def load(self, text):
        """Parse the first document, or None if there is none."""
        docs = self.load_all(text)
        return docs[0] if docs else None

    def load_all(self, text):
        """Parse every `---`-separated document into a list."""
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        parts = re.split(
            r'^---' if text.startswith('---') else r'\n---',
            text, flags=re.MULTILINE,
        )
        results = []
        for part in parts:
            if not part.strip():
                continue
            part = re.split(r'^\.\.\.', part, flags=re.MULTILINE)[0]
            self.lines = part.splitlines()
            self.line_idx = 0
            res = self._parse_block(0)
            if res is not None:
                results.append(res)
        return results

    # -- quote-aware scanning --------------------------------------
    # Splitting on a bare character is needed in three places: stripping
    # a trailing comment, separating a key from its value, and splitting
    # flow collections. Doing it naively corrupts any scalar containing
    # `#`, `:` or `,` inside quotes -- silently, which is the worst way
    # to be wrong. One scanner serves all three.
    @staticmethod
    def _find_bare(text, char, need_space=False):
        """Index of `char` outside quotes and brackets, else -1.

        `need_space` implements YAML's rule that a key separator is
        `: ` -- colon then whitespace or end of line. Without it,
        `- db_data:/data` (a docker-compose volume) parses as the
        mapping {'db_data': '/data'} instead of the plain string it is.
        """
        quote, depth = None, 0
        for i, ch in enumerate(text):
            if quote:
                if ch == quote:
                    quote = None
            elif ch in '"\'':
                quote = ch
            elif ch in '[{':
                depth += 1
            elif ch in ']}':
                depth -= 1
            elif ch == char and depth == 0:
                if need_space and i + 1 < len(text) \
                        and text[i + 1] not in ' \t':
                    continue
                return i
        return -1

    @classmethod
    def _strip_comment(cls, line):
        """Drop a trailing `# ...`, ignoring `#` inside quotes.

        A `#` only starts a comment at the start of the line or after
        whitespace, so `a: red#notacomment` keeps its value -- same rule
        PyYAML uses.
        """
        quote = None
        for i, ch in enumerate(line):
            if quote:
                if ch == quote:
                    quote = None
            elif ch in '"\'':
                quote = ch
            elif ch == '#' and (i == 0 or line[i - 1] in ' \t'):
                return line[:i].rstrip()
        return line.rstrip()

    # -- scalars ---------------------------------------------------
    def _parse_scalar(self, val):
        val = val.strip()
        low = val.lower()
        if low in self._SPECIAL:
            return self._SPECIAL[low]
        if val[0] in '"\'':
            if len(val) < 2 or val[-1] != val[0]:
                raise MicroYAMLError(
                    f"unterminated quoted scalar: {val!r}")
            inner = val[1:-1]
            if val[0] == '"':
                for k, v in (('\\n', '\n'), ('\\t', '\t'),
                             ('\\"', '"'), ('\\\\', '\\')):
                    inner = inner.replace(k, v)
            else:
                inner = inner.replace("''", "'")
            return inner
        if val[0] in '&*!':
            kind = {'&': 'anchors', '*': 'aliases',
                    '!': 'tags'}[val[0]]
            raise MicroYAMLError(
                f"{kind} are not supported: {val!r} "
                f"(quote it if you meant a literal {val[0]!r})")
        if self._HEX.match(val):
            return int(val.replace('_', ''), 16)
        if self._INT.match(val):
            return int(val.replace('_', ''))
        if self._FLOAT.match(val):
            return float(val.replace('_', ''))
        return val

    # -- blocks ----------------------------------------------------
    def _peek(self):
        """(indent, content) of the next significant line, or None."""
        i = self.line_idx
        while i < len(self.lines):
            line = self.lines[i]
            if line.strip() and not line.lstrip().startswith('#'):
                return self._get_indent(line), line.strip()
            i += 1
        return None

    @staticmethod
    def _get_indent(line):
        if not line.strip():
            return -1
        return len(line) - len(line.lstrip())

    def _parse_block(self, current_indent):
        result = None
        while self.line_idx < len(self.lines):
            line = self.lines[self.line_idx]
            if not line.strip() or line.lstrip().startswith('#'):
                self.line_idx += 1
                continue
            indent = self._get_indent(line)
            if indent < current_indent:
                break
            content = self._strip_comment(line.strip())
            if not content:
                self.line_idx += 1
                continue
            if content == '-' or content.startswith('- '):
                # A sequence cannot continue a mapping at the same
                # level; hand the line back to the caller instead of
                # appending to a dict and raising AttributeError.
                if isinstance(result, dict):
                    break
                if result is None:
                    result = []
                self._parse_seq_item(result, content, indent)
            else:
                colon = self._find_bare(content, ':', need_space=True)
                if colon < 0:
                    raise MicroYAMLError(
                        f"expected 'key: value' or '- item', got "
                        f"{content!r}")
                if isinstance(result, list):
                    break
                if result is None:
                    result = {}
                self._parse_map_entry(result, content, colon, indent)
        return result

    def _parse_seq_item(self, result, content, indent):
        val = content[1:].strip()
        self.line_idx += 1
        if not val:
            result.append(self._parse_block(indent + 1))
        elif val.startswith(('[', '{')):
            result.append(self._parse_flow(val))
        elif (val.startswith('- ')
              or self._find_bare(val, ':', need_space=True) >= 0):
            # "- key: 1" and "- - 1" both mean "a nested node starts on
            # this line". Rewrite the line without the dash, re-indented,
            # and let the normal block parser handle it.
            saved = self.line_idx - 1
            original = self.lines[saved]
            self.lines[saved] = ' ' * (indent + 2) + val
            self.line_idx = saved
            result.append(self._parse_block(indent + 2))
            self.lines[saved] = original
        else:
            result.append(self._parse_scalar(val))

    def _parse_map_entry(self, result, content, colon, indent):
        key = self._parse_scalar(content[:colon].strip())
        val = content[colon + 1:].strip()
        self.line_idx += 1
        if val:
            if val in self._BLOCK_HEADERS:
                result[key] = self._parse_block_scalar(val, indent + 1)
            elif val.startswith(('[', '{')):
                result[key] = self._parse_flow(val)
            else:
                result[key] = self._parse_scalar(val)
            return
        # An empty value means a nested block. A sequence is allowed to
        # sit at the SAME indent as its key -- extremely common, and
        # parsing it as "empty" then falling into the mapping branch is
        # what used to crash.
        nxt = self._peek()
        if (nxt and nxt[0] == indent
                and (nxt[1] == '-' or nxt[1].startswith('- '))):
            result[key] = self._parse_block(indent)
        else:
            result[key] = self._parse_block(indent + 1)

    def _parse_block_scalar(self, header, min_indent):
        lines = []
        style, chomping = header[0], header[1:]
        found_indent = -1
        while self.line_idx < len(self.lines):
            line = self.lines[self.line_idx]
            if not line.strip():
                lines.append('')
                self.line_idx += 1
                continue
            indent = self._get_indent(line)
            if indent < min_indent:
                break
            if found_indent == -1:
                found_indent = indent
            lines.append(line[found_indent:])
            self.line_idx += 1
        while lines and lines[-1] == '':
            lines.pop()
        if style == '|':
            content = '\n'.join(lines)
        else:
            out, group = [], []
            for ln in lines:
                if ln == '':
                    if group:
                        out.append(' '.join(group))
                        group = []
                    out.append('')
                else:
                    group.append(ln)
            if group:
                out.append(' '.join(group))
            content = '\n'.join(out)
        if '-' in chomping:
            return content.rstrip('\n')
        if '+' in chomping:
            return content + '\n'
        return content.rstrip('\n') + '\n' if content else ''

    # -- flow ------------------------------------------------------
    def _parse_flow(self, text):
        text = text.strip()
        if text.startswith('['):
            if not text.endswith(']'):
                raise MicroYAMLError(f"unclosed flow sequence: {text!r}")
            inner = text[1:-1].strip()
            if not inner:
                return []
            return [self._parse_flow(i) for i in self._split_flow(inner)]
        if text.startswith('{'):
            if not text.endswith('}'):
                raise MicroYAMLError(f"unclosed flow mapping: {text!r}")
            inner = text[1:-1].strip()
            if not inner:
                return {}
            items = {}
            for pair in self._split_flow(inner):
                colon = self._find_bare(pair, ':')
                if colon < 0:
                    raise MicroYAMLError(
                        f"flow mapping entry has no ':': {pair!r}")
                items[self._parse_scalar(pair[:colon])] = \
                    self._parse_flow(pair[colon + 1:])
            return items
        return self._parse_scalar(text)

    def _split_flow(self, text):
        """Split on commas outside quotes and nested collections."""
        parts, curr, depth, quote = [], [], 0, None
        for ch in text:
            if quote:
                if ch == quote:
                    quote = None
            elif ch in '"\'':
                quote = ch
            elif ch in '[{':
                depth += 1
            elif ch in ']}':
                depth -= 1
            elif ch == ',' and depth == 0:
                parts.append(''.join(curr).strip())
                curr = []
                continue
            curr.append(ch)
        tail = ''.join(curr).strip()
        if tail:
            parts.append(tail)
        return parts
