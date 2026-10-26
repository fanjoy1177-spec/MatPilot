import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_client_discovers_and_calls_tools(self):
        base = Path(__file__).resolve().parents[1] / ".work" / "test-temp"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"backend": "demo", "data_dir": tmp}), encoding="utf-8")
            params = StdioServerParameters(command=sys.executable, args=["-m", "matpilot", "--config", str(config), "serve"])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual(len(tools.tools), 10)
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
