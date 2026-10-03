"""Intercepted GitHub REST object store; native source Git remains real."""
from __future__ import annotations

import base64
from datetime import datetime
import json

import httpx

from src.execution.repo_publication import SourceGit, object_id


class GitDataTransport:
    def __init__(self, source, repository="acme/example", base_branch="main"):
        self.repository, self.base_branch = repository, base_branch
        self.source = SourceGit(source)
        self.base = self.source.head()
        self.base_tree, self.files, self.contents = self.source.tree(self.base)
        self.objects = dict(self.source.objects)
        self.refs = {base_branch: self.base}
        self.commits = {self.base: {"sha": self.base, "tree": {"sha": self.base_tree}}}
        self.trees = {self.base_tree: self.tree_view(self.contents, self.files)}
        self.pulls = {}
        self.calls = []
        self.fail_after_pr = False

    def tree_view(self, contents, files):
        return [{"path": item["path"], "mode": item["mode"], "type": "blob", "sha": object_id("blob", contents[item["path"]])} for item in files]

    def make_tree(self, entries):
        nested = {}
        for entry in entries:
            current = nested
            parts = entry["path"].split("/")
            for part in parts[:-1]:
                current = current.setdefault(part, {})
            current[parts[-1]] = entry

        def encode(tree):
            encoded = []
            for name, entry in sorted(tree.items(), key=lambda pair: pair[0] + ("/" if "sha" not in pair[1] else "")):
                mode, identity = ("40000", encode(entry)) if "sha" not in entry else (entry["mode"], entry["sha"])
                encoded.append(mode.encode() + b" " + name.encode() + b"\0" + bytes.fromhex(identity))
            raw = b"".join(encoded)
            identity = object_id("tree", raw)
            self.objects[identity] = ("tree", raw)
            return identity
        return encode(nested)

    def handler(self, request):
        assert request.url.host == "api.github.com"
        assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"
        assert request.headers["Authorization"] == "Bearer fixture-token"
        path = request.url.path.removeprefix(f"/repos/{self.repository}")
        assert request.url.path.startswith(f"/repos/{self.repository}/")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if request.method == "POST":
            if path == "/git/blobs":
                raw = base64.b64decode(body["content"])
                identity = object_id("blob", raw)
                self.objects[identity] = ("blob", raw)
                return httpx.Response(201, json={"sha": identity})
            if path == "/git/trees":
                assert body["base_tree"] == self.base_tree
                entries = {entry["path"]: entry for entry in self.trees[self.base_tree]}
                for entry in body["tree"]:
                    if entry["sha"] is None:
                        entries.pop(entry["path"])
                    else:
                        assert self.objects[entry["sha"]][0] == "blob"
                        entries[entry["path"]] = entry
                identity = self.make_tree(list(entries.values()))
                self.trees[identity] = list(entries.values())
                return httpx.Response(201, json={"sha": identity})
            if path == "/git/commits":
                assert body["parents"] == [self.base]
                stamp = int(datetime.fromisoformat(body["author"]["date"]).timestamp())
                person = f"Seraph <seraph@localhost> {stamp} +0000"
                raw = f"tree {body['tree']}\nparent {self.base}\nauthor {person}\ncommitter {person}\n\n{body['message']}\n".encode()
                identity = object_id("commit", raw)
                self.objects[identity] = ("commit", raw)
                value = {**body, "sha": identity, "tree": {"sha": body["tree"]}, "parents": [{"sha": parent} for parent in body["parents"]]}
                self.commits[identity] = value
                return httpx.Response(201, json={"sha": identity})
            if path == "/git/refs":
                ref = body["ref"].removeprefix("refs/heads/")
                assert ref not in self.refs, "a second branch POST is forbidden"
                self.refs[ref] = body["sha"]
                return httpx.Response(201, json={"ref": body["ref"], "object": {"sha": body["sha"]}})
            if path == "/pulls":
                assert not self.pulls, "a second PR POST is forbidden"
                value = {**body, "number": 1, "state": "open", "head": {"ref": body["head"], "sha": self.refs[body["head"]], "repo": {"full_name": self.repository}}, "base": {"ref": body["base"], "sha": self.base, "repo": {"full_name": self.repository}}}
                self.pulls[1] = value
                if self.fail_after_pr:
                    raise httpx.ReadTimeout("intercepted unknown POST outcome", request=request)
                return httpx.Response(201, json={"number": 1})
            raise AssertionError(f"unexpected POST {path}")
        assert request.method == "GET"
        if path.startswith("/git/ref/heads/"):
            ref = path.removeprefix("/git/ref/heads/")
            return httpx.Response(200 if ref in self.refs else 404, json={"ref": "refs/heads/" + ref, "object": {"sha": self.refs.get(ref)}})
        if path.startswith("/git/blobs/"):
            identity = path.rsplit("/", 1)[-1]
            kind, raw = self.objects[identity]
            assert kind == "blob"
            return httpx.Response(200, json={"sha": identity, "encoding": "base64", "content": base64.b64encode(raw).decode(), "size": len(raw)})
        if path.startswith("/git/trees/"):
            identity = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"sha": identity, "truncated": False, "tree": self.trees[identity]})
        if path.startswith("/git/commits/"):
            return httpx.Response(200, json=self.commits[path.rsplit("/", 1)[-1]])
        if path == "/pulls":
            return httpx.Response(200, json=list(self.pulls.values()))
        if path.endswith("/files") and path.startswith("/pulls/"):
            new_tree = self.commits[self.refs[self.pulls[1]["head"]["ref"]]]["tree"]["sha"]
            original = {item["path"]: item for item in self.trees[self.base_tree]}
            changed = [{"filename": item["path"], "status": "modified" if item["path"] in original else "added", "sha": item["sha"]}
                for item in self.trees[new_tree] if original.get(item["path"]) != item]
            return httpx.Response(200, json=changed)
        if path.startswith("/pulls/"):
            return httpx.Response(200, json=self.pulls[int(path.rsplit("/", 1)[-1])])
        raise AssertionError(f"unexpected GET {path}")
