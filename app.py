"""
Cockpit appels — Hympyr Énergies
================================

Outil de pilotage de la campagne d'appels clients : file d'appel priorisée,
saisie du résultat, suivi des points de livraison, tableau de bord et exports.

CE QUI A CHANGÉ PAR RAPPORT À LA VERSION PRÉCÉDENTE
---------------------------------------------------
1. Le fichier client ne vit plus dans st.session_state mais dans un cache de
   ressource partagé par le processus. Une déconnexion du navigateur (veille de
   l'ordinateur, onglet en arrière-plan pendant un appel, changement de réseau)
   ne fait plus réapparaître l'écran de téléversement.

2. Le compteur « modifications non sauvegardées » est calculé depuis la base et
   non depuis la session. Il ne repasse plus au vert tout seul après une
   déconnexion — c'était le pire cas : croire son travail sauvegardé.

3. SQLite est configuré en mode WAL avec un délai d'attente, pour supporter
   plusieurs onglets ou utilisateurs simultanés sans « database is locked ».

4. La position dans la file (fiche en cours) est mémorisée en base : après une
   reconnexion, on reprend là où on s'était arrêté.

5. Correction d'une perte de données : à l'import d'une sauvegarde, les dates de
   rappel étaient systématiquement écrasées. Elles sont désormais relues et
   reconverties.

6. Plus aucun st.stop() dans un onglet : une file vide n'empêche plus d'accéder
   au tableau de bord et aux exports.

7. Sauvegarde automatique optionnelle : un fichier CSV est écrit à chaque
   enregistrement dans un dossier de sauvegarde, en plus des exports manuels.

POINT DE VIGILANCE — PERSISTANCE
--------------------------------
La base SQLite est écrite sur le disque local. Si l'application tourne sur une
plateforme à système de fichiers éphémère (Streamlit Community Cloud par
exemple), ce disque est perdu à chaque redémarrage du conteneur. L'export du
soir reste alors la seule sauvegarde. Voir la variable DOSSIER_DONNEES pour
pointer vers un volume persistant.

POINT DE VIGILANCE — DONNÉES PERSONNELLES
-----------------------------------------
Cet outil traite des noms, adresses, téléphones et adresses e-mail de clients :
information classifiée C2. Hébergement et plateforme doivent être instruits en
conséquence, et l'application inscrite au registre des applications.

Dépendances : streamlit >= 1.30, pandas >= 2.0, openpyxl >= 3.1
"""

from __future__ import annotations

import io
import os
import re
import sqlite3
import datetime as dt
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

import pandas as pd
import streamlit as st

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

# Dossier des données. Pour un hébergement avec volume persistant, définir la
# variable d'environnement HYMPYR_DATA_DIR vers ce volume.
DOSSIER_DONNEES = Path(os.environ.get("HYMPYR_DATA_DIR", Path(__file__).parent))
DOSSIER_DONNEES.mkdir(parents=True, exist_ok=True)

DB_PATH = DOSSIER_DONNEES / "suivi_appels.db"
DOSSIER_SAUVEGARDES = DOSSIER_DONNEES / "sauvegardes"
DOSSIER_SAUVEGARDES.mkdir(parents=True, exist_ok=True)

VERT, VERT_FONCE, ORANGE = "#1A6B45", "#0D3D27", "#FF5C29"

PRODUITS = [
    "GNR", "Gasoil routier", "Sans plomb", "AdBlue",
    "Fioul domestique", "HVO", "Granulés de bois", "Lubrifiants / Huiles",
]

STATUTS = [
    "À appeler", "À rappeler", "Injoignable",
    "Fait ✅", "Doublon", "Ancien client (à sortir)",
]
STATUTS_TERMINES = {"Fait ✅", "Doublon", "Ancien client (à sortir)"}

STATUTS_ADRESSE = ["À vérifier", "Vérifié ✅", "Adresse obsolète"]

MOTIFS_SORTIE = [
    "—", "Passé à la concurrence", "Utilise une autre énergie",
    "Décès", "Cessation d'activité / fermeture", "Ne souhaite plus être contacté",
    "Injoignable définitivement", "Autre",
]

# Échéance : obligation d'émission de la facturation électronique pour les PME.
DEADLINE = dt.date(2027, 9, 1)

st.set_page_config(page_title="Cockpit appels — Hympyr", page_icon="📞", layout="wide")

st.markdown(
    f"""
<style>
  h1, h2, h3 {{ color: {VERT_FONCE}; }}
  .stButton>button {{ border-radius: 8px; font-weight: 600; }}
  div[data-testid="stMetricValue"] {{ color: {VERT}; }}
  .fiche {{ background:#f6faf7; border:1px solid #d1e8da; border-left:5px solid {VERT};
           border-radius:10px; padding:16px 20px; margin-bottom:12px; }}
  .pill {{ display:inline-block; background:{VERT}; color:#fff; border-radius:50px;
           padding:2px 12px; font-size:12px; font-weight:600; margin-right:6px; }}
  .pill-orange {{ background:{ORANGE}; }}
</style>
""",
    unsafe_allow_html=True,
)


# ─────────────────────────────────────────────────────────────────────────────
# UTILITAIRES GÉNÉRAUX
# ─────────────────────────────────────────────────────────────────────────────

def jours_ouvres(debut: dt.date, fin: dt.date) -> int:
    """Nombre de jours ouvrés (lundi-vendredi) entre deux dates, fin exclue."""
    if fin <= debut:
        return 0
    n, d = 0, debut
    while d < fin:
        if d.weekday() < 5:
            n += 1
        d += dt.timedelta(days=1)
    return n


def date_apres_jours_ouvres(depart: dt.date, nb: int) -> dt.date:
    """Date obtenue en ajoutant nb jours ouvrés à une date de départ."""
    d, restants = depart, max(0, int(nb))
    while restants > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            restants -= 1
    return d


def maintenant_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def formater_entier(n: int) -> str:
    return f"{int(n):,}".replace(",", " ")


def jolie_date(valeur, avec_heure: bool = False) -> str:
    """Formate une date ISO en JJ/MM/AAAA, en renvoyant la valeur telle quelle si illisible."""
    texte = str(valeur or "").strip()
    if not texte:
        return ""
    try:
        d = pd.to_datetime(texte)
        return d.strftime("%d/%m/%Y %H:%M") if avec_heure else d.strftime("%d/%m/%Y")
    except Exception:
        return texte


def vers_iso(valeur) -> str:
    """Convertit une date (JJ/MM/AAAA ou ISO) en ISO, ou chaîne vide si illisible.

    Le format ISO est détecté en premier : sans cela, « 2026-09-03 » serait lu
    en jour-mois-année et deviendrait le 9 mars.
    """
    texte = str(valeur or "").strip()
    if not texte:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}", texte):
        try:
            return pd.to_datetime(texte[:10]).date().isoformat()
        except Exception:
            return ""
    for dayfirst in (True, False):
        try:
            return pd.to_datetime(texte, dayfirst=dayfirst).date().isoformat()
        except Exception:
            continue
    return ""


def trouver_colonne(colonnes: Iterable, *cibles: str):
    """Retrouve un nom de colonne quelle que soit la casse ou les espaces."""
    norm = {str(c).strip().lower(): c for c in colonnes}
    for cible in cibles:
        if cible in norm:
            return norm[cible]
    return None


# ─────────────────────────────────────────────────────────────────────────────
# BASE DE SUIVI (SQLite)
# ─────────────────────────────────────────────────────────────────────────────

@contextmanager
def connexion():
    """Connexion SQLite configurée pour supporter plusieurs sessions simultanées.

    WAL autorise des lectures pendant une écriture ; busy_timeout évite l'erreur
    « database is locked » lorsque deux onglets enregistrent en même temps.
    """
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA synchronous=NORMAL")
        yield con
        con.commit()
    finally:
        con.close()


def initialiser_base() -> None:
    with connexion() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS suivi (
                code_client   TEXT PRIMARY KEY,
                statut        TEXT,
                existe        TEXT,
                produits      TEXT,
                email_maj     TEXT,
                tel_maj       TEXT,
                note          TEXT,
                doublon_de    TEXT,
                rappel_date   TEXT,
                motif_sortie  TEXT,
                maj_le        TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS suivi_adresses (
                code_adresse  TEXT PRIMARY KEY,
                referent      TEXT,
                tel_site      TEXT,
                statut_adr    TEXT,
                note_adr      TEXT,
                maj_le        TEXT
            )
        """)
        # Table technique : dernier export, dernière position dans la file, etc.
        con.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                cle    TEXT PRIMARY KEY,
                valeur TEXT
            )
        """)
        # Migrations : colonnes ajoutées après coup sur une base existante.
        for table, colonnes in (
            ("suivi", ("motif_sortie", "rappel_date")),
            ("suivi_adresses", ()),
        ):
            existantes = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
            for c in colonnes:
                if c not in existantes:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {c} TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS idx_suivi_maj ON suivi(maj_le)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_adr_maj ON suivi_adresses(maj_le)")


initialiser_base()


def lire_meta(cle: str, defaut: str = "") -> str:
    with connexion() as con:
        r = con.execute("SELECT valeur FROM meta WHERE cle=?", (cle,)).fetchone()
    return r[0] if r else defaut


def ecrire_meta(cle: str, valeur: str) -> None:
    with connexion() as con:
        con.execute(
            "INSERT INTO meta (cle, valeur) VALUES (?, ?) "
            "ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur",
            (cle, str(valeur)),
        )


def charger_suivi() -> pd.DataFrame:
    with connexion() as con:
        df = pd.read_sql("SELECT * FROM suivi", con, dtype=str)
    return df.fillna("")


def charger_suivi_adresses() -> pd.DataFrame:
    with connexion() as con:
        df = pd.read_sql("SELECT * FROM suivi_adresses", con, dtype=str)
    return df.fillna("")


def enregistrer(code: str, **champs) -> None:
    champs["code_client"] = str(code)
    champs["maj_le"] = maintenant_iso()
    cols = ",".join(champs)
    ph = ",".join("?" for _ in champs)
    upd = ",".join(f"{k}=excluded.{k}" for k in champs if k != "code_client")
    with connexion() as con:
        con.execute(
            f"INSERT INTO suivi ({cols}) VALUES ({ph}) "
            f"ON CONFLICT(code_client) DO UPDATE SET {upd}",
            list(champs.values()),
        )


def enregistrer_adresse(code_adresse: str, **champs) -> None:
    champs["code_adresse"] = str(code_adresse)
    champs["maj_le"] = maintenant_iso()
    cols = ",".join(champs)
    ph = ",".join("?" for _ in champs)
    upd = ",".join(f"{k}=excluded.{k}" for k in champs if k != "code_adresse")
    with connexion() as con:
        con.execute(
            f"INSERT INTO suivi_adresses ({cols}) VALUES ({ph}) "
            f"ON CONFLICT(code_adresse) DO UPDATE SET {upd}",
            list(champs.values()),
        )


def nb_modifs_depuis_export() -> int:
    """Nombre d'enregistrements postérieurs au dernier export confirmé.

    Calculé depuis la base, et non depuis la session : une déconnexion du
    navigateur ne peut plus faire croire à tort que tout est sauvegardé.
    """
    ref = lire_meta("dernier_export", "1970-01-01T00:00:00")
    with connexion() as con:
        n = con.execute("SELECT COUNT(*) FROM suivi WHERE maj_le > ?", (ref,)).fetchone()[0]
        n += con.execute("SELECT COUNT(*) FROM suivi_adresses WHERE maj_le > ?", (ref,)).fetchone()[0]
    return int(n)


def reinitialiser_tout() -> None:
    """Efface tout le suivi (appels, référents et position). Irréversible."""
    with connexion() as con:
        con.execute("DELETE FROM suivi")
        con.execute("DELETE FROM suivi_adresses")
        con.execute("DELETE FROM meta")


# ─────────────────────────────────────────────────────────────────────────────
# SAUVEGARDE AUTOMATIQUE (filet de sécurité, en plus des exports manuels)
# ─────────────────────────────────────────────────────────────────────────────

def sauvegarde_auto() -> None:
    """Écrit une copie CSV du suivi à chaque enregistrement.

    Ne remplace pas l'export du soir : si le système de fichiers est éphémère,
    ce fichier disparaît avec la base. Il protège en revanche d'une corruption
    de la base ou d'une réinitialisation accidentelle.
    """
    try:
        jour = dt.date.today().isoformat()
        charger_suivi().to_csv(
            DOSSIER_SAUVEGARDES / f"suivi_{jour}.csv", index=False, sep=";", encoding="utf-8-sig"
        )
        charger_suivi_adresses().to_csv(
            DOSSIER_SAUVEGARDES / f"referents_{jour}.csv", index=False, sep=";", encoding="utf-8-sig"
        )
    except Exception:
        # Une sauvegarde qui échoue ne doit jamais bloquer la saisie.
        pass


# ─────────────────────────────────────────────────────────────────────────────
# IMPORT DES SAUVEGARDES CSV
# ─────────────────────────────────────────────────────────────────────────────

def lire_csv_robuste(fichier) -> pd.DataFrame:
    """Lit un CSV exporté par l'outil (séparateur ; et BOM UTF-8), repli sur la virgule."""
    brut = fichier.read()
    texte = brut.decode("utf-8-sig", errors="replace") if isinstance(brut, bytes) else brut.lstrip("\ufeff")
    premiere = texte.splitlines()[0] if texte.strip() else ""
    sep = ";" if premiere.count(";") >= premiere.count(",") else ","
    return pd.read_csv(io.StringIO(texte), sep=sep, dtype=str).fillna("")


def importer_suivi_clients_csv(fichier) -> int:
    df = lire_csv_robuste(fichier)
    m = {
        "code_client":  trouver_colonne(df.columns, "code_client", "code client"),
        "statut":       trouver_colonne(df.columns, "statut", "statut de l'appel"),
        "existe":       trouver_colonne(df.columns, "existe", "client actif ?"),
        "produits":     trouver_colonne(df.columns, "produits", "produits achetés"),
        "email_maj":    trouver_colonne(df.columns, "email_maj", "e-mail confirmé"),
        "tel_maj":      trouver_colonne(df.columns, "tel_maj", "téléphone confirmé"),
        "doublon_de":   trouver_colonne(df.columns, "doublon_de", "doublon du n°"),
        "motif_sortie": trouver_colonne(df.columns, "motif_sortie", "motif de sortie"),
        "rappel_date":  trouver_colonne(df.columns, "rappel_date", "à rappeler le"),
        "note":         trouver_colonne(df.columns, "note", "notes"),
    }
    if not m["code_client"]:
        raise ValueError("Le CSV de suivi clients doit contenir une colonne « code_client ».")

    def val(ligne, cle: str) -> str:
        col = m.get(cle)
        return str(ligne[col]).strip() if col else ""

    n = 0
    for _, ligne in df.iterrows():
        code = val(ligne, "code_client")
        if not code:
            continue
        produits = val(ligne, "produits").replace(";", ",")
        produits = "|".join(x.strip() for x in produits.split(",") if x.strip())
        enregistrer(
            code,
            statut=val(ligne, "statut") or "À appeler",
            existe=val(ligne, "existe"),
            produits=produits,
            email_maj=val(ligne, "email_maj"),
            tel_maj=val(ligne, "tel_maj"),
            doublon_de=val(ligne, "doublon_de"),
            motif_sortie=val(ligne, "motif_sortie"),
            note=val(ligne, "note"),
            # Correction : la date de rappel était écrasée à chaque restauration.
            rappel_date=vers_iso(val(ligne, "rappel_date")),
        )
        n += 1
    return n


def importer_suivi_adresses_csv(fichier) -> int:
    df = lire_csv_robuste(fichier)
    m = {
        "code_adresse": trouver_colonne(df.columns, "code_adresse", "code adresse"),
        "referent":     trouver_colonne(df.columns, "referent", "référent sur place"),
        "tel_site":     trouver_colonne(df.columns, "tel_site", "tél. référent / site", "tel. référent / site"),
        "statut_adr":   trouver_colonne(df.columns, "statut_adr", "statut vérification"),
        "note_adr":     trouver_colonne(df.columns, "note_adr", "note"),
    }
    if not m["code_adresse"]:
        raise ValueError("Le CSV des référents doit contenir une colonne « code_adresse ».")

    def val(ligne, cle: str) -> str:
        col = m.get(cle)
        return str(ligne[col]).strip() if col else ""

    n = 0
    for _, ligne in df.iterrows():
        code = val(ligne, "code_adresse")
        if not code:
            continue
        enregistrer_adresse(
            code,
            referent=val(ligne, "referent"),
            tel_site=val(ligne, "tel_site"),
            statut_adr=val(ligne, "statut_adr") or "À vérifier",
            note_adr=val(ligne, "note_adr"),
        )
        n += 1
    return n


# ─────────────────────────────────────────────────────────────────────────────
# FICHIER CLIENT — conservé au niveau du processus, pas de la session
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_resource(show_spinner=False)
def coffre_fichier() -> dict:
    """Conteneur partagé par toutes les sessions du processus.

    C'est la correction principale du symptôme « l'outil se réinitialise tout
    seul » : st.session_state est détruit dès que le websocket tombe (veille de
    l'ordinateur, onglet en arrière-plan, changement de réseau), alors qu'un
    cache de ressource survit à la reconnexion.
    """
    return {"contenu": None, "nom": "", "charge_le": ""}


@st.cache_data(show_spinner=False)
def lire_fichier(contenu: bytes):
    tampon = io.BytesIO(contenu)
    xls = pd.ExcelFile(tampon, engine="openpyxl")
    clients = pd.read_excel(xls, "Clients", dtype=str).fillna("")
    adresses = (
        pd.read_excel(xls, "Adresses livraison", dtype=str).fillna("")
        if "Adresses livraison" in xls.sheet_names
        else pd.DataFrame()
    )
    return clients, adresses


def normaliser_clients(clients: pd.DataFrame) -> tuple[pd.DataFrame, str | None]:
    """Harmonise les noms de colonnes attendus par le reste de l'outil."""
    col_code = trouver_colonne(clients.columns, "code client", "code_client")
    if col_code is None:
        return clients, None

    renoms = {}
    correspondances = [
        (("raison sociale / nom", "nom", "raison sociale"), "Raison sociale / Nom"),
        (("type client", "type"), "Type client"),
        (("ville",), "Ville"),
        (("catégorie normalisée", "catégorie", "categorie"), "Catégorie"),
        (("siren (9 chiffres)", "siren", "siren / siret"), "SIREN"),
        (("code postal", "code_postal"), "Code postal"),
    ]
    for sources, cible in correspondances:
        col = trouver_colonne(clients.columns, *sources)
        if col and col != cible:
            renoms[col] = cible

    for n in (1, 2, 3):
        col = trouver_colonne(clients.columns, f"téléphone {n} (norm)", f"téléphone {n}", f"telephone {n}")
        if col and col != f"Téléphone {n}":
            renoms[col] = f"Téléphone {n}"

    if renoms:
        clients = clients.rename(columns=renoms)
    # Le renommage peut créer des doublons de noms : on garde la première
    # occurrence, qui correspond à la version nettoyée.
    clients = clients.loc[:, ~clients.columns.duplicated(keep="first")]

    for c in [
        "Raison sociale / Nom", "Type client", "Ville", "Catégorie", "À compléter",
        "Email principal", "Email secondaire", "SIREN",
        "Téléphone 1", "Téléphone 2", "Téléphone 3",
        "Adresse 1", "Adresse 2", "Adresse 3", "Code postal",
    ]:
        if c not in clients.columns:
            clients[c] = ""

    return clients, col_code


def normaliser_adresses(adresses: pd.DataFrame) -> pd.DataFrame:
    if adresses.empty:
        return adresses
    col = trouver_colonne(adresses.columns, "code client mère", "code client mere")
    if col and col != "Code client mère":
        adresses = adresses.rename(columns={col: "Code client mère"})
    if "Code adresse" not in adresses.columns:
        col = trouver_colonne(adresses.columns, "code adresse") or trouver_colonne(adresses.columns, "code client")
        if col:
            adresses = adresses.rename(columns={col: "Code adresse"})
    for sources, cible in [
        (("téléphone 1 (norm)", "téléphone 1", "telephone 1"), "Téléphone"),
        (("code postal",), "Code postal"),
        (("nom site", "nom"), "Nom site"),
    ]:
        col = trouver_colonne(adresses.columns, *sources)
        if col and col != cible and cible not in adresses.columns:
            adresses = adresses.rename(columns={col: cible})
    adresses = adresses.loc[:, ~adresses.columns.duplicated(keep="first")]
    for c in ["Code adresse", "Code client mère", "Nom site", "Adresse 1", "Adresse 2",
              "Adresse 3", "Code postal", "Ville", "Téléphone"]:
        if c not in adresses.columns:
            adresses[c] = ""
    return adresses


def priorite(type_client: str) -> int:
    """Ordre d'appel : pros d'abord (enjeu de conformité), particuliers en dernier."""
    t = (type_client or "").lower()
    if t.startswith("pro") and "déduit" not in t:
        return 0
    if t.startswith("pro"):
        return 1
    if "déterminer" in t:
        return 2
    if "public" in t or "asso" in t:
        return 3
    return 4


# ─────────────────────────────────────────────────────────────────────────────
# PRÉPARATION DES EXPORTS
# ─────────────────────────────────────────────────────────────────────────────

def preparer_export(suivi_df: pd.DataFrame, base_df: pd.DataFrame, col_code: str) -> pd.DataFrame:
    """Tableau de suivi clients, lisible dans Excel."""
    df = suivi_df.copy().fillna("")
    if df.empty:
        return df

    infos = base_df[[col_code, "Raison sociale / Nom", "Ville", "Type client"]].copy()
    infos = infos.rename(columns={col_code: "code_client"})
    df = df.merge(infos, on="code_client", how="left").fillna("")

    df["produits"] = df["produits"].fillna("").str.replace("|", ", ", regex=False)
    df["rappel_date"] = df["rappel_date"].map(lambda v: jolie_date(v))
    df["maj_le"] = df["maj_le"].map(lambda v: jolie_date(v, avec_heure=True))

    colonnes = {
        "code_client": "Code client",
        "Raison sociale / Nom": "Nom / Raison sociale",
        "Ville": "Ville",
        "Type client": "Type",
        "statut": "Statut de l'appel",
        "existe": "Client actif ?",
        "produits": "Produits achetés",
        "email_maj": "E-mail confirmé",
        "tel_maj": "Téléphone confirmé",
        "doublon_de": "Doublon du n°",
        "motif_sortie": "Motif de sortie",
        "rappel_date": "À rappeler le",
        "note": "Notes",
        "maj_le": "Dernière mise à jour",
    }
    for c in colonnes:
        if c not in df.columns:
            df[c] = ""
    return df[list(colonnes)].rename(columns=colonnes)


def preparer_export_adresses(suivi_adr: pd.DataFrame, adresses_df: pd.DataFrame) -> pd.DataFrame:
    """Tableau des points de livraison et de leurs référents."""
    if adresses_df.empty:
        return pd.DataFrame()

    base_adr = adresses_df.copy()
    sortie = base_adr[["Code adresse", "Code client mère", "Nom site", "Code postal", "Ville"]].rename(
        columns={"Nom site": "Nom du site"}
    )

    if suivi_adr.empty:
        suivi_adr = pd.DataFrame(
            columns=["code_adresse", "referent", "tel_site", "statut_adr", "note_adr", "maj_le"]
        )
    sortie = sortie.merge(
        suivi_adr, left_on="Code adresse", right_on="code_adresse", how="left"
    ).fillna("")

    sortie["maj_le"] = sortie.get("maj_le", "").map(lambda v: jolie_date(v, avec_heure=True))
    sortie["statut_adr"] = sortie.get("statut_adr", "").replace("", "À vérifier")

    libelles = {
        "referent": "Référent sur place",
        "tel_site": "Tél. référent / site",
        "statut_adr": "Statut vérification",
        "note_adr": "Note",
        "maj_le": "Dernière mise à jour",
    }
    for c in libelles:
        if c not in sortie.columns:
            sortie[c] = ""
    ordre = ["Code adresse", "Code client mère", "Nom du site", "Code postal", "Ville",
             "referent", "tel_site", "statut_adr", "note_adr", "maj_le"]
    return sortie[ordre].rename(columns=libelles)


def vers_excel(df: pd.DataFrame, nom_feuille: str) -> bytes:
    """Classeur Excel mis en forme : en-tête vert, colonnes ajustées, filtres."""
    from openpyxl.styles import Alignment, Font, PatternFill

    tampon = io.BytesIO()
    with pd.ExcelWriter(tampon, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=nom_feuille)
        ws = writer.sheets[nom_feuille]
        for j, col in enumerate(df.columns, 1):
            cellule = ws.cell(row=1, column=j)
            cellule.fill = PatternFill("solid", fgColor=VERT_FONCE.lstrip("#"))
            cellule.font = Font(bold=True, color="FFFFFF")
            cellule.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            longueurs = df[col].astype(str).str.len().head(200)
            largeur = max(len(str(col)) + 2, int(longueurs.max() or 10) + 2)
            ws.column_dimensions[cellule.column_letter].width = min(largeur, 45)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
    return tampon.getvalue()


def calculer_rythme(suivi_df: pd.DataFrame) -> float | None:
    """Nombre moyen de fiches terminées par jour d'activité réelle."""
    if suivi_df.empty or "maj_le" not in suivi_df.columns:
        return None
    termines = suivi_df[suivi_df["statut"].isin(STATUTS_TERMINES)].copy()
    if termines.empty:
        return None
    termines["jour"] = pd.to_datetime(termines["maj_le"], errors="coerce").dt.date
    jours_actifs = termines["jour"].nunique()
    if not jours_actifs:
        return None
    return len(termines) / jours_actifs


# ─────────────────────────────────────────────────────────────────────────────
# EN-TÊTE ET CHARGEMENT DU FICHIER
# ─────────────────────────────────────────────────────────────────────────────

st.title("Suivi clients — mise à jour")
st.caption(
    "Outil de pilotage. La donnée de référence reste Logimatique ; "
    "cet outil suit l'avancement et donne le bon ordre d'appel."
)

coffre = coffre_fichier()

if coffre["contenu"] is None:
    televerse = st.file_uploader("Charger le fichier clients restructuré (.xlsx)", type=["xlsx"])
    if televerse is not None:
        coffre["contenu"] = televerse.getvalue()
        coffre["nom"] = televerse.name
        coffre["charge_le"] = maintenant_iso()
        st.rerun()
    st.info("⬆️ Charge le fichier **CLIENTS_HYMPYR_restructure.xlsx** pour démarrer.")
    st.stop()

barre1, barre2, barre3 = st.columns([1.4, 1.4, 4])
if barre1.button("🔄 Rafraîchir l'affichage", use_container_width=True,
                 help="Recalcule l'avancement à partir de l'état enregistré, sans recharger le fichier."):
    st.cache_data.clear()
    st.rerun()
if barre2.button("📂 Changer de fichier", use_container_width=True,
                 help="Charger un autre fichier clients. Le suivi des appels n'est pas effacé."):
    coffre["contenu"] = None
    coffre["nom"] = ""
    st.cache_data.clear()
    st.rerun()
barre3.caption(
    f"Fichier **{coffre['nom'] or 'chargé'}** en mémoire depuis {jolie_date(coffre['charge_le'], True) or '—'}. "
    "Il reste disponible même après une déconnexion du navigateur."
)

clients_bruts, adresses_brutes = lire_fichier(coffre["contenu"])
clients, col_code = normaliser_clients(clients_bruts)
if col_code is None:
    st.error(
        "La feuille « Clients » doit contenir une colonne « Code client » "
        f"(colonnes trouvées : {', '.join(map(str, clients_bruts.columns))})."
    )
    st.stop()
adresses = normaliser_adresses(adresses_brutes)


# ── Reprise du travail de la veille ──────────────────────────────────────────
with st.expander("🔄 Reprendre le travail de la veille (import de sauvegardes)", expanded=False):
    st.caption(
        "À utiliser si le suivi a été perdu, ou pour repartir d'une sauvegarde exportée. "
        "Si tes appels d'hier sont déjà visibles dans l'avancement, tu n'as rien à faire ici."
    )
    imp1, imp2 = st.columns(2)
    csv_clients = imp1.file_uploader("Sauvegarde SUIVI CLIENTS (CSV)", type=["csv"], key="imp_cli")
    csv_referents = imp2.file_uploader("Sauvegarde RÉFÉRENTS (CSV)", type=["csv"], key="imp_adr")
    if st.button("📥 Restaurer ces sauvegardes"):
        messages = []
        try:
            if csv_clients is not None:
                messages.append(f"{importer_suivi_clients_csv(csv_clients)} fiches clients restaurées")
            if csv_referents is not None:
                messages.append(f"{importer_suivi_adresses_csv(csv_referents)} référents restaurés")
            if messages:
                ecrire_meta("dernier_export", maintenant_iso())
                st.success("✅ " + " · ".join(messages) + ". Tu peux reprendre où tu t'étais arrêtée.")
                st.cache_data.clear()
            else:
                st.info("Aucun fichier sélectionné.")
        except Exception as exc:
            st.error(f"Import impossible : {exc}")


suivi = charger_suivi()
base = clients.merge(suivi, left_on=col_code, right_on="code_client", how="left")
for c in ["statut", "existe", "produits", "email_maj", "tel_maj", "note",
          "doublon_de", "rappel_date", "motif_sortie"]:
    if c in base.columns:
        base[c] = base[c].fillna("")
    else:
        base[c] = ""
base["statut"] = base["statut"].replace("", "À appeler")
base["priorite"] = base["Type client"].map(priorite)

modifs_en_attente = nb_modifs_depuis_export()


# ─────────────────────────────────────────────────────────────────────────────
# BARRE LATÉRALE
# ─────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    if modifs_en_attente > 0:
        st.error(
            f"⚠️ {modifs_en_attente} modification(s) non exportée(s).\n\n"
            "Pense à télécharger tes CSV avant de fermer (onglet Tableau de bord)."
        )
    else:
        st.success("✅ Travail exporté : rien en attente.")

    st.divider()
    st.header("Avancement")
    total = len(base)
    faits = int(base["statut"].isin(STATUTS_TERMINES).sum())
    reste = total - faits
    st.metric("Clients au total", formater_entier(total))
    st.metric("Traités", formater_entier(faits), f"{(100 * faits / total):.1f} %" if total else "—")
    st.metric("Restants", formater_entier(reste))

    rythme = calculer_rythme(suivi)
    if rythme:
        st.divider()
        st.caption("Projection au rythme observé")
        st.metric("Fiches traitées / jour actif", f"{rythme:.0f}")
        fin_estimee = date_apres_jours_ouvres(dt.date.today(), round(reste / rythme))
        st.metric("Fin estimée", fin_estimee.strftime("%d/%m/%Y"))
        if rythme < 1:
            st.warning("Rythme très faible : la projection est indicative.")

    st.divider()
    st.caption(f"Objectif pour le {DEADLINE.strftime('%d/%m/%Y')}")
    jo = jours_ouvres(dt.date.today(), DEADLINE)
    if jo <= 0:
        st.error("Échéance atteinte ou dépassée.")
    else:
        objectif = -(-reste // jo)
        st.metric("Jours ouvrés restants", str(jo))
        st.metric("À traiter / jour pour tenir", str(objectif))
        if rythme:
            if rythme >= objectif:
                st.success(f"Rythme actuel ({rythme:.0f}/j) au-dessus de l'objectif. Dans les temps.")
            else:
                st.error(
                    f"Rythme actuel ({rythme:.0f}/j) sous l'objectif de ~{objectif - rythme:.0f}/j. "
                    "Il faut accélérer ou renforcer l'équipe."
                )

    st.divider()
    st.header("🔎 Accès direct")
    code_direct = st.text_input("Code client exact", help="Tape un code client pour aller droit à sa fiche.")

    st.divider()
    st.header("Filtres")
    f_type = st.multiselect("Type de client", sorted(base["Type client"].unique()))
    f_statut = st.multiselect("Statut d'appel", STATUTS, default=["À appeler", "À rappeler"])
    f_acompl = st.checkbox("Uniquement « À compléter » non vide", value=False)
    recherche = st.text_input("Recherche (nom, code, ville)")
    tri_priorite = st.checkbox("Trier par priorité (pros d'abord)", value=True)

    st.divider()
    with st.expander("⚙️ Réinitialisation (zone sensible)"):
        st.caption(
            "Efface tout le suivi : appels et référents. Action irréversible. "
            "Exporte tes CSV avant, par sécurité."
        )
        confirme = st.checkbox("Je comprends que tout sera effacé sans retour possible")
        mot_confirm = st.text_input("Pour confirmer, écris RESET ci-dessous", value="")
        if st.button("🗑️ Tout réinitialiser",
                     disabled=not (confirme and mot_confirm.strip().upper() == "RESET")):
            reinitialiser_tout()
            for cle in ("idx", "idx_adr"):
                st.session_state.pop(cle, None)
            st.cache_data.clear()
            st.success("Suivi réinitialisé. L'outil repart d'une base vierge.")
            st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# CONSTRUCTION DE LA FILE D'APPEL
# ─────────────────────────────────────────────────────────────────────────────

file_appel = base.copy()
acces_direct = bool(code_direct.strip())

if acces_direct:
    cible = code_direct.strip().upper()
    direct = base[base[col_code].astype(str).str.upper() == cible]
    if direct.empty:
        st.sidebar.error(f"Aucun client avec le code « {code_direct} ».")
        acces_direct = False
    else:
        file_appel = direct.reset_index(drop=True)
        st.session_state.idx = 0

if not acces_direct:
    if f_type:
        file_appel = file_appel[file_appel["Type client"].isin(f_type)]
    if f_statut:
        file_appel = file_appel[file_appel["statut"].isin(f_statut)]
    if f_acompl and "À compléter" in file_appel.columns:
        file_appel = file_appel[file_appel["À compléter"].astype(str).str.strip() != ""]
    if recherche:
        r = recherche.lower()
        masque = (
            file_appel[col_code].astype(str).str.lower().str.contains(r, na=False)
            | file_appel["Raison sociale / Nom"].str.lower().str.contains(r, na=False)
            | file_appel["Ville"].str.lower().str.contains(r, na=False)
        )
        file_appel = file_appel[masque]
    tri = ["priorite", "Raison sociale / Nom"] if tri_priorite else ["Raison sociale / Nom"]
    file_appel = file_appel.sort_values(tri).reset_index(drop=True)


onglet_appel, onglet_adr, onglet_dash = st.tabs(
    ["☎️  Appels clients", "📦  Points de livraison", "📊  Tableau de bord"]
)


# ── ONGLET APPELS ────────────────────────────────────────────────────────────
with onglet_appel:
    if file_appel.empty:
        st.success("Aucun client dans la file avec ces filtres. 🎉")
        st.caption("Modifie les filtres dans la barre latérale, ou passe au tableau de bord pour exporter.")
    else:
        # Reprise de position : mémorisée en base, donc conservée après une
        # déconnexion du navigateur.
        if "idx" not in st.session_state:
            derniere = lire_meta("derniere_fiche", "")
            positions = file_appel.index[file_appel[col_code].astype(str) == derniere].tolist()
            st.session_state.idx = positions[0] if positions else 0
        st.session_state.idx = max(0, min(st.session_state.idx, len(file_appel) - 1))

        nav1, nav2, nav3 = st.columns([1, 2, 1])
        if nav1.button("⬅️ Précédent", use_container_width=True):
            st.session_state.idx = max(0, st.session_state.idx - 1)
            st.rerun()
        if nav3.button("Suivant ➡️", use_container_width=True):
            st.session_state.idx = min(len(file_appel) - 1, st.session_state.idx + 1)
            st.rerun()
        nav2.markdown(
            f"<div style='text-align:center;font-weight:600;color:{VERT}'>"
            f"Fiche {st.session_state.idx + 1} / {len(file_appel)}</div>",
            unsafe_allow_html=True,
        )

        ligne = file_appel.iloc[st.session_state.idx]
        code = str(ligne[col_code])
        ecrire_meta("derniere_fiche", code)

        gauche, droite = st.columns([3, 2])

        with gauche:
            st.markdown(f"### {ligne['Raison sociale / Nom']}")
            st.markdown(
                f"<span class='pill'>{ligne['Type client']}</span>"
                f"<span class='pill pill-orange'>{ligne.get('Catégorie', '')}</span>"
                f"<span style='color:#5a6b62'>Code {code}</span>",
                unsafe_allow_html=True,
            )
            adresse = " ".join(
                x for x in [ligne.get("Adresse 1", ""), ligne.get("Adresse 2", ""), ligne.get("Adresse 3", "")] if x
            )
            email_sec = ligne.get("Email secondaire", "")
            st.markdown(
                f"""<div class='fiche'>
                📍 {adresse}<br>{ligne.get('Code postal', '')} {ligne.get('Ville', '')}<br><br>
                ☎️ {ligne.get('Téléphone 1', '')} &nbsp; {ligne.get('Téléphone 2', '')} &nbsp; {ligne.get('Téléphone 3', '')}<br>
                ✉️ {ligne.get('Email principal', '') or '<i>aucun e-mail</i>'}{(' · ' + email_sec) if email_sec else ''}<br>
                🏢 SIREN : {ligne.get('SIREN', '') or '<i>—</i>'}
                </div>""",
                unsafe_allow_html=True,
            )
            if ligne.get("À compléter", ""):
                st.warning(f"À compléter : {ligne['À compléter']}")

            if not adresses.empty:
                liees = adresses[adresses["Code client mère"] == code]
                if not liees.empty:
                    with st.expander(f"📦 {len(liees)} adresse(s) de livraison rattachée(s)"):
                        st.dataframe(
                            liees[["Code adresse", "Nom site", "Adresse 1", "Code postal", "Ville"]],
                            hide_index=True, use_container_width=True,
                        )

        with droite:
            st.markdown("#### Résultat de l'appel")
            produits_init = [p for p in str(ligne.get("produits") or "").split("|") if p in PRODUITS]
            rappel_init = None
            if ligne.get("rappel_date"):
                try:
                    rappel_init = pd.to_datetime(ligne["rappel_date"]).date()
                except Exception:
                    rappel_init = None

            # Chaque widget porte une clé incluant le code client : Streamlit crée
            # un widget neuf par fiche, sinon l'état d'une fiche déborde sur la
            # suivante et les saisies ne se remettent pas à zéro.
            with st.form(f"appel_{code}", clear_on_submit=False):
                statut = st.selectbox(
                    "Statut", STATUTS,
                    index=STATUTS.index(ligne["statut"]) if ligne["statut"] in STATUTS else 0,
                    key=f"statut_{code}",
                )
                existe = st.radio(
                    "Client toujours actif ?", ["Oui", "Non", "Incertain"], horizontal=True,
                    index=["Oui", "Non", "Incertain"].index(ligne.get("existe"))
                    if ligne.get("existe") in ("Oui", "Non", "Incertain") else 0,
                    key=f"existe_{code}",
                )
                produits = st.multiselect("Produits achetés", PRODUITS, default=produits_init,
                                          key=f"produits_{code}")
                email_maj = st.text_input("E-mail confirmé / corrigé", value=ligne.get("email_maj") or "",
                                          key=f"email_{code}")
                tel_maj = st.text_input("Téléphone confirmé / corrigé", value=ligne.get("tel_maj") or "",
                                        key=f"tel_{code}")
                doublon_de = st.text_input(
                    "Doublon du client n°", value=ligne.get("doublon_de") or "",
                    help="Si ce client est un doublon, indiquer le code à conserver.",
                    key=f"doublon_{code}",
                )
                motif_sortie = st.selectbox(
                    "Motif de sortie (si ancien client)", MOTIFS_SORTIE,
                    index=MOTIFS_SORTIE.index(ligne.get("motif_sortie"))
                    if ligne.get("motif_sortie") in MOTIFS_SORTIE else 0,
                    help="À renseigner si le statut est « Ancien client (à sortir) ».",
                    key=f"motif_{code}",
                )
                rappel = st.date_input("Date de rappel (si applicable)", value=rappel_init,
                                       key=f"rappel_{code}")
                note = st.text_area("Notes (commercial, vérifications…)", value=ligne.get("note") or "",
                                    height=90, key=f"note_{code}")
                valide = st.form_submit_button("💾 Enregistrer & passer au suivant",
                                               use_container_width=True, type="primary")

            if valide:
                erreurs = []
                if email_maj.strip() and not re.match(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$", email_maj.strip()):
                    erreurs.append("l'adresse e-mail ne semble pas valide")
                if statut == "Doublon" and not doublon_de.strip():
                    erreurs.append("indique le code du client à conserver")
                if statut == "Ancien client (à sortir)" and motif_sortie == "—":
                    erreurs.append("indique le motif de sortie")
                if statut == "À rappeler" and not rappel:
                    erreurs.append("indique une date de rappel")

                if erreurs:
                    st.error("Avant d'enregistrer : " + ", ".join(erreurs) + ".")
                else:
                    enregistrer(
                        code,
                        statut=statut,
                        existe=existe,
                        produits="|".join(produits),
                        email_maj=email_maj.strip(),
                        tel_maj=tel_maj.strip(),
                        doublon_de=doublon_de.strip(),
                        note=note.strip(),
                        motif_sortie="" if motif_sortie == "—" else motif_sortie,
                        rappel_date=rappel.isoformat() if rappel else "",
                    )
                    sauvegarde_auto()
                    st.session_state.idx = min(len(file_appel) - 1, st.session_state.idx + 1)
                    st.rerun()


# ── ONGLET POINTS DE LIVRAISON ───────────────────────────────────────────────
with onglet_adr:
    if adresses.empty:
        st.info("Le fichier ne contient pas de feuille « Adresses livraison ».")
    else:
        st.subheader("Vérification des points de livraison")
        st.caption("Pour chaque adresse rattachée à une entreprise : qui est le référent sur place ?")

        suivi_adr = charger_suivi_adresses()
        adr = adresses.merge(suivi_adr, left_on="Code adresse", right_on="code_adresse", how="left")
        for c in ["referent", "tel_site", "statut_adr", "note_adr"]:
            adr[c] = adr[c].fillna("") if c in adr.columns else ""
        adr["statut_adr"] = adr["statut_adr"].replace("", "À vérifier")

        m1, m2, m3 = st.columns(3)
        m1.metric("Points de livraison", len(adr))
        m2.metric("Vérifiés", int((adr["statut_adr"] == "Vérifié ✅").sum()))
        m3.metric("Restants", int((adr["statut_adr"] != "Vérifié ✅").sum()))

        rech1, rech2 = st.columns(2)
        q_adr = rech1.text_input("🔎 Code adresse exact (ex. 12771L56)")
        q_mere = rech2.text_input("🔎 ou Code client mère (montre tous ses points)")

        vue = adr.copy()
        if q_adr.strip():
            vue = vue[vue["Code adresse"].astype(str).str.upper() == q_adr.strip().upper()]
        elif q_mere.strip():
            vue = vue[vue["Code client mère"].astype(str).str.upper() == q_mere.strip().upper()]
        else:
            filtre_adr = st.multiselect("Statut", STATUTS_ADRESSE, default=["À vérifier"])
            if filtre_adr:
                vue = vue[vue["statut_adr"].isin(filtre_adr)]
        vue = vue.reset_index(drop=True)

        if vue.empty:
            st.success("Aucun point de livraison à afficher avec ce filtre.")
        else:
            if "idx_adr" not in st.session_state:
                st.session_state.idx_adr = 0
            st.session_state.idx_adr = max(0, min(st.session_state.idx_adr, len(vue) - 1))

            n1, n2, n3 = st.columns([1, 2, 1])
            if n1.button("⬅️ Précédent", key="adr_prev", use_container_width=True):
                st.session_state.idx_adr = max(0, st.session_state.idx_adr - 1)
                st.rerun()
            if n3.button("Suivant ➡️", key="adr_next", use_container_width=True):
                st.session_state.idx_adr = min(len(vue) - 1, st.session_state.idx_adr + 1)
                st.rerun()
            n2.markdown(
                f"<div style='text-align:center;font-weight:600;color:{VERT}'>"
                f"Point {st.session_state.idx_adr + 1} / {len(vue)}</div>",
                unsafe_allow_html=True,
            )

            point = vue.iloc[st.session_state.idx_adr]
            code_adr = str(point["Code adresse"])
            adresse_txt = " ".join(
                x for x in [point.get("Adresse 1", ""), point.get("Adresse 2", ""), point.get("Adresse 3", "")] if x
            )

            g, d = st.columns([3, 2])
            with g:
                st.markdown(f"### {point.get('Nom site', '') or 'Point de livraison'}")
                st.markdown(
                    f"<span class='pill'>Adresse {code_adr}</span>"
                    f"<span style='color:#5a6b62'>Client mère : {point.get('Code client mère', '')}</span>",
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f"""<div class='fiche'>
                    📍 {adresse_txt}<br>{point.get('Code postal', '')} {point.get('Ville', '')}<br><br>
                    ☎️ {point.get('Téléphone', '') or '<i>aucun téléphone</i>'}
                    </div>""",
                    unsafe_allow_html=True,
                )
            with d:
                st.markdown("#### Référent du site")
                with st.form(f"adr_form_{code_adr}"):
                    referent = st.text_input("Nom du référent sur place", value=point.get("referent") or "",
                                             key=f"referent_{code_adr}")
                    tel_site = st.text_input("Téléphone du site / référent", value=point.get("tel_site") or "",
                                             key=f"tel_site_{code_adr}")
                    statut_adr = st.selectbox(
                        "Statut", STATUTS_ADRESSE,
                        index=STATUTS_ADRESSE.index(point["statut_adr"])
                        if point["statut_adr"] in STATUTS_ADRESSE else 0,
                        key=f"statut_adr_{code_adr}",
                    )
                    note_adr = st.text_area("Note", value=point.get("note_adr") or "", height=80,
                                            key=f"note_adr_{code_adr}")
                    valide_adr = st.form_submit_button("💾 Enregistrer & suivant",
                                                       use_container_width=True, type="primary")
                if valide_adr:
                    enregistrer_adresse(
                        code_adr,
                        referent=referent.strip(),
                        tel_site=tel_site.strip(),
                        statut_adr=statut_adr,
                        note_adr=note_adr.strip(),
                    )
                    sauvegarde_auto()
                    st.session_state.idx_adr = min(len(vue) - 1, st.session_state.idx_adr + 1)
                    st.rerun()


# ── ONGLET TABLEAU DE BORD ───────────────────────────────────────────────────
with onglet_dash:
    if modifs_en_attente > 0:
        st.warning(
            f"🔔 Sauvegarde : {modifs_en_attente} modification(s) à exporter. "
            "Télécharge les CSV ci-dessous avant de fermer l'onglet."
        )

    st.subheader("🎯 Objectif pour tenir l'échéance")
    st.caption(
        f"Échéance : émission de la facturation électronique au {DEADLINE.strftime('%d/%m/%Y')} pour les PME."
    )

    perimetre = st.radio(
        "Périmètre à boucler",
        ["Tous les clients restants", "Uniquement les pros (conformité)"],
        horizontal=True,
    )
    masque = (
        base["Type client"].astype(str).str.startswith("Pro")
        if perimetre.startswith("Uniquement")
        else pd.Series(True, index=base.index)
    )
    restant_perimetre = int((masque & ~base["statut"].isin(STATUTS_TERMINES)).sum())
    jo = jours_ouvres(dt.date.today(), DEADLINE)

    o1, o2, o3, o4 = st.columns(4)
    o1.metric("Restant sur ce périmètre", formater_entier(restant_perimetre))
    o2.metric("Jours ouvrés d'ici l'échéance", str(jo))

    if jo > 0:
        objectif = -(-restant_perimetre // jo)
        o3.metric("À traiter / jour", str(objectif))
        if rythme:
            o4.metric("Rythme actuel / jour", f"{rythme:.0f}", delta=f"{rythme - objectif:+.0f} vs objectif")
            if rythme >= objectif:
                st.success(
                    f"✅ Au rythme actuel ({rythme:.0f}/jour), l'échéance est tenable sur ce périmètre."
                )
            else:
                fin_projetee = date_apres_jours_ouvres(dt.date.today(), round(restant_perimetre / rythme))
                st.error(
                    f"⚠️ Au rythme actuel ({rythme:.0f}/jour), fin estimée vers le "
                    f"{fin_projetee.strftime('%d/%m/%Y')}, soit après l'échéance. "
                    f"Il faut viser {objectif}/jour, ou renforcer l'équipe."
                )
        else:
            o4.metric("Rythme actuel / jour", "—")
            st.info("Le rythme s'affichera après les premiers appels enregistrés.")
    else:
        o3.metric("À traiter / jour", "—")
        st.error("L'échéance est atteinte ou dépassée.")

    st.divider()
    st.subheader("Avancement de la campagne")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total clients", formater_entier(len(base)))
    c2.metric("Traités", formater_entier(int(base["statut"].isin(STATUTS_TERMINES).sum())))
    c3.metric("À rappeler", formater_entier(int((base["statut"] == "À rappeler").sum())))
    c4.metric("Doublons repérés", formater_entier(int((base["statut"] == "Doublon").sum())))

    st.markdown("##### Répartition par statut")
    st.bar_chart(base["statut"].value_counts())

    st.markdown("##### Répartition par type de client")
    st.bar_chart(base["Type client"].value_counts())

    if not suivi.empty and suivi["produits"].str.len().gt(0).any():
        eclate = suivi["produits"].str.split("|").explode()
        eclate = eclate[eclate.isin(PRODUITS)]
        if not eclate.empty:
            st.markdown("##### Produits achetés (déclarés en appel)")
            st.bar_chart(eclate.value_counts())

    # Rappels du jour et en retard : la question qu'on se pose chaque matin.
    rappels = base[(base["statut"] == "À rappeler") & (base["rappel_date"].astype(str).str.len() > 0)].copy()
    if not rappels.empty:
        rappels["date"] = pd.to_datetime(rappels["rappel_date"], errors="coerce").dt.date
        aujourdhui = dt.date.today()
        a_faire = rappels[rappels["date"].notna() & (rappels["date"] <= aujourdhui)]
        if not a_faire.empty:
            st.divider()
            st.markdown(f"##### ⏰ {len(a_faire)} rappel(s) à passer aujourd'hui ou en retard")
            st.dataframe(
                a_faire[[col_code, "Raison sociale / Nom", "Ville", "Téléphone 1", "rappel_date", "note"]]
                .rename(columns={col_code: "Code client", "rappel_date": "À rappeler le", "note": "Notes"}),
                hide_index=True, use_container_width=True,
            )

    st.divider()
    st.markdown("##### Export du suivi (sauvegarde et reporting)")
    st.caption("Trace de la campagne, lisible dans Excel. La donnée de référence reste Logimatique.")

    if suivi.empty:
        st.info("Aucun appel enregistré pour le moment.")
    else:
        export = preparer_export(suivi, base, col_code)
        exp1, exp2 = st.columns(2)
        exp1.download_button(
            "⬇️ Export CSV (Excel FR)",
            export.to_csv(index=False, sep=";").encode("utf-8-sig"),
            file_name=f"suivi_appels_hympyr_{dt.date.today():%Y%m%d}.csv",
            mime="text/csv",
            use_container_width=True,
        )
        exp2.download_button(
            "⬇️ Export Excel (.xlsx)",
            vers_excel(export, "Suivi appels"),
            file_name=f"suivi_appels_hympyr_{dt.date.today():%Y%m%d}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
        with st.expander("Aperçu de l'export clients"):
            st.dataframe(export, hide_index=True, use_container_width=True)

    if not adresses.empty:
        st.divider()
        st.markdown("##### Export des points de livraison (référents)")
        export_adr = preparer_export_adresses(charger_suivi_adresses(), adresses)
        if export_adr.empty:
            st.info("Aucun point de livraison à exporter.")
        else:
            ea1, ea2 = st.columns(2)
            ea1.download_button(
                "⬇️ Points de livraison — CSV (Excel FR)",
                export_adr.to_csv(index=False, sep=";").encode("utf-8-sig"),
                file_name=f"points_livraison_hympyr_{dt.date.today():%Y%m%d}.csv",
                mime="text/csv",
                use_container_width=True,
            )
            ea2.download_button(
                "⬇️ Points de livraison — Excel (.xlsx)",
                vers_excel(export_adr, "Points de livraison"),
                file_name=f"points_livraison_hympyr_{dt.date.today():%Y%m%d}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
            with st.expander("Aperçu de l'export points de livraison"):
                st.dataframe(export_adr, hide_index=True, use_container_width=True)

    if modifs_en_attente > 0:
        st.caption("Une fois tes fichiers téléchargés, confirme pour repasser au vert :")
        if st.button("✅ J'ai bien téléchargé mes sauvegardes"):
            ecrire_meta("dernier_export", maintenant_iso())
            st.rerun()
