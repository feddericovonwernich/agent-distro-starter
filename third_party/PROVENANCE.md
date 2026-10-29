Ponytail and Superpowers skills/extensions are not redistributed from this tree. Fresh setup installs the official OMP project plugins from `DietrichGebert/ponytail` and `obra/superpowers`; OMP's native user cache and project registry record the resolved versions and payloads under the distro. Central discovery and worker catalog resolution both use those exact upstream payloads. The plugins execute upstream code and retain their upstream licenses.

## Open Relay skills

Source: https://github.com/feddericovonwernich/open-relay/archive/dac2865837bcab9444a8636053f4f1406a3f9291.tar.gz
Commit: `dac2865837bcab9444a8636053f4f1406a3f9291`
License: Apache-2.0 (upstream `LICENSE`, SHA-256 `736cb7537e55ce92c53ebab962988e66e38dbf050f620abd65cbb85de524ab4e`)
Archive SHA-256: `1c2ef0722ef66993270a03fa5c73f389b52582a77930c290d1b54d3ad119d76e`

The five canonical Open Relay skill trees are copied verbatim under `.agents/skills/` for central procedures. No upstream Open Relay skill is shipped as a worker bundle. A valid scoped-PR grant materializes the separate AGD-authored `agd-scoped-pr-relay` adapter into a task created with no ordinary worker skills; it is not an upstream-name alias.

The tracked `third_party/OPEN_RELAY_LICENSE` contains the upstream Apache-2.0 license text applicable to the copied central skill files; its SHA-256 is `736cb7537e55ce92c53ebab962988e66e38dbf050f620abd65cbb85de524ab4e`. The central files have these SHA-256 values:

| Skill | `SKILL.md` SHA-256 |
| --- | --- |
| using-open-relay | `730505ac8763bcf411f7697e265e11e6f94744f31fe7bae20f35789691a71c91` |
| setting-up-open-relay | `d710778855b1b90740095e12848c5a18bb1326a3d3317108744e2f787d93ce50` |
| waiting-for-open-relay-events | `fd74a167a5b97961e596a239aa7c5d1d3dddf881772812606c83325cd669c95f` |
| automating-github-prs-with-open-relay | `8189b1eec059ea4bf8c3b07fcd274649a74c669efcc989fe9fdd677e297ce131` |
| recovering-open-relay | `06e5d807555fa39de1a18496c00ea6f4dd74386150260eb0234326d4a52dfa2f` |

Open Relay package version is 0.1.0 and declares `ajv: ^8.17.1`; the caret range and transitive dependencies are not lock-pinned by this source archive, so npm runtime dependency resolution remains a supply-chain caveat.
