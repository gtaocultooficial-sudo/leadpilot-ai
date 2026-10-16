"""Prospecção assistida com pesquisa local em dados públicos do OpenStreetMap."""
import hashlib
import json
import re
import threading
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
            tags.get("addr:suburb", ""),
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
    clean_norm = clean_city.casefold()
    for feature in features:
        props = feature.get("properties") or {}
        name = (props.get("name") or "").strip()
        if name.casefold() == clean_norm:
            chosen = feature
            break
    if chosen is None:
        chosen = features[0]
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


def _photon_tags(segment):
    tags = _search_tags(segment)
    return [f"osm.{key}.{value}" for key, value in tags]


def _request_photon(segment, city):
    clean_city, bbox = _photon_city_bbox(city)
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
    for feature in features:
        props = feature.get("properties") or {}
        name = (props.get("name") or "").strip()
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
            "city": props.get("city") or props.get("name") if props.get("osm_key") == "place" else (props.get("city") or ""),
            "lat": ((feature.get("geometry") or {}).get("coordinates") or [None, None])[1],
            "lon": ((feature.get("geometry") or {}).get("coordinates") or [None, None])[0],
            "osm_url": f"https://www.openstreetmap.org/{osm_type_full}/{osm_id}",
            "tags": f"{props.get('osm_key') or ''}: {props.get('osm_value') or ''}".strip(": "),
        })
        if len(found) >= MAX_RESULTS:
            break
    return clean_city, found


def _get_results(segment, city):
    _ensure_cache()
    clean_city = city.split(",")[0].strip()
    if not clean_city:
        raise ValueError("Informe uma cidade, por exemplo: Guarulhos, SP.")
    cache_key = hashlib.sha256(f"{segment.casefold()}|{city.casefold().strip()}".encode("utf-8")).hexdigest()
    cached = _cached_results(cache_key)
    if cached is not None:
        return cache_key, clean_city, cached, "cache"

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

    _save_cache(cache_key, segment, result_city, results)
    return cache_key, result_city, results, "photon"


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


def _run_search_job(app_obj, job_id, segment, city):
    # Executa chamadas de rede fora da requisição web para a página não ficar bloqueada.
    with app_obj.app_context():
        _set_job(job_id, status="running", message="Consultando a fonte pública…")
        try:
            cache_key, _clean_city, results, _source_kind = _get_results(segment, city)
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


def _create_search_job(segment, city):
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
        args=(app_obj, job_id, segment, city),
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
    try:
        job_id = _create_search_job(segment, city)
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