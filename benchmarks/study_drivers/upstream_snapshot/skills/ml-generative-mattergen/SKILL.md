---
name: ml-generative-mattergen
description: Generate inorganic material structures using MatterGen, a diffusion-based generative model.
metadata:
  category: [machine-learning, materials]
  venv: [mattergen]
---

# MatterGen Structure Generation Skill

This skill provides tools for generating novel inorganic material structures using MatterGen, a state-of-the-art diffusion-based generative model for crystalline materials.

## 1. Prerequisites

> [!IMPORTANT]
> **GPU Required**: MatterGen generation needs a CUDA GPU (NVIDIA driver ≥ 525).

- Runs as the `mattergen` MCP server and its scripts run in the `mattergen` environment: on x86_64 a uv environment created on first use (CUDA 12.6 or 13 by driver), on aarch64 the `generative` container image.
- A MatterGen checkout next to this project as `../mattergen`, or anywhere with `MATTERGEN_REPO` pointing to it. MatterGen's PyPI distribution omits the data files it needs (sampling configs, GemNet scale factors), so it runs from the checkout, as upstream installs it. Fetch it with the setup script, which pins the verified commit, skips the LFS checkpoints (the weights come from Hugging Face) and replaces `np.math`, which NumPy 2 removed:

  ```bash
  ${CLAUDE_SKILL_DIR}/../../venv/run mattergen python ${CLAUDE_SKILL_DIR}/scripts/setup_mattergen.py
  ```

  `venv/run` mounts it into the container on aarch64.
- On aarch64 (e.g. NVIDIA DGX Spark) the image carries PyG's extensions compiled for CUDA, which PyG publishes no aarch64 wheels for; nothing needs building on the host.

## 2. Available Models

MatterGen provides several pretrained models:

- `mattergen_base`: Base unconditional generative model
- `mp_20_base`: Materials Project base model
- `dft_mag_density`: Model for magnetic density conditioning
- `chemical_system`: Model for chemical system conditioning

## 3. MCP Tool Usage

The MCP tool automatically loads models when needed - no explicit load step required.

### Unconditional Generation

Generate novel structures without conditioning:

```python
from mcp_base import mattergen.generate_structures

result = mattergen.generate_structures(
    model_name="mattergen_base",
    num_structures=10,
    batch_size=10,
    output_dir="research/my_project/generated"
)
```

### Chemical System Conditioning

Generate structures from a specific chemical system (controls which elements appear):

```python
result = mattergen.generate_structures(
    chemical_system="Li-Fe-P-O",  # Automatically uses chemical_system model
    guidance_scale=1.0,  # Recommended for chemical system conditioning
    num_structures=20,
    batch_size=10,
    output_dir="research/cathode_materials/generated"
)
```

> [!NOTE]
> Chemical system conditioning controls which **elements** appear, but **NOT** the exact stoichiometry.
> For example, `chemical_system="Li-Zr-Cl"` can generate Li3Cl5, LiZrCl4, Li2ZrCl5, etc., but you cannot specify exactly "Li2ZrCl6".



## 4. Fine-Tuning (Skill Scripts)

Fine-tune MatterGen on custom datasets using the skill scripts:

### Step 1: Prepare Training Data

```bash
# Convert structures and properties to CSV format
${CLAUDE_SKILL_DIR}/../../venv/run mattergen python ${CLAUDE_SKILL_DIR}/scripts/prepare_training_data.py \
  --structures-json training_structures.json \
  --property-name "formation_energy" \
  --output training_data.csv
```

**Training data JSON format**:
```json
[
  {
    "structure": {<pymatgen Structure dict>},
    "properties": {"formation_energy": -2.5}
  },
  ...
]
```

### Step 2: Run Fine-Tuning

```bash
${CLAUDE_SKILL_DIR}/../../venv/run mattergen python ${CLAUDE_SKILL_DIR}/scripts/run_finetuning.py \
  --training-data training_data.csv \
  --property-name "formation_energy" \
  --base-model "mattergen_base" \
  --epochs 100 \
  --output-dir finetuned_formation_energy
```

**Fine-tuning parameters**:
- `--training-data`: Path to CSV from Step 1
- `--property-name`: Property to condition on (must match CSV column)
- `--base-model`: Starting model (mattergen_base, chemical_system, etc.)
- `--epochs`: Training epochs (100-200 recommended)
- `--learning-rate`: Learning rate (default: 5e-6)
- `--batch-size`: Batch size (default: 32)

> [!TIP]
> - Start with 2 epochs for quick testing
> - Use 100-200 epochs for actual fine-tuning
> - GPU required (fine-tuning on CPU is extremely slow)

## 5. Output Files

### Generation Output
- `structure_XXXX.cif`: Generated structure files
- `generation_metadata.json`: Metadata about generation parameters

### Fine-Tuning Output
- `checkpoints/`: Model checkpoint files (.ckpt)
- `config.yaml`: Hydra configuration used
- Training logs and metrics

## 6. Limitations

> [!WARNING]
> **Chemical System vs. Stoichiometry**
> - `chemical_system` parameter controls which **elements** are encouraged to be present.
> - It does **NOT** guarantee that all specified elements will be in the output structure.
> - It does **NOT** prevent other elements from occasionally appearing if guidance is too low.
> - Example: `chemical_system="Li-Zr-Cl"` might generate LiCl, ZrCl4, or even structures missing Li, alongside the desired ternaries (e.g., Li2ZrCl6).
> - **Action Required:** You MUST write a post-processing script to filter the output `.cif` files and keep only the ones that match your exact target elemental composition.

> [!WARNING]
> **CSP Mode Not Available**
> - Target composition control (`target_compositions` parameter) requires CSP-trained models
> - CSP models are NOT publicly available - must be custom-trained
> - Public models (mattergen_base, chemical_system, etc.) are **generation models** only

## 7. Best Practices

> [!IMPORTANT]
> - **GPU Required**: MatterGen requires a CUDA-compatible GPU. CPU is extremely slow.
> - **Batch Size**: Use larger batches (10-50) for efficient GPU utilization
> - **Guidance Scale**: Higher values (1.0-5.0) enforce stronger conditioning
> - **Composition Filtering**: Always filter the generated output CIFs using `pymatgen` to verify that the structures contain exactly the target elements.
> - **Validation**: Always validate generated structures via relaxation and stability analysis

> [!TIP]
> - Start with unconditional generation to understand model behavior
> - Use chemical_system conditioning to explore specific element combinations
> - Fine-tune on domain-specific data for specialized applications (e.g., cathode materials)

## 8. Workflow Integration

MatterGen works well in combination with:
- **Structure relaxation**: Use MCP MLIP tools to optimize generated structures
- **Stability analysis**: Calculate E_hull to identify stable phases
- **Property prediction**: Use MLIPs or DFT to calculate properties
- **High-throughput screening**: Generate → Relax → Screen workflow

## 9. Examples

See `examples/` for:
- Unconditional generation workflow
- Chemical system conditioning
- Fine-tuning on custom datasets with complete example data

---
---

**Author:** Bowen Deng
**Contact:** [GitHub @learningmatter-mit](https://github.com/learningmatter-mit)
