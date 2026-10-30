# -*- coding: utf-8 -*-
"""Actualisation du dashboard Elite Média.

1. Récupère les fiches, statistiques, avis… via l'API Local Nord (clé en lecture seule recommandée)
2. Calcule les indicateurs (mêmes calculs que le dashboard)
3. Compresse puis CHIFFRE le tout (AES-256-GCM, clé dérivée du mot de passe) -> site/data.txt

Variables d'environnement :
  LN_KEY          clé API Local Nord (obligatoire)
  EM_PASSPHRASE   mot de passe du dashboard (obligatoire, >= 16 caractères conseillés)
  LAG_DAYS        retard supposé de Google en jours (défaut 8)
  Pour les tests : LIMIT (nombre de fiches), REVIEW_DAYS (jours d'avis), TODAY (AAAA-MM-JJ), OUT_DIR
"""
import hashlib
import base64, datetime as dt, gzip, json, os, re, sys, threading, time, random
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from sectors import sector_of, parse_address, departement

API = "https://app.localnord.io/api/v1"
RATE = 4.2        # requêtes par seconde (limite de l'API : 300 par minute)
WORKERS = 6
NON_SITE_HOSTS = ["facebook.com", "instagram.com", "planity.com", "doctolib.fr", "calendly.com", "linktr.ee", "tiktok.com", "youtube.com",
                  "linkedin.com", "wa.me", "whatsapp.com", "tidycal", "kiute", "resalib", "taplink", "beacons.ai", "lnk.bio", "linkin.bio",
                  "g.page", "maps.app", "goo.gl", "business.site"]
SOCIAL_ORDER = ["facebook", "instagram", "linkedin", "tiktok", "youtube"]
COUNTRY_WORDS = {"france", "belgique", "suisse", "luxembourg", "pays-bas", "allemagne", "espagne", "italie", "monaco", "canada"}
IMPORT_DAY = dt.date(2026, 4, 16)       # date d'ajout en bloc (import initial du portefeuille)


# =========================================================================== API
class Api:
    def __init__(self, key):
        import requests
        self.s = requests.Session(); self.s.headers["Authorization"] = "Bearer " + key
        self.lock = threading.Lock(); self.next = 0.0; self.calls = 0; self.remaining = None
        self.requests = requests

    def _throttle(self):
        with self.lock:
            now = time.monotonic(); slot = max(self.next, now); self.next = slot + 1.0 / RATE
        if slot - now > 0:
            time.sleep(slot - now)

    def get(self, path, params=None, tries=6, soft=False):
        """GET JSON. soft=True : renvoie None (au lieu d'une erreur) pour 403/404."""
        last = None
        for i in range(tries):
            self._throttle()
            try:
                r = self.s.get(API + path, params=params, timeout=90); self.calls += 1
                rem = r.headers.get("x-ratelimit-remaining")
                if rem is not None:
                    self.remaining = int(rem)
                    if self.remaining < 200:
                        raise SystemExit("Quota journalier de l'API presque épuisé : arrêt pour ne pas bloquer vos autres outils.")
                if r.status_code == 401:
                    raise SystemExit("Clé API refusée (401) : vérifiez le secret LN_KEY.")
                if r.status_code == 429:
                    time.sleep(float(r.headers.get("Retry-After", "5")) + random.random()); continue
                if r.status_code >= 500:
                    time.sleep(2 ** i + random.random()); last = r.status_code; continue
                if r.status_code in (403, 404) and soft:
                    return None
                r.raise_for_status()
                return r.json()
            except self.requests.RequestException as e:
                last = str(e)[:120]; time.sleep(2 ** i + random.random())
        raise RuntimeError(f"Échec de {path} : {last}")


# =========================================================================== collecte
def fetch_listings(api):
    first = api.get("/listings")
    data = list(first.get("data") or [])
    pg = first.get("pagination") or {}
    for p in range(2, (pg.get("total_pages") or 1) + 1):
        data += api.get("/listings", {"page": p}).get("data") or []
    seen, out = set(), []
    for x in data:
        if x["id"] not in seen:
            seen.add(x["id"]); out.append(x)
    return out


def fetch_one(api, x, today, cached=None):
    lid = x["id"]
    created = dt.datetime.fromisoformat(x["created_at"].replace("Z", "+00:00")).date()
    # historique nécessaire : depuis l'ajout + 4 mois avant l'ajout
    need = (today - created).days + 125
    pages = max(1, min(8, -(-need // 100)))
    stats = []
    for p in range(1, pages + 1):
        r = api.get("/stats", {"listing_id": lid, "per_page": 100, "page": p})
        stats += r.get("data") or []
        if p >= ((r.get("pagination") or {}).get("total_pages") or 1):
            break
    if cached is not None:       # mode léger : score, réseaux, photos et publications changent lentement -> repris de la veille
        service_area = None
        if not x.get("address"):
            service_area = ((api.get(f"/listings/{lid}", soft=True) or {}).get("data") or {}).get("service_area")
        return {"listing": x, "stats": stats, "cached": cached, "service_area": service_area, "score": {}, "social": [], "photos": None, "pub_dates": []}
    sc = (api.get(f"/listings/{lid}/score", soft=True) or {}).get("data") or {}
    attrs = (api.get(f"/listings/{lid}/attributes", soft=True) or {}).get("data") or {}
    # photos réellement présentes sur la fiche Google
    photos, token, n = 0, None, 0
    while True:
        params = {"format": "photo"}
        if token:
            params["page_token"] = token
        r = api.get(f"/listings/{lid}/media", params, soft=True)
        if r is None:
            photos = None; break
        photos += len(r.get("data") or [])
        token = (r.get("pagination") or {}).get("next_page_token"); n += 1
        if not token or n >= 30:
            break
    # publications publiées (dates)
    pubs, page = [], 1
    while True:
        r = api.get("/publications", {"listing_id": lid, "status": "published", "per_page": 100, "page": page})
        pubs += r.get("data") or []
        if page >= ((r.get("pagination") or {}).get("total_pages") or 1) or page >= 20:
            break
        page += 1
    pub_dates = [(p.get("published_at") or p.get("scheduled_at") or p.get("created_at")) for p in pubs]
    service_area = None
    if not x.get("address"):
        d = (api.get(f"/listings/{lid}", soft=True) or {}).get("data") or {}
        service_area = d.get("service_area")
    return {"listing": x, "stats": stats, "score": sc, "social": attrs.get("social") or [], "photos": photos,
            "pub_dates": pub_dates, "service_area": service_area}


def fetch_reviews(api, today, days):
    out, page = [], 1
    frm = (today - dt.timedelta(days=days)).isoformat()
    while True:
        r = api.get("/reviews", {"from_date": frm, "to_date": today.isoformat(), "per_page": 100, "page": page})
        out += [{"listing_id": x["listing_id"], "d": (x.get("published_at") or "")[:10], "r": x.get("rating")} for x in r.get("data") or []]
        if page >= ((r.get("pagination") or {}).get("total_pages") or 1) or page >= 200:
            break
        page += 1
    return out


def fetch_all(api, today, limit=None, review_days=170, prev=None):
    listings = fetch_listings(api)
    if limit:
        listings = listings[:limit]
    print(f"{len(listings)} fiches à actualiser", flush=True)
    raw, t0, done = {}, time.time(), 0
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(fetch_one, api, x, today, (prev or {}).get(x["id"])): x["id"] for x in listings}
        for f in as_completed(futs):
            raw[futs[f]] = f.result(); done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(listings)} fiches | {api.calls} appels | quota restant {api.remaining} | {time.time() - t0:.0f}s", flush=True)
    reviews = fetch_reviews(api, today, review_days)
    print(f"  avis récupérés : {len(reviews)} | total appels : {api.calls} | quota restant : {api.remaining}", flush=True)
    return listings, raw, reviews


# =========================================================================== calculs
def host(url):
    u = url if "://" in url else "http://" + url
    return urlparse(u).netloc.lower().replace("www.", "")


def has_real_site(url):
    return bool(url) and not any(p in host(url) for p in NON_SITE_HOSTS)


def city_from_service_area(sa):
    for s in sa or []:
        s = re.sub(r",\s*(France|Belgique|Suisse|Luxembourg|Pays-Bas|Allemagne|Espagne|Italie|Monaco|Canada)\s*$", "", s.strip(), flags=re.I)
        if not s or s.lower() in COUNTRY_WORDS:
            continue
        m = re.match(r"^(\d{4,5})\s+(.+)$", s)
        if m:
            return m.group(1), m.group(2).strip()
        return None, s
    return None, None


def impressions(d):
    return d["impressions_mobile_search"] + d["impressions_desktop_search"] + d["impressions_mobile_maps"] + d["impressions_desktop_maps"]


METRICS = {"v": impressions, "c": lambda d: d["website_clicks"], "a": lambda d: d["call_clicks"], "d": lambda d: d["direction_requests"]}


def block(days, start, n=30):
    vals, cnt = {k: 0 for k in METRICS}, 0
    for j in range(n):
        d = days.get((start + dt.timedelta(days=j)).isoformat())
        if d is None:
            continue
        cnt += 1
        for k, fn in METRICS.items():
            vals[k] += fn(d)
    return vals, cnt


def build_pack(listings, raw, reviews, today, last_full, series_days=210):
    fiches, series_rows = [], []
    start_series = (today - dt.timedelta(days=series_days)).isoformat()
    unmapped = {}
    for x in listings:
        r = raw[x["id"]]
        days = {d["date"]: d for d in r["stats"]}
        A = dt.datetime.fromisoformat(x["created_at"].replace("Z", "+00:00")).date()
        cp, ville = parse_address(x["address"])
        if not x["address"]:
            cp, ville = city_from_service_area(r["service_area"])
        sector = sector_of(x["category"])
        if sector == "Autres / non classé":
            unmapped[x["category"]] = unmapped.get(x["category"], 0) + 1
        soc = {}
        for s in r["social"]:
            soc.setdefault(s["type"], s.get("url"))
        pd_ = sorted(d[:10] for d in r["pub_dates"] if d and dt.date.fromisoformat(d[:10]) >= A)
        rec = {
            "id": x["id"], "nom": x["name"], "secteur": sector, "categorie": x["category"], "ville": ville, "dept": departement(cp),
            "site": "Oui" if has_real_site(x.get("website")) else "Non",
            "reseaux": "Oui" if (any(soc.get(t) for t in SOCIAL_ORDER) or any(t not in SOCIAL_ORDER for t in soc)) else "Non",
            "note": x.get("google_rating"), "avis": x.get("google_reviews_count") or 0, "photos": r["photos"], "posts": len(r["pub_dates"]),
            "score": r["score"].get("health_score"), "palier": r["score"].get("tier") or "—",
            "adresse": "Oui" if x["address"] else "Non", "ajout": A.isoformat(), "mois_gestion": round((today - A).days / 30.44, 1),
            "import_16_04": "Oui" if A == IMPORT_DAY else "Non",
            "premier_post": pd_[0] if pd_ else None,
            "mois_optim": round((today - dt.date.fromisoformat(pd_[0])).days / 30.44, 1) if pd_ else None,
        }
        dispo_pre = 0
        for j in range(1, 5):
            b, c = block(days, A - dt.timedelta(days=30 * j)); ok = c >= 28
            for k in METRICS:
                rec[f"{k}_p{j}"] = b[k] if ok else None
            if ok and dispo_pre == j - 1:
                dispo_pre = j
        rec["mois_avant_dispo"] = dispo_pre
        dispo = 0
        for m in range(1, 6):
            s0 = A + dt.timedelta(days=30 * (m - 1)); e0 = s0 + dt.timedelta(days=29)
            if e0 <= last_full:
                b, c = block(days, s0); ok = c >= 28
                for k in METRICS:
                    rec[f"{k}_m{m}"] = b[k] if ok else None
                if ok and dispo == m - 1:
                    dispo = m
            else:
                for k in METRICS:
                    rec[f"{k}_m{m}"] = None
        rec["mois_dispo"] = dispo
        nd = (last_full - A).days + 1
        if nd > 0:
            bc, _ = block(days, A, nd)
            for k in METRICS:
                rec[f"{k}_cumul"] = bc[k]
        else:
            for k in METRICS:
                rec[f"{k}_cumul"] = None
        c = r.get("cached")
        if c:
            for k in ("score", "palier", "photos", "posts", "reseaux", "premier_post"):
                rec[k] = c.get(k)
            rec["mois_optim"] = round((today - dt.date.fromisoformat(c["premier_post"])).days / 30.44, 1) if c.get("premier_post") else None
        rec["idx"] = len(fiches)
        fiches.append(rec)
        for dte, d in sorted(days.items()):
            if dte < start_series:
                continue
            v, c, a, i = impressions(d), d["website_clicks"], d["call_clicks"], d["direction_requests"]
            if v or c or a or i:
                series_rows.append((rec["idx"], dte, v, c, a, i))
    idx_of = {f["id"]: f["idx"] for f in fiches}
    avis_rows = [(idx_of[x["listing_id"]], x["d"], x["r"]) for x in reviews if x["listing_id"] in idx_of and x["d"]]
    base = dt.date.fromisoformat(start_series)
    off = lambda s: (dt.date.fromisoformat(s) - base).days
    pack = {
        "base": base.isoformat(), "built": today.isoformat(), "last_full": last_full.isoformat(), "fiches": fiches,
        "jours": {"n": [r[0] for r in series_rows], "d": [off(r[1]) for r in series_rows], "v": [r[2] for r in series_rows],
                  "c": [r[3] for r in series_rows], "a": [r[4] for r in series_rows], "i": [r[5] for r in series_rows]},
        "avis": {"n": [r[0] for r in avis_rows], "d": [off(r[1]) for r in avis_rows], "r": [r[2] for r in avis_rows]},
    }
    return pack, unmapped



# =========================================================================== GoHighLevel (clients à traiter)
GHL_API = "https://services.leadconnectorhq.com"


class Ghl:
    def __init__(self, token):
        import requests
        self.s = requests.Session()
        self.s.headers.update({"Authorization": "Bearer " + token, "Version": "2021-07-28", "Accept": "application/json"})
        self.lock = threading.Lock(); self.next = 0.0; self.requests = requests; self.calls = 0

    def get(self, path, params=None, tries=5):
        last = None
        for i in range(tries):
            with self.lock:
                now = time.monotonic(); slot = max(self.next, now); self.next = slot + 0.25
            if slot - now > 0:
                time.sleep(slot - now)
            try:
                r = self.s.get(GHL_API + path, params=params, timeout=90); self.calls += 1
                if r.status_code in (401, 403):
                    raise SystemExit(f"GoHighLevel a refusé l'accès ({r.status_code}) : vérifiez GHL_TOKEN et ses permissions de lecture (opportunités, contacts, champs personnalisés).")
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(2 ** i + random.random()); last = r.status_code; continue
                r.raise_for_status()
                return r.json()
            except self.requests.RequestException as e:
                last = str(e)[:120]; time.sleep(2 ** i + random.random())
        raise RuntimeError(f"Échec GoHighLevel {path} : {last}")


def _norm(s):
    import unicodedata
    s = unicodedata.normalize("NFD", str(s or "").lower())
    return re.sub(r"[^a-z0-9 ]", " ", "".join(c for c in s if unicodedata.category(c) != "Mn"))


def _toks(s):
    return [t for t in _norm(s).split() if len(t) > 2]


def _days(a, b):
    return int((b - a).total_seconds() // 86400)


WA_CODES = ("RESIL", "REMB", "DECU")      # demande d'arrêt, remboursement / prélèvement contesté, déception
_WA_SYN = [(re.compile(r"resil|résil|arr[êe]t|stop|annul|fin de contrat", re.I), "RESIL"),
           (re.compile(r"rembours|pr[ée]l[èe]vement|avoir|litige|opposition", re.I), "REMB"),
           (re.compile(r"d[ée][cç]u|d[ée]ception|m[ée]content|insatisf|friche", re.I), "DECU")]


def parse_wa(raw, exp):
    """Étiquette écrite par la routine WhatsApp. Format voulu : « CODE|AAAA-MM-JJ|libellé », mais on accepte aussi
    « RESIL 2026-10-29 » ou « Demande d'arrêt 2026-10-29 ». Renvoie (code, jours depuis l'alerte, libellé)."""
    if not raw:
        return None, None, None
    raw = str(raw).strip()
    code = None
    m = re.match(r"\s*(RESIL|REMB|DECU)\b", raw, re.I)
    if m:
        code = m.group(1).upper()
    else:
        for rx, c in _WA_SYN:
            if rx.search(raw):
                code = c
                break
    if code is None:
        return None, None, None
    d = None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        try:
            d = dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=dt.timezone.utc)
        except ValueError:
            d = None
    else:
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
        if m:
            try:
                d = dt.datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=dt.timezone.utc)
            except ValueError:
                d = None
    days = max(0, (exp - d).days) if d else 14      # sans date : on suppose 14 jours, pour que l'alerte finisse par expirer
    if days > 60:
        return None, None, None
    parts = [p.strip() for p in raw.split("|")]
    return code, days, (parts[2][:60] if len(parts) > 2 and parts[2] else None)


def clients_from_ghl(opps, stages, field_ids, fiches, now):
    """Une ligne par client actif (pas en Churn, pas perdu), mêmes champs que le tableau de bord."""
    exp = dt.datetime(now.year, now.month, now.day, tzinfo=dt.timezone.utc)
    by_name = [(f["id"], set(_toks(f["nom"]))) for f in fiches]
    known = {f["id"] for f in fiches}

    def parse(s):
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None

    out = []
    for o in opps:
        etape = stages.get(o.get("pipelineStageId"), "")
        if etape == "Churn" or o.get("status") == "lost":
            continue
        cfs = {c.get("id"): c for c in (o.get("customFields") or [])}

        def val(name):
            c = cfs.get(field_ids.get(name.strip().lower()))
            if not c:
                return None
            if c.get("fieldValueDate") is not None:
                return c["fieldValueDate"]
            if c.get("fieldValueArray") is not None:
                return ",".join(str(x) for x in c["fieldValueArray"])
            return c.get("fieldValueString")

        def date_of(name):
            """Accepte un champ Date (horodatage) ou un champ Texte (« AAAA-MM-JJ » ou « JJ/MM/AAAA »)."""
            v = val(name)
            if isinstance(v, (int, float)):
                return dt.datetime.fromtimestamp(v / 1000, tz=dt.timezone.utc)
            if isinstance(v, str):
                m = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", v)
                d_ = (int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None
                if not m:
                    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})", v)
                    d_ = (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
                if d_:
                    try:
                        return dt.datetime(d_[2], d_[1], d_[0], tzinfo=dt.timezone.utc)
                    except ValueError:
                        return None
            return None

        created = parse(o.get("createdAt"))
        start = date_of("Date début offre 129€") or date_of("Date début offre 250€") or created
        tags = ((o.get("contact") or {}).get("tags")) or []
        tg = ",".join(tags)
        rrm = str(val("RRM") or "").strip().lower()
        rrm = "Oui" if (rrm.startswith("oui") or rrm == "non,oui") else ("non" if rrm == "non" else "")
        sat = ",".join(x for x in (val("Satisfaction"), val("Satisfaction 2")) if x)
        upd, stg = parse(o.get("updatedAt")), parse(o.get("lastStageChangeAt"))
        dm = date_of("WA - Dernier message client") or date_of("Date du dernier message")
        ct = date_of("WA - Dernier message équipe") or date_of("Dernier contact équipe")      # alimentés chaque matin par la routine WhatsApp
        wa_c, wa_d, wa_t = parse_wa(val("WA - Alerte"), exp)
        lnid = str(val("ID Local Nord") or "").strip()
        if lnid not in known:
            lnid = None
            t = _toks((o.get("contact") or {}).get("name") or o.get("name"))
            if len(t) >= 2:
                c = [fid for fid, ft in by_name if all(x in ft for x in t)]
                lnid = c[0] if len(c) == 1 else None
        out.append({
            "id": o["id"], "nom": str(o.get("name") or ""), "etape": etape,
            "age_m": round(_days(start, exp) / 30.44, 1) if start else 0,
            "rrm": rrm, "sat": sat, "gmb": val("Situation GMB") or "", "acces": val("Accès GMB") or "",
            "maj_j": _days(upd, now) if upd else None, "etape_j": _days(stg, now) if stg else None,
            "offre": "198€" if "198€" in tg else ("98€" if "98€" in tg else ""),
            "no_rep": bool(re.search(r"sans rep|no rep", tg)), "lnid": lnid, "exp": now.date().isoformat(),
            "dm_j": _days(dm, now) if dm else None,
            "ct_j": _days(ct, now) if ct else None,
            "resp": str(o.get("assignedTo") or ""),
            "val": o.get("monetaryValue") or None,
            "wa_c": wa_c, "wa_d": wa_d, "wa_t": wa_t,
        })
    return out


def fetch_clients(token, location, fiches, now):
    g = Ghl(token)
    pipes = g.get("/opportunities/pipelines", {"locationId": location}).get("pipelines") or []
    pipe = next((p for p in pipes if "clients gmb" in (p.get("name") or "").lower()), None)
    if not pipe:
        raise RuntimeError("Pipeline « Clients GMB » introuvable dans GoHighLevel.")
    stages = {s["id"]: s["name"] for s in pipe.get("stages") or []}
    fields = g.get(f"/locations/{location}/customFields", {"model": "opportunity"}).get("customFields") or []
    field_ids = {f["name"].strip().lower(): f["id"] for f in fields}
    params = {"location_id": location, "pipeline_id": pipe["id"], "status": "all", "limit": 100}
    opps = []
    for _ in range(60):
        r = g.get("/opportunities/search", params)
        opps += r.get("opportunities") or []
        meta = r.get("meta") or {}
        if not meta.get("nextPageUrl") or not meta.get("startAfterId"):
            break
        params = {**params, "startAfter": meta["startAfter"], "startAfterId": meta["startAfterId"]}
    rows = clients_from_ghl(opps, stages, field_ids, fiches, now)
    fetch_clients.churned = {}
    for o in opps:
        if stages.get(o.get("pipelineStageId"), "") == "Churn" or o.get("status") == "lost":
            d = str(o.get("lastStageChangeAt") or o.get("updatedAt") or "")[:10]
            if d:
                fetch_clients.churned[o["id"]] = d
    print(f"GoHighLevel : {len(opps)} opportunités lues, {len(rows)} clients actifs, {g.calls} appels", flush=True)
    return rows


# =========================================================================== chiffrement
def encrypt_pack(pack, passphrase):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    raw = json.dumps(pack, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    gz = gzip.compress(raw, 9)
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=250000).derive(passphrase.encode("utf-8"))
    return base64.b64encode(salt + iv + AESGCM(key).encrypt(iv, gz, None)).decode("ascii")


def decrypt_pack(blob, passphrase):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    raw = base64.b64decode(blob.strip()); salt, iv, ct = raw[:16], raw[16:28], raw[28:]
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=250000).derive(passphrase.encode("utf-8"))
    return json.loads(gzip.decompress(AESGCM(key).decrypt(iv, ct, None)))


def load_previous(pw, today):
    """Fiches de la veille (pour le mode léger). Le lundi, ou si introuvable : None = actualisation complète."""
    load_previous.clients = []
    if os.environ.get("FORCE_FULL") or today.weekday() == 0:
        print("Mode COMPLET (lundi ou demandé).", flush=True); return None
    try:
        if os.environ.get("PREV_FILE"):
            blob = open(os.environ["PREV_FILE"], encoding="ascii").read()
        elif os.environ.get("PREV_URL"):
            import requests
            r = requests.get(os.environ["PREV_URL"], timeout=60)
            if r.status_code != 200:
                print(f"Données de la veille introuvables ({r.status_code}) -> mode COMPLET.", flush=True); return None
            blob = r.text
        else:
            print("Pas d'adresse des données de la veille -> mode COMPLET.", flush=True); return None
        old = decrypt_pack(blob, pw)
        prev = {f["id"]: f for f in old["fiches"]}
        load_previous.clients = old.get("clients") or []
        print(f"Mode LÉGER : {len(prev)} fiches reprises de la veille pour score, réseaux, photos et publications.", flush=True)
        return prev
    except Exception as e:
        print("Données de la veille illisibles -> mode COMPLET :", str(e)[:100], flush=True); return None


# =========================================================================== comptes et rôles
SETUP_RE = re.compile(r"SET UP GMB|Set up GMB|Appel Onboarding|Relance after|CLOSING", re.I)
SENSITIVE = ("sat", "no_rep", "ct_j", "dm_j", "maj_j", "wa_c", "wa_d", "wa_t")           # infos de churn : réservées à l'admin et au CSM
ROLES = {
    "admin": {"tabs": ["today", "clients", "setup", "renew", "overview", "sectors", "compare", "week", "rank", "reports", "table", "accounts"],
              "reports": ["client", "week", "dir"], "first": "today"},
    "csm":   {"tabs": ["today", "clients", "reports", "table"], "reports": ["client", "week"], "first": "today"},
    "gmb":   {"tabs": ["setup", "overview", "sectors", "compare", "week", "rank", "reports", "table"], "reports": ["client"], "first": "setup"},
    "sales": {"tabs": ["renew", "overview", "sectors", "compare", "rank", "reports", "table"], "reports": ["client"], "first": "renew"},
}


def user_key(uid):
    return hashlib.sha256(uid.strip().lower().encode("utf-8")).hexdigest()[:24]


def clients_for(user, role, clients):
    """Les clients que ce compte a le droit de recevoir (le filtrage se fait ici, avant le chiffrement)."""
    def strip(c):
        return {k: v for k, v in c.items() if k not in SENSITIVE}
    if role == "admin":
        return clients
    if role == "gmb":
        return [strip(c) for c in clients if SETUP_RE.search(c.get("etape") or "")]
    if role == "sales":
        return [strip(c) for c in clients if not SETUP_RE.search(c.get("etape") or "")]
    ids = set(user.get("ghl_user") or [])                         # csm : seulement ses clients si des identifiants sont donnés
    return [c for c in clients if not SETUP_RE.search(c.get("etape") or "") and (not ids or c.get("resp") in ids)]


def write_user_files(pack, raw_users, out_dir):
    try:
        users = json.loads(raw_users)
        assert isinstance(users, list) and users
    except Exception:
        sys.exit("Le secret EM_USERS doit être une liste JSON : [{\"id\":\"prenom\",\"name\":\"Prénom\",\"role\":\"csm\",\"password\":\"...\"}]")
    d = os.path.join(out_dir, "u")
    os.makedirs(d, exist_ok=True)
    seen, counts = set(), {}
    for u in users:
        uid = str(u.get("id") or "").strip().lower()
        role = str(u.get("role") or "").strip().lower()
        pw = str(u.get("password") or "")
        if not re.fullmatch(r"[a-z0-9._-]{2,40}", uid):
            sys.exit(f"EM_USERS : identifiant invalide « {uid} » (lettres minuscules, chiffres, . _ - ; 2 à 40 caractères).")
        if uid in seen:
            sys.exit(f"EM_USERS : identifiant en double « {uid} ».")
        if role not in ROLES:
            sys.exit(f"EM_USERS : rôle inconnu « {role} » pour {uid} (admin, csm, gmb, sales).")
        if len(pw) < 10:
            sys.exit(f"EM_USERS : mot de passe trop court pour {uid} (10 caractères minimum).")
        seen.add(uid)
        profile = {"id": uid, "name": str(u.get("name") or uid), "role": role, **ROLES[role]}
        sub = {**pack, "clients": clients_for(u, role, pack.get("clients") or []), "profile": profile}
        if role != "admin":
            sub.pop("calib", None)
        open(os.path.join(d, user_key(uid) + ".txt"), "w", encoding="ascii").write(encrypt_pack(sub, pw))
        counts[uid] = (role, len(sub["clients"]))
    open(os.path.join(d, "enabled.txt"), "w", encoding="ascii").write("1")
    print("Comptes : " + ", ".join(f"{k} ({v[0]}, {v[1]} clients)" for k, v in counts.items()), flush=True)


# =========================================================================== historique des feux
# Chaque matin on garde, pour chaque client, les SIGNAUX BRUTS (pas la couleur) : on pourra ainsi recalculer les feux avec
# d'autres réglages et mesurer, après quelques semaines, quels signaux annoncent vraiment un départ.
# Le fichier est chiffré (mot de passe du dashboard) et versionné dans le dépôt : history/feux.enc
HIST_PATH = os.environ.get("HIST_PATH", "history/feux.enc")
DEF_W = {"war": 4, "wam": 3, "wad": 2, "sil": 1, "flag": 4, "sat": 2, "win": 1, "nrep": 2, "ech1": 2, "ech2": 3, "rdv": 1, "maj60": 1, "vues": 2, "avis": 2, "chute": 2, "thr_j": 2, "thr_r": 4}
SIGNALS = [("flag", "« Risque de churn » noté"), ("sat", "Satisfaction basse (30 ou 50 %)"), ("win", "Mensuel, mois 2 à 4"), ("nrep", "Sans réponse (étiquette)"),
           ("ech1", "Échéance dans 1 mois"), ("ech2", "Échéance dans 7 jours"), ("rdv", "RDV bilan à planifier"), ("maj60", "Aucune mise à jour depuis 60 jours"),
           ("vues", "Vues en baisse de 20 % et plus (3 mois)"), ("avis", "Avis 1-2 étoiles récent"), ("chute", "Vues en chute sur 30 jours"),
           ("wa_resil", "Demande d'arrêt sur WhatsApp"), ("wa_remb", "Remboursement ou prélèvement contesté (WhatsApp)"),
           ("wa_decu", "Déception exprimée sur WhatsApp"), ("silence", "Client silencieux depuis 30 jours")]


def _ev3(f):
    n = min(5, int(f.get("mois_dispo") or 0))
    if n < 3:
        return None

    def tot(t0, t1):
        s_ = 0
        for t in range(t0, t1 + 1):
            v = f.get(f"v_m{t}") if t >= 1 else f.get(f"v_p{1 - t}")
            if v is None:
                return None
            s_ += v
        return s_
    last, prev = tot(n - 2, n), tot(n - 5, n - 3)
    return (last / prev - 1) if last is not None and prev else None


def client_features(pack, clients, today, last_full):
    base = dt.date.fromisoformat(pack["base"])
    fiches = {f["id"]: f for f in pack["fiches"]}
    lo, cut = {}, (today - dt.timedelta(days=30) - base).days
    av = pack.get("avis") or {}
    for n, d, r in zip(av.get("n", []), av.get("d", []), av.get("r", [])):
        if d >= cut and r is not None and r <= 2:
            lo[n] = lo.get(n, 0) + 1
    o2 = (last_full - base).days
    o1, p2, p1 = o2 - 27, o2 - 28, o2 - 55
    cur, prv = {}, {}
    jr = pack.get("jours") or {}
    for n, d, v in zip(jr.get("n", []), jr.get("d", []), jr.get("v", [])):
        t = cur if o1 <= d <= o2 else (prv if p1 <= d <= p2 else None)
        if t is None:
            continue
        a = t.setdefault(n, [0, 0])
        a[0] += v or 0
        a[1] += 1
    rows = [(n, prv[n][0], cur[n][0]) for n in cur if n in prv and cur[n][1] >= 20 and prv[n][1] >= 20 and prv[n][0] >= 60]
    sp, sc = sum(r[1] for r in rows), sum(r[2] for r in rows)
    g = (sc / sp) if sp else 1
    ch = {n: (c / p) / g - 1 for n, p, c in rows}
    out = []
    for c in clients:
        f = fiches.get(c.get("lnid"))
        idx = f.get("idx") if f else None
        sat = c.get("sat") or ""
        ev, chv = (_ev3(f) if f else None), (ch.get(idx) if f else None)
        out.append({
            "i": c["id"], "e": c.get("etape") or "", "a": c.get("age_m"), "r": 1 if c.get("rrm") == "Oui" else 0,
            "s": 2 if re.search(r"risque", sat, re.I) else (1 if re.search(r"(^|[^0-9])(30|50)\s?%", sat) else 0),
            "n": 1 if c.get("no_rep") else 0, "j": c.get("maj_j"), "ct": c.get("ct_j"), "dm": c.get("dm_j"),
            "ev": round(ev, 3) if ev is not None else None, "lo": (lo.get(idx, 0) if f else None),
            "ch": round(chv, 3) if chv is not None else None, "f": 1 if f else 0, "v": c.get("val"),
            "wa": c.get("wa_c"), "wd": c.get("wa_d"),
        })
    return out


def feu_of(ft, W=DEF_W):
    e, age, sigs, pts, flag = ft.get("e") or "", ft.get("a") or 0, [], 0, False

    def add(k, w):
        nonlocal pts
        if w > 0:
            sigs.append(k)
            pts += w
    if ft.get("s") == 2:
        add("flag", W["flag"]); flag = True
    elif ft.get("s") == 1:
        add("sat", W["sat"])
    if ft.get("r") and 2 <= age <= 4:
        add("win", W["win"])
    if ft.get("n"):
        add("nrep", W["nrep"])
    if re.search(r"Reste 7 jours", e, re.I):
        add("ech2", W["ech2"])
    elif re.search(r"Reste 1 mois", e, re.I):
        add("ech1", W["ech1"])
    if re.search(r"RDV \d ?mois à traiter", e, re.I):
        add("rdv", W["rdv"])
    if (ft.get("j") or 0) >= 60:
        add("maj60", W["maj60"])
    if ft.get("ev") is not None and ft["ev"] <= -0.2:
        add("vues", W["vues"])
    if ft.get("lo"):
        add("avis", W["avis"])
    if ft.get("ch") is not None and ft["ch"] <= -0.35:
        add("chute", W["chute"])
    wa, wd = ft.get("wa"), ft.get("wd")
    if wa and wd is not None and wd <= 30:
        if wa == "RESIL":
            add("wa_resil", W["war"])
            flag = flag or W["war"] > 0
        elif wa == "REMB":
            add("wa_remb", W["wam"])
        elif wa == "DECU":
            add("wa_decu", W["wad"])
    if ft.get("dm") is not None and ft["dm"] >= 30:
        add("silence", W["sil"])
    return ("rouge" if flag or pts >= W["thr_r"] else ("jaune" if pts >= W["thr_j"] else "vert")), sigs


def load_history(pw):
    try:
        return decrypt_pack(open(HIST_PATH, encoding="ascii").read(), pw)
    except FileNotFoundError:
        return {"v": 1, "days": {}, "churn": {}}
    except Exception as e:
        print("ATTENTION : historique des feux illisible, il n'est PAS modifié :", str(e)[:100], flush=True)
        return None


def update_history(pack, churned, pw, today, last_full):
    h = load_history(pw)
    if h is None:
        return None
    h["days"][today.isoformat()] = client_features(pack, pack["clients"], today, last_full)
    for k, v in (churned or {}).items():
        h["churn"].setdefault(k, v)
    d = os.path.dirname(HIST_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    open(HIST_PATH, "w", encoding="ascii").write(encrypt_pack(h, pw))
    return h


def calibration(h, today):
    days = sorted(h["days"])
    out = {"days": len(days), "first": days[0] if days else None, "mature": 0, "departures": 0, "wait": 30, "colors": {}, "signals": []}
    if not days:
        return out
    first = dt.date.fromisoformat(days[0])
    churn = {k: dt.date.fromisoformat(v) for k, v in h["churn"].items()}
    out["departures"] = sum(1 for d in churn.values() if d > first)
    out["wait"] = max(0, 30 - (today - first).days)
    col = {c: [0, 0] for c in ("rouge", "jaune", "vert")}
    sig = {k: [0, 0, 0, 0] for k, _ in SIGNALS}
    for ds in days:
        D = dt.date.fromisoformat(ds)
        if (today - D).days < 30:
            continue
        out["mature"] += 1
        for ft in h["days"][ds]:
            if SETUP_RE.search(ft.get("e") or ""):
                continue
            color, sigs = feu_of(ft)
            c = churn.get(ft["i"])
            dep = 1 if (c and D < c <= D + dt.timedelta(days=30)) else 0
            col[color][0] += 1
            col[color][1] += dep
            for k, _ in SIGNALS:
                a = sig[k]
                if k in sigs:
                    a[0] += 1; a[1] += dep
                else:
                    a[2] += 1; a[3] += dep
    out["colors"] = {c: {"n": v[0], "dep": v[1]} for c, v in col.items()}
    out["signals"] = [{"k": k, "label": lab, "n": sig[k][0], "dep": sig[k][1], "n0": sig[k][2], "dep0": sig[k][3]} for k, lab in SIGNALS if sig[k][0] > 0]
    return out


def main():
    key, pw = (os.environ.get("LN_KEY") or "").strip(), (os.environ.get("EM_PASSPHRASE") or "").strip()
    if not key or not pw:
        sys.exit("Il manque le secret LN_KEY ou EM_PASSPHRASE.")
    if len(pw) < 16:
        print("ATTENTION : mot de passe court. Le fichier de données étant public (chiffré), préférez 16 caractères ou plus.", flush=True)
    today = dt.date.fromisoformat(os.environ["TODAY"]) if os.environ.get("TODAY") else dt.date.today()
    last_full = today - dt.timedelta(days=int(os.environ.get("LAG_DAYS", "8")))
    out_dir = os.environ.get("OUT_DIR", "site")
    api = Api(key)
    prev = load_previous(pw, today)
    listings, raw, reviews = fetch_all(api, today, limit=int(os.environ["LIMIT"]) if os.environ.get("LIMIT") else None,
                                       review_days=int(os.environ.get("REVIEW_DAYS", "170")), prev=prev)
    pack, unmapped = build_pack(listings, raw, reviews, today, last_full)
    if unmapped:
        print("ATTENTION : catégories sans secteur (à ajouter dans sectors.py) :", unmapped, flush=True)
    fresh_clients = False
    token, loc = (os.environ.get("GHL_TOKEN") or "").strip(), (os.environ.get("GHL_LOCATION_ID") or "").strip()
    if token and loc:
        try:
            pack["clients"] = fetch_clients(token, loc, pack["fiches"], dt.datetime.now(dt.timezone.utc))
            fresh_clients = True
        except SystemExit:
            raise
        except Exception as e:
            print("ATTENTION : lecture GoHighLevel impossible, clients de la veille conservés :", str(e)[:150], flush=True)
            pack["clients"] = getattr(load_previous, "clients", [])
    else:
        print("GHL_TOKEN ou GHL_LOCATION_ID absent : l'onglet « À traiter » restera vide.", flush=True)
        pack["clients"] = getattr(load_previous, "clients", [])
    try:
        h = update_history(pack, getattr(fetch_clients, "churned", None), pw, today, last_full) if fresh_clients else load_history(pw)
        if h is not None:
            pack["calib"] = calibration(h, today)
            print(f"Historique des feux : {pack['calib']['days']} jour(s), {pack['calib']['departures']} départ(s) observé(s), {pack['calib']['mature']} relevé(s) mûr(s).", flush=True)
    except Exception as e:
        print("ATTENTION : historique des feux ignoré :", str(e)[:150], flush=True)
    blob = encrypt_pack(pack, pw)
    os.makedirs(out_dir, exist_ok=True)
    open(os.path.join(out_dir, "data.txt"), "w", encoding="ascii").write(blob)
    raw_users = (os.environ.get("EM_USERS") or "").strip()
    if raw_users:
        write_user_files(pack, raw_users, out_dir)
    open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8").write(
        "<!doctype html><meta charset='utf-8'><title>Données</title><p>Données chiffrées du dashboard Elite Média.</p>")
    print(f"OK : {len(pack['fiches'])} fiches, {len(pack['jours']['n'])} lignes par jour, {len(pack['avis']['n'])} avis | "
          f"{len(pack['clients'])} clients | data.txt = {len(blob) // 1024} Ko | données du {today.isoformat()} (stats complètes jusqu'au {last_full.isoformat()})", flush=True)


if __name__ == "__main__":
    main()
