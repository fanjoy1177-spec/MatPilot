# Fixed 100 / true Top50 / recommended Top20

This sanitized extract preserves the fixed 100 candidates, all six rankings,
and the original evaluator. The predeclared Wrenformer Service-plus-component
primary result is **11/20 (55%)**. The secondary MACE archived scalar comparator
is **20/20 (100%)**, with unknown convergence; it was never made a usable,
converged Service metric. It cannot replace the primary result. No new model
inference, relaxation, or DFT was performed.

From the repository root, with Python 3.10 or newer:

```sh
python benchmarks/ranking100/reproduce_metrics.py ranking100-receipt.json
```

The output must not already exist. This standard-library offline command checks
all input hashes and reproduces six evaluation blocks, including all six methods.
It re-evaluates disclosed rankings; it does not rerun the production plugin,
prove label isolation anew, or claim full training-set independence. The earlier
full production replay was separately completed locally (418 semantic checks
and 100 registered metric checks); host-dependent logs and the local evidence
ZIP are excluded from this public extract.

Numerical data derive from public Matbench Discovery archives under CC BY 4.0;
source URLs, IDs and frozen artifact hashes are in `provenance.json`. Values and
identities are preserved, labels are disclosed, and local paths are removed.
The true target is signed distance to a fixed reference hull; negative values
are intentional. Precision@20 is shortlist hit rate, not classification accuracy
over all candidates. Random-comparison probabilities concern this fixed pool,
not a universal accuracy confidence interval.
