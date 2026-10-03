"""Read generated include closures without invoking a preprocessor."""

import re


def expanded(outputs, name):
    seen = set()

    def read(key):
        if key in seen:
            return ""
        seen.add(key)
        text = outputs[key]
        return re.sub(
            r'^\s*#\s*include\s*"([^"]+)"',
            lambda match: read(match[1]) if match[1] in outputs else match[0],
            text,
            flags=re.M,
        )

    return read(name)
