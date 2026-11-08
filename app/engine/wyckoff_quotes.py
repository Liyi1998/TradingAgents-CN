"""Bind typography-only differences to the actual report span, never paraphrase."""

import re


def visible_chars(text):
    # Only paired inline emphasis/code and line-leading Markdown list/quote syntax.
    # Keep punctuation, numbers, negations, letters and word boundaries intact.
    ignored = set()
    for match in re.finditer(r"(?m)^\s*(?:[-+]\s+|>\s*)", text):
        ignored.update(range(match.start(), match.end()))
    for pattern in (r"\*\*[^\n]+?\*\*", r"`[^`\n]+`"):
        for match in re.finditer(pattern, text):
            n = 2 if pattern.startswith(r"\*\*") else 1
            ignored.update(range(match.start(), match.start() + n))
            ignored.update(range(match.end() - n, match.end()))
    chars, positions = [], []
    for index, char in enumerate(text):
        if index in ignored:
            continue
        if char.isspace():
            if chars and chars[-1] != " ":
                chars.append(" ")
                positions.append(index)
        else:
            chars.append(char)
            positions.append(index)
    return "".join(chars), positions


def bind_quote(quote, content, limit):
    if not quote or quote in content:
        return quote
    text, positions = visible_chars(content)
    needle, _ = visible_chars(quote)
    needle = needle.strip()
    index = text.find(needle)
    if not needle or index < 0 or text.find(needle, index + 1) >= 0:
        return quote
    original = content[positions[index] : positions[index + len(needle) - 1] + 1]
    return original if len(original) <= limit else quote


def complete_phase_paragraph(quote, content):
    """Recover one surrounding original paragraph, never combine distant claims."""
    if not quote or quote not in content or content.count(quote) != 1:
        return quote
    at = content.index(quote)
    boundaries = list(re.finditer(r"\n[ \t]*\n", content))
    start = max((m.end() for m in boundaries if m.end() <= at), default=0)
    end = min((m.start() for m in boundaries if m.start() >= at + len(quote)), default=len(content))
    paragraph = content[start:end].strip()
    plain, _ = visible_chars(paragraph)
    if (len(paragraph) <= 1800 and len(re.findall(r"趋势背景\s*[：:]", plain)) == 1
            and len(re.findall(r"(?:当前阶段|阶段假说|阶段判断|阶段定性)\s*[：:为]", plain)) == 1
            and not re.search(r"(?m)^\s*#", paragraph)):
        return paragraph
    return quote


def bind_assessment_quotes(value, content):
    for key, limit in [
        ("source_quote", 1800),
        ("structure_start_quote", 600),
        ("change_reason", 900),
        ("alternative_scenario", 600),
    ]:
        value[key] = bind_quote(value.get(key), content, limit)
    value["source_quote"] = complete_phase_paragraph(value["source_quote"], content)
    for key in ("confirmation_conditions", "invalidation_conditions"):
        value[key] = [bind_quote(text, content, 600) for text in value[key]]
    for entry in value["events"] + ([value["trading_range"]] if value["trading_range"] else []):
        entry["source_quote"] = bind_quote(entry["source_quote"], content, 600)
    for entry in value["events"]:
        # A model often quotes the explanation but omits its inline event label.
        # Recover only the unique original line, never another event or date.
        quote = entry["source_quote"]
        if quote and content.count(quote) == 1 and "\n" not in quote:
            at = content.index(quote)
            start = content.rfind("\n", 0, at) + 1
            end = content.find("\n", at + len(quote))
            line = content[start:end if end >= 0 else len(content)].strip()
            kinds = re.findall(r"(?<![A-Za-z])(?:Spring|Test|UTAD|LPSY|SOS|LPS|SOW|SC|AR|ST|BC|UT)(?![A-Za-z])", line, re.I)
            dates = set(re.findall(r"\d{4}-\d{2}-\d{2}", line))
            if (len(line) <= 600 and set(k.lower() for k in kinds) == {entry["kind"].lower()}
                    and dates == {entry["date"]} and not line.startswith("#")):
                entry["source_quote"] = line
    return value
