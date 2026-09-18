# Cockpit appels — Hympyr Énergies

Application Streamlit de suivi des appels clients et des points de livraison.

## Persistance obligatoire sur Streamlit Community Cloud

Le disque local de l'application ne doit pas être considéré comme une
sauvegarde. En production, l'application utilise PostgreSQL lorsque le secret
`database.url` est défini. SQLite reste uniquement un mode de développement
local.

1. Créer une base PostgreSQL hébergée dans l'Union européenne, avec TLS et
   sauvegardes automatiques activées.
2. Dans Streamlit Community Cloud, ouvrir **Manage app > Settings > Secrets**.
3. Copier le contenu de `.streamlit/secrets.toml.example`, remplacer l'URL et
   les empreintes, puis enregistrer.
4. Redémarrer l'application et vérifier la pastille
   **« Sauvegarde serveur active »** dans la barre latérale.
5. Au premier démarrage seulement, charger le classeur clients et les deux CSV
   de reprise. Le classeur et toutes les validations suivantes seront alors
   conservés dans PostgreSQL.

La variable d'environnement `HYMPYR_DATABASE_URL` peut remplacer
`database.url` sur un autre hébergeur.

## Garanties ajoutées

- chaque validation est enregistrée dans la base distante avant le passage à
  la fiche suivante ;
- chaque version de fiche est conservée dans la table `historique` ;
- le classeur clients est restauré automatiquement après un redémarrage ;
- les connexions PostgreSQL sont retentées lors d'une micro-coupure ;
- une erreur d'écriture conserve le formulaire affiché et invite à réessayer ;
- les fichiers Excel ne sont générés qu'à la demande afin d'éviter les pics de
  mémoire ;
- les exports restent disponibles, mais ne sont plus le mécanisme de reprise.

## Vérifications après déploiement

1. Valider une fiche client et un point de livraison.
2. Redémarrer volontairement l'application depuis **Manage app**.
3. Se reconnecter sans réimporter de fichier et vérifier les deux fiches.
4. Contrôler les tables `suivi`, `suivi_adresses`, `historique` et `fichiers`
   dans PostgreSQL.
5. Activer les sauvegardes quotidiennes et, si le fournisseur le permet, la
   restauration à un instant donné.

Les journaux Streamlit restent nécessaires pour diagnostiquer la cause exacte
d'une coupure (mémoire, dépendance, réseau ou redémarrage de plateforme).
