"""Prospeccao assistida sem scraping nem coleta automatica de dados externos."""
from urllib.parse import quote_plus

from flask import Blueprint, redirect, render_template, request, session, url_for

bp = Blueprint("prospecting_tools", __name__)


@bp.before_request
def protect_prospecting():
    if not session.get("authenticated"):
        return redirect(url_for("login"))


@bp.route("/prospecting", methods=["GET"])
def prospecting():
    segment = request.args.get("segment", "").strip()[:80]
    city = request.args.get("city", "").strip()[:120]
    searched = request.args.get("search") == "1" and bool(segment and city)

    maps_url = google_url = bing_url = ""
    register_url = url_for("new_lead")
    if searched:
        query = f"{segment} {city}"
        maps_url = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(query)
        google_url = "https://www.google.com/search?q=" + quote_plus(f"{segment} empresas em {city}")
        bing_url = "https://www.bing.com/search?q=" + quote_plus(f"{segment} empresas em {city}")
        register_url = url_for(
            "new_lead",
            segment=segment,
            city=city,
            source="Pesquisa assistida (revisão manual)",
        )

    return render_template(
        "prospecting.html",
        segment=segment,
        city=city,
        searched=searched,
        maps_url=maps_url,
        google_url=google_url,
        bing_url=bing_url,
        register_url=register_url,
    )
