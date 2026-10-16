"""Prospecção assistida com pesquisa local em dados públicos do OpenStreetMap."""
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

bp = Blueprint("prospecting_tools", __name__)
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
CACHE_HOURS = 12
MAX_RESULTS = 40

# Tags OSM conhecidas. Categorias fora da lista usam busca textual no nome da empresa.
CATEGORY_TAGS = {
    "barbearia": [("shop", "hairdresser"), ("craft", "barber")],
    "barbearias": [("shop", "hairdresser"), ("craft", "barber")],
    "barbeiro": [("shop", "hairdresser"), ("craft", "barber")],
    "cabeleireiro": [("shop", "hairdresser")],
    "cabeleireira": [("shop", "hairdresser")],
    "salao": [("shop", "hairdresser"), ("shop", "beauty")],
    "salão": [("shop", "hairdresser"), ("shop", "beauty")],
    "estetica": [("shop", "beauty")],
    "estética": [("shop", "beauty")],
    "restaurante": [("amenity", "restaurant")],
    "restaurantes": [("amenity", "restaurant")],
    "lanchonete": [("amenity", "fast_food")],
    "cafe": [("amenity", "cafe")],
    "cafeteria": [("amenity", "cafe")],
    "padaria": [("shop", "bakery")],
    "mercado": [("shop", "supermarket"), ("shop", "convenience")],
    "supermercado": [("shop", "supermarket")],
    "farmacia": [("amenity", "pharmacy")],
    "farmácia": [("amenity", "pharmacy")],
    "dentista": [("healthcare", "dentist"), ("amenity", "dentist")],
    "dentistas": [("healthcare", "dentist"), ("amenity", "dentist")],
    "clinica": [("amenity", "clinic"), ("healthcare", "clinic")],
    "clínica": [("amenity", "clinic"), ("healthcare", "clinic")],
    "academia": [("leisure", "fitness_centre")],
    "academias": [("leisure", "fitness_centre")],
    "pet shop": [("shop", "pet")],
    "petshop": [("shop", "pet")],
    "oficina": [("shop", "car_repair")],
    "oficina mecânica": [("shop", "car_repair")],
    "hotel": [("tourism", "hotel")],
    "imobiliaria": [("office", "estate_agent")],
    "imobiliária": [("office", "estate_agent")],
    "contabilidade": [("office", "accountant")],
    "contador": [("office", "accountant")],
    "advocacia": [("office", "lawyer")],
    "advogado": [("office", "lawyer")],
    "lavanderia": [("shop", "laundry")],
    "loja de roupas": [("shop", "clothes")],
}

@bp.before_request
def protect_prospecting():
    if not session.get("authenticated"):
        return redirect(url_for("login"))


def _db_helpers():
    # Importação tardia evita dependência circular durante a inicialização do Flask.
    from app import db, now_iso, score_lead
    return db, now_iso, score_lead


def _ensure_cache():
    db, _, _ = _db_helpers()
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS prospecting_cache (
            cache_key TEXT PRIMARY KEY,
            segment TEXT NOT NULL,
            city TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            results_json TEXT NOT NULL
        )""")


def _cached_results(cache_key):
    db, _, _ = _db_helpers()
    with db() as conn:
        row = conn.execute(
            "SELECT fetched_at, results_json FROM prospecting_cache WHERE cache_key=%s",
            (cache_key,),
        ).fetchone()
    if not row:
        return None
    try:
        fetched = datetime.fromisoformat(row["fetched_at"])
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - fetched > timedelta(hours=CACHE_HOURS):
            return None
        return json.loads(row["results_json"])
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _save_cache(cache_key, segment, city, results):
    db, now_iso, _ = _db_helpers()
    with db() as conn:
        conn.execute(
            """INSERT INTO prospecting_cache(cache_key,segment,city,fetched_at,results_json)
               VALUES(%s,%s,%s,%s,%s)
               ON CONFLICT(cache_key) DO UPDATE SET
                 segment=EXCLUDED.segment, city=EXCLUDED.city,
                 fetched_at=EXCLUDED.fetched_at, results_json=EXCLUDED.results_json""",
            (cache_key, segment, city, now_iso(), json.dumps(results, ensure_ascii=False)),
        )


def _search_tags(segment):
    key = segment.casefold().strip()
    if key in CATEGORY_TAGS:
        return [(k, v) for k, v in CATEGORY_TAGS[key]]
    # Try common singular forms; use only the supplied segment as a name filter.
    simple = key.removesuffix("s")
    if simple in CATEGORY_TAGS:
        return [(k, v) for k, v in CATEGORY_TAGS[simple]]
    return []


def _build_query(segment, city):
    clean_city = city.split(",")[0].strip()
    if not clean_city:
        raise ValueError("Informe uma cidade, por exemplo: Guarulhos, SP.")
    # Escape input before inserting it into an Overpass regular expression.
    city_re = re.escape(clean_city).replace("/", r"\/")
    tags = _search_tags(segment)
    statements = []
    if tags:
        statements = [
            f'nwr["{tag}"="{value}"](area.searchArea);'
            for tag, value in tags
        ]
    else:
        segment_re = re.escape(segment.strip())
        statements = [f'nwr["name"~"{segment_re}",i](area.searchArea);']
    query = (
        '[out:json][timeout:25];'
        f'area["boundary"="administrative"]["name"~"^{city_re}$",i]->.searchArea;'
        '(' + ''.join(statements) + ');'
        f'out center tags {MAX_RESULTS};'
    )
    return clean_city, query


def _request_overpass(query):
    body = urlencode({"data": query}).encode("utf-8")
    req = Request(
        OVERPASS_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "LeadPilotAI/0.2 (business discovery; contact: owner via app settings)",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urlopen(req, timeout=35) as response:
        payload = json.loads(response.read().decode("utf-8"))
    elements = payload.get("elements", [])
    found = []
    seen = set()
    for item in elements:
        tags = item.get("tags") or {}
        name = (tags.get("name") or "").strip()
        if not name:
            continue
        osm_type = item.get("type", "")
        osm_id = item.get("id")
        if not osm_type or not osm_id:
            continue
        osm_key = f"{osm_type}/{osm_id}"
        if osm_key in seen:
            continue
        seen.add(osm_key)
        center = item.get("center") or {}
        address_bits = [
            tags.get("addr:street", ""),
            tags.get("addr:housenumber", ""),
            tags.get("addr:suburb", ""),
        ]
        address = ", ".join(part for part in address_bits if part)
        website = tags.get("contact:website") or tags.get("website") or tags.get("url") or ""
        phone = tags.get("contact:phone") or tags.get("phone") or ""
        email = tags.get("contact:email") or tags.get("email") or ""
        found.append({
            "osm_key": osm_key,
            "name": name,
            "website": website,
            "phone": phone,
            "email": email,
            "address": address,
            "city": tags.get("addr:city") or clean_city_for_display(tags, address),
            "lat": center.get("lat", item.get("lat")),
            "lon": center.get("lon", item.get("lon")),
            "osm_url": f"https://www.openstreetmap.org/{osm_type}/{osm_id}",
            "tags": ", ".join([tags.get("shop", ""), tags.get("amenity", ""), tags.get("craft", ""), tags.get("office", "")]).strip(", "),
        })
        if len(found) >= MAX_RESULTS:
            break
    return found


def clean_city_for_display(tags, address):
    return tags.get("addr:place") or ""


def _get_results(segment, city):
    _ensure_cache()
    clean_city, query = _build_query(segment, city)
    cache_key = hashlib.sha256(f"{segment.casefold()}|{clean_city.casefold()}".encode("utf-8")).hexdigest()
    cached = _cached_results(cache_key)
    if cached is not None:
        return cache_key, clean_city, cached, "cache"
    # Small global spacing between uncached calls to avoid hammering the shared service.
    db, now_iso, _ = _db_helpers()
    with db() as conn:
        throttle = conn.execute(
            "SELECT fetched_at FROM prospecting_cache WHERE cache_key='__rate_limit__'"
        ).fetchone()
    if throttle:
        try:
            last = datetime.fromisoformat(throttle["fetched_at"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            wait = 3 - (datetime.now(timezone.utc) - last).total_seconds()
            if wait > 0:
                raise RuntimeError(f"Por favor aguarde {int(wait) + 1} segundos antes de fazer outra busca.")
        except ValueError:
            pass
    # Save attempt timestamp before contacting the public service.
    with db() as conn:
        conn.execute(
            """INSERT INTO prospecting_cache(cache_key,segment,city,fetched_at,results_json)
               VALUES('__rate_limit__','',%s,%s,'{}')
               ON CONFLICT(cache_key) DO UPDATE SET fetched_at=EXCLUDED.fetched_at""",
            (clean_city, now_iso()),
        )
    results = _request_overpass(query)
    _save_cache(cache_key, segment, clean_city, results)
    return cache_key, clean_city, results, "live"


@bp.route("/prospecting", methods=["GET"])
def prospecting():
    segment = request.args.get("segment", "").strip()[:80]
    city = request.args.get("city", "").strip()[:120]
    searched = request.args.get("search") == "1" and bool(segment and city)
    maps_url = google_url = bing_url = ""
    register_url = url_for("new_lead")
    results = []
    cache_key = ""
    search_source = ""
    error = ""
    cached = False

    if searched:
        query_text = f"{segment} {city}"
        maps_url = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(query_text)
        google_url = "https://www.google.com/search?q=" + quote_plus(f"{segment} empresas em {city}")
        bing_url = "https://www.bing.com/search?q=" + quote_plus(f"{segment} empresas em {city}")
        register_url = url_for(
            "new_lead", segment=segment, city=city,
            source="Pesquisa assistida (revisão manual)",
        )
        try:
            cache_key, clean_city, results, source_kind = _get_results(segment, city)
            search_source = "OpenStreetMap"
            cached = source_kind == "cache"
            city = clean_city + (", " + request.args.get("city", "").split(",", 1)[1].strip() if "," in request.args.get("city", "") else "")
            if not results:
                error = "Nenhuma empresa com nome e categoria compatíveis foi encontrada nesta pesquisa. Confira os links externos ou tente um segmento mais específico."
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            bp.logger.warning("Falha na busca OpenStreetMap: %s", exc)
            error = "A fonte de dados está ocupada ou indisponível agora. Aguarde um pouco ou use os links de pesquisa abaixo."
        except (RuntimeError, ValueError) as exc:
            error = str(exc)

    return render_template(
        "prospecting.html",
        segment=segment, city=city, searched=searched,
        maps_url=maps_url, google_url=google_url, bing_url=bing_url,
        register_url=register_url, results=results,
        cache_key=cache_key, search_source=search_source,
        error=error, cached=cached,
    )


@bp.route("/prospecting/import", methods=["POST"])
def import_selected():
    from app import db, now_iso, score_lead
    cache_key = request.form.get("cache_key", "")
    selected = set(request.form.getlist("selected"))
    if not cache_key or not selected:
        flash("Selecione ao menos uma empresa para cadastrar.", "error")
        return redirect(url_for("prospecting_tools.prospecting"))
    _ensure_cache()
    with db() as conn:
        row = conn.execute(
            "SELECT results_json, city, segment FROM prospecting_cache WHERE cache_key=%s",
            (cache_key,),
        ).fetchone()
    if not row:
        flash("A pesquisa expirou ou não foi encontrada. Faça a busca novamente.", "error")
        return redirect(url_for("prospecting_tools.prospecting"))
    try:
        available = json.loads(row["results_json"])
    except json.JSONDecodeError:
        available = []
    chosen = [item for item in available if item.get("osm_key") in selected]
    if not chosen or len(chosen) != len(selected):
        flash("A seleção não corresponde aos resultados registrados. Faça a pesquisa novamente.", "error")
        return redirect(url_for("prospecting_tools.prospecting"))
    now = now_iso()
    added = skipped = 0
    with db() as conn:
        for item in chosen:
            exists = conn.execute(
                "SELECT id FROM leads WHERE lower(company)=lower(%s) AND lower(city)=lower(%s) LIMIT 1",
                (item["name"], row["city"]),
            ).fetchone()
            if exists:
                skipped += 1
                continue
            data = {
                "company": item["name"],
                "contact_name": "",
                "email": item.get("email", ""),
                "phone": item.get("phone", ""),
                "website": item.get("website", ""),
                "segment": row["segment"],
                "city": item.get("city") or row["city"],
                "source": "OpenStreetMap (ODbL)",
                "need": "",
                "notes": item.get("address", ""),
                "consent_basis": "Empresa listada publicamente; revisar pertinência antes de contato",
            }
            score = score_lead(data)
            conn.execute(
                """INSERT INTO leads
                   (company,contact_name,email,phone,website,segment,city,source,need,notes,status,score,consent_basis,created_at,updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Novo',%s,%s,%s,%s)""",
                (data["company"], data["contact_name"], data["email"], data["phone"], data["website"],
                 data["segment"], data["city"], data["source"], data["need"], data["notes"],
                 score, data["consent_basis"], now, now),
            )
            added += 1
    flash(f"Busca importada: {added} empresa(s) cadastrada(s); {skipped} duplicada(s) ignorada(s).", "success")
    return redirect(url_for("leads"))


