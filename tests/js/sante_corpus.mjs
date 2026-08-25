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
// Ce que la table fait varier, et rien d'autre : `gate_profile`, `gate_cases` et
// `gate_countersigned` (ce dernier depuis la revue Codex 1.10 tour 2). Tout le reste de chaque corps
// est conforme, parce que les deux lecteurs n'ont pas le même périmètre — l'accueil affiche `ok`,
// `version`, `documents_servis` et `alerts`, le badge ne lit que le triplet. Ne varier que ce qu'ils
// lisent tous les deux est ce qui rend leurs verdicts comparables.
//
// Le fichier porte une **seconde** table depuis la story 2.1, `CORPS_DICTIONNAIRE`, qui fait varier
// `dictionary` et n'est rejouée que par l'accueil : c'est la conséquence directe de la phrase
// ci-dessus — le badge du guide ne lit pas ce champ, et confronter les deux lecteurs sur un
// périmètre qu'un seul couvre ne mesurerait aucune divergence.

/** Un corps conforme, tel que `routes/sante.py` l'écrit. */
export function santeConforme(extra) {
  return Object.assign({
    ok: true,
    version: "b79ca1b",
    documents_servis: ["axa-lu-optihome-2017", "lux-guide"],
    gate_profile: "vertical",
    gate_cases: 2,
    gate_countersigned: false,
    // AD-5 (story 2.1) : `EtatDictionnaire` a **trois** champs, tous sérialisés par
    // `routes/sante.py`. Le corps de référence porte donc les trois, et l'état publié est celui que
    // le service écrit **aujourd'hui** : `data/dictionary.json` est livré et ses `corpus_source_hashes`
    // décrivent le corpus servi (`corpus_ok: true`), mais la validation humaine est due, donc le
    // refus « zéro hit » dort. Un test compare ce corps à celui que la route rend réellement : un
    // corps de référence qui décrit un dépôt révolu fait passer des cas que la production ne voit
    // jamais (revue coordonnée 2.1).
    dictionary: { validated: false, corpus_ok: true, refus_zero_hit_actif: false },
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
  beaucoup_de_cas: { corps: santeConforme({ gate_profile: "full", gate_cases: 47,
                                            gate_countersigned: true }), lisible: true },
  sans_gate: { corps: santeConforme({ gate_profile: null, gate_cases: null,
                                     gate_countersigned: null }), lisible: true },
  contresigne: { corps: santeConforme({ gate_countersigned: true }), lisible: true },

  // --- refusés : des corps qu'aucune route n'a pu écrire -------------------
  profil_absent: { corps: (() => { const c = santeConforme(); delete c.gate_profile; return c; })(),
                   lisible: false },
  compte_absent: { corps: (() => { const c = santeConforme(); delete c.gate_cases; return c; })(),
                   lisible: false },
  // `EtatApp.gate_cases` rend `null` dès que `gate_profile` l'est : les dissocier est impossible.
  profil_sans_compte: { corps: santeConforme({ gate_cases: null }), lisible: false },
  compte_sans_profil: { corps: santeConforme({ gate_profile: null, gate_countersigned: null }),
                        lisible: false },
  // `gate_countersigned` (revue Codex 1.10 tour 2) : c'est lui qui décide si l'accueil écrit
  // « relus à la main ». Absent, ou dissocié du profil, la page devrait choisir entre deux phrases
  // dont l'une affirme une relecture humaine — c'est-à-dire l'inventer.
  contresignature_absente: {
    corps: (() => { const c = santeConforme(); delete c.gate_countersigned; return c; })(),
    lisible: false },
  profil_sans_contresignature: { corps: santeConforme({ gate_countersigned: null }),
                                 lisible: false },
  contresignature_sans_profil: { corps: santeConforme({ gate_profile: null, gate_cases: null }),
                                 lisible: false },
  contresignature_non_booleenne: { corps: santeConforme({ gate_countersigned: "true" }),
                                   lisible: false },
  contresignature_numerique: { corps: santeConforme({ gate_countersigned: 1 }), lisible: false },
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

/**
 * Les corps qui font varier `dictionary` (AD-5, story 2.1) — table **de l'accueil seul**.
 *
 * Pourquoi elle n'est pas dans `CORPS_PARTAGES` : cette table-là existe pour confronter les deux
 * lecteurs stricts sur ce qu'ils lisent **tous les deux**, et son en-tête le dit — « ne varier que
 * ce qu'ils lisent tous les deux est ce qui rend leurs verdicts comparables ». `web/app/chat.js`
 * ne lit pas `dictionary` : le badge du guide affiche le niveau de gate, pas l'état du refus. Y
 * verser ces corps ferait donc rougir `test_le_front_du_guide_et_laccueil_jugent_les_memes_corps_pareil`
 * sur un désaccord qui n'en est pas un — deux périmètres différents, pas deux règles divergentes —
 * et la seule façon de le faire passer serait d'apprendre au badge à refuser un corps sur un champ
 * qu'il n'affiche pas.
 *
 * Ce qu'elle exige, elle, est la même discipline : `routes/sante.py` sérialise toujours les trois
 * champs d'`EtatDictionnaire` (tous ont un défaut), donc un corps qui en ampute un n'a été écrit
 * par aucune route — c'est une sonde illisible, jamais un refus qu'on annoncerait désarmé sur la
 * foi d'une clé manquante.
 */
function sansDictionnaire(mutation) {
  const c = santeConforme();
  mutation(c);
  return c;
}

export const CORPS_DICTIONNAIRE = {
  // --- conformes : les quatre états que le serveur peut écrire ---------------
  // Le nominal du dépôt : chargé, conforme au corpus servi, signé par personne.
  dictionnaire_non_signe: { corps: santeConforme(), lisible: true },
  dictionnaire_arme: {
    corps: santeConforme({
      dictionary: { validated: true, corpus_ok: true, refus_zero_hit_actif: true } }),
    lisible: true },
  dictionnaire_absent: {
    corps: santeConforme({
      dictionary: { validated: false, corpus_ok: false, refus_zero_hit_actif: false } }),
    lisible: true },
  // `validated` et `corpus_ok` sont deux **faits** distincts : une signature posée sur un fichier
  // qu'une réingestion a depuis rendu étranger au corpus est un corps que la route écrit, et une
  // sonde parfaitement lisible.
  dictionnaire_signe_hors_corpus: {
    corps: santeConforme({
      dictionary: { validated: true, corpus_ok: false, refus_zero_hit_actif: false } }),
    lisible: true },

  // --- refusés : des corps qu'aucune route n'a pu écrire --------------------
  dictionary_absent: { corps: sansDictionnaire((c) => { delete c.dictionary; }), lisible: false },
  dictionary_nul: { corps: santeConforme({ dictionary: null }), lisible: false },
  dictionary_chaine: { corps: santeConforme({ dictionary: "non validé" }), lisible: false },
  dictionary_booleen: { corps: santeConforme({ dictionary: false }), lisible: false },
  dictionary_tableau: { corps: santeConforme({ dictionary: [] }), lisible: false },
  validated_absent: {
    corps: sansDictionnaire((c) => { delete c.dictionary.validated; }), lisible: false },
  // `validated: "true"` est exactement ce que la matrice d'E/S de la story appelle « fichier bricolé
  // à la main » : le serveur le traite comme `false`, il ne le republie jamais en chaîne.
  validated_chaine: {
    corps: sansDictionnaire((c) => { c.dictionary.validated = "true"; }), lisible: false },
  corpus_ok_absent: {
    corps: sansDictionnaire((c) => { delete c.dictionary.corpus_ok; }), lisible: false },
  corpus_ok_numerique: {
    corps: sansDictionnaire((c) => { c.dictionary.corpus_ok = 1; }), lisible: false },
  refus_absent: {
    corps: sansDictionnaire((c) => { delete c.dictionary.refus_zero_hit_actif; }), lisible: false },
  refus_chaine: {
    corps: sansDictionnaire((c) => { c.dictionary.refus_zero_hit_actif = "oui"; }), lisible: false },
  refus_nul: {
    corps: sansDictionnaire((c) => { c.dictionary.refus_zero_hit_actif = null; }), lisible: false },
};
