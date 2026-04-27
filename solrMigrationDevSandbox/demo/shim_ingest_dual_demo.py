#!/usr/bin/env python3
"""
Interactive shim ingest + validation demo.

Flow per cycle:
  1) Generate N synthetic docs in-memory (same logic as data/generate_dataset.py)
  2) Provision a target index on Solr (core) + OpenSearch (index)
  3) Ingest via chosen shim
       single: shim -> OpenSearch, mirror write to Solr-direct for symmetric validation
       dual:   shim -> both backends in one call, with validation header per write
  4) Run the query suite in the chosen mode and print + save a validation report
  5) Ask to run another cycle

Usage:
  cd ~/OpenSource/opensearch-migrations-1/solrMigrationDevSandbox
  python3 demo/shim_ingest_dual_demo.py
"""
import json, random, subprocess, sys, time, datetime
from pathlib import Path
import requests

SANDBOX = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SANDBOX))
from src.query_runner import run_all_queries, run_all_dual_queries
from data.generate_dataset import (
    PRODUCTS, CATEGORIES, MARKETPLACES, HEADLINE_TEMPLATES,
    gen_review_body, gen_product_id, gen_date,
)

MAPPING_FILE = SANDBOX / "config" / "opensearch" / "index-mapping.json"
QUERIES_SRC = SANDBOX / "queries" / "queries.json"
OUT_DIR = SANDBOX / "demo" / "out"
SOLR_CONTAINER = "solrmigrationdevsandbox-solr-1"
SOURCE_CORE = "testharness"


# -------- prompts --------
def ask(q, default=None):
    suffix = f" [{default}]" if default is not None else ""
    v = input(f"  {q}{suffix}: ").strip()
    return v if v else default

def ask_yn(q, default=False):
    d = "Y/n" if default else "y/N"
    v = input(f"  {q} [{d}]: ").strip().lower()
    if not v: return default
    return v.startswith("y")

def ask_choice(q, options):
    print(f"  {q}")
    for i, (label, _) in enumerate(options, 1):
        print(f"    [{i}] {label}")
    while True:
        v = input("    choice: ").strip()
        if v.isdigit() and 1 <= int(v) <= len(options):
            return options[int(v) - 1][1]


# -------- config --------
def gather_config():
    print("\n=== Solr -> OpenSearch Shim Demo ===\n")
    cfg = {}
    cfg["index"]     = ask("Target index name", "myindex")
    cfg["num_docs"]  = int(ask("Number of docs to generate", "2000"))
    cfg["mode"]      = ask_choice("Mode?", [("single", "single"), ("dual", "dual")])
    cfg["solr_port"] = int(ask("Solr port", "18983"))
    cfg["os_port"]   = int(ask("OpenSearch port", "19200"))

    if cfg["mode"] == "single":
        cfg["shim_port"] = int(ask("Single shim port", "18080"))
    else:
        cfg["shim_port"] = ask_choice(
            "Dual shim primary?",
            [("opensearch=18084", 18084), ("solr=18083", 18083)])

    cfg["skip_provision"] = ask_yn("Skip provision (reuse existing index)?", False)
    cfg["skip_ingest"]    = ask_yn("Skip ingest (only run validation)?", False)

    cfg["solr_url"] = f"http://localhost:{cfg['solr_port']}"
    cfg["os_url"]   = f"http://localhost:{cfg['os_port']}"
    cfg["shim_url"] = f"http://localhost:{cfg['shim_port']}"

    print("\n  Review:")
    for k, v in cfg.items():
        print(f"    {k}: {v}")
    if not ask_yn("Proceed?", True):
        return None
    return cfg


# -------- phases --------
def generate_docs(n, seed=42):
    print(f"\n[1/4] Generating {n} docs in-memory...")
    random.seed(seed)
    docs = []
    for i in range(n):
        star = random.randint(1, 5)
        d = {
            "id": f"R{i:08d}",
            "product_title": random.choice(PRODUCTS),
            "review_body": gen_review_body(star),
            "review_headline": random.choice(HEADLINE_TEMPLATES),
            "product_category": random.choice(CATEGORIES),
            "marketplace": random.choice(MARKETPLACES),
            "product_id": gen_product_id(),
            "star_rating": star,
            "helpful_votes": random.randint(0, 500),
            "total_votes": 0,
            "review_date": gen_date(),
            "verified_purchase": random.random() > 0.3,
            "vine": random.random() > 0.95,
        }
        d["total_votes"] = d["helpful_votes"] + random.randint(0, 50)
        docs.append(d)
    print(f"    generated {len(docs)}")
    return docs


def provision_solr(core, solr_url):
    print(f"\n[2a] Provisioning Solr core `{core}` at {solr_url}")
    r = requests.get(f"{solr_url}/solr/admin/cores", params={"action": "STATUS", "core": core})
    if r.json()["status"].get(core):
        print(f"    unloading existing `{core}`")
        requests.get(f"{solr_url}/solr/admin/cores", params={
            "action": "UNLOAD", "core": core,
            "deleteIndex": "true", "deleteDataDir": "true", "deleteInstanceDir": "true"})
    # clone testharness instanceDir inside the Solr container
    subprocess.run(
        ["docker", "exec", SOLR_CONTAINER, "sh", "-c",
         f"rm -rf /var/solr/data/{core} && "
         f"cp -R /var/solr/data/{SOURCE_CORE} /var/solr/data/{core} && "
         f"rm -rf /var/solr/data/{core}/data && "
         f"rm -f /var/solr/data/{core}/core.properties"],
        check=True)
    r = requests.get(f"{solr_url}/solr/admin/cores", params={
        "action": "CREATE", "name": core, "instanceDir": core,
        "config": "solrconfig.xml", "schema": "schema.xml", "dataDir": "data"})
    r.raise_for_status()
    print(f"    core `{core}` created")


def provision_opensearch(index, os_url):
    print(f"\n[2b] Provisioning OpenSearch index `{index}` at {os_url}")
    requests.delete(f"{os_url}/{index}")
    mapping = MAPPING_FILE.read_text()
    r = requests.put(f"{os_url}/{index}", data=mapping,
                     headers={"Content-Type": "application/json"})
    r.raise_for_status()
    print(f"    index `{index}` created")


def ingest(docs, cfg):
    mode, index = cfg["mode"], cfg["index"]
    shim, solr, os_url = cfg["shim_url"], cfg["solr_url"], cfg["os_url"]
    total = len(docs)
    print(f"\n[3/4] Ingesting {total} docs in `{mode}` mode via {shim}")

    ok = fail = 0
    t0 = time.monotonic()
    for i, doc in enumerate(docs, 1):
        try:
            r = requests.post(
                f"{shim}/solr/{index}/update/json/docs", json=doc,
                headers={"Content-Type": "application/json"}, timeout=30)
            if not r.ok:
                fail += 1
                if fail <= 3:
                    print(f"    FAIL shim id={doc.get('id')} {r.status_code} {r.text[:120]}")
                continue
            if mode == "single":
                # mirror write to Solr so validation has parity
                rs = requests.post(
                    f"{solr}/solr/{index}/update/json/docs", json=doc,
                    headers={"Content-Type": "application/json"}, timeout=30)
                if not rs.ok:
                    fail += 1
                    if fail <= 3:
                        print(f"    FAIL solr-mirror id={doc.get('id')} {rs.status_code}")
                    continue
            ok += 1
        except Exception as e:
            fail += 1
            if fail <= 3:
                print(f"    EXC id={doc.get('id')} {e}")
        if i % 500 == 0 or i == total:
            print(f"    {i}/{total}  ok={ok} fail={fail}  {time.monotonic()-t0:.1f}s")

    # Commit + refresh (hits PR #2747 /update dispatcher)
    print("    committing...")
    requests.post(f"{shim}/solr/{index}/update", json={"commit": {}},
                  headers={"Content-Type": "application/json"}, timeout=30)
    if mode == "single":
        requests.post(f"{solr}/solr/{index}/update", json={"commit": {}},
                      headers={"Content-Type": "application/json"}, timeout=30)
    requests.post(f"{os_url}/{index}/_refresh", timeout=30)

    sc = requests.get(f"{solr}/solr/{index}/select",
                      params={"q": "*:*", "rows": 0, "wt": "json"}).json()["response"]["numFound"]
    oc = requests.get(f"{os_url}/{index}/_count").json()["count"]
    print(f"    VERIFY  Solr={sc}  OpenSearch={oc}  ingest ok={ok} fail={fail}")
    return ok, fail, sc, oc


def ensure_queries_file(index):
    """Use demo/queries_<index>.json, creating it from queries/queries.json if missing."""
    target = SANDBOX / "demo" / f"queries_{index}.json"
    if not target.exists():
        print(f"    queries file not found, generating {target.name} from queries/queries.json")
        target.write_text(QUERIES_SRC.read_text().replace(SOURCE_CORE, index))
    return target


def validate(cfg):
    qpath = ensure_queries_file(cfg["index"])
    with open(qpath) as f:
        queries = json.load(f)
    print(f"\n[4/4] Validating `{cfg['mode']}` mode via {cfg['shim_url']} with {len(queries)} queries")

    if cfg["mode"] == "single":
        results = run_all_queries(queries, cfg["solr_url"], cfg["shim_url"], timeout=30)
        return single_report(results, cfg)
    results = run_dual_queries_local(queries, cfg["shim_url"], timeout=30)
    return dual_report(results, cfg)


def single_report(results, cfg):
    total = len(results)
    solr_ok = sum(1 for r in results if r.solr_error is None)
    shim_ok = sum(1 for r in results if r.shim_error is None)
    solr_errs = [r for r in results if r.solr_error]
    shim_errs = [r for r in results if r.shim_error]
    print("\n" + "=" * 60)
    print(f"  SINGLE-MODE REPORT — queries={total}")
    print("=" * 60)
    print(f"  Solr succeeded:  {solr_ok}/{total}")
    print(f"  Shim succeeded:  {shim_ok}/{total}")
    if shim_errs:
        print(f"\n  Shim errors ({len(shim_errs)}):")
        for r in shim_errs[:10]:
            print(f"    {r.query_id:30s} {r.shim_error}")
    return {
        "mode": "single", "cfg": cfg,
        "total": total, "solr_ok": solr_ok, "shim_ok": shim_ok,
        "shim_errors": [{"id": r.query_id, "err": r.shim_error} for r in shim_errs],
        "solr_errors": [{"id": r.query_id, "err": r.solr_error} for r in solr_errs],
    }


import re

# Benign diffs to ignore when reclassifying PASS/FAIL:
#  - "path.to.field: foo vs [foo]"  scalar (Solr) vs single-element array (OpenSearch)
#  - "path.to.field: [foo] vs foo"  reverse direction
#  - "_version_: missing in solr/opensearch"  (framework field)
_SCALAR_VS_ARRAY_RE = re.compile(
    r'[\w.\[\]]+:\s*(?P<v>[^;\[\]]+?)\s+vs\s+\[(?P=v)\]\s*(?=;|,|\]|$)'
)
_ARRAY_VS_SCALAR_RE = re.compile(
    r'[\w.\[\]]+:\s*\[(?P<v>[^;\[\]]+?)\]\s+vs\s+(?P=v)\s*(?=;|,|\]|$)'
)
_MISSING_VERSION_RE = re.compile(
    r'[\w.\[\]]*_version_[\w.\[\]]*:\s*missing in (solr|opensearch)\s*(?=;|,|\]|$)',
    re.IGNORECASE)

def _strip_benign_diffs(details):
    if not details:
        return details
    cleaned = _SCALAR_VS_ARRAY_RE.sub("", details)
    cleaned = _ARRAY_VS_SCALAR_RE.sub("", cleaned)
    cleaned = _MISSING_VERSION_RE.sub("", cleaned)
    # collapse empty field-equality section after stripping
    cleaned = re.sub(r'field-equality\([^)]*\):FAIL\[\s*[;,\s]*\]', '', cleaned)
    # tidy punctuation
    cleaned = re.sub(r'(\s*;\s*)+', '; ', cleaned)
    cleaned = re.sub(r'\[\s*;\s*', '[', cleaned)
    cleaned = re.sub(r'\s*;\s*\]', ']', cleaned)
    cleaned = re.sub(r',\s*,', ',', cleaned)
    return cleaned.strip(' ,;')

def _effective_status(rec):
    """
    Reclassify a FAIL as PASS when only benign diffs remain. Returns
    (status, cleaned_details). Unknown statuses pass through unchanged.
    """
    raw = (rec.get("validation_status") or "").upper()
    details = rec.get("validation_details") or ""
    if raw != "FAIL":
        return raw or None, details
    cleaned = _strip_benign_diffs(details)
    # If the cleaned details no longer contain any real FAIL marker, mark as PASS
    if "FAIL" not in cleaned:
        return "PASS", cleaned
    return "FAIL", cleaned


def _extract_validation_headers(hdrs):
    """Dual shim emits X-Validation-Status (PASS/FAIL) and X-Validation-Details."""
    if not hdrs:
        return None, None
    # HTTP headers are case-insensitive; requests returns a CaseInsensitiveDict
    return hdrs.get("X-Validation-Status"), hdrs.get("X-Validation-Details")


def run_dual_queries_local(queries, dual_url, timeout=30):
    """
    Replacement for src.query_runner.run_all_dual_queries that captures the
    correct shim validation headers (X-Validation-Status / X-Validation-Details).
    Skips cursor-walk for now; sequential cursor queries are treated as single GETs.
    """
    from urllib.parse import urljoin
    results = []
    total = len(queries)
    for i, q in enumerate(queries, 1):
        path = q.get("path", "")
        if "wt=json" not in path:
            path += ("&" if "?" in path else "?") + "wt=json"
        url = urljoin(dual_url, path)
        rec = {
            "query_id": q.get("id", "?"),
            "category": q.get("category", "?"),
            "latency_ms": 0.0, "error": None,
            "validation_status": None, "validation_details": None,
            "solr_status": None, "opensearch_status": None,
        }
        t0 = time.monotonic()
        try:
            r = requests.get(url, timeout=timeout,
                             headers={"Accept-Encoding": "identity"})
            rec["latency_ms"] = (time.monotonic() - t0) * 1000
            if not r.ok:
                rec["error"] = f"HTTP {r.status_code}"
            vs, vd = _extract_validation_headers(r.headers)
            rec["validation_status"] = vs
            rec["validation_details"] = vd
            rec["solr_status"] = r.headers.get("X-Target-solr-StatusCode")
            rec["opensearch_status"] = r.headers.get("X-Target-opensearch-StatusCode")
        except Exception as e:
            rec["latency_ms"] = (time.monotonic() - t0) * 1000
            rec["error"] = str(e)
        status_raw = "ERR" if rec["error"] else (rec["validation_status"] or "no-hdr")
        # preview reclassification inline so the live log reflects final verdict
        eff, _ = _effective_status(rec)
        status = "ERR" if rec["error"] else (eff or "no-hdr")
        marker = "" if status == status_raw else f" (raw={status_raw})"
        print(f"  [{i}/{total}] {rec['query_id']}... {status}{marker} ({rec['latency_ms']:.0f}ms)")
        results.append(rec)
    return results


def dual_report(results, cfg):
    # Reclassify FAILs that contain only benign (scalar-vs-single-array, _version_) diffs
    for r in results:
        status, cleaned = _effective_status(r)
        r["effective_status"] = status
        r["effective_details"] = cleaned

    total = len(results)
    ok      = [r for r in results if r["error"] is None]
    err     = [r for r in results if r["error"]]
    passing = [r for r in ok if r["effective_status"] == "PASS"]
    failing = [r for r in ok if r["effective_status"] == "FAIL"]
    no_hdr  = [r for r in ok if not r["effective_status"]]
    # Count how many FAILs the raw shim reported vs how many we reclassified
    raw_fail = sum(1 for r in ok if (r["validation_status"] or "").upper() == "FAIL")
    reclassified = raw_fail - len(failing)

    print("\n" + "=" * 60)
    print(f"  DUAL-MODE REPORT — queries={total}")
    print("=" * 60)
    print(f"  Succeeded:            {len(ok)}")
    print(f"  Errored:              {len(err)}")
    print(f"  Validation PASS:      {len(passing)}   (after reclassify: +{reclassified} benign-only)")
    print(f"  Validation FAIL:      {len(failing)}")
    print(f"  No validation hdr:    {len(no_hdr)}")
    if ok:
        print(f"  Avg latency:          {sum(r['latency_ms'] for r in ok)/len(ok):.0f} ms")
    if failing:
        print(f"\n  Validation FAIL (first 10):")
        for r in failing[:10]:
            print(f"    {r['query_id']:30s} {(r['effective_details'] or '')[:220]}")
    if err:
        print(f"\n  Errors (first 10):")
        for r in err[:10]:
            print(f"    {r['query_id']:30s} {r['error']}")
    return {
        "mode": "dual", "cfg": cfg,
        "total": total, "ok": len(ok), "err": len(err),
        "validation_pass": len(passing),
        "validation_fail": len(failing),
        "benign_reclassified": reclassified,
        "no_header": len(no_hdr),
        "failures": [{"id": r["query_id"],
                      "details": r["effective_details"]} for r in failing],
        "errors": [{"id": r["query_id"], "err": r["error"]} for r in err],
    }


def save_report(report):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUT_DIR / f"validation_{ts}_{report['mode']}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\n  report saved: {path}")


# -------- orchestration --------
def run_cycle():
    cfg = gather_config()
    if cfg is None:
        print("  cancelled")
        return

    docs = [] if cfg["skip_ingest"] else generate_docs(cfg["num_docs"])

    if not cfg["skip_provision"]:
        provision_solr(cfg["index"], cfg["solr_url"])
        provision_opensearch(cfg["index"], cfg["os_url"])

    if not cfg["skip_ingest"]:
        ingest(docs, cfg)

    report = validate(cfg)
    save_report(report)


def main():
    try:
        while True:
            run_cycle()
            if not ask_yn("\nRun another cycle?", False):
                break
    except (KeyboardInterrupt, EOFError):
        print("\n  bye")


if __name__ == "__main__":
    main()
