import csv
import hashlib
import hmac
import io
import os
from datetime import datetime, timezone
from functools import wraps

import psycopg
from psycopg.rows import dict_row
from flask import Flask, Response, flash, redirect, render_template, request, session, url_for

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


def make_message(lead):
    company = lead["company"]
    contact = (lead.get("contact_name") or "").strip()
    segment = (lead.get("segment") or "").strip() or "sua empresa"
    need = (lead.get("need") or "").strip()
    opening = f"Olá, {contact}!" if contact else "Olá!"
    reason = f"Vi que a {company} atua no segmento de {segment}."
    if need:
        reason += f" Pensei em uma forma de ajudar com {need.lower()}."
    else:
        reason += " Estou entrando em contato para entender se há oportunidades de simplificar tarefas comerciais e acompanhar clientes potenciais."
    return (f"{opening}\n\n{reason}\n\n"
            "Trabalho com soluções práticas de organização e automação comercial. Faz sentido eu te enviar uma demonstração curta?\n\n"
            "Se não for relevante, é só me avisar que não volto a entrar em contato.\n\nObrigado!")


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
    sql = "SELECT * FROM leads WHERE TRUE"
    params = []
    if query:
        sql += " AND (company ILIKE %s OR contact_name ILIKE %s OR segment ILIKE %s OR city ILIKE %s OR email ILIKE %s)"
        params.extend([f"%{query}%"] * 5)
    if status and status in STATUSES:
        sql += " AND status=%s"
        params.append(status)
    sql += " ORDER BY score DESC, updated_at DESC"
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return render_template("leads.html", rows=rows, statuses=STATUSES, q=query, selected_status=status)


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


@app.route("/leads/<int:lead_id>/message")
@login_required
def lead_message(lead_id):
    with db() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE id=%s", (lead_id,)).fetchone()
    if not lead:
        flash("Lead não encontrado.", "error")
        return redirect(url_for("leads"))
    return render_template("message.html", lead=lead, message=make_message(lead))


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
