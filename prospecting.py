"""Prospecção assistida com pesquisa local em dados públicos do OpenStreetMap."""
import hashlib
import json
import re
import threading
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode, urlparse
from urllib.request import Request, urlopen

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

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
    raw_city = city.split(",")[0].strip()
    clean_city = re.sub(r"[^\w\s.'’-]", "", raw_city, flags=re.UNICODE).strip()
    clean_segment = re.sub(r"[^\w\s.'’-]", "", segment, flags=re.UNICODE).strip()
    if not clean_city:
        raise ValueError("Informe uma cidade, por exemplo: Guarulhos, SP.")
    if not clean_segment:
        raise ValueError("Informe um segmento comercial válido.")
    city_re = re.escape(clean_city).replace("/", r"\/")
    tags = _search_tags(clean_segment)
    statements = []
    if tags:
        statements = [
            f'nwr["{tag}"="{value}"](area.searchArea);'
            for tag, value in tags
        ]
    else:
        segment_re = re.escape(clean_segment).replace('"', r'\"')
        statements = [f'nwr["name"~"{segment_re}",i](area.searchArea);']
    query = (
        '[out:json][timeout:12][maxsize:67108864];'
        f'area["boundary"="administrative"]["name"~"^{city_re}$",i]->.searchArea;'
        '(' + ''.join(statements) + ');'
        f'out center tags qt {MAX_RESULTS};'
    )
    return clean_city, query


def _request_overpass(query, clean_city):
    """Query one public OSM Overpass endpoint, with one fallback for outages only.

    Do not switch endpoints to evade 429/403/406 responses; respect shared-service limits.
    """
    # Use a genuinely independent public instance for fallback; overpass-api.de
    # and lz4.overpass-api.de belong to the same service family.
    endpoints = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.private.coffee/api/interpreter",
    ]
    body = urlencode({"data": query}).encode("utf-8")
    last_transient_error = None
    payload = None

    for index, endpoint in enumerate(endpoints):
        req = Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "LeadPilotAI/0.3 (+https://leadpilot-ai-5642.onrender.com)",
                "Referer": "https://leadpilot-ai-5642.onrender.com/",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            timeout_seconds = 10 if index == 0 else 8
            with urlopen(req, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except HTTPError as exc:
            if exc.code == 429:
                raise RuntimeError(
                    "A fonte pública aplicou limite de consultas (HTTP 429). Aguarde pelo menos 30 segundos antes de tentar de novo."
                ) from exc
            if exc.code in (403, 406):
                raise RuntimeError(
                    f"A fonte pública recusou a consulta (HTTP {exc.code}). A busca foi interrompida para respeitar as regras do serviço."
                ) from exc
            if exc.code in (500, 502, 503, 504, 522, 524):
                last_transient_error = exc
                current_endpoint = endpoint
                if index + 1 < len(endpoints):
                    current_app.logger.warning(
                        "Overpass temporariamente indisponível (%s, HTTP %s); tentando uma única instância alternativa.",
                        current_endpoint, exc.code
                    )
                    continue
                break
            raise RuntimeError(f"A fonte pública respondeu com HTTP {exc.code}. Tente novamente mais tarde.") from exc
        except (URLError, TimeoutError) as exc:
            last_transient_error = exc
            if index + 1 < len(endpoints):
                current_app.logger.warning(
                    "Falha temporária de conexão com Overpass (%s); tentando uma única instância alternativa.",
                    type(exc).__name__
                )
                continue
            break
        except json.JSONDecodeError as exc:
            last_transient_error = exc
            if index + 1 < len(endpoints):
                current_app.logger.warning("Overpass retornou resposta inválida; tentando uma única instância alternativa.")
                continue
            break

    if payload is None:
        current_app.logger.warning(
            "Consulta Overpass falhou em ambas as tentativas. Último erro: %s",
            last_transient_error
        )
        raise RuntimeError(
            "As duas tentativas da fonte pública estão indisponíveis ou demoraram demais. Nenhum lead foi importado. Aguarde alguns minutos ou use os links de pesquisa pública abaixo."
        ) from last_transient_error

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
            tags.get("addr:suburb", "") or tags.get("addr:place", ""),
        ]
        address = ", ".join(part for part in address_bits if part)
        website = (tags.get("contact:website") or tags.get("website") or tags.get("url") or "").strip()
        if website and not website.lower().startswith(("http://", "https://")):
            website = "https://" + website
        parsed_website = urlparse(website) if website else None
        if parsed_website and (parsed_website.scheme not in ("http", "https") or not parsed_website.netloc):
            website = ""
        phone = tags.get("contact:phone") or tags.get("phone") or ""
        email = tags.get("contact:email") or tags.get("email") or ""
        found.append({
            "osm_key": osm_key,
            "name": name,
            "website": website,
            "phone": phone,
            "email": email,
            "address": address,
            "city": (tags.get("addr:city") or "").strip(),
            "source_city": (tags.get("addr:city") or "").strip(),
            "locality": (tags.get("addr:suburb") or tags.get("addr:place") or "").strip(),
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


def _photon_json(params, timeout=8):
    url = "https://photon.komoot.io/api/?" + urlencode(params)
    req = Request(
        url,
        headers={
            "User-Agent": "LeadPilotAI/0.4 (+https://leadpilot-ai-5642.onrender.com)",
            "Accept": "application/geo+json, application/json",
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = exc.read(1200).decode("utf-8", "replace")
        except Exception:
            detail = ""
        current_app.logger.warning(
            "Photon HTTP %s at %s; response=%s",
            exc.code, urlparse(url).path, detail[:800]
        )
        raise


def _normalize_city_name(value):
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(char for char in value if not unicodedata.combining(char)).strip()


BRAZIL_STATE_NAMES = {
    "AC": "acre", "AL": "alagoas", "AP": "amapa", "AM": "amazonas",
    "BA": "bahia", "CE": "ceara", "DF": "distrito federal", "ES": "espirito santo",
    "GO": "goias", "MA": "maranhao", "MT": "mato grosso",
    "MS": "mato grosso do sul", "MG": "minas gerais", "PA": "para",
    "PB": "paraiba", "PR": "parana", "PE": "pernambuco", "PI": "piaui",
    "RJ": "rio de janeiro", "RN": "rio grande do norte", "RS": "rio grande do sul",
    "RO": "rondonia", "RR": "roraima", "SC": "santa catarina",
    "SP": "sao paulo", "SE": "sergipe", "TO": "tocantins",
}


def _photon_city_bbox(city):
    """Resolve a city once, then use its boundary to find categorized OSM points."""
    clean_city = city.split(",")[0].strip()
    if not clean_city:
        raise ValueError("Informe uma cidade, por exemplo: Guarulhos, SP.")
    # Resolve the city with a simple name plus ISO country filter.
    params = {
        "q": clean_city,
        "countrycode": "br",
        "layer": "city",
        "limit": 5,
        "lang": "default",
    }
    payload = _photon_json(params, timeout=7)
    features = payload.get("features") or []
    if not features:
        raise RuntimeError("Não consegui localizar essa cidade na fonte pública. Confira o nome e o estado.")
    chosen = None
    clean_norm = _normalize_city_name(clean_city)
    for feature in features:
        props = feature.get("properties") or {}
        name = (props.get("name") or "").strip()
        if _normalize_city_name(name) != clean_norm:
            continue
        country_code = (props.get("countrycode") or "").strip().casefold()
        if country_code and country_code != "br":
            continue
        requested_region = city.split(",", 1)[1].strip() if "," in city else ""
        actual_state = _normalize_city_name(props.get("state") or props.get("state_name") or "")
        requested_state = ""
        if len(requested_region) == 2:
            requested_state = BRAZIL_STATE_NAMES.get(requested_region.upper(), "")
        elif requested_region:
            requested_state = _normalize_city_name(requested_region)
        if requested_state and actual_state and requested_state != actual_state:
            continue
        if requested_state and not actual_state:
            # Photon did not return enough administrative information to safely choose
            # between same-name municipalities; do not silently pick a different place.
            continue
        chosen = feature
        break
    if chosen is None:
        raise RuntimeError(
            f"Não consegui confirmar a cidade exata “{clean_city}”"
            + (f" no estado “{city.split(',', 1)[1].strip()}”" if "," in city else "")
            + ". Confira o nome e a UF antes de pesquisar."
        )
    props = chosen.get("properties") or {}
    coords = (chosen.get("geometry") or {}).get("coordinates") or []
    extent = props.get("extent")
    try:
        if extent and len(extent) == 4:
            min_lon, min_lat, max_lon, max_lat = [float(v) for v in extent]
            # Guard against weird/wrong geocoder extents; use a smaller city-sized box.
            if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
                raise ValueError("limites inválidos")
            if max_lon - min_lon > 1.5 or max_lat - min_lat > 1.5:
                raise ValueError("limites administrativos grandes demais")
        elif len(coords) >= 2:
            lon, lat = float(coords[0]), float(coords[1])
            min_lon, max_lon = lon - 0.12, lon + 0.12
            min_lat, max_lat = lat - 0.12, lat + 0.12
        else:
            raise ValueError("coordenadas ausentes")
    except (TypeError, ValueError):
        if len(coords) < 2:
            raise RuntimeError("A fonte não retornou coordenadas úteis para essa cidade.")
        lon, lat = float(coords[0]), float(coords[1])
        min_lon, max_lon = lon - 0.12, lon + 0.12
        min_lat, max_lat = lat - 0.12, lat + 0.12
    return clean_city, ",".join(f"{v:.6f}" for v in (min_lon, min_lat, max_lon, max_lat))


def _filter_city_consistent_results(clean_city, results):
    """Drop cached or fresh results that explicitly identify another municipality."""
    expected = _normalize_city_name(clean_city)
    kept = []
    rejected = []
    for raw in results:
        item = dict(raw)
        source_city = (item.get("source_city") or item.get("city") or "").strip()
        if source_city and _normalize_city_name(source_city) != expected:
            rejected.append(source_city)
            continue
        item["source_city"] = source_city
        item["city_verified"] = bool(source_city)
        kept.append(item)
    if not kept and rejected:
        most_common = sorted(set(rejected), key=lambda name: (-rejected.count(name), name))[0]
        current_app.logger.warning(
            "All cached/prospected businesses rejected due to municipality conflict: requested=%s rejected=%s",
            clean_city, sorted(set(rejected))
        )
        raise RuntimeError(
            f"A fonte retornou empresas marcadas como “{most_common}”, não “{clean_city}”. "
            "Esses resultados foram excluídos para evitar cadastrar empresas de outra cidade. "
            "Faça uma nova pesquisa e confira a localização antes de importar."
        )
    return kept, rejected


def _photon_tags(segment):
    tags = _search_tags(segment)
    return [f"osm.{key}.{value}" for key, value in tags]


def _request_photon(segment, city):
    clean_city, bbox = _photon_city_bbox(city)
    includes = _photon_tags(segment)
    # Use official Photon category filters for known business sectors.
    # The public instance rejected lang=pt; supported language is now "default".
    params = {
        "bbox": bbox,
        "countrycode": "br",
        "limit": MAX_RESULTS,
        "lang": "default",
    }
    if includes:
        params["include"] = ",".join(includes)
    else:
        # For categories outside the mapping, search by the supplied business term.
        params["q"] = segment.strip()
    payload = _photon_json(params, timeout=10)
    features = payload.get("features") or []
    found = []
    seen = set()
    rejected_city_labels = []
    for feature in features:
        props = feature.get("properties") or {}
        name = (props.get("name") or "").strip()
        reported_city = (props.get("city") or props.get("municipality") or props.get("locality") or "").strip()
        if reported_city and _normalize_city_name(reported_city) != _normalize_city_name(clean_city):
            # Photon can return adjacent municipalities when the rectangle overlaps them.
            # Exclude explicit city conflicts before they can be shown or imported.
            rejected_city_labels.append(reported_city)
            continue
        osm_type = str(props.get("osm_type") or "").upper()
        osm_id = props.get("osm_id")
        if not name or not osm_type or not osm_id:
            continue
        key = f"{osm_type}/{osm_id}"
        if key in seen:
            continue
        seen.add(key)
        street = (props.get("street") or "").strip()
        number = (props.get("housenumber") or "").strip()
        district = (props.get("district") or props.get("locality") or "").strip()
        address = ", ".join(part for part in [
            " ".join(part for part in (street, number) if part),
            district,
        ] if part)
        extra = props.get("extra") or {}
        website = str(extra.get("website") or extra.get("contact:website") or "").strip()
        if website and not website.lower().startswith(("http://", "https://")):
            website = "https://" + website
        parsed_website = urlparse(website) if website else None
        if parsed_website and (parsed_website.scheme not in ("http", "https") or not parsed_website.netloc):
            website = ""
        phone = str(extra.get("phone") or extra.get("contact:phone") or "").strip()
        email = str(extra.get("email") or extra.get("contact:email") or "").strip()
        osm_type_full = {"N": "node", "W": "way", "R": "relation"}.get(osm_type, osm_type.lower())
        found.append({
            "osm_key": key,
            "name": name,
            "website": website,
            "phone": phone,
            "email": email,
            "address": address,
            "city": reported_city,
            "city_verified": bool(reported_city and _normalize_city_name(reported_city) == _normalize_city_name(clean_city)),
            "lat": ((feature.get("geometry") or {}).get("coordinates") or [None, None])[1],
            "lon": ((feature.get("geometry") or {}).get("coordinates") or [None, None])[0],
            "osm_url": f"https://www.openstreetmap.org/{osm_type_full}/{osm_id}",
            "tags": f"{props.get('osm_key') or ''}: {props.get('osm_value') or ''}".strip(": "),
        })
        if len(found) >= MAX_RESULTS:
            break
    if not found and rejected_city_labels:
        rejected = sorted(set(rejected_city_labels), key=lambda value: (-rejected_city_labels.count(value), value))[0]
        current_app.logger.warning(
            "Photon returned only results for other municipalities; requested=%s, returned=%s",
            clean_city, sorted(set(rejected_city_labels))
        )
        raise RuntimeError(
            f"A fonte retornou empresas marcadas como “{rejected}”, não “{clean_city}”. "
            "Esses resultados foram excluídos para evitar cadastrar empresas de outra cidade. "
            "Tente outro segmento ou confira os links de pesquisa pública."
        )
    return clean_city, found


def _get_results(segment, city):
    _ensure_cache()
    clean_city = city.split(",")[0].strip()
    if not clean_city:
        raise ValueError("Informe uma cidade, por exemplo: Guarulhos, SP.")
    cache_key = hashlib.sha256(f"{segment.casefold()}|{city.casefold().strip()}".encode("utf-8")).hexdigest()
    cached = _cached_results(cache_key)
    if cached is not None:
        safe_cached, rejected_cached = _filter_city_consistent_results(clean_city, cached)
        if rejected_cached:
            # Replace stale 12-hour cache records created before municipality validation.
            _save_cache(cache_key, segment, city, safe_cached)
        return cache_key, clean_city, safe_cached, "cache"

    # Re-use a shared-service throttle so a user cannot repeatedly call public APIs.
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
    with db() as conn:
        conn.execute(
            """INSERT INTO prospecting_cache(cache_key,segment,city,fetched_at,results_json)
               VALUES('__rate_limit__','',%s,%s,'{}')
               ON CONFLICT(cache_key) DO UPDATE SET fetched_at=EXCLUDED.fetched_at""",
            (clean_city, now_iso()),
        )
    try:
        result_city, results = _request_photon(segment, city)
    except HTTPError as exc:
        current_app.logger.warning("Photon request failed with HTTP %s", exc.code)
        if exc.code == 429:
            raise RuntimeError("O serviço público de busca aplicou limite de consultas. Aguarde alguns minutos antes de tentar de novo.") from exc
        if exc.code in (403, 406):
            raise RuntimeError(f"O serviço público recusou a consulta (HTTP {exc.code}). Use os links de pesquisa abaixo.") from exc
        if exc.code == 400:
            raise RuntimeError("A fonte pública rejeitou os parâmetros da consulta. A busca foi simplificada para pesquisar por nome/segmento dentro da cidade. Tente de novo após a atualização.") from exc
        raise RuntimeError(f"O serviço público de busca respondeu com HTTP {exc.code}. Tente novamente mais tarde ou use os links abaixo.") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        current_app.logger.warning("Photon indisponível (%s): %s", type(exc).__name__, str(exc)[:200])
        raise RuntimeError("A fonte pública de empresas está indisponível ou demorou demais. Nenhum lead foi importado; tente mais tarde ou use os links abaixo.") from exc

    # Preserve the complete user-provided city label (including state) in the cache
    # and use it consistently for imported leads; Photon may return the neighboring metro label.
    requested_city = city.strip()
    results, _rejected = _filter_city_consistent_results(clean_city, results)
    _save_cache(cache_key, segment, requested_city, results)
    return cache_key, requested_city, results, "photon"


def _ensure_jobs():
    db, _, _ = _db_helpers()
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS prospecting_jobs (
            id TEXT PRIMARY KEY,
            segment TEXT NOT NULL,
            city TEXT NOT NULL,
            status TEXT NOT NULL,
            cache_key TEXT NOT NULL DEFAULT '',
            result_count INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")


def _set_job(job_id, **values):
    db, now_iso, _ = _db_helpers()
    allowed = {"status", "cache_key", "result_count", "message"}
    assignments = []
    params = []
    for key, value in values.items():
        if key not in allowed:
            raise ValueError("Campo de tarefa inválido.")
        assignments.append(f"{key}=%s")
        params.append(value)
    assignments.append("updated_at=%s")
    params.extend([now_iso(), job_id])
    with db() as conn:
        conn.execute(
            "UPDATE prospecting_jobs SET " + ", ".join(assignments) + " WHERE id=%s",
            params,
        )


def _auto_import_results(segment, city, results):
    """Import public business listings automatically without checkbox-by-checkbox review.

    This does not send outreach. Unverified municipality data is clearly marked for review.
    """
    from app import db, now_iso, score_lead

    now = now_iso()
    added = skipped = unverified = invalid = 0
    target_city = (city or "").strip()[:120]
    target_segment = (segment or "").strip()[:80]

    with db() as conn:
        for item in results:
            name = str(item.get("name") or "").strip()[:250]
            osm_url = str(item.get("osm_url") or "").strip()
            parsed_osm = urlparse(osm_url) if osm_url else None
            valid_osm_ref = bool(
                parsed_osm
                and parsed_osm.scheme == "https"
                and parsed_osm.netloc == "www.openstreetmap.org"
                and re.fullmatch(r"/(?:node|way|relation)/\d+", parsed_osm.path)
            )
            if not name or not valid_osm_ref:
                invalid += 1
                continue

            # Preserve existing manual entries; repeated runs skip the same business/city.
            exists = conn.execute(
                """SELECT id FROM leads
                   WHERE lower(company)=lower(%s)
                     AND lower(COALESCE(city,''))=lower(%s)
                   LIMIT 1""",
                (name, target_city),
            ).fetchone()
            if exists:
                skipped += 1
                continue

            address = str(item.get("address") or "").strip()[:500]
            note_parts = [address] if address else []
            source_city = str(item.get("source_city") or item.get("city") or "").strip()[:120]
            city_verified = bool(item.get("city_verified") and source_city)
            if city_verified:
                note_parts.append("Município informado pela fonte: " + source_city)
            else:
                note_parts.append("Município não confirmado pela fonte; conferir antes de abordar.")
                unverified += 1
            note_parts.append("OpenStreetMap item: " + osm_url)

            website = str(item.get("website") or "").strip()[:500]
            if website and not website.lower().startswith(("http://", "https://")):
                website = "https://" + website
            parsed_website = urlparse(website) if website else None
            if parsed_website and (parsed_website.scheme not in ("http", "https") or not parsed_website.netloc):
                website = ""

            data = {
                "company": name,
                "contact_name": "",
                "email": str(item.get("email") or "").strip()[:320],
                "phone": str(item.get("phone") or "").strip()[:100],
                "website": website,
                "segment": target_segment,
                "city": target_city,
                "source": "OpenStreetMap (ODbL)",
                "need": "",
                "notes": "\n".join(note_parts),
                "consent_basis": "Empresa listada em fonte pública; confirmar pertinência e canal antes de contato",
            }
            score = score_lead(data)
            conn.execute(
                """INSERT INTO leads
                   (company,contact_name,email,phone,website,segment,city,source,need,notes,status,score,consent_basis,created_at,updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Novo',%s,%s,%s,%s)""",
                (data["company"], data["contact_name"], data["email"], data["phone"],
                 data["website"], data["segment"], data["city"], data["source"], data["need"],
                 data["notes"], score, data["consent_basis"], now, now),
            )
            added += 1

    return {"added": added, "skipped": skipped, "unverified": unverified, "invalid": invalid}


def _run_search_job(app_obj, job_id, segment, city, auto_import=False):
    # Executa chamadas de rede fora da requisição web para a página não ficar bloqueada.
    with app_obj.app_context():
        initial_message = "Buscando e cadastrando automaticamente…" if auto_import else "Consultando a fonte pública…"
        _set_job(job_id, status="running", message=initial_message)
        try:
            cache_key, _clean_city, results, _source_kind = _get_results(segment, city)
            if auto_import and results:
                counts = _auto_import_results(segment, _clean_city, results)
                message = (
                    f"Piloto automático concluído: {counts['added']} nova(s) empresa(s) cadastrada(s); "
                    f"{counts['skipped']} duplicada(s) ignorada(s); "
                    f"{counts['unverified']} com município a conferir; {counts['invalid']} registro(s) inválido(s) ignorado(s). "
                    "Nenhuma mensagem foi enviada."
                )
            elif auto_import:
                message = "Piloto automático terminou, mas a pesquisa não encontrou empresas para cadastrar. Nenhuma mensagem foi enviada."
            else:
                message = "" if results else (
                    "Nenhuma empresa compatível foi encontrada. "
                    "Tente outro termo ou confira as pesquisas externas."
                )
            _set_job(
                job_id,
                status="done",
                cache_key=cache_key,
                result_count=len(results),
                message=message,
            )
        except Exception as exc:
            app_obj.logger.warning(
                "Busca de prospecção falhou (%s): %s",
                job_id,
                str(exc)[:400],
            )
            _set_job(
                job_id,
                status="failed",
                cache_key="",
                result_count=0,
                message=str(exc)[:500] or "Não foi possível concluir a busca.",
            )


def _create_search_job(segment, city, auto_import=False):
    _ensure_jobs()
    db, now_iso, _ = _db_helpers()
    job_id = uuid.uuid4().hex
    now = now_iso()
    with db() as conn:
        conn.execute(
            """INSERT INTO prospecting_jobs
               (id,segment,city,status,cache_key,result_count,message,created_at,updated_at)
               VALUES(%s,%s,%s,'queued','',0,'Preparando busca…',%s,%s)""",
            (job_id, segment, city, now, now),
        )
    app_obj = current_app._get_current_object()
    threading.Thread(
        target=_run_search_job,
        args=(app_obj, job_id, segment, city, auto_import),
        daemon=True,
        name=f"leadpilot-search-{job_id[:8]}",
    ).start()
    return job_id


@bp.route("/prospecting/start", methods=["POST"])
def start_search():
    segment = request.form.get("segment", "").strip()[:80]
    city = request.form.get("city", "").strip()[:120]
    if not segment or not city:
        flash("Informe o segmento e a cidade para pesquisar.", "error")
        return redirect(url_for("prospecting_tools.prospecting"))
    auto_import = request.form.get("mode") == "autopilot"
    try:
        job_id = _create_search_job(segment, city, auto_import=auto_import)
    except Exception:
        current_app.logger.exception("Não foi possível criar tarefa de prospecção")
        flash("Não consegui iniciar a busca agora. Tente novamente em instantes.", "error")
        return redirect(url_for("prospecting_tools.prospecting"))
    return redirect(url_for("prospecting_tools.prospecting", segment=segment, city=city, job_id=job_id))


@bp.route("/prospecting/status/<job_id>", methods=["GET"])
def search_status(job_id):
    _ensure_jobs()
    db, _, _ = _db_helpers()
    with db() as conn:
        row = conn.execute(
            "SELECT id,status,result_count,message FROM prospecting_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
    if not row:
        return {"error": "Tarefa não encontrada."}, 404
    return {
        "job_id": row["id"],
        "status": row["status"],
        "result_count": row["result_count"],
        "message": row["message"],
    }


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

    job_id = request.args.get("job_id", "").strip()[:64]
    job_status = ""
    job_message = ""
    if searched and not job_id:
        # Compatibility with an older open page using GET; redirect immediately to async search.
        try:
            job_id = _create_search_job(segment, city)
            return redirect(url_for("prospecting_tools.prospecting", segment=segment, city=city, job_id=job_id))
        except Exception:
            current_app.logger.exception("Não foi possível iniciar tarefa de prospecção")
            error = "Não consegui iniciar a busca agora. Tente novamente em instantes."

    if job_id:
        _ensure_jobs()
        db, _, _ = _db_helpers()
        with db() as conn:
            job = conn.execute(
                "SELECT * FROM prospecting_jobs WHERE id=%s",
                (job_id,),
            ).fetchone()
        if job:
            # Only render a job for the same search terms encoded in the current page.
            segment = job["segment"]
            city = job["city"]
            searched = True
            job_status = job["status"]
            job_message = job["message"]
            error = job_message if job_status == "failed" else ""
            cache_key = job["cache_key"] or ""
            if job_status == "done" and cache_key:
                results = _cached_results(cache_key) or []
                search_source = "Photon / OpenStreetMap"
                cached = False
                if not results and not error:
                    error = job_message or "A pesquisa não retornou empresas."
            elif job_status in ("queued", "running"):
                error = ""
        else:
            error = "Esta busca expirou ou não foi encontrada. Faça uma nova pesquisa."
            searched = True
    if segment and city:
        query_text = f"{segment} {city}"
        maps_url = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(query_text)
        google_url = "https://www.google.com/search?q=" + quote_plus(f"{segment} empresas em {city}")
        bing_url = "https://www.bing.com/search?q=" + quote_plus(f"{segment} empresas em {city}")
        register_url = url_for(
            "new_lead", segment=segment, city=city,
            source="Pesquisa assistida (revisão manual)",
        )

    return render_template(
        "prospecting.html",
        segment=segment, city=city, searched=searched,
        maps_url=maps_url, google_url=google_url, bing_url=bing_url,
        register_url=register_url, results=results,
        cache_key=cache_key, search_source=search_source,
        error=error, cached=cached, job_id=job_id,
        job_status=job_status, job_message=job_message,
    )


@bp.route("/prospecting/import", methods=["POST"])
def import_selected():
    from app import db, now_iso, score_lead
    cache_key = request.form.get("cache_key", "")
    query_city = request.form.get("query_city", "").strip()[:120]
    query_segment = request.form.get("query_segment", "").strip()[:80]
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
    added = skipped = references_added = 0
    target_city = query_city or row["city"]
    target_segment = query_segment or row["segment"]

    with db() as conn:
        for item in chosen:
            osm_url = (item.get("osm_url") or "").strip()
            parsed_osm = urlparse(osm_url) if osm_url else None
            valid_osm_ref = bool(
                parsed_osm
                and parsed_osm.scheme == "https"
                and parsed_osm.netloc == "www.openstreetmap.org"
                and re.fullmatch(r"/(?:node|way|relation)/\d+", parsed_osm.path)
            )
            osm_marker = f"OpenStreetMap item: {osm_url}" if valid_osm_ref else ""
            item_address = (item.get("address") or "").strip()
            note_parts = [item_address] if item_address else []
            if item.get("city_verified") and item.get("source_city"):
                note_parts.append(f"Município informado pela fonte: {item['source_city']}")
            elif not item.get("city_verified"):
                note_parts.append("Município não informado na fonte; revisar localização antes de contato")
            if osm_marker:
                note_parts.append(osm_marker)
            imported_notes = "\n".join(note_parts)

            # Prefer an existing lead in the searched municipality so same-name
            # businesses elsewhere are not mutated merely because their names match.
            exists = conn.execute(
                """SELECT id, city, source, segment, notes
                   FROM leads WHERE lower(company)=lower(%s)
                   ORDER BY CASE WHEN lower(COALESCE(city,''))=lower(%s) THEN 0 ELSE 1 END, id
                   LIMIT 1""",
                (item["name"], target_city),
            ).fetchone()
            if exists:
                current_notes = (exists.get("notes") or "").strip()
                current_ref_match = re.search(
                    r"OpenStreetMap item:\s*(https://www\.openstreetmap\.org/(?:node|way|relation)/\d+)",
                    current_notes,
                )
                current_ref = current_ref_match.group(1) if current_ref_match else ""
                same_city = (exists.get("city") or "").strip().casefold() == (target_city or "").strip().casefold()
                same_segment = (exists.get("segment") or "").strip().casefold() == (target_segment or "").strip().casefold()
                current_address = current_notes.split("\nOpenStreetMap item:", 1)[0].strip()
                norm = lambda value: " ".join((value or "").casefold().split())
                same_address = bool(item_address and current_address and norm(item_address) == norm(current_address))

                if (
                    exists.get("source") == "OpenStreetMap (ODbL)"
                    and same_segment
                    and valid_osm_ref
                    and current_ref == osm_url
                ):
                    # The exact same OSM object is verified, so a location correction is safe.
                    if not same_city:
                        conn.execute(
                            "UPDATE leads SET city=%s, updated_at=%s WHERE id=%s",
                            (target_city, now, exists["id"]),
                        )
                    skipped += 1
                elif (
                    exists.get("source") == "OpenStreetMap (ODbL)"
                    and same_city
                    and same_segment
                    and (not current_address or not item_address or same_address)
                    and valid_osm_ref
                    and not current_ref
                ):
                    # Attach an exact OSM id only when the saved address agrees with the selected listing.
                    new_notes = (current_notes + "\n" + osm_marker).strip()
                    conn.execute(
                        "UPDATE leads SET notes=%s, updated_at=%s WHERE id=%s",
                        (new_notes, now, exists["id"]),
                    )
                    references_added += 1
                    skipped += 1
                else:
                    skipped += 1
                continue

            data = {
                "company": item["name"],
                "contact_name": "",
                "email": item.get("email", ""),
                "phone": item.get("phone", ""),
                "website": item.get("website", ""),
                "segment": target_segment,
                "city": target_city,
                "source": "OpenStreetMap (ODbL)",
                "need": "",
                "notes": imported_notes,
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
    flash(
        f"Busca importada: {added} empresa(s) cadastrada(s); "
        f"{skipped} duplicada(s) ignorada(s); {references_added} origem(ns) exata(s) anexada(s).",
        "success",
    )
    return redirect(url_for("leads"))
