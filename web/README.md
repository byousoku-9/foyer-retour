# S'installer au Luxembourg — copie servie sous `/guide/`

Forké de [lux-guide/lux-guide.github.io](https://github.com/lux-guide/lux-guide.github.io), édition `git:a8e8593`
(snapshot des fichiers de ce commit, sans `.git`, fichiers non renommés). Dans `foyer-retour`, ce site est servi
par le serveur FastAPI sous `/guide/` et son assistant est branché sur `/api/v1/chat` ; chaque modification de
`web/` est un commit séparé qui dit pourquoi. Le README d'origine suit.

---

# S'installer au Luxembourg

Site statique : un guide d'information structuré, un simulateur de salaire net et un assistant local. Démo en ligne : https://lux-guide.github.io

Le contenu est entièrement générique et anonyme. Aucune donnée personnelle n'y figure, ni dans le code, ni dans la base de connaissances.

Tout fonctionne dans le navigateur, sans serveur : il suffit d'ouvrir `index.html`.

## Ce que contient le site

| Onglet | Contenu |
|---|---|
| Accueil | Barre de question en tête de page, recherche instantanée, entrée par catégorie et questions fréquentes |
| Fiches | Fiches thématiques illustrées, recherche plein texte, filtrage selon le profil, astuces de la communauté |
| Parcours | Frise chronologique personnalisée, étapes cochables (avancement conservé dans le navigateur) |
| Simulateur | Deux sous-onglets : salaire net par classe d'impôt, capacité d'emprunt |
| Comparateur | Quatre contrats habitation du marché luxembourgeois, sinistre par sinistre, clause citée, assureurs anonymisés |
| Carte | Écoles, crèches et arrêts de bus, tram, train autour d'une adresse (données OpenStreetMap, réseau requis) |
| Assistant | Questionnaire de profil puis recherche ciblée dans le guide, aussi en widget flottant |
| Administration | Édition du contenu et des paramètres de calcul |

Navigation directe possible par ancre : `index.html#simulateur`, `#assistant`, `#fiche/banque`, etc.

## Le simulateur

Il applique le barème officiel de l'Administration des contributions directes (fichier `bareme-2025-format-excel.xlsx` publié sur [impotsdirects.public.lu](https://impotsdirects.public.lu/fr/baremes.html)), pour les classes 1, 1a et 2.

Chaîne de calcul :

1. Cotisations sociales sur le brut plafonné à cinq fois le salaire social minimum.
2. Contribution dépendance, sans plafond, après abattement d'un quart de salaire social minimum.
3. Exonération de 50 % au titre du régime des impatriés, si l'option est cochée.
4. Forfaits de frais d'obtention et de dépenses spéciales, optionnels.
5. Impôt selon le barème, puis contribution au fonds pour l'emploi.

Limite à connaître. Le résultat correspond à la retenue sur un seul salaire. En classe 2, la déclaration retient un taux moyen mondial qui intègre les revenus étrangers exonérés : le net réel peut être inférieur. Le simulateur donne donc un plafond, pas un net définitif.

## L'assistant

Il fonctionne sans réseau. Il pose six questions pour construire un profil (situation familiale, enfants, statut, logement, véhicule, avancement), puis recherche dans la base de connaissances embarquée. Un seuil de pertinence l'empêche de répondre à côté : sous ce seuil, il dit qu'il ne sait pas plutôt que de produire une réponse plausible mais fausse.

## Modifier le contenu

Onglet Administration. On peut y éditer le titre, la catégorie, le résumé, le corps, les points à retenir, les mots-clés et les sources de chaque fiche, en créer, en supprimer, et ajuster les paramètres du simulateur (taux de cotisation, plafonds, seuils).

Les modifications sont d'abord enregistrées dans le navigateur. Pour les rendre permanentes pour tout le monde :

1. Cliquer sur **Exporter kb.js**.
2. Remplacer `app/kb.js` par le fichier téléchargé.
3. Recharger la page.

Le bouton **Revenir au contenu d'origine** annule les modifications locales.

## Structure

```
index.html            page unique
assets/               photographies sous licence libre, et CREDITS.json
app/
  bareme.js           barème officiel ACD, extrait de la source
  kb.js               base de connaissances, fiches et questions
  communaute.js       astuces de la communauté, présentées comme telles
  simulateur.js       calcul du net et de la capacité d'emprunt
  chat.js             profil et recherche dans le guide
  ui.js               rendu, comparateur, carte, widget, administration
  styles.css          thème clair et sombre
comparateur/
  contrats_kb.js      verdicts par sinistre, quatre contrats anonymisés
  sinistres.html      analyse complète, sinistre par sinistre
  reco.html           classement pondéré par un profil d'exemple
```

## Le comparateur de contrats

Treize sinistres concrets posés à quatre conditions générales publiques du marché luxembourgeois (éditions 2017 à 2023), avec pour chaque contrat un verdict typé et la clause citée avec sa page. Les assureurs sont anonymisés (A à D). Analyse documentaire : elle ne remplace ni un devis ni un conseil, le contrat en vigueur fait foi.

## Fiabilité du contenu

Chaque fiche porte ses sources officielles, consultables en un clic. Les informations ont été vérifiées en août 2026 auprès des administrations luxembourgeoises : Guichet.lu, ACD, CCSS, CNS, IGSS, ministère du Logement, Caisse pour l'avenir des enfants, ITM, ADEM, ILR, SNCA.

La réglementation évolue, en particulier les barèmes, les plafonds et les taux de cotisation. Avant toute démarche engageante, la source citée fait foi, pas ce site.

## Paramètres de calcul

Les valeurs du simulateur suivent les paramètres sociaux publiés par l'IGSS, applicables au 1er juin 2026 (indice 992,24) :

1. Salaire social minimum non qualifié : 2 771,33 € par mois, soit 33 255,96 € par an.
2. Maximum cotisable : 13 856,63 € par mois, soit cinq fois le salaire social minimum.
3. Abattement pour la contribution dépendance : 692,83 € par mois.
4. Cotisations salariales : pension 8,50 % depuis le 1er janvier 2026 (loi du 18 décembre 2025 portant réforme des pensions), maladie 3,05 %, soit 11,55 %, plus 1,40 % de dépendance.

Ces paramètres sont modifiables dans l'onglet Administration, sans toucher au code.

## Photographies

Les visuels proviennent de Wikimedia Commons, sous licence Creative Commons. Les auteurs, titres et licences exacts sont listés dans `assets/CREDITS.json`, et rappelés en bas de la page d'accueil. Toute réutilisation doit conserver ces attributions.
