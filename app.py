import csv
import json
import re
import unicodedata
from difflib import SequenceMatcher
import hashlib
import hmac
import io
import os
from datetime import datetime, timezone
from functools import wraps

import psycopg
from psycopg.rows import dict_row
from flask import Flask, Response, flash, redirect, render_template, request, session, url_for
from urllib.parse import quote_plus, urlencode, urlparse
from urllib.request import Request as URLRequest, urlopen

DATABASE_URL = os.environ.get("DATABASE_URL", "")
APP_USERNAME = os.environ.get("APP_USERNAME", "")
APP_PASSWORD_SHA256 = os.environ.get("APP_PASSWORD_SHA256", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")

app = Flask(__name__)
app.secret_key = SECRET_KEY or None
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
)

STATUSES = ["Novo", "Qualificado", "Contato preparado", "Contatado", "Respondeu", "Reunião", "Proposta", "Cliente", "Sem interesse"]


def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL não configurada. Configure PostgreSQL antes de iniciar.")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapped


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id BIGSERIAL PRIMARY KEY,
                company TEXT NOT NULL,
                contact_name TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                website TEXT NOT NULL DEFAULT '',
                segment TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                need TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'Novo',
                score INTEGER NOT NULL DEFAULT 0,
                consent_basis TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company)")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def score_lead(row):
    score = 0
    if row.get("website"): score += 10
    if row.get("email") or row.get("phone"): score += 15
    if row.get("contact_name"): score += 10
    if row.get("segment"): score += 10
    if row.get("need"): score += 30
    if row.get("source"): score += 10
    if row.get("city"): score += 5
    if row.get("notes"): score += 10
    return min(score, 100)


def assess_lead(lead):
    """Explain data completeness without implying purchase intent."""
    score = score_lead(lead)
    missing = []
    if not (lead.get("website") or "").strip():
        missing.append("Conferir e registrar o site oficial da empresa.")
    if not ((lead.get("email") or "").strip() or (lead.get("phone") or "").strip()):
        missing.append("Encontrar um canal comercial público e adequado.")
    if not (lead.get("contact_name") or "").strip():
        missing.append("Identificar o contato comercial, se disponível publicamente.")
    if not (lead.get("need") or "").strip():
        missing.append("Registrar uma necessidade ou oportunidade observável antes da abordagem.")
    if not (lead.get("source") or "").strip():
        missing.append("Registrar a fonte e a data da pesquisa.")
    if score < 40:
        level = "Dados incompletos"
        summary = "Enriqueça os dados antes de priorizar este lead."
    elif score < 70:
        level = "Em verificação"
        summary = "Confira a pertinência e o canal comercial antes de abordar."
    else:
        level = "Bem documentado"
        summary = "Os dados estão mais completos; revise a abordagem antes de usar."
    return {
        "score": score,
        "level": level,
        "summary": summary,
        "missing": missing,
        "score_note": "O score mede completude dos dados, não intenção de compra nem probabilidade comprovada de fechar negócio.",
    }


def make_message_variants(lead):
    """Draft alternative outreach copy using only entered business context."""
    company = (lead.get("company") or "sua empresa").strip()
    contact = (lead.get("contact_name") or "").strip()
    segment = (lead.get("segment") or "").strip()
    city = (lead.get("city") or "").strip()
    need = (lead.get("need") or "").strip()
    greeting = f"Olá, {contact}!" if contact else "Olá! Tudo bem?"
    context = f"Vi que a {company}" 
    if segment:
        context += f" atua no segmento de {segment}"
    if city:
        context += f" em {city}"
    context += "."
    if need:
        opportunity = f"Estou entrando em contato porque registrei esta possível oportunidade: {need}. Queria confirmar se isso faz sentido para vocês."
    else:
        opportunity = "Estou entrando em contato para entender se faz sentido melhorar a organização do processo comercial e o acompanhamento de oportunidades."
    opt_out = "Se não for relevante, me avise e não volto a entrar em contato."

    whatsapp = (
        f"{greeting}\n\n{context}\n{opportunity}\n\n"
        "Trabalho com soluções práticas de organização comercial e acompanhamento de leads. "
        "Posso te explicar a ideia em poucas linhas?\n\n"
        f"{opt_out}"
    )
    email_subject = f"Ideia de organização comercial para {company}"
    email_body = (
        f"{greeting}\n\n{context}\n\n{opportunity}\n\n"
        "A proposta é conversar brevemente para entender o processo atual e avaliar se uma solução simples "
        "de organização e acompanhamento comercial seria útil. Não estou presumindo que exista um problema; "
        "quero apenas confirmar se o tema é relevante para vocês.\n\n"
        "Se fizer sentido, posso enviar um resumo curto. Caso não seja pertinente, basta responder que não deseja novos contatos.\n\n"
        "Obrigado!"
    )
    call_opener = (
        f"Olá, poderia falar com a pessoa responsável pelo comercial? "
        f"Meu nome é [seu nome]. Estou ligando para a {company}"
        + (f", do segmento de {segment}" if segment else "")
        + ". Gostaria de confirmar se faz sentido uma conversa rápida sobre organização comercial e acompanhamento de oportunidades. "
        "Se não for um assunto relevante, encerro por aqui."
    )
    return [
        {"key": "whatsapp", "title": "WhatsApp / mensagem curta", "body": whatsapp, "subject": ""},
        {"key": "email", "title": "E-mail comercial", "body": email_body, "subject": email_subject},
        {"key": "ligacao", "title": "Abertura para ligação", "body": call_opener, "subject": ""},
    ]

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password_hash = hashlib.sha256(request.form.get("password", "").encode("utf-8")).hexdigest()
        if (APP_USERNAME and APP_PASSWORD_SHA256
                and hmac.compare_digest(username, APP_USERNAME)
                and hmac.compare_digest(password_hash, APP_PASSWORD_SHA256)):
            session.clear()
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        flash("Credenciais inválidas ou variáveis de acesso não configuradas.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]
        clients = conn.execute("SELECT COUNT(*) AS n FROM leads WHERE status='Cliente'").fetchone()["n"]
        active = conn.execute("SELECT COUNT(*) AS n FROM leads WHERE status IN ('Novo','Qualificado','Contato preparado','Contatado','Respondeu','Reunião','Proposta')").fetchone()["n"]
        rows = conn.execute("SELECT * FROM leads ORDER BY updated_at DESC, id DESC LIMIT 8").fetchall()
        by_status = {status: conn.execute("SELECT COUNT(*) AS n FROM leads WHERE status=%s", (status,)).fetchone()["n"] for status in STATUSES}
    return render_template("index.html", total=total, clients=clients, active=active, rows=rows, by_status=by_status, statuses=STATUSES)


@app.route("/leads")
@login_required
def leads():
    query = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    channel = request.args.get("channel", "").strip()
    quality = request.args.get("quality", "").strip()
    sql = "SELECT * FROM leads WHERE TRUE"
    params = []
    if query:
        sql += " AND (company ILIKE %s OR contact_name ILIKE %s OR segment ILIKE %s OR city ILIKE %s OR email ILIKE %s)"
        params.extend([f"%{query}%"] * 5)
    if status and status in STATUSES:
        sql += " AND status=%s"
        params.append(status)
    if channel == "direct":
        sql += " AND (COALESCE(email,'')<>'' OR COALESCE(phone,'')<>'')"
    elif channel == "website":
        sql += " AND COALESCE(website,'')<>'' AND COALESCE(email,'')='' AND COALESCE(phone,'')=''"
    elif channel == "missing":
        sql += " AND COALESCE(website,'')='' AND COALESCE(email,'')='' AND COALESCE(phone,'')=''"
    if quality == "incomplete":
        sql += " AND score < 40"
    elif quality == "review":
        sql += " AND score >= 40 AND score < 70"
    elif quality == "documented":
        sql += " AND score >= 70"
    sql += " ORDER BY score DESC, updated_at DESC"
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    decorated = []
    for raw in rows:
        lead = dict(raw)
        has_direct = bool((lead.get("email") or "").strip() or (lead.get("phone") or "").strip())
        has_website = bool((lead.get("website") or "").strip())
        if has_direct:
            lead["channel_label"] = "Canal direto disponível"
            lead["channel_hint"] = "E-mail ou telefone cadastrado"
        elif has_website:
            lead["channel_label"] = "Site para conferir"
            lead["channel_hint"] = "Confirme um canal comercial no site oficial"
        else:
            lead["channel_label"] = "Sem canal cadastrado"
            lead["channel_hint"] = "Pesquise um meio comercial público antes de abordar"
        if lead["score"] < 40:
            lead["quality_label"] = "Dados incompletos"
            lead["next_step"] = "Completar os dados básicos"
        elif lead["score"] < 70:
            lead["quality_label"] = "Em verificação"
            lead["next_step"] = "Confirmar pertinência e canal"
        else:
            lead["quality_label"] = "Bem documentado"
            lead["next_step"] = "Revisar a abordagem"
        decorated.append(lead)
    return render_template(
        "leads.html", rows=decorated, statuses=STATUSES, q=query,
        selected_status=status, selected_channel=channel, selected_quality=quality
    )

@app.route("/leads/new", methods=["GET", "POST"])
@login_required
def new_lead():
    fields = ["company", "contact_name", "email", "phone", "website", "segment", "city", "source", "need", "notes", "consent_basis"]
    if request.method == "POST":
        data = {key: request.form.get(key, "").strip() for key in fields}
        if not data["company"]:
            flash("Informe o nome da empresa.", "error")
            return render_template("lead_form.html", lead=data, statuses=STATUSES, heading="Adicionar lead")
        score = score_lead(data)
        now = now_iso()
        with db() as conn:
            conn.execute("""INSERT INTO leads
                (company,contact_name,email,phone,website,segment,city,source,need,notes,status,score,consent_basis,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Novo',%s,%s,%s,%s)""",
                (data["company"], data["contact_name"], data["email"], data["phone"], data["website"], data["segment"], data["city"], data["source"], data["need"], data["notes"], score, data["consent_basis"], now, now))
        flash("Lead cadastrado.", "success")
        return redirect(url_for("leads"))
    lead = {
        "segment": request.args.get("segment", "").strip()[:80],
        "city": request.args.get("city", "").strip()[:120],
        "source": request.args.get("source", "").strip()[:120],
    }
    return render_template("lead_form.html", lead=lead, statuses=STATUSES, heading="Adicionar lead")


@app.route("/leads/<int:lead_id>", methods=["GET", "POST"])
@login_required
def edit_lead(lead_id):
    fields = ["company", "contact_name", "email", "phone", "website", "segment", "city", "source", "need", "notes", "status", "consent_basis"]
    with db() as conn:
        existing = conn.execute("SELECT * FROM leads WHERE id=%s", (lead_id,)).fetchone()
    if not existing:
        flash("Lead não encontrado.", "error")
        return redirect(url_for("leads"))
    if request.method == "POST":
        data = {key: request.form.get(key, "").strip() for key in fields}
        if not data["company"]:
            flash("Informe o nome da empresa.", "error")
            return render_template("lead_form.html", lead={**dict(existing), **data}, statuses=STATUSES, heading="Editar lead")
        if data["status"] not in STATUSES:
            data["status"] = "Novo"
        score = score_lead(data)
        now = now_iso()
        with db() as conn:
            conn.execute("""UPDATE leads SET company=%s,contact_name=%s,email=%s,phone=%s,website=%s,segment=%s,city=%s,source=%s,need=%s,notes=%s,status=%s,score=%s,consent_basis=%s,updated_at=%s WHERE id=%s""",
                (data["company"], data["contact_name"], data["email"], data["phone"], data["website"], data["segment"], data["city"], data["source"], data["need"], data["notes"], data["status"], score, data["consent_basis"], now, lead_id))
        flash("Lead atualizado.", "success")
        return redirect(url_for("leads"))
    return render_template("lead_form.html", lead=dict(existing), statuses=STATUSES, heading="Editar lead")


def _normalize_match_text(value):
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return " ".join("".join(ch if ch.isalnum() else " " for ch in value).split())


def _photon_contact_value(props, extra, *keys):
    for key in keys:
        value = props.get(key) or extra.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _fetch_saved_osm_item(notes):
    """Read the exact saved OSM object and derive a coordinate for location confirmation."""
    match = re.search(
        r"OpenStreetMap item:\s*https://www\.openstreetmap\.org/(node|way|relation)/(\d+)",
        notes or "",
    )
    if not match:
        return None
    osm_kind, osm_id = match.groups()
    source_url = f"https://www.openstreetmap.org/{osm_kind}/{osm_id}"
    endpoint = f"https://api.openstreetmap.org/api/0.6/{osm_kind}/{osm_id}"
    # A node has coordinates itself. Ways/relations need their referenced nodes to estimate
    # the object's center; these are read-only API requests for the already saved object.
    api_url = endpoint + (".json" if osm_kind == "node" else "/full.json")
    req = URLRequest(
        api_url,
        headers={
            "User-Agent": "LeadPilotAI/0.6 (+https://leadpilot-ai-5642.onrender.com)",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urlopen(req, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    elements = payload.get("elements", [])
    element = next(
        (item for item in elements
         if str(item.get("id")) == osm_id and item.get("type") == osm_kind),
        None,
    )
    if not element:
        raise RuntimeError("A fonte não retornou o item original salvo neste lead.")
    lat = element.get("lat")
    lon = element.get("lon")
    if lat is None or lon is None:
        points = [
            (float(item["lat"]), float(item["lon"]))
            for item in elements
            if item.get("type") == "node" and item.get("lat") is not None and item.get("lon") is not None
        ]
        if points:
            # Bounding-box center is more stable than a mean for irregular building rings.
            lat = (min(point[0] for point in points) + max(point[0] for point in points)) / 2
            lon = (min(point[1] for point in points) + max(point[1] for point in points)) / 2
    return {
        "url": source_url,
        "tags": element.get("tags") or {},
        "type": osm_kind,
        "id": osm_id,
        "lat": float(lat) if lat is not None else None,
        "lon": float(lon) if lon is not None else None,
    }


def _photon_reverse_location(lat, lon):
    """Resolve the municipality for one exact coordinate using Photon reverse geocoding."""
    if lat is None or lon is None:
        return {}
    url = "https://photon.komoot.io/reverse?" + urlencode({
        "lat": f"{float(lat):.7f}",
        "lon": f"{float(lon):.7f}",
        "lang": "default",
        "limit": 1,
    })
    req = URLRequest(
        url,
        headers={
            "User-Agent": "LeadPilotAI/0.6 (+https://leadpilot-ai-5642.onrender.com)",
            "Accept": "application/geo+json, application/json",
        },
        method="GET",
    )
    with urlopen(req, timeout=7) as response:
        payload = json.loads(response.read().decode("utf-8"))
    features = payload.get("features") or []
    if not features:
        return {}
    props = features[0].get("properties") or {}
    city_name = (
        props.get("city")
        or props.get("municipality")
        or ""
    ).strip()
    address_parts = [
        props.get("housenumber") or "",
        props.get("street") or "",
        props.get("district") or "",
        city_name,
        props.get("state") or "",
    ]
    return {
        "city": city_name,
        "state": (props.get("state") or "").strip(),
        "country": (props.get("country") or "").strip(),
        "address": ", ".join(str(part).strip() for part in address_parts if str(part).strip()),
        "osm_name": (props.get("name") or "").strip(),
        "source_url": url,
    }


def _find_overpass_business_match(company, city, ignored_urls=None):
    """Find the exact business name inside the requested OSM administrative area.

    Contact fields are returned only after the candidate's municipality is confirmed
    by an explicit addr:city tag or Photon reverse geocoding of its coordinates.
    """
    from prospecting import _build_query, _request_overpass

    ignored_urls = set(ignored_urls or [])
    requested_city = (city or "").split(",", 1)[0].strip()
    expected_city = _normalize_match_text(requested_city)
    _clean_city, query = _build_query(company, city)
    rows = _request_overpass(query, requested_city)
    company_norm = _normalize_match_text(company)
    matches = []

    for row in rows:
        source_url = (row.get("osm_url") or "").strip()
        if source_url and source_url in ignored_urls:
            continue
        found_name = (row.get("name") or "").strip()
        found_norm = _normalize_match_text(found_name)
        if not found_name or not found_norm or not company_norm:
            continue

        name_score = SequenceMatcher(None, company_norm, found_norm).ratio()
        if company_norm == found_norm:
            name_score = 1.0
        elif min(len(company_norm), len(found_norm)) >= 4 and (
            company_norm in found_norm or found_norm in company_norm
        ):
            name_score = max(name_score, 0.84)
        if name_score < 0.80:
            continue

        tagged_city = (row.get("source_city") or "").strip()
        tagged_city_norm = _normalize_match_text(tagged_city)
        tagged_city_match = bool(tagged_city_norm and expected_city == tagged_city_norm)
        if tagged_city and not tagged_city_match:
            # Explicit addr:city conflicts with the searched municipality.
            continue

        reverse = {}
        if row.get("lat") is not None and row.get("lon") is not None:
            try:
                reverse = _photon_reverse_location(row["lat"], row["lon"])
            except Exception as exc:
                current_app.logger.warning(
                    "Overpass candidate reverse geocoding failed (%s): %s",
                    source_url, str(exc)[:180]
                )
        reverse_city = (reverse.get("city") or "").strip()
        reverse_city_norm = _normalize_match_text(reverse_city)
        reverse_city_match = bool(reverse_city_norm and expected_city == reverse_city_norm)

        if reverse_city and not reverse_city_match:
            continue
        if not (tagged_city_match or reverse_city_match):
            # No confirmed municipality: do not suggest contacts from this candidate.
            continue

        verified_city = reverse_city if reverse_city_match else tagged_city
        matches.append({
            "name": found_name,
            "city": verified_city,
            "score": round(name_score * 100),
            "city_match": True,
            "source_url": source_url or "https://www.openstreetmap.org/",
            "website": (row.get("website") or "").strip(),
            "email": (row.get("email") or "").strip(),
            "phone": (row.get("phone") or "").strip()[:80],
            "address": (row.get("address") or reverse.get("address") or "").strip(),
            "location_method": (
                "Etiqueta addr:city do registro e limites administrativos do OpenStreetMap"
                if tagged_city_match
                else "Coordenadas do estabelecimento conferidas pelo Photon"
            ),
        })

    if not matches:
        return None
    matches.sort(key=lambda item: item["score"], reverse=True)
    if len(matches) > 1 and matches[0]["score"] == matches[1]["score"] and matches[0]["source_url"] != matches[1]["source_url"]:
        return {
            "ambiguous": True,
            "name": matches[0]["name"],
            "city": requested_city,
            "score": matches[0]["score"],
        }
    return matches[0]


@app.route("/leads/<int:lead_id>/research", methods=["GET", "POST"])
@login_required
def lead_research(lead_id):
    with db() as conn:
        raw = conn.execute("SELECT * FROM leads WHERE id=%s", (lead_id,)).fetchone()
    if not raw:
        flash("Lead não encontrado.", "error")
        return redirect(url_for("leads"))
    lead = dict(raw)

    company = (lead.get("company") or "").strip()
    city = (lead.get("city") or "").strip()
    segment = (lead.get("segment") or "").strip()
    base_query = " ".join(part for part in (company, segment, city) if part)
    quote = quote_plus
    research_links = [
        {
            "title": "Site oficial",
            "description": "Procure o domínio e confirme se pertence à empresa certa.",
            "url": "https://www.google.com/search?q=" + quote(f'"{company}" {city} site oficial'),
        },
        {
            "title": "Telefone e contato comercial",
            "description": "Procure telefone comercial publicado e confira no site oficial.",
            "url": "https://www.google.com/search?q=" + quote(f'"{company}" {city} telefone contato'),
        },
        {
            "title": "Mapa e endereço",
            "description": "Confira localização, nome e correspondência com a empresa cadastrada.",
            "url": "https://www.google.com/maps/search/?api=1&query=" + quote(base_query),
        },
        {
            "title": "Perfis comerciais",
            "description": "Procure perfis públicos da empresa para confirmar identidade e atividade.",
            "url": "https://www.google.com/search?q=" + quote(f'"{company}" {city} Instagram Facebook'),
        },
        {
            "title": "Busca alternativa",
            "description": "Compare resultados em outro buscador, se necessário.",
            "url": "https://www.bing.com/search?q=" + quote(f'"{company}" {city} contato comercial'),
        },
        {
            "title": "Diretório Cylex",
            "description": "Verifique se uma ficha comercial coincide com o nome, endereço e atividade.",
            "url": "https://www.google.com/search?q=" + quote(f'site:cylex.com.br/guarulhos "{company}" "{city}" telefone'),
        },
        {
            "title": "Diretório Solutudo",
            "description": "Compare o nome da empresa e o endereço antes de associar telefone ou e-mail.",
            "url": "https://www.google.com/search?q=" + quote(f'site:solutudo.com.br/empresas/sp/guarulhos "{company}"'),
        },
        {
            "title": "Diretório OpenAlfa",
            "description": "Confira a listagem de estabelecimentos no entorno e os dados da fonte.",
            "url": "https://www.google.com/search?q=" + quote(f'site:brazil-streets.openalfa.com "{company}" "{city}"'),
        },
    ]

    suggestions = {"website": "", "email": "", "phone": ""}
    enrichment_match = None
    enrichment_message = ""
    enrichment_source_checked = ""
    if request.method == "POST":
        try:
            saved_item = _fetch_saved_osm_item(lead.get("notes") or "")
            if saved_item:
                # Use the exact OSM object attached during prospecting, rather than
                # finding another business with the same name in a nearby city.
                tags = saved_item["tags"]
                found_name = (tags.get("name") or "").strip()
                company_norm = _normalize_match_text(company)
                found_norm = _normalize_match_text(found_name)
                name_score = SequenceMatcher(None, company_norm, found_norm).ratio() if found_norm and company_norm else 0
                if company_norm == found_norm:
                    name_score = 1.0
                elif company_norm in found_norm or found_norm in company_norm:
                    name_score = max(name_score, 0.84)

                if name_score < 0.80:
                    enrichment_message = (
                        "O registro original salvo no lead não confirma o nome da empresa. "
                        "Nenhum dado foi sugerido nem alterado; confira a fonte original."
                    )
                else:
                    tag_city = (
                        tags.get("addr:city")
                        or tags.get("addr:place")
                        or tags.get("is_in:city")
                        or ""
                    ).strip()
                    reverse_location = {}
                    if saved_item.get("lat") is not None and saved_item.get("lon") is not None:
                        try:
                            reverse_location = _photon_reverse_location(saved_item["lat"], saved_item["lon"])
                        except Exception as reverse_exc:
                            app.logger.warning(
                                "Photon reverse lookup unavailable for lead id=%s: %s",
                                lead_id, str(reverse_exc)[:180]
                            )

                    requested_city = city.split(",")[0].strip()
                    city_norm = _normalize_match_text(requested_city)
                    reverse_city = (reverse_location.get("city") or "").strip()
                    tag_city_matches = bool(
                        tag_city and city_norm
                        and _normalize_match_text(tag_city) == city_norm
                    )
                    reverse_city_matches = bool(
                        reverse_city and city_norm
                        and _normalize_match_text(reverse_city) == city_norm
                    )
                    mismatched_cities = [
                        value for value, matches in (
                            (tag_city, tag_city_matches),
                            (reverse_city, reverse_city_matches),
                        )
                        if value and not matches
                    ]
                    verified_city = (
                        reverse_city if reverse_city_matches
                        else tag_city if tag_city_matches
                        else ""
                    )
                    reported_city = (
                        mismatched_cities[0] if mismatched_cities
                        else verified_city
                        or tag_city
                        or reverse_city
                    )
                    location_address = reverse_location.get("address") or ""
                    location_method = (
                        "Tags do registro original do OpenStreetMap"
                        if tag_city_matches
                        else "Coordenadas conferidas pelo Photon"
                        if reverse_city_matches
                        else "Localização ainda não confirmada"
                    )

                    if mismatched_cities:
                        other_city = mismatched_cities[0]
                        enrichment_message = (
                            f"O nome “{found_name}” coincide, mas a localização pública aponta para “{other_city}”, "
                            f"não “{requested_city}”. Nenhum contato foi sugerido nem alterado. "
                            "Confira a unidade no mapa antes de usar estes dados."
                        )
                        enrichment_match = {
                            "name": found_name or company,
                            "city": reported_city or "Não especificado no registro",
                            "address": location_address,
                            "location_method": location_method,
                            "score": round(name_score * 100),
                            "city_match": False,
                            "source_url": saved_item["url"],
                            "found_count": 0,
                        }
                    elif not verified_city:
                        enrichment_message = (
                            "O registro original foi localizado, mas nem as etiquetas nem a consulta das coordenadas "
                            "confirmaram o município. Nenhum contato foi sugerido nem alterado."
                        )
                        enrichment_match = {
                            "name": found_name or company,
                            "city": reported_city or "Não especificado no registro",
                            "address": location_address,
                            "location_method": location_method,
                            "score": round(name_score * 100),
                            "city_match": False,
                            "source_url": saved_item["url"],
                            "found_count": 0,
                        }
                    else:
                        extra = tags
                        website = _photon_contact_value({}, extra, "website", "contact:website", "url")
                        email = _photon_contact_value({}, extra, "email", "contact:email")
                        phone = _photon_contact_value({}, extra, "phone", "contact:phone", "telephone")
                        if website and not website.lower().startswith(("http://", "https://")):
                            website = "https://" + website
                        parsed_site = urlparse(website) if website else None
                        if parsed_site and (parsed_site.scheme not in ("http", "https") or not parsed_site.netloc):
                            website = ""
                        if email and ("@" not in email or "." not in email.rsplit("@", 1)[-1] or any(ch.isspace() for ch in email)):
                            email = ""
                        suggestions = {"website": website, "email": email, "phone": phone[:80]}
                        enrichment_match = {
                            "name": found_name or company,
                            "city": verified_city,
                            "address": location_address,
                            "location_method": location_method,
                            "score": round(name_score * 100),
                            "city_match": True,
                            "source_url": saved_item["url"],
                            "found_count": sum(bool(value) for value in suggestions.values()),
                        }
                        enrichment_source_checked = (
                            f"OpenStreetMap — registro original: {saved_item['url']}; "
                            f"localização conferida via {location_method}"
                        )
                        if enrichment_match["found_count"]:
                            enrichment_message = (
                                f"Encontramos {enrichment_match['found_count']} campo(s) comercial(is) no registro original, "
                                f"e a localização foi confirmada como “{verified_city}”. Confira cada valor antes de salvar."
                            )
                        else:
                            enrichment_message = (
                                f"A empresa e o município “{verified_city}” foram confirmados, mas o registro original "
                                "não lista site, e-mail ou telefone. Nenhum contato foi inventado nem alterado."
                            )

            else:
                # Leads imported before the OSM item ID was saved use the conservative
                # Photon search, rejecting results whose municipality conflicts.
                from prospecting import _photon_json, _photon_city_bbox

                clean_city, bbox = _photon_city_bbox(city)
                params = {
                    "q": company,
                    "bbox": bbox,
                    "countrycode": "br",
                    "limit": 10,
                    "lang": "default",
                }
                payload = _photon_json(params, timeout=9)
                features = payload.get("features") or []
                if not features:
                    fallback_params = dict(params)
                    fallback_params["q"] = f"{company} {clean_city}"
                    payload = _photon_json(fallback_params, timeout=7)
                    features = payload.get("features") or []

                company_norm = _normalize_match_text(company)
                city_norm = _normalize_match_text(clean_city)
                ignored_urls = set(re.findall(
                    r"OpenStreetMap ignored item:\s*(https://www\.openstreetmap\.org/(?:node|way|relation)/\d+)",
                    lead.get("notes") or "",
                ))
                candidates = []
                mismatched_locations = []
                ignored_count = 0
                for feature in features:
                    props = feature.get("properties") or {}
                    osm_type = str(props.get("osm_type") or "").upper()
                    osm_id = props.get("osm_id")
                    osm_type_full = {"N": "node", "W": "way", "R": "relation"}.get(osm_type, osm_type.lower())
                    feature_source_url = (
                        f"https://www.openstreetmap.org/{osm_type_full}/{osm_id}"
                        if osm_type_full and osm_id else ""
                    )
                    if feature_source_url and feature_source_url in ignored_urls:
                        ignored_count += 1
                        continue
                    found_name = (props.get("name") or "").strip()
                    found_norm = _normalize_match_text(found_name)
                    if not found_norm or not company_norm:
                        continue
                    name_score = SequenceMatcher(None, company_norm, found_norm).ratio()
                    if company_norm in found_norm or found_norm in company_norm:
                        if min(len(company_norm), len(found_norm)) >= 4:
                            name_score = max(name_score, 0.84)
                    reported_city = (props.get("city") or props.get("locality") or "").strip()
                    found_city = _normalize_match_text(reported_city)
                    city_match = bool(found_city and city_norm and (
                        found_city == city_norm or city_norm in found_city or found_city in city_norm
                    ))
                    if name_score < 0.72:
                        continue
                    if reported_city and not city_match:
                        if name_score >= 0.84 and feature_source_url:
                            mismatched_locations.append((
                                found_name, reported_city, round(name_score * 100),
                                feature_source_url, props,
                            ))
                        continue
                    if not city_match:
                        continue
                    candidates.append((name_score + 0.08, feature, name_score, city_match))

                if not candidates and mismatched_locations:
                    mismatched_locations.sort(key=lambda item: item[2], reverse=True)
                    found_name, reported_city, name_score_int, source_url, mismatch_props = mismatched_locations[0]
                    enrichment_message = (
                        f"A fonte encontrou o nome “{found_name}”, mas registrou o município como “{reported_city}”, "
                        f"não “{clean_city}”. Nenhum dado foi sugerido nem alterado. "
                        "Você pode ignorar este registro para que ele não reapareça nas próximas consultas."
                    )
                    enrichment_match = {
                        "name": found_name,
                        "city": reported_city,
                        "score": name_score_int,
                        "city_match": False,
                        "source_url": source_url,
                        "ignore_url": source_url,
                        "found_count": 0,
                    }
                elif not candidates:
                    if ignored_count:
                        enrichment_message = (
                            "O resultado de outra cidade que você rejeitou foi ignorado. "
                            "A fonte não encontrou outra correspondência suficientemente confiável em Guarulhos; "
                            "nenhum dado foi alterado. Confira as pesquisas externas abaixo."
                        )
                    else:
                        enrichment_message = (
                            "A fonte pública não encontrou uma correspondência confiável que também confirme o município pesquisado. "
                            "Nenhum dado foi alterado. Confira o nome e a cidade nas pesquisas externas."
                        )
                else:
                    candidates.sort(key=lambda item: item[0], reverse=True)
                    best = candidates[0]
                    ambiguous = (
                        len(candidates) > 1
                        and (best[0] - candidates[1][0]) < 0.015
                        and (best[1].get("properties") or {}).get("osm_id") != (candidates[1][1].get("properties") or {}).get("osm_id")
                    )
                    if ambiguous:
                        enrichment_message = (
                            "A busca encontrou mais de uma empresa com nome muito parecido nesta região. "
                            "Para evitar sugerir o contato da empresa errada, nenhum dado foi preenchido. "
                            "Confira as pesquisas de mapa e site oficial abaixo."
                        )
                    else:
                        _, feature, name_score, city_match = best
                        props = feature.get("properties") or {}
                        extra = props.get("extra") or {}
                        website = _photon_contact_value(props, extra, "website", "contact:website", "url")
                        email = _photon_contact_value(props, extra, "email", "contact:email")
                        phone = _photon_contact_value(props, extra, "phone", "contact:phone", "telephone")
                        if website and not website.lower().startswith(("http://", "https://")):
                            website = "https://" + website
                        parsed_site = urlparse(website) if website else None
                        if parsed_site and (parsed_site.scheme not in ("http", "https") or not parsed_site.netloc):
                            website = ""
                        if email and ("@" not in email or "." not in email.rsplit("@", 1)[-1] or any(ch.isspace() for ch in email)):
                            email = ""
                        suggestions = {"website": website, "email": email, "phone": phone[:80]}
                        props_osm_type = str(props.get("osm_type") or "").upper()
                        props_osm_id = props.get("osm_id")
                        props_osm_type_full = {"N": "node", "W": "way", "R": "relation"}.get(props_osm_type, props_osm_type.lower())
                        source_url = f"https://www.openstreetmap.org/{props_osm_type_full}/{props_osm_id}" if props_osm_type_full and props_osm_id else "https://www.openstreetmap.org/"
                        enrichment_match = {
                            "name": props.get("name") or company,
                            "city": reported_city,
                            "score": round(name_score * 100),
                            "city_match": city_match,
                            "source_url": source_url,
                            "found_count": sum(bool(value) for value in suggestions.values()),
                        }
                        if enrichment_match["found_count"]:
                            enrichment_message = (
                                f"Encontramos {enrichment_match['found_count']} campo(s) comercial(is) público(s) em um resultado provável. "
                                "Confira o nome, a cidade e cada valor antes de salvar."
                            )
                            enrichment_source_checked = f"OpenStreetMap / Photon — {source_url}"
                        else:
                            enrichment_message = (
                                "Encontramos uma correspondência provável pelo nome dentro da área pesquisada, "
                                "mas a fonte não lista site, e-mail ou telefone. Nenhum contato foi inventado nem alterado."
                            )
        except Exception:
            app.logger.exception("Falha no enriquecimento público do lead id=%s", lead_id)
            enrichment_message = (
                "A fonte pública não concluiu a consulta. Nenhum dado foi alterado. "
                "Tente mais tarde ou use as pesquisas externas abaixo."
            )

    has_saved_osm_reference = bool(re.search(
        r"OpenStreetMap item:\s*https://www\.openstreetmap\.org/(?:node|way|relation)/\d+",
        lead.get("notes") or "",
    ))
    return render_template(
        "research.html",
        lead=lead,
        research_links=research_links,
        base_query=base_query,
        suggestions=suggestions,
        enrichment_match=enrichment_match,
        enrichment_message=enrichment_message,
        enrichment_source_checked=enrichment_source_checked,
        has_saved_osm_reference=has_saved_osm_reference,
    )


@app.route("/leads/<int:lead_id>/research/discard-source", methods=["POST"])
@login_required
def discard_research_source(lead_id):
    """Remove only a mismatched OSM item pointer; keep the lead and all contact fields."""
    now = now_iso()
    with db() as conn:
        existing = conn.execute("SELECT * FROM leads WHERE id=%s", (lead_id,)).fetchone()
        if not existing:
            flash("Lead não encontrado.", "error")
            return redirect(url_for("leads"))

        notes = (existing.get("notes") or "").strip()
        pattern = r"(?:^|\n)OpenStreetMap item:\s*https://www\.openstreetmap\.org/(?:node|way|relation)/\d+"
        new_notes, removed = re.subn(pattern, "", notes, count=1)
        if not removed:
            flash("Este lead não tem uma referência exata do OpenStreetMap para descartar.", "error")
            return redirect(url_for("lead_research", lead_id=lead_id))

        # Keep the street address and other notes, but record why this source link was rejected.
        new_notes = re.sub(r"\n{3,}", "\n\n", new_notes).strip()
        audit = (
            f"[Revisão de fonte {now}] Referência do OpenStreetMap desassociada porque a localização "
            "publicada não correspondeu com segurança ao município deste lead. "
            "Empresa mantida no funil; contatos não alterados."
        )
        new_notes = (new_notes + "\n" + audit).strip() if new_notes else audit
        conn.execute(
            "UPDATE leads SET notes=%s, updated_at=%s WHERE id=%s",
            (new_notes, now, lead_id),
        )

    flash(
        "Referência geográfica incorreta desassociada. A empresa e os demais dados foram preservados; "
        "você pode fazer uma nova consulta por cidade.",
        "success",
    )
    return redirect(url_for("lead_research", lead_id=lead_id))


@app.route("/leads/<int:lead_id>/research/ignore-result", methods=["POST"])
@login_required
def ignore_research_result(lead_id):
    """Remember one rejected public listing so repeated lookups skip that wrong-city item."""
    source_url = request.form.get("source_url", "").strip()
    parsed = urlparse(source_url)
    valid_source = bool(
        parsed.scheme == "https"
        and parsed.netloc == "www.openstreetmap.org"
        and re.fullmatch(r"/(?:node|way|relation)/\d+", parsed.path)
    )
    if not valid_source:
        flash("A referência enviada não é um item válido do OpenStreetMap.", "error")
        return redirect(url_for("lead_research", lead_id=lead_id))

    now = now_iso()
    with db() as conn:
        existing = conn.execute("SELECT * FROM leads WHERE id=%s", (lead_id,)).fetchone()
        if not existing:
            flash("Lead não encontrado.", "error")
            return redirect(url_for("leads"))
        notes = (existing.get("notes") or "").strip()
        already = re.search(
            r"OpenStreetMap ignored item:\s*" + re.escape(source_url),
            notes,
        )
        if already:
            flash("Essa referência já foi excluída das próximas consultas.", "success")
            return redirect(url_for("lead_research", lead_id=lead_id))
        audit = (
            f"OpenStreetMap ignored item: {source_url}\n"
            f"[Revisão de fonte {now}] Resultado público ignorado por divergência de município. "
            "Lead e dados comerciais preservados."
        )
        new_notes = (notes + "\n" + audit).strip() if notes else audit
        conn.execute(
            "UPDATE leads SET notes=%s, updated_at=%s WHERE id=%s",
            (new_notes, now, lead_id),
        )
    flash(
        "Resultado de outra cidade excluído das próximas consultas. O lead e seus contatos foram preservados.",
        "success",
    )
    return redirect(url_for("lead_research", lead_id=lead_id))


@app.route("/leads/<int:lead_id>/research/save", methods=["POST"])
@login_required
def save_research_data(lead_id):
    fields = ["website", "email", "phone"]
    data = {key: request.form.get(key, "").strip() for key in fields}
    source_checked = request.form.get("checked_source", "").strip()[:240]
    research_note = request.form.get("research_note", "").strip()[:1000]

    if not source_checked:
        flash("Informe a fonte onde você confirmou os dados (por exemplo, site oficial ou Google Maps).", "error")
        return redirect(url_for("lead_research", lead_id=lead_id))

    website = data["website"]
    if website:
        parsed = urlparse(website)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            flash("O site precisa começar com https:// ou http:// e ter um domínio válido.", "error")
            return redirect(url_for("lead_research", lead_id=lead_id))

    email = data["email"]
    if email and ("@" not in email or "." not in email.rsplit("@", 1)[-1] or any(ch.isspace() for ch in email)):
        flash("Confira o formato do e-mail comercial.", "error")
        return redirect(url_for("lead_research", lead_id=lead_id))

    if len(data["phone"]) > 80:
        flash("O telefone deve ter até 80 caracteres.", "error")
        return redirect(url_for("lead_research", lead_id=lead_id))

    now = now_iso()
    with db() as conn:
        existing = conn.execute("SELECT * FROM leads WHERE id=%s", (lead_id,)).fetchone()
        if not existing:
            flash("Lead não encontrado.", "error")
            return redirect(url_for("leads"))

        updated = dict(existing)
        # Empty form fields preserve existing data to prevent accidental deletion.
        for key, value in data.items():
            if value:
                updated[key] = value
        updated["source"] = existing.get("source") or "Pesquisa manual"
        score = score_lead(updated)
        audit = f"[Pesquisa comercial {now}] Fonte conferida: {source_checked}"
        if research_note:
            audit += f" | Observações: {research_note}"
        old_notes = (existing.get("notes") or "").strip()
        new_notes = (old_notes + "\n" + audit).strip() if old_notes else audit
        conn.execute(
            """UPDATE leads
               SET website=%s, email=%s, phone=%s, notes=%s, score=%s, updated_at=%s
               WHERE id=%s""",
            (updated.get("website", ""), updated.get("email", ""), updated.get("phone", ""), new_notes, score, now, lead_id),
        )

    flash("Dados comerciais salvos no lead e a fonte foi registrada nas notas.", "success")
    return redirect(url_for("lead_research", lead_id=lead_id))


@app.route("/leads/<int:lead_id>/message")
@login_required
def lead_message(lead_id):
    with db() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE id=%s", (lead_id,)).fetchone()
    if not lead:
        flash("Lead não encontrado.", "error")
        return redirect(url_for("leads"))
    variants = make_message_variants(lead)
    qualification = assess_lead(lead)
    return render_template("message.html", lead=lead, variants=variants, qualification=qualification)


@app.route("/import", methods=["GET", "POST"])
@login_required
def import_csv():
    if request.method == "POST":
        upload = request.files.get("file")
        if not upload or not upload.filename.lower().endswith(".csv"):
            flash("Envie um arquivo CSV.", "error")
            return redirect(url_for("import_csv"))
        try:
            content = upload.stream.read().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(content))
            names = {name.strip().lower() for name in (reader.fieldnames or [])}
            if "company" not in names:
                flash("O CSV precisa ter pelo menos a coluna company.", "error")
                return redirect(url_for("import_csv"))
            count = 0
            now = now_iso()
            with db() as conn:
                for raw in reader:
                    row = {str(key).strip().lower(): (value or "").strip() for key, value in raw.items() if key}
                    company = row.get("company", "")
                    if not company:
                        continue
                    fields = {key: row.get(key, "") for key in ["contact_name", "email", "phone", "website", "segment", "city", "source", "need", "notes", "consent_basis"]}
                    score = score_lead({"company": company, **fields})
                    conn.execute("""INSERT INTO leads (company,contact_name,email,phone,website,segment,city,source,need,notes,status,score,consent_basis,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Novo',%s,%s,%s,%s)""",
                    (company, fields["contact_name"], fields["email"], fields["phone"], fields["website"], fields["segment"], fields["city"], fields["source"], fields["need"], fields["notes"], score, fields["consent_basis"], now, now))
                    count += 1
            flash(f"Importação concluída: {count} leads adicionados.", "success")
        except (UnicodeDecodeError, csv.Error, psycopg.Error) as exc:
            app.logger.exception("Falha na importação CSV")
            flash("Não foi possível importar o arquivo. Confira o CSV e tente novamente.", "error")
        return redirect(url_for("leads"))
    return render_template("import.html")


@app.route("/export")
@login_required
def export_csv():
    with db() as conn:
        rows = conn.execute("SELECT company,contact_name,email,phone,website,segment,city,source,need,notes,status,score,consent_basis,created_at,updated_at FROM leads ORDER BY id").fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["company", "contact_name", "email", "phone", "website", "segment", "city", "source", "need", "notes", "status", "score", "consent_basis", "created_at", "updated_at"])
    for row in rows:
        writer.writerow([row[key] for key in row.keys()])
    return Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=leads.csv"})


@app.route("/sample.csv")
@login_required
def sample_csv():
    data = "company,contact_name,email,phone,website,segment,city,source,need,notes,consent_basis\nExemplo Barbearia,Marcos,contato@example.com,,https://example.com,Barbearia,Guarulhos,Site público,Organizar agendamentos,Exemplo fictício; substituir por dados reais,Contato comercial pertinente\n"
    return Response(data, mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=modelo_leads.csv"})


@app.route("/health")
def health():
    return {"status": "ok", "app": "LeadPilot AI"}


from prospecting import bp as prospecting_bp
app.register_blueprint(prospecting_bp)


if not SECRET_KEY or not APP_USERNAME or not APP_PASSWORD_SHA256:
    raise RuntimeError("Configure SECRET_KEY, APP_USERNAME e APP_PASSWORD_SHA256 antes de iniciar o app.")

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)