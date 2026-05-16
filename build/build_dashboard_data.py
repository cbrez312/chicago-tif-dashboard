#!/usr/bin/env python3
"""
Build the enriched JSON files the Chicago TIF dashboard consumes.

Inputs (downloaded from data.cityofchicago.org):
  - tif_boundaries.geojson            (eejr-xtfb) current TIF district polygons
  - rda_iga_projects.json             (mex4-ppfc) RDA + IGA project records (geocoded)
  - annual_report_projects.json       (72uz-ikdv) per-year project records w/ payments
  - special_tax_fund.json             (qm7s-3ctt) per-year per-TIF cash-flow analysis
  - itemized_expenditures.json        (umwj-yc4m) per-year per-TIF expenditure categories

Outputs (written to ../dashboard/data/):
  - tif_boundaries.geojson            (passthrough, slimmed)
  - projects.json                     enriched project list with classification + out-of-district flag
  - tif_summary.json                  per-TIF aggregate stats
  - transfers.json                    inter-governmental and inter-district money movement records
  - data_manifest.json                lineage + freshness metadata
"""
import json, re, datetime, os, sys
from collections import defaultdict
from shapely.geometry import shape, Point, mapping

HERE      = os.path.dirname(os.path.abspath(__file__))
DASHBOARD = os.path.abspath(os.path.join(HERE, '..', 'dashboard', 'data'))
os.makedirs(DASHBOARD, exist_ok=True)

WINDOW_START_YEAR = 2017   # earliest year the city's annual-report datasets cover
# Project approval-date cutoff for the "10-year window" the dashboard exposes
# (dashboard UI still lets the user slide between 5 and 10 years).
BOUNDARY_ADJACENT_METERS = 25  # within this distance of a TIF boundary the in/out flag is
                               # smaller than typical geocoder error — treat as "adjacent"
DEG_TO_M_AT_CHICAGO_LAT = 95000  # rough mean of (111km/deg lat, 83km/deg lon) good enough for adjacency

# ---------------------------------------------------------------------------
# 1. Load boundaries and build name->polygon index
#    We merge ACTIVE boundaries (eejr-xtfb) with the most recent DEPRECATED set
#    (di8g-4wjz, April 2023) so that recently-expired TIFs still have polygons
#    available for the geographic point-in-polygon check.
# ---------------------------------------------------------------------------
with open(os.path.join(HERE, 'tif_boundaries.geojson')) as f:
    active = json.load(f)
try:
    with open(os.path.join(HERE, 'tif_boundaries_extended.geojson')) as f:
        extended = json.load(f)
except FileNotFoundError:
    extended = {'features': []}

# Normalize district names (the project datasets use slightly different casing/spaces)
def norm(name):
    if not name: return ''
    s = name.lower().strip()
    s = re.sub(r'\s+', ' ', s)
    s = s.replace('–', '-').replace('—', '-')
    return s

polygons    = {}    # norm(name) -> shapely geometry
canonical   = {}    # norm(name) -> display name
active_set  = set() # norm(name) of CURRENTLY ACTIVE TIFs only
for feat in active['features']:
    props = feat['properties']
    name  = (props.get('name') or props.get('name_trim') or '').strip()
    if not name: continue
    polygons[norm(name)]  = shape(feat['geometry'])
    canonical[norm(name)] = name
    active_set.add(norm(name))

# Add expired TIFs from the deprecated set (only if not already present)
added_expired = 0
for feat in extended['features']:
    props = feat['properties']
    name  = (props.get('name') or props.get('name_trim') or '').strip()
    if not name: continue
    n = norm(name)
    if n not in polygons:
        polygons[n]  = shape(feat['geometry'])
        canonical[n] = name
        added_expired += 1

print(f'Loaded {len(active_set)} active TIF polygons + {added_expired} recently-expired TIF polygons '
      f'(total {len(polygons)}).')

# ---------------------------------------------------------------------------
# 2. Classify projects from the RDA/IGA dataset (the geocoded one)
# ---------------------------------------------------------------------------
with open(os.path.join(HERE, 'rda_iga_projects.json')) as f:
    rda_iga = json.load(f)

# Heuristic to label the kind of money movement for each record
SISTER_AGENCIES = [
    ('Chicago Public Schools', 'CPS'),
    ('Board of Education',     'CPS'),
    ('Chicago Park District',  'Park District'),
    ('City Colleges',          'City Colleges'),
    ('Chicago Transit',        'CTA'),
    ('CTA',                    'CTA'),
    ('Public Library',         'Chicago Public Library'),
    ('Library',                'Chicago Public Library'),
    ('Housing Authority',      'CHA'),
    ('Department of Aviation', 'CDA'),
    ('Aviation',               'CDA'),
    ('Chicago Police',         'CPD'),
    ('Fire Department',        'CFD'),
    ('Cook County',            'Cook County'),
    ('Public Building',        'PBC'),
]

def classify_record(rec):
    """Return (kind, partner) for a project record.

    kind ∈ {'IGA', 'RDA', 'Unknown'} where IGA = intergovernmental agreement
    (TIF dollars going to a sister agency such as CPS or the Park District),
    RDA = redevelopment agreement (TIF dollars supporting a private project).
    """
    name = (rec.get('project_name') or '').strip()
    dev  = (rec.get('developer')    or '').strip()
    desc = (rec.get('project_description') or '').strip()
    blob = f'{name} | {dev} | {desc}'
    is_iga = name.upper().startswith('IGA') or 'INTERGOVERNMENTAL' in blob.upper()
    partner = ''
    for needle, label in SISTER_AGENCIES:
        if needle.lower() in blob.lower():
            partner = label
            if not is_iga: is_iga = True
            break
    if is_iga: return 'IGA', partner or 'Other public agency'
    return 'RDA', dev

projects_out = []
skipped_nogeo = 0
skipped_notif = 0
skipped_oldyr = 0
out_of_district_count = 0
iga_count = 0
multi_tif_records = 0
for rec in rda_iga:
    # require lat/lon
    try:
        lat = float(rec.get('latitude'))
        lon = float(rec.get('longitude'))
    except (TypeError, ValueError):
        skipped_nogeo += 1
        continue
    pt = Point(lon, lat)
    raw_tif = (rec.get('tif_district') or '').strip()
    # Some records list multiple TIFs joined by commas (jointly-funded projects)
    tif_names = [t.strip() for t in raw_tif.split(',') if t.strip()]
    if len(tif_names) > 1: multi_tif_records += 1
    # date filter: keep approvals on/after 2016
    cdc = rec.get('cdc_date') or ''
    year = None
    if cdc:
        try: year = int(cdc[:4])
        except ValueError: year = None
    if year is None or year < (WINDOW_START_YEAR - 1):  # allow 2016 too
        skipped_oldyr += 1
        continue
    kind, partner = classify_record(rec)
    try:    amt = float(rec.get('approved_amount') or 0)
    except: amt = 0
    try:    tot = float(rec.get('total_project_cost') or 0)
    except: tot = 0
    # Build one project row PER funding TIF (so a 3-TIF project shows up under each)
    for tif in tif_names:
        n = norm(tif)
        poly = polygons.get(n)
        boundary_distance_m = None     # None when no polygon available
        boundary_status     = 'unknown'
        if poly is None:
            outside = None
            skipped_notif += 1
        else:
            try:
                inside_poly = poly.contains(pt)
                outside     = not inside_poly
                # Distance from point to polygon BOUNDARY (always >= 0, in degrees)
                # We use the unsimplified source polygon for this so the distance is faithful
                # to the city's published geometry.
                d_deg = poly.boundary.distance(pt)
                boundary_distance_m = round(d_deg * DEG_TO_M_AT_CHICAGO_LAT, 1)
                if boundary_distance_m <= BOUNDARY_ADJACENT_METERS:
                    boundary_status = 'adjacent'           # within geocoder noise of the line
                elif inside_poly:
                    boundary_status = 'inside'
                else:
                    boundary_status = 'outside'
            except Exception:
                outside = None
        if outside is True: out_of_district_count += 1
        if kind == 'IGA':   iga_count += 1
        projects_out.append({
            'id'          : str(rec.get('id')) + ('' if len(tif_names)==1 else f'-{n[:8]}'),
            'tif_district': canonical.get(n, tif),
            'tif_is_active': n in active_set,
            'co_funded_with': [t for t in tif_names if t != tif],
            'name'        : (rec.get('project_name') or '').strip(),
            'address'     : (rec.get('address') or '').strip(),
            'developer'   : (rec.get('developer') or '').strip(),
            'description' : (rec.get('project_description') or '').strip(),
            'cdc_date'    : cdc[:10] if cdc else '',
            'year'        : year,
            'approved_amount'   : amt / len(tif_names),
            'approved_amount_total': amt,
            'total_project_cost': tot,
            'lat': lat, 'lon': lon,
            'ward'           : rec.get('ward') or '',
            'community_area' : rec.get('community_area') or '',
            'kind'           : kind,
            'partner'        : partner,
            'outside_funding_tif'  : outside,                # True / False / None
            'boundary_distance_m'  : boundary_distance_m,    # signed: meters from polygon edge (positive number)
            'boundary_status'      : boundary_status,        # inside / adjacent / outside / unknown
            'money_leaves_district': bool(outside) or (kind == 'IGA'),
        })

adjacent_count = sum(1 for p in projects_out if p['boundary_status'] == 'adjacent')
print(f'Projects kept: {len(projects_out)} | geographically out-of-district: {out_of_district_count} | '
      f'IGAs (inter-governmental): {iga_count} | boundary-adjacent (within {BOUNDARY_ADJACENT_METERS}m): {adjacent_count} | '
      f'multi-TIF records: {multi_tif_records} | skipped (no geo): {skipped_nogeo} | '
      f'skipped (no boundary match): {skipped_notif} | skipped (pre-{WINDOW_START_YEAR-1}): {skipped_oldyr}')

# ---------------------------------------------------------------------------
# 3. Per-TIF summary stats from the Special Tax Allocation Fund dataset
# ---------------------------------------------------------------------------
with open(os.path.join(HERE, 'special_tax_fund.json')) as f:
    stf = json.load(f)
with open(os.path.join(HERE, 'itemized_expenditures.json')) as f:
    items = json.load(f)

def fnum(x):
    try:    return float(x)
    except: return 0.0

# Build a lookup: (tif_district, year) -> itemized categories
items_lookup = {}
for r in items:
    key = (r.get('tif_district',''), str(r.get('report_year','')))
    items_lookup[key] = r

per_tif_year = []
for r in stf:
    yr = str(r.get('report_year',''))
    if not yr.isdigit() or int(yr) < WINDOW_START_YEAR: continue
    tif = r.get('tif_district','')
    item = items_lookup.get((tif, yr), {})
    per_tif_year.append({
        'tif_district' : tif,
        'tif_number'   : r.get('tif_number',''),
        'year'         : int(yr),
        'fund_balance' : fnum(r.get('fund_balance')),
        'property_tax_increment': fnum(r.get('property_tax_increment_current')),
        'total_expenditure'     : fnum(r.get('total_expenditure')),
        'cash_expenses'         : fnum(r.get('cash_expenses')),
        'distribution_of_surplus': fnum(r.get('distribution_of_surplus')),
        'transfers_municipal'    : fnum(r.get('transfers_municipal')),
        # itemized transfers to overlapping taxing bodies (these are "money leaving the TIF")
        'transfer_to_school_districts' : fnum(item.get('school_districts')),
        'transfer_to_library_districts': fnum(item.get('library_districts')),
    })

# ---------------------------------------------------------------------------
# 4. Build a TIF-level summary (for the table when no district is selected)
# ---------------------------------------------------------------------------
summary = defaultdict(lambda: {
    'tif_district':'', 'tif_number':'',
    'years':set(),
    'total_property_tax_increment':0.0,
    'total_expenditure':0.0,
    'total_distribution_of_surplus':0.0,
    'total_transfers_municipal':0.0,
    'total_transfer_to_school_districts':0.0,
    'total_transfer_to_library_districts':0.0,
    'latest_fund_balance':0.0,
    'latest_year':0,
    'geographic_out_count':0,         # projects physically outside funding TIF
    'geographic_out_dollars':0.0,
    'iga_count':0,                    # inter-governmental agreements regardless of geo
    'iga_dollars':0.0,
    'rda_count':0,
    'rda_dollars':0.0,
})
for r in per_tif_year:
    s = summary[r['tif_district']]
    s['tif_district'] = r['tif_district']
    s['tif_number']   = r['tif_number']
    s['years'].add(r['year'])
    s['total_property_tax_increment']        += r['property_tax_increment']
    s['total_expenditure']                   += r['total_expenditure']
    s['total_distribution_of_surplus']       += r['distribution_of_surplus']
    s['total_transfers_municipal']           += r['transfers_municipal']
    s['total_transfer_to_school_districts']  += r['transfer_to_school_districts']
    s['total_transfer_to_library_districts'] += r['transfer_to_library_districts']
    if r['year'] > s['latest_year']:
        s['latest_year']         = r['year']
        s['latest_fund_balance'] = r['fund_balance']

for p in projects_out:
    s = summary[p['tif_district']]
    s['tif_district'] = s['tif_district'] or p['tif_district']
    if p['outside_funding_tif']:
        s['geographic_out_count']   += 1
        s['geographic_out_dollars'] += p['approved_amount']
    if p['kind'] == 'IGA':
        s['iga_count']   += 1
        s['iga_dollars'] += p['approved_amount']
    elif p['kind'] == 'RDA':
        s['rda_count']   += 1
        s['rda_dollars'] += p['approved_amount']

summary_list = []
for k,v in summary.items():
    v['years'] = sorted(v['years'])
    v['years_covered'] = f"{v['years'][0]}-{v['years'][-1]}" if v['years'] else ''
    del v['years']
    summary_list.append(v)
summary_list.sort(key=lambda x: -(x['geographic_out_dollars'] + x['iga_dollars']))

# ---------------------------------------------------------------------------
# 5. Transfers-only view (the "money leaving the district" lens)
# ---------------------------------------------------------------------------
transfers = []
# (a) per-year line-item transfers from the financial reports
for r in per_tif_year:
    for category, label in [
        ('distribution_of_surplus',       'Declared surplus to overlapping taxing bodies'),
        ('transfers_municipal',           'Transfer to municipal corporate fund'),
        ('transfer_to_school_districts',  'Payment to Chicago Public Schools (IGA / overlapping)'),
        ('transfer_to_library_districts', 'Payment to Chicago Public Library (IGA / overlapping)'),
    ]:
        amt = r[category]
        if amt and amt > 0:
            transfers.append({
                'tif_district': r['tif_district'],
                'year'        : r['year'],
                'kind'        : 'Financial transfer',
                'category'    : label,
                'amount'      : amt,
                'source'      : 'Annual Report - Special Tax Allocation Fund + Itemized Expenditures',
            })
# (b) project-level money movements where money leaves the district
#     either geographically (out-of-district) or to another government (IGA).
for p in projects_out:
    if not p['money_leaves_district']: continue
    if p['kind'] == 'IGA':
        cat = f"Inter-governmental payment to {p['partner']}"
        kind_label = 'IGA (out-of-district)' if p['outside_funding_tif'] else 'IGA (in-district, inter-governmental)'
        source = 'RDA & IGA Projects dataset (IGA flagged)'
    else:
        cat  = 'Out-of-district redevelopment project'
        kind_label = 'RDA (out-of-district)'
        source = 'RDA & IGA Projects dataset (geocoded outside funding TIF polygon)'
    transfers.append({
        'tif_district'   : p['tif_district'],
        'year'           : p['year'],
        'kind'           : kind_label,
        'category'       : cat,
        'amount'         : p['approved_amount'],
        'source'         : source,
        'project_name'   : p['name'],
        'project_address': p['address'],
        'project_lat'    : p['lat'],
        'project_lon'    : p['lon'],
    })

# ---------------------------------------------------------------------------
# 6. Slim the boundaries GeoJSON. Include active + recently-deprecated districts
#    (mark active vs expired so the map can style them differently).
# ---------------------------------------------------------------------------
slim = {'type':'FeatureCollection','features':[]}
seen = set()
SIMPLIFY_TOL = 0.00015   # ≈ 17m at Chicago's latitude — visually indistinguishable, much smaller payload

def round_geom(g, n=5):
    """Recursively round all coordinates in a GeoJSON geometry dict to n decimals."""
    def r(c):
        if isinstance(c, (int, float)): return round(c, n)
        return [r(x) for x in c]
    return {'type': g['type'], 'coordinates': r(g['coordinates'])}

def add(feat, is_active):
    p = feat['properties']
    name = (p.get('name') or '').strip()
    if not name or name in seen: return
    seen.add(name)
    geom = shape(feat['geometry']).simplify(SIMPLIFY_TOL, preserve_topology=True)
    slim['features'].append({
        'type':'Feature',
        'properties': {
            'name'      : name,
            'tif_number': (p.get('ref')  or '').strip(),
            'type'      : (p.get('type') or '').strip(),
            'approval'  : (p.get('approval_d') or '')[:10],
            'expiration': (p.get('expiration') or '')[:10],
            'wards'     : p.get('wards',''),
            'is_active' : is_active,
        },
        'geometry': round_geom(mapping(geom), 5)
    })
for feat in active['features']:    add(feat, True)
for feat in extended['features']:  add(feat, False)
print(f'Slimmed boundary file: {len(slim["features"])} polygons '
      f'({sum(1 for f in slim["features"] if f["properties"]["is_active"])} active, '
      f'{sum(1 for f in slim["features"] if not f["properties"]["is_active"])} recently-expired).')

# ---------------------------------------------------------------------------
# 7. Write outputs + manifest
# ---------------------------------------------------------------------------
def dual_write(name, varname, payload):
    """Write `<name>.json` and a `<name>.js` mirror (var assignment for file:// loads)."""
    with open(os.path.join(DASHBOARD, name + '.json'), 'w') as f:
        json.dump(payload, f)
    with open(os.path.join(DASHBOARD, name + '.js'), 'w') as f:
        f.write(f'window.{varname} = ')
        json.dump(payload, f)
        f.write(';\n')

dual_write('tif_boundaries', 'TIF_BOUNDARIES', slim)
dual_write('projects',       'TIF_PROJECTS',   projects_out)
dual_write('tif_summary',    'TIF_SUMMARY',    summary_list)
dual_write('transfers',      'TIF_TRANSFERS',  transfers)

# convenience: also emit .geojson (some viewers expect that extension)
with open(os.path.join(DASHBOARD, 'tif_boundaries.geojson'), 'w') as f:
    json.dump(slim, f)

manifest = {
    'generated_utc': datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z',
    'sources': [
        {'id':'eejr-xtfb', 'name':'Boundaries - Tax Increment Financing Districts',
         'url':'https://data.cityofchicago.org/d/eejr-xtfb',
         'use':'TIF district polygons (active TIFs)'},
        {'id':'mex4-ppfc', 'name':'TIF Funded RDA and IGA Projects',
         'url':'https://data.cityofchicago.org/d/mex4-ppfc',
         'use':'Project-level money movement with addresses, lat/lon, RDA/IGA classification'},
        {'id':'72uz-ikdv', 'name':'TIF Annual Report - Projects',
         'url':'https://data.cityofchicago.org/d/72uz-ikdv',
         'use':'Per-year project payments and statuses'},
        {'id':'qm7s-3ctt', 'name':'TIF Annual Report - Special Tax Allocation Fund Analysis',
         'url':'https://data.cityofchicago.org/d/qm7s-3ctt',
         'use':'Per-year fund balance, surplus distributions, transfers'},
        {'id':'umwj-yc4m', 'name':'TIF Annual Report - Itemized List of Expenditures',
         'url':'https://data.cityofchicago.org/d/umwj-yc4m',
         'use':'Per-year expenditure categories incl. transfers to school/library districts'},
    ],
    'reference_dashboards': [
        {'name':'Chicago Office of Inspector General - TIF Cash Flows',
         'url':'https://igchicago.org/information-portal/data-dashboards/chicago-tif-districts-cash-flows/'},
        {'name':'City of Chicago TIF Portal',
         'url':'https://webapps1.chicago.gov/ChicagoTif/'},
    ],
    'definitions': {
        'IGA (Intergovernmental Agreement)':
            'TIF money paid to another unit of local government (CPS, Park District, '
            'City Colleges, CTA, Public Library, CHA, etc.) under an inter-agency agreement.',
        'RDA (Redevelopment Agreement)':
            'TIF money paid to a private developer under a City Council-approved RDA.',
        'Declared surplus':
            'Money the TIF formally returns to the County collector, which distributes it '
            'pro-rata to overlapping taxing bodies (city, county, CPS, parks, library, MWRD, etc.).',
        'Porting (TIF-to-TIF transfer)':
            'Under 65 ILCS 5/11-74.4-4(q), surplus from one TIF can be transferred to a contiguous '
            'TIF. The city does not publish a single dedicated dataset; in the reports this typically '
            'shows up as a project expenditure in the receiving TIF after an inter-TIF transfer.',
        'Transfer to municipal corporate fund':
            'TIF dollars moved to the city\'s general corporate fund (rare; subject to legal limits).',
        'Out-of-district project':
            'A TIF-funded project whose physical address (lat/lon) does NOT fall inside the funding '
            'TIF district polygon. Often eligible because the project is a "contiguous parcel" or a '
            'qualifying intergovernmental partner site.',
    },
    'caveats': [
        {
            'title': 'approved_amount is the MAXIMUM approved subsidy, not dollars disbursed',
            'detail': 'The "Amount ($)" column for RDA and IGA projects shows the maximum subsidy '
                      'approved by the Community Development Commission, not what has actually been '
                      'paid out. Disbursements happen over multiple years and are reported separately '
                      'in the TIF Annual Report - Projects dataset (72uz-ikdv) which contains '
                      'current_year_payments per project per year. Those per-year payments are NOT '
                      'currently rolled into the project tooltips here. If a tooltip shows $98M but '
                      'a TIF reader knows only ~$30M has been disbursed so far, this is why.'
        },
        {
            'title': 'Multi-TIF (co-funded) projects are split evenly across funding TIFs',
            'detail': '24 records in the RDA/IGA Projects dataset list multiple funding TIFs joined '
                      'by commas. The city does not publish the per-TIF share. The dashboard shows '
                      'one row per funding TIF with the approved amount divided evenly. Each row '
                      'carries a co_funded_with array preserving the other TIFs involved.'
        },
        {
            'title': 'CDC date is the approval date, not the disbursement date',
            'detail': 'The "Year" column derives from cdc_date — the date the Community Development '
                      'Commission approved the project. Actual TIF dollars typically flow in years '
                      'following approval. Year-range filtering uses approval year.'
        },
        {
            'title': 'Recently-expired TIFs are included from the deprecated April-2023 boundary set',
            'detail': 'The current Boundaries dataset (eejr-xtfb) lists only 100 active TIFs. To show '
                      'post-2016 activity for recently-expired districts (Wilson Yard, Western Avenue '
                      'North/South, Portage Park, etc.), the dashboard also loads the most recent '
                      'deprecated boundary set (di8g-4wjz, 130 features). Long-expired TIFs with no '
                      'polygon in either set still appear in the table but not on the map.'
        },
        {
            'title': 'Boundary-adjacent projects (within 25m of a TIF edge) are flagged in gold',
            'detail': 'A project whose geocoded lat/lon is within 25 meters of the funding TIF '
                      'boundary is shown in GOLD rather than red (outside) or kind-color (inside). '
                      '25m is roughly the width of a Chicago street and is within typical geocoder '
                      'error — for these projects the inside/outside classification is essentially '
                      'noise. Each project popup shows the exact distance from the boundary, and '
                      'boundary distance is computed using the city\'s FULL-RESOLUTION polygon, '
                      'not the simplified display polygon, so the distance is faithful to the '
                      'published city geometry.'
        },
        {
            'title': 'IGAs to CTA capital projects dwarf everything else',
            'detail': 'The Red Line Extension TIF and the Red and Purple Modernization (RPM) TIF '
                      'were created specifically to fund CTA capital projects via single, very large '
                      'intergovernmental agreements ($959M and $622M respectively). These are real '
                      'and verifiable but will skew district-level aggregates and totals.'
        },
        {
            'title': '"Porting" (inter-TIF transfers under 65 ILCS 5/11-74.4-4(q)) is not a discrete dataset',
            'detail': 'The city does not publish a single TIF-to-TIF transfer dataset. Where money '
                      'is moved from one TIF to a contiguous TIF, it generally appears as a project '
                      'expenditure in the RECEIVING TIF rather than as a transfer line in the sending '
                      'TIF. A complete porting view would require manually reconciling annual reports.'
        },
    ],
    'counts': {
        'tif_districts'             : len(slim['features']),
        'projects_loaded'           : len(projects_out),
        'projects_geographic_out'   : sum(1 for p in projects_out if p['outside_funding_tif']),
        'projects_boundary_adjacent': sum(1 for p in projects_out if p['boundary_status'] == 'adjacent'),
        'projects_iga'              : sum(1 for p in projects_out if p['kind'] == 'IGA'),
        'projects_money_leaves'     : sum(1 for p in projects_out if p['money_leaves_district']),
        'transfer_records'          : len(transfers),
        'tif_year_records'          : len(per_tif_year),
    },
    'boundary_adjacency_threshold_m': BOUNDARY_ADJACENT_METERS,
    'time_window': {
        'earliest_year_in_financial_data': WINDOW_START_YEAR,
        'latest_year_in_financial_data'  : max(r['year'] for r in per_tif_year),
        'project_approval_date_range'    : 'cdc_date >= 2016-01-01 through latest available',
    },
}
with open(os.path.join(DASHBOARD, 'data_manifest.json'), 'w') as f:
    json.dump(manifest, f, indent=2)
with open(os.path.join(DASHBOARD, 'data_manifest.js'), 'w') as f:
    f.write('window.TIF_MANIFEST = ')
    json.dump(manifest, f)
    f.write(';\n')

print('\nWrote to', DASHBOARD)
for fn in sorted(os.listdir(DASHBOARD)):
    p = os.path.join(DASHBOARD, fn)
    print(' ', fn, '(%d KB)' % (os.path.getsize(p)//1024))
