import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPTests(unittest.IsolatedAsyncioTestCase):
    def test_cold_stdio_first_recommendation_finishes_without_client_prewarm(self):
        """A fresh real server must survive its first scientific parser tool.

        No numpy/pymatgen import in the launcher can mask native-import thread
        initialization failures. The outer process deadline also bounds a stuck
        native module that cannot be cancelled by an asyncio timeout alone.
        """
        root = Path(__file__).resolve().parents[1]
        base = root / ".work" / "test-temp"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as tmp:
            folder = Path(tmp)
            cif = folder / "synthetic-nacl.cif"
            cif.write_text("""data_synthetic_cpu_only
_cell_length_a 5.64
_cell_length_b 5.64
_cell_length_c 5.64
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
_symmetry_space_group_name_H-M 'P 1'
loop_
_atom_site_type_symbol
_atom_site_label
_atom_site_fract_x
_atom_site_fract_y
_atom_site_fract_z
_atom_site_occupancy
Na Na1 0 0 0 1
Cl Cl1 0.5 0.5 0.5 1
""", encoding="utf-8")
            manifest = folder / "collection.json"
            manifest.write_text(json.dumps({"schema_version": 1, "expected_elements": ["Na", "Cl"],
                "provenance": "synthetic_CPU_cold_stdio_fixture_not_material_evidence",
                "records": [{"candidate_id": "cold-nacl", "path": str(cif),
                             "sha256": hashlib.sha256(cif.read_bytes()).hexdigest()}]}), encoding="utf-8")
            config = folder / "config.json"
            config.write_text(json.dumps({"backend": "demo", "data_dir": str(folder / "data"),
                "real_compute_enabled": False, "candidate_collections": {"cold-fixture": {
                    "manifest_path": str(manifest), "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}}}), encoding="utf-8")
            launcher = folder / "cold_client.py"
            launcher.write_text("""import asyncio, json, os, sys
from datetime import timedelta
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
assert 'numpy' not in sys.modules and 'pymatgen.core' not in sys.modules
async def check():
    params = StdioServerParameters(command=sys.executable,
        args=['-m', 'matpilot', '--config', sys.argv[1], 'serve'],
        env={'PYTHONPATH': sys.argv[2]})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as session:
            await session.initialize()
            result = await session.call_tool('recommend_candidates',
                {'collection_id': 'cold-fixture', 'limit': 1, 'include_all_records': True})
            assert not result.isError, result
            data = json.loads(result.content[0].text)
            assert data['selected_candidate_ids'] == ['cold-nacl'], data
            assert data['effective_selection_policy'] == 'ionic_mixed_valence_v1', data
            assert data['selection_policy_resolution']['property_prediction_performed'] is False
            print(json.dumps({'selected_count': data['selected_count'],
                'selected_candidate_ids': data['selected_candidate_ids'],
                'effective_selection_policy': data['effective_selection_policy'],
                'client_prewarm': False, 'real_compute': False}))
asyncio.run(check())
""", encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": str(root / "src")}
            options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                       else {"start_new_session": True})
            process = subprocess.Popen([sys.executable, str(launcher), str(config), str(root / "src")],
                cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", **options)
            try:
                stdout, stderr = process.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                # The PID belongs to the process created just above. Never stop
                # a pre-existing Harness/MCP service to recover this test.
                if process.poll() is None:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       capture_output=True, timeout=10, check=False)
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate(timeout=10)
                self.fail("Cold stdio recommendation exceeded 60s: " + stderr[-8000:])
            self.assertEqual(process.returncode, 0, stderr)
            data = json.loads(stdout)
            self.assertEqual(data["selected_candidate_ids"], ["cold-nacl"])
            self.assertEqual(data["selected_count"], 1)
            self.assertFalse(data["client_prewarm"])
            self.assertFalse(data["real_compute"])

    async def test_official_client_discovers_and_calls_tools(self):
        base = Path(__file__).resolve().parents[1] / ".work" / "test-temp"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"backend": "demo", "data_dir": tmp}), encoding="utf-8")
            params = StdioServerParameters(command=sys.executable, args=["-m", "matpilot", "--config", str(config), "serve"],
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual({tool.name for tool in tools.tools}, {
                        "get_capabilities", "prepare_plan", "submit_job", "get_job", "control_job",
                        "get_results", "screen_candidates", "recommend_candidates", "compare_runs", "build_report",
                        "get_candidate_evidence", "prepare_property_prediction"})
                    caps = await session.call_tool("get_capabilities", {})
                    self.assertFalse(caps.isError)
                    planned = await session.call_tool("prepare_plan", {"request": {"conditions": {"chemical_system": "Fe-O", "dft_band_gap": 2}}})
                    self.assertFalse(planned.isError)
                    data = json.loads(planned.content[0].text)
                    self.assertEqual(data["state"], "blocked")
                    rejected = await session.call_tool("submit_job", {"plan_id": data["plan_id"], "approved_plan_id": data["plan_id"], "idempotency_key": "mcp-blocked-test"})
                    self.assertTrue(rejected.isError)


if __name__ == "__main__":
    unittest.main()
