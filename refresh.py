# VERSION 5 novembre 2026 (finale, page Baisse Google) : liens client/fiche (téléphone, entreprise, portail), plusieurs fiches par client, faits positifs élargis
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
QUOTA_GUARD = os.environ.get("QUOTA_GUARD") != "0"      # arrêt anticipé s'il reste moins de 200 appels dans la journée (QUOTA_GUARD=0 pour le désactiver)
MAX_CALLS = int(os.environ.get("MAX_CALLS") or "9000")      # plafond de sécurité par passage (un passage léger en utilise environ 1 700)


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
        """GET JSON. soft=True : renvoie None (au lieu d'une erreur) pour 403/404, et si le serveur reste en panne après toutes les tentatives."""
        last = None
        for i in range(tries):
            self._throttle()
            try:
                r = self.s.get(API + path, params=params, timeout=90); self.calls += 1
                rem = r.headers.get("x-ratelimit-remaining")
                if rem is not None:
                    try:
                        self.remaining = int(rem)
                    except ValueError:
                        pass
                    # Garde-fou : on s'arrête avant d'épuiser le quota quotidien partagé avec vos autres outils.
                    if QUOTA_GUARD and self.remaining is not None and self.remaining < 200:
                        raise SystemExit("Quota journalier de l'API presque épuisé : arrêt pour ne pas bloquer vos autres outils.")
                if self.calls > MAX_CALLS:
                    raise SystemExit(f"Plus de {MAX_CALLS} appels dans ce passage : arrêt de sécurité.")
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
        if soft:                                   # donnée secondaire (photos, score, réseaux) : on continue sans elle
            print(f"  ATTENTION : {path} injoignable ({last}), ignoré", flush=True)
            return None
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
        failed = []
        for f in as_completed(futs):
            try:
                raw[futs[f]] = f.result()
            except RuntimeError as e:                      # panne passagère de Local Nord sur une fiche : on réessaie plus bas
                failed.append((futs[f], str(e)[:110]))
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(listings)} fiches | {api.calls} appels | quota restant {api.remaining} | {time.time() - t0:.0f}s", flush=True)
    for lid, err in list(failed):                          # seconde chance, une fiche à la fois, après une pause
        time.sleep(5)
        x = next(l for l in listings if l["id"] == lid)
        try:
            raw[lid] = fetch_one(api, x, today, (prev or {}).get(lid))
            failed.remove((lid, err))
        except RuntimeError:
            pass
    if failed:
        if len(failed) > max(3, len(listings) // 30):
            raise RuntimeError(f"{len(failed)} fiches illisibles : panne de Local Nord ? Dernière erreur : {failed[-1][1]}")
        print(f"ATTENTION : {len(failed)} fiche(s) ignorée(s) ce matin (Local Nord ne répond pas), reprises demain : " + ", ".join(l for l, _ in failed), flush=True)
        skip = {l for l, _ in failed}
        listings = [l for l in listings if l["id"] not in skip]
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


def _is_swiss(address, phone):
    """Fiche suisse (hors Union européenne) : numéro +41 ou format 0xx xxx xx xx, avec code postal à 4 chiffres quand l’adresse est connue."""
    ph = (phone or "").strip()
    p2 = re.sub(r"[\s.\-()]", "", ph)
    if p2.startswith(("+41", "0041")):
        return True
    if not re.match(r"^0\d{2} \d{3} \d{2} \d{2}$", ph):
        return False
    a = address or ""
    return (not a) or bool(re.search(r"(?<!\d)\d{4}(?!\d)", a) and not re.search(r"(?<!\d)\d{5}(?!\d)", a))


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
        LN_PHONES[x["id"]] = _tel(x.get("phone"))
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
        if _is_swiss(x.get("address"), x.get("phone")):
            rec["ch"] = 1
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
            if dte < start_series or dte > last_full.isoformat():
                continue                                           # Google publie ses statistiques avec plusieurs jours de retard : les derniers jours, vides ou incomplets, ne comptent pas
            v, c, a, i = impressions(d), d["website_clicks"], d["call_clicks"], d["direction_requests"]
            if v or c or a or i:
                series_rows.append((rec["idx"], dte, v, c, a, i))
    idx_of = {f["id"]: f["idx"] for f in fiches}
    avis_rows = [(idx_of[x["listing_id"]], x["d"], x["r"]) for x in reviews if x["listing_id"] in idx_of and x["d"]]
    base = dt.date.fromisoformat(start_series)
    off = lambda s: (dt.date.fromisoformat(s) - base).days
    pack = {
        "base": base.isoformat(), "built": today.isoformat(), "last_full": last_full.isoformat(), "stats_fin": last_full.isoformat(), "fiches": fiches,
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

    def get(self, path, params=None, tries=5, soft=False):
        last = None
        for i in range(tries):
            with self.lock:
                now = time.monotonic(); slot = max(self.next, now); self.next = slot + 0.25
            if slot - now > 0:
                time.sleep(slot - now)
            try:
                r = self.s.get(GHL_API + path, params=params, timeout=90); self.calls += 1
                if soft and r.status_code in (401, 403, 404):
                    return None
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


LN_PHONES = {}                                                     # identifiant de fiche -> téléphone normalisé (jamais publié)
MANUAL_LINKS = {}                                                  # liens validés à la main (fichier liens.json) : identifiant d’opportunité -> identifiant de fiche
MANUAL_EXTRA = {}                                                  # fiches supplémentaires d’un client qui en a plusieurs : identifiant d’opportunité -> liste


def _tel(s):
    """Neuf derniers chiffres d’un numéro de téléphone (06 12 34 56 78, +33 6 12 34 56 78, 0033612345678 : même résultat)."""
    d = re.sub(r"\D", "", str(s or ""))
    return d[-9:] if len(d) >= 9 else ""


def link_fiche(o, field_value, known, by_name):
    """Relie un client à sa fiche Local Nord. Renvoie (identifiant, méthode) ; méthode : champ, telephone, entreprise, nom.
    Règles prudentes : une seule fiche possible à chaque étape, sinon on ne relie pas (jamais de chiffres d’un autre client)."""
    ct = o.get("contact") or {}
    lnid = str(field_value or "").strip()
    if lnid in known:
        return lnid, "champ"
    fid = MANUAL_LINKS.get(str(o.get("id") or ""))
    if fid in known:
        return fid, "manuel"
    ph = _tel(ct.get("phone"))
    if ph:
        c = [fid for fid, tel in LN_PHONES.items() if tel == ph and fid in known]
        if len(c) == 1:
            return c[0], "telephone"
    t = _toks(ct.get("companyName"))
    if len(t) >= 2:
        c = [fid for fid, ft in by_name if all(x in ft for x in t)]
        if len(c) == 1:
            return c[0], "entreprise"
    t = _toks(ct.get("name") or o.get("name"))
    if len(t) >= 2:
        c = [fid for fid, ft in by_name if all(x in ft for x in t)]
        if len(c) == 1:
            return c[0], "nom"
    return None, None


def _days(a, b):
    return int((b - a).total_seconds() // 86400)


def points_forts(recap):
    """Extrait « Ce qui va bien » du dernier bilan écrit dans la fiche : 2 lignes chiffrées au maximum, et la période."""
    if not recap:
        return [], None
    txt = str(recap)
    per = re.search(r"P[ÉE]RIODE\s*:\s*([^\n]{4,60})", txt)
    m = re.search(r"CE QUI VA BIEN[^\n]*\n(.*?)(?:\n\s*POINT[S]? DE VIGILANCE|\n\s*COMMENT LE PR[ÉE]SENTER|$)", txt, re.S | re.I)
    out = []
    if m:
        for line in m.group(1).split("\n"):
            line = line.strip()
            if not line.startswith("-"):
                continue
            line = re.sub(r"\s*\(!+\)", "", line.lstrip("- ").strip())
            line = re.split(r"\s+[-–—]\s+", line)[0].strip()
            if not re.search(r"\d", line) or re.search(r"n[ée]gatif|baisse|recul|invisible", line, re.I):
                continue
            if len(line) > 150:
                line = line[:150].rsplit(" ", 1)[0] + "…"
            out.append(line)
            if len(out) == 2:
                break
    return out, (per.group(1).strip() if per else None)


WA_CODES = ("RESIL", "REMB", "DECU", "ATTE", "PAIE")      # arrêt, remboursement / prélèvement contesté, déception, demande en attente de réponse, paiement échoué
# Tags de la phase commerciale, jamais retirés après la signature : ils ne disent rien du client en gestion.
_COMMERCIAL_TAG = re.compile(r"^\s*(no rep du jour|\d+\s*jours?\s*sans rep|r\d+\s*sans rep)\s*$", re.I)
WA_SHORT_DAYS = int(os.environ.get("WA_SHORT_DAYS") or "14")             # « demande en attente » et « paiement échoué » : valables 14 jours (les autres alertes : 30)
STALE_STAGE_DAYS = int(os.environ.get("STALE_STAGE_DAYS") or "150")     # dossier inactif : étape inchangée depuis au moins N jours
STALE_UPDATE_DAYS = int(os.environ.get("STALE_UPDATE_DAYS") or "120")   # ... et aucune mise à jour du dossier depuis au moins N jours
_WA_SYN = [(re.compile(r"resil|résil|arr[êe]t|stop|annul|fin de contrat", re.I), "RESIL"),
           (re.compile(r"rembours|pr[ée]l[èe]vement|avoir|litige|opposition", re.I), "REMB"),
           (re.compile(r"d[ée][cç]u|d[ée]ception|m[ée]content|insatisf|friche", re.I), "DECU"),
           (re.compile(r"attente|attend|sans r[ée]ponse", re.I), "ATTE"),
           (re.compile(r"paiement|impay|[ée]chou|rejet", re.I), "PAIE")]


def parse_wa(raw, exp):
    """Étiquette écrite par la routine WhatsApp. Format voulu : « CODE|AAAA-MM-JJ|libellé », mais on accepte aussi
    « RESIL 2026-10-29 » ou « Demande d'arrêt 2026-10-29 ». Renvoie (code, jours depuis l'alerte, libellé)."""
    if not raw:
        return None, None, None
    raw = str(raw).strip()
    code = None
    m = re.match(r"\s*(RESIL|REMB|DECU|ATTE|PAIE)\b", raw, re.I)
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
        pf_list, pf_per = points_forts(val("Récap écrit GMB - (W/PDF)"))
        # --- corrections « sans réponse », exclusions
        real_tags = [t for t in tags if not _COMMERCIAL_TAG.match(str(t))]
        no_rep = bool(re.search(r"sans rep|no rep", ",".join(real_tags), re.I))
        dm_j = _days(dm, now) if dm else None
        if no_rep and dm_j is not None and dm_j < 14:
            no_rep = False                                         # le client a écrit récemment : il n'est pas « sans réponse »
        if re.search(r"(^|[\s,_-])churn([\s,_-]|$)", tg, re.I) or re.search(r"test tracking|[àa] supprimer", str(o.get("name") or ""), re.I):
            continue                                               # étiqueté churn, ou fiche de test
        etape_j, maj_j = (_days(stg, now) if stg else None), (_days(upd, now) if upd else None)
        is_stale = bool(etape_j is not None and etape_j >= STALE_STAGE_DAYS and maj_j is not None and maj_j >= STALE_UPDATE_DAYS
                        and not SETUP_RE.search(etape) and not (wa_c and wa_d is not None and wa_d <= 30) and not (dm_j is not None and dm_j < 30))
        lnid, lnm = link_fiche(o, val("ID Local Nord"), known, by_name)
        out.append({
            "id": o["id"], "nom": str(o.get("name") or ""), "etape": etape,
            "age_m": round(_days(start, exp) / 30.44, 1) if start else 0,
            "rrm": rrm, "sat": sat, "gmb": val("Situation GMB") or "", "acces": val("Accès GMB") or "",
            "maj_j": _days(upd, now) if upd else None, "etape_j": _days(stg, now) if stg else None,
            "offre": "198€" if "198€" in tg else ("98€" if "98€" in tg else ""),
            "no_rep": no_rep, "lnid": lnid, "lnm": lnm, "lnx": MANUAL_EXTRA.get(str(o.get("id") or "")) or None, "exp": now.date().isoformat(),
            "dm_j": dm_j,
            "ct_j": _days(ct, now) if ct else None,
            "resp": str(o.get("assignedTo") or ""),
            "val": o.get("monetaryValue") or None,
            "wa_c": wa_c, "wa_d": wa_d, "wa_t": wa_t,
            "cid": o.get("contactId") or None, "pf": pf_list, "pfp": pf_per, "_stale": is_stale,
        })
    # --- un même client ne doit apparaître qu'une fois : on garde son dossier le mieux suivi (pas dormant, puis le plus récemment mis à jour)
    best = {}
    big = 10 ** 6
    keyof = lambda r: r.get("cid") or re.sub(r"\s+", " ", r["nom"].strip().lower())
    rank = lambda r: (1 if r.get("_stale") else 0, r.get("maj_j") if r.get("maj_j") is not None else big)
    for r in out:
        cur = best.get(keyof(r))
        if cur is None or rank(r) < rank(cur):
            best[keyof(r)] = r
    keep = {id(r) for r in best.values()}
    clients_from_ghl.dupes = [{"id": r["id"], "nom": r["nom"], "etape": r["etape"], "m": r.get("maj_j"), "garde": best[keyof(r)]["id"], "garde_nom": best[keyof(r)]["nom"], "cid": r.get("cid")}
                              for r in out if id(r) not in keep]
    clients_from_ghl.dups = len(clients_from_ghl.dupes)
    winners = [r for r in out if id(r) in keep]
    # --- dossiers vraiment dormants (le seul dossier du contact) : à nettoyer dans GoHighLevel, pas à contacter
    clients_from_ghl.stale = [{"id": r["id"], "nom": r["nom"], "etape": r["etape"], "j": r.get("etape_j"), "m": r.get("maj_j"), "cid": r.get("cid")} for r in winners if r.get("_stale")]
    rows = [r for r in winners if not r.get("_stale")]
    for r in rows:
        r.pop("_stale", None)
    return rows


NOTE_AUTO = re.compile(r"\s*(r[ée]cap|rapport|bilan|analyse|\[auto)", re.I)       # notes écrites par les routines : elles ne comptent pas comme un contact


def fetch_note_days(g, rows, now):
    """Pour chaque client actif : jours depuis la dernière note HUMAINE de sa fiche contact (champ nt_j), fusionnés dans ct_j.
    Renvoie le nombre de clients lus, ou 0 si l'accès aux notes est refusé (le reste du passage continue)."""
    todo = [r for r in rows if r.get("cid") and not SETUP_RE.search(r.get("etape") or "")]
    if not todo:
        return 0
    first = g.get(f"/contacts/{todo[0]['cid']}/notes", soft=True)
    if first is None:
        print("Notes GoHighLevel : accès refusé ou introuvable, détection par les notes ignorée.", flush=True)
        return 0

    def one(r):
        data = first if r is todo[0] else g.get(f"/contacts/{r['cid']}/notes", soft=True)
        best = None
        for n in (data or {}).get("notes") or []:
            body = str(n.get("body") or "").strip()
            if len(body) < 3 or NOTE_AUTO.match(body):
                continue
            ts = n.get("dateAdded") or n.get("createdAt")
            try:
                d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except ValueError:
                continue
            best = d if best is None or d > best else best
        return r, best
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(one, r) for r in todo]
        for f in as_completed(futs):
            try:
                r, best = f.result()
            except Exception:
                continue
            if best is not None:
                r["nt_j"] = max(0, _days(best, now))
                r["ct_j"] = r["nt_j"] if r.get("ct_j") is None else min(r["ct_j"], r["nt_j"])
    return len(todo)


def fetch_clients(token, location, fiches, now):
    try:
        with open("liens.json", encoding="utf-8") as fh:
            for k, v in json.load(fh).items():
                v = v if isinstance(v, list) else [v]
                MANUAL_LINKS[str(k)] = str(v[0])
                if len(v) > 1:
                    MANUAL_EXTRA[str(k)] = [str(x) for x in v[1:]]
        print(f"Liens validés à la main : {len(MANUAL_LINKS)}.", flush=True)
    except FileNotFoundError:
        pass
    except Exception as e:
        print("liens.json ignoré :", str(e)[:100], flush=True)
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
    from collections import Counter as _Cnt
    _m = _Cnt(r.get("lnm") or "non reliés" for r in rows)
    print("Clients reliés à une fiche Local Nord : " + ", ".join(f"{k} {v}" for k, v in _m.most_common()) + ".", flush=True)
    try:
        nn = fetch_note_days(g, rows, now)
        print(f"Notes GoHighLevel : {nn} clients lus, {sum(1 for r in rows if r.get('nt_j') is not None)} avec une note humaine.", flush=True)
    except Exception as e:
        print("Notes GoHighLevel ignorées :", str(e)[:120], flush=True)
    fetch_clients.churned = {}
    for o in opps:
        if stages.get(o.get("pipelineStageId"), "") == "Churn" or o.get("status") == "lost":
            d = str(o.get("lastStageChangeAt") or o.get("updatedAt") or "")[:10]
            if d:
                fetch_clients.churned[o["id"]] = d
    fetch_clients.stale = getattr(clients_from_ghl, "stale", [])
    fetch_clients.dupes = getattr(clients_from_ghl, "dupes", [])
    print(f"GoHighLevel : {len(opps)} opportunités lues, {len(rows)} clients actifs ({getattr(clients_from_ghl, 'dups', 0)} doublon(s) retiré(s), {len(fetch_clients.stale)} dossier(s) inactif(s) écarté(s)), {g.calls} appels", flush=True)
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
SENSITIVE = ("sat", "no_rep", "ct_j", "dm_j", "maj_j", "nt_j", "wa_c", "wa_d", "wa_t", "pf", "pfp")           # infos de churn : réservées à l'admin et au CSM
ROLES = {
    "admin": {"tabs": ["today", "clients", "gg", "setup", "renew", "overview", "sectors", "compare", "week", "rank", "reports", "table", "accounts"],
              "reports": ["client", "week", "dir"], "first": "today"},
    "csm":   {"tabs": ["today", "clients", "gg", "reports", "table"], "reports": ["client", "week"], "first": "today"},
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
    try:
        tokens = json.loads((os.environ.get("TRAITE_TOKENS") or "").strip() or "{}")    # {"emilie": "tr_…", …} : code « Traité » de chaque personne
        assert isinstance(tokens, dict)
    except Exception:
        print("ATTENTION : TRAITE_TOKENS illisible (attendu : {\"emilie\": \"tr_…\"}) : ignoré, les personnes saisiront leur code.", flush=True)
        tokens = {}
    # l'équipe (prénoms et photos) : chaque personne connectée reçoit celle de tout le monde, pour voir qui a traité quoi
    team = {}
    for u in users:
        tid = str(u.get("id") or "").strip().lower()
        ph = str(u.get("photo") or "")
        if ph and not (ph.startswith("data:image/") and len(ph) <= 40000):
            ph = ""                                             # photo absente ou trop lourde : on affiche les initiales
        team[tid] = {"name": str(u.get("name") or tid), "photo": ph or None}
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
        sub["team"] = team
        if tokens.get(uid):
            sub["traite_token"] = str(tokens[uid])          # chiffré avec le mot de passe de cette personne : elle n'a plus de code à saisir
        if role != "admin":
            sub.pop("calib", None)
            sub.pop("stale", None)
            sub.pop("dupes", None)
        open(os.path.join(d, user_key(uid) + ".txt"), "w", encoding="ascii").write(encrypt_pack(sub, pw))
        counts[uid] = (role, len(sub["clients"]))
    open(os.path.join(d, "enabled.txt"), "w", encoding="ascii").write("1")
    print("Comptes : " + ", ".join(f"{k} ({v[0]}, {v[1]} clients)" for k, v in counts.items()), flush=True)


# =========================================================================== historique des feux
# Chaque matin on garde, pour chaque client, les SIGNAUX BRUTS (pas la couleur) : on pourra ainsi recalculer les feux avec
# d'autres réglages et mesurer, après quelques semaines, quels signaux annoncent vraiment un départ.
# Le fichier est chiffré (mot de passe du dashboard) et versionné dans le dépôt : history/feux.enc
HIST_PATH = os.environ.get("HIST_PATH", "history/feux.enc")
DEF_W = {"war": 4, "wam": 3, "wad": 2, "att": 3, "pai": 3, "sil": 1, "flag": 4, "sat": 2, "win": 1, "nrep": 2, "ech1": 2, "ech2": 3, "rdv": 1, "maj60": 1, "vues": 2, "avis": 2, "chute": 2, "thr_j": 2, "thr_r": 4}
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
    # baisse GÉNÉRALE de Google : si toute la base baisse (ex. recherche Google en chute depuis fin septembre), on mesure chaque client
    # par rapport à la médiane du portefeuille, pour ne pas faire passer en orange ou rouge des clients qui suivent le marché
    import statistics as _st
    evs = [e for e in (_ev3(f) for f in fiches.values()) if e is not None]
    ev_med = _st.median(evs) if len(evs) >= 30 else 0.0
    if ev_med > -0.05:
        ev_med = 0.0                                               # on ne neutralise que les baisses générales, jamais les hausses
    client_features.ev_med = ev_med
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
        if ev is not None and ev_med:
            ev = (1 + ev) / (1 + ev_med) - 1                       # écart au portefeuille, pas baisse brute
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
        elif wa == "ATTE" and wd <= WA_SHORT_DAYS:
            add("wa_atte", W.get("att", 3))
        elif wa == "PAIE" and wd <= WA_SHORT_DAYS:
            add("wa_paie", W.get("pai", 3))
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


# --------------------------------------------------------------------------- faits pour les messages personnalisés
def _nf(n):
    return f"{int(n):,}".replace(",", " ")


def _fiche_metrics(f):
    """Vues, appels sur les 3 derniers mois comparés aux 3 précédents, note et avis."""
    n = min(5, int(f.get("mois_dispo") or 0))
    if n < 3:
        return None

    def tot(k, t0, t1):
        s_ = 0
        for t in range(t0, t1 + 1):
            v = f.get(f"{k}_m{t}") if t >= 1 else f.get(f"{k}_p{1 - t}")
            if v is None:
                return None
            s_ += v
        return s_
    out = {"note": f.get("note"), "avis": f.get("avis")}
    for k in ("v", "a"):
        out[k] = (tot(k, n - 2, n), tot(k, n - 5, n - 3))
    return out


def _positives(c, fiches):
    """Phrase positive vraie : (texte, type) ; type = « stats », « note » ou « bilan »."""
    f = fiches.get(c.get("lnid")) if c.get("lnid") else None
    m = _fiche_metrics(f) if f else None
    if m:
        growth, v, a = [], m["v"], m["a"]
        if v[0] and v[1] and v[1] >= 30 and v[0] / v[1] - 1 >= 0.10:
            growth.append(f"visibilité en hausse de {round((v[0] / v[1] - 1) * 100)} % ({_nf(v[0])} vues contre {_nf(v[1])})")
        if a[0] is not None and a[1] is not None and a[0] > a[1] and a[0] >= 5:
            growth.append(f"{a[0]} appels contre {a[1]}")
        note = None
        if m["note"] and m["avis"] and m["note"] >= 4.5 and m["avis"] >= 10:
            note = ("5 étoiles" if m["note"] >= 4.95 else str(round(m["note"], 1)).replace(".", ",")) + f" sur {m['avis']} avis"
        if growth:
            return ", ".join(growth + ([f"une note de {note}"] if note else [])), "stats"
        if note:
            return note, "note"
    if c.get("pf"):
        bl = []
        for b in c["pf"][:2]:
            b = re.split(r"\s+[-–—]\s+", b.strip())[0].rstrip(".")
            bl.append(b[:1].lower() + b[1:] if b[1:2].islower() else b)
        return " ; ".join(bl), "bilan"
    return "", None


def _merge_fiches(fs):
    """Un client qui a plusieurs fiches : on additionne leurs chiffres (avis, photos, publications, cumul) et on pondère la note."""
    if len(fs) == 1:
        return fs[0]
    out = dict(fs[0])
    for k in ("avis", "photos", "posts", "v_cumul", "a_cumul"):
        out[k] = sum((f.get(k) or 0) for f in fs)
    tot = sum((f.get("avis") or 0) for f in fs)
    if tot:
        out["note"] = round(sum((f.get("note") or 0) * (f.get("avis") or 0) for f in fs) / tot, 2)
    out["mois_dispo"] = min((f.get("mois_dispo") or 0) for f in fs)
    return out


def compute_facts(pack):
    """Ajoute à chaque client : « pos » (faits positifs vrais, un par ligne, 3 au maximum), « pos_k » (leur type), « done »
    (ce qui est déjà en place, sans aucun chiffre, pour les clients de moins de 60 jours) et « kpi » (variation des vues sur
    28 jours, comparée à la médiane de son secteur quand au moins 8 fiches comparables existent).
    Colonnes : v = vues, c = clics site, a = appels, i = itinéraires."""
    import statistics
    F = pack.get("fiches") or []
    byid = {f.get("id"): f for f in F}
    idx = {f.get("id"): i for i, f in enumerate(F)}
    J = pack.get("jours") or {}
    agg, med, rev = {}, {}, {}
    if J.get("d"):
        last = max(J["d"])
        zc, zi = J.get("c") or [0] * len(J["d"]), J.get("i") or [0] * len(J["d"])
        for n, d, v, a_, c_, i_ in zip(J["n"], J["d"], J["v"], J["a"], zc, zi):
            if last - 27 <= d <= last:
                t = agg.setdefault(n, [0] * 8); t[0] += v; t[2] += a_; t[4] += c_ or 0; t[6] += i_ or 0
            elif last - 55 <= d < last - 27:
                t = agg.setdefault(n, [0] * 8); t[1] += v; t[3] += a_; t[5] += c_ or 0; t[7] += i_ or 0
        sect = {}
        for i, f in enumerate(F):
            t = agg.get(i)
            if t and t[1] >= 100:
                sect.setdefault(f.get("secteur"), []).append((t[0] - t[1]) / t[1] * 100)
        med = {k: (statistics.median(v), len(v)) for k, v in sect.items() if k and len(v) >= 8}
        A = pack.get("avis") or {}
        for n, d, r_ in zip(A.get("n") or [], A.get("d") or [], A.get("r") or []):
            if last - 59 <= d <= last and r_:
                rev.setdefault(n, []).append(r_)
    pc = lambda x, y: round((x / y - 1) * 100)
    for c in pack.get("clients") or []:
        i = idx.get(c.get("lnid"))
        ids = [x for x in [i] + [idx.get(y) for y in (c.get("lnx") or [])] if x is not None]
        f = _merge_fiches([F[x] for x in ids]) if ids else None
        ts = [agg[x] for x in ids if x in agg]
        t = [sum(col) for col in zip(*ts)] if ts else None
        c["nfi"] = len(ids) if len(ids) > 1 else None
        items, stats = [], False
        if t:
            if t[2] >= 5 and t[3] >= 1 and t[2] > t[3]:
                items.append(f"{t[2]} appels ces 4 dernières semaines, contre {t[3]} le mois précédent"); stats = True
            if t[1] >= 100 and t[0] >= t[1] * 1.10:
                items.append(f"{pc(t[0], t[1])} % de visibilité en plus ces 4 dernières semaines"); stats = True
            if t[7] >= 5 and t[6] >= 10 and t[6] >= t[7] * 1.15:
                items.append(f"{pc(t[6], t[7])} % de demandes d’itinéraire en plus"); stats = True
            if t[5] >= 5 and t[4] >= 10 and t[4] >= t[5] * 1.15:
                items.append(f"{pc(t[4], t[5])} % de clics vers votre site en plus"); stats = True
        rv = [r for x in ids for r in rev.get(x, [])] or None
        if rv and len(rv) >= 2 and sum(rv) / len(rv) >= 4.3:
            items.append(f"{len(rv)} nouveaux avis clients ces 2 derniers mois, pour une note moyenne de {str(round(sum(rv) / len(rv), 1)).replace('.', ',')}")
        if f and f.get("note") and f.get("avis") and f["note"] >= 4.0 and f["avis"] >= 5:
            nt = "5 sur 5" if f["note"] >= 4.95 else str(round(f["note"], 1)).replace(".", ",") + " sur 5"
            items.append(f"une note de {nt} avec {f['avis']} avis")
        if f and f.get("v_cumul") and f["v_cumul"] >= 300 and (f.get("mois_dispo") or 0) >= 2:
            items.append(f"{_nf(f['v_cumul'])} affichages" + (f" et {f['a_cumul']} appels" if (f.get("a_cumul") or 0) >= 3 else "") + " depuis le début de notre collaboration")
        if f and ((f.get("posts") or 0) >= 3 or (f.get("photos") or 0) >= 10):
            parts = []
            if (f.get("posts") or 0) >= 3:
                parts.append(f"{f['posts']} publications")
            if (f.get("photos") or 0) >= 10:
                parts.append(f"{f['photos']} photos")
            items.append(" et ".join(parts) + " mises en ligne sur votre fiche")
        if items:
            c["pos"] = "\n".join(items[:3])
            c["pos_k"] = "stats" if stats else ("note" if any(("note" in x or "avis" in x) for x in items[:3]) else "work")
        else:
            pos, kind = _positives(c, byid)
            if pos:
                c["pos"], c["pos_k"] = pos, kind
        done = []
        if f:
            if (f.get("photos") or 0) >= 5:
                done.append("des photos ont déjà été ajoutées à votre fiche")
            if (f.get("posts") or 0) >= 1:
                done.append("vos premières publications sont en ligne")
            if (c.get("gmb") or "") == "GMB en ligne":
                done.append("votre fiche est en ligne et suivie chaque semaine")
        if done:
            c["done"] = "\n".join(done[:3])
        if t and t[1] >= 100:
            m = med.get(f.get("secteur")) if f else None
            c["kpi"] = {"chg": round((t[0] - t[1]) / t[1] * 100), "sm": round(m[0]) if m else None, "sn": m[1] if m else 0,
                        "ap": t[2], "app": t[3]}


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
        compute_facts(pack)
    except Exception as e:
        print("Messages personnalisés : faits ignorés (" + str(e)[:100] + ")", flush=True)
    pack["stale"] = getattr(fetch_clients, "stale", []) if fresh_clients else []
    pack["dupes"] = getattr(fetch_clients, "dupes", []) if fresh_clients else []
    pack["ghl"] = {"app": (os.environ.get("GHL_APP_URL") or "https://app.gohighlevel.com").rstrip("/"), "loc": loc} if loc else None
    try:
        fts = client_features(pack, [c for c in pack["clients"] if not SETUP_RE.search(c.get("etape") or "")], today, last_full)
        pack["ev_med"] = round(getattr(client_features, "ev_med", 0.0), 3)
        if pack["ev_med"]:
            print(f"Baisse générale des vues sur 3 mois : {round(pack['ev_med'] * 100)} % (médiane du portefeuille) : les signaux « vues en baisse » sont mesurés par rapport à elle.", flush=True)
        nb = {"rouge": 0, "jaune": 0, "vert": 0, "contactes": 0, "jaune_sans_reponse": 0}
        for ft in fts:
            col, sg = feu_of(ft)
            ct_, wd_ = ft.get("ct"), ft.get("wd")
            if col != "vert" and ct_ is not None and ct_ <= 7 and (wd_ is None or ct_ <= wd_):
                nb["contactes"] += 1
            else:
                nb[col] += 1
                nb["jaune_sans_reponse"] += 1 if (col == "jaune" and "nrep" in sg) else 0
        print(f"Feux du jour : {nb['rouge']} à appeler, {nb['jaune']} à écrire (dont {nb['jaune_sans_reponse']} avec « sans réponse »), {nb['contactes']} contactés, {nb['vert']} sans signal.", flush=True)
    except Exception as e:
        print("Résumé des feux ignoré :", str(e)[:100], flush=True)
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
