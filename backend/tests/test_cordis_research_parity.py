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
