# Chicago TIF Dashboard

An interactive map + data explorer for Chicago Tax Increment Financing (TIF) districts,
focused on **money raised in one TIF that gets spent on a project outside that district** —
either geographically (project address outside the funding TIF polygon) or
governmentally (paid to another unit of local government via an intergovernmental
agreement, e.g. CPS, the Park District, CTA, City Colleges, the library).

## What it covers

- **132 TIF districts** — 100 active + 32 recently expired (any TIF with post-2016 activity).
- **308 RDA / IGA projects** approved 2016–2026.
- **660 transfer records** combining project-level money movements with the per-year
  cash-flow lines from the annual reports (declared surplus, transfers to CPS/library, etc.).
- **2017–2024 annual financial reports** for every district (fund balances, property-tax
  increment, total expenditures, surplus distributions).

## How to use it locally

Just double-click **`index.html`**. The dashboard ships with `data/*.js` mirrors of
every data file, so it works directly from `file://` URLs — no local server needed.

If you prefer a server (so the `.json` files load instead of the `.js` mirrors):

```
cd "Chicago TIF Dashboard"
python3 -m http.server 8000
# then open http://localhost:8000/
```

## How to host it online for free

The dashboard is a static folder — no backend. Two easy paths:

**GitHub Pages (recommended):**
1. Create a new GitHub repo and push the contents of this folder to it.
2. In the repo's Settings → Pages, set Source to `main` branch, root folder.
3. GitHub serves it at `https://<your-user>.github.io/<repo-name>/`.

**Netlify drag-and-drop:**
1. Go to https://app.netlify.com/drop.
2. Drag this folder onto the page. Done — you get a public URL immediately.

## Data sources

All data comes from the City of Chicago Data Portal (Socrata):

| Dataset | ID | Used for |
|---|---|---|
| Boundaries – TIF Districts | `eejr-xtfb` | active district polygons |
| Boundaries – TIF Districts (Dep. April 2023) | `di8g-4wjz` | recently-expired district polygons |
| TIF Funded RDA and IGA Projects | `mex4-ppfc` | project records with lat/lon, $, descriptions |
| TIF Annual Report – Projects | `72uz-ikdv` | per-year project payments |
| TIF Annual Report – Special Tax Allocation Fund | `qm7s-3ctt` | per-year fund balance, surplus, transfers |
| TIF Annual Report – Itemized Expenditures | `umwj-yc4m` | per-year transfers to CPS, library, etc. |

Reference dashboards (for cross-checking):

- Chicago Office of Inspector General — TIF Cash Flows
  https://igchicago.org/information-portal/data-dashboards/chicago-tif-districts-cash-flows/
- City of Chicago TIF Portal
  https://webapps1.chicago.gov/ChicagoTif/

## Definitions

- **IGA (Intergovernmental Agreement)** — TIF money paid to another unit of local
  government (CPS, Park District, City Colleges, CTA, Public Library, CHA, etc.).
- **RDA (Redevelopment Agreement)** — TIF money paid to a private developer under a
  City Council-approved redevelopment agreement.
- **Declared surplus** — money the TIF formally returns to the County collector, which
  distributes it pro-rata to overlapping taxing bodies.
- **Porting (TIF-to-TIF transfer)** — under 65 ILCS 5/11-74.4-4(q), surplus from one
  TIF can be transferred to a contiguous TIF. The city does not publish this as a
  dedicated dataset; in the reports it shows up indirectly as a project expenditure in
  the receiving TIF after the inter-TIF transfer.
- **Out-of-district project** — a TIF-funded project whose physical address (lat/lon)
  does not fall inside the funding TIF district polygon. Often eligible because the
  project is on a "contiguous parcel" or is a qualifying intergovernmental partner site.

## Caveats (please read)

- **`approved_amount` is the maximum approved subsidy**, not necessarily the dollars
  actually disbursed. Disbursements happen over multiple years and are reported in the
  Annual Report Projects dataset (`72uz-ikdv`) — those per-year payments are not
  currently rolled into the dashboard tooltips but are part of the table data feed.
- **Multi-TIF projects** (a project co-funded by two or more TIFs) show one row per
  funding TIF, with the approved amount split evenly. The real split may differ;
  the dataset doesn't publish the per-TIF share. Look for "co-funded with" in the
  project description.
- **CDC date** (`cdc_date`) is the date the project was approved by the Community
  Development Commission. The funds typically flow in subsequent years.
- **Recently-expired TIFs** (dashed outline on the map) are pulled from the city's
  most recent deprecated boundary set (April 2023). Some long-expired TIFs from
  before that may have project records here but no polygon to show on the map —
  those still appear in the table.

## Rebuilding the data

To refresh from the live Chicago Data Portal:

```
cd data/
python3 ../build_dashboard_data.py
```

The script downloads small, recent samples and re-runs the geocoding/classification
pipeline. (You may need `pip install shapely --break-system-packages` once.)
