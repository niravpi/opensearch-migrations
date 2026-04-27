# Shim Ingest & Dual-Mode Validation Demo

Interactive end-to-end demo for the Solr → OpenSearch translation shim.
Ingests synthetic docs **through the shim** (exercising PR #2737 `/update/json/docs`
and PR #2747 `/update` dispatcher) into a user-named index on both Solr and
OpenSearch, then runs a 167-query suite in **single** or **dual** mode and
prints a validation report.

## Location

This folder is **not part of upstream** and should stay on the private branch
`demo/shim-ingest-dual-validate`. The script lives here so it can reuse the
sandbox's own `src/`, `data/`, `config/` and `queries/` modules directly:

```
opensearch-migrations/
└── solrMigrationDevSandbox/
    ├── run.sh                       # spins up Solr + OpenSearch + shim
    ├── src/query_runner.py          # reused for single-mode validation
    ├── data/generate_dataset.py     # reused for doc generation constants
    ├── config/opensearch/index-mapping.json   # reused for OS index creation
    ├── queries/queries.json         # reused as template for per-index queries
    └── demo/                        # <-- everything new lives here
        ├── README.md                # this file
        └── shim_ingest_dual_demo.py # the interactive demo
```

## Prerequisites

- Docker Desktop running
- Python 3.9+ with `requests` installed
  (already satisfied if `scripts/load_data.py` works on your machine)
- `run.sh` executed once so the sandbox clusters are up. See the **Startup**
  section below.

## Startup

```bash
cd ~/OpenSource/opensearch-migrations-1/solrMigrationDevSandbox
./run.sh          # starts Solr 9, OpenSearch 3.3, single shim, dual shims
```

Verify clusters are healthy:

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}'
curl -s 'http://localhost:18983/solr/testharness/select?q=*:*&rows=0&wt=json' \
  | grep -o '"numFound":[0-9]*'
curl -s 'http://localhost:19200/_cat/indices?v'
```

Endpoints the demo uses:

| Service | Port | URL |
|---------|------|-----|
| Solr | 18983 | http://localhost:18983 |
| OpenSearch | 19200 | http://localhost:19200 |
| Single shim (OS target) | 18080 | http://localhost:18080 |
| Dual shim (Solr primary) | 18083 | http://localhost:18083 |
| Dual shim (OpenSearch primary) | 18084 | http://localhost:18084 |

## Run the demo

```bash
cd ~/OpenSource/opensearch-migrations-1/solrMigrationDevSandbox
python3 demo/shim_ingest_dual_demo.py
```

You'll be prompted for:

| Prompt | Default | Notes |
|--------|---------|-------|
| Target index name | `myindex` | Any name; fresh Solr core + OS index is created |
| Number of docs to generate | `2000` | 200 for smoke test; 50k+ for load demo |
| Mode | | `1` single, `2` dual |
| Solr port | `18983` | |
| OpenSearch port | `19200` | |
| Single shim port *(single mode)* | `18080` | |
| Dual shim primary *(dual mode)* | `opensearch` | `1` = 18084 (OS primary), `2` = 18083 (Solr primary) |
| Skip provision? | `N` | `y` reuses existing index |
| Skip ingest? | `N` | `y` goes straight to validation |
| Proceed? | `Y` | |

After the report prints, you're asked **"Run another cycle?"** so you can flip
between modes or change params without restarting.

## What each phase does

1. **Generate docs (in-memory)** — same generator logic as
   `data/generate_dataset.py`. No dataset file written.
2. **Provision**
   - Solr: clones `testharness` instanceDir inside the Solr container and
     calls CoreAdmin `CREATE` — so the new core has the same schema and
     `solrconfig.xml`.
   - OpenSearch: DELETE + PUT with `config/opensearch/index-mapping.json`.
3. **Ingest**
   - **single mode**: `POST` to the single shim (writes to OpenSearch) +
     parallel `POST` to Solr-direct for symmetric comparison.
   - **dual mode**: single `POST` to the dual shim — fans out to both
     backends in one call.
   - After ingest: commit via `/update` (hits PR #2747 dispatcher) and
     explicit `_refresh` on OpenSearch.
   - Counts verified on both backends.
4. **Validate**
   - **single mode**: runs the full 167-query suite via `src.query_runner`;
     compares Solr-direct vs shim response client-side.
   - **dual mode**: runs the same suite via the dual shim and reads the
     shim's per-query `X-Validation-Status` / `X-Validation-Details`
     headers.
5. **Report**
   - Printed to stdout.
   - Saved to `demo/out/validation_<timestamp>_<mode>.json`.

## Reclassification of benign diffs (dual mode only)

The shim's in-process `field-equality` validator compares the **raw** backend
responses — Solr returns scalars (`"foo"`), OpenSearch returns single-element
arrays (`["foo"]`), and OpenSearch also adds `_version_`/`terminated_early`
fields. These are schema artifacts, not translation bugs, so the script
reclassifies any FAIL whose details contain **only** such diffs back to PASS.
The report prints both the final count and `(after reclassify: +N benign-only)`.

True FAILs (real doc-count mismatches, query-semantics divergences, etc.) are
preserved with their cleaned details.

## Files produced

| Path | Content |
|------|---------|
| `demo/queries_<index>.json` | Auto-generated from `queries/queries.json`, substituting the index name |
| `demo/out/validation_<ts>_<mode>.json` | Full report: cfg used, counters, per-query failures and errors |

## Tear down

```bash
cd ~/OpenSource/opensearch-migrations-1/solrMigrationDevSandbox
docker compose down -v
```

The newly-created indexes go with the volumes. Next `./run.sh` starts from a
clean slate.

## Sharing

These files are intentionally outside upstream. To share:

```bash
git checkout demo/shim-ingest-dual-validate
git add solrMigrationDevSandbox/demo/shim_ingest_dual_demo.py \
        solrMigrationDevSandbox/demo/README.md
git commit -m "demo: interactive shim ingest + dual-mode validation"
git push origin demo/shim-ingest-dual-validate
```

Do **not** merge into `main` — this is a local demo aid, not upstream code.
