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
        prev = {f["id"]: f for f in decrypt_pack(blob, pw)["fiches"]}
        print(f"Mode LÉGER : {len(prev)} fiches reprises de la veille pour score, réseaux, photos et publications.", flush=True)
        return prev
    except Exception as e:
        print("Données de la veille illisibles -> mode COMPLET :", str(e)[:100], flush=True); return None


def main():
    key, pw = os.environ.get("LN_KEY"), os.environ.get("EM_PASSPHRASE")
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
    blob = encrypt_pack(pack, pw)
    os.makedirs(out_dir, exist_ok=True)
    open(os.path.join(out_dir, "data.txt"), "w", encoding="ascii").write(blob)
    open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8").write(
        "<!doctype html><meta charset='utf-8'><title>Données</title><p>Données chiffrées du dashboard Elite Média.</p>")
    print(f"OK : {len(pack['fiches'])} fiches, {len(pack['jours']['n'])} lignes par jour, {len(pack['avis']['n'])} avis | "
          f"data.txt = {len(blob) // 1024} Ko | données du {today.isoformat()} (stats complètes jusqu'au {last_full.isoformat()})", flush=True)


if __name__ == "__main__":
    main()
