// Mini base de connaissances de test : deux fiches, une table, deux FAQ, une timeline.
/* Les constructions de kb.js : clés non citées, virgules finales,
   échappements JSON, nombres, booléens, null. */
window.KB = {
  meta: { verifie: "2026-08", version: 3, actif: true, rien: null, ratio: 1.5, },

  fiches: [
    {
      id: "arrivee",
      titre: "Les huit premiers jours",
      cat: "Administratif",
      resume: "Tout part de la commune.",
      tags: ["arrivée", "commune"],
      corps: [
        "Vous disposez de huit jours pour déclarer votre arrivée au Biergercenter de la commune.",
        { h: "Le matricule" },
        "Le certificat porte votre matricule, délivré par la commune.", // commentaire en fin de ligne
        "Il dit \"bonjour\"\tavec une tabulation et un \\ antislash.",
      ],
      tableaux: [{
        titre: "Délais",
        colonnes: ["Démarche", "Délai"],
        lignes: [["Déclaration d'arrivée", "8 jours"], ["Matricule", "immédiat"]],
      }],
      aRetenir: ["Huit jours pour la commune.", "Le matricule suit."],
      sources: [{ t: "Guichet.lu", u: "https://guichet.public.lu/" }],
    },
    {
      id: "bail_test",
      titre: "Signer un bail",
      cat: "Logement",
      resume: "Caution plafonnée.",
      tags: [],
      corps: ["La caution est plafonnée à deux mois de loyer."],
      aRetenir: [],
      sources: [],
    },
  ],

  faq: [
    { q: "Quel délai pour la commune ?", a: "Huit jours après l'emménagement.", fiche: "arrivee" },
    { q: "Et la caution ?", a: "Deux mois au maximum.", fiche: "bail_test" },
  ],

  // Story 2.3 : seules les conditions `si` sont ingérées (Document.parcours), jamais le texte `t`.
  // Une étape sans `si` (le cas nominal, 29 des 38 étapes du guide réel) est ignorée ; deux étapes
  // conditionnent ici deux fiches distinctes, et une troisième reconditionne la même fiche pour que
  // la déduplication par node_id soit exercée.
  timeline: [
    { phase: "Semaine 1", items: [
      { t: "Déclarer l'arrivée.", fiche: "arrivee" },
      { t: "Préparer le dossier de location.", fiche: "bail_test", si: { logement: "Louer" } },
      { t: "Inscrire les enfants à l'école.", fiche: "arrivee", si: { enfants: true } },
      { t: "Relire le bail avant de signer.", fiche: "bail_test", si: { logement: "Louer" } }
    ]}
  ]
};
