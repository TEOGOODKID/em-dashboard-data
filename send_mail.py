# VERSION 5 novembre 2026 (finale) : messages par phase, faits positifs en puces, « Ce qui est déjà en place », « client avec N fiches »
#!/usr/bin/env python3
"""E-mail du matin : clients à appeler et à écrire, avec un message prêt à envoyer pour chacun.

Lit les données chiffrées déjà publiées (aucun appel à Local Nord ni à GoHighLevel), calcule la liste comme le dashboard,
et envoie le mail par Gmail. Réglages par variables d'environnement (secrets GitHub) :
  EM_PASSPHRASE   mot de passe de chiffrement des données (déjà existant)
  DATA_FILE       fichier data.txt local (sinon DATA_URL : adresse publique de data.txt)
  MAIL_MODE       vide ou « off » = rien n'est envoyé ; « test » = envoi marqué [TEST] ; « reel » = envoi normal
  MAIL_TO, MAIL_CC adresses (séparées par des virgules)
  SMTP_USER, SMTP_PASSWORD   adresse Gmail d'envoi et son mot de passe d'application
  DASHBOARD_URL   lien du dashboard (facultatif) ; GHL_APP_URL, GHL_LOCATION_ID pour les liens vers les fiches
  DRY_RUN=1       écrit mail_apercu.html et n'envoie rien
"""
import datetime as dt
import html
import os
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formataddr

import refresh

SIG = "L’équipe Elite Média"
PRIO = ["wa_resil", "wa_atte", "wa_paie", "wa_remb", "wa_decu", "flag", "nrep", "silence", "ech2", "ech1", "rdv", "chute", "vues", "avis", "maj60", "sat", "win"]
WKEY = {"wa_resil": "war", "wa_remb": "wam", "wa_decu": "wad", "wa_atte": "att", "wa_paie": "pai", "silence": "sil"}
FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


def esc(s):
    return html.escape(str(s), quote=False)


def nf(n):
    return f"{int(round(n)):,}".replace(",", " ")


FN = set("""jean pierre michel philippe alain patrick nicolas christophe laurent eric daniel frederic franck frank sebastien julien david thomas stephane olivier pascal marc vincent bruno guillaume yannick cedric alexandre antoine mathieu maxime romain kevin jeremy anthony benjamin adrien quentin florian hugo lucas louis arnaud fabrice fabien gilles gerard bernard didier thierry christian dominique jacques claude henri paul rene serge joel yves lionel ludovic samuel sam gio ali abdel brice alan jessy loic damien gary cyril remi raphael theo nathan enzo tony steven dylan jordan lilian mickael michael jonathan johan jerome gregory gregoire stephen xavier emmanuel manuel marie nathalie isabelle sylvie catherine christine sandrine valerie stephanie veronique sophie aurelie celine laurence julie emilie camille laura sarah karine carine corinne virginie audrey marion pauline charlotte elodie melanie delphine sabine claire anne fatma nadine tracy jeanne carla jade lea manon chloe ines emma lisa nicole martine monique francoise brigitte patricia sandra severine magali cecile helene florence agnes josiane mohamed mohammed mehdi ahmed ahmad youssef yassine karim samir nabil hakim hicham rachid mustapha mourad omar othmane said tahar kheiredine bilal amine walid sofiane ismael ibrahim abdelkader abdelhak hedi sami slim farid fouad jamel kamel karima samira nadia leila amel yasmine imane sonia fatima aicha khadija carlo carmelo santo giovanni marco luca antonio jose carlos miguel joao pedro nikolas nikos vasilis dimitri ghanem seth yasar gaetan guy herve jean-marc jean-pierre jean-luc jean-michel""".split())
ACC = {"helene": "Hélène", "melanie": "Mélanie", "amelie": "Amélie", "aurelie": "Aurélie", "cecile": "Cécile", "frederic": "Frédéric", "stephane": "Stéphane", "stephanie": "Stéphanie", "jeremy": "Jérémy", "jerome": "Jérôme", "remi": "Rémi", "rene": "René", "loic": "Loïc", "elodie": "Élodie", "emilie": "Émilie", "ines": "Inès", "chloe": "Chloé", "lea": "Léa", "gaetan": "Gaëtan", "herve": "Hervé", "mickael": "Mickaël", "raphael": "Raphaël", "joel": "Joël", "michael": "Michaël", "theo": "Théo", "sebastien": "Sébastien", "gregoire": "Grégoire", "gerard": "Gérard", "veronique": "Véronique", "valerie": "Valérie", "severine": "Séverine", "francoise": "Françoise", "andre": "André", "jose": "José", "noemie": "Noémie"}
TITRES = {"mr", "m", "mme", "mlle", "madame", "monsieur", "dr", "me", "maitre"}


def first_name(nom):
    """Prénom seulement quand il est reconnu : sinon on écrit « Bonjour, » plutôt que de risquer une erreur."""
    nom = re.split(r"\s+[-–—]\s+", str(nom or ""))[0]
    toks = [t.strip(".,") for t in re.split(r"[\s/]+", nom) if t.strip(".,")]
    toks = [t for t in toks if t.lower() not in TITRES]
    norm = lambda t: re.sub(r"[éèêë]", "e", re.sub(r"[àâä]", "a", re.sub(r"[îï]", "i", re.sub(r"[ôö]", "o", re.sub(r"[ûüù]", "u", t.lower())))))
    for t in toks[:2]:
        if norm(t) in FN:
            if re.search(r"[À-ÿ]", t):
                return t[:1].upper() + t[1:]
            return ACC.get(norm(t), t[:1].upper() + t[1:].lower())
    return ""


# --------------------------------------------------------------------------- données
def load_pack():
    f = os.environ.get("DATA_FILE")
    if f and os.path.exists(f):
        blob = open(f, encoding="ascii").read()
    else:
        import requests
        r = requests.get(os.environ["DATA_URL"], timeout=60)
        r.raise_for_status()
        blob = r.text
    return refresh.decrypt_pack(blob, os.environ["EM_PASSPHRASE"])


def fetch_marks():
    """Marques « Traité » posées dans le dashboard (service sur le VPS). Si le service ne répond pas, aucune marque : le mail part quand même."""
    url, tok = (os.environ.get("TRAITE_URL") or "").rstrip("/"), os.environ.get("TRAITE_TOKEN") or ""
    if not url or not tok:
        return {}
    try:
        import requests
        r = requests.get(url + "/marks", headers={"Authorization": "Bearer " + tok}, timeout=8)
        r.raise_for_status()
        return {str(m["client_id"]): m for m in (r.json().get("marks") or [])}
    except Exception as e:                                                      # noqa: BLE001
        print("Marques « Traité » indisponibles :", str(e)[:100], flush=True)
        return {}


def ia_msg(c):
    """Message écrit par l’IA et encore frais (7 jours) : il remplace le message calculé."""
    if c.get("ia_msg") and c.get("ia_j") is not None and c["ia_j"] <= 7:
        return [p for p in re.split(r"\n\s*\n", str(c["ia_msg"]).strip()) if p.strip()]
    return None


def ia_note(c):
    if c.get("ia_note") and c.get("ia_msg") and c.get("ia_j") is not None and c["ia_j"] <= 7:
        return [l.strip(" -•") for l in str(c["ia_note"]).splitlines() if l.strip()]
    return None


def treated_by(c, ft, marks, built):
    """Marque « Traité » encore valable : posée il y a 7 jours ou moins, et sans nouvelle alerte WhatsApp depuis."""
    m = marks.get(str(c.get("id")))
    if not m:
        return None
    try:
        at = dt.datetime.fromisoformat(str(m["at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    limite = None if c.get("wa_c") in ("RESIL", "REMB") else 7 * 86400          # demande d’arrêt ou remboursement : « Traité » vaut jusqu’à une nouvelle alerte
    if limite is not None and (dt.datetime.now(dt.timezone.utc) - at).total_seconds() > limite:
        return None
    wd = ft.get("wd")
    if c.get("wa_c") and wd is not None:
        try:
            from zoneinfo import ZoneInfo
            mday = at.astimezone(ZoneInfo("Europe/Paris")).date()
        except Exception:                                                       # noqa: BLE001
            mday = at.date()
        if built - dt.timedelta(days=wd) > mday:
            return None
    return m


def fiche_metrics(f):
    """Vues, appels sur les 3 derniers mois comparés aux 3 précédents, note et avis."""
    n = min(5, int(f.get("mois_dispo") or 0))
    if n < 3:
        return None

    def tot(k, t0, t1):
        s = 0
        for t in range(t0, t1 + 1):
            v = f.get(f"{k}_m{t}") if t >= 1 else f.get(f"{k}_p{1 - t}")
            if v is None:
                return None
            s += v
        return s
    out = {"note": f.get("note"), "avis": f.get("avis")}
    for k in ("v", "a"):
        out[k] = (tot(k, n - 2, n), tot(k, n - 5, n - 3))
    return out


def positives(c, fiches):
    """Phrase positive vraie et sourcée. Renvoie (texte, type, source) ; type = « stats », « note » ou « bilan »."""
    f = fiches.get(c.get("lnid")) if c.get("lnid") else None
    m = fiche_metrics(f) if f else None
    if m:
        growth, v, a = [], m["v"], m["a"]
        if v[0] and v[1] and v[1] >= 30 and v[0] / v[1] - 1 >= 0.10:
            growth.append(f"visibilité en hausse de {round((v[0] / v[1] - 1) * 100)} % ({nf(v[0])} vues contre {nf(v[1])})")
        if a[0] is not None and a[1] is not None and a[0] > a[1] and a[0] >= 5:
            growth.append(f"{a[0]} appels contre {a[1]}")
        note = None
        if m["note"] and m["avis"] and m["note"] >= 4.5 and m["avis"] >= 10:
            note = ("5 étoiles" if m["note"] >= 4.95 else str(round(m["note"], 1)).replace(".", ",")) + f" sur {m['avis']} avis"
        if growth:
            return ", ".join(growth + ([f"une note de {note}"] if note else [])), "stats", "Statistiques Google de la fiche reliée"
        if note:
            return note, "note", "Statistiques Google de la fiche reliée"
    if c.get("pf"):
        bl = []
        for b in c["pf"][:2]:
            b = re.split(r"\s+[-–—]\s+", b.strip())[0].rstrip(".")
            bl.append(b[:1].lower() + b[1:] if b[1:2].islower() else b)
        return " ; ".join(bl), "bilan", "Dernier bilan écrit dans la fiche" + (f" ({c['pfp']})" if c.get("pfp") else "")
    return "", None, None


# --------------------------------------------------------------------------- raisons et messages
def reason_text(key, ft):
    wd = ft.get("wd")
    ago = "aujourd’hui" if wd == 0 else f"il y a {wd} j"
    return {
        "wa_resil": f"Demande d’arrêt sur WhatsApp ({ago})",
        "wa_remb": f"Remboursement ou prélèvement contesté sur WhatsApp ({ago})",
        "wa_decu": f"Déception exprimée sur WhatsApp ({ago})",
        "wa_atte": f"Demande du client en attente de réponse sur WhatsApp ({ago})",
        "wa_paie": f"Paiement échoué ({ago})",
        "flag": "« Risque de churn » noté",
        "sat": "Satisfaction basse notée",
        "nrep": "Sans réponse (étiquette)",
        "silence": f"Client silencieux depuis {ft.get('dm')} jours",
        "ech2": "Échéance dans 7 jours",
        "ech1": "Échéance dans 1 mois",
        "rdv": "Bilan à planifier",
        "chute": "Vues en forte baisse sur 30 jours, bien plus que le portefeuille",
        "vues": f"Vues en baisse de {abs(round((ft.get('ev') or 0) * 100))} % sur 3 mois",
        "avis": f"{ft.get('lo')} avis négatif(s) récent(s)",
        "maj60": f"Aucune mise à jour du dossier depuis {ft.get('j')} jours",
        "win": f"Abonnement mensuel au mois {round(ft.get('a') or 0)}",
    }.get(key, key)


CALL_KEYS = ("wa_resil", "wa_remb", "flag")      # cas rouges (vrais risques de churn) : un brief d'appel, pas de message écrit
DROP_KEYS = ("chute", "vues")


def _sgn(n):
    return ("+" if n > 0 else "−" if n < 0 else "") + str(abs(int(n)))


# ----------------------------------------------------------------------------- phases et messages (prompt d'Émilie)
def days_of(c):
    a = c.get("age_m")
    try:
        return None if a is None else int(round(float(a) * 30.44))
    except (TypeError, ValueError):
        return None


def phase_of(c):
    """(clé, jour, libellé) : 0-29 j étude de marché, 30-59 j stratégie, 60-89 j premiers résultats, 90 j et plus bilan."""
    d = days_of(c)
    if d is None:
        return None, None, "ancienneté inconnue"
    for lim, k, lab in ((30, "etude", "étude de marché"), (60, "strategie", "stratégie"), (90, "resultats", "premiers résultats"), (10 ** 6, "bilan", "bilan")):
        if d < lim:
            return k, d, lab


def dans(n):
    if n <= 3:
        return "dans quelques jours"
    return f"d’ici environ {max(5, int(round(n / 5.0)) * 5)} jours"


def leviers(f):
    gestes = ["demander un avis à vos clients satisfaits, en citant la prestation et la ville"]
    if f is None or (f.get("photos") or 0) < 20:
        gestes.append("nous envoyer quelques photos réelles de vos réalisations ou de votre équipe 📸")
    return gestes


MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


def _add_months(d, n):
    import calendar
    y, m = d.year + (d.month - 1 + n) // 12, (d.month - 1 + n) % 12 + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _fr_date(d):
    return ("1er" if d.day == 1 else str(d.day)) + " " + MOIS[d.month - 1]


def etape_suivante(ph, d, today=None):
    """Prochaine étape datée, comme dans le prompt d’Émilie (étude : jalons ; stratégie : bilan à 2 mois ; résultats : bilan à 3 mois ; bilan : prochain bilan). Jamais de compte à rebours."""
    if ph is None or d is None:
        return []
    if ph == "etude":
        return ["Prochaines étapes : un message stratégie à 30 jours, un point à 2 mois et le bilan à 3 mois."]
    return []                                                              # plus de date de bilan dans les messages : c’est l’équipe qui la donne elle-même au client


def message_phase(c, f, pos, kind, hello, sigs=()):
    """Message client chaleureux, vrai, adapté à la phase. Aucun chiffre avant 60 jours ; ensuite, du positif seulement."""
    ph, d, lab = phase_of(c)
    g = leviers(f)
    suite = etape_suivante(ph, d)
    gestes = ("Pour aider votre fiche, deux gestes comptent beaucoup : " + g[0] + ", et " + g[1] + ".") if len(g) > 1 else ("Pour aider votre fiche, un geste compte beaucoup : " + g[0] + ".")
    avis = ["Nous nous occupons aussi de répondre à vos derniers avis."] if "avis" in sigs else []
    deja = [x for x in (c.get("done") or "").split("\n") if x]
    en_place = (["Ce qui est déjà en place :\n" + "\n".join("✅ " + x[:1].upper() + x[1:] for x in deja)] if deja else [])
    prise = "" if deja else ("Votre fiche est déjà prise en main et suivie. " if f else "")
    if ph == "etude":
        return [hello, "Un petit point sur l’avancement de votre fiche Google 😊",
                prise + "Nous sommes dans la phase d’étude de marché : nous analysons votre secteur, les communes où vous voulez être trouvé, vos concurrents et les recherches de vos futurs clients, pour construire la bonne stratégie."] + en_place + avis + [
                gestes] + suite + ["Belle journée !", SIG]
    if ph in (None, "strategie"):
        return [hello, "Un petit point sur votre fiche Google 😊",
                prise + "Nous sommes dans la phase stratégie : nous travaillons la description et les services de votre fiche, les communes que vous voulez cibler, un rythme de publications et le suivi de vos positions."] + en_place + avis + [gestes] + suite + ["Belle journée !", SIG]
    faits = [x for x in (pos or "").split("\n") if x]
    if kind == "bilan" and faits:
        fait = "Je repense à votre dernier bilan : " + " ; ".join(faits) + "."
    elif len(faits) >= 2:
        fait = "Voici ce qui avance bien sur votre fiche :\n" + "\n".join("✅ " + x[:1].upper() + x[1:] for x in faits)
    elif faits:
        fait = "Bonne nouvelle : " + faits[0] + "."
    else:
        fait = "Nous continuons de travailler votre visibilité : publications régulières, photos, suivi de vos avis et de vos mots-clés."
    return [hello, "Un petit point sur votre fiche Google 😊", fait] + avis + [
        "Pour rappel, Google classe les fiches selon l’endroit où se trouve la personne qui cherche : une même recherche donne donc des résultats différents d’un quartier à l’autre.",
        gestes] + suite + ["Belle journée !", SIG]


def pour_toi(c, f, key, why, sigs=(), today=None):
    """Note interne pour la CSM : à rappeler, phase, ce qui est laissé de côté, alertes."""
    ph, d, lab = phase_of(c)
    k = c.get("kpi") or {}
    L = []
    if key in CALL_KEYS:
        L.append("À RAPPELER : " + (why or "client à risque"))
    L.append(f"Phase : {lab}" + (f" (jour {d})" if d is not None else "") + f" · offre {c.get('offre') or 'non renseignée'}")
    if f and f.get("ajout") and d is not None:
        try:
            ajout = dt.date.fromisoformat(str(f["ajout"])[:10]); debut = (today or dt.date.today()) - dt.timedelta(days=d)
            gap = (ajout - debut).days
            L.append(f"Fiche ajoutée dans Local Nord le {ajout.strftime('%d/%m')}" + (f" : {gap} jours après le début, prise en main tardive" if gap > 7 else ""))
        except Exception:
            pass
    perdu = []
    if k.get("chg") is not None and k["chg"] <= -10:
        perdu.append(f"vues {_sgn(k['chg'])} % sur 28 jours" + (f" (secteur {_sgn(k['sm'])} %)" if k.get("sm") is not None else ""))
    if k.get("ap") is not None and k.get("app") is not None and k["ap"] < k["app"]:
        perdu.append(f"appels {k['ap']} contre {k['app']}")
    if "avis" in sigs:
        perdu.append("avis 1-2 étoiles récents")
    if ph in ("etude", "strategie", None):
        L.append("Aucun chiffre communiqué avant 60 jours." + (" Laissé de côté : " + " ; ".join(perdu) + "." if perdu else ""))
    elif perdu:
        L.append("Laissé de côté (baisses, non mentionnées au client) : " + " ; ".join(perdu) + ".")
    if google_on(c, f, sigs):
        L.append("Changement Google en Europe : ce client n’a pas encore reçu l’information, le paragraphe est ajouté au message.")
    al = []
    if not f:
        al.append("pas de fiche Local Nord reliée : aucun chiffre disponible")
    else:
        if (f.get("photos") or 0) < 5:
            al.append(f"peu de photos ({f.get('photos') or 0})")
        if (f.get("posts") or 0) == 0 and (d or 0) >= 30:
            al.append("aucune publication enregistrée")
        if f.get("note") and f["note"] < 4.2 and (f.get("avis") or 0) >= 10:
            al.append(f"note de {str(f['note']).replace('.', ',')} : avis à travailler")
    if c.get("nfi"):
        al.append(f"client avec {c['nfi']} fiches : chiffres cumulés")
    if c.get("lnm") in ("telephone", "entreprise", "nom"):
        al.append("fiche reliée automatiquement (" + {"telephone": "téléphone", "entreprise": "nom d’entreprise", "nom": "nom du contact"}[c["lnm"]] + ") : à vérifier une fois")
    if (c.get("maj_j") or 0) >= 60:
        al.append(f"dossier non mis à jour depuis {c['maj_j']} jours")
    if c.get("gmb") and c["gmb"] != "GMB en ligne":
        al.append(f"fiche : {c['gmb']}")
    for kk, txt in (("wa_paie", "paiement échoué"), ("wa_atte", "demande du client sans réponse")):
        if kk in sigs:
            al.append(txt)
    if al:
        L.append("Alertes : " + " ; ".join(al) + ".")
    L.append("Avant d’envoyer : joindre son lien d’avis. Non disponibles ici : paiement Stripe, positions des mots-clés.")
    return L


G_FIN = "Google modifie l’affichage de ses résultats en Europe pour se conformer à une nouvelle réglementation européenne."
GOOGLE_INFO = True           # paragraphe « changement Google en Europe » : ajouté UNIQUEMENT aux clients dont les vues baissent et qui ne l’ont jamais reçu (fichier google_informes.json)


def google_on(c, f, sigs=()):
    """Vrai si ce client doit recevoir le paragraphe : vues en baisse, jamais informé, fiche reliée hors Suisse."""
    return bool(GOOGLE_INFO and f and not f.get("ch") and not c.get("ginf") and ("vues" in sigs or "chute" in sigs))


def google_wrap(par, c, f, sigs=()):
    """Ajoute aux messages écrits l’information sur la baisse générale des vues Google en Europe. Jamais pour une fiche suisse, ni sans fiche reliée."""
    if not google_on(c, f, sigs) or len(par) < 2 or par[-1] != SIG:
        return par
    k = c.get("kpi") or {}
    chute = k.get("chg") is not None and k["chg"] <= -15
    if phase_of(c)[0] in ("resultats", "bilan"):
        intro = "Une information sur votre fiche : depuis plusieurs semaines, Google compte beaucoup moins de vues de recherche sur les fiches en Europe, suite à un changement d’affichage lié à une nouvelle réglementation européenne. Ce changement touche la grande majorité des entreprises" + (", pas seulement la vôtre." if chute else ".")
    else:
        intro = "Une information utile sur les statistiques Google : depuis plusieurs semaines, Google compte beaucoup moins de vues de recherche sur les fiches en Europe, suite à un changement d’affichage lié à une nouvelle réglementation européenne. Ce changement touche la grande majorité des entreprises" + (", il n’est pas propre à votre fiche." if chute else ".")
    tail = 2 if len(par) >= 3 and par[-2].startswith("Belle journée") else 1
    return [par[0], intro] + par[1:-tail] + ["On suit votre fiche chaque semaine."] + par[-tail:]


def compose(c, key, pos, kind, wd=None, why="", f=None, sigs=()):
    """Message prêt à envoyer (liste de paragraphes terminée par la signature) ou brief d’appel pour les situations rouges."""
    p = first_name(c.get("nom"))
    hello = f"Bonjour {p}," if p else "Bonjour,"
    k = c.get("kpi") or {}
    if key in CALL_KEYS:
        par = ["APPEL À PASSER : pas de message écrit pour ce cas."]
        if why:
            par.append(f"Pourquoi : {why}.")
        par.append({
            "wa_resil": "Le client a parlé d’arrêter ou de mettre en pause : appelez avant de répondre par écrit. Écoutez d’abord la raison, sans insister. S’il demande une confirmation écrite de sa résiliation, prévenez la direction : c’est à traiter à part.",
            "wa_remb": "Vérifiez sa facturation avant d’appeler, puis expliquez simplement ce qui a été prélevé et pourquoi.",
            "wa_decu": "Reconnaissez sa déception, puis partez de ses résultats concrets plutôt que de promesses.",
        }.get(key, "Prenez de ses nouvelles, écoutez ses freins (budget, temps, résultats), puis proposez la suite adaptée."))
        ph = phase_of(c)[0]
        if pos and ph in ("resultats", "bilan"):
            par.append(("Appuyez-vous sur son dernier bilan : " if kind == "bilan" else "À dire en premier : ") + pos + ".")
        if k.get("chg") is not None:
            par.append(f"Chiffres internes (ne pas citer au client) : vues {_sgn(k['chg'])} % sur 28 jours" + (f", secteur {_sgn(k['sm'])} %" if k.get("sm") is not None else "") + ".")
        if k.get("chg") is not None and k.get("sm") is not None and k["chg"] <= k["sm"] - 10:
            par.append("Sa baisse est plus forte que celle du secteur : cherchez la cause (site, horaires, avis, catégorie) avant d’appeler.")
        return par
    if key == "wa_paie":
        return [hello, "Un petit message pour vous prévenir que votre dernier prélèvement n’a pas pu passer. Ça arrive souvent (carte expirée, plafond, changement de banque) et ce n’est rien de grave.",
                "Il suffit de vérifier votre moyen de paiement (carte à jour, plafond disponible) : le prélèvement repassera ensuite normalement, sans autre démarche de votre part.", SIG]
    if key == "wa_atte":
        return [hello, "Merci pour votre message et pour votre patience. Je prends votre demande en charge aujourd’hui et je vous réponds au plus vite.", SIG]
    par = message_phase(c, f, pos, kind, hello, tuple(sigs) + (("avis",) if key == "avis" else ()))
    if key == "rdv":
        par = par[:-2] + ["Nous reviendrons vers vous très prochainement pour fixer la date de votre bilan.", SIG]
    return google_wrap(par, c, f, sigs)


def box_label(par):
    if par and par[0].startswith("APPEL À PASSER"):
        return "À SAVOIR AVANT D’APPELER"
    if par and par[-1] != SIG:
        return "PAS DE MESSAGE CONSEILLÉ"
    return "MESSAGE PRÊT À ENVOYER (WHATSAPP)"


def logo_url():
    """Adresse publique du logo (hébergé avec le dashboard sur GitHub Pages). Vide si elle ne peut pas être déterminée."""
    u = (os.environ.get("LOGO_URL") or "").strip()
    if u:
        return u
    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/logo-mail.png"
    return ""


def build(pack, today, last_full, mode=""):
    clients = [c for c in (pack.get("clients") or []) if not refresh.SETUP_RE.search(c.get("etape") or "")]
    feats = refresh.client_features(pack, clients, today, last_full)
    fiches = {f["id"]: f for f in pack["fiches"]}
    marks = fetch_marks()
    rows, contacted, greens = [], 0, 0
    for c, ft in zip(clients, feats):
        color, sigs = refresh.feu_of(ft)
        if color == "vert":
            greens += 1
            continue
        ct, wd = c.get("ct_j"), ft.get("wd")
        if (color != "rouge" and ct is not None and ct <= 7 and (wd is None or ct <= wd)) or treated_by(c, ft, marks, today):      # un rouge ne sort pas tout seul après une réponse : il reste jusqu’au clic ou 14 jours
            contacted += 1
            continue
        key = next((k for k in PRIO if k in sigs and (color != "rouge" or k in CALL_KEYS)), sigs[0] if sigs else "win")
        pts = sum(refresh.DEF_W.get(WKEY.get(k, k), 0) for k in sigs)
        pos, kind, src = (c["pos"], c.get("pos_k"), "Statistiques Google et fiche reliée") if c.get("pos") else positives(c, fiches)
        rows.append({"c": c, "ft": ft, "color": color, "key": key, "pts": pts, "sigs": sigs, "pos": pos, "kind": kind, "src": src})
    order = lambda r: (PRIO.index(r["key"]) if r["key"] in PRIO else 99, -r["pts"], -(r["ft"].get("a") or 0))
    reds = sorted([r for r in rows if r["color"] == "rouge"], key=order)
    yell = sorted([r for r in rows if r["color"] == "jaune"], key=order)
    jour = f"{FR[today.weekday()]} {today.day} {MOIS[today.month - 1]}"
    test = mode == "test"
    subject = f"{'[TEST] ' if test else ''}Churn : {len(reds)} à appeler, {len(yell)} à écrire ({jour})"
    base = (os.environ.get("GHL_APP_URL") or "https://app.gohighlevel.com").rstrip("/")
    loc = os.environ.get("GHL_LOCATION_ID") or ""
    dash = os.environ.get("DASHBOARD_URL") or ""

    def link(c):
        return f"{base}/v2/location/{loc}/contacts/detail/{c['cid']}" if c.get("cid") and loc else ""

    def box(r, bg, border):
        f = fiches.get(r["c"].get("lnid")) if r["c"].get("lnid") else None
        msg = ia_msg(r["c"]) or compose(r["c"], r["key"], r["pos"], r["kind"], r["ft"].get("wd"), reason_text(r["key"], r["ft"]), f, r["sigs"])
        note = ia_note(r["c"]) or pour_toi(r["c"], f, r["key"], reason_text(r["key"], r["ft"]), r["sigs"], today)
        body = "<br><br>".join(esc(p) for p in msg)
        s = f'<div style="font-size:10px;font-weight:700;color:#566070;letter-spacing:.04em;margin-bottom:4px">{box_label(msg)}</div><div style="font-size:13px;line-height:1.5;background:#ffffff;border:1px solid {border};border-radius:8px;padding:10px 12px">{body}</div>'
        s += ('<div style="font-size:10px;font-weight:700;color:#566070;letter-spacing:.04em;margin:8px 0 3px">POUR TOI (INTERNE)</div><div style="font-size:12px;line-height:1.45;color:#3b4252;background:#f6f5f1;border-radius:8px;padding:8px 10px">'
              + "<br>".join(("<b>" + esc(x) + "</b>") if x.startswith("À RAPPELER") else esc(x) for x in note) + "</div>")
        return s

    def row_html(r, bg, border, ncols):
        c = r["c"]
        why = " ; ".join(reason_text(k, r["ft"]) for k in sorted(r["sigs"], key=lambda k: PRIO.index(k) if k in PRIO else 99)[:3])
        lk = link(c)
        a = f'<a href="{esc(lk)}" style="color:#e8630a;font-weight:700;text-decoration:none">Fiche →</a>' if lk else ""
        sub = esc(c.get("etape") or "")
        head = (f'<tr style="background:{bg}"><td style="padding:9px 8px 4px;border-top:2px solid {border};width:30%"><b>{esc(c.get("nom"))}</b><br><span style="color:#566070;font-size:11px">{sub}</span></td>'
                f'<td style="padding:9px 8px 4px;border-top:2px solid {border}">{esc(why)}</td><td style="padding:9px 8px 4px;border-top:2px solid {border};width:12%">{a}</td></tr>')
        return head + f'<tr style="background:{bg}"><td colspan="3" style="padding:2px 8px 10px">{box(r, bg, border)}</td></tr>'

    banner = "TEST · mail de démonstration, à relire avant la mise en route" if test else "Mail du matin"
    lu = logo_url()
    logo_cell = (f'<td width="88" style="vertical-align:middle;padding-right:16px"><div style="background:#ffffff;border-radius:12px;padding:7px 8px;width:68px;text-align:center">'
                 f'<img src="{esc(lu)}" alt="Agence Élite Média" width="60" style="display:block;border:0;height:auto;margin:0 auto"></div></td>') if lu else ""
    h = [f'<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>{esc(subject)}</title></head><body style="margin:0;background:#f3f2ee;font-family:Arial,Helvetica,sans-serif;color:#16181d">'
         '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f2ee"><tr><td align="center" style="padding:24px 12px">'
         '<table role="presentation" width="680" cellpadding="0" cellspacing="0" style="max-width:680px;width:100%;background:#ffffff;border-radius:14px;overflow:hidden">'
         f'<tr><td style="background:#e8630a;padding:16px 24px;color:#fff"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{logo_cell}<td style="vertical-align:middle;color:#fff"><div style="font-size:13px;opacity:.9">{esc(banner)}</div><div style="font-size:22px;font-weight:800;margin-top:4px">À traiter aujourd’hui</div><div style="font-size:14px;margin-top:2px">{esc(jour.capitalize())}</div></td></tr></table></td></tr>'
         '<tr><td style="padding:20px 24px 4px"><table role="presentation" width="100%" cellpadding="0" cellspacing="8"><tr>'
         f'<td width="25%" style="background:#fdf1ef;border:1px solid #f3b8b1;border-radius:10px;padding:10px 12px"><div style="font-size:26px;font-weight:800;color:#b42318">{len(reds)}</div><div style="font-size:12px;font-weight:700">à appeler</div></td>'
         f'<td width="25%" style="background:#fffbeb;border:1px solid #efd067;border-radius:10px;padding:10px 12px"><div style="font-size:26px;font-weight:800;color:#7a5300">{len(yell)}</div><div style="font-size:12px;font-weight:700">à écrire</div></td>'
         f'<td width="25%" style="background:#f4f3ef;border:1px solid #d6d2c7;border-radius:10px;padding:10px 12px"><div style="font-size:26px;font-weight:800">{contacted}</div><div style="font-size:12px;font-weight:700">contactés (7 j)</div></td>'
         f'<td width="25%" style="background:#effaf3;border:1px solid #a9dbbe;border-radius:10px;padding:10px 12px"><div style="font-size:26px;font-weight:800;color:#17653f">{greens}</div><div style="font-size:12px;font-weight:700">sans signal</div></td></tr></table></td></tr>']
    if reds:
        shown = reds[:12]
        h.append(f'<tr><td style="padding:16px 24px 4px"><div style="font-size:16px;font-weight:800;color:#b42318">À appeler aujourd’hui ({len(reds)})</div><div style="font-size:12px;color:#566070;margin-top:2px">Appelez d’abord. Le message sert à annoncer l’appel ou à relancer si le client ne répond pas. Relisez-le avant envoi.</div></td></tr>'
                 '<tr><td style="padding:6px 24px 8px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;border-collapse:collapse">' + "".join(row_html(r, "#fdf1ef", "#f3b8b1", 3) for r in shown) + '</table>'
                 + (f'<div style="font-size:12px;color:#566070;padding:6px 8px">+ {len(reds) - len(shown)} autres dans le dashboard.</div>' if len(reds) > len(shown) else "") + '</td></tr>')
    if yell:
        shown = yell[:10]
        h.append(f'<tr><td style="padding:12px 24px 4px"><div style="font-size:16px;font-weight:800;color:#7a5300">À écrire ({len(shown)} sur {len(yell)})</div><div style="font-size:12px;color:#566070;margin-top:2px">Un message suffit. Les autres sont dans le dashboard.</div></td></tr>'
                 '<tr><td style="padding:6px 24px 8px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;border-collapse:collapse">' + "".join(row_html(r, "#fffbeb", "#efd067", 3) for r in shown) + '</table></td></tr>')
    btn = f'<br><br><a href="{esc(dash)}" style="display:inline-block;background:#e8630a;color:#16181d;font-weight:800;text-decoration:none;padding:9px 16px;border-radius:8px">Ouvrir le dashboard</a>' if dash else ""
    h.append('<tr><td style="padding:14px 24px 20px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td style="background:#16181d;border-radius:10px;padding:14px 16px;color:#fff;font-size:13px;line-height:1.5"><b>Comment ça se ferme tout seul :</b> après votre appel ou votre message, laissez <b>une note sur la fiche GoHighLevel</b>. Le dashboard la voit et sort le client de cette liste.' + btn + '</td></tr></table></td></tr>'
             '<tr><td style="padding:0 24px 18px;color:#8a90a0;font-size:11px">Envoyé automatiquement chaque matin, uniquement s’il y a quelque chose à faire. Aucun numéro de téléphone ni contenu de message dans ce mail.</td></tr></table></td></tr></table></body></html>')
    # version texte
    t = [subject, ""]
    for titre, lst in (("À APPELER", reds[:12]), ("À ÉCRIRE", yell[:10])):
        if lst:
            t += [titre, "-" * len(titre)]
            for r in lst:
                t.append(f"* {r['c'].get('nom')} : " + " ; ".join(reason_text(k, r['ft']) for k in sorted(r['sigs'], key=lambda k: PRIO.index(k) if k in PRIO else 99)[:3]))
                f_ = fiches.get(r["c"].get("lnid")) if r["c"].get("lnid") else None
                t += ["  " + ligne for p in (ia_msg(r["c"]) or compose(r["c"], r["key"], r["pos"], r["kind"], r["ft"].get("wd"), reason_text(r["key"], r["ft"]), f_, r["sigs"])) for ligne in (p, "")]
                t += ["  POUR TOI (interne) :"] + ["   - " + x for x in (ia_note(r["c"]) or pour_toi(r["c"], f_, r["key"], reason_text(r["key"], r["ft"]), r["sigs"], today))] + [""]
    # liste complète pour la routine des messages (noms et raisons seulement, tous les clients rouges et jaunes, dans l'ordre du dashboard)
    t += ["LISTE COMPLÈTE (pour la routine)", "-" * 32]
    for titre, lst in (("ROUGES", reds), ("JAUNES", yell)):
        t.append(f"{titre} ({len(lst)})")
        for r in lst:
            t.append(f"* {r['c'].get('nom')} : " + " ; ".join(reason_text(k, r['ft']) for k in sorted(r['sigs'], key=lambda k: PRIO.index(k) if k in PRIO else 99)[:3]))
        t.append("")
    return subject, "".join(h), "\n".join(t), {"rouges": len(reds), "jaunes": len(yell), "contactes": contacted, "verts": greens}


def send(subject, html_body, text_body):
    user = os.environ["SMTP_USER"].strip()
    pw = os.environ["SMTP_PASSWORD"].replace(" ", "")
    split = lambda v: [a.strip() for a in (v or "").split(",") if a.strip()]
    to, cc = split(os.environ.get("MAIL_TO")), split(os.environ.get("MAIL_CC"))
    if not to:
        sys.exit("MAIL_TO est vide : aucun destinataire.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("Dashboard Elite Média", user))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60) as s:
        s.login(user, pw)
        s.send_message(msg, to_addrs=to + cc)
    print(f"Mail envoyé à {', '.join(to + cc)} : {subject}", flush=True)


def jour_autorise():
    """Le mail planifié ne part que du lundi au samedi (MAIL_DAYS pour changer : 0 = lundi ... 6 = dimanche)."""
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return True                                        # un lancement à la main (test) part toujours
    ok = {int(x) for x in re.findall(r"\d", os.environ.get("MAIL_DAYS") or "012345")}
    try:
        from zoneinfo import ZoneInfo
        d = dt.datetime.now(ZoneInfo("Europe/Paris")).weekday()
    except Exception:
        d = dt.datetime.now(dt.timezone.utc).weekday()
    return d in ok


def main():
    mode = (os.environ.get("MAIL_MODE") or "").strip().lower()
    if mode in ("test", "reel") and not os.environ.get("DRY_RUN") and not jour_autorise():
        print("Dimanche : pas de mail (MAIL_DAYS).", flush=True)
        return
    pack = load_pack()
    today = dt.date.fromisoformat(pack["built"])
    last_full = dt.date.fromisoformat(pack["last_full"])
    subject, html_body, text_body, counts = build(pack, today, last_full, mode)
    print(f"Mail : {counts}", flush=True)
    if os.environ.get("DRY_RUN"):
        open("mail_apercu.html", "w", encoding="utf-8").write(html_body)
        print("Aperçu écrit dans mail_apercu.html (rien n'est envoyé).", flush=True)
        return
    if mode not in ("test", "reel"):
        print("Envoi désactivé (MAIL_MODE vide ou « off »).", flush=True)
        return
    if counts["rouges"] + counts["jaunes"] == 0:
        print("Rien à traiter aujourd'hui : aucun mail envoyé.", flush=True)
        return
    send(subject, html_body, text_body)


if __name__ == "__main__":
    main()
