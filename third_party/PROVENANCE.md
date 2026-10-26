PONYTAIL_SHA=16352ac229eb08a9598dcae48fe9f2b342567d86
SUPERPOWERS_SHA=2ce8aa15d05bd57213e6f746f953aafb7ded7a8b

Central skills are copied from each pinned upstream skills/ directory without runtime extensions, hooks, plugin manifests, or install scripts. Worker bundles contain only approved skill payloads plus the applicable upstream MIT LICENSE notice.

Central `.agents/skills/ponytail-help/SKILL.md` is adapted to the native OMP
distribution (canonical skill IDs and no Ponytail plugin, hook, band,
marketplace, environment-activation, or config claims); all other copied
central skill bodies remain verbatim from the pinned sources.

## Open Relay skills

Source: https://github.com/feddericovonwernich/open-relay/archive/dac2865837bcab9444a8636053f4f1406a3f9291.tar.gz
Commit: `dac2865837bcab9444a8636053f4f1406a3f9291`
License: Apache-2.0 (upstream `LICENSE`, SHA-256 `736cb7537e55ce92c53ebab962988e66e38dbf050f620abd65cbb85de524ab4e`)
Archive SHA-256: `1c2ef0722ef66993270a03fa5c73f389b52582a77930c290d1b54d3ad119d76e`

The five canonical skill trees are copied verbatim under `.agents/skills/` and as worker bundles under `bundled/worker-skills/`; each worker bundle includes the upstream Apache-2.0 `LICENSE`.

| Skill | `SKILL.md` SHA-256 |
| --- | --- |
| using-open-relay | `730505ac8763bcf411f7697e265e11e6f94744f31fe7bae20f35789691a71c91` |
| setting-up-open-relay | `d710778855b1b90740095e12848c5a18bb1326a3d3317108744e2f787d93ce50` |
| waiting-for-open-relay-events | `fd74a167a5b97961e596a239aa7c5d1d3dddf881772812606c83325cd669c95f` |
| automating-github-prs-with-open-relay | `8189b1eec059ea4bf8c3b07fcd274649a74c669efcc989fe9fdd677e297ce131` |
| recovering-open-relay | `06e5d807555fa39de1a18496c00ea6f4dd74386150260eb0234326d4a52dfa2f` |

Open Relay package version is 0.1.0 and declares `ajv: ^8.17.1`; the caret range and transitive dependencies are not lock-pinned by this source archive, so npm runtime dependency resolution remains a supply-chain caveat.
