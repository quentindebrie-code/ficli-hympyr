"""
Scripts d'appel par typologie — module additionnel du Cockpit appels Hympyr
===========================================================================

Ce fichier est AUTONOME : il ne lit ni n'écrit la base SQLite du cockpit, ne
touche pas à `st.session_state` et ne déclare aucun widget Streamlit dans le
panneau lui-même. Le rendu est un composant HTML/CSS/JS isolé.

POURQUOI CE CHOIX
-----------------
Chaque widget Streamlit déclenche un rerun du script complet. Un accordéon
d'objections construit avec `st.expander` piloté par des boutons imposerait un
rerun par clic, en plein appel, avec le risque de perdre une saisie en cours sur
la fiche client. Ici, changer de typologie, chercher une objection ou déplier
une réponse ne provoque AUCUN rerun : tout se passe dans l'iframe du composant.
Conséquence : cette fonctionnalité ne peut pas casser la machine à états
existante du cockpit.

INTÉGRATION (2 lignes dans app.py, onglet « Appels clients », fiche client)
--------------------------------------------------------------------------
    import scripts_appel as sa

    detection = sa.deviner_typologie(fiche)          # fiche = dict/Series du client
    with st.expander("📞 Script d'appel et objections", expanded=False):
        sa.panneau_scripts(
            typologie=detection.typologie,
            sous_type=detection.sous_type,
            contexte=sa.Contexte(
                client=fiche.get("NOM", ""),
                appelant=st.session_state.get("profil", ""),
                produit=fiche.get("PRODUIT", ""),
            ),
            motif_detection=detection.motif,
        )

VARIANTE MODALE (si tu tiens à la pop-up plein écran)
-----------------------------------------------------
    if st.button("📞 Script d'appel", use_container_width=True):
        sa.dialogue_scripts(detection.typologie, detection.sous_type, contexte)

Dépendances : streamlit >= 1.31 (testé avec 1.62), aucune autre.
Auteur : module livré pour Hympyr Énergies — contenu des scripts fourni par le
métier, mise en forme et logique de détection ajoutées.
"""

from __future__ import annotations

import html as _html
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import streamlit as st
import streamlit.components.v1 as components

__all__ = [
    "Contexte",
    "Detection",
    "SCRIPTS",
    "deviner_typologie",
    "panneau_scripts",
    "dialogue_scripts",
]

# ═══════════════════════════════════════════════════════════════════════════
# 1. MODÈLE DE DONNÉES
# ═══════════════════════════════════════════════════════════════════════════

# Jetons remplaçables dans les phrases : {client}, {appelant}, {produit},
# {collectivite}. Ils sont substitués au rendu par le contexte de la fiche.


@dataclass(frozen=True)
class Branche:
    """Variante d'une étape selon la réponse du client (ex. TP / transport)."""

    label: str
    dire: tuple[str, ...] = ()
    relances: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Etape:
    numero: str
    titre: str
    dire: tuple[str, ...] = ()
    relances: tuple[str, ...] = ()
    note: str = ""
    branches_titre: str = ""
    branches: tuple[Branche, ...] = ()
    groupe: str = ""  # "metier" : la branche active suit le sous-type détecté


@dataclass(frozen=True)
class Objection:
    intitule: str
    dire: tuple[str, ...] = ()
    conduite: str = ""
    puis: tuple[str, ...] = ()
    alerte: str = ""


@dataclass(frozen=True)
class Script:
    cle: str
    libelle: str
    couleur: str
    regles_or: tuple[str, ...] = ()
    etapes: tuple[Etape, ...] = ()
    objections: tuple[Objection, ...] = ()
    a_consigner: tuple[str, ...] = ()


@dataclass(frozen=True)
class Contexte:
    """Valeurs injectées dans les phrases du script."""

    client: str = ""
    appelant: str = ""
    produit: str = ""

    def jetons(self) -> dict[str, str]:
        return {
            "client": (self.client or "").strip() or "Madame/Monsieur",
            "appelant": (self.appelant or "").strip() or "[votre prénom]",
            "produit": (self.produit or "").strip() or "[produit]",
            "collectivite": (self.client or "").strip() or "[la collectivité]",
        }


@dataclass(frozen=True)
class Detection:
    typologie: str  # "particulier" | "pro" | "collectivite"
    sous_type: str = ""  # "" | "tp" | "transport" | "agriculture"
    motif: str = ""
    confiance: str = "faible"  # "faible" | "moyenne" | "forte"


# ═══════════════════════════════════════════════════════════════════════════
# 2. CONTENU DES SCRIPTS
#    Source : documents métier « SCRIPT 1/2/3 » — toute modification de
#    formulation se fait ICI et nulle part ailleurs.
# ═══════════════════════════════════════════════════════════════════════════

_PARTICULIERS = Script(
    cle="particulier",
    libelle="Particulier",
    couleur="#2e7d32",
    regles_or=(
        "Ne jamais ouvrir par « mise à jour de fichier » : ça sonne administratif, "
        "le client décroche mentalement en trois secondes.",
        "Quand le client explique pourquoi il est parti : écouter, ne pas se justifier, "
        "ne pas contredire. Le retour a plus de valeur que la défense.",
        "Ne pas redemander mécaniquement toutes les informations si elles sont déjà bonnes.",
    ),
    etapes=(
        Etape(
            numero="1",
            titre="Ouverture",
            dire=(
                "Bonjour Madame/Monsieur {client}, {appelant} de chez Hympyr Énergies. "
                "Je me permets de vous appeler car cela fait quelque temps que nous n'avons pas eu "
                "l'occasion d'échanger avec vous. Vous aviez auparavant commandé chez nous pour {produit}.",
                "Je voulais simplement prendre de vos nouvelles et savoir si vous utilisez toujours "
                "ce type d'énergie aujourd'hui. Vous avez une petite minute ?",
            ),
            note="Objectif : ne pas commencer par « mise à jour de fichier », qui donne "
            "immédiatement une impression administrative ou commerciale.",
        ),
        Etape(
            numero="2",
            titre="Comprendre la situation actuelle",
            branches_titre="Le client consomme-t-il toujours la même énergie ?",
            branches=(
                Branche(
                    label="Oui, même énergie",
                    dire=("D'accord. Et aujourd'hui, vous vous approvisionnez toujours de la même manière ?",),
                    relances=(
                        "Vous commandez toujours à peu près aux mêmes périodes ?",
                        "Vous avez changé quelque chose depuis notre dernière commande ?",
                        "Vous êtes satisfait de votre organisation actuelle ?",
                        "Est-ce qu'il y a quelque chose qui vous ferait revenir chez Hympyr ?",
                    ),
                ),
                Branche(
                    label="Non, il ne consomme plus",
                    dire=(
                        "D'accord, je comprends. Vous êtes passé sur quel type d'énergie aujourd'hui ?",
                        "Et vous êtes satisfait de ce changement ?",
                    ),
                    note="Ces deux questions permettent de comprendre l'évolution du parc clients "
                    "sans transformer l'appel en argumentaire de vente. On ne cherche pas à faire "
                    "revenir quelqu'un qui a changé de chaudière.",
                ),
            ),
        ),
        Etape(
            numero="3",
            titre="La question charnière",
            dire=(
                "Et si vous ne commandez plus chez nous aujourd'hui, c'est simplement parce que vos "
                "habitudes ont changé, ou est-ce qu'il y avait une raison particulière à l'époque ?",
            ),
            note="Laisser le client parler. Ne pas chercher à se justifier immédiatement.",
        ),
        Etape(
            numero="4",
            titre="Comprendre pourquoi le client est parti",
            dire=(
                "Je comprends. Si ce n'est pas indiscret, qu'est-ce qui vous avait amené à changer ? "
                "C'est justement intéressant pour nous de le savoir.",
                "Merci de me le dire. L'objectif de mon appel est aussi de comprendre ce qui a changé "
                "pour vous. Ça nous permet de rester à l'écoute de nos anciens clients.",
            ),
            relances=(
                "Motifs à cocher : prix · disponibilité · délai de livraison · relation commerciale · "
                "qualité de service · déménagement · changement de chauffage · consommation trop faible · "
                "autre fournisseur recommandé · mauvaise expérience.",
            ),
        ),
        Etape(
            numero="5",
            titre="Vérification des informations",
            dire=(
                "J'en profite simplement pour vérifier que nous avons toujours les bonnes coordonnées, "
                "notamment si nous devons vous contacter ou vous transmettre un document.",
            ),
            relances=(
                "Nom / prénom",
                "Adresse de facturation",
                "Adresse de livraison si différente",
                "Téléphone",
                "Adresse e-mail",
                "Informations utiles à la livraison (accès, cuve, portail, chien…)",
                "Énergie actuellement utilisée",
            ),
            note="Ne pas demander mécaniquement toutes les informations si elles sont déjà correctes. "
            "On confirme, on ne réinterroge pas.",
        ),
        Etape(
            numero="6",
            titre="Faire émerger une opportunité de retour",
            dire=(
                "Et si vous aviez besoin de {produit} dans les prochains mois, est-ce que vous seriez "
                "susceptible de refaire appel à nous ?",
                "Très bien. Dans ce cas, je peux simplement noter que vous êtes toujours susceptible "
                "d'avoir besoin de {produit}. L'idée n'est pas de vous solliciter inutilement, mais que "
                "nous sachions comment vous accompagner si besoin.",
            ),
        ),
        Etape(
            numero="7",
            titre="Conclusion",
            dire=(
                "Merci pour votre temps et pour ces informations. Ça nous permet de mieux connaître la "
                "situation de nos anciens clients et de ne pas avoir des informations qui datent de "
                "plusieurs années.",
                "Et si vous avez à nouveau besoin de {produit}, n'hésitez pas à penser à nous. "
                "Nous serons ravis de reprendre contact avec vous.",
                "Je vous souhaite une très bonne journée, Madame/Monsieur {client}.",
            ),
        ),
    ),
    a_consigner=(
        "Énergie utilisée aujourd'hui (et si changement : laquelle)",
        "Motif du départ, dans les mots du client",
        "Coordonnées vérifiées / corrigées",
        "Susceptible de recommander : oui / non / période",
        "Opposition à être recontacté : à cocher impérativement si le client le demande",
    ),
    objections=(
        Objection(
            intitule="Pourquoi vous m'appelez ?",
            dire=(
                "Tout simplement parce que cela fait un moment que nous n'avons pas échangé. On reprend "
                "contact avec certains de nos anciens clients pour savoir où ils en sont aujourd'hui et "
                "vérifier que nous avons toujours les bonnes informations. C'est surtout l'occasion de "
                "reprendre contact.",
            ),
        ),
        Objection(
            intitule="Vous voulez me vendre quelque chose ?",
            dire=(
                "Non, ce n'est pas l'objectif premier de l'appel. Je cherche surtout à savoir si vous "
                "utilisez toujours {produit}, comment vous vous approvisionnez aujourd'hui et si vous "
                "avez été satisfait de votre changement. Si vous avez un besoin à l'avenir, nous serons "
                "évidemment là.",
            ),
        ),
        Objection(
            intitule="Je ne commande plus chez vous, c'était trop cher.",
            dire=("Je comprends. Le prix est évidemment important. Est-ce que c'est principalement ce "
                  "qui avait motivé votre changement ?",),
            conduite="Écouter jusqu'au bout avant de répondre. Ne pas comparer les prix au téléphone.",
            puis=(
                "Merci pour votre retour. Je vais le noter, c'est justement le type de retour qui nous "
                "permet de mieux comprendre pourquoi certains clients ne reviennent plus.",
            ),
        ),
        Objection(
            intitule="Je ne chauffe plus au fioul.",
            dire=("D'accord. Vous avez changé pour quelle solution ? Et vous en êtes satisfait aujourd'hui ?",),
            conduite="Noter le changement d'énergie sur la fiche : c'est l'information la plus utile de "
            "tout l'appel pour la suite de la campagne.",
        ),
        Objection(
            intitule="Je n'ai pas le temps.",
            dire=(
                "Aucun problème, je comprends. Je voulais simplement reprendre contact avec vous. "
                "Si vous préférez, je peux simplement vérifier avec vous que nous avons toujours vos "
                "bonnes coordonnées et ne pas vous retenir davantage.",
            ),
        ),
        Objection(
            intitule="Je ne veux pas donner mes informations.",
            dire=(
                "Je comprends tout à fait. Nous ne cherchons pas à vous demander des informations qui "
                "ne seraient pas nécessaires. L'objectif est simplement de vérifier que les coordonnées "
                "que nous avons déjà sont toujours correctes.",
            ),
        ),
        Objection(
            intitule="Je ne veux plus être contacté.",
            dire=(
                "Bien entendu, je le prends en compte. Merci de me l'avoir précisé. Je vais faire le "
                "nécessaire pour que nous respections votre demande.",
            ),
            conduite="Fin de l'appel. Ne pas poser de question supplémentaire, ne pas tenter de "
            "comprendre pourquoi.",
            alerte="RGPD — droit d'opposition (art. 21). Cette demande doit être ENREGISTRÉE dans "
            "l'outil, pas seulement dite au téléphone. Coche la case « opposition » sur la fiche avant "
            "de passer à l'appel suivant : une promesse orale non tracée est un manquement.",
        ),
    ),
)

_COLLECTIVITES = Script(
    cle="collectivite",
    libelle="Collectivité",
    couleur="#1565c0",
    regles_or=(
        "Vérifier d'abord qu'on parle à la bonne personne : dans une collectivité, "
        "l'interlocuteur change plus souvent que le besoin.",
        "Ne jamais remettre en cause le marché en cours. On se positionne pour la prochaine consultation.",
        "Repartir avec deux choses : les critères de choix actuels et la période de la prochaine consultation.",
    ),
    etapes=(
        Etape(
            numero="1",
            titre="Ouverture",
            dire=(
                "Bonjour, {appelant} de chez Hympyr Énergies. Je me permets de vous appeler car nous "
                "avons travaillé avec {collectivite} par le passé et cela fait quelque temps que nous "
                "n'avons pas eu l'occasion d'échanger.",
                "Je souhaitais simplement faire un point avec vous sur vos besoins actuels et vérifier "
                "que nous avons toujours les bons interlocuteurs et les bonnes informations. "
                "Est-ce que je peux vous prendre une minute ?",
            ),
        ),
        Etape(
            numero="2",
            titre="Identifier le bon interlocuteur",
            dire=(
                "Êtes-vous toujours la personne qui suit les approvisionnements en {produit} "
                "pour la collectivité ?",
                "D'accord, pourriez-vous m'indiquer à qui je peux m'adresser aujourd'hui ?",
            ),
            relances=(
                "Nom du nouvel interlocuteur",
                "Fonction",
                "Téléphone",
                "E-mail professionnel",
                "Service concerné",
            ),
            note="Si l'interlocuteur a changé : c'est la donnée la plus précieuse de l'appel. "
            "Un dossier collectivité avec le mauvais nom est un dossier mort.",
        ),
        Etape(
            numero="3",
            titre="Comprendre les besoins actuels",
            dire=("Et concernant vos approvisionnements, vous utilisez toujours {produit} aujourd'hui ?",),
            relances=(
                "Vos besoins sont-ils restés sensiblement les mêmes ?",
                "Avez-vous de nouveaux bâtiments ou équipements à approvisionner ?",
                "Votre organisation d'approvisionnement a-t-elle changé ?",
                "Travaillez-vous toujours avec les mêmes fournisseurs ?",
            ),
        ),
        Etape(
            numero="4",
            titre="Comprendre pourquoi Hympyr n'est plus sollicité",
            dire=(
                "Est-ce que je peux vous demander ce qui a motivé ce changement ? "
                "C'est surtout pour comprendre vos attentes actuelles.",
                "Était-ce principalement lié au prix, aux marchés publics, aux délais, à la "
                "disponibilité ou à la qualité du service ?",
            ),
            note="Ne pas chercher à contredire. En collectivité, la réponse est souvent « la procédure », "
            "pas « vous ». C'est une bonne nouvelle : ça se rejoue à chaque consultation.",
        ),
        Etape(
            numero="5",
            titre="Vérification administrative",
            dire=(
                "J'aimerais également vérifier que nous avons les bonnes informations administratives "
                "dans notre dossier, notamment les coordonnées du service concerné et les informations "
                "nécessaires à la facturation.",
            ),
            relances=(
                "Raison sociale exacte",
                "Adresse",
                "Coordonnées du service",
                "Interlocuteur",
                "E-mail",
                "Informations de facturation",
                "Informations de livraison",
                "Références administratives nécessaires (n° d'engagement, service exécutant, code service)",
            ),
        ),
        Etape(
            numero="6",
            titre="Facturation électronique — la bonne formulation",
            dire=(
                "Nous profitons également de cette campagne pour fiabiliser les informations "
                "administratives de nos clients et anciens clients, notamment parce que les échanges de "
                "factures évoluent avec la généralisation de la facturation électronique.",
            ),
            note="Ne PAS présenter la réforme comme une « obligation de mise à jour du fichier client ». "
            "Pour la sphère publique, Chorus Pro reste la plateforme de référence : la facturation "
            "électronique y est déjà obligatoire depuis 2020, ce n'est donc pas une nouveauté pour eux. "
            "L'enjeu réel de notre côté : disposer du code service, du n° d'engagement et du SIRET exact, "
            "sans lesquels une facture est rejetée.",
        ),
        Etape(
            numero="7",
            titre="Réouvrir la relation commerciale",
            dire=(
                "Et si un besoin devait se présenter dans les prochains mois, est-ce que Hympyr pourrait "
                "de nouveau être consulté ?",
                "Très bien. Qu'est-ce qui serait important pour vous aujourd'hui dans le choix "
                "d'un fournisseur ?",
            ),
            note="Question la plus rentable du script : elle donne les critères de décision actuels de "
            "la collectivité. À retranscrire mot à mot dans le commentaire de la fiche.",
        ),
        Etape(
            numero="8",
            titre="Conclusion",
            dire=(
                "Merci beaucoup pour votre temps. Ça nous permet de remettre notre dossier à jour, mais "
                "surtout de savoir comment vous fonctionnez aujourd'hui.",
                "Si un besoin se présente, n'hésitez pas à revenir vers nous. Nous serons ravis de "
                "pouvoir échanger à nouveau avec vous.",
                "Très bonne journée à vous.",
            ),
        ),
    ),
    a_consigner=(
        "Interlocuteur actuel : nom, fonction, service, e-mail direct",
        "Marché en cours : oui / non — et échéance si connue",
        "Période habituelle de consultation",
        "Critères de choix cités, dans leurs mots",
        "Données Chorus : SIRET, code service, n° d'engagement",
    ),
    objections=(
        Objection(
            intitule="Nous avons un marché en cours.",
            dire=(
                "Bien sûr, je comprends. Je ne cherche pas à remettre en cause votre marché actuel. "
                "Je souhaitais simplement savoir comment vous fonctionnez aujourd'hui et vérifier que "
                "nous avons les bons interlocuteurs. Nous pourrons ainsi rester disponibles lorsqu'une "
                "nouvelle consultation se présentera.",
            ),
            conduite="Noter impérativement l'échéance du marché si elle est donnée : c'est la date de "
            "rappel la plus qualifiée qu'on puisse obtenir.",
        ),
        Objection(
            intitule="Nous devons passer par un marché public.",
            dire=(
                "Bien entendu. C'est justement intéressant pour nous de connaître votre fonctionnement "
                "actuel. À quel moment avez-vous généralement l'occasion de consulter de nouveaux "
                "fournisseurs ?",
            ),
        ),
        Objection(
            intitule="Nous travaillons déjà avec un autre fournisseur.",
            dire=("Je comprends. Est-ce que vous êtes satisfait de cette organisation ?",),
            puis=("Et si vous deviez retenir un point important pour votre prochain fournisseur, "
                  "ce serait lequel ?",),
        ),
        Objection(
            intitule="Envoyez-nous plutôt un mail.",
            dire=(
                "Bien sûr. Pour vous envoyer quelque chose de pertinent, pouvez-vous simplement me "
                "confirmer le bon interlocuteur et son adresse e-mail ?",
                "Et est-ce que vous souhaitez plutôt recevoir nos coordonnées pour pouvoir nous "
                "solliciter en cas de besoin, ou une présentation de nos services ?",
            ),
            conduite="Ne jamais raccrocher sur un « envoyez un mail » sans avoir obtenu l'adresse "
            "nominative. Une adresse générique de mairie = appel perdu.",
        ),
        Objection(
            intitule="Nous n'avons plus besoin de ce produit.",
            dire=("D'accord. Vos besoins ont complètement disparu ou vous avez simplement changé votre "
                  "mode d'approvisionnement ?",),
            conduite="Comprendre l'évolution : bâtiment vendu, chaufferie remplacée, flotte transférée à "
            "l'intercommunalité. Le besoin a souvent juste déménagé.",
        ),
        Objection(
            intitule="Pourquoi avez-vous besoin de ces informations ?",
            dire=(
                "Principalement pour éviter de travailler avec des informations qui ne sont plus à jour. "
                "Nous reprenons contact avec nos anciens clients pour mieux comprendre leur situation "
                "actuelle et fiabiliser nos dossiers administratifs.",
            ),
        ),
        Objection(
            intitule="Nous ne sommes plus intéressés.",
            dire=(
                "Je comprends parfaitement. Est-ce que je peux simplement vous demander ce qui a changé "
                "depuis notre dernière collaboration ? C'est uniquement pour que nous comprenions mieux "
                "les attentes actuelles de nos anciens clients.",
            ),
        ),
        Objection(
            intitule="Nous n'avons pas de besoin actuellement.",
            dire=(
                "Aucun problème. Je préfère justement le savoir plutôt que de vous solliciter "
                "inutilement. Est-ce que vous souhaitez que nous conservions simplement vos coordonnées "
                "afin de pouvoir vous recontacter uniquement si vous nous le demandez ?",
            ),
        ),
    ),
)

_PROFESSIONNELS = Script(
    cle="pro",
    libelle="Professionnel",
    couleur="#e07b00",
    regles_or=(
        "On prend des nouvelles de l'activité, pas du fichier. Un pro sent en dix secondes "
        "si l'appel parle de lui ou de notre base de données.",
        "Face à une mauvaise expérience : ne pas se défendre, écouter, noter, faire remonter. "
        "C'est la seule attitude qui laisse une porte ouverte.",
        "La question qui fait tout l'appel : « qu'est-ce qu'il faudrait qu'Hympyr fasse différemment ? »",
    ),
    etapes=(
        Etape(
            numero="1",
            titre="Ouverture",
            dire=(
                "Bonjour Monsieur/Madame {client}, {appelant} de chez Hympyr Énergies. Je me permets de "
                "vous appeler car cela fait quelque temps que nous n'avons pas eu l'occasion de "
                "travailler ensemble.",
                "Vous aviez l'habitude de vous approvisionner chez nous en {produit}. Je voulais "
                "simplement prendre de vos nouvelles, savoir comment votre activité évolue et si vous "
                "avez toujours les mêmes besoins en carburant. Je ne vais pas vous retenir longtemps.",
            ),
        ),
        Etape(
            numero="2",
            titre="Comprendre l'activité actuelle",
            branches_titre="Choisir le métier du client",
            groupe="metier",
            branches=(
                Branche(
                    label="TP / BTP",
                    dire=("Vous avez toujours votre activité de travaux publics ?",),
                    relances=(
                        "Votre parc d'engins est resté à peu près le même ?",
                        "Vous utilisez toujours du GNR pour vos engins ?",
                        "Vos besoins ont augmenté ou diminué ces dernières années ?",
                        "Vous vous faites toujours livrer directement sur vos chantiers ou plutôt sur "
                        "votre dépôt ?",
                    ),
                ),
                Branche(
                    label="Transport",
                    dire=("Vous êtes toujours sur une activité de transport ?",),
                    relances=(
                        "Votre flotte a évolué depuis notre dernière commande ?",
                        "Vous vous approvisionnez toujours de la même manière ?",
                        "Vous avez principalement besoin de carburant pour votre flotte ou également "
                        "pour votre dépôt ?",
                    ),
                ),
                Branche(
                    label="Agriculture",
                    dire=("Vous êtes toujours en activité agricole ?",),
                    relances=(
                        "Votre parc de tracteurs et d'engins a évolué ?",
                        "Vous utilisez toujours du GNR ?",
                        "Vous stockez toujours votre carburant sur l'exploitation ?",
                        "Vos besoins sont plutôt concentrés sur certaines périodes de l'année ?",
                    ),
                    note="La saisonnalité est l'information à ramener : semis, moisson, vendanges. "
                    "Elle vaut une date de rappel.",
                ),
            ),
        ),
        Etape(
            numero="3",
            titre="Comprendre le changement de fournisseur",
            dire=(
                "Et aujourd'hui, vous vous approvisionnez auprès de qui ou de quelle manière ?",
                "Est-ce que je peux vous demander ce qui vous avait fait changer à l'époque ?",
            ),
            branches_titre="Selon le motif invoqué",
            branches=(
                Branche(
                    label="Prix",
                    dire=("Je comprends. Et aujourd'hui, le prix reste votre principal critère de choix "
                          "ou d'autres éléments comptent également, comme la disponibilité ou les délais ?",),
                ),
                Branche(
                    label="Livraison",
                    dire=("D'accord. Et qu'est-ce qui est important pour vous aujourd'hui dans une "
                          "livraison ? La rapidité, la possibilité d'être livré sur site, la souplesse… ?",),
                ),
                Branche(
                    label="Disponibilité",
                    dire=("Je comprends. C'est particulièrement important lorsqu'un engin ou un véhicule "
                          "doit continuer à travailler. Aujourd'hui, vous êtes satisfait de la "
                          "disponibilité de votre fournisseur ?",),
                ),
                Branche(
                    label="Mauvaise expérience Hympyr",
                    dire=(
                        "Je suis désolé que vous ayez eu cette expérience. Merci de me le dire "
                        "franchement. Est-ce que vous pouvez m'expliquer ce qui s'était passé ?",
                        "Merci pour votre retour. C'est important pour nous de le savoir.",
                    ),
                    note="Ne pas se défendre. Écouter et noter. Le litige doit remonter en interne "
                    "même si le client ne redevient pas client.",
                ),
            ),
        ),
        Etape(
            numero="4",
            titre="Tester la possibilité de revenir",
            dire=(
                "Si demain vous aviez besoin d'un nouveau fournisseur, qu'est-ce qu'il faudrait que "
                "Hympyr fasse différemment pour que vous envisagiez de retravailler avec nous ?",
                "Et aujourd'hui, est-ce que vous seriez ouvert à nous consulter ponctuellement si vous "
                "aviez un besoin ?",
            ),
            note="C'est LA question du script. Elle transforme l'appel en entretien de reconquête "
            "plutôt qu'en mise à jour administrative. Retranscrire la réponse mot à mot.",
        ),
        Etape(
            numero="5",
            titre="Mise à jour des informations",
            dire=(
                "J'aimerais également profiter de notre échange pour vérifier que nous avons toujours "
                "les bonnes informations vous concernant. Comme cela fait quelque temps que nous n'avons "
                "pas travaillé ensemble, je préfère que notre dossier soit correctement à jour.",
            ),
            relances=(
                "Raison sociale",
                "Nom du responsable / interlocuteur",
                "Téléphone",
                "E-mail",
                "Adresse de facturation",
                "Adresse du dépôt",
                "Adresses de livraison éventuelles",
                "Activité actuelle",
                "Énergies utilisées",
                "SIREN / SIRET et informations nécessaires à la facturation",
            ),
        ),
        Etape(
            numero="6",
            titre="Introduire la facturation électronique",
            dire=(
                "Nous faisons aussi ce travail parce que les entreprises doivent progressivement "
                "s'adapter à la généralisation de la facturation électronique. Depuis le 1er septembre "
                "2026, toutes les entreprises doivent être en mesure de recevoir leurs factures au "
                "format électronique, et pour les TPE et PME, l'émission devient obligatoire au "
                "1er septembre 2027. L'idée est donc d'avoir des informations clients fiables et à jour.",
                "Mais au-delà de l'aspect administratif, cela nous permet surtout de savoir si votre "
                "activité et vos besoins ont changé depuis notre dernière commande.",
            ),
            note="C'est la seconde phrase qui compte : toujours refermer sur le client, jamais sur "
            "l'administratif. Fait vérifié (calendrier officiel, economie.gouv.fr) : réception "
            "obligatoire pour TOUTES les entreprises assujetties à la TVA au 01/09/2026 ; émission "
            "au 01/09/2026 pour les grandes entreprises et ETI, au 01/09/2027 pour les PME, TPE et "
            "micro-entreprises. L'acheminement se fait par SIREN/SIRET via l'annuaire : c'est ce qui "
            "justifie concrètement qu'on demande le SIREN.",
        ),
        Etape(
            numero="7",
            titre="Identifier un besoin futur",
            dire=(
                "Et sur les prochains mois, vous pensez avoir des besoins en {produit} ?",
                "Vous avez déjà une idée des périodes auxquelles vous aurez besoin d'être livré ?",
                "Très bien. Je préfère le noter pour que nous sachions quand vous serez susceptible "
                "d'avoir besoin de nous, plutôt que de vous appeler au hasard.",
            ),
        ),
        Etape(
            numero="8",
            titre="Conclusion",
            dire=(
                "Merci pour votre temps. Ça m'a surtout permis de prendre de vos nouvelles et de "
                "comprendre comment votre activité a évolué depuis notre dernière commande.",
                "Je mets votre dossier à jour avec les informations que vous m'avez données. Et si vous "
                "avez besoin de nous à nouveau, même ponctuellement, n'hésitez pas à nous solliciter.",
                "Bonne continuation et bonne journée à vous.",
            ),
        ),
    ),
    a_consigner=(
        "Activité toujours en cours : oui / non — évolution du parc ou de la flotte",
        "Fournisseur actuel et motif du changement",
        "Réponse mot à mot à « qu'est-ce qu'il faudrait faire différemment ? »",
        "Période(s) de besoin annoncée(s) → date de rappel",
        "SIREN / SIRET, e-mail de facturation, adresses de livraison",
        "Litige à faire remonter en interne : oui / non",
    ),
    objections=(
        Objection(
            intitule="Je travaille déjà avec quelqu'un.",
            dire=("Bien sûr, je comprends. Est-ce que vous êtes satisfait de votre fournisseur actuel ?",),
            puis=("Et si vous deviez changer quelque chose dans votre approvisionnement actuel, "
                  "ce serait quoi ?",),
            conduite="L'objectif n'est pas de dénigrer le concurrent, mais de comprendre les attentes.",
        ),
        Objection(
            intitule="Je prends chez celui qui est moins cher.",
            dire=(
                "Je comprends. Sur le carburant, le prix est évidemment un critère important. Est-ce que "
                "vous regardez uniquement le prix ou également la livraison, les délais et la "
                "disponibilité ?",
            ),
            conduite="Puis écouter. Ne pas enchaîner sur une comparaison tarifaire au téléphone.",
        ),
        Objection(
            intitule="Je n'ai plus besoin de GNR.",
            dire=(
                "D'accord. Votre activité a évolué ou vous avez changé d'équipement ?",
                "Vous utilisez aujourd'hui quelle énergie pour vos engins ?",
            ),
        ),
        Objection(
            intitule="Je n'ai pas le temps.",
            dire=(
                "Aucun souci. Je voulais surtout savoir si vous aviez toujours la même activité et si "
                "vous utilisiez toujours {produit}. Si vous me confirmez simplement cela, je ne vous "
                "retiens pas davantage.",
            ),
        ),
        Objection(
            intitule="Vous appelez juste pour mettre votre fichier à jour ?",
            dire=(
                "C'est une partie de la démarche, oui, mais ce n'est pas la seule raison. Comme cela "
                "fait longtemps que nous n'avons pas travaillé ensemble, nous souhaitons surtout "
                "comprendre ce qui a changé pour vous : votre activité, vos besoins, votre façon de vous "
                "approvisionner. La mise à jour administrative vient en complément.",
            ),
        ),
        Objection(
            intitule="Je ne veux pas vous donner mon SIREN / mes informations.",
            dire=(
                "Je comprends. Je ne souhaite pas vous demander d'informations inutiles. Certaines "
                "données administratives sont simplement nécessaires pour fiabiliser nos dossiers et, "
                "pour les entreprises concernées, préparer les évolutions liées à la facturation "
                "électronique. Si vous préférez, nous pouvons nous limiter aux coordonnées que vous "
                "souhaitez actualiser.",
            ),
            conduite="Argument factuel disponible si le client insiste : le SIREN sert à l'acheminement "
            "de la facture électronique via l'annuaire. Sans lui, la facture n'arrive pas. "
            "Le SIREN est par ailleurs une donnée publique (annuaire des entreprises), ce qui "
            "désamorce souvent la réticence.",
        ),
        Objection(
            intitule="Pourquoi vous me parlez de facturation électronique ?",
            dire=(
                "Parce que la réglementation évolue. Depuis le 1er septembre 2026, toute entreprise doit "
                "pouvoir recevoir ses factures au format électronique, et à partir du 1er septembre 2027 "
                "les TPE et PME devront également être en mesure de les émettre. Nous profitons donc de "
                "cette période pour fiabiliser progressivement nos données clients.",
                "Mais mon appel n'est pas uniquement administratif : cela faisait surtout longtemps que "
                "nous n'avions pas échangé et je voulais savoir où vous en étiez aujourd'hui.",
            ),
        ),
        Objection(
            intitule="Vous essayez de me faire revenir chez Hympyr ?",
            dire=(
                "Si vous avez de nouveau besoin de nous, bien sûr que nous serons heureux de retravailler "
                "avec vous. Mais je préfère d'abord comprendre pourquoi nous ne travaillons plus ensemble "
                "et ce qui est important pour vous aujourd'hui.",
            ),
        ),
        Objection(
            intitule="J'ai eu un problème avec Hympyr à l'époque.",
            dire=(
                "Je suis désolé de l'entendre. Est-ce que vous pouvez me raconter ce qui s'était passé ?",
                "Merci de me l'avoir expliqué. Je préfère avoir ce type de retour plutôt que de faire "
                "comme si tout s'était bien passé. Je vais le faire remonter en interne.",
            ),
            conduite="Laisser parler. Ne jamais interrompre, ne jamais expliquer « ce qui s'est "
            "réellement passé ». Noter les faits, la date approximative, le montant si évoqué.",
            puis=("Si le contexte s'y prête seulement : « Si vous deviez envisager de retravailler avec "
                  "nous un jour, qu'est-ce qu'il faudrait absolument que nous améliorions ? »",),
            alerte="Un litige évoqué au téléphone doit être tracé dans la fiche ET remonté. "
            "S'il ne l'est pas, la promesse faite au client est fausse.",
        ),
        Objection(
            intitule="Je ne veux plus être client chez Hympyr.",
            dire=(
                "Je comprends et je respecte votre décision. Est-ce que vous accepteriez simplement de "
                "me dire ce qui vous avait conduit à arrêter de travailler avec nous ? Cela nous "
                "aiderait à comprendre ce que nous pouvons améliorer.",
            ),
            conduite="Puis ne pas insister. Une relance de plus transforme un ancien client neutre en "
            "détracteur actif.",
        ),
        Objection(
            intitule="Envoyez-moi vos tarifs.",
            dire=(
                "Bien sûr. Pour vous envoyer quelque chose qui corresponde réellement à votre besoin, "
                "est-ce que vous utilisez toujours {produit} et sur quel type d'activité ?",
                "Très bien. Je préfère vous envoyer quelque chose de pertinent plutôt qu'une offre "
                "générale.",
            ),
            conduite="Recueillir uniquement les informations utiles : produit, volume approximatif, "
            "adresse de livraison, e-mail. Puis créer la tâche d'envoi, sinon la promesse est perdue.",
        ),
    ),
)

SCRIPTS: dict[str, Script] = {
    s.cle: s for s in (_PARTICULIERS, _PROFESSIONNELS, _COLLECTIVITES)
}

ORDRE_ONGLETS: tuple[str, ...] = ("particulier", "pro", "collectivite")


# ═══════════════════════════════════════════════════════════════════════════
# 3. DÉTECTION DE LA TYPOLOGIE
# ═══════════════════════════════════════════════════════════════════════════

def _sans_accents(texte: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texte) if unicodedata.category(c) != "Mn"
    ).lower()


# Formes juridiques et marqueurs d'entité publique. Volontairement larges :
# un faux positif se corrige d'un clic sur l'onglet, un faux négatif fait
# dérouler le mauvais script pendant tout l'appel.
_MOTS_PUBLIC = (
    "mairie", "commune de", "ville de", "syndicat", "sivom", "sivu", "siaep",
    "smictom", "sictom", "ccas", "sdis", "communaute de communes",
    "communaute d agglomeration", "conseil departemental", "departement de",
    "region ", "college", "lycee", "ehpad", "chu ", "centre hospitalier",
    "office public", "opac", "ophlm", "regie municipale", "etablissement public",
    "cias", "smd", "sydeco", "sdet", "prefecture", "gendarmerie", "sivos",
)

_MOTS_SOCIETE = (
    "sarl", "s.a.r.l", " sas", "s.a.s", "sasu", " sa ", "eurl", "snc", "sci",
    "earl", "gaec", "scea", "sce ", "sccv", "sarlu", "entreprise", "ets ",
    "etablissements", "societe", "cooperative", "cuma", "groupement",
)

_MOTS_AGRI = ("earl", "gaec", "scea", "agricole", "exploitation", "ferme", "domaine",
              "elevage", "viticole", "vignoble", "cuma", "polyculture")
_MOTS_TRANSPORT = ("transport", "transports", "logistique", "messagerie", "fret",
                   "autocar", "taxi", "ambulance", "deménagement", "demenagement")
_MOTS_TP = ("tp", "travaux publics", "btp", "terrassement", "batiment", "vrd",
            "carriere", "demolition", "assainissement", "genie civil", "maconnerie",
            "espaces verts", "paysag")

_CLES_SIRET = ("siret", "siren", "n siret", "no siret", "numero siret", "identifiant")
_CLES_TYPE = ("typologie", "type client", "type_client", "categorie", "catégorie",
              "segment", "type", "famille", "qualite", "civilite", "civilité")
_CLES_NOM = ("raison sociale", "raison_sociale", "nom", "client", "libelle",
             "libellé", "denomination", "intitule", "societe", "société")
_CLES_NAF = ("naf", "ape", "code naf", "code ape", "activite", "activité")


def _valeurs(fiche: Mapping[str, Any], cles_recherchees: Sequence[str]) -> list[str]:
    """Retourne les valeurs des colonnes dont le nom contient l'un des mots-clés."""
    trouvees: list[str] = []
    for cle, valeur in fiche.items():
        if valeur is None:
            continue
        cle_norm = _sans_accents(str(cle))
        if any(mot in cle_norm for mot in cles_recherchees):
            texte = str(valeur).strip()
            if texte and texte.lower() not in {"nan", "none", "nat"}:
                trouvees.append(texte)
    return trouvees


def _sous_type_pro(blob: str, naf: str) -> tuple[str, str]:
    naf_num = re.sub(r"[^0-9]", "", naf)[:2]
    if naf_num in {"01", "02", "03"}:
        return "agriculture", "code NAF agricole"
    if naf_num in {"49", "52", "53"}:
        return "transport", "code NAF transport"
    if naf_num in {"41", "42", "43", "08", "23", "81"}:
        return "tp", "code NAF construction / TP"
    if any(m in blob for m in _MOTS_AGRI):
        return "agriculture", "raison sociale de type exploitation agricole"
    if any(m in blob for m in _MOTS_TRANSPORT):
        return "transport", "raison sociale de type transport"
    if any(m in blob for m in _MOTS_TP):
        return "tp", "raison sociale de type TP / BTP"
    return "", ""


def deviner_typologie(fiche: Mapping[str, Any] | None) -> Detection:
    """
    Déduit la typologie d'appel depuis une ligne du fichier clients.

    Tolérant aux noms de colonnes : on cherche par mots-clés, pas par nom exact,
    de manière à fonctionner avec « Type client », « Catégorie normalisée »,
    « Raison sociale / Nom », « SIREN » aussi bien qu'avec d'autres intitulés.

    La détection n'est qu'une pré-sélection : l'utilisateur change d'onglet d'un
    clic si elle se trompe, sans rerun et sans perdre sa saisie.
    """
    if not fiche:
        return Detection("particulier", "", "aucune donnée fiche : onglet par défaut", "faible")

    fiche = dict(fiche)

    valeurs_type = _valeurs(fiche, _CLES_TYPE)
    noms = _valeurs(fiche, _CLES_NOM)
    naf = " ".join(_valeurs(fiche, _CLES_NAF))
    # Le sous-type (TP / transport / agriculture) se cherche partout : une colonne
    # « Catégorie » vaut souvent mieux que la raison sociale pour ça.
    blob = " " + _sans_accents(" ".join(noms + valeurs_type + [naf])) + " "

    def _resultat_pro(motif: str, confiance: str) -> Detection:
        sous, motif_sous = _sous_type_pro(blob, naf)
        return Detection("pro", sous, motif + (f" · {motif_sous}" if motif_sous else ""), confiance)

    # -- 1. Colonne de type explicite : la source la plus fiable ---------------
    for brut in valeurs_type:
        v = _sans_accents(brut)
        if any(m in v for m in ("collectiv", "mairie", "public", "administration", "commune")):
            return Detection("collectivite", "", f"colonne de type : « {brut} »", "forte")
        if any(m in v for m in ("particulier", "prive", "menage", "domestique", "monsieur", "madame")):
            return Detection("particulier", "", f"colonne de type : « {brut} »", "forte")
        if any(m in v for m in ("pro", "entreprise", "societe", "agri", "tp", "transport",
                                "btp", "asso", "artisan", "commerc")):
            # « Pro (déduit) » reste une déduction du fichier : on le signale.
            confiance = "moyenne" if "deduit" in v else "forte"
            return _resultat_pro(f"colonne de type : « {brut} »", confiance)

    # -- 2. Marqueurs d'entité publique dans la raison sociale ----------------
    if any(mot in blob for mot in _MOTS_PUBLIC):
        return Detection("collectivite", "", "raison sociale de type entité publique", "forte")

    # -- 3. SIREN / SIRET -----------------------------------------------------
    for brut in _valeurs(fiche, _CLES_SIRET):
        chiffres = re.sub(r"[^0-9]", "", brut)
        if len(chiffres) in (9, 14):
            # Convention INSEE : les SIREN des organismes publics commencent par
            # 1 (services de l'État) ou 2 (collectivités territoriales).
            # Heuristique à confirmer sur un échantillon du fichier réel.
            if chiffres[0] in "12":
                return Detection(
                    "collectivite", "",
                    f"SIREN commençant par {chiffres[0]} (organisme public)", "moyenne",
                )
            return _resultat_pro("SIREN/SIRET renseigné", "forte")

    # -- 4. Forme juridique dans la raison sociale ----------------------------
    if any(mot in blob for mot in _MOTS_SOCIETE):
        return _resultat_pro("forme juridique détectée dans la raison sociale", "moyenne")

    # -- 5. Par défaut : particulier ------------------------------------------
    return Detection(
        "particulier", "",
        "aucun marqueur professionnel ni public trouvé", "faible",
    )


# ═══════════════════════════════════════════════════════════════════════════
# 4. RENDU HTML
# ═══════════════════════════════════════════════════════════════════════════

def _p(texte: str, jetons: Mapping[str, str]) -> str:
    """Substitue les jetons puis échappe le HTML."""
    rendu = texte
    for cle, valeur in jetons.items():
        rendu = rendu.replace("{" + cle + "}", valeur)
    return _html.escape(rendu)


_CSS = """
* { box-sizing: border-box; }
body { margin: 0; padding: 0; font-family: "Source Sans Pro", -apple-system,
       BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
.sa-root {
  --fond: #ffffff; --fond2: #f6f7f9; --bord: #dfe3e8; --texte: #17202a;
  --doux: #5b6773; --accent: #1565c0; --dire-fond: #f0f5fb;
  display: flex; flex-direction: column; height: 100vh;
  background: var(--fond); color: var(--texte); font-size: 14.5px; line-height: 1.55;
}
.sa-root[data-theme="dark"] {
  --fond: #0e1117; --fond2: #171b23; --bord: #2b323d; --texte: #e6e9ef;
  --doux: #9aa4b2; --dire-fond: #1a2130;
}
/* ---- barre haute ---- */
.sa-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
           padding: 2px 2px 10px; border-bottom: 1px solid var(--bord); }
.sa-tabs { display: flex; gap: 6px; }
.sa-tab { border: 1px solid var(--bord); background: var(--fond2); color: var(--doux);
          padding: 6px 14px; border-radius: 999px; cursor: pointer; font-size: 13.5px;
          font-weight: 600; transition: all .12s; }
.sa-tab:hover { border-color: var(--accent); color: var(--texte); }
.sa-tab[aria-selected="true"] { background: var(--accent); border-color: var(--accent);
          color: #fff; }
.sa-ctx { margin-left: auto; font-size: 12.5px; color: var(--doux); text-align: right; }
.sa-ctx b { color: var(--texte); }
.sa-detect { font-size: 11.5px; color: var(--doux); font-style: italic; }
/* ---- grille ---- */
.sa-grid { display: grid; grid-template-columns: 1.15fr .85fr; gap: 16px;
           flex: 1; min-height: 0; padding-top: 12px; }
.sa-col { overflow-y: auto; padding-right: 8px; min-height: 0; }
.sa-col::-webkit-scrollbar { width: 8px; }
.sa-col::-webkit-scrollbar-thumb { background: var(--bord); border-radius: 4px; }
.sa-coltitre { font-size: 11px; letter-spacing: .09em; text-transform: uppercase;
               color: var(--doux); font-weight: 700; margin: 0 0 8px; position: sticky;
               top: 0; background: var(--fond); padding: 2px 0 6px; z-index: 2; }
/* ---- règles d'or ---- */
.sa-or { background: var(--fond2); border-left: 3px solid var(--accent);
         border-radius: 0 6px 6px 0; padding: 10px 12px; margin-bottom: 14px; }
.sa-or ul { margin: 0; padding-left: 18px; }
.sa-or li { margin: 3px 0; font-size: 13.5px; }
/* ---- étapes ---- */
.sa-etape { border: 1px solid var(--bord); border-radius: 8px; padding: 12px 14px;
            margin-bottom: 10px; background: var(--fond); }
.sa-etape h4 { margin: 0 0 8px; font-size: 14.5px; display: flex; align-items: center;
               gap: 8px; }
.sa-num { background: var(--accent); color: #fff; width: 22px; height: 22px;
          border-radius: 50%; display: inline-flex; align-items: center;
          justify-content: center; font-size: 12px; font-weight: 700; flex: 0 0 auto; }
.sa-dire { background: var(--dire-fond); border-left: 3px solid var(--accent);
           padding: 8px 11px; margin: 6px 0; border-radius: 0 5px 5px 0; }
.sa-dire p { margin: 0 0 6px; }
.sa-dire p:last-child { margin-bottom: 0; }
.sa-relances { margin: 8px 0 0; padding-left: 18px; color: var(--texte); }
.sa-relances li { margin: 2px 0; font-size: 13.5px; }
.sa-note { margin-top: 9px; font-size: 12.8px; color: var(--doux);
           border-top: 1px dashed var(--bord); padding-top: 7px; }
.sa-note::before { content: "⚑ "; }
.sa-alerte { margin-top: 9px; font-size: 12.8px; background: #fff4e5; color: #8a4b00;
             border-radius: 5px; padding: 8px 10px; }
.sa-root[data-theme="dark"] .sa-alerte { background: #2e2210; color: #f0b872; }
/* ---- sous-onglets de branche ---- */
.sa-branchtitre { font-size: 12.5px; color: var(--doux); margin: 2px 0 6px; }
.sa-sub { display: flex; gap: 5px; flex-wrap: wrap; margin-bottom: 8px; }
.sa-subtab { border: 1px solid var(--bord); background: transparent; color: var(--doux);
             padding: 3px 10px; border-radius: 5px; cursor: pointer; font-size: 12.5px; }
.sa-subtab[aria-selected="true"] { background: var(--accent); color: #fff;
             border-color: var(--accent); }
/* ---- objections ---- */
.sa-search { width: 100%; padding: 8px 11px; border: 1px solid var(--bord);
             border-radius: 7px; background: var(--fond2); color: var(--texte);
             font-size: 13.5px; margin-bottom: 10px; }
.sa-search:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
.sa-compte { font-size: 12px; color: var(--doux); margin: -4px 0 8px; }
details.sa-obj { border: 1px solid var(--bord); border-radius: 7px; margin-bottom: 7px;
                 background: var(--fond); overflow: hidden; }
details.sa-obj > summary { cursor: pointer; padding: 9px 12px; font-weight: 600;
                 font-size: 13.6px; list-style: none; display: flex; gap: 8px;
                 align-items: flex-start; }
details.sa-obj > summary::-webkit-details-marker { display: none; }
details.sa-obj > summary::before { content: "▸"; color: var(--accent);
                 transition: transform .12s; flex: 0 0 auto; }
details.sa-obj[open] > summary::before { transform: rotate(90deg); }
details.sa-obj[open] > summary { background: var(--fond2); }
.sa-rep { padding: 10px 12px 12px; border-top: 1px solid var(--bord); }
.sa-hidden { display: none !important; }
.sa-consigner { border: 1px dashed var(--bord); border-radius: 8px; padding: 11px 14px;
                margin: 4px 0 20px; background: var(--fond2); }
.sa-consigner h4 { margin: 0 0 6px; font-size: 13px; text-transform: uppercase;
                letter-spacing: .06em; color: var(--doux); }
.sa-consigner ul { margin: 0; padding-left: 18px; font-size: 13.3px; }
.sa-vide { color: var(--doux); font-size: 13px; padding: 10px 2px; }
@media (max-width: 860px) {
  .sa-grid { grid-template-columns: 1fr; }
}
"""

_JS = """
(function () {
  var root = document.querySelector('.sa-root');

  function activer(cle) {
    root.querySelectorAll('.sa-tab').forEach(function (b) {
      b.setAttribute('aria-selected', String(b.dataset.cle === cle));
    });
    root.querySelectorAll('.sa-panel').forEach(function (p) {
      p.classList.toggle('sa-hidden', p.dataset.cle !== cle);
    });
    var s = root.querySelector('.sa-panel[data-cle="' + cle + '"] .sa-couleur');
    if (s) { root.style.setProperty('--accent', s.dataset.couleur); }
    filtrer();
  }

  root.querySelectorAll('.sa-tab').forEach(function (b) {
    b.addEventListener('click', function () { activer(b.dataset.cle); });
  });

  // sous-onglets de branche (métier du client, motif de départ)
  root.querySelectorAll('.sa-sub').forEach(function (groupe) {
    groupe.querySelectorAll('.sa-subtab').forEach(function (b) {
      b.addEventListener('click', function () {
        groupe.querySelectorAll('.sa-subtab').forEach(function (o) {
          o.setAttribute('aria-selected', String(o === b));
        });
        var conteneur = groupe.parentElement;
        conteneur.querySelectorAll('.sa-branche').forEach(function (d) {
          d.classList.toggle('sa-hidden', d.dataset.idx !== b.dataset.idx);
        });
      });
    });
  });

  function normaliser(t) {
    return t.normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
  }

  var champ = root.querySelector('.sa-search');

  function filtrer() {
    var terme = normaliser(champ.value.trim());
    var panneau = root.querySelector('.sa-objs .sa-panel:not(.sa-hidden)');
    if (!panneau) { return; }
    var visibles = 0;
    var items = panneau.querySelectorAll('details.sa-obj');
    items.forEach(function (d) {
      var ok = terme === '' || normaliser(d.dataset.index || '').indexOf(terme) !== -1;
      d.classList.toggle('sa-hidden', !ok);
      if (ok) { visibles++; }
      if (terme === '') { d.open = false; }
      else if (ok && visibles === 1) { d.open = true; }
    });
    var compte = root.querySelector('.sa-compte');
    compte.textContent = terme === ''
      ? items.length + ' objection(s) — tape pour filtrer, ou « / » pour venir ici'
      : visibles + ' / ' + items.length + ' objection(s) correspondent';
  }

  champ.addEventListener('input', filtrer);
  document.addEventListener('keydown', function (e) {
    if (e.key === '/' && document.activeElement !== champ) {
      e.preventDefault(); champ.focus();
    }
    if (e.key === 'Escape' && document.activeElement === champ) {
      champ.value = ''; filtrer(); champ.blur();
    }
  });

  activer(root.dataset.initial);
})();
"""


_LABELS_METIER = {"tp": "tp", "transport": "transport", "agriculture": "agriculture"}


def _index_preselection(etape: Etape, sous_type: str) -> int:
    """Index de la branche à ouvrir par défaut (métier déduit de la fiche)."""
    if etape.groupe != "metier" or not sous_type:
        return 0
    cible = _LABELS_METIER.get(sous_type, "")
    for i, branche in enumerate(etape.branches):
        label = _sans_accents(branche.label)
        if cible and (cible in label or (cible == "tp" and "btp" in label)):
            return i
    return 0


def _rendu_branches(etape: Etape, jetons: Mapping[str, str], sous_type: str = "") -> str:
    if not etape.branches:
        return ""
    actif = _index_preselection(etape, sous_type)
    morceaux = []
    if etape.branches_titre:
        morceaux.append(f'<div class="sa-branchtitre">{_html.escape(etape.branches_titre)}</div>')
    onglets = "".join(
        f'<button class="sa-subtab" data-idx="{i}" aria-selected="{str(i == actif).lower()}">'
        f"{_html.escape(b.label)}</button>"
        for i, b in enumerate(etape.branches)
    )
    morceaux.append(f'<div class="sa-sub">{onglets}</div>')
    for i, branche in enumerate(etape.branches):
        cache = "" if i == actif else " sa-hidden"
        corps = []
        if branche.dire:
            phrases = "".join(f"<p>« {_p(d, jetons)} »</p>" for d in branche.dire)
            corps.append(f'<div class="sa-dire">{phrases}</div>')
        if branche.relances:
            items = "".join(f"<li>{_p(r, jetons)}</li>" for r in branche.relances)
            corps.append(f'<ul class="sa-relances">{items}</ul>')
        if branche.note:
            corps.append(f'<div class="sa-note">{_p(branche.note, jetons)}</div>')
        morceaux.append(
            f'<div class="sa-branche{cache}" data-idx="{i}">{"".join(corps)}</div>'
        )
    return "".join(morceaux)


def _rendu_etape(etape: Etape, jetons: Mapping[str, str], sous_type: str = "") -> str:
    corps = []
    if etape.dire:
        phrases = "".join(f"<p>« {_p(d, jetons)} »</p>" for d in etape.dire)
        corps.append(f'<div class="sa-dire">{phrases}</div>')
    if etape.relances:
        items = "".join(f"<li>{_p(r, jetons)}</li>" for r in etape.relances)
        corps.append(f'<ul class="sa-relances">{items}</ul>')
    corps.append(_rendu_branches(etape, jetons, sous_type))
    if etape.note:
        corps.append(f'<div class="sa-note">{_p(etape.note, jetons)}</div>')
    return (
        f'<div class="sa-etape"><h4><span class="sa-num">{_html.escape(etape.numero)}</span>'
        f"{_html.escape(etape.titre)}</h4>{''.join(corps)}</div>"
    )


def _rendu_objection(objection: Objection, jetons: Mapping[str, str]) -> str:
    corps = []
    if objection.dire:
        phrases = "".join(f"<p>« {_p(d, jetons)} »</p>" for d in objection.dire)
        corps.append(f'<div class="sa-dire">{phrases}</div>')
    if objection.puis:
        items = "".join(f"<li>{_p(p, jetons)}</li>" for p in objection.puis)
        corps.append(f'<ul class="sa-relances">{items}</ul>')
    if objection.conduite:
        corps.append(f'<div class="sa-note">{_p(objection.conduite, jetons)}</div>')
    if objection.alerte:
        corps.append(f'<div class="sa-alerte">{_p(objection.alerte, jetons)}</div>')
    brut = " ".join(
        (objection.intitule,) + objection.dire + objection.puis
        + (objection.conduite, objection.alerte)
    )
    for cle, valeur in jetons.items():
        brut = brut.replace("{" + cle + "}", valeur)
    index = _sans_accents(brut)
    return (
        f'<details class="sa-obj" data-index="{_html.escape(index, quote=True)}">'
        f"<summary>{_p(objection.intitule, jetons)}</summary>"
        f'<div class="sa-rep">{"".join(corps)}</div></details>'
    )


def _construire_html(
    typologie: str,
    sous_type: str,
    contexte: Contexte,
    motif_detection: str,
    theme: str,
) -> str:
    jetons = contexte.jetons()

    onglets = "".join(
        f'<button class="sa-tab" data-cle="{cle}" '
        f'aria-selected="{str(cle == typologie).lower()}">{_html.escape(SCRIPTS[cle].libelle)}</button>'
        for cle in ORDRE_ONGLETS
    )

    panneaux_script = []
    panneaux_obj = []
    for cle in ORDRE_ONGLETS:
        script = SCRIPTS[cle]
        cache = "" if cle == typologie else " sa-hidden"

        regles = "".join(f"<li>{_p(r, jetons)}</li>" for r in script.regles_or)
        etapes = "".join(
            _rendu_etape(e, jetons, sous_type if cle == typologie else "")
            for e in script.etapes
        )
        consigner = "".join(f"<li>{_p(c, jetons)}</li>" for c in script.a_consigner)
        panneaux_script.append(
            f'<div class="sa-panel{cache}" data-cle="{cle}">'
            f'<span class="sa-couleur" data-couleur="{script.couleur}" hidden></span>'
            f'<div class="sa-or"><ul>{regles}</ul></div>'
            f"{etapes}"
            f'<div class="sa-consigner"><h4>À consigner avant de raccrocher</h4>'
            f"<ul>{consigner}</ul></div>"
            f"</div>"
        )
        objs = "".join(_rendu_objection(o, jetons) for o in script.objections)
        panneaux_obj.append(f'<div class="sa-panel{cache}" data-cle="{cle}">{objs}</div>')

    contexte_html = ""
    if contexte.client:
        contexte_html += f"<b>{_html.escape(contexte.client)}</b>"
    if contexte.produit:
        contexte_html += f" · {_html.escape(contexte.produit)}"
    if motif_detection:
        contexte_html += f'<br><span class="sa-detect">Typologie déduite : {_html.escape(motif_detection)}</span>'

    return (
        "<!DOCTYPE html><html lang='fr'><head><meta charset='utf-8'>"
        f"<style>{_CSS}</style></head><body>"
        f'<div class="sa-root" data-theme="{theme}" data-initial="{typologie}">'
        f'<div class="sa-head"><div class="sa-tabs">{onglets}</div>'
        f'<div class="sa-ctx">{contexte_html}</div></div>'
        '<div class="sa-grid">'
        f'<section class="sa-col sa-scripts"><div class="sa-coltitre">Déroulé de l\'appel</div>'
        f'{"".join(panneaux_script)}</section>'
        f'<aside class="sa-col sa-objs"><div class="sa-coltitre">Objections — réponses types</div>'
        '<input class="sa-search" type="search" placeholder="Chercher une objection : prix, temps, marché, SIREN…">'
        '<div class="sa-compte"></div>'
        f'{"".join(panneaux_obj)}</aside>'
        "</div></div>"
        f"<script>{_JS}</script></body></html>"
    )


def _theme_actif() -> str:
    """Suit le thème Streamlit, avec repli sur le mode clair."""
    try:  # Streamlit >= 1.44
        type_theme = getattr(getattr(st, "context", None), "theme", None)
        if type_theme is not None and getattr(type_theme, "type", None):
            return "dark" if type_theme.type == "dark" else "light"
    except Exception:  # pragma: no cover - dépend de la version
        pass
    try:
        return "dark" if st.get_option("theme.base") == "dark" else "light"
    except Exception:  # pragma: no cover
        return "light"


# ═══════════════════════════════════════════════════════════════════════════
# 5. POINTS D'ENTRÉE
# ═══════════════════════════════════════════════════════════════════════════

def panneau_scripts(
    typologie: str = "particulier",
    sous_type: str = "",
    contexte: Contexte | None = None,
    motif_detection: str = "",
    hauteur: int = 720,
) -> None:
    """
    Affiche le panneau scripts + objections.

    Ne crée aucun widget Streamlit : aucun rerun n'est déclenché par les
    interactions internes (onglets, recherche, accordéons).
    """
    if typologie not in SCRIPTS:
        typologie = "particulier"
    html_doc = _construire_html(
        typologie=typologie,
        sous_type=sous_type,
        contexte=contexte or Contexte(),
        motif_detection=motif_detection,
        theme=_theme_actif(),
    )
    components.html(html_doc, height=hauteur, scrolling=False)


def dialogue_scripts(
    typologie: str = "particulier",
    sous_type: str = "",
    contexte: Contexte | None = None,
    motif_detection: str = "",
) -> None:
    """
    Variante modale plein écran.

    À n'utiliser que si la fiche client n'a pas besoin d'être saisie en même
    temps que la lecture du script : une modale Streamlit bloque l'interaction
    avec la page qui se trouve derrière.
    """

    @st.dialog("Script d'appel", width="large")
    def _boite() -> None:
        panneau_scripts(typologie, sous_type, contexte, motif_detection, hauteur=640)

    _boite()
