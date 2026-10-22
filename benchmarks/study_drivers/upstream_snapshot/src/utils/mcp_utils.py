import sys
import os
import contextlib
import anyio
from mcp.server.stdio import stdio_server


def setup_mcp_stdout():
    """
    Standardize MCP stdout/stderr redirection to prevent library noise
    from polluting the MCP JSON-RPC protocol.

    This function:
    1. Captures the original stdout (FD 1).
    2. Redirects system-level FD 1 to FD 2 (stderr) to catch C-level prints.
    3. Reassigns sys.stdout to sys.stderr at the Python level.
    4. Returns a handle to the original stdout for the MCP transport.
    """
    try:
        # Check if we are using the safe launcher which saved the pipe to a custom FD
        custom_fd_str = os.environ.get("MCP_STDOUT_FD")
        if custom_fd_str:
            try:
                mcp_stdout_fd = int(custom_fd_str)
                # Ensure this FD is open and writable
                # We do NOT need to dup2(2, 1) because the shell already did it!
                # But we might need to patch sys.stdout just in case Python reset it.
                sys.stdout = sys.stderr
                return os.fdopen(mcp_stdout_fd, "wb", buffering=0)
            except Exception as e:
                sys.stderr.write(
                    f"Warning: Failed to use MCP_STDOUT_FD={custom_fd_str}: {e}\n"
                )
                # Fallback to normal logic

        # 1. Save the REAL stdout (the one used for MCP communication)
        mcp_stdout_fd = os.dup(1)

        # 2. Redirect system-level FD 1 to stderr (FD 2)
        os.dup2(2, 1)

        # 3. Create a handle to the REAL pipe
        # We use a raw binary file object so we can re-wrap it correctly in the transport
        mcp_pipe_binary = os.fdopen(mcp_stdout_fd, "wb", buffering=0)

        # 4. Patch Python's sys.stdout to use stderr
        sys.stdout = sys.stderr

        return mcp_pipe_binary
    except Exception as e:
        sys.stderr.write(f"Warning: Failed to setup robust MCP stdout isolation: {e}\n")
        return None


@contextlib.asynccontextmanager
async def mcp_transport_manager(mcp_pipe_binary):
    """
    Async context manager to provide explicit transport streams to stdio_server,
    bypassing sys.stdout and sys.stdin.
    """
    if mcp_pipe_binary is None:
        # Fallback to default behavior if redirection failed
        async with stdio_server() as streams:
            yield streams
        return

    # Create explicit async streams for the transport
    # Stdio server expects AsyncFile[str] (wrapped TextIOWrapper)
    from io import TextIOWrapper

    # Wrap original stdin and our saved pipe binary
    # We use buffering=1 (line buffering) for text streams
    async_stdin = anyio.wrap_file(
        TextIOWrapper(sys.stdin.buffer, encoding="utf-8", line_buffering=True)
    )
    async_stdout = anyio.wrap_file(
        TextIOWrapper(mcp_pipe_binary, encoding="utf-8", line_buffering=True)
    )

    async with stdio_server(stdin=async_stdin, stdout=async_stdout) as streams:
        yield streams


def run_fastmcp_server(mcp, mcp_pipe_binary):
    """
    Run a FastMCP server using the robust redirection transport.
    """

    async def _run():
        async with mcp_transport_manager(mcp_pipe_binary) as (
            read_stream,
            write_stream,
        ):
            init_options = mcp._mcp_server.create_initialization_options()
            await mcp._mcp_server.run(read_stream, write_stream, init_options)

    anyio.run(_run)
