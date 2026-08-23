// Comparaisons standardisées de démonstration.
//
// Deux registres, à ne pas confondre :
//   1. l'assurance habitation (onglet Comparateur, plus bas) repose sur des
//      données RÉELLES : conditions générales du marché luxembourgeois,
//      verdicts par sinistre avec clause citée ;
//   2. les comparaisons de ce fichier (auto, télécom, électricité) sont des
//      DÉMONSTRATIONS : les critères sont ceux qui comptent vraiment, mais les
//      offres et les chiffres sont fictifs. Chaque vue l'affiche clairement.
//
// Structure d'une verticale :
//   offres        : noms des colonnes
//   positionnement: sous-titre d'une colonne
//   criteres      : lignes du tableau, valeurs alignées sur offres
//   questions     : le chat d'affinage ; chaque option porte des points par
//                   offre (même ordre) et une raison réutilisée dans le verdict

window.OFFRES_KB = {
  avertissement: "Démonstration : les offres et les chiffres de cette comparaison sont fictifs. " +
    "Les critères, eux, sont ceux qui distinguent réellement les contrats. " +
    "Pour décider, demandez les grilles à jour et datées des fournisseurs.",
  verticales: [
    {
      id: "auto",
      titre: "Assurance auto",
      sousTitre: "Casco, franchise, assistance : la prime ne dit pas tout",
      fiche: "assurance_auto",
      offres: ["Offre A", "Offre B", "Offre C"],
      positionnement: ["Assureur en ligne, prime basse", "Bancassureur, équilibre", "Agent généraliste, services étendus"],
      criteres: [
        { nom: "Prime annuelle indicative (RC + casco partielle)", valeurs: ["640 €", "720 €", "850 €"] },
        { nom: "Casco complète", valeurs: ["En option, +280 €/an", "Incluse si véhicule de moins de 5 ans", "En option, +220 €/an"] },
        { nom: "Franchise casco", valeurs: ["500 €", "250 €", "150 €"] },
        { nom: "Assistance 24/7 dès le domicile", valeurs: ["Non, à partir de 25 km", "Oui", "Oui"] },
        { nom: "Véhicule de remplacement", valeurs: ["5 jours", "15 jours", "30 jours"] },
        { nom: "Protection juridique", valeurs: ["En option", "Incluse", "Incluse"] },
        { nom: "Reprise du bonus étranger", valeurs: ["Sur relevé d'informations", "Sur relevé d'informations", "Sur relevé, plafonnée"] }
      ],
      questions: [
        { question: "Votre véhicule ?", options: [
          { label: "Neuf ou récent (moins de 3 ans)", points: [0, 2, 1],
            raison: "un véhicule récent justifie une casco complète, incluse dans l'offre B" },
          { label: "Occasion de plus de 5 ans", points: [2, 1, 0],
            raison: "sur une occasion, la casco complète se discute et la prime pèse davantage" },
          { label: "Citadine d'occasion à petit prix", points: [2, 0, 0],
            raison: "sur un véhicule de faible valeur, la prime basse l'emporte sur les garanties étendues" }
        ]},
        { question: "Vous roulez ?", options: [
          { label: "Beaucoup, autoroute tous les jours", points: [0, 1, 2],
            raison: "en gros rouleur, l'assistance dès le domicile et le véhicule de remplacement comptent double" },
          { label: "Surtout en ville", points: [1, 2, 1],
            raison: "en ville, la franchise casco pèse plus que l'assistance longue distance" },
          { label: "Peu, quelques trajets par semaine", points: [2, 1, 0],
            raison: "en roulant peu, chaque euro de prime compte plus que les services" }
        ]},
        { question: "En cas de panne ou d'accident ?", options: [
          { label: "Je dois pouvoir repartir le jour même", points: [0, 1, 2],
            raison: "repartir vite, c'est l'assistance 24/7 et un long véhicule de remplacement" },
          { label: "Je peux m'organiser autrement quelques jours", points: [2, 1, 0],
            raison: "sans urgence de mobilité, les services étendus se paient pour rien" }
        ]}
      ]
    },
    {
      id: "telecom",
      titre: "Forfait mobile et internet",
      sousTitre: "Data, roaming, appels vers les voisins : l'usage décide",
      fiche: "telecom",
      offres: ["Forfait A", "Forfait B", "Forfait C"],
      positionnement: ["Low cost, sans engagement", "Intermédiaire", "Tout inclus, avec fibre"],
      criteres: [
        { nom: "Prix mensuel indicatif", valeurs: ["12 €", "25 €", "45 €"] },
        { nom: "Data au Luxembourg", valeurs: ["10 Go", "60 Go", "Illimitée"] },
        { nom: "Roaming UE inclus", valeurs: ["6 Go", "25 Go", "40 Go"] },
        { nom: "Appels vers les pays voisins", valeurs: ["En supplément", "2 h incluses", "Illimités"] },
        { nom: "Internet fixe (fibre)", valeurs: ["Non proposé", "En pack, +30 €/mois", "Inclus, 1 Gbit/s"] },
        { nom: "Engagement", valeurs: ["Sans", "12 mois", "24 mois"] }
      ],
      questions: [
        { question: "Votre usage de données mobiles ?", options: [
          { label: "Léger : messages, cartes, un peu de musique", points: [2, 1, 0],
            raison: "un usage léger tient dans une petite enveloppe, le reste est payé pour rien" },
          { label: "Moyen : réseaux sociaux, vidéo de temps en temps", points: [1, 2, 0],
            raison: "l'usage moyen sature vite 10 Go mais n'exige pas l'illimité" },
          { label: "Gros : vidéo quotidienne, partage de connexion", points: [0, 1, 2],
            raison: "en gros consommateur, l'illimité évite les mauvaises surprises" }
        ]},
        { question: "Des appels réguliers vers la France, la Belgique ou l'Allemagne ?", options: [
          { label: "Oui, famille ou travail de l'autre côté de la frontière", points: [0, 1, 2],
            raison: "les appels vers les voisins font vite déborder une facture, mieux vaut de l'inclus" },
          { label: "Rarement", points: [2, 1, 0],
            raison: "sans appels transfrontaliers, cette garantie ne vaut pas son prix" }
        ]},
        { question: "L'internet à la maison ?", options: [
          { label: "Il me faut la fibre, télétravail compris", points: [0, 1, 2],
            raison: "le pack mobile + fibre revient moins cher que deux contrats séparés" },
          { label: "Le partage de connexion mobile me suffit pour l'instant", points: [2, 1, 0],
            raison: "en attendant le logement définitif, un forfait généreux en data remplace la box" }
        ]},
        { question: "Un engagement de durée vous gêne ?", options: [
          { label: "Oui, je veux rester libre", points: [2, 1, 0],
            raison: "sans engagement, on renégocie ou on part dès qu'une meilleure offre sort" },
          { label: "Non, si le prix est meilleur", points: [1, 2, 2],
            raison: "l'engagement se paie d'une remise, acceptable si l'offre reste bonne sur la durée" }
        ]}
      ]
    },
    {
      id: "electricite",
      titre: "Électricité",
      sousTitre: "Prix fixe ou variable, origine verte : trois logiques",
      fiche: null,
      offres: ["Contrat A", "Contrat B", "Contrat C"],
      positionnement: ["Prix fixé 24 mois", "Prix variable, suit le marché", "100 % renouvelable certifiée"],
      criteres: [
        { nom: "Prix du kWh indicatif", valeurs: ["0,17 €, fixé 24 mois", "0,15 €, révisable", "0,19 €, fixé 12 mois"] },
        { nom: "Abonnement mensuel", valeurs: ["6 €", "4 €", "7 €"] },
        { nom: "Origine renouvelable", valeurs: ["30 %", "Selon le marché", "100 % certifiée"] },
        { nom: "Engagement", valeurs: ["24 mois", "Sans", "12 mois"] },
        { nom: "Résiliation", valeurs: ["Gratuite au terme", "Gratuite à tout moment", "Gratuite au terme"] }
      ],
      questions: [
        { question: "Votre logement ?", options: [
          { label: "Appartement, consommation modérée", points: [1, 2, 1],
            raison: "sur une petite consommation, l'écart de prix au kWh pèse peu, la souplesse compte plus" },
          { label: "Maison", points: [2, 1, 1],
            raison: "sur une grosse consommation, un prix fixé protège le budget" },
          { label: "Maison avec voiture électrique", points: [2, 1, 2],
            raison: "avec une recharge quotidienne, le prix du kWh devient le premier poste" }
        ]},
        { question: "Prix stable ou prix du marché ?", options: [
          { label: "Je veux du prévisible", points: [2, 0, 1],
            raison: "un prix fixé sur la durée met le budget à l'abri des hausses" },
          { label: "Je préfère suivre le marché, à la hausse comme à la baisse", points: [0, 2, 0],
            raison: "un prix variable profite des baisses, en acceptant le risque inverse" }
        ]},
        { question: "L'origine renouvelable ?", options: [
          { label: "Décisive", points: [0, 0, 2],
            raison: "seule une origine 100 % certifiée répond à ce critère" },
          { label: "Un plus, si le prix reste correct", points: [1, 1, 2],
            raison: "l'écart de prix du contrat vert reste modéré sur une consommation courante" },
          { label: "Pas un critère", points: [2, 2, 0],
            raison: "sans ce critère, le prix et la souplesse départagent" }
        ]}
      ]
    }
  ]
};
