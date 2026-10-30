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
PRIO = ["wa_resil", "wa_remb", "wa_decu", "flag", "nrep", "silence", "ech2", "ech1", "rdv", "chute", "vues", "avis", "maj60", "sat", "win"]
WKEY = {"wa_resil": "war", "wa_remb": "wam", "wa_decu": "wad", "silence": "sil"}
FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


def esc(s):
    return html.escape(str(s), quote=False)


def nf(n):
    return f"{int(round(n)):,}".replace(",", " ")


def first_name(nom):
    nom = re.split(r"\s+[-–—]\s+", str(nom or ""))[0]
    toks = [t for t in re.split(r"[\s/]+", nom) if t]
    up = lambda t: len(t) > 1 and t.isupper()
    cands = [t for t in toks if not up(t)]
    if not cands:
        return ""
    t = cands[0]
    return t[:1].upper() + t[1:]


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
    """Phrase positive vraie et sourcée. Renvoie (texte, type, source) ; type = « stats » ou « bilan »."""
    f = fiches.get(c.get("lnid")) if c.get("lnid") else None
    m = fiche_metrics(f) if f else None
    if m:
        frags = []
        v, a = m["v"], m["a"]
        if v[0] and v[1] and v[1] >= 30 and v[0] / v[1] - 1 >= 0.10:
            frags.append(f"visibilité en hausse de {round((v[0] / v[1] - 1) * 100)} % ({nf(v[0])} vues contre {nf(v[1])})")
        if a[0] is not None and a[1] is not None and a[0] > a[1] and a[0] >= 5:
            frags.append(f"{a[0]} appels contre {a[1]}")
        if m["note"] and m["avis"] and m["note"] >= 4.5 and m["avis"] >= 10:
            frags.append(f"une note de {str(round(m['note'], 1)).replace('.', ',')} sur {m['avis']} avis")
        if frags:
            return ", ".join(frags[:3]), "stats", "Statistiques Google de la fiche reliée"
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


def compose(c, key, pos, kind):
    p = first_name(c.get("nom"))
    hello = f"Bonjour {p}," if p else "Bonjour,"
    lead = "Ces derniers mois, la fiche a progressé : " if kind == "stats" else "Pour mémoire, au dernier bilan : "
    par = [hello]
    if key == "wa_resil":
        par.append("Merci pour votre message, je comprends votre démarche.")
        par.append("Je préfère qu’on en parle de vive voix : je peux vous appeler aujourd’hui ? Dites-moi le créneau qui vous arrange.")
        if pos:
            par.append(f"{lead}{pos}. On trouvera ensemble la meilleure solution pour vous.")
    elif key == "wa_remb":
        par.append("Je vérifie votre dossier de facturation et je vous appelle aujourd’hui pour en parler avec vous.")
        par.append("Si vous préférez un autre moment, dites-le-moi ici.")
    elif key == "wa_decu":
        par.append("Merci pour votre franchise, vous avez raison d’attendre des résultats concrets." + (f" Voici où en est la fiche : {pos}." if pos and kind == "stats" else (f" Pour mémoire, au dernier bilan : {pos}." if pos else "")))
        par.append("Je vous propose un court point cette semaine pour voir ce que l’on peut accélérer.")
    elif key in ("flag", "maj60", "sat"):
        par.append("Cela fait un moment que nous n’avons pas fait le point ensemble. J’aimerais vous appeler cette semaine pour voir comment cela se passe de votre côté et ce que l’on peut améliorer.")
        par.append("Quel jour vous arrange ?")
    elif key in ("nrep", "silence"):
        par.append("Je prends de vos nouvelles : comment se passent vos demandes via Google en ce moment ?")
        if pos:
            par.append(f"{lead}{pos}.")
        par.append("Si vous avez une nouveauté (photos, offre, horaires), envoyez-la ici et nous la mettons en ligne.")
    elif key in ("ech2", "ech1"):
        par.append(f"Votre période de suivi arrive à son terme dans {'une semaine' if key == 'ech2' else 'un mois'}. Je vous propose un rendez-vous bilan pour regarder vos résultats et décider ensemble de la suite.")
        par.append("Quel créneau vous convient ?")
    elif key == "rdv":
        par.append("Je vous propose un rendez-vous bilan pour regarder vos résultats ensemble.")
        par.append("Quel créneau vous convient ?")
    elif key in ("chute", "vues"):
        par.append("J’ai remarqué une baisse des vues de votre fiche ces dernières semaines. Je regarde ce qui peut être ajusté et je vous propose un point rapide.")
        par.append("Avez-vous changé quelque chose dans votre activité (horaires, adresse, téléphone) ?")
    elif key == "avis":
        par.append("J’ai vu les derniers avis sur votre fiche. Je vous propose de leur répondre ensemble, pour montrer votre sérieux aux futurs clients.")
        par.append("Je vous envoie une proposition de réponse aujourd’hui ?")
    else:
        par.append("Je prends de vos nouvelles. Comment se passe votre activité en ce moment ? N’hésitez pas à me dire s’il y a quelque chose à améliorer.")
    par.append(SIG)
    return par


def build(pack, today, last_full, mode=""):
    clients = [c for c in (pack.get("clients") or []) if not refresh.SETUP_RE.search(c.get("etape") or "")]
    feats = refresh.client_features(pack, clients, today, last_full)
    fiches = {f["id"]: f for f in pack["fiches"]}
    rows, contacted, greens = [], 0, 0
    for c, ft in zip(clients, feats):
        color, sigs = refresh.feu_of(ft)
        if color == "vert":
            greens += 1
            continue
        ct, wd = c.get("ct_j"), ft.get("wd")
        if ct is not None and ct <= 7 and (wd is None or ct <= wd):
            contacted += 1
            continue
        key = next((k for k in PRIO if k in sigs), sigs[0] if sigs else "win")
        pts = sum(refresh.DEF_W.get(WKEY.get(k, k), 0) for k in sigs)
        pos, kind, src = positives(c, fiches)
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
        msg = compose(r["c"], r["key"], r["pos"], r["kind"])
        body = "<br><br>".join(esc(p) for p in msg)
        s = f'<div style="font-size:10px;font-weight:700;color:#566070;letter-spacing:.04em;margin-bottom:4px">MESSAGE PRÊT À ENVOYER (WHATSAPP)</div><div style="font-size:13px;line-height:1.5;background:#ffffff;border:1px solid {border};border-radius:8px;padding:10px 12px">{body}</div>'
        s += f'<div style="font-size:10px;color:#8a90a0;margin-top:4px">{"Chiffres : " + esc(r["src"]) if r["src"] else "Pas de chiffres disponibles : message sans chiffres"}</div>'
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
    h = [f'<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>{esc(subject)}</title></head><body style="margin:0;background:#f3f2ee;font-family:Arial,Helvetica,sans-serif;color:#16181d">'
         '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f2ee"><tr><td align="center" style="padding:24px 12px">'
         '<table role="presentation" width="680" cellpadding="0" cellspacing="0" style="max-width:680px;width:100%;background:#ffffff;border-radius:14px;overflow:hidden">'
         f'<tr><td style="background:#e8630a;padding:18px 24px;color:#fff"><div style="font-size:13px;opacity:.9">{esc(banner)}</div><div style="font-size:22px;font-weight:800;margin-top:4px">À traiter aujourd’hui</div><div style="font-size:14px;margin-top:2px">{esc(jour.capitalize())}</div></td></tr>'
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
                t += ["  " + ligne for p in compose(r["c"], r["key"], r["pos"], r["kind"]) for ligne in (p, "")]
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


def main():
    mode = (os.environ.get("MAIL_MODE") or "").strip().lower()
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
