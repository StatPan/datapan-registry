# External datapan-cli checkout policy

Registry workflows consume `StatPan/datapan-cli` through one reviewed immutable
commit recorded in `policy/external-checkout-refs.json`. Every external checkout
must repeat that exact SHA in `actions/checkout`'s `ref` input. This removes the
GitHub default-branch discovery request and binds release, runtime verification,
consolidation, import, and publication validation to the same CLI source.

A mutable `main` ref is not an acceptable substitute. It would avoid default
branch discovery, but a previously reviewed Registry workflow could then execute
new CLI code without a Registry change or compatibility review. The selected SHA
is updated only by a pull request that proves Registry workflow compatibility,
updates all checkout sites together, and regenerates
`.github/external-checkout-refs.inventory.json`.

Run the guard with:

```sh
python3 -m pip install 'PyYAML==6.0.2'
python3 scripts/validate-external-checkout-refs.py --check
python3 -m unittest tests/test_validate_external_checkout_refs.py
```

The committed report is the review inventory. The guard parses every `.yml` and
`.yaml` workflow through PyYAML's `SafeLoader`, rejects duplicate mapping keys,
and deliberately rejects YAML anchors, aliases, and merge keys. Quoted scalars,
unnamed `uses` steps, block mappings, and flow mappings are normal AST forms and
remain discoverable. Dynamic action, repository, ref, and path expressions fail
closed where they could make checkout identity ambiguous.

CI also fails when an external CLI checkout omits `ref`, selects a different
identity, changes the reviewed 8-workflow/9-checkout expectation, or changes the
generated inventory without regeneration. The guard prints the selected SHA and
checkout count so hosted logs retain the identity used for the run.
