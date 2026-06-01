"""
SIGINT-MX · Servidor Flask v3
Fuentes: La Jornada, Proceso, Milenio, Animal Político, La Razón, Reforma, DOF + GDELT + GNews
"""

import logging
import threading
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler

# ── CONFIG ────────────────────────────────────────────────────
GNEWS_API_KEY = "df378fc63fed9b4694af2e7aa79d9fd3"
GNEWS_BASE    = "https://gnews.io/api/v4"

GNEWS_QUERIES = [
    "seguridad nacional México",
    "fuerzas armadas Sedena Marina México",
    "crimen organizado cárteles México",
    "política gobierno Sheinbaum México",
    "México Estados Unidos diplomacia Trump",
    "Pemex economía inversión México",
    "Centro Nacional Inteligencia CNI",
    "CNTE protesta conflicto social México",
]

# ── FUENTES RSS ───────────────────────────────────────────────
RSS_FEEDS = [
    {"name": "La Jornada",      "url": "https://www.jornada.com.mx/rss/politica.xml",           "backup": "https://www.jornada.com.mx/rss/ultimas.xml"},
    {"name": "La Jornada Seg.", "url": "https://www.jornada.com.mx/rss/sociedad.xml",            "backup": None},
    {"name": "Proceso",         "url": "https://www.proceso.com.mx/rss/feed.html?r=155",         "backup": "https://www.proceso.com.mx/rss/feed.html?r=136"},
    {"name": "Milenio",         "url": "https://www.milenio.com/rss/politica",                   "backup": "https://www.milenio.com/rss/policia"},
    {"name": "Animal Político", "url": "https://www.animalpolitico.com/feed",                    "backup": "https://animalpolitico.com/feed/"},
    {"name": "La Razón",        "url": "https://www.razon.com.mx/feed/",                         "backup": "https://razon.com.mx/rss.xml"},
    {"name": "Reforma",         "url": "https://www.reforma.com/rss/portada.xml",                "backup": None},
    {"name": "El Financiero",   "url": "https://www.elfinanciero.com.mx/rss/feed.xml",           "backup": None},
    {"name": "Sin Embargo",     "url": "https://www.sinembargo.mx/feed",                         "backup": None},
    {"name": "Aristegui",       "url": "https://aristeguinoticias.com/feed/",                    "backup": None},
    {"name": "NYT México",      "url": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "backup": None},
]

CNI_KEYWORDS = [
    "centro nacional de inteligencia", " cni ", "cni,", "cni.", "(cni)",
    "inteligencia civil", "espionaje", "contrainteligencia"
]

SECURITY_KEYWORDS = [
    "sedena", "marina", "guardia nacional", "ejercito", "fuerzas armadas",
    "cartel", "crimen organizado", "narco", "violencia", "homicidio",
    "secuestro", "extorsion", "fentanilo", "operativo", "detencion",
    "terrorismo", "amenaza", "ataque", "seguridad", "inteligencia",
    "espionaje", "contrainteligencia", "pemex", "corrupcion", "lavado",
    "diplomatico", "embajada", "trump", "estados unidos", "eu ",
    "sheinbaum", "gobierno federal", "congreso", "senado", "cnte",
    "dof", "decreto", "reforma", "ley", "acuerdo", "doi", "sinaloa",
    "chihuahua", "guerrero", "tamaulipas", "jalisco", "cjng"
]

# ── FLASK APP ─────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, origins=["https://noticias44.netlify.app", "http://localhost:5000", "http://127.0.0.1:5000", "null"])
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

cache = {"articles": [], "cni_mentions": [], "last_update": None, "stats": {}}
cache_lock = threading.Lock()

# ── UTILIDADES ────────────────────────────────────────────────
def calc_ire(title, desc=""):
    text = (title + " " + desc).lower()
    score = 1.0
    hits = sum(1 for kw in SECURITY_KEYWORDS if kw in text)
    score += hits * 0.4
    if any(k in text for k in ["guerra", "ataque", "terrorismo", "explosion", "masacre"]):
        score += 2.0
    if any(k in text for k in CNI_KEYWORDS):
        score += 1.5
    if any(k in text for k in ["trump", "estados unidos", "diplomatico", "embajada"]):
        score += 0.8
    if any(k in text for k in ["sinaloa", "cjng", "cartel", "narco"]):
        score += 1.0
    return round(min(score, 10.0), 1)

def classify_category(title, desc=""):
    text = (title + " " + desc).lower()
    if any(k in text for k in ["sedena", "marina", "ejercito", "guardia nacional", "militar", "fuerzas armadas"]):
        return "Seguridad"
    if any(k in text for k in ["cartel", "narco", "crimen", "homicidio", "violencia", "sinaloa", "cjng"]):
        return "Crimen Organizado"
    if any(k in text for k in CNI_KEYWORDS):
        return "Inteligencia"
    if any(k in text for k in ["trump", "eu ", "estados unidos", "diplomatico", "embajada", "doj"]):
        return "Internacional"
    if any(k in text for k in ["pemex", "economia", "pib", "inflacion", "peso", "inversion", "banxico"]):
        return "Economía"
    if any(k in text for k in ["sheinbaum", "gobierno", "congreso", "senado", "diputados", "morena", "decreto"]):
        return "Política"
    if any(k in text for k in ["dof", "diario oficial", "ley", "reglamento", "acuerdo", "norma"]):
        return "DOF/Normativo"
    return "General"

def is_cni(title, desc=""):
    text = (title + " " + desc).lower()
    return any(k in text for k in ["centro nacional de inteligencia", " cni ", "cni,", "cni.", "(cni)"])

def dedup(articles):
    seen = set()
    result = []
    for a in articles:
        url = a.get("url", "")
        title = a.get("title", "")[:60]
        key = url or title
        if key and key not in seen:
            seen.add(key)
            result.append(a)
    return result

def safe_get(url, timeout=12):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SIGINT-MX/3.0; +https://sigint-mx.local)",
        "Accept": "application/rss+xml, application/xml, text/xml, */*"
    }
    return requests.get(url, timeout=timeout, headers=headers)

# ── RSS ───────────────────────────────────────────────────────
def parse_rss(content, source_name):
    articles = []
    try:
        root = ET.fromstring(content)
        for item in root.iter("item"):
            title = item.findtext("title", "").strip()
            desc  = item.findtext("description", "").strip()
            link  = item.findtext("link", "").strip()
            pub   = item.findtext("pubDate", "")
            if not title:
                continue
            # limpiar HTML básico del description
            import re
            desc_clean = re.sub(r'<[^>]+>', '', desc)[:400]
            articles.append({
                "title": title,
                "summary": desc_clean or title,
                "analysis": desc_clean,
                "source": source_name,
                "url": link,
                "category": classify_category(title, desc_clean),
                "risk_score": calc_ire(title, desc_clean),
                "published_date": pub or datetime.now(timezone.utc).isoformat(),
                "image": None,
            })
    except Exception as e:
        logging.warning(f"Parse RSS error ({source_name}): {e}")
    return articles

def fetch_rss_feed(feed):
    name = feed["name"]
    urls = [feed["url"]]
    if feed.get("backup"):
        urls.append(feed["backup"])
    
    for url in urls:
        try:
            r = safe_get(url, timeout=12)
            if r.status_code == 200 and len(r.content) > 500:
                arts = parse_rss(r.content, name)
                if arts:
                    logging.info(f"RSS {name}: {len(arts)} artículos ✓")
                    return arts
                else:
                    logging.warning(f"RSS {name}: 0 artículos parseados desde {url}")
            else:
                logging.warning(f"RSS {name}: HTTP {r.status_code} en {url}")
        except Exception as e:
            logging.warning(f"RSS {name} error ({url}): {e}")
    
    logging.warning(f"RSS {name}: todas las URLs fallaron")
    return []

def fetch_all_rss():
    all_articles = []
    threads = []
    results = {}
    
    def worker(feed):
        results[feed["name"]] = fetch_rss_feed(feed)
    
    for feed in RSS_FEEDS:
        t = threading.Thread(target=worker, args=(feed,))
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join(timeout=20)
    
    for arts in results.values():
        all_articles.extend(arts)
    
    logging.info(f"RSS total: {len(all_articles)} artículos de {len(results)} fuentes")
    return all_articles

# ── DOF ───────────────────────────────────────────────────────
def fetch_dof():
    """Scraping del DOF - sumario del día"""
    articles = []
    try:
        from datetime import date
        today = date.today()
        url = f"https://www.dof.gob.mx/index.php?year={today.year}&month={today.month:02d}&day={today.day:02d}"
        r = safe_get(url, timeout=15)
        if r.status_code != 200:
            logging.warning(f"DOF HTTP {r.status_code}")
            return []
        
        import re
        content = r.text
        # Extraer títulos del sumario DOF
        pattern = r'<td[^>]*class="[^"]*"[^>]*>\s*([A-ZÁÉÍÓÚÑ][^<]{20,300})\s*</td>'
        matches = re.findall(pattern, content)
        
        for match in matches[:20]:
            title = match.strip()
            if len(title) < 20 or len(title) > 300:
                continue
            if any(skip in title.lower() for skip in ["javascript", "function", "var ", "css"]):
                continue
            articles.append({
                "title": title,
                "summary": f"DOF {today.strftime('%d/%m/%Y')}: {title[:200]}",
                "analysis": title,
                "source": "DOF",
                "url": url,
                "category": "DOF/Normativo",
                "risk_score": calc_ire(title),
                "published_date": datetime.now(timezone.utc).isoformat(),
                "image": None,
            })
        
        logging.info(f"DOF: {len(articles)} entradas")
    except Exception as e:
        logging.warning(f"DOF error: {e}")
    return articles

# ── GDELT ─────────────────────────────────────────────────────
def fetch_gdelt():
    articles = []
    queries = [
        "Mexico seguridad Sedena cartel",
        "Mexico narco frontera seguridad",
        "Mexico intelligence espionaje crimen",
        "Sheinbaum gobierno Mexico politica",
        "Mexico Trump Estados Unidos diplomacia",
    ]
    for q in queries:
        try:
            url = (
                f"https://api.gdeltproject.org/api/v2/doc/doc"
                f"?query={requests.utils.quote(q)}"
                f"&mode=artlist&maxrecords=25&format=json"
                f"&sourcelang=Spanish&sort=DateDesc&timespan=7d"
            )
            r = safe_get(url, timeout=20)
            if r.status_code == 429:
                logging.warning("GDELT: rate limit")
                break
            data = r.json()
            for item in data.get("articles", []):
                title = item.get("title", "")
                if not title:
                    continue
                articles.append({
                    "title": title,
                    "summary": title,
                    "analysis": "",
                    "source": item.get("domain", "GDELT"),
                    "url": item.get("url", ""),
                    "category": classify_category(title),
                    "risk_score": calc_ire(title),
                    "published_date": item.get("seendate", ""),
                    "image": None,
                })
        except Exception as e:
            logging.warning(f"GDELT error ({q}): {e}")
    logging.info(f"GDELT: {len(articles)} artículos")
    return articles

# ── GNEWS ─────────────────────────────────────────────────────
def fetch_gnews_query(query):
    try:
        url = (
            f"{GNEWS_BASE}/search"
            f"?q={requests.utils.quote(query)}"
            f"&lang=es&country=mx&max=10"
            f"&apikey={GNEWS_API_KEY}"
        )
        r = safe_get(url, timeout=12)
        if r.status_code != 200:
            logging.warning(f"GNews HTTP {r.status_code} para: {query}")
            return []
        data = r.json()
        articles = []
        for a in data.get("articles", []):
            title = a.get("title", "")
            desc  = a.get("description", "")
            articles.append({
                "title": title,
                "summary": desc[:300] if desc else title,
                "analysis": desc[:500] if desc else "",
                "source": a.get("source", {}).get("name", "GNews"),
                "url": a.get("url", ""),
                "category": classify_category(title, desc),
                "risk_score": calc_ire(title, desc),
                "published_date": a.get("publishedAt", ""),
                "image": a.get("image"),
            })
        return articles
    except Exception as e:
        logging.warning(f"GNews error ({query}): {e}")
        return []

def fetch_all_gnews():
    all_articles = []
    for q in GNEWS_QUERIES:
        arts = fetch_gnews_query(q)
        all_articles.extend(arts)
        time.sleep(0.3)
    unique = dedup(all_articles)
    cni = [a for a in unique if is_cni(a.get("title",""), a.get("summary",""))]
    logging.info(f"GNews: {len(unique)} artículos únicos, {len(cni)} menciones CNI")
    return unique, cni

# ── REFRESH PRINCIPAL ─────────────────────────────────────────
def refresh_data():
    logging.info("── Iniciando actualización de datos SIGINT-MX...")

    # 1. RSS paralelo
    rss_articles = fetch_all_rss()

    # 2. DOF
    dof_articles = fetch_dof()

    # 3. GDELT
    gdelt_articles = fetch_gdelt()

    # 4. Combinar
    combined = dedup(rss_articles + dof_articles + gdelt_articles)
    cni_mentions = [a for a in combined if is_cni(a.get("title",""), a.get("summary",""))]
    logging.info(f"RSS+DOF+GDELT: {len(combined)} artículos, {len(cni_mentions)} menciones CNI")

    # 5. GNews
    try:
        gnews_articles, gnews_cni = fetch_all_gnews()
        existing_urls = {a.get("url","") for a in combined}
        for a in gnews_articles:
            if a.get("url","") not in existing_urls:
                combined.append(a)
                existing_urls.add(a.get("url",""))
        existing_cni = {a.get("url","") for a in cni_mentions}
        for a in gnews_cni:
            if a.get("url","") not in existing_cni:
                cni_mentions.append(a)
        logging.info(f"Total con GNews: {len(combined)} artículos, {len(cni_mentions)} menciones CNI")
    except Exception as e:
        logging.warning(f"GNews error: {e}")

    # 6. Ordenar
    combined.sort(key=lambda x: x.get("risk_score", 0), reverse=True)
    cni_mentions.sort(key=lambda x: x.get("risk_score", 0), reverse=True)

    # 7. Stats
    categories = {}
    sources = {}
    for a in combined:
        cat = a.get("category", "General")
        src = a.get("source", "?")
        categories[cat] = categories.get(cat, 0) + 1
        sources[src] = sources.get(src, 0) + 1

    avg_ire = round(sum(a.get("risk_score",0) for a in combined) / len(combined), 1) if combined else 0.0

    stats = {
        "total": len(combined),
        "cni_count": len(cni_mentions),
        "avg_ire": avg_ire,
        "categories": categories,
        "sources": sources,
    }

    with cache_lock:
        cache["articles"]     = combined[:150]
        cache["cni_mentions"] = cni_mentions[:30]
        cache["last_update"]  = datetime.now(timezone.utc).isoformat()
        cache["stats"]        = stats

    logging.info(f"── Actualización completa: {len(combined)} artículos únicos, {len(cni_mentions)} menciones CNI")

# ── RUTAS ─────────────────────────────────────────────────────
@app.route("/")
def index():
    with cache_lock:
        return jsonify({"service":"SIGINT-MX","status":"running","articles":len(cache["articles"]),"cni":len(cache["cni_mentions"]),"updated":cache["last_update"]})

@app.route("/infografia")
def infografia():
    return render_template("index.html")

@app.route("/api/dashboard")
def api_dashboard():
    with cache_lock:
        return jsonify({"articles":cache["articles"],"cni_mentions":cache["cni_mentions"],"last_update":cache["last_update"],"stats":cache["stats"]})

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    threading.Thread(target=refresh_data, daemon=True).start()
    return jsonify({"status":"refresh_started"})

@app.route("/api/status")
def api_status():
    with cache_lock:
        return jsonify({"last_update":cache["last_update"],"total_articles":len(cache["articles"]),"cni_count":len(cache["cni_mentions"]),"stats":cache["stats"]})

# ── ARRANQUE ──────────────────────────────────────────────────
scheduler = BackgroundScheduler()
scheduler.add_job(refresh_data, "interval", minutes=60, id="refresh_job")
scheduler.start()

threading.Thread(target=refresh_data, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
