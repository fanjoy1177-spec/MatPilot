import os
from pathlib import Path
from datetime import datetime
from typing import Dict
from src.utils.config_utils import inject_config_into_env

# Inject configuration from ~/.mlip_agent.yaml into environment
inject_config_into_env()


def workspace_root() -> Path:
    """Return the directory that holds ``research/`` and ``.env``.

    In order of precedence:

    1. ``ATOMISTIC_WORKSPACE``, when set. MCP servers started by the plugin get
       the Claude Code project directory here, and containers started by
       ``venv/run`` get the host workspace, mounted at the same path.
    2. The repository, when the current directory is inside a checkout of it:
       the development layout, where ``research/`` sits next to the code.
    3. Otherwise the current directory. A plugin install keeps this code in
       Claude Code's plugin cache, which is replaced on every update and must
       never receive results, while the agent runs scripts from the user's
       project -- so the project is where results belong.

    ``venv/run`` applies the same rule, so a skill script and an MCP server
    started from the same project always agree on the research directory.
    """
    override = os.environ.get("ATOMISTIC_WORKSPACE")
    if override:
        return Path(override).expanduser().absolute()
    repo = Path(__file__).resolve().parents[2]
    cwd = Path.cwd().resolve()
    if cwd == repo or repo in cwd.parents:
        return repo
    return cwd


def load_env(project_root: Path) -> Dict[str, str]:
    """Simple parser for .env files to avoid dependencies."""
    env_vars = {}
    env_path = project_root / ".env"
    if env_path.exists():
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    env_vars[key.strip()] = value.strip().strip("'").strip('"')
    return env_vars


def save_env(project_root: Path, key: str, value: str):
    """Save a variable to .env, updating if exists or appending."""
    env_path = project_root / ".env"
    lines = []
    found = False

    if env_path.exists():
        with open(env_path, "r") as f:
            lines = f.readlines()

    with open(env_path, "w") as f:
        for line in lines:
            if line.strip().startswith(f"{key}="):
                f.write(f"{key}={value}\n")
                found = True
            else:
                f.write(line)

        if not found:
            if lines and not lines[-1].endswith("\n"):
                f.write("\n")
            f.write(f"{key}={value}\n")


def get_current_research_dir() -> Path:
    """
    Get the current research directory for the session.

    Logic:
    1. Loads .env from project root.
    2. Checks for CURRENT_RESEARCH_DIR.
    3. If set and exists, returns it.
    4. If not set or missing, creates a new default one.
    5. Saves the choice to .env for persistence.
    """
    # Define project root relative to this file (src/utils/research_utils.py)
    project_root = workspace_root()

    # Try to load from .env first
    env_vars = load_env(project_root)
    cached_dir = env_vars.get("CURRENT_RESEARCH_DIR")

    if cached_dir:
        cached_path = Path(cached_dir)
        if cached_path.exists():
            return cached_path

    # If not in env or doesn't exist, fall back to filesystem discovery/creation
    research_root = project_root / "research"

    if not research_root.exists():
        research_root.mkdir(parents=True)

    # List subdirectories
    subdirs = [p for p in research_root.iterdir() if p.is_dir()]

    final_dir = None
    if subdirs:
        # Sort by name (which starts with date)
        final_dir = sorted(subdirs, key=lambda p: p.name)[-1]
    else:
        # Create default
        today = datetime.now().strftime("%Y-%m-%d")
        final_dir = research_root / f"{today}_default_session"
        final_dir.mkdir(parents=True, exist_ok=True)

    # Cache the result
    save_env(project_root, "CURRENT_RESEARCH_DIR", str(final_dir))

    return final_dir


def create_new_research_dir(topic: str) -> Path:
    """
    Create a new research directory for a specific topic and set it as current.

    Args:
        topic: Short description of the research topic (e.g. "LiFePO4_stability")

    Returns:
        Path to the newly created directory.
    """
    project_root = workspace_root()
    research_root = project_root / "research"

    if not research_root.exists():
        research_root.mkdir(parents=True)

    today = datetime.now().strftime("%Y-%m-%d")

    # Sanitize topic
    safe_topic = topic.replace(" ", "_").replace("/", "-")
    dir_name = f"{today}_{safe_topic}"

    new_dir = research_root / dir_name
    new_dir.mkdir(parents=True, exist_ok=True)

    # Update cache to point to this new directory
    save_env(project_root, "CURRENT_RESEARCH_DIR", str(new_dir))

    return new_dir
