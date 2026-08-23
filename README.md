# foyer-retour

Un retour à trois problématiques partagées par l'équipe IA de Foyer — un chatbot pour les nouveaux arrivants au Luxembourg, un sinistre confronté aux conditions générales d'une assurance habitation, un « RAG » sur un Excel de cent mille lignes — prouvé par deux prototypes déployés et une réponse d'architecture pour le troisième.

Le fil rouge : **on ne gagne pas sur « comment on retrouve », on gagne sur « comment on prouve ».** Chaque affirmation montrée à l'utilisateur a survécu à une vérification par du code ; ce que le système ne sait pas est dit, avec la preuve.

## État

Dépôt en construction (août 2026). Ce README décrit l'intention ; il sera complété au fil des livraisons avec : comment lancer en local, comment tester, comment déployer, comment ajouter une fiche ou un contrat.

## Ce que contiendra ce dépôt

| Dossier | Rôle |
|---|---|
| `server/` | Le serveur Python (Cloud Run) : assistant du guide, outil sinistre / conditions générales, ingestion |
| `web/` | Le site « S'installer au Luxembourg » forké depuis [lux-guide/lux-guide.github.io](https://github.com/lux-guide/lux-guide.github.io), branché sur le serveur ; modifications minimales et justifiées, une par commit |
| `tools/` | L'outil sinistre / conditions générales, la page d'accueil, la réponse au sujet 3 |
| `tests/` | Tests unitaires et **questions-témoins** rejouées en CI, résultats publiés |
| `docs/` | `architecture.md`, `choix-et-limites.md`, résultats des questions-témoins |

## Principes

- **Restitution d'abord** : source par affirmation, état explicite (sûr / partiel / inconnu), trace consultable par réponse, coût affiché.
- **Déterminisme sur les vrais invariants** : le modèle propose, le code vérifie ; pas de verdict sans clause citée et retrouvée mot pour mot dans la source.
- **L'intelligence se paie une fois, à l'ingestion** ; la requête reste légère.
- **Un document mal ingéré n'est pas servi** : rapport d'ingestion visible, quarantaine.
- **Écrit pour être relu** : petits commits qui disent pourquoi, README pour le mainteneur qui arrive.
