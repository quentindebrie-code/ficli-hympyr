# -*- coding: utf-8 -*-
"""
HYMPYR ÉNERGIES — Cockpit de campagne d'appels clients
------------------------------------------------------
Outil de PILOTAGE (mono-utilisateur). La saisie des données reste faite
directement dans Logimatique ; cet outil ne fait que :
  - présenter une liste d'appels priorisée et dédoublonnée,
  - afficher une fiche complète par client (+ ses adresses de livraison),
  - capturer le résultat de chaque appel (existence, produits, statut, notes),
  - suivre l'avancement et projeter une date de fin réaliste.

Le fichier client n'est jamais modifié. L'état des appels est stocké
localement dans une petite base SQLite (suivi_appels.db), à côté du script.

Lancement :
    pip install streamlit pandas openpyxl
    streamlit run app.py
"""

import io
import sqlite3
import datetime as dt
from pathlib import Path

import pandas as pd
import streamlit as st

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
DB_PATH = Path(__file__).parent / "suivi_appels.db"

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

# Motifs de sortie quand le client n'est plus à conserver
MOTIFS_SORTIE = [
    "—", "Passé à la concurrence", "Utilise une autre énergie",
    "Décès", "Cessation d'activité / fermeture", "Ne souhaite plus être contacté",
    "Injoignable définitivement", "Autre",
]

# Deadline réglementaire (émission de la facturation électronique pour les PME)
DEADLINE = dt.date(2027, 9, 1)

def jours_ouvres(debut: dt.date, fin: dt.date) -> int:
    """Nombre de jours ouvrés (lun-ven) entre deux dates, fin exclue."""
    if fin <= debut:
        return 0
    n = 0
    d = debut
    while d < fin:
        if d.weekday() < 5:  # 0=lundi … 4=vendredi
            n += 1
        d += dt.timedelta(days=1)
    return n

st.set_page_config(page_title="Cockpit appels — Hympyr", page_icon="📞", layout="wide")

# Compteur de modifications depuis le dernier export (garde-fou anti-perte)
if "modifs_non_sauvees" not in st.session_state:
    st.session_state.modifs_non_sauvees = 0

def marquer_modif():
    st.session_state.modifs_non_sauvees += 1

st.markdown(f"""
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
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# BASE DE SUIVI (SQLite)
# ─────────────────────────────────────────────────────────────────────────────
def db():
    con = sqlite3.connect(DB_PATH)
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
    # migration : colonnes ajoutées après coup
    cols_exist = {r[1] for r in con.execute("PRAGMA table_info(suivi)").fetchall()}
    for c in ("motif_sortie",):
        if c not in cols_exist:
            con.execute(f"ALTER TABLE suivi ADD COLUMN {c} TEXT")
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
    con.commit()
    return con


def charger_suivi() -> pd.DataFrame:
    con = db()
    df = pd.read_sql("SELECT * FROM suivi", con, dtype=str)
    con.close()
    return df


def enregistrer(code, **champs):
    con = db()
    champs["code_client"] = str(code)
    champs["maj_le"] = dt.datetime.now().isoformat(timespec="seconds")
    cols = ",".join(champs.keys())
    ph = ",".join("?" for _ in champs)
    upd = ",".join(f"{k}=excluded.{k}" for k in champs if k != "code_client")
    con.execute(
        f"INSERT INTO suivi ({cols}) VALUES ({ph}) "
        f"ON CONFLICT(code_client) DO UPDATE SET {upd}",
        list(champs.values()),
    )
    con.commit()
    con.close()


def enregistrer_adresse(code_adresse, **champs):
    con = db()
    champs["code_adresse"] = str(code_adresse)
    champs["maj_le"] = dt.datetime.now().isoformat(timespec="seconds")
    cols = ",".join(champs.keys())
    ph = ",".join("?" for _ in champs)
    upd = ",".join(f"{k}=excluded.{k}" for k in champs if k != "code_adresse")
    con.execute(
        f"INSERT INTO suivi_adresses ({cols}) VALUES ({ph}) "
        f"ON CONFLICT(code_adresse) DO UPDATE SET {upd}",
        list(champs.values()),
    )
    con.commit()
    con.close()


def charger_suivi_adresses() -> pd.DataFrame:
    con = db()
    df = pd.read_sql("SELECT * FROM suivi_adresses", con, dtype=str)
    con.close()
    return df


def _retrouver(colnames, *cibles):
    """Retrouve un nom de colonne quelle que soit la casse/les espaces."""
    norm = {str(c).strip().lower(): c for c in colnames}
    for cible in cibles:
        if cible in norm:
            return norm[cible]
    return None


def _lire_csv_robuste(file) -> pd.DataFrame:
    """Lit un CSV exporté par l'outil (séparateur ; et BOM UTF-8), avec repli sur la virgule."""
    raw = file.read()
    if isinstance(raw, bytes):
        texte = raw.decode("utf-8-sig", errors="replace")
    else:
        texte = raw.lstrip("\ufeff")
    # On choisit le séparateur le plus présent sur la 1re ligne
    premiere = texte.splitlines()[0] if texte.strip() else ""
    sep = ";" if premiere.count(";") >= premiere.count(",") else ","
    return pd.read_csv(io.StringIO(texte), sep=sep, dtype=str).fillna("")


def importer_suivi_clients_csv(file) -> int:
    """Réinjecte un CSV de suivi clients (export du soir) dans la base. Renvoie le nb de lignes."""
    df = _lire_csv_robuste(file)
    # accepter aussi bien les noms techniques que les libellés français de l'export
    m = {
        "code_client":  _retrouver(df.columns, "code_client", "code client"),
        "statut":       _retrouver(df.columns, "statut", "statut de l'appel"),
        "existe":       _retrouver(df.columns, "existe", "client actif ?"),
        "produits":     _retrouver(df.columns, "produits", "produits achetés"),
        "email_maj":    _retrouver(df.columns, "email_maj", "e-mail confirmé"),
        "tel_maj":      _retrouver(df.columns, "tel_maj", "téléphone confirmé"),
        "doublon_de":   _retrouver(df.columns, "doublon_de", "doublon du n°"),
        "motif_sortie": _retrouver(df.columns, "motif_sortie", "motif de sortie"),
        "rappel_date":  _retrouver(df.columns, "rappel_date", "à rappeler le"),
        "note":         _retrouver(df.columns, "note", "notes"),
    }
    if not m["code_client"]:
        raise ValueError("Le CSV de suivi clients doit contenir une colonne « code_client ».")
    def val(r, key):
        col = m.get(key)
        return str(r[col]).strip() if col else ""
    n = 0
    for _, r in df.iterrows():
        code = val(r, "code_client")
        if not code:
            continue
        prod = val(r, "produits")
        prod = "|".join(x.strip() for x in prod.replace(";", ",").split(",") if x.strip())
        enregistrer(
            code,
            statut=val(r, "statut") or "À appeler",
            existe=val(r, "existe"),
            produits=prod,
            email_maj=val(r, "email_maj"),
            tel_maj=val(r, "tel_maj"),
            doublon_de=val(r, "doublon_de"),
            motif_sortie=val(r, "motif_sortie"),
            note=val(r, "note"),
            rappel_date="",
        )
        n += 1
    return n


def importer_suivi_adresses_csv(file) -> int:
    """Réinjecte un CSV de référents (export du soir) dans la base. Renvoie le nb de lignes."""
    df = _lire_csv_robuste(file)
    m = {
        "code_adresse": _retrouver(df.columns, "code_adresse", "code adresse"),
        "referent":     _retrouver(df.columns, "referent", "référent sur place"),
        "tel_site":     _retrouver(df.columns, "tel_site", "tél. référent / site", "tel. référent / site"),
        "statut_adr":   _retrouver(df.columns, "statut_adr", "statut vérification"),
        "note_adr":     _retrouver(df.columns, "note_adr", "note"),
    }
    if not m["code_adresse"]:
        raise ValueError("Le CSV des référents doit contenir une colonne « code_adresse ».")
    def val(r, key):
        col = m.get(key)
        return str(r[col]).strip() if col else ""
    n = 0
    for _, r in df.iterrows():
        cad = val(r, "code_adresse")
        if not cad:
            continue
        enregistrer_adresse(
            cad,
            referent=val(r, "referent"),
            tel_site=val(r, "tel_site"),
            statut_adr=val(r, "statut_adr") or "À vérifier",
            note_adr=val(r, "note_adr"),
        )
        n += 1
    return n


# ─────────────────────────────────────────────────────────────────────────────
# CHARGEMENT DU FICHIER CLIENT
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def lire_fichier(file_bytes: bytes):
    buffer = io.BytesIO(file_bytes)
    xls = pd.ExcelFile(buffer, engine="openpyxl")
    clients = pd.read_excel(xls, "Clients", dtype=str).fillna("")
    adresses = (pd.read_excel(xls, "Adresses livraison", dtype=str).fillna("")
                if "Adresses livraison" in xls.sheet_names else pd.DataFrame())
    return clients, adresses


def priorite(type_client: str) -> int:
    t = (type_client or "").lower()
    if t.startswith("pro") and "déduit" not in t:
        return 0
    if t.startswith("pro"):
        return 1
    if "déterminer" in t:
        return 2
    if "public" in t or "asso" in t:
        return 3
    return 4  # particuliers en dernier (hors enjeu conformité)


# ─────────────────────────────────────────────────────────────────────────────
# PRÉPARATION DE L'EXPORT (lisible, en français)
# ─────────────────────────────────────────────────────────────────────────────
def preparer_export(suivi_df: pd.DataFrame, base_df: pd.DataFrame) -> pd.DataFrame:
    """Construit un tableau de suivi propre et lisible pour Excel."""
    df = suivi_df.copy().fillna("")

    # Récupérer le nom et la ville du client depuis la base
    infos = base_df[[col_code, "Raison sociale / Nom", "Ville", "Type client"]].copy()
    infos = infos.rename(columns={col_code: "code_client"})
    df = df.merge(infos, on="code_client", how="left")

    # Produits : remplacer le séparateur technique | par une virgule lisible
    df["produits"] = df["produits"].fillna("").str.replace("|", ", ", regex=False)

    # Dates lisibles JJ/MM/AAAA + heure pour la dernière maj
    def jolie_date(x, avec_heure=False):
        x = str(x).strip()
        if not x:
            return ""
        try:
            d = pd.to_datetime(x)
            return d.strftime("%d/%m/%Y %H:%M") if avec_heure else d.strftime("%d/%m/%Y")
        except Exception:
            return x
    df["rappel_date"] = df["rappel_date"].map(lambda v: jolie_date(v))
    df["maj_le"] = df["maj_le"].map(lambda v: jolie_date(v, avec_heure=True))

    # Ordre et libellés français
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
    df = df[list(colonnes.keys())].rename(columns=colonnes)
    return df


def preparer_export_adresses(suivi_adr: pd.DataFrame, adresses_df: pd.DataFrame) -> pd.DataFrame:
    """Tableau lisible des points de livraison + référents vérifiés."""
    if adresses_df.empty:
        return pd.DataFrame()
    base_adr = adresses_df.copy()
    code_adr = "Code adresse" if "Code adresse" in base_adr.columns else base_adr.columns[0]
    mere = "Code client mère" if "Code client mère" in base_adr.columns else ""
    cols_src = {
        code_adr: "Code adresse",
        mere: "Code client mère",
        "Nom site": "Nom du site",
        "Ville": "Ville",
        "Code postal": "Code postal",
    }
    cols_src = {k: v for k, v in cols_src.items() if k and k in base_adr.columns}
    out = base_adr[list(cols_src.keys())].rename(columns=cols_src)

    sa = suivi_adr.copy().fillna("") if not suivi_adr.empty else pd.DataFrame(
        columns=["code_adresse", "referent", "tel_site", "statut_adr", "note_adr", "maj_le"])
    out = out.merge(sa, left_on="Code adresse", right_on="code_adresse", how="left").fillna("")

    def jolie(x):
        x = str(x).strip()
        if not x:
            return ""
        try:
            return pd.to_datetime(x).strftime("%d/%m/%Y %H:%M")
        except Exception:
            return x
    out["maj_le"] = out.get("maj_le", "").map(jolie)
    out["statut_adr"] = out.get("statut_adr", "").replace("", "À vérifier")

    libelles = {
        "referent": "Référent sur place",
        "tel_site": "Tél. référent / site",
        "statut_adr": "Statut vérification",
        "note_adr": "Note",
        "maj_le": "Dernière mise à jour",
    }
    for c in libelles:
        if c not in out.columns:
            out[c] = ""
    ordre = ["Code adresse", "Code client mère", "Nom du site", "Code postal", "Ville",
             "referent", "tel_site", "statut_adr", "note_adr", "maj_le"]
    ordre = [c for c in ordre if c in out.columns]
    return out[ordre].rename(columns=libelles)


# ─────────────────────────────────────────────────────────────────────────────
# EN-TÊTE
# ─────────────────────────────────────────────────────────────────────────────
st.title("📞 Cockpit de campagne d'appels — Hympyr Énergies")
st.caption("Outil de pilotage. La mise à jour des données se fait dans Logimatique ; "
           "cet outil suit l'avancement et donne le bon ordre d'appel.")

up = st.file_uploader("Charger le fichier clients restructuré (.xlsx)", type=["xlsx"])
if not up:
    st.info("⬆️ Charge le fichier **CLIENTS_HYMPYR.xlsx** pour démarrer.")
    st.stop()

clients, adresses = lire_fichier(up.getvalue())

def trouver_colonne(df, cibles):
    """Retrouve une colonne quelle que soit la casse / les espaces."""
    norm = {str(c).strip().lower(): c for c in df.columns}
    for cible in cibles:
        if cible in norm:
            return norm[cible]
    return None

col_code = trouver_colonne(clients, ["code client", "code_client"])
if col_code is None:
    st.error("La feuille « Clients » doit contenir une colonne « Code client » "
             f"(colonnes trouvées : {', '.join(map(str, clients.columns))}).")
    st.stop()

# Harmoniser les noms attendus par le reste de l'outil
renoms = {}
# Nom : "Raison sociale / Nom" si présent, sinon "Nom"
c_nom = trouver_colonne(clients, ["raison sociale / nom"]) or trouver_colonne(clients, ["nom", "raison sociale"])
if c_nom and c_nom != "Raison sociale / Nom":
    renoms[c_nom] = "Raison sociale / Nom"
c_type = trouver_colonne(clients, ["type client", "type"])
if c_type and c_type != "Type client":
    renoms[c_type] = "Type client"
c_ville = trouver_colonne(clients, ["ville"])
if c_ville and c_ville != "Ville":
    renoms[c_ville] = "Ville"
c_cat = trouver_colonne(clients, ["catégorie normalisée", "catégorie", "categorie"])
if c_cat and c_cat != "Catégorie":
    renoms[c_cat] = "Catégorie"
c_siren = trouver_colonne(clients, ["siren (9 chiffres)", "siren", "siren / siret"])
if c_siren and c_siren != "SIREN":
    renoms[c_siren] = "SIREN"
c_cp = trouver_colonne(clients, ["code postal", "code_postal"])
if c_cp and c_cp != "Code postal":
    renoms[c_cp] = "Code postal"
# Téléphones : privilégier les versions normalisées (norm)
for n in (1, 2, 3):
    col = (trouver_colonne(clients, [f"téléphone {n} (norm)"])
           or trouver_colonne(clients, [f"téléphone {n}", f"telephone {n}"]))
    if col and col != f"Téléphone {n}":
        renoms[col] = f"Téléphone {n}"
if renoms:
    clients = clients.rename(columns=renoms)
# Le renommage peut créer des doublons de noms (version nettoyée + version d'origine).
# On garde la PREMIÈRE occurrence (placée en tête = la version nettoyée).
clients = clients.loc[:, ~clients.columns.duplicated(keep="first")]

# Sécuriser les colonnes optionnelles attendues plus loin
for c in ["Raison sociale / Nom", "Type client", "Ville", "Catégorie", "À compléter",
          "Email principal", "Email secondaire", "SIREN",
          "Téléphone 1", "Téléphone 2", "Téléphone 3",
          "Adresse 1", "Adresse 2", "Adresse 3", "Code postal"]:
    if c not in clients.columns:
        clients[c] = ""

# Adapter le nom de la colonne adresses (mère) si besoin
if not adresses.empty:
    cam = trouver_colonne(adresses, ["code client mère", "code client mere"])
    if cam and cam != "Code client mère":
        adresses = adresses.rename(columns={cam: "Code client mère"})
    # Dans la feuille Adresses, l'identifiant de l'adresse est la colonne "Code Client"
    # (ex. 12771L56). On la renomme en "Code adresse" pour le reste de l'outil,
    # SAUF si une colonne "Code adresse" existe déjà.
    if "Code adresse" not in adresses.columns:
        cad = trouver_colonne(adresses, ["code adresse"]) or trouver_colonne(adresses, ["code client"])
        if cad:
            adresses = adresses.rename(columns={cad: "Code adresse"})
    # Normaliser aussi les autres colonnes attendues de la feuille Adresses
    for src_names, dest in [
        (["téléphone 1 (norm)", "téléphone 1", "telephone 1"], "Téléphone"),
        (["code postal"], "Code postal"),
        (["nom site", "nom"], "Nom site"),
    ]:
        col = trouver_colonne(adresses, src_names)
        if col and col != dest and dest not in adresses.columns:
            adresses = adresses.rename(columns={col: dest})
    adresses = adresses.loc[:, ~adresses.columns.duplicated(keep="first")]

# ── REPRISE DU TRAVAIL DE LA VEILLE (import des CSV de suivi) ──
with st.expander("🔄 Reprendre le travail de la veille (à faire chaque matin)", expanded=False):
    st.caption("Importe ici les deux fichiers de sauvegarde exportés hier soir. "
               "Sans ça, l'outil repart de l'état actuel sur ce serveur (qui peut avoir été réinitialisé).")
    ci1, ci2 = st.columns(2)
    up_suivi = ci1.file_uploader("Sauvegarde SUIVI CLIENTS (CSV)", type=["csv"], key="imp_cli")
    up_refer = ci2.file_uploader("Sauvegarde RÉFÉRENTS (CSV)", type=["csv"], key="imp_adr")
    if st.button("📥 Restaurer ces sauvegardes", type="primary"):
        msgs = []
        try:
            if up_suivi is not None:
                n = importer_suivi_clients_csv(up_suivi)
                msgs.append(f"{n} fiches clients restaurées")
            if up_refer is not None:
                n = importer_suivi_adresses_csv(up_refer)
                msgs.append(f"{n} référents restaurés")
            if msgs:
                st.session_state.modifs_non_sauvees = 0  # on repart d'un état "sauvegardé"
                st.success("✅ " + " · ".join(msgs) + ". Tu peux reprendre où tu t'étais arrêtée.")
                st.cache_data.clear()
            else:
                st.info("Aucun fichier sélectionné.")
        except Exception as e:
            st.error(f"Import impossible : {e}")

suivi = charger_suivi()
base = clients.merge(suivi, left_on=col_code, right_on="code_client", how="left")
# Colonnes issues du suivi : remplacer les NaN par "" pour éviter les erreurs .split()
for _c in ["statut","existe","produits","email_maj","tel_maj","note","doublon_de","rappel_date"]:
    if _c in base.columns:
        base[_c] = base[_c].fillna("")
base["statut"] = base["statut"].replace("", "À appeler")
base["priorite"] = base["Type client"].map(priorite)


# ─────────────────────────────────────────────────────────────────────────────
# BARRE LATÉRALE : avancement + filtres
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    nb = st.session_state.get("modifs_non_sauvees", 0)
    if nb > 0:
        st.error(f"⚠️ {nb} modification(s) NON sauvegardée(s).\n\n"
                 f"Pense à exporter tes CSV avant de fermer (onglet Tableau de bord).")
    else:
        st.success("✅ Travail à jour (rien à sauvegarder pour l'instant).")
    st.divider()
    st.header("Avancement")
    total = len(base)
    faits = base["statut"].isin(STATUTS_TERMINES).sum()
    reste = total - faits
    st.metric("Clients au total", f"{total:,}".replace(",", " "))
    st.metric("Traités", f"{faits:,}".replace(",", " "),
              f"{(100*faits/total):.1f} %" if total else "—")
    st.metric("Restants", f"{reste:,}".replace(",", " "))

    # Projection de fin
    s = charger_suivi()
    if not s.empty and s["maj_le"].notna().any():
        s["jour"] = pd.to_datetime(s["maj_le"], errors="coerce").dt.date
        termines = s[s["statut"].isin(STATUTS_TERMINES)]
        jours_actifs = termines["jour"].nunique()
        if jours_actifs > 0:
            rythme = len(termines) / jours_actifs
            if rythme > 0:
                jours_restants = reste / rythme
                fin = dt.date.today() + dt.timedelta(days=jours_restants * 7 / 5)  # jours ouvrés
                st.divider()
                st.caption("Projection (au rythme observé)")
                st.metric("Appels traités / jour actif", f"{rythme:.0f}")
                st.metric("Fin estimée", fin.strftime("%d/%m/%Y"))
                if rythme < 1:
                    st.warning("Rythme très faible : la projection est indicative.")

    # ── OBJECTIF JOURNALIER POUR TENIR LA DEADLINE ──
    st.divider()
    st.caption(f"Objectif pour le {DEADLINE.strftime('%d/%m/%Y')}")
    jo = jours_ouvres(dt.date.today(), DEADLINE)
    if jo <= 0:
        st.error("Deadline atteinte ou dépassée.")
    else:
        objectif = -(-reste // jo)  # arrondi supérieur
        st.metric("Jours ouvrés restants", f"{jo}")
        st.metric("À traiter / jour pour tenir", f"{objectif}")
        # comparaison avec le rythme observé
        try:
            rythme_obs = rythme  # défini si des appels ont été faits
        except NameError:
            rythme_obs = None
        if rythme_obs:
            if rythme_obs >= objectif:
                st.success(f"Rythme actuel ({rythme_obs:.0f}/j) ≥ objectif. Dans les temps. ✅")
            else:
                manque = objectif - rythme_obs
                st.error(f"Rythme actuel ({rythme_obs:.0f}/j) sous l'objectif "
                         f"de ~{manque:.0f}/j. Il faut accélérer ou renforcer l'équipe.")

    st.divider()
    st.header("🔎 Accès direct")
    code_direct = st.text_input("Code client exact", help="Tape le code client et valide pour aller direct à la fiche.")
    st.divider()
    st.header("Filtres")
    f_type = st.multiselect("Type de client", sorted(base["Type client"].unique()))
    f_statut = st.multiselect("Statut d'appel", STATUTS, default=["À appeler", "À rappeler"])
    f_acompl = st.checkbox("Uniquement « À compléter » non vide", value=False)
    recherche = st.text_input("Recherche (nom, code, ville)")
    prio = st.checkbox("Trier par priorité (pros d'abord)", value=True)


# ─────────────────────────────────────────────────────────────────────────────
# FILE D'APPEL
# ─────────────────────────────────────────────────────────────────────────────
file = base.copy()
# Accès direct par code client : court-circuite les filtres
if code_direct.strip():
    cd = code_direct.strip().upper()
    direct = base[base[col_code].str.upper() == cd]
    if direct.empty:
        st.sidebar.error(f"Aucun client avec le code « {code_direct} ».")
    else:
        file = direct.reset_index(drop=True)
        st.session_state.idx = 0
if not code_direct.strip():
  if f_type:
        file = file[file["Type client"].isin(f_type)]
  if f_statut:
        file = file[file["statut"].isin(f_statut)]
  if f_acompl and "À compléter" in file.columns:
        file = file[file["À compléter"].astype(str).str.strip() != ""]
  if recherche:
        r = recherche.lower()
        masque = (
            file[col_code].str.lower().str.contains(r, na=False)
            | file["Raison sociale / Nom"].str.lower().str.contains(r, na=False)
            | file["Ville"].str.lower().str.contains(r, na=False)
        )
        file = file[masque]
  file = file.sort_values(["priorite", "Raison sociale / Nom"] if prio else ["Raison sociale / Nom"])
  file = file.reset_index(drop=True)

onglet_appel, onglet_adr, onglet_dash = st.tabs(
    ["☎️  Appels clients", "📦  Points de livraison", "📊  Tableau de bord"])

# ── ONGLET APPELS ────────────────────────────────────────────────────────────
with onglet_appel:
    if file.empty:
        st.success("Aucun client dans la file avec ces filtres. 🎉")
        st.stop()

    if "idx" not in st.session_state:
        st.session_state.idx = 0
    st.session_state.idx = max(0, min(st.session_state.idx, len(file) - 1))

    c1, c2, c3 = st.columns([1, 2, 1])
    with c1:
        if st.button("⬅️ Précédent", use_container_width=True):
            st.session_state.idx = max(0, st.session_state.idx - 1)
            st.rerun()
    with c3:
        if st.button("Suivant ➡️", use_container_width=True):
            st.session_state.idx = min(len(file) - 1, st.session_state.idx + 1)
            st.rerun()
    with c2:
        st.markdown(f"<div style='text-align:center;font-weight:600;color:{VERT}'>"
                    f"Fiche {st.session_state.idx + 1} / {len(file)}</div>",
                    unsafe_allow_html=True)

    row = file.iloc[st.session_state.idx]
    code = row[col_code]

    # Fiche client
    gauche, droite = st.columns([3, 2])
    with gauche:
        st.markdown(f"### {row['Raison sociale / Nom']}")
        st.markdown(
            f"<span class='pill'>{row['Type client']}</span>"
            f"<span class='pill pill-orange'>{row.get('Catégorie','')}</span>"
            f"<span style='color:#5a6b62'>Code {code}</span>",
            unsafe_allow_html=True,
        )
        adr = " ".join(x for x in [row.get("Adresse 1",""), row.get("Adresse 2",""),
                                   row.get("Adresse 3","")] if x)
        st.markdown(f"""<div class='fiche'>
            📍 {adr}<br>{row.get('Code postal','')} {row.get('Ville','')}<br><br>
            ☎️ {row.get('Téléphone 1','')} &nbsp; {row.get('Téléphone 2','')} &nbsp; {row.get('Téléphone 3','')}<br>
            ✉️ {row.get('Email principal','') or '<i>aucun email</i>'}
            {(' · ' + row.get('Email secondaire','')) if row.get('Email secondaire','') else ''}<br>
            🏢 SIREN : {row.get('SIREN','') or '<i>—</i>'}
            </div>""", unsafe_allow_html=True)
        if row.get("À compléter", ""):
            st.warning(f"À compléter : {row['À compléter']}")

        # Adresses de livraison rattachées
        if not adresses.empty:
            liees = adresses[adresses["Code client mère"] == code]
            if not liees.empty:
                with st.expander(f"📦 {len(liees)} adresse(s) de livraison rattachée(s)"):
                    st.dataframe(
                        liees[["Code adresse", "Nom site", "Adresse 1", "Code postal", "Ville"]],
                        hide_index=True, use_container_width=True,
                    )

    # Formulaire d'appel
    with droite:
        st.markdown("#### Résultat de l'appel")
        prod_init = [p for p in str(row.get("produits") or "").split("|") if p in PRODUITS]
        with st.form("appel", clear_on_submit=False):
            statut = st.selectbox("Statut", STATUTS,
                                  index=STATUTS.index(row["statut"]) if row["statut"] in STATUTS else 0)
            existe = st.radio("Client toujours actif ?", ["Oui", "Non", "Incertain"],
                              horizontal=True,
                              index=["Oui", "Non", "Incertain"].index(row.get("existe") or "Oui")
                              if (row.get("existe") in ["Oui", "Non", "Incertain"]) else 0)
            produits = st.multiselect("Produits achetés", PRODUITS, default=prod_init)
            email_maj = st.text_input("E-mail confirmé / corrigé", value=row.get("email_maj") or "")
            tel_maj = st.text_input("Téléphone confirmé / corrigé", value=row.get("tel_maj") or "")
            doublon_de = st.text_input("Doublon du client n°", value=row.get("doublon_de") or "",
                                       help="Si ce client est un doublon, indiquer le code à conserver.")
            motif_sortie = st.selectbox(
                "Motif de sortie (si ancien client)", MOTIFS_SORTIE,
                index=MOTIFS_SORTIE.index(row.get("motif_sortie"))
                if (row.get("motif_sortie") in MOTIFS_SORTIE) else 0,
                help="À renseigner si le statut est « Ancien client (à sortir) »."
            )
            rappel = st.date_input("Date de rappel (si applicable)", value=None)
            note = st.text_area("Notes (commercial, vérifs…)", value=row.get("note") or "", height=90)
            ok = st.form_submit_button("💾 Enregistrer & passer au suivant",
                                       use_container_width=True, type="primary")
        if ok:
            enregistrer(
                code, statut=statut, existe=existe,
                produits="|".join(produits),
                email_maj=email_maj.strip(), tel_maj=tel_maj.strip(),
                doublon_de=doublon_de.strip(), note=note.strip(),
                motif_sortie="" if motif_sortie == "—" else motif_sortie,
                rappel_date=rappel.isoformat() if rappel else "",
            )
            marquer_modif()
            st.session_state.idx = min(len(file) - 1, st.session_state.idx + 1)
            st.rerun()

# ── ONGLET POINTS DE LIVRAISON ───────────────────────────────────────────────
with onglet_adr:
    if adresses.empty:
        st.info("Le fichier ne contient pas de feuille « Adresses livraison ».")
    else:
        st.subheader("Vérification des points de livraison")
        st.caption("Pour chaque adresse rattachée à une entreprise : qui est le référent sur place ?")

        sa = charger_suivi_adresses()
        adr = adresses.copy()
        adr = adr.merge(sa, left_on="Code adresse", right_on="code_adresse", how="left")
        for c in ["referent", "tel_site", "statut_adr", "note_adr"]:
            if c in adr.columns:
                adr[c] = adr[c].fillna("")
        adr["statut_adr"] = adr["statut_adr"].replace("", "À vérifier")

        # Indicateurs
        a1, a2, a3 = st.columns(3)
        a1.metric("Points de livraison", len(adr))
        a2.metric("Vérifiés", int((adr["statut_adr"] == "Vérifié ✅").sum()))
        a3.metric("Restants", int((adr["statut_adr"] != "Vérifié ✅").sum()))

        # Recherche directe par code adresse OU par code client mère
        rcol1, rcol2 = st.columns(2)
        q_adr = rcol1.text_input("🔎 Code adresse exact (ex. 12771L56)")
        q_mere = rcol2.text_input("🔎 ou Code client mère (montre tous ses points)")

        vue = adr.copy()
        if q_adr.strip():
            vue = vue[vue["Code adresse"].str.upper() == q_adr.strip().upper()]
        elif q_mere.strip():
            vue = vue[vue["Code client mère"].str.upper() == q_mere.strip().upper()]
        else:
            f_av = st.multiselect("Statut", ["À vérifier", "Vérifié ✅", "Adresse obsolète"],
                                  default=["À vérifier"])
            if f_av:
                vue = vue[vue["statut_adr"].isin(f_av)]
        vue = vue.reset_index(drop=True)

        if vue.empty:
            st.success("Aucun point de livraison à afficher avec ce filtre.")
        else:
            if "idx_adr" not in st.session_state:
                st.session_state.idx_adr = 0
            st.session_state.idx_adr = max(0, min(st.session_state.idx_adr, len(vue) - 1))

            n1, n2, n3 = st.columns([1, 2, 1])
            if n1.button("⬅️ Précédent", key="adr_prev", use_container_width=True):
                st.session_state.idx_adr = max(0, st.session_state.idx_adr - 1); st.rerun()
            if n3.button("Suivant ➡️", key="adr_next", use_container_width=True):
                st.session_state.idx_adr = min(len(vue) - 1, st.session_state.idx_adr + 1); st.rerun()
            n2.markdown(f"<div style='text-align:center;font-weight:600;color:{VERT}'>"
                        f"Point {st.session_state.idx_adr + 1} / {len(vue)}</div>", unsafe_allow_html=True)

            a = vue.iloc[st.session_state.idx_adr]
            cad = a["Code adresse"]
            adr_txt = " ".join(x for x in [a.get("Adresse 1",""), a.get("Adresse 2",""), a.get("Adresse 3","")] if x)
            g, d = st.columns([3, 2])
            with g:
                st.markdown(f"### {a.get('Nom site','') or 'Point de livraison'}")
                st.markdown(
                    f"<span class='pill'>Adresse {cad}</span>"
                    f"<span style='color:#5a6b62'>Client mère : {a.get('Code client mère','')}</span>",
                    unsafe_allow_html=True)
                st.markdown(f"""<div class='fiche'>
                    📍 {adr_txt}<br>{a.get('Code postal','')} {a.get('Ville','')}<br><br>
                    ☎️ {a.get('Téléphone','') or '<i>aucun téléphone</i>'}
                    </div>""", unsafe_allow_html=True)
            with d:
                st.markdown("#### Référent du site")
                with st.form("adr_form"):
                    referent = st.text_input("Nom du référent sur place", value=a.get("referent") or "")
                    tel_site = st.text_input("Téléphone du site / référent", value=a.get("tel_site") or "")
                    statut_adr = st.selectbox("Statut", ["À vérifier", "Vérifié ✅", "Adresse obsolète"],
                        index=["À vérifier","Vérifié ✅","Adresse obsolète"].index(a["statut_adr"])
                        if a["statut_adr"] in ["À vérifier","Vérifié ✅","Adresse obsolète"] else 0)
                    note_adr = st.text_area("Note", value=a.get("note_adr") or "", height=80)
                    ok_adr = st.form_submit_button("💾 Enregistrer & suivant",
                                                   use_container_width=True, type="primary")
                if ok_adr:
                    enregistrer_adresse(cad, referent=referent.strip(), tel_site=tel_site.strip(),
                                        statut_adr=statut_adr, note_adr=note_adr.strip())
                    marquer_modif()
                    st.session_state.idx_adr = min(len(vue) - 1, st.session_state.idx_adr + 1)
                    st.rerun()


# ── ONGLET TABLEAU DE BORD ───────────────────────────────────────────────────
with onglet_dash:
    nb = st.session_state.get("modifs_non_sauvees", 0)
    if nb > 0:
        st.warning(f"🔔 Sauvegarde du soir : tu as {nb} modification(s) à exporter. "
                   f"Télécharge les CSV ci-dessous **avant de fermer l'onglet**.")
    st.subheader("🎯 Objectif pour tenir la deadline")
    st.caption(f"Échéance : émission de la facturation électronique au "
               f"{DEADLINE.strftime('%d/%m/%Y')} (PME).")

    perimetre = st.radio(
        "Périmètre à boucler pour la deadline",
        ["Tous les clients restants", "Uniquement les pros (conformité)"],
        horizontal=True,
    )
    if perimetre.startswith("Uniquement"):
        masque_perim = base["Type client"].str.startswith("Pro")
    else:
        masque_perim = pd.Series(True, index=base.index)
    restant_perim = int((masque_perim & ~base["statut"].isin(STATUTS_TERMINES)).sum())

    jo = jours_ouvres(dt.date.today(), DEADLINE)
    o1, o2, o3, o4 = st.columns(4)
    o1.metric("Restant sur ce périmètre", f"{restant_perim:,}".replace(",", " "))
    o2.metric("Jours ouvrés d'ici la deadline", f"{jo}")
    if jo > 0:
        objectif = -(-restant_perim // jo)
        o3.metric("À traiter / jour", f"{objectif}")
        # rythme observé
        s_tmp = charger_suivi()
        rythme_obs = None
        if not s_tmp.empty and s_tmp["maj_le"].notna().any():
            s_tmp["jour"] = pd.to_datetime(s_tmp["maj_le"], errors="coerce").dt.date
            term = s_tmp[s_tmp["statut"].isin(STATUTS_TERMINES)]
            ja = term["jour"].nunique()
            if ja > 0:
                rythme_obs = len(term) / ja
        if rythme_obs:
            o4.metric("Rythme actuel / jour", f"{rythme_obs:.0f}",
                      delta=f"{rythme_obs - objectif:+.0f} vs objectif")
            if rythme_obs >= objectif:
                st.success(f"✅ Au rythme actuel ({rythme_obs:.0f}/jour), la deadline est tenable "
                           f"sur ce périmètre.")
            else:
                jours_necessaires = -(-restant_perim // max(int(rythme_obs), 1))
                fin_proj = dt.date.today()
                ajout = 0
                while jours_necessaires > 0:
                    fin_proj += dt.timedelta(days=1)
                    if fin_proj.weekday() < 5:
                        jours_necessaires -= 1
                st.error(f"⚠️ Au rythme actuel ({rythme_obs:.0f}/jour), fin estimée vers le "
                         f"{fin_proj.strftime('%d/%m/%Y')} — soit après la deadline. "
                         f"Il faut viser {objectif}/jour, ou renforcer l'équipe.")
        else:
            o4.metric("Rythme actuel / jour", "—")
            st.info("Le rythme s'affichera après les premiers appels enregistrés.")
    else:
        o3.metric("À traiter / jour", "—")
        st.error("La deadline est atteinte ou dépassée.")

    st.divider()
    st.subheader("Avancement de la campagne")
    s = charger_suivi()
    cc1, cc2, cc3, cc4 = st.columns(4)
    cc1.metric("Total clients", len(base))
    cc2.metric("Traités", int(base["statut"].isin(STATUTS_TERMINES).sum()))
    cc3.metric("À rappeler", int((base["statut"] == "À rappeler").sum()))
    cc4.metric("Doublons repérés", int((base["statut"] == "Doublon").sum()))

    st.markdown("##### Répartition par statut")
    st.bar_chart(base["statut"].value_counts())

    st.markdown("##### Répartition par type de client")
    st.bar_chart(base["Type client"].value_counts())

    if not s.empty and s["produits"].fillna("").str.len().gt(0).any():
        st.markdown("##### Produits achetés (déclarés en appel)")
        explos = (s["produits"].fillna("").str.split("|").explode())
        explos = explos[explos.isin(PRODUITS)]
        if not explos.empty:
            st.bar_chart(explos.value_counts())

    st.divider()
    st.markdown("##### Export du suivi (sauvegarde / reporting)")
    st.caption("Trace de la campagne, lisible dans Excel. "
               "La donnée de référence reste Logimatique — ceci est une sauvegarde.")

    if s.empty:
        st.info("Aucun appel enregistré pour le moment.")
    else:
        export = preparer_export(s, base)
        col_csv, col_xlsx = st.columns(2)

        # CSV pensé pour Excel français : séparateur ; et BOM UTF-8
        csv_bytes = export.to_csv(index=False, sep=";").encode("utf-8-sig")
        col_csv.download_button(
            "⬇️ Export CSV (Excel FR)",
            csv_bytes,
            file_name=f"suivi_appels_hympyr_{dt.date.today():%Y%m%d}.csv",
            mime="text/csv",
            use_container_width=True,
        )

        # Excel mis en forme
        xbuf = io.BytesIO()
        with pd.ExcelWriter(xbuf, engine="openpyxl") as writer:
            export.to_excel(writer, index=False, sheet_name="Suivi appels")
            ws = writer.sheets["Suivi appels"]
            from openpyxl.styles import Font, PatternFill, Alignment
            for j, col in enumerate(export.columns, 1):
                c = ws.cell(row=1, column=j)
                c.fill = PatternFill("solid", fgColor="0D3D27")
                c.font = Font(bold=True, color="FFFFFF")
                c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                largeur = min(max(len(str(col)) + 2,
                              int(export[col].astype(str).str.len().head(200).max() or 10) + 2), 45)
                ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = largeur
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
        col_xlsx.download_button(
            "⬇️ Export Excel (.xlsx)",
            xbuf.getvalue(),
            file_name=f"suivi_appels_hympyr_{dt.date.today():%Y%m%d}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

        with st.expander("Aperçu de l'export clients"):
            st.dataframe(export, hide_index=True, use_container_width=True)

        if st.session_state.get("modifs_non_sauvees", 0) > 0:
            st.caption("Une fois tes 2 fichiers téléchargés, confirme pour repasser au vert :")
            if st.button("✅ J'ai bien téléchargé mes sauvegardes du soir"):
                st.session_state.modifs_non_sauvees = 0
                st.rerun()

    # ── Export dédié aux points de livraison / référents ──
    if not adresses.empty:
        st.divider()
        st.markdown("##### Export des points de livraison (référents)")
        st.caption("Référents vérifiés dans l'onglet « Points de livraison ».")
        sa = charger_suivi_adresses()
        export_adr = preparer_export_adresses(sa, adresses)
        if export_adr.empty:
            st.info("Aucun point de livraison à exporter.")
        else:
            ca1, ca2 = st.columns(2)
            csv_adr = export_adr.to_csv(index=False, sep=";").encode("utf-8-sig")
            ca1.download_button(
                "⬇️ Points de livraison — CSV (Excel FR)",
                csv_adr,
                file_name=f"points_livraison_hympyr_{dt.date.today():%Y%m%d}.csv",
                mime="text/csv",
                use_container_width=True,
            )
            xbuf2 = io.BytesIO()
            with pd.ExcelWriter(xbuf2, engine="openpyxl") as writer:
                export_adr.to_excel(writer, index=False, sheet_name="Points de livraison")
                ws2 = writer.sheets["Points de livraison"]
                from openpyxl.styles import Font as _F, PatternFill as _PF, Alignment as _AL
                for j, col in enumerate(export_adr.columns, 1):
                    c = ws2.cell(row=1, column=j)
                    c.fill = _PF("solid", fgColor="0D3D27")
                    c.font = _F(bold=True, color="FFFFFF")
                    c.alignment = _AL(horizontal="center", vertical="center", wrap_text=True)
                    larg = min(max(len(str(col)) + 2,
                               int(export_adr[col].astype(str).str.len().head(200).max() or 10) + 2), 45)
                    ws2.column_dimensions[ws2.cell(row=1, column=j).column_letter].width = larg
                ws2.freeze_panes = "A2"
                ws2.auto_filter.ref = ws2.dimensions
            ca2.download_button(
                "⬇️ Points de livraison — Excel (.xlsx)",
                xbuf2.getvalue(),
                file_name=f"points_livraison_hympyr_{dt.date.today():%Y%m%d}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
            with st.expander("Aperçu de l'export points de livraison"):
                st.dataframe(export_adr, hide_index=True, use_container_width=True)
