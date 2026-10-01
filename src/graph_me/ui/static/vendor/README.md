# Vendored front-end libraries

Served from the package so the web UI works offline and needs no JS build step. Update by
downloading the same files from jsDelivr, then refresh the checksums below.

| File | Package | License | Source |
| --- | --- | --- | --- |
| `htmx.min.js` | htmx.org 2.0.11 | 0BSD | `npm/htmx.org@2.0.11/dist/htmx.min.js` |
| `sigma.min.js` | sigma 3.0.3 | MIT | `npm/sigma@3.0.3/dist/sigma.min.js` |
| `graphology.umd.min.js` | graphology 0.26.0 | MIT | `npm/graphology@0.26.0/dist/graphology.umd.min.js` |
| `graphology-layout-forceatlas2.esm.js` | graphology-layout-forceatlas2 0.10.1 | MIT | `npm/graphology-layout-forceatlas2@0.10.1/+esm`, imports rewritten to the local files below |
| `graphology-utils-is-graph.esm.js` | graphology-utils 2.5.2 | MIT | `npm/graphology-utils@2.5.2/is-graph/+esm` |
| `graphology-utils-getters.esm.js` | graphology-utils 2.5.2 | MIT | `npm/graphology-utils@2.5.2/getters/+esm` |

sha256:

```
9b051fbbb8307a040d8702803b8c69a8425c6edce33c94e8a1341b67f8a60c25  graphology-layout-forceatlas2.esm.js
dc337efa23903f61e064c8e7e7f93a429e6855dccfc2458802b4ed30c621c087  graphology.umd.min.js
2b4fb90cc1061f7a43346a7d39cdf00f728ae4111ab390dfec620f58997c2546  graphology-utils-getters.esm.js
53480107e6d5946a9b3d7cb433c24cc35e14394baad33e2e7beffbd90d8e31ad  graphology-utils-is-graph.esm.js
d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717  htmx.min.js
58e30383ab428f832068d9d16a5215c65ba12430d438ed091c5703f398de9e16  sigma.min.js
```
