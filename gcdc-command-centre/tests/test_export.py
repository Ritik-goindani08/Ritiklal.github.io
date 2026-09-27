"""The CSV export matches its declared types, and the Power BI project matches the export."""

import csv
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from gcdc.export import export_all, export_schema
from gcdc.pipeline import refresh

ROOT = Path(__file__).resolve().parents[1]


def _check_value(t: str, v: str) -> None:
    if t == "int64":
        int(v)
    elif t == "double":
        float(v)
    elif t == "date":
        date.fromisoformat(v)
    elif t == "dateTime":
        datetime.fromisoformat(v)


def test_export_values_match_declared_types(demo, cfg, tmp_path):
    out = tmp_path / "pbi"
    refresh(demo, cfg, export_dir=out)
    manifest = json.loads((out / "manifest.json").read_text())
    assert set(manifest["tables"]) == set(export_schema(demo))
    bad = []
    for table, cols in manifest["schema"].items():
        with open(out / f"{table}.csv", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            assert reader.fieldnames == list(cols), table
            for row in reader:
                for c, t in cols.items():
                    if row[c] == "":
                        continue
                    try:
                        _check_value(t, row[c])
                    except ValueError:
                        bad.append(f"{table}.{c} ({t}) = {row[c]!r}")
    assert not bad, bad[:20]
    assert not list(out.glob("*.tmp"))


def test_export_removes_stale_tables(demo, cfg, tmp_path):
    out = tmp_path / "pbi"
    out.mkdir()
    (out / "pbi_old_table.csv").write_text("x\n1\n")
    export_all(demo, cfg, out)
    assert not (out / "pbi_old_table.csv").exists()


@pytest.fixture(scope="module")
def pbip(tmp_path_factory):
    import sys

    sys.path.insert(0, str(ROOT / "powerbi"))
    import build_pbip

    out = tmp_path_factory.mktemp("pbip")
    assert build_pbip.main(["--out", str(out)]) == 0  # also asserts every visual field exists in the model
    return build_pbip, out


def test_pbip_has_ten_pages(pbip):
    _, out = pbip
    pages = json.loads((out / "GCDC Command Centre.Report/definition/pages/pages.json").read_text())
    assert len(pages["pageOrder"]) == 10


def test_pbip_measures_and_relationships_reference_real_columns(pbip, conn):
    import re

    b, _ = pbip
    schema = export_schema(conn)
    cols = {(b.table_name(t), c): ty for t, cs in schema.items() for c, ty in cs.items()}
    tables = {b.table_name(t) for t in schema}
    measures = {m.name for m in b.MEASURES}
    problems = []
    for ft, fc, tt, tc, _active in b.RELATIONSHIPS:
        if (ft, fc) not in cols or (tt, tc) not in cols:
            problems.append(f"relationship {ft}.{fc} -> {tt}.{tc}")
        elif cols[(ft, fc)] != cols[(tt, tc)]:
            problems.append(f"relationship type mismatch {ft}.{fc}")
    for m in b.MEASURES:
        for t, c in re.findall(r"'([^']+)'\[([^\]]+)\]", m.expr):
            if t not in tables or (t, c) not in cols:
                problems.append(f"{m.name}: {t}[{c}]")
        bare = re.sub(r"'[^']+'\[[^\]]+\]", "", m.expr)
        problems += [f"{m.name}: [{r}]" for r in re.findall(r"\[([^\]]+)\]", bare) if r not in measures]
        if m.expr.count("(") != m.expr.count(")"):
            problems.append(f"{m.name}: unbalanced parentheses")
    assert not problems, problems


def test_pbip_json_matches_microsoft_schemas(pbip):
    """Runs when the Microsoft json-schemas repo is available locally (GCDC_PBIR_SCHEMAS=<clone>)."""
    import os

    local = os.environ.get("GCDC_PBIR_SCHEMAS")
    if not local:
        pytest.skip("set GCDC_PBIR_SCHEMAS to a clone of github.com/microsoft/json-schemas to validate")
    from jsonschema import Draft7Validator, validators
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT7

    base = "https://developer.microsoft.com/json-schemas/"

    def retrieve(uri):
        path = Path(local) / uri.removeprefix(base).split("#")[0]
        return Resource.from_contents(json.loads(path.read_text(encoding="utf-8")), default_specification=DRAFT7)

    registry = Registry(retrieve=retrieve)
    _, out = pbip
    errors = []
    for f in out.rglob("*"):
        if f.suffix not in (".json", ".pbip", ".pbir", ".pbism"):
            continue
        doc = json.loads(f.read_text(encoding="utf-8"))
        sid = doc.get("$schema") if isinstance(doc, dict) else None
        if not sid:
            continue
        schema = retrieve(sid).contents
        v = validators.validator_for(schema, default=Draft7Validator)(schema, registry=registry)
        errors += [f"{f.name}: {e.message[:200]}" for e in v.iter_errors(doc)]
    assert not errors, errors[:10]
