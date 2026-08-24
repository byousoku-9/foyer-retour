// La table **unique** des corps de `/api/v1/sante` que les deux lecteurs stricts doivent juger de la
// même façon : `tools/accueil/accueil.js::lireSante` et `web/app/chat.js::lireValidation`.
//
// Pourquoi elle existe (revue 1.10) : le test qui prétendait empêcher la divergence des deux
// contrats grepait deux littéraux dans les deux sources. Il serait resté vert pendant que l'accueil
// acceptait `gate_profile: ""` et un `gate_cases` négatif que le badge refusait — c'est-à-dire
// pendant la divergence même qu'il existait pour empêcher. Un grep sur du texte source ne vérifie
// pas une sémantique : les corps sont donc **rejoués** dans les deux lecteurs, et le verdict est
// comparé corps par corps.
//
// Ce que la table fait varier, et rien d'autre : `gate_profile` et `gate_cases`. Tout le reste de
// chaque corps est conforme, parce que les deux lecteurs n'ont pas le même périmètre — l'accueil
// affiche `ok`, `version`, `documents_servis` et `alerts`, le badge ne lit que le couple. Ne varier
// que ce qu'ils lisent tous les deux est ce qui rend leurs verdicts comparables.

/** Un corps conforme, tel que `routes/sante.py` l'écrit. */
export function santeConforme(extra) {
  return Object.assign({
    ok: true,
    version: "b79ca1b",
    documents_servis: ["axa-lu-optihome-2017", "lux-guide"],
    gate_profile: "vertical",
    gate_cases: 2,
    dictionary: { validated: false },
    alerts: [],
    thresholds: { deadline_s: 55.0 },
  }, extra || {});
}

/**
 * `nom → {corps, lisible}` — `lisible: true` = les deux lecteurs doivent retenir le couple ;
 * `false` = les deux doivent le refuser (l'accueil peint l'état 3, le badge ne suffixe rien).
 */
export const CORPS_PARTAGES = {
  // --- conformes : le garde-fou du durcissement ----------------------------
  // Un lecteur trop strict n'afficherait plus jamais de niveau. `null` est une valeur de plein droit
  // pour les deux champs `X | None` du contrat, et un compte à 1 est le cas d'un seul document gaté.
  nominal: { corps: santeConforme(), lisible: true },
  un_seul_cas: { corps: santeConforme({ gate_cases: 1 }), lisible: true },
  beaucoup_de_cas: { corps: santeConforme({ gate_profile: "full", gate_cases: 47 }), lisible: true },
  sans_gate: { corps: santeConforme({ gate_profile: null, gate_cases: null }), lisible: true },

  // --- refusés : des corps qu'aucune route n'a pu écrire -------------------
  profil_absent: { corps: (() => { const c = santeConforme(); delete c.gate_profile; return c; })(),
                   lisible: false },
  compte_absent: { corps: (() => { const c = santeConforme(); delete c.gate_cases; return c; })(),
                   lisible: false },
  // `EtatApp.gate_cases` rend `null` dès que `gate_profile` l'est : les dissocier est impossible.
  profil_sans_compte: { corps: santeConforme({ gate_cases: null }), lisible: false },
  compte_sans_profil: { corps: santeConforme({ gate_profile: null }), lisible: false },
  // « niveau de validation :  — 2 cas » / « mode api ·  (2 cas) » ne disent rien.
  gate_profile_vide: { corps: santeConforme({ gate_profile: "" }), lisible: false },
  gate_profile_non_chaine: { corps: santeConforme({ gate_profile: 2 }), lisible: false },
  // `evals/run.py` refuse de tourner sur zéro cas : aucun gate ne porte `cases: 0`.
  gate_cases_zero: { corps: santeConforme({ gate_cases: 0 }), lisible: false },
  gate_cases_negatif: { corps: santeConforme({ gate_cases: -3 }), lisible: false },
  gate_cases_fractionnaire: { corps: santeConforme({ gate_cases: 1.5 }), lisible: false },
  gate_cases_chaine: { corps: santeConforme({ gate_cases: "2" }), lisible: false },
  gate_cases_nan: { corps: santeConforme({ gate_cases: Number.NaN }), lisible: false },
};
