# Original PS1 regression corpus

These fixtures are original synthetic C and instruction sequences authored for this repository, under its license. They contain no retail program bytes, proprietary SDK signatures or generated retail source.

corpus.json defines eight C cases (narrow arguments, plain char loads, constant division, small data, copies, switches, aggregate returns and GTE), plus seven instruction-evidence vectors. golden.json records output signatures for all nine pinned PS1 compilers using native assembly and ASPSX 2.56: 18 recipes and 144 case/recipe observations.

139 observations compile and have a fixed signature over function bytes, symbolic relocations and selected data-section bytes, sizes and alignment. Five native dense-switch observations record a specific assembler limitation: GCC 2.7.2 CDK, 2.8.0, 2.8.1, 2.91.66 and 2.95.2 emit explicit hi/lo references to ASPSX local labels which the native binutils 2.6 assembler rejects. All five compile through the optional ASPSX path. These are explicit capability limits, not successful compilation goldens; unrelated errors still fail.

Run the focused corpus with:

    uv run pytest -q tests/test_ps1_corpus.py tests/test_ps1_corpus_integration.py

Compiler-dependent cases skip when their pinned tools are absent; provenance, matrix completeness and instruction cases still run without compilers. Integration cases additionally exercise matched C in overlapping overlays, source-tree rebuilding, older layout migration, whole-disc identity and paired jump-table data. Existing focused tests cover debug inputs, CUE tracks, signature scans, probes, source restrictions and shared translation units.

To update goldens intentionally after reviewing a compiler or fixture change, install every pinned PS1 compiler plus maspsx and MIPS binutils, then run:

    uv run python -m tests.ps1_corpus --write

Normal tests never regenerate expected values. Review changes to both the fixture content hash and output signatures. Hash agreement measures compiler output stability; whole-image and link behavior are checked separately by integration tests. No retail validation result is implied.
