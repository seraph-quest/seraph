"""Isolated functional/security checks; no app DB, operator state or providers.

Run with unittest to avoid loading the full app fixture for pipe-only checks.
SERAPH_CORDIS_TEST_NODE selects a reviewed compatible test binary explicitly.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.runtime_plugins.bridge import CordisHost, HostBlocked, Pending
from src.runtime_plugins.composition import BUILD_FILES, CHILD_ENV, PACKAGE_FILES, PACKAGE_ROOT, CompositionBlocked, reviewed_composition, reviewed_node, validate_profile
from src.runtime_plugins.protocol import ProtocolError, decode_json, encode_frame, read_frame, validate_frame


def hello():
    return {"protocol": 1, "boot_nonce": "a" * 64, "request_id": "r-1", "seq": 1,
            "kind": "request", "method": "bootstrap.hello", "invocation_ref": None,
            "composition_epoch": None, "composition_digest": "b" * 64,
            "package_digest": "c" * 64, "deadline_at": int(time.time() * 1000) + 5000,
            "payload": {}}


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def test_duplicate_keys_utf8_nonfinite_depth_and_nodes(self):
        attacks = [b'{"x":1,"\\u0078":2}', b'{"x":NaN}', b'1e999', b'\xff',
                   b'[' * 17 + b'0' + b']' * 17, json.dumps([0] * 4096).encode()]
        for body in attacks:
            with self.subTest(body=body[:40]), self.assertRaises(ProtocolError):
                decode_json(body)

    def test_exact_closed_integer_and_method_schemas(self):
        for field, value in [("seq", True), ("seq", 1.0), ("seq", "1"), ("seq", 2**53),
                             ("deadline_at", -1), ("protocol", 1.0), ("composition_epoch", 1),
                             ("invocation_ref", "x"), ("method", "tools.run"), ("kind", []),
                             ("boot_nonce", "A" * 64), ("payload", {"path": "/tmp"})]:
            with self.subTest(field=field, value=value), self.assertRaises(ProtocolError):
                validate_frame({**hello(), field: value})
        with self.assertRaises(ProtocolError):
            validate_frame({**hello(), "unknown": 1})

    async def test_fragmented_raw_framing_and_bad_lengths(self):
        wire = encode_frame(hello())
        self.assertEqual(struct.unpack(">I", wire[:4])[0], len(wire) - 4)
        reader = asyncio.StreamReader()
        read = asyncio.create_task(read_frame(reader))
        reader.feed_data(wire[:1]); await asyncio.sleep(0)
        self.assertFalse(read.done())
        reader.feed_data(wire[1:7]); reader.feed_data(wire[7:])
        self.assertEqual((await read)["seq"], 1)
        for body in [struct.pack(">I", 1_048_577), struct.pack(">I", 0), b'\0\0\0\5{', b'\0\0']:
            reader = asyncio.StreamReader(); reader.feed_data(body); reader.feed_eof()
            with self.assertRaises(ProtocolError):
                await read_frame(reader)

    def test_literal_profiles_reject_imports_config_and_missing_required_plugin(self):
        base = json.loads((PACKAGE_ROOT / "profile.json").read_text())
        for change in [{"plugins": []}, {"module": "arbitrary"},
                       {"plugins": [{**base["plugins"][0], "config": {"path": "secret"}}]},
                       {"plugins": [{**base["plugins"][0], "id": "unreviewed@1.0.0"}]}]:
            with self.assertRaises(ProtocolError):
                validate_profile({**base, **change})

    async def test_missing_or_unsupported_node_blocks_without_starting_child(self):
        host = CordisHost()
        with patch("src.runtime_plugins.composition.shutil.which", return_value=None), patch("asyncio.create_subprocess_exec", new=AsyncMock()) as spawn:
            self.assertFalse(await host.start()); spawn.assert_not_called()
            self.assertEqual(host.snapshot()["reason"], "node_missing")
        node = Path(os.environ.get("SERAPH_CORDIS_TEST_NODE") or shutil.which("node") or "/usr/bin/python3")
        with patch("src.runtime_plugins.composition.subprocess.run", return_value=Mock(stdout=b"v22.11.0\n")), patch("asyncio.create_subprocess_exec", new=AsyncMock()) as spawn:
            host = CordisHost(node_path=node)
            self.assertFalse(await host.start()); spawn.assert_not_called()
            self.assertEqual(host.snapshot()["reason"], "node_unsupported")

    async def test_unknown_cleanup_retains_capacity_and_blocks_restart(self):
        process = Mock(returncode=None, stdin=None)
        process.wait = AsyncMock(side_effect=asyncio.TimeoutError)
        host = CordisHost(); host.process = process; host.state = "blocked"; host._cleanup_state = "pending"
        await host.stop(preserve_blocked=True)
        self.assertEqual(host.snapshot()["state"], "cleanup_unknown")
        self.assertFalse(host.snapshot()["cleanup"]["process_reaped"])
        self.assertIs(host.process, process)
        self.assertFalse(await host.start())
        process.terminate.assert_called_once(); process.kill.assert_called_once()


class ActualHostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        selected = os.environ.get("SERAPH_CORDIS_TEST_NODE") or shutil.which("node")
        if not selected:
            self.skipTest("compatible Node not installed; protocol-only checks still run")
        self.node = Path(selected).resolve()
        try:
            reviewed_composition(node_path=self.node)
        except CompositionBlocked as exc:
            self.skipTest(f"host prerequisite: {exc.reason}")
        self.host = CordisHost(node_path=self.node)

    async def asyncTearDown(self):
        await self.host.stop(preserve_blocked=self.host.state == "blocked")
        if self.host._cleanup_task is not None:
            await self.host._cleanup_task

    async def test_repeated_real_start_stop_fresh_boot_and_positive_reap(self):
        nonces = set()
        for _ in range(3):
            self.assertTrue(await self.host.start(), self.host.snapshot())
            self.assertNotIn(self.host.boot_nonce, nonces); nonces.add(self.host.boot_nonce)
            pid = self.host.process.pid
            self.assertEqual((await self.host.request())["state"], "ready")
            cancelled = await self.host.request("invocation.cancel", invocation_ref="never-admitted")
            self.assertEqual(cancelled, {"cancelled": False})
            await self.host.stop()
            receipt = self.host.snapshot()
            self.assertEqual(receipt["cleanup"], {"state": "clean", "process_reaped": True, "resources_remaining": 0, "cordis_disposal": "confirmed"})
            with self.assertRaises(ProcessLookupError): os.kill(pid, 0)

    async def test_child_gets_only_minimal_environment_and_closed_descriptors(self):
        original = asyncio.create_subprocess_exec
        async def inspect_spawn(*args, **kwargs):
            self.assertEqual(kwargs["env"], CHILD_ENV)
            self.assertTrue(kwargs["close_fds"])
            self.assertEqual(len(args), 2)
            return await original(*args, **kwargs)
        with patch.dict(os.environ, {"NODE_OPTIONS": "--not-a-real-node-option", "NODE_PATH": "/unreviewed", "OPENROUTER_API_KEY": "forbidden-test-sentinel"}), patch("asyncio.create_subprocess_exec", side_effect=inspect_spawn):
            self.assertTrue(await self.host.start())
        snapshot = json.dumps(self.host.snapshot())
        for forbidden in ["forbidden-test-sentinel", self.host.boot_nonce, "stderr", "pid"]:
            self.assertNotIn(forbidden, snapshot)

    async def test_start_cancellation_cannot_lose_spawned_child_handle(self):
        original = asyncio.create_subprocess_exec
        created = asyncio.Event()
        release = asyncio.Event()
        async def delayed_return(*args, **kwargs):
            process = await original(*args, **kwargs)
            created.set()
            await release.wait()
            return process
        with patch("asyncio.create_subprocess_exec", side_effect=delayed_return):
            starting = asyncio.create_task(self.host.start())
            await asyncio.wait_for(created.wait(), 5)
            starting.cancel(); release.set()
            with self.assertRaises(asyncio.CancelledError): await starting
        self.assertFalse(self.host.admitting)
        self.assertTrue(self.host.snapshot()["cleanup"]["process_reaped"])
        self.assertEqual(self.host.snapshot()["cleanup"]["resources_remaining"], 0)

    async def test_stop_cancellation_transfers_reap_to_owned_cleanup(self):
        self.assertTrue(await self.host.start())
        entered = asyncio.Event()
        original = self.host._rpc
        async def delayed_control(method, **kwargs):
            if method == "runtime.quiesce":
                entered.set()
                await asyncio.Event().wait()
            return await original(method, **kwargs)
        with patch.object(self.host, "_rpc", side_effect=delayed_control):
            stopping = asyncio.create_task(self.host.stop())
            await asyncio.wait_for(entered.wait(), 5)
            stopping.cancel()
            with self.assertRaises(asyncio.CancelledError): await stopping
            await asyncio.wait_for(self.host._cleanup_task, 5)
        self.assertFalse(self.host.admitting)
        self.assertEqual(self.host.reason, "shutdown_cancelled")
        self.assertTrue(self.host.snapshot()["cleanup"]["process_reaped"])
        self.assertEqual(self.host.snapshot()["cleanup"]["resources_remaining"], 0)

    async def _raw_attack(self, mutate):
        self.assertTrue(await self.host.start())
        reviewed = self.host.reviewed
        frame = {**hello(), "boot_nonce": self.host.boot_nonce, "seq": 2, "request_id": "r-2",
                 "method": "runtime.status", "composition_digest": reviewed.composition_digest,
                 "package_digest": reviewed.package_digest}
        wire = mutate(frame)
        self.host.process.stdin.write(wire); await self.host.process.stdin.drain()
        await asyncio.wait_for(self.host.process.wait(), 5)
        await self.host.stop(preserve_blocked=True)
        if self.host._cleanup_task is not None: await self.host._cleanup_task
        self.assertFalse(self.host.admitting)
        self.assertEqual(self.host.snapshot()["cleanup"]["state"], "clean")
        self.assertTrue(self.host.snapshot()["cleanup"]["process_reaped"])

    async def test_real_child_stale_boot_rejected(self):
        await self._raw_attack(lambda frame: encode_frame({**frame, "boot_nonce": "0" * 64}))

    async def test_real_child_repeated_sequence_and_request_id_rejected(self):
        await self._raw_attack(lambda frame: encode_frame({**frame, "seq": 1, "request_id": "r-1"}))

    async def test_real_child_duplicate_keys_never_dispatch(self):
        def attack(frame):
            body = json.dumps(frame).replace('"seq": 2', '"seq": 2, "seq": 2').encode()
            return struct.pack(">I", len(body)) + body
        await self._raw_attack(attack)

    async def test_real_child_oversized_length_never_dispatch(self):
        await self._raw_attack(lambda _: struct.pack(">I", 1_048_577))

    async def test_bounded_stderr_fences_and_reaps_actual_child(self):
        self.assertTrue(await self.host.start())
        self.host.process.stderr.feed_data(b"x" * 65_537)
        await asyncio.sleep(0)
        await self.host.stop(preserve_blocked=True)
        self.assertEqual(self.host.reason, "stderr_limit_exceeded")
        self.assertFalse(self.host.admitting)
        self.assertTrue(self.host.snapshot()["cleanup"]["process_reaped"])

    async def test_child_loss_fences_ready_boot_and_explicit_restart_gets_new_nonce(self):
        self.assertTrue(await self.host.start())
        nonce = self.host.boot_nonce
        self.host.process.kill()
        await self.host.process.wait()
        with self.assertRaises(HostBlocked): await self.host.request()
        await self.host.stop(preserve_blocked=True)
        self.assertFalse(self.host.admitting)
        self.assertTrue(self.host.snapshot()["cleanup"]["process_reaped"])
        self.assertTrue(await self.host.start())
        self.assertNotEqual(nonce, self.host.boot_nonce)

    async def test_unsolicited_parent_response_fences_actual_child(self):
        self.assertTrue(await self.host.start())
        reviewed = self.host.reviewed
        response = {**hello(), "boot_nonce": self.host.boot_nonce, "seq": 2, "request_id": "r-999",
                    "kind": "response", "method": "runtime.status", "composition_digest": reviewed.composition_digest,
                    "package_digest": reviewed.package_digest,
                    "payload": {"state": "ready", "plugins": self.host.snapshot()["plugins"], "resources_remaining": 2}}
        self.host.process.stdout.feed_data(encode_frame(response))
        await asyncio.sleep(0)
        with self.assertRaises(HostBlocked): await self.host.request()
        await self.host.stop(preserve_blocked=True)
        self.assertTrue(self.host.snapshot()["cleanup"]["process_reaped"])

    async def test_expired_deadline_and_pending_cap_prevent_writes(self):
        self.assertTrue(await self.host.start())
        seq = self.host._out_seq
        with self.assertRaises(HostBlocked): await self.host.request(deadline_at=int(time.time() * 1000) - 1)
        self.assertEqual(self.host._out_seq, seq)
        futures = [asyncio.get_running_loop().create_future() for _ in range(32)]
        self.host._pending = {f"reserved-{i}": Pending(hello(), future) for i, future in enumerate(futures)}
        try:
            with self.assertRaisesRegex(HostBlocked, "capacity"): await self.host.request()
            self.assertEqual(self.host._out_seq, seq)
        finally:
            self.host._pending.clear()
            for future in futures: future.cancel()

    async def test_unresolved_limit_also_bounds_calls_waiting_for_pipe_writer(self):
        self.assertTrue(await self.host.start())
        await self.host._write_lock.acquire()
        waiting = [asyncio.create_task(self.host.request()) for _ in range(32)]
        await asyncio.sleep(0)
        try:
            with self.assertRaisesRegex(HostBlocked, "capacity"):
                await self.host.request()
        finally:
            self.host._write_lock.release()
        self.assertEqual(len(await asyncio.gather(*waiting)), 32)
        self.assertEqual(self.host._unresolved, 0)

    async def test_stale_source_build_and_bad_integrity_fail_preflight(self):
        with tempfile.TemporaryDirectory(prefix="seraph-cordis-package-") as directory:
            root = Path(directory)
            for name in [*PACKAGE_FILES, "node_modules/cordis/package.json"]:
                target = root / name; target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(PACKAGE_ROOT / name, target)
            (root / "src/bootstrap.ts").write_text("// stale build\n")
            with self.assertRaisesRegex(CompositionBlocked, "runtime_build_stale"):
                reviewed_composition(root=root, node_path=self.node)
            shutil.copy2(PACKAGE_ROOT / "src/bootstrap.ts", root / "src/bootstrap.ts")
            lock = json.loads((root / "package-lock.json").read_text())
            lock["packages"]["node_modules/cordis"]["integrity"] = "wrong"
            (root / "package-lock.json").write_text(json.dumps(lock))
            with self.assertRaisesRegex(CompositionBlocked, "package_pin_mismatch"):
                reviewed_composition(root=root, node_path=self.node)


if __name__ == "__main__":
    unittest.main()
