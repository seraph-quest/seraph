"""Fixed readonly JSON formatter package; executed only after isolation bootstrap."""
import json


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def nonfinite(value):
    raise ValueError("non-finite JSON number")


with open("/input.json", "rb") as source:
    raw = source.read(32769)
if len(raw) > 32768:
    raise ValueError("input limit")
value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=nonfinite)
pending = [(value, 0)]
count = 0
while pending:
    item, depth = pending.pop()
    count += 1
    if count > 4096 or depth > 32:
        raise ValueError("JSON structure limit")
    if isinstance(item, dict):
        pending.extend((child, depth + 1) for child in item.values())
    elif isinstance(item, list):
        pending.extend((child, depth + 1) for child in item)
output = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
if len(output) > 65536:
    raise ValueError("output limit")
with open("/out/result.json", "wb") as destination:
    destination.write(output)
