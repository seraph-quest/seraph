"""#1008 parity: the Cordis HTML/text provider and the TS/Python research seams."""
from __future__ import annotations

import json
from pathlib import Path
import random
import re
import subprocess

import pytest

from tests.cordis_research import NODE, PACKAGE, needs_cordis

FRAGMENTS = [
    "<p>", "</p>", "<P>", "</DIV>", "<br>", "</br>", "</li>", "</h3>", "</section >", "<b class='x'>", "</b>", "<a href=\"/x\">",
    "<script>bad()</script>", "<SCRIPT type=x>a<b</ScRiPt >", "<style>p{}</style>", "<noscript>n</noscript>", "<scriptx>kept</scriptx>",
    "<!-- c -->", "<!--\nmulti\n-->", "&amp;", "&amp", "&ampx", "&AMP;", "&lt;", "&gt", "&quot;", "&apos;", "&nbsp;", "&copy",
    "&copy;", "&notin;", "&notit;", "&mdash;", "&hellip;", "&unknown;", "&#65;", "&#065", "&#x41;", "&#X1F600;", "&#0;",
    "&#x80;", "&#x9F;", "&#13;", "&#xD800;", "&#1114112;", "&#99999999999;", "&#1;", "&#xFFFE;", "&#x10FFFF;", "& ", "&;",
    " ", "  ", "\t", "\n", "\r\n", "\r", "　", "\x1c", "\x85", "\xa0", "﻿", " ", "​", "word", "Ünïcödé",
    "日本語", "😀", "a<b", ">", "<", "<>", "<!DOCTYPE html>",
]


ADVERSARIAL = {"unclosed_tags": "<a" * 131072, "unclosed_comments": "<!--" * 65536, "unclosed_scripts": "<script>" * 32768,
               "unclosed_script_open": "<script " * 32768, "whitespace_run": " " * 262143 + "x",
               "alternating": "<script>x</style>" * 15000, "charref_digits": "&#" + "0" * 262140}


def _reference_normalize(content, *, html_content=False):
    """The original regex form of normalize_source_text (quadratic on unclosed input)."""
    import html
    value = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    if html_content:
        value = re.sub(r"<!--.*?-->", "", value, flags=re.S)
        value = re.sub(r"<(script|style|noscript)\b[^>]*>.*?</\1\s*>", "\n", value, flags=re.I | re.S)
        value = re.sub(r"</(p|div|li|h[1-6]|br|tr|section|article)>", "\n", value, flags=re.I)
        value = re.sub(r"<[^>]+>", "", value)
        value = html.unescape(value)
    lines = [" ".join(line.split()) for line in value.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def test_linear_python_normalizer_equals_the_original_regex_form():
    from src.guardian.source_watch import normalize_source_text
    small = ["<a<b>c", "<>x<>", "<!--<!-- a -->b-->", "<!-->x-->y", "<script>a<script>b</script>c</script>",
             "<SCRIPT >x</ScRiPt\t>y", "<style>x</script>y</style>z", "<scriptx>a</scriptx>", "<noscript>n</noscript >",
             "<script>unclosed", "<script x='>'>y</script>z", "<ſcript>s</script>t", *FOLD_CLOSING]
    for text in [*_corpus(), *_case_fold_corpus(15000), *small, *(value[:2000] for value in ADVERSARIAL.values())]:
        for flag in (True, False):
            assert normalize_source_text(text, html_content=flag) == _reference_normalize(text, html_content=flag), text


# ``İ`` lowercases to ``i`` under CPython's re.I backreference, ``ı``/``ſ`` only to
# themselves: the closing tag of a script/style element follows that rule.
FOLD_CLOSING = ["<ScRiPT>x</SCRİPT>y", "<scrİpt>x</script>y", "<scrİpt>x</SCRIPT>y", "<SCRİPT>x</scrİpt>y",
                "<scrıpt>x</scrİpt>y", "<scrİpt>x</scrıpt>y", "<scrıpt>x</script>y", "<ſcript>x</script>y",
                "<script>x</ſcript>y", "<ſtyle>x</ſtyle>y", "<noscrİpt>x</NOSCRIPT >y", "<NosCripT>m</noscrİPt >n"]


def _case_fold_corpus(count=3000):
    """Tag names with letters Unicode case folding treats specially (ı İ ſ K Å) and mixed case."""
    rng = random.Random(10082)
    variants = {"i": "iIıİ", "s": "sSſ", "k": "kK\u212a", "a": "aA\u212b"}
    names = ["script", "style", "noscript", "p", "div", "li", "h1", "h6", "br", "tr", "section", "article", "kbd", "a"]

    def spell(name):
        return "".join(rng.choice(variants.get(c, c + c.upper())) if rng.random() < 0.6 else c for c in name)
    cases = []
    for _ in range(count):
        open_name, close_name = spell(rng.choice(names)), spell(rng.choice(names))
        cases.append(rng.choice([f"<{open_name}>x</{close_name}>y", f"a</{close_name}>b", f"<{open_name} q>z</{open_name}\t>w",
                                 f"<{open_name}>m</{close_name} >n<p>o</{spell('p')}>"]))
    return cases


def _corpus():
    rng = random.Random(1008)
    cases = ["", "plain text", "  a \t b \r\n\r\n c　d ", "<html><h1>T</h1><p>One&nbsp;&amp; two</p></html>"]
    cases += ["".join(rng.choice(FRAGMENTS) for _ in range(rng.randint(1, 40))) for _ in range(400)]
    return cases


@needs_cordis
def test_html_provider_matches_python_normalize_source_text():
    from src.guardian.source_watch import normalize_source_text
    cases = _corpus()
    module = (PACKAGE / "dist/src/plugins/source_extraction/html.js").as_uri()
    script = ("import { normalizeSourceText } from " + json.dumps(module) + ";"
              "let input = ''; process.stdin.setEncoding('utf8');"
              "for await (const chunk of process.stdin) input += chunk;"
              "const cases = JSON.parse(input);"
              "process.stdout.write(JSON.stringify(cases.map(([text, html]) => normalizeSourceText(text, html))));")
    cases += _case_fold_corpus()
    cases += [*FOLD_CLOSING, "<scrıpt>x</scrıpt>y", "<noscrıpt>x</noscrıpt>y", "a</dıv>b", "a</DİV>b",
              "<a<b>c", "<>x<>", "<!--<!-- a -->b-->", "<script>a<script>b</script>c</script>", "<SCRIPT >x</ScRiPt\t>y",
              "<style>x</script>y</style>z", "<script>unclosed", "<ſcript>s</script>t", *(v[:4000] for v in ADVERSARIAL.values())]
    pairs = [[text, html] for text in cases for html in (True, False)]
    completed = subprocess.run([NODE, "--input-type=module", "-e", script], input=json.dumps(pairs), capture_output=True,
                               text=True, env={"LANG": "C.UTF-8", "TZ": "UTC"}, timeout=60, check=True)
    observed = json.loads(completed.stdout)
    mismatches = [(text, html, ts, normalize_source_text(text, html_content=html))
                  for (text, html), ts in zip(pairs, observed, strict=True)
                  if ts != normalize_source_text(text, html_content=html)]
    assert mismatches == []


def test_research_rejection_codes_media_types_and_closures_stay_in_lockstep():
    from src.runtime_plugins.composition import EXTRA_PLUGIN_FILES, SOURCE_FORMAT_PROVIDERS, SOURCE_MEDIA_TYPES
    from src.runtime_plugins.ownership import method_closure
    from src.runtime_plugins.research_dispatch import SOURCE_REJECTIONS
    from src.guardian.goal_discovery import SOURCE_MEDIA_TYPES as DISCOVERY_MEDIA_TYPES
    guardian = (PACKAGE / "src/plugins/research/guardian.ts").read_text()
    extraction = (PACKAGE / "src/plugins/source_extraction/index.ts").read_text()
    composition = (PACKAGE / "src/composition.ts").read_text()
    rejections = re.search(r"SOURCE_REJECTIONS = Object\.freeze\(\[(.*?)\]\)", guardian, re.S).group(1)
    assert set(re.findall(r"'([a-z_]+)'", rejections)) == SOURCE_REJECTIONS
    media = re.search(r"SOURCE_MEDIA_TYPES = Object\.freeze\(\[(.*?)\]", extraction, re.S).group(1)
    assert tuple(re.findall(r"'([a-z+/]+)'", media)) == SOURCE_MEDIA_TYPES == DISCOVERY_MEDIA_TYPES
    providers = re.search(r"FORMAT_PROVIDERS = Object\.freeze\(\{(.*?)\}\);", extraction, re.S).group(1)
    assert set(re.findall(r"'([a-z-]+)':", providers)) == SOURCE_FORMAT_PROVIDERS
    for identifier, files in EXTRA_PLUGIN_FILES.items():
        block = re.search(re.escape(f"'{identifier}': [") + r"(.*?)\]", composition, re.S).group(1)
        assert tuple(re.findall(r"'([^']+)'", block)) == files
    assert method_closure("research.buildPlan", "programme_planning") == ("artifacts.read", "artifacts.stage", "research.buildPlan")
    assert method_closure("research.executeAccepted", "programme_discovery") == (
        "artifacts.read", "artifacts.stage", "research.executeAccepted", "tasks.checkpoint", "tasks.inspect")
    profile = json.loads((PACKAGE / "profile.json").read_text())
    formats = next(spec for spec in profile["plugins"] if spec["id"] == "seraph.source-extraction.v1")["config"]["formats"]
    assert set(formats) == set(SOURCE_MEDIA_TYPES) and set(formats.values()) <= SOURCE_FORMAT_PROVIDERS


@pytest.mark.parametrize("change", ["unknown_media", "unknown_provider", "extra_key"])
def test_python_profile_rejects_unreviewed_source_formats(change):
    from copy import deepcopy
    from src.runtime_plugins.composition import validate_profile
    from src.runtime_plugins.protocol import ProtocolError
    profile = json.loads((PACKAGE / "profile.json").read_text())
    assert validate_profile(deepcopy(profile))
    spec = next(item for item in profile["plugins"] if item["id"] == "seraph.source-extraction.v1")
    if change == "unknown_media":
        spec["config"]["formats"]["application/pdf"] = "plain-text"
    elif change == "unknown_provider":
        spec["config"]["formats"]["text/html"] = "python-fallback"
    else:
        spec["config"]["path"] = "/tmp"
    with pytest.raises(ProtocolError):
        validate_profile(profile)


@pytest.mark.asyncio
async def test_unavailable_host_names_the_exact_fix_in_runtime_status_and_discovery(tmp_path):
    from src.guardian.goal_discovery import research_runtime_state
    from src.runtime_plugins.bridge import CordisHost
    host = CordisHost(node_path=tmp_path / "absent-node")
    assert await host.start() is False  # no child is spawned for a missing Node
    snapshot = host.snapshot()
    assert snapshot["state"] == "blocked" and snapshot["reason"] == "node_missing"
    assert "Node 22.12+ or 24.x" in snapshot["recovery"] and "CORDIS_NODE_PATH" in snapshot["recovery"]
    discovery = research_runtime_state(host)
    assert discovery == {"state": "blocked", "reason": "node_missing", "recovery": snapshot["recovery"]}
    from src.runtime_plugins.bridge import cordis_recovery
    assert "./manage.sh -e dev cordis build" in cordis_recovery("blocked", "runtime_build_stale")
    assert cordis_recovery("ready", None) is None


@needs_cordis
def test_uninstalled_dependencies_are_named_with_their_install_fix(tmp_path):
    import shutil
    from src.runtime_plugins.bridge import cordis_recovery
    from src.runtime_plugins.composition import PACKAGE_FILES, CompositionBlocked, reviewed_composition
    root = (tmp_path / "cordis").resolve()
    for name in PACKAGE_FILES:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PACKAGE / name, root / name)
    with pytest.raises(CompositionBlocked) as missing:
        reviewed_composition(root=root, node_path=Path(NODE))
    assert missing.value.reason == "build_dependencies_missing"
    assert "npm ci --prefix runtime/cordis --ignore-scripts" in cordis_recovery("blocked", missing.value.reason)


@needs_cordis
def test_adversarial_256_kib_pages_normalize_in_bounded_time_in_both_owners():
    import time
    from src.guardian.source_watch import SourceWatchError, normalize_source_text
    for name, text in ADVERSARIAL.items():
        started = time.perf_counter()
        try:
            normalize_source_text(text, html_content=True)
        except (SourceWatchError, ValueError):
            pass  # Oversized output / html.unescape's digit limit are typed rejections, not stalls.
        assert time.perf_counter() - started < 2, name
    module = (PACKAGE / "dist/src/plugins/source_extraction/html.js").as_uri()
    script = ("import { normalizeSourceText } from " + json.dumps(module) + ";"
              "let input = ''; process.stdin.setEncoding('utf8');"
              "for await (const chunk of process.stdin) input += chunk;"
              "const out = {}; for (const [name, text] of Object.entries(JSON.parse(input))) {"
              "const started = performance.now(); normalizeSourceText(text, true); out[name] = performance.now() - started; }"
              "process.stdout.write(JSON.stringify(out));")
    completed = subprocess.run([NODE, "--input-type=module", "-e", script], input=json.dumps(ADVERSARIAL), capture_output=True,
                               text=True, env={"LANG": "C.UTF-8", "TZ": "UTC"}, timeout=60, check=True)
    timings = json.loads(completed.stdout)
    assert set(timings) == set(ADVERSARIAL) and all(ms < 2000 for ms in timings.values()), timings
