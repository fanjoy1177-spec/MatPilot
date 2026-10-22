"""CIF parsing uses atom sites, never guesses elements from free-text headers."""
from __future__ import annotations

import hashlib
import io
import warnings
import zipfile
from pathlib import Path


def analyze_cif(text: str, expected_elements: set[str] | None = None) -> dict:
    from pymatgen.io.cif import CifParser
    try:
        with warnings.catch_warnings(record=True) as caught:
            parser = CifParser(io.StringIO(text))
            structures = parser.parse_structures(primitive=False)
        if len(structures) != 1:
            raise ValueError("Expected exactly one crystal structure per CIF.")
        crystal = structures[0]
        elements = sorted(str(e) for e in crystal.composition.elements)
        distances = crystal.distance_matrix
        min_distance = min((float(distances[i, j]) for i in range(len(crystal)) for j in range(i)), default=None)
        composition_match = set(elements) == expected_elements if expected_elements else None
        geometry_pass = crystal.volume > 0 and (min_distance is None or min_distance >= 0.5)
        return {"parse_valid": True, "formula": crystal.composition.formula, "elements": elements,
                "site_count": len(crystal), "volume_angstrom3": float(crystal.volume),
                "minimum_pair_distance_angstrom": min_distance, "composition_match": composition_match,
                "basic_geometry_pass": geometry_pass, "warnings": [str(w.message) for w in caught],
                "evidence_level": "structural_sanity_only"}
    except Exception as exc:
        return {"parse_valid": False, "error": str(exc), "evidence_level": "unparsed"}


def collect_archive(archive: Path, output: Path, expected: int, elements: set[str] | None, allow_partial: bool = False, errors: list | None = None) -> list[dict]:
    output.mkdir(parents=True, exist_ok=True)
    records = []
    with zipfile.ZipFile(archive) as bundle:
        members = [m for m in bundle.infolist() if not m.is_dir() and m.filename.lower().endswith(".cif")]
        if len(members) > expected or (not allow_partial and len(members) != expected):
            raise ValueError(f"Expected {expected} CIF files, received {len(members)}.")
        if sum(m.file_size for m in members) > 32 * 1024 * 1024:
            raise ValueError("CIF output exceeds the batch size limit.")
        # Reject global limits before writing any candidate, even in partial mode.
        if any(m.file_size > 4 * 1024 * 1024 for m in members):
            raise ValueError("CIF file exceeds 4 MiB.")
        for index, member in enumerate(members):
            content = bundle.read(member)
            try:
                record = analyze_cif(content.decode("utf-8"), elements)
            except UnicodeError as exc:
                record = {"parse_valid": False, "error": str(exc)}
            if not record["parse_valid"]:
                if allow_partial:
                    if errors is not None:
                        errors.append({"source_member": member.filename, "error": record["error"]})
                    continue
                raise ValueError(f"Invalid generated structure {index}: {record['error']}")
            digest = hashlib.sha256(content).hexdigest()
            # Never extract caller-controlled archive names onto the filesystem.
            target = output / f"candidate_{index:04d}.cif"
            target.write_bytes(content)
            records.append({**record, "sha256": digest, "path": str(target), "source_member": member.filename})
    return records


def demo_archive(target: Path, count: int):
    """Fixed NaCl examples explicitly exercise plumbing, never model quality."""
    from pymatgen.core import Lattice, Structure
    from pymatgen.io.cif import CifWriter
    crystal = Structure(Lattice.cubic(5.64), ["Na", "Cl"], [[0, 0, 0], [0.5, 0.5, 0.5]])
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
        for index in range(count):
            bundle.writestr(f"demo_{index}.cif", "# MATPILOT DEMONSTRATION FIXTURE - NOT GENERATED\n" + str(CifWriter(crystal)))
