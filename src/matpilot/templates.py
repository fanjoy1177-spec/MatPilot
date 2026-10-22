"""Operator-registered CIF templates. No LLM-supplied file paths or Wyckoff labels."""
from __future__ import annotations
import hashlib
from pathlib import Path

SYMPREC = 0.01
ANGLE_TOLERANCE = 5


def resolve_template(template_id: str, settings) -> dict:
    from pymatgen.core import Structure
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
    if not isinstance(template_id, str) or template_id not in settings.templates:
        raise ValueError("Choose a template_id registered by the operator; never invent symmetry information.")
    entry = settings.templates[template_id]
    if not isinstance(entry, dict) or not {"path", "sha256", "source", "review"} <= set(entry):
        raise ValueError("Template registration needs path, SHA256, source and review metadata.")
    review = entry["review"]
    if not isinstance(review, dict) or not all(isinstance(review.get(k), str) and review[k].strip() for k in ("by", "date", "scope")):
        raise ValueError("Template review must name the reviewer, date and scope; this is operator-attested metadata.")
    if not isinstance(entry["source"], str) or not entry["source"].strip():
        raise ValueError("Template source is required.")
    path = Path(entry["path"]).expanduser().resolve()
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Template exceeds 4 MiB.")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != entry["sha256"]:
        raise ValueError("Template changed since registration; review the new file explicitly.")
    crystal = Structure.from_file(path)
    if not crystal.is_ordered or len(crystal) > 256:
        raise ValueError("Only ordered templates with up to 256 sites are supported.")
    analyzer = SpacegroupAnalyzer(crystal, symprec=SYMPREC, angle_tolerance=ANGLE_TOLERANCE)
    conventional = analyzer.get_conventional_standard_structure()
    analyzer = SpacegroupAnalyzer(conventional, symprec=SYMPREC, angle_tolerance=ANGLE_TOLERANCE)
    sym = analyzer.get_symmetrized_structure()
    atoms, letters, orbits = [], [], []
    for sites, label in zip(sym.equivalent_sites, sym.wyckoff_symbols):
        species = {site.specie.symbol for site in sites}
        if len(species) != 1 or int(label[:-1]) != len(sites):
            raise ValueError("Ambiguous symmetry orbit; manual template review required.")
        atoms.append(next(iter(species)))
        letters.append(label)
        orbits.append([site.frac_coords.tolist() for site in sites])
    return {"template_id": template_id, "sha256": digest, "source": entry["source"],
            "review": dict(review), "demo_only": entry.get("demo_only", False),
            "chemical_formula": conventional.composition.reduced_formula,
            "chemical_system": "-".join(sorted(e.symbol for e in conventional.composition.elements)),
            "space_group": int(analyzer.get_space_group_number()), "wyckoff_letters": letters,
            "atom_types": atoms, "site_count": len(conventional),
            "orbit_fractional_positions": orbits, "lattice_angstrom": conventional.lattice.matrix.tolist(),
            "derivation": {"method": "pymatgen.SpacegroupAnalyzer", "basis": "standard_conventional",
                           "symprec_angstrom": SYMPREC, "angle_tolerance_degrees": ANGLE_TOLERANCE},
            "notice": "Symmetry is numerically derived; the operator's source/review claim is retained, not independently certified. Bridge rechecks PyXtal Wyckoff multiplicities before loading weights."}


def template_catalog(settings) -> list[dict]:
    result = []
    for template_id in settings.templates:
        try:
            result.append({**resolve_template(template_id, settings), "status": "structurally_checked"})
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            result.append({"template_id": template_id, "status": "unavailable", "reason": str(exc)})
    return result
