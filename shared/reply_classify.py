"""reply_classify.py — detect 'no longer with the company' auto-replies and pull out
any replacement contacts named in the message. Shared by inbound-sequence.py and
outbound-sequence.py. Best-effort: used to enrich the Slack flag, never to send."""
import re

DEPARTURE_PATTERNS = [
    r"no longer (?:with|at|employed|here|works?)",
    r"left the (?:company|firm|organization|organisation|business)",
    r"is no longer",
    r"i am no longer",
    r"i'?m no longer",
    r"has (?:left|departed)",
    r"no longer (?:a part of|part of)",
    r"has moved on",
    r"is out of (?:the )?office permanently",
    r"is not? longer with",
]

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_NAME_RE = re.compile(
    r"(?:reach out to|contact|email|speak (?:to|with)|connect with|reaching|forward (?:this )?to)\s+"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})"
)


def detect_departure(text):
    """Return (is_departure, [replacement names/emails]) for a reply body/subject."""
    if not text:
        return False, []
    low = text.lower()
    is_dep = any(re.search(p, low) for p in DEPARTURE_PATTERNS)
    if not is_dep:
        return False, []
    found = []
    found += _EMAIL_RE.findall(text)
    found += [m.group(1) for m in _NAME_RE.finditer(text)]
    seen, out = set(), []
    for r in found:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return True, out
