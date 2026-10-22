"""
MatterGen wrapper for material generation.

MatterGen's distributions omit its data files (the sampling configs, the GemNet
scale factors, the training configs), so it runs from a source checkout, which
is how upstream installs it (editable). Its dependencies come from the
``mattergen`` environment.

Requirements:
    - Environment: mattergen (venv/run mattergen ...; aarch64 uses the generative image)
    - MatterGen checkout next to this project as ``mattergen``, or at $MATTERGEN_REPO
"""

import logging
import os
import sys
from typing import Dict, Any, Optional
from pathlib import Path

logger = logging.getLogger(__name__)

# MatterGen checkout: $MATTERGEN_REPO, else a `mattergen` checkout next to this project
MATTERGEN_REPO = Path(
    os.environ.get(
        "MATTERGEN_REPO", Path(__file__).resolve().parents[4].parent / "mattergen"
    )
).expanduser()


def use_mattergen_checkout() -> Path:
    """Import MatterGen from its checkout rather than the installed distribution.

    Returns:
        The checkout's path.

    Raises:
        FileNotFoundError: If MATTERGEN_REPO is not a MatterGen checkout.
        RuntimeError: If MatterGen was already imported from elsewhere.
    """
    if not (MATTERGEN_REPO / "sampling_conf").is_dir():
        raise FileNotFoundError(
            f"MatterGen checkout not found at {MATTERGEN_REPO}. Fetch it with "
            "skills/ml-generative-mattergen/scripts/setup_mattergen.py (it pins "
            "and patches the source), or set MATTERGEN_REPO to such a checkout."
        )
    loaded = sys.modules.get("mattergen")
    if loaded is not None and not Path(loaded.__file__).resolve().is_relative_to(
        MATTERGEN_REPO.resolve()
    ):
        raise RuntimeError(
            f"mattergen was already imported from {loaded.__file__}, not {MATTERGEN_REPO}"
        )
    if str(MATTERGEN_REPO) not in sys.path:
        sys.path.insert(0, str(MATTERGEN_REPO))
    return MATTERGEN_REPO


# Available pretrained models
AVAILABLE_MODELS = {
    "mattergen_base": "base pretrained model",
    "mp_20_base": "Materials Project base model",
    "dft_mag_density": "model for magnetic density conditioning",
    "chemical_system": "model for chemical system conditioning",
}


class MatterGenWrapper:
    """
    MatterGen wrapper for structure generation and fine-tuning.
    """

    def __init__(
        self,
        model_name: str = "mattergen_base",
        device: str = "auto",
        properties_to_condition_on: Optional[Dict[str, Any]] = None,
        guidance_scale: float = 0.0,
    ):
        """
        Initialize MatterGen wrapper and load the model.

        Args:
            model_name: Name of the pretrained model
            device: Device to run the model on ("auto", "cpu", "cuda")
            properties_to_condition_on: Properties to condition generation on
            guidance_scale: Diffusion guidance factor (gamma)
        """
        import torch

        use_mattergen_checkout()
        from mattergen.common.utils.data_classes import MatterGenCheckpointInfo
        from mattergen.generator import CrystalGenerator

        self.model_name = model_name
        self.device = device
        self.properties_to_condition_on = properties_to_condition_on or {}
        self.guidance_scale = guidance_scale

        # Set device
        if self.device == "auto":
            self.device = "cuda" if torch.cuda.is_available() else "cpu"

        # Load model immediately in constructor
        try:
            # Create checkpoint info from HuggingFace hub for pretrained models
            checkpoint_info = MatterGenCheckpointInfo.from_hf_hub(
                self.model_name, config_overrides=[]
            )

            # Initialize generator with the checkpoint info and properties
            self.generator = CrystalGenerator(
                checkpoint_info=checkpoint_info,
                properties_to_condition_on=self.properties_to_condition_on,
                batch_size=10,
                num_batches=1,
                diffusion_guidance_factor=self.guidance_scale,
                record_trajectories=False,  # Set to False for performance
            )
            self.is_loaded = True
            logger.info(f"Successfully loaded MatterGen model: {self.model_name}")
        except Exception as e:
            logger.error(f"Failed to load MatterGen model {self.model_name}: {e}")
            raise RuntimeError(f"Failed to load model: {e}")

    def generate_structures(
        self,
        num_structures: int = 10,
        batch_size: int = 10,
        output_dir: str = "outputs",
    ) -> Dict[str, Any]:
        """
        Generate inorganic material structures using MatterGen.

        Args:
            num_structures: Total number of structures to generate
            batch_size: Batch size for generation
            output_dir: Directory to save results

        Returns:
            Dictionary with generation results
        """
        if not self.is_loaded:
            raise RuntimeError("Model not loaded.")

        from pymatgen.core import Structure
        import json

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        try:
            # Calculate number of batches
            num_batches = (num_structures + batch_size - 1) // batch_size

            # Run generation
            logger.info(f"Generating {num_structures} structures...")

            generated_structures = self.generator.generate(
                batch_size=batch_size,
                num_batches=num_batches,
                output_dir=str(output_path),
            )

            # Save structures to CIF files
            structure_paths = []
            for i, struct in enumerate(generated_structures):
                if isinstance(struct, dict):
                    # Convert dict to pymatgen Structure if needed
                    struct = Structure.from_dict(struct)

                cif_path = output_path / f"structure_{i:04d}.cif"
                struct.to(filename=str(cif_path), fmt="cif")
                structure_paths.append(str(cif_path))

            # Save generation metadata
            metadata = {
                "num_generated": len(generated_structures),
                "model_name": self.model_name,
                "batch_size": batch_size,
                "properties_to_condition_on": self.properties_to_condition_on,
                "guidance_scale": self.guidance_scale,
            }

            metadata_path = output_path / "generation_metadata.json"
            with open(metadata_path, "w") as f:
                json.dump(metadata, f, indent=2)

            return {
                "num_generated": len(generated_structures),
                "output_dir": str(output_path),
                "structures": structure_paths,
                "metadata_path": str(metadata_path),
            }

        except Exception as e:
            logger.error(f"Generation failed: {e}")
            import traceback

            traceback.print_exc()
            raise RuntimeError(f"Generation failed: {e}")
