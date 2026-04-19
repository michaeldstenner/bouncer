import re


class MicroYAML:
    def __init__(self):
        self.lines = []
        self.line_idx = 0

    def load(self, text):
        docs = self.load_all(text)
        return docs[0] if docs else None

    def load_all(self, text):
        docs = re.split(
            r'^---' if text.startswith('---') else r'\n---',
            text, flags=re.MULTILINE,
        )
        results = []
        for doc in docs:
            if not doc.strip():
                continue
            doc = re.split(r'^\.\.\.', doc, flags=re.MULTILINE)[0]
            self.lines = doc.splitlines()
            self.line_idx = 0
            res = self._parse_block(0)
            if res is not None:
                results.append(res)
        return results

    def _get_indent(self, line):
        if not line.strip():
            return -1
        return len(line) - len(line.lstrip())

    def _parse_scalar(self, val):
        val = val.strip()
        if not val:
            return None
        v_lower = val.lower()
        if v_lower in ('true', 'yes', 'on'):
            return True
        if v_lower in ('false', 'no', 'off'):
            return False
        if v_lower in ('null', '~'):
            return None
        try:
            if '.' in val or 'e' in val.lower():
                return float(val)
            return int(val)
        except ValueError:
            pass
        if ((val.startswith('"') and val.endswith('"')) or
                (val.startswith("'") and val.endswith("'"))):
            inner = val[1:-1]
            if val.startswith('"'):
                for k, v in {'\\n': '\n', '\\t': '\t', '\\"': '"', '\\\\': '\\'}.items():
                    inner = inner.replace(k, v)
            else:
                inner = inner.replace("''", "'")
            return inner
        return val

    def _parse_block(self, current_indent):
        result = None
        while self.line_idx < len(self.lines):
            line = self.lines[self.line_idx]
            if not line.strip() or line.strip().startswith('#'):
                self.line_idx += 1
                continue
            indent = self._get_indent(line)
            if indent < current_indent:
                break
            content = line.strip()
            if ' #' in content:
                content = content.split(' #')[0].strip()
            if ':' in content and not content.startswith('-'):
                if result is None:
                    result = {}
                key, _, val = content.partition(':')
                key = key.strip()
                val = val.strip()
                self.line_idx += 1
                if val:
                    if val in ('|', '>', '|-', '>-', '|+', '>+'):
                        result[key] = self._parse_block_scalar(val, indent + 2)
                    elif val.startswith(('[', '{')):
                        result[key] = self._parse_flow(val)
                    else:
                        result[key] = self._parse_scalar(val)
                else:
                    result[key] = self._parse_block(indent + 1)
            elif content.startswith('-'):
                if result is None:
                    result = []
                val = content[1:].strip()
                self.line_idx += 1
                if val:
                    if val.startswith(('[', '{')):
                        result.append(self._parse_flow(val))
                    elif ':' in val:
                        saved_idx = self.line_idx - 1
                        original_line = self.lines[saved_idx]
                        self.lines[saved_idx] = ' ' * (indent + 2) + val
                        self.line_idx = saved_idx
                        result.append(self._parse_block(indent + 2))
                        self.lines[saved_idx] = original_line
                    else:
                        result.append(self._parse_scalar(val))
                else:
                    result.append(self._parse_block(indent + 1))
            else:
                self.line_idx += 1
        return result

    def _parse_block_scalar(self, header, min_indent):
        lines = []
        style = header[0]
        chomping = header[1:] if len(header) > 1 else ''
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
        if style == '|':
            content = '\n'.join(lines)
        else:
            res, current_group = [], []
            for ln in lines:
                if ln == '':
                    if current_group:
                        res.append(' '.join(current_group))
                        current_group = []
                    res.append('')
                else:
                    current_group.append(ln)
            if current_group:
                res.append(' '.join(current_group))
            content = '\n'.join(res)
        if '-' in chomping:
            return content.rstrip('\n')
        elif '+' in chomping:
            return content + '\n'
        else:
            return content.rstrip('\n') + '\n' if content else ''

    def _parse_flow(self, text):
        text = text.strip()
        if text.startswith('['):
            inner = text[1:-1].strip()
            if not inner:
                return []
            return [self._parse_scalar(item) for item in self._split_flow(inner)]
        elif text.startswith('{'):
            items = {}
            inner = text[1:-1].strip()
            if not inner:
                return {}
            for pair in self._split_flow(inner):
                if ':' in pair:
                    k, _, v = pair.partition(':')
                    items[self._parse_scalar(k)] = self._parse_scalar(v)
            return items
        return self._parse_scalar(text)

    def _split_flow(self, text):
        res, curr, depth = [], [], 0
        for char in text:
            if char in '[{':
                depth += 1
            if char in ']}':
                depth -= 1
            if char == ',' and depth == 0:
                res.append(''.join(curr).strip())
                curr = []
            else:
                curr.append(char)
        if curr:
            res.append(''.join(curr).strip())
        return res
