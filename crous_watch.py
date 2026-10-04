#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crous_watch.py
==============

Surveille la phase complémentaire de trouverunlogement.lescrous.fr et envoie
une alerte (mail + notification push optionnelle) dès qu'un logement apparaît
dans les départements/villes que vous surveillez.

INSTALLATION
------------
    pip install requests beautifulsoup4

CONFIGURATION
-------------
Au moins un canal d'alerte doit être configuré. Le plus simple, sans compte ni
mot de passe — installez l'appli **ntfy** (iOS/Android), abonnez-vous à un nom
de sujet que vous inventez, puis :

    export CROUS_NTFY_TOPIC="crous-alerte-8f3k2p"

Choisissez un nom long et unique : n'importe qui connaissant le sujet peut y
lire ou y publier. C'est tout — ni compte, ni adresse mail, ni identifiants.

Alertes par mail (facultatif, en plus ou à la place) :

    export CROUS_SMTP_USER="votre.adresse@gmail.com"
    export CROUS_SMTP_PASS="xxxx xxxx xxxx xxxx"   # mot de passe d'application
    export CROUS_MAIL_TO="votre.adresse@gmail.com,autre.adresse@yahoo.fr"

  CROUS_MAIL_TO accepte plusieurs destinataires séparés par des virgules.

  Gmail exige un « mot de passe d'application », qui n'apparaît qu'une fois la
  validation en 2 étapes activée (https://myaccount.google.com/apppasswords).
  N'utilisez JAMAIS votre mot de passe Gmail principal ici.

  Pour envoyer depuis Yahoo plutôt que Gmail (le mot de passe d'application se
  crée sur https://login.yahoo.com/account/security) :
    export CROUS_SMTP_HOST="smtp.mail.yahoo.com"
    export CROUS_SMTP_PORT="465"
    export CROUS_SMTP_USER="votre.adresse@yahoo.fr"

UTILISATION
-----------
    python3 crous_watch.py --test    # envoie un mail de test, vérifie la config
    python3 crous_watch.py --once    # une seule vérification puis quitte
    python3 crous_watch.py           # surveillance en continu

Pour le laisser tourner en fond (Linux/macOS) :
    nohup python3 crous_watch.py > crous.log 2>&1 &
"""

import argparse
import json
import logging
import os
import random
import re
import smtplib
import sys
import time
from email.message import EmailMessage
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Dépendances manquantes. Lancez :  pip install requests beautifulsoup4")

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

BASE_URL = "https://trouverunlogement.lescrous.fr"
TOOL_ID = 47                      # 47 = campagne 2026-2027
SEARCH_URL = f"{BASE_URL}/tools/{TOOL_ID}/search"

# Zones surveillées. Ajoutez, retirez ou modifiez librement.
#   cp       : préfixes de code postal (2 chiffres = département)
#   villes   : filet de sécurité si le code postal est mal formaté
#   priorite : "urgent" fait sonner le téléphone même en mode silencieux ;
#              "default" arrive comme une notification ordinaire
ZONES = {
    "Île-de-France": {
        "cp": {"75", "77", "78", "91", "92", "93", "94", "95"},
        "villes": {
            "paris", "gif-sur-yvette", "orsay", "bures", "palaiseau", "saclay",
            "massy", "antony", "versailles", "cachan", "bourg-la-reine", "sceaux",
        },
        "priorite": "urgent",
    },
    "Caen": {
        "cp": {"14"},
        "villes": {
            "caen", "herouville", "hérouville", "ifs", "mondeville", "epron",
            "fleury-sur-orne", "saint-contest", "colombelles",
        },
        "priorite": "default",
    },
}

INTERVAL = int(os.getenv("CROUS_INTERVAL", "30"))     # secondes entre 2 vérifs
JITTER = max(1, INTERVAL // 5)                        # aléa, pour ne pas marteler
FULL_SCAN_EVERY = 20      # scan complet (toutes pages) toutes les N vérifications
MAX_PAGES = 20
TIMEOUT = 25

STATE_FILE = Path(os.getenv("CROUS_STATE", "~/.crous_watch_state.json")).expanduser()

SMTP_HOST = os.getenv("CROUS_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("CROUS_SMTP_PORT", "465"))
SMTP_USER = os.getenv("CROUS_SMTP_USER", "")
SMTP_PASS = os.getenv("CROUS_SMTP_PASS", "")


def parse_recipients(raw):
    """Accepte « a@x.fr, b@y.com » ou une seule adresse."""
    return [a.strip() for a in re.split(r"[,;\s]+", raw or "") if "@" in a]


MAIL_TO = parse_recipients(os.getenv("CROUS_MAIL_TO", SMTP_USER))
NTFY_TOPIC = os.getenv("CROUS_NTFY_TOPIC", "")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%d/%m %H:%M:%S",
)
log = logging.getLogger("crous")

RE_ACCOMMODATION = re.compile(r"/tools/\d+/accommodations/(\d+)")
RE_POSTAL = re.compile(r"\b(\d{5})\b")
# Code postal suivi du nom de commune : permet de ne jamais confondre une
# commune avec un nom de rue (« Avenue de Paris » à Lormont).
RE_CP_VILLE = re.compile(
    r"\b\d{5}\s*[-–]?\s*([A-Za-zÀ-ÿ'’\-]+(?:[ \-][A-Za-zÀ-ÿ'’\-]+){0,3})"
)
RE_PRICE = re.compile(
    r"(?:de\s+)?\d[\d\s\u202f]*(?:,\d{2})?\s*(?:€\s*à\s*\d[\d\s\u202f]*(?:,\d{2})?\s*)?€"
)
RE_SURFACE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m[²2]")
RE_TOTAL = re.compile(r"(\d+)\s+logements?\s+trouvés")


# ---------------------------------------------------------------------------
# RÉCUPÉRATION ET ANALYSE DES PAGES
# ---------------------------------------------------------------------------

def parse_page(html):
    """Extrait la liste des logements d'une page de résultats."""
    soup = BeautifulSoup(html, "html.parser")
    listings = {}

    for link in soup.find_all("a", href=True):
        m = RE_ACCOMMODATION.search(link["href"])
        if not m:
            continue
        acc_id = m.group(1)
        if acc_id in listings:
            continue

        name = link.get_text(" ", strip=True) or f"Logement {acc_id}"

        # On remonte dans le DOM jusqu'à trouver le bloc contenant l'adresse,
        # sans jamais englober une deuxième annonce.
        node, container = link, None
        for _ in range(6):
            node = node.parent
            if node is None:
                break
            if len(node.find_all("a", href=RE_ACCOMMODATION)) > 1:
                break
            text = node.get_text(" ", strip=True)
            if RE_POSTAL.search(text):
                container = node
                break

        text = container.get_text(" ", strip=True) if container else name
        postal = RE_POSTAL.search(text)
        commune = RE_CP_VILLE.search(text)
        price = RE_PRICE.search(text)
        surface = RE_SURFACE.search(text)

        # L'adresse = ce qui suit le nom de la résidence dans le bloc.
        address = text
        if name in address:
            address = address.split(name, 1)[1]
        if surface:                       # on coupe avant « 18 m² », « Individuel »...
            address = address[: address.find(surface.group(0))] if surface.group(0) in address else address
        address = " ".join(address.split()).strip(" ,-")[:140]

        listings[acc_id] = {
            "id": acc_id,
            "name": name,
            "address": address,
            "postal": postal.group(1) if postal else "",
            "commune": commune.group(1).strip() if commune else "",
            "price": price.group(0) if price else "?",
            "surface": surface.group(0) if surface else "?",
            "url": f"{BASE_URL}/tools/{TOOL_ID}/accommodations/{acc_id}",
        }

    total = RE_TOTAL.search(soup.get_text(" ", strip=True))
    return list(listings.values()), int(total.group(1)) if total else None


class RateLimited(Exception):
    """Le serveur nous demande de ralentir."""

    def __init__(self, wait):
        super().__init__(f"limitation serveur, attendre {wait}s")
        self.wait = wait


def _get(session, url):
    resp = session.get(url, timeout=TIMEOUT)
    if resp.status_code in (403, 429, 503):
        retry = (resp.headers.get("Retry-After") or "").strip()
        raise RateLimited(int(retry) if retry.isdigit() else 300)
    resp.raise_for_status()
    # Sans charset annoncé, requests retombe sur ISO-8859-1 et « m² » arrive
    # sous la forme « mÂ² » : les prix et surfaces deviennent illisibles.
    if "charset" not in resp.headers.get("Content-Type", "").lower():
        resp.encoding = "utf-8"
    return resp


def fetch(full=True):
    """Récupère les annonces.

    full=False ne lit que la première page : suffisant pour détecter qu'un
    changement a eu lieu, et deux fois moins de requêtes vers le serveur.

    Retourne (logements, total, signature).
    """
    # Session neuve à chaque cycle : le site mémorise les filtres côté serveur,
    # un cookie vierge garantit la liste nationale complète.
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "fr-FR,fr;q=0.9"})

    all_listings, total, page, first_page = {}, None, 1, ()
    while page <= MAX_PAGES:
        url = SEARCH_URL if page == 1 else f"{SEARCH_URL}?page={page}"
        listings, page_total = parse_page(_get(session, url).text)

        if page == 1:
            # Le compteur fait foi sur la page 1 uniquement : sinon un scan
            # complet et un scan économe produiraient des signatures différentes.
            total = page_total
            first_page = tuple(sorted(i["id"] for i in listings))
        if not listings:
            break

        before = len(all_listings)
        for item in listings:
            all_listings[item["id"]] = item
        if len(all_listings) == before:      # plus rien de nouveau
            break
        if not full:                         # scan économe : on s'arrête là
            break

        page += 1
        time.sleep(1.0)                      # on reste poli avec le serveur

    return list(all_listings.values()), total, (total, first_page)


def zone_of(listing):
    """Retourne le nom de la zone surveillée correspondante, ou None."""
    dept = listing["postal"][:2]

    # Le code postal fait foi. Pas de repli sur les noms de villes quand il est
    # présent : sinon « 717 Avenue de PARIS, 33310 LORMONT » déclenche une
    # alerte Île-de-France pour un logement en Gironde.
    if dept:
        for nom, z in ZONES.items():
            if dept in z["cp"]:
                return nom
        return None

    # Sans code postal exploitable, on se rabat sur la commune extraite de
    # l'adresse — jamais sur l'adresse entière, qui contient les noms de rues.
    commune = listing.get("commune", "").lower()
    if not commune:
        return None
    for nom, z in ZONES.items():
        if any(re.search(rf"\b{re.escape(v)}\b", commune) for v in z["villes"]):
            return nom
    return None


# ---------------------------------------------------------------------------
# NOTIFICATIONS
# ---------------------------------------------------------------------------

def format_listing(item):
    return (
        f"🏠 {item['name']}  [{item['zone']}]\n"
        f"   {item['address']}\n"
        f"   {item['price']} — {item['surface']}\n"
        f"   {item['url']}\n"
    )


def send_email(subject, body):
    """Retourne True (envoyé), False (échec) ou None (canal non configuré)."""
    if not (SMTP_USER and SMTP_PASS and MAIL_TO):
        return None
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(MAIL_TO)
    msg["X-Priority"] = "1"
    msg.set_content(body)
    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
                smtp.login(SMTP_USER, SMTP_PASS)
                refused = smtp.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(SMTP_USER, SMTP_PASS)
                refused = smtp.send_message(msg)
        delivered = [a for a in MAIL_TO if a not in (refused or {})]
        log.info("Mail envoyé à %s", ", ".join(delivered) or "personne")
        if refused:
            log.warning("Adresse(s) refusée(s) : %s", ", ".join(refused))
        return bool(delivered)
    except Exception as exc:
        log.error("Échec de l'envoi du mail : %s", exc)
        return False


def send_push(title, body, priorite="urgent"):
    """Retourne True (envoyé), False (échec) ou None (canal non configuré)."""
    if not NTFY_TOPIC:
        return None
    try:
        resp = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": priorite,
                "Tags": "house",
            },
            timeout=15,
        )
        resp.raise_for_status()
        log.info("Notification push envoyée sur ntfy.sh/%s", NTFY_TOPIC)
        return True
    except Exception as exc:
        log.error("Échec de la notification push : %s", exc)
        return False


def alert(new_items):
    zones = sorted({i["zone"] for i in new_items})
    count = len(new_items)
    mot = "logement disponible" if count == 1 else "logements disponibles"
    subject = f"🚨 {count} {mot} — {' et '.join(zones)}"

    # Si une seule annonce est urgente, toute l'alerte l'est.
    priorite = ("urgent" if any(ZONES[z]["priorite"] == "urgent" for z in zones)
                else "default")

    body = (
        "Un logement vient d'apparaître sur la plateforme CROUS.\n"
        "Les places partent vite : réservez sans attendre.\n\n"
        + "\n".join(format_listing(i) for i in new_items)
        + f"\nRecherche complète : {SEARCH_URL}\n"
    )

    results = [send_email(subject, body), send_push(subject, body, priorite)]
    if not any(r is True for r in results):
        log.error("AUCUNE alerte n'a pu être transmise — contenu affiché ici :")
        print(f"\n=== {subject} ===\n{body}")


def alert_failure(exc):
    """Prévient que la surveillance est en panne — silencieusement inutile sinon."""
    subject = "⚠️ La surveillance CROUS est en panne"
    body = (f"10 erreurs consécutives. Dernière : {exc}\n"
            "Le script tourne encore mais ne voit plus le site.\n")
    if not any(r is True for r in (send_email(subject, body), send_push(subject, body))):
        log.error("Impossible de signaler la panne par un canal d'alerte.")


# ---------------------------------------------------------------------------
# ÉTAT PERSISTANT
# ---------------------------------------------------------------------------

def load_state():
    try:
        return set(json.loads(STATE_FILE.read_text()).get("seen", []))
    except Exception:
        return set()


def save_state(seen):
    try:
        STATE_FILE.write_text(json.dumps({"seen": sorted(seen)}, indent=2))
    except Exception as exc:
        log.warning("Impossible d'écrire %s : %s", STATE_FILE, exc)


# ---------------------------------------------------------------------------
# BOUCLE PRINCIPALE
# ---------------------------------------------------------------------------

def check_once(seen, ctx):
    """Une vérification. Retourne le nouvel ensemble d'identifiants connus."""
    ctx["cycle"] += 1
    # Scan complet toutes les FULL_SCAN_EVERY vérifications, ou au démarrage.
    full = ctx["sig"] is None or ctx["cycle"] % FULL_SCAN_EVERY == 0

    listings, total, sig = fetch(full=full)

    if not full:
        if sig == ctx["sig"]:
            log.debug("Rien n'a bougé (%s logements) — page 2 non sollicitée.", total)
            return seen
        log.info("Changement détecté → scan complet.")
        listings, total, sig = fetch(full=True)

    if not listings:
        log.warning("Aucun logement récupéré (site indisponible ?) — état conservé.")
        return seen

    ctx["sig"] = sig
    matches = []
    for item in listings:
        nom = zone_of(item)
        if nom:
            item["zone"] = nom
            matches.append(item)

    current = {i["id"] for i in matches}
    par_zone = {z: sum(1 for i in matches if i["zone"] == z) for z in ZONES}
    log.info(
        "%s logement(s) en France — %s",
        total if total is not None else len(listings),
        ", ".join(f"{z} : {n}" for z, n in par_zone.items()),
    )

    fresh = [i for i in matches if i["id"] not in seen]
    if fresh:
        for item in fresh:
            log.info("NOUVEAU [%s] → %s | %s", item["zone"], item["name"], item["address"])
        alert(fresh)

    # Un logement qui disparaît puis revient déclenchera une nouvelle alerte.
    return current


def main():
    parser = argparse.ArgumentParser(description="Surveillance logements CROUS")
    parser.add_argument("--once", action="store_true", help="une seule vérification")
    parser.add_argument("--test", action="store_true", help="envoie un mail de test")
    args = parser.parse_args()

    if args.test:
        subject = "✅ Test — surveillance CROUS"
        body = ("Si vous recevez ceci, les alertes fonctionnent.\n"
                "Le script vous préviendra dès qu'un logement apparaît.\n")
        mail, push = send_email(subject, body), send_push(subject, body)

        if mail is None and push is None:
            log.error("Aucun canal d'alerte configuré : le script ne pourrait pas "
                      "vous prévenir.\n     Définissez CROUS_NTFY_TOPIC (le plus simple, "
                      "sans compte)\n     ou CROUS_SMTP_USER / CROUS_SMTP_PASS / CROUS_MAIL_TO.")
            sys.exit(1)
        if mail is None:
            log.info("Mail non configuré — alertes par notification push uniquement.")
        if push is None:
            log.info("ntfy non configuré — alertes par mail uniquement.")
        sys.exit(0 if (mail is True or push is True) else 1)

    canaux = []
    if MAIL_TO and SMTP_USER and SMTP_PASS:
        canaux.append("mail → " + ", ".join(MAIL_TO))
    if NTFY_TOPIC:
        canaux.append(f"push → ntfy.sh/{NTFY_TOPIC}")

    seen = load_state()
    log.info("Démarrage. Zones surveillées : %s",
             ", ".join(f"{z} ({d['priorite']})" for z, d in ZONES.items()))
    log.info("Alertes : %s", " | ".join(canaux) or "AUCUN CANAL CONFIGURÉ (voir --test)")
    log.info("%s identifiant(s) déjà connu(s). Vérification toutes les ~%ss.", len(seen), INTERVAL)
    if INTERVAL < 15:
        log.warning(
            "Intervalle de %ss : risque réel de blocage de votre IP par le serveur, "
            "ce qui arrêterait la surveillance sans prévenir. 30s est un bon compromis.",
            INTERVAL,
        )

    ctx = {"sig": None, "cycle": 0}

    if args.once:
        save_state(check_once(seen, ctx))
        return

    failures, backoff = 0, 1
    while True:
        try:
            seen = check_once(seen, ctx)
            save_state(seen)
            failures, backoff = 0, 1
            pause = max(5, INTERVAL + random.randint(-JITTER, JITTER))
        except KeyboardInterrupt:
            log.info("Arrêt demandé. À bientôt.")
            break
        except RateLimited as exc:
            # Le serveur nous freine : on recule au lieu de s'entêter.
            pause = max(exc.wait, INTERVAL * backoff)
            backoff = min(backoff * 2, 32)
            log.warning("Le serveur limite les requêtes. Pause de %ss.", pause)
        except Exception as exc:
            failures += 1
            pause = max(60, INTERVAL)
            log.error("Erreur (%s tentative(s) échouée(s)) : %s", failures, exc)
            if failures == 10:
                alert_failure(exc)
        time.sleep(pause)


if __name__ == "__main__":
    main()
