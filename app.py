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
    "Fait ✅", "Ne plus contacter", "Doublon",
]
STATUTS_TERMINES = {"Fait ✅", "Ne plus contacter", "Doublon"}

st.set_page_config(page_title="Cockpit appels — Hympyr", page_icon="📞", layout="wide")

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
# EN-TÊTE
# ─────────────────────────────────────────────────────────────────────────────
st.title("Outil de suivi MAJ fichier clients")
st.caption("Outil de pilotage. La mise à jour des données se fait dans Logimatique ; "
           "cet outil suit l'avancement et donne le bon ordre d'appel ;"
           "cet outil doit rester actif jusqu'à la fin de la MAJ du fichier.")

up = st.file_uploader("Charger le fichier clients restructuré (.xlsx)", type=["xlsx"])
if not up:
    st.info("⬆️ Charge le fichier **CLIENTS_HYMPYR_restructure.xlsx** pour démarrer.")
    st.stop()

clients, adresses = lire_fichier(up.getvalue())
col_code = "Code client"
if col_code not in clients.columns:
    st.error("La feuille « Clients » doit contenir une colonne « Code client ».")
    st.stop()

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

onglet_appel, onglet_dash = st.tabs(["☎️  Appels", "📊  Tableau de bord"])

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
                rappel_date=rappel.isoformat() if rappel else "",
            )
            st.session_state.idx = min(len(file) - 1, st.session_state.idx + 1)
            st.rerun()

# ── ONGLET TABLEAU DE BORD ───────────────────────────────────────────────────
with onglet_dash:
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
    st.caption("Trace de la campagne. À conserver comme sauvegarde — la donnée de référence reste Logimatique.")
    if not s.empty:
        st.download_button("⬇️ Télécharger le suivi (CSV)",
                           s.to_csv(index=False).encode("utf-8"),
                           file_name="suivi_appels_hympyr.csv", mime="text/csv")
    else:
        st.info("Aucun appel enregistré pour le moment.")
