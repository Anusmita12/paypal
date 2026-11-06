def test_inventory_import_previews_errors_and_applies_nothing(api):
    csv_text = "sku,on_hand\nCHG-65W-PD,40\nNOPE-1,3\nPB-10K,-2\nCHG-65W-PD,5\n"
    prev = api.post("/api/imports/preview", {"kind": "inventory", "csv_text": csv_text}).json()
    assert prev["valid_rows"] == 1 and prev["error_count"] == 3
    assert [e["row"] for e in prev["errors"]] == [3, 4, 5]
    r = api.post(f"/api/imports/{prev['import_id']}/apply")
    assert r.status_code == 409 and r.json()["error"]["code"] == "import_has_errors"
    inv = {p["sku"]: p for p in api.get("/api/inventory").json()["products"]}
    assert inv["CHG-65W-PD"]["on_hand"] != 40


def test_inventory_import_applies_after_confirmation(api):
    prev = api.post("/api/imports/preview", {"kind": "inventory", "csv_text": "sku,on_hand\nCHG-65W-PD,40\n"}).json()
    assert prev["status"] == "previewed"
    inv = {p["sku"]: p for p in api.get("/api/inventory").json()["products"]}
    assert inv["CHG-65W-PD"]["on_hand"] != 40  # preview alone changes nothing
    assert api.post(f"/api/imports/{prev['import_id']}/apply").json()["status"] == "applied"
    inv = {p["sku"]: p for p in api.get("/api/inventory").json()["products"]}
    assert inv["CHG-65W-PD"]["on_hand"] == 40 and inv["CHG-65W-PD"]["need"] == 0


def test_sales_import_rejects_duplicates_and_bad_rows(api):
    good = "sale_id,sku,date,units\nS-1,PB-10K,2026-11-03,4\nS-2,PB-10K,2026-11-02,1\n"
    prev = api.post("/api/imports/preview", {"kind": "sales", "csv_text": good}).json()
    assert prev["error_count"] == 0
    before = api.get("/api/sales/summary").json()["total_units"]
    api.post(f"/api/imports/{prev['import_id']}/apply")
    assert api.get("/api/sales/summary").json()["total_units"] == before + 5

    again = api.post("/api/imports/preview", {"kind": "sales", "csv_text": good}).json()
    assert again["error_count"] == 2 and "already imported" in again["errors"][0]["message"]
    bad = "sale_id,sku,date,units\nS-9,PB-10K,2026-11-04,1\nS-10,PB-10K,yesterday,1\n"
    assert api.post("/api/imports/preview", {"kind": "sales", "csv_text": bad}).json()["error_count"] == 2


def test_import_bounds_and_schema(api, other_api):
    r = api.post("/api/imports/preview", {"kind": "inventory", "csv_text": "sku,qty\nPB-10K,1\n"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_import_header"
    big = "sku,on_hand\n" + "PB-10K,1\n" * 2001
    assert api.post("/api/imports/preview", {"kind": "inventory", "csv_text": big}).status_code == 413
    prev = api.post("/api/imports/preview", {"kind": "inventory", "csv_text": "sku,on_hand\nPB-10K,1\n"}).json()
    assert other_api.post(f"/api/imports/{prev['import_id']}/apply").status_code == 404
